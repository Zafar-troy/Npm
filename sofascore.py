"""
sofascore.py — Match Corna Live: SofaScore data layer
======================================================
Sole source, called from scraper.get_todays_matches(). ESPN was
removed as a data source (its per-league scoreboard polling — dozens
of sequential HTTP calls every tick — was the main cause of events
posting more than 3 minutes late).

WHY SOFASCORE:
  A single "live events" feed call (no key, no auth) covers everything
  currently live, rather than one HTTP round-trip per league slug.
  This module normalises that feed into the same match dict shape the
  old ESPN normaliser used to produce, so poster.py, graphics.py, and
  bot.py need no changes at all.

MATCH INCLUSION (deliberately curated, NOT everything live):
  ✅ Either team must be on the watch-list (data/teams_master.json),
     AND that team's league must be switched ON via its LEAGUE_* env
     var (config.WATCHED_LEAGUES) — Malawi Super League clubs live in
     that same file, so Malawi coverage needs no special-casing.
  ❌ Everything else — amateur/reserve/youth friendlies, women's and
     age-group football, and any match with no watched team at all.
     To add coverage for a new club or competition, add its teams to
     data/teams_master.json rather than editing this file.

GOALS / RED CARDS:
  Fetched via a second call to /event/{id}/incidents — only for matches
  that already passed the whitelist filter above, to keep API calls
  reasonable. SofaScore's incident shape is not officially documented,
  so this is parsed defensively: if a field is missing or shaped
  unexpectedly, the goal/card is skipped rather than guessed at.
  ⚠️ NEEDS LIVE VERIFICATION against a real match before you trust the
  scorer/assist/card output blindly — this cannot be fully tested
  offline since it depends on SofaScore's real in-play incident feed.
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from scraper import is_national_team, _comp_flag  # reuse existing logic
import config

# SofaScore's API sits behind Cloudflare, which fingerprints the TLS/JA3
# handshake of the plain `requests` library and blocks it with a 403 —
# this happens regardless of which server or network makes the call (so
# it isn't specifically a Railway problem, and isn't specifically a
# Termux fix either — it can start/stop at any time on any host).
# curl_cffi impersonates a real Chrome TLS/JA3 + HTTP2 fingerprint (not
# just headers), which is what actually gets through Cloudflare's
# current check — plain `requests`, and header-only spoofers like
# cloudscraper, now get a flat 403 on every request. Falls back to plain
# `requests` (old behavior, will 403) if curl_cffi isn't installed, so a
# missing dependency doesn't hard-crash the bot outright.
try:
    from curl_cffi import requests as _curl_cffi_requests

    class _CurlCffiSession:
        """Thin wrapper so _sofascore_get's `_http.get(...)` call site
        doesn't need to change regardless of which backend is active."""
        def get(self, url, headers=None, timeout=10):
            return _curl_cffi_requests.get(
                url, headers=headers, timeout=timeout, impersonate="chrome"
            )

    _http = _CurlCffiSession()
    print("[SofaScore] Using curl_cffi (Chrome TLS impersonation) for HTTP requests")
except ImportError:
    _http = requests
    print("[SofaScore] curl_cffi not installed — falling back to plain "
          "requests (will hit 403s from Cloudflare). Run: pip install curl_cffi")

# ── Master watched-team list (data/teams_master.json) ───────────────
# Any match involving one of these team IDs is treated as "priority"
# regardless of tournament name/whitelist — this is what makes
# team_fixtures.py's claim true that a watched team's match "shows up
# naturally in get_live_matches() once it kicks off": without this,
# leagues like MLS/Saudi Pro League/Brazil Série A/South African
# Premiership (not in PRIORITY_TOURNAMENTS or SECONDARY_TOURNAMENTS)
# would silently be dropped
# even though a specifically-monitored team is playing in them.
_MASTER_TEAMS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "teams_master.json"
)


def _load_master_team_ids() -> set[str]:
    try:
        with open(_MASTER_TEAMS_FILE, encoding="utf-8") as f:
            by_league = json.load(f)
        ids = set()
        for teams in by_league.values():
            ids.update(str(k) for k in teams.keys())
        return ids
    except Exception as e:
        print(f"[SofaScore] Could not load master team list: {e}")
        return set()


def _load_master_team_leagues() -> dict[str, str]:
    """team_id (str) -> league name (matches config.WATCHED_LEAGUES keys
    exactly, since both read the same top-level keys in teams_master.json).
    Used so a watched team's live match only gets the automatic
    'priority, never dropped' treatment below when its own league
    toggle is actually ON — otherwise a watched team in a league you
    just switched OFF would keep showing up regardless."""
    try:
        with open(_MASTER_TEAMS_FILE, encoding="utf-8") as f:
            by_league = json.load(f)
        leagues = {}
        for league_name, teams in by_league.items():
            for team_id in teams.keys():
                leagues[str(team_id)] = league_name
        return leagues
    except Exception as e:
        print(f"[SofaScore] Could not load master team leagues: {e}")
        return {}


