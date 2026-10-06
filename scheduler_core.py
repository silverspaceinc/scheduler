# ═══════════════════════════════════════════════════════════════════
#  scheduler_core.py — the SOLVER, extracted VERBATIM from the Vizva
#  dashboard (app.py).  Every function body is a byte-for-byte copy,
#  so the scheduler produces exactly the same solution as Schedule View.
#  Extraction source : code4_Faster.py
#  Functions         : 36 (seeds + full call-dependency closure)
# ═══════════════════════════════════════════════════════════════════
import io
import re
import json
import math
from datetime import datetime, date, timedelta
from collections import Counter

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

API_KEY = st.secrets.get("API_KEY", "") if hasattr(st, "secrets") else ""
BASE_URL = st.secrets.get("BASE_URL", "") if hasattr(st, "secrets") else ""
TASK_ORDER = ["completed", "rescheduled", "cancelled", "pending"]
TASK_LABEL = {"completed": "Completed", "rescheduled": "Rescheduled",
              "cancelled": "Cancelled", "pending": "Pending"}
CLR = {"completed": "#2ecc71", "rescheduled": "#f39c12",
       "cancelled": "#e74c3c", "pending": "#3498db"}
SHIFT_START_MIN = 210
SHIFT_END_MIN   = 750
GAP_MINUTES     = 10
EXPERT_CONFIG_FILENAME = "vizva_expert_config.json"


# ── _build_clash_groups ─────────────────────────────────────────────

def _build_clash_groups(minutes_list):
    """Given a sorted list of (index, minutes) tuples, find connected
    components where any two nodes within 30 min are connected."""
    if len(minutes_list) < 2:
        return []
    n = len(minutes_list)
    adj = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if abs(minutes_list[j][1] - minutes_list[i][1]) <= 30:
                adj[i].add(j)
                adj[j].add(i)
    visited = set()
    groups = []
    for i in range(n):
        if i in visited or not adj[i]:
            continue
        component = []
        queue = [i]
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            component.append(minutes_list[node])
            for nb in adj[node]:
                if nb not in visited:
                    queue.append(nb)
        if len(component) >= 2:
            groups.append(component)
    return groups

# ── detect_expert_clashes ─────────────────────────────────────────────

