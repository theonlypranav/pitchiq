import threading
import time

from app import db


def test_health_and_index(client) -> None:
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/").status_code == 200


def test_scorers_miss_then_hit(client) -> None:
    first = client.get("/api/scorers?era=all")
    second = client.get("/api/scorers?era=all")
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"
    body = second.json()
    assert body["data_version"]
    assert [i["player"] for i in body["items"]][:3] == ["Ann Ace", "Bob Bolt", "José Núñez"]
    stats = client.get("/api/cache/stats").json()
    assert (stats["hits"], stats["misses"], stats["size"]) == (1, 1, 1)


def test_different_params_are_different_keys(client) -> None:
    assert client.get("/api/scorers?era=all&limit=2").headers["X-Cache"] == "MISS"
    assert client.get("/api/scorers?era=all&limit=2&offset=2").headers["X-Cache"] == "MISS"
    assert client.get("/api/scorers?era=all&limit=2&offset=2").json()["items"][0]["player"] == "José Núñez"


def test_cache_disabled_with_zero_ttl(make_client) -> None:
    c = make_client(cache_ttl=0)
    assert c.get("/api/scorers").headers["X-Cache"] == "MISS"
    assert c.get("/api/scorers").headers["X-Cache"] == "MISS"


def test_scorers_validation(client) -> None:
    bad = client.get("/api/scorers?era=1800s")
    assert bad.status_code == 400
    assert bad.json()["valid"] == ["all", "1950-1999", "2000-2026"]
    assert client.get("/api/scorers?limit=101").status_code == 422
    assert client.get("/api/scorers?offset=-1").status_code == 422


def test_search(client) -> None:
    assert client.get("/api/players/search?q=j").status_code == 400
    items = client.get("/api/players/search?q=JOSE nun").json()["items"]
    assert [i["player"] for i in items] == ["José Núñez"]
    assert client.get("/api/players/search?q=%25%25").json()["items"] == []   # % is escaped


def test_concurrent_cold_requests_query_db_once(client, monkeypatch) -> None:
    calls = 0
    real = db.get_scorers

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.2)
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "get_scorers", counting)
    start = threading.Barrier(20)
    statuses = []

    def worker() -> None:
        start.wait()
        r = client.get("/api/scorers?era=2000-2026")
        statuses.append((r.status_code, r.headers["X-Cache"]))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == 1
    assert len(statuses) == 20 and all(s == 200 for s, _ in statuses)
    assert sum(1 for _, c in statuses if c == "MISS") == 1


def test_rate_limit_after_30_requests(make_client, clock) -> None:
    c = make_client(rate_limit=30)
    codes = [c.get("/api/eras").status_code for _ in range(40)]
    assert codes[:30] == [200] * 30
    assert codes[30:] == [429] * 10
    blocked = c.get("/api/eras")
    assert blocked.json() == {"error": "rate limit exceeded"}
    assert blocked.headers["Retry-After"] == "2"
    assert c.get("/api/health").status_code == 200       # exempt
    assert c.get("/").status_code == 200                 # only /api/* is limited
    clock.advance(2)
    assert c.get("/api/eras").status_code == 200         # refilled one token


def test_rate_limit_uses_first_forwarded_ip(make_client) -> None:
    c = make_client(rate_limit=1)
    assert c.get("/api/eras", headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}).status_code == 200
    assert c.get("/api/eras", headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.2"}).status_code == 429
    assert c.get("/api/eras", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 200


def test_teams_and_matches(client) -> None:
    assert client.get("/api/teams").json()["teams"] == ["Alpha", "Beta", "Delta", "Gamma"]
    stats = client.get("/api/teams/stats?team=Delta").json()
    assert stats["record"]["played"] == 2                # includes the match as "Old Delta"
    assert stats["former_names"][0]["name"] == "Old Delta"
    assert client.get("/api/teams/stats?team=Nowhere").status_code == 404
    matches = client.get("/api/matches?team=Alpha&limit=2").json()["items"]
    assert [m["date"] for m in matches] == ["2002-07-07", "2001-03-03"]


def test_simulate(client) -> None:
    body = client.get("/api/simulate?team_a=Alpha&team_b=Beta&venue=a").json()
    p = body["prediction"]
    assert abs(p["p_a_win"] + p["p_draw"] + p["p_b_win"] - 1) < 1e-3
    assert body["head_to_head"]["played"] == 1
    assert client.get("/api/simulate?team_a=Alpha&team_b=Alpha").status_code == 400
    assert client.get("/api/simulate?team_a=Alpha&team_b=Nope").status_code == 404
    assert client.get("/api/simulate?team_a=Alpha&team_b=Beta&venue=moon").status_code == 422


def test_match_detail_and_random(client) -> None:
    match_id = client.get("/api/matches/random").json()["id"]
    assert 1 <= match_id <= 6
    detail = client.get("/api/matches/2").json()           # 1998 Alpha 3-1 Beta
    assert detail["match"]["home_team"] == "Alpha"
    assert len(detail["goals"]) == 4
    assert detail["head_to_head"]["before"]["played"] == 0
    assert client.get("/api/matches/999").status_code == 404
