"""
team_fixtures.py — Match Corna Live: master-team-list fixture alerts
=====================================================================
A THIRD data path, separate from scraper.py's get_todays_matches() and the tournament
whitelist in sofascore.py (get_live_matches()). Those two answer "what's
live right now in a competition we recognise?". This module answers a
different question: "what are OUR watched teams playing today?" — driven
by an explicit master list of SofaScore team IDs (data/teams_master.json)
rather than by tournament name.

Why not just reuse sofascore._normalize_sofascore()?
  That function's tier check (_tournament_tier) silently drops anything
  not in PRIORITY_TOURNAMENTS / SECONDARY_TOURNAMENTS / an always-include
  country. Several leagues in the master list — MLS, Saudi Pro League,
  Brazil Série A, South African Premiership — aren't on either list, so
  every fixture for those teams would vanish before you ever saw it.
  Inclusion here is decided by TEAM, not by tournament name (see step 5
  below), so this module has its own lightweight normaliser instead.

PIPELINE (as specified):
  1. Master Team List   — load every monitored team ID (data/teams_master.json)
  2. Fetch Fixtures     — query SofaScore's "next events" endpoint per team
  3. Extract Details    — event id, kickoff ts, team ids, tournament, status
  4. Filter by Date     — keep only fixtures whose kickoff is today (Malawi local day)
  5. Check Team Inclusion — home or away team id must be in the master list
  5b. Check League Toggle — at least one watched side's league must be ON
       in config.WATCHED_LEAGUES (data/teams_master.json's top-level key
       is the league name each team is tagged with). Lets you mute a
       specific league's pre-match alerts on a busy day without editing
       the master list itself.
  6. Deduplicate        — skip event ids already alerted (state file)
  7. Publish            — format + output the alert

Steps 5 and 6 look redundant with step 2 (we only ever fetched fixtures
FOR master-list teams) but they matter in practice: (a) SofaScore's
next-events endpoint occasionally returns a neighbouring team's fixture
row it shouldn't, so step 5 is a cheap safety net rather than a trust
exercise; (b) the same fixture is fetched twice whenever BOTH sides are
in the master list (e.g. Arsenal vs Chelsea), so step 6's dedupe-by-
event-id is what collapses that into a single alert.

LIVE FOLLOW-UP (goals / half-time / red cards / full-time / extra time /
penalties): this module only produces the pre-match alert. The event
dict it emits is deliberately shaped exactly like sofascore.py's own
normalised match dict (same "id": "sofascore_<event id>" convention), so
once the match kicks off it shows up naturally in sofascore.get_live_matches()
and bot.process_match() takes over posting those events through the
existing pipeline — nothing further to wire up here.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

from scraper import _comp_flag
from sofascore import SOFASCORE_API, SOFASCORE_HEADERS, _sofascore_get, _norm_status, _team_crest_url

import config
import poster

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MASTER_LIST_FILE = os.path.join(DATA_DIR, "teams_master.json")
STATE_FILE = os.path.join(config.DATA_DIR, "team_fixtures_state.json")


# ══════════════════════════════════════════════════════════════════
# STEP 1 — MASTER TEAM LIST
# ══════════════════════════════════════════════════════════════════

def load_master_list() -> dict[str, dict]:
    """Loads data/teams_master.json (competition -> {team_id: team_name})
    and flattens it to {team_id: {"name": ..., "league": ...}} — this is
    the single unified list every other step in this module checks
    against."""
    with open(MASTER_LIST_FILE, encoding="utf-8") as f:
        by_league = json.load(f)

    flat: dict[str, dict] = {}
    for league, teams in by_league.items():
        for team_id, name in teams.items():
            flat[str(team_id)] = {"name": name, "league": league}
    return flat


MASTER_TEAMS = load_master_list()


# ══════════════════════════════════════════════════════════════════
# STEP 2 — FETCH FIXTURES (per team, from SofaScore)
# ══════════════════════════════════════════════════════════════════

def _fetch_team_fixtures(team_id: str, page: int = 0) -> list[dict]:
    """SofaScore's public 'next events' endpoint for one team — no key,
    no auth, same convention as the rest of sofascore.py. page 0 covers
    the next batch of fixtures; that's normally enough to reach today's
    match (if any) without wasting extra requests on days with nothing
    scheduled."""
    url = f"{SOFASCORE_API}/team/{team_id}/events/next/{page}"
    data = _sofascore_get(url)
    if not data:
        return []
    return data.get("events", []) or []


def fetch_fixtures(master_teams: dict[str, dict] | None = None) -> list[dict]:
    """Fetches upcoming fixtures for every team in the master list.
    Deliberately throttled (small batches + a short pause between them)
    rather than firing all ~230 requests near-simultaneously: this only
    runs once every WATCHED_TEAMS_INTERVAL_MIN (default 180min), so
    there's no time pressure to rush it, and a burst of ~230 requests in
    under a second is exactly the kind of pattern Cloudflare's rate
    limiting flags — even with headers/TLS that would otherwise pass.
    Spreading the same requests over ~15-20s instead looks like normal
    traffic and has been enough in practice to avoid the 403s a full
    burst was triggering. Returns the raw SofaScore event dicts,
    deduplicated by event id — a fixture between two watched teams would
    otherwise come back once per team."""
    teams = master_teams if master_teams is not None else MASTER_TEAMS
    team_ids = list(teams.keys())
    print(f"[TeamFixtures] Fetching fixtures for {len(team_ids)} watched teams (throttled)...")

    BATCH_SIZE = 4        # concurrent requests per batch
    BATCH_DELAY = 0.35    # seconds paused between batches

    raw_by_event_id: dict[int, dict] = {}
    for i in range(0, len(team_ids), BATCH_SIZE):
        batch = team_ids[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
            results = pool.map(_fetch_team_fixtures, batch)
        for events in results:
            for event in events:
                eid = event.get("id")
                if eid is not None:
                    raw_by_event_id[eid] = event
        if i + BATCH_SIZE < len(team_ids):
            time.sleep(BATCH_DELAY)

    print(f"[TeamFixtures] {len(raw_by_event_id)} unique fixture(s) found across all watched teams")
    return list(raw_by_event_id.values())


# ══════════════════════════════════════════════════════════════════
# STEP 3 — EXTRACT DETAILS
# ══════════════════════════════════════════════════════════════════

def extract_details(event: dict) -> dict | None:
    """Pulls exactly the fields the pipeline needs — event id, kickoff
    timestamp, home/away team ids, tournament name, status — out of a
    raw SofaScore event. Returns None if the event is missing something
    essential (defensive, same spirit as sofascore._normalize_sofascore)."""
    try:
        event_id = event.get("id")
        start_ts = event.get("startTimestamp")
        home = event.get("homeTeam") or {}
        away = event.get("awayTeam") or {}
        home_id = home.get("id")
        away_id = away.get("id")
        tournament = event.get("tournament", {}) or {}

        if event_id is None or start_ts is None or home_id is None or away_id is None:
            return None

        return {
            "event_id": event_id,
            "kickoff_ts": start_ts,
            "kickoff_utc": datetime.fromtimestamp(start_ts, tz=timezone.utc),
            "home_team_id": str(home_id),
            "away_team_id": str(away_id),
            "home_name": home.get("name", ""),
            "away_name": away.get("name", ""),
            "tournament_name": tournament.get("name", "Football"),
            "tournament_slug": tournament.get("slug", ""),
            "tournament_id":   (tournament.get("uniqueTournament", {}) or {}).get("id"),
            "status": event.get("status", {}) or {},
            "_raw": event,
        }
    except Exception as e:
        print(f"[TeamFixtures] Extract error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# STEP 4 — FILTER BY DATE (today only, Malawi local calendar day)
# ══════════════════════════════════════════════════════════════════

# Malawi (Africa/Blantyre) is UTC+2 year-round — no DST — so a fixed
# offset is enough; no zoneinfo/tzdata dependency needed on Termux.
MALAWI_TZ = timezone(timedelta(hours=2))


def filter_today(fixtures: list[dict], today: "datetime | None" = None) -> list[dict]:
    """Keeps only fixtures whose kickoff falls on today's Malawi (CAT,
    UTC+2) calendar date — the day starts at 00:00 Malawi time, which is
    22:00 UTC the previous day. This ONLY affects which pre-match
    fixture alerts get announced here; it has no bearing on already-live
    matches, which flow through sofascore.get_live_matches() on a
    completely separate path with no date filter at all — so this
    change can never cause an ongoing match's goal/half-time/red-card/
    full-time updates to be dropped.

    today= lets callers/tests pin the reference moment explicitly (as a
    tz-aware or naive-UTC datetime — either is normalised to Malawi time
    before taking .date())."""
    now = today or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today_date = now.astimezone(MALAWI_TZ).date()
    kept = [f for f in fixtures if f["kickoff_utc"].astimezone(MALAWI_TZ).date() == today_date]
    print(f"[TeamFixtures] {len(kept)}/{len(fixtures)} fixture(s) are scheduled for today (Malawi time)")
    return kept


# ══════════════════════════════════════════════════════════════════
# STEP 5 — CHECK TEAM INCLUSION
# ══════════════════════════════════════════════════════════════════

def check_team_inclusion(fixture: dict, master_teams: dict[str, dict] | None = None) -> bool:
    """True if at least one side (home or away) is a team we actually
    monitor. Cheap safety net — see module docstring for why this isn't
    just trusting step 2's fetch."""
    teams = master_teams if master_teams is not None else MASTER_TEAMS
    return fixture["home_team_id"] in teams or fixture["away_team_id"] in teams


