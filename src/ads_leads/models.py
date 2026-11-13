"""Основная сущность — компания-рекламодатель (ключ: домен, позже + ИНН)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Column, DateTime, Float, ForeignKey, String, Table, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ads_leads.db import Base
from ads_leads.regions import DEFAULT_REGIONS, parse_regions


class Source(StrEnum):
    KEYSO = "keyso"  # рекламодатель из Keys.so по запросам ниши
    WB_REGISTRY = "wb"  # реестр ИНН из WB-парсера
    MANUAL = "manual"


company_niches = Table(
    "company_niches",
    Base.metadata,
    Column("company_id", ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
    Column("niche_id", ForeignKey("niches.id", ondelete="CASCADE"), primary_key=True),
)


class Niche(Base):
    __tablename__ = "niches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    region_lr: Mapped[int] = mapped_column(default=213)  # устарело, оставлено для старых БД
    regions: Mapped[str] = mapped_column(String(200), default=DEFAULT_REGIONS, server_default=DEFAULT_REGIONS)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    companies: Mapped[list["Company"]] = relationship(secondary=company_niches, back_populates="niches")
    queries: Mapped[list["NicheQuery"]] = relationship(
        back_populates="niche", cascade="all, delete-orphan", order_by="NicheQuery.id"
    )
    advertisers: Mapped[list["NicheAdvertiser"]] = relationship(
        back_populates="niche", cascade="all, delete-orphan"
    )

    @property
    def region_codes(self) -> list[str]:
        return parse_regions(self.regions) or [DEFAULT_REGIONS]


class NicheQuery(Base):
    """Коммерческий запрос ниши («купить кран hansgrohe»), по нему ищем рекламодателей."""

    __tablename__ = "niche_queries"
    __table_args__ = (UniqueConstraint("niche_id", "text"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    niche_id: Mapped[int] = mapped_column(ForeignKey("niches.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    niche: Mapped[Niche] = relationship(back_populates="queries")


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    inn: Mapped[str | None] = mapped_column(String(12), index=True)
    legal_name: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(20), default=Source.MANUAL)
    status: Mapped[str] = mapped_column(String(20), default="new")
    notes: Mapped[str | None] = mapped_column(Text)

    # Keys.so, пакетный анализ домена (по первой базе ниши)
    keyso_base: Mapped[str | None] = mapped_column(String(10))
    ad_cost: Mapped[int | None] = mapped_column()  # adcost: оценка расхода в день, копейки
    ad_traffic: Mapped[int | None] = mapped_column()  # adtraf: кликов из контекста в день
    ad_keys: Mapped[int | None] = mapped_column()  # adkeyscnt: запросов в контексте
    ad_ads: Mapped[int | None] = mapped_column()  # adscnt: объявлений
    seo_traffic: Mapped[int | None] = mapped_column()  # traf: оценка трафика из поиска
    keyso_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Обход сайта (шаг 3)
    site_status: Mapped[str | None] = mapped_column(String(20))  # ok | blocked | down
    site_error: Mapped[str | None] = mapped_column(String(200))
    site_url: Mapped[str | None] = mapped_column(String(500))
    site_title: Mapped[str | None] = mapped_column(String(300))
    inn_candidates: Mapped[str | None] = mapped_column(String(200))  # все валидные ИНН с сайта через запятую
    emails: Mapped[str | None] = mapped_column(Text)  # через «; »
    phones: Mapped[str | None] = mapped_column(Text)
    socials: Mapped[str | None] = mapped_column(Text)
    signals: Mapped[str | None] = mapped_column(String(300))  # коды из site_crawl.SIGNALS через запятую
    metrika_ids: Mapped[str | None] = mapped_column(String(100))
    site_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    niches: Mapped[list[Niche]] = relationship(secondary=company_niches, back_populates="companies")

    @property
    def signal_list(self) -> list[str]:
        return [s for s in (self.signals or "").split(",") if s]

    @property
    def email_list(self) -> list[str]:
        return [s for s in (self.emails or "").split("; ") if s]

    @property
    def phone_list(self) -> list[str]:
        return [s for s in (self.phones or "").split("; ") if s]

    @property
    def ad_budget_month(self) -> int | None:
        """Оценка бюджета Директа, ₽/мес (adcost — копейки в день)."""
        return None if self.ad_cost is None else round(self.ad_cost / 100 * 30)


class NicheAdvertiser(Base):
    """Как компания рекламируется по запросам конкретной ниши в конкретном регионе."""

    __tablename__ = "niche_advertisers"
    __table_args__ = (UniqueConstraint("niche_id", "company_id", "base"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    niche_id: Mapped[int] = mapped_column(ForeignKey("niches.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    base: Mapped[str] = mapped_column(String(10))
    keyso_domain: Mapped[str] = mapped_column(String(255))  # как в Keys.so (может быть поддомен)
    share: Mapped[float] = mapped_column(Float, default=0)  # % запросов ниши, где есть реклама домена
    keys_cnt: Mapped[int] = mapped_column(default=0)  # запросов ниши с рекламой домена
    spec_cnt: Mapped[int] = mapped_column(default=0)  # из них в премиум-показах (спецразмещение)
    other_cnt: Mapped[int] = mapped_column(default=0)
    excluded: Mapped[str | None] = mapped_column(String(100))  # причина исключения (маркетплейс и т.п.)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    niche: Mapped[Niche] = relationship(back_populates="advertisers")
    company: Mapped[Company] = relationship()


class Job(Base):
    """Фоновая задача (сбор рекламодателей и т.п.) — чтобы видеть прогресс в интерфейсе."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued/running/done/error
    progress: Mapped[int] = mapped_column(default=0)
    message: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
