"""Real-answer checks on the real database (acceptance 14). The LLM is faked; nothing is written.

The database and the narrative indexes are copied into the temp repo, so the real data/ folder is
never touched. Expected figures were read from the stored values (PROGRESS: M05 restatement of
Gabriel's FY26 cash flow, finding 33 Netweb's inventory valuation change, finding 35 Gabriel's
Q1 FY27 associates share, finding 37 HDFC cost to income by basis).
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa

from finrag.core.config import get_settings
from finrag.core.db import init_db
from finrag.llm.fake import FakeLLM
from finrag.narrative.embedder import SentenceTransformerEmbedder
from finrag.query.availability import DbAvailability
from finrag.query.models import Answer
from finrag.query.postcheck import NO_EXPLANATION
from finrag.query.service import build_service

from .conftest import claim, claims_json  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data" / "finrag.db"
TODAY = date(2026, 10, 19)
GABRIEL, HDFC, NETWEB = "INE524A01029", "INE040A01034", "INE0NT901020"


def _has_real_data() -> bool:
    if not DB.exists():
        return False
    engine = sa.create_engine(f"sqlite:///file:{DB.as_posix()}?mode=ro&uri=true")
    try:
        with engine.connect() as conn:
            n = conn.execute(sa.text("select count(*) from metric_values")).scalar()
            q = conn.execute(sa.text("select count(*) from chunks")).scalar()
        return bool(n) and bool(q)
    except sa.exc.OperationalError:
        return False
    finally:
        engine.dispose()


pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(not _has_real_data(), reason="real database not populated"),
]


@pytest.fixture
def real(isolated_repo: Path):
    """The real database and indexes copied under the temp repo; migrated."""
    target = isolated_repo / "data"
    target.mkdir(exist_ok=True)
    shutil.copy(DB, target / "finrag.db")
    shutil.copytree(REPO / "data" / "indexes", target / "indexes")
    init_db()
    return target


def ask(question: str, llm: FakeLLM | None = None, embedder=None) -> Answer:
    service = build_service(llm=llm, availability=DbAvailability(), embedder=embedder)
    answer = service.ask(question, TODAY)
    assert isinstance(answer, Answer), getattr(answer, "message", answer)
    return answer


def test_gabriel_fy26_cfo_shows_the_revision_both_filings_and_no_reason(real) -> None:
    answer = ask("What was Gabriel's CFO in FY26?")
    text = answer.text
    assert answer.interpretation.startswith("Reading your question as: Gabriel India Limited")
    assert "FY26 (Apr 2025–Mar 2026), consolidated: 345.27 crore" in text  # current
    assert "was 443.56 crore (FIL-10e6bb5129cb, filed 2026-05-27)" in text  # earlier
    assert "now 345.27 crore (FIL-d656339b02dc, revision filed 2026-06-29)" in text
    assert "No reason for the restatement is given in the stored filings." in text
    assert "consolidated basis (company default)" in text


def test_hdfc_cost_to_income_defaults_to_standalone_and_offers_consolidated(real) -> None:
    answer = ask("HDFC cost to income FY26")
    assert "standalone basis (preferred for this metric)" in answer.interpretation
    assert "FY26 (Apr 2025–Mar 2026), standalone: 38.00%" in answer.text
    assert "Also on the other basis:" in answer.text and "consolidated: 58.45%" in answer.text


def test_netweb_fy26_vs_fy25_growth_is_flagged_as_a_basis_change(real) -> None:
    answer = ask("Netweb FY26 vs FY25 revenue growth")
    text = answer.text
    assert "standalone" in answer.interpretation
    assert "FY26 (Apr 2025–Mar 2026), standalone: 90.04%" in text
    assert (
        "Basis change: the periods straddle an accounting policy change effective 2025-04-01"
        in text
    )
    assert "(event 1)" in text and "moving weighted average cost" in text and "Note 5" in text
    # FY25 growth needs FY24, which no filing holds: said, not guessed
    assert "FY25 (Apr 2024–Mar 2025), standalone: not computed — Missing input" in text


@pytest.mark.slow
def test_gabriel_q1_fy27_pat_shows_the_associates_share_and_says_the_filings_do_not_explain(
    real, monkeypatch
) -> None:
    embedder = _embedder(monkeypatch)
    llm = FakeLLM(default=claims_json())
    answer = ask("Why did Gabriel's PAT jump in Q1 FY27?", llm, embedder)
    text = answer.text
    assert "108.13 crore" in text  # profit for the period 1,081.26 mn
    assert "65.38 crore" in text  # continuing operations 653.83 mn
    assert "42.74 crore" in text  # share of associates and JVs 427.43 mn
    assert "39.53%" in text  # associates share of profit
    assert answer.explanation == NO_EXPLANATION
    assert "The filings do not explain this." in text
    # Gabriel files its call as a link only: no transcript, so the results text is what was searched
    assert any("No earnings-call transcript was available" in w for w in answer.warnings)
    assert len(llm.calls) == 1
    prompt = llm.calls[0][1]
    assert "108.13 crore" in prompt and "[N1]" in prompt
    assert "earnings call" not in prompt and "results filing" in prompt
    # the auditor's report is not offered as an explanation of a profit jump
    assert "auditor report" not in prompt and "audit opinion" not in prompt
    assert "Period asked: Q1 FY27 (Apr–Jun 2026)" in prompt
    assert "no speaker" in prompt


# The three live answers of 2026-10-19 (QRY-01M5AZFF..., -AZGF..., -AZH4...), replayed with the
# model's logged sentences. Each was wrong in the way the test names.

GABRIEL_REPLAY = claims_json(
    claim(
        "The consolidated profit for Q1 FY27 increased due to the inclusion of the Group’s "
        "share of net profit after tax from an associate, which contributed Rs. 280.12 million "
        "for the quarter ended June 30, 2026.",
        ["N4"],
        "include the Group’s share of net profit after tax of Rs. 280.12 million and total "
        "comprehensive income of Rs. 280.12 million for the quarter ended June 30, 2026 in "
        "respect of one associate",
    ),
    claim(
        "The profit also includes the impact of business operations from Anchemco India Private "
        "Limited, which contributed Rs. 153.16 million in net profit after tax for the year "
        "ended March 31, 2026.",
        ["N2"],
        "total net profit after tax of Rs. 153.16 million and total comprehensive income of "
        "Rs. 151.68 million for the year ended March 31, 2026",
    ),
)


@pytest.mark.slow
def test_replay_gabriel_pat_jump_has_no_280_153_or_due_to_and_no_auditor_passage(
    real, monkeypatch
) -> None:
    embedder = _embedder(monkeypatch)
    llm = FakeLLM(default=GABRIEL_REPLAY)
    answer = ask("Why did Gabriel's PAT jump in Q1 FY27?", llm, embedder)
    for banned in ("280.12", "153.16", "due to"):
        assert banned not in answer.explanation and banned not in answer.text.split("**Notes**")[0]
    assert answer.explanation == NO_EXPLANATION
    prompt = llm.calls[0][1]
    assert "auditor report" not in prompt and "audit opinion" not in prompt
    assert "280.12 million" not in prompt and "153.16 million" not in prompt
    assert {c.detail["section"] for c in answer.citations if c.kind == "chunk"} <= {
        "results_notes",
        "segment_notes",
        "press_release",
    }
    assert "108.13 crore" in answer.text  # the figures block is unchanged


@pytest.mark.slow
def test_replay_hdfc_management_say_searches_management_turns_only(real, monkeypatch) -> None:
    embedder = _embedder(monkeypatch)
    old = [  # the four logged sentences, as claims citing the first passages
        claim(
            "HDFC management stated that the gross NPA ratio for Q1 FY27 was 1.17%.",
            ["F1"],
            "1.17%",
        ),
        claim(
            "They indicated that stage 3 assets have adequate coverage.", ["N2"], "stage 3 assets"
        ),
        claim(
            "The management expressed confidence in the adequacy of reserves.", ["N2"], "reserves"
        ),
        claim(
            "They also noted that margin pressures are influenced by cost of funds.",
            ["N1", "N4"],
            "cost of funds",
        ),
    ]
    llm = FakeLLM(default=claims_json(*old))
    # the period is named: with none, the latest period with data is now Q2 FY27 (results filed,
    # no call yet) and a management question would be answered from results text only
    answer = ask("What did HDFC management say about asset quality in Q1 FY27?", llm, embedder)
    prompt = llm.calls[0][1]
    assert "role analyst" not in prompt and "role management" in prompt
    roles = {c.detail["speaker_role"] for c in answer.citations if c.kind == "chunk"}
    assert roles <= {"management", None}
    # none of the four logged sentences survives; the retrieval was strong, so the two closest
    # sentences of management's turns are shown as related disclosures, under their speaker
    assert len(answer.dropped) == 4 and "stated that" not in answer.explanation
    assert (
        answer.text.count("  > “") == 2 and "- Management (Srinivasan Vaidyanathan)" in answer.text
    )
    assert "The filings do not explain this" not in answer.text
    assert "an analyst" not in prompt.split("Rules:")[0]  # no analyst passage was offered


def test_replay_hdfc_gnpa_latest_is_the_figure_only(real) -> None:
    llm = FakeLLM(default="never")
    answer = ask("HDFC GNPA latest", llm)
    assert llm.calls == []
    assert "Gross NPA ratio, Q2 FY27 (Jul–Sep 2026), standalone: 1.11%" in answer.text  # latest
    assert answer.explanation == "" and "What the filings say" not in answer.text
    assert [c.alias for c in answer.citations] == ["F1"]


_LOADED: list = []


def _embedder(monkeypatch):
    """The real embedding model, loaded once for the module (each load takes memory)."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    if not _LOADED:
        try:
            embedder = SentenceTransformerEmbedder(get_settings().narrative)
            embedder.embed_query("warm up")
        except Exception as exc:  # model not in the local cache
            pytest.skip(f"embedding model not available: {exc}")
        _LOADED.append(embedder)
    return _LOADED[0]


