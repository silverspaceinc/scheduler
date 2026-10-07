"""
Vizva Scheduler v3.0

* Renders the dashboard's Schedule View EXACTLY (scheduler_core.py, extracted
  verbatim from the dashboard).
* Adds a draggable planner (today only) with three STATUS lanes under the
  experts:  Unassigned / Rescheduled / Cancelled.
* Overlapping bars stack on separate rows (nothing hides behind anything) and
  get a red ring so the conflict is visible.
* Login survives a browser refresh and expires after 6 hours.
* Live data: the app re-reads the API every 2 minutes (and on "Refresh now").
* Every drag is saved to manual_moves.json (24-hour lifetime).

Run:  streamlit run scheduler_app.py
"""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
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

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except Exception:
    HAS_AUTOREFRESH = False

APP_VERSION = "SCHED v3.0 — status lanes · stacked bars · live refresh · 6h login"

st.set_page_config(page_title="Vizva Scheduler [v3.0]", page_icon="🗓️", layout="wide")

API_KEY = st.secrets.get("API_KEY", "")
BASE_URL = st.secrets.get("BASE_URL", "")

EDT = timezone(timedelta(hours=-4))
REFRESH_SECONDS = 120          # live data refresh
SESSION_HOURS = 6              # login lifetime
MOVES_TTL_HOURS = 24           # saved-move lifetime

_HERE = os.path.dirname(os.path.abspath(__file__))
MOVES_FILE = os.path.join(_HERE, "manual_moves.json")
_GANTT_DIR = os.path.join(_HERE, "draggable_gantt")
try:
    _draggable_gantt = components.declare_component("vizva_draggable_gantt", path=_GANTT_DIR)
except Exception:
    _draggable_gantt = None

# ── status lanes (internal keys can never collide with an expert name) ──
UNASSIGNED = "__UNASSIGNED__"
RESCHEDULED = "__RESCHEDULED__"
CANCELLED = "__CANCELLED__"
STATUS_LANES = [
    (UNASSIGNED, "⚪ Unassigned"),
    (RESCHEDULED, "🟠 Rescheduled"),
    (CANCELLED, "🔴 Cancelled"),
]
STATUS_KEYS = {k for k, _ in STATUS_LANES}
STATUS_LABEL = dict(STATUS_LANES)

STATUS_COLORS = {"completed": "#2ecc71", "rescheduled": "#f39c12",
                 "cancelled": "#e74c3c", "pending": "#3498db"}
REASSIGNED_COLOR = "#1abc9c"
UNRESOLVED_COLOR = "#c0392b"
EXPERTISE_COLOR = "#9b59b6"
ABSENT_COLOR = "#e67e22"
UNASSIGNED_COLOR = "#95a5a6"
MANUAL_BORDER = "#00e5ff"

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
    {"label": "Unassigned", "color": UNASSIGNED_COLOR, "border": "white", "width": 1},
    {"label": "Manually moved (saved)", "color": "#2c3e50", "border": MANUAL_BORDER, "width": 3},
]


# ═══════════════════════════════════════════════════════════════════
#  AUTH — signed token in the URL, so a browser refresh keeps you in
# ═══════════════════════════════════════════════════════════════════
def _secret_key():
    seed = "%s|%s|%s" % (st.secrets.get("VIZVA_USERNAME", ""),
                         st.secrets.get("VIZVA_PASSWORD", ""),
                         st.secrets.get("API_KEY", ""))
    return hashlib.sha256(seed.encode("utf-8")).digest()


def make_token(username, now=None):
    """'<b64 payload>.<b64 hmac>' carrying the user name and expiry time."""
    now = time.time() if now is None else now
    payload = json.dumps({"u": username, "exp": int(now + SESSION_HOURS * 3600)},
                         separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_secret_key(), payload, hashlib.sha256).digest()
    b64 = lambda raw: base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return b64(payload) + "." + b64(sig)


