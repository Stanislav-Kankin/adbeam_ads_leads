"""Автоподбор коммерческих запросов ниши из базы запросов Keys.so (частотности Вордстата).

Ввод — «семена»: товарные группы и бренды ниши («смеситель», «унитаз», «grohe»).
По каждому семени берём фразы «<семя> купить», чистим и оставляем самые частотные.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from ads_leads.keyso import KeysoClient

# Готовые наборы семян для ниш из стратегии. Ключ — слово, которое ищем в названии ниши.
PRESETS: dict[str, list[str]] = {
    "сантехн": [
        "смеситель", "унитаз", "унитаз подвесной", "инсталляция для унитаза", "ванна акриловая", "ванна чугунная",
        "душевая кабина", "душевой уголок", "душевая система", "поддон душевой", "трап душевой", "раковина",
        "тумба с раковиной", "мебель для ванной", "полотенцесушитель", "гигиенический душ", "мойка для кухни",
        "биде", "grohe", "hansgrohe", "geberit", "roca", "jacob delafon", "villeroy boch", "am pm",
        "ideal standard", "cersanit", "kludi", "iddis", "lemark",
    ],
    "мебел": [
        "диван", "угловой диван", "кровать", "кровать двуспальная", "матрас", "шкаф купе", "шкаф распашной",
        "кухня", "кухонный гарнитур", "стол обеденный", "стулья для кухни", "комод", "прихожая",
        "детская кровать", "компьютерное кресло", "мягкая мебель", "гостиная стенка", "тумба под телевизор",
        "askona", "орматек", "hoff", "шатура", "лазурит", "много мебели",
    ],
    "техник": [
        "холодильник", "стиральная машина", "посудомоечная машина", "духовой шкаф", "варочная панель",
        "вытяжка", "плита электрическая", "газовая плита", "морозильная камера", "сушильная машина",
        "встраиваемая техника", "винный шкаф", "bosch", "liebherr", "miele", "electrolux", "smeg",
        "haier", "samsung холодильник", "lg стиральная",
    ],
}

# Минус-слова: маркетплейсы, б/у, информационные и «сделай сам» запросы.
MINUS_WORDS = [
    "авито", "avito", "озон", "ozon", "wildberries", "вайлдберриз", "валдберис", "вб", "яндекс", "маркет",
    "мегамаркет", "алиэкспресс", "aliexpress", "бу", "б/у", "своими", "руками", "ремонт", "как", "что",
    "видео", "фото", "схема", "отзывы", "инструкция", "бесплатно", "игрушечный", "детский", "лего",
    # у брендов сантехники есть посуда и плитка — это не наш спрос
    "посуда", "посуду", "тарелки", "тарелка", "бокалы", "чашки", "плитка", "керамогранит", "ведро",
]

# Чужие города: для ниши по Москве фразы «… в спб» не нужны (и наоборот).
CITY_TOKENS: dict[str, tuple[str, ...]] = {
    "msk": ("москв", "мск", "подмосков"), "spb": ("спб", "питер", "петербург", "санкт"),
    "ekb": ("екатеринбург", "екб"), "nsk": ("новосибирск", "нск"), "kzn": ("казан",),
    "nnv": ("нижн", "новгород"), "che": ("челябинск",), "sam": ("самар",), "ufa": ("уф",),
    "rnd": ("ростов",), "krr": ("краснодар",), "oms": ("омск",), "kry": ("красноярск",), "vrn": ("воронеж",),
    "prm": ("перм",), "vlg": ("волгоград",), "sar": ("саратов",), "tmn": ("тюмен",), "tom": ("томск",),
    "mns": ("минск",),
}
OTHER_CITY_WORDS = (
    "сочи", "крым", "казахстан", "беларус", "тул", "тулe", "ярослав", "иркутск", "калининград", "барнаул",
    "калуг", "хабаровск", "владивосток", "ижевск", "ульяновск", "оренбург", "кемерово", "рязан", "липецк",
    "пенз", "астрахан", "твер", "брянск", "иванов", "курск", "белгород", "владимир", "смоленск", "мурманск",
    "архангельск", "вологд", "чебоксар", "сургут", "ставропол", "махачкал", "севастопол", "анап",
    "новороссийск", "тамбов", "киров", "кемеров", "новокузнецк", "набережн", "тольятти", "магнитогорск",
    "симферопол", "ростове", "уфе", "уфа", "орле", "орел", "орёл", "пскове", "псков", "омске",
)
NOISE_WORDS = tuple(MINUS_WORDS)


@dataclass
class Suggestion:
    text: str
    freq: int  # wsk — «!точная» частотность Вордстата
    seed: str


def preset_seeds(niche_name: str) -> list[str]:
    name = niche_name.lower()
    for key, seeds in PRESETS.items():
        if key in name:
            return seeds
    return [niche_name.strip().lower()]


def canonical(phrase: str) -> str:
    """Ключ для дедупа: одинаковый набор слов в любом порядке."""
    return " ".join(sorted(re.findall(r"[\w/]+", phrase.lower())))


def _matches(word: str, token: str) -> bool:
    # короткие токены («спб», «уф») — только целым словом, длинные — как префикс («москв» → «москве»)
    return word == token or (len(token) > 3 and word.startswith(token))


def _foreign_city(phrase: str, regions: list[str]) -> bool:
    banned = [t for r, toks in CITY_TOKENS.items() if r not in regions for t in toks] + list(OTHER_CITY_WORDS)
    return any(_matches(w, t) for w in phrase.lower().split() for t in banned)


def is_noise(phrase: str, regions: list[str]) -> bool:
    """Чужой город, артикул (длинные цифры) или минус-слово, которое Keys.so пропустил."""
    words = phrase.lower().split()
    if any(re.fullmatch(r"\d{5,}", w) for w in words):
        return True
    if any(w in NOISE_WORDS for w in words):
        return True
    return _foreign_city(phrase, regions)


def build_filter(seed: str) -> str:
    return "wordLIKE" + f"{seed} купить" + "".join(f"\n-{w}" for w in MINUS_WORDS)


def suggest_queries(
    client: KeysoClient,
    seeds: list[str],
    regions: list[str],
    *,
    per_seed: int = 20,
    min_freq: int = 20,
    on_progress: Callable[[int, str], None] | None = None,
) -> list[Suggestion]:
    seen: set[str] = set()
    result: list[Suggestion] = []
    for i, seed in enumerate(seeds):
        if on_progress:
            on_progress(int(i / max(len(seeds), 1) * 100), seed)
        taken = 0
        for row in client.keyword_base(build_filter(seed)):
            text = " ".join(str(row.get("word", "")).lower().split())
            freq = int(row.get("wsk") or 0)
            if freq < min_freq:
                break  # дальше только реже (сортировка по wsk)
            key = canonical(text)
            if not text or key in seen or is_noise(text, regions):
                continue
            seen.add(key)
            result.append(Suggestion(text=text, freq=freq, seed=seed))
            taken += 1
            if taken >= per_seed:
                break
    return result
