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


def test_book_mid_ignores_empty_or_one_sided_book():
    # Empty book (bid 0 / ask 1.00) has a spurious 0.50 midpoint -> use fallback.
    assert server.book_mid(0.0, 1.0, 0.001) == 0.001
    # One-sided book (no bids) -> fallback, not (0 + 0.05) / 2.
    assert server.book_mid(0.0, 0.05, 0.002) == 0.002
    # Missing quotes -> fallback.
    assert server.book_mid(None, None, 0.3) == 0.3
    # Genuine two-sided book -> midpoint.
    assert server.book_mid(0.20, 0.21, 0.19) == pytest.approx(0.205)


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


def test_pick_for_event_prefers_pregame_then_outright():
    key = server.market_key("Spain", "France")
    odds = {"Spain": {"mid": 0.3, "midPct": 30.0}, "France": {"mid": 0.1, "midPct": 10.0}}

    # Pre-game capture wins.
    pregame = {"polymarket": {"preGame": {"pick": "France", "pickPct": 55.0}, "inPlay": {"pick": "Spain", "pickPct": 80.0}}}
    locked = server.pick_for_event("polymarket", key, "Spain", "France", pregame, odds)
    assert (locked["pick"], locked["source"]) == ("France", "match")

    # In-play information must never be presented as the locked prediction.
    inplay_only = {"polymarket": {"inPlay": {"pick": "France", "pickPct": 80.0}}}
    locked2 = server.pick_for_event("polymarket", key, "Spain", "France", inplay_only, odds)
    assert (locked2["pick"], locked2["source"]) == ("Spain", "outright")

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
    goal = server.goal_event_notification(
        {"state": "in", "home_score": 0, "away_score": 0, "completed": False},
        cur(home_score=1, minute="67'"),
    )
    assert goal["title"] == "Score change!" and goal["body"] == "67': A 1-0 B"
    # full time: -> completed
    ft = server.goal_event_notification({"state": "in", "completed": False}, cur(state="post", completed=True, home_score=2, away_score=1))
    assert ft["title"] == "Full time"
    # no change while live -> nothing
    assert server.goal_event_notification({"state": "in", "home_score": 1, "away_score": 0, "completed": False}, cur(home_score=1)) is None


def test_pick_for_event_outright_only_exception():
    # Matches in LOCKED_OUTRIGHT_ONLY ignore any captured market and grade on outright.
    key = next(iter(server.LOCKED_OUTRIGHT_ONLY))
    team_a, team_b = key.split("::")
    markets = {"polymarket": {"preGame": {"pick": team_a, "pickPct": 99.0}, "inPlay": {"pick": team_a, "pickPct": 99.0}}}
    odds = {team_a: {"mid": 0.01, "midPct": 1.0}, team_b: {"mid": 0.9, "midPct": 90.0}}
    locked = server.pick_for_event("polymarket", key, team_a, team_b, markets, odds)
    assert (locked["pick"], locked["source"]) == (team_b, "outright")  # outright favourite, not the captured pick


def test_current_match_pick_prefers_inplay_then_outright():
    markets = {"kalshi": {"inPlay": {"pick": "Spain", "pickPct": 70.0}}}
    now = server.current_match_pick("kalshi", "Spain", "France", markets, {})
    assert (now["pick"], now["source"]) == ("Spain", "match")

    # A pre-game-only capture must not leak into "now".
    pregame_only = {"kalshi": {"preGame": {"pick": "Spain", "pickPct": 70.0}}}
    odds = {"Spain": {"mid": 0.1}, "France": {"mid": 0.3, "midPct": 33.0}}
    fallback = server.current_match_pick("kalshi", "Spain", "France", pregame_only, odds)
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
    assert winners[f"round-of-32::{server.market_key('France', 'Sweden')}"] == "France"


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


def test_partial_knockout_fixture_falls_back_to_seed_pairing():
    # A fixture with only one resolved team keeps the seeded pairing rather than
    # pairing the known team with a seed opponent (which would duplicate it).
    assert server.event_teams_with_fallback(
        {"home": {"team": "Germany"}, "away": {"team": "Group A 2nd Place"}},
        ("France", "Sweden"),
    ) == ("France", "Sweden")


def test_fully_resolved_knockout_fixture_overrides_seed_pairing():
    assert server.event_teams_with_fallback(
        {"home": {"team": "Germany"}, "away": {"team": "France"}},
        ("Spain", "Sweden"),
    ) == ("Germany", "France")


