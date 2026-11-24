"""Pure matchup logic: recent form, head-to-head, and a predicted result."""

import math
from typing import Any

from app.elo import HOME_ADVANTAGE, Calibration, probabilities

FORM_MATCHES = 10
SCORE_MATCHES = 20
MAX_GOALS = 7


def side_of(match: dict[str, Any], team: str) -> tuple[int, int] | None:
    """(goals for, goals against) for `team`, or None if it didn't play."""
    if match["home_canonical"] == team:
        return match["home_score"], match["away_score"]
    if match["away_canonical"] == team:
        return match["away_score"], match["home_score"]
    return None


def form(team: str, recent: list[dict[str, Any]]) -> dict[str, Any]:
    """Results of the most recent matches (newest first)."""
    results, points, gf, ga = [], 0, 0, 0
    for m in recent:
        scored, conceded = side_of(m, team) or (0, 0)
        gf, ga = gf + scored, ga + conceded
        if scored > conceded:
            results.append("W")
            points += 3
        elif scored < conceded:
            results.append("L")
        else:
            results.append("D")
            points += 1
    n = len(recent) or 1
    return {
        "results": results,
        "points_per_game": round(points / n, 2),
        "goals_for_per_game": round(gf / n, 2),
        "goals_against_per_game": round(ga / n, 2),
    }


def head_to_head(team_a: str, team_b: str, meetings: list[dict[str, Any]]) -> dict[str, Any]:
    record = {"played": len(meetings), "a_wins": 0, "draws": 0, "b_wins": 0,
              "a_goals": 0, "b_goals": 0}
    for m in meetings:
        a_goals, b_goals = side_of(m, team_a) or (0, 0)
        record["a_goals"] += a_goals
        record["b_goals"] += b_goals
        if a_goals > b_goals:
            record["a_wins"] += 1
        elif a_goals < b_goals:
            record["b_wins"] += 1
        else:
            record["draws"] += 1
    return record


def venue_gap(rating_a: float, rating_b: float, venue: str) -> float:
    """Rating gap from team A's side; `venue` is 'neutral', 'a' (A at home) or 'b'."""
    bonus = {"neutral": 0.0, "a": HOME_ADVANTAGE, "b": -HOME_ADVANTAGE}[venue]
    return rating_a - rating_b + bonus


def poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def likely_score(lam_a: float, lam_b: float, outcome: str) -> tuple[int, int]:
    """Most probable scoreline (independent Poisson goals) that agrees with `outcome`."""
    allowed = {"a": lambda a, b: a > b, "draw": lambda a, b: a == b, "b": lambda a, b: a < b}
    best, best_p = (0, 0), -1.0
    for a in range(MAX_GOALS + 1):
        for b in range(MAX_GOALS + 1):
            if allowed[outcome](a, b):
                p = poisson(a, lam_a) * poisson(b, lam_b)
                if p > best_p:
                    best, best_p = (a, b), p
    return best


def predict(
    rating_a: float,
    rating_b: float,
    venue: str,
    calibration: Calibration,
    scoring_a: dict[str, float],
    scoring_b: dict[str, float],
) -> dict[str, Any]:
    """Win/draw/loss odds from the Elo gap, plus a likely score from recent scoring.

    Expected goals for A = average of A's recent goals scored and B's recent goals
    conceded per game (and the mirror for B).
    """
    gap = venue_gap(rating_a, rating_b, venue)
    p_a, p_draw, p_b, samples = probabilities(calibration, gap)
    outcome = max((("a", p_a), ("draw", p_draw), ("b", p_b)), key=lambda kv: kv[1])[0]
    lam_a = max(0.2, (scoring_a["goals_for_per_game"] + scoring_b["goals_against_per_game"]) / 2)
    lam_b = max(0.2, (scoring_b["goals_for_per_game"] + scoring_a["goals_against_per_game"]) / 2)
    score = likely_score(lam_a, lam_b, outcome)
    return {
        "rating_gap": round(gap, 1),
        "p_a_win": round(p_a, 4),
        "p_draw": round(p_draw, 4),
        "p_b_win": round(p_b, 4),
        "outcome": outcome,
        "expected_goals": [round(lam_a, 2), round(lam_b, 2)],
        "likely_score": list(score),
        "calibration_samples": samples,
    }
