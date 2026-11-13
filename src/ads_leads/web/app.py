"""Веб-интерфейс на FastAPI (серверные шаблоны Jinja2).

Главный экран — ниши. Внутри ниши шаги пайплайна по порядку:
запросы → рекламодатели → сайты и ИНН → Контур.Продажи → досье.
"""

import threading
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ads_leads import __version__, auth, jobs
from ads_leads.config import get_settings
from ads_leads.db import get_session, init_db
from ads_leads.export import MIN_BUDGET, build_dossier, collect_rows, priority, private_mobiles
from ads_leads.kontur import KonturFormatError, build_inn_xlsx, parse_kontur_xlsx
from ads_leads.domains import normalize_domain
from ads_leads.models import Company, Job, Niche, NicheQuery, Source
from ads_leads.regions import DEFAULT_REGIONS, YANDEX_BASES, parse_regions, region_names
from ads_leads.site_crawl import CALLTRACKING, E2E_ANALYTICS, SIGNAL_NAMES, inn_is_valid

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.globals.update(YANDEX_BASES=YANDEX_BASES, region_names=region_names)


def page_url(request: Request, param: str, page: int, per: int, anchor: str, **extra) -> str:
    """Ссылка на страницу таблицы с сохранением остальных параметров (пагинация шагов независимая)."""
    params = {k: v for k, v in request.query_params.items() if k not in (param, "per", *extra)}
    params.update({param: page, "per": per, **extra})
    return "?" + urlencode(params) + "#" + anchor


templates.env.globals["page_url"] = page_url
templates.env.filters["num"] = lambda v: "—" if v is None else f"{int(v):,}".replace(",", " ")
DbSession = Annotated[Session, Depends(get_session)]

# Запуск фоновых задач подменяется в тестах (там они выполняются синхронно).
run_in_background = lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True).start()  # noqa: E731


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="AdBeam Leads", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

