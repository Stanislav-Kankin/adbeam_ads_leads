"""Веб-интерфейс на FastAPI (серверные шаблоны Jinja2).

Главный экран — ниши. Внутри ниши шаги пайплайна по порядку:
запросы → рекламодатели → сайты и ИНН → Контур.Продажи → досье.
"""

import base64
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ads_leads import __version__
from ads_leads.config import get_settings
from ads_leads.db import get_session, init_db
from ads_leads.domains import normalize_domain
from ads_leads.models import Company, Niche, NicheQuery, Source

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
DbSession = Annotated[Session, Depends(get_session)]


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="AdBeam Leads", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Вход по логину и паролю, если в настройках задан ADS_LEADS_AUTH_PASSWORD."""
    settings = get_settings()
    if not settings.auth_password or request.url.path == "/health":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            user, password = "", ""
        if secrets.compare_digest(user.encode(), settings.auth_user.encode()) and secrets.compare_digest(
            password.encode(), settings.auth_password.encode()
        ):
            return await call_next(request)
    return Response(
        "Нужен логин и пароль", status_code=401, headers={"WWW-Authenticate": 'Basic realm="AdBeam Leads"'}
    )


def _render(request: Request, name: str, **context) -> HTMLResponse:
    context.setdefault("keyso_ready", bool(get_settings().keyso_api_token))
    return templates.TemplateResponse(request, name, context)


def _get_niche(db: Session, niche_id: int) -> Niche:
    niche = db.get(Niche, niche_id)
    if niche is None:
        raise HTTPException(404, "Ниша не найдена")
    return niche


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: DbSession):
    niches = db.scalars(
        select(Niche).options(selectinload(Niche.companies), selectinload(Niche.queries)).order_by(Niche.name)
    ).all()
    return _render(request, "home.html", niches=niches)


@app.post("/niches")
def create_niche(db: DbSession, name: Annotated[str, Form()], region_lr: Annotated[int, Form()] = 213):
    name = name.strip()
    niche = db.scalar(select(Niche).where(Niche.name == name))
    if niche is None:
        niche = Niche(name=name, region_lr=region_lr)
        db.add(niche)
        db.commit()
    return RedirectResponse(f"/niches/{niche.id}", status_code=303)


@app.get("/niches/{niche_id}", response_class=HTMLResponse)
def niche_page(request: Request, db: DbSession, niche_id: int):
    niche = _get_niche(db, niche_id)
    with_inn = sum(1 for c in niche.companies if c.inn)
    steps = [
        {"n": 1, "title": "Запросы", "hint": "Коммерческие запросы ниши, по ним ищем рекламодателей",
         "count": len(niche.queries), "ready": True},
        {"n": 2, "title": "Рекламодатели", "hint": "Домены из рекламной выдачи (Keys.so) или вручную",
         "count": len(niche.companies), "ready": True},
        {"n": 3, "title": "Сайты: ИНН и сигналы", "hint": "Реквизиты, контакты, Метрика, Roistat/Calltouch",
         "count": with_inn, "ready": False},
        {"n": 4, "title": "Контур.Продажи", "hint": "Файл ИНН → выгрузка с выручкой и руководителем",
         "count": None, "ready": False},
        {"n": 5, "title": "Досье", "hint": "Excel-таблица и сводка по нише",
         "count": None, "ready": False},
    ]
    companies = sorted(niche.companies, key=lambda c: c.domain)
    return _render(request, "niche.html", niche=niche, steps=steps, companies=companies)


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


@app.post("/niches/{niche_id}/companies")
def add_companies(db: DbSession, niche_id: int, domains: Annotated[str, Form()]):
    """Ручное добавление доменов (по одному на строку), пока нет Keys.so."""
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
        select(Company).options(selectinload(Company.niches)).order_by(Company.domain)
    ).all()
    return _render(request, "companies.html", companies=companies)
