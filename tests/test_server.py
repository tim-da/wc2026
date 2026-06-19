"""Unit tests for the pure helpers in the World Cup tracker server.

The server module lives in a hyphenated directory, so it is loaded by path
rather than imported as a normal package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SERVER_PATH = Path(__file__).resolve().parent.parent / "outputs" / "world-cup-tracker" / "server.py"
_spec = importlib.util.spec_from_file_location("wc_server", SERVER_PATH)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("United States", "USA"),
        ("the Netherlands", "Netherlands"),
        ("  Türkiye ", "Turkey"),
        ("Korea Republic", "South Korea"),
        ("Côte d'Ivoire", "Ivory Coast"),
        ("Brazil", "Brazil"),
        (None, ""),
    ],
)
def test_normalized_team(raw, expected):
    assert server.normalized_team(raw) == expected


def test_market_key_is_order_independent():
    assert server.market_key("Spain", "France") == server.market_key("France", "Spain")
    assert server.market_key("Spain", None) is None
    assert server.market_key(None, None) is None


@pytest.mark.parametrize(
    "team,expected",
    [
        ("Other", True),
        ("Team A", True),
        ("Team BC", True),
        ("Spain", False),
        ("Teamwork", False),
    ],
)
def test_is_placeholder(team, expected):
    assert server.is_placeholder(team) is expected


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), ("", None), ("1.5", 1.5), (3, 3.0), ("abc", None)],
)
def test_as_float(value, expected):
    assert server.as_float(value) == expected


def test_pct_rounds_and_passes_none():
    assert server.pct(0.16753) == 16.753
    assert server.pct(None) is None


def test_country_flag():
    assert server.country_flag("US") == "\U0001F1FA\U0001F1F8"
    assert server.country_flag("USA") is None
    assert server.country_flag(None) is None


def test_parse_yes_price_prefers_bid_ask_midpoint():
    result = server.parse_yes_price({"bestBid": "0.40", "bestAsk": "0.60", "lastTradePrice": "0.55"})
    assert result["mid"] == pytest.approx(0.50)
    assert result["midPct"] == pytest.approx(50.0)
    assert result["bidPct"] == pytest.approx(40.0)
    assert result["askPct"] == pytest.approx(60.0)


def test_parse_yes_price_falls_back_to_last_trade():
    result = server.parse_yes_price({"lastTradePrice": "0.30"})
    assert result["mid"] == pytest.approx(0.30)


def test_build_fact_projection_with_empty_odds_is_deterministic():
    # With no odds and no results, every pairing resolves to the first listed team.
    projection = server.build_fact_projection({}, {})
    assert projection["champion"] == server.LEFT_R32[0][0]  # "Germany"
    assert projection["final"]["teams"] == [projection["champion"], server.RIGHT_R32[0][0]]
    assert len(projection["rounds"]["left"]) == 4
    assert len(projection["rounds"]["right"]) == 4


def test_build_fact_projection_respects_actual_winners():
    key = server.market_key("Germany", "Australia")
    projection = server.build_fact_projection({}, {key: "Australia"})
    first_match = projection["rounds"]["left"][0][0]
    assert first_match["winner"] == "Australia"
    assert first_match["source"] == "actual"


def test_build_projection_matches_fact_projection_finalists():
    odds = {"Spain": {"mid": 0.9, "midPct": 90.0}}
    projection = server.build_projection(odds)
    left_finalist = projection["rounds"]["left"][-1][0]["winner"]
    right_finalist = projection["rounds"]["right"][-1][0]["winner"]
    assert projection["finalists"] == [left_finalist, right_finalist]
    assert projection["champion"] in projection["finalists"]


def test_top_leader_handles_empty():
    assert server.top_leader({}) is None
    leader = server.top_leader({"A": {"mid": 0.1}, "B": {"mid": 0.4}})
    assert leader["mid"] == 0.4


@pytest.mark.parametrize(
    "note,expected",
    [
        ("FIFA World Cup, Group A", "A"),
        ("FIFA World Cup, Group L", "L"),
        ("FIFA World Cup", None),
        (None, None),
    ],
)
def test_match_group(note, expected):
    assert server.match_group(note) == expected


def test_outright_pick():
    odds = {"Spain": {"mid": 0.2, "midPct": 20.0}, "France": {"mid": 0.1, "midPct": 10.0}}
    assert server.outright_pick("Spain", "France", odds)["pick"] == "Spain"
    assert server.outright_pick("France", "Spain", odds)["source"] == "outright"
    assert server.outright_pick("A", "B", {"A": {"mid": 0.1}, "B": {"mid": 0.1}})["pick"] == "Draw"
    assert server.outright_pick(None, "B", odds)["pick"] is None


@pytest.mark.parametrize(
    "status,expected",
    [
        ({"state": "pre", "completed": False}, "preGame"),
        ({"state": "in", "completed": False}, "inPlay"),
        ({"state": "post", "completed": True}, None),
    ],
)
def test_match_phase(status, expected):
    assert server.match_phase(status) == expected


def test_phase_container_migrates_legacy_flat():
    legacy = {"pick": "Spain", "pickPct": 60.0, "outcomes": {}}
    assert server.phase_container(legacy) == {"preGame": legacy}
    already = {"preGame": {"pick": "x"}}
    assert server.phase_container(already) is already
    assert server.phase_container({}) == {}


def test_pick_for_event_prefers_pregame_then_inplay_then_outright():
    key = server.market_key("Spain", "France")
    odds = {"Spain": {"mid": 0.3, "midPct": 30.0}, "France": {"mid": 0.1, "midPct": 10.0}}

    # Pre-game capture wins.
    pregame = {key: {"polymarket": {"preGame": {"pick": "France", "pickPct": 55.0}, "inPlay": {"pick": "Spain", "pickPct": 80.0}}}}
    locked = server.pick_for_event("polymarket", key, "Spain", "France", pregame, odds)
    assert (locked["pick"], locked["source"]) == ("France", "match")

    # No pre-game but an in-play market exists -> use it, NOT outright.
    inplay_only = {key: {"polymarket": {"inPlay": {"pick": "Spain", "pickPct": 80.0}}}}
    locked2 = server.pick_for_event("polymarket", key, "Spain", "France", inplay_only, odds)
    assert (locked2["pick"], locked2["source"]) == ("Spain", "match")

    # No market at all -> outright last resort.
    fallback = server.pick_for_event("polymarket", key, "Spain", "France", {}, odds)
    assert (fallback["pick"], fallback["source"]) == ("Spain", "outright")


def test_compare_events_locked_baseline_now_live():
    # No captured market -> both picks fall back to outright; locked must use the
    # baseline odds while "now" uses the live odds passed as polymarket/kalshi.
    events = [{
        "home": {"team": "Spain"}, "away": {"team": "France"},
        "winner": None, "status": {"state": "in", "completed": False},
    }]
    baseline = {"Spain": {"mid": 0.5, "midPct": 50.0}, "France": {"mid": 0.1, "midPct": 10.0}}  # favours Spain
    live = {"Spain": {"mid": 0.1, "midPct": 10.0}, "France": {"mid": 0.5, "midPct": 50.0}}  # favours France
    out = server.compare_events(events, live, live, {}, baseline, baseline)
    pred = out["events"][0]["prediction"]
    assert pred["polymarketPick"] == "Spain"  # locked -> baseline favourite
    assert pred["polymarketCurrentPick"] == "France"  # now -> live favourite


def test_goal_event_notification():
    def cur(**kw):
        base = {"match_key": "A::B", "home_team": "A", "away_team": "B", "home_score": 0,
                "away_score": 0, "state": "in", "completed": False, "winner": None}
        base.update(kw)
        return base

    # kick-off: pre -> in
    assert server.goal_event_notification({"state": "pre", "completed": False}, cur())["title"] == "Kick-off"
    # goal: score change while live
    goal = server.goal_event_notification({"state": "in", "home_score": 0, "away_score": 0, "completed": False}, cur(home_score=1))
    assert goal["title"] == "Goal!" and "A 1-0 B" in goal["body"]
    # full time: -> completed
    ft = server.goal_event_notification({"state": "in", "completed": False}, cur(state="post", completed=True, home_score=2, away_score=1))
    assert ft["title"] == "Full time"
    # no change while live -> nothing
    assert server.goal_event_notification({"state": "in", "home_score": 1, "away_score": 0, "completed": False}, cur(home_score=1)) is None


def test_pick_for_event_outright_only_exception():
    # Matches in LOCKED_OUTRIGHT_ONLY ignore any captured market and grade on outright.
    key = next(iter(server.LOCKED_OUTRIGHT_ONLY))
    team_a, team_b = key.split("::")
    markets = {key: {"polymarket": {"preGame": {"pick": team_a, "pickPct": 99.0}, "inPlay": {"pick": team_a, "pickPct": 99.0}}}}
    odds = {team_a: {"mid": 0.01, "midPct": 1.0}, team_b: {"mid": 0.9, "midPct": 90.0}}
    locked = server.pick_for_event("polymarket", key, team_a, team_b, markets, odds)
    assert (locked["pick"], locked["source"]) == (team_b, "outright")  # outright favourite, not the captured pick


def test_current_match_pick_prefers_inplay_then_outright():
    key = server.market_key("Spain", "France")
    markets = {key: {"kalshi": {"inPlay": {"pick": "Spain", "pickPct": 70.0}}}}
    now = server.current_match_pick("kalshi", key, "Spain", "France", markets, {})
    assert (now["pick"], now["source"]) == ("Spain", "match")

    # A pre-game-only capture must not leak into "now".
    pregame_only = {key: {"kalshi": {"preGame": {"pick": "Spain", "pickPct": 70.0}}}}
    odds = {"Spain": {"mid": 0.1}, "France": {"mid": 0.3, "midPct": 33.0}}
    fallback = server.current_match_pick("kalshi", key, "Spain", "France", pregame_only, odds)
    assert (fallback["pick"], fallback["source"]) == ("France", "outright")


def _event(home, away, state, completed):
    return {"home": {"team": home}, "away": {"team": away}, "status": {"state": state, "completed": completed}}


def test_merge_match_baseline_phase_capture(tmp_path, monkeypatch):
    path = tmp_path / "baseline.json"
    monkeypatch.setattr(server, "MATCH_BASELINE_JSON", path)
    key = server.market_key("Spain", "France")

    def live(pick, pct):
        return {"polymarket": {key: {"pick": pick, "pickPct": pct, "outcomes": {}}}}

    # Scheduled -> preGame captured, no inPlay.
    markets, _ = server.merge_match_baseline([_event("Spain", "France", "pre", False)], live("Spain", 60.0))
    assert markets[key]["polymarket"]["preGame"]["pick"] == "Spain"
    assert "inPlay" not in markets[key]["polymarket"]

    # Still scheduled, new odds -> preGame overwritten (most current before start).
    markets, _ = server.merge_match_baseline([_event("Spain", "France", "pre", False)], live("France", 52.0))
    assert markets[key]["polymarket"]["preGame"]["pick"] == "France"

    # Live -> inPlay captured, preGame frozen.
    markets, _ = server.merge_match_baseline([_event("Spain", "France", "in", False)], live("Spain", 80.0))
    assert markets[key]["polymarket"]["preGame"]["pick"] == "France"
    assert markets[key]["polymarket"]["inPlay"]["pick"] == "Spain"

    # Completed -> both frozen, ignores fresh odds.
    markets, _ = server.merge_match_baseline([_event("Spain", "France", "post", True)], live("France", 99.0))
    assert markets[key]["polymarket"]["preGame"]["pick"] == "France"
    assert markets[key]["polymarket"]["inPlay"]["pick"] == "Spain"


def test_merge_match_baseline_migrates_legacy(tmp_path, monkeypatch):
    path = tmp_path / "baseline.json"
    key = server.market_key("Spain", "France")
    path.write_text(server.json.dumps({"createdAt": "x", "markets": {key: {"polymarket": {"pick": "Spain", "pickPct": 60.0, "outcomes": {}}}}}))
    monkeypatch.setattr(server, "MATCH_BASELINE_JSON", path)

    live = {"polymarket": {key: {"pick": "France", "pickPct": 51.0, "outcomes": {}}}}
    markets, _ = server.merge_match_baseline([_event("Spain", "France", "in", False)], live)
    assert markets[key]["polymarket"]["preGame"]["pick"] == "Spain"  # legacy flat migrated
    assert markets[key]["polymarket"]["inPlay"]["pick"] == "France"


@pytest.mark.parametrize(
    "slug,group,expected",
    [
        ("group-stage", "A", "Group A"),
        ("group-stage", None, None),
        ("round-of-32", None, "1/16 Finals"),
        ("round-of-16", None, "1/8 Finals"),
        ("quarterfinals", None, "1/4 Finals"),
        ("semifinals", None, "1/2 Finals"),
        ("final", None, "FINAL"),
        ("3rd-place-match", None, "3rd Place"),
        (None, None, None),
        ("unknown-stage", None, None),
    ],
)
def test_stage_label(slug, group, expected):
    assert server.stage_label(slug, group) == expected


def test_fetch_scoreboard_prefers_winner_flag_for_tied_knockout_score(monkeypatch):
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda *args, **kwargs: {
            "events": [
                {
                    "id": "final-1",
                    "date": "2026-07-19T19:00:00Z",
                    "season": {"slug": "final", "type": 13803},
                    "competitions": [
                        {
                            "status": {"type": {"completed": True, "state": "post"}},
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "score": "1",
                                    "winner": True,
                                    "team": {"displayName": "Spain"},
                                },
                                {
                                    "homeAway": "away",
                                    "score": "1",
                                    "winner": False,
                                    "team": {"displayName": "France"},
                                },
                            ],
                        }
                    ],
                }
            ]
        },
    )

    event = server.fetch_scoreboard()[0]

    assert event["winner"] == "Spain"
    assert event["stageSlug"] == "final"


def test_actual_winners_excludes_group_stage_results():
    group_event = {
        "stageSlug": "group-stage",
        "status": {"completed": True},
        "winner": "Australia",
        "home": {"team": "Germany"},
        "away": {"team": "Australia"},
    }
    knockout_event = {
        "stageSlug": "round-of-32",
        "status": {"completed": True},
        "winner": "France",
        "home": {"team": "France"},
        "away": {"team": "Sweden"},
    }

    winners = server.actual_winners_by_pair([group_event, knockout_event])

    assert server.market_key("Germany", "Australia") not in winners
    assert winners[server.market_key("France", "Sweden")] == "France"


def test_projection_replaces_guess_with_known_knockout_fixture_and_fact():
    event = {
        "id": "r32-1",
        "date": "2026-06-28T19:00:00Z",
        "stageSlug": "round-of-32",
        "status": {"completed": True},
        "winner": "Canada",
        "home": {"team": "Canada"},
        "away": {"team": "France"},
    }
    odds = {
        "Canada": {"mid": 0.1},
        "France": {"mid": 0.8},
    }

    projection = server.build_fact_projection(
        odds,
        server.actual_winners_by_pair([event]),
        [event],
    )
    first_match = projection["rounds"]["left"][0][0]

    assert first_match["teams"] == ["Canada", "France"]
    assert first_match["winner"] == "Canada"
    assert first_match["source"] == "actual"
    assert first_match["eventId"] == "r32-1"


def test_projection_keeps_known_team_when_other_knockout_slot_is_pending():
    event = {
        "id": "r32-1",
        "date": "2026-06-28T19:00:00Z",
        "stageSlug": "round-of-32",
        "status": {"completed": False},
        "home": {"team": "Canada"},
        "away": {"team": "Group A 2nd Place"},
    }

    projection = server.build_fact_projection({}, {}, [event])
    first_match = projection["rounds"]["left"][0][0]

    assert "Canada" in first_match["teams"]
    assert len(set(first_match["teams"])) == 2


def test_knockout_result_is_not_reused_for_later_rematch():
    events = [
        {
            "id": "r32-rematch",
            "date": "2026-06-28T19:00:00Z",
            "stageSlug": "round-of-32",
            "status": {"completed": True},
            "winner": "Spain",
            "home": {"team": "Spain"},
            "away": {"team": "France"},
        },
        {
            "id": "final-rematch",
            "date": "2026-07-19T19:00:00Z",
            "stageSlug": "final",
            "status": {"completed": False},
            "winner": None,
            "home": {"team": "Spain"},
            "away": {"team": "France"},
        },
    ]
    odds = {
        "Spain": {"mid": 0.1},
        "France": {"mid": 0.9},
    }

    projection = server.build_fact_projection(
        odds,
        server.actual_winners_by_pair(events),
        events,
    )

    assert projection["rounds"]["left"][0][0]["winner"] == "Spain"
    assert projection["final"]["teams"] == ["Spain", "France"]
    assert projection["final"]["winner"] == "France"
    assert projection["final"]["source"] == "market"


def test_match_baseline_updates_until_kickoff_then_freezes(monkeypatch, tmp_path):
    baseline_path = tmp_path / "match-baseline.json"
    monkeypatch.setattr(server, "MATCH_BASELINE_JSON", baseline_path)
    event = {
        "id": "match-1",
        "date": "2099-06-20T19:00:00Z",
        "status": {"completed": False, "state": "pre"},
        "home": {"team": "Spain"},
        "away": {"team": "France"},
    }
    pair = server.market_key("Spain", "France")

    def sources(pick, price):
        return {
            "polymarket": {
                f"{pair}::2099-06-20T19:00:00Z": {
                    "source": "polymarket",
                    "pairKey": pair,
                    "scheduledAt": "2099-06-20T19:00:00Z",
                    "pick": pick,
                    "pickPct": price,
                    "outcomes": {},
                }
            }
        }

    server.merge_match_baseline([event], sources("Spain", 55.0))
    server.merge_match_baseline([event], sources("France", 57.0))
    locked = server.load_match_baseline()["markets"][pair]["polymarket"]["preGame"]
    assert locked["pick"] == "France"
    assert locked["pickPct"] == 57.0

    event["status"]["state"] = "in"
    server.merge_match_baseline([event], sources("Spain", 80.0))
    captures = server.load_match_baseline()["markets"][pair]["polymarket"]
    frozen = captures["preGame"]
    assert frozen["pick"] == "France"
    assert frozen["pickPct"] == 57.0
    assert captures["inPlay"]["pick"] == "Spain"


def test_match_baseline_does_not_capture_after_scheduled_kickoff(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "MATCH_BASELINE_JSON", tmp_path / "match-baseline.json")
    event = {
        "id": "late-pre-status",
        "date": "2000-06-20T19:00:00Z",
        "status": {"completed": False, "state": "pre"},
        "home": {"team": "Spain"},
        "away": {"team": "France"},
    }
    pair = server.market_key("Spain", "France")
    sources = {
        "polymarket": {
            pair: {
                "source": "polymarket",
                "pick": "Spain",
                "pickPct": 60.0,
                "outcomes": {},
            }
        }
    }

    markets, _ = server.merge_match_baseline([event], sources)

    assert pair not in markets


def test_build_snapshot_uses_live_odds_and_baseline_for_performance(monkeypatch, tmp_path):
    baseline_pm = {
        "Spain": {"team": "Spain", "mid": 0.6, "midPct": 60.0},
        "France": {"team": "France", "mid": 0.4, "midPct": 40.0},
    }
    baseline_ks = {
        "Spain": {"team": "Spain", "mid": 0.55, "midPct": 55.0},
        "France": {"team": "France", "mid": 0.45, "midPct": 45.0},
    }
    live_pm = {
        "Spain": {"team": "Spain", "mid": 0.1, "midPct": 10.0},
        "France": {"team": "France", "mid": 0.9, "midPct": 90.0},
    }
    live_ks = {
        "Spain": {"team": "Spain", "mid": 0.2, "midPct": 20.0},
        "France": {"team": "France", "mid": 0.8, "midPct": 80.0},
    }
    event = {
        "id": "group-1",
        "date": "2026-06-15T19:00:00Z",
        "stageSlug": "group-stage",
        "status": {"completed": True, "state": "post"},
        "winner": "Spain",
        "home": {"team": "Spain"},
        "away": {"team": "France"},
    }
    monkeypatch.setattr(server, "MATCH_BASELINE_JSON", tmp_path / "match-baseline.json")
    monkeypatch.setattr(server, "load_baseline_odds", lambda: (baseline_pm, baseline_ks, "baseline.csv"))
    monkeypatch.setattr(server, "fetch_polymarket", lambda: live_pm)
    monkeypatch.setattr(server, "fetch_kalshi", lambda: live_ks)
    monkeypatch.setattr(server, "fetch_scoreboard", lambda: [event])
    monkeypatch.setattr(server, "fetch_standings", lambda: [])
    monkeypatch.setattr(server, "fetch_live_match_sources", lambda: ({}, {}))

    payload = server.build_snapshot()

    assert payload["predictionMode"] == "liveMarkets"
    assert payload["leaders"]["polymarket"]["team"] == "France"
    assert payload["leaders"]["kalshi"]["team"] == "France"
    prediction = payload["matches"][0]["prediction"]
    assert prediction["polymarketPick"] == "Spain"
    assert prediction["kalshiPick"] == "Spain"
    assert prediction["polymarketResult"] == "hit"
    assert prediction["kalshiResult"] == "hit"


def test_build_snapshot_propagates_espn_failure_for_stale_cache(monkeypatch):
    monkeypatch.setattr(
        server,
        "fetch_scoreboard",
        lambda: (_ for _ in ()).throw(server.requests.RequestException("scoreboard down")),
    )
    monkeypatch.setattr(server, "fetch_standings", lambda: [])
    monkeypatch.setattr(server, "fetch_live_match_sources", lambda: ({}, {}))
    monkeypatch.setattr(server, "fetch_polymarket", lambda: {})
    monkeypatch.setattr(server, "fetch_kalshi", lambda: {})

    with pytest.raises(server.requests.RequestException, match="scoreboard down"):
        server.build_snapshot()


def test_snapshot_route_serves_last_good_payload_when_refresh_fails(monkeypatch):
    cached_payload = {
        "generatedAt": "2026-06-19T12:00:00+00:00",
        "matches": [{"id": "cached-match"}],
        "teams": [{"team": "Spain"}],
    }
    monkeypatch.setitem(server._CACHE, "payload", cached_payload)
    monkeypatch.setitem(server._CACHE, "at", 0.0)
    monkeypatch.setattr(
        server,
        "build_snapshot",
        lambda: (_ for _ in ()).throw(server.requests.RequestException("ESPN unavailable")),
    )

    response = server.APP.test_client().get("/api/snapshot")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["cached"] is True
    assert payload["stale"] is True
    assert payload["matches"] == cached_payload["matches"]
    assert payload["teams"] == cached_payload["teams"]
