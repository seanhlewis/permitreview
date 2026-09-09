# Permit review — staticver

This folder contains the static permit export and a live copy of the established human-validation webapp. The copied app keeps its guided, blind, narrow, history, timing, keyboard, and reviewer-guide flows. Permit records stay in JSON. Review answers, comments, assignments, and timing records go to `instance/human_eval.sqlite3`.

## Build the export

From this folder:

```powershell
py -3 build_static_export.py
```

The builder reads the current review database to exclude permits that already have a saved review. It writes `data/permits.json` and `data/manifest.json`. The export has 21 category queues with 50 permits each.

## Run locally with live saving

```powershell
py -3 server.py
```

Open <http://127.0.0.1:8873/>. The server also exposes `/api/health` and the isolated showcase demo at <http://127.0.0.1:8873/test>.

Set `STATICVER_HOST=127.0.0.1` for local-only access. The default host is `0.0.0.0` for use behind a tunnel or a controlled network.

## Review coordination

The copied app assigns all 50 permits in each active category to a reviewer, for up to 1,050 targeted permits. Saved answers create active assignments. Each permit accepts two distinct reviewer records. Comments and unfinished selections autosave. Every saved review records elapsed time and timestamps. `/test` uses in-memory fixtures and never writes to the live database.
