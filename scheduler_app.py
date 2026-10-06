"""
Vizva Scheduler v2.1

Renders the dashboard's Schedule View EXACTLY (same code, extracted verbatim
into scheduler_core.py — including the Expert Expertise & Presence panel and
the full P1-P5 clash resolution), then adds a DRAGGABLE GANTT below it that
looks like the dashboard's Resolved Expert Schedule chart.

The draggable Gantt:
  * X axis = time (EDT), Y axis = expert lane   — same as the Plotly Gantt
  * same colours, borders, 8-item legend, shift lines and "Now" marker
  * every bar is grabbable — drag it UP or DOWN to another expert lane
  * the bar keeps its exact time: only the lane changes (vertical-only)

The drag is a Streamlit custom component (draggable_gantt/index.html) written
in vanilla JS, so no npm build is required.

Run:  streamlit run scheduler_app.py
"""
import json
import os
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from scheduler_core import (
    fetch_all_data, normalize, filter_current_year, filter_active_experts,
    render_schedule_view, build_schedule_data, load_expert_config,
    get_expertise_map, enforce_presence_first, resolve_clashes,
    enforce_gap_policy, optimize_expertise_match, apply_presence_first_labels,
    is_present, _minutes_to_label, GAP_MINUTES, SHIFT_START_MIN, SHIFT_END_MIN,
)

APP_VERSION = "SCHED v2.1 — Gantt-style draggable board"

st.set_page_config(page_title="Vizva Scheduler [v2.1]", page_icon="🗓️", layout="wide")

API_KEY = st.secrets.get("API_KEY", "")
BASE_URL = st.secrets.get("BASE_URL", "")
VIZVA_USERNAME = st.secrets.get("VIZVA_USERNAME", "")
VIZVA_PASSWORD = st.secrets.get("VIZVA_PASSWORD", "")

# ── the drag component (vanilla JS, no build step) ─────────────────
_GANTT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "draggable_gantt")
try:
    _draggable_gantt = components.declare_component("vizva_draggable_gantt", path=_GANTT_DIR)
except Exception:
    _draggable_gantt = None

# ── colours: identical to render_resolved_gantt ────────────────────
STATUS_COLORS = {"completed": "#2ecc71", "rescheduled": "#f39c12",
                 "cancelled": "#e74c3c", "pending": "#3498db"}
REASSIGNED_COLOR = "#1abc9c"
UNRESOLVED_COLOR = "#c0392b"
EXPERTISE_COLOR = "#9b59b6"
ABSENT_COLOR = "#e67e22"

LEGEND = [
    {"label": "Completed", "color": "#2ecc71", "border": "white", "width": 1},
    {"label": "Rescheduled", "color": "#f39c12", "border": "white", "width": 1},
    {"label": "Cancelled", "color": "#e74c3c", "border": "white", "width": 1},
    {"label": "Pending", "color": "#3498db", "border": "white", "width": 1},
    {"label": "Reassigned (teal/green)", "color": "#1abc9c", "border": "#27ae60", "width": 3},
    {"label": "Unresolved (no free expert)", "color": "#c0392b", "border": "#ff0000", "width": 4},
    {"label": "Expertise re-aligned (P4)", "color": "#9b59b6", "border": "#8e44ad", "width": 3},
    {"label": "Moved off Absent expert (P1 - presence)", "color": "#e67e22",
     "border": "#d35400", "width": 3},
]


def _bar_style(row):
    """(fill, border, border_width) — the same mapping render_resolved_gantt uses."""
    action = str(row.get("resolution_action", "kept") or "kept")
    if action in ("presence_reassigned", "absent_reassigned"):
        return ABSENT_COLOR, "#d35400", 3
    if action == "expertise_reassigned":
        return EXPERTISE_COLOR, "#8e44ad", 3
    if action in ("reassigned", "gap_reassigned"):
        return REASSIGNED_COLOR, "#27ae60", 3
    if action == "unresolved":
        return UNRESOLVED_COLOR, "#ff0000", 4
    return STATUS_COLORS.get(str(row.get("task_status", "")), "#95a5a6"), "white", 1


@st.cache_data(ttl=600, show_spinner="Loading interviews...")
def load_frames(pipeline_version="sched-v2.1"):
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
        fb = "This Interview is given by Candidate himself and so no support was required."
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
    """Clash / 10-minute gap / absent-expert checks on the current assignment."""
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


def _hover(row):
    action = str(row.get("resolution_action", "kept") or "kept")
    original = row.get("original_expert", row.get("expert_name", ""))
    tag = ""
    if action == "reassigned":
        tag = " | Reassigned from " + str(original)
    elif action == "gap_reassigned":
        tag = " | Gap-moved (10-min rule) from " + str(original)
    elif action == "expertise_reassigned":
        tag = " | Expertise re-aligned (P4) from " + str(original)
    elif action in ("presence_reassigned", "absent_reassigned"):
        tag = " | Moved off Absent expert " + str(original) + " (P1)"
    elif action == "unresolved":
        tag = " | UNRESOLVED - no free expert"
    return "%s | %s | %s | %s-%s | %s min%s" % (
        row.get("candidate_name", ""), row.get("company_name", ""),
        row.get("round_name", ""), row.get("start_label", ""), row.get("end_label", ""),
        row.get("duration", ""), tag)


