import os
import sqlite3

import pytest

from scripts.build_top_scorers import canonical_team, era_matches, rank_players
from tests.conftest import FIXTURES, build_fixture_db


def rows(db_path: str, era: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT rank, player, team, goals, penalties FROM top_scorers "
            "WHERE era = ? ORDER BY rank, player", (era,)
        ).fetchall()


def test_ties_share_a_rank() -> None:
    tally = {("A", "X"): [5, 0], ("B", "Y"): [3, 1], ("C", "Z"): [3, 0], ("D", "X"): [1, 0]}
    assert [r[0] for r in rank_players(tally)] == [1, 2, 2, 4]


def test_era_boundaries_are_inclusive() -> None:
    assert era_matches(1999, (1950, 1999))
    assert not era_matches(2000, (1950, 1999))
    assert era_matches(2000, (2000, 2026))
    assert era_matches(1872, (None, None))


def test_former_name_maps_only_inside_its_dates() -> None:
    aliases = {"Old Delta": [("Delta", "1990-01-01", "2002-12-31")]}
    assert canonical_team("Old Delta", "2002-07-07", aliases) == "Delta"
    assert canonical_team("Old Delta", "1980-01-01", aliases) == "Old Delta"


def test_build_counts_and_rankings(tmp_path) -> None:
    db_path = str(tmp_path / "app.db")
    summary = build_fixture_db(db_path)
    assert summary["read"] == 9
    assert summary["skipped"] == 1       # blank scorer
    assert summary["own_goals"] == 1     # excluded from totals
    assert summary["written"] == 8
    # Own goal by Bob Bolt is not counted; Ann's penalty counts as a goal and a penalty.
    assert rows(db_path, "all") == [
        (1, "Ann Ace", "Alpha", 2, 1),
        (1, "Bob Bolt", "Beta", 2, 0),
        (1, "José Núñez", "Gamma", 2, 0),
        (4, "Old Timer", "Alpha", 1, 0),
    ]
    assert rows(db_path, "1950-1999") == [(1, "Ann Ace", "Alpha", 2, 1), (2, "Bob Bolt", "Beta", 1, 0)]
    assert rows(db_path, "2000-2026") == [(1, "José Núñez", "Gamma", 2, 0), (2, "Bob Bolt", "Beta", 1, 0)]


def test_build_joins_shootouts_and_former_names(tmp_path) -> None:
    db_path = str(tmp_path / "app.db")
    build_fixture_db(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT shootout_winner FROM matches WHERE date = '2001-03-03'"
        ).fetchone() == ("Alpha",)
        assert conn.execute(
            "SELECT home_team, home_canonical FROM matches WHERE date = '2002-07-07'"
        ).fetchone() == ("Old Delta", "Delta")


def test_build_is_idempotent(tmp_path) -> None:
    db_path = str(tmp_path / "app.db")

    def snapshot() -> list:
        with sqlite3.connect(db_path) as conn:
            return [conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall()
                    for t in ("top_scorers", "matches", "elo_ratings", "elo_calibration")]

    build_fixture_db(db_path)
    first = snapshot()
    build_fixture_db(db_path)
    assert snapshot() == first
    assert not os.path.exists(db_path + ".tmp")


def test_missing_column_is_rejected(tmp_path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("date,team,scorer\n2000-01-01,A,B\n")
    from scripts.build_top_scorers import build
    with pytest.raises(ValueError, match="missing columns"):
        build(str(bad), str(tmp_path / "app.db"),
              results_path=str(FIXTURES / "results.csv"),
              shootouts_path=str(FIXTURES / "shootouts.csv"),
              former_names_path=str(FIXTURES / "former_names.csv"))
