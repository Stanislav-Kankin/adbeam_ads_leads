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

    r = client.post("/niches", data={"name": "сантехника", "regions": ["msk", "spb", "bad"]})
    assert r.url.path == "/niches/1"
    assert "сантехника" in client.get("/").text

    client.post("/niches/1/queries", data={"queries": "Купить  кран hansgrohe\nкупить кран hansgrohe\n\nсмеситель grohe"})
    page = client.get("/niches/1").text
    assert page.count('class="chip query"') == 2  # нормализация и дедуп запросов
    assert "Санкт-Петербург" in page

    client.post("/niches/1/companies", data={"domains": "https://www.santeh.ru/\nsanteh.ru\nмусор"})
    page = client.get("/niches/1").text
    assert page.count('href="https://santeh.ru"') == 1  # дедуп по домену
    assert 'href="https://santeh.ru"' in client.get("/companies").text

    assert client.get("/niches/999").status_code == 404


def test_login(tmp_path, monkeypatch):
    monkeypatch.setenv("ADS_LEADS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADS_LEADS_AUTH_PASSWORD", "secret")
    monkeypatch.setenv("ADS_LEADS_AUTH_USERS", "stas:pw1, nonna:pw2")
    from ads_leads import config, db

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    from ads_leads.web.app import app

    try:
        with TestClient(app) as anon:
            assert anon.get("/health").status_code == 200  # проверка живости без входа
            r = anon.get("/niches/1", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
            assert anon.post("/login", data={"login": "stas", "password": "bad"}).status_code == 401
            # внешний next не принимаем
            r = anon.post("/login", data={"login": "stas", "password": "pw1", "next": "//evil.com"},
                          follow_redirects=False)
            assert r.headers["location"] == "/"

        # два пользователя одновременно видят одни и те же данные
        with TestClient(app) as stas, TestClient(app) as nonna:
            stas.post("/login", data={"login": "stas", "password": "pw1"})
            nonna.post("/login", data={"login": "nonna", "password": "pw2"})
            stas.post("/niches", data={"name": "сантехника"})
            page = nonna.get("/")
            assert "сантехника" in page.text and "nonna" in page.text
            assert "сантехника" in stas.get("/").text  # stas не выкинуло после входа nonna
            # общий логин тоже работает
            with TestClient(app) as shared:
                shared.post("/login", data={"login": "adbeam", "password": "secret"})
                assert shared.get("/").status_code == 200
            nonna.post("/logout")
            assert nonna.get("/", follow_redirects=False).status_code == 303
            assert stas.get("/", follow_redirects=False).status_code == 200
    finally:
        db.get_engine.cache_clear()
        config.get_settings.cache_clear()
