"""Веб-интерфейс на FastAPI (серверные шаблоны Jinja2).

Главный экран — ниши. Внутри ниши шаги пайплайна по порядку:
запросы → рекламодатели → сайты и ИНН → Контур.Продажи → досье.
"""

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ads_leads import __version__, auth, jobs
from ads_leads.config import get_settings
from ads_leads.db import get_session, init_db
from ads_leads.domains import normalize_domain
from ads_leads.models import Company, Job, Niche, NicheQuery, Source
from ads_leads.regions import DEFAULT_REGIONS, YANDEX_BASES, parse_regions, region_names

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.globals.update(YANDEX_BASES=YANDEX_BASES, region_names=region_names)
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
        select(Niche).options(selectinload(Niche.companies), selectinload(Niche.queries)).order_by(Niche.name)
    ).all()
    return _render(request, "home.html", niches=niches)


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


@app.get("/niches/{niche_id}", response_class=HTMLResponse)
def niche_page(request: Request, db: DbSession, niche_id: int):
    niche = _get_niche(db, niche_id)
    advertisers, excluded = _advertiser_rows(niche)
    with_inn = sum(1 for r in advertisers if r["company"].inn)
    steps = [
        {"n": 1, "title": "Запросы", "count": len(niche.queries), "ready": True},
        {"n": 2, "title": "Рекламодатели", "count": len(advertisers), "ready": True},
        {"n": 3, "title": "Сайты: ИНН и сигналы", "count": with_inn, "ready": False},
        {"n": 4, "title": "Контур.Продажи", "count": None, "ready": False},
        {"n": 5, "title": "Досье", "count": None, "ready": False},
    ]
    job = jobs.active_job(db, niche_id) or db.scalar(
        select(Job).where(Job.niche_id == niche_id, Job.kind == jobs.KIND_COLLECT).order_by(Job.id.desc())
    )
    return _render(
        request, "niche.html", niche=niche, steps=steps, advertisers=advertisers, excluded=excluded, job=job
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
    if jobs.active_job(db, niche_id) is None:
        job = Job(niche_id=niche_id, kind=jobs.KIND_COLLECT, started_by=request.state.user,
                  message="В очереди…")
        db.add(job)
        db.commit()
        run_in_background(jobs.collect_advertisers, job.id)
    return RedirectResponse(f"/niches/{niche_id}#step-2", status_code=303)


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
