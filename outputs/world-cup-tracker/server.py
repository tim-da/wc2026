from __future__ import annotations

import base64
import json
import os
import subprocess
import re
import sys
import time
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from pathlib import Path
from typing import Any

import requests
from flask import Flask, Response, jsonify, render_template, request

try:  # optional at import time (installed on the server); keeps local tooling working
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover
    webpush = None

    class WebPushException(Exception):
        pass


ROOT = Path(__file__).resolve().parent
APP = Flask(__name__, static_folder=str(ROOT / "static"), template_folder=str(ROOT / "templates"))
BASELINE_CSV = ROOT.parent / "world_cup_2026_market_odds_polymarket_kalshi.csv"
MATCH_BASELINE_JSON = ROOT.parent / "world_cup_2026_match_market_baseline.json"
BRACKET_GENERATION_STATE_JSON = ROOT / ".bracket-generation-state.json"

POLYMARKET_EVENT_URL = "https://gamma-api.polymarket.com/events?slug=world-cup-winner"
POLYMARKET_EVENTS_URL = "https://gamma-api.polymarket.com/events"
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_STANDINGS_URL = "https://site.web.api.espn.com/apis/v2/sports/soccer/fifa.world/standings"

CACHE_TTL_SECONDS = 60
_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_BRACKET_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}

# Durable storage for the pre-game / in-play market captures (survives restarts).
# When unset, the app falls back to the local JSON file (dev) / in-memory.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_TABLE = "match_captures"

# "Message us" form -> Web3Forms (free form-to-email). The free tier only accepts
# client-side submissions, so the key is injected into the page from this env var.
WEB3FORMS_KEY = os.environ.get("WEB3FORMS_KEY", "")

# Web Push (VAPID). Public key is safe to ship to the browser; private key is a secret.
VAPID_PUBLIC_KEY = "BBZXwxtWEIPyQ3tc_UIdks262j9ehKlL7qeTmVBOrgRq5JDhLPC61AI33OpaWeohOeCJ--joHVOD_q7rkdwVEXY"
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")  # base64-encoded PKCS8 PEM
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:d.timoshin@ami-business.com")
CHECK_GOALS_TOKEN = os.environ.get("CHECK_GOALS_TOKEN", "")

COUNTRY_CODE_BY_NAME = {
    "ca": "CA",
    "can": "CA",
    "canada": "CA",
    "mx": "MX",
    "mex": "MX",
    "mexico": "MX",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "united states of america": "US",
}

HOST_CITY_COUNTRY_CODES = {
    "arlington": "US",
    "atlanta": "US",
    "boston": "US",
    "dallas": "US",
    "east rutherford": "US",
    "foxborough": "US",
    "houston": "US",
    "inglewood": "US",
    "kansas city": "US",
    "los angeles": "US",
    "miami": "US",
    "miami gardens": "US",
    "new york": "US",
    "new york new jersey": "US",
    "philadelphia": "US",
    "san francisco": "US",
    "santa clara": "US",
    "seattle": "US",
    "guadalajara": "MX",
    "mexico city": "MX",
    "monterrey": "MX",
    "toronto": "CA",
    "vancouver": "CA",
}


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


def country_code_for(value: str | None, city: str | None = None) -> str | None:
    raw = (value or "").strip()
    if raw:
        code = COUNTRY_CODE_BY_NAME.get(raw.casefold())
        if code:
            return code
        if len(raw) == 2 and raw.isalpha():
            return raw.upper()

    city_key = (city or "").strip().casefold()
    return HOST_CITY_COUNTRY_CODES.get(city_key)


def country_flag(code: str | None) -> str | None:
    if not code or len(code) != 2 or not code.isalpha():
        return None
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code.upper())