PUBLIC_PATHS = {"/login", "/health", "/favicon.ico"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Пускает только вошедших, если в .env заданы пароли. Данные у всех пользователей общие."""
    settings = get_settings()
    request.state.user = None
    if not auth.get_users(settings):
        return await call_next(request)
    request.state.user = auth.read_session(settings, request.cookies.get(auth.COOKIE_NAME))
    path = request.url.path
    if request.state.user or path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


def _render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    context.setdefault("keyso_ready", bool(get_settings().keyso_api_token))
    context.setdefault("user", getattr(request.state, "user", None))
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _get_niche(db: Session, niche_id: int) -> Niche:
    niche = db.get(Niche, niche_id)
    if niche is None:
        raise HTTPException(404, "Ниша не найдена")
    return niche


# --- служебное ---------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse("/static/favicon.ico")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if request.state.user or not auth.get_users(get_settings()):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _render(request, "login.html", next=_safe_next(next), error=None)


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    login: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    settings = get_settings()
    if not auth.check_credentials(settings, login, password):
        return _render(
            request, "login.html", status_code=401, next=_safe_next(next), error="Неверный логин или пароль",
            login=login,
        )
    response = RedirectResponse(_safe_next(next), status_code=303)
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(
        auth.COOKIE_NAME, auth.make_session(settings, login.strip()), max_age=auth.SESSION_TTL,
        httponly=True, samesite="lax", secure=https,
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


# --- ниши --------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: DbSession):
    niches = db.scalars(
        select(Niche).options(selectinload(Niche.companies), selectinload(Niche.queries),
                              selectinload(Niche.advertisers)).order_by(Niche.name)
    ).all()
    return _render(request, "home.html", niches=niches, stats={n.id: niche_stats(n) for n in niches},
                   min_budget=MIN_BUDGET)


def niche_stats(niche: Niche) -> dict:
    """Для сравнения ниш: хватает ли в нише компаний под гипотезу «50 на нишу, Директ от 300 тыс.»."""
    main, rest, rejected = collect_rows([niche])
    kept = main + rest
    prios = [priority(r.company)[0] for r in main]
    return {
        "companies": len(kept) + len(rejected),
        "with_inn": sum(1 for r in kept if r.company.inn),
        "budget_ok": sum(1 for r in kept if (r.company.ad_budget_month or 0) >= MIN_BUDGET),
        "A": prios.count("A"), "B": prios.count("B"),
        "picked": sum(1 for r in kept if r.company.pick == "yes"),
    }


@app.post("/niches")
def create_niche(
    db: DbSession,
    name: Annotated[str, Form()],
    regions: Annotated[list[str] | None, Form()] = None,
):
    name = name.strip()
    niche = db.scalar(select(Niche).where(Niche.name == name))
    if niche is None:
        codes = parse_regions(regions) or [DEFAULT_REGIONS]
        niche = Niche(name=name, regions=",".join(codes))
        db.add(niche)
        db.commit()
    return RedirectResponse(f"/niches/{niche.id}", status_code=303)


@app.post("/niches/{niche_id}/regions")
def update_regions(db: DbSession, niche_id: int, regions: Annotated[list[str] | None, Form()] = None):
    niche = _get_niche(db, niche_id)
    niche.regions = ",".join(parse_regions(regions) or [DEFAULT_REGIONS])
    db.commit()
    return RedirectResponse(f"/niches/{niche_id}", status_code=303)


def _advertiser_rows(niche: Niche) -> tuple[list[dict], list[dict]]:
    """Сводка по рекламодателям ниши: одна строка на компанию (по всем регионам)."""
    rows: dict[int, dict] = {}
    excluded: dict[int, dict] = {}
    for adv in niche.advertisers:
        target = excluded if adv.excluded else rows
        row = target.setdefault(
            adv.company_id,
            {"company": adv.company, "share": 0.0, "keys_cnt": 0, "spec_cnt": 0, "bases": [], "reason": adv.excluded},
        )
        row["share"] = max(row["share"], adv.share)
        row["keys_cnt"] = max(row["keys_cnt"], adv.keys_cnt)
        row["spec_cnt"] = max(row["spec_cnt"], adv.spec_cnt)
        row["bases"].append(adv.base)
    in_keyso = set(rows)
    for company in niche.companies:  # добавленные вручную
        if company.id not in in_keyso:
            rows[company.id] = {"company": company, "share": None, "keys_cnt": None, "spec_cnt": None, "bases": [],
                                "reason": None}
    ordered = sorted(
        rows.values(), key=lambda r: (-(r["company"].ad_cost or -1), -(r["share"] or 0), r["company"].domain)
    )
    return ordered, sorted(excluded.values(), key=lambda r: -r["share"])


PER_PAGE_OPTIONS = (20, 50, 100)


def _paginate(rows: list, page: int, per: int) -> dict:
    per = per if per in PER_PAGE_OPTIONS else PER_PAGE_OPTIONS[0]
    pages = max(1, -(-len(rows) // per))
    page = min(max(page, 1), pages)
    start = (page - 1) * per
    return {"rows": rows[start : start + per], "page": page, "pages": pages, "per": per, "total": len(rows),
            "offset": start}


def _last_job(db: Session, niche_id: int, kind: str) -> Job | None:
    return jobs.active_job(db, niche_id, kind) or db.scalar(
        select(Job).where(Job.niche_id == niche_id, Job.kind == kind).order_by(Job.id.desc())
    )


SITE_FILTERS = {
    "all": ("Все", lambda c: True),
    "inn": ("С ИНН", lambda c: bool(c.inn)),
    "noinn": ("Без ИНН", lambda c: c.site_checked_at is not None and not c.inn),
    "e2e": ("Сквозная аналитика", lambda c: bool(set(c.signal_list) & E2E_ANALYTICS)),
    "ct": ("Коллтрекинг", lambda c: bool(set(c.signal_list) & CALLTRACKING)),
    "blocked": ("Защита от ботов", lambda c: c.site_status == "blocked"),
    "down": ("Не открылись", lambda c: c.site_status == "down"),
}


# Отбор в шорт-лист (шаг 5): кандидаты A/B, по которым ещё не решили, — первыми
PICK_FILTERS = {
    "todo": ("A и B, не решено", lambda r: r.company.pick is None and priority(r.company)[0] in "AB"),
    "yes": ("Берём", lambda r: r.company.pick == "yes"),
    "no": ("Не берём", lambda r: r.company.pick == "no"),
    "all": ("Все", lambda r: True),
}


@app.get("/niches/{niche_id}", response_class=HTMLResponse)
def niche_page(request: Request, db: DbSession, niche_id: int, per: int = 20, p2: int = 1, p3: int = 1,
               f3: str = "all", p4: int = 1, p5: int = 1, f5: str = "todo"):
    niche = _get_niche(db, niche_id)
    advertisers, excluded = _advertiser_rows(niche)
    checked = [r for r in advertisers if r["company"].site_checked_at]
    with_inn = sum(1 for r in advertisers if r["company"].inn)
    dossier_main, dossier_rest, dossier_rejected = collect_rows([niche])
    dossier_prio = [priority(r.company)[0] for r in dossier_main]
    candidates = dossier_main + dossier_rest + dossier_rejected
    f5 = f5 if f5 in PICK_FILTERS else "todo"
    steps = [
        {"n": 1, "title": "Запросы", "count": len(niche.queries), "ready": True},
        {"n": 2, "title": "Рекламодатели", "count": len(advertisers), "ready": True},
        {"n": 3, "title": "Сайты: ИНН и сигналы", "count": with_inn, "ready": True},
        {"n": 4, "title": "Контур.Продажи", "count": sum(1 for r in advertisers if r["company"].kontur_loaded_at),
         "ready": True},
        {"n": 5, "title": "Досье и отбор", "count": sum(1 for r in candidates if r.company.pick == "yes"),
         "ready": True},
    ]
    return _render(
        request, "niche.html", niche=niche, steps=steps, excluded=excluded,
        adv_page=_paginate(advertisers, p2, per),
        site_page=_paginate([r for r in advertisers if SITE_FILTERS.get(f3, SITE_FILTERS["all"])[1](r["company"])],
                            p3, per),
        f3=f3 if f3 in SITE_FILTERS else "all",
        site_filters=[(k, label, sum(1 for r in advertisers if fn(r["company"]))) for k, (label, fn) in SITE_FILTERS.items()],
        advertisers_total=len(advertisers), checked_total=len(checked), with_inn=with_inn,
        job=_last_job(db, niche_id, jobs.KIND_COLLECT), crawl_job=_last_job(db, niche_id, jobs.KIND_CRAWL),
        kontur_job=_last_job(db, niche_id, jobs.KIND_KONTUR),
        kontur_page=_paginate(sorted([r for r in advertisers if r["company"].kontur_loaded_at],
                                     key=lambda r: -(r["company"].revenue or 0)), p4, per),
        inns_for_kontur=_inns_for_kontur(advertisers),
        dossier={"total": len(dossier_main), "rest": len(dossier_rest),
                 "A": dossier_prio.count("A"), "B": dossier_prio.count("B"), "C": dossier_prio.count("C"),
                 "no_inn": sum(1 for r in dossier_main if not r.company.inn),
                 "picked": sum(1 for r in candidates if r.company.pick == "yes")},
        pick_page=_paginate([r for r in candidates if PICK_FILTERS[f5][1](r)], p5, per), f5=f5,
        pick_filters=[(k, label, sum(1 for r in candidates if fn(r))) for k, (label, fn) in PICK_FILTERS.items()],
        priority=priority, private_mobiles=private_mobiles, min_budget=MIN_BUDGET,
        per_options=PER_PAGE_OPTIONS, signal_names=SIGNAL_NAMES, e2e_signals=E2E_ANALYTICS,
        calltracking_signals=CALLTRACKING,
    )


@app.post("/niches/{niche_id}/queries")
def add_queries(db: DbSession, niche_id: int, queries: Annotated[str, Form()]):
    niche = _get_niche(db, niche_id)
    existing = {q.text for q in niche.queries}
    for line in queries.splitlines():
        text = " ".join(line.split()).lower()
        if text and text not in existing:
            niche.queries.append(NicheQuery(text=text))
            existing.add(text)
    db.commit()
    return RedirectResponse(f"/niches/{niche_id}#step-1", status_code=303)


@app.post("/niches/{niche_id}/queries/clear")
def clear_queries(db: DbSession, niche_id: int):
    niche = _get_niche(db, niche_id)
    niche.queries.clear()
    db.commit()
    return RedirectResponse(f"/niches/{niche_id}#step-1", status_code=303)


@app.post("/niches/{niche_id}/collect")
def start_collect(request: Request, db: DbSession, niche_id: int):
    """Шаг 2: запустить сбор рекламодателей в Keys.so (в фоне)."""
    niche = _get_niche(db, niche_id)
    if not get_settings().keyso_api_token:
        raise HTTPException(400, "Keys.so не подключён: нет токена в .env")
    if not niche.queries:
        raise HTTPException(400, "Сначала добавьте запросы на шаге 1")
    if jobs.active_job(db, niche_id, jobs.KIND_COLLECT) is None:
        job = Job(niche_id=niche_id, kind=jobs.KIND_COLLECT, started_by=request.state.user,
                  message="В очереди…")
        db.add(job)
        db.commit()
        run_in_background(jobs.collect_advertisers, job.id, None)
    return RedirectResponse(f"/niches/{niche_id}#step-2", status_code=303)


@app.post("/niches/{niche_id}/crawl")
def start_crawl(request: Request, db: DbSession, niche_id: int, recheck: Annotated[str | None, Form()] = None):
    """Шаг 3: обойти сайты рекламодателей ниши (в фоне)."""
    niche = _get_niche(db, niche_id)
    if not niche.companies:
        raise HTTPException(400, "Сначала соберите рекламодателей на шаге 2")
    if jobs.active_job(db, niche_id, jobs.KIND_CRAWL) is None:
        job = Job(niche_id=niche_id, kind=jobs.KIND_CRAWL, started_by=request.state.user, message="В очереди…")
        db.add(job)
        db.commit()
        run_in_background(jobs.crawl_sites, job.id, None, not recheck)
    return RedirectResponse(f"/niches/{niche_id}#step-3", status_code=303)


def _inns_for_kontur(advertisers: list[dict]) -> list[tuple[str, str, str | None]]:
    seen, rows = set(), []
    for r in advertisers:
        c = r["company"]
        if c.inn and c.inn not in seen:
            seen.add(c.inn)
            rows.append((c.inn, c.domain, c.legal_name))
    return rows


@app.get("/niches/{niche_id}/kontur/inns.xlsx")
def kontur_inns_file(db: DbSession, niche_id: int):
    """Шаг 4: файл ИНН ниши для загрузки в Контур.Продажи."""
    niche = _get_niche(db, niche_id)
    advertisers, _ = _advertiser_rows(niche)
    content = build_inn_xlsx(_inns_for_kontur(advertisers))
    filename = quote(f"ИНН для Контура — {niche.name}.xlsx")
    return Response(
        content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@app.post("/niches/{niche_id}/kontur/upload")
async def kontur_upload(request: Request, db: DbSession, niche_id: int, file: Annotated[UploadFile, File()]):
    """Шаг 4: загрузить выгрузку из Контура и склеить с компаниями по ИНН (и по сайту, если ИНН не было)."""
    niche = _get_niche(db, niche_id)
    job = Job(niche_id=niche_id, kind=jobs.KIND_KONTUR, started_by=request.state.user, status="running")
    db.add(job)
    try:
        rows = parse_kontur_xlsx(await file.read())
    except KonturFormatError as exc:
        job.status, job.message, job.finished_at = "error", str(exc), datetime.now()
        db.commit()
        return RedirectResponse(f"/niches/{niche_id}#step-4", status_code=303)

    by_inn: dict[str, list[Company]] = {}
    by_domain: dict[str, Company] = {}
    for c in db.scalars(select(Company)):
        if c.inn:
            by_inn.setdefault(c.inn, []).append(c)
        by_domain[c.domain] = c
    niche_ids = {c.id for c in niche.companies}
    matched, matched_niche, by_site, unknown = set(), set(), 0, 0
    now = datetime.now()
    for row in rows:
        targets = by_inn.get(row["inn"], [])
        if not targets and row.get("kontur_site"):  # ИНН на сайте не нашли, но Контур знает сайт компании
            company = by_domain.get(normalize_domain(row["kontur_site"]) or "")
            if company and not company.inn:
                company.inn = row["inn"]
                targets, by_site = [company], by_site + 1
        if not targets:
            unknown += 1
            continue
        for company in targets:
            for field, value in row.items():
                if field != "inn":
                    setattr(company, field, value)
            company.kontur_loaded_at = now
            matched.add(company.id)
            if company.id in niche_ids:
                matched_niche.add(company.id)
    msg = f"Загружено строк: {len(rows)}. Склеено с компаниями ниши: {len(matched_niche)}"
    if by_site:
        msg += f" (из них {by_site} — по сайту, ИНН взят из Контура)"
    if len(matched) > len(matched_niche):
        msg += f"; ещё {len(matched) - len(matched_niche)} — из других ниш"
    if unknown:
        msg += f"; {unknown} строк не нашли пары (ИНН нет в базе)"
    job.status, job.progress, job.message, job.finished_at = "done", 100, msg, now
    db.commit()
    return RedirectResponse(f"/niches/{niche_id}#step-4", status_code=303)


def _xlsx_response(content: bytes, filename: str) -> Response:
    return Response(
        content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/niches/{niche_id}/dossier.xlsx")
def niche_dossier(db: DbSession, niche_id: int):
    """Шаг 5: итоговое досье по нише."""
    niche = _get_niche(db, niche_id)
    stamp = datetime.now().strftime("%Y-%m-%d")
    return _xlsx_response(build_dossier([niche], f"Ниша «{niche.name}»"), f"Досье — {niche.name} — {stamp}.xlsx")


@app.get("/niches/{niche_id}/shortlist.xlsx")
def niche_shortlist(db: DbSession, niche_id: int):
    """Шаг 5: только отобранные «берём» — список для поиска ЛПР."""
    niche = _get_niche(db, niche_id)
    stamp = datetime.now().strftime("%Y-%m-%d")
    return _xlsx_response(build_dossier([niche], f"Ниша «{niche.name}»", only_picked=True),
                          f"Шорт-лист — {niche.name} — {stamp}.xlsx")


@app.get("/companies/shortlist.xlsx")
def all_shortlist(db: DbSession):
    niches = list(db.scalars(select(Niche).order_by(Niche.name)))
    stamp = datetime.now().strftime("%Y-%m-%d")
    return _xlsx_response(build_dossier(niches, "Все ниши", only_picked=True), f"Шорт-лист — все ниши — {stamp}.xlsx")


@app.post("/companies/{company_id}/pick")
def set_pick(request: Request, db: DbSession, company_id: int, value: Annotated[str, Form()],
             next: Annotated[str, Form()] = "/"):
    """Отбор в шорт-лист: yes — берём, no — не берём (уходит на лист «Не берём»), иное — сбросить."""
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404)
    company.pick = value if value in ("yes", "no") else None
    company.pick_by, company.pick_at = request.state.user, datetime.now()
    db.commit()
    return RedirectResponse(_safe_next(next) + "#step-5", status_code=303)


@app.get("/companies/dossier.xlsx")
def all_dossier(db: DbSession):
    """Досье по всем нишам сразу (компания в нескольких нишах — одна строка, ниши через запятую)."""
    niches = list(db.scalars(select(Niche).order_by(Niche.name)))
    stamp = datetime.now().strftime("%Y-%m-%d")
    return _xlsx_response(build_dossier(niches, "Все ниши"), f"Досье — все ниши — {stamp}.xlsx")


@app.post("/companies/{company_id}/inn", response_class=HTMLResponse)
def set_inn(
    request: Request,
    db: DbSession,
    company_id: int,
    inn: Annotated[str, Form()],
    legal_name: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
):
    """Ручной ввод ИНН (для сайтов, где обход не нашёл реквизиты). Повторный обход его не затирает."""
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404)
    inn = "".join(ch for ch in inn if ch.isdigit())
    if not inn_is_valid(inn):
        return HTMLResponse(f"Неверный ИНН «{inn}»: не сходится контрольная сумма. "
                            f"<a href='{_safe_next(next)}'>Назад</a>", status_code=400)
    company.inn = inn
    company.inn_manual = True
    if legal_name.strip():
        company.legal_name = legal_name.strip()
    db.commit()
    return RedirectResponse(_safe_next(next) + "#step-3", status_code=303)


@app.get("/jobs/{job_id}")
def job_status(db: DbSession, job_id: int) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404)
    return {"id": job.id, "status": job.status, "progress": job.progress, "message": job.message}


@app.post("/niches/{niche_id}/companies")
def add_companies(db: DbSession, niche_id: int, domains: Annotated[str, Form()]):
    """Ручное добавление доменов (по одному на строку)."""
    niche = _get_niche(db, niche_id)
    for line in domains.splitlines():
        domain = normalize_domain(line)
        if not domain:
            continue
        company = db.scalar(select(Company).where(Company.domain == domain))
        if company is None:
            company = Company(domain=domain, source=Source.MANUAL)
            db.add(company)
        if niche not in company.niches:
            company.niches.append(niche)
    db.commit()
    return RedirectResponse(f"/niches/{niche_id}#step-2", status_code=303)


@app.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, db: DbSession):
    companies = db.scalars(
        select(Company).options(selectinload(Company.niches)).order_by(Company.ad_cost.desc().nulls_last(),
                                                                       Company.domain)
    ).all()
    return _render(request, "companies.html", companies=companies)
