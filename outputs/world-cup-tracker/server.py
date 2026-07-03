from __future__ import annotations

import base64
from collections import defaultdict, deque
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
LOG = logging.getLogger(__name__)

# Upstream/network failures during snapshot refresh; stale cache is served when available.
_SNAPSHOT_RECOVERABLE_ERRORS = (requests.RequestException, ValueError, KeyError, TypeError)
BASELINE_CSV = ROOT.parent / "world_cup_2026_market_odds_polymarket_kalshi.csv"
MATCH_BASELINE_JSON = Path(
    os.environ.get("WC_MATCH_BASELINE_PATH", ROOT.parent / "world_cup_2026_match_market_baseline.json")
)
BRACKET_GENERATION_STATE_JSON = Path(
    os.environ.get("WC_BRACKET_STATE_PATH", ROOT / ".bracket-generation-state.json")
)

POLYMARKET_EVENT_URL = "https://gamma-api.polymarket.com/events?slug=world-cup-winner"
POLYMARKET_EVENTS_URL = "https://gamma-api.polymarket.com/events"
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_STANDINGS_URL = "https://site.web.api.espn.com/apis/v2/sports/soccer/fifa.world/standings"

CACHE_TTL_SECONDS = 60
LIVE_CACHE_TTL_SECONDS = 20  # faster refresh while a match is in play (live goals/penalties)


def _cache_ttl(payload: dict[str, Any] | None) -> int:
    matches = (payload or {}).get("matches") or []
    live = any((match.get("status") or {}).get("state") == "in" for match in matches)
    return LIVE_CACHE_TTL_SECONDS if live else CACHE_TTL_SECONDS
_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_BRACKET_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_CACHE_LOCK = threading.Lock()
_BRACKET_STATUS_LOCK = threading.Lock()
_MATCH_BASELINE_LOCK = threading.RLock()
_CACHE_REFRESHING = False
_PUSH_RATE_LOCK = threading.Lock()
_PUSH_RATE_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)

PUSH_RATE_WINDOW_SECONDS = 10 * 60
PUSH_RATE_LIMIT = 20
PUSH_ENDPOINT_HOSTS = {
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
}

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

KNOCKOUT_STAGE_SLUGS = {
    "round-of-32",
    "round-of-16",
    "quarterfinals",
    "semifinals",
    "3rd-place-match",
    "final",
}

# ESPN numbers knockout matches chronologically. These orders arrange those
# official slots into the left/right trees used by the supplied bracket image.
KNOCKOUT_EVENT_NUMBERS = {
    "left": {
        "round-of-32": [1, 4, 3, 6, 11, 12, 9, 10],
        "round-of-16": [1, 2, 5, 6],
        "quarterfinals": [1, 2],
        "semifinals": [1],
    },
    "right": {
        "round-of-32": [2, 5, 7, 8, 14, 16, 13, 15],
        "round-of-16": [3, 4, 7, 8],
        "quarterfinals": [3, 4],
        "semifinals": [2],
    },
}

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