def verify_token(token, now=None):
    """Return the payload dict when the token is authentic and unexpired, else None."""
    now = time.time() if now is None else now
    try:
        p64, s64 = str(token).split(".", 1)
        pad = lambda s: s + "=" * (-len(s) % 4)
        payload = base64.urlsafe_b64decode(pad(p64))
        sig = base64.urlsafe_b64decode(pad(s64))
        expected = hmac.new(_secret_key(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload.decode("utf-8"))
        if float(data.get("exp", 0)) < now:
            return None
        return data
    except Exception:
        return None


def _qp_get(name):
    try:
        v = st.query_params.get(name)
    except Exception:
        return None
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _qp_set(name, value):
    try:
        st.query_params[name] = value
    except Exception:
        pass


def _qp_del(name):
    try:
        if name in st.query_params:
            del st.query_params[name]
    except Exception:
        pass


def is_authenticated():
    tok = st.session_state.get("auth_token") or _qp_get("auth")
    data = verify_token(tok) if tok else None
    if data:
        st.session_state["auth_token"] = tok
        st.session_state["auth_exp"] = data["exp"]
        if _qp_get("auth") != tok:
            _qp_set("auth", tok)            # keep the URL in sync (refresh-safe)
        return True
    st.session_state.pop("auth_token", None)
    st.session_state.pop("auth_exp", None)
    _qp_del("auth")
    return False


def logout():
    st.session_state.pop("auth_token", None)
    st.session_state.pop("auth_exp", None)
    _qp_del("auth")
    st.rerun()


def login():
    st.title("🗓️ Vizva Scheduler — Sign in")
    st.caption("Use the same username and password as the Vizva dashboard. "
               "You stay signed in for " + str(SESSION_HOURS) + " hours, even if you refresh.")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)
    if submitted:
        valid_user = str(st.secrets.get("VIZVA_USERNAME", ""))
        valid_pass = str(st.secrets.get("VIZVA_PASSWORD", ""))
        if not valid_user or not valid_pass:
            st.error("Login secrets (VIZVA_USERNAME / VIZVA_PASSWORD) not configured.")
            st.stop()
        if username.strip() == valid_user.strip() and password == valid_pass:
            tok = make_token(username.strip())
            st.session_state["auth_token"] = tok
            _qp_set("auth", tok)
            st.rerun()
        else:
            st.error("Invalid username or password.")
    st.stop()


# ═══════════════════════════════════════════════════════════════════
#  SAVED MOVES — backend JSON, 24-hour lifetime, atomic writes
# ═══════════════════════════════════════════════════════════════════
def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value):
    try:
        ts = datetime.fromisoformat(str(value))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load_moves():
    """Read the store, dropping entries older than MOVES_TTL_HOURS."""
    empty = {"updated_at": None, "ttl_hours": MOVES_TTL_HOURS, "moves": {}}
    if not os.path.exists(MOVES_FILE):
        return empty
    try:
        with open(MOVES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MOVES_TTL_HOURS)
    purged = {}
    for dkey, block in (data.get("moves") or {}).items():
        keep = {}
        for skey, mv in (block or {}).items():
            ts = _parse_iso((mv or {}).get("at"))
            if ts is not None and ts >= cutoff:
                keep[skey] = mv
        if keep:
            purged[dkey] = keep
    data["moves"] = purged
    data["ttl_hours"] = MOVES_TTL_HOURS
    return data


def _write_moves(data):
    """Atomic write: temp file + os.replace, so a crash never leaves half a JSON."""
    data["updated_at"] = _now_iso()
    data["ttl_hours"] = MOVES_TTL_HOURS
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(MOVES_FILE) or ".",
                                   prefix=".moves_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, MOVES_FILE)
        return True
    except Exception:
        return False


