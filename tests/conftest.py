import pytest


@pytest.fixture(autouse=True)
def isolate_from_local_env(monkeypatch):
    """Тесты не должны зависеть от локального .env (пароли, токен Keys.so): переменные окружения важнее файла."""
    for name in ("ADS_LEADS_AUTH_PASSWORD", "ADS_LEADS_AUTH_USERS", "ADS_LEADS_KEYSO_API_TOKEN",
                 "ADS_LEADS_SECRET_KEY", "ADS_LEADS_DATABASE_URL"):
        monkeypatch.setenv(name, "")
