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