def venue_city(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    return raw.split(",", 1)[0].strip() or None


def venue_location(venue: dict[str, Any]) -> dict[str, Any]:
    address = venue.get("address") or {}
    city = venue_city(address.get("city") or venue.get("city"))
    country = address.get("country") or venue.get("country")
    country_code = country_code_for(country, city)
    flag = country_flag(country_code)

    return {
        "city": city,
        "country": country,
        "countryCode": country_code,
        "flag": flag,
    }


def applescript_string(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def send_macos_notification(title: str, message: str) -> bool:
    if sys.platform != "darwin":
        return False

    safe_title = str(title or "World Cup Alert")[:80]
    safe_message = str(message or "")[:240]
    script = (
        f"display notification {applescript_string(safe_message)} "
        f"with title {applescript_string(safe_title)} "
        'sound name "Glass"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


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
    # USD traded on this outcome. Polymarket gives dollar volume directly; Kalshi gives
    # contracts (volume_fp), so approximate USD staked as contracts * price.
    pm_volume = as_float(market.get("volumeNum"))
    if pm_volume is not None:
        volume_usd = pm_volume
    else:
        contracts = as_float(market.get("volume_fp"))
        volume_usd = contracts * mid if (contracts is not None and mid is not None) else None
    return {
        "mid": mid,
        "midPct": pct(mid),
        "bidPct": pct(bid),
        "askPct": pct(ask),
        "lastPct": pct(last),
        "volumeUsd": round(volume_usd) if volume_usd is not None else None,
    }


def pick_from_outcomes(outcomes: dict[str, dict[str, Any]]) -> tuple[str | None, float | None]:
    priced = [(outcome, data.get("mid")) for outcome, data in outcomes.items() if data.get("mid") is not None]
    if not priced:
        return None, None
    return max(priced, key=lambda item: item[1])


def sum_outcome_volume(outcomes: dict[str, dict[str, Any]]) -> int | None:
    total = sum(o["volumeUsd"] for o in outcomes.values() if o.get("volumeUsd"))
    return round(total) if total else None


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
                "pickVolume": (outcomes.get(pick) or {}).get("volumeUsd"),
                "totalVolume": sum_outcome_volume(outcomes),
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
        match_market["pickVolume"] = (match_market["outcomes"].get(pick) or {}).get("volumeUsd")
        match_market["totalVolume"] = sum_outcome_volume(match_market["outcomes"])

    return grouped


def load_match_baseline() -> dict[str, Any]:
    if not MATCH_BASELINE_JSON.exists():
        return {"createdAt": None, "updatedAt": None, "markets": {}}
    try:
        return json.loads(MATCH_BASELINE_JSON.read_text())
    except json.JSONDecodeError:
        return {"createdAt": None, "updatedAt": None, "markets": {}}


def match_phase(status: dict[str, Any]) -> str | None:
    """Which capture window a match is in: 'preGame', 'inPlay', or None once it has ended."""
    if status.get("completed"):
        return None
    if status.get("state") == "in":
        return "inPlay"
    return "preGame"


def phase_container(source_entry: dict[str, Any]) -> dict[str, Any]:
    """Normalise a per-source entry to {preGame?, inPlay?}, migrating the legacy flat format."""
    if "preGame" in source_entry or "inPlay" in source_entry:
        return source_entry
    if source_entry.get("pick") or source_entry.get("outcomes"):
        return {"preGame": source_entry}  # legacy first-seen capture -> pre-game slot
    return source_entry


def _apply_phase_captures(
    markets: dict[str, Any],
    events: list[dict[str, Any]],
    live_sources: dict[str, dict[str, Any]],
    now: str,
) -> list[dict[str, Any]]:
    """Mutate `markets` with this poll's captures; return the rows that changed.

    Overwrites the current phase's capture each poll so it tracks the most-current
    odds, and freezes a capture once the match leaves that phase (completed -> None).
    """
    updates: list[dict[str, Any]] = []
    for event in events:
        key = market_key(event.get("home", {}).get("team"), event.get("away", {}).get("team"))
        if not key:
            continue
        phase = match_phase(event.get("status", {}))
        if phase is None:
            continue
        match_entry = markets.setdefault(key, {})
        for source_name, source_markets in live_sources.items():
            market = source_markets.get(key)
            if not market:
                continue
            data = {**market, "capturedAt": now}
            source_entry = phase_container(match_entry.get(source_name) or {})
            source_entry[phase] = data
            match_entry[source_name] = source_entry
            updates.append({"match_key": key, "source": source_name, "phase": phase, "data": data})
    return updates


def supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _supabase_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def supabase_load_markets() -> dict[str, Any]:
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        params={"select": "match_key,source,phase,data"},
        headers=_supabase_headers(),
        timeout=15,
    )
    response.raise_for_status()
    markets: dict[str, Any] = {}
    for row in response.json():
        markets.setdefault(row["match_key"], {}).setdefault(row["source"], {})[row["phase"]] = row["data"]
    return markets


def supabase_upsert_captures(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers=_supabase_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        json=rows,
        timeout=15,
    )
    response.raise_for_status()


# --- Web Push: subscriptions, match-state diffing, and sending ---


def supabase_load_subscriptions() -> list[dict[str, Any]]:
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/push_subscriptions",
        params={"select": "endpoint,p256dh,auth"},
        headers=_supabase_headers(),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def supabase_delete_subscription(endpoint: str) -> None:
    try:
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/push_subscriptions",
            params={"endpoint": f"eq.{endpoint}"},
            headers=_supabase_headers(),
            timeout=15,
        )
    except requests.RequestException:
        pass


def supabase_load_match_state() -> dict[str, dict[str, Any]]:
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/match_state",
        params={"select": "match_key,home_score,away_score,state,completed"},
        headers=_supabase_headers(),
        timeout=15,
    )
    response.raise_for_status()
    return {row["match_key"]: row for row in response.json()}


def supabase_upsert_match_state(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    requests.post(
        f"{SUPABASE_URL}/rest/v1/match_state",
        headers=_supabase_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        json=rows,
        timeout=15,
    ).raise_for_status()


def supabase_get_app_state(key: str) -> str | None:
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/app_state",
        params={"select": "value", "key": f"eq.{key}"},
        headers=_supabase_headers(),
        timeout=15,
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0]["value"] if rows else None


def supabase_set_app_state(key: str, value: str) -> None:
    requests.post(
        f"{SUPABASE_URL}/rest/v1/app_state",
        headers=_supabase_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        json=[{"key": key, "value": value}],
        timeout=15,
    ).raise_for_status()


def goal_event_notification(old: dict[str, Any] | None, cur: dict[str, Any]) -> dict[str, Any] | None:
    """A push payload for a meaningful change, or None. Mirrors the in-page alerts."""
    teams = f'{cur["home_team"]} vs {cur["away_team"]}'
    score = f'{cur["home_team"]} {cur["home_score"]}-{cur["away_score"]} {cur["away_team"]}'
    if cur["state"] == "in" and (old is None or old.get("state") != "in") and not cur["completed"]:
        return {"title": "Kick-off", "body": teams, "tag": cur["match_key"]}
    if cur["completed"] and (old is None or not old.get("completed")):
        return {"title": "Full time", "body": score, "tag": cur["match_key"]}
    if cur["state"] == "in" and old and old.get("state") == "in":
        if cur["home_score"] != old.get("home_score") or cur["away_score"] != old.get("away_score"):
            return {"title": "Goal!", "body": score, "tag": cur["match_key"]}
    return None


_VAPID_PEM_PATH: str | None = None


def vapid_pem_path() -> str:
    """Write the base64-decoded PKCS8 PEM to a temp file once; pywebpush loads PEM files reliably."""
    global _VAPID_PEM_PATH
    if _VAPID_PEM_PATH is None:
        import tempfile

        fd, path = tempfile.mkstemp(suffix="-vapid.pem")
        os.write(fd, base64.b64decode(VAPID_PRIVATE_KEY))
        os.close(fd)
        _VAPID_PEM_PATH = path
    return _VAPID_PEM_PATH


