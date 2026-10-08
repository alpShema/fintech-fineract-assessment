#!/bin/bash
# scripts/run-fineract-tests.sh
#
# Runs just the new Part A JUnit tests (unit + integration) against the local
# scratch clone of the pinned Fineract tag, and saves the output as evidence.
# See fineract-customization/NOTES.md for why this is separate from the
# Docker image build (which skips tests for build-time/resource reasons) and
# for how to (re)generate fineract-customization/fineract.patch from the same
# clone once you're happy with your changes.
#
# Usage:
#   scripts/run-fineract-tests.sh <path-to-local-fineract-clone>
#
# Example:
#   scripts/run-fineract-tests.sh "C:/Users/ashem/OneDrive/Desktop/fineract-upstream"

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FINERACT_CLONE="${1:?Usage: $0 <path-to-local-fineract-clone>}"
EVIDENCE_LOG="$REPO_ROOT/evidence/fineract-java-tests.log"

cd "$FINERACT_CLONE"

echo "Running Part A unit test (GL account external_reference idempotency)..."
./gradlew --no-daemon --console=plain \
  --init-script "$REPO_ROOT/fineract-customization/gradle-init.gradle" \
  :fineract-provider:test \
  --tests "org.apache.fineract.accounting.glaccount.service.GLAccountWritePlatformServiceJpaRepositoryImplTest" \
  -x spotlessCheck -x checkstyleMain \
  2>&1 | tee "$EVIDENCE_LOG"

echo
echo "Unit test log saved to: $EVIDENCE_LOG"
echo

INTEGRATION_LOG="$REPO_ROOT/evidence/fineract-integration-test.log"
echo "Running Part A integration test against the running customized Fineract..."
echo "(requires: docker compose up -d already run, fineract container healthy)"
echo

# Two things the integration-tests module needs that aren't there by default:
# 1. integration-tests' own `test` task defaults to spinning up ITS OWN separate
#    Tomcat+Cargo deployment (cargoStartLocal) on port 8443 -- which would collide
#    with our already-running Docker container on that same port. -PcargoDisabled
#    skips that and just points REST-assured at whatever's already listening on
#    localhost:8443 (our Docker container).
# 2. Utils.initializeRESTAssured() loads a keystore from the relative path
#    "src/main/resources/keystore.jks", resolved against integration-tests' own
#    project directory -- but that file only exists under fineract-provider's
#    resources in a stock checkout. This is a pre-existing Fineract test-harness
#    quirk, unrelated to our patch, so it's copied here rather than fixed upstream.
mkdir -p integration-tests/src/main/resources
cp fineract-provider/src/main/resources/keystore.jks integration-tests/src/main/resources/keystore.jks

./gradlew --no-daemon --console=plain \
  --init-script "$REPO_ROOT/fineract-customization/gradle-init.gradle" \
  -PcargoDisabled \
  :integration-tests:test --tests "*GLAccountExternalReferenceIntegrationTest*" \
  -x spotlessCheck -x checkstyleMain -x spotbugsTest \
  2>&1 | tee "$INTEGRATION_LOG"

echo
echo "Integration test log saved to: $INTEGRATION_LOG"
