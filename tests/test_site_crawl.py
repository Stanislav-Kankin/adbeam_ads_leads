import httpx

from ads_leads.site_crawl import crawl_site, inn_is_valid, normalize_phone

HOME = """<html><head><title>Сантех Шоп</title>
<script src="https://mc.yandex.ru/metrika/tag.js"></script><script>ym(12345678, "init", {});</script>
<script src="https://cloud.roistat.com/x.js"></script></head>
<body><a href="/kontakty/">Контакты</a> <a href="tel:+74951234567">звоните</a>
<a href="https://vk.com/santehshop">ВК</a></body></html>"""
CONTACTS = """<html><body><h1>Реквизиты</h1>
<p>ООО «Сантех Шоп Плюс», ИНН 7724419293, КПП 772401001. Также ИНН: 1234567890 (опечатка). Сплит: ООО «Яндекс», ИНН 7736207543. АО «ТБанк» — рассрочка.</p>
<p>Почта: info@santeh.ru, test@example.com. Тел.: 8 (800) 555-35-35, +7 050 000-00-80</p></body></html>"""


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "blocked.ru":
        return httpx.Response(200, text="<html><script src='https://servicepipe.tech/x.js'></script>js-challenge</html>",
                              headers={"content-type": "text/html"})
    pages = {"/": HOME, "/kontakty/": CONTACTS}
    html = pages.get(request.url.path)
    if html is None:
        return httpx.Response(404)
    return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})


def test_inn_checksum():
    assert inn_is_valid("7724419293")
    assert not inn_is_valid("1234567890")
    assert inn_is_valid("032616721612")
    assert normalize_phone("8 (800) 555-35-35") == "+7 800 555-35-35"
    assert normalize_phone("+7 050 000-00-80") is None


def test_crawl_site_extracts_everything():
    r = crawl_site("santeh.ru", transport=httpx.MockTransport(handler))
    assert r.status == "ok" and r.title == "Сантех Шоп"
    assert r.inn == "7724419293" and r.inn_candidates == ["7724419293"]
    assert r.legal_name == 'ООО "Сантех Шоп Плюс"'
    assert r.emails == ["info@santeh.ru"]
    assert r.phones[:2] == ["+7 495 123-45-67", "+7 800 555-35-35"]
    assert r.signals == ["metrika", "roistat"]
    assert r.metrika_ids == ["12345678"]
    assert r.socials == ["https://vk.com/santehshop"]


def test_crawl_site_blocked():
    r = crawl_site("blocked.ru", transport=httpx.MockTransport(handler))
    assert r.status == "blocked"


def test_block_page_detection():
    from ads_leads.site_crawl import Page, _looks_blocked

    assert _looks_blocked(Page("u", 200, "<h1>Forbidden</h1><p>If you are not a bot, please copy the report</p>"))
    assert _looks_blocked(Page("u", 403, "<title>Доступ ограничен</title>" + "x" * 15000))
    assert _looks_blocked(Page("u", 429, "<html>Too many</html>"))
    # браузер прошёл проверку: большая страница сайта, а код 403 остался от первого ответа
    assert not _looks_blocked(Page("u", 403, "<title>Купить сантехнику</title>" + "x" * 150000))
    assert not _looks_blocked(Page("u", 200, "<title>Сантехника</title><p>ИНН 7724419293</p>"))
