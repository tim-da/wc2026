from __future__ import annotations

import json
import re
import time
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, render_template


ROOT = Path(__file__).resolve().parent
APP = Flask(__name__, static_folder=str(ROOT / "static"), template_folder=str(ROOT / "templates"))
BASELINE_CSV = ROOT.parent / "world_cup_2026_market_odds_polymarket_kalshi.csv"

POLYMARKET_EVENT_URL = "https://gamma-api.polymarket.com/events?slug=world-cup-winner"
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_STANDINGS_URL = "https://site.web.api.espn.com/apis/v2/sports/soccer/fifa.world/standings"

CACHE_TTL_SECONDS = 60
_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}


LEFT_R32 = [
    ("Germany", "Australia"),
    ("France", "Sweden"),
    ("South Korea", "Canada"),
    ("Netherlands", "Morocco"),
    ("Portugal", "Croatia"),
    ("Spain", "Austria"),
    ("USA", "Bosnia-Herzegovina"),
    ("Belgium", "Algeria"),
]

RIGHT_R32 = [
    ("Brazil", "Japan"),
    ("Ecuador", "Norway"),
    ("Mexico", "Scotland"),
    ("England", "Senegal"),
    ("Argentina", "Uruguay"),
    ("Turkey", "Iran"),
    ("Switzerland", "Egypt"),
    ("Colombia", "Ivory Coast"),
]

ALIASES = {
    "bosnia and herzegovina": "Bosnia-Herzegovina",
    "bosnia & herzegovina": "Bosnia-Herzegovina",
    "bosnia-herzegovina": "Bosnia-Herzegovina",
    "congo dr": "Congo DR",
    "dr congo": "Congo DR",
    "côte d'ivoire": "Ivory Coast",
    "cote d'ivoire": "Ivory Coast",
    "cote divoire": "Ivory Coast",
    "ivory coast": "Ivory Coast",
    "curacao": "Curaçao",
    "curaçao": "Curaçao",
    "iran": "Iran",
    "ir iran": "Iran",
    "korea republic": "South Korea",
    "south korea": "South Korea",
    "saudi arabia": "Saudi Arabia",
    "turkey": "Turkey",
    "turkiye": "Turkey",
    "türkiye": "Turkey",
    "united states": "USA",
    "us": "USA",
    "usa": "USA",
}


def normalized_team(name: str | None) -> str:
    raw = (name or "").strip()
    raw = re.sub(r"^the\s+", "", raw, flags=re.I)
    folded = re.sub(r"\s+", " ", raw).casefold()
    return ALIASES.get(folded, raw)


def is_placeholder(team: str) -> bool:
    return team == "Other" or bool(re.fullmatch(r"Team [A-Z]{1,2}", team))


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(value: float | None) -> float | None:
    return None if value is None else round(value * 100, 3)


def fetch_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(url, params=params, timeout=25, headers={"User-Agent": "WorldCupTracker/1.0"})
    response.raise_for_status()
    return response.json()


def fetch_polymarket() -> dict[str, dict[str, Any]]:
    data = fetch_json(POLYMARKET_EVENT_URL)
    event = data[0] if isinstance(data, list) and data else {}
    markets: dict[str, dict[str, Any]] = {}

    for market in event.get("markets", []):
        team = market.get("groupItemTitle") or market.get("question", "")
        team = re.sub(r"^Will\s+", "", team).strip()
        team = re.sub(r"\s+win the 2026 FIFA World Cup\??$", "", team).strip()
        team = normalized_team(team)
        if is_placeholder(team):
            continue

        try:
            prices = json.loads(market.get("outcomePrices") or "[]")
        except json.JSONDecodeError:
            prices = []

        outcome_price = as_float(prices[0]) if prices else None
        bid = as_float(market.get("bestBid"))
        ask = as_float(market.get("bestAsk"))
        mid = (bid + ask) / 2 if bid is not None and ask is not None else outcome_price
        markets[team] = {
            "team": team,
            "mid": mid,
            "midPct": pct(mid),
            "bidPct": pct(bid),
            "askPct": pct(ask),
            "lastPct": pct(as_float(market.get("lastTradePrice"))),
            "volume": as_float(market.get("volumeNum")),
            "liquidity": as_float(market.get("liquidityNum")),
            "slug": market.get("slug"),
            "updatedAt": market.get("updatedAt"),
        }

    return markets


