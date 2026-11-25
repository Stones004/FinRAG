"""Streamlit entry point: `finrag ui` runs this file."""

from __future__ import annotations

import streamlit as st

from finrag.app import runtime
from finrag.app.pages import ask, status

PAGES = {"Ask": ask.render, "Status": status.render}


def main() -> None:
    """Page setup, sidebar navigation, the chosen page."""
    st.set_page_config(page_title=runtime.app_settings().ui.page_title)
    choice = st.sidebar.radio("Page", list(PAGES))
    PAGES[choice]()


main()