# ══════════════════════════════════════════════════════════════════
# STEP 5b — CHECK LEAGUE TOGGLE (config.WATCHED_LEAGUES)
# ══════════════════════════════════════════════════════════════════

def check_league_enabled(fixture: dict, master_teams: dict[str, dict] | None = None) -> bool:
    """True if at least one watched side (home or away) belongs to a
    league that's switched ON in config.WATCHED_LEAGUES. Lets you dial
    down a busy day (e.g. a pre-season friendly pile-up across a dozen
    leagues) without touching the master team list itself — see the
    config.py comment for the exact ON/OFF semantics (OR across sides,
    unlisted leagues default ON)."""
    teams = master_teams if master_teams is not None else MASTER_TEAMS
    for team_id in (fixture["home_team_id"], fixture["away_team_id"]):
        team = teams.get(team_id)
        if not team:
            continue
        league = team["league"]
        if config.WATCHED_LEAGUES.get(league, config.WATCHED_LEAGUES_DEFAULT):
            return True
    return False


# ══════════════════════════════════════════════════════════════════
# STEP 5c — CHECK FAVOURITES (config.FAVORITES_MODE)
# ══════════════════════════════════════════════════════════════════

def check_favorites(fixture: dict) -> bool:
    """No-op (always True) when config.FAVORITES_MODE is off. When on,
    at least one side (home or away) must also be listed in
    data/favorites.json — narrows the leagues already kept by
    check_league_enabled down to favourite clubs only. Applied AFTER
    the league toggle, never instead of it: a league switched OFF still
    stays off even for a favourite team."""
    if not config.FAVORITES_MODE:
        return True
    return (fixture["home_team_id"] in config.FAVORITE_TEAM_IDS
            or fixture["away_team_id"] in config.FAVORITE_TEAM_IDS)