def save_move(date_key, key, to_lane, from_lane):
    """Create or UPDATE the saved move for one interview."""
    data = load_moves()
    block = data.setdefault("moves", {}).setdefault(str(date_key), {})
    block[str(key)] = {"to": str(to_lane), "from": str(from_lane), "at": _now_iso()}
    return _write_moves(data)


def delete_move(date_key, key):
    data = load_moves()
    block = (data.get("moves") or {}).get(str(date_key), {})
    if str(key) in block:
        block.pop(str(key), None)
        if not block:
            (data.get("moves") or {}).pop(str(date_key), None)
        return _write_moves(data)
    return False


def clear_moves(date_key=None):
    data = load_moves()
    if date_key is None:
        data = {"updated_at": None, "ttl_hours": MOVES_TTL_HOURS, "moves": {}}
    else:
        (data.get("moves") or {}).pop(str(date_key), None)
    return _write_moves(data)


def moves_for(date_key):
    return (load_moves().get("moves") or {}).get(str(date_key), {})


def stable_key(row):
    """Identifies the INTERVIEW itself (survives re-solving and API refreshes)."""
    return "%s | %s | %s | %s" % (
        str(row.get("candidate_name", "")).strip(),
        str(row.get("company_name", "")).strip(),
        str(row.get("round_name", "")).strip(),
        str(row.get("start_label", "")).strip())


# ═══════════════════════════════════════════════════════════════════
#  DATA + SOLVER
# ═══════════════════════════════════════════════════════════════════
@st.cache_data(ttl=REFRESH_SECONDS, show_spinner="Loading live interviews...")
def load_frames(pipeline_version="sched-v3.0"):
    """Same pipeline as the dashboard. Cached for REFRESH_SECONDS, so the API is
    re-read on the first rerun after each 2-minute window."""
    raw = fetch_all_data()
    if raw is None or raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    raw = normalize(raw)
    raw = filter_current_year(raw)
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    if "expert_name" in raw.columns:
        self_mask = raw["expert_name"].astype(str).str.strip().str.lower() == "self"
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


def default_lane(row):
    """Where the solver puts an interview before any manual move."""
    status = str(row.get("task_status", "")).strip().lower()
    if status == "rescheduled":
        return RESCHEDULED
    if status == "cancelled":
        return CANCELLED
    exp = str(row.get("expert_name", "") or "").strip()
    if not exp or exp.lower() in ("nan", "none"):
        return UNASSIGNED
    return exp


def apply_moves(resolved, saved):
    """Add `lane`, `default_lane`, `manual` columns. Saved moves win."""
    df = resolved.copy()
    df["skey"] = [stable_key(r) for _, r in df.iterrows()]
    df["default_lane"] = [default_lane(r) for _, r in df.iterrows()]
    df["lane"] = df["default_lane"]
    df["manual"] = False
    if saved:
        to = df["skey"].map(lambda k: (saved.get(k) or {}).get("to"))
        mask = to.notna()
        df.loc[mask, "lane"] = to[mask]
        df.loc[mask, "manual"] = True
    return df


def clash_keys(df):
    """skeys of bars that overlap another bar in the same EXPERT lane."""
    out = set()
    work = df[~df["lane"].isin(STATUS_KEYS)]
    for _, g in work.groupby("lane"):
        items = g.sort_values("start_min")[["skey", "start_min", "end_min"]].to_dict("records")
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if items[j]["start_min"] >= items[i]["end_min"]:
                    break
                out.add(items[i]["skey"])
                out.add(items[j]["skey"])
    return out