def test_partial_knockout_fixtures_do_not_duplicate_teams_in_bracket():
    # Reproduces the live bug: real R32 fixtures where only the host is known
    # (USA, Germany) must not appear in two slots at once.
    events = [
        {
            "id": f"r32-{i}",
            "date": f"2026-06-{28 + i // 4:02d}T{12 + i % 4 * 3:02d}:00:00Z",
            "stageSlug": "round-of-32",
            "status": {"completed": False},
            "home": {"team": home},
            "away": {"team": "Third Place Group A/B/C/D/F"},
        }
        for i, home in enumerate(["Germany", "USA", "Mexico"])
    ]
    projection = server.build_fact_projection({}, {}, events)
    r32 = projection["rounds"]["left"][0] + projection["rounds"]["right"][0]
    teams = [team for match in r32 for team in match["teams"]]
    assert len(teams) == len(set(teams)), f"duplicate teams in R32: {teams}"


def test_seeded_third_is_not_also_matched_to_a_placeholder_slot():
    # ESPN seeds a real third ("B3") into one R32 slot; the matcher must not also
    # assign Group B's third to a "Third Place …" placeholder slot (live "two
    # Bosnias" bug).
    standings = _make_standings()
    events = [
        {"stageSlug": "round-of-32", "home": {"team": "Group A Winner"}, "away": {"team": "B3"}},
        {"stageSlug": "round-of-32", "home": {"team": "Group C Winner"},
         "away": {"team": "Third Place Group A/B/C/D/E"}},
    ]
    resolve = server.build_slot_resolver(standings, events)
    # B3 is already seeded, so its group must not be matched to the placeholder.
    assert resolve("Third Place Group A/B/C/D/E") != "B3"


def _make_standings():
    rows = []
    for letter in "ABCDEFGHIJKL":
        entries = [
            {"team": f"{letter}1", "points": 9, "gd": 5, "gf": 7},
            {"team": f"{letter}2", "points": 6, "gd": 2, "gf": 4},
            # all thirds level on points; GD descends A..L so A's third is best, L's worst
            {"team": f"{letter}3", "points": 3, "gd": 12 - "ABCDEFGHIJKL".index(letter), "gf": 1},
            {"team": f"{letter}4", "points": 0, "gd": -20, "gf": 0},
        ]
        rows.append({"name": f"Group {letter}", "entries": entries})
    return rows


def test_slot_resolver_uses_current_standings():
    resolve = server.build_slot_resolver(_make_standings(), [])
    assert resolve("Group A Winner") == "A1"
    assert resolve("Group B 2nd Place") == "B2"
    # a real team ESPN has already seeded into a slot is trusted as-is, NOT
    # forced to the group winner — otherwise a seeded runner-up would collapse
    # onto the winner and appear twice (the live "two Germanys" bug).
    assert resolve("A2") == "A2"
    assert resolve("A4") == "A4"
    # unknown / later-round labels resolve to None so the caller keeps its fallback
    assert resolve("Winners Match 73") is None


def test_seeded_runner_up_does_not_duplicate_group_winner():
    # Group E: winner "E1" seeded in one slot, runner-up "E2" seeded in another.
    # Both must survive as themselves rather than both resolving to the winner.
    standings = _make_standings()
    events = [
        {"home": {"team": "E1"}, "away": {"team": "Group A 2nd Place"}},
        {"home": {"team": "E2"}, "away": {"team": "Group B 2nd Place"}},
    ]
    resolve = server.build_slot_resolver(standings, events)
    assert resolve("E1") == "E1"
    assert resolve("E2") == "E2"


def test_qualifying_thirds_keeps_best_eight_only():
    ranking = server.group_ranking(_make_standings())
    qualifying = set(server.qualifying_third_letters(ranking))
    assert qualifying == set("ABCDEFGH")
    assert "I" not in qualifying and "L" not in qualifying


def test_confirmed_teams_locks_top_two_and_best_eight_thirds():
    # 9 finished groups; thirds get descending GD so group I's third is 9th (out).
    groups = []
    for index, letter in enumerate("ABCDEFGHI"):
        groups.append({
            "name": f"Group {letter}",
            "entries": [
                {"team": f"{letter}1", "points": 9, "gp": 3, "gd": 5, "gf": 0},
                {"team": f"{letter}2", "points": 6, "gp": 3, "gd": 2, "gf": 0},
                {"team": f"{letter}3", "points": 3, "gp": 3, "gd": 9 - index, "gf": 0},
                {"team": f"{letter}4", "points": 0, "gp": 3, "gd": -9, "gf": 0},
            ],
        })
    confirmed = server.confirmed_teams(groups)
    # Every finished group's top two is a locked berth.
    for letter in "ABCDEFGHI":
        assert f"{letter}1" in confirmed and f"{letter}2" in confirmed
    # The best 8 thirds (A..H) are confirmed; the 9th-best (I3) is not.
    for letter in "ABCDEFGH":
        assert f"{letter}3" in confirmed
    assert "I3" not in confirmed
    # 4th places never qualify.
    assert all(f"{letter}4" not in confirmed for letter in "ABCDEFGHI")


