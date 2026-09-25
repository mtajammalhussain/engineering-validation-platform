"""SQL built for the reports (no database: statements are inspected or compiled).

Assertions look at the statement's structure (WHERE conditions, selected columns) and at
key fragments of the compiled PostgreSQL SQL, not at one exact SQL string.
"""

import operator
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from app.models import test_results
from app.queries import (
    SummaryCounts,
    build_grouped_statement,
    build_summary_statement,
    build_timeseries_statement,
    fetch_rows,
    fetch_summary_counts,
)
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


# --- grouped reports: by-device / by-test ----------------------------------------------------

GROUP_KEYS = ["device_id", "test_name"]


@pytest.mark.parametrize("key", GROUP_KEYS)
def test_grouped_statement_is_select_only(key):
    statement = build_grouped_statement(key, START, END)
    sql = compiled(statement)

    assert isinstance(statement, Select)
    assert not (sql.isinsert or sql.isupdate or sql.isdelete)


@pytest.mark.parametrize("key", GROUP_KEYS)
def test_grouped_statement_selects_key_and_aggregates_only(key):
    statement = build_grouped_statement(key, START, END)

    assert [c.name for c in statement.selected_columns] == [key, "total", "passed", "failed"]


@pytest.mark.parametrize("key", GROUP_KEYS)
def test_grouped_statement_counts_in_sql(key):
    sql = compiled(build_grouped_statement(key, START, END))
    text = str(sql)

    assert "count(*) AS total" in text
    assert text.count("count(*) FILTER (WHERE test_results.verdict = ") == 2
    assert {"PASS", "FAIL"} <= set(sql.params.values())


@pytest.mark.parametrize("key", GROUP_KEYS)
def test_grouped_statement_groups_by_the_key_only(key):
    statement = build_grouped_statement(key, START, END)

    assert [c.name for c in statement._group_by_clauses] == [key]
    assert f"GROUP BY test_results.{key}" in str(compiled(statement))


@pytest.mark.parametrize("key", GROUP_KEYS)
def test_grouped_statement_orders_by_failed_desc_then_key_asc(key):
    statement = build_grouped_statement(key, START, END)

    assert f"ORDER BY failed DESC, test_results.{key} ASC" in str(compiled(statement))


@pytest.mark.parametrize("key", GROUP_KEYS)
def test_grouped_statement_applies_window(key):
    statement = build_grouped_statement(key, START, END)

    assert conditions(statement) == [
        ("started_at", operator.ge, START),
        ("started_at", operator.lt, END),
    ]


@pytest.mark.parametrize("key", GROUP_KEYS)
def test_grouped_statement_applies_both_exact_filters(key):
    statement = build_grouped_statement(key, START, END, device_id="ECU-004", test_name="X")

    assert conditions(statement)[2:] == [
        ("device_id", operator.eq, "ECU-004"),
        ("test_name", operator.eq, "X"),
    ]


def test_grouped_statement_filter_on_its_own_key_is_applied_before_grouping():
    statement = build_grouped_statement("device_id", START, END, device_id="ECU-004")
    text = str(compiled(statement))

    assert ("device_id", operator.eq, "ECU-004") in conditions(statement)
    assert text.index("WHERE") < text.index("GROUP BY")


def test_grouped_statement_rejects_unknown_key():
    # The grouping column comes from an application allow-list, never from the request.
    with pytest.raises(KeyError):
        build_grouped_statement("verdict", START, END)


def test_fetch_rows_runs_one_statement_and_returns_all_aggregate_rows():
    session = FakeSession()
    session.rows = [SimpleNamespace(device_id="ECU-001", total=3, passed=2, failed=1)]
    statement = build_grouped_statement("device_id", START, END)

    assert fetch_rows(session, statement) == session.rows
    assert session.statements == [statement]


# --- timeseries ------------------------------------------------------------------------------

INTERVALS = ["hour", "day"]


@pytest.mark.parametrize("interval", INTERVALS)
def test_timeseries_statement_is_select_only(interval):
    statement = build_timeseries_statement(interval, START, END)
    sql = compiled(statement)

    assert isinstance(statement, Select)
    assert not (sql.isinsert or sql.isupdate or sql.isdelete)


@pytest.mark.parametrize("interval", INTERVALS)
def test_timeseries_uses_three_argument_date_trunc_in_utc(interval):
    sql = compiled(build_timeseries_statement(interval, START, END))
    text = str(sql)

    assert "date_trunc(%(date_trunc_1)s, test_results.started_at, %(date_trunc_2)s)" in text
    assert sql.params["date_trunc_1"] == interval
    assert sql.params["date_trunc_2"] == "UTC"


@pytest.mark.parametrize("interval", INTERVALS)
def test_timeseries_groups_and_orders_by_the_bucket_label_not_a_second_expression(interval):
    # PostgreSQL rejects GROUP BY date_trunc($3, ...) next to SELECT date_trunc($1, ...)
    # ("must appear in the GROUP BY clause"). Grouping by the output column name avoids a
    # second, separately parameterised copy of the expression.
    text = str(compiled(build_timeseries_statement(interval, START, END)))

    assert text.count("date_trunc(") == 1
    assert "AS bucket_start" in text
    assert text.rstrip().endswith("GROUP BY bucket_start ORDER BY bucket_start")
    assert "date_trunc_3" not in text


@pytest.mark.parametrize("interval", INTERVALS)
def test_timeseries_selects_bucket_and_aggregates_only(interval):
    statement = build_timeseries_statement(interval, START, END)
    text = str(compiled(statement))

    assert [c.name for c in statement.selected_columns] == [
        "bucket_start", "total", "passed", "failed",
    ]
    assert "count(*) AS total" in text
    assert text.count("count(*) FILTER (WHERE test_results.verdict = ") == 2


def test_timeseries_applies_window_and_both_filters():
    statement = build_timeseries_statement(
        "hour", START, END, device_id="ECU-004", test_name="Sleep Current"
    )

    assert conditions(statement) == [
        ("started_at", operator.ge, START),
        ("started_at", operator.lt, END),
        ("device_id", operator.eq, "ECU-004"),
        ("test_name", operator.eq, "Sleep Current"),
    ]


@pytest.mark.parametrize(
    "device_id, test_name, expected",
    [("ECU-004", None, [("device_id", operator.eq, "ECU-004")]),
     (None, "Sleep Current", [("test_name", operator.eq, "Sleep Current")]),
     (None, None, [])],
    ids=["device-only", "test-only", "none"],
)
def test_timeseries_applies_single_filters(device_id, test_name, expected):
    statement = build_timeseries_statement("day", START, END, device_id, test_name)

    assert conditions(statement)[2:] == expected


def test_timeseries_generates_no_empty_buckets():
    text = str(compiled(build_timeseries_statement("hour", START, END)))

    assert "generate_series" not in text


@pytest.mark.parametrize("interval", ["minute", "week", "hour'; DROP TABLE x;--", ""])
def test_timeseries_rejects_intervals_outside_the_allow_list(interval):
    with pytest.raises(ValueError):
        build_timeseries_statement(interval, START, END)
