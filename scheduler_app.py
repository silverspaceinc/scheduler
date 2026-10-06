"""
Vizva Scheduler — a dedicated, standalone Streamlit app.

It runs the SAME solver as the dashboard's Schedule View (the solver is
imported verbatim from scheduler_core.py, which was extracted byte-for-byte
from app.py), renders the SAME resolved Gantt, and adds one thing the
dashboard cannot do: you can drag an interview bar VERTICALLY to another
expert lane.

Vertical-only is enforced by construction: the drag board moves a card
between EXPERT containers, and the interview's time is part of the card
text and is never editable. Nothing in this app can change a start time.

Run:  streamlit run scheduler_app.py
"""
import json
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from scheduler_core import (
    build_schedule_data,
    enforce_presence_first,
    resolve_clashes,
    enforce_gap_policy,
    optimize_expertise_match,
    apply_presence_first_labels,
    render_resolved_gantt,
    load_expert_config,
    get_expertise_map,
    is_present,
    eligible_experts,
    _parse_time_to_minutes,
    _minutes_to_label,
    GAP_MINUTES,
)

try:
    from streamlit_sortables import sort_items
    HAS_SORTABLES = True
except Exception:
    HAS_SORTABLES = False

APP_VERSION = "SCHED v1.0 — vertical drag scheduler"

st.set_page_config(page_title="Vizva Scheduler [v1.0]", page_icon="🗓️", layout="wide")

API_KEY = st.secrets.get("API_KEY", "")
BASE_URL = st.secrets.get("BASE_URL", "")
# Same login credentials as the dashboard
VIZVA_USERNAME = st.secrets.get("VIZVA_USERNAME", "")
VIZVA_PASSWORD = st.secrets.get("VIZVA_PASSWORD", "")


# ═══════════════════════════════════════════════════════════════════
#  DATA LOADING  (same endpoint + normalisation as the dashboard)
# ═══════════════════════════════════════════════════════════════════
@st.cache_data(ttl=600, show_spinner="Loading interviews...")
def load_interviews(pipeline_version="sched-v1"):
    headers = {"x-api-key": API_KEY}
    records, offset, limit = [], 0, 500
    while True:
        r = requests.get(BASE_URL + "/api/app-case", headers=headers,
                         params={"limit": limit, "offset": offset})
        r.raise_for_status()
        batch = r.json()["data"]
        if not batch:
            break
        records.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    df = pd.DataFrame(records)
    if df.empty:
        return df
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if "task_status" in df.columns:
        df["task_status"] = (df["task_status"].fillna("pending").astype(str)
                             .str.strip().str.lower().replace("not done", "pending"))
        df.loc[~df["task_status"].isin(["completed", "rescheduled", "cancelled", "pending"]),
               "task_status"] = "pending"
    if "support_name" in df.columns:
        df["support_name"] = df["support_name"].astype(str).str.strip()
    if "expert_status_flag" in df.columns:
        df = df[df["expert_status_flag"] == True].copy()
    if "date" in df.columns:
        df = df[df["date"].dt.year == datetime.now().year].copy()
    return df


def expert_names_for(df):
    if df.empty or "expert_name" not in df.columns:
        return []
    names = [str(x).strip() for x in df["expert_name"].dropna().unique()]
    return sorted({n for n in names if n and n.lower() not in ("hcr", "self", "nan", "none")})


# ═══════════════════════════════════════════════════════════════════
#  SOLVE  — identical call sequence to the dashboard's Schedule View
# ═══════════════════════════════════════════════════════════════════
def solve(sched, all_expert_names, expertise_map, presence_map, round_map,
          expertise_source, reallocate_absent):
    resolved = enforce_presence_first(
        sched, all_expert_names, expertise_map=expertise_map,
        round_map=round_map, presence_map=presence_map,
        expertise_source=expertise_source, enabled=reallocate_absent)
    resolved = resolve_clashes(
        resolved, all_expert_names, expertise_map=expertise_map,
        presence_map=presence_map, round_map=round_map,
        expertise_source=expertise_source)
    resolved = enforce_gap_policy(
        resolved, all_expert_names, expertise_map=expertise_map,
        presence_map=presence_map, round_map=round_map,
        expertise_source=expertise_source)
    resolved = optimize_expertise_match(
        resolved, all_expert_names, expertise_map=expertise_map,
        round_map=round_map, presence_map=presence_map,
        reallocate_absent=False, expertise_source=expertise_source)
    resolved = apply_presence_first_labels(resolved, presence_map)
    return resolved


