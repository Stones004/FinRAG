"""M05 unit tests beyond the acceptance criteria: ordering, quarantine, derivation rules, CLI."""

from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from finrag.cli import app
from finrag.core.db import tables, transaction
from finrag.core.types import Period, PeriodType
from finrag.facts.current_view import get_fact, get_series
from finrag.facts.derivation import derive_groups, derive_isin
from finrag.ingestion import registry
from finrag.ingestion.filename_time import filename_timestamp

ISIN = "INE111A01010"
BASIS = "consolidated"
runner = CliRunner()


def per(kit, name: str) -> Period:
    s, e, k = kit.FY26[name]
    return Period(s, e, PeriodType(k))


def cur(kit, name, concept="revenue", **kw):
    return get_fact(ISIN, concept, per(kit, name), BASIS, **kw)


def restatements() -> list[sa.Row]:
    with transaction() as conn:
        return list(conn.execute(sa.select(tables.restatements).order_by(tables.restatements.c.id)))


def test_revision_with_same_filed_on_wins_by_revision_no(kit) -> None:
    kit.add_filing("FIL-o", filed_on="2026-05-27")
    kit.add_filing("FIL-r", filed_on="2026-05-27", revision_no=1)
    kit.write([kit.cand("FIL-o", "FY", "4435590000", "cfo")])
    kit.write([kit.cand("FIL-r", "FY", "3452740000", "cfo", revision_no=1)])
    assert cur(kit, "FY", "cfo").value == kit.D("3452740000")
    row = restatements()[0]
    assert row.flagged == 1 and row.abs_diff == "982850000"


def test_revision_written_before_original_still_restates_the_right_way(kit) -> None:
    kit.add_filing("FIL-o", filed_on="2026-05-27")
    kit.add_filing("FIL-r", filed_on="2026-05-27", revision_no=1)
    kit.write([kit.cand("FIL-r", "FY", "3452740000", "cfo", revision_no=1)])
    kit.write([kit.cand("FIL-o", "FY", "4435590000", "cfo")])
    assert cur(kit, "FY", "cfo").value == kit.D("3452740000")
    with transaction() as conn:
        old, new = (
            conn.execute(
                sa.select(tables.canonical_facts.c.value).where(
                    tables.canonical_facts.c.fact_id.in_([restatements()[0].old_fact_id])
                )
            ).scalar(),
            conn.execute(
                sa.select(tables.canonical_facts.c.value).where(
                    tables.canonical_facts.c.fact_id == restatements()[0].new_fact_id
                )
            ).scalar(),
        )
    assert (old, new) == ("4435590000", "3452740000")


def test_set_filed_on_is_recorded_and_as_of_returns_the_original(kit) -> None:
    kit.add_filing("FIL-o", filed_on="2026-05-27")
    kit.add_filing("FIL-r", filed_on="2026-05-27", revision_no=1)
    kit.write([kit.cand("FIL-o", "FY", "4435590000", "cfo")])
    kit.write([kit.cand("FIL-r", "FY", "3452740000", "cfo", revision_no=1)])
    old, new = registry.set_filed_on("FIL-r", "2026-06-29", "NSE listing: revised 29-06-2026")
    assert (old, new) == ("2026-05-27", "2026-06-29")
    assert cur(kit, "FY", "cfo", as_of=date(2026, 6, 1)).value == kit.D("4435590000")
    assert cur(kit, "FY", "cfo", as_of=date(2026, 6, 29)).value == kit.D("3452740000")
    assert cur(kit, "FY", "cfo").value == kit.D("3452740000")
    with transaction() as conn:
        job = conn.execute(
            sa.select(tables.jobs).where(tables.jobs.c.job_type == "set_filed_on")
        ).one()
    assert "2026-05-27" in job.stats_json and "2026-06-29" in job.stats_json
    assert "NSE listing" in job.stats_json


def test_set_filed_on_needs_a_reason_and_a_valid_date(kit) -> None:
    kit.add_filing("FIL-o")
    with pytest.raises(registry.RegistryError):
        registry.set_filed_on("FIL-o", "2026-06-29", "  ")
    with pytest.raises(registry.RegistryError):
        registry.set_filed_on("FIL-o", "29/06/2026", "why")
    with pytest.raises(registry.RegistryError):
        registry.set_filed_on("FIL-none", "2026-06-29", "why")


def test_set_filed_on_cli(kit) -> None:
    kit.add_filing("FIL-o", filed_on="2026-05-27")
    result = runner.invoke(
        app, ["ingest", "set-filed-on", "FIL-o", "2026-06-29", "--reason", "from listing"]
    )
    assert result.exit_code == 0 and "2026-05-27 -> 2026-06-29" in result.output
    assert runner.invoke(app, ["ingest", "set-filed-on", "FIL-o", "2026-06-29"]).exit_code != 0


