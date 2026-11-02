import io
import json
import math
import os
import re
import sqlite3
import sys
import warnings
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pydeck as pdk
import requests
import streamlit as st
import streamlit.components.v1 as components
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError


# Miniforge on Windows can install PROJ's database in the environment-level
# Library directory while pyproj initially looks in its package-local folder.
# Configure the valid database before any local GeoPackage code imports pyproj
# or GeoPandas. Respect an explicitly configured path when it is already valid.
_miniforge_proj_dir = Path(sys.prefix) / "Library" / "share" / "proj"
_configured_proj_dir = Path(os.environ.get("PROJ_DATA", ""))
if not (_configured_proj_dir / "proj.db").is_file() and (
    _miniforge_proj_dir / "proj.db"
).is_file():
    os.environ["PROJ_DATA"] = str(_miniforge_proj_dir)
    os.environ["PROJ_LIB"] = str(_miniforge_proj_dir)
if (_miniforge_proj_dir / "proj.db").is_file():
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="pyproj unable to set PROJ database path.*",
                category=UserWarning,
                module=r"pyproj\.network",
            )
            import pyproj

        pyproj.datadir.set_data_dir(str(_miniforge_proj_dir))
    except ImportError:
        # The online Cesium viewer can still run without the optional local
        # GeoPackage stack; its UI reports the missing dependency separately.
        pass


st.set_page_config(
    page_title="BAG Demolition Explorer",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Dashboard typography and chart styling are deliberately optimized for both
# interactive use and thesis-quality PNG export. Plotly uses a larger type scale
# than the Streamlit UI because exported figures are usually reduced in size in
# LaTeX/Word and therefore need stronger labels at source resolution.
st.markdown(
    """
    <style>
      html, body, [class*="css"] {
        font-size: 16px;
      }
      .stApp p, .stApp li, .stApp label {
        font-size: 1rem;
        line-height: 1.48;
      }
      .stApp [data-testid="stCaptionContainer"] p {
        font-size: 0.92rem;
        line-height: 1.42;
      }
      .stApp [data-testid="stSidebar"] p,
      .stApp [data-testid="stSidebar"] label {
        font-size: 0.96rem;
      }
      .stApp button, .stApp input, .stApp select, .stApp textarea {
        font-size: 0.98rem;
      }
      .stApp [data-baseweb="tab"] {
        font-size: 0.98rem;
      }
      .stApp [data-testid="stMetricLabel"] {
        font-size: 0.92rem;
      }
      .stApp [data-testid="stMetricValue"] {
        font-size: 1.65rem;
      }
      .stApp h1 { letter-spacing: -0.02em; }
      .stApp h2, .stApp h3 { letter-spacing: -0.01em; }
    </style>
    """,
    unsafe_allow_html=True,
)


# Publication palette: Paul Tol's colour-blind-safe Muted palette. Terracotta is
# the analytical identity colour; categorical colours are reserved for variables
# whose categories have actual semantic meaning.
PRIMARY_COLOR = "#CC6677"
SELECTED_COLOR = "#994455"
CONTEXT_COLOR = "#D5D0CD"
UNKNOWN_COLOR = "#DDDDDD"
DARK_GREY = "#4B4B4B"
MUNICIPALITY_COLORS = [
    "#CC6677", "#332288", "#DDCC77", "#117733", "#88CCEE", "#44AA99",
    "#882255",
]
TERRACOTTA_SCALE = [
    "#FEF0D9", "#FDCC8A", "#FC8D59", "#E34A33", "#B30000",
]
DIVERGING_SCALE = [
    "#8C510A", "#D8B365", "#F6E8C3", "#F5F5F5",
    "#C7EAE5", "#5AB4AC", "#01665E",
]
FUNCTION_COLORS = {
    "woonfunctie": "#CC6677",
    "logiesfunctie": "#332288",
    "kantoorfunctie": "#DDCC77",
    "winkelfunctie": "#117733",
    "industriefunctie": "#88CCEE",
    "onderwijsfunctie": "#44AA99",
    "gezondheidszorgfunctie": "#882255",
    "sportfunctie": "#AA4499",
    "bijeenkomstfunctie": "#999933",
    "celfunctie": "#6699CC",
    "overige gebruiksfunctie": "#D5D0CD",
    "Onbekend": "#DDDDDD",
}
LAG_CATEGORY_ORDER = [
    "≤ 1 year",
    "1–2 years",
    "2–3 years",
    "3–5 years",
    "> 5 years",
    "No replacement found",
]
LAG_CATEGORY_COLORS = {
    "≤ 1 year": "#E9C7B9",
    "1–2 years": "#DDAA96",
    "2–3 years": "#D08C75",
    "3–5 years": "#C06E58",
    "> 5 years": "#9F4E40",
    "No replacement found": UNKNOWN_COLOR,
}


_STREAMLIT_PLOTLY_CHART = st.plotly_chart


def categorical_color_map(values, palette=None):
    """Return a deterministic value-to-colour mapping for categorical charts."""
    palette = list(palette or MUNICIPALITY_COLORS)
    clean_values = sorted(
        {
            str(value)
            for value in values
            if value is not None and not pd.isna(value) and str(value).strip()
        }
    )
    return {
        value: palette[index % len(palette)]
        for index, value in enumerate(clean_values)
    }


def sequential_color_map(values, palette=None):
    """Assign ordered categories to a muted sequential palette."""
    clean_values = [str(value) for value in values]
    if not clean_values:
        return {}
    palette = list(palette or TERRACOTTA_SCALE)
    if len(clean_values) == 1:
        sampled = [palette[len(palette) // 2]]
    else:
        sampled = px.colors.sample_colorscale(
            palette,
            [index / (len(clean_values) - 1) for index in range(len(clean_values))],
        )
    return dict(zip(clean_values, sampled))


def chart_geo_axis_label(geo_level):
    """Human-readable geography label used instead of the internal geo_unit field."""
    return "Municipality" if geo_level == "municipality" else "Wijk"


def style_categorical_xaxis(figure, category_count, title):
    """Keep long Dutch municipality/wijk labels readable on screen and in exports."""
    angle = -32 if int(category_count or 0) > 8 else 0
    bottom_margin = 135 if angle else 85
    figure.update_xaxes(title=title, tickangle=angle)
    figure.update_layout(margin={"l": 75, "r": 35, "t": 85, "b": bottom_margin})
    return figure


def style_municipality_line_traces(figure, municipality_names):
    """Differentiate many municipal lines using colour, dash and marker shape."""
    dash_styles = ["solid", "dash", "dot"]
    marker_symbols = ["circle", "square", "diamond"]
    municipality_order = {name: index for index, name in enumerate(municipality_names)}
    for trace in figure.data:
        index = municipality_order.get(str(trace.name), 0)
        variation = (index // max(1, len(MUNICIPALITY_COLORS))) % len(dash_styles)
        trace.update(
            line={"width": 2.6, "dash": dash_styles[variation]},
            marker={"size": 7, "symbol": marker_symbols[variation]},
            opacity=0.92,
        )
    return figure


def style_plotly_figure(figure):
    """Apply a publication-readable typography system to every Plotly chart."""
    if figure is None:
        return figure

    is_sankey = any(getattr(trace, "type", "") == "sankey" for trace in figure.data)
    base_font_size = 13 if is_sankey else 17

    figure.update_layout(
        template="plotly_white",
        font={"family": "Arial, sans-serif", "size": base_font_size, "color": "#1f2937"},
        title={
            "font": {"family": "Arial, sans-serif", "size": 24, "color": "#1f2937"},
            "x": 0.01,
            "xanchor": "left",
        },
        legend={
            "font": {"size": 15},
            "title": {"font": {"size": 15}},
            "itemsizing": "constant",
        },
        hoverlabel={"font": {"size": 15}},
        margin={"l": 75, "r": 35, "t": 85, "b": 85},
        uniformtext={"minsize": 14, "mode": "hide"},
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    figure.update_xaxes(
        tickfont={"size": 15},
        title_font={"size": 17},
        automargin=True,
        showgrid=False,
        zeroline=False,
        linecolor="rgba(100,116,139,0.45)",
    )
    figure.update_yaxes(
        tickfont={"size": 15},
        title_font={"size": 17},
        automargin=True,
        gridcolor="rgba(148,163,184,0.22)",
        zeroline=False,
        linecolor="rgba(100,116,139,0.45)",
    )
    return figure


def render_plotly_chart(figure, *args, **kwargs):
    """Render a chart with consistent styling and a high-resolution PNG export."""
    figure = style_plotly_figure(figure)
    title_text = "bag_demolition_chart"
    try:
        if figure.layout.title.text:
            title_text = str(figure.layout.title.text)
    except Exception:
        pass
    filename = re.sub(r"[^A-Za-z0-9_-]+", "_", title_text).strip("_").lower()
    filename = filename[:90] or "bag_demolition_chart"

    default_config = {
        "displaylogo": False,
        "responsive": True,
        "toImageButtonOptions": {
            "format": "png",
            "filename": filename,
            "width": 1800,
            "height": 1000,
            "scale": 2,
        },
    }
    supplied_config = kwargs.pop("config", None) or {}
    merged_config = {**default_config, **supplied_config}
    if "toImageButtonOptions" in supplied_config:
        merged_config["toImageButtonOptions"] = {
            **default_config["toImageButtonOptions"],
            **supplied_config["toImageButtonOptions"],
        }

    return _STREAMLIT_PLOTLY_CHART(
        figure,
        *args,
        config=merged_config,
        **kwargs,
    )


st.title("BAG Demolition Explorer · Fryslân")
st.caption(
    "Fryslân 2011–2025 · historical municipality-at-event analysis, "
    "present-day municipal comparison, current wijk/buurt indicator maps, "
    "and specialized spatial inspection tools"
)

# ============================================================
# Configuration and database connection
# ============================================================

db = st.secrets["postgres"]

engine = create_engine(
    f"postgresql+psycopg2://{db['user']}:{db['password']}"
    f"@{db['host']}:{db['port']}/{db['database']}",
    pool_pre_ping=True,
    connect_args={"options": "-c max_parallel_workers_per_gather=0"},
)

# TEMPORARY DATABASE CONNECTION TEST
with engine.connect() as conn:
    result = conn.execute(
        text("""
            SELECT
                current_database(),
                current_user,
                inet_server_port(),
                to_regclass(
                    'thesis_ch4.vw_demolition_analysis_vbo_eligible_2011_2025'
                )
        """)
    ).fetchone()

st.write("Connected database:", result[0])
st.write("Connected user:", result[1])
st.write("PostgreSQL server port:", result[2])
st.write("Required analysis view:", result[3])

BAG_VIEWER_BASE = "https://bagviewer.kadaster.nl/"
BAG_API_BASE = (
    "https://api.bag.kadaster.nl/lvbag/individuelebevragingen/v2"
)

# Harmonised annual municipality boundaries created for this thesis.
# The layer is used only as an optional visual inspection overlay; it does not
# replace the fixed AOI or the current wijk/buurt geometries used by the
# demolition indicators.
HISTORICAL_BOUNDARY_TABLE = (
    "postgres.gemeentegrenzen_fryslan_2011_2026"
)

# Official 3DBAG release used by the thesis prototype. Pinning the release
# makes the visualization reproducible instead of following a moving /latest URL.
CESIUM_VERSION = "1.133"
THREEDBAG_RELEASE = "v20250903"
THREEDBAG_LOCAL_GPKG_DIR = Path(__file__).resolve().parent / "3D BAG" / "raw" / "gpkg"
# The three heritage layer definitions are shipped beside this app.  Resolving
# them relative to the source file keeps the viewer portable and avoids silently
# loading the unrelated province-layer definitions from datasets_mon.
EDUGIS_HERITAGE_LAYER_DIR = Path(__file__).resolve().parent

# 3D contextual overlays. Administrative boundaries deliberately use one neon
# yellow hue with different line widths so they remain legible against both the
# muted basemap and the coloured 3DBAG buildings.
ADMIN_BOUNDARY_COLOR = "#F4FF00"
LEGAL_MONUMENT_COLORS = {
    "EWE": "#d53e4f",  # Rijksmonument
    "GG": "#218B45",   # Gemeentelijk monument
    "MP": "#673AB7",   # Provinciaal monument
    "EWD": "#FFC107",  # Voorbeschermd rijksmonument
}
LEGAL_MONUMENT_LABELS = {
    "EWE": "Wkpb restriction · Rijksmonument",
    "GG": "Wkpb restriction · Gemeentelijk monument",
    "MP": "Wkpb restriction · Provinciaal monument",
    "EWD": "Wkpb restriction · Voorbeschermd rijksmonument",
}
THREEDBAG_TILESETS = {
    "LoD1.2 · fastest": (
        f"https://data.3dbag.nl/{THREEDBAG_RELEASE}/"
        "cesium3dtiles/lod12/tileset.json"
    ),
    "LoD1.3 · intermediate": (
        f"https://data.3dbag.nl/{THREEDBAG_RELEASE}/"
        "cesium3dtiles/lod13/tileset.json"
    ),
    "LoD2.2 · detailed roofs": (
        f"https://data.3dbag.nl/{THREEDBAG_RELEASE}/"
        "cesium3dtiles/lod22/tileset.json"
    ),
}

VALIDATION_CATALOG = [
    {
        "issue_type": "INVALID_GEOMETRY",
        "test": "Null, empty or invalid geometry",
        "stage": "Geometry filter",
        "severity": "Excluded",
        "meaning": (
            "The pand cannot be assigned spatially to the current Fryslân municipality geometry. "
            "This count is source-wide because a null geometry has no spatial municipality."
        ),
    },
    {
        "issue_type": "MISSING_DEMOLITION_DATE",
        "test": "Missing demolition date",
        "stage": "Temporal extraction",
        "severity": "Excluded",
        "meaning": (
            "No document date, validity start or registration timestamp was available "
            "to position the demolition in time."
        ),
    },
    {
        "issue_type": "REPEATED_DEMOLITION_STATUS",
        "test": "Repeated 'Pand gesloopt' records",
        "stage": "Status history",
        "severity": "Review",
        "meaning": (
            "The same pand_id has more than one demolition-status record. "
            "This can be an administrative duplicate or a corrected/repeated event."
        ),
    },
    {
        "issue_type": "FIRST_LAST_DEMOLITION_DIFFER",
        "test": "First and last demolition dates differ",
        "stage": "Event selection",
        "severity": "Review",
        "meaning": (
            "Selecting the first rather than the last demolition occurrence would change "
            "the analytical year for this pand."
        ),
    },
    {
        "issue_type": "DEMOLITION_REVERSED",
        "test": "Demolished status followed by non-demolished status",
        "stage": "Status history",
        "severity": "Review",
        "meaning": (
            "A demolition status was later followed by in-use, renovation, permit or another "
            "non-demolished status. The earlier demolition was not terminal."
        ),
    },
    {
        "issue_type": "NON_TERMINAL_DEMOLITION",
        "test": "Latest complete BAG status is not demolished",
        "stage": "Terminal-status rule",
        "severity": "Potential exclusion",
        "meaning": (
            "The latest demolition record exists, but a later full-history record has a "
            "non-demolished status. These records should not enter a terminal-demolition sample."
        ),
    },
    {
        "issue_type": "MISSING_BOUWJAAR",
        "test": "Missing construction year",
        "stage": "Lifespan filter",
        "severity": "Excluded from age analysis",
        "meaning": "A positive building lifespan cannot be calculated without bouwjaar.",
    },
    {
        "issue_type": "INVALID_BOUWJAAR",
        "test": "Invalid construction year",
        "stage": "Construction-cohort filter",
        "severity": "Excluded from age analysis",
        "meaning": (
            "The registered construction year is zero, negative, equal to or later "
            "than the demolition year. The demolition event remains in counts, but "
            "the record is excluded from cohort and lifespan statistics."
        ),
    },
    {
        "issue_type": "NON_POSITIVE_LIFESPAN",
        "test": "Zero or negative lifespan",
        "stage": "Lifespan filter",
        "severity": "Excluded",
        "meaning": (
            "The demolition year is equal to or earlier than the registered construction year. "
            "This is analytically implausible and requires manual inspection."
        ),
    },
    {
        "issue_type": "MISSING_WIJK",
        "test": "No wijk assignment",
        "stage": "Administrative enrichment",
        "severity": "Review",
        "meaning": "The building point was not covered by a wijk geometry.",
    },
    {
        "issue_type": "MISSING_BUURT",
        "test": "No buurt assignment",
        "stage": "Administrative enrichment",
        "severity": "Review",
        "meaning": "The building point was not covered by a buurt geometry.",
    },
]


def bag_viewer_url(pand_id):
    """Create a BAG Viewer search URL for a BAG object identifier."""
    if pand_id is None or str(pand_id).strip() == "":
        return None
    return f"{BAG_VIEWER_BASE}?searchQuery={quote_plus(str(pand_id).strip())}"


def get_bag_api_key():
    """Read an optional BAG API key without making it visible in the browser."""
    try:
        return st.secrets["bag_api"]["api_key"]
    except Exception:
        return None


def relation_exists(qualified_name):
    """Return True when a table/view exists in the connected PostgreSQL database."""
    try:
        check = pd.read_sql(
            text("SELECT to_regclass(:qualified_name) AS relation_name;"),
            engine,
            params={"qualified_name": qualified_name},
        )
        return pd.notna(check.iloc[0]["relation_name"])
    except ProgrammingError:
        return False


@st.cache_data
def get_relation_columns(qualified_name):
    """Return the exposed column names of a PostgreSQL table or view."""
    schema_name, relation_name = qualified_name.split(".", 1)
    columns = pd.read_sql(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema_name
              AND table_name = :relation_name
            ORDER BY ordinal_position;
            """
        ),
        engine,
        params={
            "schema_name": schema_name,
            "relation_name": relation_name,
        },
    )
    return tuple(columns["column_name"].astype(str).tolist())


def municipality_columns_for_relation(qualified_name, geography_mode):
    """Resolve historical/current municipality fields exposed by a helper view."""
    available = set(get_relation_columns(qualified_name))
    historical = geography_mode == "Historical municipality at demolition date"

    if historical and {
        "event_gemeentecode",
        "event_gemeentenaam",
    }.issubset(available):
        return "event_gemeentecode", "event_gemeentenaam"

    if not historical and {
        "current_gemeentecode",
        "current_gemeentenaam",
    }.issubset(available):
        return "current_gemeentecode", "current_gemeentenaam"

    return "gemeentecode", "gemeentenaam"


ANALYSIS_START_YEAR = 2011
ANALYSIS_END_YEAR = 2025

# Prefer the validated all-Fryslân analytical relation. The old case-study
# relation remains a final fallback so the app still starts while the database
# migration is being completed.
DEMOLITION_SOURCE_CANDIDATES = [
    "thesis_ch4.vw_demolition_analysis_vbo_eligible_2011_2025",
]

DEMOLITION_REQUIRED_COLUMNS = {
    "pand_id",
    "bouwjaar",
    "pandstatus",
    "demolition_date",
    "demolition_year",
    "gemeentecode",
    "gemeentenaam",
    "wijkcode",
    "wijknaam",
    "buurtcode",
    "buurtnaam",
    "lifespan_years",
    "valid_lifespan",
    "point_rd",
}


def relation_has_columns(qualified_name, required_columns):
    """Check that a candidate table/view exposes the app's required columns."""
    available = set(get_relation_columns(qualified_name))
    return set(required_columns).issubset(available)


def resolve_demolition_records_table():
    """Resolve the broadest compatible demolition dataset available."""
    for qualified_name in DEMOLITION_SOURCE_CANDIDATES:
        try:
            if relation_exists(qualified_name) and relation_has_columns(
                qualified_name,
                DEMOLITION_REQUIRED_COLUMNS,
            ):
                return qualified_name
        except Exception:
            continue
    raise RuntimeError(
        "No compatible demolition analysis relation was found. Expected one of: "
        + ", ".join(DEMOLITION_SOURCE_CANDIDATES)
    )


DEMOLITION_RECORDS_TABLE = resolve_demolition_records_table()
USING_CASE_STUDY_FALLBACK = (
    DEMOLITION_RECORDS_TABLE
    == "thesis_ch4.case_demolition_records_2011_2025"
)

DEMOLITION_SOURCE_COLUMNS = set(
    get_relation_columns(DEMOLITION_RECORDS_TABLE)
)
HAS_TEMPORAL_MUNICIPALITY_FIELDS = {
    "event_gemeentecode",
    "event_gemeentenaam",
    "current_gemeentecode",
    "current_gemeentenaam",
}.issubset(DEMOLITION_SOURCE_COLUMNS)

CURRENT_GEOGRAPHY_MODE = "Current municipality territory"
HISTORICAL_GEOGRAPHY_MODE = "Historical municipality at demolition date"

GEOGRAPHY_MODE_OPTIONS = [CURRENT_GEOGRAPHY_MODE]
if HAS_TEMPORAL_MUNICIPALITY_FIELDS:
    GEOGRAPHY_MODE_OPTIONS.append(HISTORICAL_GEOGRAPHY_MODE)


def analysis_municipality_columns(geography_mode):
    """Return municipality columns for the selected analytical perspective."""
    if (
        geography_mode == HISTORICAL_GEOGRAPHY_MODE
        and HAS_TEMPORAL_MUNICIPALITY_FIELDS
    ):
        return "event_gemeentecode", "event_gemeentenaam"
    if HAS_TEMPORAL_MUNICIPALITY_FIELDS:
        return "current_gemeentecode", "current_gemeentenaam"
    return "gemeentecode", "gemeentenaam"


# ============================================================
# Data-loading helpers
# ============================================================

@st.cache_data(ttl=300, show_spinner=False)
def load_filter_values(geography_mode):
    years = pd.read_sql(
        f"""
        SELECT
            MIN(demolition_year) AS min_year,
            MAX(demolition_year) AS max_year
        FROM {DEMOLITION_RECORDS_TABLE}
        WHERE demolition_year BETWEEN
              {ANALYSIS_START_YEAR} AND {ANALYSIS_END_YEAR};
        """,
        engine,
    )

    if (
        geography_mode == HISTORICAL_GEOGRAPHY_MODE
        and HAS_TEMPORAL_MUNICIPALITY_FIELDS
    ):
        municipalities = pd.read_sql(
            f"""
            SELECT DISTINCT
                event_gemeentecode::text AS gemeentecode,
                event_gemeentenaam AS gemeentenaam
            FROM {DEMOLITION_RECORDS_TABLE}
            WHERE demolition_year BETWEEN
                  {ANALYSIS_START_YEAR} AND {ANALYSIS_END_YEAR}
              AND event_gemeentenaam IS NOT NULL
            ORDER BY event_gemeentenaam;
            """,
            engine,
        )
    else:
        # Current municipality choices come from current administrative geometry,
        # keeping municipalities with zero selected demolition events visible.
        municipalities = pd.read_sql(
            """
            SELECT DISTINCT
                gemeentecode::text AS gemeentecode,
                gemeentenaam
            FROM dt_frl_base.vw_gemeente_admin
            WHERE gemeentenaam IS NOT NULL
              AND (
                    LOWER(TRIM(COALESCE(provincienaam, '')))
                        IN ('fryslân', 'friesland')
                    OR NOT EXISTS (
                        SELECT 1
                        FROM dt_frl_base.vw_gemeente_admin
                        WHERE LOWER(TRIM(COALESCE(provincienaam, '')))
                              IN ('fryslân', 'friesland')
                    )
                  )
            ORDER BY gemeentenaam;
            """,
            engine,
        )

    return years, municipalities


@st.cache_data
def load_demolition_source_coverage(geography_mode):
    _, municipality_name_column = analysis_municipality_columns(
        geography_mode
    )
    return pd.read_sql(
        f"""
        SELECT
            {municipality_name_column} AS gemeentenaam,
            COUNT(DISTINCT pand_id)::bigint AS demolition_records
        FROM {DEMOLITION_RECORDS_TABLE}
        WHERE demolition_year BETWEEN
              {ANALYSIS_START_YEAR} AND {ANALYSIS_END_YEAR}
          AND {municipality_name_column} IS NOT NULL
        GROUP BY {municipality_name_column}
        ORDER BY {municipality_name_column};
        """,
        engine,
    )


def sanitize_demolition_age_fields(df):
    """Apply the validated final-view lifespan flag used by the thesis analysis.

    Demolition counts retain every event. Age-dependent analyses use only rows
    where ``valid_lifespan`` is true. This keeps the dashboard aligned with the
    validated PostgreSQL relation instead of independently redefining lifespan.
    """
    out = df.copy()
    if out.empty:
        out["invalid_bouwjaar_flag"] = pd.Series(dtype="boolean")
        out["invalid_lifespan_flag"] = pd.Series(dtype="boolean")
        return out

    demolition_year = pd.to_numeric(out.get("demolition_year"), errors="coerce")
    source_bouwjaar = pd.to_numeric(out.get("bouwjaar"), errors="coerce")
    source_lifespan = pd.to_numeric(out.get("lifespan_years"), errors="coerce")

    valid_bouwjaar = (
        source_bouwjaar.notna()
        & demolition_year.notna()
        & source_bouwjaar.ge(1)
        & source_bouwjaar.lt(demolition_year)
        & source_bouwjaar.le(ANALYSIS_END_YEAR)
    )

    if "valid_lifespan" in out.columns:
        valid_lifespan = out["valid_lifespan"].fillna(False).astype(bool)
    else:
        # Compatibility fallback only; the validated final source exposes this flag.
        valid_lifespan = (
            valid_bouwjaar
            & source_lifespan.notna()
            & source_lifespan.gt(0)
        )

    out["invalid_bouwjaar_flag"] = (
        source_bouwjaar.notna() & ~valid_bouwjaar
    ).astype("boolean")
    out["invalid_lifespan_flag"] = (
        source_lifespan.notna() & ~valid_lifespan
    ).astype("boolean")

    out["bouwjaar"] = source_bouwjaar.where(valid_bouwjaar).round().astype("Int64")
    out["lifespan_years"] = source_lifespan.where(valid_lifespan).astype("Float64")
    return out


@st.cache_data
def load_records(
    selected_municipalities,
    start_year,
    end_year,
    geography_mode,
):
    municipality_code_column, municipality_name_column = (
        analysis_municipality_columns(geography_mode)
    )

    if HAS_TEMPORAL_MUNICIPALITY_FIELDS:
        boundary_year_expression = (
            "event_boundary_year"
            if "event_boundary_year" in DEMOLITION_SOURCE_COLUMNS
            else "demolition_year"
        )
        assignment_expression = (
            "event_boundary_assignment_method"
            if "event_boundary_assignment_method"
            in DEMOLITION_SOURCE_COLUMNS
            else "'event-year spatial assignment'"
        )
        temporal_fields = f"""
            event_gemeentecode,
            event_gemeentenaam,
            {boundary_year_expression} AS event_boundary_year,
            {assignment_expression} AS event_assignment_method,
            current_gemeentecode,
            current_gemeentenaam,
        """
    else:
        temporal_fields = """
            gemeentecode AS event_gemeentecode,
            gemeentenaam AS event_gemeentenaam,
            NULL::integer AS event_boundary_year,
            'legacy single-geography relation'::text AS event_assignment_method,
            gemeentecode AS current_gemeentecode,
            gemeentenaam AS current_gemeentenaam,
        """

    query = text(
        f"""
        SELECT
            pand_id,
            bouwjaar,
            pandstatus,
            demolition_date,
            demolition_year,
            {temporal_fields}
            {municipality_code_column}::text AS gemeentecode,
            {municipality_name_column} AS gemeentenaam,
            wijkcode,
            wijknaam,
            buurtcode,
            buurtnaam,
            lifespan_years,
            valid_lifespan,
            ST_X(ST_Transform(point_rd, 4326)) AS lon,
            ST_Y(ST_Transform(point_rd, 4326)) AS lat
        FROM {DEMOLITION_RECORDS_TABLE}
        WHERE {municipality_name_column} = ANY(:municipalities)
          AND demolition_year BETWEEN :start_year AND :end_year;
        """
    )

    records = pd.read_sql(
        query,
        engine,
        params={
            "municipalities": list(selected_municipalities),
            "start_year": int(start_year),
            "end_year": int(end_year),
        },
    )
    return sanitize_demolition_age_fields(records)


@st.cache_data
def load_pipeline_summary(selected_municipalities):
    summary_relation = None
    for candidate in [
        "thesis_ch4.vw_pipeline_fryslan_summary",
        "thesis_ch4.vw_pipeline_case_summary",
    ]:
        if relation_exists(candidate):
            summary_relation = candidate
            break

    if summary_relation is None:
        return pd.DataFrame()

    query = text(
        f"""
        SELECT
            gemeentenaam AS municipality,
            aoi_records_all_years,
            records_2011_2025,
            missing_bouwjaar_2011_2025,
            removed_non_positive_lifespan_2011_2025,
            expected_final_sample,
            materialized_final_sample
        FROM {summary_relation}
        WHERE gemeentenaam = ANY(:municipalities)
        ORDER BY gemeentenaam;
        """
    )
    return pd.read_sql(
        query,
        engine,
        params={"municipalities": list(selected_municipalities)},
    )


@st.cache_data
def load_global_source_quality():
    if not relation_exists("thesis_ch4.vw_source_geometry_quality"):
        return pd.DataFrame()

    return pd.read_sql(
        """
        SELECT *
        FROM thesis_ch4.vw_source_geometry_quality;
        """,
        engine,
    )



# Construction periods are intentionally broader than decades. The purpose is
# to preserve historically interpretable building eras without allowing dozens
# of nearly empty early cohorts to dominate the dashboard. Exact bouwjaar stays
# available in record-level tables and downloads.
CONSTRUCTION_COHORTS = (
    {"start": 1, "end": 1899, "label": "Before 1900"},
    {"start": 1900, "end": 1929, "label": "1900–1929"},
    {"start": 1930, "end": 1945, "label": "1930–1945"},
    {"start": 1946, "end": 1964, "label": "1946–1964"},
    {"start": 1965, "end": 1974, "label": "1965–1974"},
    {"start": 1975, "end": 1991, "label": "1975–1991"},
    {"start": 1992, "end": 2005, "label": "1992–2005"},
    {"start": 2006, "end": 2014, "label": "2006–2014"},
    {"start": 2015, "end": ANALYSIS_END_YEAR, "label": "2015–2025"},
)


def add_construction_cohort(df):
    """Assign valid construction years to shared analytical periods.

    Missing and chronologically invalid construction years remain null. They are
    validation issues, not analytical cohorts, and are excluded from cohort
    charts, tables, downloads and the 3D cohort style.
    """
    out = df.copy()
    bouwjaar = pd.to_numeric(out.get("bouwjaar"), errors="coerce")
    demolition_year = pd.to_numeric(
        out.get("demolition_year"), errors="coerce"
    )
    valid = bouwjaar.notna() & bouwjaar.ge(1) & bouwjaar.le(ANALYSIS_END_YEAR)
    if isinstance(demolition_year, pd.Series):
        valid &= demolition_year.notna() & bouwjaar.lt(demolition_year)

    out["construction_cohort_order"] = pd.Series(
        pd.NA, index=out.index, dtype="Int64"
    )
    out["construction_cohort_label"] = pd.Series(
        pd.NA, index=out.index, dtype="string"
    )

    for order, cohort in enumerate(CONSTRUCTION_COHORTS):
        mask = (
            valid
            & bouwjaar.ge(cohort["start"])
            & bouwjaar.le(cohort["end"])
        )
        out.loc[mask, "construction_cohort_order"] = order
        out.loc[mask, "construction_cohort_label"] = cohort["label"]

    return out


def _cohort_color(index, total):
    """Return a deterministic Paul Tol Muted colour for a construction period."""
    cohort_palette = MUNICIPALITY_COLORS + ["#AA4499", "#6699CC"]
    return cohort_palette[index % len(cohort_palette)]


def build_construction_cohort_definitions(df):
    """Build the exact construction periods shared by statistics and 3D."""
    if df is None or df.empty or "bouwjaar" not in df.columns:
        return []

    classified = add_construction_cohort(df)
    represented_orders = set(
        classified["construction_cohort_order"].dropna().astype(int).tolist()
    )
    represented = [
        cohort
        for order, cohort in enumerate(CONSTRUCTION_COHORTS)
        if order in represented_orders
    ]
    total = len(represented)
    return [
        {
            "start": cohort["start"],
            "end": cohort["end"],
            "label": cohort["label"],
            "color": _cohort_color(index, total),
        }
        for index, cohort in enumerate(represented)
    ]


def format_geo_unit(df, geo_level):
    out = df.copy()
    if geo_level == "municipality":
        out["geo_unit"] = out["gemeentenaam"]
    else:
        out["geo_unit"] = out["wijknaam"].fillna("Onbekende wijk")
    return out


@st.cache_data
def load_dashboard_current_stock_wijk(selected_municipalities):
    relation = None
    for candidate in [
        "thesis_ch4.vw_dashboard_current_stock_wijk_final",
        "thesis_ch4.vw_dashboard_current_stock_wijk",
    ]:
        if relation_exists(candidate):
            relation = candidate
            break
    if relation is None:
        return pd.DataFrame()

    query = text(
        f"""
        SELECT *
        FROM {relation}
        WHERE gemeentenaam = ANY(:municipalities)
        ORDER BY gemeentenaam, wijknaam;
        """
    )
    try:
        return pd.read_sql(
            query,
            engine,
            params={"municipalities": list(selected_municipalities)},
        )
    except ProgrammingError:
        return pd.DataFrame()


@st.cache_data
def load_dashboard_functional_profile(
    selected_municipalities,
    start_year,
    end_year,
    geography_mode,
):
    relation = None
    for candidate in [
        "thesis_ch4.vw_dashboard_functional_loss_final",
        "thesis_ch4.vw_dashboard_functional_loss",
    ]:
        if relation_exists(candidate):
            relation = candidate
            break
    if relation is None:
        return pd.DataFrame()

    _, name_column = municipality_columns_for_relation(
        relation,
        geography_mode,
    )
    query = text(
        f"""
        SELECT
            f.*,
            f.{name_column} AS analysis_gemeentenaam
        FROM {relation} f
        WHERE f.{name_column} = ANY(:municipalities)
          AND f.demolition_year BETWEEN :start_year AND :end_year
        ORDER BY f.{name_column}, f.wijknaam, f.gebruiksdoel;
        """
    )
    try:
        result = pd.read_sql(
            query,
            engine,
            params={
                "municipalities": list(selected_municipalities),
                "start_year": int(start_year),
                "end_year": int(end_year),
            },
        )
        result["gemeentenaam"] = result["analysis_gemeentenaam"]
        return result
    except ProgrammingError:
        return pd.DataFrame()



@st.cache_data
def load_dashboard_functional_stock(selected_municipalities):
    """Load the validated current-stock functional composition."""
    relation = "thesis_ch4.vw_dashboard_functional_stock_final"
    if not relation_exists(relation):
        return pd.DataFrame()

    available = set(get_relation_columns(relation))
    name_column = (
        "current_gemeentenaam"
        if "current_gemeentenaam" in available
        else "gemeentenaam"
    )
    if name_column not in available:
        return pd.DataFrame()

    query = text(
        f"""
        SELECT
            s.*,
            s.{name_column} AS analysis_gemeentenaam
        FROM {relation} s
        WHERE s.{name_column} = ANY(:municipalities)
        ORDER BY s.{name_column}, s.gebruiksdoel;
        """
    )
    try:
        result = pd.read_sql(
            query,
            engine,
            params={"municipalities": list(selected_municipalities)},
        )
        result["gemeentenaam"] = result["analysis_gemeentenaam"]
        return result
    except Exception:
        return pd.DataFrame()


def _hex_to_rgba(hex_color, alpha=220):
    """Convert #RRGGBB to a PyDeck-compatible RGBA list."""
    value = str(hex_color or "#999999").lstrip("#")
    if len(value) != 6:
        value = "999999"
    return [
        int(value[0:2], 16),
        int(value[2:4], 16),
        int(value[4:6], 16),
        int(alpha),
    ]


@st.cache_data(show_spinner=False, ttl=900)
def load_demolished_pand_footprints(
    selected_municipalities,
    start_year,
    end_year,
    geography_mode,
):
    """Load object-level footprints for the validated demolition population."""
    selected_municipalities = [
        str(value)
        for value in (selected_municipalities or [])
        if str(value).strip()
    ]
    if not selected_municipalities:
        return {"type": "FeatureCollection", "features": []}, pd.DataFrame()

    _, municipality_name_column = analysis_municipality_columns(geography_mode)

    source_columns = set(get_relation_columns(DEMOLITION_RECORDS_TABLE))
    if "geovlak" in source_columns:
        geometry_join = ""
        geometry_expression = "d.geovlak"
    elif relation_exists("thesis_ch4.demolition_analysis_2011_2025"):
        geometry_join = """
            JOIN thesis_ch4.demolition_analysis_2011_2025 g
              ON g.pand_id::text = d.pand_id::text
        """
        geometry_expression = "g.geovlak"
    else:
        return {"type": "FeatureCollection", "features": []}, pd.DataFrame()

    query = text(
        f"""
        SELECT
            d.pand_id::text AS pand_id,
            d.demolition_date,
            d.demolition_year,
            d.bouwjaar,
            d.pandstatus,
            d.lifespan_years,
            d.wijknaam,
            d.buurtnaam,
            ST_AsGeoJSON(
                ST_Transform(
                    ST_SimplifyPreserveTopology(
                        ST_MakeValid(ST_Force2D({geometry_expression})),
                        0.15
                    ),
                    4326
                ),
                6
            ) AS geojson
        FROM {DEMOLITION_RECORDS_TABLE} d
        {geometry_join}
        WHERE d.{municipality_name_column} = ANY(:municipalities)
          AND d.demolition_year BETWEEN :start_year AND :end_year
          AND {geometry_expression} IS NOT NULL
          AND NOT ST_IsEmpty({geometry_expression})
        ORDER BY d.demolition_year, d.pand_id;
        """
    )
    try:
        frame = pd.read_sql(
            query,
            engine,
            params={
                "municipalities": selected_municipalities,
                "start_year": int(start_year),
                "end_year": int(end_year),
            },
        )
    except Exception:
        return {"type": "FeatureCollection", "features": []}, pd.DataFrame()

    features = []
    for row in frame.itertuples(index=False):
        try:
            geometry = json.loads(row.geojson) if isinstance(row.geojson, str) else row.geojson
        except Exception:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "object_type": "Demolished PAND",
                    "object_id": str(row.pand_id),
                    "pand_id": str(row.pand_id),
                    "demolition_year": (
                        int(row.demolition_year)
                        if pd.notna(row.demolition_year)
                        else ""
                    ),
                    "demolition_date": (
                        str(row.demolition_date)
                        if pd.notna(row.demolition_date)
                        else ""
                    ),
                    "bouwjaar": (
                        int(row.bouwjaar)
                        if pd.notna(row.bouwjaar)
                        else ""
                    ),
                    "gebruiksdoel": "",
                    "pandstatus": str(row.pandstatus or ""),
                    "lifespan_years": str(row.lifespan_years) if pd.notna(row.lifespan_years) else "",
                    "wijknaam": str(row.wijknaam or ""),
                    "buurtnaam": str(row.buurtnaam or ""),
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}, frame


@st.cache_data(show_spinner=False, ttl=900)
def load_demolished_vbo_function_points(
    selected_municipalities,
    start_year,
    end_year,
    geography_mode,
):
    """Load VBOs linked to demolished PANDs at each PAND demolition date.

    One row is returned per VBO. Multi-use VBOs retain their complete BAG
    function set instead of inventing a primary function.
    """
    selected_municipalities = [
        str(value)
        for value in (selected_municipalities or [])
        if str(value).strip()
    ]
    if not selected_municipalities:
        return pd.DataFrame()

    _, municipality_name_column = analysis_municipality_columns(geography_mode)

    query = text(
        f"""
        WITH demolished AS (
            SELECT
                pand_id::text AS pand_id,
                demolition_date::date AS demolition_date,
                demolition_year,
                wijknaam,
                buurtnaam
            FROM {DEMOLITION_RECORDS_TABLE}
            WHERE {municipality_name_column} = ANY(:municipalities)
              AND demolition_year BETWEEN :start_year AND :end_year
              AND demolition_date IS NOT NULL
        ),
        valid_relation AS (
            SELECT DISTINCT
                d.pand_id,
                d.demolition_date,
                d.demolition_year,
                d.wijknaam,
                d.buurtnaam,
                vp.identificatie::text AS vbo_id
            FROM demolished d
            JOIN dt_frl.verblijfsobjectpand vp
              ON vp.gerelateerdpand::text = d.pand_id
             AND vp.aanduidingrecordinactief IS DISTINCT FROM TRUE
             AND vp.begindatumtijdvakgeldigheid
                   < d.demolition_date + INTERVAL '1 day'
             AND COALESCE(
                    vp.einddatumtijdvakgeldigheid,
                    'infinity'::timestamptz
                 ) >= d.demolition_date
        ),
        vbo_version AS (
            SELECT DISTINCT ON (r.pand_id, r.vbo_id)
                r.pand_id,
                r.vbo_id,
                r.demolition_date,
                r.demolition_year,
                r.wijknaam,
                r.buurtnaam,
                v.oppervlakteverblijfsobject,
                v.verblijfsobjectstatus::text AS verblijfsobjectstatus,
                v.geopunt
            FROM valid_relation r
            JOIN dt_frl.verblijfsobject v
              ON v.identificatie::text = r.vbo_id
             AND v.aanduidingrecordinactief IS DISTINCT FROM TRUE
             AND v.begindatumtijdvakgeldigheid
                   < r.demolition_date + INTERVAL '1 day'
             AND COALESCE(
                    v.einddatumtijdvakgeldigheid,
                    'infinity'::timestamptz
                 ) >= r.demolition_date
            WHERE v.geopunt IS NOT NULL
            ORDER BY
                r.pand_id,
                r.vbo_id,
                v.begindatumtijdvakgeldigheid DESC NULLS LAST,
                v.tijdstipregistratie DESC NULLS LAST,
                v.voorkomenidentificatie DESC NULLS LAST,
                v.gid DESC
        ),
        function_sets AS (
            SELECT
                r.pand_id,
                r.vbo_id,
                STRING_AGG(
                    DISTINCT g.gebruiksdoelverblijfsobject::text,
                    ' + '
                    ORDER BY g.gebruiksdoelverblijfsobject::text
                ) AS gebruiksdoel_set,
                COUNT(
                    DISTINCT g.gebruiksdoelverblijfsobject::text
                )::integer AS function_count
            FROM valid_relation r
            JOIN dt_frl.verblijfsobjectgebruiksdoel g
              ON g.identificatie::text = r.vbo_id
             AND g.aanduidingrecordinactief IS DISTINCT FROM TRUE
             AND g.begindatumtijdvakgeldigheid
                   < r.demolition_date + INTERVAL '1 day'
             AND COALESCE(
                    g.einddatumtijdvakgeldigheid,
                    'infinity'::timestamptz
                 ) >= r.demolition_date
            WHERE g.gebruiksdoelverblijfsobject IS NOT NULL
            GROUP BY r.pand_id, r.vbo_id
        )
        SELECT
            v.pand_id,
            v.vbo_id,
            v.demolition_date,
            v.demolition_year,
            v.wijknaam,
            v.buurtnaam,
            v.oppervlakteverblijfsobject,
            v.verblijfsobjectstatus,
            COALESCE(f.gebruiksdoel_set, 'Onbekend') AS gebruiksdoel_set,
            COALESCE(f.function_count, 0) AS function_count,
            ST_X(ST_Transform(v.geopunt, 4326))::double precision AS lon,
            ST_Y(ST_Transform(v.geopunt, 4326))::double precision AS lat
        FROM vbo_version v
        LEFT JOIN function_sets f
          ON f.pand_id = v.pand_id
         AND f.vbo_id = v.vbo_id
        ORDER BY v.demolition_year, v.pand_id, v.vbo_id;
        """
    )

    try:
        result = pd.read_sql(
            query,
            engine,
            params={
                "municipalities": selected_municipalities,
                "start_year": int(start_year),
                "end_year": int(end_year),
            },
        )
    except Exception:
        return pd.DataFrame()

    if result.empty:
        return result

    result["gebruiksdoel_set"] = (
        result["gebruiksdoel_set"].fillna("Onbekend").astype(str)
    )
    result["function_count"] = (
        pd.to_numeric(result["function_count"], errors="coerce")
        .fillna(0)
        .astype(int)
    )
    result["display_function"] = result.apply(
        lambda row: (
            row["gebruiksdoel_set"]
            if int(row["function_count"]) == 1
            else ("Mixed use" if int(row["function_count"]) > 1 else "Onbekend")
        ),
        axis=1,
    )
    result["point_color"] = result["display_function"].apply(
        lambda value: _hex_to_rgba(
            FUNCTION_COLORS.get(
                value,
                "#7F7F7F" if value == "Mixed use" else UNKNOWN_COLOR,
            ),
            230,
        )
    )
    return result


def render_function_legend(point_df):
    if point_df is None or point_df.empty:
        return
    categories = (
        point_df["display_function"]
        .fillna("Onbekend")
        .astype(str)
        .drop_duplicates()
        .tolist()
    )
    chips = []
    for value in categories:
        color = FUNCTION_COLORS.get(
            value,
            "#7F7F7F" if value == "Mixed use" else UNKNOWN_COLOR,
        )
        chips.append(
            f'<span style="display:inline-flex;align-items:center;margin:2px 12px 4px 0;">'
            f'<span style="width:11px;height:11px;border-radius:50%;'
            f'background:{color};display:inline-block;margin-right:6px;"></span>'
            f'{value}</span>'
        )
    st.markdown(
        "<div style='font-size:0.88rem;color:#4B4B4B;'>"
        + "".join(chips)
        + "</div>",
        unsafe_allow_html=True,
    )


def render_demolished_pand_vbo_map(
    pand_feature_collection,
    vbo_points,
    title,
    key,
    show_vbos=True,
):
    """Render PAND footprints and optional function-coloured VBO points."""
    features = (pand_feature_collection or {}).get("features", [])
    if not features:
        st.info("No demolished PAND polygon geometries are available for this scope.")
        return

    pand_layer = pdk.Layer(
        "GeoJsonLayer",
        id=f"demolished-panden-{key}",
        data=pand_feature_collection,
        pickable=True,
        auto_highlight=True,
        stroked=True,
        filled=True,
        get_fill_color=[204, 102, 119, 28],
        get_line_color=[153, 68, 85, 230],
        line_width_min_pixels=1.4,
        highlight_color=[255, 220, 225, 120],
    )

    layers = [pand_layer]
    if show_vbos and vbo_points is not None and not vbo_points.empty:
        point_records = []
        for row in vbo_points.itertuples(index=False):
            if pd.isna(row.lon) or pd.isna(row.lat):
                continue
            point_records.append(
                {
                    "lon": float(row.lon),
                    "lat": float(row.lat),
                    "object_type": "VBO linked at PAND demolition date",
                    "object_id": str(row.vbo_id),
                    "pand_id": str(row.pand_id),
                    "demolition_year": (
                        int(row.demolition_year)
                        if pd.notna(row.demolition_year)
                        else ""
                    ),
                    "demolition_date": (
                        str(row.demolition_date)
                        if pd.notna(row.demolition_date)
                        else ""
                    ),
                    "bouwjaar": "",
                    "gebruiksdoel": str(row.gebruiksdoel_set),
                    "wijknaam": str(row.wijknaam or ""),
                    "buurtnaam": str(row.buurtnaam or ""),
                    "point_color": [int(x) for x in row.point_color],
                }
            )

        if point_records:
            layers.append(
                pdk.Layer(
                    "ScatterplotLayer",
                    id=f"demolished-vbos-{key}",
                    data=point_records,
                    get_position="[lon, lat]",
                    get_fill_color="point_color",
                    get_line_color=[45, 45, 45, 190],
                    get_radius=11,
                    radius_min_pixels=5,
                    radius_max_pixels=12,
                    line_width_min_pixels=0.7,
                    stroked=True,
                    filled=True,
                    pickable=True,
                    auto_highlight=True,
                )
            )

    tooltip = {
        "html": """
            <b>{object_type}</b><br/>
            ID: {object_id}<br/>
            PAND: {pand_id}<br/>
            Demolition date: {demolition_date}<br/>
            Demolition year: {demolition_year}<br/>
            Construction year: {bouwjaar}<br/>
            Use function(s): {gebruiksdoel}<br/>
            Wijk: {wijknaam}<br/>
            Buurt: {buurtnaam}
        """,
        "style": {"backgroundColor": "white", "color": "black"},
    }

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=derive_map_view_state(pand_feature_collection),
        tooltip=tooltip,
        map_style=None,
    )
    st.markdown(f"#### {title}")
    st.pydeck_chart(deck, use_container_width=True, key=key)


def build_lemmer_sensitivity_summary(records_df):
    """Compare the main sample with the DFM and Lemmer sensitivity exclusions."""
    if records_df is None or records_df.empty:
        return pd.DataFrame()

    base = records_df.copy()
    scenarios = [
        ("All selected records", pd.Series(True, index=base.index)),
        (
            "Without De Fryske Marren",
            base["gemeentenaam"].astype(str) != "De Fryske Marren",
        ),
        (
            "Without Lemmer-Frieslandpark",
            ~(
                (base["gemeentenaam"].astype(str) == "De Fryske Marren")
                & (base["buurtnaam"].astype(str) == "Lemmer-Frieslandpark")
            ),
        ),
    ]

    rows = []
    for label, mask in scenarios:
        subset = base.loc[mask].copy()
        annual = (
            subset.groupby("demolition_year")["pand_id"]
            .nunique()
            .sort_values(ascending=False)
        )
        if annual.empty:
            peak_year = None
            peak_count = 0
        else:
            max_count = int(annual.max())
            peak_year = int(
                sorted(annual[annual == max_count].index.astype(int).tolist())[0]
            )
            peak_count = max_count

        lifespan = pd.to_numeric(
            subset.get("lifespan_years"), errors="coerce"
        ).dropna()

        rows.append(
            {
                "Scenario": label,
                "Demolished PANDs": int(subset["pand_id"].nunique()),
                "Valid lifespan records": int(len(lifespan)),
                "Mean lifespan (years)": (
                    round(float(lifespan.mean()), 1)
                    if not lifespan.empty
                    else None
                ),
                "Median lifespan (years)": (
                    round(float(lifespan.median()), 1)
                    if not lifespan.empty
                    else None
                ),
                "Peak year": peak_year,
                "Peak demolitions": peak_count,
            }
        )
    return pd.DataFrame(rows)


def build_function_sensitivity_summary(functional_rows, records_df):
    """Track logies and overige shares under the sensitivity exclusions."""
    if (
        functional_rows is None
        or functional_rows.empty
        or records_df is None
        or records_df.empty
        or "pand_id" not in functional_rows.columns
        or "fractional_weight" not in functional_rows.columns
    ):
        return pd.DataFrame()

    base_ids = set(records_df["pand_id"].astype(str))
    without_dfm_ids = set(
        records_df.loc[
            records_df["gemeentenaam"].astype(str) != "De Fryske Marren",
            "pand_id",
        ].astype(str)
    )
    without_lemmer_ids = set(
        records_df.loc[
            ~(
                (records_df["gemeentenaam"].astype(str) == "De Fryske Marren")
                & (records_df["buurtnaam"].astype(str) == "Lemmer-Frieslandpark")
            ),
            "pand_id",
        ].astype(str)
    )

    scenarios = [
        ("All selected records", base_ids),
        ("Without De Fryske Marren", without_dfm_ids),
        ("Without Lemmer-Frieslandpark", without_lemmer_ids),
    ]

    source = functional_rows.copy()
    source["pand_id"] = source["pand_id"].astype(str)
    source["fractional_weight"] = pd.to_numeric(
        source["fractional_weight"], errors="coerce"
    ).fillna(0.0)
    source["gebruiksdoel"] = (
        source["gebruiksdoel"].fillna("Onbekend").astype(str)
    )

    rows = []
    for label, ids in scenarios:
        subset = source[source["pand_id"].isin(ids)].copy()
        total = float(subset["fractional_weight"].sum())
        by_function = (
            subset.groupby("gebruiksdoel")["fractional_weight"].sum()
            if not subset.empty
            else pd.Series(dtype=float)
        )
        rows.append(
            {
                "Scenario": label,
                "Logies share (%)": (
                    round(
                        float(by_function.get("logiesfunctie", 0.0))
                        / total
                        * 100,
                        2,
                    )
                    if total > 0
                    else None
                ),
                "Overige share (%)": (
                    round(
                        float(by_function.get("overige gebruiksfunctie", 0.0))
                        / total
                        * 100,
                        2,
                    )
                    if total > 0
                    else None
                ),
            }
        )
    return pd.DataFrame(rows)



@st.cache_data
def load_dashboard_usage_transitions(
    selected_municipalities,
    start_year,
    end_year,
):
    """Load historical BAG verblijfsobject gebruiksdoel transitions.

    A transition is registered only when the *set* of gebruiksdoelen differs
    between consecutive valid occurrences of the same verblijfsobject. This
    avoids inventing a primary function when BAG records multiple functions.

    The query returns transition points plus municipality, wijk and buurt scope
    rows. The scope rows provide transparent denominators for normalized rates
    and allow the same events to be mapped at wijk and buurt granularity.
    """
    required_relations = [
        "dt_frl.verblijfsobjectgebruiksdoel",
        "dt_frl.verblijfsobject",
        "dt_frl.verblijfsobject_current",
        "dt_frl_base.vw_wijken_admin",
        "dt_frl_base.vw_buurten_admin",
    ]
    if not all(relation_exists(name) for name in required_relations):
        return pd.DataFrame()

    query = text(
        """
        WITH usage_sets AS (
            SELECT
                g.identificatie::text AS vbo_id,
                g.voorkomenidentificatie,
                MIN(g.begindatumtijdvakgeldigheid) AS valid_from,
                MAX(g.einddatumtijdvakgeldigheid) AS valid_to,
                ARRAY_AGG(
                    DISTINCT g.gebruiksdoelverblijfsobject::text
                    ORDER BY g.gebruiksdoelverblijfsobject::text
                ) AS use_set
            FROM dt_frl.verblijfsobjectgebruiksdoel g
            WHERE g.aanduidingrecordinactief IS DISTINCT FROM TRUE
              AND g.gebruiksdoelverblijfsobject IS NOT NULL
            GROUP BY
                g.identificatie,
                g.voorkomenidentificatie
        ),
        version_geometry AS (
            SELECT DISTINCT ON (v.identificatie, v.voorkomenidentificatie)
                v.identificatie::text AS vbo_id,
                v.voorkomenidentificatie,
                v.geopunt
            FROM dt_frl.verblijfsobject v
            WHERE v.aanduidingrecordinactief IS DISTINCT FROM TRUE
            ORDER BY
                v.identificatie,
                v.voorkomenidentificatie,
                v.tijdstipregistratie DESC NULLS LAST,
                v.gid DESC
        ),
        current_geometry AS (
            SELECT
                identificatie::text AS vbo_id,
                geopunt
            FROM dt_frl.verblijfsobject_current
        ),
        usage_versions AS (
            SELECT
                u.vbo_id,
                u.voorkomenidentificatie,
                u.valid_from,
                u.valid_to,
                u.use_set,
                COALESCE(vg.geopunt, cg.geopunt) AS geopunt
            FROM usage_sets u
            LEFT JOIN version_geometry vg
              ON vg.vbo_id = u.vbo_id
             AND vg.voorkomenidentificatie = u.voorkomenidentificatie
            LEFT JOIN current_geometry cg
              ON cg.vbo_id = u.vbo_id
            WHERE u.valid_from IS NOT NULL
        ),
        ordered_versions AS (
            SELECT
                u.*,
                LAG(u.use_set) OVER (
                    PARTITION BY u.vbo_id
                    ORDER BY
                        u.valid_from,
                        u.voorkomenidentificatie
                ) AS previous_use_set
            FROM usage_versions u
        ),
        scoped_versions AS (
            SELECT
                o.*,
                a.gemeentecode::text AS gemeentecode,
                a.gemeentenaam,
                COALESCE(w.wijkcode, 'UNKNOWN') AS wijkcode,
                COALESCE(w.wijknaam, 'Onbekende wijk') AS wijknaam,
                COALESCE(b.buurtcode, 'UNKNOWN') AS buurtcode,
                COALESCE(b.buurtnaam, 'Onbekende buurt') AS buurtnaam,
                CASE
                    WHEN o.geopunt IS NOT NULL
                    THEN ST_X(ST_Transform(o.geopunt, 4326))
                    ELSE NULL
                END AS lon,
                CASE
                    WHEN o.geopunt IS NOT NULL
                    THEN ST_Y(ST_Transform(o.geopunt, 4326))
                    ELSE NULL
                END AS lat
            FROM ordered_versions o
            JOIN dt_frl_base.vw_gemeente_admin a
              ON o.geopunt IS NOT NULL
             AND ST_Covers(a.geom, o.geopunt)
            LEFT JOIN dt_frl_base.vw_wijken_admin w
              ON w.gemeentecode::text = a.gemeentecode::text
             AND ST_Covers(w.geom, o.geopunt)
            LEFT JOIN dt_frl_base.vw_buurten_admin b
              ON b.gemeentecode::text = a.gemeentecode::text
             AND ST_Covers(b.geom, o.geopunt)
            WHERE a.gemeentenaam = ANY(:municipalities)
        ),
        scope_totals AS (
            SELECT
                'municipality'::text AS geo_level,
                gemeentenaam,
                NULL::text AS wijkcode,
                NULL::text AS wijknaam,
                NULL::text AS buurtcode,
                NULL::text AS buurtnaam,
                COUNT(DISTINCT vbo_id)::bigint AS observed_vbos
            FROM scoped_versions
            WHERE valid_from < MAKE_DATE(:end_year + 1, 1, 1)
              AND COALESCE(valid_to, 'infinity'::timestamptz)
                    >= MAKE_DATE(:start_year, 1, 1)
            GROUP BY gemeentenaam

            UNION ALL

            SELECT
                'wijk'::text AS geo_level,
                gemeentenaam,
                wijkcode,
                wijknaam,
                NULL::text AS buurtcode,
                NULL::text AS buurtnaam,
                COUNT(DISTINCT vbo_id)::bigint AS observed_vbos
            FROM scoped_versions
            WHERE valid_from < MAKE_DATE(:end_year + 1, 1, 1)
              AND COALESCE(valid_to, 'infinity'::timestamptz)
                    >= MAKE_DATE(:start_year, 1, 1)
            GROUP BY gemeentenaam, wijkcode, wijknaam

            UNION ALL

            SELECT
                'buurt'::text AS geo_level,
                gemeentenaam,
                wijkcode,
                wijknaam,
                buurtcode,
                buurtnaam,
                COUNT(DISTINCT vbo_id)::bigint AS observed_vbos
            FROM scoped_versions
            WHERE valid_from < MAKE_DATE(:end_year + 1, 1, 1)
              AND COALESCE(valid_to, 'infinity'::timestamptz)
                    >= MAKE_DATE(:start_year, 1, 1)
            GROUP BY
                gemeentenaam,
                wijkcode,
                wijknaam,
                buurtcode,
                buurtnaam
        ),
        transition_events AS (
            SELECT
                vbo_id,
                valid_from::date AS transition_date,
                EXTRACT(YEAR FROM valid_from)::integer AS transition_year,
                gemeentenaam,
                wijkcode,
                wijknaam,
                buurtcode,
                buurtnaam,
                ARRAY_TO_STRING(previous_use_set, ' + ') AS from_use,
                ARRAY_TO_STRING(use_set, ' + ') AS to_use,
                lon,
                lat
            FROM scoped_versions
            WHERE previous_use_set IS NOT NULL
              AND previous_use_set IS DISTINCT FROM use_set
              AND EXTRACT(YEAR FROM valid_from)::integer
                    BETWEEN :start_year AND :end_year
        )
        SELECT
            'transition'::text AS record_type,
            t.vbo_id,
            t.transition_date,
            t.transition_year,
            t.gemeentenaam,
            t.wijkcode,
            t.wijknaam,
            t.buurtcode,
            t.buurtnaam,
            t.from_use,
            t.to_use,
            t.lon,
            t.lat,
            NULL::text AS geo_level,
            NULL::bigint AS observed_vbos
        FROM transition_events t

        UNION ALL

        SELECT
            'scope'::text AS record_type,
            NULL::text AS vbo_id,
            NULL::date AS transition_date,
            NULL::integer AS transition_year,
            s.gemeentenaam,
            s.wijkcode,
            s.wijknaam,
            s.buurtcode,
            s.buurtnaam,
            NULL::text AS from_use,
            NULL::text AS to_use,
            NULL::double precision AS lon,
            NULL::double precision AS lat,
            s.geo_level,
            s.observed_vbos
        FROM scope_totals s
        ORDER BY record_type, transition_year, gemeentenaam, wijknaam, buurtnaam;
        """
    )

    try:
        return pd.read_sql(
            query,
            engine,
            params={
                "municipalities": list(selected_municipalities),
                "start_year": int(start_year),
                "end_year": int(end_year),
            },
        )
    except Exception:
        # Keep the rest of the ten-indicator dashboard available when the
        # historical function query is not supported by a local BAG import.
        return pd.DataFrame()

def add_annual_transition_signal(annual_df, value_column="transition_events"):
    """Classify annual function-change intensity against its local baseline.

    A robust z-score based on the median absolute deviation is used when
    possible. If the series has no dispersion under that measure, the function
    falls back to a conventional population standard deviation. The signal is
    descriptive, not proof of a causal policy or physical transformation event.
    """
    out = annual_df.copy()
    values = pd.to_numeric(out[value_column], errors="coerce").fillna(0.0)
    median = float(values.median()) if not values.empty else 0.0
    mad = float((values - median).abs().median()) if not values.empty else 0.0

    if mad > 0:
        out["anomaly_score"] = 0.6745 * (values - median) / mad
        baseline_method = "robust z-score (median/MAD)"
    else:
        std = float(values.std(ddof=0)) if len(values) > 1 else 0.0
        if std > 0:
            out["anomaly_score"] = (values - float(values.mean())) / std
            baseline_method = "z-score (mean/standard deviation fallback)"
        else:
            out["anomaly_score"] = 0.0
            baseline_method = "constant-series fallback"

    def classify(row):
        count = float(row[value_column])
        score = float(row["anomaly_score"])
        if count >= 5 and score >= 2.5:
            return "Exceptional"
        if count >= 3 and score >= 1.0:
            return "Elevated"
        return "Normal"

    out["signal"] = out.apply(classify, axis=1)
    out["signal_icon"] = out["signal"].map(
        {
            "Normal": "🟢",
            "Elevated": "🟠",
            "Exceptional": "🔴",
        }
    )
    out["baseline_method"] = baseline_method
    return out


@st.cache_data
def load_dashboard_registration_delay(
    selected_municipalities,
    start_year,
    end_year,
    geography_mode,
):
    relation = "thesis_ch4.vw_dashboard_registration_delay"
    if not relation_exists(relation):
        return pd.DataFrame()

    _, name_column = municipality_columns_for_relation(
        relation,
        geography_mode,
    )
    query = text(
        f"""
        SELECT
            r.*,
            r.{name_column} AS analysis_gemeentenaam
        FROM {relation} r
        WHERE EXISTS (
            SELECT 1 FROM {DEMOLITION_RECORDS_TABLE} eligible
            WHERE eligible.pand_id::text = r.pand_id::text
              AND eligible.demolition_year = r.demolition_year
        )
          AND r.{name_column} = ANY(:municipalities)
          AND r.demolition_year BETWEEN :start_year AND :end_year
        ORDER BY r.{name_column}, r.demolition_year, r.pand_id;
        """
    )
    try:
        result = pd.read_sql(
            query,
            engine,
            params={
                "municipalities": list(selected_municipalities),
                "start_year": int(start_year),
                "end_year": int(end_year),
            },
        )
        result["gemeentenaam"] = result["analysis_gemeentenaam"]
        return result
    except ProgrammingError:
        return pd.DataFrame()


@st.cache_data
def load_dashboard_replacement_lag(
    selected_municipalities,
    start_year,
    end_year,
    geography_mode,
):
    relation = "thesis_ch4.vw_dashboard_replacement_lag"
    if not relation_exists(relation):
        return pd.DataFrame()

    _, name_column = municipality_columns_for_relation(
        relation,
        geography_mode,
    )
    query = text(
        f"""
        SELECT
            r.*,
            r.{name_column} AS analysis_gemeentenaam
        FROM {relation} r
        WHERE EXISTS (
            SELECT 1 FROM {DEMOLITION_RECORDS_TABLE} eligible
            WHERE eligible.pand_id::text = r.pand_id::text
              AND eligible.demolition_year = r.demolition_year
        )
          AND r.{name_column} = ANY(:municipalities)
          AND r.demolition_year BETWEEN :start_year AND :end_year
        ORDER BY r.{name_column}, r.wijknaam, r.demolition_year, r.pand_id;
        """
    )
    try:
        result = pd.read_sql(
            query,
            engine,
            params={
                "municipalities": list(selected_municipalities),
                "start_year": int(start_year),
                "end_year": int(end_year),
            },
        )
        result["gemeentenaam"] = result["analysis_gemeentenaam"]
        return result
    except ProgrammingError:
        return pd.DataFrame()


@st.cache_data
def load_historical_annual_demolition_rate(
    selected_municipalities,
    start_year,
    end_year,
):
    relation = "thesis_ch4.vw_dashboard_annual_demolition_rate_historical"
    if not relation_exists(relation):
        return pd.DataFrame()

    query = text(
        f"""
        SELECT *
        FROM {relation}
        WHERE gemeentenaam = ANY(:municipalities)
          AND demolition_year BETWEEN :start_year AND :end_year
        ORDER BY demolition_year, gemeentenaam;
        """
    )
    try:
        return pd.read_sql(
            query,
            engine,
            params={
                "municipalities": list(selected_municipalities),
                "start_year": int(start_year),
                "end_year": int(end_year),
            },
        )
    except ProgrammingError:
        return pd.DataFrame()


# ------------------------------------------------------------
# Gebruiksdoel transition helpers

# ------------------------------------------------------------

def split_usage_set(value):
    """Return a normalized tuple from a BAG use-set label."""
    if value is None or pd.isna(value):
        return tuple()
    return tuple(
        sorted(
            {
                part.strip()
                for part in str(value).split("+")
                if part and part.strip()
            }
        )
    )


USE_CATEGORY_MAP = {
    "woonfunctie": "Wonen",
    "logiesfunctie": "Logies",
    "kantoorfunctie": "Kantoor / werk",
    "winkelfunctie": "Detailhandel",
    "industriefunctie": "Industrie",
    "onderwijsfunctie": "Onderwijs",
    "gezondheidszorgfunctie": "Zorg",
    "sportfunctie": "Sport",
    "bijeenkomstfunctie": "Bijeenkomst",
    "celfunctie": "Cel",
    "overige gebruiksfunctie": "Overig",
}


def collapse_usage_set(value):
    """Collapse detailed BAG functions into transparent analytical categories."""
    categories = {
        USE_CATEGORY_MAP.get(token, token)
        for token in split_usage_set(value)
    }
    return " + ".join(sorted(categories)) if categories else "Onbekend"


def add_transition_display_columns(events, matching_mode):
    """Add token and display columns used by filters and Sankey diagrams."""
    out = events.copy()
    out["from_tokens"] = out["from_use"].apply(split_usage_set)
    out["to_tokens"] = out["to_use"].apply(split_usage_set)

    if matching_mode == "Collapsed analytical category":
        out["from_display"] = out["from_use"].apply(collapse_usage_set)
        out["to_display"] = out["to_use"].apply(collapse_usage_set)
    else:
        out["from_display"] = out["from_use"].fillna("Onbekend").astype(str)
        out["to_display"] = out["to_use"].fillna("Onbekend").astype(str)

    out["transition_label"] = (
        out["from_display"].astype(str)
        + " → "
        + out["to_display"].astype(str)
    )
    return out


def transition_filter_options(events, matching_mode):
    """Return valid source and target options for the selected matching mode."""
    if events.empty:
        return ["All"], ["All"]

    if matching_mode == "Contains individual BAG function":
        from_values = sorted(
            {token for tokens in events["from_tokens"] for token in tokens}
        )
        to_values = sorted(
            {token for tokens in events["to_tokens"] for token in tokens}
        )
    else:
        from_values = sorted(events["from_display"].dropna().astype(str).unique())
        to_values = sorted(events["to_display"].dropna().astype(str).unique())

    return ["All"] + from_values, ["All"] + to_values


def filter_transition_pairs(events, matching_mode, from_filter, to_filter):
    """Filter transitions without discarding multi-use BAG registrations."""
    if events.empty:
        return events.copy()

    mask = pd.Series(True, index=events.index)

    if matching_mode == "Contains individual BAG function":
        if from_filter != "All":
            mask &= events["from_tokens"].apply(lambda x: from_filter in x)
        if to_filter != "All":
            mask &= events["to_tokens"].apply(lambda x: to_filter in x)
    else:
        if from_filter != "All":
            mask &= events["from_display"] == from_filter
        if to_filter != "All":
            mask &= events["to_display"] == to_filter

    return events.loc[mask].copy()


def build_transition_sankey(events, top_n, title):
    """Build a readable Sankey from already scoped and filtered transitions."""
    if events.empty:
        return None

    flow_counts = (
        events.groupby(["from_display", "to_display"], dropna=False)
        .size()
        .reset_index(name="transition_events")
        .sort_values("transition_events", ascending=False)
        .head(int(top_n))
    )
    if flow_counts.empty:
        return None

    function_short_names = {
        "bijeenkomstfunctie": "bijeenkomst",
        "gezondheidszorgfunctie": "gezondheidszorg",
        "industriefunctie": "industrie",
        "kantoorfunctie": "kantoor",
        "logiesfunctie": "logies",
        "onderwijsfunctie": "onderwijs",
        "overige gebruiksfunctie": "overig",
        "sportfunctie": "sport",
        "winkelfunctie": "winkel",
        "woonfunctie": "wonen",
    }

    def compact_node_label(value, prefix):
        parts = [part.strip() for part in str(value).split("+") if part.strip()]
        parts = [function_short_names.get(part, part.replace("functie", "")) for part in parts]
        lines = [" + ".join(parts[index:index + 3]) for index in range(0, len(parts), 3)]
        return f"{prefix} · " + "<br>".join(lines)

    source_nodes = [compact_node_label(label, "From") for label in flow_counts["from_display"]]
    target_nodes = [compact_node_label(label, "To") for label in flow_counts["to_display"]]
    node_labels = list(dict.fromkeys(source_nodes + target_nodes))
    node_index = {label: idx for idx, label in enumerate(node_labels)}
    source_unique = list(dict.fromkeys(source_nodes))
    target_unique = list(dict.fromkeys(target_nodes))
    node_x = [0.01 if label in source_unique else 0.99 for label in node_labels]
    node_y = []
    for label in node_labels:
        side = source_unique if label in source_unique else target_unique
        node_y.append((side.index(label) + 0.5) / max(1, len(side)))
    sankey_palette = [
        "#CC6677", "#332288", "#DDCC77", "#117733", "#88CCEE",
        "#44AA99", "#882255", "#AA4499", "#6699CC",
    ]
    node_color_lookup = {
        label: sankey_palette[index % len(sankey_palette)]
        for index, label in enumerate(node_labels)
    }
    node_colors = [node_color_lookup[label] for label in node_labels]

    def rgba_from_hex(hex_color, alpha):
        value = hex_color.lstrip("#")
        red, green, blue = (
            int(value[index:index + 2], 16) for index in (0, 2, 4)
        )
        return f"rgba({red},{green},{blue},{alpha})"

    link_colors = [
        rgba_from_hex(node_color_lookup[source_label], 0.38)
        for source_label in source_nodes
    ]

    figure = go.Figure(
        data=[
            go.Sankey(
                arrangement="fixed",
                textfont={
                    "family": "Arial, sans-serif",
                    "size": 16,
                    "color": "#222222",
                    "weight": 600,
                    "shadow": "none",
                },
                node={
                    "pad": 22,
                    "thickness": 20,
                    "line": {"color": "#4B4B4B", "width": 0.7},
                    "label": node_labels,
                    "color": node_colors,
                    "x": node_x,
                    "y": node_y,
                },
                link={
                    "source": [node_index[label] for label in source_nodes],
                    "target": [node_index[label] for label in target_nodes],
                    "value": flow_counts["transition_events"].astype(int).tolist(),
                    "color": link_colors,
                    "customdata": [
                        f"{row.from_display} → {row.to_display}"
                        for row in flow_counts.itertuples()
                    ],
                    "hovertemplate": (
                        "%{customdata}<br>Transition events: %{value}"
                        "<extra></extra>"
                    ),
                },
            )
        ]
    )
    figure.update_layout(
        title=title,
        font_size=13,
        height=max(620, 390 + max(len(source_unique), len(target_unique)) * 34),
        margin={"l": 45, "r": 45, "t": 80, "b": 35},
    )
    return figure


@st.cache_data
def load_transition_admin_boundaries(level, selected_municipalities):
    """Load current wijk or buurt polygons for transition choropleths."""
    if level not in {"wijk", "buurt"}:
        return pd.DataFrame()

    if level == "wijk":
        query = text(
            """
            SELECT
                a.gemeentenaam,
                w.wijkcode::text AS unit_code,
                w.wijknaam AS unit_name,
                ST_AsGeoJSON(ST_Transform(w.geom, 4326)) AS geojson
            FROM dt_frl_base.vw_wijken_admin w
            JOIN dt_frl_base.vw_gemeente_admin a
              ON a.gemeentecode::text = w.gemeentecode::text
            WHERE a.gemeentenaam = ANY(:municipalities)
            ORDER BY a.gemeentenaam, w.wijknaam;
            """
        )
    else:
        query = text(
            """
            SELECT
                a.gemeentenaam,
                b.buurtcode::text AS unit_code,
                b.buurtnaam AS unit_name,
                ST_AsGeoJSON(ST_Transform(b.geom, 4326)) AS geojson
            FROM dt_frl_base.vw_buurten_admin b
            JOIN dt_frl_base.vw_gemeente_admin a
              ON a.gemeentecode::text = b.gemeentecode::text
            WHERE a.gemeentenaam = ANY(:municipalities)
            ORDER BY a.gemeentenaam, b.buurtnaam;
            """
        )

    try:
        return pd.read_sql(
            query,
            engine,
            params={"municipalities": list(selected_municipalities)},
        )
    except Exception:
        return pd.DataFrame()


def build_transition_map_table(
    selected_events,
    all_scope_events,
    scope_rows,
    boundaries,
    level,
):
    """Aggregate selected transitions and transparent denominators by area."""
    if boundaries.empty:
        return pd.DataFrame()

    code_col = "wijkcode" if level == "wijk" else "buurtcode"
    name_col = "wijknaam" if level == "wijk" else "buurtnaam"

    selected_counts = (
        selected_events.groupby(["gemeentenaam", code_col, name_col], dropna=False)
        .size()
        .reset_index(name="selected_transitions")
    )
    all_counts = (
        all_scope_events.groupby(["gemeentenaam", code_col, name_col], dropna=False)
        .size()
        .reset_index(name="all_transitions")
    )

    denominators = scope_rows[scope_rows["geo_level"] == level].copy()
    denominator_cols = ["gemeentenaam", code_col, name_col, "observed_vbos"]
    denominators = denominators[denominator_cols].drop_duplicates(
        ["gemeentenaam", code_col]
    )

    for frame in (selected_counts, all_counts, denominators):
        if not frame.empty:
            frame[code_col] = frame[code_col].astype(str)

    out = boundaries.copy()
    out["unit_code"] = out["unit_code"].astype(str)
    out = out.merge(
        selected_counts.rename(columns={code_col: "unit_code", name_col: "event_unit_name"}),
        on=["gemeentenaam", "unit_code"],
        how="left",
    )
    out = out.merge(
        all_counts.rename(columns={code_col: "unit_code", name_col: "all_unit_name"}),
        on=["gemeentenaam", "unit_code"],
        how="left",
    )
    out = out.merge(
        denominators.rename(columns={code_col: "unit_code", name_col: "scope_unit_name"}),
        on=["gemeentenaam", "unit_code"],
        how="left",
    )

    # Keep only units represented by the selected analytical scope. For a
    # municipality overview this retains all observed wijken/buurten; for a
    # selected wijk it prevents unrelated polygons from appearing as zero values.
    if not denominators.empty:
        valid_keys = set(
            zip(
                denominators["gemeentenaam"].astype(str),
                denominators[code_col].astype(str),
            )
        )
        out = out[
            [
                (str(row.gemeentenaam), str(row.unit_code)) in valid_keys
                for row in out.itertuples()
            ]
        ].copy()

    out["selected_transitions"] = (
        pd.to_numeric(out["selected_transitions"], errors="coerce")
        .fillna(0)
        .astype(int)
    )
    out["all_transitions"] = (
        pd.to_numeric(out["all_transitions"], errors="coerce")
        .fillna(0)
        .astype(int)
    )
    out["observed_vbos"] = (
        pd.to_numeric(out["observed_vbos"], errors="coerce")
        .fillna(0)
        .astype(int)
    )
    out["rate_per_1000"] = out.apply(
        lambda row: (
            row["selected_transitions"] / row["observed_vbos"] * 1000
            if row["observed_vbos"] > 0
            else 0.0
        ),
        axis=1,
    )
    out["share_of_area_transitions_pct"] = out.apply(
        lambda row: (
            row["selected_transitions"] / row["all_transitions"] * 100
            if row["all_transitions"] > 0
            else 0.0
        ),
        axis=1,
    )
    return out


def transition_map_feature_collection(df, metric_column, metric_label):
    """Convert transition aggregates to a PyDeck-ready GeoJSON collection."""
    if df.empty:
        return {"type": "FeatureCollection", "features": []}

    max_value = float(pd.to_numeric(df[metric_column], errors="coerce").fillna(0).max())
    features = []
    for _, row in df.iterrows():
        raw_geometry = row.get("geojson")
        if raw_geometry is None:
            continue
        geometry = json.loads(raw_geometry) if isinstance(raw_geometry, str) else raw_geometry
        value = float(row.get(metric_column, 0) or 0)
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "display_name": row.get("unit_name"),
                    "municipality": row.get("gemeentenaam"),
                    "selected_transitions": int(row.get("selected_transitions", 0)),
                    "all_transitions": int(row.get("all_transitions", 0)),
                    "observed_vbos": int(row.get("observed_vbos", 0)),
                    "rate_per_1000": round(float(row.get("rate_per_1000", 0)), 2),
                    "share_pct": round(float(row.get("share_of_area_transitions_pct", 0)), 2),
                    "metric_label": metric_label,
                    "metric_value": round(value, 2),
                    "fill_color": color_from_count(value, max_value),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def render_transition_choropleth(feature_collection, title, key):
    """Render a transition choropleth with counts, rates and shares in the tooltip."""
    if not feature_collection.get("features"):
        st.info("No polygons are available for this transition selection.")
        return

    layer = pdk.Layer(
        "GeoJsonLayer",
        data=feature_collection,
        pickable=True,
        auto_highlight=True,
        stroked=True,
        filled=True,
        get_fill_color="properties.fill_color",
        get_line_color=[70, 70, 70, 150],
        line_width_min_pixels=1,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=derive_map_view_state(feature_collection),
        tooltip={
            "html": """
                <b>{display_name}</b><br/>
                Municipality: {municipality}<br/>
                Selected transitions: {selected_transitions}<br/>
                All transitions: {all_transitions}<br/>
                Observed VBOs: {observed_vbos}<br/>
                Rate / 1,000 VBOs: {rate_per_1000}<br/>
                Share of area transitions: {share_pct}%<br/>
                <b>{metric_label}: {metric_value}</b>
            """,
            "style": {"backgroundColor": "white", "color": "black"},
        },
        map_style=None,
    )
    st.markdown(f"#### {title}")
    st.pydeck_chart(deck, use_container_width=True, key=key)


def render_transition_point_map(events, title, key):
    """Render individual VBO transition locations for object-level inspection.

    PyDeck serializes every value supplied to a layer. Pandas nullable scalars
    (especially ``pd.NA`` and nullable ``Int64`` values) are not JSON serializable
    in several PyDeck/Streamlit versions and can trigger:
    ``TypeError: vars() argument must have __dict__ attribute``.

    To avoid that version-dependent failure, this function sends PyDeck a small
    list of plain Python dictionaries rather than the full pandas DataFrame.
    """
    points = events.copy()
    points["lon"] = pd.to_numeric(points.get("lon"), errors="coerce")
    points["lat"] = pd.to_numeric(points.get("lat"), errors="coerce")
    points = points.dropna(subset=["lon", "lat"])

    if points.empty:
        st.info("No transition point geometries are available for this selection.")
        return

    # Only keep fields used by the map. Cast all tooltip values to regular
    # strings so pandas Timestamp, pd.NA and nullable integer objects never
    # enter PyDeck's JSON serializer.
    points["vbo_id"] = points.get("vbo_id", "").fillna("").astype(str)
    points["transition_label"] = (
        points.get("transition_label", "Unknown transition")
        .fillna("Unknown transition")
        .astype(str)
    )
    points["transition_date_label"] = (
        points.get("transition_date", "").fillna("").astype(str)
    )
    points["wijknaam"] = points.get("wijknaam", "Onbekende wijk").fillna(
        "Onbekende wijk"
    ).astype(str)
    points["buurtnaam"] = points.get("buurtnaam", "Onbekende buurt").fillna(
        "Onbekende buurt"
    ).astype(str)

    top_pairs = points["transition_label"].value_counts().head(5).index.tolist()
    palette = [
        [37, 99, 235, 190],
        [234, 88, 12, 190],
        [5, 150, 105, 190],
        [124, 58, 237, 190],
        [202, 138, 4, 190],
    ]
    color_map = {pair: palette[idx] for idx, pair in enumerate(top_pairs)}

    map_records = []
    for row in points.itertuples(index=False):
        color = color_map.get(str(row.transition_label), [100, 116, 139, 150])
        map_records.append(
            {
                "lon": float(row.lon),
                "lat": float(row.lat),
                "vbo_id": str(row.vbo_id),
                "transition_label": str(row.transition_label),
                "transition_date_label": str(row.transition_date_label),
                "wijknaam": str(row.wijknaam),
                "buurtnaam": str(row.buurtnaam),
                "point_color": [int(channel) for channel in color],
            }
        )

    center_lat = sum(record["lat"] for record in map_records) / len(map_records)
    center_lon = sum(record["lon"] for record in map_records) / len(map_records)

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=map_records,
        get_position="[lon, lat]",
        get_fill_color="point_color",
        get_radius=38,
        radius_min_pixels=3,
        radius_max_pixels=12,
        pickable=True,
        auto_highlight=True,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(
            latitude=float(center_lat),
            longitude=float(center_lon),
            zoom=10.2,
            pitch=0,
        ),
        tooltip={
            "html": """
                <b>{transition_label}</b><br/>
                VBO: {vbo_id}<br/>
                Date: {transition_date_label}<br/>
                Wijk: {wijknaam}<br/>
                Buurt: {buurtnaam}
            """,
            "style": {"backgroundColor": "white", "color": "black"},
        },
        map_style=None,
    )
    st.markdown(f"#### {title}")
    st.pydeck_chart(deck, use_container_width=True, key=key)


def build_animated_transition_map(events, title):
    """Create a year-by-year point animation for selected VBO transitions."""
    points = events.dropna(subset=["lon", "lat", "transition_year"]).copy()
    if points.empty:
        return None
    points["transition_year"] = pd.to_numeric(
        points["transition_year"], errors="coerce"
    ).astype("Int64")
    points = points.dropna(subset=["transition_year"])
    if points.empty:
        return None

    center = {
        "lat": float(pd.to_numeric(points["lat"], errors="coerce").mean()),
        "lon": float(pd.to_numeric(points["lon"], errors="coerce").mean()),
    }
    fig = px.scatter_mapbox(
        points,
        lat="lat",
        lon="lon",
        animation_frame="transition_year",
        animation_group="vbo_id",
        hover_name="transition_label",
        hover_data={
            "vbo_id": True,
            "transition_date": True,
            "wijknaam": True,
            "buurtnaam": True,
            "lat": False,
            "lon": False,
        },
        center=center,
        zoom=9.5,
        height=650,
        title=title,
    )
    fig.update_layout(
        mapbox_style="open-street-map",
        margin={"l": 0, "r": 0, "t": 55, "b": 0},
    )
    fig.update_traces(marker={"size": 10, "opacity": 0.72})
    return fig

def color_from_count(value, max_value):
    if value is None or value == 0 or max_value == 0:
        return [221, 221, 221, 105]

    ratio = value / max_value

    if ratio <= 0.2:
        return [254, 240, 217, 180]
    if ratio <= 0.4:
        return [253, 204, 138, 190]
    if ratio <= 0.6:
        return [252, 141, 89, 200]
    if ratio <= 0.8:
        return [227, 74, 51, 210]
    return [179, 0, 0, 220]


def render_sequential_map_legend(feature_collection, metric_label, property_name):
    """Render an explicit warm low-to-high legend below a PyDeck map."""
    values = []
    for feature in (feature_collection or {}).get("features", []):
        raw_value = feature.get("properties", {}).get(property_name)
        numeric_value = pd.to_numeric(pd.Series([raw_value]), errors="coerce").iloc[0]
        if pd.notna(numeric_value):
            values.append(float(numeric_value))
    if not values:
        return
    minimum, maximum = min(values), max(values)
    number_format = ".1f" if any(not float(value).is_integer() for value in values) else ",.0f"
    st.markdown(
        f"""
        <div style="max-width:560px;margin:-4px 0 18px 0;color:#4B4B4B;font-size:0.88rem;">
          <div style="font-weight:600;margin-bottom:4px;">{metric_label}</div>
          <div style="height:13px;border:1px solid #b8b8b8;border-radius:2px;
                      background:linear-gradient(90deg,#FEF0D9 0%,#FDCC8A 25%,
                      #FC8D59 50%,#E34A33 75%,#B30000 100%);"></div>
          <div style="display:flex;justify-content:space-between;margin-top:2px;">
            <span>Low · {format(minimum, number_format)}</span>
            <span>High · {format(maximum, number_format)}</span>
          </div>
          <div style="margin-top:3px;color:#6b7280;">Grey = zero or unavailable</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def dataframe_to_feature_collection(df):
    """Convert the analytical wijk/buurt dataframe to GeoJSON.

    The generic tooltip fields are also used by the historical-boundary overlay,
    allowing both layer types to be inspected in the same PyDeck map.
    """
    features = []

    for _, row in df.iterrows():
        if row["geojson"] is None:
            continue

        geometry = json.loads(row["geojson"])
        mean_lifespan = (
            round(float(row["mean_lifespan_years"]), 1)
            if pd.notna(row.get("mean_lifespan_years"))
            else "Not available"
        )
        demolished_panden = int(row.get("demolished_panden", 0))

        properties = {
            "gemeentenaam": row.get("gemeentenaam"),
            "unit_code": row.get("unit_code"),
            "unit_name": row.get("unit_name"),
            "demolished_panden": demolished_panden,
            "mean_lifespan_years": mean_lifespan,
            "fill_color": row["fill_color"],
            # Shared inspection-tooltip fields.
            "display_name": row.get("unit_name"),
            "layer_name": "Demolition aggregation",
            "detail_1_label": "Demolished panden",
            "detail_1_value": demolished_panden,
            "detail_2_label": "Mean lifespan",
            "detail_2_value": mean_lifespan,
            "source_label": "BAG demolition sample + current wijk/buurt geometry",
        }

        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": properties,
            }
        )

    return {"type": "FeatureCollection", "features": features}


@st.cache_data
def load_dashboard_municipality_boundaries(selected_municipalities):
    """Load current municipal polygons for the dashboard comparison map."""
    if not selected_municipalities:
        return pd.DataFrame()

    query = text(
        """
        SELECT
            gemeentecode::text AS gemeentecode,
            gemeentenaam,
            ST_AsGeoJSON(
                ST_Transform(
                    ST_MakeValid(ST_Force2D(geom)),
                    4326
                )
            ) AS geojson
        FROM dt_frl_base.vw_gemeente_admin
        WHERE gemeentenaam = ANY(:municipalities)
        ORDER BY gemeentenaam;
        """
    )
    return pd.read_sql(
        query,
        engine,
        params={"municipalities": list(selected_municipalities)},
    )


@st.cache_data
def load_dashboard_historical_municipality_boundaries(
    boundary_year,
    selected_municipalities,
):
    """Load municipality polygons valid in one historical reference year."""
    selected_municipalities = [
        str(value)
        for value in (selected_municipalities or [])
        if str(value).strip()
    ]
    query = text(
        f"""
        SELECT
            LPAD(TRIM(code::text), 4, '0') AS gemeentecode,
            naam AS gemeentenaam,
            jaar AS boundary_year,
            startdatum,
            einddatum,
            geometry_source_year,
            geometry_source_name,
            bron,
            ST_AsGeoJSON(
                ST_Transform(
                    ST_MakeValid(ST_Force2D(shape)),
                    4326
                )
            ) AS geojson
        FROM {HISTORICAL_BOUNDARY_TABLE}
        WHERE jaar = :boundary_year
          AND naam = ANY(:municipalities)
        ORDER BY naam;
        """
    )
    return pd.read_sql(
        query,
        engine,
        params={
            "boundary_year": int(boundary_year),
            "municipalities": list(selected_municipalities),
        },
    )


def _safe_float(value, digits=2):
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric) or not math.isfinite(float(numeric)):
        return None
    return round(float(numeric), digits)


def build_municipality_indicator_summary(
    boundaries,
    demolition_records,
    stock_rows,
    registration_rows,
    replacement_rows,
):
    """Build one transparent indicator row for every selected municipality."""
    if boundaries.empty:
        return pd.DataFrame()

    base = boundaries.copy()

    demolition_summary = (
        demolition_records.groupby("gemeentenaam", dropna=False)
        .agg(
            demolished_panden=("pand_id", "nunique"),
            mean_lifespan_years=("lifespan_years", "mean"),
            median_lifespan_years=("lifespan_years", "median"),
        )
        .reset_index()
        if not demolition_records.empty
        else pd.DataFrame(
            columns=[
                "gemeentenaam",
                "demolished_panden",
                "mean_lifespan_years",
                "median_lifespan_years",
            ]
        )
    )
    base = base.merge(demolition_summary, on="gemeentenaam", how="left")

    if not stock_rows.empty and "current_stock" in stock_rows.columns:
        stock_summary = (
            stock_rows.groupby("gemeentenaam", as_index=False)["current_stock"]
            .sum()
        )
        base = base.merge(stock_summary, on="gemeentenaam", how="left")
    else:
        base["current_stock"] = pd.NA

    if (
        not registration_rows.empty
        and "registration_delay_days" in registration_rows.columns
    ):
        registration_summary = (
            registration_rows.groupby("gemeentenaam", as_index=False)
            ["registration_delay_days"]
            .mean()
            .rename(
                columns={
                    "registration_delay_days": "mean_registration_delay_days"
                }
            )
        )
        base = base.merge(
            registration_summary,
            on="gemeentenaam",
            how="left",
        )
    else:
        base["mean_registration_delay_days"] = pd.NA

    if (
        not replacement_rows.empty
        and "temporal_substitution_flag" in replacement_rows.columns
    ):
        replacement_summary = (
            replacement_rows.groupby("gemeentenaam", as_index=False)
            ["temporal_substitution_flag"]
            .sum()
            .rename(
                columns={
                    "temporal_substitution_flag": "flagged_substitutions"
                }
            )
        )
        base = base.merge(
            replacement_summary,
            on="gemeentenaam",
            how="left",
        )
    else:
        base["flagged_substitutions"] = pd.NA

    base["demolished_panden"] = (
        pd.to_numeric(base["demolished_panden"], errors="coerce")
        .fillna(0)
        .astype(int)
    )
    base["current_stock"] = pd.to_numeric(
        base["current_stock"], errors="coerce"
    )
    base["demolition_rate_per_1000"] = base.apply(
        lambda row: (
            row["demolished_panden"] / row["current_stock"] * 1000
            if pd.notna(row["current_stock"]) and row["current_stock"] > 0
            else pd.NA
        ),
        axis=1,
    )
    base["flagged_substitutions"] = pd.to_numeric(
        base["flagged_substitutions"], errors="coerce"
    )
    base["temporal_substitution_share_pct"] = base.apply(
        lambda row: (
            row["flagged_substitutions"]
            / row["demolished_panden"]
            * 100
            if pd.notna(row["flagged_substitutions"])
            and row["demolished_panden"] > 0
            else pd.NA
        ),
        axis=1,
    )
    return base


def municipality_summary_to_feature_collection(
    summary_df,
    metric_column,
    metric_label,
):
    """Convert municipality indicator rows to a PyDeck GeoJSON collection."""
    if summary_df.empty:
        return {"type": "FeatureCollection", "features": []}

    numeric_values = pd.to_numeric(
        summary_df[metric_column], errors="coerce"
    )
    max_value = float(numeric_values.max()) if numeric_values.notna().any() else 0.0

    features = []
    for _, row in summary_df.iterrows():
        raw_geometry = row.get("geojson")
        if raw_geometry is None:
            continue
        geometry = (
            json.loads(raw_geometry)
            if isinstance(raw_geometry, str)
            else raw_geometry
        )
        metric_value = _safe_float(row.get(metric_column))
        historical_stock = row.get("stock_at_start_of_year")
        current_stock = row.get("current_stock")
        stock_value = (
            int(historical_stock)
            if pd.notna(historical_stock)
            else (
                int(current_stock)
                if pd.notna(current_stock)
                else "N/A"
            )
        )
        stock_label = (
            "Stock at 1 January"
            if pd.notna(historical_stock)
            else "Current stock"
        )
        historical_rate = row.get(
            "demolition_rate_per_1000_start_stock"
        )
        current_rate = row.get("demolition_rate_per_1000")
        rate_value = (
            _safe_float(historical_rate)
            if pd.notna(historical_rate)
            else (
                _safe_float(current_rate)
                if pd.notna(current_rate)
                else "N/A"
            )
        )
        rate_label = (
            "Annual rate / 1,000 start stock"
            if pd.notna(historical_rate)
            else "Cumulative intensity / 1,000 current stock"
        )
        fill_color = (
            color_from_count(metric_value, max_value)
            if metric_value is not None
            else [210, 210, 210, 95]
        )

        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "municipality": str(row.get("gemeentenaam") or "Unknown"),
                    "metric_label": metric_label,
                    "metric_value": metric_value if metric_value is not None else "N/A",
                    "demolished_panden": int(row.get("demolished_panden", 0) or 0),
                    "stock_label": stock_label,
                    "stock_value": stock_value,
                    "rate_label": rate_label,
                    "demolition_rate": rate_value,
                    "mean_lifespan": (
                        _safe_float(row.get("mean_lifespan_years"), 1)
                        if pd.notna(row.get("mean_lifespan_years"))
                        else "N/A"
                    ),
                    "median_lifespan": (
                        _safe_float(row.get("median_lifespan_years"), 1)
                        if pd.notna(row.get("median_lifespan_years"))
                        else "N/A"
                    ),
                    "mean_registration_delay": (
                        _safe_float(row.get("mean_registration_delay_days"), 1)
                        if pd.notna(row.get("mean_registration_delay_days"))
                        else "N/A"
                    ),
                    "substitution_share": (
                        _safe_float(row.get("temporal_substitution_share_pct"), 1)
                        if pd.notna(row.get("temporal_substitution_share_pct"))
                        else "N/A"
                    ),
                    "fill_color": fill_color,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def render_municipality_indicator_map(
    feature_collection,
    metric_label,
    key,
):
    if not feature_collection.get("features"):
        st.info("No municipality polygons are available for this scope.")
        return

    layer = pdk.Layer(
        "GeoJsonLayer",
        data=feature_collection,
        pickable=True,
        auto_highlight=True,
        stroked=True,
        filled=True,
        get_fill_color="properties.fill_color",
        get_line_color=[70, 70, 70, 180],
        line_width_min_pixels=1,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=derive_map_view_state(feature_collection),
        tooltip={
            "html": """
                <b>{municipality}</b><br/>
                <b>{metric_label}: {metric_value}</b><br/>
                Demolished panden: {demolished_panden}<br/>
                {stock_label}: {stock_value}<br/>
                {rate_label}: {demolition_rate}<br/>
                Mean lifespan: {mean_lifespan} years<br/>
                Median lifespan: {median_lifespan} years<br/>
                Mean registration delay: {mean_registration_delay} days<br/>
                Temporal substitution share: {substitution_share}%
            """,
            "style": {"backgroundColor": "white", "color": "black"},
        },
        map_style=None,
    )
    st.pydeck_chart(deck, use_container_width=True, key=key)
    render_sequential_map_legend(
        feature_collection,
        metric_label,
        "metric_value",
    )


@st.cache_data
def load_historical_municipality_evolution(selected_current_municipalities):
    """Load historical boundaries contributing to the selected current municipalities.

    The municipality names selected in the analysis are first resolved to current CBS
    codes. Those codes are then used to obtain the latest footprints from the same
    harmonised history table. Historical polygons are included when they overlap one
    or more selected latest footprints by more than 10,000 m².
    """
    selected_current_municipalities = [
        str(value).strip()
        for value in (selected_current_municipalities or [])
        if str(value).strip()
    ]
    if not selected_current_municipalities:
        return pd.DataFrame()

    query = text(
        f"""
        WITH latest_year AS (
            SELECT MAX(jaar)::integer AS jaar
            FROM {HISTORICAL_BOUNDARY_TABLE}
        ),
        selected_codes AS (
            SELECT
                g.gemeentenaam AS target_current_municipality,
                LPAD(TRIM(g.gemeentecode::text), 4, '0') AS code
            FROM dt_frl_base.vw_gemeente_admin g
            WHERE g.gemeentenaam = ANY(:selected_current_municipalities)
        ),
        selected_current AS (
            SELECT
                s.target_current_municipality,
                ST_MakeValid(ST_Force2D(h.shape)) AS geom
            FROM selected_codes s
            JOIN {HISTORICAL_BOUNDARY_TABLE} h
              ON LPAD(TRIM(h.code::text), 4, '0') = s.code
            CROSS JOIN latest_year ly
            WHERE h.jaar = ly.jaar
        ),
        scoped AS (
            SELECT
                h.identifica,
                h.naam,
                h.code,
                h.status,
                h.jaar,
                COALESCE(
                    h.startdatum,
                    MAKE_DATE(h.jaar, 1, 1)::timestamp
                ) AS startdatum,
                COALESCE(
                    h.einddatum,
                    MAKE_DATE(h.jaar, 12, 31)::timestamp
                ) AS einddatum,
                h.geometry_source_year,
                h.geometry_source_name,
                h.bron,
                h.source_url,
                h.shape,
                overlap_summary.overlap_area_m2,
                overlap_summary.contributes_to
            FROM {HISTORICAL_BOUNDARY_TABLE} h
            JOIN LATERAL (
                SELECT
                    SUM(
                        ST_Area(
                            ST_Intersection(
                                ST_MakeValid(ST_Force2D(h.shape)),
                                c.geom
                            )
                        )
                    ) AS overlap_area_m2,
                    STRING_AGG(
                        DISTINCT c.target_current_municipality,
                        ' + '
                        ORDER BY c.target_current_municipality
                    ) AS contributes_to
                FROM selected_current c
                WHERE ST_MakeValid(ST_Force2D(h.shape)) && c.geom
                  AND ST_Intersects(
                        ST_MakeValid(ST_Force2D(h.shape)),
                        c.geom
                  )
            ) overlap_summary
              ON overlap_summary.overlap_area_m2 > 10000
            WHERE h.jaar BETWEEN 2011 AND 2026
        )
        SELECT
            identifica,
            naam,
            code,
            status,
            jaar,
            startdatum,
            einddatum,
            geometry_source_year,
            geometry_source_name,
            bron,
            source_url,
            overlap_area_m2,
            contributes_to,
            ST_AsGeoJSON(
                ST_Transform(
                    ST_MakeValid(ST_Force2D(shape)),
                    4326
                )
            ) AS geojson
        FROM scoped
        ORDER BY startdatum, contributes_to, naam;
        """
    )

    return pd.read_sql(
        query,
        engine,
        params={
            "selected_current_municipalities": selected_current_municipalities,
        },
    )

@st.cache_data
def load_current_municipality_boundaries(focus_municipalities):
    """Load latest selected footprints from the harmonised history table by CBS code."""
    focus_municipalities = [
        str(value).strip()
        for value in (focus_municipalities or [])
        if str(value).strip()
    ]
    if not focus_municipalities:
        return pd.DataFrame()

    query = text(
        f"""
        WITH latest_year AS (
            SELECT MAX(jaar)::integer AS jaar
            FROM {HISTORICAL_BOUNDARY_TABLE}
        ),
        selected_codes AS (
            SELECT
                g.gemeentenaam,
                LPAD(TRIM(g.gemeentecode::text), 4, '0') AS code
            FROM dt_frl_base.vw_gemeente_admin g
            WHERE g.gemeentenaam = ANY(:focus_municipalities)
        )
        SELECT
            h.code,
            s.gemeentenaam AS naam,
            ST_AsGeoJSON(
                ST_Transform(
                    ST_MakeValid(ST_Force2D(h.shape)),
                    4326
                )
            ) AS geojson
        FROM selected_codes s
        JOIN {HISTORICAL_BOUNDARY_TABLE} h
          ON LPAD(TRIM(h.code::text), 4, '0') = s.code
        CROSS JOIN latest_year ly
        WHERE h.jaar = ly.jaar
        ORDER BY s.gemeentenaam;
        """
    )

    return pd.read_sql(
        query,
        engine,
        params={"focus_municipalities": focus_municipalities},
    )

def current_boundaries_to_feature_collection(df):
    features = []
    for _, row in df.iterrows():
        raw_geometry = row.get("geojson")
        if raw_geometry is None:
            continue
        geometry = json.loads(raw_geometry) if isinstance(raw_geometry, str) else raw_geometry
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "display_name": row.get("naam"),
                    "layer_name": "Current inspection footprint",
                    "detail_1_label": "Reference geography",
                    "detail_1_value": "Current municipality boundary",
                    "detail_2_label": "Municipality code",
                    "detail_2_value": row.get("code"),
                    "source_label": "Harmonised municipality history table (latest year)",
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def historical_boundaries_to_feature_collection(df):
    """Convert all historical municipality intervals to temporal GeoJSON.

    Each feature preserves its validity interval. The browser-side map filters these
    features by ``startdatum`` and ``einddatum`` without rerunning Streamlit.
    """
    features = []

    for _, row in df.iterrows():
        raw_geometry = row.get("geojson")
        if raw_geometry is None:
            continue

        geometry = (
            json.loads(raw_geometry)
            if isinstance(raw_geometry, str)
            else raw_geometry
        )

        analysis_year = int(row["jaar"])
        geometry_source_year = (
            int(row["geometry_source_year"])
            if pd.notna(row.get("geometry_source_year"))
            else None
        )

        start_value = pd.to_datetime(row.get("startdatum"), errors="coerce")
        end_value = pd.to_datetime(row.get("einddatum"), errors="coerce")
        start_iso = (
            start_value.date().isoformat()
            if pd.notna(start_value)
            else f"{analysis_year}-01-01"
        )
        end_iso = (
            end_value.date().isoformat()
            if pd.notna(end_value)
            else f"{analysis_year}-12-31"
        )

        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "display_name": row.get("naam"),
                    "municipality_code": row.get("code"),
                    "boundary_status": row.get("status"),
                    "analysis_year": analysis_year,
                    "startdatum": start_iso,
                    "einddatum": end_iso,
                    "geometry_source_year": geometry_source_year,
                    "geometry_source_name": row.get("geometry_source_name"),
                    "contributes_to": (
                        row.get("contributes_to")
                        or "Selected analysis area"
                    ),
                    "source_label": row.get("bron") or "Unknown source",
                    "source_url": row.get("source_url"),
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}

@st.cache_data
def load_aggregated_geojson(level, selected_municipalities, start_year, end_year):
    if level == "wijk":
        query = text(
            f"""
            WITH counts AS (
                SELECT
                    current_gemeentecode AS gemeentecode,
                    wijkcode,
                    COUNT(*) AS demolished_panden,
                    ROUND(
                        AVG((demolition_year - bouwjaar)::numeric)
                        FILTER (
                            WHERE bouwjaar >= 1
                              AND bouwjaar < demolition_year
                              AND (demolition_year - bouwjaar) > 0
                              AND (demolition_year - bouwjaar) <= demolition_year
                        ),
                        1
                    ) AS mean_lifespan_years
                FROM {DEMOLITION_RECORDS_TABLE}
                WHERE current_gemeentenaam = ANY(:municipalities)
                  AND demolition_year BETWEEN :start_year AND :end_year
                GROUP BY current_gemeentecode, wijkcode
            )
            SELECT
                a.gemeentenaam,
                w.wijkcode AS unit_code,
                w.wijknaam AS unit_name,
                COALESCE(c.demolished_panden, 0) AS demolished_panden,
                c.mean_lifespan_years,
                ST_AsGeoJSON(ST_Transform(w.geom, 4326)) AS geojson
            FROM dt_frl_base.vw_wijken_admin w
            JOIN dt_frl_base.vw_gemeente_admin a
              ON a.gemeentecode::text = w.gemeentecode::text
            LEFT JOIN counts c
              ON c.gemeentecode::text = w.gemeentecode::text
             AND c.wijkcode = w.wijkcode
            WHERE a.gemeentenaam = ANY(:municipalities)
            ORDER BY a.gemeentenaam, w.wijknaam;
            """
        )
    else:
        query = text(
            f"""
            WITH counts AS (
                SELECT
                    current_gemeentecode AS gemeentecode,
                    buurtcode,
                    COUNT(*) AS demolished_panden,
                    ROUND(
                        AVG((demolition_year - bouwjaar)::numeric)
                        FILTER (
                            WHERE bouwjaar >= 1
                              AND bouwjaar < demolition_year
                              AND (demolition_year - bouwjaar) > 0
                              AND (demolition_year - bouwjaar) <= demolition_year
                        ),
                        1
                    ) AS mean_lifespan_years
                FROM {DEMOLITION_RECORDS_TABLE}
                WHERE current_gemeentenaam = ANY(:municipalities)
                  AND demolition_year BETWEEN :start_year AND :end_year
                GROUP BY current_gemeentecode, buurtcode
            )
            SELECT
                a.gemeentenaam,
                b.buurtcode AS unit_code,
                b.buurtnaam AS unit_name,
                COALESCE(c.demolished_panden, 0) AS demolished_panden,
                c.mean_lifespan_years,
                ST_AsGeoJSON(ST_Transform(b.geom, 4326)) AS geojson
            FROM dt_frl_base.vw_buurten_admin b
            JOIN dt_frl_base.vw_gemeente_admin a
              ON a.gemeentecode::text = b.gemeentecode::text
            LEFT JOIN counts c
              ON c.gemeentecode::text = b.gemeentecode::text
             AND c.buurtcode = b.buurtcode
            WHERE a.gemeentenaam = ANY(:municipalities)
            ORDER BY a.gemeentenaam, b.buurtnaam;
            """
        )

    df = pd.read_sql(
        query,
        engine,
        params={
            "municipalities": list(selected_municipalities),
            "start_year": start_year,
            "end_year": end_year,
        },
    )

    max_count = df["demolished_panden"].max() if not df.empty else 0
    df["fill_color"] = df["demolished_panden"].apply(
        lambda value: color_from_count(value, max_count)
    )
    return df


def _walk_geojson_coordinates(value):
    """Yield lon/lat pairs from nested GeoJSON coordinate arrays."""
    if not isinstance(value, (list, tuple)):
        return
    if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
        yield float(value[0]), float(value[1])
        return
    for child in value:
        yield from _walk_geojson_coordinates(child)


def derive_map_view_state(*feature_collections):
    """Approximate a useful PyDeck center and zoom from one or more collections."""
    coordinates = []
    for collection in feature_collections:
        if not collection:
            continue
        for feature in collection.get("features", []):
            geometry = feature.get("geometry") or {}
            coordinates.extend(_walk_geojson_coordinates(geometry.get("coordinates")))

    if not coordinates:
        return pdk.ViewState(latitude=53.05, longitude=5.65, zoom=8.3, pitch=0)

    lons = [p[0] for p in coordinates]
    lats = [p[1] for p in coordinates]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2

    # Longitude and latitude have different visual scales in Fryslân. The factor
    # below gives a practical fit for municipality-sized AOIs without a JS fitBounds.
    span = max(max_lon - min_lon, (max_lat - min_lat) * 1.7, 0.01)
    zoom = max(7.0, min(11.5, math.log2(360.0 / span) - 1.25))
    return pdk.ViewState(
        latitude=center_lat,
        longitude=center_lon,
        zoom=zoom,
        pitch=0,
    )


def make_geojson_map(
    feature_collection,
    title,
    map_key,
    historical_boundary_collection=None,
    historical_boundary_year=None,
    current_focus_collection=None,
):
    """Render an analytical map with scoped historical and current outlines."""
    analysis_layer = pdk.Layer(
        "GeoJsonLayer",
        id=f"analysis-{map_key}",
        data=feature_collection,
        pickable=True,
        auto_highlight=True,
        stroked=True,
        filled=True,
        get_fill_color="properties.fill_color",
        get_line_color=[80, 80, 80, 140],
        line_width_min_pixels=1,
    )

    layers = [analysis_layer]

    if historical_boundary_collection:
        boundary_features = historical_boundary_collection.get("features", [])
        if boundary_features:
            layers.append(
                pdk.Layer(
                    "GeoJsonLayer",
                    id=f"historical-boundaries-{map_key}-{historical_boundary_year}",
                    data=historical_boundary_collection,
                    pickable=True,
                    auto_highlight=True,
                    stroked=True,
                    filled=False,
                    get_line_color=[24, 24, 27, 235],
                    line_width_min_pixels=2,
                    highlight_color=[255, 255, 255, 220],
                )
            )

    if current_focus_collection:
        focus_features = current_focus_collection.get("features", [])
        if focus_features:
            layers.append(
                pdk.Layer(
                    "GeoJsonLayer",
                    id=f"current-focus-{map_key}",
                    data=current_focus_collection,
                    pickable=True,
                    auto_highlight=True,
                    stroked=True,
                    filled=False,
                    get_line_color=[37, 99, 235, 245],
                    line_width_min_pixels=4,
                    highlight_color=[147, 197, 253, 230],
                )
            )

    # Keep the map extent fixed while the time slider changes. Historical
    # predecessor polygons may extend beyond the current boundary, but they should not
    # make the map jump or zoom during playback.
    view_state = derive_map_view_state(
        current_focus_collection,
        feature_collection,
    )

    tooltip = {
        "html": """
        <b>{display_name}</b><br/>
        Layer: {layer_name}<br/>
        {detail_1_label}: {detail_1_value}<br/>
        {detail_2_label}: {detail_2_value}<br/>
        Source: {source_label}
        """,
        "style": {"backgroundColor": "white", "color": "black"},
    }

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        tooltip=tooltip,
        map_style=None,
    )

    st.markdown(f"### {title}")
    st.pydeck_chart(
        deck,
        use_container_width=True,
        key=f"{map_key}_pydeck",
    )
    legend_property = (
        "mean_lifespan_years"
        if "lifespan" in title.lower()
        else "demolished_panden"
    )
    legend_label = (
        "Mean lifespan at demolition (years)"
        if legend_property == "mean_lifespan_years"
        else "Demolished buildings"
    )
    render_sequential_map_legend(
        feature_collection,
        legend_label,
        legend_property,
    )


@st.cache_data
def load_validation_counts(selected_municipalities, start_year, end_year):
    """Load database validation counts and add app-level bouwjaar checks."""
    frames = []
    if relation_exists("thesis_ch4.vw_validation_issues"):
        query = text(
            """
            SELECT
                issue_type,
                COUNT(*) AS affected_records
            FROM thesis_ch4.vw_validation_issues
            WHERE (
                    scope = 'source_global'
                    OR gemeentenaam = ANY(:municipalities)
                  )
              AND (
                    demolition_year IS NULL
                    OR demolition_year BETWEEN :start_year AND :end_year
                  )
            GROUP BY issue_type;
            """
        )
        try:
            database_counts = pd.read_sql(
                query,
                engine,
                params={
                    "municipalities": list(selected_municipalities),
                    "start_year": int(start_year),
                    "end_year": int(end_year),
                },
            )
            database_counts = database_counts[
                ~database_counts["issue_type"].isin(
                    ["MISSING_BOUWJAAR", "INVALID_BOUWJAAR"]
                )
            ]
            frames.append(database_counts)
        except ProgrammingError:
            pass

    _, current_name_column = analysis_municipality_columns(
        CURRENT_GEOGRAPHY_MODE
    )
    age_quality_query = text(
        f"""
        SELECT
            CASE
                WHEN bouwjaar IS NULL THEN 'MISSING_BOUWJAAR'
                ELSE 'INVALID_BOUWJAAR'
            END::text AS issue_type,
            COUNT(*)::bigint AS affected_records
        FROM {DEMOLITION_RECORDS_TABLE}
        WHERE {current_name_column} = ANY(:municipalities)
          AND demolition_year BETWEEN :start_year AND :end_year
          AND (
                bouwjaar IS NULL
                OR bouwjaar < 1
                OR bouwjaar >= demolition_year
                OR bouwjaar > {ANALYSIS_END_YEAR}
              )
        GROUP BY 1;
        """
    )
    try:
        frames.append(
            pd.read_sql(
                age_quality_query,
                engine,
                params={
                    "municipalities": list(selected_municipalities),
                    "start_year": int(start_year),
                    "end_year": int(end_year),
                },
            )
        )
    except ProgrammingError:
        pass

    frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=["issue_type", "affected_records"])
    return (
        pd.concat(frames, ignore_index=True)
        .groupby("issue_type", as_index=False)["affected_records"]
        .sum()
    )


def build_validation_summary(counts):
    catalog = pd.DataFrame(VALIDATION_CATALOG)
    if counts.empty:
        catalog["affected_records"] = 0
    else:
        catalog = catalog.merge(counts, on="issue_type", how="left")
        catalog["affected_records"] = (
            catalog["affected_records"].fillna(0).astype(int)
        )

    return catalog[
        [
            "test",
            "stage",
            "severity",
            "affected_records",
            "meaning",
            "issue_type",
        ]
    ]


@st.cache_data
def load_validation_issues(
    issue_type,
    selected_municipalities,
    start_year,
    end_year,
    search_text,
    limit,
):
    if issue_type in {"MISSING_BOUWJAAR", "INVALID_BOUWJAAR"}:
        _, current_name_column = analysis_municipality_columns(
            CURRENT_GEOGRAPHY_MODE
        )
        if issue_type == "MISSING_BOUWJAAR":
            issue_condition = "bouwjaar IS NULL"
            issue_test = "Missing construction year"
            detail_expression = (
                "'No bouwjaar is available; the record cannot be assigned to a "
                "construction period.'"
            )
            local_expression = "NULL::text"
        else:
            issue_condition = f"""bouwjaar IS NOT NULL AND (
                bouwjaar < 1
                OR bouwjaar >= demolition_year
                OR bouwjaar > {ANALYSIS_END_YEAR}
            )"""
            issue_test = "Invalid construction year"
            detail_expression = """CONCAT(
                'Invalid bouwjaar ', bouwjaar,
                '; expected 1 <= bouwjaar < demolition_year'
            )"""
            local_expression = "bouwjaar::text"

        query = text(
            f"""
            SELECT
                CAST(:issue_type AS text) AS issue_type,
                'Excluded from age analysis'::text AS severity,
                'Construction-cohort filter'::text AS stage,
                'selected_scope'::text AS scope,
                pand_id::text AS pand_id,
                {current_name_column} AS gemeentenaam,
                demolition_date,
                demolition_year,
                {detail_expression} AS detail,
                {local_expression} AS local_value,
                demolition_year::text AS comparison_value,
                ST_X(ST_Transform(point_rd, 4326)) AS lon,
                ST_Y(ST_Transform(point_rd, 4326)) AS lat
            FROM {DEMOLITION_RECORDS_TABLE}
            WHERE {current_name_column} = ANY(:municipalities)
              AND demolition_year BETWEEN :start_year AND :end_year
              AND {issue_condition}
              AND (
                    :search_text = ''
                    OR pand_id::text ILIKE '%' || :search_text || '%'
                    OR COALESCE(bouwjaar::text, '') ILIKE '%' || :search_text || '%'
                    OR LOWER(CAST(:issue_test AS text)) ILIKE '%' || LOWER(:search_text) || '%'
                  )
            ORDER BY {current_name_column}, demolition_year, pand_id
            LIMIT :row_limit;
            """
        )
        try:
            return pd.read_sql(
                query,
                engine,
                params={
                    "issue_type": issue_type,
                    "issue_test": issue_test,
                    "municipalities": list(selected_municipalities),
                    "start_year": int(start_year),
                    "end_year": int(end_year),
                    "search_text": search_text,
                    "row_limit": int(limit),
                },
            )
        except ProgrammingError:
            return pd.DataFrame()

    if not relation_exists("thesis_ch4.vw_validation_issues"):
        return pd.DataFrame()

    query = text(
        """
        SELECT
            issue_type,
            severity,
            stage,
            scope,
            pand_id,
            gemeentenaam,
            demolition_date,
            demolition_year,
            detail,
            local_value,
            comparison_value,
            lon,
            lat
        FROM thesis_ch4.vw_validation_issues
        WHERE issue_type = :issue_type
          AND (
                scope = 'source_global'
                OR gemeentenaam = ANY(:municipalities)
              )
          AND (
                demolition_year IS NULL
                OR demolition_year BETWEEN :start_year AND :end_year
              )
          AND (
                :search_text = ''
                OR pand_id ILIKE '%' || :search_text || '%'
                OR COALESCE(detail, '') ILIKE '%' || :search_text || '%'
              )
        ORDER BY
            gemeentenaam NULLS LAST,
            demolition_year NULLS LAST,
            pand_id
        LIMIT :row_limit;
        """
    )

    try:
        df = pd.read_sql(
            query,
            engine,
            params={
                "issue_type": issue_type,
                "municipalities": list(selected_municipalities),
                "start_year": start_year,
                "end_year": end_year,
                "search_text": search_text.strip(),
                "row_limit": int(limit),
            },
        )
    except ProgrammingError:
        return pd.DataFrame()

    if not df.empty:
        df["bag_viewer"] = df["pand_id"].apply(bag_viewer_url)

    return df


@st.cache_data
def load_pand_history(pand_id):
    if not relation_exists("thesis_ch4.vw_pand_status_history"):
        return pd.DataFrame()

    query = text(
        """
        SELECT
            sequence_no,
            total_versions,
            pand_id,
            bouwjaar,
            pandstatus,
            status_date,
            documentdatum,
            documentnummer,
            voorkomenidentificatie,
            begindatumtijdvakgeldigheid,
            einddatumtijdvakgeldigheid,
            tijdstipregistratie,
            eindregistratie,
            previous_status,
            next_status,
            is_demolition_transition,
            is_reversal_after_demolition,
            is_terminal_version,
            has_valid_geometry,
            geometry_area_m2
        FROM thesis_ch4.vw_pand_status_history
        WHERE pand_id = :pand_id
        ORDER BY sequence_no;
        """
    )
    try:
        return pd.read_sql(
            query,
            engine,
            params={"pand_id": pand_id.strip()},
        )
    except ProgrammingError:
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_live_bag_pand(pand_id):
    api_key = get_bag_api_key()
    if not api_key:
        raise RuntimeError(
            "No BAG API key configured in .streamlit/secrets.toml."
        )

    response = requests.get(
        f"{BAG_API_BASE}/panden/{pand_id}",
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/hal+json",
            "Accept-Crs": "epsg:28992",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


# ============================================================
# Downloadable ten-indicator result packages
# ============================================================

# Fixed export window requested for the thesis hand-in package. These constants
# are intentionally independent of the interactive dashboard filters so the
# same reproducible files are produced from every app session.
EXPORT_START_YEAR = 2011
EXPORT_END_YEAR = 2025

EXPORT_INDICATOR_FOLDERS = {
    1: "01_annual_demolition_count",
    2: "02_demolition_intensity",
    3: "03_building_age_at_demolition",
    4: "04_cohort_specific_loss",
    5: "05_mean_lifespan",
    6: "06_median_lifespan",
    7: "07_functional_demolition_profile",
    8: "08_replacement_lag",
    9: "09_registration_delay",
    10: "10_temporal_substitution_bias",
}


def _export_safe_name(value):
    """Return a Windows-safe folder/file component while retaining Unicode."""
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", str(value or "Unknown"))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned or "Unknown"


def _export_summary_table(items):
    """Create a consistent long-format indicator summary table."""
    rows = []
    for metric, value in items:
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        rows.append({"metric": metric, "value": value})
    return pd.DataFrame(rows, columns=["metric", "value"])


def _export_numeric_value(value, digits=2):
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric) or not math.isfinite(float(numeric)):
        return None
    return round(float(numeric), digits)


def _export_font(size, bold=False):
    """Load a portable chart font, falling back to Pillow's default font."""
    from PIL import ImageFont

    candidates = (
        ["DejaVuSans-Bold.ttf", "arialbd.ttf"]
        if bold
        else ["DejaVuSans.ttf", "arial.ttf"]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _export_plot_png(
    table,
    title,
    x_column,
    y_column,
    chart_type="bar",
    x_label="",
    y_label="",
    note="",
):
    """Render a lightweight PNG chart without requiring Kaleido or a browser.

    Pillow is used instead of Plotly image export because the annual package can
    contain more than one thousand figures. This keeps package generation
    deterministic and avoids a hard dependency on a Chromium/Kaleido runtime.
    """
    from PIL import Image, ImageDraw

    width, height = 1200, 700
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    title_font = _export_font(30, bold=True)
    axis_font = _export_font(19, bold=True)
    label_font = _export_font(16)
    small_font = _export_font(14)

    draw.text((50, 28), str(title), fill=(25, 33, 45), font=title_font)

    left, top, right, bottom = 120, 105, width - 55, height - 125
    draw.rectangle((left, top, right, bottom), outline=(90, 100, 115), width=2)

    frame = table.copy() if isinstance(table, pd.DataFrame) else pd.DataFrame()
    if (
        frame.empty
        or x_column not in frame.columns
        or y_column not in frame.columns
    ):
        draw.text(
            (left + 40, top + 80),
            "No data available for this indicator and scope.",
            fill=(90, 100, 115),
            font=axis_font,
        )
    else:
        labels = frame[x_column].fillna("Unknown").astype(str).tolist()
        values = pd.to_numeric(frame[y_column], errors="coerce").fillna(0.0).tolist()
        values = [float(value) if math.isfinite(float(value)) else 0.0 for value in values]
        maximum = max(values) if values else 0.0

        if maximum <= 0:
            draw.text(
                (left + 40, top + 80),
                "No positive values are available for this indicator and scope.",
                fill=(90, 100, 115),
                font=axis_font,
            )
        else:
            # Horizontal grid and labels.
            for step in range(6):
                ratio = step / 5
                y = bottom - int((bottom - top) * ratio)
                grid_value = maximum * ratio
                draw.line((left, y, right, y), fill=(224, 228, 234), width=1)
                value_text = (
                    f"{grid_value:,.0f}"
                    if maximum >= 10
                    else f"{grid_value:,.2f}"
                )
                draw.text(
                    (left - 88, y - 9),
                    value_text,
                    fill=(70, 78, 90),
                    font=small_font,
                )

            count = max(1, len(values))
            plot_width = right - left
            plot_height = bottom - top
            centers = [
                left + (index + 0.5) * plot_width / count
                for index in range(count)
            ]

            if chart_type == "line":
                points = []
                for center, value in zip(centers, values):
                    y = bottom - (value / maximum) * plot_height
                    points.append((center, y))
                if len(points) > 1:
                    draw.line(points, fill=(45, 93, 140), width=4)
                for point in points:
                    x, y = point
                    draw.ellipse(
                        (x - 6, y - 6, x + 6, y + 6),
                        fill=(45, 93, 140),
                        outline=(25, 60, 95),
                    )
            else:
                slot_width = plot_width / count
                bar_width = max(3, slot_width * 0.68)
                for center, value in zip(centers, values):
                    y = bottom - (value / maximum) * plot_height
                    draw.rectangle(
                        (
                            center - bar_width / 2,
                            y,
                            center + bar_width / 2,
                            bottom,
                        ),
                        fill=(65, 105, 145),
                        outline=(38, 72, 105),
                    )

            # Keep category labels legible in long time series or distributions.
            label_step = max(1, math.ceil(len(labels) / 12))
            for index, (center, label) in enumerate(zip(centers, labels)):
                if index % label_step != 0 and index != len(labels) - 1:
                    continue
                short_label = label if len(label) <= 16 else label[:13] + "…"
                draw.text(
                    (center - 34, bottom + 12),
                    short_label,
                    fill=(55, 62, 72),
                    font=small_font,
                )

    if y_label:
        draw.text((25, top + 10), str(y_label), fill=(45, 52, 62), font=small_font)
    if x_label:
        draw.text(
            ((left + right) / 2 - 50, height - 82),
            str(x_label),
            fill=(45, 52, 62),
            font=axis_font,
        )
    if note:
        draw.text((50, height - 38), str(note), fill=(85, 92, 104), font=small_font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _export_age_distribution(records):
    if "lifespan_years" not in records.columns:
        return (
            pd.DataFrame(columns=["age_band_years", "demolished_panden"]),
            pd.Series(dtype="float64"),
        )
    ages = pd.to_numeric(records["lifespan_years"], errors="coerce").dropna()
    ages = ages[ages >= 0]
    if ages.empty:
        return pd.DataFrame(columns=["age_band_years", "demolished_panden"]), ages

    maximum = max(10, int(math.ceil(float(ages.max()) / 10.0) * 10))
    bins = list(range(0, maximum + 11, 10))
    categories = pd.cut(ages, bins=bins, include_lowest=True, right=False)
    table = (
        categories.value_counts(sort=False)
        .rename_axis("age_band_years")
        .reset_index(name="demolished_panden")
    )
    table["age_band_years"] = table["age_band_years"].astype(str)
    return table, ages


def _export_registration_distribution(registration_rows):
    if "registration_delay_days" not in registration_rows.columns:
        return (
            pd.DataFrame(columns=["delay_band_days", "demolished_panden"]),
            pd.Series(dtype="float64"),
        )
    delays = pd.to_numeric(
        registration_rows["registration_delay_days"],
        errors="coerce",
    ).dropna()
    if delays.empty:
        return pd.DataFrame(columns=["delay_band_days", "demolished_panden"]), delays

    lower = math.floor(float(delays.min()) / 30.0) * 30
    upper = math.ceil(float(delays.max()) / 30.0) * 30
    if lower == upper:
        upper = lower + 30
    bins = list(range(int(lower), int(upper) + 31, 30))
    categories = pd.cut(delays, bins=bins, include_lowest=True, right=False)
    table = (
        categories.value_counts(sort=False)
        .rename_axis("delay_band_days")
        .reset_index(name="demolished_panden")
    )
    table["delay_band_days"] = table["delay_band_days"].astype(str)
    return table, delays


def _export_indicator_outputs(
    records,
    stock_rows,
    functional_rows,
    registration_rows,
    replacement_rows,
    period_start,
    period_end,
    scope_label,
):
    """Calculate the ten primary indicators for one province/municipality scope."""
    years = list(range(int(period_start), int(period_end) + 1))
    records = records.copy()
    stock_rows = stock_rows.copy()
    functional_rows = functional_rows.copy()
    registration_rows = registration_rows.copy()
    replacement_rows = replacement_rows.copy()

    if "demolition_year" in records.columns:
        records["demolition_year"] = pd.to_numeric(
            records["demolition_year"], errors="coerce"
        ).astype("Int64")

    annual = (
        records.groupby("demolition_year")
        .size()
        .reindex(years, fill_value=0)
        .rename("demolished_panden")
        .rename_axis("demolition_year")
        .reset_index()
    )
    total_demolitions = int(annual["demolished_panden"].sum())
    peak_row = annual.sort_values(
        ["demolished_panden", "demolition_year"], ascending=[False, True]
    ).iloc[0]

    outputs = {}
    period_label = (
        str(period_start)
        if int(period_start) == int(period_end)
        else f"{period_start}–{period_end}"
    )

    outputs[1] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("total_demolished_panden", total_demolitions),
                ("mean_demolitions_per_year", _export_numeric_value(annual["demolished_panden"].mean(), 2)),
                ("peak_year", int(peak_row["demolition_year"])),
                ("peak_year_demolitions", int(peak_row["demolished_panden"])),
            ]
        ),
        "table": annual,
        "plot": _export_plot_png(
            annual,
            f"1. Annual demolition count — {scope_label} ({period_label})",
            "demolition_year",
            "demolished_panden",
            chart_type="bar",
            x_label="Demolition year",
            y_label="Demolished buildings",
        ),
    }

    current_stock = None
    if not stock_rows.empty and "current_stock" in stock_rows.columns:
        stock_values = pd.to_numeric(stock_rows["current_stock"], errors="coerce")
        if stock_values.notna().any():
            current_stock = float(stock_values.fillna(0).sum())

    rate_table = annual.copy()
    rate_table["current_stock_panden"] = current_stock
    if current_stock and current_stock > 0:
        rate_table["intensity_per_1000_current_panden"] = (
            rate_table["demolished_panden"] / current_stock * 1000
        )
        cumulative_intensity = total_demolitions / current_stock * 1000
    else:
        rate_table["intensity_per_1000_current_panden"] = pd.NA
        cumulative_intensity = None

    outputs[2] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("total_demolished_panden", total_demolitions),
                ("current_stock_panden", int(current_stock) if current_stock is not None else None),
                ("cumulative_intensity_per_1000_current_panden", _export_numeric_value(cumulative_intensity, 3)),
                ("denominator_note", "Current-reference BAG pand stock; this is an intensity, not a reconstructed historical stock rate."),
            ]
        ),
        "table": rate_table,
        "plot": _export_plot_png(
            rate_table,
            f"2. Demolition intensity — {scope_label} ({period_label})",
            "demolition_year",
            "intensity_per_1000_current_panden",
            chart_type="line",
            x_label="Demolition year",
            y_label="Per 1,000 current panden",
            note="Current-reference denominator; interpret as intensity rather than a historical annual rate.",
        ),
    }

    age_table, ages = _export_age_distribution(records)
    outputs[3] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("valid_lifespan_records", int(len(ages))),
                ("mean_age_years", _export_numeric_value(ages.mean(), 2) if not ages.empty else None),
                ("median_age_years", _export_numeric_value(ages.median(), 2) if not ages.empty else None),
                ("minimum_age_years", _export_numeric_value(ages.min(), 0) if not ages.empty else None),
                ("maximum_age_years", _export_numeric_value(ages.max(), 0) if not ages.empty else None),
                ("standard_deviation_years", _export_numeric_value(ages.std(ddof=1), 2) if len(ages) > 1 else None),
            ]
        ),
        "table": age_table,
        "plot": _export_plot_png(
            age_table,
            f"3. Building age at demolition — {scope_label} ({period_label})",
            "age_band_years",
            "demolished_panden",
            chart_type="bar",
            x_label="Lifespan band (years)",
            y_label="Demolished panden",
        ),
    }

    cohort_source = add_construction_cohort(records)
    cohort_source = cohort_source.dropna(
        subset=["construction_cohort_order", "construction_cohort_label"]
    )
    if not cohort_source.empty:
        cohort_table = (
            cohort_source.groupby(
                ["construction_cohort_order", "construction_cohort_label"],
                as_index=False,
            )
            .size()
            .rename(columns={"size": "demolished_panden"})
            .sort_values("construction_cohort_order")
            .rename(columns={"construction_cohort_label": "construction_cohort"})
        )
    else:
        cohort_table = pd.DataFrame(
            columns=[
                "construction_cohort_order",
                "construction_cohort",
                "demolished_panden",
            ]
        )
    top_cohort = (
        cohort_table.sort_values("demolished_panden", ascending=False).iloc[0]
        if not cohort_table.empty
        else None
    )
    outputs[4] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("construction_period_categories", int(len(cohort_table))),
                ("largest_loss_construction_period", top_cohort["construction_cohort"] if top_cohort is not None else None),
                ("largest_loss_construction_period_count", int(top_cohort["demolished_panden"]) if top_cohort is not None else None),
            ]
        ),
        "table": cohort_table,
        "plot": _export_plot_png(
            cohort_table,
            f"4. Cohort-specific loss — {scope_label} ({period_label})",
            "construction_cohort",
            "demolished_panden",
            chart_type="bar",
            x_label="Construction period",
            y_label="Demolished panden",
        ),
    }

    lifespan_by_year = records.copy()
    lifespan_by_year["lifespan_years"] = pd.to_numeric(
        lifespan_by_year.get("lifespan_years"), errors="coerce"
    )
    mean_table = (
        lifespan_by_year.groupby("demolition_year")["lifespan_years"]
        .mean()
        .reindex(years)
        .rename("mean_lifespan_years")
        .rename_axis("demolition_year")
        .reset_index()
    )
    outputs[5] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("valid_lifespan_records", int(lifespan_by_year["lifespan_years"].notna().sum())),
                ("mean_lifespan_years", _export_numeric_value(lifespan_by_year["lifespan_years"].mean(), 2)),
            ]
        ),
        "table": mean_table,
        "plot": _export_plot_png(
            mean_table,
            f"5. Mean lifespan — {scope_label} ({period_label})",
            "demolition_year",
            "mean_lifespan_years",
            chart_type="line",
            x_label="Demolition year",
            y_label="Mean lifespan (years)",
        ),
    }

    median_table = (
        lifespan_by_year.groupby("demolition_year")["lifespan_years"]
        .median()
        .reindex(years)
        .rename("median_lifespan_years")
        .rename_axis("demolition_year")
        .reset_index()
    )
    outputs[6] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("valid_lifespan_records", int(lifespan_by_year["lifespan_years"].notna().sum())),
                ("median_lifespan_years", _export_numeric_value(lifespan_by_year["lifespan_years"].median(), 2)),
            ]
        ),
        "table": median_table,
        "plot": _export_plot_png(
            median_table,
            f"6. Median lifespan — {scope_label} ({period_label})",
            "demolition_year",
            "median_lifespan_years",
            chart_type="line",
            x_label="Demolition year",
            y_label="Median lifespan (years)",
        ),
    }

    if not functional_rows.empty and "gebruiksdoel" in functional_rows.columns:
        if "fractional_weight" in functional_rows.columns:
            functional_table = (
                functional_rows.groupby("gebruiksdoel", dropna=False)["fractional_weight"]
                .sum()
                .reset_index(name="demolished_panden_equivalent")
                .sort_values("demolished_panden_equivalent", ascending=False)
            )
        else:
            functional_table = (
                functional_rows.groupby("gebruiksdoel", dropna=False)
                .size()
                .reset_index(name="demolished_panden_equivalent")
                .sort_values("demolished_panden_equivalent", ascending=False)
            )
        functional_table["gebruiksdoel"] = functional_table[
            "gebruiksdoel"
        ].fillna("Unknown")
    else:
        functional_table = pd.DataFrame(
            columns=["gebruiksdoel", "demolished_panden_equivalent"]
        )
    top_function = functional_table.iloc[0] if not functional_table.empty else None
    outputs[7] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("functional_rows", int(len(functional_rows))),
                ("functional_categories", int(len(functional_table))),
                ("largest_function_category", top_function["gebruiksdoel"] if top_function is not None else None),
                ("largest_function_category_equivalent", _export_numeric_value(top_function["demolished_panden_equivalent"], 1) if top_function is not None else None),
                ("availability_note", "Functional demolition uses fractionally weighted PAND-function rows; mixed-use PANDs therefore do not inflate the demolition denominator."),
            ]
        ),
        "table": functional_table,
        "plot": _export_plot_png(
            functional_table,
            f"7. Functional demolition profile — {scope_label} ({period_label})",
            "gebruiksdoel",
            "demolished_panden_equivalent",
            chart_type="bar",
            x_label="BAG gebruiksdoel",
            y_label="Fractionally weighted PAND equivalents",
        ),
    }

    if not replacement_rows.empty and "lag_category" in replacement_rows.columns:
        lag_table = (
            replacement_rows.groupby("lag_category", dropna=False)
            .size()
            .reset_index(name="demolished_panden")
            .sort_values("demolished_panden", ascending=False)
        )
        lag_table["lag_category"] = lag_table["lag_category"].fillna("Unknown")
    else:
        lag_table = pd.DataFrame(columns=["lag_category", "demolished_panden"])
    top_lag = lag_table.iloc[0] if not lag_table.empty else None
    outputs[8] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("replacement_rows", int(len(replacement_rows))),
                ("lag_categories", int(len(lag_table))),
                ("largest_lag_category", top_lag["lag_category"] if top_lag is not None else None),
                ("largest_lag_category_count", int(top_lag["demolished_panden"]) if top_lag is not None else None),
                ("availability_note", "Empty results can mean no replacement matches or an unavailable thesis_ch4.vw_dashboard_replacement_lag helper view."),
            ]
        ),
        "table": lag_table,
        "plot": _export_plot_png(
            lag_table,
            f"8. Replacement lag — {scope_label} ({period_label})",
            "lag_category",
            "demolished_panden",
            chart_type="bar",
            x_label="Replacement lag category",
            y_label="Demolished panden",
        ),
    }

    delay_table, delays = _export_registration_distribution(registration_rows)
    outputs[9] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("valid_registration_delay_records", int(len(delays))),
                ("mean_registration_delay_days", _export_numeric_value(delays.mean(), 2) if not delays.empty else None),
                ("median_registration_delay_days", _export_numeric_value(delays.median(), 2) if not delays.empty else None),
                ("p25_registration_delay_days", _export_numeric_value(delays.quantile(0.25), 2) if not delays.empty else None),
                ("p75_registration_delay_days", _export_numeric_value(delays.quantile(0.75), 2) if not delays.empty else None),
                ("minimum_registration_delay_days", _export_numeric_value(delays.min(), 0) if not delays.empty else None),
                ("maximum_registration_delay_days", _export_numeric_value(delays.max(), 0) if not delays.empty else None),
            ]
        ),
        "table": delay_table,
        "plot": _export_plot_png(
            delay_table,
            f"9. Registration delay — {scope_label} ({period_label})",
            "delay_band_days",
            "demolished_panden",
            chart_type="bar",
            x_label="Registration delay band (days)",
            y_label="Demolished panden",
        ),
    }

    substitution_table = annual.rename(
        columns={"demolished_panden": "total_demolitions"}
    )
    if (
        not replacement_rows.empty
        and "temporal_substitution_flag" in replacement_rows.columns
        and "demolition_year" in replacement_rows.columns
    ):
        flagged = replacement_rows.copy()
        flagged["demolition_year"] = pd.to_numeric(
            flagged["demolition_year"], errors="coerce"
        ).astype("Int64")
        flagged["temporal_substitution_flag"] = pd.to_numeric(
            flagged["temporal_substitution_flag"], errors="coerce"
        ).fillna(0)
        flagged_by_year = (
            flagged.groupby("demolition_year")["temporal_substitution_flag"]
            .sum()
            .reindex(years, fill_value=0)
            .rename("flagged_demolitions")
            .rename_axis("demolition_year")
            .reset_index()
        )
        substitution_table = substitution_table.merge(
            flagged_by_year, on="demolition_year", how="left"
        )
    else:
        substitution_table["flagged_demolitions"] = pd.NA

    substitution_table["substitution_bias_share_pct"] = substitution_table.apply(
        lambda row: (
            float(row["flagged_demolitions"]) / float(row["total_demolitions"]) * 100
            if pd.notna(row["flagged_demolitions"])
            and float(row["total_demolitions"]) > 0
            else None
        ),
        axis=1,
    )
    flagged_total = pd.to_numeric(
        substitution_table["flagged_demolitions"], errors="coerce"
    ).sum(min_count=1)
    overall_share = (
        float(flagged_total) / total_demolitions * 100
        if pd.notna(flagged_total) and total_demolitions > 0
        else None
    )
    outputs[10] = {
        "summary": _export_summary_table(
            [
                ("scope", scope_label),
                ("period", period_label),
                ("total_demolitions", total_demolitions),
                ("flagged_demolitions", int(flagged_total) if pd.notna(flagged_total) else None),
                ("temporal_substitution_bias_share_pct", _export_numeric_value(overall_share, 2)),
                ("availability_note", "The flag is supplied by thesis_ch4.vw_dashboard_replacement_lag and is descriptive of possible temporal substitution."),
            ]
        ),
        "table": substitution_table,
        "plot": _export_plot_png(
            substitution_table,
            f"10. Temporal substitution bias — {scope_label} ({period_label})",
            "demolition_year",
            "substitution_bias_share_pct",
            chart_type="line",
            x_label="Demolition year",
            y_label="Flagged share (%)",
        ),
    }

    return outputs


@st.cache_data(show_spinner=False, ttl=3600)
def load_indicator_export_sources(export_start_year, export_end_year):
    """Load all current-reference Fryslân rows needed by both ZIP packages."""
    _, municipalities_export_df = load_filter_values(CURRENT_GEOGRAPHY_MODE)
    municipality_names = tuple(
        municipalities_export_df["gemeentenaam"].dropna().astype(str).tolist()
    )
    export_records = load_records(
        municipality_names,
        int(export_start_year),
        int(export_end_year),
        CURRENT_GEOGRAPHY_MODE,
    )
    export_stock = load_dashboard_current_stock_wijk(municipality_names)
    export_functional = load_dashboard_functional_profile(
        municipality_names,
        int(export_start_year),
        int(export_end_year),
        CURRENT_GEOGRAPHY_MODE,
    )
    export_registration = load_dashboard_registration_delay(
        municipality_names,
        int(export_start_year),
        int(export_end_year),
        CURRENT_GEOGRAPHY_MODE,
    )
    export_replacement = load_dashboard_replacement_lag(
        municipality_names,
        int(export_start_year),
        int(export_end_year),
        CURRENT_GEOGRAPHY_MODE,
    )
    return (
        municipality_names,
        export_records,
        export_stock,
        export_functional,
        export_registration,
        export_replacement,
    )


def _export_filter_scope(frame, municipality=None, year=None):
    out = frame.copy()
    if municipality is not None and not out.empty and "gemeentenaam" in out.columns:
        out = out[out["gemeentenaam"].astype(str) == str(municipality)].copy()
    if year is not None and not out.empty and "demolition_year" in out.columns:
        year_values = pd.to_numeric(out["demolition_year"], errors="coerce")
        out = out[year_values == int(year)].copy()
    return out


def _export_write_dataframe(zip_file, path, frame):
    payload = frame.to_csv(index=False, lineterminator="\n").encode("utf-8-sig")
    zip_file.writestr(path, payload)


def _export_write_scope(
    zip_file,
    root_path,
    scope_label,
    records,
    stock_rows,
    functional_rows,
    registration_rows,
    replacement_rows,
    period_start,
    period_end,
    manifest_rows,
):
    outputs = _export_indicator_outputs(
        records,
        stock_rows,
        functional_rows,
        registration_rows,
        replacement_rows,
        period_start,
        period_end,
        scope_label,
    )
    combined_summary = []
    for indicator_number in range(1, 11):
        folder = EXPORT_INDICATOR_FOLDERS[indicator_number]
        indicator_path = f"{root_path}/{folder}"
        result = outputs[indicator_number]
        _export_write_dataframe(
            zip_file,
            f"{indicator_path}/summary.csv",
            result["summary"],
        )
        _export_write_dataframe(
            zip_file,
            f"{indicator_path}/table.csv",
            result["table"],
        )
        zip_file.writestr(f"{indicator_path}/plot.png", result["plot"])

        indicator_summary = result["summary"].copy()
        indicator_summary.insert(0, "indicator", indicator_number)
        indicator_summary.insert(1, "indicator_folder", folder)
        combined_summary.append(indicator_summary)
        manifest_rows.append(
            {
                "scope": scope_label,
                "period_start": int(period_start),
                "period_end": int(period_end),
                "indicator": indicator_number,
                "indicator_folder": folder,
                "table_rows": int(len(result["table"])),
                "zip_path": indicator_path,
            }
        )

    if combined_summary:
        _export_write_dataframe(
            zip_file,
            f"{root_path}/00_all_indicator_summaries.csv",
            pd.concat(combined_summary, ignore_index=True),
        )


def _export_readme(package_kind, start_year, end_year, municipality_count):
    organisation = (
        "One province folder and one folder per current municipality for the complete period."
        if package_kind == "period"
        else "One year folder per year; each year contains a province folder and one folder per current municipality."
    )
    return f"""BAG Demolition Explorer — ten-indicator export

Export window: {start_year}–{end_year}
Municipality folders: {municipality_count}
Geography: current municipality territory
Package organisation: {organisation}

Each scope contains exactly ten numbered indicator folders. Every indicator
folder contains:
- summary.csv: compact descriptive results and methodological notes;
- table.csv: the values used to draw the plot;
- plot.png: a static plot image suitable for reports and appendices.

A scope-level 00_all_indicator_summaries.csv combines all compact summaries.
The package root also contains manifest.csv, listing every generated indicator.

Indicator definitions
1. Annual demolition count.
2. Demolition intensity relative to current BAG pand stock.
3. Building age at demolition (demolition year minus construction year).
4. Cohort-specific loss by construction decade.
5. Mean lifespan.
6. Median lifespan.
7. Functional demolition profile using fractionally weighted BAG gebruiksdoel rows.
8. Replacement-lag categories.
9. BAG registration-delay distribution.
10. Temporal-substitution bias share.

Important methodological limits
- Indicator 2 uses the current-reference BAG stock as denominator because this
  export uses stable current municipality territories. It is therefore labelled
  intensity and must not be described as a reconstructed historical stock rate.
- Indicators 7–10 depend on the corresponding thesis_ch4 helper views. Empty
  tables can indicate either no observations or an unavailable helper view.
- The specialized historical gebruiksdoel transition explorer is not included,
  because it is treated as an extension of functional indicator 7 rather than
  an eleventh primary indicator.
- Province values are aggregations across the current Fryslân municipality scope.
- Files are generated from the connected PostgreSQL database at package-build time.
"""


@st.cache_data(show_spinner=False, ttl=3600)
def build_indicator_export_package(package_kind, export_start_year, export_end_year):
    """Build either the complete-period or year-by-year ZIP package in memory."""
    if package_kind not in {"period", "annual"}:
        raise ValueError("package_kind must be 'period' or 'annual'.")

    (
        municipality_names,
        export_records,
        export_stock,
        export_functional,
        export_registration,
        export_replacement,
    ) = load_indicator_export_sources(export_start_year, export_end_year)

    buffer = io.BytesIO()
    manifest_rows = []
    root = (
        f"ten_indicators_period_{export_start_year}_{export_end_year}"
        if package_kind == "period"
        else f"ten_indicators_by_year_{export_start_year}_{export_end_year}"
    )

    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zip_file:
        zip_file.writestr(
            f"{root}/README.txt",
            _export_readme(
                package_kind,
                export_start_year,
                export_end_year,
                len(municipality_names),
            ).encode("utf-8"),
        )

        periods = (
            [(int(export_start_year), int(export_end_year), None)]
            if package_kind == "period"
            else [
                (year, year, year)
                for year in range(int(export_start_year), int(export_end_year) + 1)
            ]
        )

        for period_start, period_end, year in periods:
            year_prefix = f"/{year}" if year is not None else ""

            province_records = _export_filter_scope(export_records, year=year)
            province_functional = _export_filter_scope(export_functional, year=year)
            province_registration = _export_filter_scope(export_registration, year=year)
            province_replacement = _export_filter_scope(export_replacement, year=year)
            _export_write_scope(
                zip_file,
                f"{root}{year_prefix}/province_fryslan",
                "Fryslân",
                province_records,
                export_stock,
                province_functional,
                province_registration,
                province_replacement,
                period_start,
                period_end,
                manifest_rows,
            )

            for municipality in municipality_names:
                safe_municipality = _export_safe_name(municipality)
                municipality_records = _export_filter_scope(
                    export_records, municipality=municipality, year=year
                )
                municipality_stock = _export_filter_scope(
                    export_stock, municipality=municipality
                )
                municipality_functional = _export_filter_scope(
                    export_functional, municipality=municipality, year=year
                )
                municipality_registration = _export_filter_scope(
                    export_registration, municipality=municipality, year=year
                )
                municipality_replacement = _export_filter_scope(
                    export_replacement, municipality=municipality, year=year
                )
                _export_write_scope(
                    zip_file,
                    f"{root}{year_prefix}/municipalities/{safe_municipality}",
                    municipality,
                    municipality_records,
                    municipality_stock,
                    municipality_functional,
                    municipality_registration,
                    municipality_replacement,
                    period_start,
                    period_end,
                    manifest_rows,
                )

        _export_write_dataframe(
            zip_file,
            f"{root}/manifest.csv",
            pd.DataFrame(manifest_rows),
        )

    return buffer.getvalue()


# ============================================================
# Sidebar and global filters
# ============================================================

with st.sidebar:
    st.header("Filters")
    geography_mode = st.radio(
        "Municipal geography",
        GEOGRAPHY_MODE_OPTIONS,
        index=0,
        help=(
            "Current territory aggregates every historical event into today's "
            "municipalities. Historical geography uses the municipality boundary "
            "valid at the demolition date."
        ),
        key="municipality_geography_mode",
    )
    if geography_mode == HISTORICAL_GEOGRAPHY_MODE:
        st.info(
            "Municipality charts use event-time boundaries. Wijk, buurt, "
            "gebruiksdoel-transition and 3DBAG views remain current-reference."
        )
    else:
        st.caption(
            "All years are aggregated into present-day municipality territories."
        )

years, municipalities_df = load_filter_values(geography_mode)

municipalities = sorted(
    municipalities_df["gemeentenaam"].dropna().astype(str).unique().tolist()
)
# Keep the two proof-of-concept municipalities immediately visible in the
# general indicator selector. Both are verified in the current administrative
# view and in the current/event-time demolition relation.
featured_municipalities = ["Leeuwarden", "De Fryske Marren"]
municipalities = [
    municipality for municipality in featured_municipalities
    if municipality in municipalities
] + [
    municipality for municipality in municipalities
    if municipality not in featured_municipalities
]
min_year = (
    int(years["min_year"].iloc[0])
    if not years.empty and pd.notna(years["min_year"].iloc[0])
    else ANALYSIS_START_YEAR
)
max_year = (
    int(years["max_year"].iloc[0])
    if not years.empty and pd.notna(years["max_year"].iloc[0])
    else ANALYSIS_END_YEAR
)

with st.sidebar:
    analysis_scope = st.radio(
        "Municipality scope",
        ["Selected municipalities", "Whole Fryslân"],
        horizontal=False,
        key="analysis_scope_mode_v2",
    )

    with st.form("analysis_filters", border=False):
        if analysis_scope == "Whole Fryslân":
            selected_municipalities = list(municipalities)
            scope_kind = (
                "historical municipality names represented in 2011–2025"
                if geography_mode == HISTORICAL_GEOGRAPHY_MODE
                else "current municipalities"
            )
            st.caption(
                f"All {len(selected_municipalities)} {scope_kind} are included."
            )
        else:
            selected_municipalities = st.multiselect(
                "Municipalities",
                municipalities,
                default=municipalities[:1],
                key=f"analysis_municipalities_{geography_mode}",
                help=(
                    "Choose one municipality for a complete municipal summary, "
                    "or several municipalities for comparison."
                ),
            )

        year_range = st.slider(
            "Demolition year",
            min_year,
            max_year,
            (min_year, max_year),
            key="analysis_year_range",
        )

        st.form_submit_button(
            "Apply analysis filters",
            use_container_width=True,
            help=(
                "Municipality and demolition-year changes are applied together, "
                "so dragging the slider does not rerun the complete app."
            ),
        )

    st.caption(
        "The analysis filters are batched. Adjust them first, then press "
        "**Apply analysis filters** once."
    )

    st.markdown("---")

    table_aggregation = st.selectbox(
        "Table aggregation",
        ["pand", "year", "municipality", "wijk", "buurt"],
    )

    st.caption(
        "The applied municipality and year filters control the indicator charts, "
        "wijk/buurt maps, downloads and specialized tools. Detailed geography "
        "controls are placed inside the relevant tab."
    )

if not selected_municipalities:
    st.warning("Select at least one municipality.")
    st.stop()

start_year, end_year = year_range
records = load_records(
    selected_municipalities,
    start_year,
    end_year,
    geography_mode,
)
invalid_bouwjaar_count = int(
    records.get("invalid_bouwjaar_flag", pd.Series(dtype="boolean"))
    .fillna(False)
    .sum()
)
invalid_lifespan_count = int(
    records.get("invalid_lifespan_flag", pd.Series(dtype="boolean"))
    .fillna(False)
    .sum()
)
if invalid_bouwjaar_count or invalid_lifespan_count:
    st.warning(
        "Age-quality filter applied: "
        f"{invalid_bouwjaar_count:,} record(s) had an impossible construction year "
        f"and {invalid_lifespan_count:,} record(s) had an impossible or inconsistent "
        "stored lifespan. Demolition counts are retained, but invalid age values are "
        "excluded from lifespan and construction-cohort analyses."
    )
source_coverage = load_demolition_source_coverage(geography_mode)
covered_municipalities = set(source_coverage["gemeentenaam"].dropna().astype(str))
selected_without_rows = sorted(
    set(map(str, selected_municipalities)) - covered_municipalities
)

if geography_mode == CURRENT_GEOGRAPHY_MODE:
    # Preserve the complete applied selection, including municipalities with zero
    # demolition rows, so current wijk/buurt maps and 3D tools remain inspectable.
    current_reference_municipalities = list(selected_municipalities)
elif (
    "current_gemeentenaam" in records.columns
    and records["current_gemeentenaam"].notna().any()
):
    current_reference_municipalities = sorted(
        records["current_gemeentenaam"].dropna().astype(str).unique().tolist()
    )
else:
    current_reference_municipalities = []

if USING_CASE_STUDY_FALLBACK:
    st.warning(
        "The app is running on the old two-municipality case-study relation "
        f"`{DEMOLITION_RECORDS_TABLE}`. The new controls and maps cover all current "
        "municipalities, but demolition indicators outside the source coverage will "
        "remain empty until `thesis_ch4.vw_demolition_analysis_vbo_eligible_2011_2025` is available."
    )
elif selected_without_rows:
    st.info(
        "No demolition rows were found in the analytical source for: "
        + ", ".join(selected_without_rows)
        + ". They remain visible in municipality maps with zero or unavailable values."
    )

if geography_mode == HISTORICAL_GEOGRAPHY_MODE:
    st.info(
        "**Historical municipality perspective:** municipality totals use the "
        "boundary valid at each demolition date. Current wijk/buurt and 3DBAG "
        "views use the present-day municipalities intersecting those events: "
        + (
            ", ".join(current_reference_municipalities)
            if current_reference_municipalities
            else "none in the applied filter"
        )
        + "."
    )
else:
    st.caption(
        "Municipality totals use current territories. Historical boundary evolution "
        "is available in Specialized spatial tools."
    )


# The export controls are visible immediately in the sidebar. ZIP generation is
# intentionally triggered by a button so the large annual image package does
# not block every ordinary map/filter rerun. Once built, Streamlit retains the
# bytes in session state and exposes a normal download button.
with st.sidebar:
    st.markdown("---")

    export_signature = (
        f"{EXPORT_START_YEAR}-{EXPORT_END_YEAR}-ten-indicator-package-v2"
    )
    if st.session_state.get("indicator_export_signature") != export_signature:
        st.session_state.pop("indicator_period_zip", None)
        st.session_state.pop("indicator_annual_zip", None)
        st.session_state["indicator_export_signature"] = export_signature

    with st.expander("Download 10-indicator result packages", expanded=True):
        st.caption(
            f"Fixed export window: {EXPORT_START_YEAR}–{EXPORT_END_YEAR} inclusive "
            f"({EXPORT_END_YEAR - EXPORT_START_YEAR + 1} annual folders). "
            "Both packages include Fryslân and one folder per current municipality, "
            "with summary CSVs, plot-data CSVs and PNG plots for all ten indicators."
        )

        if st.button(
            "Prepare complete-period ZIP",
            use_container_width=True,
            key="prepare_indicator_period_zip",
        ):
            with st.spinner("Building complete-period tables and plot images…"):
                st.session_state["indicator_period_zip"] = (
                    build_indicator_export_package(
                        "period",
                        EXPORT_START_YEAR,
                        EXPORT_END_YEAR,
                    )
                )

        if "indicator_period_zip" in st.session_state:
            period_bytes = st.session_state["indicator_period_zip"]
            st.download_button(
                "Download complete-period ZIP",
                data=period_bytes,
                file_name=(
                    f"ten_indicators_period_"
                    f"{EXPORT_START_YEAR}_{EXPORT_END_YEAR}.zip"
                ),
                mime="application/zip",
                use_container_width=True,
                key="download_indicator_period_zip",
            )
            st.caption(f"Prepared size: {len(period_bytes) / 1024 / 1024:.1f} MB")

        if st.button(
            "Prepare year-by-year ZIP",
            use_container_width=True,
            key="prepare_indicator_annual_zip",
        ):
            with st.spinner("Building annual tables and plot images…"):
                st.session_state["indicator_annual_zip"] = (
                    build_indicator_export_package(
                        "annual",
                        EXPORT_START_YEAR,
                        EXPORT_END_YEAR,
                    )
                )

        if "indicator_annual_zip" in st.session_state:
            annual_bytes = st.session_state["indicator_annual_zip"]
            st.download_button(
                "Download year-by-year ZIP",
                data=annual_bytes,
                file_name=(
                    f"ten_indicators_by_year_"
                    f"{EXPORT_START_YEAR}_{EXPORT_END_YEAR}.zip"
                ),
                mime="application/zip",
                use_container_width=True,
                key="download_indicator_annual_zip",
            )
            st.caption(f"Prepared size: {len(annual_bytes) / 1024 / 1024:.1f} MB")

# ============================================================
# Spatial time-travel fragment
# ============================================================

def render_temporal_boundary_comparison(
    wijk_collection,
    buurt_collection,
    historical_collection,
    current_focus_collection,
    scope_label,
):
    """Render two synchronised Leaflet maps with one client-side time controller.

    All interval filtering occurs in JavaScript. Moving or playing the time slider
    therefore does not rerun Streamlit or execute a new database query.
    """

    payload = {
        "wijk": wijk_collection or {"type": "FeatureCollection", "features": []},
        "buurt": buurt_collection or {"type": "FeatureCollection", "features": []},
        "historical": historical_collection
        or {"type": "FeatureCollection", "features": []},
        "current": current_focus_collection
        or {"type": "FeatureCollection", "features": []},
        "scope": scope_label,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    html = r"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    crossorigin=""
  />
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    crossorigin=""
  ></script>
  <style>
    html, body { margin: 0; padding: 0; font-family: Arial, sans-serif; color: #172033; font-size: 15px; }
    .time-shell {
      border: 1px solid #d9dee8;
      border-radius: 10px;
      background: #fff;
      overflow: hidden;
    }
    .time-bar {
      display: grid;
      grid-template-columns: auto auto auto minmax(260px, 1fr) auto auto;
      gap: 10px;
      align-items: center;
      padding: 12px 14px;
      border-bottom: 1px solid #d9dee8;
      background: #f8fafc;
    }
    .time-btn {
      border: 1px solid #cbd5e1;
      border-radius: 7px;
      background: white;
      min-width: 44px;
      height: 36px;
      cursor: pointer;
      font-size: 15px;
    }
    .time-btn:hover { background: #eef2f7; }
    #timeSlider { width: 100%; accent-color: #ef4444; }
    #dateLabel {
      min-width: 104px;
      font-weight: 700;
      color: #b91c1c;
      text-align: right;
    }
    .toggle-label {
      display: flex;
      gap: 5px;
      align-items: center;
      white-space: nowrap;
      font-size: 14px;
    }
    #activeSummary {
      padding: 10px 14px;
      border-bottom: 1px solid #d9dee8;
      background: #eaf3ff;
      color: #0756a5;
      min-height: 20px;
      font-size: 14px;
    }
    .map-heads, .maps {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .map-heads {
      padding: 12px 12px 5px 12px;
      font-weight: 700;
      font-size: 18px;
    }
    .maps { padding: 0 12px 12px 12px; }
    .map { height: 570px; border: 1px solid #e2e8f0; border-radius: 8px; }
    .legend {
      background: rgba(255,255,255,0.94);
      padding: 8px 10px;
      border-radius: 6px;
      box-shadow: 0 1px 5px rgba(0,0,0,.25);
      line-height: 1.45;
      font-size: 13px;
    }
    .legend-line {
      display: inline-block;
      width: 25px;
      height: 0;
      margin-right: 6px;
      vertical-align: middle;
      border-top: 3px solid #111827;
    }
    .legend-line.current { border-top-color: #2563eb; border-top-width: 4px; }
    @media (max-width: 900px) {
      .time-bar { grid-template-columns: auto auto auto 1fr; }
      .toggle-label { display: none; }
      .map-heads, .maps { grid-template-columns: 1fr; }
      .map { height: 480px; }
    }
  </style>
</head>
<body>
  <div class="time-shell">
    <div class="time-bar">
      <button class="time-btn" id="previousYear" title="Previous year">◀</button>
      <button class="time-btn" id="playPause" title="Play or pause">▶</button>
      <button class="time-btn" id="nextYear" title="Next year">▶|</button>
      <input id="timeSlider" type="range" />
      <span id="dateLabel"></span>
      <label class="toggle-label">
        <input id="showCurrent" type="checkbox" checked />
        Current footprint
      </label>
      <label class="toggle-label">
        <input id="showHistory" type="checkbox" checked />
        Historical boundaries
      </label>
    </div>
    <div id="activeSummary"></div>
    <div class="map-heads">
      <div>Wijk level</div>
      <div>Buurt level</div>
    </div>
    <div class="maps">
      <div id="wijkMap" class="map"></div>
      <div id="buurtMap" class="map"></div>
    </div>
  </div>

<script>
const DATA = __PAYLOAD__;

const DAY_MS = 24 * 60 * 60 * 1000;

function dateToDay(iso) {
  const parts = String(iso).slice(0, 10).split("-").map(Number);
  return Math.floor(Date.UTC(parts[0], parts[1] - 1, parts[2]) / DAY_MS);
}
function dayToDate(day) {
  return new Date(Number(day) * DAY_MS);
}
function dateToIso(date) {
  return date.toISOString().slice(0, 10);
}
function formatDate(date) {
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "short", year: "numeric", timeZone: "UTC"
  }).format(date);
}
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function popupForFeature(feature, type) {
  const p = feature.properties || {};
  if (type === "analysis") {
    return `
      <b>${escapeHtml(p.display_name || p.unit_name || "")}</b><br>
      Demolished panden: ${escapeHtml(p.demolished_panden ?? 0)}<br>
      Mean lifespan: ${escapeHtml(p.mean_lifespan_years ?? "—")}
    `;
  }
  if (type === "current") {
    return `
      <b>${escapeHtml(p.display_name || "")}</b><br>
      Current municipality footprint<br>
      Code: ${escapeHtml(p.detail_2_value || "")}
    `;
  }
  return `
    <b>${escapeHtml(p.display_name || "")}</b><br>
    Valid: ${escapeHtml(p.startdatum)} — ${escapeHtml(p.einddatum)}<br>
    Contributes to: ${escapeHtml(p.contributes_to)}<br>
    Geometry source year: ${escapeHtml(p.geometry_source_year ?? "—")}<br>
    Source: ${escapeHtml(p.source_label || "")}
  `;
}
function analysisStyle(feature) {
  const p = feature.properties || {};
  const c = Array.isArray(p.fill_color) ? p.fill_color : [235, 235, 235, 170];
  return {
    color: "rgba(80,80,80,0.65)",
    weight: 1,
    fillColor: `rgb(${c[0]},${c[1]},${c[2]})`,
    fillOpacity: Math.max(0.18, Math.min(0.82, (c[3] ?? 170) / 255))
  };
}
const historicalStyle = {
  color: "#111827",
  weight: 2.2,
  opacity: 0.95,
  fill: false,
  fillOpacity: 0
};
const currentStyle = {
  color: "#2563eb",
  weight: 4,
  opacity: 0.95,
  fill: false,
  fillOpacity: 0
};

function createMap(divId, analysisCollection) {
  const map = L.map(divId, { zoomControl: true });
  L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    {
      maxZoom: 20,
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO"
    }
  ).addTo(map);

  const analysis = L.geoJSON(analysisCollection, {
    style: analysisStyle,
    onEachFeature: (f, layer) => layer.bindPopup(popupForFeature(f, "analysis"))
  }).addTo(map);

  const historical = L.layerGroup().addTo(map);
  const current = L.geoJSON(DATA.current, {
    style: currentStyle,
    onEachFeature: (f, layer) => layer.bindPopup(popupForFeature(f, "current"))
  }).addTo(map);

  const candidate = current.getBounds().isValid()
    ? current.getBounds()
    : analysis.getBounds();

  if (candidate && candidate.isValid()) {
    map.fitBounds(candidate.pad(0.08));
  } else {
    map.setView([53.1, 5.8], 8);
  }

  const legend = L.control({position: "bottomright"});
  legend.onAdd = function() {
    const div = L.DomUtil.create("div", "legend");
    div.innerHTML = `
      <div><span class="legend-line"></span> Historical validity interval</div>
      <div><span class="legend-line current"></span> Current selected municipality</div>
      <div style="margin-top:4px;">Filled polygons: demolition aggregation</div>
    `;
    return div;
  };
  legend.addTo(map);

  return { map, analysis, historical, current };
}

const wijk = createMap("wijkMap", DATA.wijk);
const buurt = createMap("buurtMap", DATA.buurt);

let syncing = false;
function syncMap(source, target) {
  source.on("move", () => {
    if (syncing) return;
    syncing = true;
    target.setView(source.getCenter(), source.getZoom(), {animate: false});
    syncing = false;
  });
}
syncMap(wijk.map, buurt.map);
syncMap(buurt.map, wijk.map);

const historyFeatures = (DATA.historical.features || []).filter(
  f => f?.properties?.startdatum && f?.properties?.einddatum
);

const startDay = historyFeatures.length
  ? Math.min(...historyFeatures.map(f => dateToDay(f.properties.startdatum)))
  : dateToDay("2011-01-01");
const endDay = historyFeatures.length
  ? Math.max(...historyFeatures.map(f => dateToDay(f.properties.einddatum)))
  : dateToDay("2026-12-31");

const slider = document.getElementById("timeSlider");
slider.min = String(startDay);
slider.max = String(endDay);
slider.step = "1";
slider.value = String(startDay);

const dateLabel = document.getElementById("dateLabel");
const summary = document.getElementById("activeSummary");
const playPause = document.getElementById("playPause");
const showCurrent = document.getElementById("showCurrent");
const showHistory = document.getElementById("showHistory");

let timer = null;

function activeFeaturesAt(day) {
  return historyFeatures.filter(f => {
    const p = f.properties;
    return dateToDay(p.startdatum) <= day && day <= dateToDay(p.einddatum);
  });
}
function groupSummary(features, date) {
  if (!features.length) {
    return `${escapeHtml(DATA.scope)} · ${formatDate(date)} · no active historical boundaries`;
  }
  const groups = {};
  for (const f of features) {
    const p = f.properties || {};
    const key = p.contributes_to || DATA.scope;
    if (!groups[key]) groups[key] = [];
    groups[key].push(p.display_name || p.municipality_code || "Unknown");
  }
  const text = Object.entries(groups)
    .map(([target, names]) => `${target}: ${[...new Set(names)].join(", ")}`)
    .join(" | ");
  return `${escapeHtml(DATA.scope)} · ${formatDate(date)} · ${escapeHtml(text)}`;
}
function drawHistorical(target, features) {
  target.historical.clearLayers();
  if (!showHistory.checked) return;
  const layer = L.geoJSON(
    {type: "FeatureCollection", features},
    {
      style: historicalStyle,
      onEachFeature: (f, l) => l.bindPopup(popupForFeature(f, "historical"))
    }
  );
  layer.eachLayer(l => target.historical.addLayer(l));
}
function setCurrentVisibility(target, visible) {
  if (visible) {
    if (!target.map.hasLayer(target.current)) target.current.addTo(target.map);
  } else if (target.map.hasLayer(target.current)) {
    target.map.removeLayer(target.current);
  }
}
function renderDay(day) {
  const date = dayToDate(day);
  dateLabel.textContent = formatDate(date);
  const active = activeFeaturesAt(Number(day));
  drawHistorical(wijk, active);
  drawHistorical(buurt, active);
  setCurrentVisibility(wijk, showCurrent.checked);
  setCurrentVisibility(buurt, showCurrent.checked);
  summary.innerHTML = groupSummary(active, date);
}
function setDay(day) {
  const bounded = Math.max(startDay, Math.min(endDay, Number(day)));
  slider.value = String(bounded);
  renderDay(bounded);
}
function shiftYear(delta) {
  const d = dayToDate(Number(slider.value));
  const shifted = Date.UTC(
    d.getUTCFullYear() + delta,
    d.getUTCMonth(),
    d.getUTCDate()
  ) / DAY_MS;
  setDay(Math.floor(shifted));
}
function shiftMonth(delta) {
  const d = dayToDate(Number(slider.value));
  const shifted = Date.UTC(
    d.getUTCFullYear(),
    d.getUTCMonth() + delta,
    Math.min(d.getUTCDate(), 28)
  ) / DAY_MS;
  setDay(Math.floor(shifted));
}
function stopPlayback() {
  if (timer) clearInterval(timer);
  timer = null;
  playPause.textContent = "▶";
}
function startPlayback() {
  stopPlayback();
  playPause.textContent = "■";
  timer = setInterval(() => {
    if (Number(slider.value) >= endDay) {
      setDay(startDay);
    } else {
      shiftMonth(1);
    }
  }, 350);
}

slider.addEventListener("input", () => renderDay(Number(slider.value)));
document.getElementById("previousYear").addEventListener("click", () => shiftYear(-1));
document.getElementById("nextYear").addEventListener("click", () => shiftYear(1));
playPause.addEventListener("click", () => timer ? stopPlayback() : startPlayback());
showCurrent.addEventListener("change", () => renderDay(Number(slider.value)));
showHistory.addEventListener("change", () => renderDay(Number(slider.value)));

renderDay(startDay);
setTimeout(() => {
  wijk.map.invalidateSize();
  buurt.map.invalidateSize();
}, 100);
</script>
</body>
</html>
"""
    html = html.replace("__PAYLOAD__", payload_json)
    components.html(html, height=760, scrolling=False)


def _render_spatial_time_travel(
    selected_municipalities,
    start_year,
    end_year,
):
    selected_municipalities = list(selected_municipalities)
    scope_label = " + ".join(selected_municipalities)

    st.header("Spatial aggregation comparison")
    st.caption(
        "Current-reference municipalities define the wijk/buurt and boundary-"
        "evolution scope. The time controller below runs entirely in the browser: moving or "
        "playing it does not rerun Streamlit or query PostgreSQL again."
    )

    try:
        current_focus_df = load_current_municipality_boundaries(
            tuple(selected_municipalities)
        )
        historical_evolution_df = load_historical_municipality_evolution(
            tuple(selected_municipalities)
        )
    except Exception as exc:
        st.error(
            "The historical boundary query failed. The temporal controller was not "
            "rendered because no boundary payload could be created."
        )
        st.code(str(exc))
        current_focus_df = pd.DataFrame()
        historical_evolution_df = pd.DataFrame()

    current_focus_collection = current_boundaries_to_feature_collection(
        current_focus_df
    )
    historical_collection = historical_boundaries_to_feature_collection(
        historical_evolution_df
    )

    wijk_df = load_aggregated_geojson(
        "wijk",
        selected_municipalities,
        start_year,
        end_year,
    )
    buurt_df = load_aggregated_geojson(
        "buurt",
        selected_municipalities,
        start_year,
        end_year,
    )

    wijk_collection = dataframe_to_feature_collection(wijk_df)
    buurt_collection = dataframe_to_feature_collection(buurt_df)

    if historical_evolution_df.empty:
        st.warning(
            "No historical boundary intervals were returned for the selected "
            f"municipalities: {scope_label}. The app therefore cannot display the "
            "browser-side time controller. Check that the final boundary table has "
            "latest-year rows whose `naam` values match the selected municipalities."
        )
        left, right = st.columns(2)
        with left:
            make_geojson_map(
                wijk_collection,
                "Wijk level",
                map_key="wijk_fallback",
                current_focus_collection=current_focus_collection,
            )
        with right:
            make_geojson_map(
                buurt_collection,
                "Buurt level",
                map_key="buurt_fallback",
                current_focus_collection=current_focus_collection,
            )
        return

    st.info(
        f"Temporal inspection scope: {scope_label}. Historical polygons are "
        "selected by overlap with these present-day municipality footprints."
    )

    render_temporal_boundary_comparison(
        wijk_collection,
        buurt_collection,
        historical_collection,
        current_focus_collection,
        scope_label,
    )

    st.markdown(
        """
        **Interpretation:** the date slider filters municipality polygons by their
        `startdatum` and `einddatum`. The dark outlines therefore represent the
        administrative boundaries valid on the selected date. The blue outline is the
        current analytical footprint, and the filled wijk/buurt polygons remain the
        demolition representation.
        """
    )

# ============================================================
# 3DBAG Cesium viewer
# ============================================================

@st.cache_data(show_spinner=False)
def load_3dbag_camera_bounds(selected_municipalities):
    'Return the exact WGS84 extent of the selected current municipalities.'
    selected_municipalities = list(selected_municipalities or [])
    if not selected_municipalities:
        return pd.DataFrame()

    query = text(
        '''
        SELECT
            ST_XMin(extent)::double precision AS west,
            ST_YMin(extent)::double precision AS south,
            ST_XMax(extent)::double precision AS east,
            ST_YMax(extent)::double precision AS north
        FROM (
            SELECT
                ST_Extent(ST_Transform(geom, 4326))::box3d AS extent
            FROM dt_frl_base.vw_gemeente_admin
            WHERE gemeentenaam = ANY(:municipalities)
        ) bounds;
        '''
    )

    try:
        return pd.read_sql(
            query,
            engine,
            params={"municipalities": list(selected_municipalities)},
        )
    except ProgrammingError:
        return pd.DataFrame()


def _rows_to_geojson_feature_collection(df, level_label):
    """Convert rows containing unit_name + geojson into a compact FeatureCollection."""
    features = []
    if df is None or df.empty:
        return {"type": "FeatureCollection", "features": features}
    for row in df.itertuples(index=False):
        raw_geometry = getattr(row, "geojson", None)
        if raw_geometry is None or pd.isna(raw_geometry):
            continue
        try:
            geometry = json.loads(raw_geometry) if isinstance(raw_geometry, str) else raw_geometry
        except Exception:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "name": str(getattr(row, "unit_name", "") or ""),
                    "level": level_label,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


HERITAGE_CASE_STUDIES = ("Leeuwarden", "De Fryske Marren")

# Logical names requested by the thesis design followed by the names currently
# exposed by the imported source files. Resolution is metadata-driven, so a
# future database migration to the normalized names needs no application edit.
THESIS_HERITAGE_RELATION_CANDIDATES = {
    "case_municipalities": ("case_municipalities", "Leeuwarden _De Fryske Marren"),
    "national_monuments_points": ("national_monuments_points", "national_monuments_point"),
    "national_monuments_areas": ("national_monuments_areas",),
    "national_monument_buildings": ("national_monument_buildings", "national_monuments_buildings"),
    "protected_town_village_views": ("protected_town_village_views", "protected town_villages"),
    "wkpb_heritage_restrictions": ("wkpb_heritage_restrictions",),
    "chk_characteristic_buildings": ("chk_characteristic_buildings", "characteristic_buildings"),
    "chk_architecture_1850_1940": ("chk_architecture_1850_1940", "1850_1940"),
    "chk_postwar_neighbourhoods": ("chk_postwar_neighbourhoods", "chk_postwar_neighbourhood"),
    "chk_special_value_areas": ("chk_special_value_areas",),
    "chk_protected_estates": ("chk_protected_estates", "chk_protected_rural_houses"),
    "chk_post65": ("chk_post65", "chk_Post65"),
}


def _quote_pg_relation(qualified_name):
    """Quote a metadata-resolved PostgreSQL relation, including spaces/case."""
    schema_name, relation_name = qualified_name.split(".", 1)
    return '.'.join(
        f'"{part.replace(chr(34), chr(34) * 2)}"'
        for part in (schema_name, relation_name)
    )


def resolve_thesis_heritage_relation(logical_name):
    """Resolve one logical heritage layer through PostgreSQL metadata."""
    for relation_name in THESIS_HERITAGE_RELATION_CANDIDATES.get(logical_name, ()):
        qualified_name = f"thesis_heritage.{relation_name}"
        try:
            # Quoted lookup is required for the current imported names that
            # contain spaces or preserved uppercase characters.
            exists = relation_exists(qualified_name) or relation_exists(
                _quote_pg_relation(qualified_name)
            )
            if exists and get_relation_columns(qualified_name):
                return qualified_name
        except Exception:
            continue
    return None


def _first_available_column(columns, candidates):
    lookup = {str(column).lower(): str(column) for column in columns}
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    return None


@st.cache_data(show_spinner=False)
def load_thesis_heritage_layers(case_municipality):
    """Load the dedicated thesis_heritage proof-of-concept layers as 2D GeoJSON."""
    specifications = [
        ("national_monuments_points", "National monuments · points", "Formal / regulatory", "#332288", False),
        ("national_monuments_areas", "National monuments · designated areas", "Formal / regulatory", "#882255", False),
        ("national_monument_buildings", "National monument buildings", "Formal / regulatory", "#CC6677", True),
        ("protected_town_village_views", "Protected town and village views", "Formal / regulatory", "#DDCC77", True),
        ("wkpb_heritage_restrictions", "Wkpb heritage restrictions", "Formal / regulatory", "#994455", False),
        ("chk_characteristic_buildings", "CHK2 · characteristic buildings", "Broader cultural-historical", "#117733", False),
        ("chk_architecture_1850_1940", "CHK2 · architecture 1850–1940", "Broader cultural-historical", "#88CCEE", False),
        ("chk_postwar_neighbourhoods", "CHK2 · post-war neighbourhoods", "Broader cultural-historical", "#44AA99", False),
        ("chk_special_value_areas", "CHK2 · special-value areas", "Broader cultural-historical", "#DDCC77", False),
        ("chk_protected_estates", "CHK2 · protected estates", "Broader cultural-historical", "#117733", False),
        ("chk_post65", "CHK2 · Post-65", "Broader cultural-historical", "#88CCEE", False),
    ]
    layers, inventory, warnings = [], [], []
    for logical_name, title, group, color, visible in specifications:
        relation = resolve_thesis_heritage_relation(logical_name)
        if not relation:
            warnings.append(f"{title}: relation not available in thesis_heritage; layer skipped.")
            continue
        columns = get_relation_columns(relation)
        geom_column = _first_available_column(columns, ("geom", "geometry", "geovlak"))
        if not geom_column:
            warnings.append(f"{title}: no geometry column was found; layer skipped.")
            continue
        label_column = _first_available_column(
            columns,
            ("name", "naam", "OBJ_NAAM", "designation", "text", "identificatie", "localid", "id"),
        )
        identifier_column = _first_available_column(
            columns,
            ("identificatie", "monumentnummer", "OBJ_RIJKSN", "localid", "fuuid", "id"),
        )
        relation_sql = _quote_pg_relation(relation)
        geom_sql = f'h."{geom_column.replace(chr(34), chr(34) * 2)}"'
        label_sql = (
            f'h."{label_column.replace(chr(34), chr(34) * 2)}"::text'
            if label_column else "NULL::text"
        )
        identifier_sql = (
            f'h."{identifier_column.replace(chr(34), chr(34) * 2)}"::text'
            if identifier_column else "NULL::text"
        )
        query = text(
            f"""
            WITH aoi AS (
                SELECT ST_UnaryUnion(ST_Collect(ST_MakeValid(ST_Force2D(geom)))) AS geom
                FROM dt_frl_base.vw_gemeente_admin
                WHERE gemeentenaam = :municipality
            )
            SELECT
                {identifier_sql} AS identificatie,
                {label_sql} AS feature_name,
                ST_AsGeoJSON(
                    ST_Transform(
                        CASE
                            WHEN GeometryType({geom_sql}) IN ('POLYGON', 'MULTIPOLYGON')
                            THEN ST_SimplifyPreserveTopology(
                                ST_MakeValid(ST_Force2D({geom_sql})), 0.75
                            )
                            ELSE ST_MakeValid(ST_Force2D({geom_sql}))
                        END,
                        4326
                    ),
                    6
                ) AS geojson
            FROM {relation_sql} h
            CROSS JOIN aoi a
            WHERE {geom_sql} IS NOT NULL
              AND NOT ST_IsEmpty({geom_sql})
              AND {geom_sql} && a.geom
              AND ST_Intersects({geom_sql}, a.geom)
            LIMIT 20000;
            """
        )
        try:
            frame = pd.read_sql(query, engine, params={"municipality": case_municipality})
        except Exception as exc:
            warnings.append(f"{title}: query failed ({exc}); layer skipped.")
            continue
        features = []
        for row in frame.itertuples(index=False):
            try:
                geometry = json.loads(row.geojson) if isinstance(row.geojson, str) else row.geojson
            except Exception:
                continue
            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "name": str(row.feature_name or title),
                    "identificatie": str(row.identificatie or ""),
                    "monument_type": title,
                    "heritage_group": group,
                    "source_relation": relation,
                },
            })
        layers.append({
            "id": f"thesis_heritage_{logical_name}",
            "title": title,
            "group": group,
            "kind": "geojson",
            "visible": visible,
            "color": color,
            "fillAlpha": 0.22,
            "strokeAlpha": 0.95,
            "strokeWidth": 2.0,
            "featureCollection": {"type": "FeatureCollection", "features": features},
            "note": f"{len(features):,} feature(s) from {relation}; 2D contextual overlay.",
        })
        inventory.append({"group": group, "layer": title, "relation": relation, "features": len(features)})
    return layers, pd.DataFrame(inventory), warnings


@st.cache_data(show_spinner=False)
def load_case_demolition_footprints(case_municipality, cohort_start, cohort_end):
    """Load former BAG polygons only for the selected proof-of-concept scope."""
    relation = DEMOLITION_RECORDS_TABLE
    columns = set(get_relation_columns(relation))
    geometry_column = "geovlak" if "geovlak" in columns else None
    if not geometry_column and relation_exists("thesis_ch4.map_4_2_demolition_footprints_2011_2025"):
        relation = "thesis_ch4.map_4_2_demolition_footprints_2011_2025"
        columns = set(get_relation_columns(relation))
        geometry_column = "geom" if "geom" in columns else None
    if not geometry_column:
        return [], "No former BAG polygon geometry is exposed by the demolition sources."
    municipality_column = (
        "current_gemeentenaam" if "current_gemeentenaam" in columns else "gemeentenaam"
    )
    relation_sql = _quote_pg_relation(relation)
    cohort_filter = (
        "AND bouwjaar BETWEEN :cohort_start AND :cohort_end"
        if cohort_start is not None and cohort_end is not None
        else ""
    )
    query = text(
        f"""
        SELECT pand_id::text AS pand_id, bouwjaar, demolition_year,
               ST_AsGeoJSON(
                   ST_Transform(
                       ST_SimplifyPreserveTopology(
                           ST_MakeValid(ST_Force2D({geometry_column})), 0.20
                       ),
                       4326
                   ),
                   6
               ) AS geojson
        FROM {relation_sql}
        WHERE {municipality_column} = :municipality
          AND demolition_year BETWEEN 2011 AND 2025
          {cohort_filter}
          AND {geometry_column} IS NOT NULL
          AND NOT ST_IsEmpty({geometry_column})
        ORDER BY demolition_year, pand_id
        LIMIT 30000;
        """
    )
    try:
        params = {"municipality": case_municipality}
        if cohort_start is not None and cohort_end is not None:
            params.update({
                "cohort_start": int(cohort_start),
                "cohort_end": int(cohort_end),
            })
        frame = pd.read_sql(query, engine, params=params)
    except Exception as exc:
        return [], f"Demolition footprint query failed: {exc}"
    features = []
    for row in frame.itertuples(index=False):
        try:
            geometry = json.loads(row.geojson) if isinstance(row.geojson, str) else row.geojson
        except Exception:
            continue
        features.append({
            "type": "Feature", "geometry": geometry,
            "properties": {
                "name": f"Former BAG pand {row.pand_id}",
                "identificatie": str(row.pand_id),
                "monument_type": "Demolition footprint",
                "demolition_year": int(row.demolition_year),
                "bouwjaar": int(row.bouwjaar) if pd.notna(row.bouwjaar) else None,
            },
        })
    return features, None


@st.cache_data(show_spinner=False)
def load_case_cohort_decline_by_buurt(
    case_municipality,
    cohort_start,
    cohort_end,
    analysis_start_year=ANALYSIS_START_YEAR,
    analysis_end_year=ANALYSIS_END_YEAR,
):
    """Calculate bounded cohort loss D/(D+S) by current buurt from BAG/PostGIS.

    D is the number of observed demolition events in the selected construction
    cohort and analysis window. S is the current surviving BAG pand stock in the
    same cohort. The denominator D+S is therefore an observed bounded cohort,
    not a reconstructed historical stock snapshot at the beginning of the window.
    """
    query = text(
        f"""
        WITH scoped_buurten AS (
            SELECT b.buurtcode::text AS buurtcode, b.buurtnaam::text AS buurtnaam,
                   ST_MakeValid(ST_Force2D(b.geom)) AS geom
            FROM dt_frl_base.vw_buurten_admin b
            JOIN dt_frl_base.vw_gemeente_admin m
              ON m.gemeentecode::text = b.gemeentecode::text
            WHERE m.gemeentenaam = :municipality
        ), demolished AS (
            SELECT b.buurtcode, COUNT(DISTINCT d.pand_id)::bigint AS demolished
            FROM {DEMOLITION_RECORDS_TABLE} d
            JOIN scoped_buurten b ON d.point_rd IS NOT NULL
             AND ST_Covers(b.geom, d.point_rd)
            WHERE d.demolition_year BETWEEN :analysis_start_year AND :analysis_end_year
              AND d.bouwjaar BETWEEN :cohort_start AND :cohort_end
            GROUP BY b.buurtcode
        ), surviving AS (
            SELECT b.buurtcode, COUNT(DISTINCT p.identificatie)::bigint AS surviving
            FROM dt_frl.pand_current p
            JOIN scoped_buurten b ON p.geovlak IS NOT NULL
             AND NOT ST_IsEmpty(p.geovlak) AND ST_IsValid(p.geovlak)
             AND ST_Covers(b.geom, ST_PointOnSurface(p.geovlak))
            WHERE COALESCE(p.aanduidingrecordinactief, FALSE) IS FALSE
              AND p.pandstatus::text IN (
                  'Pand in gebruik',
                  'Pand in gebruik (niet ingemeten)',
                  'Pand buiten gebruik',
                  'Verbouwing pand',
                  'Sloopvergunning verleend'
              )
              AND p.bouwjaar BETWEEN :cohort_start AND :cohort_end
            GROUP BY b.buurtcode
        )
        SELECT b.buurtcode, b.buurtnaam,
               COALESCE(d.demolished, 0)::bigint AS demolished,
               COALESCE(s.surviving, 0)::bigint AS surviving,
               (COALESCE(d.demolished, 0) + COALESCE(s.surviving, 0))::bigint AS bounded_cohort_population,
               CASE WHEN COALESCE(d.demolished, 0) + COALESCE(s.surviving, 0) > 0
                    THEN COALESCE(d.demolished, 0)::numeric /
                         (COALESCE(d.demolished, 0) + COALESCE(s.surviving, 0)) * 100
                    ELSE NULL END AS cohort_decline_pct,
               ST_AsGeoJSON(
                   ST_Transform(ST_SimplifyPreserveTopology(b.geom, 2.0), 4326),
                   6
               ) AS geojson
        FROM scoped_buurten b
        LEFT JOIN demolished d USING (buurtcode)
        LEFT JOIN surviving s USING (buurtcode)
        ORDER BY b.buurtnaam;
        """
    )
    try:
        frame = pd.read_sql(query, engine, params={
            "municipality": case_municipality,
            "cohort_start": int(cohort_start),
            "cohort_end": int(cohort_end),
            "analysis_start_year": int(analysis_start_year),
            "analysis_end_year": int(analysis_end_year),
        })
    except Exception as exc:
        return pd.DataFrame(), {"type": "FeatureCollection", "features": []}, str(exc)

    def loss_band(value, population):
        if population is None or pd.isna(population) or int(population) <= 0:
            return "No observed cohort"
        if value is None or pd.isna(value):
            return "Not available"
        value = float(value)
        if value < 30:
            return "<30% · below suggested threshold"
        if value < 50:
            return "30–<50% · Vulnerable-inspired"
        if value < 70:
            return "50–<70% · Endangered-inspired"
        if value < 90:
            return "70–<90% · Critically endangered-inspired"
        return "≥90% · extreme observed loss"

    def fixed_loss_color(value, population):
        if population is None or pd.isna(population) or int(population) <= 0:
            return [221, 221, 221, 105]
        if value is None or pd.isna(value):
            return [221, 221, 221, 105]
        value = max(0.0, min(100.0, float(value)))
        if value < 20:
            return [254, 240, 217, 180]
        if value < 40:
            return [253, 204, 138, 190]
        if value < 60:
            return [252, 141, 89, 200]
        if value < 80:
            return [227, 74, 51, 210]
        return [179, 0, 0, 220]

    frame["cohort_decline_pct"] = pd.to_numeric(
        frame["cohort_decline_pct"], errors="coerce"
    )
    frame["bounded_cohort_population"] = pd.to_numeric(
        frame["bounded_cohort_population"], errors="coerce"
    ).fillna(0).astype(int)
    frame["loss_band"] = frame.apply(
        lambda row: loss_band(
            row.get("cohort_decline_pct"), row.get("bounded_cohort_population")
        ),
        axis=1,
    )
    frame["denominator_note"] = frame["bounded_cohort_population"].apply(
        lambda n: "Sparse denominator (N < 10)" if 0 < int(n) < 10 else ""
    )

    features = []
    for row in frame.itertuples(index=False):
        try:
            geometry = json.loads(row.geojson)
        except Exception:
            continue
        value = float(row.cohort_decline_pct) if pd.notna(row.cohort_decline_pct) else None
        percentage_label = f"{value:.1f}%" if value is not None else "Not available"
        features.append({
            "type": "Feature", "geometry": geometry,
            "properties": {
                "name": str(row.buurtnaam or row.buurtcode),
                "display_name": str(row.buurtnaam or row.buurtcode),
                "identificatie": str(row.buurtcode),
                "buurtcode": str(row.buurtcode),
                "monument_type": "Observed bounded cohort loss by current buurt",
                "layer_name": "Observed bounded cohort loss",
                "demolished": int(row.demolished),
                "surviving": int(row.surviving),
                "bounded_cohort_population": int(row.bounded_cohort_population),
                "cohort_decline_pct": value,
                "loss_band": str(row.loss_band),
                "denominator_note": str(row.denominator_note),
                "detail_1_label": "Observed cohort loss",
                "detail_1_value": percentage_label,
                "detail_2_label": "D / S / N",
                "detail_2_value": (
                    f"{int(row.demolished)} / {int(row.surviving)} / "
                    f"{int(row.bounded_cohort_population)}"
                ),
                "source_label": (
                    "BAG demolitions + current BAG pand stock; current buurt geometry"
                ),
                "fill_color": fixed_loss_color(value, row.bounded_cohort_population),
            },
        })
    return frame, {"type": "FeatureCollection", "features": features}, None


def build_cohort_decline_choropleth(
    frame,
    municipality,
    cohort_label,
    analysis_start_year=ANALYSIS_START_YEAR,
    analysis_end_year=ANALYSIS_END_YEAR,
    min_denominator=10,
):
    """Create a thesis-exportable buurt choropleth for bounded cohort loss.

    Percentages with N < ``min_denominator`` are retained in the data and hover
    information but deliberately not encoded on the continuous colour scale.
    This prevents very small bounded cohorts (for example D=1, S=0) from being
    visually interpreted as robust 100% neighbourhood-wide losses.
    """
    if frame is None or frame.empty:
        return None

    usable = frame[frame["geojson"].notna()].copy()
    if usable.empty:
        return None

    usable["bounded_cohort_population"] = pd.to_numeric(
        usable["bounded_cohort_population"], errors="coerce"
    ).fillna(0).astype(int)
    usable["cohort_decline_pct"] = pd.to_numeric(
        usable["cohort_decline_pct"], errors="coerce"
    )

    features = []
    for row in usable.itertuples(index=False):
        try:
            geometry = json.loads(row.geojson) if isinstance(row.geojson, str) else row.geojson
        except Exception:
            continue
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "buurtcode": str(row.buurtcode),
                "buurtnaam": str(row.buurtnaam or row.buurtcode),
            },
        })
    if not features:
        return None

    geojson = {"type": "FeatureCollection", "features": features}
    observed = usable[
        usable["cohort_decline_pct"].notna()
        & usable["bounded_cohort_population"].gt(0)
    ].copy()
    reliable = observed[
        observed["bounded_cohort_population"].ge(int(min_denominator))
    ].copy()
    sparse = observed[
        observed["bounded_cohort_population"].lt(int(min_denominator))
    ].copy()
    no_cohort = usable[
        usable["bounded_cohort_population"].le(0)
        | usable["cohort_decline_pct"].isna()
    ].copy()

    fig = go.Figure()

    if not reliable.empty:
        fig.add_trace(
            go.Choropleth(
                geojson=geojson,
                locations=reliable["buurtcode"].astype(str),
                featureidkey="properties.buurtcode",
                z=reliable["cohort_decline_pct"],
                zmin=0,
                zmax=100,
                colorscale=TERRACOTTA_SCALE,
                colorbar={
                    "title": {"text": "Loss share (%)", "side": "top"},
                    "ticksuffix": "%",
                    "len": 0.74,
                    "thickness": 18,
                    "y": 0.53,
                    "x": 1.01,
                    "outlinewidth": 0,
                },
                customdata=reliable[[
                    "buurtnaam",
                    "demolished",
                    "surviving",
                    "bounded_cohort_population",
                    "loss_band",
                ]].to_numpy(),
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "Observed loss: %{z:.1f}%<br>"
                    "D · demolished: %{customdata[1]:,.0f}<br>"
                    "S · surviving: %{customdata[2]:,.0f}<br>"
                    "N · D + S: %{customdata[3]:,.0f}<br>"
                    "%{customdata[4]}<extra></extra>"
                ),
                marker_line_width=0.7,
                marker_line_color="#6b7280",
                showscale=True,
                showlegend=False,
                name=f"N ≥ {int(min_denominator)}",
            )
        )

    if not sparse.empty:
        fig.add_trace(
            go.Choropleth(
                geojson=geojson,
                locations=sparse["buurtcode"].astype(str),
                featureidkey="properties.buurtcode",
                z=[1] * len(sparse),
                zmin=0,
                zmax=1,
                colorscale=[[0, "#CBD5E1"], [1, "#CBD5E1"]],
                customdata=sparse[[
                    "buurtnaam",
                    "cohort_decline_pct",
                    "demolished",
                    "surviving",
                    "bounded_cohort_population",
                ]].to_numpy(),
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "Observed loss: %{customdata[1]:.1f}%<br>"
                    "D · demolished: %{customdata[2]:,.0f}<br>"
                    "S · surviving: %{customdata[3]:,.0f}<br>"
                    "N · D + S: %{customdata[4]:,.0f}<br>"
                    f"<b>Shown grey: sparse denominator (N &lt; {int(min_denominator)})</b>"
                    "<extra></extra>"
                ),
                marker_line_width=0.7,
                marker_line_color="#64748b",
                showscale=False,
                showlegend=True,
                name=f"Sparse denominator (N < {int(min_denominator)})",
            )
        )

    if not no_cohort.empty:
        fig.add_trace(
            go.Choropleth(
                geojson=geojson,
                locations=no_cohort["buurtcode"].astype(str),
                featureidkey="properties.buurtcode",
                z=[1] * len(no_cohort),
                zmin=0,
                zmax=1,
                colorscale=[[0, "#F4F4F5"], [1, "#F4F4F5"]],
                customdata=no_cohort[["buurtnaam"]].to_numpy(),
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "No observed pre-1945 cohort in D + S.<extra></extra>"
                ),
                marker_line_width=0.7,
                marker_line_color="#a1a1aa",
                showscale=False,
                showlegend=True,
                name="No observed cohort (N = 0)",
            )
        )

    fig.update_geos(
        fitbounds="locations",
        visible=False,
        projection_type="mercator",
        domain={"x": [0.01, 0.90], "y": [0.02, 0.98]},
    )
    fig.update_layout(
        title=(
            f"{cohort_label.capitalize()} cohort loss by buurt — {municipality}, "
            f"{analysis_start_year}–{analysis_end_year}"
        ),
        height=560,
        margin={"l": 0, "r": 95, "t": 72, "b": 42},
        legend={
            "orientation": "h",
            "yanchor": "top",
            "y": -0.03,
            "xanchor": "left",
            "x": 0.01,
            "font": {"size": 13},
        },
    )
    return fig


@st.cache_data(show_spinner=False)
def load_3d_admin_boundary_layers(selected_municipalities):
    """Load province, municipality, wijk and buurt outlines for the Cesium viewer.

    Province and municipality layers provide provincial context. Wijk and buurt
    layers are restricted to the current camera municipality selection to avoid
    sending an unnecessarily large browser payload.
    """
    selected_municipalities = list(selected_municipalities or [])
    if not selected_municipalities:
        return [], ["No municipality scope was selected for administrative boundaries."]

    province_filter = """
        LOWER(TRIM(COALESCE(provincienaam, ''))) IN ('fryslân', 'friesland')
        OR NOT EXISTS (
            SELECT 1
            FROM dt_frl_base.vw_gemeente_admin p
            WHERE LOWER(TRIM(COALESCE(p.provincienaam, '')))
                  IN ('fryslân', 'friesland')
        )
    """

    layer_specs = [
        (
            "province_boundary",
            "Province boundary · Fryslân",
            "province",
            True,
            5,
            text(
                f"""
                WITH province AS (
                    SELECT ST_UnaryUnion(
                        ST_Collect(ST_MakeValid(ST_Force2D(geom)))
                    ) AS geom
                    FROM dt_frl_base.vw_gemeente_admin
                    WHERE {province_filter}
                )
                SELECT
                    'Fryslân'::text AS unit_name,
                    ST_AsGeoJSON(
                        ST_Transform(
                            ST_Boundary(
                                ST_SimplifyPreserveTopology(geom, 25.0)
                            ),
                            4326
                        )
                    ) AS geojson
                FROM province
                WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom);
                """
            ),
            {},
        ),
        (
            "municipality_boundaries",
            "Municipality boundaries",
            "municipality",
            True,
            3,
            text(
                f"""
                SELECT
                    gemeentenaam::text AS unit_name,
                    ST_AsGeoJSON(
                        ST_Transform(
                            ST_Boundary(
                                ST_SimplifyPreserveTopology(
                                    ST_MakeValid(ST_Force2D(geom)),
                                    12.0
                                )
                            ),
                            4326
                        )
                    ) AS geojson
                FROM dt_frl_base.vw_gemeente_admin
                WHERE {province_filter}
                ORDER BY gemeentenaam;
                """
            ),
            {},
        ),
        (
            "wijk_boundaries",
            "Wijk boundaries · selected municipality scope",
            "wijk",
            True,
            2.4,
            text(
                """
                SELECT
                    COALESCE(w.wijknaam, w.wijkcode::text)::text AS unit_name,
                    ST_AsGeoJSON(
                        ST_Transform(
                            ST_Boundary(
                                ST_SimplifyPreserveTopology(
                                    ST_MakeValid(ST_Force2D(w.geom)),
                                    6.0
                                )
                            ),
                            4326
                        )
                    ) AS geojson
                FROM dt_frl_base.vw_wijken_admin w
                JOIN dt_frl_base.vw_gemeente_admin m
                  ON m.gemeentecode::text = w.gemeentecode::text
                WHERE m.gemeentenaam = ANY(:municipalities)
                ORDER BY m.gemeentenaam, w.wijknaam;
                """
            ),
            {"municipalities": selected_municipalities},
        ),
        (
            "buurt_boundaries",
            "Buurt boundaries · selected municipality scope",
            "buurt",
            False,
            1.4,
            text(
                """
                SELECT
                    COALESCE(b.buurtnaam, b.buurtcode::text)::text AS unit_name,
                    ST_AsGeoJSON(
                        ST_Transform(
                            ST_Boundary(
                                ST_SimplifyPreserveTopology(
                                    ST_MakeValid(ST_Force2D(b.geom)),
                                    3.0
                                )
                            ),
                            4326
                        )
                    ) AS geojson
                FROM dt_frl_base.vw_buurten_admin b
                JOIN dt_frl_base.vw_gemeente_admin m
                  ON m.gemeentecode::text = b.gemeentecode::text
                WHERE m.gemeentenaam = ANY(:municipalities)
                ORDER BY m.gemeentenaam, b.buurtnaam;
                """
            ),
            {"municipalities": selected_municipalities},
        ),
    ]

    layers = []
    errors = []
    for layer_id, title, level, visible, width, query, params in layer_specs:
        try:
            frame = pd.read_sql(query, engine, params=params)
        except Exception as exc:
            errors.append(f"{title}: {exc}")
            continue
        feature_collection = _rows_to_geojson_feature_collection(frame, level)
        if not feature_collection["features"]:
            errors.append(f"{title}: the query returned no usable geometry.")
            continue
        layers.append(
            {
                "id": layer_id,
                "title": title,
                "kind": "boundary",
                "visible": visible,
                "color": ADMIN_BOUNDARY_COLOR,
                "width": width,
                "featureCollection": feature_collection,
                "note": (
                    f"{len(feature_collection['features']):,} {level} polygon(s); "
                    "neon-yellow line overlay."
                ),
            }
        )
    return layers, errors


@st.cache_data(show_spinner=False)
def load_postgis_legal_monument_layers(selected_municipalities):
    """Load the imported EduGIS/Kadaster monument restriction polygons from PostGIS.

    The expected relation is created by the existing MVT import workflow:
    ``thesis_ch4.monumenten_legal_edugis``. It is a cartographic derivative of
    the EduGIS vector-tile layer and should be used for visual inspection rather
    than as authoritative legal evidence.
    """
    selected_municipalities = list(selected_municipalities or [])
    relation = "thesis_ch4.monumenten_legal_edugis"
    if not selected_municipalities:
        return [], pd.DataFrame(), "No municipality scope was selected."
    if not relation_exists(relation):
        return (
            [],
            pd.DataFrame(),
            "PostGIS heritage relation thesis_ch4.monumenten_legal_edugis was not found.",
        )

    available = set(get_relation_columns(relation))
    required = {"geom", "identificatie", "grondslag_code"}
    if not required.issubset(available):
        return (
            [],
            pd.DataFrame(),
            "The PostGIS monument relation exists but does not expose geom, identificatie and grondslag_code.",
        )

    monument_type_expr = (
        "h.monument_type::text"
        if "monument_type" in available
        else "CASE h.grondslag_code::text "
             "WHEN 'EWE' THEN 'Rijksmonument' "
             "WHEN 'GG' THEN 'Gemeentelijk monument' "
             "WHEN 'MP' THEN 'Provinciaal monument' "
             "WHEN 'EWD' THEN 'Voorbeschermd rijksmonument' "
             "ELSE h.grondslag_code::text END"
    )
    description_expr = (
        "h.grondslag_omschrijving::text"
        if "grondslag_omschrijving" in available
        else "NULL::text"
    )
    authority_expr = (
        "h.statutaire_naam_bestuursorgaan::text"
        if "statutaire_naam_bestuursorgaan" in available
        else "NULL::text"
    )

    query = text(
        f"""
        WITH aoi AS (
            SELECT ST_UnaryUnion(
                ST_Collect(ST_MakeValid(ST_Force2D(geom)))
            ) AS geom
            FROM dt_frl_base.vw_gemeente_admin
            WHERE gemeentenaam = ANY(:municipalities)
        ),
        clipped AS (
            SELECT
                h.identificatie::text AS identificatie,
                h.grondslag_code::text AS grondslag_code,
                {monument_type_expr} AS monument_type,
                {description_expr} AS grondslag_omschrijving,
                {authority_expr} AS authority_name,
                ST_CollectionExtract(
                    ST_Intersection(ST_MakeValid(ST_Force2D(h.geom)), a.geom),
                    3
                ) AS geom
            FROM {relation} h
            CROSS JOIN aoi a
            WHERE a.geom IS NOT NULL
              AND h.geom && a.geom
              AND ST_Intersects(h.geom, a.geom)
              AND h.grondslag_code::text IN ('EWE', 'GG', 'MP', 'EWD')
        )
        SELECT
            identificatie,
            grondslag_code,
            monument_type,
            grondslag_omschrijving,
            authority_name,
            ST_AsGeoJSON(
                ST_Transform(
                    ST_SimplifyPreserveTopology(geom, 0.50),
                    4326
                )
            ) AS geojson
        FROM clipped
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
        ORDER BY grondslag_code, identificatie
        LIMIT 15000;
        """
    )

    try:
        frame = pd.read_sql(
            query,
            engine,
            params={"municipalities": selected_municipalities},
        )
    except Exception as exc:
        return [], pd.DataFrame(), f"PostGIS monument overlay query failed: {exc}"

    layers = []
    inventory_rows = []
    for code in ["EWE", "GG", "MP", "EWD"]:
        subset = frame[frame["grondslag_code"].astype(str) == code].copy()
        if subset.empty:
            continue
        features = []
        for row in subset.itertuples(index=False):
            try:
                geometry = json.loads(row.geojson) if isinstance(row.geojson, str) else row.geojson
            except Exception:
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        "identificatie": str(row.identificatie or ""),
                        "grondslag_code": code,
                        "monument_type": str(row.monument_type or ""),
                        "grondslag_omschrijving": str(row.grondslag_omschrijving or ""),
                        "authority_name": str(row.authority_name or ""),
                    },
                }
            )
        if not features:
            continue
        title = LEGAL_MONUMENT_LABELS.get(code, f"Wkpb restriction · {code}")
        layers.append(
            {
                "id": f"wkpb_{code.lower()}",
                "title": title,
                "kind": "geojson",
                "visible": False,
                "color": LEGAL_MONUMENT_COLORS.get(code, "#d53e4f"),
                "fillAlpha": 0.24,
                "strokeAlpha": 0.95,
                "strokeWidth": 2.0,
                "featureCollection": {
                    "type": "FeatureCollection",
                    "features": features,
                },
                "note": (
                    f"{len(features):,} polygon(s) in the selected municipality scope. "
                    "Rendered as a translucent 2D ground overlay; it does not classify or recolor 3DBAG buildings."
                ),
            }
        )
        inventory_rows.append(
            {
                "source": relation,
                "grondslag_code": code,
                "layer": title,
                "features": len(features),
                "status": "Ready in Cesium",
            }
        )

    return layers, pd.DataFrame(inventory_rows), None


@st.cache_data(show_spinner=False)
def load_current_rijksmonument_points(selected_municipalities):
    """Load current matched Rijksmonumenten for an optional 3DBAG overlay."""
    selected_municipalities = list(selected_municipalities or [])
    if not selected_municipalities:
        return pd.DataFrame()

    if relation_exists("post65.rijksmonumenten_bag_match_nl"):
        query = text(
            """
            SELECT DISTINCT
                r.monument_id,
                r.monument_name,
                r.status,
                r.function_type,
                r.registration_date,
                r.pand_id,
                ST_X(
                    ST_Transform(ST_PointOnSurface(ST_MakeValid(r.geovlak)), 4326)
                )::double precision AS lon,
                ST_Y(
                    ST_Transform(ST_PointOnSurface(ST_MakeValid(r.geovlak)), 4326)
                )::double precision AS lat
            FROM post65.rijksmonumenten_bag_match_nl r
            JOIN dt_frl_base.vw_gemeente_admin m
              ON r.geovlak IS NOT NULL
             AND ST_Covers(m.geom, ST_PointOnSurface(ST_MakeValid(r.geovlak)))
            WHERE COALESCE(r.matched_to_bag, FALSE)
              AND m.gemeentenaam = ANY(:municipalities)
            ORDER BY r.monument_name, r.monument_id;
            """
        )
    elif relation_exists("post65.rijksmonumenten_bag_match"):
        query = text(
            """
            SELECT DISTINCT
                r.monument_id,
                r.monument_name,
                r.status,
                r.function_type,
                r.registration_date,
                r.pand_id,
                ST_X(
                    ST_Transform(ST_PointOnSurface(p.geovlak), 4326)
                )::double precision AS lon,
                ST_Y(
                    ST_Transform(ST_PointOnSurface(p.geovlak), 4326)
                )::double precision AS lat
            FROM post65.rijksmonumenten_bag_match r
            JOIN dt_frl.pand_current p
              ON p.identificatie::text = r.pand_id::text
            JOIN dt_frl_base.vw_gemeente_admin m
              ON p.geovlak IS NOT NULL
             AND ST_Covers(m.geom, ST_PointOnSurface(p.geovlak))
            WHERE COALESCE(r.matched_to_bag, FALSE)
              AND m.gemeentenaam = ANY(:municipalities)
            ORDER BY r.monument_name, r.monument_id;
            """
        )
    else:
        return pd.DataFrame()

    try:
        return pd.read_sql(
            query,
            engine,
            params={"municipalities": list(selected_municipalities)},
        )
    except Exception:
        return pd.DataFrame()


def _json_safe_heritage_records(df):
    """Convert monument points into simple browser-safe dictionaries."""
    if df.empty:
        return []
    records = []
    for row in df.itertuples(index=False):
        if pd.isna(row.lon) or pd.isna(row.lat):
            continue
        records.append(
            {
                "monument_id": str(row.monument_id or ""),
                "name": str(row.monument_name or "Unnamed Rijksmonument"),
                "status": str(row.status or ""),
                "function_type": str(row.function_type or ""),
                "registration_date": (
                    str(row.registration_date)
                    if pd.notna(row.registration_date)
                    else ""
                ),
                "pand_id": str(row.pand_id or ""),
                "lon": float(row.lon),
                "lat": float(row.lat),
            }
        )
    return records


def _unpack_wms_template(tile_url):
    """Translate an EduGIS WMS tile template into Cesium WMS parameters."""
    parsed = urlparse(str(tile_url))
    outer_query = parse_qs(parsed.query)

    # Some EduGIS styles wrap the original RCE service in an EduGIS proxy.
    if "url" in outer_query and outer_query["url"]:
        tile_url = unquote(outer_query["url"][0])
        parsed = urlparse(tile_url)

    query = parse_qs(parsed.query)
    query_lower = {str(key).lower(): values for key, values in query.items()}

    def first(name, default=""):
        values = query_lower.get(name.lower(), [])
        return str(values[0]) if values else default

    service_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    layers = first("layers")
    if not service_url or not layers:
        return None

    return {
        "url": service_url,
        "layers": layers,
        "styles": first("styles"),
        "version": first("version", "1.3.0"),
        "format": first("format", "image/png"),
        "transparent": first("transparent", "true").lower() != "false",
        "crs": first("crs") or first("srs", "EPSG:3857"),
    }


@st.cache_data(show_spinner=False)
def load_edugis_heritage_layer_configs(folder):
    """Read local EduGIS style JSON files for the Cesium heritage panel.

    WMS raster definitions are translated into Cesium imagery-layer settings.
    Mapbox Vector Tile definitions are inventoried but not rendered because
    CesiumJS has no native MVT imagery provider.
    """
    folder_path = Path(folder)
    if not folder_path.exists():
        return [], pd.DataFrame(), "Heritage layer folder does not exist on this computer."

    configs = []
    inventory = []
    for json_path in sorted(folder_path.glob("*.json")):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
            style = payload.get("style", payload)
            source = style.get("source", {}) or {}
            metadata = style.get("metadata", {}) or {}
            title = str(metadata.get("title") or style.get("id") or json_path.stem)
            source_type = str(source.get("type") or "unknown").lower()
            visible = bool(metadata.get("visible", True))

            if source_type == "raster" and source.get("tiles"):
                wms = _unpack_wms_template(source["tiles"][0])
                if wms:
                    configs.append({
                        "id": str(style.get("id") or json_path.stem),
                        "title": title,
                        "kind": "wms",
                        "visible": visible,
                        "alpha": 0.90 if "punt" in title.lower() else 0.48,
                        # EduGIS minzoom is a cartographic display hint, not a WMS
                        # server limitation. Cesium level numbering differs, so forcing
                        # minzoom=13 made the Rijksmonument WMS look broken at municipal
                        # overview scale. Keep it as metadata but allow imagery from level 0.
                        "minimumLevel": 0,
                        "originalMinZoom": int(style.get("minzoom", 0) or 0),
                        "credit": str(source.get("attribution") or "RCE / EduGIS"),
                        **wms,
                    })
                    inventory.append({
                        "file": json_path.name,
                        "title": title,
                        "source_type": source_type,
                        "cesium_mode": "WMS imagery",
                        "status": "Ready",
                    })
                    continue

            if source_type == "vector" and source.get("tiles"):
                configs.append({
                    "id": str(style.get("id") or json_path.stem),
                    "title": title,
                    "kind": "mvt",
                    "visible": False,
                    "tiles": str(source["tiles"][0]),
                    "sourceLayer": str(style.get("source-layer") or ""),
                    "note": (
                        "MVT configuration loaded. Convert this source to GeoJSON/PostGIS "
                        "or publish it as WMS before rendering it in Cesium."
                    ),
                })
                inventory.append({
                    "file": json_path.name,
                    "title": title,
                    "source_type": source_type,
                    "cesium_mode": "MVT conversion required",
                    "status": "Configuration loaded; not rendered",
                })
                continue

            inventory.append({
                "file": json_path.name,
                "title": title,
                "source_type": source_type,
                "cesium_mode": "Unsupported",
                "status": "No compatible WMS or MVT source found",
            })
        except Exception as exc:
            inventory.append({
                "file": json_path.name,
                "title": json_path.stem,
                "source_type": "error",
                "cesium_mode": "Not loaded",
                "status": str(exc),
            })

    deduplicated = []
    seen = set()
    for config in configs:
        signature = (
            config.get("id"),
            config.get("kind"),
            config.get("url") or config.get("tiles"),
            config.get("layers") or config.get("sourceLayer"),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduplicated.append(config)

    return deduplicated, pd.DataFrame(inventory), None


@st.cache_data(show_spinner=False)
def inventory_local_3dbag_gpkgs(folder):
    """Read tile extents from GeoPackage metadata without loading geometries."""
    rows = []
    for gpkg_path in sorted(Path(folder).glob("*.gpkg")):
        try:
            with sqlite3.connect(gpkg_path) as connection:
                metadata = connection.execute(
                    """
                    SELECT min_x, min_y, max_x, max_y, srs_id
                    FROM gpkg_contents
                    WHERE table_name = 'lod12_2d'
                    """
                ).fetchone()
            if metadata:
                rows.append(
                    {
                        "path": str(gpkg_path),
                        "file": gpkg_path.name,
                        "bytes": gpkg_path.stat().st_size,
                        "min_x": metadata[0],
                        "min_y": metadata[1],
                        "max_x": metadata[2],
                        "max_y": metadata[3],
                        "srs_id": metadata[4],
                    }
                )
        except Exception:
            continue
    return pd.DataFrame(rows)


@st.cache_data(show_spinner=False)
def load_local_3dbag_lod12(folder, west, south, east, north, max_tiles=150):
    """Load municipality-scoped local LoD1.2 footprints and BAG attributes."""
    try:
        import geopandas as gpd
        from pyproj import Transformer
    except ImportError as exc:
        return pd.DataFrame(), pd.DataFrame(), f"Local GeoPackage dependency missing: {exc}"

    inventory = inventory_local_3dbag_gpkgs(folder)
    if inventory.empty:
        return pd.DataFrame(), inventory, "No local .gpkg tiles with lod12_2d were found."

    try:
        transformer = Transformer.from_crs(4326, 7415, always_xy=True)
        rd_west, rd_south = transformer.transform(float(west), float(south))[:2]
        rd_east, rd_north = transformer.transform(float(east), float(north))[:2]
        min_x, max_x = sorted((rd_west, rd_east))
        min_y, max_y = sorted((rd_south, rd_north))
    except Exception as exc:
        return pd.DataFrame(), inventory, f"Could not transform the selection to EPSG:7415: {exc}"

    selected = inventory[
        inventory["max_x"].ge(min_x)
        & inventory["min_x"].le(max_x)
        & inventory["max_y"].ge(min_y)
        & inventory["min_y"].le(max_y)
    ].copy()
    if selected.empty:
        return pd.DataFrame(), selected, "No local 3DBAG tiles intersect the selected municipality extent."
    if len(selected) > max_tiles:
        return (
            pd.DataFrame(),
            selected,
            f"The selection intersects {len(selected)} tiles; narrow the camera focus or raise the {max_tiles}-tile safety limit.",
        )

    frames = []
    failures = []
    bbox = (min_x, min_y, max_x, max_y)
    for row in selected.itertuples(index=False):
        try:
            lod = gpd.read_file(
                row.path,
                layer="lod12_2d",
                bbox=bbox,
                columns=["identificatie", "b3_h_min", "b3_h_70p", "b3_h_max"],
            )
            if lod.empty:
                continue
            pand = gpd.read_file(
                row.path,
                layer="pand",
                bbox=bbox,
                columns=["identificatie", "oorspronkelijkbouwjaar", "status"],
                ignore_geometry=True,
            )
            lod = lod.merge(pand, on="identificatie", how="left")
            lod["source_tile"] = row.file
            frames.append(lod)
        except Exception as exc:
            failures.append(f"{row.file}: {exc}")

    if not frames:
        message = "No LoD1.2 buildings could be read from the intersecting tiles."
        if failures:
            message += " " + failures[0]
        return pd.DataFrame(), selected, message

    buildings = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        geometry="geometry",
        crs=frames[0].crs,
    ).to_crs(4326)
    buildings = buildings[buildings.geometry.notna() & ~buildings.geometry.is_empty].copy()
    buildings["building_height"] = (
        pd.to_numeric(buildings["b3_h_70p"], errors="coerce")
        - pd.to_numeric(buildings["b3_h_min"], errors="coerce")
    ).clip(lower=2, upper=120).fillna(6)
    buildings["bouwjaar"] = pd.to_numeric(
        buildings["oorspronkelijkbouwjaar"], errors="coerce"
    )
    buildings = add_construction_cohort(buildings)
    cohort_palette = {
        cohort["label"]: _cohort_color(index, len(CONSTRUCTION_COHORTS))
        for index, cohort in enumerate(CONSTRUCTION_COHORTS)
    }

    def rgb_for(label):
        color = cohort_palette.get(str(label), UNKNOWN_COLOR).lstrip("#")
        return [int(color[index:index + 2], 16) for index in (0, 2, 4)] + [220]

    buildings["fill_color"] = buildings["construction_cohort_label"].map(rgb_for)
    return buildings, selected, ("; ".join(failures[:3]) if failures else None)


def render_local_3dbag_pydeck(
    buildings,
    bounds,
    admin_boundaries,
    heritage_layers,
    visible_admin_levels=None,
    label_admin_levels=None,
):
    """Render local LoD1.2 extrusion and 2D context in one WebGL scene."""
    if buildings is None or buildings.empty:
        return
    # GeoPandas otherwise serializes every source attribute and full floating-
    # point precision into Streamlit's websocket message. For a large case-study
    # municipality that can add hundreds of megabytes even though the viewer uses
    # only six attributes. This is a display copy: analytical data remains intact.
    display_columns = [
        column for column in [
            "identificatie",
            "oorspronkelijkbouwjaar",
            "construction_cohort_label",
            "building_height",
            "fill_color",
            "geometry",
        ]
        if column in buildings.columns
    ]
    display_buildings = buildings[display_columns].copy().rename(
        columns={
            "identificatie": "id",
            "oorspronkelijkbouwjaar": "y",
            "construction_cohort_label": "c",
            "building_height": "h",
            "fill_color": "f",
        }
    )
    display_buildings["h"] = pd.to_numeric(
        display_buildings["h"], errors="coerce"
    ).round(1)
    # Roughly 0.15 m at Fryslân's latitude. It removes redundant vertices but
    # preserves individual building footprints and polygon topology.
    display_buildings.geometry = display_buildings.geometry.simplify(
        0.000002,
        preserve_topology=True,
    )
    # PolygonLayer avoids GeoJSON's repeated Feature/geometry/properties wrapper
    # while retaining the actual LoD1.2 footprint rings. MultiPolygons are split
    # into their constituent parts with the same BAG attributes.
    building_records = []
    for building in display_buildings.itertuples(index=False):
        geometry = building.geometry
        polygon_parts = (
            list(geometry.geoms)
            if getattr(geometry, "geom_type", "") == "MultiPolygon"
            else [geometry]
        )
        raw_year = pd.to_numeric(pd.Series([building.y]), errors="coerce").iloc[0]
        raw_height = pd.to_numeric(pd.Series([building.h]), errors="coerce").iloc[0]
        raw_color = building.f
        color = [int(value) for value in list(raw_color)]
        for polygon in polygon_parts:
            if polygon is None or polygon.is_empty or not hasattr(polygon, "exterior"):
                continue
            rings = [
                [
                    [round(float(coordinate[0]), 6), round(float(coordinate[1]), 6)]
                    for coordinate in polygon.exterior.coords
                ]
            ]
            rings.extend(
                [
                    [round(float(coordinate[0]), 6), round(float(coordinate[1]), 6)]
                    for coordinate in interior.coords
                ]
                for interior in polygon.interiors
            )
            building_records.append(
                {
                    "id": str(building.id) if pd.notna(building.id) else "",
                    "y": int(raw_year) if pd.notna(raw_year) else None,
                    "c": str(building.c) if pd.notna(building.c) else "Unknown",
                    "h": float(raw_height) if pd.notna(raw_height) else 2.0,
                    "f": color,
                    "p": rings,
                }
            )
    layers = [
        pdk.Layer(
            "PolygonLayer",
            building_records,
            id="local-3dbag-exact-buildings",
            pickable=True,
            filled=True,
            extruded=True,
            wireframe=False,
            stroked=True,
            get_polygon="p",
            get_fill_color="f",
            get_elevation="h",
            elevation_scale=1,
            get_line_color=[75, 75, 75, 150],
            line_width_min_pixels=0.5,
        )
    ]
    visible_admin_levels = set(
        visible_admin_levels or ["province", "municipality", "wijk"]
    )
    label_admin_levels = set(label_admin_levels or ["municipality", "wijk"])
    boundary_colors = {
        "province": [75, 75, 75, 235],
        "municipality": [204, 102, 119, 245],
        "wijk": [51, 34, 136, 235],
        "buurt": [17, 119, 51, 220],
    }
    label_records = []
    for boundary in admin_boundaries or []:
        features = boundary.get("featureCollection", {}).get("features", [])
        level = ""
        if features:
            level = str(features[0].get("properties", {}).get("level", ""))
        if level not in visible_admin_levels:
            continue
        layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                boundary.get("featureCollection"),
                id=f"local-{boundary.get('id')}",
                pickable=True,
                filled=False,
                stroked=True,
                get_line_color=boundary_colors.get(level, [75, 75, 75, 235]),
                line_width_min_pixels=max(1, float(boundary.get("width", 2))),
                visible=True,
            )
        )
        if level in label_admin_levels:
            try:
                from shapely.geometry import shape

                for feature in features:
                    geometry = feature.get("geometry")
                    name = feature.get("properties", {}).get("name")
                    if not geometry or not name:
                        continue
                    point = shape(geometry).representative_point()
                    label_records.append(
                        {
                            "position": [float(point.x), float(point.y)],
                            "name": str(name),
                            "level": level,
                            "color": boundary_colors.get(level, [75, 75, 75, 235]),
                        }
                    )
            except Exception:
                pass
    for heritage in heritage_layers or []:
        if heritage.get("kind") != "geojson":
            continue
        color = heritage.get("color", "#CC6677").lstrip("#")
        rgb = [int(color[index:index + 2], 16) for index in (0, 2, 4)]
        property_fill = bool(heritage.get("styleByFillColor"))
        layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                heritage.get("featureCollection"),
                id=f"local-{heritage.get('id')}",
                pickable=True,
                filled=True,
                stroked=True,
                extruded=False,
                get_fill_color=(
                    "properties.fill_color" if property_fill
                    else rgb + [int(255 * float(heritage.get("fillAlpha", 0.27)))]
                ),
                get_line_color=rgb + [230],
                line_width_min_pixels=2,
                point_radius_min_pixels=5,
                point_radius_max_pixels=10,
            )
        )

    if label_records:
        layers.append(
            pdk.Layer(
                "TextLayer",
                label_records,
                id="local-administrative-labels",
                pickable=True,
                get_position="position",
                get_text="name",
                get_color="color",
                get_size=14,
                size_units="pixels",
                get_alignment_baseline="'center'",
                get_text_anchor="'middle'",
                background=True,
                get_background_color=[255, 255, 255, 205],
                background_padding=[3, 2],
            )
        )

    row = bounds.iloc[0]
    longitude_span = max(float(row["east"]) - float(row["west"]), 0.001)
    latitude_span = max(float(row["north"]) - float(row["south"]), 0.001)
    zoom = max(7.5, min(13.0, math.log2(360 / max(longitude_span, latitude_span)) - 1.2))
    view_state = pdk.ViewState(
        longitude=(float(row["west"]) + float(row["east"])) / 2,
        latitude=(float(row["south"]) + float(row["north"])) / 2,
        zoom=zoom,
        pitch=48,
        bearing=0,
    )
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        tooltip={
            "html": (
                "<b>BAG:</b> {id}<br/>"
                "<b>Construction year:</b> {y}<br/>"
                "<b>Cohort:</b> {c}<br/>"
                "<b>Height:</b> {h} m<br/>"
                "<b>Administrative unit:</b> {name}"
            )
        },
    )
    deck_size_mb = len(deck.to_json().encode("utf-8")) / (1024 * 1024)
    st.caption(
        f"Exact local LoD1.2 building footprints · {deck_size_mb:.1f} MB browser payload."
    )
    st.pydeck_chart(deck, use_container_width=True, height=760)


def render_3dbag_viewer(
    selected_municipalities,
    tileset_url,
    lod_label,
    focus_label,
    heritage_points=None,
    cohort_definitions=None,
    heritage_layers=None,
    admin_boundaries=None,
):
    """Embed 3DBAG as the only 3D layer and keep all context as 2D overlays.

    Administrative boundaries, WMS heritage, legal heritage polygons and monument
    points are rendered on the ground/ellipsoid. They never classify or clamp onto
    3DBAG building surfaces.
    """
    bounds = load_3dbag_camera_bounds(tuple(selected_municipalities))

    if bounds.empty or bounds.iloc[0].isna().any():
        st.error(
            "The current municipality bounds could not be read from "
            "`dt_frl_base.vw_gemeente_admin`."
        )
        return

    row = bounds.iloc[0]
    viewer_config = {
        "tilesetUrl": tileset_url,
        "lodLabel": lod_label,
        "release": THREEDBAG_RELEASE,
        "west": float(row["west"]),
        "south": float(row["south"]),
        "east": float(row["east"]),
        "north": float(row["north"]),
        "focusLabel": focus_label,
        "heritagePoints": heritage_points or [],
        "cohortDefinitions": cohort_definitions or [],
        "heritageLayers": heritage_layers or [],
        "adminBoundaries": admin_boundaries or [],
    }

    html = r'''
    <link
      rel="stylesheet"
      href="https://cesium.com/downloads/cesiumjs/releases/__CESIUM_VERSION__/Build/Cesium/Widgets/widgets.css"
    />
    <script src="https://cesium.com/downloads/cesiumjs/releases/__CESIUM_VERSION__/Build/Cesium/Cesium.js"></script>

    <style>
      html, body, #cesiumContainer {
        width: 100%; height: 100%; margin: 0; padding: 0;
        overflow: hidden; font-family: Arial, sans-serif;
      }
      #cesiumContainer { position: relative; background: #111827; }
      #statusPanel {
        position: absolute; top: 12px; left: 12px; z-index: 20;
        max-width: 520px; padding: 9px 12px; border-radius: 7px;
        background: rgba(255,255,255,.95); color: #111827;
        font-size: 13px; line-height: 1.4;
        box-shadow: 0 1px 7px rgba(0,0,0,.32);
      }
      #toolbar {
        position: absolute; top: 12px; right: 12px; z-index: 21;
        display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end;
      }
      #toolbar button {
        border: 1px solid #cbd5e1; background: rgba(255,255,255,.96);
        color: #111827; border-radius: 6px; padding: 7px 9px;
        cursor: pointer; font-size: 12px;
      }
      #toolbar button:hover { background: #f1f5f9; }
      #layerPanel {
        position: absolute; top: 54px; right: 12px; z-index: 22;
        display: none; width: min(330px, calc(100% - 24px));
        max-height: 52%; overflow: auto; padding: 11px 12px;
        border-radius: 7px; background: rgba(255,255,255,.97);
        color: #111827; font-size: 12px; line-height: 1.45;
        box-shadow: 0 1px 7px rgba(0,0,0,.28);
      }
      #layerPanel label {
        display: flex; gap: 8px; align-items: flex-start; margin: 7px 0;
      }
      #layerPanel select {
        width: 100%; margin-top: 4px; padding: 6px;
        border: 1px solid #cbd5e1; border-radius: 5px;
      }
      .layer-note { color: #64748b; font-size: 11px; margin: -3px 0 6px 24px; }
      .layer-section-title {
        margin-top: 11px; padding-top: 8px; border-top: 1px solid #e5e7eb;
        font-weight: 700; color: #374151;
      }
      #cohortLegend {
        position: absolute; left: 12px; bottom: 34px; z-index: 20;
        width: 250px; max-height: 48%; overflow-y: auto;
        padding: 10px 12px; border-radius: 7px;
        background: rgba(255,255,255,.96); color: #111827;
        font-size: 13px; line-height: 1.45;
        box-shadow: 0 1px 6px rgba(0,0,0,.2);
      }
      .cohort-row {
        display: flex; align-items: center; gap: 8px; margin: 3px 0;
      }
      .cohort-swatch {
        width: 18px; height: 12px; border: 1px solid rgba(0,0,0,.28);
        border-radius: 2px; flex: 0 0 auto;
      }
      .heritage-dot {
        width: 11px; height: 11px; border-radius: 50%;
        background: #00e5ff; border: 2px solid #111827;
        box-shadow: 0 0 0 1px #fff;
        flex: 0 0 auto;
      }
      #featurePanel {
        position: absolute; right: 12px; bottom: 34px; z-index: 20;
        display: none; width: min(390px, calc(100% - 24px));
        max-height: 44%; overflow: auto; padding: 10px 12px;
        border-radius: 7px; background: rgba(255,255,255,.97);
        color: #111827; font-size: 12px; line-height: 1.4;
        box-shadow: 0 1px 6px rgba(0,0,0,.22);
      }
      #featurePanel table { width: 100%; border-collapse: collapse; }
      #featurePanel td {
        padding: 3px 5px; border-bottom: 1px solid #e5e7eb;
        vertical-align: top; word-break: break-word;
      }
      #featurePanel td:first-child { font-weight: 600; width: 42%; }
      #attribution {
        position: absolute; left: 8px; bottom: 5px; z-index: 20;
        padding: 3px 6px; border-radius: 4px;
        background: rgba(255,255,255,.88); color: #374151;
        font-size: 10px;
      }
    </style>

    <div id="cesiumContainer">
      <div id="statusPanel">Starting Cesium and checking 3DBAG…</div>
      <div id="toolbar">
        <button id="zoomIn" type="button" title="Zoom in">＋</button>
        <button id="zoomOut" type="button" title="Zoom out">－</button>
        <button id="topView" type="button">Top view</button>
        <button id="tiltedView" type="button">3D view</button>
        <button id="focusSelection" type="button">Reset to selection</button>
        <button id="focusDataset" type="button">Whole 3DBAG</button>
        <button id="admin2dTest" type="button">Admin 2D test</button>
        <button id="toggleHeritage" type="button">Hide monument points</button>
        <button id="toggleLayers" type="button">Layers</button>
      </div>
      <div id="layerPanel">
        <strong>2D context and overlay layers</strong>
        <label style="display:block;margin-top:9px;">
          Background map
          <select id="basemapMode">
            <option value="muted" selected>Muted context map</option>
            <option value="detailed">Detailed context map</option>
            <option value="off">No background map</option>
          </select>
        </label>
        <div class="layer-section-title">Administrative boundaries</div>
        <div class="layer-note" style="margin:2px 0 5px 0;">Neon yellow · 2D ground lines · province / municipality / wijk / buurt</div>
        <div id="adminLayerRows"></div>
        <div class="layer-section-title">Heritage layers</div>
        <div id="heritageLayerRows"></div>
      </div>
      <div id="cohortLegend">
        <strong>Current surviving stock · 3DBAG</strong>
        <div id="cohortRows"></div>
        <div class="cohort-row"><span class="cohort-swatch" style="background:#CC6677aa"></span>Disappearance · former BAG footprint</div>
        <div class="cohort-row"><span class="cohort-swatch" style="background:#B30000aa"></span>Cohort decline · D / (D + S)</div>
        <div class="cohort-row" id="heritageLegendRow"><span class="heritage-dot"></span>Monument point · 2D ground overlay</div>
        <div style="margin-top:6px;color:#4b5563;">
          Only 3DBAG buildings are 3D. Boundaries and heritage layers remain 2D ground overlays.
        </div>
      </div>
      <div id="featurePanel"></div>
      <div id="attribution">
        3DBAG © 3D geoinformation research group &amp; 3DGI · CC BY 4.0 ·
        map © OpenStreetMap contributors · heritage RCE/Kadaster/EduGIS
      </div>
    </div>

    <script type="module">
      const config = __VIEWER_CONFIG__;
      const statusPanel = document.getElementById("statusPanel");
      const featurePanel = document.getElementById("featurePanel");
      const cohortRows = document.getElementById("cohortRows");
      const layerPanel = document.getElementById("layerPanel");
      const adminLayerRows = document.getElementById("adminLayerRows");
      const heritageLayerRows = document.getElementById("heritageLayerRows");

      for (const cohort of (config.cohortDefinitions || [])) {
        const row = document.createElement("div");
        row.className = "cohort-row";
        const swatch = document.createElement("span");
        swatch.className = "cohort-swatch";
        swatch.style.background = cohort.color;
        const label = document.createElement("span");
        label.textContent = cohort.label;
        row.appendChild(swatch);
        row.appendChild(label);
        cohortRows.appendChild(row);
      }
      if (!(config.cohortDefinitions || []).length) {
        cohortRows.textContent = "No valid construction-year cohorts in this selection.";
      }

      function escapeHtml(value) {
        return String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#039;");
      }

      function selectedRectangle() {
        return Cesium.Rectangle.fromDegrees(
          config.west, config.south, config.east, config.north
        );
      }

      const viewer = new Cesium.Viewer("cesiumContainer", {
        animation: false,
        timeline: false,
        geocoder: false,
        baseLayerPicker: false,
        navigationHelpButton: false,
        fullscreenButton: false,
        sceneModePicker: true,
        homeButton: true,
        // Streamlit already renders this viewer inside a sandboxed iframe.
        // Cesium's default InfoBox creates a second about:blank iframe, which
        // triggers the blocked-script warnings visible in Edge/Chrome. We use
        // the custom featurePanel below, so the nested iframe is unnecessary.
        infoBox: false,
        selectionIndicator: false,
        baseLayer: false,
        globe: new Cesium.Globe(Cesium.Ellipsoid.WGS84),
        terrainProvider: new Cesium.EllipsoidTerrainProvider(),
        requestRenderMode: false,
        skyBox: false,
        skyAtmosphere: false,
      });

      viewer.scene.backgroundColor = Cesium.Color.fromCssColorString("#111827");
      viewer.scene.globe.show = true;
      viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString("#cbd5e1");
      viewer.scene.globe.enableLighting = false;
      viewer.scene.globe.depthTestAgainstTerrain = false;
      viewer.scene.globe.showGroundAtmosphere = false;
      viewer.scene.highDynamicRange = false;
      if (viewer.scene.postProcessStages?.fxaa) {
        viewer.scene.postProcessStages.fxaa.enabled = true;
      }

      let basemapLayer = null;
      try {
        const osm = new Cesium.UrlTemplateImageryProvider({
          url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
          credit: "© OpenStreetMap contributors",
          maximumLevel: 19,
        });
        basemapLayer = viewer.imageryLayers.addImageryProvider(osm);
      } catch (imageryError) {
        console.warn("Basemap could not be created", imageryError);
      }

      function applyBasemapMode(mode) {
        if (!basemapLayer) return;
        if (mode === "off") {
          basemapLayer.show = false;
          return;
        }
        basemapLayer.show = true;
        if (mode === "detailed") {
          basemapLayer.alpha = 0.92;
          basemapLayer.saturation = 0.75;
          basemapLayer.brightness = 0.98;
          basemapLayer.contrast = 1.02;
          basemapLayer.gamma = 1.0;
        } else {
          basemapLayer.alpha = 0.72;
          basemapLayer.saturation = 0.0;
          basemapLayer.brightness = 0.82;
          basemapLayer.contrast = 0.78;
          basemapLayer.gamma = 1.12;
        }
      }
      applyBasemapMode("muted");
      document.getElementById("basemapMode").addEventListener("change", (event) => {
        applyBasemapMode(event.target.value);
      });

      function makeLayerCheckbox(container, layerConfig) {
        const layerRow = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = Boolean(layerConfig.visible);
        const labelText = document.createElement("span");
        labelText.textContent = layerConfig.title;
        layerRow.appendChild(checkbox);
        layerRow.appendChild(labelText);
        container.appendChild(layerRow);
        if (layerConfig.note) {
          const note = document.createElement("div");
          note.className = "layer-note";
          note.textContent = layerConfig.note;
          container.appendChild(note);
        }
        return { checkbox, labelText };
      }

      function addBoundaryRing(dataSource, ring, layerConfig, featureName) {
        if (!Array.isArray(ring) || ring.length < 2) return;
        const degreesWithHeight = [];
        const boundaryHeight = Number(layerConfig.height || 18);
        for (const coordinate of ring) {
          if (!Array.isArray(coordinate) || coordinate.length < 2) continue;
          degreesWithHeight.push(
            Number(coordinate[0]), Number(coordinate[1]), boundaryHeight
          );
        }
        if (degreesWithHeight.length < 6) return;
        const description =
          `<strong>${escapeHtml(featureName || layerConfig.title)}</strong><br>` +
          `Administrative level: ${escapeHtml(layerConfig.title)}<br>` +
          `Display: neon-yellow boundary overlay`;
        const boundaryColor = Cesium.Color.fromCssColorString(
          layerConfig.color || "#F4FF00"
        );
        dataSource.entities.add({
          name: featureName || layerConfig.title,
          polyline: {
            positions: Cesium.Cartesian3.fromDegreesArrayHeights(degreesWithHeight),
            width: Number(layerConfig.width || 2),
            material: boundaryColor.withAlpha(0.98),
            // A small fixed offset plus a depth-fallback material keeps the
            // outline visible in a scene dominated by 3DBAG surfaces. It does
            // not classify or recolour the building tileset.
            clampToGround: false,
            depthFailMaterial: boundaryColor.withAlpha(0.92),
            arcType: Cesium.ArcType.GEODESIC,
          },
          description,
        });
      }

      function addBoundaryGeometry(dataSource, geometry, layerConfig, featureName) {
        if (!geometry) return;
        if (geometry.type === "LineString") {
          addBoundaryRing(dataSource, geometry.coordinates, layerConfig, featureName);
        } else if (geometry.type === "MultiLineString") {
          for (const line of geometry.coordinates || []) {
            addBoundaryRing(dataSource, line, layerConfig, featureName);
          }
        } else if (geometry.type === "Polygon") {
          for (const ring of geometry.coordinates || []) {
            addBoundaryRing(dataSource, ring, layerConfig, featureName);
          }
        } else if (geometry.type === "MultiPolygon") {
          for (const polygon of geometry.coordinates || []) {
            for (const ring of polygon || []) {
              addBoundaryRing(dataSource, ring, layerConfig, featureName);
            }
          }
        }
        const labelLevels = new Set(["municipality", "wijk", "buurt"]);
        const level = String(layerConfig.id || "").split("_")[0];
        if (featureName && labelLevels.has(level)) {
          const coordinates = [];
          const collectCoordinates = (value) => {
            if (!Array.isArray(value)) return;
            if (value.length >= 2 && Number.isFinite(Number(value[0])) && Number.isFinite(Number(value[1]))) {
              coordinates.push([Number(value[0]), Number(value[1])]);
              return;
            }
            for (const child of value) collectCoordinates(child);
          };
          collectCoordinates(geometry.coordinates);
          if (coordinates.length) {
            const west = Math.min(...coordinates.map((item) => item[0]));
            const east = Math.max(...coordinates.map((item) => item[0]));
            const south = Math.min(...coordinates.map((item) => item[1]));
            const north = Math.max(...coordinates.map((item) => item[1]));
            dataSource.entities.add({
              name: featureName,
              position: Cesium.Cartesian3.fromDegrees((west + east) / 2, (south + north) / 2, 45),
              label: {
                text: featureName,
                font: level === "municipality" ? "bold 15px sans-serif" : "12px sans-serif",
                fillColor: Cesium.Color.WHITE,
                outlineColor: Cesium.Color.fromCssColorString("#4B4B4B"),
                outlineWidth: 3,
                style: Cesium.LabelStyle.FILL_AND_OUTLINE,
                showBackground: true,
                backgroundColor: Cesium.Color.fromCssColorString("#4B4B4B").withAlpha(0.72),
                pixelOffset: new Cesium.Cartesian2(0, -8),
                disableDepthTestDistance: Number.POSITIVE_INFINITY,
                distanceDisplayCondition: new Cesium.DistanceDisplayCondition(
                  0,
                  level === "buurt" ? 18000 : (level === "wijk" ? 45000 : 180000)
                ),
              },
            });
          }
        }
      }

      const adminDataSources = new Map();
      for (const boundaryConfig of (config.adminBoundaries || [])) {
        const { checkbox, labelText } = makeLayerCheckbox(adminLayerRows, boundaryConfig);
        try {
          const dataSource = new Cesium.CustomDataSource(boundaryConfig.title);
          for (const feature of (boundaryConfig.featureCollection?.features || [])) {
            addBoundaryGeometry(
              dataSource,
              feature.geometry,
              boundaryConfig,
              feature.properties?.name || boundaryConfig.title
            );
          }
          await viewer.dataSources.add(dataSource);
          dataSource.show = checkbox.checked;
          dataSource._layerCheckbox = checkbox;
          adminDataSources.set(boundaryConfig.id, dataSource);
          checkbox.addEventListener("change", () => {
            dataSource.show = checkbox.checked;
            viewer.scene.requestRender();
          });
        } catch (boundaryError) {
          checkbox.disabled = true;
          labelText.textContent += " — could not be created";
          console.warn("Administrative boundary layer failed", boundaryConfig, boundaryError);
        }
      }
      if (!(config.adminBoundaries || []).length) {
        adminLayerRows.innerHTML =
          '<div class="layer-note" style="margin:4px 0 0 0;">Administrative boundaries were not available from PostGIS.</div>';
      }

      const heritageImageryLayers = new Map();
      const heritageVectorLayers = new Map();
      let lastHeritageGroup = "";
      for (const layerConfig of (config.heritageLayers || [])) {
        const heritageGroup = String(layerConfig.group || "Heritage context");
        if (heritageGroup !== lastHeritageGroup) {
          const groupTitle = document.createElement("div");
          groupTitle.style.fontWeight = "700";
          groupTitle.style.marginTop = "10px";
          groupTitle.style.color = "#374151";
          groupTitle.textContent = heritageGroup;
          heritageLayerRows.appendChild(groupTitle);
          lastHeritageGroup = heritageGroup;
        }
        const { checkbox, labelText } = makeLayerCheckbox(heritageLayerRows, layerConfig);

        if (layerConfig.kind === "wms") {
          try {
            const version = String(layerConfig.version || "1.3.0");
            const parameters = {
              transparent: layerConfig.transparent !== false,
              format: layerConfig.format || "image/png",
              styles: layerConfig.styles || "",
              version,
            };
            if (version.startsWith("1.3")) {
              parameters.crs = layerConfig.crs || "EPSG:3857";
            } else {
              parameters.srs = layerConfig.crs || "EPSG:3857";
            }
            const provider = new Cesium.WebMapServiceImageryProvider({
              url: layerConfig.url,
              layers: layerConfig.layers,
              parameters,
              tilingScheme: new Cesium.WebMercatorTilingScheme(),
              minimumLevel: Number(layerConfig.minimumLevel || 0),
              enablePickFeatures: false,
              credit: layerConfig.credit || "RCE / EduGIS",
            });
            const imagery = viewer.imageryLayers.addImageryProvider(provider);
            imagery.alpha = Number(layerConfig.alpha || 0.65);
            imagery.show = checkbox.checked;
            imagery._layerCheckbox = checkbox;
            heritageImageryLayers.set(layerConfig.id, imagery);
            checkbox.addEventListener("change", () => {
              imagery.show = checkbox.checked;
              viewer.scene.requestRender();
            });
          } catch (layerError) {
            checkbox.disabled = true;
            labelText.textContent += " — WMS could not be created";
            console.warn("Heritage WMS layer failed", layerConfig, layerError);
          }
        } else if (layerConfig.kind === "geojson") {
          try {
            const baseColor = Cesium.Color.fromCssColorString(layerConfig.color || "#d53e4f");
            const vectorSource = await Cesium.GeoJsonDataSource.load(
              layerConfig.featureCollection,
              {
                clampToGround: true,
                stroke: baseColor.withAlpha(Number(layerConfig.strokeAlpha || 0.95)),
                fill: baseColor.withAlpha(Number(layerConfig.fillAlpha || 0.24)),
                strokeWidth: Number(layerConfig.strokeWidth || 2),
              }
            );
            vectorSource.name = layerConfig.title;
            for (const entity of vectorSource.entities.values) {
              const properties = entity.properties;
              const now = Cesium.JulianDate.now();
              const featureFill = properties?.fill_color?.getValue(now);
              const entityColor = Array.isArray(featureFill) && featureFill.length >= 3
                ? Cesium.Color.fromBytes(
                    Number(featureFill[0]), Number(featureFill[1]), Number(featureFill[2]),
                    Number(featureFill[3] ?? 110)
                  )
                : baseColor.withAlpha(Number(layerConfig.fillAlpha || 0.24));
              if (entity.polygon) {
                entity.polygon.material = entityColor;
                entity.polygon.outline = true;
                entity.polygon.outlineColor = baseColor.withAlpha(Number(layerConfig.strokeAlpha || 0.95));
                entity.polygon.classificationType = Cesium.ClassificationType.TERRAIN;
                entity.polygon.zIndex = 30;
              }
              if (entity.polyline) {
                entity.polyline.material = baseColor.withAlpha(Number(layerConfig.strokeAlpha || 0.95));
                entity.polyline.width = Number(layerConfig.strokeWidth || 2);
                entity.polyline.clampToGround = true;
                entity.polyline.classificationType = Cesium.ClassificationType.TERRAIN;
              }
              if (entity.point) {
                entity.point.color = baseColor.withAlpha(0.95);
                entity.point.outlineColor = Cesium.Color.WHITE;
                entity.point.outlineWidth = 2;
                entity.point.pixelSize = 9;
                entity.point.heightReference = Cesium.HeightReference.CLAMP_TO_GROUND;
              }
              const monumentType = properties?.monument_type?.getValue(now) || layerConfig.title;
              const identifier = properties?.identificatie?.getValue(now) || "";
              const authority = properties?.authority_name?.getValue(now) || "";
              entity.description =
                `<strong>${escapeHtml(monumentType)}</strong><br>` +
                `Layer: ${escapeHtml(layerConfig.title)}<br>` +
                `Identifier: ${escapeHtml(identifier || "—")}<br>` +
                `Authority: ${escapeHtml(authority || "—")}<br>` +
                `<em>Cartographic derivative for visual inspection; verify against the authoritative register for legal use.</em>`;
            }
            await viewer.dataSources.add(vectorSource);
            vectorSource.show = checkbox.checked;
            vectorSource._layerCheckbox = checkbox;
            heritageVectorLayers.set(layerConfig.id, vectorSource);
            checkbox.addEventListener("change", () => {
              vectorSource.show = checkbox.checked;
              viewer.scene.requestRender();
            });
          } catch (vectorError) {
            checkbox.disabled = true;
            labelText.textContent += " — GeoJSON could not be created";
            console.warn("Heritage GeoJSON layer failed", layerConfig, vectorError);
          }
        } else {
          checkbox.checked = false;
          checkbox.disabled = true;
          labelText.textContent += " — MVT source; use the PostGIS overlay after import";
        }
      }
      if (!(config.heritageLayers || []).length) {
        heritageLayerRows.innerHTML =
          '<div class="layer-note" style="margin:8px 0 0 0;">No heritage layer definitions or PostGIS heritage overlays were loaded.</div>';
      }

      function selectionMetrics() {
        const rectangle = selectedRectangle();
        const center = Cesium.Rectangle.center(rectangle);
        const lonSpan = Math.max(rectangle.width, 0.0001);
        const latSpan = Math.max(rectangle.height, 0.0001);
        const angularSpan = Math.max(lonSpan, latSpan);
        const surfaceSpan = Cesium.Ellipsoid.WGS84.maximumRadius * angularSpan;
        const target = Cesium.Cartesian3.fromRadians(
          center.longitude, center.latitude, 0
        );
        return { rectangle, target, surfaceSpan };
      }

      function focusSelectedArea(mode = "tilted") {
        const { rectangle, target, surfaceSpan } = selectionMetrics();
        if (mode === "top") {
          viewer.camera.flyTo({
            destination: rectangle,
            orientation: {
              heading: 0.0,
              pitch: -Cesium.Math.PI_OVER_TWO,
              roll: 0.0,
            },
            duration: 0.9,
          });
          return;
        }
        const range = Math.max(surfaceSpan * 1.15, 3500);

        viewer.camera.flyToBoundingSphere(
          new Cesium.BoundingSphere(target, Math.max(surfaceSpan * 0.42, 1200)),
          {
            duration: 0.9,
            offset: new Cesium.HeadingPitchRange(0.0, -0.72, range),
          }
        );
      }

      function cameraStep() {
        const height = viewer.camera.positionCartographic.height;
        return Math.max(height * 0.35, 150);
      }

      viewer.scene.screenSpaceCameraController.enableZoom = true;
      viewer.scene.screenSpaceCameraController.enableRotate = true;
      viewer.scene.screenSpaceCameraController.enableTilt = true;
      viewer.scene.screenSpaceCameraController.minimumZoomDistance = 15;
      viewer.scene.screenSpaceCameraController.maximumZoomDistance = 3000000;

      let tileset = null;
      let heritageVisible = true;
      let adminOnlyMode = false;
      const heritageDataSource = new Cesium.CustomDataSource(
        "BAG-matched monument points"
      );
      const monumentEntities = [];

      for (const item of (config.heritagePoints || [])) {
        const entity = heritageDataSource.entities.add({
          name: item.name,
          position: Cesium.Cartesian3.fromDegrees(item.lon, item.lat, 0),
          point: {
            pixelSize: 11,
            color: Cesium.Color.fromCssColorString("#00e5ff"),
            outlineColor: Cesium.Color.fromCssColorString("#111827"),
            outlineWidth: 3,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          },
          properties: {
            layer: "Current Rijksmonument",
            monument_id: item.monument_id,
            status: item.status,
            function_type: item.function_type,
            registration_date: item.registration_date,
            pand_id: item.pand_id,
            display_mode: "2D ground point",
          },
          description:
            `<strong>${escapeHtml(item.name)}</strong><br>` +
            `Rijksmonument: ${escapeHtml(item.monument_id)}<br>` +
            `BAG pand: ${escapeHtml(item.pand_id)}<br>` +
            `Function: ${escapeHtml(item.function_type || "—")}<br>` +
            `Registration: ${escapeHtml(item.registration_date || "—")}`,
        });
        monumentEntities.push({ entity, item });
      }
      viewer.dataSources.add(heritageDataSource);
      if (!(config.heritagePoints || []).length) {
        document.getElementById("toggleHeritage").style.display = "none";
        document.getElementById("heritageLegendRow").style.display = "none";
      }

      // Monument markers intentionally remain 2D ground points.

      document.getElementById("toggleHeritage").addEventListener("click", () => {
        heritageVisible = !heritageVisible;
        heritageDataSource.show = heritageVisible;
        document.getElementById("toggleHeritage").textContent =
          heritageVisible ? "Hide monument points" : "Show monument points";
      });
      document.getElementById("toggleLayers").addEventListener("click", () => {
        layerPanel.style.display =
          layerPanel.style.display === "block" ? "none" : "block";
      });
      document.getElementById("admin2dTest").addEventListener("click", () => {
        adminOnlyMode = !adminOnlyMode;
        if (tileset) tileset.show = !adminOnlyMode;
        heritageDataSource.show = !adminOnlyMode && heritageVisible;
        for (const source of heritageVectorLayers.values()) {
          source.show = !adminOnlyMode && Boolean(source._layerCheckbox?.checked);
        }
        for (const imagery of heritageImageryLayers.values()) {
          imagery.show = !adminOnlyMode && Boolean(imagery._layerCheckbox?.checked);
        }
        for (const source of adminDataSources.values()) {
          source.show = adminOnlyMode || Boolean(source._layerCheckbox?.checked);
        }
        document.getElementById("cohortLegend").style.display =
          adminOnlyMode ? "none" : "block";
        document.getElementById("admin2dTest").textContent =
          adminOnlyMode ? "Restore 3D" : "Admin 2D test";
        if (adminOnlyMode) {
          viewer.scene.globe.show = true;
          viewer.scene.globe.depthTestAgainstTerrain = false;
          applyBasemapMode("detailed");
          document.getElementById("basemapMode").value = "detailed";
          const availableAdminSources = Array.from(adminDataSources.values());
          const focusAdminSource =
            availableAdminSources.find((source) =>
              String(source.name || "").toLowerCase().includes("wijk")
            ) ||
            availableAdminSources.find((source) =>
              String(source.name || "").toLowerCase().includes("municipality")
            ) ||
            availableAdminSources[0];
          if (focusAdminSource) {
            viewer.flyTo(focusAdminSource, {
              duration: 0.9,
              offset: new Cesium.HeadingPitchRange(
                0.0, -Cesium.Math.PI_OVER_TWO, 0.0
              ),
            });
          } else {
            focusSelectedArea("top");
          }
          const featureCount = (config.adminBoundaries || []).reduce(
            (total, layer) => total +
              Number(layer.featureCollection?.features?.length || 0),
            0
          );
          statusPanel.innerHTML =
            `<strong>Administrative 2D isolation test</strong><br>` +
            `Configured layers: ${(config.adminBoundaries || []).length} · ` +
            `features: ${featureCount}<br>` +
            `WGS84 extent: ${config.west.toFixed(4)}, ${config.south.toFixed(4)} — ` +
            `${config.east.toFixed(4)}, ${config.north.toFixed(4)}<br>` +
            `3DBAG and heritage are temporarily hidden.`;
        } else {
          applyBasemapMode("muted");
          document.getElementById("basemapMode").value = "muted";
          focusSelectedArea("tilted");
        }
        viewer.scene.requestRender();
      });

      document.getElementById("zoomIn").addEventListener("click", () => {
        viewer.camera.zoomIn(cameraStep());
      });
      document.getElementById("zoomOut").addEventListener("click", () => {
        viewer.camera.zoomOut(cameraStep());
      });
      document.getElementById("topView").addEventListener("click", () => {
        focusSelectedArea("top");
      });
      document.getElementById("tiltedView").addEventListener("click", () => {
        focusSelectedArea("tilted");
      });
      document.getElementById("focusSelection").addEventListener("click", () => {
        focusSelectedArea("tilted");
      });

      document.getElementById("focusDataset").addEventListener("click", () => {
        if (!tileset) return;
        viewer.zoomTo(
          tileset,
          new Cesium.HeadingPitchRange(
            0.5,
            -0.5,
            tileset.boundingSphere.radius * 0.05
          )
        );
      });

      try {
        tileset = await Cesium.Cesium3DTileset.fromUrl(config.tilesetUrl, {
          maximumScreenSpaceError: 8,
        });
        viewer.scene.primitives.add(tileset);
        tileset.show = !adminOnlyMode;
        tileset.colorBlendMode = Cesium.Cesium3DTileColorBlendMode.REPLACE;
        tileset.colorBlendAmount = 1.0;
        tileset.shadows = Cesium.ShadowMode.ENABLED;

        // The 3D viewer uses the exact analytical construction periods generated for
        // Indicator 4. Missing, zero, negative and future years are hidden rather
        // than presented as an "Unknown" cohort.
        const cohortColorConditions = (config.cohortDefinitions || []).map(
          (cohort) => [
            `\${oorspronkelijkbouwjaar} >= ${cohort.start} && ` +
              `\${oorspronkelijkbouwjaar} <= ${cohort.end}`,
            `color('${cohort.color}', 1.0)`
          ]
        );
        cohortColorConditions.push(["true", "color('rgba(0,0,0,0)')"]);

        const cohortShowConditions = (config.cohortDefinitions || []).map(
          (cohort) => [
            `\${oorspronkelijkbouwjaar} >= ${cohort.start} && ` +
              `\${oorspronkelijkbouwjaar} <= ${cohort.end}`,
            "true"
          ]
        );
        cohortShowConditions.push(["true", "false"]);

        tileset.style = new Cesium.Cesium3DTileStyle({
          color: { conditions: cohortColorConditions },
          show: { conditions: cohortShowConditions }
        });

        // CesiumJS 1.133 exposes `loadProgress`, not `tileLoadProgressEvent`.
        // Guard optional events so a missing diagnostic hook can never be reported
        // incorrectly as a failure to load the root 3DBAG tileset.
        const loadProgressEvent = tileset.loadProgress;
        if (loadProgressEvent && typeof loadProgressEvent.addEventListener === "function") {
          loadProgressEvent.addEventListener((pending, processing) => {
            statusPanel.innerHTML =
              `<strong>3DBAG ${escapeHtml(config.lodLabel)}</strong><br>` +
              `Release: ${escapeHtml(config.release)} · pending requests: ${pending} · processing: ${processing}<br>` +
              `Camera focus: ${escapeHtml(config.focusLabel)}<br>` +
              `Cohort field: oorspronkelijkbouwjaar · monument points: ` +
              `${(config.heritagePoints || []).length} · heritage overlays: ` +
              `${(config.heritageLayers || []).filter((layer) => ["wms", "geojson"].includes(layer.kind)).length} · ` +
              `2D administrative overlays: ${(config.adminBoundaries || []).length}<br>` +
              `Extent: ${config.west.toFixed(4)}, ${config.south.toFixed(4)} — ` +
              `${config.east.toFixed(4)}, ${config.north.toFixed(4)}`;
          });
        } else {
          console.warn("Cesium tileset.loadProgress event is unavailable; continuing without progress diagnostics.");
        }

        if (tileset.tileFailed && typeof tileset.tileFailed.addEventListener === "function") {
          tileset.tileFailed.addEventListener((error) => {
            console.error("3DBAG tile failed", error);
            statusPanel.innerHTML =
              `<strong>A 3DBAG child tile failed.</strong><br>` +
              `${escapeHtml(error?.message || error)}`;
            statusPanel.style.color = "#991b1b";
          });
        }

        // The municipality selection controls the initial camera. Reapply once
        // after the first tile requests begin, because Cesium can otherwise keep
        // the previous iframe camera for a moment during reruns.
        focusSelectedArea("tilted");
        window.setTimeout(() => focusSelectedArea("tilted"), 650);

        const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
        handler.setInputAction((movement) => {
          const picked = viewer.scene.pick(movement.position);

          if (!Cesium.defined(picked)) {
            featurePanel.style.display = "none";
            return;
          }

          if (picked.id && monumentEntities.some(({ entity }) => entity === picked.id)) {
            const entity = picked.id;
            const properties = entity.properties;
            const now = Cesium.JulianDate.now();
            featurePanel.innerHTML =
              `<strong>${escapeHtml(entity.name || "Selected monument")}</strong>` +
              `<table>` +
              `<tr><td>Layer</td><td>Current Rijksmonument</td></tr>` +
              `<tr><td>Monument ID</td><td>${escapeHtml(properties.monument_id?.getValue(now) || "")}</td></tr>` +
              `<tr><td>BAG pand</td><td>${escapeHtml(properties.pand_id?.getValue(now) || "")}</td></tr>` +
              `<tr><td>Function</td><td>${escapeHtml(properties.function_type?.getValue(now) || "")}</td></tr>` +
              `<tr><td>Display</td><td>${escapeHtml(properties.display_mode?.getValue(now) || "")}</td></tr>` +
              `</table>`;
            featurePanel.style.display = "block";
            return;
          }

          if (picked.id && picked.id.description) {
            try {
              const now = Cesium.JulianDate.now();
              const description = typeof picked.id.description.getValue === "function"
                ? picked.id.description.getValue(now)
                : picked.id.description;
              if (description) {
                featurePanel.innerHTML = String(description);
                featurePanel.style.display = "block";
                return;
              }
            } catch (_) { /* continue to 3DBAG feature inspection */ }
          }

          // Cesium 1.133 uses getPropertyIds() for Cesium3DTileFeature.
          // Retain getPropertyNames() as a compatibility fallback for older
          // tiles/content implementations.
          const getPropertyIds =
            typeof picked.getPropertyIds === "function"
              ? () => picked.getPropertyIds([])
              : typeof picked.getPropertyNames === "function"
                ? () => picked.getPropertyNames([])
                : null;
          if (!getPropertyIds) {
            featurePanel.style.display = "none";
            return;
          }

          const propertyNames = getPropertyIds() || [];
          const rows = propertyNames.slice(0, 35).map((name) => {
            let value = picked.getProperty(name);
            if (value && typeof value === "object") {
              try { value = JSON.stringify(value); }
              catch (_) { value = String(value); }
            }
            return `<tr><td>${escapeHtml(name)}</td><td>${escapeHtml(value)}</td></tr>`;
          });

          featurePanel.innerHTML =
            `<strong>Selected 3DBAG building</strong>` +
            (rows.length
              ? `<table>${rows.join("")}</table>`
              : `<div>No feature attributes were published for this tile.</div>`);
          featurePanel.style.display = "block";
        }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

      } catch (error) {
        console.error(error);
        statusPanel.innerHTML =
          `<strong>3DBAG viewer initialization failed.</strong><br>` +
          `${escapeHtml(error?.message || error)}<br>` +
          `The 2D administrative and heritage overlays are independent of the 3D tileset; use Layers to inspect any overlays that loaded successfully. ` +
          `Open the browser console (F12) for the complete error.`;
        statusPanel.style.color = "#991b1b";
        focusSelectedArea("tilted");
      }
    </script>
    '''

    html = html.replace("__CESIUM_VERSION__", CESIUM_VERSION)
    html = html.replace("__VIEWER_CONFIG__", json.dumps(viewer_config))

    components.html(html, height=760, scrolling=False)


@st.cache_data(ttl=60, show_spinner=False)
def inspect_local_3dbag_archives(folder):
    """Inventory local downloaded 3DBAG tile archives without extracting them."""
    folder_path = Path(folder)
    if not folder_path.exists():
        return pd.DataFrame(), "Folder does not exist on this computer."

    rows = []
    for archive in sorted(folder_path.glob("*.zip")):
        gpkg_members = []
        zip_error = None
        try:
            with zipfile.ZipFile(archive) as zf:
                gpkg_members = [n for n in zf.namelist() if n.lower().endswith(".gpkg")]
        except Exception as exc:
            zip_error = str(exc)

        rows.append({
            "archive": archive.name,
            "size_mb": round(archive.stat().st_size / (1024 * 1024), 1),
            "gpkg_inside": gpkg_members[0] if gpkg_members else None,
            "zip_error": zip_error,
        })

    return pd.DataFrame(rows), None



_STREAMLIT_FRAGMENT = getattr(st, "fragment", lambda function: function)


@_STREAMLIT_FRAGMENT
def render_historical_boundary_year_viewer(selected_municipalities):
    """Render one lightweight year-specific historical municipality map.

    The cached history query is executed once for the selected current footprint.
    Changing the year reruns only this fragment in Streamlit versions that support
    fragments, avoiding the former two-map browser payload and full app rerun.
    """
    selected_municipalities = list(selected_municipalities or [])
    if not selected_municipalities:
        st.info("Select at least one current municipality to inspect boundary history.")
        return

    try:
        evolution_df = load_historical_municipality_evolution(
            tuple(selected_municipalities)
        )
        current_df = load_current_municipality_boundaries(
            tuple(selected_municipalities)
        )
    except Exception as exc:
        st.error("The historical municipality boundary query failed.")
        st.code(str(exc))
        return

    if evolution_df.empty:
        st.warning(
            "No historical boundary rows overlap the selected current municipality "
            "footprint. Check the harmonised history table and municipality codes."
        )
        return

    available_years = sorted(
        year
        for year in pd.to_numeric(
            evolution_df["jaar"], errors="coerce"
        ).dropna().astype(int).unique().tolist()
        if ANALYSIS_START_YEAR <= year <= ANALYSIS_END_YEAR
    )
    if not available_years:
        st.warning("No boundary years are available inside the 2011–2025 study period.")
        return

    selected_year = st.select_slider(
        "Historical boundary year",
        options=available_years,
        value=available_years[-1],
        key="specialized_historical_boundary_year",
        help=(
            "Amber polygons are municipalities valid in the selected year that "
            "contribute spatially to the selected current municipality footprint."
        ),
    )

    year_df = evolution_df[
        pd.to_numeric(evolution_df["jaar"], errors="coerce") == selected_year
    ].copy()
    historical_collection = historical_boundaries_to_feature_collection(year_df)
    current_collection = current_boundaries_to_feature_collection(current_df)

    historical_layer = pdk.Layer(
        "GeoJsonLayer",
        id=f"historical-boundary-{selected_year}",
        data=historical_collection,
        pickable=True,
        auto_highlight=True,
        stroked=True,
        filled=True,
        get_fill_color=[245, 158, 11, 55],
        get_line_color=[120, 53, 15, 235],
        line_width_min_pixels=2,
    )
    current_layer = pdk.Layer(
        "GeoJsonLayer",
        id="current-boundary-reference",
        data=current_collection,
        pickable=True,
        auto_highlight=True,
        stroked=True,
        filled=False,
        get_line_color=[37, 99, 235, 245],
        line_width_min_pixels=4,
    )
    deck = pdk.Deck(
        layers=[historical_layer, current_layer],
        initial_view_state=derive_map_view_state(
            current_collection,
            historical_collection,
        ),
        tooltip={
            "html": """
                <b>{display_name}</b><br/>
                Validity: {startdatum} – {einddatum}<br/>
                CBS code: {municipality_code}<br/>
                Contributes to: {contributes_to}<br/>
                Geometry source year: {geometry_source_year}<br/>
                Source: {source_label}
            """,
            "style": {"backgroundColor": "white", "color": "black"},
        },
        map_style=None,
    )
    st.pydeck_chart(
        deck,
        use_container_width=True,
        key=f"historical_boundary_map_{selected_year}",
    )
    st.caption(
        "Amber: municipality polygons valid in the selected year. Blue: the "
        "selected present-day analytical footprint. Only overlapping predecessor "
        "municipalities are loaded."
    )

    boundary_table_columns = [
        column
        for column in [
            "naam",
            "code",
            "status",
            "startdatum",
            "einddatum",
            "contributes_to",
            "geometry_source_year",
            "geometry_source_name",
            "bron",
        ]
        if column in year_df.columns
    ]
    st.dataframe(
        year_df[boundary_table_columns].sort_values(
            [column for column in ["contributes_to", "naam"] if column in year_df.columns]
        ),
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# Persistent historical PAND/VBO explorer — outside main tabs
# ============================================================

@st.cache_data(ttl=900, show_spinner=False)
def load_current_context_pands(municipalities, year_reference=2025, maximum=None):
    """Dated BAG PAND snapshot, geographically focused near selected demolitions.

    Historical PAND versions are queried, never dt_frl.pand_current (which is
    not a reproducible 2025 snapshot). The All option has no row cap, although browser memory can
    be substantial. A finite cap still reports truncation. Historical VBO functions are linked as of end 2025.
    """
    qualified = "dt_frl.pand"
    if not relation_exists(qualified):
        return {"type": "FeatureCollection", "features": []}, "Historical BAG PAND versions unavailable"
    cols = set(get_relation_columns(qualified))
    required = {"identificatie", "geovlak", "begindatumtijdvakgeldigheid", "einddatumtijdvakgeldigheid", "pandstatus"}
    if not required.issubset(cols):
        return {"type": "FeatureCollection", "features": []}, "BAG historical snapshot fields unavailable"
    inactive = "AND p.aanduidingrecordinactief IS DISTINCT FROM TRUE" if "aanduidingrecordinactief" in cols else ""
    built = "p.bouwjaar" if "bouwjaar" in cols else "NULL::integer"
    # Prefer current administrative municipality geometry: covers areas even
    # where no demolition points exist. Fallback is a buffered demolition extent.
    admin_relation = "dt_frl_base.vw_gemeente_admin"
    admin_geom = None
    if relation_exists(admin_relation):
        admin_columns = set(get_relation_columns(admin_relation))
        admin_geom = next((c for c in ("geovlak", "geom", "geometry") if c in admin_columns), None)
    if admin_geom:
        extent_cte = f"""SELECT ST_Union(ST_Force2D(a.{admin_geom})) AS geom
            FROM {admin_relation} a
            WHERE a.gemeentenaam = ANY(:municipalities)"""
        spatial_condition = "p.geovlak && ext.geom AND ST_Intersects(ST_PointOnSurface(p.geovlak), ext.geom)"
    else:
        extent_cte = f"""SELECT ST_Expand(ST_Extent(d.point_rd)::geometry, 3000) AS geom
            FROM {DEMOLITION_RECORDS_TABLE} d
            WHERE d.point_rd IS NOT NULL
              AND d.current_gemeentenaam = ANY(:municipalities)
              AND d.demolition_year BETWEEN :start_year AND :end_year"""
        spatial_condition = "p.geovlak && ext.geom"
    limit_sql = "LIMIT :maximum" if maximum is not None else ""
    q = text(f"""
        WITH demolition_extent AS (
            {extent_cte}
        ), snapshot AS (
            SELECT DISTINCT ON (p.identificatie)
                p.identificatie::text AS pand_id, p.pandstatus::text AS pandstatus,
                {built} AS bouwjaar, p.geovlak
            FROM dt_frl.pand p CROSS JOIN demolition_extent ext
            WHERE p.geovlak IS NOT NULL AND NOT ST_IsEmpty(p.geovlak)
              AND ext.geom IS NOT NULL AND {spatial_condition}
              AND p.begindatumtijdvakgeldigheid < :snapshot_end
              AND (p.einddatumtijdvakgeldigheid IS NULL OR
                   p.einddatumtijdvakgeldigheid >= :snapshot_end)
              {inactive}
            ORDER BY p.identificatie, p.begindatumtijdvakgeldigheid DESC
        )
        SELECT pand_id, pandstatus, bouwjaar,
               ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(
                   ST_MakeValid(ST_Force2D(geovlak)), 0.15),4326),6) AS geojson
        FROM snapshot
        WHERE pandstatus IN ('Pand in gebruik','Pand in gebruik (niet ingemeten)',
          'Pand buiten gebruik','Verbouwing pand','Sloopvergunning verleend')
        ORDER BY pand_id {limit_sql}
    """)
    query_params = {
            "municipalities":list(municipalities),
            "start_year":int(start_year), "end_year":int(end_year),
            "snapshot_end":f"{year_reference}-12-31 23:59:59",
        }
    if maximum is not None:
        query_params["maximum"] = int(maximum) + 1
    try:
        data = pd.read_sql(q, engine, params=query_params)
    except Exception as exc:
        return {"type":"FeatureCollection","features":[]}, f"Historical snapshot unavailable: {type(exc).__name__}: {exc}"
    truncated = maximum is not None and len(data) > maximum
    # Historical VBO-to-PAND association and VBO functions as of 2025-12-31.
    # Restrict lookup to the actual PAND IDs fetched above to avoid province-wide
    # scans. Do not infer use from the PAND status or from spatial intersection.
    function_map = {}
    if not data.empty and all(relation_exists(x) for x in (
        'dt_frl.verblijfsobjectpand', 'dt_frl.verblijfsobjectgebruiksdoel'
    )):
        ids = data.iloc[:maximum if maximum is not None else None]['pand_id'].astype(str).tolist()
        try:
            usage_sql = text("""
                WITH linked AS (
                    SELECT DISTINCT vp.gerelateerdpand::text AS pand_id,
                                    vp.identificatie::text AS vbo_id
                    FROM dt_frl.verblijfsobjectpand vp
                    WHERE vp.gerelateerdpand::text = ANY(:pand_ids)
                      AND vp.aanduidingrecordinactief IS DISTINCT FROM TRUE
                      AND vp.begindatumtijdvakgeldigheid < :snapshot_end
                      AND (vp.einddatumtijdvakgeldigheid IS NULL
                           OR vp.einddatumtijdvakgeldigheid >= :snapshot_end)
                )
                SELECT l.pand_id,
                       STRING_AGG(DISTINCT g.gebruiksdoelverblijfsobject::text,
                                  ' + ' ORDER BY g.gebruiksdoelverblijfsobject::text) AS gebruiksdoel,
                       COUNT(DISTINCT l.vbo_id)::integer AS vbo_count
                FROM linked l
                LEFT JOIN dt_frl.verblijfsobjectgebruiksdoel g
                  ON g.identificatie::text = l.vbo_id
                 AND g.aanduidingrecordinactief IS DISTINCT FROM TRUE
                 AND g.begindatumtijdvakgeldigheid < :snapshot_end
                 AND (g.einddatumtijdvakgeldigheid IS NULL
                      OR g.einddatumtijdvakgeldigheid >= :snapshot_end)
                GROUP BY l.pand_id
            """)
            # Batching avoids oversized SQL array payloads and query planners
            # struggling with very long lists of identifiers.
            for i in range(0, len(ids), 2000):
                batch = pd.read_sql(usage_sql, engine, params={
                    'pand_ids': ids[i:i+2000],
                    'snapshot_end': f'{year_reference}-12-31 23:59:59',
                })
                for rec in batch.itertuples(index=False):
                    function_map[str(rec.pand_id)] = (
                        str(rec.gebruiksdoel) if pd.notna(rec.gebruiksdoel) else 'Onbekend',
                        int(rec.vbo_count),
                    )
        except Exception as exc:
            # A missing/bad historical VBO table must not hide 2025 PAND geometry.
            st.warning(f'2025 PAND footprints loaded; historical VBO function lookup unavailable: {exc}')
    features=[]
    for row in data.iloc[:maximum if maximum is not None else None].itertuples(index=False):
        try: geom=json.loads(row.geojson)
        except (ValueError, TypeError): continue
        features.append({"type":"Feature","geometry":geom,"properties":{
            "object_type":"PAND in 2025 reference snapshot", "object_id":str(row.pand_id),
            "pand_id":str(row.pand_id),"vbo_id":"","pandstatus":str(row.pandstatus),
            "bouwjaar":str(row.bouwjaar) if pd.notna(row.bouwjaar) else "",
            "demolition_date":"","demolition_year":"",
            "gebruiksdoel":function_map.get(str(row.pand_id), ('Onbekend',0))[0],
            "vbo_count":function_map.get(str(row.pand_id), ('Onbekend',0))[1],
            "wijknaam":"","buurtnaam":"", "reference_date":"2025-12-31",
        }})
    return {"type":"FeatureCollection","features":features}, (
        f"2025 reference · {len(features):,} context buildings"
        + (f" — WARNING: truncated at {maximum:,}; select All for no cap" if truncated else " — complete within selected demolition extent")
        + "; buildings considered within a 3 km buffer of the selected demolition extent (not a certified complete provincial stock)"
    )


def render_persistent_spatial_explorer():
    st.markdown('### Building-level demolition and VBO-function inspection')
    st.caption('Persistent spatial reference · synchronized with applied municipality and demolition-year filters. '
               'VBO records reflect historical relationships at the demolition event; the 2025 layer is a dated reference.')
    with st.expander('Map layers and inspection options', expanded=True):
        c1,c2,c3,c4=st.columns(4)
        show_pands=c1.checkbox('Demolished PAND polygons', value=True, key='persistent_demolished')
        show_vbos=c2.checkbox('Historical VBO points', value=True, key='persistent_vbos')
        show_current=c3.checkbox('2025 PAND reference',value=False,key='persistent_current')
        load_geometry=c4.toggle('Load map geometry',value=True,key='persistent_geometry')
        c5,c6=st.columns([2,1])
        with c5:
            selected_functions=st.multiselect('VBO use function filter (only VBO points)',
                options=sorted(FUNCTION_COLORS), default=[],
                key='persistent_function_filter',
                help='Leave empty for all use functions. PAND polygons always remain unchanged.')
        with c6:
            show_bag_helper=st.checkbox('BAG Viewer helper · docked',value=True,key='persistent_bag_helper')
        current_limit_choice=st.selectbox('2025 reference building limit',
            options=['15,000','50,000','150,000','All (no row limit)'],
            index=1,key='persistent_2025_limit',
            help='All has no SQL row limit. Very large GeoJSON datasets can slow down the browser; filtering by municipality is recommended.')
        current_limit=None if current_limit_choice.startswith('All') else int(current_limit_choice.replace(',',''))
        map_base=st.radio('Map background', ['Street map','Satellite / aerial imagery'],
            horizontal=True,key='persistent_basemap')
        floating_map=st.checkbox('Floating mini-map while reading indicators',
            value=False,key='persistent_floating_map',
            help='Keeps a second map visible while scrolling. The miniature has the same data and initial area, but manual pan/zoom is not synchronized.')
    if not load_geometry:
        st.info('Map geometry is paused. Enable “Load map geometry” to explore the selected scope.')
        return
    with st.spinner('Loading spatial features for the applied analysis filters…'):
        pand_fc,_=load_demolished_pand_footprints(tuple(selected_municipalities),start_year,end_year,geography_mode)
        vbos=(load_demolished_vbo_function_points(tuple(selected_municipalities),start_year,end_year,geography_mode)
              if show_vbos else pd.DataFrame())
    if show_vbos and not vbos.empty and selected_functions:
        fset=set(selected_functions)
        vbos=vbos[vbos['gebruiksdoel_set'].apply(lambda x:bool(fset.intersection(split_usage_set(x))))].copy()
    current_fc={"type":"FeatureCollection","features":[]}
    current_note=''
    if show_current:
        if not current_reference_municipalities:
            st.warning('No current municipality reference is available for this selection.')
        elif 'current_gemeentenaam' not in DEMOLITION_SOURCE_COLUMNS:
            st.warning('Current municipality fields missing; cannot safely locate 2025 context geometries.')
        else:
            current_fc,current_note=load_current_context_pands(tuple(current_reference_municipalities), maximum=current_limit)
            if 'WARNING: truncated' in current_note:
                st.warning(current_note)
            else:
                st.caption(current_note)
    layers=[]
    satellite_style = {
        "version": 8,
        "sources": {"satellite": {"type": "raster", "tileSize": 256,
            "tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"]}},
        "layers": [{"id": "satellite", "type": "raster", "source": "satellite"}],
    }
    background_style = satellite_style if map_base == 'Satellite / aerial imagery' else None
    if show_current and current_fc['features']:
        layers.append(pdk.Layer('GeoJsonLayer',id='reference-2025-pands',data=current_fc,
            filled=True,stroked=True,pickable=True,auto_highlight=True,
            get_fill_color=[80,135,166,30],get_line_color=[73,113,145,210],line_width_min_pixels=1.3))
    if show_pands and pand_fc.get('features'):
        layers.append(pdk.Layer('GeoJsonLayer',id='historical-demolition-pands',data=pand_fc,
            filled=True,stroked=True,pickable=True,auto_highlight=True,
            get_fill_color=[204,102,119,38],get_line_color=[153,68,85,240],line_width_min_pixels=1.7))
    if show_vbos and not vbos.empty:
        points=[]
        for r in vbos.itertuples(index=False):
            if pd.isna(r.lon) or pd.isna(r.lat): continue
            points.append({'lon':float(r.lon),'lat':float(r.lat),'object_type':'Historical VBO',
                'object_id':str(r.vbo_id),'vbo_id':str(r.vbo_id),'pand_id':str(r.pand_id),
                'bouwjaar':'','pandstatus':str(r.verblijfsobjectstatus),
                'demolition_date':str(r.demolition_date),
                'demolition_year':str(r.demolition_year),'gebruiksdoel':str(r.gebruiksdoel_set),
                'wijknaam':str(r.wijknaam or ''),'buurtnaam':str(r.buurtnaam or ''),
                'oppervlakte':str(r.oppervlakteverblijfsobject),
                'point_color':list(map(int,r.point_color))})
        if points:
            layers.append(pdk.Layer('ScatterplotLayer',id='historical-demolition-vbos',data=points,
                get_position='[lon, lat]',get_fill_color='point_color',get_radius=3,
                radius_min_pixels=2,radius_max_pixels=7,pickable=True,auto_highlight=True,
                stroked=False,filled=True))
    st.caption(f'Analytical source: {DEMOLITION_RECORDS_TABLE} · VBO-eligible demolished PANDs; invalid lifespan values are excluded only from age-dependent indicators.')
    st.caption(f"Applied: {', '.join(selected_municipalities)} · {start_year}–{end_year} · "
               f"{len(pand_fc.get('features',[])):,} demolition footprints · "
               f"{len(vbos):,} displayed VBO records")
    if not layers:
        st.info('No enabled map layers have geometries for the applied filter.')
    else:
        deck=pdk.Deck(layers=layers,initial_view_state=derive_map_view_state(pand_fc,current_fc),
            map_style=background_style,map_provider='mapbox' if isinstance(background_style,dict) else 'carto',tooltip={'html': '<b>{object_type}</b><br/>ID: {object_id}'
                '<br/>PAND: {pand_id}<br/>Status: {pandstatus}<br/>Demolition: {demolition_date}'
                '<br/>Construction: {bouwjaar}<br/>Use: {gebruiksdoel}<br/>Linked VBOs: {vbo_count}<br/>Wijk: {wijknaam}',
                'style':{'backgroundColor':'white','color':'black'}})
        try:
            event=st.pydeck_chart(deck,use_container_width=True,key='persistent_pand_vbo_map',
                                  on_select='rerun',selection_mode='single-object')
            selection=getattr(event,'selection',None)
            objects=(selection or {}).get('objects',{}) if selection else {}
            for records_clicked in objects.values():
                if records_clicked:
                    picked=records_clicked[0]
                    st.session_state['persistent_selected_object']=picked
                    props=picked.get('properties',picked) if isinstance(picked,dict) else {}
                    pand_id=str(props.get('pand_id') or props.get('object_id') or '').strip()
                    if pand_id and pand_id != st.session_state.get('persistent_bag_id',''):
                        st.session_state['persistent_bag_id']=pand_id
                    break
        except TypeError:
            st.pydeck_chart(deck,use_container_width=True,key='persistent_pand_vbo_map_fallback')
            st.caption('Map selection requires a Streamlit version supporting pydeck on_select.')
    if floating_map and layers:
        st.markdown('''<style>
          div.st-key-floating_spatial_reference {
            position: fixed; top: 94px; right: 22px; width: 350px;
            padding: 12px; background: white; border: 1px solid #b8c1ca;
            border-radius: 10px; z-index: 999; box-shadow: 0 4px 22px #0003;
          }
          @media (max-width: 900px) {
            div.st-key-floating_spatial_reference {width: 260px; right: 8px; top: 72px;}
          }
        </style>''', unsafe_allow_html=True)
        with st.container(key='floating_spatial_reference'):
            st.caption('Spatial reference · live layers, independent camera')
            small_deck=pdk.Deck(layers=layers,
                initial_view_state=derive_map_view_state(pand_fc,current_fc),
                map_style=background_style,map_provider='mapbox' if isinstance(background_style,dict) else 'carto',tooltip={'text':'{object_type}: {object_id}'})
            st.pydeck_chart(small_deck, use_container_width=True,
                            height=240,key='persistent_floating_map_chart')
            st.caption('Disable in Map layers to close')
    legend=['<b>Layers:</b>']
    if show_pands:legend.append('<span style="color:#994455">■ Demolished PAND</span>')
    if show_current:legend.append('<span style="color:#4981a5">■ 2025 PAND reference</span>')
    if show_vbos:
        if not vbos.empty:
            categories=sorted(vbos['display_function'].dropna().astype(str).unique())
            from html import escape
            for value in categories:
                color=FUNCTION_COLORS.get(value,'#7F7F7F')
                legend.append(f'<span style="color:{color}">●</span> {escape(value)}')
        else:legend.append('● VBO (none in scope)')
    st.markdown(' &nbsp; | &nbsp; '.join(legend),unsafe_allow_html=True)
    selected=st.session_state.get('persistent_selected_object')
    if selected:
        with st.expander('Selected BAG feature · attributes',expanded=True):
            if isinstance(selected,dict):
                attrs=selected.get('properties',selected)
                st.dataframe(pd.DataFrame([{'Attribute':k,'Value':str(v)} for k,v in attrs.items()]),
                             use_container_width=True,hide_index=True)
                identifier=attrs.get('pand_id') or attrs.get('object_id')
                if identifier:st.link_button('Inspect identifier in BAG Viewer',bag_viewer_url(identifier))
    if show_bag_helper:
        # This is a compact fixed dock: the selected BAG object remains inspectable
        # while reading the map and the rest of the application (Excel freeze-pane analogy).
        # The fixed dock is intentionally limited in height to avoid covering charts.
        st.markdown("""<style>
        div.st-key-persistent_bag_dock {
            position: fixed; bottom: 12px; right: 18px;
            z-index: 1000; width: min(470px, 94vw);
            background: rgba(255,255,255,.98); border: 1px solid #b5bec7;
            border-radius: 10px; padding: 7px 12px;
            box-shadow: 0 5px 18px rgba(0,0,0,.18);
            max-height: 57vh; overflow-y: auto;
        }
        @media (max-width: 760px) {
            div.st-key-persistent_bag_dock {bottom: 4px; right: 3%; width: 94%;}
        }
        </style>""", unsafe_allow_html=True)
        if 'persistent_bag_id' not in st.session_state:
            st.session_state['persistent_bag_id']=''
        with st.container(key='persistent_bag_dock'):
            with st.expander('BAG Viewer · pinned object inspection', expanded=False):
                st.text_input('PAND ID', key='persistent_bag_id',
                    help='Click a PAND or VBO on the map, or enter a PAND identifier.')
                input_id=st.session_state['persistent_bag_id'].strip()
                if input_id:
                    st.link_button('Open in BAG Viewer',bag_viewer_url(input_id))
                    if st.checkbox('Show historical BAG records',key='persistent_show_history'):
                        try:
                            hist=load_pand_history(input_id)
                            if hist.empty:
                                st.info('No local BAG historical versions found.')
                            else:
                                st.dataframe(hist,use_container_width=True,hide_index=True,
                                    height=min(320, 38*(len(hist)+1)))
                        except Exception as exc:
                            st.warning(f'BAG history lookup unavailable: {exc}')
                else:
                    st.caption('Click a PAND or VBO on the map to inspect its linked building.')


render_persistent_spatial_explorer()

# ============================================================
# Main tabs
# ============================================================

(
    tab_dashboard,
    tab_specialized,
    tab_data,
    tab_overview,
    tab_validation,
) = st.tabs(
    [
        "10 indicators",
        "Specialized spatial tools",
        "Data table",
        "Overview",
        "Validation and BAG audit",
    ]
)



# ------------------------------------------------------------
# 10 indicators dashboard
# ------------------------------------------------------------

with tab_dashboard:
    st.header("10 BAG-derived demolition indicators")
    st.caption(
        "This dashboard groups the promised ten indicators into four families. "
        "The applied municipality and year filters control every chart and map. "
        "Wijk and buurt representations use current CBS subdivisions."
    )

    dashboard_geo_options = ["Municipality (selected geography)"]
    if geography_mode == CURRENT_GEOGRAPHY_MODE:
        dashboard_geo_options.append("Current wijk reference")

    detail_control_1, detail_control_2 = st.columns([1, 1])
    with detail_control_1:
        dashboard_geo_label = st.selectbox(
            "Indicator chart geography",
            dashboard_geo_options,
            index=0,
            help=(
                "Municipality charts follow the selected municipality geography. "
                "Wijk charts use current CBS wijk boundaries."
            ),
            key="dashboard_chart_geography",
        )
    dashboard_geo_level = (
        "municipality"
        if dashboard_geo_label.startswith("Municipality")
        else "wijk"
    )

    dashboard_focus_municipality = None
    with detail_control_2:
        if dashboard_geo_level == "wijk":
            focus_options = (
                current_reference_municipalities
                if current_reference_municipalities
                else list(selected_municipalities)
            )
            dashboard_focus_municipality = st.selectbox(
                "Municipality for wijk charts",
                focus_options,
                index=0,
                key="dashboard_wijk_focus_municipality",
            )
        else:
            st.text_input(
                "Municipality for wijk charts",
                value="Not applicable at municipality level",
                disabled=True,
                key="dashboard_wijk_focus_disabled",
            )

    dashboard_records = records.copy()
    if (
        dashboard_geo_level == "wijk"
        and dashboard_focus_municipality
        and geography_mode == CURRENT_GEOGRAPHY_MODE
    ):
        dashboard_records = dashboard_records[
            dashboard_records["gemeentenaam"] == dashboard_focus_municipality
        ].copy()

    dashboard_records = format_geo_unit(dashboard_records, dashboard_geo_level)
    dashboard_records = add_construction_cohort(dashboard_records)
    municipality_names = sorted(
        records["gemeentenaam"].dropna().astype(str).unique().tolist()
    )
    municipality_color_map = categorical_color_map(
        municipality_names,
        MUNICIPALITY_COLORS,
    )
    geography_axis_label = chart_geo_axis_label(dashboard_geo_level)

    # Keep all selected municipalities for the municipal overview map. Separate
    # filtered copies are then used by wijk-level charts.
    stock_df_all = load_dashboard_current_stock_wijk(
        current_reference_municipalities
    )
    functional_df_all = load_dashboard_functional_profile(
        selected_municipalities,
        start_year,
        end_year,
        geography_mode,
    )
    functional_stock_df_all = load_dashboard_functional_stock(
        current_reference_municipalities
    )
    reg_delay_df_all = load_dashboard_registration_delay(
        selected_municipalities,
        start_year,
        end_year,
        geography_mode,
    )
    replacement_df_all = load_dashboard_replacement_lag(
        selected_municipalities,
        start_year,
        end_year,
        geography_mode,
    )
    historical_rate_df_all = load_historical_annual_demolition_rate(
        selected_municipalities,
        start_year,
        end_year,
    )

    stock_df = stock_df_all.copy()
    functional_df = functional_df_all.copy()
    reg_delay_df = reg_delay_df_all.copy()
    replacement_df = replacement_df_all.copy()

    if (
        dashboard_geo_level == "wijk"
        and dashboard_focus_municipality
        and geography_mode == CURRENT_GEOGRAPHY_MODE
    ):
        if not stock_df.empty:
            stock_df = stock_df[
                stock_df["gemeentenaam"] == dashboard_focus_municipality
            ].copy()
        if not functional_df.empty:
            functional_df = functional_df[
                functional_df["gemeentenaam"] == dashboard_focus_municipality
            ].copy()
        if not reg_delay_df.empty:
            reg_delay_df = reg_delay_df[
                reg_delay_df["gemeentenaam"] == dashboard_focus_municipality
            ].copy()
        if not replacement_df.empty:
            replacement_df = replacement_df[
                replacement_df["gemeentenaam"] == dashboard_focus_municipality
            ].copy()

    st.markdown("### Dashboard scope")
    selected_demolitions = int(dashboard_records["pand_id"].nunique())
    selected_stock = None
    if not stock_df.empty and "current_stock" in stock_df.columns:
        stock_values = pd.to_numeric(stock_df["current_stock"], errors="coerce")
        if stock_values.notna().any():
            selected_stock = int(stock_values.fillna(0).sum())

    selected_lifespan = pd.to_numeric(
        dashboard_records.get("lifespan_years"), errors="coerce"
    ).dropna()
    selected_intensity = (
        selected_demolitions / selected_stock * 1000
        if selected_stock and selected_stock > 0
        else None
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Demolished PANDs", f"{selected_demolitions:,}")
    c2.metric(
        "Current analytical stock",
        f"{selected_stock:,}" if selected_stock is not None else "N/A",
    )
    c3.metric(
        "Cumulative intensity / 1,000",
        f"{selected_intensity:.3f}" if selected_intensity is not None else "N/A",
    )
    c4.metric(
        "Mean lifespan",
        f"{selected_lifespan.mean():.1f} y"
        if not selected_lifespan.empty
        else "N/A",
    )
    c5.metric(
        "Median lifespan",
        f"{selected_lifespan.median():.1f} y"
        if not selected_lifespan.empty
        else "N/A",
    )
    st.caption(
        "PAND is the demolition statistical unit. Lifespan metrics use only records "
        "flagged as valid in the validated final demolition relation."
    )

    # Object-level extrema are descriptive supplements, not additional demolition
    # indicators. They are restricted to the same eligible dashboard population.
    with st.expander("Oldest / newest demolished PANDs · object inspection", expanded=False):
        st.caption(
            "Oldest/newest by registered construction year or valid lifespan use only "
            f"the {len(selected_lifespan):,} records with valid construction-age data "
            f"(of {selected_demolitions:,} eligible demolished PANDs). "
            "Earliest/latest registered demolition dates use the full eligible scope. "
            "Registered bouwjaar is not a historically verified construction date."
        )
        age_records = dashboard_records.loc[
            dashboard_records["lifespan_years"].notna()
            & dashboard_records["bouwjaar"].notna()
        ].copy()
        date_records = dashboard_records.loc[
            dashboard_records["demolition_date"].notna()
        ].copy()
        extreme_rows = []
        cases = [
            ("Oldest construction year (demolished)", age_records, "bouwjaar", True),
            ("Newest construction year (demolished)", age_records, "bouwjaar", False),
            ("Longest valid lifespan", age_records, "lifespan_years", False),
            ("Shortest positive lifespan", age_records, "lifespan_years", True),
            ("Earliest registered demolition", date_records, "demolition_date", True),
            ("Latest registered demolition", date_records, "demolition_date", False),
        ]
        for title, source, sort_col, ascending in cases:
            if source.empty:
                continue
            record = source.sort_values([sort_col, "pand_id"],
                ascending=[ascending, True], na_position="last").iloc[0]
            extreme_rows.append({
                "Measure": title,
                "PAND ID": str(record["pand_id"]),
                "Registered bouwjaar": record.get("bouwjaar"),
                "Demolition date": str(record.get("demolition_date") or ""),
                "Lifespan (years)": record.get("lifespan_years"),
                "Municipality": record.get("gemeentenaam"),
                "BAG Viewer": bag_viewer_url(record["pand_id"]),
            })
        if extreme_rows:
            st.dataframe(pd.DataFrame(extreme_rows), use_container_width=True,
                hide_index=True, column_config={
                    "BAG Viewer": st.column_config.LinkColumn("BAG Viewer",display_text="Inspect")
                })
        else:
            st.info("No eligible PAND records for the selected period and geography.")

    st.markdown("### Municipality summary map")

    if geography_mode == HISTORICAL_GEOGRAPHY_MODE:
        st.caption(
            "A range-wide historical choropleth would combine incompatible boundary "
            "systems. Select one year: the numerator, denominator and municipality "
            "polygons then all use that year's administrative geography."
        )
        historical_map_year = st.selectbox(
            "Historical municipality map year",
            options=list(range(start_year, end_year + 1)),
            index=max(0, end_year - start_year),
            key="dashboard_historical_map_year",
        )
        municipality_boundaries = (
            load_dashboard_historical_municipality_boundaries(
                historical_map_year,
                tuple(selected_municipalities),
            )
        )
        map_records = records[
            records["demolition_year"] == historical_map_year
        ].copy()
        map_reg_delay = reg_delay_df_all[
            reg_delay_df_all["demolition_year"] == historical_map_year
        ].copy() if not reg_delay_df_all.empty else pd.DataFrame()
        map_replacement = replacement_df_all[
            replacement_df_all["demolition_year"] == historical_map_year
        ].copy() if not replacement_df_all.empty else pd.DataFrame()

        municipality_summary = build_municipality_indicator_summary(
            municipality_boundaries,
            map_records,
            pd.DataFrame(),
            map_reg_delay,
            map_replacement,
        )
        rate_year = historical_rate_df_all[
            historical_rate_df_all["demolition_year"] == historical_map_year
        ].copy()
        if not rate_year.empty:
            municipality_summary = municipality_summary.merge(
                rate_year[
                    [
                        "gemeentecode",
                        "stock_at_start_of_year",
                        "demolition_rate_per_1000_start_stock",
                    ]
                ],
                on="gemeentecode",
                how="left",
            )

        municipality_metric_options = {
            "Demolished panden in selected year": (
                "demolished_panden",
                f"Demolished panden ({historical_map_year})",
            ),
            "Annual demolition rate per 1,000 start stock": (
                "demolition_rate_per_1000_start_stock",
                "Annual rate / 1,000 start stock",
            ),
            "Mean lifespan at demolition": (
                "mean_lifespan_years",
                "Mean lifespan (years)",
            ),
            "Median lifespan at demolition": (
                "median_lifespan_years",
                "Median lifespan (years)",
            ),
            "Mean BAG registration delay": (
                "mean_registration_delay_days",
                "Mean registration delay (days)",
            ),
            "Temporal substitution share": (
                "temporal_substitution_share_pct",
                "Temporal substitution share (%)",
            ),
        }
    else:
        st.caption(
            "Each polygon is a current municipality. Values aggregate historical "
            "events into present-day territory for comparison."
        )
        municipality_boundaries = load_dashboard_municipality_boundaries(
            tuple(selected_municipalities)
        )
        municipality_summary = build_municipality_indicator_summary(
            municipality_boundaries,
            records,
            stock_df_all,
            reg_delay_df_all,
            replacement_df_all,
        )
        municipality_metric_options = {
            "Demolished panden": (
                "demolished_panden",
                "Demolished panden",
            ),
            "Cumulative intensity per 1,000 current panden": (
                "demolition_rate_per_1000",
                "Cumulative intensity / 1,000 current panden",
            ),
            "Mean lifespan at demolition": (
                "mean_lifespan_years",
                "Mean lifespan (years)",
            ),
            "Median lifespan at demolition": (
                "median_lifespan_years",
                "Median lifespan (years)",
            ),
            "Mean BAG registration delay": (
                "mean_registration_delay_days",
                "Mean registration delay (days)",
            ),
            "Temporal substitution share": (
                "temporal_substitution_share_pct",
                "Temporal substitution share (%)",
            ),
        }

    selected_map_metric = st.selectbox(
        "Municipality map metric",
        list(municipality_metric_options),
        key=f"dashboard_municipality_map_metric_{geography_mode}",
    )
    metric_column, metric_label = municipality_metric_options[
        selected_map_metric
    ]

    if metric_column not in municipality_summary.columns:
        municipality_summary[metric_column] = pd.NA

    municipality_map_collection = municipality_summary_to_feature_collection(
        municipality_summary,
        metric_column,
        metric_label,
    )
    render_municipality_indicator_map(
        municipality_map_collection,
        metric_label,
        key=f"municipality_summary_{metric_column}_{geography_mode}",
    )

    with st.expander("Municipality summary table"):
        summary_columns = [
            column
            for column in [
                "gemeentenaam",
                "boundary_year",
                "demolished_panden",
                "stock_at_start_of_year",
                "current_stock",
                "demolition_rate_per_1000_start_stock",
                "demolition_rate_per_1000",
                "mean_lifespan_years",
                "median_lifespan_years",
                "mean_registration_delay_days",
                "temporal_substitution_share_pct",
            ]
            if column in municipality_summary.columns
        ]
        st.dataframe(
            municipality_summary[summary_columns].sort_values(
                metric_column,
                ascending=False,
                na_position="last",
            ),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("### Current wijk and buurt indicator maps")
    st.caption(
        "These maps use the same applied municipality and demolition-year filters "
        "as the ten indicators. They use current CBS wijk and buurt geometries, "
        "including when municipal totals are shown in historical geography mode."
    )

    if not current_reference_municipalities:
        st.info("No current municipality footprint is available for wijk/buurt mapping.")
    else:
        admin_map_metric = st.selectbox(
            "Wijk/buurt map indicator",
            ["Demolished panden", "Mean lifespan at demolition"],
            key="indicator_admin_map_metric",
        )
        wijk_indicator_df = load_aggregated_geojson(
            "wijk",
            tuple(current_reference_municipalities),
            start_year,
            end_year,
        )
        buurt_indicator_df = load_aggregated_geojson(
            "buurt",
            tuple(current_reference_municipalities),
            start_year,
            end_year,
        )

        if admin_map_metric == "Demolished panden":
            admin_metric_column = "demolished_panden"
        else:
            admin_metric_column = "mean_lifespan_years"

        combined_values = pd.concat(
            [
                pd.to_numeric(
                    frame.get(admin_metric_column), errors="coerce"
                )
                for frame in [wijk_indicator_df, buurt_indicator_df]
                if not frame.empty
            ],
            ignore_index=True,
        ) if (not wijk_indicator_df.empty or not buurt_indicator_df.empty) else pd.Series(dtype=float)
        shared_max = (
            float(combined_values.max())
            if not combined_values.empty and combined_values.notna().any()
            else 0.0
        )

        for frame in [wijk_indicator_df, buurt_indicator_df]:
            if frame.empty:
                continue
            frame["fill_color"] = frame[admin_metric_column].apply(
                lambda value: (
                    [230, 230, 230, 80]
                    if pd.isna(value)
                    else color_from_count(float(value), shared_max)
                )
            )

        wijk_indicator_collection = dataframe_to_feature_collection(
            wijk_indicator_df
        )
        buurt_indicator_collection = dataframe_to_feature_collection(
            buurt_indicator_df
        )
        wijk_map_tab, buurt_map_tab = st.tabs(["Wijk indicator map", "Buurt indicator map"])
        with wijk_map_tab:
            make_geojson_map(
                wijk_indicator_collection,
                f"{admin_map_metric} by current wijk · {start_year}–{end_year}",
                map_key=f"indicator_wijk_{admin_metric_column}_{start_year}_{end_year}",
            )
        with buurt_map_tab:
            make_geojson_map(
                buurt_indicator_collection,
                f"{admin_map_metric} by current buurt · {start_year}–{end_year}",
                map_key=f"indicator_buurt_{admin_metric_column}_{start_year}_{end_year}",
            )

    st.divider()
    st.markdown("## Family 1 · Temporal loss")

    st.markdown("### 1. Annual demolition count")
    annual_counts = (
        dashboard_records.groupby(["gemeentenaam", "demolition_year"])
        .size()
        .reset_index(name="demolished_panden")
        .sort_values(["gemeentenaam", "demolition_year"])
    )
    fig_annual = px.line(
        annual_counts,
        x="demolition_year",
        y="demolished_panden",
        color="gemeentenaam",
        color_discrete_map=municipality_color_map,
        markers=True,
        title=(
            "Annual demolition count by municipality · "
            + (
                "municipality at demolition"
                if geography_mode == HISTORICAL_GEOGRAPHY_MODE
                else "current municipality territory"
            )
        ),
        labels={
            "demolition_year": "Year",
            "demolished_panden": "Demolished panden",
            "gemeentenaam": "Municipality",
        },
    )
    fig_annual.update_layout(height=680, legend_title_text="Municipality")
    style_municipality_line_traces(fig_annual, municipality_names)
    render_plotly_chart(fig_annual, use_container_width=True)

    st.markdown("### 2. Demolition rate")
    if geography_mode == HISTORICAL_GEOGRAPHY_MODE:
        if historical_rate_df_all.empty:
            st.warning(
                "The historical annual-rate view is unavailable. Create "
                "`thesis_ch4.vw_dashboard_annual_demolition_rate_historical`."
            )
        else:
            rate_df = historical_rate_df_all.copy()
            if dashboard_geo_level == "municipality":
                fig_rate = px.line(
                    rate_df,
                    x="demolition_year",
                    y="demolition_rate_per_1000_start_stock",
                    color="gemeentenaam",
                    color_discrete_map=municipality_color_map,
                    markers=True,
                    title=(
                        "Annual demolition rate using event-time municipality "
                        "and stock at 1 January"
                    ),
                    labels={
                        "demolition_year": "Year",
                        "demolition_rate_per_1000_start_stock":
                            "Demolitions per 1,000 start-stock panden",
                        "gemeentenaam": "Historical municipality",
                    },
                )
                fig_rate.update_layout(height=680)
                style_municipality_line_traces(fig_rate, municipality_names)
                render_plotly_chart(fig_rate, use_container_width=True)
            else:
                st.info(
                    "A historical annual denominator is available at municipality "
                    "level. Wijk rates remain current-reference because annual "
                    "historical wijk boundaries are not loaded."
                )
    else:
        if stock_df.empty:
            st.warning(
                "To show current-reference demolition intensity, first create "
                "`thesis_ch4.vw_dashboard_current_stock_wijk_final`."
            )
        else:
            if dashboard_geo_level == "municipality":
                demolitions_by_unit = (
                    dashboard_records.groupby("gemeentenaam")
                    .size()
                    .reset_index(name="demolished_panden")
                    .rename(columns={"gemeentenaam": "geo_unit"})
                )
                stock_by_unit = (
                    stock_df.groupby("gemeentenaam", as_index=False)["current_stock"]
                    .sum()
                    .rename(columns={"gemeentenaam": "geo_unit"})
                )
            else:
                demolitions_by_unit = (
                    dashboard_records.groupby("wijknaam", dropna=False)
                    .size()
                    .reset_index(name="demolished_panden")
                    .rename(columns={"wijknaam": "geo_unit"})
                )
                stock_by_unit = (
                    stock_df.groupby("wijknaam", as_index=False)["current_stock"]
                    .sum()
                    .rename(columns={"wijknaam": "geo_unit"})
                )
                demolitions_by_unit["geo_unit"] = demolitions_by_unit[
                    "geo_unit"
                ].fillna("Onbekende wijk")
                stock_by_unit["geo_unit"] = stock_by_unit[
                    "geo_unit"
                ].fillna("Onbekende wijk")

            rate_df = demolitions_by_unit.merge(
                stock_by_unit,
                on="geo_unit",
                how="left",
            )
            rate_df["demolition_rate_per_1000"] = (
                rate_df["demolished_panden"]
                / rate_df["current_stock"]
                * 1000
            )
            rate_df = rate_df.sort_values(
                "demolition_rate_per_1000",
                ascending=False,
            )

            fig_rate = px.bar(
                rate_df,
                x="geo_unit",
                y="demolition_rate_per_1000",
                color="demolition_rate_per_1000",
                color_continuous_scale=TERRACOTTA_SCALE,
                hover_data={
                    "geo_unit": False,
                    "demolished_panden": ":,",
                    "current_stock": ":,",
                    "demolition_rate_per_1000": ":.2f",
                },
                title=(
                    "Cumulative demolition intensity per 1,000 "
                    "current buildings"
                ),
                labels={
                    "geo_unit": geography_axis_label,
                    "demolition_rate_per_1000":
                        "Demolitions per 1,000 current panden",
                    "demolished_panden": "Demolished panden",
                    "current_stock": "Current pand stock",
                },
            )
            fig_rate.update_layout(showlegend=False, coloraxis_showscale=False, height=680)
            style_categorical_xaxis(
                fig_rate,
                rate_df["geo_unit"].nunique(),
                geography_axis_label,
            )
            render_plotly_chart(fig_rate, use_container_width=True)

    st.divider()
    st.markdown("## Family 2 · Building-age loss")

    st.markdown("### 3. Building age at demolition")
    fig_age = px.histogram(
        dashboard_records,
        x="lifespan_years",
        nbins=40,
        opacity=0.90,
        title="Building age at demolition",
        labels={
            "lifespan_years": "Lifespan at demolition (years)",
            "count": "Demolished panden",
        },
    )
    fig_age.update_traces(
        marker_color="#CC6677",
        marker_line_color="#994455",
        marker_line_width=0.6,
    )
    fig_age.update_layout(height=680, showlegend=False)
    render_plotly_chart(fig_age, use_container_width=True)

    st.markdown("### 4. Cohort-specific loss")
    valid_cohort_records = dashboard_records.dropna(
        subset=["construction_cohort_order", "construction_cohort_label"]
    ).copy()
    cohort_df = (
        valid_cohort_records.groupby(
            ["construction_cohort_order", "construction_cohort_label"],
            as_index=False,
        )
        .size()
        .rename(columns={"size": "demolished_panden"})
        .sort_values("construction_cohort_order")
    )
    excluded_cohort_records = int(
        dashboard_records["construction_cohort_order"].isna().sum()
    )
    st.caption(
        f"Nine shared construction periods are used in this chart and in the 3D "
        f"viewer. Exact bouwjaar remains available in the underlying records. "
        f"{excluded_cohort_records:,} record(s) with missing or invalid bouwjaar "
        "are excluded and reported in Validation."
    )
    if cohort_df.empty:
        st.info("No valid construction-year cohorts are available for this scope.")
    else:
        cohort_labels = cohort_df["construction_cohort_label"].astype(str).tolist()
        cohort_color_map = sequential_color_map(
            cohort_labels,
            TERRACOTTA_SCALE,
        )
        fig_cohort = px.bar(
            cohort_df,
            x="construction_cohort_label",
            y="demolished_panden",
            color="construction_cohort_label",
            color_discrete_map=cohort_color_map,
            text_auto=",.0f",
            category_orders={
                "construction_cohort_label": cohort_labels
            },
            title="Cohort-specific loss by construction period",
            labels={
                "construction_cohort_label": "Construction period",
                "demolished_panden": "Demolished panden",
            },
        )
        fig_cohort.update_traces(textposition="outside", cliponaxis=False)
        fig_cohort.update_layout(showlegend=False, height=660)
        fig_cohort.update_xaxes(tickangle=0)
        render_plotly_chart(fig_cohort, use_container_width=True)

    # Spatial extension requested in the thesis review: normalize one old-building
    # cohort and make its disappearance legible at buurt level without changing the
    # ten-indicator numbering. The thesis figure uses the complete 2011–2025 window
    # so its denominator and numerator remain fixed even when dashboard year filters
    # are temporarily changed for other indicators.
    st.markdown("#### Spatial extension · observed pre-1945 cohort loss by current buurt")
    st.caption(
        "This map normalizes pre-1945 demolition by the observed bounded cohort: "
        "D / (D + S) × 100, where D is a pre-1945 BAG pand demolished in 2011–2025 "
        "and S is a currently surviving pre-1945 BAG pand. It is not a reconstructed "
        "1 January 2011 stock-depletion rate because BAG does not observe buildings "
        "demolished before the historical source window."
    )

    cohort_map_options = list(current_reference_municipalities)
    if not cohort_map_options:
        st.info("No current municipality footprint is available for the buurt cohort-loss map.")
    else:
        default_cohort_map_index = (
            cohort_map_options.index("Leeuwarden")
            if "Leeuwarden" in cohort_map_options
            else 0
        )
        cohort_map_municipality = st.selectbox(
            "Municipality for thesis buurt cohort-loss map",
            options=cohort_map_options,
            index=default_cohort_map_index,
            key="thesis_pre1945_cohort_loss_municipality",
            help=(
                "The thesis figure should use one municipality at a time. Leeuwarden "
                "is selected by default when it is part of the applied current geography."
            ),
        )
        pre1945_df, pre1945_collection, pre1945_error = (
            load_case_cohort_decline_by_buurt(
                cohort_map_municipality,
                1,
                1945,
                ANALYSIS_START_YEAR,
                ANALYSIS_END_YEAR,
            )
        )
        if pre1945_error:
            st.warning(f"Pre-1945 buurt cohort-loss map: {pre1945_error}")
        elif pre1945_df.empty:
            st.info("No pre-1945 cohort observations were available for this municipality.")
        else:
            total_d = int(pre1945_df["demolished"].sum())
            total_s = int(pre1945_df["surviving"].sum())
            total_n = total_d + total_s
            total_loss_pct = (total_d / total_n * 100) if total_n > 0 else None
            valid_buurt_loss = pre1945_df[
                pre1945_df["bounded_cohort_population"] >= 10
            ].copy()
            highest_buurt = (
                valid_buurt_loss.sort_values(
                    ["cohort_decline_pct", "bounded_cohort_population"],
                    ascending=[False, False],
                ).iloc[0]
                if not valid_buurt_loss.empty
                else None
            )

            cohort_metric_1, cohort_metric_2, cohort_metric_3, cohort_metric_4 = st.columns(4)
            cohort_metric_1.metric("D · demolished pre-1945", f"{total_d:,}")
            cohort_metric_2.metric("S · surviving pre-1945", f"{total_s:,}")
            cohort_metric_3.metric("N · observed cohort", f"{total_n:,}")
            cohort_metric_4.metric(
                "Observed loss share",
                f"{total_loss_pct:.1f}%" if total_loss_pct is not None else "–",
            )

            pre1945_fig = build_cohort_decline_choropleth(
                pre1945_df,
                cohort_map_municipality,
                "pre-1945",
                ANALYSIS_START_YEAR,
                ANALYSIS_END_YEAR,
            )
            if pre1945_fig is not None:
                render_plotly_chart(pre1945_fig, use_container_width=True)

            sparse_count = int(
                (
                    pre1945_df["bounded_cohort_population"].gt(0)
                    & pre1945_df["bounded_cohort_population"].lt(10)
                ).sum()
            )
            no_cohort_count = int(
                pre1945_df["bounded_cohort_population"].le(0).sum()
            )
            st.caption(
                "Colour uses a fixed 0–100% scale for buurten with N ≥ 10. "
                f"{sparse_count} buurt(en) with 0 < N < 10 are shown in grey rather "
                "than on the percentage colour scale because their rates are highly "
                f"sensitive to one or two events; {no_cohort_count} buurt(en) have N = 0. "
                "Exact sparse-denominator percentages remain available in hover and "
                "the ranking table. The named bands are an IUCN-inspired interpretive "
                "aid, not an application of IUCN extinction-risk methodology."
            )

            if highest_buurt is not None:
                st.markdown(
                    f"**Highest mapped buurt loss with N ≥ 10:** "
                    f"{highest_buurt['buurtnaam']} — "
                    f"{float(highest_buurt['cohort_decline_pct']):.1f}% "
                    f"(D={int(highest_buurt['demolished'])}, "
                    f"S={int(highest_buurt['surviving'])}, "
                    f"N={int(highest_buurt['bounded_cohort_population'])})."
                )

            ranking_table = pre1945_df.copy()
            ranking_table = ranking_table[
                [
                    "buurtnaam",
                    "buurtcode",
                    "demolished",
                    "surviving",
                    "bounded_cohort_population",
                    "cohort_decline_pct",
                    "loss_band",
                    "denominator_note",
                ]
            ].sort_values(
                ["cohort_decline_pct", "bounded_cohort_population"],
                ascending=[False, False],
                na_position="last",
            )
            ranking_table["cohort_decline_pct"] = ranking_table[
                "cohort_decline_pct"
            ].round(1)
            st.markdown("**Buurt ranking · highest to lowest observed pre-1945 loss**")
            st.dataframe(
                ranking_table,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "buurtnaam": "Buurt",
                    "buurtcode": "Buurt code",
                    "demolished": "D · demolished",
                    "surviving": "S · surviving",
                    "bounded_cohort_population": "N · D + S",
                    "cohort_decline_pct": st.column_config.NumberColumn(
                        "Observed loss (%)", format="%.1f%%"
                    ),
                    "loss_band": "Interpretive band",
                    "denominator_note": "Denominator note",
                },
            )
            st.download_button(
                "Download pre-1945 buurt cohort-loss table (CSV)",
                data=ranking_table.to_csv(index=False).encode("utf-8"),
                file_name=(
                    f"pre1945_buurt_cohort_loss_{cohort_map_municipality.lower().replace(' ', '_')}_"
                    f"{ANALYSIS_START_YEAR}_{ANALYSIS_END_YEAR}.csv"
                ),
                mime="text/csv",
                key="download_pre1945_buurt_cohort_loss",
            )

            with st.expander("Methodological interpretation of the buurt cohort-loss map"):
                st.markdown(
                    """
- **Numerator (D):** distinct BAG `pand` demolition events with `bouwjaar ≤ 1945` observed between 2011 and 2025 and spatially assigned to the current buurt.
- **Surviving stock (S):** distinct current BAG `pand` objects with `bouwjaar ≤ 1945` and an active lifecycle status, spatially assigned to the same current buurt.
- **Indicator:** `D / (D + S) × 100`. This is a bounded observational loss share, not a true 2011 baseline decline rate.
- **Spatial reference:** current CBS buurt geometry is used to make the result legible and comparable at neighborhood scale.
- **Interpretive bands:** 30/50/70/90% breakpoints are retained only as an IUCN-inspired descriptive framing. They are not biological risk categories and do not imply heritage significance.
- **Small denominators:** buurten with `N < 10` are flagged because a few buildings can produce an unstable percentage.
- **Heritage limit:** overlap with Wkpb or another heritage layer indicates spatial/legal context only; it does not establish irregular demolition or loss of heritage value.
                    """
                )

    st.markdown("### 5. Mean lifespan")
    mean_df = (
        dashboard_records.groupby("geo_unit")
        .agg(mean_lifespan=("lifespan_years", "mean"))
        .reset_index()
        .sort_values("mean_lifespan", ascending=False)
    )
    fig_mean = px.bar(
        mean_df,
        x="geo_unit",
        y="mean_lifespan",
        color="mean_lifespan",
        color_continuous_scale=TERRACOTTA_SCALE,
        text_auto=".1f",
        title=f"Mean lifespan by {dashboard_geo_level}",
        labels={
            "geo_unit": geography_axis_label,
            "mean_lifespan": "Mean lifespan (years)",
        },
    )
    fig_mean.update_traces(textposition="outside", cliponaxis=False)
    fig_mean.update_layout(showlegend=False, coloraxis_showscale=False, height=680)
    style_categorical_xaxis(
        fig_mean, mean_df["geo_unit"].nunique(), geography_axis_label
    )
    render_plotly_chart(fig_mean, use_container_width=True)

    st.markdown("### 6. Median lifespan")
    median_df = (
        dashboard_records.groupby("geo_unit")
        .agg(median_lifespan=("lifespan_years", "median"))
        .reset_index()
        .sort_values("median_lifespan", ascending=False)
    )
    fig_median = px.bar(
        median_df,
        x="geo_unit",
        y="median_lifespan",
        color="median_lifespan",
        color_continuous_scale=TERRACOTTA_SCALE,
        text_auto=".1f",
        title=f"Median lifespan by {dashboard_geo_level}",
        labels={
            "geo_unit": geography_axis_label,
            "median_lifespan": "Median lifespan (years)",
        },
    )
    fig_median.update_traces(textposition="outside", cliponaxis=False)
    fig_median.update_layout(showlegend=False, coloraxis_showscale=False, height=680)
    style_categorical_xaxis(
        fig_median, median_df["geo_unit"].nunique(), geography_axis_label
    )
    render_plotly_chart(fig_median, use_container_width=True)

    st.divider()
    st.markdown("## Family 3 · Functional loss")

    st.markdown("### 7. Functional demolition profile")
    if functional_df.empty:
        st.warning(
            "To show the functional demolition profile, create the helper view "
            "`thesis_ch4.vw_dashboard_functional_loss_final`."
        )
    else:
        if dashboard_geo_level == "municipality":
            if "fractional_weight" in functional_df.columns:
                functional_chart = (
                    functional_df.groupby(["gemeentenaam", "gebruiksdoel"])["fractional_weight"]
                    .sum()
                    .reset_index(name="demolished_panden_equivalent")
                    .rename(columns={"gemeentenaam": "geo_unit"})
                )
            else:
                functional_chart = (
                    functional_df.groupby(["gemeentenaam", "gebruiksdoel"])
                    .size()
                    .reset_index(name="demolished_panden_equivalent")
                    .rename(columns={"gemeentenaam": "geo_unit"})
                )
        else:
            if "fractional_weight" in functional_df.columns:
                functional_chart = (
                    functional_df.groupby(["wijknaam", "gebruiksdoel"], dropna=False)["fractional_weight"]
                    .sum()
                    .reset_index(name="demolished_panden_equivalent")
                    .rename(columns={"wijknaam": "geo_unit"})
                )
            else:
                functional_chart = (
                    functional_df.groupby(["wijknaam", "gebruiksdoel"], dropna=False)
                    .size()
                    .reset_index(name="demolished_panden_equivalent")
                    .rename(columns={"wijknaam": "geo_unit"})
                )
            functional_chart["geo_unit"] = functional_chart["geo_unit"].fillna("Onbekende wijk")

        functional_chart["gebruiksdoel"] = (
            functional_chart["gebruiksdoel"].fillna("Onbekend").astype(str)
        )
        function_values = functional_chart["gebruiksdoel"]
        function_color_map = {
            value: FUNCTION_COLORS.get(
                value,
                MUNICIPALITY_COLORS[index % len(MUNICIPALITY_COLORS)],
            )
            for index, value in enumerate(sorted(function_values.unique()))
        }
        fig_function = px.bar(
            functional_chart,
            x="geo_unit",
            y="demolished_panden_equivalent",
            color="gebruiksdoel",
            color_discrete_map=function_color_map,
            barmode="stack",
            title=f"Functional demolition profile by {dashboard_geo_level}",
            labels={
                "geo_unit": geography_axis_label,
                "demolished_panden_equivalent": "Fractionally weighted PAND equivalents",
                "gebruiksdoel": "BAG use function",
            },
        )
        fig_function.update_layout(height=720, legend_title_text="BAG use function")
        style_categorical_xaxis(
            fig_function,
            functional_chart["geo_unit"].nunique(),
            geography_axis_label,
        )
        # Keep the function legend outside the plot without squeezing the labels.
        fig_function.update_layout(margin={"l": 75, "r": 275, "t": 85, "b": 135})
        render_plotly_chart(fig_function, use_container_width=True)

        st.markdown("### Functional composition · current stock versus demolition")
        st.caption(
            "Both sides use fractional PAND weights. Mixed-use buildings therefore "
            "remain represented without inflating the PAND denominator."
        )

        if geography_mode != CURRENT_GEOGRAPHY_MODE:
            st.info(
                "The current-stock comparison is shown only in current-territory mode, "
                "because its denominator is a present-day reference stock."
            )
        elif functional_stock_df_all.empty:
            st.warning(
                "The current-stock functional view "
                "`thesis_ch4.vw_dashboard_functional_stock_final` is unavailable."
            )
        else:
            demolition_function = (
                functional_df.groupby(
                    "gebruiksdoel", dropna=False
                )["fractional_weight"]
                .sum()
                .rename("demolition_weight")
                .reset_index()
                if "fractional_weight" in functional_df.columns
                else pd.DataFrame()
            )
            stock_function = (
                functional_stock_df_all[
                    functional_stock_df_all["gemeentenaam"].isin(
                        current_reference_municipalities
                    )
                ]
                .groupby(
                    "gebruiksdoel", dropna=False
                )["fractional_weight"]
                .sum()
                .rename("stock_weight")
                .reset_index()
                if "fractional_weight" in functional_stock_df_all.columns
                else pd.DataFrame()
            )

            if demolition_function.empty or stock_function.empty:
                st.info(
                    "No comparable fractional functional composition is available."
                )
            else:
                function_compare = stock_function.merge(
                    demolition_function,
                    on="gebruiksdoel",
                    how="outer",
                ).fillna(
                    {"stock_weight": 0.0, "demolition_weight": 0.0}
                )
                function_compare["gebruiksdoel"] = (
                    function_compare["gebruiksdoel"]
                    .fillna("Onbekend")
                    .astype(str)
                )

                stock_total = float(function_compare["stock_weight"].sum())
                demolition_total = float(
                    function_compare["demolition_weight"].sum()
                )

                function_compare["Current stock (%)"] = (
                    function_compare["stock_weight"] / stock_total * 100
                    if stock_total > 0
                    else 0.0
                )
                function_compare["Demolition (%)"] = (
                    function_compare["demolition_weight"]
                    / demolition_total
                    * 100
                    if demolition_total > 0
                    else 0.0
                )
                function_compare["Difference (pp)"] = (
                    function_compare["Demolition (%)"]
                    - function_compare["Current stock (%)"]
                )
                function_compare["Representation ratio"] = (
                    function_compare.apply(
                        lambda row: (
                            row["Demolition (%)"]
                            / row["Current stock (%)"]
                            if row["Current stock (%)"] > 0
                            else None
                        ),
                        axis=1,
                    )
                )
                function_compare = function_compare.sort_values(
                    "Demolition (%)",
                    ascending=True,
                )

                comparison_long = function_compare.melt(
                    id_vars=["gebruiksdoel"],
                    value_vars=[
                        "Current stock (%)",
                        "Demolition (%)",
                    ],
                    var_name="Population",
                    value_name="Share (%)",
                )

                fig_compare = px.bar(
                    comparison_long,
                    y="gebruiksdoel",
                    x="Share (%)",
                    color="Population",
                    barmode="group",
                    orientation="h",
                    title=(
                        "Functional composition: current stock versus "
                        "demolished PANDs"
                    ),
                    labels={
                        "gebruiksdoel": "BAG use function",
                        "Share (%)": (
                            "Share of fractionally weighted PANDs (%)"
                        ),
                    },
                    color_discrete_map={
                        "Current stock (%)": DARK_GREY,
                        "Demolition (%)": PRIMARY_COLOR,
                    },
                )
                fig_compare.update_layout(
                    height=max(
                        560,
                        46 * len(function_compare) + 170,
                    ),
                    legend_title_text="Population",
                    margin={
                        "l": 190,
                        "r": 35,
                        "t": 85,
                        "b": 80,
                    },
                )
                render_plotly_chart(
                    fig_compare,
                    use_container_width=True,
                )

                with st.expander(
                    "Functional comparison table",
                    expanded=False,
                ):
                    comparison_table = function_compare[
                        [
                            "gebruiksdoel",
                            "stock_weight",
                            "demolition_weight",
                            "Current stock (%)",
                            "Demolition (%)",
                            "Difference (pp)",
                            "Representation ratio",
                        ]
                    ].copy()
                    comparison_table = comparison_table.rename(
                        columns={
                            "stock_weight":
                                "Current-stock PAND equivalents",
                            "demolition_weight":
                                "Demolished PAND equivalents",
                        }
                    )
                    st.dataframe(
                        comparison_table.sort_values(
                            "Demolition (%)",
                            ascending=False,
                        ).style.format(
                            {
                                "Current-stock PAND equivalents":
                                    "{:,.1f}",
                                "Demolished PAND equivalents":
                                    "{:,.1f}",
                                "Current stock (%)":
                                    "{:.2f}",
                                "Demolition (%)":
                                    "{:.2f}",
                                "Difference (pp)":
                                    "{:+.2f}",
                                "Representation ratio":
                                    "{:.2f}",
                            }
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )

    # The permanent spatial explorer is rendered above the main tab navigation.

    if (
        geography_mode == CURRENT_GEOGRAPHY_MODE
        and "De Fryske Marren"
        in set(records["gemeentenaam"].astype(str))
        and (
            (
                records["gemeentenaam"].astype(str)
                == "De Fryske Marren"
            )
            & (
                records["buurtnaam"].astype(str)
                == "Lemmer-Frieslandpark"
            )
        ).any()
    ):
        st.markdown(
            "### Sensitivity · Lemmer-Frieslandpark"
        )
        st.caption(
            "The main analytical sample is retained. These scenarios "
            "show how the interpretation changes when the identified "
            "2016 Lemmer-Frieslandpark administrative-transition "
            "cluster is excluded."
        )

        sensitivity_table = (
            build_lemmer_sensitivity_summary(records)
        )
        st.dataframe(
            sensitivity_table,
            use_container_width=True,
            hide_index=True,
        )

        function_sensitivity = (
            build_function_sensitivity_summary(
                functional_df_all,
                records,
            )
        )
        if not function_sensitivity.empty:
            st.markdown("#### Functional sensitivity")
            st.dataframe(
                function_sensitivity,
                use_container_width=True,
                hide_index=True,
            )

        lemmer_cluster_count = int(
            records.loc[
                (
                    records["gemeentenaam"].astype(str)
                    == "De Fryske Marren"
                )
                & (
                    records["buurtnaam"].astype(str)
                    == "Lemmer-Frieslandpark"
                ),
                "pand_id",
            ].nunique()
        )

        st.info(
            f"Validated BAG audit: {lemmer_cluster_count:,} "
            "Lemmer-Frieslandpark PANDs receive `Pand gesloopt` "
            "in 2016. The audit found the same number of current "
            "standplaatsen, a strict one-to-one point-in-polygon "
            "match, and a 0-day difference between PAND demolition "
            "date and first standplaats validity date. This is treated "
            "as a sensitivity case consistent with an administrative "
            "object-type transition, not silently removed from the "
            "main reconstruction."
        )

    st.divider()
    st.markdown("## Family 4 · Administrative uncertainty")

    st.markdown("### 8. Replacement lag")
    if replacement_df.empty:
        st.warning(
            "To show replacement lag and temporal substitution bias, create the helper view "
            "`thesis_ch4.vw_dashboard_replacement_lag`."
        )
    else:
        lag_chart = (
            replacement_df.groupby("lag_category")
            .size()
            .reset_index(name="demolished_panden")
        )
        observed_lag_order = [
            category for category in LAG_CATEGORY_ORDER
            if category in set(lag_chart["lag_category"].astype(str))
        ]
        observed_lag_order += [
            category for category in lag_chart["lag_category"].astype(str).unique()
            if category not in observed_lag_order
        ]
        fig_lag = px.bar(
            lag_chart,
            x="lag_category",
            y="demolished_panden",
            color="lag_category",
            color_discrete_map=LAG_CATEGORY_COLORS,
            category_orders={"lag_category": observed_lag_order},
            text_auto=",.0f",
            title="Replacement lag categories",
            labels={
                "lag_category": "Replacement lag",
                "demolished_panden": "Demolished panden",
            },
        )
        fig_lag.update_traces(textposition="outside", cliponaxis=False)
        fig_lag.update_layout(showlegend=False, height=650)
        render_plotly_chart(fig_lag, use_container_width=True)

    st.markdown("### 9. Registration delay")
    if reg_delay_df.empty:
        st.warning(
            "To show registration delay, create the helper view "
            "`thesis_ch4.vw_dashboard_registration_delay`."
        )
    else:
        valid_delay = reg_delay_df[reg_delay_df["registration_delay_days"].notna()].copy()
        if valid_delay.empty:
            st.info("No registration-delay values were available in the selected filter.")
        else:
            delay_metric_1, delay_metric_2 = st.columns(2)
            delay_metric_1.metric(
                "Mean registration delay (days)",
                round(valid_delay["registration_delay_days"].mean(), 1),
            )
            delay_metric_2.metric(
                "Median registration delay (days)",
                round(valid_delay["registration_delay_days"].median(), 1),
            )
            fig_delay = px.histogram(
                valid_delay,
                x="registration_delay_days",
                nbins=40,
                title="Registration delay distribution",
                labels={
                    "registration_delay_days": "Registration delay (days)",
                    "count": "Demolished panden",
                },
            )
            fig_delay.update_traces(marker_color="#C77B67", opacity=0.90)
            fig_delay.update_layout(height=650)
            render_plotly_chart(fig_delay, use_container_width=True)

    st.markdown("### 10. Temporal substitution bias")
    if replacement_df.empty:
        st.info("Temporal substitution bias depends on the replacement-lag helper view.")
    else:
        if dashboard_geo_level == "municipality":
            flagged = (
                replacement_df.groupby("gemeentenaam")["temporal_substitution_flag"]
                .sum()
                .reset_index(name="flagged_demolitions")
                .rename(columns={"gemeentenaam": "geo_unit"})
            )
            totals = (
                dashboard_records.groupby("gemeentenaam")
                .size()
                .reset_index(name="total_demolitions")
                .rename(columns={"gemeentenaam": "geo_unit"})
            )
        else:
            flagged = (
                replacement_df.groupby("wijknaam", dropna=False)["temporal_substitution_flag"]
                .sum()
                .reset_index(name="flagged_demolitions")
                .rename(columns={"wijknaam": "geo_unit"})
            )
            totals = (
                dashboard_records.groupby("wijknaam", dropna=False)
                .size()
                .reset_index(name="total_demolitions")
                .rename(columns={"wijknaam": "geo_unit"})
            )
            flagged["geo_unit"] = flagged["geo_unit"].fillna("Onbekende wijk")
            totals["geo_unit"] = totals["geo_unit"].fillna("Onbekende wijk")

        substitution_df = totals.merge(flagged, on="geo_unit", how="left")
        substitution_df["flagged_demolitions"] = substitution_df["flagged_demolitions"].fillna(0)
        substitution_df["substitution_bias_share_pct"] = (
            substitution_df["flagged_demolitions"] / substitution_df["total_demolitions"] * 100
        )
        substitution_df = substitution_df.sort_values(
            "substitution_bias_share_pct", ascending=False
        )

        fig_substitution = px.bar(
            substitution_df,
            x="geo_unit",
            y="substitution_bias_share_pct",
            color="substitution_bias_share_pct",
            color_continuous_scale=TERRACOTTA_SCALE,
            hover_data={
                "geo_unit": False,
                "flagged_demolitions": ":,",
                "total_demolitions": ":,",
                "substitution_bias_share_pct": ":.1f",
            },
            text_auto=".1f",
            title=f"Temporal substitution bias by {dashboard_geo_level}",
            labels={
                "geo_unit": geography_axis_label,
                "substitution_bias_share_pct": "Temporal substitution share (%)",
                "flagged_demolitions": "Flagged demolitions",
                "total_demolitions": "Total demolitions",
            },
        )
        fig_substitution.update_traces(textposition="outside", cliponaxis=False)
        fig_substitution.update_layout(showlegend=False, coloraxis_showscale=False, height=680)
        style_categorical_xaxis(
            fig_substitution,
            substitution_df["geo_unit"].nunique(),
            geography_axis_label,
        )
        render_plotly_chart(fig_substitution, use_container_width=True)

    with st.expander("Indicator definitions used in this dashboard"):
        st.markdown(
            """
            1. **Annual demolition count** = number of demolished BAG pand records per year.  
            2. **Demolition rate** = in historical mode, annual demolitions divided by the physical pand stock on 1 January within the municipality boundary valid that year; in current mode, a cumulative comparison against current stock is shown and labelled as intensity.  
            3. **Building age at demolition** = demolition year minus construction year.  
            4. **Cohort-specific loss** = demolished panden grouped by construction decade.  
            5. **Mean lifespan** = average building age at demolition.  
            6. **Median lifespan** = median building age at demolition.  
            7. **Functional demolition profile** = demolished panden linked to verblijfsobject gebruiksdoel categories.  
               **Specialized gebruiksdoel transition explorer:** consecutive historical VBO occurrences are compared to show changes in the complete gebruiksdoel set as a Sankey and annual traffic-light signal. This extension does not create an eleventh primary indicator.  
            8. **Replacement lag** = years between demolition and best overlapping current replacement building.  
            9. **Registration delay** = difference between the demolition validity start and the BAG registration timestamp.  
            10. **Temporal substitution bias** = share of demolitions with a spatially overlapping replacement within a short lag window.
            """
        )


# ------------------------------------------------------------
# Overview
# ------------------------------------------------------------

with tab_overview:
    st.subheader("Analytical sample")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Demolished panden", len(records))
    col2.metric("Municipalities", records["gemeentenaam"].nunique())
    col3.metric("Year range", f"{start_year}–{end_year}")
    col4.metric(
        "Mean lifespan",
        round(records["lifespan_years"].mean(), 1)
        if not records.empty
        else "-",
    )

    st.markdown("### Reproducible filtering pipeline")
    st.caption(
        "Each valid demolition point retains two municipality references: the "
        "municipality valid at the demolition date and the municipality containing "
        "the location today. Null geometry cannot be assigned truthfully to either "
        "spatial geography and is reported separately."
    )

    pipeline = load_pipeline_summary(current_reference_municipalities)
    if pipeline.empty:
        st.warning(
            "No pipeline summary view was found. Create "
            "`thesis_ch4.vw_pipeline_fryslan_summary` for all municipalities, or keep "
            "`thesis_ch4.vw_pipeline_case_summary` as the temporary case-study fallback."
        )
    else:
        st.dataframe(pipeline, use_container_width=True, hide_index=True)

    source_quality = load_global_source_quality()
    if source_quality.empty:
        st.warning(
            "The PostgreSQL view `thesis_ch4.vw_source_geometry_quality` does not exist yet."
        )
    if not source_quality.empty:
        q = source_quality.iloc[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Source demolition records", int(q["raw_demolition_records_all_years"]))
        c2.metric("Valid geometry", int(q["valid_geometry"]))
        c3.metric("Invalid / missing geometry", int(q["invalid_geometry"]))

    with st.expander("How to read the pipeline"):
        st.markdown(
            """
            - **Provincial records, all years:** latest terminal demolition occurrence per `pand_id` within the Fryslân study extent.
            - **Event municipality:** polygon valid on the demolition date.
            - **Current municipality:** present-day polygon containing the same location.
            - **Records 2011–2025:** the fixed comparison period.
            - **Missing bouwjaar:** cannot be used for lifespan or construction-cohort analysis.
            - **Removed non-positive lifespan:** demolition year minus construction year is zero or negative.
            - **Expected final sample:** rows that satisfy the analytical filters.
            - **Materialized final sample:** rows actually present in the resolved demolition relation shown above.
            """
        )


# ------------------------------------------------------------
# 3DBAG viewer
# ------------------------------------------------------------

with tab_specialized:
    st.header("Specialized spatial tools")
    st.caption(
        "These tools are analytical extensions rather than additional primary "
        "indicators. Each tool has a distinct spatial purpose and uses the applied "
        "municipality and 2011–2025 year filters where relevant."
    )

    specialized_tool = st.radio(
        "Choose a specialized tool",
        [
            "3D cohort + heritage",
            "Gebruiksdoel transition explorer",
            "Historical municipality boundaries",
        ],
        horizontal=True,
        key="specialized_spatial_tool",
        help=(
            "Only the selected specialized tool is executed. This avoids loading "
            "the 3D viewer, historical transition query and boundary history together."
        ),
    )

    if specialized_tool == "3D cohort + heritage":
        st.header("Surviving stock, disappearance and heritage context")
        st.caption(
            "Current 3DBAG buildings represent surviving stock. Former BAG demolition "
            "footprints represent disappearance, while thesis_heritage supplies formal "
            "and broader cultural-historical context as separate 2D overlays."
        )
        st.info(
            "Heritage-context integration is implemented as a two-municipality proof "
            "of concept. The main demolition dashboard remains province-wide."
        )
        st.info(
            "Construction period is a screening variable, not a demolition-risk "
            "prediction. Potential vulnerability should be interpreted together with "
            "heritage status, use, condition, policy pressure and the provincial "
            "demolition patterns."
        )

        st.caption("3D source: downloaded local 3DBAG LoD1.2 GeoPackages.")
        viewer_source = "Local GeoPackages (recommended)"
        lod_label = "LoD1.2 · fastest"
        case_municipality = st.selectbox(
            "Heritage case-study municipality",
            options=list(HERITAGE_CASE_STUDIES),
            index=0,
            key="heritage_case_study_municipality",
            help=(
                "This local selector controls only the proof-of-concept viewer, "
                "metrics and overlays; it does not change the dashboard filters."
            ),
        )

        st.markdown("### Heritage context")
        show_demolitions = st.checkbox(
            "Demolished buildings",
            value=True,
            key="heritage_show_demolitions",
        )
        demolition_display_scope = st.radio(
            "Demolition selection",
            ["All demolitions", "Pre-1945 cohort"],
            horizontal=True,
            disabled=not show_demolitions,
            key="heritage_demolition_display_scope",
            label_visibility="collapsed",
        )
        context_control_1, context_control_2 = st.columns(2)
        with context_control_1:
            show_national_monuments = st.checkbox(
                "National monuments", value=False, key="heritage_show_national"
            )
            show_protected_views = st.checkbox(
                "Protected town & village views", value=False, key="heritage_show_views"
            )
        with context_control_2:
            show_wkpb = st.checkbox(
                "Public-law restrictions (Wkpb)", value=False, key="heritage_show_wkpb"
            )
            show_chk2 = st.checkbox(
                "Cultural-historical context (CHK2)", value=False, key="heritage_show_chk2"
            )

        cohort_choice = st.selectbox(
            "Construction cohort for disappearance and bounded buurt loss",
            options=[cohort["label"] for cohort in CONSTRUCTION_COHORTS],
            index=3,
            key="heritage_case_cohort",
        )
        selected_cohort = next(
            cohort for cohort in CONSTRUCTION_COHORTS
            if cohort["label"] == cohort_choice
        )
        camera_municipalities = (case_municipality,)
        camera_focus_label = case_municipality

        st.caption(
            "The camera follows the current-reference municipality footprint. Mouse "
            "wheel or trackpad zoom is enabled, and the viewer also provides +, −, "
            "top-view and 3D-view controls."
        )

        monument_records = []
        cohort_definitions = build_construction_cohort_definitions(records)
        admin_boundary_layers, admin_boundary_errors = load_3d_admin_boundary_layers(
            camera_municipalities
        )
        thesis_heritage_layers, thesis_heritage_inventory, thesis_heritage_warnings = (
            load_thesis_heritage_layers(case_municipality)
        )
        demolition_display_start, demolition_display_end = (
            (1, 1945)
            if demolition_display_scope == "Pre-1945 cohort"
            else (None, None)
        )
        if show_demolitions:
            demolition_features, demolition_error = load_case_demolition_footprints(
                case_municipality,
                demolition_display_start,
                demolition_display_end,
            )
        else:
            demolition_features, demolition_error = [], None
        decline_df, decline_collection, decline_error = load_case_cohort_decline_by_buurt(
            case_municipality, selected_cohort["start"], selected_cohort["end"]
        )
        disappearance_layer = {
            "id": "former_bag_demolition_footprints",
            "title": f"Demolished buildings · {demolition_display_scope}",
            "group": "Disappearance",
            "kind": "geojson", "visible": True, "color": "#1F1F1F",
            "fillAlpha": 0.38, "strokeAlpha": 0.95, "strokeWidth": 1.5,
            "featureCollection": {"type": "FeatureCollection", "features": demolition_features},
            "note": f"{len(demolition_features):,} former polygons; translucent 2D ground footprints.",
        }
        decline_layer = {
            "id": "cohort_decline_current_buurt",
            "title": f"Observed cohort loss · current buurt · {cohort_choice}",
            "group": "Disappearance",
            "kind": "geojson", "visible": False, "color": "#B30000",
            "fillAlpha": 0.35, "strokeAlpha": 0.9, "strokeWidth": 1.5,
            "styleByFillColor": True, "featureCollection": decline_collection,
            "note": "D / (D + S) × 100; D=former BAG demolitions, S=current surviving BAG stock.",
        }
        heritage_layer_configs = []
        if show_demolitions:
            heritage_layer_configs.append(disappearance_layer)
        for layer in thesis_heritage_layers:
            layer_id = str(layer.get("id", ""))
            enabled = (
                show_national_monuments
                if layer_id.endswith((
                    "national_monuments_points",
                    "national_monument_buildings",
                ))
                else show_protected_views
                if layer_id.endswith("protected_town_village_views")
                else show_wkpb
                if layer_id.endswith("wkpb_heritage_restrictions")
                else show_chk2
                if "_chk_" in layer_id
                else False
            )
            if enabled:
                visible_layer = dict(layer)
                visible_layer["visible"] = True
                heritage_layer_configs.append(visible_layer)

        heritage_col1, heritage_col2, heritage_col3, heritage_col4 = st.columns(4)
        heritage_col1.metric(
            "Demolition footprints",
            len(demolition_features),
        )
        heritage_col2.metric(
            "Current municipality scope",
            len(camera_municipalities),
        )
        heritage_col3.metric(
            "Administrative overlays",
            len(admin_boundary_layers),
        )
        heritage_col4.metric(
            "thesis_heritage layers",
            len(thesis_heritage_layers),
        )
        if not decline_df.empty:
            case_demolished = int(decline_df["demolished"].sum())
            case_surviving = int(decline_df["surviving"].sum())
            case_population = case_demolished + case_surviving
            case_decline = (
                case_demolished / case_population * 100
                if case_population > 0 else None
            )
            metric_d, metric_s, metric_rate = st.columns(3)
            metric_d.metric(f"D · demolished · {cohort_choice}", f"{case_demolished:,}")
            metric_s.metric(f"S · surviving · {cohort_choice}", f"{case_surviving:,}")
            metric_rate.metric(
                "Municipality observed cohort loss",
                f"{case_decline:.2f}%" if case_decline is not None else "–",
                help="D / (D + S) × 100 for the selected case-study municipality.",
            )
        with st.expander("thesis_heritage and disappearance layers", expanded=False):
            if demolition_error:
                st.warning(demolition_error)
            if decline_error:
                st.warning(f"Cohort-decline layer: {decline_error}")
            for warning in thesis_heritage_warnings:
                st.warning(warning)
            if thesis_heritage_inventory.empty:
                st.warning("No usable thesis_heritage layer was returned for this municipality.")
            else:
                st.dataframe(thesis_heritage_inventory, use_container_width=True, hide_index=True)

            st.markdown("**Observed cohort-loss denominator audit**")
            if not decline_df.empty:
                audit_columns = ["buurtnaam", "demolished", "surviving", "bounded_cohort_population", "cohort_decline_pct"]
                st.dataframe(decline_df[audit_columns], use_container_width=True, hide_index=True)
                st.caption(
                    "S is queried from dt_frl.pand_current with the same lifecycle-status "
                    "rule as Indicator 2. It is not inferred from the 3DBAG browser payload."
                )

            st.markdown("**Administrative boundary overlays**")
            for boundary_error in admin_boundary_errors:
                st.warning(boundary_error)
            if admin_boundary_layers:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "layer": layer["title"],
                                "features": len(
                                    layer.get("featureCollection", {}).get("features", [])
                                ),
                                "default_visible": layer.get("visible", False),
                                "line_colour": layer.get("color", ADMIN_BOUNDARY_COLOR),
                            }
                            for layer in admin_boundary_layers
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.warning(
                    "No province/municipality/wijk/buurt boundaries could be read from "
                    "the current PostGIS administrative views. See the query diagnostics above."
                )

        with st.expander("Downloaded local GeoPackage tiles", expanded=False):
            st.code(str(THREEDBAG_LOCAL_GPKG_DIR), language=None)
            local_inventory = inventory_local_3dbag_gpkgs(
                str(THREEDBAG_LOCAL_GPKG_DIR)
            )
            if local_inventory.empty:
                st.warning("No readable .gpkg files containing lod12_2d were detected.")
            else:
                c1, c2 = st.columns(2)
                c1.metric("GeoPackage tiles detected", len(local_inventory))
                c2.metric("Local data size", f"{local_inventory['bytes'].sum() / 1e9:.2f} GB")
                st.dataframe(
                    local_inventory[["file", "srs_id", "bytes"]].head(50),
                    use_container_width=True,
                    hide_index=True,
                )
                st.caption(
                    "Local mode selects only tiles intersecting the current camera scope; "
                    "it does not load the complete province-wide 8+ GB collection."
                )

        if viewer_source == "Local GeoPackages (recommended)":
            admin_level_labels = {
                "Province": "province",
                "Municipality": "municipality",
                "Wijk": "wijk",
                "Buurt": "buurt",
            }
            local_control1, local_control2 = st.columns(2)
            with local_control1:
                selected_admin_labels = st.multiselect(
                    "Administrative boundary layers",
                    options=list(admin_level_labels),
                    default=["Municipality", "Wijk", "Buurt"],
                    key="local_3dbag_admin_levels",
                )
            with local_control2:
                selected_text_labels = st.multiselect(
                    "Administrative name labels",
                    options=list(admin_level_labels),
                    default=["Municipality", "Wijk"],
                    key="local_3dbag_label_levels",
                    help="Buurt labels can be dense; enable them for close inspection.",
                )
            visible_admin_levels = [
                admin_level_labels[label] for label in selected_admin_labels
            ]
            label_admin_levels = [
                admin_level_labels[label] for label in selected_text_labels
            ]
            local_bounds = load_3dbag_camera_bounds(camera_municipalities)
            if local_bounds.empty or local_bounds.iloc[0].isna().any():
                st.error("The selected municipality extent could not be loaded.")
            else:
                bound = local_bounds.iloc[0]
                with st.spinner("Selecting and reading local LoD1.2 GeoPackage tilesâ€¦"):
                    local_buildings, selected_local_tiles, local_error = (
                        load_local_3dbag_lod12(
                            str(THREEDBAG_LOCAL_GPKG_DIR),
                            float(bound["west"]),
                            float(bound["south"]),
                            float(bound["east"]),
                            float(bound["north"]),
                        )
                    )
                metric1, metric2 = st.columns(2)
                metric1.metric("Local tiles selected", len(selected_local_tiles))
                metric2.metric("Local building parts", len(local_buildings))
                if local_error:
                    st.warning(local_error)
                if local_buildings.empty:
                    st.error(
                        "No local LoD1.2 buildings are available for this extent. "
                        "Choose a municipality covered by the downloaded tiles or inspect "
                        "the local-tile message above."
                    )
                else:
                    represented_cohorts = set(
                        local_buildings["construction_cohort_label"]
                        .dropna()
                        .astype(str)
                    )
                    legend_items = []
                    for index, cohort in enumerate(CONSTRUCTION_COHORTS):
                        if cohort["label"] not in represented_cohorts:
                            continue
                        color = _cohort_color(index, len(CONSTRUCTION_COHORTS))
                        legend_items.append(
                            f'<span style="display:inline-flex;align-items:center;gap:5px;'
                            f'margin:3px 14px 3px 0"><span style="width:14px;height:14px;'
                            f'background:{color};border:1px solid #666;display:inline-block"></span>'
                            f'{cohort["label"]}</span>'
                        )
                    if local_buildings["construction_cohort_label"].isna().any():
                        legend_items.append(
                            '<span style="display:inline-flex;align-items:center;gap:5px;'
                            'margin:3px 14px 3px 0"><span style="width:14px;height:14px;'
                            f'background:{UNKNOWN_COLOR};border:1px solid #666;display:inline-block"></span>'
                            'Unknown</span>'
                        )
                    st.markdown(
                        "**Construction cohort legend**<br>" + "".join(legend_items),
                        unsafe_allow_html=True,
                    )
                    context_legend_items = []
                    if show_demolitions:
                        context_legend_items.append(
                            ("#1F1F1F", "Demolished buildings", "Former BAG footprints · analytical disappearance")
                        )
                    if show_national_monuments:
                        context_legend_items.append(
                            ("linear-gradient(90deg,#332288 0 48%,#CC6677 52% 100%)", "National monuments", "Registered monument points + monument building polygons")
                        )
                    if show_protected_views:
                        context_legend_items.append(
                            ("#DDCC77", "Protected town & village views", "Formally designated protected cultural landscapes")
                        )
                    if show_wkpb:
                        context_legend_items.append(
                            ("#994455", "Public-law restrictions (Wkpb)", "Legally registered heritage-related restrictions")
                        )
                    if show_chk2:
                        context_legend_items.append(
                            ("linear-gradient(90deg,#117733,#44AA99,#88CCEE)", "Cultural-historical context (CHK2)", "Broader cultural-historical values; not equivalent to legal protection")
                        )

                    if context_legend_items:
                        context_legend_html = "".join(
                            f"""
                            <div style="display:flex;align-items:flex-start;gap:9px;margin:7px 0;">
                              <span style="width:24px;height:15px;flex:0 0 24px;margin-top:2px;
                                           border:1px solid #666;border-radius:2px;background:{swatch};"></span>
                              <span><strong>{label}</strong><br>
                                <span style="color:#5f6368;font-size:0.86rem;">{meaning}</span>
                              </span>
                            </div>
                            """
                            for swatch, label, meaning in context_legend_items
                        )
                        st.markdown(
                            f"""
                            <div style="border:1px solid #d1d5db;border-radius:7px;padding:10px 14px;
                                        margin:8px 0 12px;background:rgba(255,255,255,0.96);">
                              <div style="font-weight:700;margin-bottom:4px;">Visible 2D context layers</div>
                              {context_legend_html}
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    st.caption(
                        "Administrative outlines: municipality = terracotta, wijk = indigo, "
                        "buurt = green. Hover buildings and labels for attributes."
                    )
                    render_local_3dbag_pydeck(
                        local_buildings,
                        local_bounds,
                        admin_boundary_layers,
                        heritage_layer_configs,
                        visible_admin_levels=visible_admin_levels,
                        label_admin_levels=label_admin_levels,
                    )
                    st.caption(
                        "Local LoD1.2 footprints are extruded using 3DBAG height attributes. "
                        "Administrative and imported PostGIS heritage geometries are rendered "
                        "in the same PyDeck scene."
                    )
        st.caption(
            "Current 3DBAG is the surviving three-dimensional stock and retains the "
            "Indicator 4 construction-period colours. Former BAG footprints show "
            "disappearance as translucent 2D polygons. Administrative boundaries, "
            "national monuments, protected views, Wkpb restrictions and CHK2 sources "
            "remain independent 2D contextual overlays loaded from thesis_heritage. "
            "The cohort-decline denominator is calculated from BAG/PostGIS, not from "
            "the local 3DBAG visualization payload."
        )



    elif specialized_tool == "Gebruiksdoel transition explorer":
        st.header("Historical Gebruiksdoel Transition Explorer")
        st.caption(
            "This specialized tool compares consecutive BAG verblijfsobject "
            "occurrences and records a transition only when the complete registered "
            "set of gebruiksdoelen changes. It supports Sankey, annual, wijk, buurt "
            "and object-level inspection, but it is not an eleventh indicator."
        )
        transition_case_municipalities = st.multiselect(
            "Municipalities included in the transition analysis",
            options=list(HERITAGE_CASE_STUDIES),
            default=list(HERITAGE_CASE_STUDIES),
            key="usage_transition_case_municipalities",
            help=(
                "This scope belongs only to the Historical Gebruiksdoel Transition "
                "Explorer and does not alter the global dashboard municipality filter."
            ),
        )
        if transition_case_municipalities:
            usage_transition_data = load_dashboard_usage_transitions(
                tuple(transition_case_municipalities),
                start_year,
                end_year,
            )
        else:
            usage_transition_data = pd.DataFrame()
        if usage_transition_data.empty:
            st.warning(
                "Select Leeuwarden and/or De Fryske Marren. If a municipality is already "
                "selected, no historical gebruiksdoel transitions could be loaded; check that "
                "`dt_frl.verblijfsobjectgebruiksdoel`, the historical "
                "`dt_frl.verblijfsobject` table and the current municipality geometries are available."
            )
        else:
            transition_events_all = usage_transition_data[
                usage_transition_data["record_type"] == "transition"
            ].copy()
            transition_scope_all = usage_transition_data[
                usage_transition_data["record_type"] == "scope"
            ].copy()

            if transition_events_all.empty:
                st.info(
                    "No changed gebruiksdoel sets were found in the selected municipality "
                    "and year range."
                )
            else:
                transition_events_all["transition_year"] = pd.to_numeric(
                    transition_events_all["transition_year"], errors="coerce"
                ).astype("Int64")

                st.markdown("#### Scope and transition definition")
                st.caption(
                    "UI geography: current municipality, current wijk and current buurt. "
                    "This diagnostic is intentionally independent of the historical "
                    "municipality-at-demolition selector."
                )
                scope_c1, scope_c2, scope_c3 = st.columns(3)
                municipality_options = sorted(
                    transition_events_all["gemeentenaam"].dropna().astype(str).unique()
                )
                default_municipality = (
                    dashboard_focus_municipality
                    if dashboard_focus_municipality in municipality_options
                    else (
                        "Leeuwarden"
                        if "Leeuwarden" in municipality_options
                        else municipality_options[0]
                    )
                )

                with scope_c1:
                    transition_detail_scope = st.radio(
                        "Sankey/detail scope",
                        ["Municipality overview", "Selected wijk"],
                        horizontal=False,
                        key="usage_transition_detail_scope",
                    )
                with scope_c2:
                    transition_municipality = st.selectbox(
                        "Municipality for transition analysis",
                        municipality_options,
                        index=municipality_options.index(default_municipality),
                        key="usage_transition_municipality",
                    )

                municipality_events = transition_events_all[
                    transition_events_all["gemeentenaam"] == transition_municipality
                ].copy()
                municipality_scope = transition_scope_all[
                    transition_scope_all["gemeentenaam"] == transition_municipality
                ].copy()

                selected_transition_wijk = None
                with scope_c3:
                    if transition_detail_scope == "Selected wijk":
                        wijk_options = sorted(
                            municipality_events["wijknaam"]
                            .fillna("Onbekende wijk")
                            .astype(str)
                            .unique()
                        )
                        selected_transition_wijk = st.selectbox(
                            "Wijk",
                            wijk_options,
                            key="usage_transition_selected_wijk",
                        )
                    else:
                        st.text_input(
                            "Wijk",
                            value="All wijken",
                            disabled=True,
                            key="usage_transition_wijk_disabled",
                        )

                if transition_detail_scope == "Selected wijk":
                    scope_events = municipality_events[
                        municipality_events["wijknaam"].fillna("Onbekende wijk")
                        == selected_transition_wijk
                    ].copy()
                    scope_rows = municipality_scope[
                        municipality_scope["wijknaam"].fillna("Onbekende wijk")
                        == selected_transition_wijk
                    ].copy()
                    denominator_row = scope_rows[
                        scope_rows["geo_level"] == "wijk"
                    ].drop_duplicates(["gemeentenaam", "wijkcode"])
                    scope_label = f"{transition_municipality} · {selected_transition_wijk}"
                else:
                    scope_events = municipality_events.copy()
                    scope_rows = municipality_scope.copy()
                    denominator_row = scope_rows[
                        scope_rows["geo_level"] == "municipality"
                    ].drop_duplicates(["gemeentenaam"])
                    scope_label = transition_municipality

                definition_c1, definition_c2, definition_c3 = st.columns(3)
                with definition_c1:
                    matching_mode = st.selectbox(
                        "Transition matching",
                        [
                            "Exact complete use set",
                            "Contains individual BAG function",
                            "Collapsed analytical category",
                        ],
                        index=1,
                        help=(
                            "Exact compares complete registered sets. Contains keeps records "
                            "where the selected individual function occurs in the source/target "
                            "set. Collapsed groups functions into documented analytical categories."
                        ),
                        key="usage_transition_matching_mode",
                    )

                prepared_scope_events = add_transition_display_columns(
                    scope_events,
                    matching_mode,
                )
                from_options, to_options = transition_filter_options(
                    prepared_scope_events,
                    matching_mode,
                )

                preferred_from = (
                    "Wonen"
                    if matching_mode == "Collapsed analytical category"
                    else "woonfunctie"
                )
                preferred_to = (
                    "Logies"
                    if matching_mode == "Collapsed analytical category"
                    else "logiesfunctie"
                )
                default_from = (
                    from_options.index(preferred_from)
                    if preferred_from in from_options
                    else 0
                )
                default_to = (
                    to_options.index(preferred_to)
                    if preferred_to in to_options
                    else 0
                )

                with definition_c2:
                    from_filter = st.selectbox(
                        "From use",
                        from_options,
                        index=default_from,
                        key="usage_transition_from_filter",
                    )
                with definition_c3:
                    to_filter = st.selectbox(
                        "To use",
                        to_options,
                        index=default_to,
                        key="usage_transition_to_filter",
                    )

                filtered_events = filter_transition_pairs(
                    prepared_scope_events,
                    matching_mode,
                    from_filter,
                    to_filter,
                )

                observed_vbos = int(
                    pd.to_numeric(
                        denominator_row["observed_vbos"],
                        errors="coerce",
                    )
                    .fillna(0)
                    .sum()
                )
                total_transition_events = int(len(filtered_events))
                changed_vbos = int(filtered_events["vbo_id"].nunique())
                transition_rate = (
                    total_transition_events / observed_vbos * 1000
                    if observed_vbos > 0
                    else None
                )

                annual_transition = (
                    filtered_events.groupby("transition_year", dropna=True)
                    .agg(
                        transition_events=("vbo_id", "size"),
                        changed_vbos=("vbo_id", "nunique"),
                    )
                    .reset_index()
                )
                all_years = pd.DataFrame(
                    {"transition_year": list(range(start_year, end_year + 1))}
                )
                annual_transition = all_years.merge(
                    annual_transition,
                    on="transition_year",
                    how="left",
                )
                annual_transition[["transition_events", "changed_vbos"]] = (
                    annual_transition[["transition_events", "changed_vbos"]]
                    .fillna(0)
                    .astype(int)
                )
                annual_transition["rate_per_1000_observed_vbos"] = (
                    annual_transition["transition_events"] / observed_vbos * 1000
                    if observed_vbos > 0
                    else 0.0
                )
                annual_transition = add_annual_transition_signal(
                    annual_transition,
                    value_column="transition_events",
                )

                peak_row = annual_transition.sort_values(
                    ["transition_events", "transition_year"],
                    ascending=[False, True],
                ).iloc[0]
                latest_row = annual_transition[
                    annual_transition["transition_year"] == end_year
                ].iloc[0]

                metric_1, metric_2, metric_3, metric_4 = st.columns(4)
                metric_1.metric("Filtered transition events", total_transition_events)
                metric_2.metric("Changed verblijfsobjecten", changed_vbos)
                metric_3.metric(
                    "Transitions per 1,000 observed VBOs",
                    round(transition_rate, 1) if transition_rate is not None else "—",
                    help=(
                        "Denominator: distinct VBOs with a valid gebruiksdoel occurrence "
                        "during the selected period and spatial scope."
                    ),
                )
                metric_4.metric(
                    f"Signal in {end_year}",
                    f"{latest_row['signal_icon']} {latest_row['signal']}",
                    delta=f"Peak: {int(peak_row['transition_year'])}",
                    delta_color="off",
                )
                st.caption(
                    f"Current detail scope: **{scope_label}** · matching: **{matching_mode}** · "
                    f"filter: **{from_filter} → {to_filter}**"
                )

                if filtered_events.empty:
                    st.info(
                        "No events match this source/target definition. Change the matching "
                        "mode or select a different transition pair."
                    )
                else:
                    control_1, control_2 = st.columns([1, 1])
                    pair_count = max(
                        1,
                        filtered_events.groupby(["from_display", "to_display"]).ngroups,
                    )
                    with control_1:
                        max_flow_count = min(20, pair_count)
                        if max_flow_count == 1:
                            top_flow_count = 1
                            st.caption("Number of Sankey flows: **1**")
                        else:
                            top_flow_count = st.slider(
                                "Number of Sankey flows",
                                min_value=1,
                                max_value=max_flow_count,
                                value=min(10, max_flow_count),
                                key="gebruiksdoel_sankey_top_n_spatial",
                            )
                    with control_2:
                        annual_measure = st.radio(
                            "Annual traffic-light measure",
                            ["Transition events", "Rate per 1,000 observed VBOs"],
                            horizontal=True,
                            key="gebruiksdoel_annual_measure_spatial",
                        )

                    sankey_figure = build_transition_sankey(
                        filtered_events,
                        top_flow_count,
                        (
                            f"Most frequent gebruiksdoel transitions — {scope_label}, "
                            f"{start_year}–{end_year}"
                        ),
                    )
                    if sankey_figure is not None:
                        render_plotly_chart(sankey_figure, use_container_width=True)

                    if annual_measure == "Transition events":
                        annual_value = "transition_events"
                        annual_title = "Annual selected gebruiksdoel transition events"
                        y_axis_title = "Transition events"
                    else:
                        annual_value = "rate_per_1000_observed_vbos"
                        annual_title = (
                            "Annual selected gebruiksdoel transitions per 1,000 observed VBOs"
                        )
                        y_axis_title = "Transitions per 1,000 observed VBOs"

                    traffic_figure = px.bar(
                        annual_transition,
                        x="transition_year",
                        y=annual_value,
                        color="signal",
                        color_discrete_map={
                            "Normal": "#2e7d32",
                            "Elevated": "#f9a825",
                            "Exceptional": "#c62828",
                        },
                        category_orders={
                            "signal": ["Normal", "Elevated", "Exceptional"]
                        },
                        hover_data={
                            "transition_events": True,
                            "changed_vbos": True,
                            "rate_per_1000_observed_vbos": ":.2f",
                            "anomaly_score": ":.2f",
                            "baseline_method": True,
                        },
                        title=annual_title,
                        labels={
                            "transition_year": "Year",
                            annual_value: y_axis_title,
                            "signal": "Traffic-light signal",
                        },
                    )
                    traffic_figure.update_layout(bargap=0.16)
                    render_plotly_chart(traffic_figure, use_container_width=True)

                st.markdown("#### Spatial pattern of function changes")
                map_control_1, map_control_2, map_control_3 = st.columns(3)
                with map_control_1:
                    transition_map_level = st.selectbox(
                        "Map granularity",
                        ["wijk", "buurt"],
                        key="usage_transition_map_level",
                    )
                with map_control_2:
                    transition_map_metric = st.selectbox(
                        "Map measure",
                        [
                            "Transition count",
                            "Rate per 1,000 observed VBOs",
                            "Share of all transitions in area (%)",
                        ],
                        key="usage_transition_map_metric",
                    )
                with map_control_3:
                    transition_map_period = st.selectbox(
                        "Map time",
                        ["All selected years"]
                        + [str(year) for year in range(start_year, end_year + 1)],
                        key="usage_transition_map_period",
                    )

                if transition_map_period == "All selected years":
                    map_events = filtered_events.copy()
                    map_all_events = prepared_scope_events.copy()
                    map_period_label = f"{start_year}–{end_year}"
                else:
                    map_year = int(transition_map_period)
                    map_events = filtered_events[
                        filtered_events["transition_year"] == map_year
                    ].copy()
                    map_all_events = prepared_scope_events[
                        prepared_scope_events["transition_year"] == map_year
                    ].copy()
                    map_period_label = str(map_year)

                boundary_df = load_transition_admin_boundaries(
                    transition_map_level,
                    [transition_municipality],
                )
                map_table = build_transition_map_table(
                    map_events,
                    map_all_events,
                    scope_rows,
                    boundary_df,
                    transition_map_level,
                )

                if transition_map_metric == "Transition count":
                    map_metric_column = "selected_transitions"
                    map_metric_label = "Selected transition count"
                elif transition_map_metric == "Rate per 1,000 observed VBOs":
                    map_metric_column = "rate_per_1000"
                    map_metric_label = "Rate per 1,000 observed VBOs"
                else:
                    map_metric_column = "share_of_area_transitions_pct"
                    map_metric_label = "Share of all area transitions (%)"

                map_feature_collection = transition_map_feature_collection(
                    map_table,
                    map_metric_column,
                    map_metric_label,
                )

                map_tab_1, map_tab_2, map_tab_3, map_tab_4 = st.tabs(
                    [
                        "Choropleth",
                        "Object points",
                        "Animated yearly points",
                        "Event table",
                    ]
                )
                with map_tab_1:
                    render_transition_choropleth(
                        map_feature_collection,
                        (
                            f"{map_metric_label} by {transition_map_level} — "
                            f"{scope_label}, {map_period_label}"
                        ),
                        key=(
                            f"usage_transition_choropleth_{transition_map_level}_"
                            f"{transition_map_period}_{matching_mode}_{from_filter}_{to_filter}"
                        ),
                    )
                with map_tab_2:
                    render_transition_point_map(
                        map_events,
                        f"Individual changed VBOs — {scope_label}, {map_period_label}",
                        key=(
                            f"usage_transition_points_{transition_map_period}_"
                            f"{matching_mode}_{from_filter}_{to_filter}"
                        ),
                    )
                with map_tab_3:
                    animated_figure = build_animated_transition_map(
                        filtered_events,
                        f"Year-by-year selected function changes — {scope_label}",
                    )
                    if animated_figure is None:
                        st.info("No point geometries are available for animation.")
                    else:
                        render_plotly_chart(animated_figure, use_container_width=True)
                with map_tab_4:
                    event_table = map_events[
                        [
                            "vbo_id",
                            "transition_date",
                            "transition_year",
                            "gemeentenaam",
                            "wijknaam",
                            "buurtnaam",
                            "from_use",
                            "to_use",
                            "lon",
                            "lat",
                        ]
                    ].copy()
                    event_table["bag_viewer"] = event_table["vbo_id"].apply(bag_viewer_url)
                    st.dataframe(
                        event_table.sort_values(
                            ["transition_year", "wijknaam", "buurtnaam", "vbo_id"],
                            ascending=[False, True, True, True],
                        ),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "bag_viewer": st.column_config.LinkColumn(
                                "Inspect in BAG Viewer",
                                display_text="Open",
                            )
                        },
                    )
                    st.download_button(
                        "Download selected transition events as CSV",
                        event_table.to_csv(index=False).encode("utf-8"),
                        file_name=(
                            "gebruiksdoel_transitions_"
                            f"{transition_municipality}_{start_year}_{end_year}.csv"
                        ).replace(" ", "_"),
                        mime="text/csv",
                        key="download_usage_transition_events",
                    )

                selected_from_label = "All origins" if from_filter == "All" else from_filter
                selected_to_label = "All destinations" if to_filter == "All" else to_filter
                selected_transition_label = f"{selected_from_label} → {selected_to_label}"
                selected_transition_events = filtered_events.copy()

                st.markdown("#### Selected transition object analysis")
                st.caption(
                    f"Map, records, annual trend and wijk ranking follow the active Sankey "
                    f"filters: **{selected_transition_label}** · matching: **{matching_mode}**."
                )

                selected_c1, selected_c2, selected_c3 = st.columns(3)
                selected_c1.metric("Transition events", len(selected_transition_events))
                selected_c2.metric(
                    "Changed VBOs",
                    selected_transition_events["vbo_id"].nunique(),
                )
                selected_share = (
                    len(selected_transition_events) / len(scope_events) * 100
                    if len(scope_events) > 0 else 0.0
                )
                selected_c3.metric("Share of scope transitions", f"{selected_share:.1f}%")

                if selected_transition_events.empty:
                    st.info(
                        "No objects match the current Sankey transition filters and detail scope."
                    )
                else:
                    selected_annual = (
                        selected_transition_events.groupby("transition_year")
                        .size()
                        .reindex(range(start_year, end_year + 1), fill_value=0)
                        .rename("transition_events")
                        .reset_index()
                    )
                    selected_trend = px.line(
                        selected_annual,
                        x="transition_year",
                        y="transition_events",
                        markers=True,
                        title=f"Selected transitions through time — {scope_label}",
                        labels={
                            "transition_year": "Year",
                            "transition_events": "Transition events",
                        },
                    )
                    selected_trend.update_traces(
                        line={"color": PRIMARY_COLOR, "width": 3},
                        marker={"color": SELECTED_COLOR, "size": 8},
                    )
                    render_plotly_chart(selected_trend, use_container_width=True)

                    selected_by_wijk = (
                        selected_transition_events.groupby(
                            ["gemeentenaam", "wijknaam"], dropna=False
                        )
                        .agg(
                            transition_events=("vbo_id", "size"),
                            changed_vbos=("vbo_id", "nunique"),
                        )
                        .reset_index()
                        .sort_values("transition_events", ascending=False)
                    )
                    selected_wijk_chart = px.bar(
                        selected_by_wijk.head(15),
                        x="wijknaam",
                        y="transition_events",
                        hover_data={"changed_vbos": True, "gemeentenaam": True},
                        title=f"Top wijken · {selected_transition_label}",
                        labels={
                            "wijknaam": "Wijk",
                            "transition_events": "Transition events",
                        },
                    )
                    selected_wijk_chart.update_traces(marker_color=PRIMARY_COLOR)
                    render_plotly_chart(selected_wijk_chart, use_container_width=True)

                    selected_map_tab, selected_table_tab = st.tabs(
                        ["Selected transition object map", "Selected transition records"]
                    )
                    with selected_map_tab:
                        render_transition_point_map(
                            selected_transition_events,
                            f"{selected_transition_label} — {scope_label}",
                            key="selected_transition_point_map",
                        )
                    with selected_table_tab:
                        selected_transition_table = selected_transition_events[
                            [
                                "vbo_id",
                                "transition_date",
                                "transition_year",
                                "wijknaam",
                                "buurtnaam",
                                "from_use",
                                "to_use",
                            ]
                        ].copy()
                        selected_transition_table["bag_viewer"] = (
                            selected_transition_table["vbo_id"].apply(bag_viewer_url)
                        )
                        st.dataframe(
                            selected_transition_table.sort_values(
                                ["transition_year", "wijknaam", "buurtnaam"],
                                ascending=[False, True, True],
                            ),
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "bag_viewer": st.column_config.LinkColumn(
                                    "Inspect in BAG Viewer", display_text="Open"
                                )
                            },
                        )

                with st.expander("Method and interpretation of the function-change signal"):
                    st.markdown(
                        """
                        - **Unit of observation:** BAG `verblijfsobject`, because `gebruiksdoel` is not a pand attribute.  
                        - **Transition rule:** the sorted set of use functions in occurrence *t* must differ from occurrence *t−1*. Reordered arrays and repeated identical records therefore do not count as changes.  
                        - **Exact complete set:** source and target must match the complete BAG use sets shown in the controls.  
                        - **Contains individual function:** preserves multi-use sets but filters events containing the selected source and target functions.  
                        - **Collapsed category:** groups the official BAG functions into explicit analytical categories for a simpler overview; this is an analytical transformation, not a BAG field.  
                        - **Traffic light:** each annual count is compared with the local time-series baseline. Green is normal; orange is elevated; red is exceptional. A robust median/MAD score is preferred, with a standard-deviation fallback where necessary.  
                        - **Choropleth count:** absolute selected transition events per area.  
                        - **Choropleth rate:** selected transition events divided by distinct observed VBOs in the area over the selected period.  
                        - **Choropleth share:** selected transitions divided by all function-change events in the same area and map period.  
                        - **Interpretive limit:** a BAG function change is an administrative change in registered use. It does not by itself prove physical conversion, demolition, tourism pressure or a policy intervention.  
                        - **Spatial limit:** the event uses the best available historical/current VBO point. Current wijk and buurt polygons are used for analytical aggregation.
                        """
                    )



    else:
        st.header("Historical municipality boundary inspector")
        st.caption(
            "A lightweight single-map view replaces the former synchronized wijk/buurt "
            "time-travel maps. Select a year to see the valid predecessor municipalities "
            "against the present-day analysis footprint."
        )
        render_historical_boundary_year_viewer(
            tuple(current_reference_municipalities)
        )


# ------------------------------------------------------------
# Data table
# ------------------------------------------------------------

with tab_data:
    st.header("Aggregation table")

    if table_aggregation == "pand":
        table = records[
            [
                "pand_id",
                "event_gemeentenaam",
                "current_gemeentenaam",
                "gemeentenaam",
                "wijknaam",
                "buurtnaam",
                "bouwjaar",
                "demolition_year",
                "lifespan_years",
            ]
        ].sort_values(["gemeentenaam", "demolition_year", "pand_id"])
        table["bag_viewer"] = table["pand_id"].apply(bag_viewer_url)

    elif table_aggregation == "year":
        table = (
            records.groupby(["gemeentenaam", "demolition_year"])
            .agg(
                demolished_panden=("pand_id", "count"),
                mean_lifespan_years=("lifespan_years", "mean"),
            )
            .reset_index()
            .sort_values(["gemeentenaam", "demolition_year"])
        )

    elif table_aggregation == "municipality":
        table = (
            records.groupby(["gemeentenaam"])
            .agg(
                demolished_panden=("pand_id", "count"),
                mean_lifespan_years=("lifespan_years", "mean"),
                min_year=("demolition_year", "min"),
                max_year=("demolition_year", "max"),
            )
            .reset_index()
            .sort_values("gemeentenaam")
        )

    elif table_aggregation == "wijk":
        table = (
            records.groupby(["gemeentenaam", "wijknaam"], dropna=False)
            .agg(
                demolished_panden=("pand_id", "count"),
                mean_lifespan_years=("lifespan_years", "mean"),
                min_year=("demolition_year", "min"),
                max_year=("demolition_year", "max"),
            )
            .reset_index()
            .sort_values(
                ["gemeentenaam", "demolished_panden"],
                ascending=[True, False],
            )
        )

    else:
        table = (
            records.groupby(
                ["gemeentenaam", "wijknaam", "buurtnaam"],
                dropna=False,
            )
            .agg(
                demolished_panden=("pand_id", "count"),
                mean_lifespan_years=("lifespan_years", "mean"),
                min_year=("demolition_year", "min"),
                max_year=("demolition_year", "max"),
            )
            .reset_index()
            .sort_values(
                ["gemeentenaam", "demolished_panden"],
                ascending=[True, False],
            )
        )

    column_config = {}
    if "bag_viewer" in table.columns:
        column_config["bag_viewer"] = st.column_config.LinkColumn(
            "BAG Viewer",
            display_text="Open object",
        )

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config=column_config,
    )

    csv = table.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download current table as CSV",
        data=csv,
        file_name=(
            f"demolition_{table_aggregation}_{start_year}_{end_year}.csv"
        ),
        mime="text/csv",
    )


# ------------------------------------------------------------
# Validation and audit
# ------------------------------------------------------------

with tab_validation:
    st.header("Validation and audit trail")
    st.caption(
        "This tab makes exclusions and ambiguous BAG histories visible. "
        "Its municipality filter follows current-reference geography because the "
        "existing validation view is administratively enriched with current areas."
    )

    validation_views_ready = relation_exists("thesis_ch4.vw_validation_issues")
    if not validation_views_ready:
        st.error(
            "Validation views are not installed in the connected database. "
            "Run `bag_validation_views_v2.sql` in pgAdmin, then clear the Streamlit cache and reload."
        )

    counts = load_validation_counts(
        current_reference_municipalities,
        start_year,
        end_year,
    )
    validation_summary = build_validation_summary(counts)

    total_flagged = int(validation_summary["affected_records"].sum())
    review_count = int(
        validation_summary.loc[
            validation_summary["severity"].str.contains(
                "Review|Potential",
                case=False,
                regex=True,
            ),
            "affected_records",
        ].sum()
    )
    excluded_count = int(
        validation_summary.loc[
            validation_summary["severity"].str.contains(
                "Excluded",
                case=False,
                regex=True,
            ),
            "affected_records",
        ].sum()
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Issue instances", total_flagged)
    c2.metric("Manual-review instances", review_count)
    c3.metric("Exclusion instances", excluded_count)

    st.info(
        "One pand may occur under several validation tests, so these metrics are "
        "issue instances rather than a deduplicated number of buildings."
    )

    st.markdown("### Test register")
    st.dataframe(
        validation_summary.drop(columns=["issue_type"]),
        use_container_width=True,
        hide_index=True,
        column_config={
            "affected_records": st.column_config.NumberColumn(
                "Affected records",
                format="%d",
            )
        },
    )

    st.markdown("### Inspect affected records")

    labels = {
        row["issue_type"]: row["test"]
        for row in VALIDATION_CATALOG
    }

    selected_issue_type = st.selectbox(
        "Validation test",
        options=list(labels.keys()),
        format_func=lambda value: labels[value],
    )

    filter_col1, filter_col2 = st.columns([2, 1])
    with filter_col1:
        issue_search = st.text_input(
            "Search pand_id or issue detail",
            placeholder="For example: 0104100000124802",
        )
    with filter_col2:
        issue_limit = st.selectbox(
            "Maximum rows",
            [25, 50, 100, 250, 500],
            index=1,
        )

    issues = load_validation_issues(
        selected_issue_type,
        current_reference_municipalities,
        start_year,
        end_year,
        issue_search,
        issue_limit,
    )

    if issues.empty:
        st.success("No affected records were found for this test and filter.")
    else:
        st.dataframe(
            issues,
            use_container_width=True,
            hide_index=True,
            column_config={
                "bag_viewer": st.column_config.LinkColumn(
                    "BAG Viewer",
                    display_text="Verify",
                ),
                "lon": st.column_config.NumberColumn(format="%.6f"),
                "lat": st.column_config.NumberColumn(format="%.6f"),
            },
        )

        issue_csv = issues.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download validation result as CSV",
            data=issue_csv,
            file_name=(
                f"validation_{selected_issue_type.lower()}_"
                f"{start_year}_{end_year}.csv"
            ),
            mime="text/csv",
        )

    st.divider()
    st.markdown("### Verify one BAG pand")

    suggested_pand = ""
    if not issues.empty:
        first_valid_id = issues["pand_id"].dropna()
        if not first_valid_id.empty:
            suggested_pand = str(first_valid_id.iloc[0])

    pand_id_input = st.text_input(
        "BAG pand identification",
        value=suggested_pand,
        placeholder="16-digit BAG pand_id",
        key="validation_pand_id",
    ).strip()

    if pand_id_input:
        viewer_url = bag_viewer_url(pand_id_input)
        st.link_button("Open current object in BAG Viewer", viewer_url)

        history = load_pand_history(pand_id_input)

        if history.empty:
            st.warning(
                "This pand_id was not found in the local BAG history table."
            )
        else:
            st.markdown("#### Local BAG history")
            st.dataframe(
                history,
                use_container_width=True,
                hide_index=True,
            )

            demolition_transitions = int(
                history["is_demolition_transition"].fillna(False).sum()
            )
            reversals = int(
                history["is_reversal_after_demolition"].fillna(False).sum()
            )
            latest_status = str(history.iloc[-1]["pandstatus"])

            h1, h2, h3 = st.columns(3)
            h1.metric("Status versions", len(history))
            h2.metric("Demolition transitions", demolition_transitions)
            h3.metric("Reversals after demolition", reversals)

            st.write(f"**Latest local status:** {latest_status}")

        api_key = get_bag_api_key()

        if api_key:
            st.markdown("#### Live LV-BAG API check")
            st.warning(
                "The API and BAG Viewer show current near-real-time LV-BAG data. "
                "Your thesis database is a snapshot, so a difference may represent "
                "a later mutation rather than an error in your extraction."
            )

            if st.button("Retrieve live BAG API record"):
                try:
                    live_data = fetch_live_bag_pand(pand_id_input)
                    st.success("Live BAG response received.")
                    with st.expander("Show raw live BAG JSON", expanded=True):
                        st.json(live_data)
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response else "unknown"
                    st.error(f"BAG API request failed with HTTP status {status}.")
                except Exception as exc:
                    st.error(str(exc))
        else:
            with st.expander("Optional: enable live BAG API verification"):
                st.code(
                    """# .streamlit/secrets.toml
[bag_api]
api_key = "YOUR_KADASTER_BAG_API_KEY"
""",
                    language="toml",
                )
                st.write(
                    "The BAG Viewer link already supports manual verification without "
                    "an API key. Add the API only when you want the app to retrieve and "
                    "display the current LV-BAG response automatically."
                )
