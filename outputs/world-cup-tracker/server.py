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
MATCH_BASELINE_JSON = ROOT.parent / "world_cup_2026_match_market_baseline.json"

POLYMARKET_EVENT_URL = "https://gamma-api.polymarket.com/events?slug=world-cup-winner"
POLYMARKET_EVENTS_URL = "https://gamma-api.polymarket.com/events"
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
    "czech republic": "Czechia",
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

KALSHI_CODE_TO_TEAM = {
    "ARG": "Argentina",
    "AUS": "Australia",
    "AUT": "Austria",
    "BEL": "Belgium",
    "BIH": "Bosnia-Herzegovina",
    "BRA": "Brazil",
    "CAN": "Canada",
    "CIV": "Ivory Coast",
    "COD": "Congo DR",
    "COL": "Colombia",
    "CPV": "Cape Verde",
    "CRO": "Croatia",
    "CUW": "Curaçao",
    "CZE": "Czechia",
    "DZA": "Algeria",
    "ECU": "Ecuador",
    "EGY": "Egypt",
    "ENG": "England",
    "ESP": "Spain",
    "FRA": "France",
    "GER": "Germany",
    "GHA": "Ghana",
    "HTI": "Haiti",
    "IRI": "Iran",
    "IRQ": "Iraq",
    "JOR": "Jordan",
    "JPN": "Japan",
    "KOR": "South Korea",
    "KSA": "Saudi Arabia",
    "MAR": "Morocco",
    "MEX": "Mexico",
    "NED": "Netherlands",
    "NOR": "Norway",
    "NZL": "New Zealand",
    "PAN": "Panama",
    "PAR": "Paraguay",
    "POR": "Portugal",
    "QAT": "Qatar",
    "RSA": "South Africa",
    "SCO": "Scotland",
    "SEN": "Senegal",
    "SUI": "Switzerland",
    "SWE": "Sweden",
    "TUN": "Tunisia",
    "TUR": "Turkey",
    "URU": "Uruguay",
    "USA": "USA",
    "UZB": "Uzbekistan",
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


def market_key(team_a: str | None, team_b: str | None) -> str | None:
    if not team_a or not team_b:
        return None
    return "::".join(sorted([normalized_team(team_a), normalized_team(team_b)]))


def parse_yes_price(market: dict[str, Any]) -> dict[str, Any]:
    try:
        prices = json.loads(market.get("outcomePrices") or "[]")
    except json.JSONDecodeError:
        prices = []

    outcome_price = as_float(prices[0]) if prices else None
    bid = as_float(market.get("bestBid"))
    if bid is None:
        bid = as_float(market.get("yes_bid_dollars"))
    ask = as_float(market.get("bestAsk"))
    if ask is None:
        ask = as_float(market.get("yes_ask_dollars"))
    last = as_float(market.get("lastTradePrice"))
    if last is None:
        last = as_float(market.get("last_price_dollars"))
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2
    elif outcome_price is not None:
        mid = outcome_price
    else:
        mid = last
    return {
        "mid": mid,
        "midPct": pct(mid),
        "bidPct": pct(bid),
        "askPct": pct(ask),
        "lastPct": pct(last),
    }


def pick_from_outcomes(outcomes: dict[str, dict[str, Any]]) -> tuple[str | None, float | None]:
    priced = [(outcome, data.get("mid")) for outcome, data in outcomes.items() if data.get("mid") is not None]
    if not priced:
        return None, None
    return max(priced, key=lambda item: item[1])


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


def fetch_polymarket_match_markets() -> dict[str, dict[str, Any]]:
    events = fetch_json(POLYMARKET_EVENTS_URL, {"limit": 500, "series_slug": "soccer-fifwc", "closed": "false"})
    markets: dict[str, dict[str, Any]] = {}

    for event in events if isinstance(events, list) else []:
        title = event.get("title") or ""
        title_match = re.search(r"(.+?)\s+vs\.?\s+(.+)", title)
        if not title_match:
            continue

        team_a = normalized_team(title_match.group(1))
        team_b = normalized_team(title_match.group(2))
        key = market_key(team_a, team_b)
        if not key:
            continue

        outcomes: dict[str, dict[str, Any]] = {}
        for market in event.get("markets", []):
            if market.get("sportsMarketType") and market.get("sportsMarketType") != "moneyline":
                continue

            group = market.get("groupItemTitle") or ""
            if group.casefold().startswith("draw"):
                outcome = "Draw"
            else:
                outcome = normalized_team(group)

            if outcome not in {team_a, team_b, "Draw"}:
                continue

            outcomes[outcome] = {
                **parse_yes_price(market),
                "question": market.get("question"),
                "slug": market.get("slug"),
            }

        if outcomes:
            pick, pick_price = pick_from_outcomes(outcomes)
            markets[key] = {
                "source": "polymarket",
                "eventTitle": title,
                "teams": [team_a, team_b],
                "outcomes": outcomes,
                "pick": pick,
                "pickPct": pct(pick_price),
            }

    return markets


def fetch_kalshi_match_markets() -> dict[str, dict[str, Any]]:
    data = fetch_json(KALSHI_MARKETS_URL, {"limit": 1000, "series_ticker": "KXWCGAME"})
    grouped: dict[str, dict[str, Any]] = {}

    for market in data.get("markets", []):
        title = market.get("title") or ""
        title_match = re.search(r"(.+?)\s+vs\s+(.+?)\s+Winner\?", title)
        if not title_match:
            continue

        team_a = normalized_team(title_match.group(1))
        team_b = normalized_team(title_match.group(2))
        key = market_key(team_a, team_b)
        if not key:
            continue

        suffix = (market.get("ticker") or "").split("-")[-1]
        outcome = "Draw" if suffix == "TIE" else normalized_team(KALSHI_CODE_TO_TEAM.get(suffix, suffix))
        if outcome not in {team_a, team_b, "Draw"}:
            continue

        if key not in grouped:
            grouped[key] = {
                "source": "kalshi",
                "eventTitle": title,
                "eventTicker": market.get("event_ticker"),
                "teams": [team_a, team_b],
                "outcomes": {},
            }

        grouped[key]["outcomes"][outcome] = {
            **parse_yes_price(market),
            "ticker": market.get("ticker"),
        }

    for match_market in grouped.values():
        pick, pick_price = pick_from_outcomes(match_market["outcomes"])
        match_market["pick"] = pick
        match_market["pickPct"] = pct(pick_price)

    return grouped


def load_match_baseline() -> dict[str, Any]:
    if not MATCH_BASELINE_JSON.exists():
        return {"createdAt": None, "updatedAt": None, "markets": {}}
    try:
        return json.loads(MATCH_BASELINE_JSON.read_text())
    except json.JSONDecodeError:
        return {"createdAt": None, "updatedAt": None, "markets": {}}


def merge_match_baseline(events: list[dict[str, Any]], live_sources: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    baseline = load_match_baseline()
    baseline.setdefault("markets", {})
    eligible_keys = {
        market_key(event.get("home", {}).get("team"), event.get("away", {}).get("team"))
        for event in events
        if not event.get("status", {}).get("completed") and event.get("status", {}).get("state") != "in"
    }
    eligible_keys.discard(None)

    changed = False
    now = datetime.now(timezone.utc).isoformat()
    if baseline.get("createdAt") is None:
        baseline["createdAt"] = now
        changed = True

    for key in sorted(eligible_keys):
        match_entry = baseline["markets"].setdefault(key, {})
        for source_name, source_markets in live_sources.items():
            if source_name in match_entry or key not in source_markets:
                continue
            match_entry[source_name] = {**source_markets[key], "capturedAt": now}
            changed = True

    if changed:
        baseline["updatedAt"] = now
        MATCH_BASELINE_JSON.write_text(json.dumps(baseline, indent=2, sort_keys=True))

    return baseline["markets"], str(MATCH_BASELINE_JSON) if MATCH_BASELINE_JSON.exists() else None


def fetch_live_match_sources() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    sources: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    fetchers = {
        "polymarket": fetch_polymarket_match_markets,
        "kalshi": fetch_kalshi_match_markets,
    }

    for source_name, fetcher in fetchers.items():
        try:
            sources[source_name] = fetcher()
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            sources[source_name] = {}
            errors[source_name] = str(exc)

    return sources, errors


def consensus_for(team: str, polymarket: dict[str, Any], kalshi: dict[str, Any]) -> float | None:
    values = []
    if polymarket.get(team, {}).get("mid") is not None:
        values.append(polymarket[team]["mid"])
    if kalshi.get(team, {}).get("mid") is not None:
        values.append(kalshi[team]["mid"])
    return sum(values) / len(values) if values else None


def pick_between(team_a: str, team_b: str, odds: dict[str, Any]) -> str:
    return team_a if (odds.get(team_a, {}).get("mid") or 0) >= (odds.get(team_b, {}).get("mid") or 0) else team_b


def pick_for_event(
    source_name: str,
    match_key: str | None,
    team_a: str | None,
    team_b: str | None,
    match_markets: dict[str, Any],
    outright_odds: dict[str, Any],
) -> dict[str, Any]:
    match_market = (match_markets.get(match_key or "") or {}).get(source_name)
    if match_market and match_market.get("pick"):
        return {
            "pick": match_market.get("pick"),
            "pickPct": match_market.get("pickPct"),
            "source": "match",
        }

    if team_a and team_b:
        team_a_mid = outright_odds.get(team_a, {}).get("mid")
        team_b_mid = outright_odds.get(team_b, {}).get("mid")
        if team_a_mid is not None and team_b_mid is not None and team_a_mid == team_b_mid:
            return {
                "pick": "Draw",
                "pickPct": None,
                "source": "outright",
            }

        pick = pick_between(team_a, team_b, outright_odds)
        return {
            "pick": pick,
            "pickPct": outright_odds.get(pick, {}).get("midPct"),
            "source": "outright",
        }

    return {"pick": None, "pickPct": None, "source": "unknown"}


def current_match_pick(source_name: str, match_key: str | None, live_match_sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    match_market = live_match_sources.get(source_name, {}).get(match_key or "")
    if not match_market or not match_market.get("pick"):
        return {"pick": None, "pickPct": None, "source": None}

    return {
        "pick": match_market.get("pick"),
        "pickPct": match_market.get("pickPct"),
        "source": "match",
    }


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


def compare_events(
    events: list[dict[str, Any]],
    polymarket: dict[str, Any],
    kalshi: dict[str, Any],
    match_markets: dict[str, Any] | None = None,
    live_match_sources: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    match_markets = match_markets or {}
    live_match_sources = live_match_sources or {}
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
        if not pick:
            return "unknown"
        return "hit" if actual_winner == pick else "miss"

    for event in events:
        home_team = event.get("home", {}).get("team")
        away_team = event.get("away", {}).get("team")
        match_key_value = market_key(home_team, away_team)
        pm_prediction = pick_for_event("polymarket", match_key_value, home_team, away_team, match_markets, polymarket)
        ks_prediction = pick_for_event("kalshi", match_key_value, home_team, away_team, match_markets, kalshi)
        if event.get("status", {}).get("completed"):
            pm_current = {"pick": None, "pickPct": None, "source": None}
            ks_current = {"pick": None, "pickPct": None, "source": None}
        else:
            pm_current = current_match_pick("polymarket", match_key_value, live_match_sources)
            ks_current = current_match_pick("kalshi", match_key_value, live_match_sources)
        pm_pick = pm_prediction["pick"]
        ks_pick = ks_prediction["pick"]
        pm_result = result_for(event.get("winner"), pm_pick)
        ks_result = result_for(event.get("winner"), ks_pick)

        if event.get("status", {}).get("completed"):
            summary["completed"] += 1
            if event.get("winner") == "Draw":
                summary["draws"] += 1
            if event.get("winner"):
                summary["polymarketHits"] += int(pm_result == "hit")
                summary["kalshiHits"] += int(ks_result == "hit")
                summary["polymarketMisses"] += int(pm_result == "miss")
                summary["kalshiMisses"] += int(ks_result == "miss")
            if event.get("winner") and event.get("winner") != "Draw":
                summary["decisive"] += 1
        elif event.get("status", {}).get("state") == "in":
            summary["live"] += 1

        compared.append(
            {
                **event,
                "prediction": {
                    "polymarketPick": pm_pick,
                    "polymarketPickPct": pm_prediction["pickPct"],
                    "polymarketSource": pm_prediction["source"],
                    "polymarketResult": pm_result,
                    "polymarketCurrentPick": pm_current["pick"],
                    "polymarketCurrentPickPct": pm_current["pickPct"],
                    "polymarketCurrentSource": pm_current["source"],
                    "kalshiPick": ks_pick,
                    "kalshiPickPct": ks_prediction["pickPct"],
                    "kalshiSource": ks_prediction["source"],
                    "kalshiResult": ks_result,
                    "kalshiCurrentPick": ks_current["pick"],
                    "kalshiCurrentPickPct": ks_current["pickPct"],
                    "kalshiCurrentSource": ks_current["source"],
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
    baseline_polymarket, baseline_kalshi, baseline_path = load_baseline_odds()
    if baseline_polymarket and baseline_kalshi:
        polymarket = baseline_polymarket
        kalshi = baseline_kalshi
    else:
        polymarket = fetch_polymarket()
        kalshi = fetch_kalshi()

    scoreboard = fetch_scoreboard()
    live_match_sources, match_market_errors = fetch_live_match_sources()
    match_markets, match_baseline_path = merge_match_baseline(scoreboard, live_match_sources)
    standings = fetch_standings()
    comparisons = compare_events(scoreboard, polymarket, kalshi, match_markets, live_match_sources)
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
            "polymarketMatchMarkets": f"{POLYMARKET_EVENTS_URL}?series_slug=soccer-fifwc",
            "kalshiMatchMarkets": f"{KALSHI_MARKETS_URL}?series_ticker=KXWCGAME",
            "matchMarketBaseline": match_baseline_path,
        },
        "predictionMode": "baselineCsv" if baseline_path else "liveMarkets",
        "matchMarketErrors": match_market_errors,
        "matchMarketsCaptured": sum(len(source) for source in live_match_sources.values()),
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