def test_quarantined_filing_is_refused_and_never_a_source(kit) -> None:
    kit.add_filing("FIL-q", status="quarantined")
    stats = kit.write([kit.cand("FIL-q", "Q1", "100")])
    assert (stats.inserted, stats.refused) == (0, 1)
    # a fact written before the filing was quarantined is not served either
    kit.add_filing("FIL-ok")
    kit.write([kit.cand("FIL-ok", "Q1", "100")])
    with transaction() as conn:
        conn.execute(sa.update(tables.filings).values(status="quarantined"))
    assert cur(kit, "Q1") is None


def test_pdf_facts_only_restate_against_pdf_facts(kit) -> None:
    kit.add_filing("FIL-p1", fmt="pdf", filed_on=None)
    kit.add_filing("FIL-p2", fmt="pdf", filed_on="2026-09-01")
    kit.write([kit.cand("FIL-p1", "Q1", "100", extractor="ocr", filed_on=None)])
    kit.write([kit.cand("FIL-p2", "Q1", "101", extractor="pdf_table", filed_on="2026-09-01")])
    assert len(restatements()) == 1 and cur(kit, "Q1").value == kit.D("101")


def test_restatement_rounding_differences_below_tolerance_are_unflagged(kit) -> None:
    kit.add_filing("FIL-1", filed_on="2026-05-01")
    kit.add_filing("FIL-2", filed_on="2026-05-02")
    kit.write([kit.cand("FIL-1", "FY", "780670000", "net_change_in_cash")])
    kit.write([kit.cand("FIL-2", "FY", "780690000", "net_change_in_cash", filed_on="2026-05-02")])
    assert [r.flagged for r in restatements()] == [0]


def test_per_share_restatement_uses_the_relative_tolerance_only(kit) -> None:
    kit.add_filing("FIL-1", filed_on="2026-05-01")
    kit.add_filing("FIL-2", filed_on="2026-05-02")
    kit.write([kit.cand("FIL-1", "FY", "10.00", "eps_basic", unit="INR_PER_SHARE")])
    kit.write(
        [kit.cand("FIL-2", "FY", "12.00", "eps_basic", unit="INR_PER_SHARE", filed_on="2026-05-02")]
    )
    assert [r.flagged for r in restatements()] == [1]


def test_a_zero_eps_filled_in_by_a_revision_is_recorded_but_not_flagged(kit) -> None:
    """Gabriel Q1 FY26 standalone: the revision only fixes the EPS and face value tags."""
    kit.add_filing("FIL-1", filed_on="2026-05-01")
    kit.add_filing("FIL-2", filed_on="2026-05-01")
    kit.write([kit.cand("FIL-1", "FY", "0", "eps_basic", unit="INR_PER_SHARE")])
    kit.write(
        [kit.cand("FIL-2", "FY", "3.88", "eps_basic", unit="INR_PER_SHARE", filed_on="2026-05-01")]
    )
    rows = restatements()
    assert len(rows) == 1 and rows[0].flagged == 0 and rows[0].rel_diff is None
    kit.add_filing("FIL-3", filed_on="2026-05-02")
    kit.add_filing("FIL-4", filed_on="2026-05-03")
    kit.write([kit.cand("FIL-3", "H1", "0", "cfo", filed_on="2026-05-02")])
    kit.write([kit.cand("FIL-4", "H1", "500000000", "cfo", filed_on="2026-05-03")])
    assert [r.flagged for r in restatements()][-1] == 1  # money that appears from 0 is flagged


def test_series_returns_latest_n_oldest_first(kit) -> None:
    kit.add_filing("FIL-1")
    kit.write([kit.cand("FIL-1", q, v) for q, v in (("Q1", "1"), ("Q2", "2"), ("Q3", "3"))])
    series = get_series(ISIN, "revenue", PeriodType.Q, BASIS, 2)
    assert [f.value for f in series] == [kit.D("2"), kit.D("3")]


def test_h2_is_derived_from_fy_and_h1(kit, real_taxonomy) -> None:
    kit.add_filing("FIL-1", filed_on="2026-01-01")
    kit.add_filing("FIL-2", filed_on="2026-05-01")
    kit.write([kit.cand("FIL-1", "H1", "400", "cfo", filed_on="2026-01-01")])
    kit.write([kit.cand("FIL-2", "FY", "1000", "cfo")])
    derive_isin(ISIN, taxonomy=real_taxonomy)
    h2 = cur(kit, "H2", "cfo")
    assert h2.value == kit.D("600") and h2.source_filing_id == "FIL-2"  # the latest input's filing
    assert h2.derivation["all_xbrl"] is True and h2.derivation["input_classes"] == ["xbrl"]
    assert h2.source_class.value == "derived"


