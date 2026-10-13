"""Formula language: numbers, aliases, `+ - * /`, unary minus, parentheses and four functions.

A formula is parsed with `ast` and walked node by node; anything outside the whitelist is refused
(attribute access, calls to other names, `**`, comparisons, strings ...). Nothing is ever passed
to `eval` or `exec`. Arithmetic is `Decimal` at the precision given by `settings.metrics`.
"""

from __future__ import annotations

import ast
import decimal
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from finrag.core.config import get_settings

DIVISION_BY_ZERO = "division_by_zero"
INVALID_FORMULA = "invalid_formula"
UNKNOWN_NAME = "unknown_name"

_BINARY = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div"}
# function -> (minimum, maximum) number of arguments
_FUNCTIONS: dict[str, tuple[int, int | None]] = {
    "avg": (2, None),
    "abs": (1, 1),
    "min": (2, None),
    "max": (2, None),
}


class EvalError(Exception):
    """A formula cannot be evaluated; `code` says why (e.g. `division_by_zero`)."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class FormulaError(EvalError):
    """A formula is outside the allowed language or names something unknown."""

    def __init__(self, message: str, code: str = INVALID_FORMULA) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class Compiled:
    """A parsed, validated formula and the aliases it uses."""

    text: str
    tree: ast.expr
    names: frozenset[str]


def _check(node: ast.AST, names: set[str]) -> None:
    """Walk a node; refuse anything outside the language and collect the names used."""
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BINARY:
            raise FormulaError(f"operator {type(node.op).__name__} is not allowed")
        _check(node.left, names)
        _check(node.right, names)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub):
            raise FormulaError(f"unary operator {type(node.op).__name__} is not allowed")
        _check(node.operand, names)
    elif isinstance(node, ast.Name):
        if node.id in _FUNCTIONS:
            raise FormulaError(f"'{node.id}' is a function and needs arguments")
        names.add(node.id)
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise FormulaError(f"constant {node.value!r} is not a number")
    elif isinstance(node, ast.Call):
        _check_call(node, names)
    else:
        raise FormulaError(f"{type(node).__name__} is not allowed in a formula")


def _check_call(node: ast.Call, names: set[str]) -> None:
    """A call must be one of the whitelisted functions with plain positional arguments."""
    if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
        raise FormulaError("only avg, abs, min and max may be called")
    if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
        raise FormulaError(f"{node.func.id} takes plain positional arguments")
    low, high = _FUNCTIONS[node.func.id]
    if len(node.args) < low or (high is not None and len(node.args) > high):
        raise FormulaError(f"{node.func.id} was given {len(node.args)} argument(s)")
    for arg in node.args:
        _check(arg, names)


def compile_formula(text: str) -> Compiled:
    """Formula text -> Compiled (FormulaError if it is not in the language)."""
    try:
        tree = ast.parse(text.strip(), mode="eval").body
    except SyntaxError as exc:
        raise FormulaError(f"cannot parse '{text}': {exc.msg}") from exc
    names: set[str] = set()
    _check(tree, names)
    return Compiled(text.strip(), tree, frozenset(names))


def _walk(node: ast.expr, values: Mapping[str, Decimal]) -> Decimal:
    """Evaluate a validated node."""
    if isinstance(node, ast.Constant):
        return Decimal(str(node.value))
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise FormulaError(f"'{node.id}' has no value", UNKNOWN_NAME)
        return values[node.id]
    if isinstance(node, ast.UnaryOp):
        return -_walk(node.operand, values)
    if isinstance(node, ast.BinOp):
        left, right = _walk(node.left, values), _walk(node.right, values)
        kind = _BINARY[type(node.op)]
        if kind == "add":
            return left + right
        if kind == "sub":
            return left - right
        if kind == "mul":
            return left * right
        return left / right
    assert isinstance(node, ast.Call) and isinstance(node.func, ast.Name)  # validated
    args = [_walk(a, values) for a in node.args]
    if node.func.id == "abs":
        return abs(args[0])
    if node.func.id == "min":
        return min(args)
    if node.func.id == "max":
        return max(args)
    return sum(args, Decimal(0)) / len(args)


def evaluate(
    formula: str | Compiled, values: Mapping[str, Decimal], precision: int | None = None
) -> Decimal:
    """Formula and alias values -> Decimal; EvalError('division_by_zero') on a zero divisor."""
    compiled = formula if isinstance(formula, Compiled) else compile_formula(formula)
    digits = precision or get_settings().metrics.decimal_precision
    context = decimal.Context(
        prec=digits, traps=[decimal.DivisionByZero, decimal.InvalidOperation, decimal.Overflow]
    )
    with decimal.localcontext(context):
        try:
            return _walk(compiled.tree, values)
        except (decimal.DivisionByZero, decimal.InvalidOperation) as exc:
            raise EvalError(DIVISION_BY_ZERO, f"division by zero in '{compiled.text}'") from exc
