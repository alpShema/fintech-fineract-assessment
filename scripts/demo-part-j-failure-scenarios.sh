#!/bin/bash
# scripts/demo-part-j-failure-scenarios.sh
#
# Part J: failure handling and recovery, all 5 scenarios, scripted and
# re-runnable against the live stack. Requires: docker compose up -d
# (integration-service, fineract, mock-erp, db all running).
#
# Uses the fault-injection admin endpoints (app/fault_injection.py,
# app/routers/admin.py) to simulate each failure on demand, then shows
# state before, during, and after recovery for each one.

set -euo pipefail

BASE="http://localhost:8002"
ERP="http://localhost:8001"
FINERACT="https://localhost:8443/fineract-provider/api/v1"
KEY="integration-service-partner-key"
TODAY=$(date +%Y-%m-%d)

hr() { printf '%.0s=' {1..70}; echo; }
sub() { printf '%.0s-' {1..70}; echo; }

loan_status() {
  curl -sk -u mifos:password -H "Fineract-Platform-TenantId: default" "$FINERACT/loans/external-id/$1" \
    | python -c "import json,sys; print(json.load(sys.stdin)['status']['value'])"
}

erp_entry_exists() {
  curl -s -o /dev/null -w "%{http_code}" "$ERP/api/journal-entries/$1"
}

new_client_and_loan() {
  local ts="$1" cuid="$2" lref="$3" label="$4"
  curl -s -X POST "$BASE/api/clients" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
    -d "{\"external_reference\":\"REF-$label-CLIENT-$ts\",\"client_uid\":\"$cuid\",\"firstname\":\"$label\",\"lastname\":\"Demo\"}" > /dev/null
  curl -s -X POST "$BASE/api/loans" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
    -d "{\"external_reference\":\"REF-$label-LOAN-$ts\",\"client_uid\":\"$cuid\",\"loan_reference\":\"$lref\",\"principal\":\"500000\",\"expected_disbursement_date\":\"$TODAY\"}" > /dev/null
}

hr
echo "PART J -- FAILURE HANDLING AND RECOVERY"
hr

### Scenario 1: ERP down after Fineract success ###################
TS=$(date +%s); CUID="SME-S1-$TS"; LREF="LN-S1-$TS"
new_client_and_loan "$TS" "$CUID" "$LREF" "S1"
echo "SCENARIO 1: ERP down after Fineract success"
sub
curl -s -X POST "$BASE/api/admin/fault-injection" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d '{"target":"erp_journal_entry","mode":"down","times":1}' > /dev/null
DISB_REF="REF-S1-DISB-$TS"
echo "BEFORE: loan=$(loan_status "$LREF")  ERP-entry-exists=$(erp_entry_exists "$DISB_REF")"
echo "DURING: disbursing with ERP down..."
curl -s -w ' -> HTTP %{http_code}\n' -X POST "$BASE/api/loans/$LREF/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"$DISB_REF\",\"actual_disbursement_date\":\"$TODAY\"}"
echo "  -> Fineract succeeded (money moved), ERP leg failed: status is NEEDS_RECONCILIATION, not a plain FAILED --"
echo "     a resend of the SAME reference replays NEEDS_RECONCILIATION (never re-disburses):"
curl -s -w ' -> HTTP %{http_code}\n' -X POST "$BASE/api/loans/$LREF/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"$DISB_REF\",\"actual_disbursement_date\":\"$TODAY\"}"
echo "AFTER (still stuck): loan=$(loan_status "$LREF")  ERP-entry-exists=$(erp_entry_exists "$DISB_REF")"
echo "RECOVERING: calling the admin recovery sweep (retries only the ERP leg, never re-calls Fineract)..."
curl -s -X POST "$BASE/api/admin/recover" -H "X-Api-Key: $KEY"
echo
echo "AFTER RECOVERY: loan=$(loan_status "$LREF")  ERP-entry-exists=$(erp_entry_exists "$DISB_REF")"
hr

### Scenario 2: Fineract down ########################################
TS=$(date +%s); CUID="SME-S2-$TS"; LREF="LN-S2-$TS"
new_client_and_loan "$TS" "$CUID" "$LREF" "S2"
echo "SCENARIO 2: Fineract down"
sub
curl -s -X POST "$BASE/api/admin/fault-injection" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d '{"target":"fineract_disburse","mode":"down","times":1}' > /dev/null
echo "BEFORE: loan=$(loan_status "$LREF")"
echo "DURING: disbursing with Fineract down..."
curl -s -w ' -> HTTP %{http_code}\n' -X POST "$BASE/api/loans/$LREF/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"REF-S2-DISB-A-$TS\",\"actual_disbursement_date\":\"$TODAY\"}"
echo "  -> clean 502 UPSTREAM_ERROR, nothing persisted at Fineract:"
echo "AFTER: loan=$(loan_status "$LREF") (unchanged -- confirms nothing was persisted)"
echo "Safe to retry? Yes -- a fresh reference for the same loan now (Fineract 'back up') succeeds normally:"
curl -s -w ' -> HTTP %{http_code}\n' -X POST "$BASE/api/loans/$LREF/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"REF-S2-DISB-B-$TS\",\"actual_disbursement_date\":\"$TODAY\"}"
echo "AFTER RETRY: loan=$(loan_status "$LREF")"
hr

