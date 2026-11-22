"""Streamlit dashboard. Launch with `leads dashboard` (or `streamlit run src/leadgen/dashboard/app.py -- --config config/config.yaml`)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import altair as alt
import pandas as pd
import streamlit as st

from leadgen.dashboard.errors import render_error  # stdlib + streamlit only: always importable

try:
    from leadgen.config import AppConfig, SearchConfig, Secrets, load_config, load_secrets
    from leadgen.logging_setup import setup_logging
    from leadgen.models import LEAD_COLUMNS, LeadStatus
    from leadgen.store import open_store
    from leadgen.store.base import LeadStore
    from leadgen.views import OPT_OUT_VALUES, export_filename
except ImportError as e:  # e.g. the code was updated while this dashboard was running
    st.set_page_config(page_title="Leads", page_icon="📇", layout="wide")
    render_error(e)
    st.stop()

# One hue for single-series magnitude bars (validated reference palette; light/dark steps).
BAR_COLOR = {"light": "#2a78d6", "dark": "#3987e5"}
EDITABLE = ["status", "owner", "notes", "next_follow_up_at"]
TABLE_COLUMNS = ["lead_id", "status", "company_name", "email", "email_status", "contact_name", "phone",
                 "website", "city", "country", "industry", "source", "owner", "notes", "next_follow_up_at",
                 "created_at"]  # fmt: skip
TOP_N = 12
ROW_PX = 30  # vertical space per bar row
AXIS_PX = 56  # x-axis labels + title below the bars

st.set_page_config(page_title="Leads", page_icon="📇", layout="wide")


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument("--config", default=os.environ.get("LEADGEN_CONFIG", "config/config.yaml"))
    p.add_argument("--store", default=os.environ.get("LEADGEN_STORE") or None)
    return p.parse_known_args(sys.argv[1:])[0]


@st.cache_resource(show_spinner=False)
def get_context(config_path: str, store_kind: str | None) -> tuple[AppConfig, Secrets, LeadStore]:
    cfg = load_config(config_path)
    setup_logging(cfg.log_dir, cfg.log_level, console=False)
    secrets = load_secrets()
    return cfg, secrets, open_store(cfg, secrets, store_kind)


@st.cache_data(ttl=120, show_spinner="Loading leads…")
def load_df(_store: LeadStore, source: str, nonce: int) -> pd.DataFrame:
    """`source` (config path + store kind) is part of the cache key: `_store` itself is not hashed."""
    leads = _store.load_leads()
    return pd.DataFrame([lead.to_row() for lead in leads], columns=LEAD_COLUMNS)


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def ranked_bars(counts: pd.DataFrame, label: str, order: list[str] | None = None) -> alt.Chart:
    """Ranked horizontal bars, one hue, thin marks with rounded data-ends, hover tooltip."""
    sort = order if order else "-x"
    return (
        alt.Chart(counts)
        # Bar thickness comes from band padding (not a fixed px size) so bars can never overlap,
        # whatever height the container gives the chart.
        .mark_bar(color=BAR_COLOR[theme_mode()], cornerRadiusEnd=4)
        .encode(
            x=alt.X("leads:Q", title="Leads", axis=alt.Axis(tickMinStep=1, gridOpacity=0.35, domain=False)),
            y=alt.Y(
                "label:N",
                sort=sort,
                title=None,
                scale=alt.Scale(paddingInner=0.4, paddingOuter=0.2),
                axis=alt.Axis(labelLimit=240, ticks=False, domain=False, labelOverlap=False),
            ),
            tooltip=[alt.Tooltip("label:N", title=label), alt.Tooltip("leads:Q", title="Leads")],
        )
        .properties(height=ROW_PX * len(counts))
        .configure_view(stroke=None)
    )


def chart_height(counts: pd.DataFrame) -> int:
    return ROW_PX * len(counts) + AXIS_PX


def top_counts(series: pd.Series, n: int = TOP_N) -> pd.DataFrame:
    vc = series.replace("", "(blank)").value_counts()
    if len(vc) > n:
        vc = pd.concat([vc.iloc[:n], pd.Series({"Other": int(vc.iloc[n:].sum())})])
    return pd.DataFrame({"label": vc.index.astype(str), "leads": vc.values})


def chart_card(title: str, counts: pd.DataFrame, label: str, order: list[str] | None = None) -> None:
    st.markdown(f"**{title}**")
    if counts.empty:
        st.caption("No data for the current filters.")
        return
    st.altair_chart(ranked_bars(counts, label, order), width="stretch", height=chart_height(counts))
    with st.expander("Table view"):
        st.dataframe(
            counts.rename(columns={"label": label, "leads": "Leads"}), hide_index=True, width="stretch"
        )


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    def opts(col: str) -> list[str]:
        return sorted(v for v in df[col].dropna().unique() if v)

    c1, c2, c3, c4, c5, c6 = st.columns([1.3, 1, 1, 1, 1, 1.4])
    industry = c1.multiselect("Industry", opts("industry"))
    country = c2.multiselect("Country", opts("country"))
    city = c3.multiselect("City", opts("city"))
    status = c4.multiselect("Status", [s.value for s in LeadStatus])
    email_status = c5.multiselect("Email", ["valid", "risky", "invalid", "missing"])
    text = c6.text_input("Search", placeholder="name, email, domain, notes")

    out = df
    for col, chosen in (("industry", industry), ("country", country), ("city", city),
                        ("status", status), ("email_status", email_status)):  # fmt: skip
        if chosen:
            out = out[out[col].isin(chosen)]
    if text:
        t = text.lower()
        hay = (
            out["company_name"] + " " + out["email"] + " " + out["domain"] + " " + out["notes"]
        ).str.lower()
        out = out[hay.str.contains(t, regex=False)]
    return out


def kpis(df: pd.DataFrame) -> None:
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    total = len(df)
    new_week = int((df["created_at"] >= week_ago).sum()) if total else 0
    with_email = df[df["email"] != ""]
    valid_pct = (with_email["email_status"] == "valid").mean() * 100 if len(with_email) else 0.0
    contactable = int(
        ((df["email"] != "") & (df["email_status"] != "invalid") & ~df["status"].isin(OPT_OUT_VALUES)).sum()
    )
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Leads", f"{total:,}", border=True)
    k2.metric("Added in last 7 days", f"{new_week:,}", border=True)
    k3.metric("Valid email (of leads with email)", f"{valid_pct:.0f}%", border=True)
    k4.metric("Contactable now", f"{contactable:,}", border=True,
              help="Has a non-invalid email and is not unsubscribed / do-not-contact / bounced")  # fmt: skip


def leads_table(df: pd.DataFrame, store: LeadStore, filters: dict[str, object] | None = None) -> None:
    st.subheader("Leads")
    view = df[TABLE_COLUMNS].sort_values("created_at", ascending=False).reset_index(drop=True)
    edited = st.data_editor(
        view,
        hide_index=True,
        width="stretch",
        height=460,
        disabled=[c for c in TABLE_COLUMNS if c not in EDITABLE],
        column_config={
            "status": st.column_config.SelectboxColumn(
                "status", options=[s.value for s in LeadStatus], required=True
            ),
            "website": st.column_config.LinkColumn("website"),
            "notes": st.column_config.TextColumn("notes", width="medium"),
            "lead_id": st.column_config.TextColumn("lead_id", width="small"),
        },
        key="lead_editor",
    )
    changed: dict[str, dict[str, str]] = {}
    status_changes: list[tuple[str, str, str]] = []
    for i in range(len(view)):
        before, after = view.iloc[i], edited.iloc[i]
        diff = {c: str(after[c]) for c in EDITABLE if str(after[c]) != str(before[c])}
        if diff:
            changed[before["lead_id"]] = diff
            if "status" in diff:
                status_changes.append((before["lead_id"], before["status"], diff["status"]))

    left, mid, right = st.columns([1, 1, 4])
    if left.button(f"Save {len(changed)} change(s)", type="primary", disabled=not changed):
        for lead_id, _, new in status_changes:
            if new == LeadStatus.CONTACTED.value:
                changed[lead_id]["last_contacted_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        try:
            store.update_leads(changed)
            for lead_id, old, new in status_changes:
                store.add_activity(lead_id, "status_change", f"{old} -> {new} (dashboard)")
        except Exception as e:
            render_error(e, where="Saving your changes", key="retry_save")
        else:
            st.session_state.nonce = st.session_state.get("nonce", 0) + 1
            st.toast(f"Saved {len(changed)} lead(s)")
            st.rerun()
    filters = filters or {}
    mid.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"),
                        file_name=export_filename(filters, "csv"), mime="text/csv",
                        help="The table as shown, opted-out leads included. For an email tool, use the API link below.")  # fmt: skip
    with st.expander("Same filters as a REST API link (for scripts and email tools)"):
        st.code(api_export_url(filters), language=None)
        st.caption("Contactable leads only: opted-out and invalid emails are left out. Add `&format=json` for JSON. "
                   "Start the API with `leads api` (Swagger docs at /docs).")  # fmt: skip


def run_panel(cfg: AppConfig, secrets: Secrets, store: LeadStore) -> None:
    from leadgen.runner import run_search

    st.subheader("Run a search")
    by_name = {s.name: s for s in cfg.searches}
    configured = list(by_name)
    scheduled = {sch.search for sch in cfg.schedules}

    def describe(name: str) -> str:
        """e.g. 'law-firms-developed · lawyer · 12 locations · osm'."""
        s = by_name.get(name)
        if s is None:
            return name
        where = s.locations[0] if len(s.locations) == 1 else f"{len(s.locations)} locations"
        label = f"{name}  ·  {', '.join(s.keywords)}  ·  {where}  ·  {', '.join(s.sources)}"
        return label + ("  ·  scheduled" if {name, "all"} & scheduled else "")

    with st.form("run_form"):
        mode = st.radio("Search", ["Configured search", "Ad-hoc"], horizontal=True)
        chosen = st.selectbox(
            f"Configured search ({len(configured)} in config.yaml)",
            configured or ["(none in config)"],
            format_func=describe,
            help="Searches come from config/config.yaml. After editing that file, restart the dashboard.",
        )
        c2, c3 = st.columns(2)
        keyword = c2.text_input("Ad-hoc keyword", placeholder="e.g. dentist")
        location = c3.text_input("Ad-hoc location", placeholder="e.g. Bristol, UK")
        c4, c5, c6 = st.columns(3)
        sources = c4.multiselect("Sources", ["osm", "google_places"], default=["osm"])
        limit = c5.number_input("Max new candidates", min_value=1, max_value=1000, value=25)
        dry_run = c6.checkbox("Dry run (don't save)", value=True)
        submitted = st.form_submit_button("Run", type="primary")

    if not submitted:
        return
    if mode == "Ad-hoc":
        if not (keyword and location):
            st.error("Ad-hoc runs need a keyword and a location.")
            return
        search = SearchConfig(
            name="adhoc", keywords=[keyword], locations=[location], sources=sources or ["osm"]
        )
        source_override = None
    else:
        if not configured:
            st.error("No searches in config.yaml.")
            return
        search = cfg.get_search(chosen)
        source_override = sources or None
    try:
        with st.spinner(f"Running '{search.name}' — polite crawling takes a while…"):
            result = asyncio.run(run_search(cfg, secrets, store, search, sources=source_override,
                                            limit=int(limit), dry_run=dry_run))  # fmt: skip
    except Exception as e:
        render_error(e, where=f"Running '{search.name}'", key="retry_run")
        return
    s = result.stats
    st.success(f"{s.run_id}: discovered {s.discovered}, crawled {s.crawled}, new {s.new}, "
               f"duplicates {s.duplicates}, errors {s.errors}" + (" (dry run, nothing saved)" if dry_run else ""))  # fmt: skip
    if s.notes:
        st.warning(s.notes)
    if result.leads:
        st.dataframe(pd.DataFrame([lead.to_row() for lead in result.leads])[TABLE_COLUMNS[2:12]],
                     hide_index=True, width="stretch")  # fmt: skip
    if not dry_run:
        st.session_state.nonce = st.session_state.get("nonce", 0) + 1


def runs_panel(store: LeadStore) -> None:
    st.subheader("Recent runs")
    runs = store.list_runs(25)
    if not runs:
        st.caption("No runs yet.")
        return
    st.dataframe(pd.DataFrame(runs), hide_index=True, width="stretch")


def main() -> None:
    args = _args()
    try:
        cfg, secrets, store = get_context(args.config, args.store)
    except Exception as e:  # config, credentials, database unreachable …
        render_error(e, where="Starting the dashboard")
        st.stop()

    st.title("Leads")
    st.caption(f"Store: {store.name} · config: {args.config}")
    top = st.columns([6, 1])
    if top[1].button("Refresh"):
        st.session_state.nonce = st.session_state.get("nonce", 0) + 1
    df = load_df(store, f"{args.config}|{store.name}", st.session_state.get("nonce", 0))

    tab_overview, tab_leads, tab_run, tab_runs = st.tabs(["Overview", "Leads", "Run search", "Runs"])
    with tab_overview:
        filtered = apply_filters(df)
        kpis(filtered)
        c1, c2 = st.columns(2)
        with c1:
            chart_card("Leads by industry", top_counts(filtered["industry"]), "Industry")
        with c2:
            loc = (
                filtered["city"].where(filtered["city"] != "", "(no city)") + ", " + filtered["country"]
            ).str.strip(", ")
            chart_card("Leads by location", top_counts(loc), "Location")
        c3, c4 = st.columns(2)
        with c3:
            present = [s.value for s in LeadStatus if s.value in set(filtered["status"])]
            counts = filtered["status"].value_counts().reindex(present).fillna(0).astype(int)
            chart_card("Pipeline status", pd.DataFrame({"label": counts.index, "leads": counts.values}),
                       "Status", order=present)  # fmt: skip
        with c4:
            chart_card("Lead source", top_counts(filtered["source"]), "Source")
    with tab_leads:
        filtered_leads, lead_filters = apply_filters_compact(df)
        leads_table(filtered_leads, store, lead_filters)
    with tab_run:
        run_panel(cfg, secrets, store)
    with tab_runs:
        runs_panel(store)


def apply_filters_compact(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    """Leads-tab filters. Returns the filtered table and the filters (for the file name and API link)."""
    c1, c2, c3, c4, c5 = st.columns([1, 1.2, 1, 1, 1.4])
    status = c1.multiselect("Status ", [s.value for s in LeadStatus], key="t_status")
    industry = c2.multiselect("Industry ", sorted(v for v in df["industry"].unique() if v), key="t_industry")
    city = c3.multiselect("City ", sorted(v for v in df["city"].unique() if v), key="t_city")
    email_status = c4.multiselect("Email ", ["valid", "risky", "invalid", "missing"], key="t_email")
    text = c5.text_input("Search ", key="t_text", placeholder="name, email, domain, notes")
    out = df
    for col, chosen in (
        ("status", status),
        ("industry", industry),
        ("city", city),
        ("email_status", email_status),
    ):
        if chosen:
            out = out[out[col].isin(chosen)]
    if text:
        hay = (
            out["company_name"] + " " + out["email"] + " " + out["domain"] + " " + out["notes"]
        ).str.lower()
        out = out[hay.str.contains(text.lower(), regex=False)]
    return out, {
        "status": status,
        "industry": industry,
        "city": city,
        "email_status": email_status,
        "text": text,
    }


def api_export_url(filters: dict[str, object]) -> str:
    """The REST API URL that exports these leads (`leads api`); base URL from LEADGEN_API_URL."""
    base = os.environ.get("LEADGEN_API_URL", "http://localhost:8000").rstrip("/")
    params = [(k, v) for k in ("status", "industry", "city", "email_status") for v in filters.get(k) or []]  # type: ignore[union-attr]
    if filters.get("text"):
        params.append(("q", str(filters["text"])))
    return f"{base}/api/v1/leads/export" + (f"?{urlencode(params)}" if params else "")


try:
    main()
except Exception as e:  # e.g. the database dropped mid-page; Streamlit's own rerun/stop signals pass through
    render_error(e)