def send_web_push(subscription: dict[str, Any], payload: dict[str, Any]) -> None:
    webpush(
        subscription_info={
            "endpoint": subscription["endpoint"],
            "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
        },
        data=json.dumps({**payload, "url": "/"}),
        vapid_private_key=vapid_pem_path(),
        vapid_claims={"sub": VAPID_SUBJECT},
        ttl=600,
    )


def push_to_all(subscriptions: list[dict[str, Any]], payload: dict[str, Any]) -> tuple[int, list[str]]:
    sent = 0
    errors: list[str] = []
    for subscription in subscriptions:
        try:
            send_web_push(subscription, payload)
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            errors.append(f"webpush {status}: {str(exc)[:140]}")
            if status in (404, 410):  # subscription gone -> prune it
                supabase_delete_subscription(subscription["endpoint"])
        except Exception as exc:  # pragma: no cover
            errors.append(f"{type(exc).__name__}: {str(exc)[:140]}")
    return sent, errors


def merge_match_baseline(events: list[dict[str, Any]], live_sources: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    now = datetime.now(timezone.utc).isoformat()

    if supabase_enabled():
        try:
            markets = supabase_load_markets()
            supabase_upsert_captures(_apply_phase_captures(markets, events, live_sources, now))
            return markets, "supabase"
        except (requests.RequestException, ValueError, KeyError, TypeError):
            # Supabase unreachable/paused: capture this poll in-memory so the current
            # snapshot still reflects live markets; durable persistence resumes on recovery.
            markets = {}
            _apply_phase_captures(markets, events, live_sources, now)
            return markets, None

    # Local / file fallback (no Supabase configured).
    baseline = load_match_baseline()
    markets = baseline.setdefault("markets", {})
    if baseline.get("createdAt") is None:
        baseline["createdAt"] = now
    if _apply_phase_captures(markets, events, live_sources, now):
        baseline["updatedAt"] = now
        MATCH_BASELINE_JSON.write_text(json.dumps(baseline, indent=2, sort_keys=True))

    return markets, str(MATCH_BASELINE_JSON) if MATCH_BASELINE_JSON.exists() else None


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


def outright_pick(team_a: str | None, team_b: str | None, outright_odds: dict[str, Any]) -> dict[str, Any]:
    if not team_a or not team_b:
        return {"pick": None, "pickPct": None, "source": "unknown"}

    team_a_mid = outright_odds.get(team_a, {}).get("mid")
    team_b_mid = outright_odds.get(team_b, {}).get("mid")
    if team_a_mid is not None and team_b_mid is not None and team_a_mid == team_b_mid:
        return {"pick": "Draw", "pickPct": None, "source": "outright"}

    pick = pick_between(team_a, team_b, outright_odds)
    return {"pick": pick, "pickPct": outright_odds.get(pick, {}).get("midPct"), "source": "outright"}


def capture_pick(match_markets: dict[str, Any], source_name: str, match_key: str | None, phase: str) -> dict[str, Any] | None:
    capture = ((match_markets.get(match_key or "") or {}).get(source_name) or {}).get(phase)
    if capture and capture.get("pick"):
        return {"pick": capture["pick"], "pickPct": capture.get("pickPct"), "source": "match", "volume": capture.get("pickVolume"), "total": capture.get("totalVolume")}
    return None


# One-off exception: these matches were already live when durable capture began,
# so their only "match" capture is a misleading mid-game read. Grade their locked
# pick against the outright odds (the honest pre-kickoff signal) instead.
LOCKED_OUTRIGHT_ONLY = {"Saudi Arabia::Uruguay"}


def pick_for_event(
    source_name: str,
    match_key: str | None,
    team_a: str | None,
    team_b: str | None,
    match_markets: dict[str, Any],
    outright_odds: dict[str, Any],
) -> dict[str, Any]:
    if match_key in LOCKED_OUTRIGHT_ONLY:
        return outright_pick(team_a, team_b, outright_odds)
    # "Locked" = the last pre-game market read. If we never captured one (e.g. the
    # market only appeared after kick-off, or the baseline reset), prefer the in-play
    # market over outright — outright is only a last resort when no match market exists.
    return (
        capture_pick(match_markets, source_name, match_key, "preGame")
        or capture_pick(match_markets, source_name, match_key, "inPlay")
        or outright_pick(team_a, team_b, outright_odds)
    )


def current_match_pick(
    source_name: str,
    match_key: str | None,
    team_a: str | None,
    team_b: str | None,
    match_markets: dict[str, Any],
    outright_odds: dict[str, Any],
) -> dict[str, Any]:
    # "Now" = the last in-play market read (frozen once the match ends), falling back to outright.
    return capture_pick(match_markets, source_name, match_key, "inPlay") or outright_pick(team_a, team_b, outright_odds)


def build_projection(odds: dict[str, Any]) -> dict[str, Any]:
    # Pure market projection: a fact projection with no completed results to override picks.
    projection = build_fact_projection(odds, {})
    return {
        "champion": projection["champion"],
        "finalists": projection["final"]["teams"],
        "runnerUp": projection["runnerUp"],
        "thirdPlace": projection["thirdPlace"],
        "fourthPlace": projection["fourthPlace"],
        "rounds": projection["rounds"],
    }


KNOCKOUT_STAGE_LABELS = {
    "round-of-32": "1/16 Finals",
    "round-of-16": "1/8 Finals",
    "quarterfinals": "1/4 Finals",
    "semifinals": "1/2 Finals",
    "final": "FINAL",
    "3rd-place-match": "3rd Place",
}


def match_group(note: str | None) -> str | None:
    found = re.search(r"Group\s+([A-Z])", note or "")
    return found.group(1) if found else None


def stage_label(slug: str | None, group: str | None) -> str | None:
    if slug == "group-stage":
        return f"Group {group}" if group else None
    return KNOCKOUT_STAGE_LABELS.get(slug or "")


def fetch_scoreboard() -> list[dict[str, Any]]:
    data = fetch_json(ESPN_SCOREBOARD_URL, {"dates": "20260611-20260719", "limit": 500})
    events = []

    for event in data.get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        status = competition.get("status", {})
        status_type = status.get("type", {})
        venue = competition.get("venue") or {}
        competitors = competition.get("competitors") or []
        stage_slug = (event.get("season") or {}).get("slug")
        group_letter = match_group(competition.get("altGameNote"))

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
                "venue": venue.get("fullName"),
                "location": venue_location(venue),
                "stage": stage_slug,
                "group": group_letter,
                "stageLabel": stage_label(stage_slug, group_letter),
                "status": {
                    "state": status_type.get("state"),
                    "completed": bool(status_type.get("completed")),
                    "detail": status_type.get("detail") or status_type.get("description"),
                    "shortDetail": status_type.get("shortDetail"),
                    "displayClock": status.get("displayClock"),
                    "clock": as_float(status.get("clock")),
                    "period": as_float(status.get("period")),
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
    locked_polymarket: dict[str, Any] | None = None,
    locked_kalshi: dict[str, Any] | None = None,
) -> dict[str, Any]:
    match_markets = match_markets or {}
    # "Locked" grades against the pre-kickoff (baseline) outright odds; "now" uses the
    # live outright odds passed as polymarket/kalshi. They differ once the market moves.
    locked_polymarket = polymarket if locked_polymarket is None else locked_polymarket
    locked_kalshi = kalshi if locked_kalshi is None else locked_kalshi
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
        pm_prediction = pick_for_event("polymarket", match_key_value, home_team, away_team, match_markets, locked_polymarket)
        ks_prediction = pick_for_event("kalshi", match_key_value, home_team, away_team, match_markets, locked_kalshi)
        status = event.get("status", {})
        started = bool(status.get("completed") or status.get("state") == "in")
        if started:
            # "Now" only exists once the match has kicked off; it stays (frozen) after it ends.
            pm_current = current_match_pick("polymarket", match_key_value, home_team, away_team, match_markets, polymarket)
            ks_current = current_match_pick("kalshi", match_key_value, home_team, away_team, match_markets, kalshi)
        else:
            pm_current = {"pick": None, "pickPct": None, "source": None}
            ks_current = {"pick": None, "pickPct": None, "source": None}
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
                    "polymarketPickVolume": pm_prediction.get("volume"),
                    "polymarketPickTotal": pm_prediction.get("total"),
                    "polymarketResult": pm_result,
                    "polymarketCurrentPick": pm_current["pick"],
                    "polymarketCurrentPickPct": pm_current["pickPct"],
                    "polymarketCurrentSource": pm_current["source"],
                    "polymarketCurrentVolume": pm_current.get("volume"),
                    "polymarketCurrentTotal": pm_current.get("total"),
                    "kalshiPick": ks_pick,
                    "kalshiPickPct": ks_prediction["pickPct"],
                    "kalshiSource": ks_prediction["source"],
                    "kalshiPickVolume": ks_prediction.get("volume"),
                    "kalshiPickTotal": ks_prediction.get("total"),
                    "kalshiResult": ks_result,
                    "kalshiCurrentPick": ks_current["pick"],
                    "kalshiCurrentPickPct": ks_current["pickPct"],
                    "kalshiCurrentSource": ks_current["source"],
                    "kalshiCurrentVolume": ks_current.get("volume"),
                    "kalshiCurrentTotal": ks_current.get("total"),
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


def top_leader(odds: dict[str, Any]) -> dict[str, Any] | None:
    if not odds:
        return None
    return max(odds.values(), key=lambda item: item.get("mid") or 0)


def _resolve(future: Any, fallback: Any, errors: dict[str, str], label: str) -> Any:
    try:
        return future.result()
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        errors[label] = str(exc)
        return fallback


def build_snapshot() -> dict[str, Any]:
    fetch_errors: dict[str, str] = {}
    baseline_polymarket, baseline_kalshi, baseline_path = load_baseline_odds()

    # Independent network calls run concurrently so a cold snapshot is not the sum of every timeout.
    with ThreadPoolExecutor(max_workers=5) as pool:
        scoreboard_future = pool.submit(fetch_scoreboard)
        standings_future = pool.submit(fetch_standings)
        live_sources_future = pool.submit(fetch_live_match_sources)
        polymarket_future = pool.submit(fetch_polymarket)
        kalshi_future = pool.submit(fetch_kalshi)

        scoreboard = _resolve(scoreboard_future, [], fetch_errors, "espnScoreboard")
        standings = _resolve(standings_future, [], fetch_errors, "espnStandings")
        # fetch_live_match_sources already isolates per-source failures and never raises.
        live_match_sources, match_market_errors = live_sources_future.result()
        # Live outright odds drive the dashboard display; fall back to the baseline snapshot.
        polymarket = _resolve(polymarket_future, baseline_polymarket, fetch_errors, "polymarket")
        kalshi = _resolve(kalshi_future, baseline_kalshi, fetch_errors, "kalshi")

    live_odds = not fetch_errors.get("polymarket") and not fetch_errors.get("kalshi") and bool(polymarket) and bool(kalshi)
    # Grade "locked" picks against the pre-kickoff baseline outright odds (fall back to live).
    locked_polymarket = baseline_polymarket or polymarket
    locked_kalshi = baseline_kalshi or kalshi

    match_markets, match_baseline_path = merge_match_baseline(scoreboard, live_match_sources)
    comparisons = compare_events(scoreboard, polymarket, kalshi, match_markets, locked_polymarket, locked_kalshi)
    team_rows = build_team_rows(standings, polymarket, kalshi)

    pm_top = top_leader(polymarket)
    ks_top = top_leader(kalshi)

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
        "predictionMode": "liveMarkets" if live_odds else "baselineCsv",
        "matchMarketErrors": match_market_errors,
        "fetchErrors": fetch_errors,
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


def consensus_odds(polymarket: dict[str, Any], kalshi: dict[str, Any]) -> dict[str, dict[str, Any]]:
    odds: dict[str, dict[str, Any]] = {}
    for team in sorted(set(polymarket) | set(kalshi)):
        mid = consensus_for(team, polymarket, kalshi)
        if mid is None:
            continue
        odds[team] = {"team": team, "mid": mid, "midPct": pct(mid)}
    return odds


def current_consensus_odds() -> tuple[dict[str, dict[str, Any]], list[str]]:
    baseline_polymarket, baseline_kalshi, _ = load_baseline_odds()
    sources: list[str] = []

    try:
        polymarket = fetch_polymarket()
        sources.append("Polymarket live")
    except requests.RequestException:
        polymarket = baseline_polymarket
        sources.append("Polymarket baseline")

    try:
        kalshi = fetch_kalshi()
        sources.append("Kalshi live")
    except requests.RequestException:
        kalshi = baseline_kalshi
        sources.append("Kalshi baseline")

    return consensus_odds(polymarket, kalshi), sources


def actual_winners_by_pair(events: list[dict[str, Any]]) -> dict[str, str]:
    winners: dict[str, str] = {}
    for event in events:
        winner = event.get("winner")
        if not event.get("status", {}).get("completed") or not winner or winner == "Draw":
            continue
        key = market_key(event.get("home", {}).get("team"), event.get("away", {}).get("team"))
        if key:
            winners[key] = winner
    return winners


def bracket_pick(team_a: str, team_b: str, odds: dict[str, Any], actual_winners: dict[str, str]) -> tuple[str, str]:
    actual = actual_winners.get(market_key(team_a, team_b) or "")
    if actual in {team_a, team_b}:
        return actual, "actual"

    value_a = odds.get(team_a, {}).get("mid") or 0
    value_b = odds.get(team_b, {}).get("mid") or 0
    return (team_a, "market") if value_a >= value_b else (team_b, "market")


def build_fact_projection(odds: dict[str, Any], actual_winners: dict[str, str]) -> dict[str, Any]:
    def build_round(pairings: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], list[str]]:
        matches = []
        winners = []
        for team_a, team_b in pairings:
            winner, source = bracket_pick(team_a, team_b, odds, actual_winners)
            loser = team_b if winner == team_a else team_a
            matches.append({"teams": [team_a, team_b], "winner": winner, "loser": loser, "source": source})
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

    left_final = left_rounds[-1][0]
    right_final = right_rounds[-1][0]
    champion, final_source = bracket_pick(left_final["winner"], right_final["winner"], odds, actual_winners)
    runner_up = right_final["winner"] if champion == left_final["winner"] else left_final["winner"]
    third_place, third_source = bracket_pick(left_final["loser"], right_final["loser"], odds, actual_winners)
    fourth_place = right_final["loser"] if third_place == left_final["loser"] else left_final["loser"]

    return {
        "champion": champion,
        "runnerUp": runner_up,
        "thirdPlace": third_place,
        "fourthPlace": fourth_place,
        "final": {"teams": [left_final["winner"], right_final["winner"]], "winner": champion, "loser": runner_up, "source": final_source},
        "thirdPlaceMatch": {
            "teams": [left_final["loser"], right_final["loser"]],
            "winner": third_place,
            "loser": fourth_place,
            "source": third_source,
        },
        "rounds": {"left": left_rounds, "right": right_rounds},
    }