@pytest.mark.slow
def test_netweb_capex_plan_fy28_searches_the_latest_calls_and_labels_the_numbers(
    real, monkeypatch
) -> None:
    embedder = _embedder(monkeypatch)
    llm = FakeLLM(default=claims_json())
    answer = ask("What is Netweb's capex plan for FY28?", llm, embedder)
    assert "management guidance and outlook" in answer.interpretation
    assert "which is not filed yet" in answer.interpretation
    assert "they are not management guidance for FY28 (Apr 2027–Mar 2028)" in answer.text
    assert "(actual)" in answer.numbers_block
    prompt = llm.calls[0][1]
    import re

    quarters = set(re.findall(r"period (Q\d FY\d\d)", prompt))
    assert quarters and quarters <= {"Q4 FY26", "Q1 FY27"}  # the latest calls, not FY28's quarters
    assert "role analyst" not in prompt  # guidance is management's, not an analyst's question
    assert "Management guidance is not in the indexed filings." not in answer.text
    assert answer.text.count("  > “") == 2  # related disclosures, from management turns only


def test_gabriel_q1_fy27_profit_growth_shows_the_original_and_the_restated_base(real) -> None:
    """74.48% is against the Q1 FY26 profit first filed (619.72 mn); the Q1 FY27 results print it
    after the Scheme of Arrangement as 1,054.53 mn (page 7), on which the growth is 2.53%."""
    answer = ask("Gabriel PAT growth year on year Q1 FY27 consolidated")
    text = answer.text
    assert "[F1] Profit growth (year on year), Q1 FY27 (Apr–Jun 2026), consolidated: 74.48%" in text
    assert "Not like for like: the earlier period was restated (a comparative restatement" in text
    assert "effective 2026-04-01, event 2 — Composite Scheme of Arrangement" in text
    assert "Originally filed: Profit for the period, Q1 FY26 61.97 crore [F4]" in text
    assert (
        "Against the restated comparative: Profit for the period, Q1 FY26 105.45 crore [F5] "
        "(printed in FIL-83a2f50f0a6d, p. 7) → Profit growth (year on year) 2.53%" in text
    )
    assert (
        "Also on the other basis: [F3] Profit growth (year on year), Q1 FY27 (Apr–Jun 2026)" in text
    )
    assert "Originally filed: Profit for the period, Q1 FY26 55.67 crore [F6]" in text
    assert "(printed in FIL-83a2f50f0a6d, p. 4) → Profit growth (year on year) 27.44%" in text
    # every source line is the record of the figure shown, with its own basis
    cited = {c.alias: c for c in answer.citations}
    assert (
        cited["F1"].detail["basis"] == "consolidated"
        and cited["F3"].detail["basis"] == "standalone"
    )
    assert (
        cited["F5"].detail["basis"] == "consolidated"
        and cited["F7"].detail["basis"] == "standalone"
    )
    assert cited["F5"].detail["comparative_in"] == "2026-06-30"
    assert cited["F4"].detail["comparative_in"] is None  # the original is not a comparative