def fetch_kalshi() -> dict[str, dict[str, Any]]:
    data = fetch_json(KALSHI_MARKETS_URL, {"limit": 200, "series_ticker": "KXMENWORLDCUP"})
    markets: dict[str, dict[str, Any]] = {}

    for market in data.get("markets", []):
        match = re.search(r"^Will\s+(?:the\s+)?(.+?)\s+win the 2026 Men", market.get("title", ""))
        if not match:
            continue

        team = normalized_team(match.group(1))
        if is_placeholder(team):
            continue

        bid = as_float(market.get("yes_bid_dollars"))
        ask = as_float(market.get("yes_ask_dollars"))
        last = as_float(market.get("last_price_dollars"))
        mid = (bid + ask) / 2 if bid is not None and ask is not None else last
        markets[team] = {
            "team": team,
            "mid": mid,
            "midPct": pct(mid),
            "bidPct": pct(bid),
            "askPct": pct(ask),
            "lastPct": pct(last),
            "ticker": market.get("ticker"),
            "updatedAt": market.get("updated_time"),
        }

    return markets


def load_baseline_odds() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str | None]:
    if not BASELINE_CSV.exists():
        return {}, {}, None

    polymarket: dict[str, dict[str, Any]] = {}
    kalshi: dict[str, dict[str, Any]] = {}
    with BASELINE_CSV.open(newline="") as handle:
        for row in csv.DictReader(handle):
            team = normalized_team(row.get("team"))
            if not team or is_placeholder(team):
                continue

            pm_pct = as_float(row.get("polymarket_mid_pct"))
            ks_pct = as_float(row.get("kalshi_mid_pct"))
            if pm_pct is not None:
                polymarket[team] = {
                    "team": team,
                    "mid": pm_pct / 100,
                    "midPct": round(pm_pct, 3),
                    "bidPct": as_float(row.get("polymarket_bid_pct")),
                    "askPct": as_float(row.get("polymarket_ask_pct")),
                }
            if ks_pct is not None:
                kalshi[team] = {
                    "team": team,
                    "mid": ks_pct / 100,
                    "midPct": round(ks_pct, 3),
                    "bidPct": as_float(row.get("kalshi_bid_pct")),
                    "askPct": as_float(row.get("kalshi_ask_pct")),
                }

    return polymarket, kalshi, str(BASELINE_CSV)


def consensus_for(team: str, polymarket: dict[str, Any], kalshi: dict[str, Any]) -> float | None:
    values = []
    if polymarket.get(team, {}).get("mid") is not None:
        values.append(polymarket[team]["mid"])
    if kalshi.get(team, {}).get("mid") is not None:
        values.append(kalshi[team]["mid"])
    return sum(values) / len(values) if values else None


def pick_between(team_a: str, team_b: str, odds: dict[str, Any]) -> str:
    return team_a if (odds.get(team_a, {}).get("mid") or 0) >= (odds.get(team_b, {}).get("mid") or 0) else team_b


def build_projection(odds: dict[str, Any]) -> dict[str, Any]:
    def build_round(pairings: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], list[str]]:
        matches = []
        winners = []
        for team_a, team_b in pairings:
            winner = pick_between(team_a, team_b, odds)
            loser = team_b if winner == team_a else team_a
            matches.append({"teams": [team_a, team_b], "winner": winner, "loser": loser})
            winners.append(winner)
        return matches, winners

    def next_pairings(winners: list[str]) -> list[tuple[str, str]]:
        return [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]

    left_rounds, right_rounds = [], []
    left_matches, left_winners = build_round(LEFT_R32)
    right_matches, right_winners = build_round(RIGHT_R32)
    left_rounds.append(left_matches)
    right_rounds.append(right_matches)

    for _ in range(3):
        left_matches, left_winners = build_round(next_pairings(left_winners))
        right_matches, right_winners = build_round(next_pairings(right_winners))
        left_rounds.append(left_matches)
        right_rounds.append(right_matches)

    left_finalist = left_rounds[-1][0]["winner"]
    right_finalist = right_rounds[-1][0]["winner"]
    champion = pick_between(left_finalist, right_finalist, odds)
    runner_up = right_finalist if champion == left_finalist else left_finalist

    return {
        "champion": champion,
        "finalists": [left_finalist, right_finalist],
        "runnerUp": runner_up,
        "rounds": {"left": left_rounds, "right": right_rounds},
    }


