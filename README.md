# Permit review — staticver

This folder is a self-contained static copy of the permit categorization reviewer. It contains a JSON export and a browser-only reviewer UI. It does not include the live Flask application or `audit.sqlite3`.

## Build the export

From this folder:

```powershell
py -3 build_static_export.py
```

The builder reads the current review database only to exclude permits that already have a saved review. It writes `data/permits.json` and `data/manifest.json`. The export has 21 category queues with 50 category permits each when the source inventory supports the requirement. A permit can appear in more than one category when its source evidence supports both categories.

## Run locally

```powershell
py -3 -m http.server 8877
```

Open <http://127.0.0.1:8877/>. The page must be served over HTTP because browsers block `fetch()` from a `file:` URL.

## Coordination boundary

GitHub Pages can host the page and data, but it cannot provide shared reviewer state. This version saves answers, comments, and two-slot permit coordination in each browser's `localStorage`. Reviewer initials are visible only to other people using that same browser profile. A real shared lock and shared answer history still require the existing backend or another shared datastore. No GitHub credential or token is included in this site.
