"""Обход сайта рекламодателя: ИНН, юрлицо, контакты, сигналы рекламы и сквозной аналитики.

Частично перенесено из adbeam_wb_marketguru_parser (icp_autosearch.py, site_audit.py).
Только httpx: сайты с JS-защитой (DDoS-Guard, Qrator, Cloudflare) помечаются как blocked.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from selectolax.parser import HTMLParser

from ads_leads.domains import normalize_domain

TIMEOUT = httpx.Timeout(12.0, connect=6.0)
MAX_PAGES = 7
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}
BLOCK_STATUSES = {401, 403, 429, 498, 503}
BLOCK_MARKERS = ("ddos-guard", "qrator", "cf-chl", "challenge-platform", "проверка браузера",
                 "checking your browser", "servicepipe", "js-challenge", "id_captcha_frame", "variti",
                 "if you are not a bot", "доступ ограничен", "bothunt", "система защиты сайта", "access denied",
                 "just a moment", "<title>forbidden</title>")

EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{2,}@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?![\w.+-])")
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-().]*\d{3}[\s\-().]*\d{3}[\s\-().]*\d{2}[\s\-().]*\d{2}")
INN_RE = re.compile(r"ИНН\D{0,25}?(\d{12}|\d{10})(?!\d)", re.IGNORECASE)
_FORMS = r"(?:ООО|ОАО|АО|ЗАО|ПАО|Общество с ограниченной ответственностью|Акционерное общество)"
LEGAL_RE = re.compile(
    rf"({_FORMS}\s*[«\"„“][^«»\"„“]{{2,80}}[»\"“”]"  # ООО «Название в кавычках»
    r"|(?:ИП|Индивидуальный предприниматель)\s+[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){0,2}"  # ИП Фамилия Имя Отчество
    r"|(?:ИП)\s+[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.)"  # ИП Фамилия И.О.
)

# Ссылки, где обычно лежат реквизиты
LINK_TERMS = ("контакт", "реквизит", "о компании", "оферт", "политик", "конфиденц", "contact", "rekvizit",
              "requisit", "about", "kontakt", "oferta", "offer", "privacy", "politika", "company")
COMMON_PATHS = ("/contacts/", "/kontakty/", "/rekvizity/", "/requisites/", "/about/", "/oferta/",
                "/policy/", "/privacy/", "/company/")

# Сигналы: (код, название, признаки в HTML)
SIGNALS: list[tuple[str, str, tuple[str, ...]]] = [
    ("metrika", "Метрика", ("mc.yandex.ru/metrika", "mc.yandex.ru/watch", "yandex_metrika", "ym(")),
    ("gads", "Google Ads", ("googleadservices.com", "aw-")),
    ("ga", "Google Analytics/GTM", ("googletagmanager.com", "google-analytics.com", "gtag(")),
    ("vk", "VK Реклама", ("vk.com/rtrg", "top-fwz1.mail.ru", "vk.com/js/api/openapi", "ads.vk.com")),
    ("roistat", "Roistat", ("roistat",)),
    ("calltouch", "Calltouch", ("calltouch",)),
    ("comagic", "CoMagic/UIS", ("comagic", "uiscom", "uis.ru/")),
    ("callibri", "Callibri", ("callibri",)),
    ("mango", "Mango Office", ("mango-office", "mango.office")),
    ("alloka", "Alloka", ("alloka",)),
    ("bitrix24", "Битрикс24 (CRM)", ("bitrix24", "b24-")),
    ("amocrm", "amoCRM", ("amocrm",)),
]
SIGNAL_NAMES = {code: name for code, name, _ in SIGNALS}
END_TO_END = {"roistat", "calltouch", "comagic", "callibri", "mango", "alloka"}  # сквозная аналитика/коллтрекинг
METRIKA_ID_RE = re.compile(r"(?:ym\(\s*|metrika/tag\.js\?id=|watch/|counter_id[\"']?\s*[:=]\s*)(\d{6,9})")
AW_RE = re.compile(r"\bAW-(\d{9,11})\b")
SOCIAL_HOSTS = ("vk.com", "t.me", "ok.ru", "youtube.com", "rutube.ru", "dzen.ru")


@dataclass
class SiteResult:
    domain: str
    status: str = "ok"  # ok | blocked | down
    final_url: str | None = None
    title: str | None = None
    inn: str | None = None
    inn_candidates: list[str] = field(default_factory=list)
    legal_name: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    socials: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    metrika_ids: list[str] = field(default_factory=list)
    pages: int = 0
    error: str | None = None


# --- ИНН -----------------------------------------------------------------------
def inn_is_valid(inn: str) -> bool:
    if not inn.isdigit() or len(inn) not in (10, 12):
        return False
    d = [int(c) for c in inn]

    def check(coeffs: list[int], n: int) -> bool:
        return sum(c * x for c, x in zip(coeffs, d)) % 11 % 10 == d[n]

    if len(d) == 10:
        return check([2, 4, 10, 3, 5, 9, 4, 6, 8], 9)
    return check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], 10) and check([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], 11)


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    # мусор: «код» 0xx/1xx или номер из одних нулей (+7 050 000-00-80)
    if len(digits) == 11 and (digits[1] in "01" or digits[4:].count("0") >= 5):
        return None
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
    return None


def _useful_email(email: str, domain: str) -> bool:
    e = email.lower()
    local, _, host = e.partition("@")
    if local in {"test", "example", "noreply", "no-reply", "email", "your-email", "name", "info@example"}:
        return False
    if host.startswith("example.") or "sentry" in host or host.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return False
    return True


def _unique(values: list[str], limit: int) -> list[str]:
    seen, out = set(), []
    for v in values:
        k = v.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(v.strip())
    return out[:limit]


# Банки, платёжные и кредитные партнёры, доставка — упоминаются на сайтах, но это не владелец сайта
PARTNER_MARKERS = ("банк", "тинькофф", "сбер", "яндекс", "озон", "вайлдберриз", "кредит", "финанс", "страхов",
                   "лизинг", "сдэк", "деловые линии", "почта россии", "пэк", "робокасса", "юкасса", "долями",
                   "сплит", "мтс", "мегафон", "билайн", "ростелеком")


# ИНН партнёров, которые часто стоят на чужих сайтах (Сплит, рассрочка, оплата, маркетплейсы)
PARTNER_INNS = {
    "7736207543",  # ООО «Яндекс»
    "7710140679",  # АО «ТБанк»
    "7707083893",  # ПАО Сбербанк
    "7702070139",  # Банк ВТБ
    "7728168971",  # АО «Альфа-Банк»
    "7704217370",  # ООО «Интернет Решения» (Ozon)
    "7721546864",  # ООО «Вайлдберриз»
    "9714053621",  # ООО «РВБ» (Wildberries)
    "7750005725",  # ООО «Озон Банк»
}


def _clean_legal(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" .,;:-")
    return re.sub(r"[«»\"„“”]", '"', value)


def _is_partner(name: str) -> bool:
    low = name.lower()
    return any(m in low for m in PARTNER_MARKERS)


# --- обход ---------------------------------------------------------------------
@dataclass
class Page:
    url: str  # итоговый URL после редиректов
    status: int
    html: str
    is_html: bool = True


Fetcher = Callable[[str], "tuple[Page | None, str | None]"]  # url → (страница, ошибка)


def _looks_blocked(page: Page) -> bool:
    """Заглушка защиты: маркер в начале страницы или код 403/429 на небольшой странице.

    Большая страница с кодом 403 — это уже настоящий сайт (браузер прошёл проверку, код остался от первого ответа).
    """
    small = len(page.html) < 60000
    head = page.html[:8000].lower() if page.is_html else ""
    if small and any(m in head for m in BLOCK_MARKERS):
        return True
    return page.status in BLOCK_STATUSES and small


def _candidate_links(html: str, base_url: str, domain: str) -> list[str]:
    scored: dict[str, float] = {}
    tree = HTMLParser(html)
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href)
        p = urlparse(url)
        if p.scheme not in ("http", "https") or normalize_domain(p.netloc) != domain:
            continue
        if p.path.lower().endswith((".pdf", ".jpg", ".png", ".zip", ".doc", ".docx", ".xlsx")):
            continue
        text = f"{p.path} {a.text(strip=True)}".lower()
        score = sum(6 for t in LINK_TERMS if t in text)
        if any(t in text for t in ("реквизит", "rekvizit", "requisit", "оферт", "oferta")):
            score += 10
        if score:
            clean = urlunparse((p.scheme, p.netloc, p.path or "/", "", "", ""))
            scored[clean] = max(score, scored.get(clean, 0))
    origin = urlunparse((urlparse(base_url).scheme, urlparse(base_url).netloc, "", "", "", ""))
    for i, path in enumerate(COMMON_PATHS):
        scored.setdefault(urljoin(origin, path), 5 - i * 0.1)
    return [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])]


def _crawl(domain: str, fetch: Fetcher) -> SiteResult:
    """Общая логика обхода: главная → до MAX_PAGES страниц с реквизитами → извлечение данных."""
    result = SiteResult(domain=domain)
    entry, err = None, None
    for url in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
        entry, err = fetch(url)
        if entry is not None:
            break
    if entry is None:
        result.status, result.error = "down", err
        return result
    result.final_url = entry.url
    if _looks_blocked(entry):
        result.status, result.error = "blocked", f"HTTP {entry.status}"
        return result
    if entry.status >= 400 or not entry.is_html:
        result.status, result.error = "down", f"HTTP {entry.status}"
        return result

    pages = [entry.html]
    title = HTMLParser(entry.html).css_first("title")
    result.title = title.text(strip=True)[:200] if title else None
    seen = {entry.url.rstrip("/")}
    for url in _candidate_links(entry.html, entry.url, domain):
        if len(pages) >= MAX_PAGES:
            break
        if url.rstrip("/") in seen:
            continue
        seen.add(url.rstrip("/"))
        page, _ = fetch(url)
        if page is not None and page.status < 400 and page.is_html and not _looks_blocked(page):
            seen.add(page.url.rstrip("/"))
            pages.append(page.html)
    result.pages = len(pages)
    _extract(result, pages)
    return result


def crawl_site(domain: str, *, transport: httpx.BaseTransport | None = None) -> SiteResult:
    """Быстрый обход обычными HTTP-запросами."""
    with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True, verify=False,
                      transport=transport) as client:

        def fetch(url: str) -> tuple[Page | None, str | None]:
            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                return None, exc.__class__.__name__
            ctype = resp.headers.get("content-type", "").lower()
            return Page(str(resp.url), resp.status_code, resp.text, not ctype or "html" in ctype), None

        return _crawl(domain, fetch)


# --- браузер (Playwright) для сайтов с JS-защитой ------------------------------
BROWSER_NAV_TIMEOUT_MS = 30_000
BROWSER_CHALLENGE_WAIT_S = 20


_XVFB_LOCK = threading.Lock()
_XVFB: subprocess.Popen | None = None
XVFB_DISPLAY = ":99"


def _ensure_xvfb() -> str | None:
    """Запускает Xvfb один раз на процесс (только Linux без DISPLAY). Возвращает номер дисплея или None."""
    global _XVFB
    with _XVFB_LOCK:
        if _XVFB is not None and _XVFB.poll() is None:
            return XVFB_DISPLAY
        binary = shutil.which("Xvfb")
        if not binary:
            return None
        _XVFB = subprocess.Popen(
            [binary, XVFB_DISPLAY, "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        lock_file = f"/tmp/.X11-unix/X{XVFB_DISPLAY[1:]}"
        for _ in range(50):  # до 5 секунд ждём, пока дисплей поднимется
            if os.path.exists(lock_file) or _XVFB.poll() is not None:
                break
            time.sleep(0.1)
        if _XVFB.poll() is not None:  # упал (например, дисплей :99 уже занят чужим Xvfb)
            _XVFB = None
            return XVFB_DISPLAY if os.path.exists(lock_file) else None
        return XVFB_DISPLAY


class BrowserCrawler:
    """Один Chromium на всю задачу; на каждый сайт — новый контекст (свои cookies).

    Использовать из одного потока: with BrowserCrawler() as b: b.crawl("site.ru")
    """

    def __init__(self, headless: bool = False):
        # Видимое окно проходит больше защит (BotHunt и др.). На сервере без экрана — через Xvfb (см. Dockerfile).
        self.headless = headless

    def __enter__(self) -> "BrowserCrawler":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            headless, env = self.headless, None
            if not headless and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
                display = _ensure_xvfb()  # сервер без экрана: поднимаем виртуальный дисплей
                if display:
                    env = {**os.environ, "DISPLAY": display}
                else:
                    headless = True  # Xvfb нет — работаем без окна (часть защит не пройдём)
            self._browser = self._pw.chromium.launch(
                headless=headless, env=env,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception:
            self._pw.stop()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._browser.close()
        self._pw.stop()

    def crawl(self, domain: str) -> SiteResult:
        context = self._browser.new_context(
            user_agent=HEADERS["User-Agent"], locale="ru-RU", viewport={"width": 1366, "height": 900},
            ignore_https_errors=True, extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        def fetch(url: str) -> tuple[Page | None, str | None]:
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_NAV_TIMEOUT_MS)
                status = resp.status if resp else 200
                html = page.content()
                # JS-проверка: ждём, пока защита сама перезагрузит страницу на настоящий сайт
                deadline = time.monotonic() + BROWSER_CHALLENGE_WAIT_S
                while _looks_blocked(Page(page.url, status, html)) and time.monotonic() < deadline:
                    page.wait_for_timeout(1500)
                    html = page.content()
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                    html = page.content()
                except Exception:  # noqa: BLE001 — сеть может не успокоиться, нам хватит DOM
                    pass
                result_page = Page(page.url, status, html)
                if status >= 400 and not _looks_blocked(result_page):
                    result_page.status = 200  # проверка пройдена, код остался от первого ответа
                return result_page, None
            except Exception as exc:  # noqa: BLE001
                return None, exc.__class__.__name__

        try:
            result = _crawl(domain, fetch)
            if result.error:
                result.error = "браузер: " + result.error
            return result
        finally:
            context.close()


def browser_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def _extract(result: SiteResult, pages: list[str]) -> None:
    inns: Counter[str] = Counter()
    near_inn: dict[str, Counter[str]] = {}  # юрлица в ±300 символов от ИНН
    legal, emails, phones, socials = [], [], [], []
    signals: set[str] = set()
    metrika: list[str] = []
    for html in pages:
        tree = HTMLParser(html)
        for node in tree.css("script, style, noscript"):
            if node.tag != "script":
                node.decompose()
        text = (tree.body or tree.root).text(separator=" ", strip=True) if (tree.body or tree.root) else ""
        lower_html = html.lower()

        page_legal = [(m.start(), _clean_legal(m.group(1))) for m in LEGAL_RE.finditer(text)]
        page_legal = [(pos, name) for pos, name in page_legal if name and not _is_partner(name)]
        legal += [name for _, name in page_legal]
        for m in INN_RE.finditer(text):
            if inn_is_valid(m.group(1)) and m.group(1) not in PARTNER_INNS:
                inns[m.group(1)] += 1
                near = near_inn.setdefault(m.group(1), Counter())
                for pos, name in page_legal:
                    if abs(pos - m.start()) <= 300:
                        near[name] += 1
        emails += [e for e in EMAIL_RE.findall(html) if _useful_email(e, result.domain)]
        phones += [p for p in (normalize_phone(x) for x in PHONE_RE.findall(text)) if p]
        for a in tree.css("a[href]"):
            href = a.attributes.get("href") or ""
            host = urlparse(href if "//" in href else "").netloc.lower().removeprefix("www.")
            if host in SOCIAL_HOSTS and urlparse(href).path.strip("/"):
                socials.append(href.split("?")[0])
            if href.startswith("tel:"):
                p = normalize_phone(href[4:])
                if p:
                    phones.insert(0, p)
            if href.startswith("mailto:"):
                e = href[7:].split("?")[0].strip()
                if EMAIL_RE.fullmatch(e) and _useful_email(e, result.domain):
                    emails.insert(0, e)
        for code, _, markers in SIGNALS:
            if code == "gads":
                if AW_RE.search(html) or "googleadservices.com" in lower_html:
                    signals.add(code)
            elif any(mk in lower_html for mk in markers):
                signals.add(code)
        metrika += METRIKA_ID_RE.findall(html)

    if inns:
        result.inn_candidates = [i for i, _ in inns.most_common(5)]
        result.inn = result.inn_candidates[0]
    near = near_inn.get(result.inn or "", Counter())
    names = Counter(x for x in legal if len(x) > 6)
    if near:  # юрлицо рядом с ИНН надёжнее, чем самое частое на сайте
        result.legal_name = near.most_common(1)[0][0]
    elif names and not result.inn:
        result.legal_name = names.most_common(1)[0][0]
    elif names:
        result.legal_name = names.most_common(1)[0][0]
    # свои почты (на домене сайта) — первыми
    emails = _unique(emails, 20)
    emails.sort(key=lambda e: 0 if e.lower().endswith("@" + result.domain) else 1)
    result.emails = emails[:5]
    result.phones = _unique(phones, 5)
    result.socials = _unique(socials, 6)
    result.signals = [code for code, _, _ in SIGNALS if code in signals]
    result.metrika_ids = _unique(metrika, 3)
