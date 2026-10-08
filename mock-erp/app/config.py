from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (12-factor style).

    Defaults match the mock_erp database/user docker-compose will create --
    see fineract/postgresql-init/02-mock-erp-init.sh.
    """

    model_config = SettingsConfigDict(env_prefix="MOCK_ERP_")

    database_url: str = "postgresql+psycopg://mock_erp:mock_erp@db:5432/mock_erp"


settings = Settings()