# ═══════════════════════════════════════════════════════════════════
#  VALIDATION of a manually moved schedule
# ═══════════════════════════════════════════════════════════════════
def validate(resolved, presence_map):
    """Return a list of human-readable problems with the current assignment."""
    problems = []
    df = resolved.copy()
    if df.empty:
        return problems

    # 1) clashes + 2) gap rule, per expert
    for expert, grp in df.groupby("expert_name"):
        items = grp.sort_values("start_min")[["start_min", "end_min", "candidate_name",
                                              "start_label"]].to_dict("records")
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                s1, e1 = items[a]["start_min"], items[a]["end_min"]
                s2, e2 = items[b]["start_min"], items[b]["end_min"]
                if s1 < e2 and s2 < e1:
                    problems.append(("CLASH", expert,
                                     "%s and %s overlap for %s"
                                     % (items[a]["candidate_name"], items[b]["candidate_name"], expert)))
                else:
                    gap = min(abs(e1 - s2), abs(e2 - s1))
                    if 0 <= gap < GAP_MINUTES:
                        problems.append(("GAP", expert,
                                         "only %d min between %s and %s for %s (rule: %d min)"
                                         % (gap, items[a]["candidate_name"],
                                            items[b]["candidate_name"], expert, GAP_MINUTES)))

    # 3) absent experts holding interviews
    if presence_map:
        for expert, grp in df.groupby("expert_name"):
            if not is_present(expert, presence_map) and len(grp) > 0:
                problems.append(("ABSENT", expert,
                                 "%d interview(s) sit with an Absent expert" % len(grp)))
    return problems


# ═══════════════════════════════════════════════════════════════════
#  DRAG BOARD  — vertical only: containers are EXPERTS
# ═══════════════════════════════════════════════════════════════════
def card_text(row):
    return "%s–%s · %s · %s · %s · #%d" % (
        row.get("start_label", "?"), row.get("end_label", "?"),
        str(row.get("candidate_name", ""))[:28],
        str(row.get("company_name", ""))[:22],
        str(row.get("round_name", ""))[:24],
        int(row["row_id"]))


def parse_card_id(text):
    try:
        return int(str(text).rsplit("#", 1)[1].strip())
    except Exception:
        return None


def render_drag_board(resolved, all_expert_names, overrides):
    """Expert lanes as drag containers. Returns the new {row_id: expert} map."""
    df = resolved.copy()
    if "row_id" not in df.columns:
        df = df.reset_index(drop=True)
        df["row_id"] = range(len(df))

    lanes = list(all_expert_names)
    for e in df["expert_name"].dropna().unique():
        e = str(e)
        if e not in lanes and e.lower() not in ("hcr", "self"):
            lanes.append(e)

    containers = []
    for lane in lanes:
        sub = df[df["expert_name"].astype(str) == lane].sort_values("start_min")
        containers.append({"header": lane,
                           "items": [card_text(r) for _, r in sub.iterrows()]})
    if not containers:
        st.info("No expert lanes to show.")
        return overrides

    style = """
    .sortable-component { background: transparent; }
    .sortable-container-header { font-weight: 600; padding-left: .5rem; }
    .sortable-item { font-family: ui-monospace, monospace; font-size: 12px; }
    """
    try:
        result = sort_items(containers, multi_containers=True, custom_style=style)
    except TypeError:
        result = sort_items(containers, multi_containers=True)

    new_overrides = dict(overrides)
    if isinstance(result, list):
        for cont in result:
            if not isinstance(cont, dict):
                continue
            lane = str(cont.get("header", ""))
            for item in cont.get("items", []):
                rid = parse_card_id(item)
                if rid is not None and lane:
                    new_overrides[rid] = lane
    return new_overrides


