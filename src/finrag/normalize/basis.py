"""Decide a fact's basis: the fact's own, then the filing row; otherwise unknown."""

from __future__ import annotations

from finrag.core.types import Basis


def resolve_basis(fact_basis: str | None, filing_basis: str | None) -> Basis:
    """Fact basis (XBRL meta or page heading) -> filing row basis -> unknown."""
    for candidate in (fact_basis, filing_basis):
        if candidate and candidate != Basis.UNKNOWN.value:
            return Basis(candidate)
    return Basis.UNKNOWN
