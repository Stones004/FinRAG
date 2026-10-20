"""Fixtures for M09: the real registries and prompts in the temp repo."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REAL = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def registries(isolated_repo: Path) -> None:
    """Put the real registries, vocabulary and prompts where settings.paths points."""
    for name in ("taxonomy.yaml", "metrics.yaml", "validation_rules.yaml", "intents.yaml"):
        shutil.copy(REAL / "config" / name, isolated_repo / "config" / name)
    shutil.copytree(REAL / "config" / "prompts", isolated_repo / "config" / "prompts")
    shutil.copytree(REAL / "config" / "vocab", isolated_repo / "config" / "vocab")


@pytest.fixture(autouse=True)
def _claim_prompts(isolated_repo: Path, request: pytest.FixtureRequest) -> None:
    """Tests of the claim shape (prompt v2) run on v2; tests marked `selection` run on v3."""
    if request.node.get_closest_marker("selection") is None:
        path = isolated_repo / "config" / "settings.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("prompt_version: v3", "prompt_version: v2"),
            encoding="utf-8",
        )
        reset_settings_cache()


# --- shared helpers ------------------------------------------------------------------------

import sqlalchemy as sa  # noqa: E402

from finrag.core.config import get_settings, reset_settings_cache  # noqa: E402
from finrag.core.db import init_db, tables, transaction  # noqa: E402
from finrag.core.fiscal import label_to_period  # noqa: E402
from finrag.core.types import utc_now_iso  # noqa: E402
from finrag.query.availability import StubAvailability  # noqa: E402
from finrag.query.intents import load_intents  # noqa: E402
from finrag.query.text import Working, reset_vocab_cache  # noqa: E402

GABRIEL, HDFC, NETWEB = "INE524A01029", "INE040A01034", "INE0NT901020"
TODAY = __import__("datetime").date(2026, 10, 19)


@pytest.fixture(autouse=True)
def _fresh_vocab() -> None:
    reset_vocab_cache()


@pytest.fixture
def seeded() -> None:
    """The three watch-list companies in a temp database (as the real one holds them)."""
    init_db()
    rows = [
        (GABRIEL, "Gabriel India Limited", "GABRIEL", "general", "consolidated"),
        (HDFC, "HDFC Bank Limited", "HDFCBANK", "bank", "consolidated"),
        (NETWEB, "Netweb Technologies India Limited", "NETWEB", "general", "standalone"),
    ]
    now = utc_now_iso()
    with transaction() as conn:
        for isin, name, symbol, sector, basis in rows:
            conn.execute(
                sa.insert(tables.entities).values(
                    isin=isin,
                    name=name,
                    nse_symbol=symbol,
                    sector_type=sector,
                    default_basis=basis,
                    status="active",
                    added_on=now,
                    updated_on=now,
                )
            )
            for alias, kind in ((symbol, "symbol"), (name, "name")):
                conn.execute(
                    sa.insert(tables.entity_aliases).values(isin=isin, alias=alias, alias_type=kind)
                )


@pytest.fixture
def intents():
    return load_intents()


@pytest.fixture
def settings():
    return get_settings().query


@pytest.fixture
def avail() -> StubAvailability:
    """Every company has data up to Q1 FY27 and FY26."""
    labels = ["Q1 FY26", "Q2 FY26", "Q3 FY26", "Q4 FY26", "Q1 FY27", "H1 FY26", "FY25", "FY26"]
    return StubAvailability([label_to_period(x) for x in labels])


def working(question: str, intents) -> Working:
    return Working(question, intents)


# --- database kit for retrieval and answer tests -------------------------------------------

import hashlib  # noqa: E402
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from finrag.core.ids import PREFIX_FACT, PREFIX_METRIC, filing_id_from_sha, new_id  # noqa: E402


def _span(label: str) -> tuple[str, str, str]:
    p = label_to_period(label)
    return p.start.isoformat(), p.end.isoformat(), p.type.value


@pytest.fixture
def kit(seeded) -> SimpleNamespace:
    """Functions that write filings, facts, metric values and restatements to the temp database."""
    now = utc_now_iso()
    made: set[str] = set()

    def filing(
        isin: str, label: str, key: str = "", revision: int = 0, filed_on: str = "2026-06-01"
    ):
        start, end, ptype = _span(label)
        sha = hashlib.sha256(f"{isin}{label}{key}{revision}".encode()).hexdigest()
        fid = filing_id_from_sha(sha)
        if fid in made:
            return fid
        made.add(fid)
        with transaction() as conn:
            conn.execute(
                sa.insert(tables.filings).values(
                    filing_id=fid,
                    isin=isin,
                    source="test",
                    filing_type="financial_results",
                    submission="revision" if revision else "original",
                    revision_no=revision,
                    taxonomy_family="indas",
                    basis="consolidated",
                    period_start=start,
                    period_end=end,
                    period_type=ptype,
                    filed_on=filed_on,
                    raw_path="x",
                    sha256=sha,
                    format="xbrl",
                    status="normalized",
                    created_at=now,
                )
            )
        return fid

    def fact(
        isin, concept, label, value, basis="consolidated", filing_id=None, filed_on="2026-06-01"
    ):
        start, end, ptype = _span(label)
        fid = filing_id or filing(isin, label)
        fact_id = new_id(PREFIX_FACT)
        with transaction() as conn:
            conn.execute(
                sa.insert(tables.canonical_facts).values(
                    fact_id=fact_id,
                    isin=isin,
                    concept=concept,
                    period_start=start,
                    period_end=end,
                    period_type=ptype,
                    basis=basis,
                    value=str(value),
                    unit="INR",
                    source_filing_id=fid,
                    mapping_method="standard",
                    is_derived=0,
                    filed_on=filed_on,
                    created_at=now,
                    source_class="xbrl",
                )
            )
        return fact_id

    def restatement(isin, concept, label, basis, old_id, new_id_, abs_diff, flagged=1):
        start, end, _ = _span(label)
        with transaction() as conn:
            conn.execute(
                sa.insert(tables.restatements).values(
                    isin=isin,
                    concept=concept,
                    period_start=start,
                    period_end=end,
                    basis=basis,
                    old_fact_id=old_id,
                    new_fact_id=new_id_,
                    abs_diff=str(abs_diff),
                    rel_diff="0.2",
                    flagged=flagged,
                    created_at=now,
                )
            )

    def metric(
        isin,
        metric_id,
        label,
        value,
        basis="consolidated",
        details=None,
        status=None,
        reason="Computed from reported facts.",
        instant_end=None,
    ):
        start, end, ptype = _span(label)
        if instant_end:
            start = end = instant_end
            ptype = "INSTANT"
        status = status or ("computed" if value is not None else "not_computed")
        mv_id = new_id(PREFIX_METRIC)
        with transaction() as conn:
            conn.execute(
                sa.insert(tables.metric_values).values(
                    metric_value_id=mv_id,
                    isin=isin,
                    metric_id=metric_id,
                    formula_version="1.0",
                    period_start=start,
                    period_end=end,
                    period_type=ptype,
                    basis=basis,
                    value=None if value is None else str(value),
                    status=status,
                    reason=reason,
                    input_fact_ids_json="[]",
                    run_id="RUN-test",
                    created_at=now,
                    details_json=json.dumps(details or {"code": "computed", "basis_used": basis}),
                )
            )
        return mv_id

    return SimpleNamespace(filing=filing, fact=fact, restatement=restatement, metric=metric)


def claim(text: str, cites: list[str], quote: str) -> dict:
    """One claim as the model returns it."""
    return {"text": text, "cites": cites, "quote": quote}


def claims_json(*claims: dict) -> str:
    """Claims -> the JSON reply of the model."""
    import json

    return json.dumps({"claims": list(claims)})
