"""Build data/app.db from the raw CSVs in data/raw/.

Tables: `top_scorers` (goalscorers.csv), `matches` (results.csv joined with
shootouts.csv, former team names resolved via former_names.csv),
`former_names`, and `meta`.

Usage: python scripts/build_top_scorers.py [--csv PATH] [--db PATH]
"""

import argparse
import csv
import hashlib
import io
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_DB_PATH, ERAS
from app.elo import EloRun, run_elo

log = logging.getLogger("build")

REQUIRED_COLUMNS = {"date", "team", "scorer", "own_goal", "penalty"}
BOOLS = {"TRUE": True, "FALSE": False}

# (player, team) -> [goals, penalties]
Tally = dict[tuple[str, str], list[int]]


def era_matches(year: int, bounds: tuple[int | None, int | None]) -> bool:
    start, end = bounds
    return (start is None or year >= start) and (end is None or year <= end)


def tally_goals(rows: list[dict[str, str]]) -> tuple[dict[str, Tally], dict[str, int]]:
    """Count goals per (player, team) for every era. Own goals are excluded."""
    tallies: dict[str, Tally] = {era: defaultdict(lambda: [0, 0]) for era in ERAS}
    stats = {"read": 0, "skipped": 0, "own_goals": 0}
    for line_no, row in enumerate(rows, start=2):
        stats["read"] += 1
        scorer = (row.get("scorer") or "").strip()
        team = (row.get("team") or "").strip()
        own_goal = BOOLS.get((row.get("own_goal") or "").strip().upper())
        penalty = BOOLS.get((row.get("penalty") or "").strip().upper())
        try:
            year = date.fromisoformat((row.get("date") or "").strip()).year
        except ValueError:
            year = None
        if not scorer or not team or year is None or own_goal is None or penalty is None:
            stats["skipped"] += 1
            log.warning("skipping line %d: %r", line_no, row)
            continue
        if own_goal:
            stats["own_goals"] += 1
            continue
        for era, bounds in ERAS.items():
            if era_matches(year, bounds):
                counts = tallies[era][(scorer, team)]
                counts[0] += 1
                counts[1] += int(penalty)
    return tallies, stats


def rank_players(tally: Tally) -> list[tuple[int, str, str, int, int]]:
    """Return (rank, player, team, goals, penalties) sorted by goals; ties share a rank."""
    ordered = sorted(tally.items(), key=lambda kv: (-kv[1][0], kv[0][0], kv[0][1]))
    ranked = []
    prev_goals, rank = None, 0
    for position, ((player, team), (goals, pens)) in enumerate(ordered, start=1):
        if goals != prev_goals:
            rank, prev_goals = position, goals
        ranked.append((rank, player, team, goals, pens))
    return ranked


def read_csv(path: str, required: set[str]) -> tuple[bytes, list[dict[str, str]]]:
    raw = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    return raw, list(reader)


# former name -> list of (current name, start date, end date), ISO date strings
Aliases = dict[str, list[tuple[str, str, str]]]


def load_aliases(rows: list[dict[str, str]]) -> Aliases:
    aliases: Aliases = defaultdict(list)
    for row in rows:
        aliases[row["former"].strip()].append(
            (row["current"].strip(), row["start_date"].strip(), row["end_date"].strip())
        )
    return aliases


def canonical_team(name: str, match_date: str, aliases: Aliases) -> str:
    """Map a former team name to its current one if the match falls in that name's era."""
    for current, start, end in aliases.get(name, []):
        if start <= match_date <= end:
            return current
    return name