def test_confirmed_teams_excludes_unsettled_positions():
    standings = [
        {"name": "Group A", "entries": [  # finished -> top two locked
            {"team": "A1", "points": 9, "gp": 3, "gd": 5, "gf": 7},
            {"team": "A2", "points": 4, "gp": 3, "gd": 2, "gf": 4},
            {"team": "A3", "points": 1, "gp": 3, "gd": -3, "gf": 2},
            {"team": "A4", "points": 0, "gp": 3, "gd": -6, "gf": 0},
        ]},
        {"name": "Group B", "entries": [  # one game left
            {"team": "B1", "points": 9, "gp": 2},  # uncatchable leader -> locked
            {"team": "B2", "points": 3, "gp": 2},  # 2nd/3rd still open
            {"team": "B3", "points": 3, "gp": 2},
            {"team": "B4", "points": 3, "gp": 2},
        ]},
    ]
    confirmed = server.confirmed_teams(standings)
    assert "A1" in confirmed and "A2" in confirmed
    assert "B1" in confirmed
    # B's chasers and B's (unsettled) third are not confirmed.
    assert "B2" not in confirmed and "B3" not in confirmed and "B4" not in confirmed


def test_bracket_greens_actual_winner_in_next_round():
    # A completed R32 win makes that team certainly present in the R16 -> green
    # band, even with no confirmed group qualifiers.
    from datetime import datetime, timezone

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
        "Canada": {"team": "Canada", "mid": 0.1, "midPct": 10.0},
        "France": {"team": "France", "mid": 0.9, "midPct": 90.0},
    }
    projection = server.build_fact_projection(odds, server.actual_winners_by_pair([event]), [event])
    svg = server.render_bracket_svg(
        projection, odds, ["t"], datetime(2026, 7, 1, tzinfo=timezone.utc), set(), None, set()
    )
    # confirmed is empty, so the only green band can come from Canada's real R16 advance.
    assert "#dcf5e4" in svg


def test_projection_only_includes_currently_qualifying_teams():
    # 12 group-stage R32 slot labels (2nd places + thirds) plus winners; the
    # bracket must contain only group winners, runners-up and the best 8 thirds.
    standings = _make_standings()
    third_pools = {
        "Third Place Group A/B/C/D/E": set("ABCDE"),
        "Third Place Group F/G/H/I/J": set("FGHIJ"),
    }
    # minimal: assert no non-qualifying third (I3..L3) is reachable for a slot
    resolve = server.build_slot_resolver(
        standings,
        [
            {"home": {"team": "Group A Winner"}, "away": {"team": label}}
            for label in third_pools
        ],
    )
    assert resolve("Third Place Group A/B/C/D/E") in {f"{l}3" for l in "ABCDE"}
    # L3 (worst third) is never assigned because it is outside the best 8
    assert resolve("Third Place Group F/G/H/I/J") != "L3"


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


def test_stage_scoped_fact_does_not_leak_without_later_fixture():
    pair = server.market_key("Spain", "France")
    facts = {f"round-of-32::{pair}": "Spain"}
    odds = {"Spain": {"mid": 0.1}, "France": {"mid": 0.9}}

    assert server.bracket_pick("Spain", "France", odds, facts, "final", allow_legacy_fact=False) == (
        "France",
        "market",
    )


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
    event_key = server.event_market_key(event)
    locked = server.load_match_baseline()["markets"][event_key]["polymarket"]["preGame"]
    assert locked["pick"] == "France"
    assert locked["pickPct"] == 57.0

    event["status"]["state"] = "in"
    server.merge_match_baseline([event], sources("Spain", 80.0))
    captures = server.load_match_baseline()["markets"][event_key]["polymarket"]
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

    assert server.event_market_key(event) not in markets


def test_match_captures_keep_rematches_separate():
    pair = server.market_key("Spain", "France")
    events = [
        {
            "id": "group-match",
            "date": "2026-06-20T19:00:00Z",
            "status": {"completed": False, "state": "pre"},
            "home": {"team": "Spain"},
            "away": {"team": "France"},
        },
        {
            "id": "final-match",
            "date": "2026-07-19T19:00:00Z",
            "status": {"completed": False, "state": "pre"},
            "home": {"team": "Spain"},
            "away": {"team": "France"},
        },
    ]
    sources = {
        "polymarket": {
            "group": {
                "pairKey": pair,
                "scheduledAt": events[0]["date"],
                "pick": "Spain",
                "pickPct": 60.0,
            },
            "final": {
                "pairKey": pair,
                "scheduledAt": events[1]["date"],
                "pick": "France",
                "pickPct": 70.0,
            },
        }
    }
    markets = {}

    server._apply_phase_captures(markets, [events[0]], sources, "2026-06-20T18:00:00Z")
    server._apply_phase_captures(markets, [events[1]], sources, "2026-07-19T18:00:00Z")

    assert markets["event:group-match"]["polymarket"]["preGame"]["pick"] == "Spain"
    assert markets["event:final-match"]["polymarket"]["preGame"]["pick"] == "France"


