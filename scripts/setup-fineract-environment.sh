#!/bin/bash
# scripts/setup-fineract-environment.sh
#
# Part B: Fineract environment configuration. Scripted and repeatable, per the
# brief ("Setup must be scripted and repeatable"). Requires: docker compose up
# already run, Fineract container healthy, Part A patch applied (GL account
# sync with external_reference).
#
# Creates, in order (order matters -- GL accounts must exist before the loan
# product references them, per the brief):
#   1. Enable RWF as a usable currency
#   2. Three GL accounts via the Part A sync endpoint (Bank, Loan Receivable,
#      Interest Income), matching the chart-of-accounts codes Part G's Mock
#      ERP will later seed (1100/1200/4000)
#   3. Office "Kigali Main Office" (child of the default Head Office)
#   4. Loan product "SME Asset Finance": 18% p.a., monthly repayments, 12
#      months, single-disbursement, cash-based accounting, wired to the GL
#      accounts from step 2
#   5. A least-privilege role + API user for the Integration Service
#
# Safe to re-run: every step checks for the thing it creates first and skips
# if already present (idempotent), rather than erroring on a second run.

set -euo pipefail

BASE="https://localhost:8443/fineract-provider/api/v1"
AUTH=(-u mifos:password -H "Fineract-Platform-TenantId: default")
JSON=(-H "Content-Type: application/json")

get() { curl -sk "${AUTH[@]}" "$BASE$1"; }
post() { curl -sk "${AUTH[@]}" "${JSON[@]}" -X POST "$BASE$1" -d "$2"; }
put() { curl -sk "${AUTH[@]}" "${JSON[@]}" -X PUT "$BASE$1" -d "$2"; }

json_get() { python -c "import json,sys; print(json.load(sys.stdin)$1)"; }

hr() { printf '%.0s-' {1..70}; echo; }

echo "=== Part B: Fineract environment setup ==="
hr

echo "1. Enabling RWF currency..."
put "/currencies" '{"currencies":["USD","RWF"]}' > /dev/null
echo "   done."
hr

echo "2. Creating GL accounts (Bank, Loan Receivable, Interest Income) via Part A sync endpoint..."