MASTER_TEAM_IDS = _load_master_team_ids()
MASTER_TEAM_LEAGUES = _load_master_team_leagues()

SOFASCORE_API = "https://api.sofascore.com/api/v1"
# Full Chrome-like header set — confirmed working in practice (two
# independent low-volume test scripts using this shape got 200s at the
# exact moment bot.py's minimal User-Agent-only headers were 403ing).
# The extra fields (Sec-Ch-Ua*, Origin, Referer, Sec-Fetch-*) cost
# nothing to send and make every request look more like an actual
# browser tab on sofascore.com rather than a bare script — can't hurt,
# and matches what's demonstrably getting through Cloudflare right now.
SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="131", "Google Chrome";v="131"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# Tracks the last known state of every in-progress SofaScore match, keyed
# by our normalized "id" (e.g. "sofascore_12345"). SofaScore's public
# live-events endpoint only lists matches that are CURRENTLY live — once
# a match ends, it can disappear from that feed within a poll or two,
# often before we ever see its status flip to FINISHED. Without this
# cache, a match that vanishes mid-status-transition never posts its
# full-time result at all (this is the exact "showed IN_PLAY for two
# hours, marked finished, zero posts" bug). See _detect_vanished_matches().
_last_seen_live: dict[str, dict] = {}
_IN_PROGRESS_STATUSES = {"IN_PLAY", "PAUSED", "EXTRA_TIME", "SHOOTOUT"}

# Every match id we've ever synthesized a FINISHED copy for, for the
# life of this process. Belt-and-suspenders against duplicate full-time
# posts: bot.py's own _key_fulltime dedup already stops a second
# identical post, but if a match flickers out of the live feed, comes
# back (still genuinely in progress), and then flickers out again, the
# id re-enters _last_seen_live on the reappearance and would otherwise
# be eligible for synthesis a second time. Once synthesized, never
# again — a match only gets one shot at a synthetic full-time.
_synthesized_ever: set[str] = set()


def export_vanish_state() -> dict:
    """Serializable snapshot of the vanished-match tracking state
    (_last_seen_live + _synthesized_ever), for persisting into
    bot.py's state_v2.json across restarts. Without this, a match that
    vanishes from SofaScore's live feed right around a redeploy/restart
    loses its tracking entry and never gets its FT + sealed goals
    posted at all — the exact bug this closes."""
    return {
        "last_seen_live":   _last_seen_live,
        "synthesized_ever": list(_synthesized_ever),
    }


def import_vanish_state(data: dict | None):
    """Restores _last_seen_live + _synthesized_ever from a dict
    produced by export_vanish_state(). Safe to call with an empty/None
    dict (fresh install, or an old state file predating this key)."""
    global _last_seen_live, _synthesized_ever
    if not data:
        return
    _last_seen_live   = data.get("last_seen_live", {}) or {}
    _synthesized_ever = set(data.get("synthesized_ever", []) or [])


def _fetch_event_details(raw_id: str) -> dict | None:
    """One-off lookup of a single event's current data straight from
    SofaScore, independent of the /live feed. Used when a match vanishes
    from /live so we can grab its REAL final score/status instead of
    trusting whatever we last polled — closing the gap where a goal in
    the last ~1 poll interval before full time got missed entirely."""
    data = _sofascore_get(f"{SOFASCORE_API}/event/{raw_id}")
    if not data:
        return None
    return data.get("event")


