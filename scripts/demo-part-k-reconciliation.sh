#!/bin/bash
# scripts/demo-part-k-reconciliation.sh
#
# Part K: reconciliation, scripted and re-runnable. Deliberately seeds all
# four outcomes (MATCHED, MISSING_IN_ERP, MISSING_IN_FINERACT,
# AMOUNT_MISMATCH) then runs the report and shows each one present.
# Requires: docker compose up -d (integration-service, fineract, mock-erp, db).

set -euo pipefail

BASE="http://localhost:8002"
ERP="http://localhost:8001"
KEY="integration-service-partner-key"
TODAY=$(date +%Y-%m-%d)
TS=$(date +%s)

hr() { printf '%.0s=' {1..70}; echo; }

new_client_and_loan() {
  local label="$1"
  local cuid="SME-$label-$TS"
  local lref="LN-$label-$TS"
  curl -s -X POST "$BASE/api/clients" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
    -d "{\"external_reference\":\"REF-$label-CLIENT-$TS\",\"client_uid\":\"$cuid\",\"firstname\":\"$label\",\"lastname\":\"Recon\"}" > /dev/null
  curl -s -X POST "$BASE/api/loans" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
    -d "{\"external_reference\":\"REF-$label-LOAN-$TS\",\"client_uid\":\"$cuid\",\"loan_reference\":\"$lref\",\"principal\":\"500000\",\"expected_disbursement_date\":\"$TODAY\"}" > /dev/null
  echo "$lref"
}

hr
echo "PART K -- RECONCILIATION: seeding all four outcomes"
hr

echo "1. MATCHED -- a normal disbursement, auto-posted correctly"
LREF1=$(new_client_and_loan "K1")
REF_MATCHED="REF-K-MATCHED-$TS"
curl -s -X POST "$BASE/api/loans/$LREF1/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"$REF_MATCHED\",\"actual_disbursement_date\":\"$TODAY\"}" > /dev/null
echo "   loan=$LREF1 reference=$REF_MATCHED"

echo "2. MISSING_IN_ERP -- Fineract succeeds, ERP leg suppressed (fault-injected), left unrecovered"
LREF2=$(new_client_and_loan "K2")
curl -s -X POST "$BASE/api/admin/fault-injection" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d '{"target":"erp_journal_entry","mode":"down","times":1}' > /dev/null
REF_MISSING_ERP="REF-K-MISSING-ERP-$TS"
curl -s -X POST "$BASE/api/loans/$LREF2/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"$REF_MISSING_ERP\",\"actual_disbursement_date\":\"$TODAY\"}" > /dev/null
echo "   loan=$LREF2 reference=$REF_MISSING_ERP (deliberately left stuck -- no recovery sweep run)"

echo "3. MISSING_IN_FINERACT -- a journal entry posted directly to the ERP, no Fineract transaction will ever carry this reference"
REF_MISSING_FINERACT="REF-K-MISSING-FINERACT-$TS"
curl -s -X POST "$ERP/api/journal-entries" -H "Content-Type: application/json" \
  -d "{\"external_reference\":\"$REF_MISSING_FINERACT\",\"entry_date\":\"$TODAY\",\"description\":\"manual entry, no Fineract counterpart\",\"lines\":[{\"account_code\":\"1200\",\"debit\":\"75000.00\"},{\"account_code\":\"1100\",\"credit\":\"75000.00\"}]}" > /dev/null
echo "   reference=$REF_MISSING_FINERACT"

echo "4. AMOUNT_MISMATCH -- a real Fineract disbursement, but its ERP entry (posted directly, auto-posting suppressed) has the wrong amount"
LREF4=$(new_client_and_loan "K4")
curl -s -X POST "$BASE/api/admin/fault-injection" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d '{"target":"erp_journal_entry","mode":"down","times":1}' > /dev/null
REF_MISMATCH="REF-K-MISMATCH-$TS"
curl -s -X POST "$BASE/api/loans/$LREF4/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"$REF_MISMATCH\",\"actual_disbursement_date\":\"$TODAY\"}" > /dev/null
# Real disbursement was 500000; post a WRONG amount directly to the ERP for the same reference.
curl -s -X POST "$ERP/api/journal-entries" -H "Content-Type: application/json" \
  -d "{\"external_reference\":\"$REF_MISMATCH\",\"entry_date\":\"$TODAY\",\"description\":\"wrong amount, deliberately\",\"lines\":[{\"account_code\":\"1200\",\"debit\":\"1234.00\"},{\"account_code\":\"1100\",\"credit\":\"1234.00\"}]}" > /dev/null
echo "   loan=$LREF4 reference=$REF_MISMATCH (Fineract: 500000, ERP: 1234)"

hr
echo "RUNNING RECONCILIATION REPORT (today's date range)"
hr
curl -s -X POST "$BASE/api/reconciliation" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"date_from\":\"$TODAY\",\"date_to\":\"$TODAY\"}" | python -m json.tool

hr
echo "PART K DEMO COMPLETE -- all four outcomes present in the report above"