def test_gabriel_growth_on_the_standalone_basis_leads_with_standalone_and_cites_it(real) -> None:
    answer = ask("Gabriel PAT growth year on year Q1 FY27 standalone")
    cited = {c.alias: c for c in answer.citations}
    assert (
        "[F1] Profit growth (year on year), Q1 FY27 (Apr–Jun 2026), standalone: 36.46%"
        in answer.text
    )
    assert [cited[a].detail["basis"] for a in ("F1", "F3")] == ["standalone", "consolidated"]
    assert (
        f"- [F1] {cited['F1'].id} (metric pat_growth_yoy, period ending 2026-06-30, standalone)"
        in answer.text
    )
    assert (
        f"- [F3] {cited['F3'].id} (metric pat_growth_yoy, period ending 2026-06-30, consolidated)"
        in answer.text
    )


@pytest.mark.slow
def test_hdfc_management_question_with_no_period_takes_the_latest_call(real, monkeypatch) -> None:
    """Results are filed up to Q2 FY27, the latest call is Q1 FY27: management said it in Q1."""
    embedder = _embedder(monkeypatch)
    llm = FakeLLM(default=claims_json())
    answer = ask("What did HDFC management say about asset quality?", llm, embedder)
    assert "Q1 FY27 (Apr–Jun 2026)" in answer.interpretation
    assert "the latest period with an earnings-call transcript is used" in answer.interpretation
    assert "Period asked: Q1 FY27 (Apr–Jun 2026)" in llm.calls[0][1]
    quarters = {c.detail["period_label"] for c in answer.citations if c.kind == "chunk"}
    assert quarters <= {"Q1 FY27"}