def _detect_vanished_matches(current_matches: list[dict]) -> list[dict]:
    """Compares this poll's matches against the last poll's. Any match
    that was in progress last time but is completely absent now is
    assumed to have finished — SofaScore just didn't keep it in the live
    feed long enough for us to see the transition. Returns FINISHED
    copies so bot.py still posts a full-time result instead of silence.

    Before falling back to the last-known cached state, this does one
    direct per-event fetch (_fetch_event_details) to get the REAL final
    score and a fresh incidents pull — this closes the gap where a goal
    scored in the same poll interval the match disappeared in would
    otherwise be missing from the full-time post (e.g. a 4th goal never
    showing up because the last /live poll we saw still had it at 3).
    Only falls back to the old best-effort cached-snapshot reconstruction
    if that direct fetch itself fails (network error, event pulled, etc)."""
    global _last_seen_live
    current_ids = {m["id"] for m in current_matches}
    synthesized = []
    for old_id, old_match in _last_seen_live.items():
        if old_id in current_ids:
            continue
        if old_id in _synthesized_ever:
            # Already got one synthetic full-time out of this id in a
            # previous poll (it must have flickered back into the live
            # feed since then, or we'd have removed it from the cache
            # below) — don't synthesize a second one.
            continue

        finished = None
        fresh_event = _fetch_event_details(old_match["_raw_id"])
        if fresh_event:
            fresh_match = _normalize_sofascore(fresh_event)
            if fresh_match:
                fresh_match["status"] = "FINISHED"
                if not fresh_match.get("_full_time_only"):
                    goals, bookings, ok = _fetch_incidents(
                        int(fresh_match["_raw_id"]),
                        fresh_match["homeTeam"]["name"],
                        fresh_match["awayTeam"]["name"],
                    )
                    # If this incidents fetch itself failed, don't wipe
                    # goals/bookings to empty — fall back to the last
                    # genuinely-known snapshot rather than posting a
                    # full-time result that's silently missing every goal.
                    fresh_match["goals"] = goals if ok else old_match.get("goals", [])
                    fresh_match["bookings"] = bookings if ok else old_match.get("bookings", [])
                finished = fresh_match

        if finished is None:
            # Direct fetch failed — fall back to the old best-effort
            # reconstruction from the last poll we actually saw.
            h = old_match["homeTeam"]["name"]
            a = old_match["awayTeam"]["name"]
            hs = old_match["score"]["fullTime"].get("home")
            as_ = old_match["score"]["fullTime"].get("away")
            print(f"[SofaScore] ⚠️  {h} vs {a} vanished from the live feed and the "
                  f"direct re-fetch also failed — posting final score from last "
                  f"known state ({hs}-{as_})")
            finished = {**old_match, "status": "FINISHED"}
        else:
            hs = finished["score"]["fullTime"].get("home")
            as_ = finished["score"]["fullTime"].get("away")
            print(f"[SofaScore] ⚠️  {finished['homeTeam']['name']} vs "
                  f"{finished['awayTeam']['name']} vanished from the live feed — "
                  f"re-fetched real final score ({hs}-{as_})")

        synthesized.append(finished)
        _synthesized_ever.add(old_id)

    # Rebuild the cache for next poll: only matches still genuinely in
    # progress need tracking — anything FINISHED (seen normally or just
    # synthesized above) is done and shouldn't be watched for vanishing
    # again.
    _last_seen_live = {
        m["id"]: m for m in current_matches
        if m["status"] in _IN_PROGRESS_STATUSES
    }
    return synthesized

# ══════════════════════════════════════════════════════════════════
# INCLUSION RULE
# ══════════════════════════════════════════════════════════════════
# Coverage is entirely watched-team-driven now (see
# _normalize_sofascore's _watched_and_league_on check below) — a match
# passes only if a team in data/teams_master.json is playing AND that
# team's league is switched ON in config.WATCHED_LEAGUES (LEAGUE_* env
# vars). A per-tournament-id whitelist used to sit alongside that as a
# second way in; it was fully disabled and removed since it never
# fired once the watched-team check became the only real gate — every
# match Match Corna Live posts already comes through a watched team,
# so a separate tournament allowlist added nothing except more moving
# parts to keep in sync. To cover a new competition, add its teams to
# data/teams_master.json instead of an id here.
#
# Keeps age-group / reserve / women's football out even if the fixture
# happens to involve a watched team.
_EXCLUDE_KEYWORDS = (
    "u15", "u16", "u17", "u18", "u19", "u20", "u21", "u23",
    "women", "reserve", "reserves", "youth", "junior", "academy",
)


def _is_excluded_team_name(name: str) -> bool:
    name_l = (name or "").lower()
    return any(kw in name_l for kw in _EXCLUDE_KEYWORDS)


# ══════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════

