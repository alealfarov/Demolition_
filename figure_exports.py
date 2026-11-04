"""Standalone Plotly HTML download directly beneath each chart."""
import hashlib
import re
import plotly.io as pio
import streamlit as st

def download_chart_html(figure, chart_number):
    title = str(getattr(figure.layout.title, "text", None) or "BAG demolition chart")
    slug = re.sub(r"[^a-z0-9]+", "_", re.sub(r"<[^>]*>", "", title).lower()).strip("_")[:70] or "chart"
    digest = hashlib.sha256(pio.to_json(figure).encode("utf-8")).hexdigest()[:10]
    st.download_button(
        "Download interactive HTML",
        data=pio.to_html(figure, full_html=True, include_plotlyjs=True,
                         config={"displaylogo": False, "responsive": True}),
        file_name=f"{slug}_{digest}.html",
        mime="text/html",
        key=f"interactive_html_{chart_number}_{digest}",
        help="Standalone interactive Plotly figure; works offline and preserves current filters.",
    )
