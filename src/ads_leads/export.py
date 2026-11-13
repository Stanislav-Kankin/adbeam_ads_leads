"""Итоговое досье в Excel: одна строка на компанию, сначала обязательные колонки, затем справочные."""

import io
from dataclasses import dataclass
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ads_leads.filters import exclusion_reason
from ads_leads.models import Company, Niche
from ads_leads.site_crawl import (CALLTRACKING, E2E_ANALYTICS, SIGNAL_NAMES, TRACKING, clean_emails, clean_phones,
                                  is_mobile, phone_key)

# Порог отбора из гипотезы инициативы: Директ от 300 тыс. ₽/мес. Оценка Keys.so, единицы ещё сверяем.
MIN_BUDGET = 300_000
MIN_REVENUE = 200e6

NAVY, YELLOW = "14254E", "F1B62B"
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
HEADER_FONT = Font(color="FFFFFF", bold=True)
PRIORITY_FILLS = {"A": PatternFill("solid", fgColor="D9F2E3"), "B": PatternFill("solid", fgColor="FFF2CC"),
                  "C": PatternFill("solid", fgColor="F2F2F2")}
E2E_FILL = PatternFill("solid", fgColor="F9DC8C")  # сквозная аналитика — ярче
CALLTRACKING_FILL = PatternFill("solid", fgColor="FDF3D8")
WRAP = Alignment(wrap_text=True, vertical="top")

# (заголовок, ширина) — первые колонки в порядке, который попросил пользователь
COLUMNS = [
    ("Сайт", 26), ("ИНН", 14), ("Название компании", 34), ("Почты с сайта", 30), ("Почты из Контура", 30),
    ("Телефоны с сайта", 20), ("Телефоны из Контура", 20), ("Траты на Директ, ₽/мес (оценка Keys.so)", 18),
    ("Сигналы", 34), ("Выручка (оборот), млн ₽", 14), ("Чистая прибыль, млн ₽", 14), ("Баланс, млн ₽", 12),
    ("Ниша", 16),
    # справочные
    ("Приоритет", 10), ("Почему такой приоритет", 34), ("Отбор", 10), ("Руководитель", 28), ("Должность", 22),
    ("Мобильные из Контура, которых нет на сайте", 20), ("Сотрудники", 11), ("Статус компании", 22), ("МСП", 10),
    ("Регион", 18), ("Доля запросов ниши, %", 12), ("Запросов ниши с рекламой", 12), ("Кликов из Директа в день", 12),
    ("Соцсети", 30), ("Статус сайта", 14),
]
COL = {name: i for i, (name, _) in enumerate(COLUMNS, 1)}
MLN_COLS = {COL["Выручка (оборот), млн ₽"], COL["Чистая прибыль, млн ₽"], COL["Баланс, млн ₽"]}
INT_COLS = {COL["Траты на Директ, ₽/мес (оценка Keys.so)"], COL["Сотрудники"], COL["Кликов из Директа в день"]}


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


def _emails(value: str | None) -> str:
    return "\n".join(clean_emails(value))


def _phones(value: str | None) -> str:
    return "\n".join(clean_phones(value))


def private_mobiles(c: Company) -> list[str]:
    """Мобильные из Контура, которых нет на сайте: скорее личные (руководитель, бухгалтер), а не общая линия."""
    on_site = {phone_key(p) for p in clean_phones(c.phones)}
    return [p for p in clean_phones(c.kontur_phones) if is_mobile(p) and phone_key(p) not in on_site]


def has_contacts(c: Company) -> bool:
    return bool(clean_emails(c.emails) or clean_phones(c.phones) or clean_emails(c.kontur_emails)
                or clean_phones(c.kontur_phones))


def has_e2e(c: Company) -> bool:
    return bool(set(c.signal_list) & E2E_ANALYTICS)


def has_calltracking(c: Company) -> bool:
    return bool(set(c.signal_list) & CALLTRACKING)


