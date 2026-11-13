"""Веб-интерфейс на FastAPI (серверные шаблоны Jinja2)."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ads_leads import __version__
from ads_leads.db import get_session, init_db
from ads_leads.domains import normalize_domain
from ads_leads.models import Company, Niche, Source

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
DbSession = Annotated[Session, Depends(get_session)]


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="AdBeam Leads", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/companies")


@app.get("/niches", response_class=HTMLResponse)
def niches_page(request: Request, db: DbSession):
    niches = db.scalars(select(Niche).order_by(Niche.name)).all()
    return templates.TemplateResponse(request, "niches.html", {"niches": niches})


@app.post("/niches")
def create_niche(db: DbSession, name: Annotated[str, Form()], region_lr: Annotated[int, Form()] = 213):
    name = name.strip()
    if name and not db.scalar(select(Niche).where(Niche.name == name)):
        db.add(Niche(name=name, region_lr=region_lr))
        db.commit()
    return RedirectResponse("/niches", status_code=303)


@app.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, db: DbSession, niche_id: int | None = None):
    query = select(Company).options(selectinload(Company.niches)).order_by(Company.domain)
    if niche_id:
        query = query.where(Company.niches.any(Niche.id == niche_id))
    companies = db.scalars(query).all()
    niches = db.scalars(select(Niche).order_by(Niche.name)).all()
    return templates.TemplateResponse(
        request,
        "companies.html",
        {"companies": companies, "niches": niches, "niche_id": niche_id},
    )


@app.post("/companies")
def add_companies(
    db: DbSession,
    domains: Annotated[str, Form()],
    niche_id: Annotated[int | None, Form()] = None,
):
    """Ручное добавление списка доменов (по одному на строку)."""
    niche = db.get(Niche, niche_id) if niche_id else None
    for line in domains.splitlines():
        domain = normalize_domain(line)
        if not domain:
            continue
        company = db.scalar(select(Company).where(Company.domain == domain))
        if company is None:
            company = Company(domain=domain, source=Source.MANUAL)
            db.add(company)
        if niche and niche not in company.niches:
            company.niches.append(niche)
    db.commit()
    url = f"/companies?niche_id={niche_id}" if niche_id else "/companies"
    return RedirectResponse(url, status_code=303)
