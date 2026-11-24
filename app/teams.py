"""Pure team-record logic over match rows (no database access)."""

from typing import Any


def summarize(team: str, matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Win/draw/loss record for `team` from matches keyed by canonical team names.

    A match decided by a shootout counts as a draw; the shootout is tallied separately.
    """
    record = {
        "played": 0, "won": 0, "drawn": 0, "lost": 0,
        "goals_for": 0, "goals_against": 0,
        "shootouts_won": 0, "shootouts_lost": 0,
        "first_match": None, "last_match": None,
    }
    for m in matches:
        if m["home_canonical"] == team:
            scored, conceded, played_as = m["home_score"], m["away_score"], m["home_team"]
        elif m["away_canonical"] == team:
            scored, conceded, played_as = m["away_score"], m["home_score"], m["away_team"]
        else:
            continue
        record["played"] += 1
        record["goals_for"] += scored
        record["goals_against"] += conceded
        if scored > conceded:
            record["won"] += 1
        elif scored < conceded:
            record["lost"] += 1
        else:
            record["drawn"] += 1
        if m["shootout_winner"]:
            key = "shootouts_won" if m["shootout_winner"] == played_as else "shootouts_lost"
            record[key] += 1
        if record["first_match"] is None or m["date"] < record["first_match"]:
            record["first_match"] = m["date"]
        if record["last_match"] is None or m["date"] > record["last_match"]:
            record["last_match"] = m["date"]
    return record
