#!/bin/bash
# scripts/demo-part-g.sh
#
# Part G: Mock ERP, scripted and re-runnable. Requires: docker compose up -d
# mock-erp already running (depends on db).

set -euo pipefail

BASE="http://localhost:8001"
REF="TXN-DEMO-$(date +%s)"

hr() { printf '%.0s-' {1..70}; echo; }

echo "=== Part G demo -- Mock ERP ==="
hr

echo "STEP 1: initial state -- chart of accounts seeded on startup"
curl -s "$BASE/api/accounts" | python -m json.tool
hr

echo "STEP 2: request -- create a balanced journal entry (disbursement: DR Loan Receivable / CR Bank)"
PAYLOAD="{\"external_reference\":\"$REF\",\"entry_date\":\"2026-01-15\",\"description\":\"demo disbursement\",\"lines\":[{\"account_code\":\"1200\",\"debit\":\"5000000.00\"},{\"account_code\":\"1100\",\"credit\":\"5000000.00\"}]}"
RESP1=$(curl -s -w '\n%{http_code}' -X POST "$BASE/api/journal-entries" -H "Content-Type: application/json" -d "$PAYLOAD")
echo "STEP 3: response -- $(echo "$RESP1" | sed '$d')"
echo "STEP 8: actual -- HTTP $(echo "$RESP1" | tail -1) (expect 201, created)"
hr

echo "STEP 2: request -- resend IDENTICAL payload (expect 200, same id, no duplicate)"
RESP2=$(curl -s -w '\n%{http_code}' -X POST "$BASE/api/journal-entries" -H "Content-Type: application/json" -d "$PAYLOAD")
echo "STEP 3: response -- $(echo "$RESP2" | sed '$d')"
echo "STEP 8: actual -- HTTP $(echo "$RESP2" | tail -1) (expect 200)"
hr

echo "STEP 2: request -- same reference, UNBALANCED different payload (expect 409)"
CONFLICT="{\"external_reference\":\"$REF\",\"entry_date\":\"2026-01-15\",\"description\":\"CHANGED\",\"lines\":[{\"account_code\":\"1200\",\"debit\":\"1000000.00\"},{\"account_code\":\"1100\",\"credit\":\"1000000.00\"}]}"
RESP3=$(curl -s -w '\n%{http_code}' -X POST "$BASE/api/journal-entries" -H "Content-Type: application/json" -d "$CONFLICT")
echo "STEP 3: response -- $(echo "$RESP3" | sed '$d')"
echo "STEP 8: actual -- HTTP $(echo "$RESP3" | tail -1) (expect 409)"
hr

echo "STEP 2: request -- an entry that doesn't balance at all (expect 422, rejected before it ever reaches the DB)"
UNBALANCED="{\"external_reference\":\"TXN-BAD-$(date +%s)\",\"entry_date\":\"2026-01-15\",\"lines\":[{\"account_code\":\"1200\",\"debit\":\"100.00\"},{\"account_code\":\"1100\",\"credit\":\"50.00\"}]}"
RESP4=$(curl -s -w '\n%{http_code}' -X POST "$BASE/api/journal-entries" -H "Content-Type: application/json" -d "$UNBALANCED")
echo "STEP 3: response -- $(echo "$RESP4" | sed '$d')"
echo "STEP 8: actual -- HTTP $(echo "$RESP4" | tail -1) (expect 422)"
hr

echo "STEP 4/5: final state -- account balances after the demo entry"
curl -s "$BASE/api/accounts/1200/balance"
echo
curl -s "$BASE/api/accounts/1100/balance"
echo
hr
echo "PART G DEMO COMPLETE"