# ══════════════════════════════════════════════════════════════════
# STEP 6 — DEDUPLICATE (state file, keyed by event id)
# ══════════════════════════════════════════════════════════════════

def _load_state() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("posted_event_ids", []))
    except Exception as e:
        print(f"[TeamFixtures] State load error: {e}")
        return set()


def _save_state(posted_ids: set[str]):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"posted_event_ids": sorted(posted_ids)}, f)
    except Exception as e:
        print(f"[TeamFixtures] State save error: {e}")


def dedupe(fixtures: list[dict]) -> tuple[list[dict], set[str]]:
    """Splits fixtures into (new, already_posted_ids) using the
    persisted state file — same 'same event ID = skip' rule bot.py's
    STATE_FILE already follows for live events."""
    posted = _load_state()
    new = [f for f in fixtures if str(f["event_id"]) not in posted]
    skipped = len(fixtures) - len(new)
    if skipped:
        print(f"[TeamFixtures] Skipping {skipped} fixture(s) already alerted")
    return new, posted


# ══════════════════════════════════════════════════════════════════
# STEP 7 — PUBLISH
# ══════════════════════════════════════════════════════════════════

def _to_match_dict(fixture: dict) -> dict:
    """Reshapes an extracted fixture into the same normalised match dict
    shape sofascore._normalize_sofascore() produces, so this fixture can
    flow straight into bot.process_match() once it goes live — no
    separate formatting path needed for goals/half-time/red cards/full-
    time/extra time/penalties; the existing live pipeline already knows
    how to post all of those for a "sofascore_<id>" match id."""
    norm_status, went_to_et, went_to_penalties = _norm_status(fixture["status"])
    home = fixture["_raw"].get("homeTeam", {}) or {}
    away = fixture["_raw"].get("awayTeam", {}) or {}

    return {
        "id":                 f"sofascore_{fixture['event_id']}",
        "_raw_id":            str(fixture["event_id"]),
        "_league_slug":       f"sofascore:{fixture['tournament_slug']}",
        "utcDate":            fixture["kickoff_utc"].isoformat(),
        "status":             norm_status,
        "_minute":            "",
        "_source":            "sofascore",
        "_comp_name":         fixture["tournament_name"],
        "_comp_flag":         _comp_flag(fixture["tournament_name"]),
        "_is_intl":           False,
        "_tier":              "priority",   # a watched team is never capped/downgraded
        "_full_time_only":    False,
        "var_events":         [],
        "_went_to_et":        went_to_et,
        "_went_to_penalties": went_to_penalties,
        "_penalty_home":      None,
        "_penalty_away":      None,
        "homeTeam": {
            "id":        fixture["home_team_id"],
            "name":      fixture["home_name"],
            "shortName": home.get("shortName", fixture["home_name"]),
            "crest":     _team_crest_url(fixture["home_team_id"]),
        },
        "awayTeam": {
            "id":        fixture["away_team_id"],
            "name":      fixture["away_name"],
            "shortName": away.get("shortName", fixture["away_name"]),
            "crest":     _team_crest_url(fixture["away_team_id"]),
        },
        "score": {
            "halfTime": {"home": None, "away": None},
            "fullTime": {"home": None, "away": None},
        },
        "goals":    [],
        "bookings": [],
        "lineups":  [],
    }


