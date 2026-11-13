"""Нормализация доменов — ключ дедупа компаний."""

from urllib.parse import urlsplit

import tldextract

# Встроенный снапшот списка суффиксов, без похода в интернет.
_extract = tldextract.TLDExtract(suffix_list_urls=())


def normalize_domain(value: str) -> str | None:
    """'https://www.Shop.santeh.ru/catalog' → 'santeh.ru'. Мусор → None."""
    value = (value or "").strip().lower()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    host = urlsplit(value).hostname or ""
    parts = _extract(host)
    if not parts.domain or not parts.suffix:
        return None
    return f"{parts.domain}.{parts.suffix}"
