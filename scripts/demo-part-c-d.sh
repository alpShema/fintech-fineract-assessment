#!/bin/bash
# scripts/demo-part-c-d.sh
#
# Part C (identities) + Part D (loan lifecycle), scripted and re-runnable.
# Requires: docker compose up already run, Fineract healthy,
# scripts/setup-fineract-environment.sh already run (needs the Kigali office
# and SME Asset Finance loan product to exist).
#
# Part C demonstrates: create client -> find by client_uid -> validate
# (exists, active, correct name). Uses Fineract's native
# GET /clients/external-id/{externalId} lookup -- client_uid is never a
# Fineract internal ID, it's stored as the client's externalId.
#
# Part D runs one loan end to end: application -> approval -> disbursement
# -> 3 partial repayments -> final payoff -> Closed (obligations met).
# loan_reference is likewise stored as the loan's externalId, looked up via
# GET /loans/external-id/{externalId}.
#
# Usage: scripts/demo-part-c-d.sh [client_uid] [loan_reference]
# (both default to freshly generated values each run, so this is safe to
# re-run repeatedly without colliding with a previous run.)

set -euo pipefail

BASE="https://localhost:8443/fineract-provider/api/v1"
AUTH=(-u mifos:password -H "Fineract-Platform-TenantId: default")
JSON=(-H "Content-Type: application/json")

get() { curl -sk "${AUTH[@]}" "$BASE$1"; }
post() { curl -sk "${AUTH[@]}" "${JSON[@]}" -X POST "$BASE$1" -d "$2"; }

json_get() { python -c "import json,sys; print(json.load(sys.stdin)$1)"; }

hr() { printf '%.0s-' {1..70}; echo; }

SUFFIX=$(date +%s)
CLIENT_UID="${1:-SME-DEMO-$SUFFIX}"
LOAN_REF="${2:-LN-DEMO-$SUFFIX}"
KIGALI_OFFICE_ID=2
SME_PRODUCT_ID=1

echo "Part C + D demo -- client_uid=$CLIENT_UID, loan_reference=$LOAN_REF"
hr

echo "=== PART C: Client identity ==="
echo "STEP 1: initial state -- no client exists yet with client_uid='$CLIENT_UID'"
echo "STEP 2: request -- create client, storing client_uid as Fineract's externalId"
CLIENT_RESP=$(post "/clients" "{
  \"officeId\": $KIGALI_OFFICE_ID,
  \"firstname\": \"Amina\",
  \"lastname\": \"Uwase\",
  \"externalId\": \"$CLIENT_UID\",
  \"legalFormId\": 1,
  \"active\": true,
  \"activationDate\": \"01 January 2026\",
  \"dateFormat\": \"dd MMMM yyyy\",
  \"locale\": \"en\"
}")
CLIENT_ID=$(echo "$CLIENT_RESP" | json_get "['resourceId']" 2>/dev/null || echo "")
echo "STEP 3: response -- $CLIENT_RESP"
if [ -z "$CLIENT_ID" ]; then
  echo "  EXPLANATION: FAILED to create client" >&2
  exit 1
fi

echo "STEP 2b: request -- find by client_uid (GET /clients/external-id/{client_uid}), not Fineract's internal id"
LOOKUP=$(get "/clients/external-id/$CLIENT_UID")
STATUS=$(echo "$LOOKUP" | json_get "['status']['value']")
NAME=$(echo "$LOOKUP" | json_get "['displayName']")
echo "STEP 3b: response -- status=$STATUS, displayName=$NAME"
echo "STEP 7: expected result -- exists, status=Active, displayName='Amina Uwase'"
echo "STEP 8: actual result -- status=$STATUS, displayName=$NAME"
if [ "$STATUS" = "Active" ] && [ "$NAME" = "Amina Uwase" ]; then
  echo "  EXPLANATION: PASS -- client found by client_uid alone, active, correct name"
else
  echo "  EXPLANATION: FAIL" >&2
  exit 1
fi
hr

echo "=== PART D: Loan lifecycle ==="
echo "STEP 1: initial state -- client '$CLIENT_UID' active, no loans yet"
echo "STEP 2: request -- loan application (5,000,000 RWF, 18% p.a., 12 monthly repayments)"
LOAN_RESP=$(post "/loans" "{
  \"clientId\": $CLIENT_ID,
  \"productId\": $SME_PRODUCT_ID,
  \"loanType\": \"individual\",
  \"principal\": 5000000,
  \"loanTermFrequency\": 12,
  \"loanTermFrequencyType\": 2,
  \"numberOfRepayments\": 12,
  \"repaymentEvery\": 1,
  \"repaymentFrequencyType\": 2,
  \"interestRatePerPeriod\": 18,
  \"interestType\": 0,
  \"amortizationType\": 1,
  \"interestCalculationPeriodType\": 1,
  \"transactionProcessingStrategyCode\": \"mifos-standard-strategy\",
  \"expectedDisbursementDate\": \"15 January 2026\",
  \"submittedOnDate\": \"15 January 2026\",
  \"externalId\": \"$LOAN_REF\",
  \"dateFormat\": \"dd MMMM yyyy\",
  \"locale\": \"en\"
}")
LOAN_ID=$(echo "$LOAN_RESP" | json_get "['resourceId']" 2>/dev/null || echo "")
echo "STEP 3: response -- $LOAN_RESP"
if [ -z "$LOAN_ID" ]; then
  echo "  EXPLANATION: FAILED to create loan application" >&2
  exit 1
