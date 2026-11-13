"""Настройки приложения. Читаются из переменных окружения и файла .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ADS_LEADS_", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str = ""  # пусто → sqlite в data_dir
    host: str = "127.0.0.1"
    port: int = 8000

    keyso_api_token: str = ""
    keyso_base: str = "msk"  # регион базы Keys.so (Москва)

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'ads_leads.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
