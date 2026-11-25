"""Objects built once per app process (st.cache_resource): database, embedding model, service.

Without the cache, every question would reload the embedding model's weights.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from finrag.core.config import Settings, get_settings
from finrag.core.db import get_engine, init_db
from finrag.narrative.embedder import SentenceTransformerEmbedder
from finrag.query.service import QueryService, build_service


@st.cache_resource(show_spinner="Opening the database")
def get_app_engine() -> Any:
    """The database connection, with the schema created or upgraded."""
    init_db()
    return get_engine()


@st.cache_resource(show_spinner="Loading the embedding model")
def get_embedder() -> SentenceTransformerEmbedder:
    """The embedding model, loaded by a first embedding."""
    settings = get_settings()
    embedder = SentenceTransformerEmbedder(settings.narrative)
    embedder.embed_query(settings.ui.embedder_warmup_text)
    return embedder


@st.cache_resource(show_spinner="Starting the query service")
def get_service() -> QueryService:
    """The query service on the cached engine and embedder."""
    return build_service(embedder=get_embedder(), use_configured_llm=True, engine=get_app_engine())


def app_settings() -> Settings:
    """The settings, loaded once per process."""
    return get_settings()