fi
STATUS=$(get "/loans/$LOAN_ID" | json_get "['status']['value']")
echo "STEP 4: Fineract state -- status=$STATUS"
hr

echo "STEP 2: request -- approve (command=approve)"
post "/loans/$LOAN_ID?command=approve" "{\"approvedOnDate\":\"15 January 2026\",\"expectedDisbursementDate\":\"15 January 2026\",\"dateFormat\":\"dd MMMM yyyy\",\"locale\":\"en\"}" > /dev/null
STATUS=$(get "/loans/$LOAN_ID" | json_get "['status']['value']")
echo "STEP 4: Fineract state -- status=$STATUS"
hr

echo "STEP 2: request -- disburse (command=disburse)"
post "/loans/$LOAN_ID?command=disburse" "{\"actualDisbursementDate\":\"15 January 2026\",\"dateFormat\":\"dd MMMM yyyy\",\"locale\":\"en\"}" > /dev/null
LOAN_STATE=$(get "/loans/$LOAN_ID")
STATUS=$(echo "$LOAN_STATE" | json_get "['status']['value']")
PRINCIPAL_OUT=$(echo "$LOAN_STATE" | json_get "['summary']['principalOutstanding']")
echo "STEP 4: Fineract state -- status=$STATUS, principalOutstanding=$PRINCIPAL_OUT"
hr

echo "=== 3 partial repayments ==="
DATES=("15 February 2026" "15 March 2026" "15 April 2026")
for i in 0 1 2; do
  DATE="${DATES[$i]}"
  echo "STEP 2: request -- repayment $((i+1)) of 458400 RWF on $DATE"
  post "/loans/$LOAN_ID/transactions?command=repayment" "{\"transactionDate\":\"$DATE\",\"transactionAmount\":458400,\"dateFormat\":\"dd MMMM yyyy\",\"locale\":\"en\"}" > /dev/null
  LOAN_STATE=$(get "/loans/$LOAN_ID")
  TOTAL_OUT=$(echo "$LOAN_STATE" | json_get "['summary']['totalOutstanding']")
  echo "STEP 4: Fineract state -- totalOutstanding=$TOTAL_OUT"
done
hr

echo "=== Final payoff ==="
FINAL_AMOUNT=$(get "/loans/$LOAN_ID" | json_get "['summary']['totalOutstanding']")
echo "STEP 2: request -- final repayment of remaining balance ($FINAL_AMOUNT RWF) on 20 April 2026"
post "/loans/$LOAN_ID/transactions?command=repayment" "{\"transactionDate\":\"20 April 2026\",\"transactionAmount\":$FINAL_AMOUNT,\"dateFormat\":\"dd MMMM yyyy\",\"locale\":\"en\"}" > /dev/null
LOAN_STATE=$(get "/loans/$LOAN_ID")
STATUS=$(echo "$LOAN_STATE" | json_get "['status']['value']")
PRINCIPAL_OUT=$(echo "$LOAN_STATE" | json_get "['summary']['principalOutstanding']")
TOTAL_OUT=$(echo "$LOAN_STATE" | json_get "['summary']['totalOutstanding']")
echo "STEP 3/4: Fineract state -- status=$STATUS, principalOutstanding=$PRINCIPAL_OUT, totalOutstanding=$TOTAL_OUT"
echo "STEP 7: expected result -- status=Closed (obligations met), outstanding=0"
echo "STEP 8: actual result -- status=$STATUS, principalOutstanding=$PRINCIPAL_OUT, totalOutstanding=$TOTAL_OUT"
if [ "$STATUS" = "Closed (obligations met)" ] && [ "$PRINCIPAL_OUT" = "0.0" ]; then
  echo "  EXPLANATION: PASS -- full loan lifecycle reached Closed (obligations met), zero outstanding"
else
  echo "  EXPLANATION: FAIL" >&2
  exit 1
fi
hr

echo "=== Lookup by loan_reference (not Fineract's internal loan id) ==="
LOOKUP=$(get "/loans/external-id/$LOAN_REF")
echo "$LOOKUP" | python -c "
import json,sys
d = json.load(sys.stdin)
print('externalId (loan_reference):', d.get('externalId'))
print('clientExternalId (client_uid):', d.get('clientExternalId'))
print('status:', d['status']['value'])
"
hr
echo "PART C + D COMPLETE for client_uid=$CLIENT_UID, loan_reference=$LOAN_REF"
