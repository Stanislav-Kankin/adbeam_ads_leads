"""Фоновые задачи. Запускаются в отдельном потоке, прогресс пишут в таблицу jobs."""

import logging
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ads_leads.config import get_settings
from ads_leads.db import get_engine
from ads_leads.domains import normalize_domain
from ads_leads.filters import exclusion_reason
from ads_leads.keyso import KeysoClient, KeysoError
from ads_leads.models import Company, Job, Niche, NicheAdvertiser, Source
from ads_leads.regions import YANDEX_BASES
from ads_leads.site_crawl import BrowserCrawler, SiteResult, browser_available, crawl_site

log = logging.getLogger(__name__)

KIND_COLLECT = "collect_advertisers"
KIND_CRAWL = "crawl_sites"
CRAWL_WORKERS = 8

ClientFactory = Callable[[], KeysoClient]


def _session() -> Session:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)()


def _update(db: Session, job: Job, *, progress: int | None = None, message: str | None = None,
            status: str | None = None) -> None:
    if progress is not None:
        job.progress = max(0, min(100, progress))
    if message is not None:
        job.message = message
    if status is not None:
        job.status = status
        if status in ("done", "error"):
            job.finished_at = datetime.now()
    db.commit()


def active_job(db: Session, niche_id: int, kind: str = KIND_COLLECT) -> Job | None:
    return db.scalar(
        select(Job).where(Job.niche_id == niche_id, Job.kind == kind, Job.status.in_(("queued", "running")))
    )


def collect_advertisers(job_id: int, client_factory: ClientFactory | None = None) -> None:
    """Запросы ниши → Keys.so «Доля конкурентов по фразам» (контекст) → компании + пакетный анализ доменов."""
    factory = client_factory or (lambda: KeysoClient(get_settings().keyso_api_token))
    with _session() as db:
        job = db.get(Job, job_id)
        niche = db.get(Niche, job.niche_id)
        try:
            _update(db, job, status="running", progress=1, message="Запускаю сбор в Keys.so…")
            queries = [q.text for q in niche.queries]
            if not queries:
                raise KeysoError("В нише нет запросов — добавьте их на шаге 1")
            bases = niche.region_codes
            with factory() as client:
                found, excluded = _collect(db, job, niche, queries, bases, client)
                _update(db, job, progress=85, message="Оцениваю рекламные бюджеты доменов…")
                _enrich_budgets(db, niche, bases[0], client)
            msg = f"Готово: {found} рекламодателей"
            if excluded:
                msg += f", ещё {excluded} отброшено (маркетплейсы, агрегаторы, федеральные сети)"
            _update(db, job, status="done", progress=100, message=msg)
        except Exception as exc:  # noqa: BLE001 — показываем любую ошибку в интерфейсе
            log.exception("Задача %s упала", job_id)
            db.rollback()
            _update(db, job, status="error", message=str(exc) or exc.__class__.__name__)


def _collect(db: Session, job: Job, niche: Niche, queries: list[str], bases: list[str],
             client: KeysoClient) -> tuple[int, int]:
    found: set[int] = set()
    excluded: set[str] = set()
    span = 80 / len(bases)
    for i, base in enumerate(bases):
        start = 2 + i * span
        region = YANDEX_BASES.get(base, base)
        _update(db, job, progress=int(start), message=f"{region}: Keys.so обрабатывает {len(queries)} запросов…")
        rows = client.advertisers_by_keywords(
            queries, base, on_progress=lambda p, s=start: _update(db, job, progress=int(s + span * 0.8 * p / 100))
        )
        _update(db, job, progress=int(start + span * 0.85), message=f"{region}: сохраняю {len(rows)} доменов…")

        old = {
            (a.company_id): a
            for a in db.scalars(
                select(NicheAdvertiser).where(NicheAdvertiser.niche_id == niche.id, NicheAdvertiser.base == base)
            )
        }
        seen: set[int] = set()
        for row in rows:
            domain = normalize_domain(row.get("name", ""))
            if not domain:
                continue
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(domain=domain, source=Source.KEYSO)
                db.add(company)
                db.flush()
            if company.id in seen:  # поддомены одного сайта: оставляем лучший показатель
                continue
            seen.add(company.id)
            reason = exclusion_reason(domain)
            adv = old.get(company.id) or NicheAdvertiser(niche_id=niche.id, company_id=company.id, base=base)
            adv.keyso_domain = row.get("name") or domain
            adv.share = round(float(row.get("perc") or 0), 2)
            adv.keys_cnt = int(row.get("cnt") or 0)
            adv.spec_cnt = int(row.get("spec") or 0)
            adv.other_cnt = int(row.get("gar") or 0)
            adv.excluded = reason
            db.add(adv)
            if reason:
                excluded.add(domain)
            else:
                found.add(company.id)
                if niche not in company.niches:
                    company.niches.append(niche)
        for company_id, adv in old.items():  # домены, которых больше нет в выдаче по этой базе
            if company_id not in seen:
                db.delete(adv)
        db.commit()
    return len(found), len(excluded)


