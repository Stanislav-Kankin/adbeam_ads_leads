"""Шаг 2: сбор рекламодателей из Keys.so (Keys.so подменён через httpx.MockTransport)."""

import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

ROWS = {
    "msk": [
        {"name": "santehnika-online.ru", "cnt": 3, "spec": 2, "gar": 1, "perc": 100.0},
        {"name": "moscow.santeh.ru", "cnt": 2, "spec": 1, "gar": 1, "perc": 66.7},
        {"name": "santeh.ru", "cnt": 1, "spec": 0, "gar": 1, "perc": 33.3},  # тот же сайт, что поддомен выше
        {"name": "market.yandex.ru", "cnt": 3, "spec": 3, "gar": 0, "perc": 100.0},
        {"name": "ozon.ru", "cnt": 2, "spec": 2, "gar": 0, "perc": 66.7},
    ],
    "spb": [{"name": "santehnika-online.ru", "cnt": 3, "spec": 3, "gar": 0, "perc": 100.0}],
}


def fake_keyso(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/tools/concurents_by_keywords":
        body = json.loads(request.content)
        assert body["list"] and body["base"] in ROWS
        return httpx.Response(200, json={"uid": "u-" + body["base"]})
    if path.startswith("/tools/concurents_by_keywords/state/"):
        return httpx.Response(200, json={"state": 10, "progress": 100})
    if path.startswith("/tools/concurents_by_keywords/"):
        assert request.url.params["view"] == "context"
        rows = ROWS[path.rsplit("-", 1)[1]]
        return httpx.Response(200, json={"data": rows, "last_page": 1})
    if path == "/tools/domains-batch":
        domains = json.loads(request.content)["data"]["domains"]
        assert "ozon.ru" not in domains  # маркетплейсы не тратят лимит
        return httpx.Response(200, json={"last_page": 1, "data": [
            {"name": d, "adcost": 100_000 * (i + 1), "adtraf": 10, "adkeyscnt": 5, "adscnt": 7, "traf": 1}
            for i, d in enumerate(domains)]})
    return httpx.Response(404)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADS_LEADS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADS_LEADS_KEYSO_API_TOKEN", "test-token")
    from ads_leads import config, db, jobs
    from ads_leads.keyso import KeysoClient
    from ads_leads.web import app as web

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    factory = lambda: KeysoClient("t", transport=httpx.MockTransport(fake_keyso), min_interval=0)  # noqa: E731
    from ads_leads.site_crawl import SiteResult

    def fake_crawl(domain):
        if domain == "santeh.ru":
            return SiteResult(domain=domain, status="blocked", error="HTTP 403")
        return SiteResult(domain=domain, inn="7724419293", inn_candidates=["7724419293"], legal_name='ООО "Тест"',
                          emails=["info@" + domain], phones=["+7 495 111-22-33"], signals=["metrika", "roistat"])

    def run_sync(fn, job_id, _factory=None, *rest):
        if fn is jobs.crawl_sites:
            fn(job_id, fake_crawl, *rest)
        else:
            fn(job_id, factory)

    monkeypatch.setattr(web, "run_in_background", run_sync)
    with TestClient(web.app) as c:
        yield c
    db.get_engine.cache_clear()
    config.get_settings.cache_clear()


def test_collect_advertisers(app_client):
    c = app_client
    c.post("/niches", data={"name": "сантехника", "regions": ["msk", "spb"]})
    page = c.get("/niches/1").text
    assert "Сначала добавьте запросы" in page

    c.post("/niches/1/queries", data={"queries": "купить смеситель grohe\nунитаз подвесной купить\nкупить кран hansgrohe"})
    page = c.post("/niches/1/collect").text  # редирект обратно на нишу, задача уже выполнена
    assert "Готово: 2 рекламодателей" in page
    assert "ещё 2 отброшено" in page
    assert page.count('class="domain" href="https://santehnika-online.ru"') == 1
    assert page.count('class="domain" href="https://santeh.ru"') == 1  # поддомен склеен
    assert "ozon.ru · маркетплейс" in page
    assert "Москва, Санкт-Петербург" in page  # santehnika-online.ru есть в обоих регионах
    # бюджет: adcost в копейках/день → ₽/мес; santeh.ru второй в пакете → 200 000 коп/день = 60 000 ₽/мес
    assert "60 000" in page

    # повторный сбор не плодит дубли
    c.post("/niches/1/collect")
    from ads_leads.db import get_engine
    with get_engine().connect() as conn:
        n = conn.exec_driver_sql("select count(*) from niche_advertisers").scalar()
    assert n == 5  # msk: 2 рекламодателя + 2 исключённых, spb: 1


def test_collect_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ADS_LEADS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADS_LEADS_KEYSO_API_TOKEN", "")
    from ads_leads import config, db
    from ads_leads.web.app import app

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    try:
        with TestClient(app) as c:
            c.post("/niches", data={"name": "x"})
            c.post("/niches/1/queries", data={"queries": "q"})
            assert c.post("/niches/1/collect").status_code == 400
    finally:
        db.get_engine.cache_clear()
        config.get_settings.cache_clear()


def test_old_database_is_migrated(tmp_path, monkeypatch):
    """БД из первой версии (без колонки regions и новых таблиц) открывается без ошибок."""
    conn = sqlite3.connect(tmp_path / "ads_leads.db")
    conn.executescript("""
        CREATE TABLE niches (id INTEGER PRIMARY KEY, name VARCHAR(200) UNIQUE, region_lr INTEGER NOT NULL,
                             created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO niches (name, region_lr) VALUES ('старая ниша', 213);
        CREATE TABLE companies (id INTEGER PRIMARY KEY, domain VARCHAR(255) UNIQUE, inn VARCHAR(12),
            legal_name VARCHAR(500), source VARCHAR(20), status VARCHAR(20), notes TEXT,
            created_at DATETIME, updated_at DATETIME);
    """)
    conn.close()
    monkeypatch.setenv("ADS_LEADS_DATA_DIR", str(tmp_path))
    from ads_leads import config, db
    from ads_leads.web.app import app

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    try:
        with TestClient(app) as c:
            assert "старая ниша" in c.get("/").text
            assert "Москва" in c.get("/niches/1").text
    finally:
        db.get_engine.cache_clear()
        config.get_settings.cache_clear()


def test_crawl_and_pagination(app_client):
    c = app_client
    c.post("/niches", data={"name": "сантехника", "regions": ["msk"]})
    c.post("/niches/1/queries", data={"queries": "купить смеситель grohe"})
    c.post("/niches/1/collect")
    page = c.post("/niches/1/crawl").text
    assert "Готово: 2 сайтов, ИНН найден у 1, 1 с защитой от ботов" in page
    assert "7724419293" in page and "Roistat" in page and "защита от ботов" in page
    assert "ozon.ru" not in page.split('id="step-3"')[1]  # исключённые не обходим
    # повторный запуск проверяет только новые
    assert "Все сайты уже проверены" in c.post("/niches/1/crawl").text

    # пагинация: 25 доменов вручную → 2 страницы по 20
    c.post("/niches/1/companies", data={"domains": chr(10).join(f"site{i}.ru" for i in range(25))})
    page = c.get("/niches/1?per=20&p2=2").text
    step2 = page.split('id="step-2"')[1].split('id="step-3"')[0]
    assert "Показано 21–27 из 27" in step2
    assert "per=50" in step2
    page = c.get("/niches/1?per=50").text
    assert "Показано 1–27 из 27" in page.split('id="step-2"')[1]