def build_matches(
    results: list[dict[str, str]], shootouts: list[dict[str, str]], aliases: Aliases
) -> tuple[list[tuple], dict[str, int]]:
    """Join results with shootout winners and resolve former team names."""
    winners = {
        (s["date"].strip(), s["home_team"].strip(), s["away_team"].strip()): s["winner"].strip()
        for s in shootouts
    }
    matches, stats = [], {"read": 0, "skipped": 0}
    for line_no, row in enumerate(results, start=2):
        stats["read"] += 1
        try:
            match_date = date.fromisoformat(row["date"].strip()).isoformat()
            home_score, away_score = int(row["home_score"]), int(row["away_score"])
        except (ValueError, TypeError):
            stats["skipped"] += 1
            log.warning("skipping results line %d: %r", line_no, row)
            continue
        home, away = row["home_team"].strip(), row["away_team"].strip()
        matches.append((
            match_date, home, away, home_score, away_score,
            row["tournament"].strip(), row["city"].strip(), row["country"].strip(),
            int(row["neutral"].strip().upper() == "TRUE"),
            winners.get((match_date, home, away)),
            canonical_team(home, match_date, aliases),
            canonical_team(away, match_date, aliases),
        ))
    return matches, stats


def build_goals(rows: list[dict[str, str]]) -> list[tuple]:
    """Individual goal events (own goals included) for match pages; invalid rows dropped."""
    goals = []
    for row in rows:
        scorer = (row.get("scorer") or "").strip()
        own_goal = BOOLS.get((row.get("own_goal") or "").strip().upper())
        penalty = BOOLS.get((row.get("penalty") or "").strip().upper())
        minute = (row.get("minute") or "").strip()
        if not scorer or own_goal is None or penalty is None:
            continue
        goals.append((
            row["date"].strip(), row["home_team"].strip(), row["away_team"].strip(),
            row["team"].strip(), scorer, int(minute) if minute.isdigit() else None,
            int(own_goal), int(penalty),
        ))
    return goals