def priority(c: Company) -> tuple[str, str]:
    """A — тратит на Директ от 300 тыс., есть чем сравнить подрядчиков, не мелкая; B — тратит, но без аналитики
    или выручка меньше 200 млн; C — трат мало (или нет оценки), компания не действующая."""
    if not c.is_active:
        return "C", f"статус: {c.legal_status}"
    budget = c.ad_budget_month
    if budget is None:
        return "C", "нет оценки трат на Директ"
    if budget < MIN_BUDGET:
        return "C", "траты на Директ меньше 300 тыс. ₽/мес"
    tracking = "сквозная аналитика" if has_e2e(c) else "коллтрекинг" if has_calltracking(c) else None
    small = c.revenue is not None and c.revenue < MIN_REVENUE
    if tracking and not small:
        return "A", f"Директ от 300 тыс. ₽/мес, есть {tracking}" + ("" if c.revenue is not None else
                                                                  ", выручка неизвестна")
    if small:
        return "B", "Директ от 300 тыс. ₽/мес, но выручка меньше 200 млн"
    return "B", "Директ от 300 тыс. ₽/мес, аналитики на сайте не видно"


def is_picked(c: Company) -> bool:
    """В шорт-листе: отмечена вручную или (по умолчанию) приоритет A/B, пока галочку не сняли."""
    if c.pick is not None:
        return c.pick == "yes"
    return priority(c)[0] in "AB"


def collect_rows(niches: list[Niche]) -> tuple[list[DossierRow], list[DossierRow], list[DossierRow]]:
    """(лист «Досье»: есть ИНН или контакты; без ИНН и контактов; отмечены «не берём»).
    Автоматически исключённые домены (маркетплейсы и т.п.) не попадают никуда."""
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
            if c.id in excluded or exclusion_reason(c.domain):  # список исключений мог пополниться после сбора
                continue
            row = rows.setdefault(c.id, DossierRow(company=c, niches=[]))
            row.niches.append(niche.name)
            if c.id in stats and (row.share is None or stats[c.id][0] > row.share):
                row.share, row.keys_cnt = stats[c.id]
    order = {"A": 0, "B": 1, "C": 2}
    all_rows = sorted(rows.values(), key=lambda r: (not is_picked(r.company), order[priority(r.company)[0]],
                                                     -(r.company.ad_cost or -1), -(r.company.revenue or -1)))
    rejected = [r for r in all_rows if r.company.pick == "no"]
    kept = [r for r in all_rows if r.company.pick != "no"]
    main = [r for r in kept if r.company.inn or has_contacts(r.company)]
    rest = [r for r in kept if not (r.company.inn or has_contacts(r.company))]
    return main, rest, rejected


def _values(r: DossierRow) -> list:
    c = r.company
    prio, why = priority(c)
    signals = ", ".join(SIGNAL_NAMES.get(s, s) for s in c.signal_list)
    site_status = {"ok": "открыт", "blocked": "защита от ботов", "down": "не открылся"}.get(c.site_status or "",
                                                                                           "не проверен")
    return [
        c.domain, c.inn or "", c.kontur_name or c.legal_name or "", _emails(c.emails), _emails(c.kontur_emails),
        _phones(c.phones), _phones(c.kontur_phones), c.ad_budget_month, signals, _mln(c.revenue), _mln(c.profit),
        _mln(c.balance), ", ".join(r.niches),
        prio, why, "берём" if is_picked(c) else "", c.director_name or "", c.director_post or "",
        "\n".join(private_mobiles(c)), c.employees, c.legal_status or "", c.msp or "", c.region or "",
        round(r.share, 1) if r.share is not None else None, r.keys_cnt, c.ad_traffic, _lines(c.socials), site_status,
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
        ws.cell(row=n, column=COL["ИНН"]).number_format = "@"
        prio_cell = ws.cell(row=n, column=COL["Приоритет"])
        prio_cell.fill = PRIORITY_FILLS.get(prio_cell.value, PatternFill())
        prio_cell.font = Font(bold=True)
        if has_e2e(r.company):
            ws.cell(row=n, column=COL["Сигналы"]).fill = E2E_FILL
        elif has_calltracking(r.company):
            ws.cell(row=n, column=COL["Сигналы"]).fill = CALLTRACKING_FILL
        if is_picked(r.company):
            ws.cell(row=n, column=COL["Отбор"]).font = Font(bold=True, color="1E7B45")
        for col in MLN_COLS:
            ws.cell(row=n, column=col).number_format = "#,##0.#"
        for col in INT_COLS:
            ws.cell(row=n, column=col).number_format = "#,##0"
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(ws.max_row, 1)}"


