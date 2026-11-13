"""Итоговое досье в Excel: одна строка на компанию, сначала обязательные колонки, затем справочные."""

import io
from dataclasses import dataclass
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ads_leads.models import Company, Niche
from ads_leads.site_crawl import END_TO_END, SIGNAL_NAMES

NAVY, YELLOW = "14254E", "F1B62B"
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
HEADER_FONT = Font(color="FFFFFF", bold=True)
PRIORITY_FILLS = {"A": PatternFill("solid", fgColor="D9F2E3"), "B": PatternFill("solid", fgColor="FFF2CC"),
                  "C": PatternFill("solid", fgColor="F2F2F2")}
E2E_FILL = PatternFill("solid", fgColor="FDF3D8")
WRAP = Alignment(wrap_text=True, vertical="top")

# (заголовок, ширина) — первые колонки в порядке, который попросил пользователь
COLUMNS = [
    ("Сайт", 26), ("Название компании", 34), ("Почты с сайта", 30), ("Почты из Контура", 30),
    ("Телефоны с сайта", 20), ("Телефоны из Контура", 20), ("Траты на Директ, ₽/мес (оценка Keys.so)", 18),
    ("Сигналы", 34), ("Выручка (оборот), млн ₽", 14), ("Чистая прибыль, млн ₽", 14), ("Баланс, млн ₽", 12),
    ("Ниша", 16),
    # справочные
    ("Приоритет", 10), ("Почему такой приоритет", 34), ("ИНН", 14), ("Руководитель", 28), ("Должность", 22),
    ("Сотрудники", 11), ("Статус компании", 22), ("МСП", 10), ("Регион", 18), ("Доля запросов ниши, %", 12),
    ("Запросов ниши с рекламой", 12), ("Кликов из Директа в день", 12), ("Соцсети", 30), ("Статус сайта", 14),
]


@dataclass
class DossierRow:
    company: Company
    niches: list[str]
    share: float | None = None
    keys_cnt: int | None = None


def _mln(value: int | None) -> float | None:
    return None if value is None else round(value / 1e6, 1)


def _lines(value: str | None) -> str:
    return "\n".join(x for x in (value or "").split("; ") if x)


def has_contacts(c: Company) -> bool:
    return bool(c.emails or c.phones or c.kontur_emails or c.kontur_phones)


def priority(c: Company) -> tuple[str, str]:
    """A — крупный, активный, уже считает сквозную аналитику; B — средний или крупный без аналитики; C — остальное."""
    e2e = bool(set(c.signal_list) & END_TO_END)
    rev = c.revenue
    if not c.is_active:
        return "C", f"статус: {c.legal_status}"
    if rev is not None and rev >= 500e6 and e2e:
        return "A", "выручка от 500 млн и есть сквозная аналитика/коллтрекинг"
    if rev is not None and rev >= 200e6:
        return "B", "выручка от 200 млн" + ("" if e2e else ", сквозной аналитики на сайте не видно")
    if rev is None and e2e:
        return "B", "нет данных Контура, но есть сквозная аналитика"
    if rev is None:
        return "C", "нет данных Контура (выручка неизвестна)"
    return "C", "выручка меньше 200 млн"


def collect_rows(niches: list[Niche]) -> tuple[list[DossierRow], list[DossierRow]]:
    """(с контактами или ИНН, без контактов и без ИНН). Исключённые домены (маркетплейсы и т.п.) не попадают."""
    rows: dict[int, DossierRow] = {}
    for niche in niches:
        excluded = {a.company_id for a in niche.advertisers if a.excluded}
        stats = {}
        for a in niche.advertisers:
            if not a.excluded:
                best = stats.get(a.company_id)
                if best is None or a.share > best[0]:
                    stats[a.company_id] = (a.share, a.keys_cnt)
        for c in niche.companies:
            if c.id in excluded:
                continue
            row = rows.setdefault(c.id, DossierRow(company=c, niches=[]))
            row.niches.append(niche.name)
            if c.id in stats and (row.share is None or stats[c.id][0] > row.share):
                row.share, row.keys_cnt = stats[c.id]
    order = {"A": 0, "B": 1, "C": 2}
    all_rows = sorted(rows.values(), key=lambda r: (order[priority(r.company)[0]], -(r.company.revenue or -1),
                                                     -(r.company.ad_cost or -1)))
    main = [r for r in all_rows if r.company.inn or has_contacts(r.company)]
    rest = [r for r in all_rows if not (r.company.inn or has_contacts(r.company))]
    return main, rest


