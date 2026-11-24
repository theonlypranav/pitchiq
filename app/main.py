"""FastAPI app: routes and middleware wiring."""

import logging
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app import db, simulator
from app import elo as elo_lib
from app.cache import TTLCache
from app.config import CACHE_MAX_ENTRIES, ERAS, Settings, load_settings
from app.rate_limit import RateLimiter, client_ip
from app.teams import summarize

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pitchiq.request")


def create_app(
    settings: Settings | None = None, clock: Callable[[], float] = time.monotonic
) -> FastAPI:
    """`clock` drives cache expiry and token refill; tests pass a fake one."""
    settings = settings or load_settings()
    app = FastAPI(title="PitchIQ API")
    app.state.settings = settings
    cache = TTLCache(CACHE_MAX_ENTRIES, settings.cache_ttl_seconds, clock)
    limiter = (
        RateLimiter(settings.rate_limit_per_minute, clock)
        if settings.rate_limit_per_minute > 0 else None
    )
    app.state.cache = cache

    @app.middleware("http")
    async def rate_limit_and_log(request: Request, call_next: Callable) -> Any:
        started = time.perf_counter()
        path = request.url.path
        if limiter and path.startswith("/api/") and path != "/api/health":
            wait = limiter.check(client_ip(request))
            if wait > 0:
                response = JSONResponse(
                    {"error": "rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(math.ceil(wait))},
                )
                _log(request, response, started)
                return response
        response = await call_next(request)
        _log(request, response, started)
        return response

    if not Path(settings.db_path).exists():
        raise RuntimeError(
            f"Database not found at {settings.db_path}. "
            "Run: python scripts/build_top_scorers.py"
        )
    conn = db.connect(settings.db_path)
    conn_lock = threading.Lock()  # one shared connection; serialize access
    data_version = db.get_data_version(conn)

    def error(status: int, message: str, **extra: Any) -> JSONResponse:
        return JSONResponse({"error": message, **extra}, status_code=status)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str | None]:
        return {"status": "ok", "data_version": data_version}

    @app.get("/api/eras")
    def eras() -> dict[str, list[str]]:
        return {"eras": list(ERAS)}

    @app.get("/api/scorers", response_model=None)
    def scorers(
        era: str = "all",
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any] | JSONResponse:
        if era not in ERAS:
            return error(400, "invalid era", valid=list(ERAS))

        def load() -> dict[str, Any]:
            with conn_lock:
                items = db.get_scorers(conn, era, limit, offset)
            return {"era": era, "data_version": data_version, "items": items}

        # data_version in the key: a rebuilt database never serves stale entries.
        body, hit = cache.get_or_load(f"scorers:{data_version}:{era}:{limit}:{offset}", load)
        return JSONResponse(body, headers={"X-Cache": "HIT" if hit else "MISS"})

    @app.get("/api/cache/stats")
    def cache_stats() -> dict[str, Any]:
        return cache.stats()

    @app.get("/api/players/search", response_model=None)
    def search(
        q: str = Query("", max_length=100),
        limit: int = Query(10, ge=1, le=100),
    ) -> dict[str, Any] | JSONResponse:
        if len(q.strip()) < 2:
            return error(400, "query must be at least 2 characters")
        with conn_lock:
            items = db.search_players(conn, q, limit)
        return {"query": q, "data_version": data_version, "items": items}

    # Match/team endpoints read SQLite directly; they are not behind the cache.
    with conn_lock:
        known_teams = db.list_teams(conn)
    team_set = set(known_teams)

    @app.get("/api/teams")
    def teams() -> dict[str, list[str]]:
        return {"teams": known_teams}

    @app.get("/api/teams/stats", response_model=None)
    def team_stats(team: str = Query(..., max_length=100)) -> dict[str, Any] | JSONResponse:
        if team not in team_set:
            return error(404, "unknown team")
        with conn_lock:
            matches = db.get_matches(conn, team, limit=None)
            former = db.get_former_names(conn, team)
            top = db.get_team_scorers(conn, team, limit=5)
        return {
            "team": team,
            "data_version": data_version,
            "former_names": former,
            "record": summarize(team, matches),
            "top_scorers": top,
        }

    @app.get("/api/matches", response_model=None)
    def matches(
        team: str | None = Query(None, max_length=100),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any] | JSONResponse:
        if team is not None and team not in team_set:
            return error(404, "unknown team")
        with conn_lock:
            items = db.get_matches(conn, team, limit, offset)
        return {"team": team, "data_version": data_version, "items": items}

    # Elo ratings, calibration and overview totals are fixed for a given
    # data_version, so they are loaded once at startup.
    with conn_lock:
        elo = db.get_elo_ratings(conn)
        calibration = db.get_calibration(conn)
        meta = db.get_meta(conn)
        overview = db.get_overview(conn)
    backtest = {
        k.removeprefix("backtest_"): int(v) if v.isdigit() else float(v)
        for k, v in meta.items()
        if k.startswith("backtest_")
    }
    # "Active" = played within 4 years of the latest match in the dataset.
    active_since = f"{int(overview['last_match'][:4]) - 4}{overview['last_match'][4:]}"
    ranked = sorted(
        (e for e in elo.values() if e["last_match"] >= active_since),
        key=lambda e: -e["rating"],
    )
    rankings = [
        {
            "rank": i, "team": e["team"], "rating": e["rating"],
            "change_1y": round(e["rating"] - e["rating_year_ago"], 1)
            if e["rating_year_ago"] is not None else None,
            "matches": e["matches"],
        }
        for i, e in enumerate(ranked, start=1)
    ]
    rank_of = {r["team"]: r["rank"] for r in rankings}

    @app.get("/api/overview")
    def get_overview() -> dict[str, Any]:
        return {
            "data_version": data_version,
            **overview,
            "teams": len(known_teams),
            "active_teams": len(rankings),
            "model_backtest": backtest,
        }

    @app.get("/api/rankings")
    def get_rankings(limit: int = Query(20, ge=1, le=400)) -> dict[str, Any]:
        return {"data_version": data_version, "items": rankings[:limit]}

    @app.get("/api/elo/model")
    def elo_model() -> dict[str, Any]:
        return {
            "initial_rating": elo_lib.INITIAL_RATING,
            "home_advantage": elo_lib.HOME_ADVANTAGE,
            "bucket_width": elo_lib.BUCKET_WIDTH,
            "min_bucket_samples": elo_lib.MIN_BUCKET_SAMPLES,
            "max_gap": elo_lib.MAX_GAP,
            "ratings": {team: e["rating"] for team, e in elo.items()},
            "calibration": [
                {"gap": b, "wins": c[0], "draws": c[1], "losses": c[2]}
                for b, c in sorted(calibration.items())
            ],
            "backtest": backtest,
        }

    @app.get("/api/matches/random")
    def random_match() -> dict[str, int]:
        with conn_lock:
            return {"id": db.random_match_id(conn)}

    @app.get("/api/matches/{match_id}", response_model=None)
    def match_detail(match_id: int) -> dict[str, Any] | JSONResponse:
        with conn_lock:
            match = db.get_match(conn, match_id)
            if match is None:
                return error(404, "match not found")
            goals = db.get_goals(conn, match)
            meetings = db.get_head_to_head(
                conn, match["home_canonical"], match["away_canonical"]
            )
        home, away = match["home_canonical"], match["away_canonical"]
        before = [m for m in meetings if m["date"] < match["date"]]
        after = [m for m in meetings if m["date"] > match["date"]]
        gap = match["home_elo"] - match["away_elo"] + (
            0 if match["neutral"] else elo_lib.HOME_ADVANTAGE
        )
        p_home, p_draw, p_away, samples = elo_lib.probabilities(calibration, gap)
        result = elo_lib.match_result(match["home_score"], match["away_score"])
        winner_prob = {1.0: p_home, 0.5: p_draw, 0.0: p_away}[result]
        return {
            "data_version": data_version,
            "match": match,
            "goals": goals,
            "elo": {
                "home_before": match["home_elo"],
                "away_before": match["away_elo"],
                "gap": round(gap, 1),
                "expected_home": round(elo_lib.expected_score(gap), 4),
                "home_change": match["elo_change"],
                "p_home": round(p_home, 4),
                "p_draw": round(p_draw, 4),
                "p_away": round(p_away, 4),
                "calibration_samples": samples,
                "result_probability": round(winner_prob, 4),
                "upset": result != 0.5 and winner_prob < 0.3,
            },
            "head_to_head": {
                "before": simulator.head_to_head(home, away, before),
                "all_time": simulator.head_to_head(home, away, meetings),
                "previous": before[:5],
                "next": after[-1] if after else None,
            },
        }

    @app.get("/api/simulate", response_model=None)
    def simulate(
        team_a: str = Query(..., max_length=100),
        team_b: str = Query(..., max_length=100),
        venue: str = Query("neutral", pattern="^(neutral|a|b)$"),
    ) -> dict[str, Any] | JSONResponse:
        for t in (team_a, team_b):
            if t not in team_set:
                return error(404, f"unknown team: {t}")
        if team_a == team_b:
            return error(400, "pick two different teams")
        with conn_lock:
            history = {t: db.get_matches(conn, t, limit=None) for t in (team_a, team_b)}
            meetings = db.get_head_to_head(conn, team_a, team_b)

        def profile(team: str) -> dict[str, Any]:
            matches = history[team]
            return {
                "team": team,
                "rating": elo[team]["rating"],
                "rank": rank_of.get(team),
                "record": summarize(team, matches),
                "form": simulator.form(team, matches[: simulator.FORM_MATCHES]),
                "scoring": simulator.form(team, matches[: simulator.SCORE_MATCHES]),
            }

        a, b = profile(team_a), profile(team_b)
        return {
            "data_version": data_version,
            "venue": venue,
            "team_a": a,
            "team_b": b,
            "head_to_head": {
                **simulator.head_to_head(team_a, team_b, meetings),
                "recent": meetings[:5],
            },
            "prediction": simulator.predict(
                a["rating"], b["rating"], venue, calibration, a["scoring"], b["scoring"]
            ),
            "model_backtest": backtest,
        }

    return app


def _log(request: Request, response: Any, started: float) -> None:
    log.info(
        "%s %s %d %.1fms cache=%s",
        request.method,
        request.url.path,
        response.status_code,
        (time.perf_counter() - started) * 1000,
        response.headers.get("X-Cache", "-"),
    )


app = create_app()
