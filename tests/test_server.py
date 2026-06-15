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
