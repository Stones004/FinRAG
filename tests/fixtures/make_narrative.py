"""Synthetic narrative inputs for M08 (a fictional company; no real filing text).

* `results_pages()` - pages.jsonl-style records of a results PDF with a covering letter, statement
  pages that carry notes at the bottom, numbered notes that continue after a statement page (the
  first pages are standalone, the auditor's report consolidated), an auditor's report that ends
  with a UDIN, an annexure, and a press release with a "Note:" footnote.
* `transcript_pages()` - page records of an earnings-call transcript with per-line bold flags: cover
  letter, title, participant block, repeated page header, prepared remarks, Q&A (moderator intros,
  an intro read by the IR adviser, a lost analyst label, a re-introduced analyst), closing remarks.
* `text_facts()` - text_facts.jsonl records: the four indexed text blocks plus ignored facts.

The layout is a test fixture only; it is not evidence of any real document's format.
Run as a script to print a summary: `python tests/fixtures/make_narrative.py`.
"""

from __future__ import annotations

from typing import Any

NOTES_TEXT = (
    "1 The above results were reviewed by the Audit Committee and approved by the Board of "
    "Directors at their meeting held on 5 May 2026. "
    "5 With effect from 1 April 2025 the Company changed its method of valuing inventories from "
    "first-in-first-out to the weighted average cost method. The change in inventory valuation "
    "method has been applied retrospectively and comparative figures have been restated."
)


def _page(number: int, lines: list[str], quality: float = 0.95) -> dict[str, Any]:
    """A pages.jsonl-style record for a results PDF page."""
    text = "\n".join(lines)
    return {
        "page": number,
        "text": text,
        "method": "native",
        "ocr_backend": None,
        "char_count": len(text),
        "lines": None,
        "quality": quality,
        "low_quality": False,
    }


def _pnl_lines() -> list[str]:
    return [
        "ACME WIDGETS LIMITED",
        "STATEMENT OF STANDALONE FINANCIAL RESULTS FOR THE QUARTER ENDED MARCH 31, 2026",
        "Revenue from operations 7,737.02 8,049.25",
        "Total income 7,839.37 8,115.60",
        "Profit before tax 948.22 980.66",
        "Earnings per share (basic) 4.31 4.50",
    ]


def results_pages() -> list[dict[str, Any]]:
    """Fourteen pages; see the module docstring. Page numbers are the keys the tests assert on."""
    return [
        _page(
            1,
            [
                "To,",
                "The Manager",
                "Listing Department",
                "Dear Sir/Madam,",
                "Outcome of the Board meeting",
            ],
        ),
        _page(2, [*_pnl_lines(), "Notes:", NOTES_TEXT[:120], NOTES_TEXT[120:240]]),
        _page(
            3,
            [
                "2 The figures for the quarter are balancing figures between audited figures.",
                "3 Segment reporting is given in note 4 of the results.",
            ],
        ),
        _page(
            4,
            [
                "ACME WIDGETS LIMITED",
                "STATEMENT OF CASH FLOWS",
                "Net cash generated from operating activities 1,714.92",
                "Net cash used in investing activities (1,200.10)",
                "Cash flows from financing activities (141.63)",
            ],
        ),
        _page(
            5,
            [
                "5 From 1 April 2025 the Company changed its inventory valuation method.",
                "6 Previous period figures have been regrouped wherever necessary.",
            ],
        ),
        _page(
            6,
            [
                "Independent Auditor's Report on Audited Consolidated Financial Results for the",
                "quarter and year ended March 31, 2026",
                "Opinion We have audited the financial results of Acme Widgets Limited.",
                "Basis for Opinion We conducted our audit under the Standards on Auditing.",
            ],
        ),
        _page(
            7,
            [
                "Auditor's Responsibilities for the Audit of the Financial Results",
                "We communicate with those charged with governance.",
            ],
        ),
        _page(
            8,
            [
                "For ABC & Associates, Chartered Accountants",
                "UDIN: 26012345ABCDEF1234",
                "Place: Faridabad",
            ],
        ),
        _page(
            9,
            [
                "Annexure 1",
                "Declaration under Regulation 33",
                "We declare that the audit report is unmodified.",
            ],
        ),
        _page(
            10,
            [
                "NEWS RELEASE Acme Widgets Ltd.",
                "Acme Widgets reports strong quarter.",
                "Revenue grew 40 percent.",
            ],
        ),
        _page(11, ["Note:", "Figures are rounded off to the nearest crore."]),
        _page(
            12,
            ["11 This numbered line follows the press release and is not a note to the results."],
        ),
    ]


