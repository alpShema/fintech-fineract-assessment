#!/bin/bash
# Creates a dedicated database + least-privilege role for the Mock ERP,
# separate from Fineract's own tenant databases. Same Postgres container
# (one DB service for the whole stack is simpler to run locally), but fully
# separate schema/credentials -- the Mock ERP has no business reading or
# writing Fineract's tables, and vice versa.
set -e
export PGPASSWORD=$POSTGRES_PASSWORD
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE USER mock_erp WITH PASSWORD 'mock_erp';
  CREATE DATABASE mock_erp OWNER mock_erp;
EOSQL
