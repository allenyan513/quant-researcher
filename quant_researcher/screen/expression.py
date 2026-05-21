"""Fundamental-screen expression parser.

Users write Python-like expressions, e.g.::

    pe < 30 and peg < 1.5 and sector == 'Technology'
    market_cap > 10e9 and 5 < pe < 25
    sector in ['Technology', 'Energy'] and fcf_yield > 0.04

We parse via `ast.parse(..., mode='eval')` and walk the AST manually — we
**never** call Python's `eval`. Only the following AST node types are
allowed; anything else (`Call`, `Attribute`, `Subscript`, `Lambda`,
comprehensions, …) raises `ExpressionError` at parse time.

Allowed: `BoolOp(And|Or)`, `UnaryOp(Not|USub|UAdd)`,
`Compare(Lt|LtE|Gt|GtE|Eq|NotEq|In|NotIn)`, `Name` (must resolve to a
registered field), `Constant` (number / string / bool), `List`, `Tuple`.

At evaluation time, each registered field name is looked up in a
per-symbol `state: dict[str, Any]`. Missing or `None` values short-circuit
the surrounding comparison to `False` (the symbol is excluded), so a
ticker with no `pe_ratio` row in `financial_ratios` won't pass `pe < 30`.

Field registry → DB source (used by the engine to build the state dict):
* `sector`, `industry`, `country`, `beta` → `profiles.<col>`
* `market_cap` → `profiles.raw["mktCap"]` (FMP /profile field, JSON)
* `pe`, `peg`, `pb`, `ps`, `ev_ebitda`, `current_ratio`,
  `debt_equity`, `roe`, `roa`, `gross_margin`, `operating_margin`,
  `net_margin`, `fcf_yield` → `financial_ratios.<col>` (latest annual row)
* `close` → `daily_prices.close` (latest trade_date)
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Compare,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
    ast.In,
    ast.NotIn,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
)


FIELDS: dict[str, str] = {
    # profile
    "sector": "profile.sector",
    "industry": "profile.industry",
    "country": "profile.country",
    "beta": "profile.beta",
    "market_cap": "profile.raw[mktCap]",
    # financial_ratios (latest annual row per symbol)
    "pe": "financial_ratios.pe_ratio",
    "peg": "financial_ratios.peg_ratio",
    "pb": "financial_ratios.price_to_book",
    "ps": "financial_ratios.price_to_sales",
    "ev_ebitda": "financial_ratios.ev_to_ebitda",
    "current_ratio": "financial_ratios.current_ratio",
    "debt_equity": "financial_ratios.debt_to_equity",
    "roe": "financial_ratios.return_on_equity",
    "roa": "financial_ratios.return_on_assets",
    "gross_margin": "financial_ratios.gross_margin",
    "operating_margin": "financial_ratios.operating_margin",
    "net_margin": "financial_ratios.net_margin",
    "fcf_yield": "financial_ratios.fcf_yield",
    # daily_prices (latest trade_date per symbol)
    "close": "daily_prices.close",
}


class ExpressionError(ValueError):
    """Raised on syntax error, disallowed node, or unknown field."""


Predicate = Callable[[dict[str, Any]], bool]


def parse_expression(expr: str) -> Predicate:
    """Parse `expr` → predicate(state_dict) -> bool."""
    if not expr or not expr.strip():
        raise ExpressionError("empty expression")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error: {e.msg}") from e
    _validate_nodes(tree)
    _validate_names(tree)
    return lambda state: bool(_eval(tree.body, state))


def _validate_nodes(tree: ast.AST) -> None:
    for child in ast.walk(tree):
        if not isinstance(child, _ALLOWED_NODES):
            raise ExpressionError(
                f"disallowed AST node: {type(child).__name__}"
                " (only comparisons, and/or/not, names, and constants are allowed)"
            )


def _validate_names(tree: ast.AST) -> None:
    for child in ast.walk(tree):
        if isinstance(child, ast.Name) and child.id not in FIELDS:
            raise ExpressionError(
                f"unknown field: {child.id!r} "
                f"(valid: {', '.join(sorted(FIELDS))})"
            )


def _eval(node: ast.AST, state: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return state.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(el, state) for el in node.elts]
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, state)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v if v is not None else None
        if isinstance(node.op, ast.UAdd):
            return +v if v is not None else None
        raise ExpressionError(f"unsupported unary op {type(node.op).__name__}")
    if isinstance(node, ast.BoolOp):
        results = [_eval(v, state) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(results)
        if isinstance(node.op, ast.Or):
            return any(results)
        raise ExpressionError(f"unsupported bool op {type(node.op).__name__}")
    if isinstance(node, ast.Compare):
        left = _eval(node.left, state)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            right = _eval(right_node, state)
            if not _cmp(left, op, right):
                return False
            left = right  # Python's chained comparison semantics
        return True
    raise ExpressionError(f"unsupported node {type(node).__name__}")


def _cmp(left: Any, op: ast.cmpop, right: Any) -> bool:
    # `in` / `not in` work even with None on the left side.
    if isinstance(op, ast.In):
        try:
            return left in right if right is not None else False
        except TypeError:
            return False
    if isinstance(op, ast.NotIn):
        try:
            return left not in right if right is not None else True
        except TypeError:
            return False
    # For ordering and equality, treat None on either side as a failed match
    # (the symbol is excluded). This matches "no data => can't pass screen".
    if left is None or right is None:
        return False
    try:
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
    except TypeError:
        return False
    raise ExpressionError(f"unsupported comparison op {type(op).__name__}")