def validate(df, presence_map):
    problems = []
    work = df[~df["lane"].isin(STATUS_KEYS)]
    for expert, grp in work.groupby("lane"):
        items = grp.sort_values("start_min")[["start_min", "end_min",
                                              "candidate_name"]].to_dict("records")
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                s1, e1 = items[a]["start_min"], items[a]["end_min"]
                s2, e2 = items[b]["start_min"], items[b]["end_min"]
                if s1 < e2 and s2 < e1:
                    problems.append(("CLASH", "%s and %s overlap for %s" % (
                        items[a]["candidate_name"], items[b]["candidate_name"], expert)))
                else:
                    gap = min(abs(e1 - s2), abs(e2 - s1))
                    if 0 <= gap < GAP_MINUTES:
                        problems.append(("GAP", "only %d min between %s and %s for %s" % (
                            gap, items[a]["candidate_name"], items[b]["candidate_name"], expert)))
        if presence_map and not is_present(expert, presence_map):
            problems.append(("ABSENT", "%d interview(s) sit with Absent expert %s"
                             % (len(grp), expert)))
    n_un = int((df["lane"] == UNASSIGNED).sum())
    if n_un:
        problems.append(("UNASSIGNED", "%d interview(s) have no expert yet" % n_un))
    return problems


def _bar_style(row):
    """(fill, border, border_width) — same mapping as render_resolved_gantt."""
    lane = row["lane"]
    if lane == RESCHEDULED:
        fill, border, w = STATUS_COLORS["rescheduled"], "white", 1
    elif lane == CANCELLED:
        fill, border, w = STATUS_COLORS["cancelled"], "white", 1
    elif lane == UNASSIGNED:
        fill, border, w = UNASSIGNED_COLOR, "white", 1
    else:
        action = str(row.get("resolution_action", "kept") or "kept")
        if action in ("presence_reassigned", "absent_reassigned"):
            fill, border, w = ABSENT_COLOR, "#d35400", 3
        elif action == "expertise_reassigned":
            fill, border, w = EXPERTISE_COLOR, "#8e44ad", 3
        elif action in ("reassigned", "gap_reassigned"):
            fill, border, w = REASSIGNED_COLOR, "#27ae60", 3
        elif action == "unresolved":
            fill, border, w = UNRESOLVED_COLOR, "#ff0000", 4
        else:
            fill, border, w = STATUS_COLORS.get(str(row.get("task_status", "")), "#95a5a6"), "white", 1
    if bool(row.get("manual")):
        border, w = MANUAL_BORDER, 3
    return fill, border, w


def lane_label(key):
    return STATUS_LABEL.get(key, key)


def _hover(row):
    tag = ""
    if bool(row.get("manual")):
        tag = " | MANUAL MOVE: %s -> %s" % (lane_label(row["default_lane"]), lane_label(row["lane"]))
    return "%s | %s | %s | %s-%s | %s min | %s%s" % (
        row.get("candidate_name", ""), row.get("company_name", ""),
        row.get("round_name", ""), row.get("start_label", ""), row.get("end_label", ""),
        row.get("duration", ""), lane_label(row["lane"]), tag)


def build_lanes(df, all_expert_names):
    """Expert lanes (with interviews first by earliest start, then the rest),
    followed by the three status lanes."""
    experts = df[~df["lane"].isin(STATUS_KEYS)]
    with_items = (experts.groupby("lane")["start_min"].min().sort_values().index.tolist()
                  if not experts.empty else [])
    rest = [e for e in all_expert_names if e not in with_items]
    lanes = [{"key": e, "label": e, "kind": "expert"} for e in with_items + sorted(rest)]
    lanes += [{"key": k, "label": lbl, "kind": "status"} for k, lbl in STATUS_LANES]
    return lanes


