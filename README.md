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
              ├── rate limiter middleware (token bucket per client IP, in memory)
              ├── /api/scorers ──> TTL+LRU cache ──miss──> SQLite top_scorers
              ├── /api/players/search ──> SQLite top_scorers
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

## System design features

### 1. Cache in front of `/api/scorers` (`app/cache.py`)
- In-memory TTL cache (default 24 h, `CACHE_TTL_SECONDS`; `0` disables it), capped at 500 entries with LRU eviction
  (`OrderedDict`).
- Key `scorers:{data_version}:{era}:{limit}:{offset}`: rebuilding the data changes `data_version`, so old entries are
  never served.
- Every response carries `X-Cache: HIT|MISS`. Stats: `GET /api/cache/stats`.
- Per-key locks: when 20 requests miss the same key at once, one runs the query and the rest wait and reuse its result.
  Those waiters count as hits.

```bash
curl -i "localhost:8000/api/scorers?era=all&limit=5"   # X-Cache: MISS, then HIT on repeat
```

### 2. Precomputed tables (`scripts/build_top_scorers.py`)
Rankings, Elo history and calibration are built offline in ~1 s, so requests only read indexed rows. The build writes
to a temp file and swaps it in atomically. It is idempotent: same input, same table contents.

### 3. Rate limiting (`app/rate_limit.py`)
- Token bucket per client IP on `/api/*`, except `/api/health`.
- Default 30 requests/minute (`RATE_LIMIT_PER_MINUTE`): a burst of 30, refilling 0.5 tokens/s. `0` disables it.
- Over the limit: `429 {"error": "rate limit exceeded"}` with a `Retry-After` header in seconds.
- Client IP is the first `X-Forwarded-For` value when present (Render's proxy sets it), else the socket address. The
  header can be spoofed if the app is exposed without a proxy.
- Buckets live in memory: they reset on restart and are per process (at most 10,000 clients, least recent dropped).

```bash
for i in $(seq 40); do curl -s -o /dev/null -w "%{http_code} " localhost:8000/api/eras; done   # 30× 200, then 429
```

Every request is logged with method, path, status, duration and `X-Cache`.

### Tradeoffs
- **In-memory cache vs Redis:** zero infrastructure and sub-millisecond hits, but each process has its own copy and
  it empties on restart. With several instances, Redis would give one shared cache.
- **Precomputed tables vs live queries:** ranking 47k goals or replaying 50k matches for Elo per request would be slow.
  Precomputing makes reads trivial. The cost is a rebuild whenever the data changes, which is fine for data that
  updates a few times a year.
- **Per-process rate limiting vs a shared store:** with N instances a client effectively gets N× the limit. A shared
  store (Redis `INCR` / Lua token bucket) fixes that at the cost of a network hop per request.

## Tests

```bash
pytest          # 37 tests, < 1 s
ruff check .
```

Tests use small fixture CSVs in `tests/fixtures/` and a fake clock. They cover:
- **Ranking:** ties share a rank, era boundaries, own goals excluded, penalties counted, blank scorers skipped,
  idempotent builds.
- **Cache:** TTL expiry, LRU eviction, a cold key hit by 20 concurrent requests running exactly one DB query (in unit
  and API tests).
- **Rate limiting:** token refill; 40 requests give 30× 200 then 429 with `Retry-After`; `X-Forwarded-For` handling.
- **Elo:** the formula and the worked example.
- **API:** input validation on every endpoint.

## Load test

`scripts/load_test.py`: 20 concurrent workers for 30 s against `/api/scorers?era=all&limit=50`, run locally on an
Apple M3 with one uvicorn worker and rate limiting off (`RATE_LIMIT_PER_MINUTE=0`, otherwise one IP is capped at
30/min).

| Mode | Requests/s | p50 | p95 | p99 |
|---|---|---|---|---|
| Cache on (`CACHE_TTL_SECONDS=86400`) | **3,540** | 5.2 ms | **6.9 ms** | 11.8 ms |
| Cache off (`CACHE_TTL_SECONDS=0`) | 2,386 | 7.9 ms | 9.1 ms | 15.1 ms |

A single warm-cache request takes ~1.3 ms locally (target: < 50 ms). The SQLite query is already fast thanks to the
`(era, rank)` index, so the cache mainly removes the DB round trip and lock contention (+48% throughput). The deployed
free Render instance answered in ~0.3 s from the dev machine, which is mostly network latency. It wasn't load-tested
because the rate limiter (correctly) caps a single client at 30 requests/minute.

## Future work
- Redis for shared cache and rate-limit state across instances
- Scheduled data refresh
- Auth for admin endpoints
- Live match feeds
