# Permit review — staticver

This folder contains the static permit export and its separate live reviewer server. The permit records stay in JSON. Review answers, comments, category assignments, and timing records go to `instance/staticver.sqlite3`. The existing live application and its database are not used.

## Build the export

From this folder:

```powershell
py -3 build_static_export.py
```

The builder reads the current review database only to exclude permits that already have a saved review. It writes `data/permits.json` and `data/manifest.json`. The export has 21 category queues with 50 category permits each when the source inventory supports the requirement. A permit can appear in more than one category when its source evidence supports both categories.

## Run locally with live saving

```powershell
py -3 server.py
```

Open <http://127.0.0.1:8873/>. The server also exposes `/api/health` and the isolated showcase demo at <http://127.0.0.1:8873/test>.

Set `STATICVER_HOST=127.0.0.1` for local-only access. The default host is `0.0.0.0` for use behind a tunnel or a controlled network.

## Review coordination

The first saved answer creates the reviewer’s category assignment. Each category accepts two distinct reviewers. A third reviewer receives a server-side lock response. Each permit also accepts two distinct reviewer records. Comments save as drafts and do not create an assignment. Every answer and draft records elapsed time and timestamps. `/test` uses in-memory fixtures and never writes to the live database.
