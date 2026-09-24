"""Database table definitions (docs/APP_SPEC.md §4)."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Double, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Parent class of all tables. ``Base.metadata`` describes the whole schema (for Alembic)."""


class Result(Base):
    """One test result from a test bench: table ``test_results``.

    ``Mapped[str]`` means NOT NULL, ``Mapped[str | None]`` means NULL is allowed.
    Defaults marked ``server_default`` are applied by PostgreSQL, not by Python.
    """

    __tablename__ = "test_results"
    __table_args__ = (
        CheckConstraint("verdict IN ('PASS', 'FAIL')", name="ck_test_results_verdict"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, index=True)
    test_name: Mapped[str] = mapped_column(Text, index=True)
    temperature_c: Mapped[float] = mapped_column(Double)
    measured_value: Mapped[float] = mapped_column(Double)
    unit: Mapped[str] = mapped_column(Text)
    limit_min: Mapped[float | None] = mapped_column(Double)
    limit_max: Mapped[float | None] = mapped_column(Double)
    verdict: Mapped[str] = mapped_column(Text, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_s: Mapped[float] = mapped_column(Double)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source: Mapped[str] = mapped_column(Text, server_default="simulator")
