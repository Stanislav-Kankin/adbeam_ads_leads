"""Регионы = региональные базы Яндекса в Keys.so (параметр base)."""

YANDEX_BASES: dict[str, str] = {
    "msk": "Москва",
    "spb": "Санкт-Петербург",
    "ekb": "Екатеринбург",
    "nsk": "Новосибирск",
    "kzn": "Казань",
    "nnv": "Нижний Новгород",
    "che": "Челябинск",
    "sam": "Самара",
    "ufa": "Уфа",
    "rnd": "Ростов-на-Дону",
    "krr": "Краснодар",
    "oms": "Омск",
    "kry": "Красноярск",
    "vrn": "Воронеж",
    "prm": "Пермь",
    "vlg": "Волгоград",
    "sar": "Саратов",
    "tmn": "Тюмень",
    "tom": "Томск",
    "mns": "Минск",
}
DEFAULT_REGIONS = "msk"


def parse_regions(value: str | list[str] | None) -> list[str]:
    """'msk,spb' или ['msk','spb'] → только известные коды, без дублей, в исходном порядке."""
    items = value.split(",") if isinstance(value, str) else (value or [])
    result: list[str] = []
    for code in (c.strip().lower() for c in items):
        if code in YANDEX_BASES and code not in result:
            result.append(code)
    return result


def region_names(codes: list[str]) -> str:
    return ", ".join(YANDEX_BASES[c] for c in codes if c in YANDEX_BASES)