def format_alert(fixture: dict) -> str:
    """Formats the pre-match fixture alert: competition + kickoff time.
    Goals/half-time/red card/full-time/extra-time/penalties aren't known
    yet at fixture-announcement time — those post separately, live, once
    the match reaches SofaScore's in-play feed (see _to_match_dict)."""
    flag = _comp_flag(fixture["tournament_name"])
    ko_local = fixture["kickoff_utc"].strftime("%H:%M UTC")
    watched = []
    if fixture["home_team_id"] in MASTER_TEAMS:
        watched.append(fixture["home_name"])
    if fixture["away_team_id"] in MASTER_TEAMS:
        watched.append(fixture["away_name"])

    lines = [
        f"📅 {flag} {fixture['tournament_name']}",
        f"{fixture['home_name']} vs {fixture['away_name']}",
        f"🕒 Kick-off: {ko_local}",
    ]
    lines.append("#MatchCornaLive")
    return "\n".join(lines)


def publish(fixture: dict) -> bool:
    """Does NOT post an individual Facebook alert for this fixture —
    that used to happen here, one post per watched-team fixture, which
    is redundant with bot.py's maybe_post_preview() (one compiled post
    covering every match of the day, posted once at DAILY_PREVIEW_HOUR)
    and, worse, means a cold start with an empty team_fixtures_state.json
    fires off a Facebook post for every single fixture found in the same
    tick — a multi-post-per-minute spam burst.
    Still returns True: this fixture still needs to be marked "seen" in
    the state file (see dedupe()) and still needs to flow into
    _to_match_dict() so goals/kickoff/red cards for it are tracked live
    once it kicks off — only the pre-match Facebook alert is skipped."""
    return True


# ══════════════════════════════════════════════════════════════════
# ORCHESTRATION
# ══════════════════════════════════════════════════════════════════

def run() -> list[dict]:
    """Runs the full 7-step pipeline once. Returns the list of match
    dicts (see _to_match_dict) that were newly published this run, so a
    caller (e.g. bot.py's main loop) can fold them straight into its own
    live-tracking pass if desired."""
    raw_events = fetch_fixtures()
    extracted = [d for d in (extract_details(e) for e in raw_events) if d]
    today_fixtures = filter_today(extracted)
    included = [f for f in today_fixtures if check_team_inclusion(f)]
    league_ok = [f for f in included if check_league_enabled(f)]
    if len(league_ok) != len(included):
        print(f"[TeamFixtures] {len(league_ok)}/{len(included)} fixture(s) kept after league toggles "
              f"({len(included) - len(league_ok)} skipped — league switched OFF in config.py)")
    favorites_ok = [f for f in league_ok if check_favorites(f)]
    if config.FAVORITES_MODE and len(favorites_ok) != len(league_ok):
        print(f"[TeamFixtures] {len(favorites_ok)}/{len(league_ok)} fixture(s) kept after favourites filter "
              f"({len(league_ok) - len(favorites_ok)} skipped — FAVORITES=on and non-favourite club)")
    new_fixtures, posted = dedupe(favorites_ok)

    published_matches = []
    for fixture in new_fixtures:
        if publish(fixture):
            posted.add(str(fixture["event_id"]))
            published_matches.append(_to_match_dict(fixture))

    _save_state(posted)
    print(f"[TeamFixtures] Published {len(published_matches)} new fixture alert(s)")
    return published_matches


if __name__ == "__main__":
    run()