def detect_expert_clashes(df):
    """Detect scheduling clash GROUPS for experts.

    Returns two DataFrames:
      1) clash_groups_df
      2) clash_pairs_df
    """
    empty_groups = pd.DataFrame()
    empty_pairs = pd.DataFrame()

    if "_parsed_start" not in df.columns or "expert_name" not in df.columns or "date" not in df.columns:
        return empty_groups, empty_pairs

    valid = df.dropna(subset=["_parsed_start", "expert_name", "date"]).copy()
    if valid.empty:
        return empty_groups, empty_pairs

    valid["_day"] = valid["date"].dt.date
    valid["_start_minutes"] = valid["_parsed_start"].dt.hour * 60 + valid["_parsed_start"].dt.minute

    group_rows = []
    pair_rows = []

    for (expert, day), grp in valid.groupby(["expert_name", "_day"]):
        if len(grp) < 2:
            continue
        sorted_grp = grp.sort_values("_start_minutes")
        minutes_indexed = list(enumerate(zip(
            sorted_grp["_start_minutes"].values,
            sorted_grp["_parsed_start"].dt.strftime("%I:%M %p").values
        )))
        minutes_list = [(i, int(m)) for i, (m, _) in minutes_indexed]
        time_labels = {i: lbl for i, (_, lbl) in minutes_indexed}

        groups = _build_clash_groups(minutes_list)

        for group in groups:
            size = len(group)
            times = [time_labels[idx] for idx, _ in group]
            mins_vals = [m for _, m in group]
            mean_min = sum(mins_vals) / len(mins_vals)
            mean_h = int(mean_min // 60)
            mean_m = int(mean_min % 60)
            mean_label = datetime(2000, 1, 1, mean_h, mean_m).strftime("%I:%M %p")

            group_rows.append({
                "expert_name": expert,
                "date": day,
                "group_size": size,
                "interviews_str": ", ".join(times),
                "mean_start_minutes": round(mean_min, 1),
                "mean_start_label": mean_label,
                "start_times_list": times,
            })

            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    diff = abs(group[j][1] - group[i][1])
                    if diff <= 30:
                        pair_rows.append({
                            "expert_name": expert,
                            "date": day,
                            "start_time_1": time_labels[group[i][0]],
                            "start_time_2": time_labels[group[j][0]],
                            "time_diff_min": diff,
                        })

    if not group_rows:
        return empty_groups, empty_pairs

    clash_groups_df = pd.DataFrame(group_rows)
    clash_groups_df["date"] = pd.to_datetime(clash_groups_df["date"])
    clash_groups_df["month"] = clash_groups_df["date"].dt.to_period("M").astype(str)

    clash_pairs_df = pd.DataFrame(pair_rows) if pair_rows else empty_pairs
    if not clash_pairs_df.empty:
        clash_pairs_df["date"] = pd.to_datetime(clash_pairs_df["date"])
        clash_pairs_df["month"] = clash_pairs_df["date"].dt.to_period("M").astype(str)

    return clash_groups_df, clash_pairs_df

# ── is_out_of_shift ─────────────────────────────────────────────

def is_out_of_shift(start_minutes):
    """Return True if the start_minutes value falls outside the shift window."""
    if pd.isna(start_minutes):
        return None
    m = int(start_minutes)
    return m < SHIFT_START_MIN or m >= SHIFT_END_MIN

# ── add_out_of_shift_column ─────────────────────────────────────────────

def add_out_of_shift_column(df):
    """Add 'out_of_shift' boolean column to df. Requires '_parsed_start' or 'start_hour'.
    This marks ALL rows that are outside shift hours (regardless of status/expert).
    Filtering for completed+non-Self is done at the analytics/render level.
    """
    df = df.copy()
    if "_parsed_start" in df.columns:
        minutes = df["_parsed_start"].dt.hour * 60 + df["_parsed_start"].dt.minute
        df["_start_minutes_oos"] = minutes
    elif "start_hour" in df.columns:
        df["_start_minutes_oos"] = df["start_hour"] * 60
    else:
        df["out_of_shift"] = None
        return df

    df["out_of_shift"] = df["_start_minutes_oos"].apply(is_out_of_shift)
    return df

# ── _parse_time_to_minutes ─────────────────────────────────────────────

def _parse_time_to_minutes(val):
    """Convert a time string to minutes since midnight. 'noon' → 720."""
    if not isinstance(val, str) or not val.strip():
        return None
    v = val.strip()
    if v.lower() == "noon":
        return 720
    for fmt in ("%I:%M %p", "%H:%M", "%I:%M%p", "%I %p"):
        try:
            t = datetime.strptime(v, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    try:
        t = pd.to_datetime(v, errors="coerce")
        if pd.notna(t):
            return t.hour * 60 + t.minute
    except Exception:
        pass
    return None

# ── _minutes_to_label ─────────────────────────────────────────────

def _minutes_to_label(m):
    """Convert minutes since midnight to 'HH:MM AM/PM' label."""
    h = int(m // 60) % 24
    mi = int(m % 60)
    return datetime(2000, 1, 1, h, mi).strftime("%I:%M %p")

# ── build_schedule_data ─────────────────────────────────────────────

def build_schedule_data(all_data, selected_date):
    """Build interview slots DataFrame for the Gantt chart.

    Returns DataFrame with columns:
      expert_name, candidate_name, company_name, round_name, support_name,
      task_status, start_min, end_min, start_label, end_label, duration,
      has_clash (bool), is_oos (bool)
    """
    if all_data.empty or "date" not in all_data.columns:
        return pd.DataFrame()

    day_df = all_data[
        (all_data["date"].dt.date == selected_date)
        & (all_data["support_name"].str.lower() == "interview support")
    ].copy()
    if day_df.empty:
        return pd.DataFrame()

    rows = []
    for _, r in day_df.iterrows():
        expert = r.get("expert_name", "Unknown")
        candidate = r.get("candidate_name", "")
        company = r.get("company_name", "")
        round_name = r.get("round_name", "")
        support = r.get("support_name", "")
        status = r.get("task_status", "pending")

        start_raw = r.get("start_time", None)
        start_min = _parse_time_to_minutes(str(start_raw)) if pd.notna(start_raw) else None
        end_raw = r.get("end_time", None)
        end_min = _parse_time_to_minutes(str(end_raw)) if pd.notna(end_raw) else None

        if start_min is None:
            continue
        if end_min is None or end_min <= start_min:
            end_min = start_min + 30

        duration = end_min - start_min
        oos_flag = is_out_of_shift(start_min)

        rows.append({
            "expert_name": expert, "candidate_name": candidate,
            "company_name": company, "round_name": round_name,
            "support_name": support, "task_status": status,
            "start_min": start_min, "end_min": end_min,
            "start_label": _minutes_to_label(start_min),
            "end_label": _minutes_to_label(end_min),
            "duration": duration,
            "is_oos": bool(oos_flag) if oos_flag is not None else False,
        })

    if not rows:
        return pd.DataFrame()

    sched = pd.DataFrame(rows)
    sched["has_clash"] = False
    for expert, grp in sched.groupby("expert_name"):
        if len(grp) < 2:
            continue
        idxs = grp.index.tolist()
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                s1, e1 = sched.loc[idxs[a], "start_min"], sched.loc[idxs[a], "end_min"]
                s2, e2 = sched.loc[idxs[b], "start_min"], sched.loc[idxs[b], "end_min"]
                if s1 < e2 and s2 < e1:
                    sched.loc[idxs[a], "has_clash"] = True
                    sched.loc[idxs[b], "has_clash"] = True
    return sched

# ── render_schedule_gantt ─────────────────────────────────────────────

def render_schedule_gantt(sched, selected_date, all_expert_names=None):
    """Gantt timeline chart with merged hover for overlapping slots.
    OOS interviews get purple fill (#9b59b6) + orange border (#ff6600).
    Clash interviews retain status color + red border.
    Shift window drawn as dashed vertical lines (3:30 AM and 12:30 PM).
    Legend includes status colors, Clash (red border), Out of Shift
    (purple/orange). All interviews shown (including Self, all statuses);
    only OOS rows are visually flagged.
    """

    ref = datetime(2000, 1, 1)
    if not sched.empty:
        sched = sched.copy()
        sched["start_dt"] = sched["start_min"].apply(lambda m: ref + timedelta(minutes=int(m)))
        sched["end_dt"] = sched["end_min"].apply(lambda m: ref + timedelta(minutes=int(m)))
        experts_with_interviews = (sched.groupby("expert_name")["start_min"].min()
                                   .sort_values().index.tolist())
    else:
        experts_with_interviews = []

    if all_expert_names:
        experts_without = [e for e in all_expert_names if e not in experts_with_interviews]
        expert_order = experts_with_interviews + sorted(experts_without)
    else:
        expert_order = experts_with_interviews

    if not expert_order:
        st.info("No experts found for " + str(selected_date))
        return

    status_colors = {
        "completed": "#2ecc71",
        "rescheduled": "#f39c12",
        "cancelled": "#e74c3c",
        "pending": "#3498db",
    }
    OOS_COLOR = "#9b59b6"   # purple fill for OOS
    OOS_BORDER = "#ff6600"  # orange border for OOS

    fig = go.Figure()
    shift_start_dt = ref + timedelta(minutes=SHIFT_START_MIN)
    shift_end_dt = ref + timedelta(minutes=SHIFT_END_MIN)

    if not sched.empty:
        for expert in experts_with_interviews:
            expert_df = sched[sched["expert_name"] == expert].sort_values("start_min")

            clusters = []
            for _, row in expert_df.iterrows():
                row_dict = row.to_dict()
                placed = False
                for cluster in clusters:
                    for c_row in cluster:
                        if (row_dict["start_min"] < c_row["end_min"]
                                and c_row["start_min"] < row_dict["end_min"]):
                            cluster.append(row_dict)
                            placed = True
                            break
                    if placed:
                        break
                if not placed:
                    clusters.append([row_dict])

            for cluster in clusters:
                if len(cluster) == 1:
                    row = cluster[0]
                    is_oos = bool(row.get("is_oos", False))
                    has_clash = bool(row.get("has_clash", False))
                    if is_oos:
                        base_color = OOS_COLOR
                        line_dict = dict(color=OOS_BORDER, width=3)
                    elif has_clash:
                        base_color = status_colors.get(row["task_status"], "#95a5a6")
                        line_dict = dict(color="#ff0000", width=3)
                    else:
                        base_color = status_colors.get(row["task_status"], "#95a5a6")
                        line_dict = dict(color="white", width=1)

                    oos_tag = " | OOS" if is_oos else ""
                    clash_tag = " | CLASH" if has_clash else ""
                    hover = (
                        "<b>" + str(row.get("candidate_name", "") or "") + "</b><br>"
                        + "Company: " + str(row.get("company_name", "") or "") + "<br>"
                        + "Round: " + str(row.get("round_name", "") or "") + "<br>"
                        + "Status: " + str(row.get("task_status", "") or "") + "<br>"
                        + "Time: " + str(row["start_label"]) + " - " + str(row["end_label"]) + "<br>"
                        + "Duration: " + str(row["duration"]) + " min"
                        + oos_tag + clash_tag
                    )
                    fig.add_trace(go.Bar(
                        y=[expert],
                        x=[(row["end_dt"] - row["start_dt"]).total_seconds() * 1000],
                        base=[row["start_dt"]], orientation="h",
                        marker=dict(color=base_color, line=line_dict),
                        hovertext=hover, hoverinfo="text",
                        showlegend=False, width=0.6,
                    ))
                else:
                    min_start = min(r["start_min"] for r in cluster)
                    max_end = max(r["end_min"] for r in cluster)
                    start_dt = ref + timedelta(minutes=int(min_start))
                    end_dt = ref + timedelta(minutes=int(max_end))
                    any_oos = any(bool(r.get("is_oos", False)) for r in cluster)
                    hover_parts = ["<b>" + str(len(cluster)) + " overlapping</b>"]
                    if any_oos:
                        hover_parts[0] += " (OOS)"
                    hover_parts.append("<br><br>")
                    for idx, row in enumerate(cluster):
                        oos_marker = " [OOS]" if bool(row.get("is_oos", False)) else ""
                        hover_parts.append(
                            str(idx + 1) + ". "
                            + str(row.get("candidate_name", "") or "") + oos_marker + " | "
                            + str(row.get("company_name", "") or "") + " | "
                            + str(row.get("round_name", "") or "") + " | "
                            + str(row.get("task_status", "") or "") + " | "
                            + str(row["start_label"]) + "-" + str(row["end_label"]) + " ("
                            + str(row["duration"]) + " min)<br>"
                        )
                    base_color = (OOS_COLOR if any_oos
                                  else status_colors.get(cluster[0]["task_status"], "#95a5a6"))
                    merged_hover = "".join(hover_parts)
                    fig.add_trace(go.Bar(
                        y=[expert],
                        x=[(end_dt - start_dt).total_seconds() * 1000],
                        base=[start_dt], orientation="h",
                        marker=dict(color=base_color, line=dict(color="#ff0000", width=3)),
                        hovertext=merged_hover, hoverinfo="text",
                        showlegend=False, width=0.6,
                    ))
                    for row in cluster:
                        is_oos = bool(row.get("is_oos", False))
                        bar_color = (OOS_COLOR if is_oos
                                     else status_colors.get(row["task_status"], "#95a5a6"))
                        border_color = OOS_BORDER if is_oos else "#ff0000"
                        fig.add_trace(go.Bar(
                            y=[expert],
                            x=[(row["end_dt"] - row["start_dt"]).total_seconds() * 1000],
                            base=[row["start_dt"]], orientation="h",
                            marker=dict(color=bar_color,
                                        line=dict(color=border_color, width=2),
                                        opacity=0.7),
                            hovertext=merged_hover, hoverinfo="text",
                            showlegend=False, width=0.3,
                        ))

    experts_on_chart = set(experts_with_interviews)
    for expert in expert_order:
        if expert not in experts_on_chart:
            fig.add_trace(go.Bar(
                y=[expert], x=[0], base=[ref + timedelta(hours=8)],
                orientation="h", marker=dict(color="rgba(0,0,0,0)"),
                hovertext="No interviews scheduled",
                hoverinfo="text", showlegend=False, width=0.6,
            ))

    for status, color in status_colors.items():
        fig.add_trace(go.Bar(
            y=[None], x=[None],
            marker=dict(color=color),
            name=TASK_LABEL.get(status, status.title()),
            showlegend=True,
        ))
    fig.add_trace(go.Bar(
        y=[None], x=[None],
        marker=dict(color="#95a5a6", line=dict(color="#ff0000", width=3)),
        name="Clash (red border)", showlegend=True,
    ))
    fig.add_trace(go.Bar(
        y=[None], x=[None],
        marker=dict(color=OOS_COLOR, line=dict(color=OOS_BORDER, width=3)),
        name="Out of Shift (purple/orange)", showlegend=True,
    ))

    if not sched.empty:
        min_start_val = sched["start_min"].min()
        max_end_val = sched["end_min"].max()
    else:
        min_start_val = 8 * 60
        max_end_val = 18 * 60

    range_start = ref + timedelta(minutes=max(0, int(min_start_val) - 30))
    range_end = ref + timedelta(minutes=min(1440, int(max_end_val) + 30))

    fig.add_vline(x=shift_start_dt, line_dash="dash", line_color="#2ecc71",
                  opacity=0.6, annotation_text="Shift Start 3:30 AM",
                  annotation_position="top")
    fig.add_vline(x=shift_end_dt, line_dash="dash", line_color="#2ecc71",
                  opacity=0.6, annotation_text="Shift End 12:30 PM",
                  annotation_position="top")

    fig.update_layout(
        title="Expert Schedule - " + str(selected_date) + " (EDT)",
        height=max(500, len(expert_order) * 45),
        xaxis=dict(type="date", tickformat="%I:%M %p",
                   range=[range_start, range_end], title="Time (EDT)",
                   dtick=30 * 60 * 1000),
        yaxis=dict(categoryorder="array", categoryarray=expert_order[::-1], title="Expert"),
        barmode="overlay",
        legend=dict(orientation="h", y=1.08, x=0.5, xanchor="center"),
        hovermode="closest",
    )

    # -- Current-time marker line (only on today) -------------
    EDT = timezone(timedelta(hours=-4))
    now_edt = datetime.now(EDT)
    
    if selected_date == now_edt.date():
        # Build marker using the SAME reference date (2000-01-01) as the chart
        now_marker = ref.replace(
            hour=now_edt.hour,
            minute=now_edt.minute,
            second=0,
            microsecond=0,
        )
    
        fig.add_vline(
            x=now_marker,
            line_width=2,
            line_dash="dash",
            line_color="#FF00FF",
            annotation_text="Now",
            annotation_position="top",
            annotation_font_size=12,
            annotation_font_color="#FF00FF",
        )


    st.plotly_chart(fig, use_container_width=True)


# ── AVAILABILITY SUMMARY ─────────────────────────────────────

# ── render_availability_summary ─────────────────────────────────────────────

def render_availability_summary(sched, selected_date, all_expert_names=None):
    """Show expert availability — free slots between interviews."""
    st.subheader("Expert Availability — " + str(selected_date) + " (EDT)")
    st.caption("Free gaps between scheduled interviews (working hours: 8 AM – 10 PM EDT)")

    work_start = 8 * 60
    work_end = 22 * 60

    avail_rows = []

    if not sched.empty:
        for expert, grp in sched.groupby("expert_name"):
            intervals = sorted(zip(grp["start_min"], grp["end_min"]))
            merged = [intervals[0]]
            for s, e in intervals[1:]:
                if s <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))

            free_slots = []
            prev_end = work_start
            for s, e in merged:
                gap_start = max(prev_end, work_start)
                gap_end = min(s, work_end)
                if gap_end > gap_start and (gap_end - gap_start) >= 15:
                    free_slots.append(_minutes_to_label(gap_start) + " – " + _minutes_to_label(gap_end) +
                                      " (" + str(int(gap_end - gap_start)) + " min)")
                prev_end = max(prev_end, e)

            if prev_end < work_end and (work_end - prev_end) >= 15:
                free_slots.append(_minutes_to_label(prev_end) + " – " + _minutes_to_label(work_end) +
                                  " (" + str(int(work_end - prev_end)) + " min)")

            total_busy = sum(e - s for s, e in merged)
            total_interviews = len(grp)
            has_clash = grp["has_clash"].any()

            avail_rows.append({
                "Expert": expert,
                "Interviews": total_interviews,
                "Busy Time": str(int(total_busy)) + " min",
                "Clashes": "⚠️ Yes" if has_clash else "No",
                "Free Slots": " | ".join(free_slots) if free_slots else "No free slots",
            })

    if all_expert_names:
        experts_in_sched = set(sched["expert_name"].unique()) if not sched.empty else set()
        for expert in sorted(all_expert_names):
            if expert not in experts_in_sched:
                avail_rows.append({
                    "Expert": expert,
                    "Interviews": 0,
                    "Busy Time": "0 min",
                    "Clashes": "No",
                    "Free Slots": "Fully available (8:00 AM – 10:00 PM EDT)",
                })

    if not avail_rows:
        st.info("No expert data available.")
        return

    avail_df = pd.DataFrame(avail_rows).sort_values("Interviews", ascending=False)
    st.dataframe(avail_df, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════
#  INTELLIGENT CLASH RESOLUTION  (3 priorities)
#  1. Clashes: split overlapping interviews across experts.
#  2. Round preference: Technical Coding / Final Round stay with
#     their original expert wherever possible.
#  3. 10-minute gap rule: at least 10 min between an expert's interviews.
#  Self / HCR experts and interviews are never touched.
# ═══════════════════════════════════════════════════════════════════

# ── _get_expert_busy_intervals ─────────────────────────────────────────────

def _get_expert_busy_intervals(sched, expert_name):
    """Return sorted list of (start_min, end_min) merged busy intervals
    for a given expert from the schedule DataFrame."""
    if sched.empty:
        return []
    grp = sched[sched["expert_name"] == expert_name]
    if grp.empty:
        return []
    intervals = sorted(zip(grp["start_min"].values, grp["end_min"].values))
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged

# ── _is_expert_free ─────────────────────────────────────────────

def _is_expert_free(busy_intervals, start_min, end_min):
    """Return True if the expert has no overlapping busy interval
    for the window [start_min, end_min)."""
    for bs, be in busy_intervals:
        if start_min < be and bs < end_min:
            return False
    return True


# Minimum gap (minutes) every expert must have between two interviews.
# Defined here, BEFORE any function that uses it as a default argument.
GAP_MINUTES = 10

# ── is_protected_round ─────────────────────────────────────────────

def is_protected_round(round_name):
    """True if this interview round should prefer to stay with its original
    expert: Technical Coding or Final Round (case-insensitive match on the
    round_name text)."""
    if round_name is None:
        return False
    try:
        r = str(round_name).strip().lower()
    except Exception:
        return False
    return "coding" in r or "final" in r

# ── _pick_interviews_to_keep ─────────────────────────────────────────────

def _pick_interviews_to_keep(resolved, group_idxs, presence_map=None):
    """Greedily pick the interviews of a clash group that stay where they are.

    Claim order (first claim wins, then the classic end-time greedy keeps the
    maximum number of non-overlapping interviews):
        1. PRIORITY 1 - the owner is PRESENT.
        2. Protected round (Technical Coding / Final Round).
    A protected round owned by an ABSENT expert therefore loses its claim and
    is offered to another expert, exactly like any other interview.
    """
    def _owner_of(idx):
        if "original_expert" in resolved.columns:
            return resolved.loc[idx, "original_expert"]
        return resolved.loc[idx, "expert_name"]

    def _claim_key(idx):
        absent = 0 if is_present(_owner_of(idx), presence_map or {}) else 1
        protected = 0 if is_protected_round(resolved.loc[idx, "round_name"]) else 1
        return (absent, protected,
                resolved.loc[idx, "end_min"], resolved.loc[idx, "start_min"])

    keep = []

    def _try_keep(idx):
        s = resolved.loc[idx, "start_min"]
        e = resolved.loc[idx, "end_min"]
        for k in keep:
            ks = resolved.loc[k, "start_min"]
            ke = resolved.loc[k, "end_min"]
            if s < ke and ks < e:
                return False
        keep.append(idx)
        return True

    for idx in sorted(group_idxs, key=_claim_key):
        _try_keep(idx)
    return keep

# ── recompute_clash_flags ─────────────────────────────────────────────

def recompute_clash_flags(df):
    """Recompute has_clash for the CURRENT expert assignment (strict overlap)."""
    out = df.copy()
    out["has_clash"] = False
    for exp, grp in out.groupby("expert_name"):
        if len(grp) < 2:
            continue
        gi = grp.index.tolist()
        for a in range(len(gi)):
            for b in range(a + 1, len(gi)):
                s1 = out.loc[gi[a], "start_min"]
                e1 = out.loc[gi[a], "end_min"]
                s2 = out.loc[gi[b], "start_min"]
                e2 = out.loc[gi[b], "end_min"]
                if s1 < e2 and s2 < e1:
                    out.loc[gi[a], "has_clash"] = True
                    out.loc[gi[b], "has_clash"] = True
    return out

# ── enforce_presence_first ─────────────────────────────────────────────

def enforce_presence_first(resolved, all_expert_names, expertise_map=None,
                           round_map=None, presence_map=None,
                           expertise_source="round", min_gap=GAP_MINUTES,
                           enabled=True):
    """PRIORITY 1 - PRESENCE FIRST.  Run BEFORE every other pass.

    A task may never stay with an ABSENT expert while a PRESENT expert can
    take it.  Protected rounds (Technical Coding / Final Round) are NOT
    exempt: they are offered to another expert with the SAME expertise
    first and then through the configured fallback cycle
    (Data/Business -> Software -> DevOps, and so on).

    If no Present expert is free, the task stays where it is and is marked
    `presence_violation` so it is visible instead of silently kept.
    Self / HCR are never touched.  Adds:
        presence_moved     - True when the task was moved off an absent expert
        presence_violation - True when it still sits with an absent expert
    """
    resolved = resolved.copy()
    resolved["presence_moved"] = False
    resolved["presence_violation"] = False
    if "original_expert" not in resolved.columns:
        resolved["original_expert"] = resolved["expert_name"]
    if "resolution_action" not in resolved.columns:
        resolved["resolution_action"] = "kept"
    resolved = recompute_clash_flags(resolved)

    presence_map = presence_map or {}
    pool = eligible_experts(all_expert_names, presence_map)

    if enabled and pool:
        for idx in list(resolved.index):
            current = resolved.loc[idx, "expert_name"]
            if str(current).strip().lower() in ("hcr", "self"):
                continue
            if is_present(current, presence_map):
                continue

            task_exp = task_expertise_for_row(resolved, idx, expertise_map or {},
                                              round_map, expertise_source)
            ranked = (rank_candidate_experts(pool, task_exp, expertise_map or {})
                      if task_exp else list(pool))
            s = resolved.loc[idx, "start_min"]
            e = resolved.loc[idx, "end_min"]
            for alt in ranked:
                if alt == current:
                    continue
                busy = _get_expert_busy_intervals(resolved, alt)
                if _is_expert_free_with_gap(busy, s, e, min_gap):
                    resolved.loc[idx, "expert_name"] = alt
                    resolved.loc[idx, "presence_moved"] = True
                    break

        resolved = recompute_clash_flags(resolved)

    for idx in resolved.index:
        current = resolved.loc[idx, "expert_name"]
        if str(current).strip().lower() in ("hcr", "self"):
            continue
        if not is_present(current, presence_map):
            resolved.loc[idx, "presence_violation"] = True
    return resolved

# ── apply_presence_first_labels ─────────────────────────────────────────────

def apply_presence_first_labels(resolved, presence_map=None):
    """Label every task that was carried off an Absent expert (Priority 1)."""
    df = resolved.copy()
    if "presence_moved" not in df.columns:
        return df
    if "resolution_action" not in df.columns:
        df["resolution_action"] = "kept"
    moved = df["presence_moved"].fillna(False).astype(bool)
    if "original_expert" in df.columns:
        moved = moved & (df["expert_name"].astype(str) != df["original_expert"].astype(str))
    df.loc[moved, "resolution_action"] = "presence_reassigned"
    return df

# ── resolve_clashes ─────────────────────────────────────────────

def resolve_clashes(sched, all_expert_names, expertise_map=None, presence_map=None,
                    round_map=None, expertise_source=None):
    """Intelligently resolve clashing interviews.

    Priorities:
      1. "Self" / "HCR" experts and their interviews are NEVER touched
         (never moved, never marked, never used as targets).
      2. Protected rounds (Technical Coding / Final Round) get first
         claim to stay with their original expert.
      3. PRIORITY 1 - only experts marked PRESENT may receive a task.
      4. PRIORITY 5 - candidates are tried in expertise-fit order for the
         task (same expertise first, then the fallback chain), so an offer
         goes to the best-suited free expert.
      5. Remaining clashing interviews are offered - in order - to each
         eligible expert who is FREE during that window.
      6. If no expert is free, mark the interview as "unresolved".

    Returns
    -------
    resolved : pd.DataFrame
        Copy of *sched* with two extra columns:
        - ``original_expert``  : the expert before resolution
        - ``resolution_action``: one of
            "kept"       - no clash, stays as-is
            "retained"   - kept with the original expert
            "reassigned" - moved to a different expert
            "unresolved" - no free expert found (highlighted)
    """
    resolved = sched.copy()
    if "original_expert" not in resolved.columns:
        resolved["original_expert"] = resolved["expert_name"]
    if "resolution_action" not in resolved.columns:
        resolved["resolution_action"] = "kept"

    # Priority 1 may have re-seated tasks - rebuild the clash picture first
    resolved = recompute_clash_flags(resolved)

    # Experts that can accept interviews (exclude HCR, Self; PRIORITY 1 = Present only)
    EXCLUDE = {"hcr", "self"}
    candidate_experts = eligible_experts(all_expert_names, presence_map)

    # Only real experts are processed - Self / HCR interviews stay untouched
    clash_experts = [
        e for e in resolved.loc[resolved["has_clash"], "expert_name"].unique()
        if str(e).strip().lower() not in EXCLUDE
    ]

    for expert in clash_experts:
        expert_mask = resolved["expert_name"] == expert
        expert_rows = resolved.loc[expert_mask].sort_values("start_min")

        if len(expert_rows) < 2:
            continue

        # Build overlap groups for this expert (connected components)
        idxs = expert_rows.index.tolist()
        visited = set()
        overlap_groups = []

        for i, idx_a in enumerate(idxs):
            if idx_a in visited:
                continue
            group = [idx_a]
            visited.add(idx_a)
            queue = [idx_a]
            while queue:
                cur = queue.pop(0)
                s1 = resolved.loc[cur, "start_min"]
                e1 = resolved.loc[cur, "end_min"]
                for idx_b in idxs:
                    if idx_b in visited:
                        continue
                    s2 = resolved.loc[idx_b, "start_min"]
                    e2 = resolved.loc[idx_b, "end_min"]
                    if s1 < e2 and s2 < e1:
                        group.append(idx_b)
                        visited.add(idx_b)
                        queue.append(idx_b)
            if len(group) >= 2:
                overlap_groups.append(group)

        for group_idxs in overlap_groups:
            # Protected rounds (Technical Coding / Final Round) stay first
            keep_idxs = _pick_interviews_to_keep(resolved, group_idxs, presence_map)
            for idx in keep_idxs:
                if resolved.loc[idx, "resolution_action"] == "kept":
                    resolved.loc[idx, "resolution_action"] = "retained"

            # Every interview that could not stay is offered to other experts
            movers = sorted(
                (i for i in group_idxs if i not in keep_idxs),
                key=lambda i: resolved.loc[i, "start_min"],
            )
            for idx in movers:
                s = resolved.loc[idx, "start_min"]
                e = resolved.loc[idx, "end_min"]

                assigned = False
                task_exp = task_expertise_for_row(resolved, idx, expertise_map or {}, round_map,
                                                  expertise_source or "round")
                for alt_expert in rank_candidate_experts(candidate_experts, task_exp,
                                                         expertise_map or {}):
                    if alt_expert == expert:
                        continue
                    # Build current busy intervals for alt_expert
                    # (use the *resolved* DataFrame so earlier reassignments
                    #  are taken into account)
                    busy = _get_expert_busy_intervals(resolved, alt_expert)
                    if _is_expert_free(busy, s, e):
                        resolved.loc[idx, "expert_name"] = alt_expert
                        resolved.loc[idx, "resolution_action"] = "reassigned"
                        assigned = True
                        break

                if not assigned:
                    resolved.loc[idx, "resolution_action"] = "unresolved"

    # Re-compute has_clash on the resolved schedule
    resolved["has_clash"] = False
    for exp, grp in resolved.groupby("expert_name"):
        if len(grp) < 2:
            continue
        grp_idxs = grp.index.tolist()
        for a in range(len(grp_idxs)):
            for b in range(a + 1, len(grp_idxs)):
                s1 = resolved.loc[grp_idxs[a], "start_min"]
                e1 = resolved.loc[grp_idxs[a], "end_min"]
                s2 = resolved.loc[grp_idxs[b], "start_min"]
                e2 = resolved.loc[grp_idxs[b], "end_min"]
                if s1 < e2 and s2 < e1:
                    resolved.loc[grp_idxs[a], "has_clash"] = True
                    resolved.loc[grp_idxs[b], "has_clash"] = True

    return resolved



# ═════════════════════════════════════════════════════════════════
#  10-MINUTE GAP POLICY  (3rd priority)
#  Every expert must have at least GAP_MINUTES between two interviews.
#  Where possible, gap-violating interviews are moved to another
#  expert that has room WITH the buffer. If all experts are busy,
#  the interview stays where it is (marked gap_violation=True).
# ═════════════════════════════════════════════════════════════════

# ── _is_expert_free_with_gap ─────────────────────────────────────────────

def _is_expert_free_with_gap(busy_intervals, start_min, end_min, min_gap=GAP_MINUTES):
    """True if [start_min, end_min) fits on the expert with a min_gap-minute
    buffer on BOTH sides (no interview ending <10 min before start,
    no interview starting <10 min after end)."""
    for bs, be in busy_intervals:
        if start_min < be + min_gap and bs < end_min + min_gap:
            return False
    return True

# ── _try_relocate_with_gap ─────────────────────────────────────────────

def _try_relocate_with_gap(resolved, idx, current_expert, candidate_experts, min_gap):
    """Try to move the interview at `idx` to another expert that can host it
    while respecting the min_gap buffer on both sides. Returns True if moved."""
    s = resolved.loc[idx, "start_min"]
    e = resolved.loc[idx, "end_min"]
    for alt in candidate_experts:
        if alt == current_expert:
            continue
        busy = _get_expert_busy_intervals(resolved, alt)
        if _is_expert_free_with_gap(busy, s, e, min_gap):
            resolved.loc[idx, "expert_name"] = alt
            resolved.loc[idx, "resolution_action"] = "gap_reassigned"
            resolved.loc[idx, "gap_violation"] = False
            return True
    return False

# ── enforce_gap_policy ─────────────────────────────────────────────

def enforce_gap_policy(resolved, all_expert_names, min_gap=GAP_MINUTES,
                       expertise_map=None, presence_map=None, round_map=None,
                       expertise_source=None):
    """THIRD-PRIORITY RULE - call AFTER resolve_clashes().

    For each expert, every pair of consecutive interviews must be at least
    `min_gap` minutes apart. If a pair is too close (or overlapping):
      1. try to move the NON-protected interview of the pair first, so
         Technical Coding / Final Round prefer to stay with their expert;
      2. try to move the other interview of the pair to another expert
         that has room WITH the buffer respected;
      3. if every expert is busy, leave it as-is (no issue, just marked).
    Iterates until the schedule is stable. Returns the updated DataFrame
    with an extra column `gap_violation` (True = still violates the
    10-minute rule because no expert was free). Moved interviews get
    resolution_action = "gap_reassigned".
    """
    resolved = resolved.copy()
    resolved["gap_violation"] = False

    candidate_experts = eligible_experts(all_expert_names, presence_map)

    for _ in range(max(2, len(resolved))):
        changed = False
        for expert in candidate_experts:
            mask = resolved["expert_name"] == expert
            idxs = resolved.loc[mask].sort_values("start_min").index.tolist()
            for a in range(len(idxs) - 1):
                i1, i2 = idxs[a], idxs[a + 1]
                e1 = resolved.loc[i1, "end_min"]
                s2 = resolved.loc[i2, "start_min"]
                if s2 - e1 >= min_gap:
                    continue  # already fine
                # Try to move the non-protected interview first so protected
                # rounds (Technical Coding / Final Round) prefer to stay
                # with their original expert.
                p1 = is_protected_round(resolved.loc[i1, "round_name"])
                p2 = is_protected_round(resolved.loc[i2, "round_name"])
                if p1 == p2:
                    order = [i2, i1]
                elif p2:
                    order = [i1, i2]
                else:
                    order = [i2, i1]
                moved = False
                for idx in order:
                    task_exp = task_expertise_for_row(resolved, idx, expertise_map or {}, round_map,
                                                  expertise_source or "round")
                    ranked = rank_candidate_experts(candidate_experts, task_exp,
                                                    expertise_map or {})
                    if _try_relocate_with_gap(resolved, idx, expert, ranked, min_gap):
                        moved = True
                        break
                if moved:
                    changed = True
                    continue
                resolved.loc[i1, "gap_violation"] = True
                resolved.loc[i2, "gap_violation"] = True
        if not changed:
            break

    # Final clean recompute of gap_violation
    resolved["gap_violation"] = False
    for expert in candidate_experts:
        mask = resolved["expert_name"] == expert
        idxs = resolved.loc[mask].sort_values("start_min").index.tolist()
        for a in range(len(idxs) - 1):
            i1, i2 = idxs[a], idxs[a + 1]
            if (resolved.loc[i2, "start_min"] - resolved.loc[i1, "end_min"]) < min_gap:
                resolved.loc[i1, "gap_violation"] = True
                resolved.loc[i2, "gap_violation"] = True
    return resolved

# ── render_resolved_gantt ─────────────────────────────────────────────

def render_resolved_gantt(resolved, selected_date, all_expert_names=None):
    """Gantt chart for the resolved schedule.

    "Self" interviews are untouched and never shown here.
    Colour scheme by resolution_action:
      kept / retained → original status colour
      reassigned      → teal (#1abc9c) + dashed green border
      unresolved      → dark red (#c0392b) + thick red border
    """

    ref = datetime(2000, 1, 1)
    resolved = resolved.copy()
    resolved["start_dt"] = resolved["start_min"].apply(
        lambda m: ref + timedelta(minutes=int(m)))
    resolved["end_dt"] = resolved["end_min"].apply(
        lambda m: ref + timedelta(minutes=int(m)))

    # Self interviews are untouched - never shown in the resolved Gantt
    resolved = resolved[
        ~resolved["expert_name"].astype(str).str.strip().str.lower().eq("self")
    ]

    experts_with = (resolved.groupby("expert_name")["start_min"]
                    .min().sort_values().index.tolist())

    if all_expert_names:
        extras = [e for e in all_expert_names
                  if e not in experts_with
                  and str(e).strip().lower() != "self"]
        expert_order = experts_with + sorted(extras)
    else:
        expert_order = experts_with

    if not expert_order:
        st.info("No experts found for resolved view.")
        return

    status_colors = {
        "completed": "#2ecc71", "rescheduled": "#f39c12",
        "cancelled": "#e74c3c", "pending": "#3498db",
    }
    REASSIGNED_COLOR = "#1abc9c"
    UNRESOLVED_COLOR = "#c0392b"
    EXPERTISE_COLOR = "#9b59b6"
    ABSENT_COLOR = "#e67e22"

    fig = go.Figure()

    for expert in experts_with:
        expert_df = resolved[resolved["expert_name"] == expert].sort_values("start_min")
        for _, row in expert_df.iterrows():
            action = row.get("resolution_action", "kept")
            original = row.get("original_expert", expert)

            if action in ("presence_reassigned", "absent_reassigned"):
                bar_color = ABSENT_COLOR
                line_dict = dict(color="#d35400", width=3)
            elif action == "expertise_reassigned":
                bar_color = EXPERTISE_COLOR
                line_dict = dict(color="#8e44ad", width=3)
            elif action == "absent_reassigned":
                bar_color = ABSENT_COLOR
                line_dict = dict(color="#d35400", width=3)
            elif action in ("reassigned", "gap_reassigned"):
                bar_color = REASSIGNED_COLOR
                line_dict = dict(color="#27ae60", width=3)
            elif action == "unresolved":
                bar_color = UNRESOLVED_COLOR
                line_dict = dict(color="#ff0000", width=4)
            else:
                bar_color = status_colors.get(row["task_status"], "#95a5a6")
                line_dict = dict(color="white", width=1)

            reassign_tag = ""
            if action == "reassigned":
                reassign_tag = "<br><b>↪ Reassigned from " + str(original) + "</b>"
            elif action == "gap_reassigned":
                reassign_tag = "<br><b>🕐 Gap-moved (10-min rule) from " + str(original) + "</b>"
            elif action == "expertise_reassigned":
                reassign_tag = ("<br><b>🎯 Expertise re-aligned (P4) from "
                                + str(original) + "</b>")
            elif action == "presence_reassigned":
                reassign_tag = ("<br><b>🚫 Moved off ABSENT expert " + str(original)
                                + " (P1 - presence)</b>")
            elif action == "absent_reassigned":
                reassign_tag = ("<br><b>🚫 Moved off Absent expert " + str(original)
                                + " (P1 - presence)</b>")
            elif action == "unresolved":
                reassign_tag = "<br><b>⚠️ UNRESOLVED — no free expert</b>"

            hover = (
                "<b>" + str(row.get("candidate_name", "") or "") + "</b><br>"
                + "Company: " + str(row.get("company_name", "") or "") + "<br>"
                + "Round: " + str(row.get("round_name", "") or "") + "<br>"
                + "Status: " + str(row.get("task_status", "") or "") + "<br>"
                + "Time: " + str(row["start_label"]) + " - " + str(row["end_label"]) + "<br>"
                + "Duration: " + str(row["duration"]) + " min<br>"
                + "Original Expert: " + str(original)
                + reassign_tag
            )

            fig.add_trace(go.Bar(
                y=[expert],
                x=[(row["end_dt"] - row["start_dt"]).total_seconds() * 1000],
                base=[row["start_dt"]], orientation="h",
                marker=dict(color=bar_color, line=line_dict),
                hovertext=hover, hoverinfo="text",
                showlegend=False, width=0.6,
            ))

    # Placeholder bars for experts with no interviews
    for expert in expert_order:
        if expert not in set(experts_with):
            fig.add_trace(go.Bar(
                y=[expert], x=[0],
                base=[ref + timedelta(hours=8)],
                orientation="h",
                marker=dict(color="rgba(0,0,0,0)"),
                hovertext="No interviews scheduled",
                hoverinfo="text", showlegend=False, width=0.6,
            ))

    # Legend entries
    for status, color in status_colors.items():
        fig.add_trace(go.Bar(y=[None], x=[None], marker=dict(color=color),
                             name=TASK_LABEL.get(status, status.title()), showlegend=True))
    fig.add_trace(go.Bar(y=[None], x=[None],
                         marker=dict(color=REASSIGNED_COLOR, line=dict(color="#27ae60", width=3)),
                         name="Reassigned (teal/green)", showlegend=True))
    fig.add_trace(go.Bar(y=[None], x=[None],
                         marker=dict(color=UNRESOLVED_COLOR, line=dict(color="#ff0000", width=4)),
                         name="Unresolved (no free expert)", showlegend=True))
    fig.add_trace(go.Bar(y=[None], x=[None],
                         marker=dict(color=EXPERTISE_COLOR, line=dict(color="#8e44ad", width=3)),
                         name="Expertise re-aligned (P4)", showlegend=True))
    fig.add_trace(go.Bar(y=[None], x=[None],
                         marker=dict(color=ABSENT_COLOR, line=dict(color="#d35400", width=3)),
                         name="Moved off Absent expert (P1 - presence)", showlegend=True))

    if not resolved.empty:
        min_s = resolved["start_min"].min()
        max_e = resolved["end_min"].max()
    else:
        min_s, max_e = 480, 1080

    range_start = ref + timedelta(minutes=max(0, int(min_s) - 30))
    range_end = ref + timedelta(minutes=min(1440, int(max_e) + 30))

    # Shift window lines
    fig.add_vline(x=ref + timedelta(minutes=SHIFT_START_MIN),
                  line_dash="dash", line_color="#2ecc71", opacity=0.6,
                  annotation_text="Shift Start 3:30 AM", annotation_position="top")
    fig.add_vline(x=ref + timedelta(minutes=SHIFT_END_MIN),
                  line_dash="dash", line_color="#2ecc71", opacity=0.6,
                  annotation_text="Shift End 12:30 PM", annotation_position="top")

    fig.update_layout(
        title="🧠 Resolved Expert Schedule — " + str(selected_date) + " (EDT)",
        height=max(500, len(expert_order) * 45),
        xaxis=dict(type="date", tickformat="%I:%M %p",
                   range=[range_start, range_end], title="Time (EDT)",
                   dtick=30 * 60 * 1000),
        yaxis=dict(categoryorder="array", categoryarray=expert_order[::-1],
                   title="Expert"),
        barmode="overlay",
        legend=dict(orientation="h", y=1.08, x=0.5, xanchor="center"),
        hovermode="closest",
    )

    # Current time marker
    EDT = timezone(timedelta(hours=-4))
    now_edt = datetime.now(EDT)
    if selected_date == now_edt.date():
        now_marker = ref.replace(hour=now_edt.hour, minute=now_edt.minute,
                                  second=0, microsecond=0)
        fig.add_vline(x=now_marker, line_width=2, line_dash="dash",
                      line_color="#FF00FF", annotation_text="Now",
                      annotation_position="top",
                      annotation_font_size=12, annotation_font_color="#FF00FF")

    st.plotly_chart(fig, use_container_width=True)

# ── render_resolution_summary ─────────────────────────────────────────────

def render_resolution_summary(resolved, selected_date):
    """Show KPIs and tables summarizing what the resolver did."""
    # Self interviews are untouched - exclude them from resolution stats
    resolved = resolved[
        ~resolved["expert_name"].astype(str).str.strip().str.lower().eq("self")
    ]
    total = len(resolved)
    kept = int((resolved["resolution_action"] == "kept").sum())
    retained = int((resolved["resolution_action"] == "retained").sum())
    reassigned = int((resolved["resolution_action"] == "reassigned").sum())
    unresolved = int((resolved["resolution_action"] == "unresolved").sum())
    gap_moved = int((resolved["resolution_action"] == "gap_reassigned").sum())
    expertise_moved = int((resolved["resolution_action"] == "expertise_reassigned").sum())
    absent_moved = int((resolved["resolution_action"] == "absent_reassigned").sum())
    off_expertise = int(resolved["expertise_violation"].sum()) if "expertise_violation" in resolved.columns else 0
    presence_moved = int((resolved["resolution_action"] == "presence_reassigned").sum())
    still_absent = int(resolved["presence_violation"].sum()) if "presence_violation" in resolved.columns else 0

    k = st.columns(6)
    k[0].metric("Total Interviews", total)
    k[1].metric("No Change", kept)
    k[2].metric("Retained (original expert)", retained)
    k[3].metric("✅ Reassigned", reassigned)
    k[4].metric("🕐 Gap-Moved (10-min)", gap_moved)
    k[5].metric("❌ Unresolved", unresolved)

    k2 = st.columns(5)
    k2[0].metric("🚫 Moved off Absent expert (P1)", presence_moved + absent_moved,
                 help="Tasks that were sitting with an Absent expert and were given to a "
                      "Present expert - Technical/Final rounds included. Same expertise is "
                      "tried first, then the configured fallback cycle.")
    k2[1].metric("⚠️ Still with Absent expert", still_absent,
                 help="Nobody Present was free for these, so they stayed put. They will be "
                      "picked up as soon as someone Present is free.")
    k2[2].metric("🎯 Expertise Re-aligned (P5)", expertise_moved)
    k2[3].metric("⚠️ Still Off-Expertise", off_expertise,
                 help="Task stayed on a fallback/expertise-mismatched expert because "
                      "no better-suited Present expert was free.")
    k2[4].metric("✅ Fully Matched", max(0, total - off_expertise - still_absent),
                 help="Tasks on a Present expert of the required expertise.")

    # ── Reassignment details ─────────────────────────────────────
    reassigned_df = resolved[resolved["resolution_action"].isin(
        ["reassigned", "gap_reassigned", "expertise_reassigned", "absent_reassigned",
         "presence_reassigned"])]
    if not reassigned_df.empty:
        st.markdown("##### ✅ Reassigned Interviews")
        ra_display = reassigned_df[[
            "candidate_name", "company_name", "round_name", "task_status",
            "start_label", "end_label", "original_expert", "expert_name",
        ]].copy()
        ra_display.columns = [
            "Candidate", "Company", "Round", "Status",
            "Start", "End", "From Expert", "To Expert",
        ]
        st.dataframe(ra_display.sort_values("Start"),
                     use_container_width=True, hide_index=True)

    # ── Unresolved details ───────────────────────────────────────
    unresolved_df = resolved[resolved["resolution_action"] == "unresolved"]
    if not unresolved_df.empty:
        st.markdown("##### ❌ Unresolved — No Free Expert Available")
        st.caption(
            "These interviews could not be reassigned because all experts "
            "are busy during the same time window. Consider adding capacity "
            "or rescheduling."
        )
        ur_display = unresolved_df[[
            "candidate_name", "company_name", "round_name", "task_status",
            "start_label", "end_label", "original_expert",
        ]].copy()
        ur_display.columns = [
            "Candidate", "Company", "Round", "Status",
            "Start", "End", "Original Expert",
        ]

        # Style unresolved rows with red background
        def _highlight_unresolved(row):
            return ["background-color: #ffcccc"] * len(row)

        st.dataframe(
            ur_display.sort_values("Start").style.apply(_highlight_unresolved, axis=1),
            use_container_width=True, hide_index=True,
        )

    # ── Before / After clash comparison ──────────────────────────
    remaining_clashes = int(resolved["has_clash"].sum())
    original_clashes = int((resolved["resolution_action"].isin(
        ["retained", "reassigned", "unresolved"])).sum())

    ba_cols = st.columns(3)
    ba_cols[0].metric("Original Clashing Interviews", original_clashes)
    ba_cols[1].metric("Remaining Clashes After Resolution", remaining_clashes)
    ba_cols[2].metric("Clashes Resolved",
                      max(0, original_clashes - remaining_clashes - reassigned))


# ═══════════════════════════════════════════════════════════════════
#  EXPERT CONFIGURATION — PRIORITY 4 (EXPERTISE) & PRIORITY 5 (PRESENCE)
#
#  The database does not store what kind of engineer each expert is, nor
#  whether they are present today.  This block keeps that information in
#  a small JSON file next to the app, editable from the Schedule View, so
#  it survives reruns.
#
#  PRIORITY 4 — EXPERTISE ROUTING
#    Data/Business Analyst task  -> Data/Business -> Software Eng. -> DevOps
#    Software Engineer task      -> Software Eng. -> DevOps        -> Data/Business
#    DevOps/Cyber task           -> DevOps/Cyber  -> Software Eng. -> Data/Business
#  PRIORITY 5 — PRESENCE
#    Only experts marked "Present" can RECEIVE a task.  Existing tasks of
#    an Absent expert are reported; they are only moved when the
#    "reallocate" option is switched on.
#
#  "Self" and "HCR" are never touched and never used as targets.
# ═══════════════════════════════════════════════════════════════════

EXPERT_CONFIG_FILENAME = "vizva_expert_config.json"

EXPERTISE_CATEGORIES = [
    "Data/Business Analyst",
    "Software Engineer",
    "DevOps/Cyber Security",
    "Unspecified",
]
PRESENCE_OPTIONS = ["Present", "Absent"]

# Preferred order of experts for each task expertise.
EXPERTISE_FALLBACK = {
    "Data/Business Analyst": ["Data/Business Analyst", "Software Engineer", "DevOps/Cyber Security"],
    "Software Engineer": ["Software Engineer", "DevOps/Cyber Security", "Data/Business Analyst"],
    "DevOps/Cyber Security": ["DevOps/Cyber Security", "Software Engineer", "Data/Business Analyst"],
}

# Keyword rules used to guess the expertise a ROUND requires.  Only used
# when the owning expert has no expertise set.  Order matters: the first
# matching category wins.
ROUND_EXPERTISE_KEYWORDS = [
    ("Data/Business Analyst", ["sql", "data", "analytic", "business", "case study",
                               "case", "excel", "tableau", "power bi", "statistic",
                               "product", "guesstimate", "market"]),
    ("DevOps/Cyber Security", ["devops", "dev ops", "cloud", "aws", "azure", "gcp",
                               "kubernetes", "k8s", "docker", "terraform", "cyber",
                               "security", "penetration", "soc", "network", "linux",
                               "infra"]),
    ("Software Engineer", ["coding", "technical", "dsa", "algorithm", "software",
                           "developer", "programming", "system design", "backend",
                           "frontend", "full stack", "oops", "data structure"]),
]

# ── _expert_config_path ─────────────────────────────────────────────

def _expert_config_path():
    """Absolute path of the JSON file that stores expertise + presence."""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        base = os.getcwd()
    if not base:
        base = os.getcwd()
    return os.path.join(base, EXPERT_CONFIG_FILENAME)

# ── _empty_expert_config ─────────────────────────────────────────────

def _empty_expert_config():
    return {"expertise": {}, "presence": {}, "round_expertise": {},
            "presence_first": True, "reallocate_absent": True,
            "task_expertise_source": "round", "updated_at": None}

# ── load_expert_config ─────────────────────────────────────────────

def load_expert_config():
    """Read the saved expert configuration (never raises)."""
    cfg = _empty_expert_config()
    try:
        path = _expert_config_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key in ("expertise", "presence", "round_expertise"):
                    val = loaded.get(key)
                    if isinstance(val, dict):
                        cfg[key] = {str(k): str(v) for k, v in val.items()}
                cfg["reallocate_absent"] = bool(loaded.get("reallocate_absent", True))
                # PRIORITY 1 defaults to ON for EVERYONE, including config files
                # written by an older revision (those only carry the legacy
                # "reallocate_absent" key, usually False). Presence-first is the
                # new intended behaviour; switch it off in the panel if needed.
                cfg["presence_first"] = bool(loaded.get("presence_first", True))
                cfg["task_expertise_source"] = (
                    "owner" if str(loaded.get("task_expertise_source", "round")).lower() == "owner"
                    else "round")
                cfg["updated_at"] = loaded.get("updated_at")
    except Exception:
        pass
    return cfg

# ── save_expert_config ─────────────────────────────────────────────

def save_expert_config(cfg):
    """Write the expert configuration atomically. Returns (ok, info)."""
    try:
        path = _expert_config_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, default=str)
        os.replace(tmp, path)
        return True, path
    except Exception as exc:
        return False, str(exc)

# ── get_expertise_map ─────────────────────────────────────────────

def get_expertise_map(cfg):
    """expert name -> expertise category (only explicit choices)."""
    raw = (cfg or {}).get("expertise") or {}
    return {str(k): str(v) for k, v in raw.items() if str(v) in EXPERTISE_FALLBACK}

# ── _has_any_expertise ─────────────────────────────────────────────

def _has_any_expertise(expertise_map):
    """Priority 4 only engages once at least one expert is classified."""
    if not expertise_map:
        return False
    return any(str(v) in EXPERTISE_FALLBACK for v in expertise_map.values())

# ── is_present ─────────────────────────────────────────────

def is_present(expert_name, presence_map):
    """Absent only when explicitly marked Absent. Unknown = Present."""
    if not presence_map:
        return True
    return str(presence_map.get(expert_name, "Present")).strip().lower() != "absent"

# ── eligible_experts ─────────────────────────────────────────────

def eligible_experts(all_expert_names, presence_map):
    """PRIORITY 5 — the experts that may RECEIVE a task (Self/HCR excluded)."""
    EXCLUDE = {"hcr", "self"}
    return [e for e in all_expert_names
            if str(e).strip().lower() not in EXCLUDE
            and is_present(e, presence_map)]

# ── infer_round_expertise ─────────────────────────────────────────────

def infer_round_expertise(round_name, round_map=None):
    """Which expertise a round needs, from the user's override then keywords."""
    if round_name is None:
        return None
    r = str(round_name).strip()
    if not r or r.lower() in ("nan", "none"):
        return None
    if round_map:
        if r in round_map and str(round_map[r]) in EXPERTISE_FALLBACK:
            return str(round_map[r])
        low = {str(k).strip().lower(): str(v) for k, v in round_map.items()}
        hit = low.get(r.lower())
        if hit and hit in EXPERTISE_FALLBACK:
            return hit
    rl = r.lower()
    for category, keywords in ROUND_EXPERTISE_KEYWORDS:
        for kw in keywords:
            if kw in rl:
                return category
    return None

# ── task_expertise_for_row ─────────────────────────────────────────────

def task_expertise_for_row(resolved, idx, expertise_map, round_map=None, source="round"):
    """The expertise a task requires.

    source="round" (default) - the ROUND decides first (SQL Round -> Data,
      Coding/Technical Round -> Software, DevOps -> DevOps).  This is what
      lets the resolver FIX a mis-assignment: a data round sitting on a
      software engineer is still a Data task and moves to a Data analyst.
    source="owner" - the expertise of the expert who owns the task decides
      first and the round is only a fallback.

    In both modes the other signal is the fallback, so a generic round
    (Final Round / HR Round) simply inherits the owning expert's profile.
    """
    rnd = resolved.loc[idx, "round_name"] if "round_name" in resolved.columns else None
    owner = None
    if "original_expert" in resolved.columns:
        owner = resolved.loc[idx, "original_expert"]
    if owner is None or str(owner).strip() in ("", "nan", "None"):
        owner = (resolved.loc[idx, "expert_name"]
                 if "expert_name" in resolved.columns else None)

    def _round_exp():
        got = infer_round_expertise(rnd, round_map)
        return got if got in EXPERTISE_FALLBACK else None

    def _owner_exp():
        if owner is not None and expertise_map:
            got = expertise_map.get(owner)
            if got in EXPERTISE_FALLBACK:
                return got
        return None

    if str(source).lower() == "owner":
        order = (_owner_exp, _round_exp)
    else:
        order = (_round_exp, _owner_exp)
    for fn in order:
        got = fn()
        if got:
            return got
    return None

# ── expertise_rank ─────────────────────────────────────────────

def expertise_rank(task_expertise, expert_expertise):
    """0 = best match, 1 / 2 = fallback tiers, len(chain) = unmatched."""
    if not task_expertise or task_expertise not in EXPERTISE_FALLBACK:
        return 0
    chain = EXPERTISE_FALLBACK[task_expertise]
    if expert_expertise in chain:
        return chain.index(expert_expertise)
    return len(chain)

# ── rank_candidate_experts ─────────────────────────────────────────────

def rank_candidate_experts(candidates, task_expertise, expertise_map):
    """PRIORITY 4 — order candidate experts by expertise fit for the task.

    Only reorders; it never removes anyone, so the old behaviour is kept
    when no expertise has been configured yet.
    """
    candidates = list(candidates)
    if not task_expertise or task_expertise not in EXPERTISE_FALLBACK:
        return candidates
    if not _has_any_expertise(expertise_map):
        return candidates

    def _key(name):
        exp = (expertise_map or {}).get(name, "Unspecified")
        return (expertise_rank(task_expertise, exp), str(name).lower())

    return sorted(candidates, key=_key)

# ── expert_pool_summary ─────────────────────────────────────────────

def expert_pool_summary(all_expert_names, expertise_map, presence_map):
    """Rows describing the expert pool, for display."""
    rows = []
    for e in all_expert_names:
        exp = (expertise_map or {}).get(e, "Unspecified")
        rows.append({
            "Expert": e,
            "Expertise": exp,
            "Presence": "Absent" if not is_present(e, presence_map) else "Present",
            "Eligible for new tasks": "No" if not is_present(e, presence_map) else "Yes",
        })
    return rows

# ── optimize_expertise_match ─────────────────────────────────────────────

def optimize_expertise_match(resolved, all_expert_names, expertise_map=None,
                             round_map=None, presence_map=None,
                             reallocate_absent=False, min_gap=GAP_MINUTES,
                             expertise_source="round"):
    """PRIORITY 4 + 5 PASS - run AFTER resolve_clashes() and enforce_gap_policy().

    1. PRIORITY 5: an interview sitting with an ABSENT expert is moved to a
       Present expert when `reallocate_absent` is on and a free one exists.
       An Absent expert is treated as the WORST possible owner, so their
       tasks move to the best-matching Present expert available.
    2. PRIORITY 4: every interview is moved to a better-fitting expert
       (same expertise first, then the documented fallback order) whenever
       such an expert is free WITH the 10-minute buffer respected.

    Never touches Self / HCR.  Never assigns to an Absent expert.
    Adds/updates two columns:
      resolution_action   -> "expertise_reassigned" / "absent_reassigned"
      expertise_violation -> True when the task still sits on a lower tier
                             because nobody better was free.
    """
    resolved = resolved.copy()
    resolved["expertise_violation"] = False
    if "expertise_fit" not in resolved.columns:
        resolved["expertise_fit"] = ""

    expertise_map = expertise_map or {}
    round_map = round_map or {}
    presence_map = presence_map or {}

    pool = eligible_experts(all_expert_names, presence_map)
    if not pool:
        return resolved

    if not _has_any_expertise(expertise_map):
        # nothing configured yet -> record the requirement only, change nothing
        for idx in resolved.index:
            task_exp = task_expertise_for_row(resolved, idx, expertise_map, round_map,
                                              expertise_source)
            resolved.loc[idx, "expertise_fit"] = task_exp or "No requirement"
        return resolved

    WORST = len(EXPERTISE_FALLBACK["Software Engineer"]) + 1

    for _ in range(max(2, len(resolved))):
        changed = False
        for idx in list(resolved.index):
            current = resolved.loc[idx, "expert_name"]
            if str(current).strip().lower() in ("hcr", "self"):
                continue

            task_exp = task_expertise_for_row(resolved, idx, expertise_map, round_map,
                                              expertise_source)
            cur_absent = not is_present(current, presence_map)
            if task_exp:
                cur_tier = expertise_rank(task_exp, expertise_map.get(current, "Unspecified"))
            else:
                cur_tier = 0

            if cur_absent and reallocate_absent:
                # an Absent expert is worse than any Present expert
                cur_tier = WORST if task_exp else (WORST + 1)
                needs_move = True
            else:
                needs_move = bool(task_exp) and cur_tier > 0
            if not needs_move:
                continue

            s = resolved.loc[idx, "start_min"]
            e = resolved.loc[idx, "end_min"]
            if task_exp:
                ranked = rank_candidate_experts(pool, task_exp, expertise_map)
            else:
                ranked = list(pool)

            for alt in ranked:
                if alt == current:
                    continue
                alt_tier = (expertise_rank(task_exp, expertise_map.get(alt, "Unspecified"))
                            if task_exp else 0)
                if alt_tier >= cur_tier:
                    continue
                busy = _get_expert_busy_intervals(resolved, alt)
                if _is_expert_free_with_gap(busy, s, e, min_gap):
                    resolved.loc[idx, "expert_name"] = alt
                    resolved.loc[idx, "resolution_action"] = (
                        "absent_reassigned" if cur_absent else "expertise_reassigned")
                    resolved.loc[idx, "gap_violation"] = False
                    changed = True
                    break

        if not changed:
            break

    # Final state of every task
    for idx in resolved.index:
        task_exp = task_expertise_for_row(resolved, idx, expertise_map, round_map,
                                          expertise_source)
        current = resolved.loc[idx, "expert_name"]
        if not task_exp:
            resolved.loc[idx, "expertise_fit"] = "No requirement"
            continue
        tier = expertise_rank(task_exp, expertise_map.get(current, "Unspecified"))
        resolved.loc[idx, "expertise_fit"] = task_exp + " - tier " + str(tier + 1)
        if tier > 0 and str(current).strip().lower() not in ("hcr", "self"):
            resolved.loc[idx, "expertise_violation"] = True
    return resolved