create_gl_account_if_missing() {
  local name="$1" glcode="$2" type="$3" extref="$4"
  local existing
  existing=$(get "/glaccounts" | python -c "
import json,sys
accts = json.load(sys.stdin)
match = [a['id'] for a in accts if a.get('glCode') == '$glcode']
print(match[0] if match else '')
")
  if [ -n "$existing" ]; then
    echo "   '$name' ($glcode) already exists as id=$existing, skipping"
    echo "$existing"
    return
  fi
  local resp id ext_field
  # Omit externalReference entirely when empty rather than sending "" -- an empty
  # string is a real, DB-comparable value (unlike NULL, which the UNIQUE constraint
  # exempts from collision), so two accounts both sent with "" would violate
  # uq_acc_gl_account_external_reference against each other.
  ext_field=""
  if [ -n "$extref" ]; then
    ext_field=",\"externalReference\":\"$extref\""
  fi
  resp=$(post "/glaccounts" "{\"name\":\"$name\",\"glCode\":\"$glcode\",\"type\":$type,\"usage\":1,\"manualEntriesAllowed\":true$ext_field}")
  id=$(echo "$resp" | json_get "['resourceId']" 2>/dev/null || echo "")
  if [ -z "$id" ]; then
    echo "   FAILED creating '$name': $resp" >&2
    exit 1
  fi
  echo "   created '$name' ($glcode) as id=$id"
  echo "$id"
}

BANK_ID=$(create_gl_account_if_missing "Bank" "1100" 1 "GL-1100" | tail -1)
LOAN_RECEIVABLE_ID=$(create_gl_account_if_missing "Loan Receivable" "1200" 1 "GL-1200" | tail -1)
INTEREST_INCOME_ID=$(create_gl_account_if_missing "Interest Income" "4000" 4 "GL-4000" | tail -1)
# Fineract requires these two mappings even when fees/penalties/write-off/overpayment
# are out of scope (per the brief) -- the loan product API validates they're present
# and of the right account type (expense / liability respectively), even if never
# actually exercised. Not ERP-synced accounts (no external_reference): these are
# Fineract-only bookkeeping placeholders, not part of the ERP's chart of accounts.
WRITEOFF_ID=$(create_gl_account_if_missing "Loan Losses Written Off" "5000" 5 "" | tail -1)
OVERPAYMENT_ID=$(create_gl_account_if_missing "Loan Overpayments" "2100" 2 "" | tail -1)
echo "   Bank=$BANK_ID, Loan Receivable=$LOAN_RECEIVABLE_ID, Interest Income=$INTEREST_INCOME_ID, Write-off=$WRITEOFF_ID, Overpayment=$OVERPAYMENT_ID"
hr

echo "3. Creating office 'Kigali Main Office'..."
OFFICE_ID=$(get "/offices" | python -c "
import json,sys
offices = json.load(sys.stdin)
match = [o['id'] for o in offices if o['name'] == 'Kigali Main Office']
print(match[0] if match else '')
")
if [ -n "$OFFICE_ID" ]; then
  echo "   already exists as id=$OFFICE_ID, skipping"
else
  RESP=$(post "/offices" '{"name":"Kigali Main Office","parentId":1,"openingDate":"01 January 2024","dateFormat":"dd MMMM yyyy","locale":"en"}')
  OFFICE_ID=$(echo "$RESP" | json_get "['resourceId']")
  echo "   created as id=$OFFICE_ID"
fi
hr

echo "4. Creating loan product 'SME Asset Finance'..."
EXISTING_PRODUCT=$(get "/loanproducts" | python -c "
import json,sys
prods = json.load(sys.stdin)
match = [p['id'] for p in prods if p['name'] == 'SME Asset Finance']
print(match[0] if match else '')
")
if [ -n "$EXISTING_PRODUCT" ]; then
  echo "   already exists as id=$EXISTING_PRODUCT, skipping"
else
  PRODUCT_PAYLOAD=$(cat <<EOF
{
  "name": "SME Asset Finance",
  "shortName": "SMEF",
  "description": "SME asset financing, single disbursement, 18% p.a., 12 monthly repayments",
  "currencyCode": "RWF",
  "digitsAfterDecimal": 0,
  "inMultiplesOf": 0,
  "principal": 5000000,
  "minPrincipal": 500000,
  "maxPrincipal": 20000000,
  "numberOfRepayments": 12,
  "minNumberOfRepayments": 6,
  "maxNumberOfRepayments": 24,
  "repaymentEvery": 1,
  "repaymentFrequencyType": 2,
  "interestRatePerPeriod": 18,
  "minInterestRatePerPeriod": 10,
  "maxInterestRatePerPeriod": 30,
  "interestRateFrequencyType": 3,
  "amortizationType": 1,
  "interestType": 0,
  "interestCalculationPeriodType": 1,
  "transactionProcessingStrategyCode": "mifos-standard-strategy",
  "daysInYearType": 365,
  "daysInMonthType": 1,
  "accountingRule": 2,
  "isInterestRecalculationEnabled": false,
  "fundSourceAccountId": $BANK_ID,
  "loanPortfolioAccountId": $LOAN_RECEIVABLE_ID,
  "interestOnLoanAccountId": $INTEREST_INCOME_ID,
  "incomeFromFeeAccountId": $INTEREST_INCOME_ID,
  "incomeFromPenaltyAccountId": $INTEREST_INCOME_ID,
  "incomeFromRecoveryAccountId": $INTEREST_INCOME_ID,
  "writeOffAccountId": $WRITEOFF_ID,
  "overpaymentLiabilityAccountId": $OVERPAYMENT_ID,
  "transfersInSuspenseAccountId": $BANK_ID,
  "multiDisburseLoan": false,
  "allowAttributeOverrides": {
    "amortizationType": true, "interestType": true, "transactionProcessingStrategyCode": true,
    "interestCalculationPeriodType": true, "inArrearsTolerance": true, "repaymentEvery": true,
    "graceOnPrincipalAndInterestPayment": true, "graceOnArrearsAgeing": true
  },
  "locale": "en",
  "dateFormat": "dd MMMM yyyy"
}
EOF
)
  RESP=$(post "/loanproducts" "$PRODUCT_PAYLOAD")
  EXISTING_PRODUCT=$(echo "$RESP" | json_get "['resourceId']" 2>/dev/null || echo "")
  if [ -z "$EXISTING_PRODUCT" ]; then
    echo "   FAILED: $RESP" >&2
    exit 1
  fi
  echo "   created as id=$EXISTING_PRODUCT"
fi
hr

echo "5. Creating least-privilege role + API user for the Integration Service..."
ROLE_ID=$(get "/roles" | python -c "
import json,sys
roles = json.load(sys.stdin)
match = [r['id'] for r in roles if r['name'] == 'Integration Service']
print(match[0] if match else '')
")
if [ -n "$ROLE_ID" ]; then
  echo "   role already exists as id=$ROLE_ID, skipping role creation"
else
  RESP=$(post "/roles" '{"name":"Integration Service","description":"Least-privilege role for the Integration Service API user: GL account sync, client and loan lifecycle only."}')
  ROLE_ID=$(echo "$RESP" | json_get "['resourceId']")
  echo "   role created as id=$ROLE_ID"

  # Grant exactly the permissions the Integration Service needs: GL account
  # sync (Part A), client lifecycle, loan application/approval/disbursement/
  # repayment (Parts C/D) -- nothing else (no user/role management, no
  # organisation-wide config, no other products' permissions).
  #
  # NOTE: the API wants a single FLAT "permissions" object -- grouping by
  # category (as GET /roles/{id}/permissions *displays* them) is rejected with
  # "parameter <grouping> is not supported". Learned this by having the first
  # attempt silently "succeed" (no error surfaced because the response was
  # piped to /dev/null) while actually applying zero permissions -- the >
  # /dev/null below was the bug, not just this payload shape.
  PERMS='{"permissions":{"CREATE_GLACCOUNT":true,"READ_GLACCOUNT":true,"CREATE_CLIENT":true,"READ_CLIENT":true,"ACTIVATE_CLIENT":true,"CREATE_LOAN":true,"READ_LOAN":true,"APPROVE_LOAN":true,"DISBURSE_LOAN":true,"REPAYMENT_LOAN":true}}'
  PERMS_RESP=$(put "/roles/$ROLE_ID/permissions" "$PERMS")
  if ! echo "$PERMS_RESP" | grep -q '"resourceId"'; then
    echo "   FAILED setting permissions: $PERMS_RESP" >&2
    exit 1
  fi
  echo "   permissions granted: CREATE/READ_GLACCOUNT, CREATE/READ/ACTIVATE_CLIENT, CREATE/READ_LOAN, APPROVE/DISBURSE/REPAYMENT_LOAN"
fi

USER_EXISTS=$(get "/users" | python -c "
import json,sys
users = json.load(sys.stdin)
match = [u['id'] for u in users if u['username'] == 'integration-service']
print(match[0] if match else '')
")
if [ -n "$USER_EXISTS" ]; then
  echo "   user 'integration-service' already exists as id=$USER_EXISTS, skipping"
else
  USER_PAYLOAD=$(cat <<EOF
{
  "username": "integration-service",
  "firstname": "Integration",
  "lastname": "Service",
  "email": "integration-service@example.invalid",
  "officeId": $OFFICE_ID,
  "roles": [$ROLE_ID],
  "sendPasswordToEmail": false,
  "password": "IntegrationService!2024",
  "repeatPassword": "IntegrationService!2024"
}
EOF
)
  RESP=$(post "/users" "$USER_PAYLOAD")
  USER_EXISTS=$(echo "$RESP" | json_get "['resourceId']" 2>/dev/null || echo "")
  if [ -z "$USER_EXISTS" ]; then
    echo "   FAILED: $RESP" >&2
    exit 1
  fi
  echo "   user created as id=$USER_EXISTS (username: integration-service)"
fi
hr

echo "Part B setup complete."
echo "  Office: Kigali Main Office (id=$OFFICE_ID)"
echo "  Loan product: SME Asset Finance (id=$EXISTING_PRODUCT)"
echo "  GL accounts: Bank=$BANK_ID, Loan Receivable=$LOAN_RECEIVABLE_ID, Interest Income=$INTEREST_INCOME_ID"
echo "  Role: Integration Service (id=$ROLE_ID), User: integration-service (id=$USER_EXISTS)"
