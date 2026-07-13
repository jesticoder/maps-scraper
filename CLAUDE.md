# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A German-language lead-gen tool for a web agency: it finds local businesses (currently Handwerker/tradespeople in Hannover) via the Google Places API, analyzes whether their website is missing, dead, outdated, or otherwise weak, and scores each business as a sales lead for a website rebuild pitch. Output feeds `dashboard.html`, a standalone client-side viewer.

Two independent pieces, no build system, no package manifest (no `requirements.txt`/`pyproject.toml`):
- `scraper.py` — the whole pipeline (search → analyze → score → save)
- `dashboard.html` — a single self-contained HTML file (inline CSS/JS, custom CSV parser, no CDN deps) that you drag a `leads_*.csv` into

## Running the scraper

```bash
python3 -m venv .venv && source .venv/bin/activate   # if recreating: existing .venv/ in this repo is Windows-built (Lib/Scripts layout) and won't run on Linux/macOS
pip install python-dotenv
python3 scraper.py
```

Requires `GOOGLE_PLACES_API_KEY` in `.env` (see `.env.example`). **Without a key, or with the literal placeholder value, `scraper.py` silently falls back to fabricated demo data** (`_demo_daten`) — check `GOOGLE_PLACES_API_KEY` in `.env` before assuming a run is real. A live key means every run spends real Google Places API quota (Text Search + Details calls), so don't run the full pipeline speculatively.

There is no test suite, linter, or CI config in this repo.

## Architecture (`scraper.py`)

Single-file pipeline, run per-`branche` (trade/category) from the `BRANCHEN` list, sequentially, against one hardcoded `ZIELSTADT` (target city):

1. `suche_google_places()` — Google Places Text Search, up to 3 pages (60 results) per branche/city query. Falls back to `_demo_daten()` when no real API key is set.
2. `verarbeite_place()` — per-result, runs in a `ThreadPoolExecutor` (`MAX_WORKERS`, default 10) across all results for a branche. Fetches Place Details (phone/website) if missing, then calls `analysiere_website()`.
3. `analysiere_website()` — for a given business website, runs an HTTP reachability/SSL/load-time/mobile-viewport/on-page-SEO/email check, a Wayback Machine age lookup, and a PageSpeed Insights (Lighthouse SEO+performance) check via `pruefe_pagespeed()` **concurrently** (inner `ThreadPoolExecutor(max_workers=3)`) and merges the results. PageSpeed is the slowest of the three (~3-10s per site) and toggleable via `PRUEFE_PAGESPEED`; on-page SEO signals (title/meta-description presence, `noindex`, thin content, email) are parsed for free from the HTML already fetched for the reachability check — no extra request.
4. `berechne_score()` — turns the `Lead` dataclass fields into a 0–100 score with human-readable reasons (`score_gruende`): website existence/age/speed/SSL/mobile-friendliness, SEO signals (only scored when the site is reachable — `noindex` alone is +20, the single highest-weighted signal, since a fast modern site Google is told to ignore is a stronger pitch than a merely outdated one), and Google Business Profile completeness (photo count via `gbp_fotos_anzahl`, which is `None` when the field was never fetched vs. `0` when confirmed empty — don't collapse that distinction). `HOT_LEAD_SCORE` (default 70) is the hot-lead cutoff used in the summary and dashboard.
5. `verarbeite_branche()` runs steps 1–4 for one branche and prints a live per-lead progress line; `main()` loops over all `BRANCHEN`, calling `speichere_leads()` (writes both `OUTPUT_FILE` CSV and `OUTPUT_JSON`, sorted by score, overwritten after *each* branche as a checkpoint) after every branche — so a killed/interrupted run still leaves usable partial output, and `Ctrl+C` triggers an explicit save-and-exit via `KeyboardInterrupt`.

Known behavior to be aware of when modifying: **the same business can appear multiple times** in the output if it matches more than one `branche` in the Places text search (e.g., a plumber/heating business matching both "Klempner" and "Heizungsbauer") — there is no cross-branche dedupe by `place_id`. Each occurrence re-runs the full website analysis independently.

## Architecture (`dashboard.html`)

Pure client-side, no server, no build step. User drags/selects a CSV matching the `speichere_leads()` output schema (see `fieldnames` in `scraper.py`); `parseCSV()` parses it in-browser, `score_gruende` is un-joined from its `" | "`-delimited string back into a list. All filtering/sorting/detail-panel logic and the CSV re-export live in inline `<script>`. Keep field names in sync between `Lead` (in `scraper.py`) and the column list dashboard.html expects (search for `hat_website`, `website_erreichbar`, `score_gruende` handling) if you change the `Lead` dataclass.

## Config knobs (top of `scraper.py`)

`BRANCHEN` (trade list), `ZIELSTADT` (city), `HOT_LEAD_SCORE`, `MAX_WORKERS`, `PRUEFE_PAGESPEED`, `PAGESPEED_API_KEY` (falls back to `GOOGLE_PLACES_API_KEY`), `MIN_WOERTER_CONTENT` — all plain module-level constants, no CLI args or config file. `OUTPUT_FILE`/`OUTPUT_JSON` are derived from `ZIELSTADT` (e.g. `leads_hannover.csv`) so switching cities doesn't overwrite a previous city's output — don't hardcode these back to a fixed filename.

## Git workflow

Solo project, one contributor, no feature branches — commit straight to `main`. Push to `origin` (github.com/jesticoder/maps-scraper, SSH remote — `git@github.com:jesticoder/maps-scraper.git`) right after any commit that represents a complete, working change, without asking for confirmation each time — the user explicitly authorized this standing behavior. To undo a bad push, use `git revert <commit>` (adds a new commit, safe post-push); don't use `git reset --hard` + force-push unless the user explicitly asks for it in the moment.

`origin` was originally HTTPS with no credential helper configured, which made `git push` fail (`could not read Username`). It's now set to SSH, and a working `id_ed25519` key for this account is already in `~/.ssh` — if push ever fails with an auth error again, check `git remote -v` hasn't drifted back to HTTPS before troubleshooting further.
