"""Post-check of the model's claims, and the mapping of aliases back to real IDs.

The checks themselves are in `claims`. A claim that fails any of them is dropped and recorded; what
survives is shown with its quote. Nothing the model writes reaches the answer unchecked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from finrag.core.config import AnswerSettings
from finrag.narrative.search import SearchHit
from finrag.query.claims import Claim, ClaimRules, check_claim
from finrag.query.models import Citation, Evidence
from finrag.query.numbers_block import NumbersBlock
from finrag.query.selection import (
    LeadInContext,
    Picked,
    Selection,
    lead_in_problem,
    sentence_problem,
)
from finrag.query.sentences import Sentence

NO_EXPLANATION = "The filings do not explain this."
NO_CHUNKS = NO_EXPLANATION  # nothing usable was retrieved: the same sentence, with a note saying so
NO_GUIDANCE = "Management guidance is not in the indexed filings."

_CITE_GROUP = re.compile(r"\[([^\]]*)\]")


@dataclass
class PostcheckResult:
    """Claims kept and dropped (with reasons), and the aliases the kept ones cite."""

    kept: list[Claim] = field(default_factory=list)  # claims of the earlier prompt shape (v2)
    picked: list[Picked] = field(default_factory=list)  # selected sentences that passed
    dropped: list[dict[str, str]] = field(default_factory=list)
    lead_ins_dropped: list[dict[str, str]] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)

    @property
    def survived(self) -> bool:
        """True if anything the model chose reached the answer."""
        return bool(self.kept or self.picked)

    @property
    def explanation(self) -> str:
        """The surviving claims as sentences, each with its citations."""
        if self.picked:
            return " ".join(
                f"{p.lead_in or p.sentence.text} [{p.sentence.alias}]" for p in self.picked
            )
        return " ".join(c.sentence for c in self.kept)


def is_refusal(text: str) -> bool:
    """True for the standard 'the filings do not explain this' sentences (they need no quote)."""
    bare = _CITE_GROUP.sub("", text).strip().rstrip(".").casefold()
    return bare in (NO_EXPLANATION.rstrip(".").casefold(), NO_GUIDANCE.rstrip(".").casefold())


def postcheck(
    claims: list[Claim],
    evidence: Evidence,
    block: NumbersBlock,
    settings: AnswerSettings,
    rules: ClaimRules,
) -> PostcheckResult:
    """The model's claims -> the ones that pass every check."""
    result = PostcheckResult()
    by_id = {h.chunk_id: h for h in evidence.chunks}
    passages = {alias: by_id[cid] for alias, cid in evidence.chunk_aliases.items() if cid in by_id}
    figure_aliases = set(evidence.fact_aliases) | set(evidence.metric_aliases)
    figures = {alias: block.allowed.get(alias, set()) for alias in figure_aliases}
    rules = replace(
        rules,
        require_citation=settings.require_citation_per_sentence,
        check_numbers=settings.numeric_check_enabled,
        min_quote_chars=settings.min_quote_chars,
    )
    for claim in claims:
        if is_refusal(claim.bare_text):
            continue  # the fallback sentence is added when nothing else survives
        reason = check_claim(claim, passages, figures, rules)
        if reason is not None:
            result.dropped.append({"sentence": claim.bare_text, "reason": reason})
            continue
        result.kept.append(claim)
        result.cited += [a for a in claim.cites if a not in result.cited]
    return result


def postcheck_selections(
    picks: list[Selection],
    sentences: dict[str, Sentence],
    block: NumbersBlock,
    rules: ClaimRules,
    context: LeadInContext,
    lead_ins: bool = True,
) -> PostcheckResult:
    """The model's picks -> the sentences that pass, each with the lead-in that passed.

    With `lead_ins` off a lead-in the model wrote anyway is ignored (not checked, not shown).
    """
    result = PostcheckResult()
    seen: set[str] = set()
    for pick in picks:
        reason = sentence_problem(pick, sentences, seen, rules)
        if reason is not None:
            result.dropped.append({"sentence": pick.id, "reason": reason})
            continue
        seen.add(pick.id)
        sentence = sentences[pick.id]
        lead_in = pick.lead_in if lead_ins else ""
        if lead_in:
            problem = lead_in_problem(lead_in, sentence, rules, context)
            if problem is not None:
                result.lead_ins_dropped.append(
                    {"sentence": pick.id, "lead_in": lead_in, "reason": problem}
                )
                lead_in = ""
        result.picked.append(Picked(sentence, lead_in))
        if sentence.alias not in result.cited:
            result.cited.append(sentence.alias)
    return result


def chunk_header(hit: SearchHit) -> str:
    """A passage -> where it comes from, as shown next to a citation."""
    when = hit.period_label or ""
    if hit.source_type == "transcript":
        who = hit.speaker or "speaker not identified"
        role = hit.speaker_role or "role not identified"
        firm = f", {hit.firm}" if hit.firm else ""
        return f"earnings call {when}: {who} ({role}{firm})"
    section = (hit.section or "").replace("_", " ")
    where = f"p. {hit.page}" if hit.page is not None else "XBRL text block"
    return f"results {section}, {when}, {where}"


def build_citations(evidence: Evidence, aliases: list[str]) -> list[Citation]:
    """Aliases -> Citation objects carrying the real IDs and what a reader needs to find them."""
    metric_by_id = {m.metric_value_id: m for m in evidence.metric_values}
    fact_by_id = {f.fact_id: f for f in evidence.facts}
    chunk_by_id = {h.chunk_id: h for h in evidence.chunks}
    out: list[Citation] = []
    for alias in aliases:
        if alias in evidence.metric_aliases:
            mv = metric_by_id[evidence.metric_aliases[alias]]
            out.append(
                Citation(
                    alias,
                    "metric",
                    mv.metric_value_id,
                    {
                        "metric_id": mv.metric_id,
                        "period_end": mv.period.end.isoformat(),
                        "period_type": mv.period.type.value,
                        "basis": mv.basis_used,
                        "formula_version": mv.formula_version,
                        "input_fact_ids": list(mv.input_fact_ids),
                    },
                )
            )
        elif alias in evidence.fact_aliases:
            fact = fact_by_id[evidence.fact_aliases[alias]]
            out.append(
                Citation(
                    alias,
                    "fact",
                    fact.fact_id,
                    {
                        "concept": fact.concept,
                        "period_end": fact.period.end.isoformat(),
                        "basis": fact.basis,
                        "filing_id": fact.source_filing_id,
                        "filed_on": fact.filed_on,
                        "comparative_in": (
                            fact.filing_period_end if fact.is_pdf_comparative else None
                        ),
                    },
                )
            )
        elif alias in evidence.chunk_aliases:
            hit = chunk_by_id[evidence.chunk_aliases[alias]]
            out.append(
                Citation(
                    alias,
                    "chunk",
                    hit.chunk_id,
                    {
                        "source_type": hit.source_type,
                        "period_label": hit.period_label,
                        "section": hit.section,
                        "filing_id": hit.filing_id,
                        "page": hit.page,
                        "speaker": hit.speaker,
                        "speaker_role": hit.speaker_role,
                        "firm": hit.firm,
                        "header": chunk_header(hit),
                    },
                )
            )
    return out