# ═══════════════════════════════════════════════════════════════════
#  FALLBACK EDITOR (used when streamlit-sortables is unavailable)
# ═══════════════════════════════════════════════════════════════════
def render_fallback_editor(resolved, all_expert_names, overrides):
    df = resolved.copy()
    if "row_id" not in df.columns:
        df = df.reset_index(drop=True)
        df["row_id"] = range(len(df))
    df = df.sort_values(["expert_name", "start_min"])
    view = pd.DataFrame({
        "row_id": df["row_id"].values,
        "Time": df["start_label"].astype(str).values,
        "Candidate": df["candidate_name"].astype(str).values,
        "Company": df["company_name"].astype(str).values,
        "Round": df["round_name"].astype(str).values,
        "Expert": df["expert_name"].astype(str).values,
    })
    edited = st.data_editor(
        view, hide_index=True, use_container_width=True, key="fallback_editor",
        column_config={
            "row_id": st.column_config.NumberColumn("row_id", disabled=True),
            "Time": st.column_config.TextColumn("Time", disabled=True, help="Locked — vertical moves only"),
            "Candidate": st.column_config.TextColumn("Candidate", disabled=True),
            "Company": st.column_config.TextColumn("Company", disabled=True),
            "Round": st.column_config.TextColumn("Round", disabled=True),
            "Expert": st.column_config.SelectboxColumn("Expert", options=list(all_expert_names)),
        })
    new_overrides = dict(overrides)
    for _, r in edited.iterrows():
        new_overrides[int(r["row_id"])] = str(r["Expert"])
    return new_overrides


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════
def main():
    st.title("🗓️ Vizva Scheduler")
    st.caption(APP_VERSION + " · same solver as Schedule View · drag a card up/down to move it "
               "to another expert lane. Times are locked — vertical moves only.")

    if not API_KEY or not BASE_URL:
        st.error("Set API_KEY and BASE_URL in `.streamlit/secrets.toml`.")
        st.stop()
    if not VIZVA_USERNAME or not VIZVA_PASSWORD:
        st.error("Set VIZVA_USERNAME and VIZVA_PASSWORD in `.streamlit/secrets.toml`.")
        st.stop()

    all_data = load_interviews()
    if all_data is None or all_data.empty:
        st.warning("No interview data returned by the API.")
        st.stop()

    interviews = all_data[all_data["support_name"].str.lower() == "interview support"].copy()
    if interviews.empty or "date" not in interviews.columns:
        st.warning("No Interview Support rows found.")
        st.stop()

    all_expert_names = expert_names_for(all_data)

    # ── expert config (expertise + presence) ─────────────────────
    cfg = load_expert_config()
    expertise_map = get_expertise_map(cfg)
    presence_map = cfg.get("presence", {}) or {}
    round_map = cfg.get("round_expertise", {}) or {}
    reallocate_absent = bool(cfg.get("reallocate_absent", False))
    expertise_source = ("owner" if str(cfg.get("task_expertise_source", "round")).lower() == "owner"
                        else "round")

    # ── date ─────────────────────────────────────────────────────
    valid = interviews["date"].dropna()
    if valid.empty:
        st.warning("No valid dates.")
        st.stop()
    min_d, max_d = valid.min().date(), valid.max().date()
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        selected_date = st.date_input("Date", value=min(max_d, max(min_d, date.today())),
                                      min_value=min_d, max_value=max_d, key="sched_date")
    with c2:
        st.metric("Experts", len(all_expert_names))
    with c3:
        st.caption("Solver: P1 presence → P2 clashes → P3 round preference → P4 10-min gap → "
                   "P5 expertise. Self / HCR are never touched.")

    sched = build_schedule_data(all_data, selected_date)
    if sched.empty:
        st.info("No interviews scheduled on " + str(selected_date) + ".")
        return

    # stable row ids so manual moves can be tracked
    sched = sched.reset_index(drop=True)
    sched["row_id"] = range(len(sched))

    # ── solve (same sequence as the dashboard) ───────────────────
    resolved = solve(sched, all_expert_names, expertise_map, presence_map,
                     round_map, expertise_source, reallocate_absent)
    resolved = resolved.reset_index(drop=True)
    resolved["row_id"] = range(len(resolved))

    # ── apply manual overrides (vertical moves) ──────────────────
    if "overrides" not in st.session_state:
        st.session_state["overrides"] = {}
    overrides = st.session_state["overrides"]

    def apply_overrides(df, ov):
        out = df.copy()
        if not ov:
            return out
        moved = out["row_id"].map(lambda r: ov.get(int(r)))
        mask = moved.notna()
        out.loc[mask, "expert_name"] = moved[mask]
        if "manual_move" not in out.columns:
            out["manual_move"] = False
        out.loc[mask, "manual_move"] = True
        return out

    resolved = apply_overrides(resolved, overrides)

    # ── KPIs ─────────────────────────────────────────────────────
    k = st.columns(4)
    k[0].metric("Interviews", len(resolved))
    k[1].metric("Experts Used", resolved["expert_name"].nunique())
    k[2].metric("Manually Moved", int(resolved.get("manual_move", pd.Series(dtype=bool)).sum())
                if "manual_move" in resolved.columns else 0)
    k[3].metric("Clash Flags", int(resolved["has_clash"].sum())
                if "has_clash" in resolved.columns else 0)

    # ── validation ───────────────────────────────────────────────
    problems = validate(resolved, presence_map)
    if problems:
        kinds = {}
        for kind, _e, msg in problems:
            kinds.setdefault(kind, []).append(msg)
        for kind, msgs in kinds.items():
            label = {"CLASH": "Overlapping interviews", "GAP": "10-minute gap rule",
                     "ABSENT": "Absent expert"}.get(kind, kind)
            st.warning("**" + label + "** (" + str(len(msgs)) + ")\n\n" +
                       "\n".join("- " + m for m in msgs[:8]))
    else:
        st.success("No clashes, gap violations or absent-expert assignments on this date.")

    # ── the resolved Gantt (same renderer as the dashboard) ──────
    st.markdown("---")
    st.subheader("Solved Gantt — " + str(selected_date))
    try:
        render_resolved_gantt(resolved, selected_date, all_expert_names)
    except Exception as exc:
        st.info("Using the built-in Gantt renderer (" + type(exc).__name__ + ").")
        _simple_gantt(resolved)

    # ── the drag board ───────────────────────────────────────────
    st.markdown("---")
    st.subheader("Move interviews between expert lanes")
    st.caption("Drag a card **up or down** into another expert's lane. The time on the card is "
               "locked — this board cannot change when an interview happens, only who covers it.")

    if HAS_SORTABLES:
        before = dict(overrides)
        after = render_drag_board(resolved, all_expert_names, overrides)
        if after != before:
            st.session_state["overrides"] = after
            st.rerun()
    else:
        st.info("`streamlit-sortables` is not installed, so the drag board is disabled. "
                "Install it with `pip install streamlit-sortables`, or use the editor below.")
        before = dict(overrides)
        after = render_fallback_editor(resolved, all_expert_names, overrides)
        if after != before:
            st.session_state["overrides"] = after
            st.rerun()

    # ── controls ─────────────────────────────────────────────────
    st.markdown("---")
    a, b = st.columns([1, 3])
    with a:
        if st.button("↩︎ Reset manual moves", use_container_width=True):
            st.session_state["overrides"] = {}
            st.rerun()
    with b:
        if overrides:
            st.download_button(
                "⬇️ Download manual moves (JSON)",
                data=json.dumps({str(kk): vv for kk, vv in overrides.items()}, indent=2).encode(),
                file_name="scheduler_moves_" + str(selected_date) + ".json",
                mime="application/json")


