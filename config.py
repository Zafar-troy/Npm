"""
config.py — ScoreLine Live configuration
All settings come from environment variables so Railway deployment is clean.
A local .env file is loaded automatically when present (for development).
"""
import os
import json

# Load .env if present (dev only — Railway uses real env vars)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Bot mode ──────────────────────────────────────────────────────
# BOT_MODE=developer  -> everything runs exactly as normal (fetching,
#   tracking, console logs) but NOTHING actually posts to Facebook —
#   poster.py prints what WOULD have been posted instead. Lets you
#   keep FB_PAGE_ID/FB_PAGE_ACCESS_TOKEN in .env permanently and just
#   flip this one line, instead of deleting/re-adding credentials
#   every time you want to test safely.
# BOT_MODE=active (or unset) -> posts for real. This is also the
#   fail-safe default direction: anything other than exactly "active"
#   (a typo, an empty value, etc.) is treated as developer mode, so a
#   mistake here can never accidentally go live.
BOT_MODE  = os.getenv("BOT_MODE", "active").strip().lower()
DEV_MODE  = BOT_MODE != "active"

# ── Facebook ──────────────────────────────────────────────────────
FB_PAGE_ID           = os.getenv("FB_PAGE_ID", "")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN", "")

# ── What to post ──────────────────────────────────────────────────
POST_LINEUPS        = os.getenv("POST_LINEUPS",        "true").lower() == "true"
POST_KICKOFF        = os.getenv("POST_KICKOFF",        "true").lower() == "true"
POST_GOALS          = os.getenv("POST_GOALS",          "true").lower() == "true"
POST_HALFTIME       = os.getenv("POST_HALFTIME",       "true").lower() == "true"
POST_RED_CARDS      = os.getenv("POST_RED_CARDS",      "true").lower() == "true"
POST_FULLTIME       = os.getenv("POST_FULLTIME",       "true").lower() == "true"
POST_DAILY_PREVIEW  = os.getenv("POST_DAILY_PREVIEW",  "false").lower() == "false"

# Man of the Match — neither data source carries player ratings, so this
# currently always posts nothing (kept as a flag for a future source).
POST_MOTM            = os.getenv("POST_MOTM",            "true").lower() == "true"

# Hour (UTC) to post the morning fixture list
DAILY_PREVIEW_HOUR  = int(os.getenv("DAILY_PREVIEW_HOUR", "9"))

# ── Lineups ───────────────────────────────────────────────────────
# Lineups are fetched once a match gets within this many minutes of
# kickoff — posting too early just wastes a fetch that always comes
# back empty. process_match() re-checks every tick until either
# lineups are found or the match kicks off.
LINEUP_LEAD_MINUTES  = int(os.getenv("LINEUP_LEAD_MINUTES", "55"))

# ── Polling ───────────────────────────────────────────────────────
POLL_INTERVAL       = int(os.getenv("POLL_INTERVAL", "60"))

# ── Anti-spam ─────────────────────────────────────────────────────
MIN_POST_GAP        = int(os.getenv("MIN_POST_GAP",        "20"))
MAX_POSTS_PER_HOUR  = int(os.getenv("MAX_POSTS_PER_HOUR",  "25"))
# "Fresh-or-Trash" cutoff. If a kickoff/goal/red-card event is still
# unposted (e.g. stuck behind MAX_POSTS_PER_HOUR, or the feed just
# returned it late) by the time it's older than this, it's dropped
# entirely rather than posted late — a "Kickoff" or "Goal" post showing
# up minutes after it actually happened, possibly AFTER a later event
# from the same match already posted, looks broken to followers even
# though nothing was technically lost. Dropping stale events immediately
# also frees up rate-limit/queue capacity for genuinely live events
# instead of burning it retrying news that's no longer useful.
# NOTE: if MAX_EVENT_AGE_MINUTES is set in your .env (or Railway/host
# env vars), that value wins over the fallback below — editing this
# line alone won't change anything if it's set elsewhere. Check your
# actual .env file if you're trying to tune this.
MAX_EVENT_AGE_MINUTES = int(os.getenv("MAX_EVENT_AGE_MINUTES", "8"))

# ── Railway keep-alive ────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8080"))

# ── Persistent data directory ────────────────────────────────────────
# Railway rebuilds a fresh container on every deploy, which wipes any
# file written to the default local path (like state.json). Point this
# at a mounted Railway Volume (e.g. "/data") to survive redeploys —
# otherwise the bot has no memory of what it already posted and will
# repost recent matches/goals/news after every update.
DATA_DIR = os.getenv("DATA_DIR", ".")

# ── VAR / disallowed goals ────────────────────────────────────────────
POST_VAR_DISALLOWED  = os.getenv("POST_VAR_DISALLOWED", "true").lower() == "true"

# ── Watched-team fixture alerts (team_fixtures.py) ───────────────────
# Queries SofaScore's "next events" endpoint for every team in
# data/teams_master.json (~200+ teams) — too heavy to run every
# POLL_INTERVAL tick, so it runs on its own slower interval instead.
# Once a watched team's match goes live, the normal live pipeline
# (scraper.py / sofascore.py) picks up goals/cards/half-time/full-time
# on its own — this only handles the pre-match "fixture today" alert.
POST_TEAM_FIXTURES           = os.getenv("POST_TEAM_FIXTURES", "true").lower() == "true"
TEAM_FIXTURES_INTERVAL_MINUTES = int(os.getenv("TEAM_FIXTURES_INTERVAL_MINUTES", "180"))