def baseline_projection() -> dict[str, Any] | None:
    odds = baseline_consensus_odds()
    if not odds:
        return None
    return build_fact_projection(odds, {})


def baseline_consensus_odds() -> dict[str, dict[str, Any]]:
    baseline_polymarket, baseline_kalshi, _ = load_baseline_odds()
    return consensus_odds(baseline_polymarket, baseline_kalshi)


def projection_slot_teams(projection: dict[str, Any]) -> dict[str, str]:
    slots: dict[str, str] = {}

    for side in ("left", "right"):
        for round_idx, matches in enumerate(projection.get("rounds", {}).get(side, [])):
            for match_idx, match in enumerate(matches):
                for team_idx, team in enumerate(match.get("teams", [])):
                    slots[f"{side}-{round_idx}-{match_idx}-{team_idx}"] = team

    for slot_name, match_key in (("final", "final"), ("third", "thirdPlaceMatch")):
        for team_idx, team in enumerate(projection.get(match_key, {}).get("teams", [])):
            slots[f"{slot_name}-{team_idx}"] = team

    return slots


def projection_slot_entries(projection: dict[str, Any], odds: dict[str, Any]) -> dict[str, tuple[str, float | None]]:
    entries: dict[str, tuple[str, float | None]] = {}
    slots = projection_slot_teams(projection)
    for slot, team in slots.items():
        value = odds.get(team, {}).get("midPct")
        entries[slot] = (team, round(value, 1) if value is not None else None)
    return entries


