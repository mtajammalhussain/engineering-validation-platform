"""SQL built for the summary report (no database: statements are inspected or compiled).

Assertions look at the statement's structure (WHERE conditions, selected columns) and at
key fragments of the compiled PostgreSQL SQL, not at one exact SQL string.
"""

import operator
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from app.models import test_results
from app.queries import SummaryCounts, build_summary_statement, fetch_summary_counts
from tests.unit.fakes import FakeSession

START = datetime(2026, 9, 16, tzinfo=timezone.utc)
END = datetime(2026, 9, 23, tzinfo=timezone.utc)


def compiled(statement):
    return statement.compile(dialect=postgresql.dialect())


def conditions(statement) -> list[tuple[str, object, object]]:
    """The WHERE clause as (column name, operator, value) triples."""
    clauses = statement.whereclause.clauses
    return [(c.left.name, c.operator, c.right.value) for c in clauses]


def test_statement_is_a_select_and_nothing_else():
    statement = build_summary_statement(START, END)
    sql = compiled(statement)

    assert isinstance(statement, Select)
    assert not (sql.isinsert or sql.isupdate or sql.isdelete)
    assert str(sql).lstrip().startswith("SELECT")


def test_selects_only_the_three_aggregates():
    statement = build_summary_statement(START, END)

    assert [c.name for c in statement.selected_columns] == ["total", "passed", "failed"]
    # No plain column is selected, so no individual result row can come back.
    assert all(c.name not in test_results.c for c in statement.selected_columns)


def test_counts_use_count_and_filter_on_verdict():
    sql = compiled(build_summary_statement(START, END))
    text = str(sql)

    assert "count(*) AS total" in text
    assert text.count("count(*) FILTER (WHERE test_results.verdict = ") == 2
    assert "AS passed" in text and "AS failed" in text
    assert {"PASS", "FAIL"} <= set(sql.params.values())
    assert "FROM test_results" in text
    assert "GROUP BY" not in text


def test_window_is_from_inclusive_to_exclusive():
    statement = build_summary_statement(START, END)

    assert conditions(statement) == [
        ("started_at", operator.ge, START),
        ("started_at", operator.lt, END),
    ]
    text = str(compiled(statement))
    assert "test_results.started_at >= " in text
    assert "test_results.started_at < " in text


def test_absent_filters_add_no_predicates():
    text = str(compiled(build_summary_statement(START, END)))

    assert "device_id" not in text
    assert "test_name" not in text


def test_device_id_adds_exact_equality():
    statement = build_summary_statement(START, END, device_id="ECU-004")

    assert ("device_id", operator.eq, "ECU-004") in conditions(statement)
    assert "test_name" not in str(compiled(statement))


def test_test_name_adds_exact_equality():
    statement = build_summary_statement(START, END, test_name="Sleep Current")

    assert ("test_name", operator.eq, "Sleep Current") in conditions(statement)
    assert "device_id" not in str(compiled(statement))


def test_both_filters_combine_with_the_window_using_and():
    statement = build_summary_statement(START, END, device_id="ECU-004", test_name="Wake-up Time")

    assert conditions(statement) == [
        ("started_at", operator.ge, START),
        ("started_at", operator.lt, END),
        ("device_id", operator.eq, "ECU-004"),
        ("test_name", operator.eq, "Wake-up Time"),
    ]
    assert statement.whereclause.operator is operator.and_


def test_filter_values_are_bound_parameters_not_sql_text():
    sql = compiled(build_summary_statement(START, END, device_id="ECU-004'; DROP TABLE x;--"))

    assert "DROP TABLE" not in str(sql)
    assert "ECU-004'; DROP TABLE x;--" in sql.params.values()


def test_fetch_runs_one_statement_and_returns_its_single_row():
    session = FakeSession()
    session.row = SimpleNamespace(total=120, passed=111, failed=9)

    counts = fetch_summary_counts(session, START, END, device_id="ECU-001")

    assert counts == SummaryCounts(total=120, passed=111, failed=9)
    assert len(session.statements) == 1
    assert ("device_id", operator.eq, "ECU-001") in conditions(session.statements[0])