def _simple_gantt(resolved):
    """Minimal fallback Gantt: y = expert lane, x = time."""
    if resolved.empty:
        return
    fig = go.Figure()
    for _, r in resolved.sort_values("start_min").iterrows():
        fig.add_trace(go.Bar(
            x=[r["end_min"] - r["start_min"]], y=[str(r["expert_name"])],
            base=[r["start_min"]], orientation="h",
            marker_color="#e74c3c" if r.get("has_clash") else "#2ecc71",
            text=str(r.get("candidate_name", ""))[:18], textposition="inside",
            hovertext="%s–%s · %s · %s" % (r.get("start_label"), r.get("end_label"),
                                            r.get("candidate_name"), r.get("round_name")),
            showlegend=False))
    fig.update_layout(barmode="overlay", height=max(400, resolved["expert_name"].nunique() * 34),
                      xaxis_title="Time (minutes from midnight)", yaxis_title="Expert")
    st.plotly_chart(fig, use_container_width=True)


def login():
    """Login gate — uses the SAME credentials as the dashboard.

    Secrets required (identical keys to the dashboard's secrets.toml):
        API_KEY, BASE_URL, VIZVA_USERNAME, VIZVA_PASSWORD
    """
    st.title("🗓️ Vizva Scheduler — Sign in")
    st.caption("Use the same username and password as the Vizva dashboard.")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)
    if submitted:
        try:
            valid_user = st.secrets["VIZVA_USERNAME"]
            valid_pass = st.secrets["VIZVA_PASSWORD"]
        except Exception:
            st.error("Login secrets (VIZVA_USERNAME / VIZVA_PASSWORD) not configured.")
            st.stop()
        if username.strip() == str(valid_user).strip() and password == str(valid_pass):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Invalid username or password.")
    st.stop()


if __name__ == "__main__":
    if st.session_state.get("authenticated"):
        main()
    else:
        login()