def build_dossier(niches: list[Niche], title: str, only_picked: bool = False) -> bytes:
    """Полное досье или шорт-лист (only_picked — только отмеченные «берём»)."""
    main, rest, rejected = collect_rows(niches)
    if only_picked:
        main = [r for r in main + rest if is_picked(r.company)]
        rest, rejected = [], []
    wb = Workbook()
    ws = wb.active
    ws.title = "Шорт-лист" if only_picked else "Досье"
    _write_sheet(ws, main)

    summary = wb.create_sheet("Сводка")
    prios = [priority(r.company)[0] for r in main]
    lines = [
        ("Досье" if not only_picked else "Шорт-лист", title),
        ("Сформировано", datetime.now().strftime("%d.%m.%Y %H:%M")), ("", ""),
        ("Компаний на листе", len(main)), ("Отобрано («берём»)", sum(1 for r in main if is_picked(r.company))),
        ("Приоритет A", prios.count("A")), ("Приоритет B", prios.count("B")), ("Приоритет C", prios.count("C")),
        ("С ИНН", sum(1 for r in main if r.company.inn)),
        ("Без ИНН, но с почтой или телефоном", sum(1 for r in main if not r.company.inn)),
        ("С данными Контура", sum(1 for r in main if r.company.kontur_loaded_at)),
        ("Со сквозной аналитикой", sum(1 for r in main if has_e2e(r.company))),
        ("С коллтрекингом (без сквозной)", sum(1 for r in main if has_calltracking(r.company)
                                             and not has_e2e(r.company))),
        ("С личным мобильным из Контура", sum(1 for r in main if private_mobiles(r.company))),
    ]
    if not only_picked:
        lines += [("Без ИНН и без контактов (лист «Без контактов»)", len(rest)),
                  ("Отмечены «не берём» (лист «Не берём»)", len(rejected))]
    lines += [
        ("", ""),
        ("Приоритет A", "Директ от 300 тыс. ₽/мес + сквозная аналитика или коллтрекинг, выручка от 200 млн "
                        "или неизвестна, компания действующая"),
        ("Приоритет B", "Директ от 300 тыс. ₽/мес, но аналитики на сайте не видно или выручка меньше 200 млн"),
        ("Приоритет C", "Директ меньше 300 тыс. ₽/мес или нет оценки, банкротство/ликвидация"),
        ("Сквозная аналитика", "Roistat, Alytics: связывают расходы на рекламу с продажами"),
        ("Коллтрекинг", "Calltouch, CoMagic/UIS, Callibri, Mango, Alloka: считают звонки по источникам"),
        ("Траты на Директ", "оценка Keys.so по всему контексту домена (не только эта ниша), без учёта таргетингов"),
        ("Мобильные из Контура", "номера +7 9xx, которых нет на сайте: часто личный номер руководителя или "
                                 "бухгалтера. Принадлежность проверяется вручную"),
    ]
    for k, v in lines:
        summary.append([k, v])
    summary.column_dimensions["A"].width = 46
    summary.column_dimensions["B"].width = 90
    summary["A1"].font = summary["B1"].font = Font(bold=True, size=13)

    if rest:
        _write_sheet(wb.create_sheet("Без контактов"), rest)
    if rejected:
        _write_sheet(wb.create_sheet("Не берём"), rejected)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
