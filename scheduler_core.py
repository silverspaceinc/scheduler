# ═══════════════════════════════════════════════════════════════════
#  scheduler_core.py — the Schedule View, extracted VERBATIM from the
#  Vizva dashboard (app.py).  Every function body is a byte-for-byte
#  copy, so the scheduler renders the SAME UI and computes the SAME
#  solution as the dashboard's Schedule View.
#  Extraction source : code4_Faster.py
#  Functions         : 55 (seeds + full call-dependency closure)
# ═══════════════════════════════════════════════════════════════════
import io
import os
import re
import json
import math
import requests
from datetime import datetime, date, timedelta, timezone
from collections import Counter

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

# _expert_config_path() resolves the config next to this module
__file__ = os.path.abspath(globals().get("__file__", "scheduler_core.py"))

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
_EXCEL_MAX_CELL_LEN = 32767


HIST = {
    "Interview Support": {
        "2025-07": {"completed": 54, "rescheduled": 14, "cancelled": 11, "candidates": 17},
        "2025-08": {"completed": 56, "rescheduled": 5,  "cancelled": 17, "candidates": 26},
        "2025-09": {"completed": 50, "rescheduled": 2,  "cancelled": 24, "candidates": 35},
        "2025-10": {"completed": 37, "rescheduled": 10, "cancelled": 19, "candidates": 24},
        "2025-11": {"completed": 32, "rescheduled": 2,  "cancelled": 9,  "candidates": 19},
        "2025-12": {"completed": 37, "rescheduled": 1,  "cancelled": 19, "candidates": 29},
        "2026-01": {"completed": 49, "rescheduled": 3,  "cancelled": 17, "candidates": 29},
        "2026-02": {"completed": 66, "rescheduled": 6,  "cancelled": 11, "candidates": 28},
        "2026-03": {"completed": 81, "rescheduled": 6,  "cancelled": 21, "candidates": 37},
        "2026-04": {"completed": 71, "rescheduled": 9,  "cancelled": 24, "candidates": 52},
    },
}

# ── render_start_time_insights ─────────────────────────────────────────────