def fetch_scoreboard() -> list[dict[str, Any]]:
    data = fetch_json(ESPN_SCOREBOARD_URL, {"dates": "20260611-20260719", "limit": 500})
    events = []

    for event in data.get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        status = competition.get("status", {})
        status_type = status.get("type", {})
        competitors = competition.get("competitors") or []

        sides: dict[str, Any] = {}
        for competitor in competitors:
            team = competitor.get("team", {})
            side = competitor.get("homeAway", "")
            sides[side] = {
                "team": normalized_team(team.get("displayName") or team.get("name")),
                "displayName": team.get("displayName") or team.get("name"),
                "abbreviation": team.get("abbreviation"),
                "logo": team.get("logo") or (team.get("logos") or [{}])[0].get("href"),
                "score": as_float(competitor.get("score")),
                "winner": bool(competitor.get("winner")),
            }

        home = sides.get("home") or (sides.get("away") if len(sides) == 1 else {})
        away = sides.get("away") or {}
        winner = None
        if status_type.get("completed") and home.get("score") is not None and away.get("score") is not None:
            if home["score"] > away["score"]:
                winner = home["team"]
            elif away["score"] > home["score"]:
                winner = away["team"]
            else:
                winner = "Draw"

        events.append(
            {
                "id": event.get("id"),
                "name": event.get("name"),
                "shortName": event.get("shortName"),
                "date": event.get("date") or competition.get("date"),
                "venue": ((competition.get("venue") or {}).get("fullName")),
                "status": {
                    "state": status_type.get("state"),
                    "completed": bool(status_type.get("completed")),
                    "detail": status_type.get("detail") or status_type.get("description"),
                    "shortDetail": status_type.get("shortDetail"),
                },
                "home": home,
                "away": away,
                "winner": winner,
            }
        )

    return sorted(events, key=lambda item: item.get("date") or "")


def fetch_standings() -> list[dict[str, Any]]:
    data = fetch_json(ESPN_STANDINGS_URL)
    groups = []

    for child in data.get("children", []):
        entries = []
        for entry in child.get("standings", {}).get("entries", []):
            team = entry.get("team", {})
            stats = {stat.get("name"): stat for stat in entry.get("stats", [])}

            def stat_value(name: str) -> float | None:
                value = stats.get(name, {}).get("value")
                return as_float(value)

            entries.append(
                {
                    "team": normalized_team(team.get("displayName") or team.get("name")),
                    "displayName": team.get("displayName") or team.get("name"),
                    "abbreviation": team.get("abbreviation"),
                    "logo": (team.get("logos") or [{}])[0].get("href"),
                    "note": (entry.get("note") or {}).get("description"),
                    "gp": stat_value("gamesPlayed"),
                    "wins": stat_value("wins"),
                    "draws": stat_value("ties"),
                    "losses": stat_value("losses"),
                    "points": stat_value("points"),
                    "gd": stat_value("pointDifferential"),
                    "gf": stat_value("pointsFor"),
                    "ga": stat_value("pointsAgainst"),
                }
            )

        groups.append({"name": child.get("name"), "entries": entries})

    return groups


def compare_events(events: list[dict[str, Any]], polymarket: dict[str, Any], kalshi: dict[str, Any]) -> dict[str, Any]:
    compared = []
    summary = {
        "completed": 0,
        "decisive": 0,
        "live": 0,
        "polymarketHits": 0,
        "kalshiHits": 0,
        "polymarketMisses": 0,
        "kalshiMisses": 0,
        "draws": 0,
    }

    def result_for(actual_winner: str | None, pick: str | None) -> str:
        if actual_winner is None:
            return "pending"
        if actual_winner == "Draw":
            return "draw"
        if not pick:
            return "unknown"
        return "hit" if actual_winner == pick else "miss"

    for event in events:
        home_team = event.get("home", {}).get("team")
        away_team = event.get("away", {}).get("team")
        pm_pick = pick_between(home_team, away_team, polymarket) if home_team and away_team else None
        ks_pick = pick_between(home_team, away_team, kalshi) if home_team and away_team else None
        pm_result = result_for(event.get("winner"), pm_pick)
        ks_result = result_for(event.get("winner"), ks_pick)

        if event.get("status", {}).get("completed"):
            summary["completed"] += 1
            if event.get("winner") == "Draw":
                summary["draws"] += 1
            elif event.get("winner"):
                summary["decisive"] += 1
                summary["polymarketHits"] += int(pm_result == "hit")
                summary["kalshiHits"] += int(ks_result == "hit")
                summary["polymarketMisses"] += int(pm_result == "miss")
                summary["kalshiMisses"] += int(ks_result == "miss")
        elif event.get("status", {}).get("state") == "in":
            summary["live"] += 1

        compared.append(
            {
                **event,
                "prediction": {
                    "polymarketPick": pm_pick,
                    "polymarketResult": pm_result,
                    "kalshiPick": ks_pick,
                    "kalshiResult": ks_result,
                },
            }
        )

    return {"events": compared, "summary": summary}


