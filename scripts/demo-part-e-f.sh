#!/bin/bash
# scripts/demo-part-e-f.sh
#
# Parts E/F: the Integration Service, scripted and re-runnable. Requires:
# docker compose up -d integration-service (depends on db, fineract, mock-erp).

set -euo pipefail

BASE="http://localhost:8002"
KEY="integration-service-partner-key"
TS=$(date +%s)
CLIENT_UID="SME-DEMO-$TS"
LOAN_REF="LN-DEMO-$TS"
TODAY=$(date +%Y-%m-%d)

hr() { printf '%.0s-' {1..70}; echo; }
auth() { curl -s -H "X-Api-Key: $KEY" "$@"; }

echo "=== Parts E/F demo -- Integration Service ==="
hr

echo "STEP 1: health check"
curl -s "$BASE/health"
echo
hr

echo "STEP 2: sync the ERP's chart of accounts into Fineract (GL_ACCOUNT_SYNC)"
auth -X POST "$BASE/api/accounts/sync" -H "Content-Type: application/json" -d '{}' | python -m json.tool
hr

echo "STEP 3: resync -- expect the SAME results, zero new Fineract GL accounts created"
auth -X POST "$BASE/api/accounts/sync" -H "Content-Type: application/json" -d '{}' | python -m json.tool
hr

echo "STEP 4: create client (external_reference REF-DEMO-CLIENT-$TS, client_uid $CLIENT_UID)"
CLIENT_PAYLOAD="{\"external_reference\":\"REF-DEMO-CLIENT-$TS\",\"client_uid\":\"$CLIENT_UID\",\"firstname\":\"Demo\",\"lastname\":\"Borrower\"}"
RESP1=$(curl -s -w '\n%{http_code}' -X POST "$BASE/api/clients" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" -d "$CLIENT_PAYLOAD")
echo "response: $(echo "$RESP1" | sed '$d')"
echo "HTTP $(echo "$RESP1" | tail -1) (expect 201, created)"
hr

echo "STEP 5: resend IDENTICAL client create (expect 200 replay, no second Fineract client)"
RESP2=$(curl -s -w '\n%{http_code}' -X POST "$BASE/api/clients" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" -d "$CLIENT_PAYLOAD")
echo "response: $(echo "$RESP2" | sed '$d')"
echo "HTTP $(echo "$RESP2" | tail -1) (expect 200)"
hr

echo "STEP 6: get client by client_uid"
auth "$BASE/api/clients/$CLIENT_UID"
echo
hr

echo "STEP 7: create + auto-approve loan application (loan_reference $LOAN_REF)"
LOAN_PAYLOAD="{\"external_reference\":\"REF-DEMO-LOAN-$TS\",\"client_uid\":\"$CLIENT_UID\",\"loan_reference\":\"$LOAN_REF\",\"principal\":\"1000000\",\"expected_disbursement_date\":\"$TODAY\"}"
auth -X POST "$BASE/api/loans" -H "Content-Type: application/json" -d "$LOAN_PAYLOAD"
echo
hr

echo "STEP 8: get loan (expect Approved)"
auth "$BASE/api/loans/$LOAN_REF"
echo
hr

echo "STEP 9: disburse loan"
DISB_PAYLOAD="{\"external_reference\":\"REF-DEMO-DISB-$TS\",\"actual_disbursement_date\":\"$TODAY\"}"
RESP3=$(curl -s -w '\n%{http_code}' -X POST "$BASE/api/loans/$LOAN_REF/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" -d "$DISB_PAYLOAD")
echo "response: $(echo "$RESP3" | sed '$d')"
echo "HTTP $(echo "$RESP3" | tail -1) (expect 201)"
hr

echo "STEP 10: re-disburse with a DIFFERENT external_reference, same loan -- the brief's exact scenario"
DISB_RETRY_PAYLOAD="{\"external_reference\":\"REF-DEMO-DISB-$TS-RETRY\",\"actual_disbursement_date\":\"$TODAY\"}"
RESP4=$(curl -s -w '\n%{http_code}' -X POST "$BASE/api/loans/$LOAN_REF/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" -d "$DISB_RETRY_PAYLOAD")
echo "response: $(echo "$RESP4" | sed '$d')"
echo "HTTP $(echo "$RESP4" | tail -1) (expect 200 replay, same fineract_transaction_id as step 9 -- exactly one Fineract disbursement)"
hr

echo "STEP 11: repay loan"
REPAY_PAYLOAD="{\"external_reference\":\"REF-DEMO-REPAY-$TS\",\"transaction_date\":\"$TODAY\",\"transaction_amount\":\"100000\"}"
auth -X POST "$BASE/api/loans/$LOAN_REF/repayments" -H "Content-Type: application/json" -d "$REPAY_PAYLOAD"
echo
hr

echo "STEP 12: get loan after repayment (expect reduced outstanding)"
auth "$BASE/api/loans/$LOAN_REF"
echo
hr

echo "STEP 13: look up a transaction by external_reference (searches across all operation types)"
auth "$BASE/api/transactions/REF-DEMO-LOAN-$TS"
echo
hr

echo "PARTS E/F DEMO COMPLETE"
