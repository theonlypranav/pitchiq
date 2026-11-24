"""Elo ratings for national teams, plus a data-driven win/draw/loss calibration.

Rating method follows the World Football Elo system (eloratings.net):
  new = old + K * G * (W - We)
  K  = weight of the tournament (World Cup 60 ... friendly 20)
  G  = goal-difference multiplier (1, 1.5, then (11 + N) / 8)
  W  = 1 win, 0.5 draw (a penalty shootout counts as a draw), 0 loss
  We = 1 / (10 ** (-dr / 400) + 1), dr = rating gap incl. +100 for the home side

Elo gives an expected *score*, not separate draw odds. To turn a rating gap into
win/draw/loss probabilities we bucket every past match by its pre-match rating
gap and count how often the stronger side actually won, drew or lost.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

INITIAL_RATING = 1500.0
HOME_ADVANTAGE = 100.0
BUCKET_WIDTH = 50
MAX_GAP = 700  # gaps beyond this share the last bucket
MIN_BUCKET_SAMPLES = 200
CALIBRATION_START = "1950-01-01"
BACKTEST_START = "2010-01-01"

MAJOR_FINALS = {
    "UEFA Euro", "Copa América", "African Cup of Nations", "AFC Asian Cup",
    "Gold Cup", "CONCACAF Championship", "Confederations Cup", "Oceania Nations Cup",
}

# bucket -> [wins, draws, losses] from the perspective of the side with gap `bucket`
Calibration = dict[int, list[int]]


def k_factor(tournament: str) -> float:
    if tournament == "FIFA World Cup":
        return 60
    if tournament in MAJOR_FINALS:
        return 50
    if "qualification" in tournament or "Nations League" in tournament:
        return 40
    if tournament == "Friendly":
        return 20
    return 30


def goal_multiplier(goal_diff: int) -> float:
    n = abs(goal_diff)
    if n <= 1:
        return 1.0
    if n == 2:
        return 1.5
    return (11 + n) / 8


def expected_score(gap: float) -> float:
    return 1 / (10 ** (-gap / 400) + 1)


def bucket_of(gap: float) -> int:
    clamped = max(-MAX_GAP, min(MAX_GAP, gap))
    return round(clamped / BUCKET_WIDTH) * BUCKET_WIDTH


def record_outcome(cal: Calibration, gap: float, result: float) -> None:
    """Count one match twice, once from each side, so the table is symmetric."""
    idx = {1.0: 0, 0.5: 1, 0.0: 2}[result]
    b = bucket_of(gap)
    cal[b][idx] += 1
    cal[-b][2 - idx] += 1


def probabilities(cal: Calibration, gap: float) -> tuple[float, float, float, int]:
    """(p_win, p_draw, p_loss, sample size) for the side with rating gap `gap`.

    Widens the window of neighbouring buckets until it has enough samples.
    """
    center = bucket_of(gap)
    totals = [0, 0, 0]
    for radius in range(2 * MAX_GAP // BUCKET_WIDTH + 1):
        offsets = {0} if radius == 0 else {-radius, radius}
        for off in offsets:
            counts = cal.get(center + off * BUCKET_WIDTH)
            if counts:
                totals = [t + c for t, c in zip(totals, counts, strict=True)]
        if sum(totals) >= MIN_BUCKET_SAMPLES:
            break
    n = sum(totals)
    if n == 0:
        p = expected_score(gap)
        return p, 0.0, 1 - p, 0
    return totals[0] / n, totals[1] / n, totals[2] / n, n


def match_result(home_score: int, away_score: int) -> float:
    """1 home win, 0.5 draw (shootouts included), 0 away win."""
    if home_score > away_score:
        return 1.0
    if home_score < away_score:
        return 0.0
    return 0.5


@dataclass
class EloRun:
    ratings: dict[str, float] = field(default_factory=lambda: defaultdict(lambda: INITIAL_RATING))
    year_ago: dict[str, float] = field(default_factory=dict)
    played: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_match: dict[str, str] = field(default_factory=dict)
    calibration: Calibration = field(default_factory=lambda: defaultdict(lambda: [0, 0, 0]))
    backtest: dict[str, Any] = field(default_factory=dict)
    # per input match, in input order: (home rating before, away rating before, home change)
    per_match: list[tuple[float, float, float]] = field(default_factory=list)


def run_elo(matches: Iterable[dict[str, Any]]) -> EloRun:
    """Process matches in date order.

    Each match needs: date, home, away, home_score, away_score, tournament, neutral.
    Also back-tests the calibration: fitted on 1950-2009, scored on 2010 onwards.
    """
    matches = list(matches)
    order = sorted(range(len(matches)), key=lambda i: matches[i]["date"])
    run = EloRun()
    if not matches:
        return run
    ordered = [matches[i] for i in order]
    run.per_match = [(0.0, 0.0, 0.0)] * len(matches)
    snapshot_after = (date.fromisoformat(ordered[-1]["date"]) - timedelta(days=365)).isoformat()
    train: Calibration = defaultdict(lambda: [0, 0, 0])
    correct = tested = baseline_correct = 0
    brier_total = 0.0

    for idx, m in zip(order, ordered, strict=True):
        if not run.year_ago and m["date"] > snapshot_after:
            run.year_ago = dict(run.ratings)
        home, away = m["home"], m["away"]
        gap = run.ratings[home] - run.ratings[away] + (0 if m["neutral"] else HOME_ADVANTAGE)
        result = match_result(m["home_score"], m["away_score"])

        if m["date"] >= BACKTEST_START:
            p = probabilities(train, gap)[:3]
            actual = {1.0: 0, 0.5: 1, 0.0: 2}[result]
            correct += int(max(range(3), key=lambda i: p[i]) == actual)
            baseline_correct += int(actual == 0)  # naive: always pick the home side
            brier_total += sum((p[i] - (i == actual)) ** 2 for i in range(3))
            tested += 1
        elif m["date"] >= CALIBRATION_START:
            record_outcome(train, gap, result)
        if m["date"] >= CALIBRATION_START:
            record_outcome(run.calibration, gap, result)

        change = (
            k_factor(m["tournament"])
            * goal_multiplier(m["home_score"] - m["away_score"])
            * (result - expected_score(gap))
        )
        run.per_match[idx] = (run.ratings[home], run.ratings[away], change)
        run.ratings[home] += change
        run.ratings[away] -= change
        for team in (home, away):
            run.played[team] += 1
            run.last_match[team] = m["date"]

    if tested:
        run.backtest = {
            "matches": tested,
            "accuracy": round(correct / tested, 4),
            "baseline_accuracy": round(baseline_correct / tested, 4),
            "brier": round(brier_total / tested, 4),
        }
    return run