def parse_datetime(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def event_market_key(event: dict[str, Any]) -> str | None:
    event_id = event.get("id")
    if event_id:
        return f"event:{event_id}"

    pair = market_key(event.get("home", {}).get("team"), event.get("away", {}).get("team"))
    if not pair:
        return None
    scheduled = parse_datetime(event.get("date"))
    return f"{pair}::{scheduled.isoformat()}" if scheduled else pair


def is_fixture_placeholder(team: str | None) -> bool:
    value = (team or "").strip().casefold()
    if not value:
        return True
    return (
        " winner" in value
        or " loser" in value
        or " place" in value
        or value.startswith("group ")
        or value in {"tbd", "to be determined"}
    )


def live_market_for_event(source_markets: dict[str, dict[str, Any]], event: dict[str, Any]) -> dict[str, Any] | None:
    pair = market_key(event.get("home", {}).get("team"), event.get("away", {}).get("team"))
    if not pair:
        return None

    direct = source_markets.get(pair)
    if direct:
        return direct

    candidates = [market for market in source_markets.values() if market.get("pairKey") == pair]
    if not candidates:
        return None

    event_time = parse_datetime(event.get("date"))
    if event_time is None:
        return candidates[0] if len(candidates) == 1 else None

    dated_candidates = []
    for market in candidates:
        scheduled = parse_datetime(market.get("scheduledAt"))
        if scheduled is not None:
            dated_candidates.append((abs((scheduled - event_time).total_seconds()), market))

    if not dated_candidates:
        return candidates[0] if len(candidates) == 1 else None

    distance, closest = min(dated_candidates, key=lambda item: item[0])
    return closest if distance <= 24 * 60 * 60 else None


def locked_markets_for_event(match_markets: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    event_key = event_market_key(event)
    if event_key and event_key in match_markets:
        return match_markets[event_key]

    pair = market_key(event.get("home", {}).get("team"), event.get("away", {}).get("team"))
    legacy_entry = match_markets.get(pair or "", {})
    event_time = parse_datetime(event.get("date"))
    if not legacy_entry or event_time is None:
        return {}

    for source_entry in legacy_entry.values():
        phases = phase_container(source_entry or {})
        for capture in phases.values():
            scheduled = parse_datetime((capture or {}).get("scheduledAt"))
            if scheduled and abs((scheduled - event_time).total_seconds()) <= 24 * 60 * 60:
                return legacy_entry
    return {}


def valid_push_endpoint(endpoint: str) -> bool:
    if not endpoint or len(endpoint) > 2048:
        return False
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    return host in PUSH_ENDPOINT_HOSTS or host.endswith(".notify.windows.com")


def push_rate_limited(client_key: str, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    with _PUSH_RATE_LOCK:
        attempts = _PUSH_RATE_ATTEMPTS[client_key]
        while attempts and current - attempts[0] >= PUSH_RATE_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= PUSH_RATE_LIMIT:
            return True
        attempts.append(current)
        return False


def push_client_key() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return forwarded or request.remote_addr or "unknown"


def book_mid(bid: float | None, ask: float | None, fallback: float | None) -> float | None:
    """Midpoint of a genuine two-sided book. An empty/illiquid book reports
    bid 0 / ask 1.00, whose midpoint (0.50) is meaningless — so require a real
    bid (> 0) and a real ask (< 1) and otherwise use the fallback price
    (last trade / outcome price)."""
    if bid is not None and ask is not None and bid > 0 and ask < 1:
        return (bid + ask) / 2
    return fallback


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
    mid = book_mid(bid, ask, outcome_price if outcome_price is not None else last)
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
        mid = book_mid(bid, ask, outcome_price)
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
        mid = book_mid(bid, ask, last)
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
        scheduled_at = event.get("endDate")
        for market in event.get("markets", []):
            if market.get("sportsMarketType") and market.get("sportsMarketType") != "moneyline":
                continue
            scheduled_at = scheduled_at or market.get("gameStartTime") or market.get("endDate")

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
            source_key = f"{key}::{scheduled_at}" if scheduled_at else key
            markets[source_key] = {
                "source": "polymarket",
                "eventTitle": title,
                "teams": [team_a, team_b],
                "pairKey": key,
                "scheduledAt": scheduled_at,
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

        event_ticker = market.get("event_ticker") or key
        if event_ticker not in grouped:
            grouped[event_ticker] = {
                "source": "kalshi",
                "eventTitle": title,
                "eventTicker": market.get("event_ticker"),
                "teams": [team_a, team_b],
                "pairKey": key,
                "scheduledAt": market.get("expected_expiration_time") or market.get("close_time"),
                "outcomes": {},
            }

        grouped[event_ticker]["outcomes"][outcome] = {
            **parse_yes_price(market),
            "ticker": market.get("ticker"),
        }

    markets: dict[str, dict[str, Any]] = {}
    for match_market in grouped.values():
        pick, pick_price = pick_from_outcomes(match_market["outcomes"])
        match_market["pick"] = pick
        match_market["pickPct"] = pct(pick_price)
        match_market["pickVolume"] = (match_market["outcomes"].get(pick) or {}).get("volumeUsd")
        match_market["totalVolume"] = sum_outcome_volume(match_market["outcomes"])
        pair = match_market["pairKey"]
        scheduled_at = match_market.get("scheduledAt")
        source_key = f"{pair}::{scheduled_at}" if scheduled_at else pair
        markets[source_key] = match_market

    return markets


def load_match_baseline() -> dict[str, Any]:
    with _MATCH_BASELINE_LOCK:
        if not MATCH_BASELINE_JSON.exists():
            return {"createdAt": None, "updatedAt": None, "markets": {}}
        try:
            return json.loads(MATCH_BASELINE_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"createdAt": None, "updatedAt": None, "markets": {}}


def write_match_baseline(baseline: dict[str, Any]) -> None:
    MATCH_BASELINE_JSON.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = MATCH_BASELINE_JSON.with_name(f".{MATCH_BASELINE_JSON.name}.tmp")
    temporary_path.write_text(
        json.dumps(baseline, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(MATCH_BASELINE_JSON)


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
    captured_at = parse_datetime(now)
    for event in events:
        key = event_market_key(event)
        if not key:
            continue
        phase = match_phase(event.get("status", {}))
        if phase is None:
            continue
        scheduled_at = parse_datetime(event.get("date"))
        if phase == "preGame" and captured_at and scheduled_at and captured_at >= scheduled_at:
            continue
        match_entry = markets.setdefault(key, {})
        for source_name, source_markets in live_sources.items():
            market = live_market_for_event(source_markets, event)
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
    minute = cur.get("minute")
    timed_score = f"{minute}: {score}" if minute else score
    if cur["state"] == "in" and (old is None or old.get("state") != "in") and not cur["completed"]:
        return {"title": "Kick-off", "body": teams, "tag": cur["match_key"]}
    if cur["completed"] and (old is None or not old.get("completed")):
        return {"title": "Full time", "body": timed_score, "tag": cur["match_key"]}
    if cur["state"] == "in" and old and old.get("state") == "in":
        if cur["home_score"] != old.get("home_score") or cur["away_score"] != old.get("away_score"):
            return {"title": "Score change!", "body": timed_score, "tag": cur["match_key"]}
    return None


def status_minute(status: dict[str, Any]) -> str | None:
    for value in (status.get("displayClock"), status.get("shortDetail"), status.get("detail")):
        text = str(value or "").strip()
        if not text or re.search(r"scheduled|^ft$|^final", text, re.IGNORECASE):
            continue
        match = re.fullmatch(r"(\d{1,3})(?::\d{2})?", text)
        if match:
            return f"{match.group(1)}'"
        if any(character.isdigit() for character in text):
            return text
    clock = as_float(status.get("clock"))
    return f"{int(clock // 60)}'" if clock and clock > 0 else None


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
        write_match_baseline(baseline)

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


def capture_pick(fixture_markets: dict[str, Any], source_name: str, phase: str) -> dict[str, Any] | None:
    capture = (fixture_markets.get(source_name) or {}).get(phase)
    if capture and capture.get("pick"):
        return {"pick": capture["pick"], "pickPct": capture.get("pickPct"), "source": "match", "volume": capture.get("pickVolume"), "total": capture.get("totalVolume")}
    return None


# One-off exception: these matches were already live when durable capture began,
# so their only "match" capture is a misleading mid-game read. Grade their locked
# pick against the outright odds (the honest pre-kickoff signal) instead.
LOCKED_OUTRIGHT_ONLY = {"Saudi Arabia::Uruguay"}


def pick_for_event(
    source_name: str,
    pair_key: str | None,
    team_a: str | None,
    team_b: str | None,
    fixture_markets: dict[str, Any],
    outright_odds: dict[str, Any],
) -> dict[str, Any]:
    if pair_key in LOCKED_OUTRIGHT_ONLY:
        return outright_pick(team_a, team_b, outright_odds)
    # "Locked" is strictly the latest capture before kickoff. In-play information
    # must never be graded as a pre-match prediction.
    return capture_pick(fixture_markets, source_name, "preGame") or outright_pick(team_a, team_b, outright_odds)


def current_match_pick(
    source_name: str,
    team_a: str | None,
    team_b: str | None,
    fixture_markets: dict[str, Any],
    outright_odds: dict[str, Any],
) -> dict[str, Any]:
    # "Now" = the last in-play market read (frozen once the match ends), falling back to outright.
    return capture_pick(fixture_markets, source_name, "inPlay") or outright_pick(team_a, team_b, outright_odds)


def build_projection(
    odds: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
    standings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    events = events or []
    projection = build_fact_projection(odds, actual_winners_by_pair(events), events, standings)
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
        season = event.get("season") or {}
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
                "shootoutScore": as_float(competitor.get("shootoutScore")),
                "winner": bool(competitor.get("winner")),
            }

        home = sides.get("home") or (sides.get("away") if len(sides) == 1 else {})
        away = sides.get("away") or {}
        winner = None
        if status_type.get("completed"):
            flagged_winners = [side.get("team") for side in (home, away) if side.get("winner") and side.get("team")]
            if len(flagged_winners) == 1:
                winner = flagged_winners[0]
            elif home.get("score") is not None and away.get("score") is not None and home["score"] > away["score"]:
                winner = home["team"]
            elif home.get("score") is not None and away.get("score") is not None and away["score"] > home["score"]:
                winner = away["team"]
            elif home.get("score") is not None and away.get("score") is not None:
                winner = "Draw"

        events.append(
            {
                "id": event.get("id"),
                "name": event.get("name"),
                "shortName": event.get("shortName"),
                "date": event.get("date") or competition.get("date"),
                "stageSlug": season.get("slug"),
                "stageType": season.get("type"),
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
        pair_key = market_key(home_team, away_team)
        fixture_markets = locked_markets_for_event(match_markets, event)
        pm_prediction = pick_for_event("polymarket", pair_key, home_team, away_team, fixture_markets, locked_polymarket)
        ks_prediction = pick_for_event("kalshi", pair_key, home_team, away_team, fixture_markets, locked_kalshi)
        status = event.get("status", {})
        started = bool(status.get("completed") or status.get("state") == "in")
        if started:
            # "Now" only exists once the match has kicked off; it stays (frozen) after it ends.
            pm_current = current_match_pick("polymarket", home_team, away_team, fixture_markets, polymarket)
            ks_current = current_match_pick("kalshi", home_team, away_team, fixture_markets, kalshi)
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


def _resolve_market(
    future: Any,
    fallback: dict[str, Any],
    errors: dict[str, str],
    label: str,
) -> tuple[dict[str, Any], bool]:
    try:
        markets = future.result()
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        errors[label] = str(exc)
        return fallback, False
    if not markets:
        errors[label] = "empty market response"
        return fallback, False
    return markets, True


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

        # ESPN is the source of truth for matches and standings. Let failures
        # reach the route so it can serve the last complete cached snapshot.
        scoreboard = scoreboard_future.result()
        standings = standings_future.result()
        # fetch_live_match_sources already isolates per-source failures and never raises.
        live_match_sources, match_market_errors = live_sources_future.result()
        polymarket, polymarket_live = _resolve_market(
            polymarket_future, baseline_polymarket, fetch_errors, "polymarket"
        )
        kalshi, kalshi_live = _resolve_market(kalshi_future, baseline_kalshi, fetch_errors, "kalshi")

    match_markets, match_baseline_path = merge_match_baseline(scoreboard, live_match_sources)
    locked_polymarket = baseline_polymarket or polymarket
    locked_kalshi = baseline_kalshi or kalshi
    comparisons = compare_events(
        scoreboard,
        polymarket,
        kalshi,
        match_markets,
        locked_polymarket,
        locked_kalshi,
    )
    team_rows = build_team_rows(standings, polymarket, kalshi)

    pm_top = top_leader(polymarket)
    ks_top = top_leader(kalshi)
    if polymarket_live and kalshi_live:
        prediction_mode = "liveMarkets"
    elif polymarket_live or kalshi_live:
        prediction_mode = "mixedMarkets"
    else:
        prediction_mode = "baselineCsv"

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
        "predictionMode": prediction_mode,
        "currentOddsSources": {
            "polymarket": "live" if polymarket_live else "baseline",
            "kalshi": "live" if kalshi_live else "baseline",
        },
        "matchMarketErrors": match_market_errors,
        "fetchErrors": fetch_errors,
        "matchMarketsCaptured": sum(len(source) for source in live_match_sources.values()),
        "leaders": {"polymarket": pm_top, "kalshi": ks_top},
        "projections": {
            "polymarket": build_projection(polymarket, scoreboard, standings),
            "kalshi": build_projection(kalshi, scoreboard, standings),
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
        if not polymarket:
            raise ValueError("empty market response")
        sources.append("Polymarket live")
    except (requests.RequestException, ValueError, KeyError, TypeError):
        polymarket = baseline_polymarket
        sources.append("Polymarket baseline")

    try:
        kalshi = fetch_kalshi()
        if not kalshi:
            raise ValueError("empty market response")
        sources.append("Kalshi live")
    except (requests.RequestException, ValueError, KeyError, TypeError):
        kalshi = baseline_kalshi
        sources.append("Kalshi baseline")

    return consensus_odds(polymarket, kalshi), sources


def actual_winners_by_pair(events: list[dict[str, Any]]) -> dict[str, str]:
    winners: dict[str, str] = {}
    for event in events:
        stage_slug = event.get("stageSlug")
        if stage_slug not in KNOCKOUT_STAGE_SLUGS:
            continue
        winner = event.get("winner")
        if not event.get("status", {}).get("completed") or not winner or winner == "Draw":
            continue
        key = market_key(event.get("home", {}).get("team"), event.get("away", {}).get("team"))
        if key:
            winners[f"{stage_slug}::{key}"] = winner
    return winners


def knockout_events_by_stage(events: list[dict[str, Any]]) -> dict[str, dict[int, dict[str, Any]]]:
    stages: dict[str, dict[int, dict[str, Any]]] = {}
    for stage_slug in KNOCKOUT_STAGE_SLUGS:
        stage_events = sorted(
            [event for event in events if event.get("stageSlug") == stage_slug],
            key=lambda event: event.get("date") or "",
        )
        stages[stage_slug] = {index: event for index, event in enumerate(stage_events, start=1)}
    return stages


WINNER_SLOT_RE = re.compile(r"^Group\s+([A-L])\s+Winner$", re.IGNORECASE)
SECOND_SLOT_RE = re.compile(r"^Group\s+([A-L])\s+2nd Place$", re.IGNORECASE)
THIRD_SLOT_RE = re.compile(r"^Third Place Group\s+([A-L/]+)$", re.IGNORECASE)


def _group_letter(name: str | None) -> str:
    text = (name or "").upper()
    match = re.search(r"GROUP\s+([A-L])\b", text) or re.search(r"\b([A-L])\b", text)
    return match.group(1) if match else ""


def group_ranking(standings: list[dict[str, Any]] | None) -> dict[str, list[dict[str, Any]]]:
    """Rank each group by current results (points, GD, GF, then name)."""
    ranking: dict[str, list[dict[str, Any]]] = {}
    for group in standings or []:
        letter = _group_letter(group.get("name"))
        if not letter:
            continue
        ranking[letter] = sorted(
            group.get("entries", []),
            key=lambda entry: (
                -(entry.get("points") or 0),
                -(entry.get("gd") or 0),
                -(entry.get("gf") or 0),
                entry.get("team") or "",
            ),
        )
    return ranking


def qualifying_third_letters(ranking: dict[str, list[dict[str, Any]]]) -> list[str]:
    """The group letters whose third-placed team is among the best 8 (those that advance)."""
    thirds = [
        (letter, entries[2]) for letter, entries in ranking.items() if len(entries) >= 3
    ]
    thirds.sort(
        key=lambda item: (
            -(item[1].get("points") or 0),
            -(item[1].get("gd") or 0),
            -(item[1].get("gf") or 0),
        )
    )
    return [letter for letter, _ in thirds[:8]]


def _assign_thirds(qualifying: list[str], slots: list[tuple[str, set[str]]]) -> dict[str, str]:
    """Bipartite-match qualifying third-place groups to their allowed bracket slots."""
    slot_to_letter: dict[int, str] = {}

    def augment(letter: str, seen: set[int]) -> bool:
        for index, (_, allowed) in enumerate(slots):
            if letter in allowed and index not in seen:
                seen.add(index)
                if index not in slot_to_letter or augment(slot_to_letter[index], seen):
                    slot_to_letter[index] = letter
                    return True
        return False

    for letter in qualifying:
        augment(letter, set())
    return {slots[index][0]: letter for index, letter in slot_to_letter.items()}


def build_slot_resolver(
    standings: list[dict[str, Any]] | None,
    events: list[dict[str, Any]] | None,
):
    """Resolve a knockout slot label (or a seeded team) to the team currently
    projected to fill it from the live group standings. Returns None when no
    standings are available (callers then keep the static seed bracket)."""
    if not standings:
        return None
    ranking = group_ranking(standings)
    if not ranking:
        return None
    team_letter = {
        entry.get("team"): letter
        for letter, entries in ranking.items()
        for entry in entries
        if entry.get("team")
    }

    third_slots: list[tuple[str, set[str]]] = []
    seen_labels: set[str] = set()
    for event in events or []:
        for side in ("home", "away"):
            label = (event.get(side) or {}).get("team")
            match = THIRD_SLOT_RE.match(label or "")
            if match and label not in seen_labels:
                seen_labels.add(label)
                third_slots.append((label, set(match.group(1).upper().split("/"))))

    # Real teams ESPN has already placed into an R32 slot (only knockout slots,
    # never group-stage fixtures, where every team appears).
    seeded: set[str] = set()
    for event in events or []:
        if event.get("stageSlug") != "round-of-32":
            continue
        for side in ("home", "away"):
            label = (event.get(side) or {}).get("team")
            if label and not is_fixture_placeholder(label) and label in team_letter:
                seeded.add(label)

    # Don't match a third whose group is already seeded into a slot (else it would
    # be placed twice, e.g. ESPN's seeded "Bosnia" plus a matched Group-B third).
    qualifying = [
        letter
        for letter in qualifying_third_letters(ranking)
        if len(ranking.get(letter, [])) < 3 or ranking[letter][2].get("team") not in seeded
    ]
    third_team = {}
    for label, letter in _assign_thirds(qualifying, third_slots).items():
        entries = ranking.get(letter, [])
        if len(entries) >= 3:
            third_team[label] = entries[2].get("team")

    def position(letter: str, index: int) -> str | None:
        entries = ranking.get(letter, [])
        return entries[index].get("team") if len(entries) > index else None

    def resolve(team_string: str | None) -> str | None:
        if not team_string:
            return None
        match = WINNER_SLOT_RE.match(team_string)
        if match:
            return position(match.group(1), 0)
        match = SECOND_SLOT_RE.match(team_string)
        if match:
            return position(match.group(1), 1)
        if THIRD_SLOT_RE.match(team_string):
            return third_team.get(team_string)
        # A real, known group team that ESPN has already seeded into a slot:
        # trust it as-is. ESPN fills both winner and runner-up slots as groups
        # firm up, so forcing it to the group winner would duplicate that winner
        # (e.g. Group E's runner-up Ivory Coast collapsing onto winner Germany).
        # Anything else (later-round "Winners Match N" labels, unknown teams)
        # returns None so the caller keeps its projected fallback.
        if team_string in team_letter:
            return team_string
        return None

    return resolve


def event_teams_with_fallback(
    event: dict[str, Any] | None,
    fallback: tuple[str, str],
    resolver=None,
) -> tuple[str, str]:
    if not event:
        return fallback
    home = event.get("home", {}).get("team")
    away = event.get("away", {}).get("team")
    home_real = bool(home and not is_fixture_placeholder(home))
    away_real = bool(away and not is_fixture_placeholder(away))
    # Both slots resolved to real teams -> the actual matchup is known, use it.
    if home_real and away_real:
        return home, away
    if resolver is None:
        # No standings: keep the static seed bracket, but only when both teams
        # were known (handled above) to avoid duplicating a seed opponent.
        return fallback
    # Project each slot from the live standings; fall back to the seed if a slot
    # cannot be resolved.
    return (resolver(home) or fallback[0]), (resolver(away) or fallback[1])


def bracket_pick(
    team_a: str,
    team_b: str,
    odds: dict[str, Any],
    actual_winners: dict[str, str],
    stage_slug: str | None = None,
    allow_legacy_fact: bool = True,
) -> tuple[str, str]:
    pair = market_key(team_a, team_b) or ""
    actual = actual_winners.get(f"{stage_slug}::{pair}") if stage_slug else None
    if actual is None and allow_legacy_fact:
        actual = actual_winners.get(pair)
    if actual in {team_a, team_b}:
        return actual, "actual"

    value_a = odds.get(team_a, {}).get("mid") or 0
    value_b = odds.get(team_b, {}).get("mid") or 0
    return (team_a, "market") if value_a >= value_b else (team_b, "market")


KNOCKOUT_REF_PATTERNS = {
    "round-of-16": re.compile(r"^Round of 32 (\d+) Winner$", re.IGNORECASE),
    "quarterfinals": re.compile(r"^Round of 16 (\d+) Winner$", re.IGNORECASE),
    "semifinals": re.compile(r"^Quarterfinal (\d+) Winner$", re.IGNORECASE),
    "final": re.compile(r"^Semifinal (\d+) Winner$", re.IGNORECASE),
}
KNOCKOUT_PREVIOUS_STAGE = {
    "round-of-16": "round-of-32",
    "quarterfinals": "round-of-16",
    "semifinals": "quarterfinals",
    "final": "semifinals",
}


def derive_knockout_tree(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Derive the bracket wiring from ESPN's own cross-round references instead
    of a hardcoded position map. ESPN numbers knockout matches with FIFA's fixed
    match numbers, which do NOT always follow kickoff date order — hardcoded
    mappings kept drifting as fixtures were populated (USA vs USA, two Bosnias,
    Belgium vs Belgium). Here every fixture side is resolved to the previous-round
    event that feeds it:

    - a real team seeded in a slot anchors that slot to the previous-round match
      the team played in;
    - "Round of 32 N Winner"-style references claim the remaining previous-round
      events (sorted numbers onto date-sorted events).

    Each previous-round event feeds exactly one slot, so duplicates and
    self-matches are impossible by construction. Returns None when the knockout
    fixture list is incomplete (pre-tournament, tests, baseline) — callers then
    keep the static seed bracket.
    """
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for slug in ("round-of-32", "round-of-16", "quarterfinals", "semifinals", "final", "3rd-place-match"):
        by_stage[slug] = sorted(
            [event for event in events if event.get("stageSlug") == slug],
            key=lambda event: event.get("date") or "",
        )
    expected = {"round-of-32": 16, "round-of-16": 8, "quarterfinals": 4, "semifinals": 2, "final": 1}
    if any(len(by_stage[slug]) != count for slug, count in expected.items()):
        return None

    feeders: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    for slug, pattern in KNOCKOUT_REF_PATTERNS.items():
        previous = by_stage[KNOCKOUT_PREVIOUS_STAGE[slug]]
        current = by_stage[slug]
        slot_feeder: dict[tuple[int, str], dict[str, Any]] = {}
        used: set[int] = set()
        pending: list[tuple[int, str, int | None]] = []
        for index, event in enumerate(current):
            for side in ("home", "away"):
                label = (event.get(side) or {}).get("team") or ""
                reference = pattern.match(label)
                if reference:
                    pending.append((index, side, int(reference.group(1))))
                    continue
                feeder = None
                if label and not is_fixture_placeholder(label):
                    feeder = next(
                        (
                            candidate
                            for candidate in previous
                            if label
                            in ((candidate.get("home") or {}).get("team"), (candidate.get("away") or {}).get("team"))
                        ),
                        None,
                    )
                if feeder is not None and id(feeder) not in used:
                    slot_feeder[(index, side)] = feeder
                    used.add(id(feeder))
                else:
                    pending.append((index, side, None))
        remaining = [candidate for candidate in previous if id(candidate) not in used]
        if len(pending) != len(remaining):
            return None
        # Sorted match numbers onto date-sorted remaining events; unknown labels last.
        pending.sort(key=lambda item: (item[2] is None, item[2] or 0))
        for (index, side, _), feeder in zip(pending, remaining):
            slot_feeder[(index, side)] = feeder
        for index, event in enumerate(current):
            home_feeder = slot_feeder.get((index, "home"))
            away_feeder = slot_feeder.get((index, "away"))
            if home_feeder is None or away_feeder is None:
                return None
            feeders[id(event)] = (home_feeder, away_feeder)

    final_event = by_stage["final"][0]
    sf_left, sf_right = feeders[id(final_event)]

    def rounds_under(semifinal: dict[str, Any]) -> list[list[dict[str, Any]]]:
        quarterfinals = list(feeders[id(semifinal)])
        r16 = [child for quarter in quarterfinals for child in feeders[id(quarter)]]
        r32 = [child for sixteenth in r16 for child in feeders[id(sixteenth)]]
        return [r32, r16, quarterfinals, [semifinal]]

    return {
        "left": rounds_under(sf_left),
        "right": rounds_under(sf_right),
        "final": final_event,
        "third": by_stage["3rd-place-match"][0] if by_stage["3rd-place-match"] else None,
    }


def build_fact_projection(
    odds: dict[str, Any],
    actual_winners: dict[str, str],
    events: list[dict[str, Any]] | None = None,
    standings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stage_events = knockout_events_by_stage(events or [])
    resolver = build_slot_resolver(standings, events)

    tree = derive_knockout_tree(events or [])
    if tree:
        return _projection_from_tree(tree, odds, resolver)

    def build_round(
        pairings: list[tuple[str, str]],
        stage_slug: str,
        event_numbers: list[int],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        matches = []
        winners = []
        for (fallback_a, fallback_b), event_number in zip(pairings, event_numbers):
            event = stage_events.get(stage_slug, {}).get(event_number)
            team_a, team_b = event_teams_with_fallback(event, (fallback_a, fallback_b), resolver)
            event_winner = event.get("winner") if event and event.get("status", {}).get("completed") else None
            if event_winner in {team_a, team_b}:
                winner, source = event_winner, "actual"
            else:
                winner, source = bracket_pick(
                    team_a,
                    team_b,
                    odds,
                    actual_winners if event is None else {},
                    stage_slug,
                    allow_legacy_fact=not bool(events),
                )
            loser = team_b if winner == team_a else team_a
            matches.append(
                {
                    "teams": [team_a, team_b],
                    "winner": winner,
                    "loser": loser,
                    "source": source,
                    "eventId": event.get("id") if event else None,
                    "date": event.get("date") if event else None,
                    "stageSlug": stage_slug,
                }
            )
            winners.append(winner)
        return matches, winners

    def next_pairings(winners: list[str]) -> list[tuple[str, str]]:
        return [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]

    round_slugs = ["round-of-32", "round-of-16", "quarterfinals", "semifinals"]
    left_rounds, right_rounds = [], []
    left_pairings = LEFT_R32
    right_pairings = RIGHT_R32

    for round_index, stage_slug in enumerate(round_slugs):
        left_matches, left_winners = build_round(
            left_pairings,
            stage_slug,
            KNOCKOUT_EVENT_NUMBERS["left"][stage_slug],
        )
        right_matches, right_winners = build_round(
            right_pairings,
            stage_slug,
            KNOCKOUT_EVENT_NUMBERS["right"][stage_slug],
        )
        left_rounds.append(left_matches)
        right_rounds.append(right_matches)
        if round_index < len(round_slugs) - 1:
            left_pairings = next_pairings(left_winners)
            right_pairings = next_pairings(right_winners)

    left_final = left_rounds[-1][0]
    right_final = right_rounds[-1][0]
    final_match, _ = build_round(
        [(left_final["winner"], right_final["winner"])],
        "final",
        [1],
    )
    third_place_match, _ = build_round(
        [(left_final["loser"], right_final["loser"])],
        "3rd-place-match",
        [1],
    )
    final = final_match[0]
    third_match = third_place_match[0]

    return {
        "champion": final["winner"],
        "runnerUp": final["loser"],
        "thirdPlace": third_match["winner"],
        "fourthPlace": third_match["loser"],
        "final": final,
        "thirdPlaceMatch": third_match,
        "rounds": {"left": left_rounds, "right": right_rounds},
    }


def _projection_from_tree(tree: dict[str, Any], odds: dict[str, Any], resolver) -> dict[str, Any]:
    """Project the bracket over the ESPN-derived wiring: each slot shows its real
    seeded team when known, otherwise the projected winner of the feeder match."""
    results: dict[int, dict[str, Any]] = {}

    def slot_team(event: dict[str, Any], side: str, feeder: dict[str, Any] | None) -> str:
        label = (event.get(side) or {}).get("team") or ""
        if label and not is_fixture_placeholder(label):
            if resolver:
                resolved = resolver(label)
                if resolved:
                    return resolved
            return label
        if feeder is not None and id(feeder) in results:
            return results[id(feeder)]["winner"]
        # Round-of-32 group placeholders resolve from standings when available.
        if resolver:
            resolved = resolver(label)
            if resolved:
                return resolved
        return label

    def build_match(event: dict[str, Any], feeder_pair, stage_slug: str) -> dict[str, Any]:
        home_feeder, away_feeder = feeder_pair
        team_a = slot_team(event, "home", home_feeder)
        team_b = slot_team(event, "away", away_feeder)
        event_winner = event.get("winner") if event.get("status", {}).get("completed") else None
        if event_winner in {team_a, team_b}:
            winner, source = event_winner, "actual"
        else:
            winner, source = bracket_pick(team_a, team_b, odds, {}, stage_slug)
        match = {
            "teams": [team_a, team_b],
            "winner": winner,
            "loser": team_b if winner == team_a else team_a,
            "source": source,
            "eventId": event.get("id"),
            "date": event.get("date"),
            "stageSlug": stage_slug,
        }
        results[id(event)] = match
        return match

    round_slugs = ["round-of-32", "round-of-16", "quarterfinals", "semifinals"]
    rounds: dict[str, list[list[dict[str, Any]]]] = {}
    for side_name in ("left", "right"):
        side_rounds = []
        for round_index, stage_slug in enumerate(round_slugs):
            stage_matches = []
            for match_index, event in enumerate(tree[side_name][round_index]):
                if round_index == 0:
                    feeder_pair = (None, None)
                else:
                    below = tree[side_name][round_index - 1]
                    feeder_pair = (below[match_index * 2], below[match_index * 2 + 1])
                stage_matches.append(build_match(event, feeder_pair, stage_slug))
            side_rounds.append(stage_matches)
        rounds[side_name] = side_rounds

    left_final = rounds["left"][-1][0]
    right_final = rounds["right"][-1][0]
    final = build_match(tree["final"], (tree["left"][-1][0], tree["right"][-1][0]), "final")

    third_event = tree["third"]
    if third_event is not None:
        # Third-place sides fall back to the semifinal losers when not yet seeded.
        def third_team(side: str, fallback: str) -> str:
            label = (third_event.get(side) or {}).get("team") or ""
            if label and not is_fixture_placeholder(label):
                return label
            return fallback

        team_a = third_team("home", left_final["loser"])
        team_b = third_team("away", right_final["loser"])
        event_winner = third_event.get("winner") if third_event.get("status", {}).get("completed") else None
        if event_winner in {team_a, team_b}:
            winner, source = event_winner, "actual"
        else:
            winner, source = bracket_pick(team_a, team_b, odds, {}, "3rd-place-match")
        third_match = {
            "teams": [team_a, team_b],
            "winner": winner,
            "loser": team_b if winner == team_a else team_a,
            "source": source,
            "eventId": third_event.get("id"),
            "date": third_event.get("date"),
            "stageSlug": "3rd-place-match",
        }
    else:
        team_a, team_b = left_final["loser"], right_final["loser"]
        winner, source = bracket_pick(team_a, team_b, odds, {}, "3rd-place-match")
        third_match = {
            "teams": [team_a, team_b],
            "winner": winner,
            "loser": team_b if winner == team_a else team_a,
            "source": source,
            "eventId": None,
            "date": None,
            "stageSlug": "3rd-place-match",
        }

    return {
        "champion": final["winner"],
        "runnerUp": final["loser"],
        "thirdPlace": third_match["winner"],
        "fourthPlace": third_match["loser"],
        "final": final,
        "thirdPlaceMatch": third_match,
        "rounds": {"left": rounds["left"], "right": rounds["right"]},
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
    BRACKET_GENERATION_STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = BRACKET_GENERATION_STATE_JSON.with_name(f".{BRACKET_GENERATION_STATE_JSON.name}.tmp")
    temporary_path.write_text(
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(BRACKET_GENERATION_STATE_JSON)


def stored_match_markets() -> dict[str, Any]:
    """Read-only view of the captured match markets (no new captures written)."""
    if supabase_enabled():
        try:
            return supabase_load_markets()
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return {}
    try:
        return load_match_baseline().get("markets", {})
    except (OSError, ValueError):
        return {}


def locked_upset_winners(
    events: list[dict[str, Any]],
    match_markets: dict[str, Any],
    locked_polymarket: dict[str, Any],
    locked_kalshi: dict[str, Any],
) -> set[str]:
    """Winners of completed knockout matches that beat the locked pre-game
    prediction — the exact picks the match cards grade Hit/Miss against
    (pre-game match-market capture, falling back to the outright odds). A win is
    an upset when at least one graded source predicted the other team, matching
    the app's Upsets filter."""
    upsets: set[str] = set()
    for event in events:
        if event.get("stageSlug") not in KNOCKOUT_STAGE_SLUGS:
            continue
        if not (event.get("status") or {}).get("completed"):
            continue
        winner = event.get("winner")
        home_team = (event.get("home") or {}).get("team")
        away_team = (event.get("away") or {}).get("team")
        if not winner or winner == "Draw" or winner not in {home_team, away_team}:
            continue
        pair_key = event_market_key(event)
        fixture_markets = locked_markets_for_event(match_markets, event)
        for source_name, outright in (("polymarket", locked_polymarket), ("kalshi", locked_kalshi)):
            pick = pick_for_event(source_name, pair_key, home_team, away_team, fixture_markets, outright).get("pick")
            if pick and pick != "Draw" and pick != winner:
                upsets.add(winner)
                break
    return upsets


def fmt_bracket_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def fmt_bracket_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{moment.strftime('%b')} {moment.day}"


def confirmed_teams(standings: list[dict[str, Any]] | None) -> set[str]:
    """Teams whose Round-of-32 berth is certain — either a locked top-2 group
    finish, or a third place already guaranteed to be among the best 8. A group
    position is locked when the order against every rival is settled (one side's
    current points beat the other's maximum, or both have finished and a fixed
    tiebreaker separates them). A third place is guaranteed when its group is
    finished and at most 7 other thirds can possibly rank above it."""

    def remaining(entry: dict[str, Any]) -> int:
        return max(0, 3 - int(entry.get("gp") or 0))

    def key(entry: dict[str, Any]) -> tuple[float, float, float]:
        return (entry.get("points") or 0, entry.get("gd") or 0, entry.get("gf") or 0)

    confirmed: set[str] = set()
    thirds: dict[str, dict[str, Any]] = {}  # group letter -> third-placed entry
    group_done: dict[str, bool] = {}
    for group in standings or []:
        entries = group.get("entries", [])
        letter = _group_letter(group.get("name"))
        ranked = sorted(entries, key=key, reverse=True)
        if letter:
            group_done[letter] = all(remaining(e) == 0 for e in entries)
            if len(ranked) >= 3:
                thirds[letter] = ranked[2]
        for team in entries:
            t_pts = team.get("points") or 0
            t_max = t_pts + 3 * remaining(team)
            settled = True
            above = 0  # rivals guaranteed to finish above this team
            for rival in entries:
                if rival.get("team") == team.get("team"):
                    continue
                r_pts = rival.get("points") or 0
                r_max = r_pts + 3 * remaining(rival)
                if t_pts > r_max:
                    continue  # team can never be caught by this rival
                if r_pts > t_max:
                    above += 1  # rival can never be caught by the team
                    continue
                both_finished = remaining(team) == 0 and remaining(rival) == 0
                if both_finished and key(team) != key(rival):
                    if key(rival) > key(team):
                        above += 1
                    continue  # done and split by a tiebreaker that can no longer move
                settled = False
                break
            # Locked position AND guaranteed top 2 -> berth in the Round of 32 is certain.
            if settled and above <= 1 and team.get("team"):
                confirmed.add(team["team"])

    # Best-8 third places: a finished group's third is in once at most 7 other
    # thirds can outrank it (unfinished groups' thirds count as "could be above").
    for letter, third in thirds.items():
        if not group_done.get(letter) or not third.get("team"):
            continue
        could_outrank = 0
        for other_letter, other_third in thirds.items():
            if other_letter == letter:
                continue
            if not group_done.get(other_letter) or key(other_third) > key(third):
                could_outrank += 1
        if could_outrank <= 7:
            confirmed.add(third["team"])
    return confirmed


def render_bracket_svg(
    projection: dict[str, Any],
    odds: dict[str, Any],
    sources: list[str],
    generated_at: datetime,
    changed_slots: set[str] | None = None,
    baseline_champion: str | None = None,
    confirmed: set[str] | None = None,
    upset_teams: set[str] | None = None,
) -> str:
    changed_slots = changed_slots or set()
    confirmed = confirmed or set()
    # Winners whose completed match beat the locked pre-game prediction — the
    # same picks the match cards grade Hit/Miss against (locked_upset_winners).
    upset_teams = upset_teams or set()
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

    def match_box(x: int, y: int, match: dict[str, Any], slot_prefix: str, highlight: bool = False, certain: set[str] | None = None, upset: set[str] | None = None, show_date: bool = False) -> str:
        certain = certain or set()
        upset = upset or set()
        team_a, team_b = match["teams"]
        winner = match["winner"]
        source = match.get("source")
        # Finished matches (result is an actual fact) read light grey — that fill
        # replaces the old "FACT" mark above the box; the final keeps its warm
        # highlight until it is actually played.
        fill = "#e8ebef" if source == "actual" else "#fffbea" if highlight else "#fbfcfe"
        rows = []
        for idx, team in enumerate((team_a, team_b)):
            row_y = y + 20 + idx * 25
            is_winner = team == winner
            changed_class = " changedEntry" if f"{slot_prefix}-{idx}" in changed_slots else ""
            # Band: this team's presence here is certain — a locked group qualifier
            # (R32) or a team that actually won its way into a later round. Green
            # when the result went as predicted, light orange when it was an upset
            # (the actual winner was not the market favourite). Projected
            # (not-yet-played) advancements stay uncoloured. Finished boxes skip
            # the bands so their grey fill stays visible — the presence signal is
            # trivial once the result is known (the advance/upset marking lives in
            # the next round's box).
            band_fill = None
            if source != "actual":
                band_fill = "#ffd9a8" if team in upset else "#dcf5e4" if team in certain else None
            if band_fill:
                band_y = y + idx * (box_h / 2)
                rows.append(
                    f'<rect x="{x + 1.6}" y="{band_y + 1.6}" width="{box_w - 3.2}" height="{box_h / 2 - 3.2}" rx="2" fill="{band_fill}" />'
                )
            rows.append(
                f'<text x="{x + 8}" y="{row_y}" class="team {"winner" if is_winner else "loser"}{changed_class}">{escape(svg_team_label(team))}</text>'
                f'<text x="{x + box_w - 8}" y="{row_y}" class="pct {"winner" if is_winner else "loser"}{changed_class}">{team_pct(team)}</text>'
            )
        when = fmt_bracket_date(match.get("date")) if show_date else ""
        date_label = (
            f'<text x="{x + box_w}" y="{y + box_h + 12}" class="when" text-anchor="end">{escape(when)}</text>' if when else ""
        )
        return (
            f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="4" class="box" fill="{fill}" />'
            f'<line x1="{x}" y1="{y + box_h / 2}" x2="{x + box_w}" y2="{y + box_h / 2}" class="divider" />'
            + "".join(rows)
            + date_label
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
          .when { font: 10px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #8a919b; }
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

    # A team is certainly in a box if it qualified (R32) or actually won the
    # previous round; projected advancements are not.
    def fact_winners(matches: list[dict[str, Any]]) -> set[str]:
        return {m["winner"] for m in matches if m.get("source") == "actual" and m.get("winner")}

    def fact_losers(matches: list[dict[str, Any]]) -> set[str]:
        return {m["loser"] for m in matches if m.get("source") == "actual" and m.get("loser")}

    def fact_upsets(matches: list[dict[str, Any]]) -> set[str]:
        # Actual winners whose match beat the locked pre-game prediction.
        return fact_winners(matches) & upset_teams

    left_rounds = projection["rounds"]["left"]
    right_rounds = projection["rounds"]["right"]
    for round_idx, matches in enumerate(left_rounds):
        prev = left_rounds[round_idx - 1] if round_idx else []
        certain = confirmed if round_idx == 0 else fact_winners(prev)
        upset = fact_upsets(prev)
        for match_idx, (match, y) in enumerate(zip(matches, round_ys[round_idx])):
            svg_parts.append(match_box(left_x[round_idx], y, match, f"left-{round_idx}-{match_idx}", certain=certain, upset=upset, show_date=round_idx >= 1))
    for round_idx, matches in enumerate(right_rounds):
        prev = right_rounds[round_idx - 1] if round_idx else []
        certain = confirmed if round_idx == 0 else fact_winners(prev)
        upset = fact_upsets(prev)
        for match_idx, (match, y) in enumerate(zip(matches, round_ys[round_idx])):
            svg_parts.append(match_box(right_x[round_idx], y, match, f"right-{round_idx}-{match_idx}", certain=certain, upset=upset, show_date=round_idx >= 1))
    semifinals = [left_rounds[3][0], right_rounds[3][0]]

    champion_class = "champion changedEntry" if baseline_champion and projection["champion"] != baseline_champion else "champion"
    svg_parts.append(f'<text x="{final_x + box_w / 2}" y="{final_y - 18}" class="{champion_class}">Champion: {escape(svg_team_label(projection["champion"]))}</text>')
    svg_parts.append(match_box(final_x, final_y, projection["final"], "final", highlight=True, certain=fact_winners(semifinals), upset=fact_upsets(semifinals), show_date=True))
    svg_parts.append(f'<text x="{third_x + box_w / 2}" y="{third_y - 18}" class="round" text-anchor="middle">Third-place play-off</text>')
    svg_parts.append(match_box(third_x, third_y, projection["thirdPlaceMatch"], "third", certain=fact_losers(semifinals), show_date=True))
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
    try:
        standings = fetch_standings()
    except requests.RequestException:
        standings = None
    projection = build_fact_projection(odds, actual_winners_by_pair(events), events, standings)
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
        baseline_polymarket, baseline_kalshi, _ = load_baseline_odds()
        upsets = locked_upset_winners(
            events,
            stored_match_markets(),
            baseline_polymarket or odds,
            baseline_kalshi or odds,
        )
        payload["svg"] = render_bracket_svg(
            projection,
            odds,
            fact_sources,
            generated_at,
            changed_projection_slots(projection, initial_projection, odds, initial_odds),
            initial_projection.get("champion") if initial_projection else None,
            confirmed_teams(standings),
            upsets,
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
    if push_rate_limited(push_client_key()):
        return jsonify({"ok": False, "error": "rate limited"}), 429
    data = request.get_json(silent=True) or {}
    sub = data.get("subscription") or {}
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if (
        not valid_push_endpoint(endpoint)
        or not isinstance(p256dh, str)
        or not isinstance(auth, str)
        or not 20 <= len(p256dh) <= 256
        or not 8 <= len(auth) <= 128
    ):
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
    global _CACHE_REFRESHING

    with _CACHE_LOCK:
        now = time.time()
        if _CACHE["payload"] is not None and now - _CACHE["at"] < _cache_ttl(_CACHE["payload"]):
            return jsonify({**_CACHE["payload"], "cached": True})
        if _CACHE_REFRESHING and _CACHE["payload"] is not None:
            return jsonify({**_CACHE["payload"], "cached": True, "stale": True, "refreshing": True})
        _CACHE_REFRESHING = True

    try:
        payload = build_snapshot()
    except _SNAPSHOT_RECOVERABLE_ERRORS as exc:
        with _CACHE_LOCK:
            _CACHE_REFRESHING = False
            if _CACHE["payload"] is not None:
                return jsonify({**_CACHE["payload"], "cached": True, "stale": True, "error": str(exc)})
        return jsonify({"error": str(exc)}), 503
    except Exception:
        with _CACHE_LOCK:
            _CACHE_REFRESHING = False
        LOG.exception("Unexpected error refreshing snapshot")
        raise

    with _CACHE_LOCK:
        _CACHE["payload"] = payload
        _CACHE["at"] = time.time()
        _CACHE_REFRESHING = False
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
        key = event_market_key(event)
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
        notification = goal_event_notification(old, {**cur, "minute": status_minute(status)})
        if notification and old is not None:  # only on a transition from a known state (no first-run spam)
            notifications.append(notification)
        if old is None or any(cur[k] != old.get(k) for k in ("home_score", "away_score", "state", "completed")):
            changed.append(cur)

    # New-favorite: diff the consensus title-odds leader against the stored one.
    # Skip when either market fell back to baseline odds — a transient live-market
    # outage would otherwise flip the leader and fire a spurious "new favorite"
    # (and flip back on the next run).
    try:
        odds, odds_sources = current_consensus_odds()
        fully_live = odds and not any("baseline" in source for source in odds_sources)
        leader = top_leader(odds) if fully_live else None
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
    with _BRACKET_STATUS_LOCK:
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
