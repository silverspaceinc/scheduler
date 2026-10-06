"""
Vizva Scheduler v1.1

Renders the dashboard's Schedule View EXACTLY (same code, extracted verbatim
into scheduler_core.py — including the Expert Expertise & Presence panel and
the full P1-P5 clash resolution), then adds one extra section: a drag board
that lets you move interview bars VERTICALLY between expert lanes.

Vertical-only is enforced by construction: a drag changes only which expert
container a card sits in, and no code path ever writes start_min / end_min.

Run:  streamlit run scheduler_app.py
"""
import json
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from scheduler_core import (
    fetch_all_data, normalize, filter_current_year, filter_active_experts,
    render_schedule_view, build_schedule_data, load_expert_config,
    get_expertise_map, enforce_presence_first, resolve_clashes,
    enforce_gap_policy, optimize_expertise_match, apply_presence_first_labels,
    is_present, GAP_MINUTES,
)

try:
    from streamlit_sortables import sort_items
    HAS_SORTABLES = True
except Exception:
    HAS_SORTABLES = False

APP_VERSION = "SCHED v1.1 — full Schedule View + vertical drag"

st.set_page_config(page_title="Vizva Scheduler [v1.1]", page_icon="🗓️", layout="wide")

API_KEY = st.secrets.get("API_KEY", "")
BASE_URL = st.secrets.get("BASE_URL", "")
VIZVA_USERNAME = st.secrets.get("VIZVA_USERNAME", "")
VIZVA_PASSWORD = st.secrets.get("VIZVA_PASSWORD", "")


@st.cache_data(ttl=600, show_spinner="Loading interviews...")
def load_frames(pipeline_version="sched-v1.1"):
    """Same pipeline as the dashboard: all_case_df + active_expert_df."""
    raw = fetch_all_data()
    if raw is None or raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    raw = normalize(raw)
    raw = filter_current_year(raw)
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    if "expert_name" in raw.columns:
        self_mask = raw["expert_name"].str.strip().str.lower() == "self"
        if "task_status" in raw.columns:
            raw.loc[self_mask, "task_status"] = "completed"
        fb = ("This Interview is given by Candidate himself and so "
              "no support was required.")
        for col in ["feedback", "expert_feedback", "client_feedback"]:
            if col in raw.columns:
                raw.loc[self_mask, col] = fb
    all_case_df = raw.copy()
    active_expert_df = filter_active_experts(raw)
    return all_case_df, active_expert_df


def solve(sched, all_expert_names, expertise_map, presence_map, round_map,
          expertise_source, reallocate_absent):
    """Identical five-pass sequence to the dashboard."""
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
    return apply_presence_first_labels(resolved, presence_map)


def validate(resolved, presence_map):
    problems = []
    if resolved is None or resolved.empty:
        return problems
    for expert, grp in resolved.groupby("expert_name"):
        items = grp.sort_values("start_min")[["start_min", "end_min",
                                              "candidate_name"]].to_dict("records")
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                s1, e1 = items[a]["start_min"], items[a]["end_min"]
                s2, e2 = items[b]["start_min"], items[b]["end_min"]
                if s1 < e2 and s2 < e1:
                    problems.append(("CLASH", expert,
                                     "%s and %s overlap for %s"
                                     % (items[a]["candidate_name"],
                                        items[b]["candidate_name"], expert)))
                else:
                    gap = min(abs(e1 - s2), abs(e2 - s1))
                    if 0 <= gap < GAP_MINUTES:
                        problems.append(("GAP", expert,
                                         "only %d min between %s and %s for %s"
                                         % (gap, items[a]["candidate_name"],
                                            items[b]["candidate_name"], expert)))
    if presence_map:
        for expert, grp in resolved.groupby("expert_name"):
            if not is_present(expert, presence_map) and len(grp) > 0:
                problems.append(("ABSENT", expert,
                                 "%d interview(s) sit with an Absent expert" % len(grp)))
    return problems


def card_text(row):
    return "%s-%s | %s | %s | %s | #%d" % (
        row.get("start_label", "?"), row.get("end_label", "?"),
        str(row.get("candidate_name", ""))[:26],
        str(row.get("company_name", ""))[:20],
        str(row.get("round_name", ""))[:22], int(row["row_id"]))


def parse_card_id(text):
    try:
        return int(str(text).rsplit("#", 1)[1].strip())
    except Exception:
        return None


def render_drag_board(resolved, all_expert_names, overrides):
    df = resolved.copy().reset_index(drop=True)
    df["row_id"] = range(len(df))
    lanes = list(all_expert_names)
    for e in df["expert_name"].dropna().unique():
        e = str(e)
        if e not in lanes and e.strip().lower() not in ("hcr", "self"):
            lanes.append(e)
    containers = []
    for lane in lanes:
        sub = df[df["expert_name"].astype(str) == lane].sort_values("start_min")
        containers.append({"header": lane,
                           "items": [card_text(r) for _, r in sub.iterrows()]})
    if not containers:
        return overrides
    try:
        result = sort_items(containers, multi_containers=True)
    except TypeError:
        result = sort_items(containers, multi_containers=True)
    new_ov = dict(overrides)
    if isinstance(result, list):
        for cont in result:
            if not isinstance(cont, dict):
                continue
            lane = str(cont.get("header", ""))
            for item in cont.get("items", []):
                rid = parse_card_id(item)
                if rid is not None and lane:
                    new_ov[rid] = lane
    return new_ov