# ── Per-league on/off switches (team_fixtures.py) ────────────────────
# Every fixture alert produced from data/teams_master.json is tagged
# with the league it was found under (see team_fixtures.load_master_list
# -> each team's "league" field, which is literally the top-level key in
# teams_master.json — "Premier League", "La Liga", etc). This lets you
# cut the daily fixture-alert volume down to just the leagues you care
# about on a busy day (e.g. a pre-season friendly pile-up), without
# touching the master team list itself.
#
# Turn a league OFF here and:
#   - its watched teams' fixtures won't get a pre-match "today" alert
#   - BUT if one of those teams' matches kicks off anyway, it still
#     flows through the normal live pipeline (scraper.py/sofascore.py)
#     and posts goals/cards/half-time/full-time as usual — this toggle
#     ONLY silences the pre-match fixture-list alert, nothing live.
# If a fixture has one watched team in an ON league and the other in an
# OFF league (e.g. an interleague friendly), it's posted — ON wins over
# OFF whenever either side qualifies. A league name not listed below
# defaults to ON (fails open, so nothing silently vanishes just because
# you forgot to add a new league key here).
#
# Each can also be flipped via env var (e.g. LEAGUE_LA_LIGA=false in
# .env/Railway) without editing this file — the hardcoded value below
# is just the fallback when no env var is set.
#
# leagues.json (repo root, optional) takes priority over BOTH of the
# above for any league key it lists — this is the file meant to be
# hand-edited directly on GitHub (web editor or the mobile app) and
# pushed. Railway's GitHub auto-deploy then picks up the new choices
# on its own, no need to touch Railway's env var UI at all. A league
# left out of leagues.json still falls back to its env var / default,
# so a partial file (or no file) is always safe.
def _load_leagues_json() -> dict | None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leagues.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        print(f"[CONFIG] Using leagues.json for league on/off choices ({len(data)} league(s) listed)")
        return data
    except Exception as e:
        print(f"[CONFIG] ⚠️  Could not read leagues.json ({e}) — falling back to LEAGUE_* env vars")
        return None


_LEAGUES_JSON = _load_leagues_json()


def _league_on(env_name: str, league_key: str, default: bool = True) -> bool:
    if _LEAGUES_JSON is not None and league_key in _LEAGUES_JSON:
        return bool(_LEAGUES_JSON[league_key])
    return os.getenv(env_name, str(default)).lower() == "true"


WATCHED_LEAGUES = {
    "Premier League":            _league_on("LEAGUE_PREMIER_LEAGUE", "Premier League"),
    "La Liga":                   _league_on("LEAGUE_LA_LIGA", "La Liga"),
    "Serie A":                   _league_on("LEAGUE_SERIE_A", "Serie A"),
    "Bundesliga":                _league_on("LEAGUE_BUNDESLIGA", "Bundesliga"),
    "Ligue 1":                   _league_on("LEAGUE_LIGUE_1", "Ligue 1"),
    "Eredivisie":                _league_on("LEAGUE_EREDIVISIE", "Eredivisie"),
    "MLS":                       _league_on("LEAGUE_MLS", "MLS"),
    "Brazil Série A":            _league_on("LEAGUE_BRAZIL_SERIE_A", "Brazil Série A"),
    "Saudi Pro League":          _league_on("LEAGUE_SAUDI_PRO_LEAGUE", "Saudi Pro League"),
    "South African Premiership": _league_on("LEAGUE_SOUTH_AFRICAN_PREMIERSHIP", "South African Premiership"),
    "Malawi Super League":       _league_on("LEAGUE_MALAWI_SUPER_LEAGUE", "Malawi Super League"),
    "Süper Lig":                 _league_on("LEAGUE_SUPER_LIG", "Süper Lig"),
    "Liga MX":                   _league_on("LEAGUE_LIGA_MX", "Liga MX"),
    "Championship":              _league_on("LEAGUE_CHAMPIONSHIP", "Championship"),
}

# Default for any league key found in teams_master.json but NOT listed
# above (e.g. you add a new league to the master list later and forget
# to add its toggle here) — True keeps behaviour "fail open" instead of
# silently dropping a league you never explicitly turned off.
WATCHED_LEAGUES_DEFAULT = _league_on("LEAGUE_DEFAULT", "__default__", True)

# ── Favorites (busy-day filter) ───────────────────────────────────────
# FAVORITES=on/true -> within a league that's still ON in WATCHED_LEAGUES
# above, ONLY matches involving a club listed in data/favorites.json get
# posted (the other, non-favourite clubs in that league are skipped that
# day). A league already switched OFF in WATCHED_LEAGUES stays fully off
# regardless of this flag — league toggle is always checked first,
# favourites only narrows further within a league that's already ON. If
# a favourite team is playing a non-favourite team, the match still
# posts (only ONE side needs to be a favourite). FAVORITES=off (default)
# changes nothing — behaves exactly as before this flag existed.
FAVORITES_MODE = os.getenv("FAVORITES", "off").strip().lower() in ("on", "true", "1", "yes")


def _load_favorite_team_ids() -> set[str]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "favorites.json")
    try:
        with open(path, encoding="utf-8") as f:
            by_league = json.load(f)
        return {str(team_id) for teams in by_league.values() for team_id in teams.keys()}
    except Exception as e:
        print(f"[CONFIG] ⚠️  Could not read data/favorites.json ({e}) — FAVORITES filter will match nothing")
        return set()


FAVORITE_TEAM_IDS = _load_favorite_team_ids()