def test_derived_from_xbrl_beats_a_reported_pdf_value(kit, real_taxonomy) -> None:
    kit.add_filing("FIL-1", filed_on="2026-01-01")
    kit.add_filing("FIL-2", filed_on="2026-05-01")
    kit.add_filing("FIL-p", fmt="pdf", filed_on="2026-09-01")
    kit.write([kit.cand("FIL-1", "H1", "400"), kit.cand("FIL-2", "FY", "1000")])
    kit.write([kit.cand("FIL-p", "H2", "599", extractor="ocr", filed_on="2026-09-01")])
    derive_isin(ISIN, taxonomy=real_taxonomy)  # H2 is reported (by OCR): not derived
    assert cur(kit, "H2").value == kit.D("599")


@pytest.mark.parametrize(
    "concept,unit", [("eps_basic", "INR_PER_SHARE"), ("bank_gross_npa", "INR")]
)
def test_never_derivable_concepts_are_not_derived(kit, real_taxonomy, concept, unit) -> None:
    kit.add_filing("FIL-1")
    kit.write(
        [kit.cand("FIL-1", p, v, concept, unit=unit) for p, v in (("H1", "400"), ("Q1", "150"))]
    )
    assert derive_groups([(ISIN, concept, BASIS)], taxonomy=real_taxonomy).inserted == 0
    assert cur(kit, "Q2", concept) is None


def test_bank_ratios_are_not_derived(kit, real_taxonomy) -> None:
    kit.add_filing("FIL-1")
    kit.write(
        [
            kit.cand("FIL-1", p, v, "bank_cet1_ratio", unit="RATIO")
            for p, v in (("H1", "0.19"), ("Q1", "0.18"))
        ]
    )
    assert derive_groups([(ISIN, "bank_cet1_ratio", BASIS)], taxonomy=real_taxonomy).inserted == 0


def test_low_confidence_pdf_input_is_not_used(kit, real_taxonomy) -> None:
    kit.add_filing("FIL-x")
    kit.add_filing("FIL-p", fmt="pdf", filed_on="2026-09-01")
    kit.write([kit.cand("FIL-x", "H1", "400")])
    kit.write(
        [kit.cand("FIL-p", "Q1", "150", extractor="ocr", confidence=0.3, filed_on="2026-09-01")]
    )
    assert derive_isin(ISIN, taxonomy=real_taxonomy).inserted == 0
    # at or above the table confidence floor the PDF/OCR fact is a usable input
    kit.add_filing("FIL-p2", fmt="pdf", filed_on="2026-09-02")
    kit.write(
        [kit.cand("FIL-p2", "Q1", "150", extractor="ocr", confidence=0.9, filed_on="2026-09-02")]
    )
    assert derive_isin(ISIN, taxonomy=real_taxonomy).inserted == 1
    q2 = cur(kit, "Q2")
    assert q2.derivation["all_xbrl"] is False and set(q2.derivation["input_classes"]) == {
        "ocr",
        "xbrl",
    }


def test_derived_facts_are_never_inputs(kit, real_taxonomy) -> None:
    kit.add_filing("FIL-1")
    kit.write([kit.cand("FIL-1", "FY", "1000"), kit.cand("FIL-1", "H1", "400")])
    derive_isin(ISIN, taxonomy=real_taxonomy)  # H2 = 600
    # Q4 = H2 - Q3 would need the derived H2 and a Q3; with Q3 present it still must not derive
    kit.write([kit.cand("FIL-1", "Q3", "250")])
    derive_isin(ISIN, taxonomy=real_taxonomy)
    assert cur(kit, "Q4") is None


def test_can_derive_rule_on_the_real_taxonomy(real_taxonomy) -> None:
    c = real_taxonomy.concepts
    assert c["revenue"].can_derive and c["cfo"].can_derive
    assert not c["eps_basic"].can_derive and not c["eps_diluted"].can_derive
    assert not c["bank_cet1_ratio"].can_derive and not c["total_assets"].can_derive
    assert all(not c[n].can_derive for n in c if c[n].unit in ("INR_PER_SHARE", "RATIO"))


def test_filename_timestamp() -> None:
    name = "INTEGRATED_FILING_INDAS_1668217_16052026103806_WEB.xml"
    assert filename_timestamp(name).isoformat() == "2026-05-16T10:38:06"
    assert filename_timestamp("INE0__Q4FY26__standalone.xml") is None
    assert filename_timestamp("x_31022026103806_y.xml") is None  # not a real date


def test_facts_cli(kit) -> None:
    kit.add_filing("FIL-1", filed_on="2026-05-01")
    kit.add_filing("FIL-2", filed_on="2026-06-01")
    with transaction() as conn:
        conn.execute(sa.update(tables.entities).values(nse_symbol="XCO"))
    kit.write([kit.cand("FIL-1", "Q1", "100")])
    kit.write([kit.cand("FIL-2", "Q1", "200", filed_on="2026-06-01")])
    shown = runner.invoke(app, ["facts", "show", "--isin", "XCO", "--concept", "revenue"])
    assert shown.exit_code == 0 and "200" in shown.output and "FIL-2" in shown.output
    listed = runner.invoke(app, ["facts", "restatements", "--isin", ISIN])
    assert listed.exit_code == 0 and "100 -> 200" in listed.output
