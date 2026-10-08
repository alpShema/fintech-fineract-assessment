from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables. Defaults match
    docker-compose.yml's wiring (Fineract on 8443, Mock ERP on the internal
    mock-erp:8000 hostname, own Postgres database/role)."""

    model_config = SettingsConfigDict(env_prefix="INTEGRATION_SERVICE_")

    database_url: str = "postgresql+psycopg://integration_service:integration_service@db:5432/integration_service"

    fineract_base_url: str = "https://fineract:8443/fineract-provider/api/v1"
    fineract_tenant_id: str = "default"
    fineract_username: str = "integration-service"
    fineract_password: str = "IntegrationService!2024"
    # Our own HTTPS cert is self-signed (see fineract-customization/Dockerfile) --
    # this is a local dev stack, not a certificate a client should actually
    # verify against a public CA.
    fineract_verify_ssl: bool = False

    mock_erp_base_url: str = "http://mock-erp:8000"

    # Shared API key callers (the "financing partner") must present. Simple and
    # sufficient for this assessment's scope -- see DECISIONS.md for why this
    # wasn't built out into full OAuth2/JWT.
    api_key: str = "integration-service-partner-key"


settings = Settings()