def build_team_rows(groups: list[dict[str, Any]], polymarket: dict[str, Any], kalshi: dict[str, Any]) -> list[dict[str, Any]]:
    pm_rank = {team: rank + 1 for rank, team in enumerate(sorted(polymarket, key=lambda t: polymarket[t].get("mid") or 0, reverse=True))}
    ks_rank = {team: rank + 1 for rank, team in enumerate(sorted(kalshi, key=lambda t: kalshi[t].get("mid") or 0, reverse=True))}
    rows = []

    for group in groups:
        for actual_rank, entry in enumerate(group.get("entries", []), start=1):
            team = entry["team"]
            consensus = consensus_for(team, polymarket, kalshi)
            rows.append(
                {
                    **entry,
                    "group": group.get("name"),
                    "groupRank": actual_rank,
                    "polymarketPct": polymarket.get(team, {}).get("midPct"),
                    "kalshiPct": kalshi.get(team, {}).get("midPct"),
                    "consensusPct": pct(consensus),
                    "polymarketRank": pm_rank.get(team),
                    "kalshiRank": ks_rank.get(team),
                }
            )

    return rows


def build_snapshot() -> dict[str, Any]:
    live_polymarket = fetch_polymarket()
    live_kalshi = fetch_kalshi()
    baseline_polymarket, baseline_kalshi, baseline_path = load_baseline_odds()
    polymarket = baseline_polymarket or live_polymarket
    kalshi = baseline_kalshi or live_kalshi
    scoreboard = fetch_scoreboard()
    standings = fetch_standings()
    comparisons = compare_events(scoreboard, polymarket, kalshi)
    team_rows = build_team_rows(standings, polymarket, kalshi)

    pm_top = max(polymarket.values(), key=lambda item: item.get("mid") or 0)
    ks_top = max(kalshi.values(), key=lambda item: item.get("mid") or 0)

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "polymarket": POLYMARKET_EVENT_URL,
            "kalshi": "https://api.elections.kalshi.com/trade-api/v2/events/KXMENWORLDCUP-26",
            "espnScoreboard": ESPN_SCOREBOARD_URL,
            "espnStandings": ESPN_STANDINGS_URL,
            "predictionBaseline": baseline_path,
        },
        "predictionMode": "baselineCsv" if baseline_path else "liveMarkets",
        "leaders": {"polymarket": pm_top, "kalshi": ks_top},
        "projections": {
            "polymarket": build_projection(polymarket),
            "kalshi": build_projection(kalshi),
        },
        "odds": {
            "polymarket": polymarket,
            "kalshi": kalshi,
            "consensus": sorted(
                [
                    {
                        "team": team,
                        "pct": pct(consensus_for(team, polymarket, kalshi)),
                        "polymarketPct": polymarket.get(team, {}).get("midPct"),
                        "kalshiPct": kalshi.get(team, {}).get("midPct"),
                    }
                    for team in sorted(set(polymarket) | set(kalshi))
                    if consensus_for(team, polymarket, kalshi) is not None
                ],
                key=lambda item: item["pct"] or 0,
                reverse=True,
            ),
        },
        "matches": comparisons["events"],
        "summary": comparisons["summary"],
        "groups": standings,
        "teams": team_rows,
    }


@APP.get("/")
def index():
    return render_template("index.html")


@APP.get("/api/snapshot")
def snapshot():
    now = time.time()
    if _CACHE["payload"] is not None and now - _CACHE["at"] < CACHE_TTL_SECONDS:
        return jsonify({**_CACHE["payload"], "cached": True})

    payload = build_snapshot()
    _CACHE["payload"] = payload
    _CACHE["at"] = now
    return jsonify({**payload, "cached": False})


if __name__ == "__main__":
    APP.run(host="127.0.0.1", port=5055, debug=False)
