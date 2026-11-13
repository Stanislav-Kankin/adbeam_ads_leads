"""Домены, которые не являются нашими лидами: маркетплейсы, агрегаторы, доски, поисковики.

Сравнение идёт по нормализованному домену (normalize_domain): market.yandex.ru → yandex.ru.
"""

EXCLUDED_DOMAINS: dict[str, str] = {
    # маркетплейсы
    "wildberries.ru": "маркетплейс", "wb.ru": "маркетплейс", "ozon.ru": "маркетплейс",
    "megamarket.ru": "маркетплейс", "sbermegamarket.ru": "маркетплейс", "aliexpress.ru": "маркетплейс",
    "aliexpress.com": "маркетплейс", "lamoda.ru": "маркетплейс", "kazanexpress.ru": "маркетплейс",
    "magnit.ru": "маркетплейс", "goods.ru": "маркетплейс", "sima-land.ru": "маркетплейс (опт)",
    # Яндекс, VK, Google и их сервисы (Маркет, Директ, Дзен)
    "yandex.ru": "поисковик/Маркет", "ya.ru": "поисковик", "google.com": "поисковик", "google.ru": "поисковик",
    "dzen.ru": "площадка", "vk.com": "соцсеть", "ok.ru": "соцсеть", "t.me": "мессенджер",
    "youtube.com": "видео", "rutube.ru": "видео",
    # доски объявлений и агрегаторы
    "avito.ru": "доска объявлений", "youla.ru": "доска объявлений", "2gis.ru": "справочник",
    "zoon.ru": "справочник", "otzovik.com": "отзывы", "irecommend.ru": "отзывы",
    "price.ru": "агрегатор цен", "e-katalog.ru": "агрегатор цен", "pulscen.ru": "агрегатор",
    "tiu.ru": "агрегатор", "satom.ru": "агрегатор", "blizko.ru": "агрегатор", "regmarkets.ru": "агрегатор",
    "mail.ru": "портал", "rambler.ru": "портал", "wikipedia.org": "энциклопедия",
    # крупные федеральные сети (не наш ICP для параллельного теста)
    "dns-shop.ru": "федеральная сеть", "mvideo.ru": "федеральная сеть", "eldorado.ru": "федеральная сеть",
    "leroymerlin.ru": "федеральная сеть", "lemanapro.ru": "федеральная сеть", "obi.ru": "федеральная сеть",
    "petrovich.ru": "федеральная сеть", "vseinstrumenti.ru": "федеральная сеть", "citilink.ru": "федеральная сеть",
    "hoff.ru": "федеральная сеть", "castorama.ru": "федеральная сеть",
    "baucenter.ru": "федеральная сеть", "maxidom.ru": "федеральная сеть", "stroylandiya.ru": "федеральная сеть",
    "220-volt.ru": "федеральная сеть", "domotex.ru": "федеральная сеть",
    # глобальные сервисы и площадки, которые Keys.so иногда показывает рекламодателями по товарным запросам
    "apple.com": "не по теме", "appsflyer.com": "не по теме", "tilda.ws": "конструктор сайтов",
    "tilda.cc": "конструктор сайтов", "made-in-china.com": "международная площадка", "alibaba.com": "международная площадка",
    "amazon.com": "международная площадка", "ebay.com": "международная площадка", "temu.com": "маркетплейс",
    "joom.com": "маркетплейс", "aliexpress.us": "маркетплейс",
}


def exclusion_reason(domain: str) -> str | None:
    return EXCLUDED_DOMAINS.get(domain)
