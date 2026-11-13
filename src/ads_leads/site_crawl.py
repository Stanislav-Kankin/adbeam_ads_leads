"""Обход сайта рекламодателя: ИНН, юрлицо, контакты, сигналы рекламы и сквозной аналитики.

Частично перенесено из adbeam_wb_marketguru_parser (icp_autosearch.py, site_audit.py).
Только httpx: сайты с JS-защитой (DDoS-Guard, Qrator, Cloudflare) помечаются как blocked.
"""

import re
from collections import Counter
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
                 "checking your browser", "servicepipe", "js-challenge", "id_captcha_frame", "variti")

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
def _fetch(client: httpx.Client, url: str) -> tuple[httpx.Response | None, str | None]:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        return None, exc.__class__.__name__
    return resp, None


def _is_html(resp: httpx.Response) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    return not ctype or "html" in ctype


def _looks_blocked(resp: httpx.Response) -> bool:
    if resp.status_code in BLOCK_STATUSES:
        return True
    head = resp.text[:4000].lower() if _is_html(resp) else ""
    return any(m in head for m in BLOCK_MARKERS) and len(resp.text) < 30000


def _candidate_links(html: str, base_url: str, domain: str) -> list[str]:
    scored: dict[str, int] = {}
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


def crawl_site(domain: str, *, transport: httpx.BaseTransport | None = None) -> SiteResult:
    result = SiteResult(domain=domain)
    with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True, verify=False,
                      transport=transport) as client:
        entry, err = None, None
        for url in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
            entry, err = _fetch(client, url)
            if entry is not None:
                break
        if entry is None:
            result.status, result.error = "down", err
            return result
        if _looks_blocked(entry):
            result.status, result.error = "blocked", f"HTTP {entry.status_code}"
            result.final_url = str(entry.url)
            return result
        if entry.status_code >= 400 or not _is_html(entry):
            result.status, result.error = "down", f"HTTP {entry.status_code}"
            return result

        result.final_url = str(entry.url)
        pages = [entry.text]
        title = HTMLParser(entry.text).css_first("title")
        result.title = title.text(strip=True)[:200] if title else None
        seen = {str(entry.url).rstrip("/")}
        for url in _candidate_links(entry.text, str(entry.url), domain):
            if len(pages) >= MAX_PAGES:
                break
            if url.rstrip("/") in seen:
                continue
            seen.add(url.rstrip("/"))
            resp, _ = _fetch(client, url)
            if resp is not None and resp.status_code < 400 and _is_html(resp) and not _looks_blocked(resp):
                seen.add(str(resp.url).rstrip("/"))
                pages.append(resp.text)
        result.pages = len(pages)
        _extract(result, pages)
    return result


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