def _values(r: DossierRow) -> list:
    c = r.company
    prio, why = priority(c)
    signals = ", ".join(SIGNAL_NAMES.get(s, s) for s in c.signal_list)
    site_status = {"ok": "открыт", "blocked": "защита от ботов", "down": "не открылся"}.get(c.site_status or "",
                                                                                           "не проверен")
    return [
        c.domain, c.kontur_name or c.legal_name or "", _lines(c.emails), _lines(c.kontur_emails), _lines(c.phones),
        _lines(c.kontur_phones), c.ad_budget_month, signals, _mln(c.revenue), _mln(c.profit), _mln(c.balance),
        ", ".join(r.niches),
        prio, why, c.inn or "", c.director_name or "", c.director_post or "", c.employees, c.legal_status or "",
        c.msp or "", c.region or "", round(r.share, 1) if r.share is not None else None, r.keys_cnt,
        c.ad_traffic, _lines(c.socials), site_status,
    ]


def _write_sheet(ws, rows: list[DossierRow]) -> None:
    ws.append([h for h, _ in COLUMNS])
    for i, (_, width) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=i)
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[1].height = 45
    for r in rows:
        ws.append(_values(r))
        n = ws.max_row
        ws.cell(row=n, column=1).hyperlink = f"https://{r.company.domain}"
        ws.cell(row=n, column=1).font = Font(color="1F4E9E", underline="single")
        for col in range(1, len(COLUMNS) + 1):
            ws.cell(row=n, column=col).alignment = WRAP
        prio_cell = ws.cell(row=n, column=13)
        prio_cell.fill = PRIORITY_FILLS.get(prio_cell.value, PatternFill())
        prio_cell.font = Font(bold=True)
        if set(r.company.signal_list) & END_TO_END:
            ws.cell(row=n, column=8).fill = E2E_FILL
        for col in (7, 9, 10, 11, 18, 24):
            ws.cell(row=n, column=col).number_format = "#,##0.#" if col in (9, 10, 11) else "#,##0"
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(ws.max_row, 1)}"


def build_dossier(niches: list[Niche], title: str) -> bytes:
    main, rest = collect_rows(niches)
    wb = Workbook()
    ws = wb.active
    ws.title = "Досье"
    _write_sheet(ws, main)

    summary = wb.create_sheet("Сводка")
    prios = [priority(r.company)[0] for r in main]
    lines = [
        ("Досье", title), ("Сформировано", datetime.now().strftime("%d.%m.%Y %H:%M")), ("", ""),
        ("Компаний в досье (есть ИНН или контакты)", len(main)), ("Приоритет A", prios.count("A")),
        ("Приоритет B", prios.count("B")), ("Приоритет C", prios.count("C")),
        ("С ИНН", sum(1 for r in main if r.company.inn)),
        ("Без ИНН, но с контактами", sum(1 for r in main if not r.company.inn)),
        ("С данными Контура", sum(1 for r in main if r.company.kontur_loaded_at)),
        ("Со сквозной аналитикой/коллтрекингом", sum(1 for r in main if set(r.company.signal_list) & END_TO_END)),
        ("Без контактов и ИНН (лист «Без контактов»)", len(rest)), ("", ""),
        ("Приоритет A", "выручка от 500 млн ₽ + сквозная аналитика/коллтрекинг на сайте, компания действующая"),
        ("Приоритет B", "выручка от 200 млн ₽, или нет данных Контура, но есть сквозная аналитика"),
        ("Приоритет C", "выручка меньше 200 млн ₽, нет данных Контура, банкротство/ликвидация"),
        ("Траты на Директ", "оценка Keys.so по всему контексту домена (не только эта ниша), без учёта таргетингов"),
    ]
    for k, v in lines:
        summary.append([k, v])
    summary.column_dimensions["A"].width = 46
    summary.column_dimensions["B"].width = 90
    summary["A1"].font = summary["B1"].font = Font(bold=True, size=13)

    if rest:
        _write_sheet(wb.create_sheet("Без контактов"), rest)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
