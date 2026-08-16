"""
odds.py — Match Corna Live: pre-match betting odds cards
==========================================================
Fetches today's 1X2 (match result) odds from odds-api.io, but only for
fixtures where BOTH teams are on the watch-list (data/teams_master.json
— the exact same source of truth team_fixtures.py already uses, loaded
once and reused here rather than re-reading the file). Entertainment-only
price display, never framed as a tip/pick — see poster.fmt_odds_caption
and graphics.render_odds_card for how it's actually presented.

Requires ODDS_API_KEY (Railway env var) — with no key set, every
function here degrades to "no matches" rather than raising, so the
whole feature just silently does nothing until a key is added.
"""
import os
import math
import requests
from datetime import datetime, timezone, timedelta

import team_fixtures  # reuse the already-loaded master team list

ODDS_API_KEY   = os.getenv("ODDS_API_KEY", "").strip()
ODDS_BOOKMAKER = os.getenv("ODDS_BOOKMAKER", "Bet365").strip() or "Bet365"
BASE_URL       = "https://api.odds-api.io/v3"

_HEADERS = {"User-Agent": "MatchCornaLive/1.0"}


def _normalize(name: str) -> str:
    """Loose name matching between odds-api.io's team names and
    data/teams_master.json's SofaScore-sourced names — the two
    providers agree closely enough (both ultimately track the same
    real-world club names) that lowercasing plus a few common suffix
    variants is enough; no fuzzy-matching library needed."""
    if not name:
        return ""
    return (
        name.lower()
        .replace(" & ", " ")
        .replace("fc ", "")
        .replace(" cf", "")
        .replace(".", "")
        .replace("-", " ")
        .strip()
    )


def _allowed_teams() -> set:
    return {_normalize(info["name"]) for info in team_fixtures.MASTER_TEAMS.values()}


def _today_range_utc() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(path: str, params: dict):
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Odds] request to {path} failed: {e}")
        return None


def _fetch_todays_events() -> list[dict]:
    from_date, to_date = _today_range_utc()
    data = _get("/events", {
        "apiKey": ODDS_API_KEY,
        "sport": "football",
        "from": from_date,
        "to": to_date,
        "status": "pending,live",
        "limit": 100,
    })
    return data or []


def _fetch_odds_multi(event_ids: list) -> list[dict]:
    if not event_ids:
        return []
    data = _get("/odds/multi", {
        "apiKey": ODDS_API_KEY,
        "eventIds": ",".join(map(str, event_ids)),
        "bookmakers": ODDS_BOOKMAKER,
    })
    return data or []


def _extract_markets(odds_data: dict) -> dict:
    """Prefers ODDS_BOOKMAKER's own prices; falls back to whichever
    bookmaker odds/multi did return for this event (e.g. ODDS_BOOKMAKER
    didn't cover this particular match) rather than dropping the match
    entirely over a bookmaker-coverage gap. Returns {"1x2": {...} or
    None, "bts": {...} or None} — BTS (both teams to score) is optional
    per match; not every bookmaker/event carries it."""
    bookmakers = odds_data.get("bookmakers", {}) or {}
    markets = bookmakers.get(ODDS_BOOKMAKER) or next(iter(bookmakers.values()), None)
    result = {"1x2": None, "bts": None}
    if not markets:
        return result

    for m in markets:
        name = (m.get("name") or "").lower()
        odds = (m.get("odds") or [{}])[0]

        if result["1x2"] is None and name in ("ml", "1x2", "match result", "full time result"):
            h, x, a = odds.get("home"), odds.get("draw"), odds.get("away")
            if h and x and a:
                result["1x2"] = {"home": h, "draw": x, "away": a}

        elif result["bts"] is None and (
            "both teams to score" in name or name in ("bts", "gg", "both teams score")
        ):
            yes = odds.get("yes") or odds.get("Yes")
            no  = odds.get("no")  or odds.get("No")
            if yes and no:
                result["bts"] = {"yes": yes, "no": no}

    return result


def get_todays_odds_matches() -> list[dict]:
    """Today's watched-team matches with a 1X2 price attached, sorted
    by kickoff. Each entry: {home, away, league_name, kickoff_iso, odds}.
    Returns [] (never raises) if ODDS_API_KEY is unset or either API
    call fails — callers just treat that as "nothing to post today"."""
    if not ODDS_API_KEY:
        print("[Odds] ODDS_API_KEY not set — skipping odds cards")
        return []

    allowed = _allowed_teams()
    events = _fetch_todays_events()
    if not events:
        return []

    filtered = [
        e for e in events
        if _normalize(e.get("home", "")) in allowed and _normalize(e.get("away", "")) in allowed
    ]
    if not filtered:
        print("[Odds] 0 of today's events matched a watched team on both sides")
        return []

    odds_list = _fetch_odds_multi([e["id"] for e in filtered])
    odds_by_id = {o["id"]: o for o in odds_list}

    out = []
    for e in filtered:
        odds_data = odds_by_id.get(e["id"])
        if not odds_data:
            continue
        markets = _extract_markets(odds_data)
        if not markets["1x2"]:
            continue
        out.append({
            "home":         e.get("home", "?"),
            "away":         e.get("away", "?"),
            "league_name":  (e.get("league") or {}).get("name") or "Football",
            "kickoff_iso":  e.get("date") or "",
            "odds":         markets["1x2"],
            "bts":          markets["bts"],  # optional — may be None
        })

    out.sort(key=lambda m: m["kickoff_iso"])
    print(f"[Odds] {len(out)} watched-team match(es) with a {ODDS_BOOKMAKER} price today")
    return out


def chunk_for_cards(matches: list[dict], min_per_card: int = 3, max_per_card: int = 5) -> list[list[dict]]:
    """Splits into evenly-balanced groups of min_per_card..max_per_card
    (e.g. 8 matches with max=5 -> [4, 4], never [5, 3]). A day with
    max_per_card or fewer matches always stays a single card, even if
    that's below min_per_card — better to post what's actually
    available than hold matches back waiting for a minimum that may
    never arrive."""
    n = len(matches)
    if n == 0:
        return []
    if n <= max_per_card:
        return [matches]
    num_cards = math.ceil(n / max_per_card)
    base, remainder = divmod(n, num_cards)
    chunks, i = [], 0
    for c in range(num_cards):
        size = base + (1 if c < remainder else 0)
        chunks.append(matches[i:i + size])
        i += size
    return chunks