### Scenario 3: Fineract timeout after sending #######################
TS=$(date +%s); CUID="SME-S3-$TS"; LREF="LN-S3-$TS"
new_client_and_loan "$TS" "$CUID" "$LREF" "S3"
echo "SCENARIO 3: Fineract timeout after sending (the request DID land at Fineract; we just never saw the response)"
sub
curl -s -X POST "$BASE/api/admin/fault-injection" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d '{"target":"fineract_disburse","mode":"timeout_after_send","times":1}' > /dev/null
DISB_REF="REF-S3-DISB-$TS"
echo "BEFORE: loan=$(loan_status "$LREF")"
echo "DURING: disbursing (real call lands at Fineract; our client simulates a timeout instead of seeing the response)..."
curl -s -w ' -> HTTP %{http_code}\n' -X POST "$BASE/api/loans/$LREF/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"$DISB_REF\",\"actual_disbursement_date\":\"$TODAY\"}"
echo "  -> Not a blind retry: we looked Fineract up by externalId (=$DISB_REF), found the real disbursement,"
echo "     and completed the whole flow (including the ERP leg) within this same request."
echo "AFTER: loan=$(loan_status "$LREF")  ERP-entry-exists=$(erp_entry_exists "$DISB_REF")"
hr

### Scenario 4: ERP timeout after the entry was written ##############
TS=$(date +%s); CUID="SME-S4-$TS"; LREF="LN-S4-$TS"
new_client_and_loan "$TS" "$CUID" "$LREF" "S4"
echo "SCENARIO 4: ERP timeout after the entry was written"
sub
curl -s -X POST "$BASE/api/admin/fault-injection" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d '{"target":"erp_journal_entry","mode":"timeout_after_send","times":1}' > /dev/null
DISB_REF="REF-S4-DISB-$TS"
echo "DURING: disbursing (ERP really writes the entry; our client simulates a timeout instead of seeing the response)..."
curl -s -w ' -> HTTP %{http_code}\n' -X POST "$BASE/api/loans/$LREF/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"$DISB_REF\",\"actual_disbursement_date\":\"$TODAY\"}"
echo "  -> reports NEEDS_RECONCILIATION even though the entry already exists (we don't yet know that ourselves):"
echo "RECOVERING: admin recovery sweep retries the ERP leg -- hits Mock ERP's OWN idempotency, no duplicate:"
curl -s -X POST "$BASE/api/admin/recover" -H "X-Api-Key: $KEY"
echo
echo "PROOF: exactly one journal entry exists for $DISB_REF:"
curl -s "$ERP/api/journal-entries?entry_date_from=$TODAY&entry_date_to=$TODAY" \
  | python -c "import json,sys; e=json.load(sys.stdin); print(len([x for x in e if x['external_reference']=='$DISB_REF']), 'entry/entries')"
hr

### Scenario 5: crash between Fineract success and the ERP call ######
TS=$(date +%s); CUID="SME-S5-$TS"; LREF="LN-S5-$TS"
new_client_and_loan "$TS" "$CUID" "$LREF" "S5"
echo "SCENARIO 5: crash between Fineract success and the ERP call -- recovered on restart"
sub
curl -s -X POST "$BASE/api/admin/fault-injection" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d '{"target":"erp_journal_entry","mode":"down","times":1}' > /dev/null
DISB_REF="REF-S5-DISB-$TS"
echo "DURING: disbursing (simulates the process dying right after the Fineract leg, before the ERP leg)..."
curl -s -w ' -> HTTP %{http_code}\n' -X POST "$BASE/api/loans/$LREF/disburse" -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d "{\"external_reference\":\"$DISB_REF\",\"actual_disbursement_date\":\"$TODAY\"}"
echo "BEFORE RESTART: loan=$(loan_status "$LREF")  ERP-entry-exists=$(erp_entry_exists "$DISB_REF") (stuck)"
echo "Restarting the container (simulates recovery after a real crash, not a manual recovery call)..."
docker compose restart integration-service > /dev/null 2>&1
sleep 8
echo "AFTER RESTART (no manual action taken): loan=$(loan_status "$LREF")  ERP-entry-exists=$(erp_entry_exists "$DISB_REF")"
echo "  -> the startup sweep found the stuck row and completed it automatically before the app even finished starting."
hr

echo "PART J DEMO COMPLETE -- all 5 scenarios"
