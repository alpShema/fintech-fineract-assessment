#!/bin/bash
# Reused verbatim from Apache Fineract's own
# config/docker/postgresql/docker-entrypoint-initdb.d/01-init.sh at tag
# 1.10.0. Runs once, the first time the `db` container's data volume is
# initialized, to create the Fineract tenant-registry and default-tenant
# databases plus the app's DB user.

set -e
export PGPASSWORD=$POSTGRES_PASSWORD;
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE USER $FINERACT_DB_USER WITH PASSWORD '$FINERACT_DB_PASS';
  CREATE DATABASE $FINERACT_TENANTS_DB_NAME;
  CREATE DATABASE $FINERACT_TENANT_DEFAULT_DB_NAME;
  GRANT ALL PRIVILEGES ON DATABASE $FINERACT_TENANTS_DB_NAME TO $FINERACT_DB_USER;
  GRANT ALL PRIVILEGES ON DATABASE $FINERACT_TENANT_DEFAULT_DB_NAME TO $FINERACT_DB_USER;
  \c $FINERACT_TENANTS_DB_NAME
  GRANT ALL ON SCHEMA public TO $FINERACT_DB_USER;
  \c $FINERACT_TENANT_DEFAULT_DB_NAME
  GRANT ALL ON SCHEMA public TO $FINERACT_DB_USER;
EOSQL
