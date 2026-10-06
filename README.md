# Vizva Scheduler

A dedicated Streamlit app that runs the **same solver** as the Vizva dashboard's
Schedule View and lets you move interview bars **vertically** between expert lanes.

## What it does

* Loads interviews from the same API endpoint as the dashboard (`/api/app-case`).
* Runs the identical five-pass solver, in the same order:

  | Pass | Function | Purpose |
  |------|----------|---------|
  | P1 | `enforce_presence_first` | move tasks off Absent experts |
  | P2 | `resolve_clashes` | split overlapping interviews |
  | P3 | `enforce_gap_policy` | 10-minute minimum gap |
  | P4 | `optimize_expertise_match` | expertise re-alignment |
  | P5 | `apply_presence_first_labels` | label Priority-1 moves |

* Renders the resolved Gantt with the dashboard's own renderer, so colours mean
  the same thing.
* Adds a **drag board**: every expert is a container, every interview is a card.
  Drag a card up or down into another expert's lane.

## Why the solution is identical

`scheduler_core.py` is **not** a rewrite. It was machine-extracted from the
dashboard's `app.py` as a byte-for-byte copy of 36 functions — the solver/render
seeds plus their full call-dependency closure. The logic cannot drift.

## Vertical-only, by construction

The time axis is immutable:

* a drag can only change **which expert container** a card sits in — the Y axis;
* the interview time is **text inside the card**, never editable;
* no code path writes `start_min` / `end_min` after the solver runs.

After each move the app re-runs validation and warns you about any clash, gap
violation (< 10 min) or Absent-expert assignment the move created.

## Project layout

```
vizva-scheduler/
├── scheduler_app.py                 # the app — run this
├── scheduler_core.py                # solver, extracted verbatim from app.py
├── test_scheduler.py                # verification suite (6 checks)
├── requirements.txt
├── README.md
├── .gitignore
└── .streamlit/
    ├── config.toml
    └── secrets.toml.example         # copy to secrets.toml and fill in
```

## Setup

```bash
git clone <your-repo-url>
cd vizva-scheduler
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml with the real values
streamlit run scheduler_app.py
```

### Secrets

Same keys as the dashboard:

| Key | Purpose |
|-----|---------|
| `API_KEY` | sent as the `x-api-key` header on every API call |
| `BASE_URL` | API host |
| `VIZVA_USERNAME` | login username (identical to the dashboard) |
| `VIZVA_PASSWORD` | login password (identical to the dashboard) |

`.streamlit/secrets.toml` is gitignored. Only `secrets.toml.example` is committed.

## Deploy to Streamlit Community Cloud

1. Push this folder to GitHub.
2. Create a new app; set **Main file path** to `scheduler_app.py`.
3. Paste the four secrets into the app's **Secrets** box (Advanced settings).
4. Deploy.

## Tests

```bash
python3 test_scheduler.py
```

Expected:

```
PASS presence: enforce_presence_first cleared the Absent expert
PASS clashes:   0 overlapping interviews after resolution
PASS gap rule:  no pair closer than 10 minutes
PASS vertical-only: expert changed, start/end times byte-identical
PASS deterministic: identical assignment on a second run
PASS empty-day guard
ALL SCHEDULER TESTS PASSED
```

## Notes

* `presence_map` values are the **strings** `"Present"` / `"Absent"` — not booleans.
  Missing/unknown entries are treated as Present.
* A stranded Absent expert after the full pipeline is expected behaviour (a clash
  fix can re-place an interview onto an absent expert); the app surfaces it as a
  warning, exactly like the dashboard.
* If `streamlit-sortables` is unavailable, the app falls back to a `st.data_editor`
  table with a read-only Time column and an Expert dropdown — same vertical-only
  semantics, without drag.
