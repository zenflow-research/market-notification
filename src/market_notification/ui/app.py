"""Streamlit UI app entry. Phase 12 builds this out.

Phase 0: minimal placeholder so `streamlit run` doesn't 404.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Market Notification", layout="wide")
st.title("Market Notification System")
st.warning("Phase 0 placeholder. Real UI arrives in Phase 12. See `docs/PLAN.md`.")
st.markdown(
    "Reading order for orientation:\n"
    "1. `docs/CONTEXT.md`\n"
    "2. `docs/STATUS.md`\n"
    "3. `docs/PLAN.md`"
)