def changed_projection_slots(
    current: dict[str, Any],
    baseline: dict[str, Any] | None,
    current_odds: dict[str, Any],
    baseline_odds: dict[str, Any],
) -> set[str]:
    if not baseline:
        return set()

    current_slots = projection_slot_entries(current, current_odds)
    baseline_slots = projection_slot_entries(baseline, baseline_odds)
    return {slot for slot, entry in current_slots.items() if baseline_slots.get(slot) != entry}


def projection_composition(projection: dict[str, Any]) -> dict[str, str]:
    composition = projection_slot_teams(projection)
    for key in ("champion", "runnerUp", "thirdPlace", "fourthPlace"):
        value = projection.get(key)
        if value:
            composition[key] = value
    return dict(sorted(composition.items()))


def projection_composition_hash(composition: dict[str, str]) -> str:
    encoded = json.dumps(composition, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def svg_team_label(team: str) -> str:
    return "B & Herz" if team == "Bosnia-Herzegovina" else team


def read_latest_bracket_generation() -> dict[str, Any] | None:
    try:
        data = json.loads(BRACKET_GENERATION_STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_latest_bracket_generation(payload: dict[str, Any]) -> None:
    state = {
        "generatedAt": payload["generatedAt"].isoformat(),
        "composition": payload["composition"],
        "compositionHash": payload["compositionHash"],
        "sources": payload["sources"],
    }
    try:
        BRACKET_GENERATION_STATE_JSON.write_text(
            json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        pass


def fmt_bracket_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def render_bracket_svg(
    projection: dict[str, Any],
    odds: dict[str, Any],
    sources: list[str],
    generated_at: datetime,
    changed_slots: set[str] | None = None,
    baseline_champion: str | None = None,
) -> str:
    changed_slots = changed_slots or set()
    width, height = 1600, 900
    box_w, box_h = 132, 54
    left_x = [68, 214, 360, 506]
    right_x = [1400, 1254, 1108, 962]
    round_ys = [
        [164, 234, 304, 374, 454, 524, 594, 664],
        [199, 339, 489, 629],
        [269, 559],
        [414],
    ]
    final_x, final_y = 734, 414
    third_x, third_y = 734, 674

    def team_pct(team: str) -> str:
        return fmt_bracket_pct(odds.get(team, {}).get("midPct"))

    def top_consensus() -> str:
        rows = sorted(odds.values(), key=lambda item: item.get("mid") or 0, reverse=True)[:8]
        return "  |  ".join(f"{svg_team_label(row['team'])} {fmt_bracket_pct(row.get('midPct'))}" for row in rows)

    def match_box(x: int, y: int, match: dict[str, Any], slot_prefix: str, highlight: bool = False) -> str:
        team_a, team_b = match["teams"]
        winner = match["winner"]
        source = match.get("source")
        fill = "#fffbea" if highlight else "#fbfcfe"
        source_mark = "FACT" if source == "actual" else ""
        rows = []
        for idx, team in enumerate((team_a, team_b)):
            row_y = y + 20 + idx * 25
            is_winner = team == winner
            changed_class = " changedEntry" if f"{slot_prefix}-{idx}" in changed_slots else ""
            rows.append(
                f'<text x="{x + 8}" y="{row_y}" class="team {"winner" if is_winner else "loser"}{changed_class}">{escape(svg_team_label(team))}</text>'
                f'<text x="{x + box_w - 8}" y="{row_y}" class="pct {"winner" if is_winner else "loser"}{changed_class}">{team_pct(team)}</text>'
            )
        mark = f'<text x="{x + box_w - 8}" y="{y - 5}" class="fact">{source_mark}</text>' if source_mark else ""
        return (
            f'{mark}<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="4" class="box" fill="{fill}" />'
            f'<line x1="{x}" y1="{y + box_h / 2}" x2="{x + box_w}" y2="{y + box_h / 2}" class="divider" />'
            + "".join(rows)
        )

    def connector(prev_x: int, prev_ys: list[int], next_x: int, next_ys: list[int], *, leftward: bool) -> str:
        lines = []
        x1, x2 = (prev_x + box_w, next_x) if leftward else (prev_x, next_x + box_w)
        xm = (x1 + x2) / 2
        for idx, next_y in enumerate(next_ys):
            y_a = prev_ys[idx * 2] + box_h / 2
            y_b = prev_ys[idx * 2 + 1] + box_h / 2
            y_n = next_y + box_h / 2
            lines.append(
                f'<path d="M{x1},{y_a} H{xm} M{x1},{y_b} H{xm} M{xm},{y_a} V{y_b} M{xm},{y_n} H{x2}" class="connector" />'
            )
        return "".join(lines)

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        """<style>
          .title { font: 800 28px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #111827; }
          .subtitle { font: italic 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #7a7f87; }
          .round { font: 800 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #3f4650; }
          .box { stroke: #9ba1aa; stroke-width: 1.4; }
          .divider { stroke: #e2e5ea; stroke-width: 1; }
          .connector { stroke: #a7abb2; stroke-width: 1.2; fill: none; }
          .team { font: 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
          .pct { font: 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; text-anchor: end; }
          .winner { fill: #111827; font-weight: 800; }
          .loser { fill: #8a9099; }
          .champion { font: 800 16px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #a38316; text-anchor: middle; }
          .changedEntry { fill: #1e3a8a; }
          .watermark { font: 800 34px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #e5e7eb; text-anchor: middle; }
          .small { font: 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #68707a; }
          .foot { font: 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #4b5563; }
          .fact { font: 800 9px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #166534; text-anchor: end; }
        </style>""",
        '<rect width="1600" height="900" fill="#ffffff" />',
        f'<text x="800" y="68" class="title" text-anchor="middle">FIFA World Cup 2026 - Knock-Out Stage: Current Market + Facts</text>',
        f'<text x="800" y="90" class="subtitle" text-anchor="middle">Numbers are current avg title-win Yes prices (%) from Polymarket + Kalshi where available. Completed facts override matching pairings.</text>',
        f'<text x="800" y="112" class="subtitle" text-anchor="middle">Generated {escape(generated_at.strftime("%d %b %Y %H:%M UTC"))}. Winner in bold; dark blue entries changed team or percentage from the initial 13 Jun bracket.</text>',
        '<text x="800" y="355" class="watermark">POLYMARKET + KALSHI</text>',
    ]

    headers = [
        ("Round of 32", left_x[0] + box_w / 2),
        ("Round of 16", left_x[1] + box_w / 2),
        ("Quarter-finals", left_x[2] + box_w / 2),
        ("Semi-finals", left_x[3] + box_w / 2),
        ("Final", final_x + box_w / 2),
        ("Semi-finals", right_x[3] + box_w / 2),
        ("Quarter-finals", right_x[2] + box_w / 2),
        ("Round of 16", right_x[1] + box_w / 2),
        ("Round of 32", right_x[0] + box_w / 2),
    ]
    svg_parts.extend(f'<text x="{x}" y="145" class="round" text-anchor="middle">{label}</text>' for label, x in headers)

    for round_idx in range(3):
        svg_parts.append(connector(left_x[round_idx], round_ys[round_idx], left_x[round_idx + 1], round_ys[round_idx + 1], leftward=True))
        svg_parts.append(connector(right_x[round_idx], round_ys[round_idx], right_x[round_idx + 1], round_ys[round_idx + 1], leftward=False))
    svg_parts.append(f'<path d="M{left_x[3] + box_w},{round_ys[3][0] + box_h / 2} H{final_x}" class="connector" />')
    svg_parts.append(f'<path d="M{right_x[3]},{round_ys[3][0] + box_h / 2} H{final_x + box_w}" class="connector" />')

    for round_idx, matches in enumerate(projection["rounds"]["left"]):
        for match_idx, (match, y) in enumerate(zip(matches, round_ys[round_idx])):
            svg_parts.append(match_box(left_x[round_idx], y, match, f"left-{round_idx}-{match_idx}"))
    for round_idx, matches in enumerate(projection["rounds"]["right"]):
        for match_idx, (match, y) in enumerate(zip(matches, round_ys[round_idx])):
            svg_parts.append(match_box(right_x[round_idx], y, match, f"right-{round_idx}-{match_idx}"))

    champion_class = "champion changedEntry" if baseline_champion and projection["champion"] != baseline_champion else "champion"
    svg_parts.append(f'<text x="{final_x + box_w / 2}" y="{final_y - 18}" class="{champion_class}">Champion: {escape(svg_team_label(projection["champion"]))}</text>')
    svg_parts.append(match_box(final_x, final_y, projection["final"], "final", highlight=True))
    svg_parts.append(f'<text x="{third_x + box_w / 2}" y="{third_y - 18}" class="round" text-anchor="middle">Third-place play-off</text>')
    svg_parts.append(match_box(third_x, third_y, projection["thirdPlaceMatch"], "third"))
    svg_parts.append(f'<text x="800" y="763" class="small" text-anchor="middle">Top consensus: {escape(top_consensus())}</text>')
    svg_parts.append(
        f'<text x="28" y="815" class="foot">Sources: {escape(" / ".join(sources))}; ESPN scoreboard facts. Bracket order follows supplied reference image.</text>'
    )
    svg_parts.append('<text x="1528" y="815" class="foot" text-anchor="end">Data/API check</text>')
    svg_parts.append("</svg>")
    return "".join(svg_parts)


def build_bracket_payload(mark_generated: bool = False, render_svg: bool = True) -> dict[str, Any]:
    odds, sources = current_consensus_odds()
    if not odds:
        baseline_polymarket, baseline_kalshi, _ = load_baseline_odds()
        odds = consensus_odds(baseline_polymarket, baseline_kalshi)
        sources = ["saved baseline"]
    try:
        events = fetch_scoreboard()
        fact_sources = sources + ["ESPN facts"]
    except requests.RequestException:
        events = []
        fact_sources = sources + ["ESPN facts unavailable"]
    projection = build_fact_projection(odds, actual_winners_by_pair(events))
    initial_odds = baseline_consensus_odds()
    initial_projection = build_fact_projection(initial_odds, {}) if initial_odds else None
    generated_at = datetime.now(timezone.utc)
    composition = projection_composition(projection)
    payload = {
        "projection": projection,
        "odds": odds,
        "sources": fact_sources,
        "generatedAt": generated_at,
        "composition": composition,
        "compositionHash": projection_composition_hash(composition),
    }
    # The status poll only needs the composition hash, so skip the (relatively costly) SVG render there.
    if render_svg:
        payload["svg"] = render_bracket_svg(
            projection,
            odds,
            fact_sources,
            generated_at,
            changed_projection_slots(projection, initial_projection, odds, initial_odds),
            initial_projection.get("champion") if initial_projection else None,
        )
    if mark_generated:
        write_latest_bracket_generation(payload)
    return payload


def build_bracket_svg(mark_generated: bool = False) -> str:
    return build_bracket_payload(mark_generated=mark_generated)["svg"]


@APP.get("/")
def index():
    # Web3Forms free tier requires client-side submission, so inject the key into
    # the page (from the env var, kept out of the repo) for the browser to use.
    return render_template("index.html", web3forms_key=WEB3FORMS_KEY, vapid_public_key=VAPID_PUBLIC_KEY)


@APP.get("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole site.
    return Response(
        (ROOT / "static" / "sw.js").read_text(),
        mimetype="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@APP.post("/api/push/subscribe")
def push_subscribe():
    data = request.get_json(silent=True) or {}
    sub = data.get("subscription") or {}
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return jsonify({"ok": False, "error": "invalid subscription"}), 400
    if not supabase_enabled():
        return jsonify({"ok": False, "error": "storage not configured"}), 503
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/push_subscriptions",
            headers=_supabase_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            json=[{"endpoint": endpoint, "p256dh": p256dh, "auth": auth}],
            timeout=15,
        ).raise_for_status()
    except (requests.RequestException, ValueError):
        return jsonify({"ok": False, "error": "store failed"}), 502
    return jsonify({"ok": True})


@APP.post("/api/push/unsubscribe")
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    if not endpoint:
        return jsonify({"ok": False, "error": "missing endpoint"}), 400
    if supabase_enabled():
        supabase_delete_subscription(endpoint)
    return jsonify({"ok": True})


@APP.get("/api/snapshot")
def snapshot():
    now = time.time()
    if _CACHE["payload"] is not None and now - _CACHE["at"] < CACHE_TTL_SECONDS:
        return jsonify({**_CACHE["payload"], "cached": True})

    try:
        payload = build_snapshot()
    except Exception as exc:  # last-resort guard: serve the last good snapshot instead of a 500
        if _CACHE["payload"] is not None:
            return jsonify({**_CACHE["payload"], "cached": True, "stale": True, "error": str(exc)})
        return jsonify({"error": str(exc)}), 503

    _CACHE["payload"] = payload
    _CACHE["at"] = now
    return jsonify({**payload, "cached": False})


@APP.get("/api/desktop-alerts/capability")
def desktop_alerts_capability():
    return jsonify({"macosNative": sys.platform == "darwin"})


@APP.post("/api/desktop-alert")
def desktop_alert():
    data = request.get_json(silent=True) or {}
    title = f"World Cup: {data.get('title') or 'Score alert'}"
    message = data.get("text") or ""
    if not message:
        return jsonify({"sent": False, "error": "missing text"}), 400

    sent = send_macos_notification(title, message)
    return jsonify({"sent": sent, "macosNative": sys.platform == "darwin"}), (200 if sent else 503)


@APP.route("/check-goals", methods=["GET", "POST"])
def check_goals():
    # Triggered by a scheduler (Supabase pg_cron). Diffs ESPN scores against the
    # stored match state and sends a Web Push for kick-offs, goals and full time.
    if not CHECK_GOALS_TOKEN or request.args.get("token") != CHECK_GOALS_TOKEN:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if not (supabase_enabled() and VAPID_PRIVATE_KEY and webpush is not None):
        return jsonify({"ok": False, "error": "not configured"}), 503

    try:
        events = fetch_scoreboard()
        prior = supabase_load_match_state()
    except (requests.RequestException, ValueError):
        return jsonify({"ok": False, "error": "fetch"}), 502

    notifications: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for event in events:
        key = market_key(event.get("home", {}).get("team"), event.get("away", {}).get("team"))
        if not key:
            continue
        status = event.get("status", {})
        cur = {
            "match_key": key,
            "home_team": event.get("home", {}).get("team"),
            "away_team": event.get("away", {}).get("team"),
            "home_score": int(event.get("home", {}).get("score") or 0),
            "away_score": int(event.get("away", {}).get("score") or 0),
            "state": status.get("state"),
            "completed": bool(status.get("completed")),
            "winner": event.get("winner"),
        }
        old = prior.get(key)
        notification = goal_event_notification(old, cur)
        if notification and old is not None:  # only on a transition from a known state (no first-run spam)
            notifications.append(notification)
        if old is None or any(cur[k] != old.get(k) for k in ("home_score", "away_score", "state", "completed")):
            changed.append(cur)

    # New-favorite: diff the consensus title-odds leader against the stored one.
    try:
        odds, _ = current_consensus_odds()
        leader = top_leader(odds)
        favorite = leader["team"] if leader else None
        if favorite:
            previous_favorite = supabase_get_app_state("favorite")
            if previous_favorite and previous_favorite != favorite:
                notifications.append({"title": "New favorite", "body": f"We have a new favorite - {favorite}", "tag": "favorite"})
            supabase_set_app_state("favorite", favorite)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass

    sent = 0
    push_errors: list[str] = []
    if notifications:
        try:
            subscriptions = supabase_load_subscriptions()
        except (requests.RequestException, ValueError):
            subscriptions = []
        for notification in notifications:
            count, errors = push_to_all(subscriptions, notification)
            sent += count
            push_errors.extend(errors)

    try:
        supabase_upsert_match_state(changed)
    except (requests.RequestException, ValueError):
        pass

    result = {"ok": True, "events": len(notifications), "pushes": sent, "tracked": len(changed)}
    if push_errors:
        result["pushErrors"] = push_errors[:5]
    return jsonify(result)


@APP.get("/api/bracket-status")
def bracket_status():
    now = time.time()
    if _BRACKET_STATUS_CACHE["payload"] is None or now - _BRACKET_STATUS_CACHE["at"] >= CACHE_TTL_SECONDS:
        _BRACKET_STATUS_CACHE["payload"] = build_bracket_payload(mark_generated=False, render_svg=False)
        _BRACKET_STATUS_CACHE["at"] = now
    payload = _BRACKET_STATUS_CACHE["payload"]
    latest = read_latest_bracket_generation()
    latest_hash = latest.get("compositionHash") if latest else None
    return jsonify(
        {
            "changed": bool(latest_hash and latest_hash != payload["compositionHash"]),
            "compositionHash": payload["compositionHash"],
            "generatedAt": payload["generatedAt"].isoformat(),
            "hasLatestGeneration": bool(latest_hash),
            "latestGeneratedAt": latest.get("generatedAt") if latest else None,
        }
    )


@APP.get("/bracket")
def bracket_page():
    svg = build_bracket_svg(mark_generated=True)
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>World Cup Current Bracket</title>
    <script defer src="https://cloud.umami.is/script.js" data-website-id="868c8fa8-c8cf-4843-84b3-8231cd582298" data-domains="wc2026-m91b.onrender.com"></script>
    <style>
      body {{
        margin: 0;
        background: #f6f7f9;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      .bar {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 12px 16px;
        background: #ffffff;
        border-bottom: 1px solid #d9dee5;
      }}
      .bar strong {{
        color: #111827;
      }}
      .bar a {{
        border: 1px solid #d9dee5;
        border-radius: 8px;
        padding: 8px 11px;
        color: #111827;
        font-size: 13px;
        font-weight: 700;
        text-decoration: none;
      }}
      .frame {{
        padding: 16px;
        overflow: auto;
      }}
      .frame svg {{
        display: block;
        width: min(1600px, 100%);
        height: auto;
        margin: 0 auto;
        background: #ffffff;
        box-shadow: 0 12px 34px rgba(24, 32, 42, 0.08);
      }}
    </style>
  </head>
  <body>
    <div class="bar">
      <strong>Current Bracket Image</strong>
      <a href="/api/bracket.svg" download="world-cup-current-bracket.svg">Download SVG</a>
    </div>
    <div class="frame">{svg}</div>
  </body>
</html>"""
    return Response(html, mimetype="text/html")


@APP.get("/api/bracket.svg")
def bracket_svg():
    svg = build_bracket_svg(mark_generated=True)
    return Response(
        svg,
        mimetype="image/svg+xml",
        headers={"Content-Disposition": 'inline; filename="world-cup-current-bracket.svg"'},
    )


if __name__ == "__main__":
    APP.run(host="127.0.0.1", port=5055, debug=False)
