import pytest

from app import elo, simulator
from app.teams import summarize


def test_expected_score_is_symmetric() -> None:
    assert elo.expected_score(0) == 0.5
    assert elo.expected_score(200) + elo.expected_score(-200) == pytest.approx(1)
    assert elo.expected_score(200) == pytest.approx(0.76, abs=0.001)


def test_weights() -> None:
    assert elo.k_factor("FIFA World Cup") == 60
    assert elo.k_factor("UEFA Euro") == 50
    assert elo.k_factor("UEFA Euro qualification") == 40
    assert elo.k_factor("Friendly") == 20
    assert elo.k_factor("King's Cup") == 30
    assert [elo.goal_multiplier(d) for d in (0, 1, 2, 3, -4)] == [1, 1, 1.5, 1.75, 1.875]


def test_worked_example_from_the_guide() -> None:
    run = elo.run_elo([
        {"date": "2000-01-01", "home": "A", "away": "X", "home_score": 9, "away_score": 0,
         "tournament": "Friendly", "neutral": True},
    ])
    # One rating change, zero sum.
    assert run.ratings["A"] + run.ratings["X"] == pytest.approx(3000)
    change = 60 * 1.5 * (1 - elo.expected_score(200))
    assert change == pytest.approx(21.6, abs=0.05)


def test_shootout_counts_as_draw() -> None:
    run = elo.run_elo([
        {"date": "2000-01-01", "home": "A", "away": "B", "home_score": 1, "away_score": 1,
         "tournament": "Friendly", "neutral": True},
    ])
    assert run.ratings["A"] == pytest.approx(1500)
    assert run.per_match == [(1500, 1500, 0)]


def test_probabilities_sum_to_one_and_widen_window() -> None:
    cal = {0: [10, 5, 10], 100: [300, 100, 50], -100: [50, 100, 300]}
    p_win, p_draw, p_loss, n = elo.probabilities(cal, 100)
    assert p_win + p_draw + p_loss == pytest.approx(1)
    assert n == 450
    *_, n_wide = elo.probabilities(cal, 0)    # 25 samples < 200, so neighbours are added
    assert n_wide == 925


def test_likely_score_agrees_with_outcome() -> None:
    assert simulator.likely_score(0.8, 2.5, "a")[0] > simulator.likely_score(0.8, 2.5, "a")[1]
    a, b = simulator.likely_score(2.0, 2.0, "draw")
    assert a == b


def test_team_summary_counts_shootouts_as_draws() -> None:
    matches = [
        {"date": "2001-01-01", "home_team": "A", "away_team": "B", "home_canonical": "A",
         "away_canonical": "B", "home_score": 1, "away_score": 1, "shootout_winner": "A"},
        {"date": "2002-01-01", "home_team": "C", "away_team": "A", "home_canonical": "C",
         "away_canonical": "A", "home_score": 3, "away_score": 0, "shootout_winner": None},
    ]
    r = summarize("A", matches)
    assert (r["played"], r["won"], r["drawn"], r["lost"]) == (2, 0, 1, 1)
    assert (r["goals_for"], r["goals_against"], r["shootouts_won"]) == (1, 4, 1)
    assert (r["first_match"], r["last_match"]) == ("2001-01-01", "2002-01-01")
