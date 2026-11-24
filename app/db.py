"""SQLite connection helpers and read queries."""

import sqlite3
import unicodedata
from typing import Any


def normalize_name(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace: 'José  Piendibene' -> 'jose piendibene'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("normalize_name", 1, normalize_name, deterministic=True)
    return conn


def get_data_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    return row["value"] if row else None


def get_scorers(
    conn: sqlite3.Connection, era: str, limit: int, offset: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT rank, player, team, goals, penalties FROM top_scorers "
        "WHERE era = ? ORDER BY rank, player, team LIMIT ? OFFSET ?",
        (era, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


MATCH_COLUMNS = (
    "rowid AS id, date, home_team, away_team, home_score, away_score, tournament, city, "
    "country, neutral, shootout_winner, home_canonical, away_canonical, home_elo, "
    "away_elo, elo_change"
)


def _match(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["neutral"] = bool(item["neutral"])
    return item


def list_teams(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT home_canonical AS team FROM matches "
        "UNION SELECT away_canonical FROM matches ORDER BY team"
    ).fetchall()
    return [r["team"] for r in rows]


def get_matches(
    conn: sqlite3.Connection, team: str | None, limit: int | None, offset: int = 0
) -> list[dict[str, Any]]:
    """Most recent matches first, optionally for one team (current or former names)."""
    where, params = "", []
    if team is not None:
        where = "WHERE home_canonical = ? OR away_canonical = ?"
        params = [team, team]
    rows = conn.execute(
        f"SELECT {MATCH_COLUMNS} FROM matches {where} "
        "ORDER BY date DESC, home_team LIMIT ? OFFSET ?",
        (*params, -1 if limit is None else limit, offset),
    ).fetchall()
    return [_match(r) for r in rows]


def get_former_names(conn: sqlite3.Connection, team: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT former AS name, start_date, end_date FROM former_names "
        "WHERE current = ? ORDER BY start_date",
        (team,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_team_scorers(conn: sqlite3.Connection, team: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT player, goals, penalties FROM top_scorers "
        "WHERE era = 'all' AND team = ? ORDER BY goals DESC, player LIMIT ?",
        (team, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_head_to_head(
    conn: sqlite3.Connection, team_a: str, team_b: str, limit: int | None = None
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {MATCH_COLUMNS} FROM matches "
        "WHERE (home_canonical = ? AND away_canonical = ?) "
        "OR (home_canonical = ? AND away_canonical = ?) "
        "ORDER BY date DESC LIMIT ?",
        (team_a, team_b, team_b, team_a, -1 if limit is None else limit),
    ).fetchall()
    return [_match(r) for r in rows]


def get_match(conn: sqlite3.Connection, match_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {MATCH_COLUMNS} FROM matches WHERE rowid = ?", (match_id,)
    ).fetchone()
    return _match(row) if row else None


def random_match_id(conn: sqlite3.Connection) -> int:
    """Uniformly random match: skip a random number of rows."""
    row = conn.execute(
        "SELECT rowid FROM matches LIMIT 1 OFFSET abs(random()) % (SELECT COUNT(*) FROM matches)"
    ).fetchone()
    return row["rowid"]


def get_goals(conn: sqlite3.Connection, match: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT team, scorer, minute, own_goal, penalty FROM goals "
        "WHERE date = ? AND home_team = ? AND away_team = ? ORDER BY minute",
        (match["date"], match["home_team"], match["away_team"]),
    ).fetchall()
    return [
        {**dict(r), "own_goal": bool(r["own_goal"]), "penalty": bool(r["penalty"])}
        for r in rows
    ]


def get_elo_ratings(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT team, rating, rating_year_ago, matches, last_match FROM elo_ratings"
    ).fetchall()
    return {r["team"]: dict(r) for r in rows}


def get_calibration(conn: sqlite3.Connection) -> dict[int, list[int]]:
    rows = conn.execute("SELECT gap_bucket, wins, draws, losses FROM elo_calibration").fetchall()
    return {r["gap_bucket"]: [r["wins"], r["draws"], r["losses"]] for r in rows}


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}


def get_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    totals = conn.execute(
        "SELECT COUNT(*) AS matches, SUM(home_score + away_score) AS goals, "
        "COUNT(DISTINCT tournament) AS tournaments, MIN(date) AS first_match, "
        "MAX(date) AS last_match FROM matches"
    ).fetchone()
    by_year = conn.execute(
        "SELECT CAST(substr(date, 1, 4) AS INTEGER) AS year, COUNT(*) AS matches, "
        "SUM(home_score + away_score) AS goals FROM matches GROUP BY year ORDER BY year"
    ).fetchall()
    return {**dict(totals), "goals_by_year": [dict(r) for r in by_year]}


def search_players(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    """Accent- and case-insensitive substring search over the all-time ranking."""
    escaped = normalize_name(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT rank, player, team, goals, penalties FROM top_scorers "
        "WHERE era = 'all' AND normalize_name(player) LIKE ? ESCAPE '\\' "
        "ORDER BY goals DESC, player LIMIT ?",
        (f"%{escaped}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]
