"""Fundamental-screen expression parser — happy path + safety + edge cases."""

from __future__ import annotations

import pytest

from quant_researcher.screen.expression import (
    FIELDS,
    ExpressionError,
    parse_expression,
)

# ----- Happy path ---------------------------------------------------------


def test_single_comparison_truthy() -> None:
    p = parse_expression("pe < 30")
    assert p({"pe": 25}) is True
    assert p({"pe": 35}) is False


def test_and_combines_predicates() -> None:
    p = parse_expression("pe < 30 and peg < 1.5")
    assert p({"pe": 25, "peg": 1.2}) is True
    assert p({"pe": 25, "peg": 2.0}) is False
    assert p({"pe": 35, "peg": 1.0}) is False


def test_or_combines_predicates() -> None:
    p = parse_expression("sector == 'Technology' or sector == 'Energy'")
    assert p({"sector": "Technology"}) is True
    assert p({"sector": "Energy"}) is True
    assert p({"sector": "Healthcare"}) is False


def test_chained_comparison() -> None:
    p = parse_expression("5 < pe < 25")
    assert p({"pe": 10}) is True
    assert p({"pe": 5}) is False
    assert p({"pe": 25}) is False
    assert p({"pe": 50}) is False


def test_in_list() -> None:
    p = parse_expression("sector in ['Technology', 'Energy']")
    assert p({"sector": "Technology"}) is True
    assert p({"sector": "Healthcare"}) is False


def test_not_in_list() -> None:
    p = parse_expression("sector not in ['Utilities']")
    assert p({"sector": "Technology"}) is True
    assert p({"sector": "Utilities"}) is False


def test_unary_not() -> None:
    p = parse_expression("not (pe > 50)")
    assert p({"pe": 30}) is True
    assert p({"pe": 60}) is False


def test_scientific_notation() -> None:
    p = parse_expression("market_cap > 10e9")
    assert p({"market_cap": 1.5e10}) is True
    assert p({"market_cap": 5e9}) is False


# ----- Missing data ------------------------------------------------------


def test_missing_field_fails_ordering_comparison() -> None:
    p = parse_expression("pe < 30")
    # Missing pe → comparison is False → symbol excluded.
    assert p({}) is False
    assert p({"pe": None}) is False


def test_missing_field_passes_with_or_when_other_branch_true() -> None:
    p = parse_expression("pe < 30 or sector == 'Tech'")
    assert p({"sector": "Tech"}) is True  # pe missing but OR branch matches


# ----- Safety: disallowed AST nodes --------------------------------------


def test_rejects_function_call() -> None:
    with pytest.raises(ExpressionError, match="Call"):
        parse_expression("__import__('os')")


def test_rejects_attribute_access() -> None:
    # ast.walk hits the Call before the Attribute, so either rejection is
    # fine — we just need it to error.
    with pytest.raises(ExpressionError):
        parse_expression("pe.bit_length() < 30")


def test_rejects_subscript() -> None:
    with pytest.raises(ExpressionError, match="Subscript"):
        parse_expression("pe[0] < 30")


def test_rejects_lambda() -> None:
    # Rejected via the surrounding Call() or the Lambda node, either is OK.
    with pytest.raises(ExpressionError):
        parse_expression("(lambda x: x)(pe) < 30")


def test_rejects_arithmetic() -> None:
    # BinOp (Add/Mult/etc) is intentionally NOT allowed in v1.
    with pytest.raises(ExpressionError):
        parse_expression("pe + 5 < 30")


# ----- Safety: unknown fields --------------------------------------------


def test_unknown_field_rejected_at_parse() -> None:
    with pytest.raises(ExpressionError, match="unknown field"):
        parse_expression("forward_pe < 30")


def test_unknown_field_error_lists_valid() -> None:
    with pytest.raises(ExpressionError, match="valid:"):
        parse_expression("nope < 30")


# ----- Syntax errors -----------------------------------------------------


def test_empty_expression_rejected() -> None:
    with pytest.raises(ExpressionError, match="empty"):
        parse_expression("")
    with pytest.raises(ExpressionError, match="empty"):
        parse_expression("   ")


def test_syntax_error_surfaced() -> None:
    with pytest.raises(ExpressionError, match="syntax"):
        parse_expression("pe < < 30")


# ----- Registry coverage -------------------------------------------------


def test_fields_registry_has_expected_keys() -> None:
    expected = {
        "sector",
        "industry",
        "country",
        "beta",
        "market_cap",
        "pe",
        "peg",
        "pb",
        "ps",
        "ev_ebitda",
        "current_ratio",
        "debt_equity",
        "roe",
        "roa",
        "gross_margin",
        "operating_margin",
        "net_margin",
        "fcf_yield",
        "close",
    }
    assert expected <= set(FIELDS), f"missing: {expected - set(FIELDS)}"