def render_start_time_insights(df, title_suffix=""):
    """Render the Start Time Insights section."""
    if "start_hour" not in df.columns:
        st.info("No start_time data available for time-of-day analysis.")
        return

    valid = df.dropna(subset=["start_hour"]).copy()
    if valid.empty:
        st.info("No valid start_time entries found" + title_suffix + ".")
        return

    st.subheader("Start Time Insights" + title_suffix)

    total_with_time = len(valid)
    hour_counts = valid["start_hour"].value_counts().sort_index()
    peak_hour = int(hour_counts.idxmax())
    peak_count = int(hour_counts.max())
    peak_label = datetime(2000, 1, 1, peak_hour).strftime("%I:%M %p")
    mean_hour = valid["start_hour"].mean()
    mean_label = datetime(2000, 1, 1, int(mean_hour), int((mean_hour % 1) * 60)).strftime("%I:%M %p")

    k_cols = st.columns(4)
    k_cols[0].metric("Interviews with Time", total_with_time)
    k_cols[1].metric("Peak Hour", peak_label)
    k_cols[2].metric("Interviews at Peak", peak_count)
    k_cols[3].metric("Mean Start Time", mean_label)

    all_hours = list(range(0, 24))
    hour_labels = [datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0") for h in all_hours]
    counts = [int(hour_counts.get(h, 0)) for h in all_hours]

    bar_colors = ["#e74c3c" if h == peak_hour else "#3498db" for h in all_hours]

    fig_hourly = go.Figure(go.Bar(
        x=hour_labels, y=counts,
        marker_color=bar_colors,
        text=counts, textposition="outside",
    ))
    fig_hourly.update_layout(
        title="Interview Count by Hour of Day" + title_suffix,
        height=420,
        xaxis_title="Hour of Day",
        yaxis_title="Number of Interviews",
        xaxis=dict(tickangle=-45),
    )
    st.plotly_chart(fig_hourly, use_container_width=True)

    slots = {
        "Early Morning (7-9 AM)": (6, 9),
        "Morning (9 AM-12 PM)": (9, 12),
        "Afternoon (12-3 PM)": (12, 15),
        "Late Afternoon (3-6 PM)": (15, 18),
        "Evening (6-9 PM)": (18, 21),
        "Night (9 PM-6 AM)": None,
    }
    slot_rows = []
    for slot_name, rng in slots.items():
        if rng:
            cnt = int(((valid["start_hour"] >= rng[0]) & (valid["start_hour"] < rng[1])).sum())
        else:
            cnt = int(((valid["start_hour"] >= 21) | (valid["start_hour"] < 6)).sum())
        pct = round(cnt / total_with_time * 100, 1) if total_with_time > 0 else 0
        slot_rows.append({"Time Slot": slot_name, "Count": cnt, "% of Total": pct})
    slot_df = pd.DataFrame(slot_rows)

    with st.expander("Time Slot Breakdown" + title_suffix):
        st.dataframe(slot_df, use_container_width=True, hide_index=True)

# ── render_monthly_start_time_trend ─────────────────────────────────────────────

def render_monthly_start_time_trend(df, title_suffix=""):
    """Render a monthly × hour-of-day heatmap and monthly mean start time line."""
    if "start_hour" not in df.columns or "date" not in df.columns:
        return

    valid = df.dropna(subset=["start_hour"]).copy()
    if valid.empty:
        return

    valid["month"] = valid["date"].dt.to_period("M").astype(str)

    st.subheader("Monthly Start Time Trends" + title_suffix)

    pivot = valid.groupby(["month", "start_hour"]).size().reset_index(name="count")
    pivot_wide = pivot.pivot(index="start_hour", columns="month", values="count").fillna(0).astype(int)
    pivot_wide = pivot_wide.reindex(range(24), fill_value=0)
    pivot_wide.index = [datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0") for h in range(24)]

    fig_heat = px.imshow(
        pivot_wide, text_auto=True, aspect="auto",
        color_continuous_scale="Blues",
        title="Interviews by Hour & Month" + title_suffix,
        labels=dict(x="Month", y="Hour of Day", color="Count"),
    )
    fig_heat.update_layout(height=550)
    st.plotly_chart(fig_heat, use_container_width=True)

    monthly_agg = valid.groupby("month").agg(
        interviews=("start_hour", "size"),
        mean_start_hour=("start_hour", "mean"),
    ).reset_index()
    monthly_agg["mean_start_hour"] = monthly_agg["mean_start_hour"].round(2)
    monthly_agg["mean_start_label"] = monthly_agg["mean_start_hour"].apply(
        lambda h: datetime(2000, 1, 1, int(h), int((h % 1) * 60)).strftime("%I:%M %p")
    )

    fig_mean = go.Figure()
    fig_mean.add_trace(go.Scatter(
        x=monthly_agg["month"],
        y=monthly_agg["mean_start_hour"],
        mode="lines+markers+text",
        text=monthly_agg["mean_start_label"],
        textposition="top center",
        line=dict(color="#e67e22", width=3),
        marker=dict(size=10),
    ))
    fig_mean.update_layout(
        title="Mean Interview Start Time by Month" + title_suffix,
        height=400, xaxis_title="Month",
        yaxis_title="Hour of Day (24h)",
        yaxis=dict(range=[
            max(0, monthly_agg["mean_start_hour"].min() - 2),
            min(24, monthly_agg["mean_start_hour"].max() + 2),
        ]),
    )
    st.plotly_chart(fig_mean, use_container_width=True)

    with st.expander("Monthly Start Time Data" + title_suffix):
        st.dataframe(monthly_agg[["month", "interviews", "mean_start_label"]],
                     use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════
#  SCHEDULING CLASH DETECTION
# ═══════════════════════════════════════════════════════════════════

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

# ── _mean_start_label_from_minutes ─────────────────────────────────────────────

def _mean_start_label_from_minutes(minutes_val):
    """Convert float minutes-since-midnight to HH:MM AM/PM label."""
    h = int(minutes_val // 60) % 24
    m = int(minutes_val % 60)
    return datetime(2000, 1, 1, h, m).strftime("%I:%M %p")

# ── render_clash_summary ─────────────────────────────────────────────

def render_clash_summary(df, title_suffix=""):
    """Render the full Scheduling Clash Summary section."""
    clash_groups, clash_pairs = detect_expert_clashes(df)

    st.subheader("Scheduling Clash Detection" + title_suffix)
    st.caption(
        "A clash occurs when the same expert has 2+ interviews within a "
        "30-minute window on the same day. A '3-interview clash' means 3 "
        "interviews form a connected overlap group."
    )

    if clash_groups.empty:
        st.success("No scheduling clashes detected" + title_suffix + ".")
        return

    # ── KPI row ──────────────────────────────────────────────────
    total_groups = len(clash_groups)
    total_interviews_in_clashes = int(clash_groups["group_size"].sum())
    experts_with_clashes = clash_groups["expert_name"].nunique()
    days_with_clashes = clash_groups["date"].dt.date.nunique()
    overall_mean_min = clash_groups["mean_start_minutes"].mean()
    overall_mean_label = _mean_start_label_from_minutes(overall_mean_min)

    k = st.columns(5)
    k[0].metric("Clash Groups", total_groups)
    k[1].metric("Interviews in Clashes", total_interviews_in_clashes)
    k[2].metric("Experts with Clashes", experts_with_clashes)
    k[3].metric("Days with Clashes", days_with_clashes)
    k[4].metric("Mean Clash Start Time", overall_mean_label)

    # ── Clash Size Distribution ──────────────────────────────────
    st.markdown("---")
    st.subheader("Clash Size Distribution" + title_suffix)
    st.caption("How many interviews overlap in each clash group")

    size_counts = clash_groups["group_size"].value_counts().sort_index()
    size_labels = [str(s) + "-Interview Clash" for s in size_counts.index]
    size_colors = ["#f39c12" if s == 2 else "#e74c3c" if s == 3 else "#8e44ad"
                   for s in size_counts.index]

    col_size1, col_size2 = st.columns(2)
    with col_size1:
        fig_size = go.Figure(go.Bar(
            x=size_labels, y=size_counts.values,
            marker_color=size_colors,
            text=size_counts.values, textposition="outside",
        ))
        fig_size.update_layout(
            title="Overall Clash Size Distribution",
            height=400,
            xaxis_title="Clash Type",
            yaxis_title="Number of Clash Groups",
        )
        st.plotly_chart(fig_size, use_container_width=True)

    with col_size2:
        fig_pie = go.Figure(go.Pie(
            labels=size_labels, values=size_counts.values.tolist(),
            hole=0.45,
            marker=dict(colors=size_colors),
            textinfo="label+value+percent",
        ))
        fig_pie.update_layout(title="Clash Size Split", height=400, showlegend=False)
        st.plotly_chart(fig_pie, use_container_width=True)

    # ── Expert-wise Clash Size Breakdown ─────────────────────────
    st.markdown("---")
    st.subheader("Expert-wise Clash Breakdown" + title_suffix)

    expert_size = clash_groups.groupby(["expert_name", "group_size"]).size().reset_index(name="count")
    expert_size["size_label"] = expert_size["group_size"].apply(lambda s: str(s) + "-Interview")

    expert_totals = expert_size.groupby("expert_name")["count"].sum().sort_values(ascending=False)
    top_experts = expert_totals.head(20).index.tolist()
    expert_size_top = expert_size[expert_size["expert_name"].isin(top_experts)]

    col_e1, col_e2 = st.columns(2)
    with col_e1:
        pivot_es = expert_size_top.pivot_table(
            index="expert_name", columns="size_label", values="count", fill_value=0
        )
        pivot_es["_total"] = pivot_es.sum(axis=1)
        pivot_es = pivot_es.sort_values("_total", ascending=True).drop(columns="_total")

        fig_es = go.Figure()
        color_map = {"2-Interview": "#f39c12", "3-Interview": "#e74c3c",
                     "4-Interview": "#8e44ad", "5-Interview": "#2c3e50"}
        for col_name in sorted(pivot_es.columns):
            clr = color_map.get(col_name, "#95a5a6")
            fig_es.add_trace(go.Bar(
                y=pivot_es.index, x=pivot_es[col_name],
                name=col_name, orientation="h",
                marker_color=clr,
                text=pivot_es[col_name], textposition="inside",
            ))
        fig_es.update_layout(
            barmode="stack",
            title="Clash Groups by Expert & Size",
            height=max(420, len(top_experts) * 35),
            xaxis_title="Clash Groups",
            legend=dict(orientation="h", y=1.05, x=0.5, xanchor="center"),
        )
        st.plotly_chart(fig_es, use_container_width=True)

    with col_e2:
        expert_agg = clash_groups.groupby("expert_name").agg(
            clash_groups_count=("group_size", "size"),
            total_interviews=("group_size", "sum"),
            clash_days=("date", lambda x: x.dt.date.nunique()),
            mean_start=("mean_start_minutes", "mean"),
        ).reset_index().sort_values("clash_groups_count", ascending=False)
        expert_agg["mean_start_label"] = expert_agg["mean_start"].apply(_mean_start_label_from_minutes)

        fig_e2 = go.Figure()
        fig_e2.add_trace(go.Bar(
            y=expert_agg["expert_name"].head(15),
            x=expert_agg["clash_groups_count"].head(15),
            orientation="h",
            marker_color="#e74c3c",
            text=expert_agg["clash_groups_count"].head(15),
            textposition="outside",
            name="Clash Groups",
        ))
        fig_e2.update_layout(
            title="Top 15 Experts by Clash Groups",
            height=max(420, 15 * 35),
            yaxis=dict(autorange="reversed"),
            xaxis_title="Clash Groups",
        )
        st.plotly_chart(fig_e2, use_container_width=True)

    # ── Monthly Clash Trend with Size Breakdown ──────────────────
    st.markdown("---")
    st.subheader("Monthly Clash Trends" + title_suffix)

    monthly_size = clash_groups.groupby(["month", "group_size"]).size().reset_index(name="count")
    monthly_size["size_label"] = monthly_size["group_size"].apply(lambda s: str(s) + "-Interview")

    pivot_ms = monthly_size.pivot_table(
        index="month", columns="size_label", values="count", fill_value=0
    ).reset_index()

    col_m1, col_m2 = st.columns(2)
    with col_m1:
        fig_ms = go.Figure()
        for col_name in sorted([c for c in pivot_ms.columns if c != "month"]):
            clr = color_map.get(col_name, "#95a5a6")
            fig_ms.add_trace(go.Bar(
                x=pivot_ms["month"], y=pivot_ms[col_name],
                name=col_name, marker_color=clr,
                text=pivot_ms[col_name], textposition="inside",
            ))
        fig_ms.update_layout(
            barmode="stack",
            title="Monthly Clash Groups by Size",
            height=420,
            yaxis_title="Clash Groups",
            legend=dict(orientation="h", y=1.05, x=0.5, xanchor="center"),
        )
        st.plotly_chart(fig_ms, use_container_width=True)

    with col_m2:
        monthly_mean = clash_groups.groupby("month").agg(
            mean_start=("mean_start_minutes", "mean"),
        ).reset_index()
        monthly_mean["mean_start_label"] = monthly_mean["mean_start"].apply(
            _mean_start_label_from_minutes
        )
        monthly_mean["mean_start_hour"] = (monthly_mean["mean_start"] / 60).round(2)

        fig_mm = go.Figure()
        fig_mm.add_trace(go.Scatter(
            x=monthly_mean["month"],
            y=monthly_mean["mean_start_hour"],
            mode="lines+markers+text",
            text=monthly_mean["mean_start_label"],
            textposition="top center",
            line=dict(color="#e74c3c", width=3),
            marker=dict(size=10),
        ))
        fig_mm.update_layout(
            title="Mean Clash Start Time by Month",
            height=420,
            xaxis_title="Month",
            yaxis_title="Hour of Day (24h)",
            yaxis=dict(range=[
                max(0, monthly_mean["mean_start_hour"].min() - 2),
                min(24, monthly_mean["mean_start_hour"].max() + 2),
            ]),
        )
        st.plotly_chart(fig_mm, use_container_width=True)

    # ── Time-of-Day Distribution ─────────────────────────────────
    st.markdown("---")
    st.subheader("Clash Time-of-Day Distribution" + title_suffix)

    clash_hours = (clash_groups["mean_start_minutes"] // 60).astype(int)
    hour_counts = clash_hours.value_counts().sort_index()
    all_hours = list(range(0, 24))
    hour_labels = [datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0") for h in all_hours]
    counts = [int(hour_counts.get(h, 0)) for h in all_hours]
    peak_h = int(hour_counts.idxmax()) if not hour_counts.empty else 0
    bar_colors = ["#e74c3c" if h == peak_h else "#f39c12" for h in all_hours]

    fig_hour = go.Figure(go.Bar(
        x=hour_labels, y=counts,
        marker_color=bar_colors,
        text=counts, textposition="outside",
    ))
    fig_hour.update_layout(
        title="Clash Groups by Hour of Day" + title_suffix,
        height=420,
        xaxis_title="Hour of Day",
        yaxis_title="Clash Groups",
        xaxis=dict(tickangle=-45),
    )
    st.plotly_chart(fig_hour, use_container_width=True)

    # ── Expert-wise summary table ────────────────────────────────
    with st.expander("Expert Clash Summary Table" + title_suffix):
        expert_agg_display = expert_agg.copy()
        expert_agg_display = expert_agg_display[["expert_name", "clash_groups_count",
                                                  "total_interviews", "clash_days",
                                                  "mean_start_label"]]
        expert_agg_display.columns = ["Expert", "Clash Groups", "Interviews in Clashes",
                                      "Days with Clashes", "Mean Clash Start Time"]
        st.dataframe(expert_agg_display, use_container_width=True, hide_index=True)

    # ── Detailed Clash Groups table ──────────────────────────────
    with st.expander("Detailed Clash Groups" + title_suffix):
        detail = clash_groups[["expert_name", "date", "group_size",
                               "interviews_str", "mean_start_label", "month"]].copy()
        detail.columns = ["Expert", "Date", "Group Size", "Overlapping Times",
                          "Mean Start", "Month"]
        detail["Date"] = detail["Date"].dt.strftime("%Y-%m-%d")
        st.dataframe(detail, use_container_width=True, hide_index=True)

    if not clash_pairs.empty:
        with st.expander("Detailed Clash Pairs" + title_suffix):
            pairs_disp = clash_pairs[["expert_name", "date", "start_time_1",
                                      "start_time_2", "time_diff_min", "month"]].copy()
            pairs_disp.columns = ["Expert", "Date", "Time 1", "Time 2",
                                  "Diff (min)", "Month"]
            pairs_disp["Date"] = pairs_disp["Date"].dt.strftime("%Y-%m-%d")
            st.dataframe(pairs_disp, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════
#  TODAY'S CLASH SUMMARY (compact view for Today's Snapshot)
# ═══════════════════════════════════════════════════════════════════

# ── render_today_clash_summary ─────────────────────────────────────────────

def render_today_clash_summary(df):
    """Compact clash summary for today's snapshot."""
    clash_groups, clash_pairs = detect_expert_clashes(df)

    if clash_groups.empty:
        st.success("No scheduling clashes detected today.")
        return

    total_groups = len(clash_groups)
    total_interviews = int(clash_groups["group_size"].sum())
    experts_with = clash_groups["expert_name"].nunique()
    overall_mean_min = clash_groups["mean_start_minutes"].mean()
    overall_mean_label = _mean_start_label_from_minutes(overall_mean_min)

    k = st.columns(4)
    k[0].metric("⚠️ Clash Groups", total_groups)
    k[1].metric("Interviews in Clashes", total_interviews)
    k[2].metric("Experts with Clashes", experts_with)
    k[3].metric("Mean Clash Start Time", overall_mean_label)

    with st.expander("Clash Details"):
        display = clash_groups[["expert_name", "group_size", "interviews_str",
                                "mean_start_label"]].copy()
        display.columns = ["Expert", "Group Size", "Overlapping Times", "Mean Start"]
        st.dataframe(display, use_container_width=True, hide_index=True)

        if not clash_pairs.empty:
            st.caption("Pairwise Clashes")
            pairs_display = clash_pairs[["expert_name", "start_time_1",
                                         "start_time_2", "time_diff_min"]].copy()
            pairs_display.columns = ["Expert", "Time 1", "Time 2", "Diff (min)"]
            st.dataframe(pairs_display, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════
#  BLOCKAGE DETECTION
#  A blockage occurs in a 30-minute bracket on a given day when:
#    1) ALL active experts have at least one interview in that bracket
#    2) At least one expert has a clash (2+ interviews) in that bracket
#
#  Active experts = all unique expert names from the last 5 calendar
#  days of data PLUS any new experts appearing on the target day.
# ═══════════════════════════════════════════════════════════════════

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

# ── get_oos_valid_df ─────────────────────────────────────────────

def get_oos_valid_df(df, analytics_filter=True):
    """Return rows with valid out_of_shift data, optionally filtered for analytics.

    If analytics_filter=True: only completed + non-Self rows with valid OOS data
    If analytics_filter=False: all rows with valid OOS data
    """
    if "out_of_shift" not in df.columns:
        return pd.DataFrame()
    valid = df[df["out_of_shift"].notna()].copy()
    if analytics_filter and not valid.empty:
        if "task_status" in valid.columns:
            valid = valid[valid["task_status"].astype(str).str.strip().str.lower() == "completed"]
        if "expert_name" in valid.columns:
            valid = valid[valid["expert_name"].astype(str).str.strip().str.lower() != "self"]
    return valid

# ── render_oos_kpi ─────────────────────────────────────────────

def render_oos_kpi(df, title_suffix=""):
    """Render Out-of-Shift KPI row.
    Only considers completed + non-Self interviews for analytics.
    """
    if "out_of_shift" not in df.columns:
        return
    valid = get_oos_valid_df(df, analytics_filter=True)
    if valid.empty:
        st.info("No completed (non-Self) interviews with valid time data for out-of-shift analysis" + title_suffix + ".")
        return

    # Recompute _start_minutes_oos if it's been dropped (filtered subset)
    if "_parsed_start" in valid.columns and "_start_minutes_oos" not in valid.columns:
        valid["_start_minutes_oos"] = valid["_parsed_start"].dt.hour * 60 + valid["_parsed_start"].dt.minute
    elif "start_hour" in valid.columns and "_start_minutes_oos" not in valid.columns:
        valid["_start_minutes_oos"] = valid["start_hour"] * 60

    total_with_time = len(valid)
    oos_df = valid[valid["out_of_shift"] == True]
    oos_count = len(oos_df)
    in_shift_count = total_with_time - oos_count
    oos_pct = round(oos_count / total_with_time * 100, 1) if total_with_time > 0 else 0

    oos_candidates = oos_df["candidate_name"].nunique() if "candidate_name" in oos_df.columns and not oos_df.empty else 0
    oos_experts = oos_df["expert_name"].nunique() if "expert_name" in oos_df.columns and not oos_df.empty else 0
    oos_companies = oos_df["company_name"].nunique() if "company_name" in oos_df.columns and not oos_df.empty else 0

    k = st.columns(7)
    k[0].metric("Completed (non-Self) w/ Time", total_with_time)
    k[1].metric("In Shift (3:30AM-12:30PM)", in_shift_count)
    k[2].metric("Out of Shift", oos_count)
    k[3].metric("Out of Shift %", f"{oos_pct}%",
                delta="Lower is better", delta_color="inverse")
    k[4].metric("OOS Candidates", oos_candidates)
    k[5].metric("OOS Experts", oos_experts)
    k[6].metric("OOS Companies", oos_companies)

# ── render_oos_section ─────────────────────────────────────────────

def render_oos_section(df, title_suffix=""):
    """Full Out-of-Shift analysis section with charts and tables.
    Only considers completed + non-Self interviews for analytics (OOS analytics).
    """
    if "out_of_shift" not in df.columns:
        st.info("No start_time data available for out-of-shift analysis.")
        return

    valid = get_oos_valid_df(df, analytics_filter=True)
    if valid.empty:
        st.info("No completed (non-Self) interviews with valid time data" + title_suffix + ".")
        return

    if "_start_minutes_oos" not in valid.columns:
        if "_parsed_start" in valid.columns:
            valid["_start_minutes_oos"] = (
                valid["_parsed_start"].dt.hour * 60 + valid["_parsed_start"].dt.minute
            )
        elif "start_hour" in valid.columns:
            valid["_start_minutes_oos"] = valid["start_hour"] * 60

    oos_df = valid[valid["out_of_shift"] == True].copy()
    total_with_time = len(valid)
    oos_count = len(oos_df)
    in_shift_count = total_with_time - oos_count
    oos_pct = round(oos_count / total_with_time * 100, 1) if total_with_time > 0 else 0

    st.subheader("Out-of-Shift Interview Analysis" + title_suffix)
    st.caption(
        "Shift: 3:30 AM - 12:30 PM EDT. Interviews before 3:30 AM or on/after "
        "12:30 PM EDT are classified as **Out of Shift**. Only **completed** "
        "interviews by **non-Self** experts are analyzed."
    )

    render_oos_kpi(df, title_suffix)

    if oos_count == 0:
        st.success("No out-of-shift interviews detected" + title_suffix + ".")
        return

    oos_c1, oos_c2 = st.columns(2)
    with oos_c1:
        fig_donut = go.Figure(go.Pie(
            labels=["In Shift", "Out of Shift"],
            values=[in_shift_count, oos_count], hole=0.5,
            marker=dict(colors=["#2ecc71", "#e74c3c"]),
            textinfo="label+value+percent",
        ))
        fig_donut.update_layout(title="Shift Split" + title_suffix,
                                height=400, showlegend=False)
        st.plotly_chart(fig_donut, use_container_width=True)
    with oos_c2:
        if "_start_minutes_oos" in oos_df.columns:
            oos_hours = (oos_df["_start_minutes_oos"] // 60).astype(int)
            hour_counts = oos_hours.value_counts().sort_index()
            all_hours = list(range(0, 24))
            hour_labels = [datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0")
                           for h in all_hours]
            counts = [int(hour_counts.get(h, 0)) for h in all_hours]
            bar_colors = [("#e74c3c" if (h * 60) < SHIFT_START_MIN
                           or (h * 60) >= SHIFT_END_MIN else "#2ecc71")
                          for h in all_hours]
            fig_hour = go.Figure(go.Bar(
                x=hour_labels, y=counts, marker_color=bar_colors,
                text=counts, textposition="outside",
            ))
            fig_hour.update_layout(title="OOS Interviews by Hour" + title_suffix,
                                   height=400, xaxis_title="Hour of Day (EDT)",
                                   yaxis_title="OOS Interviews",
                                   xaxis=dict(tickangle=-45))
            st.plotly_chart(fig_hour, use_container_width=True)

    if "expert_name" in oos_df.columns:
        st.markdown("---")
        st.subheader("Expert-wise Out-of-Shift Breakdown" + title_suffix)
        expert_oos = oos_df.groupby("expert_name").agg(
            oos_interviews=("out_of_shift", "size")).reset_index()
        expert_total = valid.groupby("expert_name").size().reset_index(
            name="total_interviews")
        expert_oos = expert_oos.merge(expert_total, on="expert_name", how="left")
        expert_oos["oos_pct"] = (expert_oos["oos_interviews"]
                                 / expert_oos["total_interviews"] * 100).round(1)
        expert_oos = expert_oos.sort_values("oos_interviews", ascending=False)
        oos_e1, oos_e2 = st.columns(2)
        with oos_e1:
            top_experts = expert_oos.head(15).sort_values("oos_interviews", ascending=True)
            avg_oos_pct = expert_oos["oos_pct"].mean()
            e_colors = ["#e74c3c" if p >= avg_oos_pct else "#f39c12" for p in top_experts["oos_pct"]]
            fig_e = go.Figure(go.Bar(
                y=top_experts["expert_name"], x=top_experts["oos_interviews"],
                orientation="h", marker_color=e_colors,
                text=top_experts.apply(
                    lambda r: str(int(r["oos_interviews"])) + " (" + str(r["oos_pct"]) + "%)", axis=1),
                textposition="outside",
            ))
            fig_e.update_layout(title="Top 15 Experts by OOS Interviews",
                                height=max(420, len(top_experts) * 35),
                                xaxis_title="OOS Interviews")
            st.plotly_chart(fig_e, use_container_width=True)
        with oos_e2:
            top_by_pct = expert_oos[expert_oos["total_interviews"] >= 3].sort_values(
                "oos_pct", ascending=False).head(15)
            top_by_pct_sorted = top_by_pct.sort_values("oos_pct", ascending=True)
            pct_colors = ["#e74c3c" if p >= 50 else "#f39c12" if p >= 25 else "#2ecc71"
                          for p in top_by_pct_sorted["oos_pct"]]
            fig_ep = go.Figure(go.Bar(
                y=top_by_pct_sorted["expert_name"], x=top_by_pct_sorted["oos_pct"],
                orientation="h", marker_color=pct_colors,
                text=top_by_pct_sorted["oos_pct"].apply(lambda v: f"{v:.1f}%"),
                textposition="outside",
            ))
            fig_ep.update_layout(title="Top 15 Experts by OOS % (min 3 interviews)",
                                 height=max(420, len(top_by_pct_sorted) * 35),
                                 xaxis_title="OOS %",
                                 xaxis=dict(range=[0, min(100, top_by_pct_sorted["oos_pct"].max() + 15)]))
            st.plotly_chart(fig_ep, use_container_width=True)
        with st.expander("Expert OOS Data" + title_suffix):
            disp = expert_oos[["expert_name", "oos_interviews",
                               "total_interviews", "oos_pct"]].copy()
            disp.columns = ["Expert", "OOS Interviews", "Total Interviews", "OOS %"]
            st.dataframe(disp, use_container_width=True, hide_index=True)

    if "candidate_name" in oos_df.columns:
        st.markdown("---")
        st.subheader("Candidate-wise Out-of-Shift Breakdown" + title_suffix)
        cand_oos = oos_df.groupby("candidate_name").agg(
            oos_interviews=("out_of_shift", "size")).reset_index()
        cand_total = valid.groupby("candidate_name").size().reset_index(name="total_interviews")
        cand_oos = cand_oos.merge(cand_total, on="candidate_name", how="left")
        cand_oos["oos_pct"] = (cand_oos["oos_interviews"]
                               / cand_oos["total_interviews"] * 100).round(1)
        cand_oos = cand_oos.sort_values("oos_interviews", ascending=False)
        oos_ca1, oos_ca2 = st.columns(2)
        with oos_ca1:
            top_cands = cand_oos.head(15).sort_values("oos_interviews", ascending=True)
            fig_c = go.Figure(go.Bar(
                y=top_cands["candidate_name"], x=top_cands["oos_interviews"],
                orientation="h", marker_color="#e74c3c",
                text=top_cands.apply(
                    lambda r: str(int(r["oos_interviews"])) + " (" + str(r["oos_pct"]) + "%)", axis=1),
                textposition="outside",
            ))
            fig_c.update_layout(title="Top 15 Candidates by OOS Interviews",
                                height=max(420, len(top_cands) * 35),
                                xaxis_title="OOS Interviews")
            st.plotly_chart(fig_c, use_container_width=True)
        with oos_ca2:
            top_cands_pct = cand_oos[cand_oos["total_interviews"] >= 3].sort_values(
                "oos_pct", ascending=False).head(15)
            top_cands_pct_s = top_cands_pct.sort_values("oos_pct", ascending=True)
            cpct_colors = ["#e74c3c" if p >= 50 else "#f39c12" if p >= 25 else "#2ecc71"
                           for p in top_cands_pct_s["oos_pct"]]
            fig_cp = go.Figure(go.Bar(
                y=top_cands_pct_s["candidate_name"], x=top_cands_pct_s["oos_pct"],
                orientation="h", marker_color=cpct_colors,
                text=top_cands_pct_s["oos_pct"].apply(lambda v: f"{v:.1f}%"),
                textposition="outside",
            ))
            fig_cp.update_layout(title="Top 15 Candidates by OOS % (min 3 interviews)",
                                 height=max(420, len(top_cands_pct_s) * 35),
                                 xaxis_title="OOS %",
                                 xaxis=dict(range=[0, min(100, top_cands_pct_s["oos_pct"].max() + 15)]))
            st.plotly_chart(fig_cp, use_container_width=True)
        with st.expander("Candidate OOS Data" + title_suffix):
            disp_c = cand_oos[["candidate_name", "oos_interviews",
                               "total_interviews", "oos_pct"]].copy()
            disp_c.columns = ["Candidate", "OOS Interviews", "Total Interviews", "OOS %"]
            st.dataframe(disp_c, use_container_width=True, hide_index=True)

    if "round_name" in oos_df.columns:
        st.markdown("---")
        st.subheader("Round-wise Out-of-Shift Breakdown" + title_suffix)
        round_oos = oos_df.groupby("round_name").agg(
            oos_interviews=("out_of_shift", "size")).reset_index()
        if "round_name" in valid.columns:
            round_total = valid.groupby("round_name").size().reset_index(name="total_interviews")
        else:
            round_total = pd.DataFrame()
        if not round_total.empty:
            round_oos = round_oos.merge(round_total, on="round_name", how="left")
            round_oos["oos_pct"] = (round_oos["oos_interviews"]
                                    / round_oos["total_interviews"] * 100).round(1)
        else:
            round_oos["total_interviews"] = round_oos["oos_interviews"]
            round_oos["oos_pct"] = 100.0
        round_oos = round_oos.sort_values("oos_interviews", ascending=False)
        oos_r1, oos_r2 = st.columns(2)
        with oos_r1:
            round_sorted = round_oos.sort_values("oos_interviews", ascending=True)
            fig_r = go.Figure(go.Bar(
                y=round_sorted["round_name"], x=round_sorted["oos_interviews"],
                orientation="h", marker_color="#e67e22",
                text=round_sorted.apply(
                    lambda r: str(int(r["oos_interviews"])) + " (" + str(r["oos_pct"]) + "%)", axis=1),
                textposition="outside",
            ))
            fig_r.update_layout(title="Rounds by OOS Count",
                                height=max(400, len(round_sorted) * 40),
                                xaxis_title="OOS Interviews")
            st.plotly_chart(fig_r, use_container_width=True)
        with oos_r2:
            fig_rp = go.Figure(go.Pie(
                labels=round_oos["round_name"],
                values=round_oos["oos_interviews"].tolist(),
                hole=0.45, textinfo="label+value+percent",
            ))
            fig_rp.update_layout(title="OOS Round Split",
                                 height=400, showlegend=False)
            st.plotly_chart(fig_rp, use_container_width=True)
        with st.expander("Round OOS Data" + title_suffix):
            disp_r = round_oos[["round_name", "oos_interviews",
                                "total_interviews", "oos_pct"]].copy()
            disp_r.columns = ["Round", "OOS Interviews", "Total Interviews", "OOS %"]
            st.dataframe(disp_r, use_container_width=True, hide_index=True)

    if "date" in valid.columns:
        st.markdown("---")
        st.subheader("Monthly Out-of-Shift Trend" + title_suffix)
        valid_m = valid.copy()
        valid_m["month"] = valid_m["date"].dt.to_period("M").astype(str)
        monthly_total = valid_m.groupby("month").size().reset_index(name="total")
        monthly_oos = valid_m[valid_m["out_of_shift"] == True].groupby(
            "month").size().reset_index(name="oos")
        monthly_trend = monthly_total.merge(monthly_oos, on="month", how="left").fillna(0)
        monthly_trend["oos"] = monthly_trend["oos"].astype(int)
        monthly_trend["in_shift"] = monthly_trend["total"] - monthly_trend["oos"]
        monthly_trend["oos_pct"] = (monthly_trend["oos"]
                                    / monthly_trend["total"] * 100).round(1)
        mt1, mt2 = st.columns(2)
        with mt1:
            fig_mt = go.Figure()
            fig_mt.add_trace(go.Bar(x=monthly_trend["month"], y=monthly_trend["in_shift"],
                                    name="In Shift", marker_color="#2ecc71",
                                    text=monthly_trend["in_shift"], textposition="inside"))
            fig_mt.add_trace(go.Bar(x=monthly_trend["month"], y=monthly_trend["oos"],
                                    name="Out of Shift", marker_color="#e74c3c",
                                    text=monthly_trend["oos"], textposition="inside"))
            fig_mt.update_layout(barmode="stack", title="Monthly: In Shift vs OOS",
                                 height=420, yaxis_title="Interviews",
                                 legend=dict(orientation="h", y=1.05,
                                             x=0.5, xanchor="center"))
            st.plotly_chart(fig_mt, use_container_width=True)
        with mt2:
            pct_colors = ["#e74c3c" if p >= 30 else "#f39c12" if p >= 15 else "#2ecc71"
                          for p in monthly_trend["oos_pct"]]
            fig_mp = go.Figure()
            fig_mp.add_trace(go.Scatter(
                x=monthly_trend["month"], y=monthly_trend["oos_pct"],
                mode="lines+markers+text",
                text=monthly_trend["oos_pct"].apply(lambda v: f"{v:.1f}%"),
                textposition="top center",
                line=dict(color="#e74c3c", width=3),
                marker=dict(size=10, color=pct_colors),
            ))
            fig_mp.update_layout(title="Monthly OOS %", height=420, yaxis_title="OOS %",
                                 yaxis=dict(range=[0, max(50, monthly_trend["oos_pct"].max() + 10)]))
            st.plotly_chart(fig_mp, use_container_width=True)
        with st.expander("Monthly OOS Data" + title_suffix):
            st.dataframe(monthly_trend, use_container_width=True, hide_index=True)

    if "company_name" in oos_df.columns:
        st.markdown("---")
        st.subheader("Company-wise Out-of-Shift Breakdown" + title_suffix)
        comp_oos = oos_df.groupby("company_name").agg(
            oos_interviews=("out_of_shift", "size")).reset_index()
        if "company_name" in valid.columns:
            comp_total = valid.groupby("company_name").size().reset_index(name="total_interviews")
        else:
            comp_total = pd.DataFrame()
        if not comp_total.empty:
            comp_oos = comp_oos.merge(comp_total, on="company_name", how="left")
            comp_oos["oos_pct"] = (comp_oos["oos_interviews"]
                                   / comp_oos["total_interviews"] * 100).round(1)
        else:
            comp_oos["total_interviews"] = comp_oos["oos_interviews"]
            comp_oos["oos_pct"] = 100.0
        comp_oos = comp_oos.sort_values("oos_interviews", ascending=False)
        top_comp = comp_oos.head(15).sort_values("oos_interviews", ascending=True)
        fig_comp = go.Figure(go.Bar(
            y=top_comp["company_name"], x=top_comp["oos_interviews"],
            orientation="h", marker_color="#e74c3c",
            text=top_comp.apply(
                lambda r: str(int(r["oos_interviews"])) + " (" + str(r["oos_pct"]) + "%)", axis=1),
            textposition="outside",
        ))
        fig_comp.update_layout(title="Top 15 Companies by OOS Interviews",
                               height=max(420, len(top_comp) * 35),
                               xaxis_title="OOS Interviews")
        st.plotly_chart(fig_comp, use_container_width=True)
        with st.expander("Company OOS Data" + title_suffix):
            disp_co = comp_oos[["company_name", "oos_interviews",
                                "total_interviews", "oos_pct"]].copy()
            disp_co.columns = ["Company", "OOS Interviews", "Total Interviews", "OOS %"]
            st.dataframe(disp_co, use_container_width=True, hide_index=True)

    with st.expander("All Out-of-Shift Interviews (Completed, non-Self)" + title_suffix):
        display_cols = [c for c in ["date", "candidate_name", "expert_name",
                                    "company_name", "round_name", "task_status",
                                    "start_time", "sentiment_score",
                                    "sentiment_label"] if c in oos_df.columns]
        if display_cols:
            df_show = oos_df[display_cols]
            if "date" in df_show.columns:
                df_show = df_show.sort_values("date", ascending=False)
            st.dataframe(df_show, use_container_width=True, hide_index=True)
        else:
            st.dataframe(oos_df, use_container_width=True, hide_index=True)

# ── fetch_all_data ─────────────────────────────────────────────

def fetch_all_data():
    headers = {"x-api-key": API_KEY}
    all_records = []
    offset = 0
    limit = 500
    while True:
        response = requests.get(
            BASE_URL + "/api/app-case",
            headers=headers,
            params={"limit": limit, "offset": offset}
        )
        response.raise_for_status()
        batch = response.json()["data"]
        if not batch:
            break
        all_records.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    df = pd.DataFrame(all_records)
    return df

# ── normalize ─────────────────────────────────────────────

def normalize(df):
    cols_to_drop = ["case_candidate_phone", "status", "filled_by_username", "candidate_resume",
                    "case_candidate_email", "candidate_phone", "candidate_email",
                    "expert_is_team_lead", "expert_date_of_joining", "filled_by_first_name",
                    "filled_by_last_name", "filled_by_email"]
    id_cols = [c for c in df.columns if c.endswith("_id") or c == "id"]
    cols_to_drop = cols_to_drop + id_cols
    df = df.drop(columns=[c for c in cols_to_drop if c in df.columns])
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if "task_status" in df.columns:
        df["task_status"] = df["task_status"].fillna("pending")
        df["task_status"] = df["task_status"].astype(str).str.strip().str.lower()
        df["task_status"] = df["task_status"].replace("not done", "pending")
        df.loc[~df["task_status"].isin(["completed", "rescheduled", "cancelled", "pending"]), "task_status"] = "pending"
    if "support_name" in df.columns:
        df["support_name"] = df["support_name"].astype(str).str.strip()
    if "candidate_status_flag" in df.columns:
        _cs = df["candidate_status_flag"]
        df["candidate_status_flag"] = _cs.map(
            lambda v: v if isinstance(v, bool)
            else str(v).strip().lower() in ("true", "1", "yes", "y", "active", "enabled")
        ).astype(bool)
    return df

# ── filter_current_year ─────────────────────────────────────────────

def filter_current_year(df):
    if "date" not in df.columns:
        return df
    current_year = datetime.now().year
    return df[df["date"].dt.year == current_year].copy()

# ── filter_active_experts ─────────────────────────────────────────────

def filter_active_experts(df):
    if "expert_status_flag" not in df.columns:
        return df
    filtered = df[df["expert_status_flag"] == True].copy()
    filtered = filtered.drop(columns=["expert_status_flag"], errors="ignore")
    return filtered

# ── get_by_support ─────────────────────────────────────────────

def get_by_support(df, support_type):
    if df.empty or "support_name" not in df.columns:
        return df
    return df[df["support_name"].str.lower() == support_type.lower()].copy()

# ── hist_monthly_df ─────────────────────────────────────────────

def hist_monthly_df(support_type):
    if support_type not in HIST:
        return pd.DataFrame()
    rows = []
    for m, d in HIST[support_type].items():
        total = d["completed"] + d["rescheduled"] + d["cancelled"]
        rows.append({"month": m, "completed": d["completed"], "rescheduled": d["rescheduled"],
                      "cancelled": d["cancelled"], "pending": 0,
                      "total": total, "candidates": d["candidates"]})
    return pd.DataFrame(rows)

# ── _clean_cell_value ─────────────────────────────────────────────

def _clean_cell_value(val):
    """Sanitise a single cell value for Excel export."""
    if isinstance(val, bytes):
        try:
            val = val.decode("utf-8", errors="replace")
        except Exception:
            val = str(val)
    if not isinstance(val, str):
        return val
    # Truncate to Excel's max cell length
    if len(val) > _EXCEL_MAX_CELL_LEN:
        val = val[:_EXCEL_MAX_CELL_LEN - 60] + '... [TRUNCATED - original length: ' + str(len(val)) + ']'
    return val

# ── to_excel_bytes ─────────────────────────────────────────────

def to_excel_bytes(df):
    """Convert a DataFrame to Excel (.xlsx) bytes using xlsxwriter.
    xlsxwriter silently strips illegal XML characters — no more
    IllegalCharacterError.
    """
    clean = df.copy()

    # Clean every column for cell length limits
    for col in clean.columns:
        if clean[col].dtype == object:
            clean[col] = clean[col].map(_clean_cell_value)

    # Make datetimes timezone-unaware
    for col in clean.select_dtypes(include=["datetimetz"]).columns:
        clean[col] = clean[col].dt.tz_localize(None)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        clean.to_excel(writer, index=False, sheet_name="Data")
    buffer.seek(0)
    return buffer.getvalue()

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

# ── get_presence_map ─────────────────────────────────────────────

def get_presence_map(cfg):
    """expert name -> 'Present' / 'Absent'. Unknown names default Present."""
    raw = (cfg or {}).get("presence") or {}
    return {str(k): str(v) for k, v in raw.items()
            if str(v) in PRESENCE_OPTIONS}

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

# ── render_expert_config_panel ─────────────────────────────────────────────

def render_expert_config_panel(all_expert_names, all_rounds):
    """Editable, SAVED list of Expertise + Presence for every expert.

    Returns (expertise_map, presence_map, round_map, reallocate_absent).
    """
    cfg = load_expert_config()
    expertise_map = get_expertise_map(cfg)
    presence_map = get_presence_map(cfg)
    round_map = {str(k): str(v) for k, v in (cfg.get("round_expertise") or {}).items()
                 if str(v) in EXPERTISE_FALLBACK}
    reallocate_absent = bool(cfg.get("presence_first", True))
    updated_at = cfg.get("updated_at")

    n_classified = sum(1 for e in all_expert_names if e in expertise_map)
    n_absent = sum(1 for e in all_expert_names if not is_present(e, presence_map))

    st.caption(
        "Set what kind of engineer each expert is (Priority 4) and whether they are "
        "Present today (Priority 5). Choices are saved to **" + EXPERT_CONFIG_FILENAME +
        "** and remembered on the next run. Press **Save** to persist."
    )
    s1, s2, s3 = st.columns(3)
    s1.metric("Experts Classified", str(n_classified) + " / " + str(len(all_expert_names)))
    s2.metric("Marked Absent", n_absent)
    s3.metric("Last Saved", str(updated_at)[:19].replace("T", " ") if updated_at else "never")

    AUTO = "(auto-detect)"

    with st.form("expert_config_form"):
        st.markdown("**Expertise & Presence**")
        h1, h2, h3 = st.columns([2, 2, 2])
        h1.markdown("**Expert**")
        h2.markdown("**Expertise**")
        h3.markdown("**Presence**")
        new_expertise = {}
        new_presence = {}
        for e in all_expert_names:
            c1, c2, c3 = st.columns([2, 2, 2])
            c1.markdown(str(e))
            current_exp = expertise_map.get(e, "Unspecified")
            idx_exp = (EXPERTISE_CATEGORIES.index(current_exp)
                       if current_exp in EXPERTISE_CATEGORIES else len(EXPERTISE_CATEGORIES) - 1)
            new_expertise[e] = c2.selectbox(
                "Expertise", EXPERTISE_CATEGORIES, index=idx_exp,
                key="cfg_exp_" + str(e), label_visibility="collapsed")
            current_pres = "Absent" if not is_present(e, presence_map) else "Present"
            new_presence[e] = c3.selectbox(
                "Presence", PRESENCE_OPTIONS, index=PRESENCE_OPTIONS.index(current_pres),
                key="cfg_pres_" + str(e), label_visibility="collapsed")

        new_round_map = {}
        if all_rounds:
            st.markdown("---")
            st.markdown("**Round → required expertise**")
            st.caption("Used when the owning expert has no expertise set. "
                       "'(auto-detect)' applies the built-in keyword rules.")
            for r in all_rounds:
                suggested = infer_round_expertise(r, round_map)
                options = [AUTO] + EXPERTISE_CATEGORIES
                if r in round_map:
                    idx_r = options.index(round_map[r]) if round_map[r] in options else 0
                else:
                    idx_r = options.index(suggested) if suggested in options else 0
                picked = st.selectbox(
                    str(r) + "  →", options, index=idx_r,
                    key="cfg_rnd_" + str(r),
                    help="Auto-detected: " + str(suggested or "no requirement"))
                new_round_map[r] = picked

        st.markdown("---")
        opt_reallocate = st.checkbox(
            "PRIORITY 1 — move tasks off an Absent expert to a Present expert",
            value=reallocate_absent,
            help="ON (recommended): a task can never stay with an Absent expert while a "
                 "Present expert can take it — Technical/Final rounds included. The same "
                 "expertise is tried first, then the configured fallback cycle. "
                 "OFF: Absent experts only stop receiving NEW tasks; their existing ones "
                 "stay listed and are flagged.")
        opt_source = st.selectbox(
            "The task's required expertise comes from",
            ["Round type (recommended)", "Owner expert's expertise"],
            index=1 if str(cfg.get("task_expertise_source", "round")).lower() == "owner" else 0,
            key="cfg_expertise_source",
            help="Round type: the round name decides which expertise the task needs "
                 "(SQL/Case -> Data, Coding/Technical -> Software, DevOps/Cyber -> DevOps). "
                 "This lets the resolver FIX a task that sits on the wrong type of expert. "
                 "Owner expert: the expert the task currently sits with decides its type.")
        st.markdown("---")
        b1, b2 = st.columns(2)
        save_clicked = b1.form_submit_button("💾 Save Configuration", use_container_width=True)
        reset_clicked = b2.form_submit_button("✅ Mark All Present & Save", use_container_width=True)

    if save_clicked or reset_clicked:
        new_cfg = {
            "expertise": {str(k): str(v) for k, v in new_expertise.items()
                          if str(v) in EXPERTISE_FALLBACK},
            "presence": ({str(k): "Present" for k in all_expert_names}
                         if reset_clicked else
                         {str(k): str(v) for k, v in new_presence.items()}),
            "round_expertise": {str(k): str(v) for k, v in new_round_map.items()
                                if str(v) in EXPERTISE_FALLBACK},
            "presence_first": bool(opt_reallocate),
            "reallocate_absent": bool(opt_reallocate),
            "task_expertise_source": ("owner" if str(opt_source).startswith("Owner") else "round"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        ok, info = save_expert_config(new_cfg)
        if ok:
            st.success("Saved to " + str(info) + ("" if not reset_clicked
                                                  else " — all experts marked Present."))
            st.rerun()
        else:
            st.error("Could not save the configuration: " + str(info))

    st.markdown("---")
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            "⬇️ Download expert config (JSON)",
            data=json.dumps(cfg, indent=2).encode("utf-8"),
            file_name=EXPERT_CONFIG_FILENAME, mime="application/json",
            use_container_width=True)
    with d2:
        up = st.file_uploader("⬆️ Restore expert config (JSON)", type=["json"],
                              key="cfg_upload")
        if up is not None:
            try:
                restored = json.loads(up.getvalue().decode("utf-8"))
                if isinstance(restored, dict):
                    restored.setdefault("expertise", {})
                    restored.setdefault("presence", {})
                    restored.setdefault("round_expertise", {})
                    restored["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    ok, info = save_expert_config(restored)
                    if ok:
                        st.success("Configuration restored from file.")
                        st.rerun()
                    else:
                        st.error("Restore failed: " + str(info))
                else:
                    st.error("That JSON file does not contain an expert configuration.")
            except Exception as exc:
                st.error("Could not read that file: " + str(exc))

    expertise_source = "owner" if str(cfg.get("task_expertise_source", "round")).lower() == "owner" else "round"
    return expertise_map, presence_map, round_map, reallocate_absent, expertise_source

# ── render_schedule_view ─────────────────────────────────────────────

def render_schedule_view(all_data, active_expert_df):
    """Main renderer for the Schedule View page."""
    st.header("Schedule View")
    st.caption("Select any date to see Interview Support interviews organized by expert timeline (EDT)")

    if "date" not in all_data.columns:
        st.warning("No date column available in data.")
        return

    valid_dates = all_data["date"].dropna()
    if valid_dates.empty:
        st.warning("No valid dates found.")
        return

    min_d = valid_dates.min().date()
    max_d = valid_dates.max().date()

    selected_date = st.date_input("Select Date", value=date.today(),
                                   min_value=min_d,
                                   max_value=max_d + timedelta(days=90),
                                   key="schedule_date")

    sched = build_schedule_data(all_data, selected_date)

    # Get ALL active expert names (excluding HCR, Self)
    EXCLUDE_EXPERTS = {"hcr", "self"}
    all_expert_names = sorted([
        e for e in active_expert_df["expert_name"].dropna().unique()
        if e.strip().lower() not in EXCLUDE_EXPERTS
    ]) if "expert_name" in active_expert_df.columns else []

    # ── PRIORITY 4 & 5 — EXPERT EXPERTISE + PRESENCE (saved) ─────
    all_rounds = []
    if "round_name" in all_data.columns:
        all_rounds = sorted({
            str(r).strip() for r in all_data["round_name"].dropna().unique()
            if str(r).strip() and str(r).strip().lower() not in ("nan", "none")
        })
    with st.expander("🧑‍🏫 Expert Expertise & Presence — Priority 4 & 5",
                     expanded=False):
        (expertise_map, presence_map, round_map,
         reallocate_absent, expertise_source) = \
            render_expert_config_panel(all_expert_names, all_rounds)

    if sched.empty:
        st.info("No Interview Support interviews with valid time data on " + str(selected_date))
        day_all = all_data[all_data["date"].dt.date == selected_date]
        if not day_all.empty:
            st.caption(str(len(day_all)) + " record(s) found but none have valid start_time.")
        if all_expert_names:
            st.markdown("---")
            render_schedule_gantt(sched, selected_date, all_expert_names)
            st.markdown("---")
            render_availability_summary(sched, selected_date, all_expert_names)
        return

    # ── KPI row ──────────────────────────────────────────────────
    k = st.columns(6)
    k[0].metric("Total Interviews", len(sched))
    k[1].metric("Experts", sched["expert_name"].nunique())
    k[2].metric("Completed", int((sched["task_status"] == "completed").sum()))
    k[3].metric("Pending", int((sched["task_status"] == "pending").sum()))
    k[4].metric("Rescheduled", int((sched["task_status"] == "rescheduled").sum()))
    k[5].metric("Cancelled", int((sched["task_status"] == "cancelled").sum()))

    clash_count = int(sched["has_clash"].sum())
    experts_with_clash = sched[sched["has_clash"]]["expert_name"].nunique()
    if clash_count > 0:
        st.warning(
            "⚠️ **" + str(clash_count) + " interview(s) have clashes** across **"
            + str(experts_with_clash) + " expert(s)**. See the 🧠 Intelligent Clash Resolution section below."
        )

    st.caption("Showing Interview Support only")

    # ── GANTT CHART ──────────────────────────────────────────────
    st.markdown("---")
    render_schedule_gantt(sched, selected_date, all_expert_names)

    # ── AVAILABILITY SUMMARY ─────────────────────────────────────
    st.markdown("---")
    render_availability_summary(sched, selected_date, all_expert_names)

    # ═════════════════════════════════════════════════════════════
    #  🧠 INTELLIGENT CLASH RESOLUTION
    # ═════════════════════════════════════════════════════════════
    if True:  # presence (1) + clash (2) + round pref (3) + 10-min gap (4) + expertise (5)
        st.markdown("---")
        st.header("🧠 Intelligent Clash Resolution")
        st.caption(
            "Priority 1 - PRESENCE: a task is never left with an Absent expert while a "
            "Present expert can take it. Technical Coding / Final Round interviews are "
            "moved too - the same expertise is tried first, then the configured fallback "
            "cycle (orange bars). Priority 2 - Clashes: overlapping interviews are split "
            "across experts. Priority 3 - Round preference: Technical Coding / Final Round "
            "prefer to stay with their original expert when that expert is Present. "
            "Priority 4 - 10-minute rule: at least 10 minutes of gap between interviews. "
            "Priority 5 - Expertise: re-align to the required profile (purple bars). "
            "Anything that could not be fixed is highlighted in red. Self / HCR are never "
            "touched."
        )

        # PRIORITY 1 - presence: move tasks off Absent experts first
        resolved = enforce_presence_first(
            sched, all_expert_names, expertise_map=expertise_map,
            round_map=round_map, presence_map=presence_map,
            expertise_source=expertise_source,
            enabled=reallocate_absent)
        # PRIORITY 2-4 - clashes, round preference, 10-minute gap
        resolved = resolve_clashes(resolved, all_expert_names,
                                   expertise_map=expertise_map,
                                   presence_map=presence_map,
                                   round_map=round_map,
                                   expertise_source=expertise_source)
        resolved = enforce_gap_policy(resolved, all_expert_names,
                                      expertise_map=expertise_map,
                                      presence_map=presence_map,
                                      round_map=round_map,
                                      expertise_source=expertise_source)
        # PRIORITY 5 - expertise routing
        resolved = optimize_expertise_match(
            resolved, all_expert_names, expertise_map=expertise_map,
            round_map=round_map, presence_map=presence_map,
            reallocate_absent=False,
            expertise_source=expertise_source)
        resolved = apply_presence_first_labels(resolved, presence_map)

        # ── Expert pool actually used for allocation (P4/P5) ──────
        pool_rows = expert_pool_summary(all_expert_names, expertise_map, presence_map)
        pool_df = pd.DataFrame(pool_rows)
        if not pool_df.empty:
            pool_df["Interviews Assigned"] = pool_df["Expert"].apply(
                lambda e: int((resolved["expert_name"] == e).sum()))
            with st.expander("🧑‍🏫 Expert Pool Used for Allocation (expertise + presence)",
                             expanded=False):
                st.dataframe(pool_df, use_container_width=True, hide_index=True)
                stranded = pool_df[(pool_df["Presence"] == "Absent")
                                   & (pool_df["Interviews Assigned"] > 0)]
                if not stranded.empty:
                    st.warning(
                        "⚠️ " + str(len(stranded)) + " Absent expert(s) still hold scheduled "
                        "interviews: " + ", ".join(str(x) for x in stranded["Expert"].tolist())
                        + ". Turn on 'Reallocate tasks that sit with an Absent expert' in the "
                          "Expert Expertise & Presence panel to move them to a Present expert.")

        # ── Resolution Summary KPIs & Tables ─────────────────────
        render_resolution_summary(resolved, selected_date)

        # ── Resolved Gantt Chart ─────────────────────────────────
        st.markdown("---")
        render_resolved_gantt(resolved, selected_date, all_expert_names)

        # ── Updated Availability After Resolution ────────────────
        st.markdown("---")
        render_availability_summary(resolved, selected_date, all_expert_names)

        # ── Download Resolved Schedule ───────────────────────────
        resolved_display = resolved[[
            c for c in [
                "expert_name", "original_expert", "resolution_action",
                "candidate_name", "company_name", "round_name",
                "support_name", "task_status",
                "start_label", "end_label", "duration",
                "has_clash", "is_oos", "gap_violation",
                "expertise_fit", "expertise_violation", "presence_violation",
            ] if c in resolved.columns
        ]].copy()
        resolved_display.columns = [
            c.replace("_", " ").title() for c in resolved_display.columns
        ]

        st.download_button(
            label="📥 Download Resolved Schedule",
            data=to_excel_bytes(resolved_display),
            file_name="resolved_schedule_" + str(selected_date) + ".xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # ── CLASH DETAILS ────────────────────────────────────────────
    clash_df = sched[sched["has_clash"]].copy()
    if not clash_df.empty:
        st.markdown("---")
        st.subheader("Original Clash Details — " + str(selected_date))
        clash_display = clash_df[["expert_name", "candidate_name", "company_name",
                                   "round_name", "support_name", "task_status",
                                   "start_label", "end_label", "duration"]].copy()
        clash_display.columns = ["Expert", "Candidate", "Company", "Round", "Support",
                                  "Status", "Start", "End", "Duration (min)"]
        st.dataframe(clash_display.sort_values(["Expert", "Start"]),
                     use_container_width=True, hide_index=True)


    # OUT-OF-SHIFT DETAILS (Schedule View) - kept here between clash block
    # and the FULL SCHEDULE TABLE expander.
    if "is_oos" in sched.columns and not sched.empty:
        oos_sched = sched[sched["is_oos"] == True].copy()
        if not oos_sched.empty:
            st.markdown("---")
            st.subheader("Out-of-Shift Interviews - " + str(selected_date))
            st.caption(
                "All interviews (any status, including Self) scheduled "
                "outside the shift window (3:30 AM - 12:30 PM EDT). "
                "Purple/orange bars in the Gantt above flag OOS."
            )
            oos_k = st.columns(5)
            oos_k[0].metric("OOS Interviews", len(oos_sched))
            oos_k[1].metric("OOS Experts", int(oos_sched["expert_name"].nunique()))
            oos_k[2].metric(
                "OOS Candidates",
                int(oos_sched["candidate_name"].nunique())
                if "candidate_name" in oos_sched.columns else 0,
            )
            oos_k[3].metric(
                "OOS Completed",
                int((oos_sched["task_status"] == "completed").sum()),
            )
            oos_k[4].metric(
                "OOS Pending",
                int((oos_sched["task_status"] == "pending").sum()),
            )

            oos_sc1, oos_sc2 = st.columns(2)
            with oos_sc1:
                oos_status_series = oos_sched["task_status"].value_counts()
                labels_s = [TASK_LABEL.get(s, s.title()) for s in oos_status_series.index]
                colors_s = [CLR.get(s, "#95a5a6") for s in oos_status_series.index]
                fig_os = go.Figure(go.Pie(
                    labels=labels_s, values=oos_status_series.values.tolist(),
                    hole=0.45, marker=dict(colors=colors_s),
                    textinfo="label+value+percent",
                ))
                fig_os.update_layout(title="OOS by Task Status",
                                     height=380, showlegend=False)
                st.plotly_chart(fig_os, use_container_width=True)
            with oos_sc2:
                oos_by_expert = oos_sched["expert_name"].value_counts()
                fig_oe = go.Figure(go.Bar(
                    y=oos_by_expert.index, x=oos_by_expert.values,
                    orientation="h", marker_color="#9b59b6",
                    text=oos_by_expert.values, textposition="outside",
                ))
                fig_oe.update_layout(
                    title="OOS by Expert",
                    height=max(380, len(oos_by_expert) * 35),
                    yaxis=dict(autorange="reversed"),
                    xaxis_title="OOS Interviews",
                )
                st.plotly_chart(fig_oe, use_container_width=True)

            with st.expander("OOS Interview Details - " + str(selected_date)):
                cols_show = [c for c in ["expert_name", "candidate_name",
                                          "company_name", "round_name",
                                          "task_status", "start_label",
                                          "end_label", "duration"]
                             if c in oos_sched.columns]
                oos_display = oos_sched[cols_show].copy()
                oos_display.columns = [c.replace("_", " ").title() for c in cols_show]
                sort_cols = [c for c in ["Expert Name", "Start Label"]
                             if c in oos_display.columns]
                st.dataframe(
                    oos_display.sort_values(sort_cols) if sort_cols else oos_display,
                    use_container_width=True, hide_index=True,
                )

    # ── FULL SCHEDULE TABLE ──────────────────────────────────────
    with st.expander("Full Schedule Table — " + str(selected_date)):
        table_cols = [c for c in ["expert_name", "candidate_name", "company_name",
                                   "round_name", "support_name", "task_status",
                                   "start_label", "end_label", "duration", "has_clash"]
                      if c in sched.columns]
        display = sched[table_cols].copy()
        display.columns = [c.replace("_", " ").title() for c in table_cols]
        st.dataframe(display.sort_values(["Expert Name", "Start Label"]),
                     use_container_width=True, hide_index=True)