def write_db(
    db_path: str,
    ranked: dict[str, list],
    matches: list[tuple],
    former_names: list[dict[str, str]],
    elo: EloRun,
    goals: list[tuple],
    data_version: str,
) -> int:
    """Write to a temp file then swap it in, so readers never see a half-built db."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    conn = sqlite3.connect(tmp_path)
    written = 0
    with conn:
        conn.execute(
            "CREATE TABLE top_scorers (era TEXT NOT NULL, rank INTEGER NOT NULL, "
            "player TEXT NOT NULL, team TEXT NOT NULL, goals INTEGER NOT NULL, "
            "penalties INTEGER NOT NULL)"
        )
        conn.execute("CREATE INDEX idx_top_scorers_era_rank ON top_scorers (era, rank)")
        conn.execute("CREATE INDEX idx_top_scorers_era_player ON top_scorers (era, player)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for era, rows in ranked.items():
            conn.executemany(
                "INSERT INTO top_scorers (era, rank, player, team, goals, penalties) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(era, *r) for r in rows],
            )
            written += len(rows)
        conn.execute(
            "CREATE TABLE matches (date TEXT NOT NULL, home_team TEXT NOT NULL, "
            "away_team TEXT NOT NULL, home_score INTEGER NOT NULL, "
            "away_score INTEGER NOT NULL, tournament TEXT NOT NULL, city TEXT NOT NULL, "
            "country TEXT NOT NULL, neutral INTEGER NOT NULL, shootout_winner TEXT, "
            "home_canonical TEXT NOT NULL, away_canonical TEXT NOT NULL, "
            "home_elo REAL NOT NULL, away_elo REAL NOT NULL, elo_change REAL NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (*m, round(home_elo, 1), round(away_elo, 1), round(change, 2))
                for m, (home_elo, away_elo, change) in zip(matches, elo.per_match, strict=True)
            ],
        )
        conn.execute("CREATE INDEX idx_matches_home ON matches (home_canonical, date)")
        conn.execute("CREATE INDEX idx_matches_away ON matches (away_canonical, date)")
        conn.execute("CREATE INDEX idx_matches_date ON matches (date)")
        conn.execute(
            "CREATE TABLE goals (date TEXT NOT NULL, home_team TEXT NOT NULL, "
            "away_team TEXT NOT NULL, team TEXT NOT NULL, scorer TEXT NOT NULL, "
            "minute INTEGER, own_goal INTEGER NOT NULL, penalty INTEGER NOT NULL)"
        )
        conn.executemany("INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?, ?, ?)", goals)
        conn.execute("CREATE INDEX idx_goals_match ON goals (date, home_team, away_team)")
        conn.execute(
            "CREATE TABLE former_names (current TEXT NOT NULL, former TEXT NOT NULL, "
            "start_date TEXT NOT NULL, end_date TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO former_names VALUES (?, ?, ?, ?)",
            [
                (r["current"].strip(), r["former"].strip(),
                 r["start_date"].strip(), r["end_date"].strip())
                for r in former_names
            ],
        )
        conn.execute(
            "CREATE TABLE elo_ratings (team TEXT PRIMARY KEY, rating REAL NOT NULL, "
            "rating_year_ago REAL, matches INTEGER NOT NULL, last_match TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO elo_ratings VALUES (?, ?, ?, ?, ?)",
            [
                (team, round(rating, 1),
                 round(elo.year_ago[team], 1) if team in elo.year_ago else None,
                 elo.played[team], elo.last_match[team])
                for team, rating in sorted(elo.ratings.items())
            ],
        )
        conn.execute(
            "CREATE TABLE elo_calibration (gap_bucket INTEGER PRIMARY KEY, "
            "wins INTEGER NOT NULL, draws INTEGER NOT NULL, losses INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO elo_calibration VALUES (?, ?, ?, ?)",
            [(b, *counts) for b, counts in sorted(elo.calibration.items())],
        )
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [("data_version", data_version)]
            + [(f"backtest_{k}", str(v)) for k, v in elo.backtest.items()],
        )
    conn.close()
    os.replace(tmp_path, db_path)
    return written


def build(
    csv_path: str,
    db_path: str,
    results_path: str = "data/raw/results.csv",
    shootouts_path: str = "data/raw/shootouts.csv",
    former_names_path: str = "data/raw/former_names.csv",
) -> dict[str, int | float | str]:
    started = time.perf_counter()
    raw, scorer_rows = read_csv(csv_path, REQUIRED_COLUMNS)
    raw_results, results = read_csv(
        results_path, {"date", "home_team", "away_team", "home_score", "away_score",
                       "tournament", "city", "country", "neutral"}
    )
    raw_shootouts, shootouts = read_csv(
        shootouts_path, {"date", "home_team", "away_team", "winner"}
    )
    raw_former, former = read_csv(
        former_names_path, {"current", "former", "start_date", "end_date"}
    )

    tallies, stats = tally_goals(scorer_rows)
    ranked = {era: rank_players(t) for era, t in tallies.items()}
    matches, match_stats = build_matches(results, shootouts, load_aliases(former))
    elo = run_elo(
        {"date": m[0], "home": m[10], "away": m[11], "home_score": m[3],
         "away_score": m[4], "tournament": m[5], "neutral": bool(m[8])}
        for m in matches
    )

    built_at = datetime.now(UTC).isoformat(timespec="seconds")
    content_hash = hashlib.sha256(raw + raw_results + raw_shootouts + raw_former).hexdigest()
    data_version = hashlib.sha256(f"{content_hash}:{built_at}".encode()).hexdigest()[:12]

    written = write_db(
        db_path, ranked, matches, former, elo, build_goals(scorer_rows), data_version
    )
    return {
        **stats,
        "elo_teams": len(elo.ratings),
        "backtest": elo.backtest,
        "matches_read": match_stats["read"],
        "matches_skipped": match_stats["skipped"],
        "matches_written": len(matches),
        "written": written,
        "data_version": data_version,
        "runtime_s": round(time.perf_counter() - started, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="data/raw/goalscorers.csv")
    parser.add_argument("--db", default=os.environ.get("APP_DB_PATH", DEFAULT_DB_PATH))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    s = build(args.csv, args.db)
    print(
        f"rows read: {s['read']}\n"
        f"rows skipped: {s['skipped']}\n"
        f"own goals excluded: {s['own_goals']}\n"
        f"rows written: {s['written']}\n"
        f"matches read: {s['matches_read']}, skipped: {s['matches_skipped']}, "
        f"written: {s['matches_written']}\n"
        f"elo teams rated: {s['elo_teams']}, backtest: {s['backtest']}\n"
        f"data_version: {s['data_version']}\n"
        f"runtime: {s['runtime_s']}s"
    )


if __name__ == "__main__":
    main()
