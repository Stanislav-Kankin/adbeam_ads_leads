"""Адаптер выгрузки Контур.Продаж / Контур.Поиска клиентов (Excel, лист «Контрагенты»).

Колонки ищутся по названию (порядок не важен). Склейка с компаниями — по ИНН.
Чтобы перейти на другой источник (Фокус и т.п.), достаточно своего parse_*, отдающего те же поля.
"""

import io
import re
from datetime import date, datetime
from typing import Any

import openpyxl

# заголовок в выгрузке → поле Company
COLUMNS: dict[str, str] = {
    "наименование": "kontur_name",
    "инн": "inn",
    "кпп": "kpp",
    "огрн": "ogrn",
    "дата регистрации": "reg_date",
    "адрес": "address",
    "регион регистрации": "region",
    "статус": "legal_status",
    "реестр мсп": "msp",
    "фио руководителя": "director_name",
    "иннфл руководителя": "director_inn",
    "должность руководителя": "director_post",
    "номер телефона": "kontur_phones",
    "электронная почта": "kontur_emails",
    "ссылка на сайт": "kontur_site",
    "основной вид деятельности": "okved_main",
    "выручка": "revenue",
    "баланс": "balance",
    "чистая прибыль/ убыток": "profit",
    "чистая прибыль/убыток": "profit",
    "арбитраж (ответчик)": "arbitration",
    "количество сотрудников": "employees",
}
MONEY_FIELDS = {"revenue", "balance", "profit", "arbitration"}
INT_FIELDS = {"employees"}
MULTILINE_FIELDS = {"kontur_phones", "kontur_emails"}
REQUIRED = {"inn"}


class KonturFormatError(ValueError):
    pass


def normalize_inn(value: Any) -> str | None:
    """Excel любит терять ведущие нули: 32616721612 → 032616721612."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value).split(".")[0])
    if len(digits) in (9, 11):
        digits = "0" + digits
    return digits if len(digits) in (10, 12) else None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = re.sub(r"[^\d\-]", "", str(value).replace(",", ".").split(".")[0])
    return int(s) if s not in ("", "-") else None


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%d.%m.%Y")
    text = str(value).strip()
    return text or None


def parse_kontur_xlsx(content: bytes) -> list[dict]:
    """Строки выгрузки → список словарей с полями Company (inn обязателен)."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise KonturFormatError(f"Не удалось открыть Excel: {exc}") from exc
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            continue
        mapping = {i: COLUMNS[str(h).strip().lower()] for i, h in enumerate(header)
                   if h is not None and str(h).strip().lower() in COLUMNS}
        if not REQUIRED <= set(mapping.values()):
            continue  # не тот лист
        result = []
        for row in rows:
            item: dict[str, Any] = {}
            for i, field in mapping.items():
                value = row[i] if i < len(row) else None
                if field == "inn":
                    item[field] = normalize_inn(value)
                elif field in MONEY_FIELDS or field in INT_FIELDS:
                    item[field] = _to_int(value)
                elif field in MULTILINE_FIELDS:
                    text = _to_text(value)
                    item[field] = "; ".join(x.strip() for x in text.splitlines() if x.strip()) if text else None
                else:
                    item[field] = _to_text(value)
            if item.get("inn"):
                result.append(item)
        return result
    raise KonturFormatError("В файле не найден лист с колонкой «ИНН». Это точно выгрузка из Контура?")


def build_inn_xlsx(rows: list[tuple[str, str, str | None]]) -> bytes:
    """Файл для загрузки в Контур: ИНН (текстом, чтобы не потерять нули) + домен и юрлицо для ориентира."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ИНН"
    ws.append(["ИНН", "Сайт", "Юрлицо с сайта"])
    for inn, domain, legal in rows:
        ws.append([inn, domain, legal or ""])
        ws.cell(row=ws.max_row, column=1).number_format = "@"
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 45
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
