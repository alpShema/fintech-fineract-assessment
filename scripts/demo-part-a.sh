#!/bin/bash
# scripts/demo-part-a.sh
#
# Scripted, re-runnable demonstration of Part A's required behaviors 1-4
# against a running customized Fineract (see evidence/ for the brief's
# before/request/response/expected/actual template, printed per step below).
# Requires: `docker compose up -d` already run, Fineract container healthy.
#
# Usage: scripts/demo-part-a.sh [external_reference]
# (external_reference defaults to a freshly generated one each run, so this
# script is safe to re-run repeatedly without colliding with previous runs.)

set -euo pipefail

BASE="https://localhost:8443/fineract-provider/api/v1/glaccounts"
AUTH=(-u mifos:password -H "Fineract-Platform-TenantId: default" -H "Content-Type: application/json")
REF="${1:-GL-DEMO-$(date +%s)}"
GLCODE="DEMO-${REF}"

hr() { printf '%.0s-' {1..70}; echo; }

echo "Part A demo -- external_reference = $REF"
hr

echo "STEP 1: initial state"
echo "  No GL account exists yet with external_reference='$REF'."
hr

echo "STEP 2: request -- create GL account"
PAYLOAD1="{\"name\":\"Loan Receivable\",\"glCode\":\"$GLCODE\",\"type\":1,\"usage\":1,\"manualEntriesAllowed\":true,\"externalReference\":\"$REF\"}"
echo "  POST $BASE"
echo "  body: $PAYLOAD1"
RESP1=$(curl -sk -w '\n%{http_code}' "${AUTH[@]}" -X POST "$BASE" -d "$PAYLOAD1")
HTTP1=$(echo "$RESP1" | tail -1)
BODY1=$(echo "$RESP1" | sed '$d')
echo "STEP 3: response"
echo "  HTTP $HTTP1"
echo "  $BODY1"
echo "STEP 4/5/6: Fineract state -- new acc_gl_account row created"
echo "STEP 7: expected result -- a new resourceId, HTTP 200/201"
echo "STEP 8: actual result -- HTTP $HTTP1, $BODY1"
if [ "$HTTP1" != "200" ]; then
  echo "  EXPLANATION: unexpected status on first create -- investigate before continuing."
  exit 1
fi
RESOURCE_ID1=$(echo "$BODY1" | grep -o '"resourceId":[0-9]*' | grep -o '[0-9]*')
echo "  EXPLANATION: created successfully, resourceId=$RESOURCE_ID1"
hr

echo "STEP 1: initial state -- same external_reference '$REF' already exists (resourceId=$RESOURCE_ID1)"
echo "STEP 2: request -- resend the IDENTICAL payload (simulates ERP retry / at-least-once delivery)"
echo "  POST $BASE"
echo "  body: $PAYLOAD1"
RESP2=$(curl -sk -w '\n%{http_code}' "${AUTH[@]}" -X POST "$BASE" -d "$PAYLOAD1")
HTTP2=$(echo "$RESP2" | tail -1)
BODY2=$(echo "$RESP2" | sed '$d')
echo "STEP 3: response"
echo "  HTTP $HTTP2"
echo "  $BODY2"
RESOURCE_ID2=$(echo "$BODY2" | grep -o '"resourceId":[0-9]*' | grep -o '[0-9]*')
echo "STEP 4/5/6: Fineract state -- must still be exactly ONE row for this external_reference"
echo "STEP 7: expected result -- HTTP 200, same resourceId=$RESOURCE_ID1, no duplicate"
echo "STEP 8: actual result -- HTTP $HTTP2, resourceId=$RESOURCE_ID2"
if [ "$HTTP2" = "200" ] && [ "$RESOURCE_ID2" = "$RESOURCE_ID1" ]; then
  echo "  EXPLANATION: PASS -- behavior 2 confirmed (idempotent return, no duplicate created)"
else
  echo "  EXPLANATION: FAIL -- expected resourceId=$RESOURCE_ID1, got HTTP=$HTTP2 resourceId=$RESOURCE_ID2"
  exit 1
fi
hr

echo "STEP 1: initial state -- same external_reference '$REF' already exists (resourceId=$RESOURCE_ID1)"
echo "STEP 2: request -- same external_reference, DIFFERENT payload (conflicting reuse of the business key)"
PAYLOAD3="{\"name\":\"Loan Receivable CHANGED\",\"glCode\":\"${GLCODE}-CONFLICT\",\"type\":1,\"usage\":1,\"manualEntriesAllowed\":true,\"externalReference\":\"$REF\"}"
echo "  POST $BASE"
echo "  body: $PAYLOAD3"
RESP3=$(curl -sk -w '\n%{http_code}' "${AUTH[@]}" -X POST "$BASE" -d "$PAYLOAD3")
HTTP3=$(echo "$RESP3" | tail -1)
BODY3=$(echo "$RESP3" | sed '$d')
echo "STEP 3: response"
echo "  HTTP $HTTP3"
echo "  $BODY3"
echo "STEP 4/5/6: Fineract state -- unchanged, no second row, no mutation of the existing one"
echo "STEP 7: expected result -- HTTP 409 Conflict"
echo "STEP 8: actual result -- HTTP $HTTP3"
if [ "$HTTP3" = "409" ]; then
  echo "  EXPLANATION: PASS -- behavior 3 confirmed (conflicting payload rejected, not silently applied)"
else
  echo "  EXPLANATION: FAIL -- expected HTTP 409, got HTTP=$HTTP3"
  exit 1
fi
hr

echo "STEP 1: initial state -- authenticated as 'mifos' (has CREATE_GLACCOUNT permission)"
echo "STEP 2: request -- same create call, but WITHOUT credentials"
echo "  POST $BASE (no Authorization header)"
RESP4=$(curl -sk -w '\n%{http_code}' -H "Fineract-Platform-TenantId: default" -H "Content-Type: application/json" -X POST "$BASE" -d "$PAYLOAD1")
HTTP4=$(echo "$RESP4" | tail -1)
echo "STEP 3: response -- HTTP $HTTP4"
echo "STEP 7: expected result -- HTTP 401 (rejected before our logic ever runs)"
echo "STEP 8: actual result -- HTTP $HTTP4"
if [ "$HTTP4" = "401" ]; then
  echo "  EXPLANATION: PASS -- behavior 4 confirmed (Fineract's own permission/auth model protects this path)"
else
  echo "  EXPLANATION: FAIL -- expected HTTP 401, got HTTP=$HTTP4"
  exit 1
fi
hr

echo "ALL PART A BEHAVIORS (1-4) CONFIRMED for external_reference=$REF"
