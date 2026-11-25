"""Status page: per company, how far the data reaches, and the open review items by type."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from finrag.app import data, runtime


def render() -> None:
    """Draw the page."""
    st.header("Status")
    view = data.status_view(runtime.get_app_engine())
    st.subheader("Companies")
    if view.companies:
        st.dataframe(
            pd.DataFrame(
                {
                    "Company": [c.name for c in view.companies],
                    "ISIN": [c.isin for c in view.companies],
                    "Status": [c.status for c in view.companies],
                    "Latest period with data": [c.latest_period_end for c in view.companies],
                    "Latest transcript": [c.latest_transcript_period_end for c in view.companies],
                }
            ),
            hide_index=True,
        )
    else:
        st.write("No companies on the watch list.")
    st.subheader("Open review items")
    if view.review_counts:
        st.dataframe(
            pd.DataFrame(
                {"Type": list(view.review_counts), "Open": list(view.review_counts.values())}
            ),
            hide_index=True,
        )
    else:
        st.write("None open.")
