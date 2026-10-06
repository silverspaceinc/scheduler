# Vizva Scheduler

A dedicated Streamlit app that renders the dashboard's **Schedule View exactly**
(same code, same UI, same solution) and adds one extra section: a drag board for
moving interview bars **vertically** between expert lanes.

## Identical to Schedule View

`scheduler_core.py` is **not** a rewrite. It was machine-extracted from the
dashboard's `app.py` as a byte-for-byte copy of **55 functions** — including:

| Included | Purpose |
|----------|---------|
| `render_schedule_view` | the whole page |
| `render_expert_config_panel` | **Expert Expertise & Presence** editor |
| `render_schedule_gantt`, `render_resolved_gantt` | both Gantt charts |
| `render_availability_summary` | availability table |
| `render_resolution_summary` | resolution KPIs |
| `enforce_presence_first` … `optimize_expertise_match` | P1–P5 solver |
| `fetch_all_data`, `normalize`, `filter_active_experts` | data pipeline |

The app calls `render_schedule_view(all_case_df, active_expert_df)` — the same
call, with the same two frames, as the dashboard.

## Vertical-only, by construction

The drag board places each **expert as a container** and each interview as a card
(`09:00 AM-09:30 AM | Candidate | Company | Round | #12`). A drag can only change
which container a card sits in. The time is text inside the card, and no code path
writes `start_min` / `end_min` after the solver runs.

## Project layout

```
vizva-scheduler/
├── scheduler_app.py                 # run this
├── scheduler_core.py                # Schedule View, extracted verbatim
├── test_scheduler.py                # solver verification suite
├── requirements.txt
├── README.md
├── .gitignore
└── .streamlit/
    ├── config.toml
    └── secrets.toml.example
```

## Setup

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then edit
streamlit run scheduler_app.py
```

Secrets (same keys as the dashboard): `API_KEY`, `BASE_URL`,
`VIZVA_USERNAME`, `VIZVA_PASSWORD`.

## Deploy

Push to GitHub, create a Streamlit Cloud app with **Main file path** =
`scheduler_app.py`, and paste the four secrets into the app's Secrets box.

## Notes

* The **Expert Expertise & Presence** panel lives inside Schedule View, exactly
  as in the dashboard — expand it to set attendance and expertise
  (Data/Business Analyst, Software Engineer, DevOps/Cyber Security, Unspecified).
* The config is written to `vizva_expert_config.json` next to the app
  (gitignored). On Streamlit Cloud this file is per-container and resets on
  redeploy — use the panel's Download/Restore buttons to keep a copy.
* `presence_map` values are the **strings** `"Present"` / `"Absent"`.
* If `streamlit-sortables` is unavailable, the drag board falls back to a table
  editor with a read-only Time column and an Expert dropdown.