def render_planner(df, all_expert_names, selected_date):
    if _draggable_gantt is None:
        st.warning("Drag component not found — expected the folder `draggable_gantt/` "
                   "next to scheduler_app.py.")
        return None
    lanes = build_lanes(df, all_expert_names)
    if df.empty:
        tmin, tmax = 480.0, 1080.0
    else:
        tmin = float(max(0, int(df["start_min"].min()) - 30))
        tmax = float(min(1440, int(df["end_min"].max()) + 30))
    span = max(tmax - tmin, 1.0)
    clashes = clash_keys(df)

    rows = []
    for _, r in df.iterrows():
        fill, border, bw = _bar_style(r)
        wpct = (float(r["end_min"]) - float(r["start_min"])) / span * 100.0
        short = ""
        if wpct >= 6.0:
            short = str(r.get("candidate_name", ""))[:16]
        elif wpct >= 3.5:
            short = str(r.get("candidate_name", ""))[:6]
        rows.append({
            "key": r["skey"], "lane": str(r["lane"]),
            "start_min": float(r["start_min"]), "end_min": float(r["end_min"]),
            "color": fill, "border": border, "border_width": bw,
            "clash": r["skey"] in clashes, "short": short, "hover": _hover(r),
        })

    ticks = []
    t = int(tmin // 30 * 30)
    while t <= tmax:
        ticks.append({"min": t, "label": _minutes_to_label(t)})
        t += 30

    now = datetime.now(EDT)
    now_min = now.hour * 60 + now.minute if selected_date == now.date() else None

    return _draggable_gantt(
        rows=rows, lanes=lanes, legend=LEGEND,
        title="✋ Manual Planner — " + str(selected_date) + " (EDT)",
        tmin=tmin, tmax=tmax, ticks=ticks, now_min=now_min,
        shift_start=SHIFT_START_MIN, shift_end=SHIFT_END_MIN,
        key="drag_gantt", default=None)


def handle_drag(event, df, selected_date):
    """Persist a drag. Returns True when something was saved/removed."""
    if not isinstance(event, dict) or not event.get("key") or not event.get("nonce"):
        return False
    if event["nonce"] == st.session_state.get("drag_nonce"):
        return False                                     # already processed
    st.session_state["drag_nonce"] = event["nonce"]
    key, to = str(event["key"]), str(event.get("to", ""))
    match = df[df["skey"] == key]
    if match.empty or not to:
        return False                                     # interview no longer exists
    if to == str(match.iloc[0]["default_lane"]):
        delete_move(selected_date, key)                  # dropped back where it belongs
    else:
        save_move(selected_date, key, to, str(event.get("from", "")))
    return True


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════
def main():
    # ── live refresh: rerun every 2 minutes; the data cache expires at the same rate
    if HAS_AUTOREFRESH:
        st_autorefresh(interval=REFRESH_SECONDS * 1000, key="live_refresh")

    with st.sidebar:
        st.markdown("### 🗓️ Vizva Scheduler")
        st.caption(APP_VERSION)
        exp_left = max(0, int(st.session_state.get("auth_exp", 0) - time.time()))
        st.caption("Signed in · session ends in %dh %02dm" % (exp_left // 3600, (exp_left % 3600) // 60))
        st.caption("Live data · auto-refresh every %d min" % (REFRESH_SECONDS // 60)
                   if HAS_AUTOREFRESH else
                   "Install `streamlit-autorefresh` for automatic refresh.")
        if st.button("🔄 Refresh now", use_container_width=True):
            load_frames.clear()
            st.rerun()
        if st.button("🚪 Log out", use_container_width=True):
            logout()

    if not API_KEY or not BASE_URL:
        st.error("Set API_KEY and BASE_URL in `.streamlit/secrets.toml`.")
        st.stop()

    all_case_df, active_expert_df = load_frames()
    if all_case_df is None or all_case_df.empty:
        st.warning("No data returned by the API.")
        st.stop()
    st.caption("Data loaded at " + datetime.now(EDT).strftime("%I:%M:%S %p") + " EDT")

    render_schedule_view(all_case_df, active_expert_df)

    # ══════════════════════════════════════════════════════════════
    #  MANUAL PLANNER — today only
    # ══════════════════════════════════════════════════════════════
    st.markdown("---")
    st.header("✋ Manual Planner — drag bars between lanes")
    selected_date = st.session_state.get("schedule_date", date.today())
    today = datetime.now(EDT).date()
    if selected_date != today:
        st.info("The manual planner is available **today only** (%s EDT). You are viewing %s "
                "— pick today's date above to plan lanes." % (today, selected_date))
        return

    st.caption("Drag a bar up or down into another lane — the time never changes. Below the "
               "experts are three status lanes: **Unassigned** (park a task, then drag it to "
               "any expert), **Rescheduled** and **Cancelled**. Overlapping bars stack on "
               "separate rows with a red ring, so nothing hides. Every move is saved for "
               "%d hours." % MOVES_TTL_HOURS)

    sched = build_schedule_data(all_case_df, selected_date)
    if sched is None or sched.empty:
        st.info("No interviews with valid time data on %s." % selected_date)
        return

    EXCLUDE = {"hcr", "self"}
    all_expert_names = sorted({
        str(e).strip() for e in active_expert_df["expert_name"].dropna().unique()
        if str(e).strip() and str(e).strip().lower() not in EXCLUDE
    }) if "expert_name" in active_expert_df.columns else []

    cfg = load_expert_config()
    expertise_map = get_expertise_map(cfg)
    presence_map = cfg.get("presence", {}) or {}
    round_map = cfg.get("round_expertise", {}) or {}
    reallocate_absent = bool(cfg.get("reallocate_absent", False))
    expertise_source = ("owner" if str(cfg.get("task_expertise_source", "round")).lower() == "owner"
                        else "round")

    resolved = solve(sched.reset_index(drop=True), all_expert_names, expertise_map,
                     presence_map, round_map, expertise_source, reallocate_absent)
    resolved = resolved[~resolved["expert_name"].astype(str).str.strip().str.lower()
                        .isin(EXCLUDE)].reset_index(drop=True)

    df = apply_moves(resolved, moves_for(selected_date))

    k = st.columns(5)
    k[0].metric("Interviews", len(df))
    k[1].metric("With an expert", int((~df["lane"].isin(STATUS_KEYS)).sum()))
    k[2].metric("Unassigned", int((df["lane"] == UNASSIGNED).sum()))
    k[3].metric("Rescheduled / Cancelled", int(df["lane"].isin([RESCHEDULED, CANCELLED]).sum()))
    k[4].metric("Saved manual moves", int(df["manual"].sum()))

    event = render_planner(df, all_expert_names, selected_date)
    if handle_drag(event, df, selected_date):
        st.rerun()

    problems = validate(df, presence_map)
    if problems:
        groups = {}
        for kind, msg in problems:
            groups.setdefault(kind, []).append(msg)
        labels = {"CLASH": "Overlapping interviews", "GAP": "10-minute gap rule",
                  "ABSENT": "Absent expert", "UNASSIGNED": "Unassigned interviews"}
        for kind, msgs in groups.items():
            st.warning("**%s** (%d)\n\n%s" % (labels.get(kind, kind), len(msgs),
                                             "\n".join("- " + m for m in msgs[:8])))
    else:
        st.success("No clashes, gap violations, absent-expert or unassigned interviews.")

    a, b = st.columns([1, 1])
    with a:
        if st.button("↩︎ Clear today's moves", use_container_width=True):
            clear_moves(selected_date)
            st.session_state["drag_nonce"] = None
            st.rerun()
    with b:
        st.download_button("⬇️ Saved moves JSON",
                           data=json.dumps(load_moves(), indent=2).encode("utf-8"),
                           file_name="manual_moves.json", mime="application/json",
                           use_container_width=True)
    block = moves_for(selected_date)
    if block:
        st.dataframe(pd.DataFrame([
            {"Interview": key, "From": lane_label(mv.get("from", "")),
             "Moved to": lane_label(mv.get("to", "")), "Saved at (UTC)": mv.get("at")}
            for key, mv in block.items()
        ]), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    if is_authenticated():
        main()
    else:
        login()