def render_draggable_gantt(resolved, all_expert_names, selected_date, overrides):
    """The draggable Gantt, styled like the dashboard's Resolved Gantt."""
    if _draggable_gantt is None:
        st.warning("Drag component not found. Expected the folder "
                   "`draggable_gantt/` next to scheduler_app.py.")
        return None

    df = resolved.copy().reset_index(drop=True)
    df["row_id"] = range(len(df))
    df = df[~df["expert_name"].astype(str).str.strip().str.lower().eq("self")]

    # expert order: those holding interviews (by earliest start), then the rest
    experts_with = (df.groupby("expert_name")["start_min"].min()
                    .sort_values().index.tolist())
    extras = [e for e in all_expert_names
              if e not in experts_with and str(e).strip().lower() != "self"]
    expert_order = experts_with + sorted(extras)
    if not expert_order:
        st.info("No experts to show on the drag board.")
        return None

    if df.empty:
        tmin, tmax = 480.0, 1080.0
    else:
        tmin = float(max(0, int(df["start_min"].min()) - 30))
        tmax = float(min(1440, int(df["end_min"].max()) + 30))
    span = max(tmax - tmin, 1.0)

    rows = []
    for _, r in df.iterrows():
        fill, border, bwidth = _bar_style(r)
        width_pct = (float(r["end_min"]) - float(r["start_min"])) / span * 100.0
        short = ""
        if width_pct >= 6.0:
            short = str(r.get("candidate_name", ""))[:16]
        elif width_pct >= 3.5:
            short = str(r.get("candidate_name", ""))[:6]
        rows.append({
            "row_id": int(r["row_id"]),
            "expert": str(r["expert_name"]),
            "start_min": float(r["start_min"]),
            "end_min": float(r["end_min"]),
            "color": fill,
            "border": border,
            "border_width": bwidth,
            "short": short,
            "hover": _hover(r),
        })

    ticks = []
    t = int(tmin // 30 * 30)
    while t <= tmax:
        ticks.append({"min": t, "label": _minutes_to_label(t)})
        t += 30

    now_min = None
    try:
        now_edt = datetime.now(timezone(timedelta(hours=-4)))
        if selected_date == now_edt.date():
            now_min = now_edt.hour * 60 + now_edt.minute
    except Exception:
        now_min = None

    return _draggable_gantt(
        rows=rows, experts=expert_order, legend=LEGEND,
        title="🧠 Resolved Expert Schedule — " + str(selected_date) + " (EDT)",
        tmin=tmin, tmax=tmax, ticks=ticks,
        now_min=now_min, shift_start=SHIFT_START_MIN, shift_end=SHIFT_END_MIN,
        key="drag_gantt", default=None)


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
    # ══════════════════════════════════════════════════════════════
    render_schedule_view(all_case_df, active_expert_df)

    # ══════════════════════════════════════════════════════════════
    #  ADD-ON: DRAGGABLE GANTT — drag a bar up/down to another expert
    # ══════════════════════════════════════════════════════════════
    st.markdown("---")
    st.header("✋ Manual Lane Adjustment — drag a bar up or down")
    st.caption("Same chart as the Resolved Expert Schedule above, but every bar is grabbable. "
               "Drag a bar up or down into another expert's lane — the bar keeps its exact time, "
               "so only the expert changes. Bars you move get a cyan outline.")

    selected_date = st.session_state.get("schedule_date", date.today())
    sched = build_schedule_data(all_case_df, selected_date)
    if sched is None or sched.empty:
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

    # apply manual moves (lane only — time is never touched)
    if overrides:
        moved = resolved["row_id"].map(lambda r: overrides.get(int(r)))
        mask = moved.notna()
        resolved.loc[mask, "expert_name"] = moved[mask]
        resolved.loc[mask, "manual_move"] = True

    k = st.columns(4)
    k[0].metric("Interviews", len(resolved))
    k[1].metric("Experts Used", resolved["expert_name"].nunique())
    k[2].metric("Manually Moved", len(overrides))
    k[3].metric("Clash Flags", int(resolved["has_clash"].sum())
                if "has_clash" in resolved.columns else 0)

    event = render_draggable_gantt(resolved, all_expert_names, selected_date, overrides)

    if isinstance(event, dict) and event.get("row_id") is not None:
        if event.get("nonce") != st.session_state.get("drag_nonce"):
            st.session_state["drag_nonce"] = event.get("nonce")
            rid = int(event["row_id"])
            to = str(event.get("to"))
            if to == str(event.get("from")):
                st.session_state["overrides"].pop(rid, None)
            else:
                st.session_state["overrides"][rid] = to
            st.rerun()

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