def crawl_sites(job_id: int, crawler: Callable[[str], SiteResult] | None = None, only_new: bool = True,
                use_browser: bool | None = None) -> None:
    """Шаг 3: обойти сайты рекламодателей ниши (параллельно), сохранить ИНН, контакты, сигналы."""
    crawl = crawler or crawl_site
    if use_browser is None:
        use_browser = crawler is None and get_settings().browser_fallback
    with _session() as db:
        job = db.get(Job, job_id)
        niche = db.get(Niche, job.niche_id)
        try:
            excluded = {a.company_id for a in niche.advertisers if a.excluded}
            companies = [c for c in niche.companies if c.id not in excluded]
            if only_new:
                # новые + те, что в прошлый раз упёрлись в защиту или не открылись
                companies = [c for c in companies if c.site_checked_at is None or c.site_status in ("blocked", "down")]
            if not companies:
                _update(db, job, status="done", progress=100, message="Все сайты уже проверены")
                return
            total = len(companies)
            _update(db, job, status="running", progress=1, message=f"Проверяю {total} сайтов…")
            by_domain = {c.domain: c for c in companies}
            stats = Counter()
            with ThreadPoolExecutor(CRAWL_WORKERS) as pool:
                futures = {pool.submit(crawl, d): d for d in by_domain}
                for done, fut in enumerate(as_completed(futures), 1):
                    domain = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        res = SiteResult(domain=domain, status="down", error=str(exc)[:200])
                    _apply_site(by_domain[domain], res)
                    stats[res.status] += 1
                    stats["inn"] += bool(res.inn)
                    db.commit()
                    _update(db, job, progress=int(done / total * 100), message=f"Проверено {done} из {total}: {domain}")
            blocked = [c for c in by_domain.values() if c.site_status == "blocked"]
            if blocked and use_browser and browser_available():
                stats = _browser_pass(db, job, blocked, stats)
            msg = f"Готово: {total} сайтов, ИНН найден у {stats['inn']}"
            if stats["browser_ok"]:
                msg += f" (из них {stats['browser_ok']} через браузер в обход защиты)"
            if stats["blocked"]:
                msg += f", {stats['blocked']} с защитой от ботов"
            if stats["down"]:
                msg += f", {stats['down']} не открылись"
            _update(db, job, status="done", progress=100, message=msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("Задача %s упала", job_id)
            db.rollback()
            _update(db, job, status="error", message=str(exc) or exc.__class__.__name__)


def _browser_pass(db: Session, job: Job, companies: list[Company], stats: Counter) -> Counter:
    """Сайты с JS-защитой — повторно через настоящий браузер (по одному, ~15–25 с на сайт)."""
    n = len(companies)
    try:
        with BrowserCrawler() as browser:
            for i, company in enumerate(companies, 1):
                _update(db, job, progress=int(i / n * 100),
                        message=f"Обхожу защиту через браузер: {i} из {n} — {company.domain}")
                try:
                    res = browser.crawl(company.domain)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Браузер %s: %s", company.domain, exc)
                    continue
                if res.status == "ok":
                    _apply_site(company, res)
                    stats["blocked"] -= 1
                    stats["ok"] += 1
                    stats["inn"] += bool(res.inn)
                    stats["browser_ok"] += 1
                    db.commit()
    except Exception as exc:  # noqa: BLE001 — нет браузера/дисплея: оставляем результат обычного обхода
        log.warning("Браузер недоступен: %s", exc)
    return stats


def _apply_site(company: Company, res: SiteResult) -> None:
    company.site_status = res.status
    company.site_error = res.error
    company.site_url = res.final_url
    company.site_title = res.title
    company.site_checked_at = datetime.now()
    if res.status != "ok":
        return
    # сайт открылся — его данные актуальнее прежних (ИНН оставляем, если в этот раз не нашёлся)
    company.inn = res.inn or company.inn
    company.inn_candidates = ",".join(res.inn_candidates) or None
    company.legal_name = res.legal_name
    company.emails = "; ".join(res.emails) or None
    company.phones = "; ".join(res.phones) or None
    company.socials = "; ".join(res.socials) or None
    company.signals = ",".join(res.signals) or None
    company.metrika_ids = ",".join(res.metrika_ids) or None


def _enrich_budgets(db: Session, niche: Niche, base: str, client: KeysoClient) -> None:
    advs = db.scalars(
        select(NicheAdvertiser).where(
            NicheAdvertiser.niche_id == niche.id, NicheAdvertiser.base == base, NicheAdvertiser.excluded.is_(None)
        )
    ).all()
    by_name = {a.keyso_domain: a.company for a in advs}
    if not by_name:
        return
    now = datetime.now()
    for row in client.domains_batch(list(by_name), base):
        company = by_name.get(row.get("name"))
        if company is None:
            continue
        company.keyso_base = base
        company.ad_cost = row.get("adcost")
        company.ad_traffic = row.get("adtraf")
        company.ad_keys = row.get("adkeyscnt")
        company.ad_ads = row.get("adscnt")
        company.seo_traffic = row.get("traf")
        company.keyso_updated_at = now
    db.commit()
