#!/bin/bash
# Creates a dedicated database + least-privilege role for the Integration
# Service, separate from Fineract's tenant databases and from the Mock ERP's
# database. Same Postgres container, fully separate schema/credentials.
set -e
export PGPASSWORD=$POSTGRES_PASSWORD
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE USER integration_service WITH PASSWORD 'integration_service';
  CREATE DATABASE integration_service OWNER integration_service;
EOSQL