def render_fallback_editor(resolved, all_expert_names, overrides):
    df = resolved.copy().reset_index(drop=True)
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
            "Time": st.column_config.TextColumn("Time", disabled=True,
                                                help="Locked - vertical moves only"),
            "Candidate": st.column_config.TextColumn("Candidate", disabled=True),
            "Company": st.column_config.TextColumn("Company", disabled=True),
            "Round": st.column_config.TextColumn("Round", disabled=True),
            "Expert": st.column_config.SelectboxColumn("Expert",
                                                       options=list(all_expert_names)),
        })
    new_ov = dict(overrides)
    for _, r in edited.iterrows():
        new_ov[int(r["row_id"])] = str(r["Expert"])
    return new_ov


def main():
    st.caption(APP_VERSION)
    if not API_KEY or not BASE_URL:
        st.error("Set API_KEY and BASE_URL in `.streamlit/secrets.toml`.")
        st.stop()

    all_case_df, active_expert_df = load_frames()
    if all_case_df is None or all_case_df.empty:
        st.warning("No data returned by the API.")
        st.stop()

    # ══════════════════════════════════════════════════════════════
    #  THE DASHBOARD'S SCHEDULE VIEW, UNCHANGED
    #  (includes the Expert Expertise & Presence panel and the full
    #   P1-P5 Intelligent Clash Resolution)
    # ══════════════════════════════════════════════════════════════
    render_schedule_view(all_case_df, active_expert_df)

    # ══════════════════════════════════════════════════════════════
    #  ADD-ON: manual vertical lane adjustment
    # ══════════════════════════════════════════════════════════════
    st.markdown("---")
    st.header("✋ Manual Lane Adjustment (vertical moves only)")
    st.caption("Drag a card up or down into another expert's lane. The time on the card is "
               "locked - this board cannot change when an interview happens, only who covers it.")

    selected_date = st.session_state.get("schedule_date", date.today())
    sched = build_schedule_data(all_case_df, selected_date)
    if sched.empty:
        st.info("No interviews with valid time data on " + str(selected_date) + ".")
        return

    sched = sched.reset_index(drop=True)
    sched["row_id"] = range(len(sched))

    EXCLUDE = {"hcr", "self"}
    all_expert_names = sorted([
        str(e).strip() for e in active_expert_df["expert_name"].dropna().unique()
        if str(e).strip().lower() not in EXCLUDE
    ]) if "expert_name" in active_expert_df.columns else []

    cfg = load_expert_config()
    expertise_map = get_expertise_map(cfg)
    presence_map = cfg.get("presence", {}) or {}
    round_map = cfg.get("round_expertise", {}) or {}
    reallocate_absent = bool(cfg.get("reallocate_absent", False))
    expertise_source = ("owner" if str(cfg.get("task_expertise_source", "round")).lower() == "owner"
                        else "round")

    resolved = solve(sched, all_expert_names, expertise_map, presence_map,
                     round_map, expertise_source, reallocate_absent)
    resolved = resolved.reset_index(drop=True)
    resolved["row_id"] = range(len(resolved))

    if "overrides" not in st.session_state:
        st.session_state["overrides"] = {}
    overrides = st.session_state["overrides"]

    if overrides:
        moved = resolved["row_id"].map(lambda r: overrides.get(int(r)))
        mask = moved.notna()
        resolved.loc[mask, "expert_name"] = moved[mask]

    k = st.columns(4)
    k[0].metric("Interviews", len(resolved))
    k[1].metric("Experts Used", resolved["expert_name"].nunique())
    k[2].metric("Manually Moved", len(overrides))
    k[3].metric("Clash Flags", int(resolved["has_clash"].sum())
                if "has_clash" in resolved.columns else 0)

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

    if HAS_SORTABLES:
        before = dict(overrides)
        after = render_drag_board(resolved, all_expert_names, overrides)
        if after != before:
            st.session_state["overrides"] = after
            st.rerun()
    else:
        st.info("`streamlit-sortables` is not installed - using the table editor below. "
                "Install it with `pip install streamlit-sortables` for drag-and-drop.")
        before = dict(overrides)
        after = render_fallback_editor(resolved, all_expert_names, overrides)
        if after != before:
            st.session_state["overrides"] = after
            st.rerun()

    a, b = st.columns([1, 3])
    with a:
        if st.button("Reset manual moves", use_container_width=True):
            st.session_state["overrides"] = {}
            st.rerun()
    with b:
        if overrides:
            st.download_button(
                "Download manual moves (JSON)",
                data=json.dumps({str(x): y for x, y in overrides.items()}, indent=2).encode(),
                file_name="scheduler_moves_" + str(selected_date) + ".json",
                mime="application/json")


def login():
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
