# PitchIQ

**Live:** https://pitchiq-u7va.onrender.com (free instance: the first load after idle takes ~30 s)

International football analytics over every men's international since 1872: top scorers, team records,
Elo power rankings, a match simulator that predicts win/draw/loss odds, and a random-match explorer with
head-to-head history. FastAPI + SQLite backend, a single vanilla-JS page, no build step.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/build_top_scorers.py      # builds data/app.db from data/raw/*.csv (~1 s)
uvicorn app.main:app --reload            # open http://localhost:8000
```

## Deploy (Render)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/theonlypranav/pitchiq)

`render.yaml` builds the database during the build step and starts uvicorn. Free instances sleep after
inactivity, so the first request after a sleep is slow.

## Architecture

```
Browser ──> FastAPI app (app/main.py)
              ├── /api/scorers, /api/players/search ──> SQLite top_scorers
              ├── /api/teams, /api/matches, /api/matches/{id} ──> SQLite matches + goals
              ├── /api/rankings, /api/simulate, /api/elo/model ──> Elo ratings + calibration (loaded at startup)
              └── / ──> app/static/index.html

Offline: data/raw/*.csv ──> scripts/build_top_scorers.py ──> data/app.db
```

The build script precomputes everything expensive: per-era scorer rankings, the match table (results joined with
shootout winners), each team's Elo rating before every match, and the win/draw/loss calibration table.

## How predictions work

- **Elo ratings** follow the World Football Elo method: `change = K × G × (W − We)`, where K weights the tournament
  (World Cup 60 … friendly 20), G scales with goal margin, and home sides get +100. Penalty shootouts count as draws.
- **Win/draw/loss odds** are the real outcome frequencies of past matches with a similar rating gap (50-point buckets).
- **Likely score** is the most probable Poisson scoreline for the predicted outcome, using recent goals for and against.
- **Backtest:** fitted on 1950–2009 and tested on 16,512 matches from 2010 onwards, it picks the right outcome
  59.4% of the time. Always picking the home team manages 47.8%.

The site's "How Elo works" page has an interactive calculator for all of this.

## Data

[International football results from 1872](https://github.com/martj42/international_results) by Mart Jürisoo
(also on Kaggle as `martj42/international-football-results`), licensed **CC0-1.0**. The four CSVs are committed in
`data/raw/`. Own goals are excluded from player totals; penalties count as goals and are also reported separately.

## Status

Done: data build, read APIs, team/match/simulator/Elo features, dashboard UI. Still to do from the original spec:
in-memory TTL cache with `X-Cache` header, per-IP token-bucket rate limiting, automated tests, load-test numbers.
