import io

import openpyxl
from fastapi.testclient import TestClient

from ads_leads.kontur import normalize_inn, parse_kontur_xlsx

HEADER = ["Наименование", "ИНН", "КПП", "Статус", "Реестр МСП", "ФИО руководителя", "Должность руководителя",
          "Номер телефона", "Электронная почта", "Ссылка на сайт", "Выручка", "Чистая прибыль/ убыток",
          "Количество сотрудников", "Регион регистрации"]


def make_xlsx(rows, header=HEADER) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Контрагенты"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ROWS = [
    ['ООО "Сантех"', "7724419293", "772401001", "Действующее предприятие", "Среднее", "Иванов Иван Иванович",
     "Генеральный Директор", "+7 (495) 111-22-33\n+7 800 555-35-35", "a@santeh.ru\nb@santeh.ru", None,
     "1703080000", "-27365000", 164, "г Москва"],
    # ИНН потерял ведущий ноль (Excel), сайт есть — склеится с компанией без ИНН по сайту
    ["ИП Тихонова", 32616721612, "", "Действующее предприятие", "Микро", "Тихонова Лариса Юрьевна",
     "Индивидуальный предприниматель", "", "", "http://www.dushevoi.ru", "250000000", "1000000", 12, "г Москва"],
    ['ООО "Банкрот"', "7716807310", "", "В отношении юридического лица введено наблюдение", "", "", "", "", "",
     None, "1000", "0", 1, ""],
    ['ООО "Чужая"', "7751246233", "", "Действующее предприятие", "", "", "", "", "", None, "5", "0", 1, ""],
]


def test_parse_and_normalize():
    assert normalize_inn(32616721612) == "032616721612"
    assert normalize_inn("7724419293.0") == "7724419293"
    rows = parse_kontur_xlsx(make_xlsx(ROWS))
    assert rows[0]["inn"] == "7724419293" and rows[0]["revenue"] == 1703080000 and rows[0]["profit"] == -27365000
    assert rows[0]["kontur_phones"] == "+7 (495) 111-22-33; +7 800 555-35-35"
    assert rows[1]["inn"] == "032616721612"
    # другой порядок колонок тоже работает
    reordered = [HEADER[1], HEADER[0], HEADER[10]]
    assert parse_kontur_xlsx(make_xlsx([["7724419293", "X", "10"]], reordered))[0]["revenue"] == 10


def test_upload_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("ADS_LEADS_DATA_DIR", str(tmp_path))
    from ads_leads import config, db
    from ads_leads.web.app import app

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    try:
        with TestClient(app) as c:
            c.post("/niches", data={"name": "сантехника"})
            c.post("/niches/1/companies", data={"domains": "santeh.ru\ndushevoi.ru\nsanok.ru"})
            with db.get_engine().begin() as conn:
                conn.exec_driver_sql("update companies set inn='7724419293' where domain='santeh.ru'")
                conn.exec_driver_sql("update companies set inn='7716807310' where domain='sanok.ru'")

            inns = c.get("/niches/1/kontur/inns.xlsx")
            assert inns.status_code == 200
            ws = openpyxl.load_workbook(io.BytesIO(inns.content)).active
            assert sorted(r[0] for r in ws.iter_rows(min_row=2, values_only=True)) == ["7716807310", "7724419293"]

            bad = c.post("/niches/1/kontur/upload", files={"file": ("x.xlsx", b"not excel")}).text
            assert "Не удалось открыть Excel" in bad

            page = c.post("/niches/1/kontur/upload", files={"file": ("k.xlsx", make_xlsx(ROWS))}).text
            assert "Загружено строк: 4. Склеено с компаниями ниши: 3 (из них 1 — по сайту" in page
            assert "1 строк не нашли пары" in page
            step4 = page.split('id="step-4"')[1]
            assert "1 703" in step4 and "Иванов Иван Иванович" in step4  # выручка в млн
            assert "ИНН 032616721612" in step4  # dushevoi.ru получил ИНН из Контура
            assert "введено наблюдение" in step4  # банкротство подсвечено
            assert step4.index("santeh.ru") < step4.index("dushevoi.ru")  # сортировка по выручке
    finally:
        db.get_engine.cache_clear()
        config.get_settings.cache_clear()


def test_dossier(tmp_path, monkeypatch):
    monkeypatch.setenv("ADS_LEADS_DATA_DIR", str(tmp_path))
    from ads_leads import config, db
    from ads_leads.web.app import app

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    try:
        with TestClient(app) as c:
            c.post("/niches", data={"name": "сантехника"})
            c.post("/niches/1/companies", data={"domains": "santeh.ru\nmailonly.ru\nempty.ru"})
            with db.get_engine().begin() as conn:
                conn.exec_driver_sql(
                    "update companies set inn='7724419293', signals='metrika,roistat', emails='a@santeh.ru', "
                    "ad_cost=67392714 where domain='santeh.ru'")
                conn.exec_driver_sql("update companies set emails='info@mailonly.ru' where domain='mailonly.ru'")
            c.post("/niches/1/kontur/upload", files={"file": ("k.xlsx", make_xlsx(ROWS))})
            page = c.get("/niches/1").text.split('id="step-5"')[1]
            assert "A · 1" in page and "без ИНН, но с контактами: 1" in page and "без контактов и ИНН: 1" in page

            resp = c.get("/niches/1/dossier.xlsx")
            wb = openpyxl.load_workbook(io.BytesIO(resp.content))
            assert wb.sheetnames == ["Досье", "Сводка", "Без контактов"]
            ws = wb["Досье"]
            header = [x.value for x in ws[1]]
            assert header[:13] == ["Сайт", "ИНН", "Название компании", "Почты с сайта", "Почты из Контура",
                                   "Телефоны с сайта", "Телефоны из Контура", "Траты на Директ, ₽/мес (оценка Keys.so)",
                                   "Сигналы", "Выручка (оборот), млн ₽", "Чистая прибыль, млн ₽", "Баланс, млн ₽", "Ниша"]
            assert header.count("ИНН") == 1
            rows = {r[0]: r for r in ws.iter_rows(min_row=2, values_only=True)}
            s = rows["santeh.ru"]
            assert s[1] == "7724419293" and s[2] == 'ООО "Сантех"' and s[3] == "a@santeh.ru"
            assert s[4] == "a@santeh.ru\nb@santeh.ru"
            assert s[7] == 20217814 and "Roistat" in s[8] and s[9] == 1703.1 and s[12] == "сантехника" and s[13] == "A"
            assert "mailonly.ru" in rows and rows["mailonly.ru"][3] == "info@mailonly.ru"  # без ИНН, но с почтой
            assert "empty.ru" not in rows
            assert [r[0] for r in wb["Без контактов"].iter_rows(min_row=2, values_only=True)] == ["empty.ru"]
            assert c.get("/companies/dossier.xlsx").status_code == 200
    finally:
        db.get_engine.cache_clear()
        config.get_settings.cache_clear()
