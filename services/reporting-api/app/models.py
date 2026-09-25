"""Read-only description of the ``test_results`` table (docs/APP_SPEC.md §4, §7).

The table is owned by the Results API: its schema is created and changed only by the
Results API's Alembic migrations. This module does not own, create or change anything.

It is a SQLAlchemy Core ``Table``: a plain Python description that lets SQLAlchemy build
SELECT statements with checked column names. Only the four columns the reports read are
listed; the types are the expected contract with the Results API schema. Whether this
description matches the real table is verified by the PostgreSQL integration tests.
"""

from sqlalchemy import Column, DateTime, MetaData, Table, Text

metadata = MetaData()

test_results = Table(
    "test_results",
    metadata,
    Column("device_id", Text),
    Column("test_name", Text),
    Column("verdict", Text),  # "PASS" or "FAIL", decided by the test bench
    Column("started_at", DateTime(timezone=True)),  # PostgreSQL timestamptz
)
