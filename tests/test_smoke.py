import pytest
from fastapi.testclient import TestClient

from ads_leads.domains import normalize_domain


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Shop.santeh.ru/catalog?x=1", "santeh.ru"),
        ("santehnika-online.ru", "santehnika-online.ru"),
        ("http://market.yandex.ru", "yandex.ru"),
        ("сантехника.рф", "сантехника.рф"),
        ("", None),
        ("не домен", None),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADS_LEADS_DATA_DIR", str(tmp_path))
    from ads_leads import config, db

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    from ads_leads.web.app import app

    with TestClient(app) as c:
        yield c
    db.get_engine.cache_clear()
    config.get_settings.cache_clear()


def test_niche_flow(client):
    assert client.get("/health").json()["status"] == "ok"
    assert "Ниш пока нет" in client.get("/").text

    r = client.post("/niches", data={"name": "сантехника", "region_lr": 213})
    assert r.url.path == "/niches/1"
    assert "сантехника" in client.get("/").text

    client.post("/niches/1/queries", data={"queries": "Купить  кран hansgrohe\nкупить кран hansgrohe\n\nсмеситель grohe"})
    page = client.get("/niches/1").text
    assert page.count('class="badge info" style="margin:2px"') == 2  # нормализация и дедуп запросов

    client.post("/niches/1/companies", data={"domains": "https://www.santeh.ru/\nsanteh.ru\nмусор"})
    page = client.get("/niches/1").text
    assert page.count('href="https://santeh.ru"') == 1  # дедуп по домену
    assert 'href="https://santeh.ru"' in client.get("/companies").text

    assert client.get("/niches/999").status_code == 404


def test_basic_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("ADS_LEADS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADS_LEADS_AUTH_PASSWORD", "secret")
    from ads_leads import config, db

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    from ads_leads.web.app import app

    try:
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200  # проверка живости без пароля
            assert c.get("/").status_code == 401
            assert c.get("/", auth=("adbeam", "wrong")).status_code == 401
            assert c.get("/", auth=("adbeam", "secret")).status_code == 200
    finally:
        db.get_engine.cache_clear()
        config.get_settings.cache_clear()
