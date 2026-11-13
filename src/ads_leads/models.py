"""Основная сущность — компания-рекламодатель (ключ: домен, позже + ИНН).

Поля сигналов сайта и данных Контура добавим, когда будет понятна
структура выгрузок Keys.so и Контур.Продаж.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Column, DateTime, ForeignKey, String, Table, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ads_leads.db import Base


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
    region_lr: Mapped[int] = mapped_column(default=213)  # 213 = Москва
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    companies: Mapped[list["Company"]] = relationship(
        secondary=company_niches, back_populates="niches"
    )
    queries: Mapped[list["NicheQuery"]] = relationship(
        back_populates="niche", cascade="all, delete-orphan", order_by="NicheQuery.id"
    )


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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    niches: Mapped[list[Niche]] = relationship(
        secondary=company_niches, back_populates="companies"
    )