def _line(text: str, bold: bool = False) -> dict[str, Any]:
    return {"text": text, "bold": bold, "font": "Arial-Bold" if bold else "Arial", "size": 10.0}


def _tpage(number: int, lines: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(line["text"] for line in lines)
    return {
        "page": number,
        "text": text,
        "method": "native",
        "ocr_backend": None,
        "char_count": len(text),
        "lines": lines,
        "quality": 0.95,
        "low_quality": False,
    }


def _header(page_no: int, total: int) -> list[dict[str, Any]]:
    return [
        _line("Acme Widgets Limited"),
        _line("May 04, 2026"),
        _line(f"Page {page_no} of {total}"),
    ]


def _label(name: str, text: str = "") -> dict[str, Any]:
    return _line(f"{name}: {text}".rstrip() if text else f"{name}:", bold=True)


def transcript_pages() -> list[dict[str, Any]]:
    """Five pages of a synthetic call (page 1 is the exchange cover letter)."""
    q = "Our margin was flat. What drove the gross margin movement and what is next year?"
    return [
        _tpage(
            1,
            [
                _line("Acme Widgets Limited", True),
                _line("Sub: Transcript of the earnings call", True),
                _line("Scrip Code: 500001"),
                _line("Dear Sir/Madam,"),
                _line("The transcript of the call is enclosed."),
            ],
        ),
        _tpage(
            2,
            [
                _line("Page 1 of 4"),
                _line("“Acme Widgets Limited"),
                _line("Q4 FY '26 Earnings Conference Call”"),
                _line("May 04, 2026"),
                _line("MANAGEMENT: MR. RAJ SHARMA – CHAIRMAN AND MANAGING", True),
                _line("DIRECTOR – ACME WIDGETS LIMITED", True),
                _line("MR. ASHA RAO – CHIEF FINANCIAL OFFICER – ACME WIDGETS", True),
                _line("LIMITED", True),
                _line("MR. VIKRAM SETH – HEAD IR – SETH ADVISORS – ACME", True),
                _line("MODERATOR:", True),
                _line("MS. NINA PATEL – EXAMPLE SECURITIES", True),
            ],
        ),
        _tpage(
            3,
            [
                *_header(2, 4),
                _label("Moderator"),
                _line(
                    "Ladies and gentlemen, good day and welcome to the Acme Widgets Q4 FY '26 call."
                ),
                _line("I now hand the conference over to Ms. Nina Patel. Over to you."),
                _label("Nina Patel", "Thank you. Welcome everyone. Over to Mr. Sharma."),
                _label("Raj Sharma"),
                _line("Revenue for the quarter was INR 7,737 million, up 86 percent year on year."),
                _line("We expect the order book to convert over the next three quarters."),
                _label("Asha Rao"),
                _line(
                    "Gross margin was 21.5 percent in the quarter against 22.1 percent last year,"
                ),
                _line("because of the mix of strategic orders. Thank you. Over to the moderator."),
                _label("Moderator"),
                _line("Thank you. We will now begin the question-and-answer session. The first"),
                _line(
                    "question is from the line of Ravi Menon from Alpha Capital. Please go ahead."
                ),
                _label("Ravi Menon"),
                _line(q),
            ],
        ),
        _tpage(
            4,
            [
                *_header(3, 4),
                _label("Asha Rao"),
                _line("Gross margin fell because of mix. We expect it to recover by two points"),
                _line("as the strategic orders complete and the base business grows faster."),
                _label("Ravi Menon"),
                _line("Thank you. On the order book, how much is strategic?"),
                _label("Raj Sharma"),
                _line("About 1,600 crores of the order book is strategic."),
                _label("Moderator"),
                _line(
                    "The next question is from the line of Chi Ho Wong, Pictet Asset Management."
                ),
                _label("Chi Ho Wong"),
                _line("Is there a capex plan for next year and how will it be funded?"),
                _label("Vikram Seth"),
                _line(
                    "No major capex is planned. The next question is from the line of Meera Iyer"
                ),
                _line(
                    "from Beta Securities. Please go ahead. Our capex will be routine and funded"
                ),
                _line(
                    "from internal accruals, as it has always been, and we do not plan to borrow."
                ),
                _label("Raj Sharma"),
                _line("Routine capex only."),
            ],
        ),
        _tpage(
            5,
            [
                *_header(4, 4),
                _label("Moderator"),
                _line("The next question is from the line of Pranav from Gamma Funds. Go ahead."),
                _label("Pranav"),
                _line("What is the tax rate going forward?"),
                _label("Asha Rao"),
                _line("We expect an effective tax rate of 25 percent."),
                _label("Moderator"),
                _line("The next question is from the line of Ravi Menon from Alpha Capital."),
                _label("Ravi Menon"),
                _line("One last point on dividends."),
                _label("Raj Sharma"),
                _line("The Board has recommended a dividend of INR 3 per share."),
                _label("Moderator"),
                _line(
                    "Ladies and gentlemen, that was the last question. I now hand the conference"
                ),
                _line("over to the management for the closing comments."),
                _label("Raj Sharma"),
                _line("Thank you all. We expect to guide for 35 percent growth next year."),
                _label("Moderator"),
                _line(
                    "Thank you. On behalf of Example Securities, that concludes this conference."
                ),
            ],
        ),
    ]


def text_facts() -> list[dict[str, Any]]:
    """text_facts.jsonl records: four indexed blocks, and facts that must be ignored."""

    def fact(concept: str, value: str, dims: list[Any] | None = None) -> dict[str, Any]:
        return {
            "concept": f"in-capmkt:{concept}",
            "context": "OneD",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "dimensions": dims or [],
            "value": value,
        }

    return [
        fact("ISIN", "INE111A01010"),
        fact("DateOfEndOfReportingPeriod", "2026-03-31"),
        fact(
            "DisclosureOfNotesOnFinancialResultsExplanatoryTextBlock",
            "1. The results were approved by the Board. <BR> \n"
            "2. The Company changed its inventory valuation method with effect from 1 April 2025 "
            "and comparatives have been restated.",
        ),
        fact(
            "DisclosureOfNotesOnSegmentsExplanatoryTextBlock",
            "The Company operates in a single segment, computing systems.",
        ),
        fact(
            "DisclosureOfNotesOnAssetsAndLiabilitiesExplanatoryTextBlock",
            "The statement of assets and liabilities is given in the results.",
        ),
        fact(
            "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification",
            "Unmodified opinion",
        ),
        fact("WhetherTheCompanyHasAnyRelatedParty", "Yes"),
        fact(
            "DisclosureOfNotesOnSegmentsExplanatoryTextBlock",
            "A dimensioned segment note that must be ignored.",
            [{"axis": "ReportableSegmentsAxis", "member": "ReportableSegments1Member"}],
        ),
        fact("NameOfCounterParty", "Some Related Party Pvt Ltd", [{"axis": "a", "member": "m"}]),
    ]


if __name__ == "__main__":
    print(len(results_pages()), "results pages;", len(transcript_pages()), "transcript pages;")
    print(len(text_facts()), "text facts")