def test_legacy_pair_capture_only_matches_same_fixture_time():
    pair = server.market_key("Spain", "France")
    legacy = {
        pair: {
            "polymarket": {
                "preGame": {
                    "pick": "Spain",
                    "scheduledAt": "2026-06-20T19:00:00Z",
                }
            }
        }
    }
    group_event = {
        "date": "2026-06-20T19:00:00Z",
        "home": {"team": "Spain"},
        "away": {"team": "France"},
    }
    final_event = {
        "date": "2026-07-19T19:00:00Z",
        "home": {"team": "Spain"},
        "away": {"team": "France"},
    }

    assert server.locked_markets_for_event(legacy, group_event)
    assert server.locked_markets_for_event(legacy, final_event) == {}


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("https://fcm.googleapis.com/wp/abc", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
        ("https://web.push.apple.com/Qabc", True),
        ("https://wns2-am3p.notify.windows.com/w/?token=x", True),
        ("http://fcm.googleapis.com/wp/abc", False),
        ("https://127.0.0.1/push", False),
        ("https://example.com/push", False),
        ("https://user:pass@fcm.googleapis.com/push", False),
    ],
)
def test_valid_push_endpoint(endpoint, expected):
    assert server.valid_push_endpoint(endpoint) is expected


def test_push_rate_limit(monkeypatch):
    monkeypatch.setattr(server, "PUSH_RATE_LIMIT", 2)
    monkeypatch.setattr(server, "_PUSH_RATE_ATTEMPTS", server.defaultdict(server.deque))

    assert server.push_rate_limited("client", now=1.0) is False
    assert server.push_rate_limited("client", now=2.0) is False
    assert server.push_rate_limited("client", now=3.0) is True


def test_push_subscribe_rejects_untrusted_endpoint_before_storage(monkeypatch):
    monkeypatch.setattr(server, "push_rate_limited", lambda client_key: False)
    monkeypatch.setattr(server, "supabase_enabled", lambda: True)
    response = server.APP.test_client().post(
        "/api/push/subscribe",
        json={
            "subscription": {
                "endpoint": "https://example.com/internal-target",
                "keys": {"p256dh": "x" * 32, "auth": "y" * 16},
            }
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid subscription"


@pytest.mark.parametrize(
    "status,expected",
    [
        ({"displayClock": "67:12"}, "67'"),
        ({"shortDetail": "90+4'"}, "90+4'"),
        ({"detail": "Final", "clock": 7200}, "120'"),
        ({}, None),
    ],
)
def test_status_minute(status, expected):
    assert server.status_minute(status) == expected


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


def test_snapshot_route_returns_500_on_unexpected_error(monkeypatch):
    monkeypatch.setitem(server._CACHE, "payload", None)
    monkeypatch.setitem(server._CACHE, "at", 0.0)
    monkeypatch.setattr(server, "_CACHE_REFRESHING", False)
    monkeypatch.setattr(
        server,
        "build_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("programming mistake")),
    )

    response = server.APP.test_client().get("/api/snapshot")

    assert response.status_code == 500


def test_snapshot_serves_stale_while_another_refresh_is_running(monkeypatch):
    cached_payload = {"generatedAt": "2026-06-19T12:00:00+00:00", "matches": [], "teams": []}
    monkeypatch.setitem(server._CACHE, "payload", cached_payload)
    monkeypatch.setitem(server._CACHE, "at", 0.0)
    monkeypatch.setattr(server, "_CACHE_REFRESHING", True)
    monkeypatch.setattr(server, "build_snapshot", lambda: pytest.fail("must not start a second refresh"))

    response = server.APP.test_client().get("/api/snapshot")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["stale"] is True
    assert payload["refreshing"] is True


def test_write_latest_bracket_generation_creates_parent_and_replaces_atomically(monkeypatch, tmp_path):
    path = tmp_path / "nested" / "bracket-state.json"
    monkeypatch.setattr(server, "BRACKET_GENERATION_STATE_JSON", path)
    payload = {
        "generatedAt": server.datetime(2026, 6, 20, tzinfo=server.timezone.utc),
        "composition": {"champion": "Spain"},
        "compositionHash": "abc",
        "sources": ["test"],
    }

    server.write_latest_bracket_generation(payload)

    assert server.json.loads(path.read_text())["compositionHash"] == "abc"
    assert not path.with_name(f".{path.name}.tmp").exists()