def _sofascore_get(url: str, timeout: int = 10, _retried_plain: bool = False) -> dict | None:
    try:
        r = _http.get(url, headers=SOFASCORE_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            print("[SofaScore] ⚠️  Rate limited — waiting 30s")
            time.sleep(30)
            return _sofascore_get(url, timeout, _retried_plain)
        if r.status_code == 403:
            # Cloudflare's block flips on/off unpredictably (confirmed in
            # practice: curl_cffi's Chrome TLS impersonation and plain
            # `requests` have BOTH been the one that gets through, at
            # different times, on the same network). Rather than betting
            # on one approach staying correct, fall back to plain
            # `requests` once per call if the primary backend (_http,
            # normally curl_cffi) 403s — and only if _http isn't already
            # plain `requests` (no point retrying the same thing twice).
            if not _retried_plain and _http is not requests:
                print(f"[SofaScore] HTTP 403 via curl_cffi — retrying with plain requests: {url[:80]}")
                try:
                    r2 = requests.get(url, headers=SOFASCORE_HEADERS, timeout=timeout)
                    if r2.status_code == 200:
                        print("[SofaScore] ✅ Plain requests got through")
                        return r2.json()
                    print(f"[SofaScore] HTTP {r2.status_code} on plain-requests retry too: {url[:80]}")
                    return None
                except Exception as e:
                    print(f"[SofaScore] ❌ Plain-requests retry failed: {e}")
                    return None
        print(f"[SofaScore] HTTP {r.status_code}: {url[:80]}")
    except Exception as e:
        print(f"[SofaScore] ❌ {e}")
    return None


# ══════════════════════════════════════════════════════════════════
# STATUS MAPPING — SofaScore -> the same norm_status values ESPN uses
# (SCHEDULED / IN_PLAY / PAUSED / EXTRA_TIME / SHOOTOUT / FINISHED)
# ══════════════════════════════════════════════════════════════════

def _team_crest_url(team_id) -> str:
    """SofaScore doesn't embed a badge URL in the event payload — badges
    live at a separate per-team image endpoint. Unverified whether
    graphics.py can consume this URL directly (vs needing bytes fetched
    first) — check that assumption on a real match before relying on it."""
    if not team_id:
        return ""
    return f"{SOFASCORE_API}/team/{team_id}/image"


def _norm_status(status: dict) -> tuple[str, bool, bool]:
    """Returns (norm_status, went_to_et, went_to_penalties)."""
    code = status.get("code")
    desc = (status.get("description", "") or "").lower()

    went_to_et = False
    went_to_penalties = False

    if "penalt" in desc:
        went_to_et = True
        went_to_penalties = True
        return "SHOOTOUT", went_to_et, went_to_penalties
    if "extra time" in desc or "et " in desc or desc.startswith("et"):
        went_to_et = True
        return "EXTRA_TIME", went_to_et, went_to_penalties
    if "halftime" in desc or desc == "ht":
        return "PAUSED", went_to_et, went_to_penalties
    if desc in ("finished", "ft", "after extra time", "after penalties", "ended"):
        if "extra time" in desc:
            went_to_et = True
        if "penalt" in desc:
            went_to_et = True
            went_to_penalties = True
        return "FINISHED", went_to_et, went_to_penalties
    if status.get("type") == "inprogress" or "half" in desc or "live" in desc:
        return "IN_PLAY", went_to_et, went_to_penalties
    if status.get("type") == "finished":
        return "FINISHED", went_to_et, went_to_penalties
    return "SCHEDULED", went_to_et, went_to_penalties


# ══════════════════════════════════════════════════════════════════
# INCIDENTS (goals / red cards) — best effort, verify against live data
# ══════════════════════════════════════════════════════════════════

def _parse_minute_sort(minute_str) -> int:
    """Numeric sort value for a minute string like '45', '45+2', '90+5',
    used ONLY for ordering goals chronologically (never for display —
    poster._minute() still handles that). Handles int/float/str input.
    Stoppage time is folded in as hundredths so e.g. '45+2' (4502) sorts
    after '45' (4500) but before '46' (4600). Anything unparseable
    (None, '?', a weird shape) sorts as 9999*100 — i.e. dead last —
    rather than crashing or accidentally sorting first, since an
    unknown minute is the one case we can't safely place in the
    timeline at all."""
    try:
        s = str(minute_str).strip()
        m = re.match(r"(\d+)(?:\+(\d+))?", s)
        if not m:
            return 999900
        base = int(m.group(1))
        added = int(m.group(2)) if m.group(2) else 0
        return base * 100 + added
    except Exception:
        return 999900


def _goal_sort_key(g: dict) -> tuple:
    """Chronological sort key for one goal dict: (minute, score-snapshot
    total). The score snapshot is the tiebreaker for two goals that land
    in the same minute — SofaScore's own homeScore/awayScore-at-incident
    total only ever increases, so a higher total means it happened
    later even when the minute string is identical (e.g. a 90+2' brace).
    Goals with no usable snapshot sort after ones that have one, at that
    same minute, rather than arbitrarily first."""
    minute_val = _parse_minute_sort(g.get("minute"))
    sc = g.get("score") or []
    if sc and len(sc) == 2 and sc[0] is not None and sc[1] is not None:
        try:
            score_total = int(sc[0]) + int(sc[1])
        except Exception:
            score_total = 9999
    else:
        score_total = 9999
    return (minute_val, score_total)


def _fetch_incidents(event_id: int, home_name: str, away_name: str) -> tuple[list, list, bool]:
    goals, bookings = [], []
    data = _sofascore_get(f"{SOFASCORE_API}/event/{event_id}/incidents")
    if not data:
        # Fetch failed (network error, 403, etc) — the empty return below
        # must NOT be read as "SofaScore says there are genuinely zero
        # incidents right now". The caller needs to tell those two cases
        # apart (see the `ok` flag) so a transient failure never gets
        # mistaken for every previously-seen goal having been cancelled.
        return goals, bookings, False

    for inc in data.get("incidents", []):
        itype = inc.get("incidentType", "")
        is_home = inc.get("isHome", True)
        team_name = home_name if is_home else away_name
        minute = str(inc.get("time", "?"))

        if itype == "goal":
            # Defensive penalty-shootout detection: SofaScore's shootout
            # kicks have shown up in the wild tagged a few different ways
            # depending on endpoint/season — sometimes a distinct
            # incidentType, sometimes still "goal" but with a period/
            # reason field mentioning the shootout. Check every hint we
            # know of; if ANY of them fire, tag the goal _is_shootout
            # instead of dropping it outright — bot.py then knows to
            # never turn it into a live goal card, and poster.py knows
            # to leave it out of the open-play scorer lines, while it
            # still exists in match["goals"] in case a future FT-caption
            # feature wants to summarize the shootout from it.
            # ⚠️ NEEDS LIVE VERIFICATION against a real shootout — the
            # exact field shape isn't documented, so this is best-effort.
            period_hint = str(inc.get("period", "") or inc.get("periodType", "") or "").lower()
            reason_hint = str(inc.get("reason", "") or inc.get("incidentClass", "") or "").lower()
            is_shootout_kick = (
                "shootout" in period_hint
                or ("penalt" in period_hint and "shootout" in period_hint)
                or "shootout" in reason_hint
                or bool(inc.get("isPenaltyShootout"))
            )
            # SofaScore uses two different shapes for the scorer, seemingly
            # interchangeably: sometimes a full nested "player": {"name":...}
            # object, sometimes just a flat top-level "playerName" string
            # with no "player" object at all. Check both before falling
            # back to the team name.
            player = (
                (inc.get("player") or {}).get("name")
                or inc.get("playerName")
                or None
            )
            assist_obj = inc.get("assist1") or {}
            assist_name = assist_obj.get("name") or inc.get("assist1Name")

            # Use SofaScore's own score-at-this-incident snapshot rather
            # than reconstructing the running score by counting goals in
            # list order — that order isn't guaranteed chronological, so
            # counting can misattribute the score when two goals land in
            # the same poll tick. If SofaScore ever omits these fields on
            # a given incident, leave score empty — bot.py's own fallback
            # counter then takes over for that one goal only.
            snap_home = inc.get("homeScore")
            snap_away = inc.get("awayScore")
            score = (
                [int(snap_home), int(snap_away)]
                if snap_home is not None and snap_away is not None
                else []
            )

            # PRIMARY: use RESULTING SCORELINE as the stable key (NEVER scorer name).
            # The scoreline snapshot is what matters: only ever increases, never 
            # corrected backward, stable across polls. NEVER includes scorer name.
            # This is the deduplication key that MUST be used for open-play goals.
            if snap_home is not None and snap_away is not None:
                _play_id = f"{event_id}_{'H' if is_home else 'A'}_{int(snap_home)}-{int(snap_away)}"
            else:
                # Fallback if no score snapshot: use SofaScore's incident id
                if inc.get("id"):
                    _play_id = str(inc.get("id"))
                else:
                    # Last resort: base minute (stripping stoppage like "45+2" -> "45")
                    base_minute = re.match(r"(\d+)", minute).group(1) if re.match(r"(\d+)", minute) else minute
                    _play_id = f"{event_id}_{'H' if is_home else 'A'}_{base_minute}"

            goals.append({
                "minute": minute,
                "_play_id": _play_id,
                "scorer": {"name": player},
                "assist": {"name": assist_name} if assist_name else {},
                "team": {"shortName": team_name},
                "isHome": is_home,
                "score": score,
                "_is_shootout": is_shootout_kick,
            })
        elif itype == "card":
            card_class = (inc.get("incidentClass", "") or "").lower()
            if "red" not in card_class:
                continue  # only red cards are posted, same as ESPN path
            player = (inc.get("player") or {}).get("name") or inc.get("playerName") or None
            # Same minute-instability risk as goals — fall back to
            # player+team (a specific person being sent off) rather than
            # minute, which can shift between polls.
            card_fallback_id = f"{event_id}_{team_name}_{player}_card"
            bookings.append({
                "minute": minute,
                "_play_id": inc.get("id") or card_fallback_id,
                "card": "RED_CARD",
                "player": {"name": player},
                "team": {"shortName": team_name},
                "isHome": is_home,
            })

    # SofaScore's incident feed is NOT guaranteed to be in chronological
    # order (see module docstring) — sort here so every downstream
    # consumer (bot.py's goal loop, poster.py's scorer lines) sees goals
    # in the order they actually happened, not the order the API
    # happened to return them in. bot.py's goal loop additionally
    # re-derives this same ordering itself before posting (belt and
    # suspenders — see _goal_sort_key equivalent there), but sorting the
    # source list here means every OTHER consumer (half-time/full-time
    # scorer lines) is correct too without having to know about ordering
    # at all.
    goals.sort(key=_goal_sort_key)

    return goals, bookings, True


# ══════════════════════════════════════════════════════════════════
# NORMALISER — produces the exact same dict shape as _normalize_espn
# ══════════════════════════════════════════════════════════════════

def _normalize_sofascore(event: dict) -> dict | None:
    try:
        home_name = (event.get("homeTeam") or {}).get("name", "")
        away_name = (event.get("awayTeam") or {}).get("name", "")
        if not home_name or not away_name:
            return None

        if _is_excluded_team_name(home_name) or _is_excluded_team_name(away_name):
            return None

        tournament = event.get("tournament", {}) or {}
        home_id = str((event.get("homeTeam") or {}).get("id", ""))
        away_id = str((event.get("awayTeam") or {}).get("id", ""))

        def _watched_and_league_on(team_id: str) -> bool:
            if team_id not in MASTER_TEAM_IDS:
                return False
            league = MASTER_TEAM_LEAGUES.get(team_id)
            # No league on record for this team id (shouldn't normally
            # happen) -> fail open, same "don't silently drop" spirit
            # as WATCHED_LEAGUES_DEFAULT.
            if league is None:
                league_on = True
            else:
                league_on = config.WATCHED_LEAGUES.get(league, config.WATCHED_LEAGUES_DEFAULT)
            if not league_on:
                return False
            # Favourites narrows further, only within a league that's
            # already ON above — league OFF always wins regardless of
            # favourites. See config.FAVORITES_MODE.
            if config.FAVORITES_MODE and team_id not in config.FAVORITE_TEAM_IDS:
                return False
            return True

        is_watched_team = _watched_and_league_on(home_id) or _watched_and_league_on(away_id)

        # ONLY watched teams (data/teams_master.json) pass through.
        if not is_watched_team:
            return None

        comp_name = tournament.get("name", "Football")
        event_id = event.get("id")
        status = event.get("status", {}) or {}
        norm_status, went_to_et, went_to_penalties = _norm_status(status)

        # Incidents (goals/cards) are deliberately NOT fetched here — see
        # get_live_matches(), which fetches them concurrently afterwards
        # for all matches that need them. Doing it inline here would mean
        # one slow/blocked network request per live match, back-to-back.
        goals, bookings = [], []

        home_sc = (event.get("homeScore") or {}).get("current")
        away_sc = (event.get("awayScore") or {}).get("current")

        # Penalty-shootout score, when the match went that far. SofaScore
        # nests this differently across endpoints/seasons in the wild —
        # try every shape seen so far, defensively, and leave None (not
        # 0) if nothing matches so poster.fmt_fulltime's "if ph is not
        # None" check correctly falls back to just the score line instead
        # of claiming a false 0-0 shootout.
        # ⚠️ NEEDS LIVE VERIFICATION against a real shootout.
        home_score_obj = event.get("homeScore") or {}
        away_score_obj = event.get("awayScore") or {}
        pen_home = home_score_obj.get("penalties")
        pen_away = away_score_obj.get("penalties")
        if pen_home is None:
            pen_home = home_score_obj.get("period3")  # some feeds stash it as an extra "period"
        if pen_away is None:
            pen_away = away_score_obj.get("period3")
        try:
            pen_home = int(pen_home) if pen_home is not None else None
            pen_away = int(pen_away) if pen_away is not None else None
        except Exception:
            pen_home = pen_away = None

        start_ts = event.get("startTimestamp")
        utc_date = (
            datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
            if start_ts else ""
        )

        is_intl = is_national_team(home_name) and is_national_team(away_name)

        return {
            "id":                 f"sofascore_{event_id}",
            "_raw_id":            str(event_id),
            "_league_slug":       f"sofascore:{tournament.get('slug', '')}",
            "utcDate":            utc_date,
            "status":             norm_status,
            "_minute":            str(status.get("description", "")),
            "_source":            "sofascore",
            "_comp_name":         comp_name,
            "_comp_flag":         _comp_flag(comp_name),
            "_is_intl":           is_intl,
            "_full_time_only":    False,
            "var_events":         [],  # filled in by _detect_var_disallowed_goals(), if any
            "_went_to_et":        went_to_et,
            "_went_to_penalties": went_to_penalties,
            "_penalty_home":      pen_home,
            "_penalty_away":      pen_away,
            "homeTeam": {
                "id":        str((event.get("homeTeam") or {}).get("id", "")),
                "name":      home_name,
                "shortName": (event.get("homeTeam") or {}).get("shortName", home_name),
                "crest":     _team_crest_url((event.get("homeTeam") or {}).get("id")),
            },
            "awayTeam": {
                "id":        str((event.get("awayTeam") or {}).get("id", "")),
                "name":      away_name,
                "shortName": (event.get("awayTeam") or {}).get("shortName", away_name),
                "crest":     _team_crest_url((event.get("awayTeam") or {}).get("id")),
            },
            "score": {
                "halfTime": {"home": None, "away": None},
                "fullTime": {
                    "home": int(home_sc) if home_sc is not None else None,
                    "away": int(away_sc) if away_sc is not None else None,
                },
            },
            "goals":    goals,
            "bookings": bookings,
            "lineups":  [],
        }

    except Exception as e:
        print(f"[SofaScore] Normalize error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# LINEUPS — mirrors ESPN's output shape, so poster.py needs no changes
# ══════════════════════════════════════════════════════════════════

def get_lineup(event_id: str, home_name: str = "", away_name: str = "") -> list[dict]:
    """Fetch starting XI + formation for a SofaScore match.
    Returns [] if not yet available (typical before ~60min pre-kickoff),
    same convention as ESPN's get_lineup in scraper.py.

    home_name/away_name: the match's actual team names (from the event
    object). SofaScore's /event/{id}/lineups endpoint does NOT include a
    team name in its "home"/"away" objects (only formation + players) —
    tagging each side with the real name we already know, instead of
    trying to read a name back out of the lineups payload, is what
    fixes lineups that were being *fetched* successfully but never
    posted: without a real name here every side fell back to "?",
    poster.py's name-matching failed for both teams, and the caption
    came back empty (silently skipped, forever, since this is only
    attempted while the match is still SCHEDULED)."""
    raw_id = str(event_id).replace("sofascore_", "")
    data = _sofascore_get(f"{SOFASCORE_API}/event/{raw_id}/lineups")
    if not data:
        print(f"[SofaScore] Lineup: no response for event {raw_id}")
        return []

    try:
        lineups = []
        side_names = {"home": home_name, "away": away_name}
        for side in ("home", "away"):
            team_data = data.get(side)
            if not team_data:
                continue

            team_name = (side_names.get(side)
                         or team_data.get("name")
                         or (team_data.get("team", {}) or {}).get("name", "?"))
            formation = team_data.get("formation") or ""
            players = team_data.get("players", []) or team_data.get("lineup", [])
            if not players:
                continue

            # Only starters — SofaScore marks bench players with
            # "substitute": True, so False (explicitly) is what we want.
            starters = []
            for p in players:
                if p.get("substitute") is not False:
                    continue
                name = (p.get("player", {}) or {}).get("name") or p.get("name", "?")
                if name and name != "?":
                    starters.append({"player": {"name": name}})

            if starters:
                lineups.append({
                    "team":      team_name,
                    "formation": formation,
                    "startXI":   starters,
                })

        if not lineups:
            print(f"[SofaScore] Lineup: no starters found yet for event {raw_id} (normal pre-kickoff)")
        return lineups

    except Exception as e:
        print(f"[SofaScore] Lineup parse error for event {raw_id}: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════

_INCIDENT_STATUSES = {"IN_PLAY", "PAUSED", "EXTRA_TIME", "SHOOTOUT", "FINISHED"}


def _attach_incidents(match: dict) -> dict:
    """Fetches goals/red cards for one match. Safe to run in a thread —
    only touches its own match dict, no shared state."""
    if match["status"] not in _INCIDENT_STATUSES:
        return match
    goals, bookings, ok = _fetch_incidents(
        int(match["_raw_id"]), match["homeTeam"]["name"], match["awayTeam"]["name"]
    )
    if ok:
        match["goals"] = goals
        match["bookings"] = bookings
    else:
        # Fetch failed this tick (network blip, 403, etc) — leave
        # match["goals"] as whatever _normalize_sofascore defaulted it
        # to and flag it. Without this flag, the vanished-goal/VAR
        # check in get_live_matches() would see "no goals this poll"
        # and wrongly conclude every goal from the previous poll got
        # cancelled by VAR, when really we just failed to fetch.
        match["_incidents_fetch_failed"] = True
    return match


def _detect_var_disallowed_goals(matches: list[dict]) -> None:
    """Catches goals SofaScore's incidents feed silently retracted — its
    real-world VAR behaviour is to just remove the incident from the
    list once a goal is overturned, not to flag it in place. Compares
    THIS poll's goal ids against the PREVIOUS poll's (via
    _last_seen_live, read here — it's still holding last poll's data,
    since _detect_vanished_matches() hasn't overwritten it yet at this
    point in get_live_matches()). Any goal id present last poll but
    missing this poll, for a match that's still genuinely live, is
    proof the goal never actually stood.

    This replaces the old approach of comparing a goal's score
    snapshot to the match's CURRENT/final score — that heuristic only
    caught a cancelled goal if the real scoreline never caught back up
    to the cancelled goal's snapshot. Once later, genuine goals bring
    the score back in line (e.g. a 54' goal gets overturned, then a
    76'/77' goal-goal exchange lands on the exact same scoreline), the
    old check saw nothing wrong and the phantom goal leaked straight
    into the half-time/full-time scorer lines — this is the bug behind
    a "1-0" full-time post for a match that actually finished 1-1.
    Modifies each match dict in place: appends a var_events entry (so
    bot.py's existing POST_VAR_DISALLOWED path can post a "No Goal"
    correction if the goal was already posted live) and leaves the
    goal itself already absent from match["goals"] — nothing further
    to strip, since the fresh incidents fetch this poll simply never
    included it."""
    for m in matches:
        if m.get("_incidents_fetch_failed"):
            continue  # can't tell "cancelled" from "we failed to fetch"
        prev = _last_seen_live.get(m["id"])
        if not prev:
            continue
        prev_goals = [g for g in prev.get("goals", []) if not g.get("_is_shootout")]
        curr_ids = {g["_play_id"] for g in m.get("goals", []) if not g.get("_is_shootout")}
        for g in prev_goals:
            if g["_play_id"] in curr_ids:
                continue
            scorer = (g.get("scorer") or {}).get("name") or "Unknown"
            hname, aname = m["homeTeam"]["name"], m["awayTeam"]["name"]
            team_name = (g.get("team") or {}).get("shortName") or (hname if g.get("isHome") else aname)
            print(f"[SofaScore] 🚫 Goal disallowed by VAR — {scorer} "
                  f"({g.get('minute')}') no longer in incidents feed — {hname} vs {aname}")
            m.setdefault("var_events", []).append({
                "player": scorer,
                "minute": g.get("minute"),
                "reason": "VAR Review",
                "team": team_name,
                "isHome": g.get("isHome"),
                "_play_id": g["_play_id"],
            })


def get_live_matches() -> list[dict]:
    """Fetch + filter + normalise every currently-live match SofaScore has."""
    print("[SofaScore] Fetching live matches...")
    data = _sofascore_get(f"{SOFASCORE_API}/sport/football/events/live")
    if not data:
        # Fetch failed (network error, 403, etc) — don't treat this as
        # "no matches are live"; that would make _detect_vanished_matches
        # think every in-progress match just vanished and post a batch
        # of premature "final scores" off one bad poll. Leave last-known
        # state untouched and just return no updates for this tick.
        return []

    matches = []
    for event in data.get("events", []):
        n = _normalize_sofascore(event)
        if n:
            matches.append(n)

    # Incidents (goals/cards) each need their own network round-trip.
    # Fetching them one-by-one for every live match is what was making
    # each poll tick noticeably slower — running them concurrently instead
    # cuts that back down to roughly the time of the single slowest call.
    needs_incidents = [
        m for m in matches
        if m["status"] in _INCIDENT_STATUSES and not m.get("_full_time_only")
    ]
    if needs_incidents:
        with ThreadPoolExecutor(max_workers=min(8, len(needs_incidents))) as pool:
            list(pool.map(_attach_incidents, needs_incidents))

    _detect_var_disallowed_goals(matches)

    print(f"[SofaScore] {len(matches)} watched-team match(es) live")
    if matches:
        summary = ", ".join(
            f"{m['homeTeam']['name']} vs {m['awayTeam']['name']} [{m['status']}]"
            for m in matches
        )
        print(f"[SofaScore]   {summary}")

    vanished = _detect_vanished_matches(matches)
    matches.extend(vanished)
    return matches
