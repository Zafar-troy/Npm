"""
bot.py — Match Corna Live main bot
==================================
Events posted:
  1. 📋 Lineup confirmed (~LINEUP_LEAD_MINUTES before kickoff)
  2. 📌 Kick-off
  3. ⚽ Goal (with score, scorer, and assist when available)
  4. 🟥 Red card
  5. ⏸️  Half time (current score + scorers/assists)
  6. ⏱️  Extra time start
  7. 🏁  Full time (includes AET / penalty result)
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import config
import scraper
import poster
import graphics
import team_fixtures
import sofascore

# ══════════════════════════════════════════════════════════════════
# RAILWAY KEEP-ALIVE SERVER
# ══════════════════════════════════════════════════════════════════

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Match Corna Live is running OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def _start_keepalive():
    server = HTTPServer(("0.0.0.0", config.PORT), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[KEEPALIVE] HTTP server running on port {config.PORT}")


# ══════════════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════════════

STATE_FILE = os.path.join(config.DATA_DIR, "state_v2.json")
os.makedirs(config.DATA_DIR, exist_ok=True)

_events:            dict[str, float] = {}
_last_preview_date: str              = ""
_post_timestamps:   list[float]      = []    # rolling timestamps for rate limiting
_last_post_time:    float            = 0.0   # for MIN_POST_GAP enforcement


def _load_state():
    global _events, _last_preview_date, _pending_stale
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
        _events                 = raw.get("events", {})
        _last_preview_date      = raw.get("last_preview_date", "")
        # Restores each event's original "first seen stale" wall-clock
        # time, so a redeploy/restart mid-retry-window doesn't reset the
        # clock and grant it a fresh STALE_RETRY_WINDOW_MINUTES it
        # shouldn't get (previously in-memory only — see bug report).
        _pending_stale.update(raw.get("pending_stale", {}))
        # Restores sofascore.py's vanished-match tracking (last-seen-live
        # snapshot + which ids already got a synthesized full-time) so a
        # match that vanishes from the live feed right around a
        # redeploy/restart still gets its FT + sealed goals posted
        # instead of the tracking silently resetting to empty.
        sofascore.import_vanish_state(raw.get("vanish_state", {}))
        print(f"[STATE] Loaded {len(_events)} posted events from disk"
              + (f", {len(_pending_stale)} pending-stale retries in progress" if _pending_stale else ""))
    except Exception as e:
        print(f"[STATE] ⚠️  Could not load state: {e}")


def _save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "events":                _events,
                "last_preview_date":     _last_preview_date,
                "pending_stale":         _pending_stale,
                "vanish_state":          sofascore.export_vanish_state(),
            }, f)
    except Exception as e:
        print(f"[STATE] ⚠️  Could not save state: {e}")


def _cleanup_state():
    global _events
    cutoff  = time.time() - 86400
    before  = len(_events)
    _events = {k: v for k, v in _events.items() if v > cutoff}
    removed = before - len(_events)
    if removed:
        print(f"[STATE] Cleaned up {removed} old events")


# ══════════════════════════════════════════════════════════════════
# DUPLICATE DETECTION
# ══════════════════════════════════════════════════════════════════

def _already_posted(key: str) -> bool:
    return key in _events


def _mark_posted(key: str):
    _events[key] = time.time()
    _save_state()


def _rate_limit_ok() -> bool:
    """Return True if it is safe to post now (gap + hourly cap)."""
    global _post_timestamps, _last_post_time
    now = time.time()
    # Enforce minimum gap between posts
    if now - _last_post_time < config.MIN_POST_GAP:
        wait = int(config.MIN_POST_GAP - (now - _last_post_time))
        print(f"[BOT] ⏳ Rate limit: waiting {wait}s (MIN_POST_GAP)")
        time.sleep(wait)
    # Enforce hourly cap
    hour_ago = now - 3600
    _post_timestamps = [t for t in _post_timestamps if t > hour_ago]
    if len(_post_timestamps) >= config.MAX_POSTS_PER_HOUR:
        print(f"[BOT] ⚠️  MAX_POSTS_PER_HOUR ({config.MAX_POSTS_PER_HOUR}) reached — skipping post")
        return False
    return True


def _post_if_new(key: str, message: str, image_path: str | None = None) -> bool:
    global _post_timestamps, _last_post_time
    if _already_posted(key):
        return False
    if not message:
        return False
    if not _rate_limit_ok():
        return False
    ok = poster.post_photo(image_path, caption=message) if image_path else poster.post(message)
    if not ok:
        print(f"[BOT] ⚠️  Post failed — retrying in 10s...")
        time.sleep(10)
        ok = poster.post_photo(image_path, caption=message) if image_path else poster.post(message)
    if ok:
        _mark_posted(key)
        _post_timestamps.append(time.time())
        _last_post_time = time.time()
    return ok


def _current_score(match: dict) -> tuple[int, int]:
    """The match's current full-time score, defaulting unknown values to
    0. NOT the same thing as _current_goal_score below — this is "the
    scoreboard right now" (used for red card/half time/extra time/full
    time cards, where the score is just the score), while
    _current_goal_score is "the score AT THE MOMENT one specific goal
    was scored" (used for the goal card itself). Deliberately None-safe:
    match["score"]["fullTime"]["home"/"away"] is always present as a
    KEY but is None until a match actually kicks off, so a plain
    `.get("home", 0)` does NOT catch that — dict.get's default only
    fires when the key is missing entirely, not when it's present and
    None. Using `.get("home", 0)` directly (as several call sites here
    used to) silently produces None instead of 0, relying on callers to
    remember an `or 0` at every use site instead of getting a safe value
    once, here."""
    h = match["score"]["fullTime"].get("home")
    a = match["score"]["fullTime"].get("away")
    return (h if h is not None else 0, a if a is not None else 0)


def _goal_identity_key(g: dict) -> tuple:
    """Stable identity for matching one goal dict against another WITHOUT
    relying on Python object identity (`g is goal`) — a goal can end up
    copied into a new list (e.g. filtered/sorted for the ordering logic
    below) between when it's fetched and when we're comparing it here,
    at which point `is` silently stops matching anything and callers
    fall through to the wrong fallback. Prefers the feed's own stable
    play id; falls back to (minute, isHome, scorer name, score
    snapshot) — the same fields _key_goal() already leans on for dedup,
    so two genuinely different goals essentially never collide here."""
    play_id = g.get("_play_id")
    if play_id:
        return ("pid", play_id)
    sc = g.get("score") or []
    score_tuple = tuple(sc) if sc else None
    return ("fallback", str(g.get("minute")), g.get("isHome"),
            (g.get("scorer") or {}).get("name"), score_tuple)


def _current_goal_score(match: dict, goal: dict) -> tuple:
    sc = goal.get("score", [])
    if sc and len(sc) == 2 and sc[0] is not None:
        return sc[0], sc[1]
    h_sc, a_sc = 0, 0
    target = _goal_identity_key(goal)
    for g in match.get("goals", []):
        if g["isHome"]:
            h_sc += 1
        else:
            a_sc += 1
        if _goal_identity_key(g) == target:
            break
    return h_sc, a_sc


def _goal_chronological_key(goal: dict) -> tuple:
    """Chronological ranking key: (has_score, total_score, minute).
    Goals with valid scores rank first (0), then by score total, then by minute.
    Goals missing scores rank last (1, 9999, 9999) so they cannot jump the posting queue.
    This ensures: 1) Goals with clear score snapshots post in order
                   2) Late goals with missing scores sort last (handled as post-enriched)"""
    minute_val = _parse_minute(goal.get("minute"))
    minute_val = minute_val if minute_val is not None else 9999
    
    sc = goal.get("score") or []
    if sc and len(sc) == 2 and sc[0] is not None and sc[1] is not None:
        try:
            score_total = int(sc[0]) + int(sc[1])
            return (0, score_total, minute_val)  # Has score: posts first
        except (ValueError, TypeError):
            pass
    
    # Missing or invalid score: sort last so it can't jump earlier goals
    return (1, 9999, 9999)


def _goals_match_scoreboard(match: dict) -> tuple[bool, int, int, int, int]:
    """Sanity check before rendering a goal poster: does the number of
    goals in match['goals'] actually add up to the scoreboard? A feed
    update can land the incremented score a tick before the new goal's
    scorer/assist details show up in the goals array — rendering off
    the array in that state would show a scoreline one goal behind (or
    attribute the wrong scorer once it does land). Returns
    (ok, home_goals_in_array, away_goals_in_array, home_score, away_score).
    Unknown score (None) is treated as ok — nothing to check against."""
    score   = match.get("score", {}).get("fullTime", {})
    h_score = score.get("home")
    a_score = score.get("away")
    goals   = match.get("goals", [])
    h_goals = sum(1 for g in goals if g.get("isHome"))
    a_goals = len(goals) - h_goals
    if h_score is None or a_score is None:
        return True, h_goals, a_goals, h_score, a_score
    return (h_goals == h_score and a_goals == a_score), h_goals, a_goals, h_score, a_score


def _safe_image(builder, *args, **kwargs) -> str | None:
    """Runs a graphics.py card builder; returns None (falls back to
    text-only post) on any failure rather than blocking the real post."""
    try:
        return builder(*args, **kwargs)
    except Exception as e:
        print(f"[GRAPHICS] ⚠️  Card generation failed, posting text-only: {e}")
        return None


def _minutes_until_kickoff(match: dict) -> float | None:
    """Minutes remaining until kickoff, or None if utcDate is missing/
    unparseable. Negative once the match has actually kicked off."""
    utc_str = match.get("utcDate", "")
    if not utc_str:
        return None
    try:
        ko = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return (ko - datetime.now(timezone.utc)).total_seconds() / 60
    except Exception:
        return None


def _parse_minute(minute_str) -> int | None:
    """Parses a match-minute string into a plain integer, handling
    stoppage time formats like '45+2' or '90+5' (-> 47, 95). Returns
    None if unparseable (e.g. '?') — callers should treat that as
    "can't tell, don't block the post" rather than as stale."""
    if minute_str is None:
        return None
    m = re.match(r"(\d+)(?:\+(\d+))?", str(minute_str).strip())
    if not m:
        return None
    base = int(m.group(1))
    added = int(m.group(2)) if m.group(2) else 0
    return base + added


def _current_match_minute(match: dict) -> int | None:
    """The provider's own live match-clock minute (e.g. '63'' -> 63),
    parsed from match['_minute']. This is the actual played time, so —
    unlike wall-clock-since-scheduled-kickoff — it is NOT thrown off by
    a delayed real-world kick-off (SofaScore's clock only starts once
    the ball is actually rolling) and it does not advance during
    half-time / any other in-match pause (the feed reports a
    non-numeric status like 'HT' then, which parses to None). Returns
    None if the feed doesn't give us a numeric minute right now."""
    return _parse_minute(match.get("_minute"))


def _kickoff_age_minutes(match: dict) -> float | None:
    """How long this match has actually been playing, used to gate a
    late 'Kick-off!' post. Deliberately based on the LIVE match-clock
    minute rather than wall-clock time since the scheduled utcDate —
    real kick-offs are routinely delayed 15-30min+, which used to make
    a brand-new match look "stale" the instant it went IN_PLAY. Using
    the provider's own played-time clock instead means a delayed
    kick-off or a half-time pause no longer inflates this number."""
    return _current_match_minute(match)


def _is_stale(age_minutes: float | None) -> bool:
    """True only on a POSITIVE signal that an event is too old to post.
    Unknown age (None) is never treated as stale — don't guess."""
    return age_minutes is not None and age_minutes > config.MAX_EVENT_AGE_MINUTES


# In-memory only (doesn't need to survive a restart) — tracks, using our
# OWN wall clock, the first time we saw a given event look "stale". This
# is what makes retrying actually work. age_minutes for a goal/red card
# is (current live minute - event minute): most of the time this is
# genuine feed lag, which clears itself within a poll or two, but a
# stuck/broken feed can also hold a match's live minute frozen, in
# which case the gap won't self-correct either. If we don't track
# first-seen time ourselves and just re-check _is_stale() every tick,
# a genuinely stuck case would loop forever — "retry next tick" never
# gives up and never posts. Bounding the retry to real elapsed minutes
# (via time.time(), not the recomputed match-age) is what lets us actually
# give up after a fair chance instead of looping forever.
_pending_stale: dict[str, float] = {}
STALE_RETRY_WINDOW_MINUTES = 3  # keep retrying a stale-looking event for this many REAL minutes before giving up for good

# In-memory only (fine to lose on restart — worst case is one extra
# fetch right after coming back up). Tracks the last time we actually
# HIT the lineup endpoint for a given match, so we're not making an HTTP
# call on every single POLL_INTERVAL tick for the whole LINEUP_LEAD_MINUTES
# window while waiting for a lineup to be published — that's dozens of
# wasted requests per match on a short poll interval. Retried on its own
# cadence (LINEUP_RETRY_MINUTES) instead, independent of POLL_INTERVAL.
_last_lineup_attempt: dict[str, float] = {}
LINEUP_RETRY_MINUTES = 5  # minimum real-clock gap between lineup fetch attempts for the same match

# Same shape/purpose as _pending_stale above, but for the goal-count-vs-
# scoreboard sanity check (_goals_match_scoreboard): first real-clock
# time we saw THIS match's goal array disagree with its scoreboard.
# Without a give-up, a mismatch that never resolves (e.g. SofaScore
# simply never exposes the incident for one particular goal — an own
# goal, say) would block EVERY future goal in that match forever, since
# the check gates the whole goals list, not just the newest entry.
_pending_goal_mismatch: dict[str, float] = {}
GOAL_MISMATCH_RETRY_WINDOW_MINUTES = 5  # keep waiting for the feed to catch up this many REAL minutes before posting anyway


def _handle_stale(key: str, age_minutes: float | None, label: str) -> bool:
    """Returns True if the caller should skip this event for now (continue).
    Call this INSTEAD of a bare `if _is_stale(...)` check, then `continue`
    if it returns True. On first sight of a stale-looking event, marks it
    pending and waits — the raw data may just be delayed (e.g. a
    rate-limited/cached feed response) and could correct itself on a
    later poll. If it's still stale after STALE_RETRY_WINDOW_MINUTES of
    real time, gives up and marks it posted so it stops being
    reprocessed/relogged every tick forever."""
    if not _is_stale(age_minutes):
        _pending_stale.pop(key, None)
        return False

    now = time.time()
    first_seen = _pending_stale.get(key)
    if first_seen is None:
        _pending_stale[key] = now
        print(f"[BOT] ⏭️  Temporarily skipping (age ~{int(age_minutes)}min, may be delayed by "
              f"rate limiting) — will retry for up to {STALE_RETRY_WINDOW_MINUTES}min: {label}")
        return True

    if now - first_seen < STALE_RETRY_WINDOW_MINUTES * 60:
        return True  # still inside the retry window — stay quiet, try again next tick

    print(f"[BOT] ⏭️  Giving up on stale event after {STALE_RETRY_WINDOW_MINUTES}min of retries: {label}")
    _mark_posted(key)
    _pending_stale.pop(key, None)
    return True


def _handle_goal_mismatch(mid: str, goals_ok: bool, h_goals: int, a_goals: int,
                           h_score, a_score, label: str) -> bool:
    """Returns True if the caller should hold off processing this match's
    goals for now (continue past the goal loop). Mirrors _handle_stale:
    on first sight of a mismatch, waits (the incidents feed may just be a
    tick behind the scoreboard) and retries. If it's still mismatched
    after GOAL_MISMATCH_RETRY_WINDOW_MINUTES of real time, gives up
    waiting and lets the caller process whatever goals ARE in the array
    — better to post a possibly-incomplete goal list than to silently
    withhold every goal in the match forever over one persistent
    discrepancy."""
    if goals_ok:
        _pending_goal_mismatch.pop(mid, None)
        return False

    now = time.time()
    first_seen = _pending_goal_mismatch.get(mid)
    if first_seen is None:
        _pending_goal_mismatch[mid] = now
        print(f"[BOT] ⏳ Goal count doesn't match scoreboard yet "
              f"({h_goals}-{a_goals} in feed vs {h_score}-{a_score} on scoreboard) — "
              f"waiting up to {GOAL_MISMATCH_RETRY_WINDOW_MINUTES}min: {label}")
        return True

    if now - first_seen < GOAL_MISMATCH_RETRY_WINDOW_MINUTES * 60:
        return True  # still inside the retry window — stay quiet, try again next tick

    print(f"[BOT] ⚠️  Goal count still doesn't match scoreboard after "
          f"{GOAL_MISMATCH_RETRY_WINDOW_MINUTES}min ({h_goals}-{a_goals} vs "
          f"{h_score}-{a_score}) — giving up waiting, posting what we have: {label}")
    return False


def _goal_event_age_minutes(match: dict, minute_str) -> float | None:
    """How many match-minutes behind the current live clock a goal/card
    is, i.e. (current live minute - event minute). Deliberately compares
    two points on the PROVIDER'S OWN match clock rather than wall-clock
    time since scheduled kick-off: a delayed real-world kick-off shifts
    both the "kickoff happened" wall-clock point and every event equally,
    so it used to add a flat, spurious offset to every event's age (see
    _kickoff_age_minutes). A half-time pause is even worse under the old
    method — wall-clock keeps ticking while the match doesn't, so an
    event from just before HT would keep getting "older" for the entire
    break. Using the live minute for both sides of the subtraction means
    neither a delayed kick-off nor a half-time/stoppage pause moves this
    number — it only reflects genuine feed lag (e.g. rate limiting)."""
    current_minute = _current_match_minute(match)
    event_minute = _parse_minute(minute_str)
    if current_minute is None or event_minute is None:
        return None
    return current_minute - event_minute


# ══════════════════════════════════════════════════════════════════
# EVENT KEYS
# ══════════════════════════════════════════════════════════════════

def _key_lineup(mid: str)               -> str: return f"lineup:{mid}"
def _key_kickoff(mid: str)              -> str: return f"kickoff:{mid}"
def _key_goal(mid: str, g: dict, idx: int = 0) -> str:
    """Primary deduplication key for one goal. Priority:
    1. Score snapshot [home-away] (most stable, never includes scorer name)
    2. Play ID (sofascore incident id or fallback id)
    3. Side+Minute (H|A:minute, base minute stripped of stoppage)
    NEVER includes scorer name in the primary key — scorer is unstable across feeds."""
    sc = g.get("score")
    if sc and len(sc) == 2 and sc[0] is not None and sc[1] is not None:
        try:
            home, away = int(sc[0]), int(sc[1])
            return f"goal:{mid}:score:{home}-{away}"
        except (ValueError, TypeError):
            pass
    
    play_id = g.get("_play_id")
    if play_id:
        return f"goal:{mid}:pid:{play_id}"
    
    minute = str(g.get('minute', '?')).strip()
    side = 'H' if g.get('isHome') else 'A'
    if minute not in ("?", "", "0") and minute:
        base_minute = re.match(r"(\d+)", minute).group(1) if re.match(r"(\d+)", minute) else minute
        return f"goal:{mid}:{side}:{base_minute}"
    
    return f"goal:{mid}:{side}:idx{idx}"


def _goal_alias_keys(mid: str, g: dict, idx: int = 0) -> set[str]:
    """ALL possible dedup keys for one goal (primary + all aliases).
    When a goal is posted or late-skipped, ALL these aliases are sealed in _events
    so no other alias can trigger a duplicate post later.
    Includes: score key, play_id key, side+minute keys, and legacy scorer keys."""
    keys = set()
    
    # Primary key
    keys.add(_key_goal(mid, g, idx))
    
    # Score-based key (if score available)
    sc = g.get("score")
    if sc and len(sc) == 2 and sc[0] is not None and sc[1] is not None:
        try:
            home, away = int(sc[0]), int(sc[1])
            keys.add(f"goal:{mid}:score:{home}-{away}")
        except (ValueError, TypeError):
            pass
    
    # Play ID key (if available)
    play_id = g.get("_play_id")
    if play_id:
        keys.add(f"goal:{mid}:pid:{play_id}")
    
    # Side+minute keys (all variations of minute parsing)
    side = 'H' if g.get('isHome') else 'A'
    minute_raw = str(g.get('minute', '?')).strip()
    if minute_raw and minute_raw not in ("?", "", "0"):
        # Base minute (stripped stoppage)
        base_minute = re.match(r"(\d+)", minute_raw).group(1) if re.match(r"(\d+)", minute_raw) else minute_raw
        keys.add(f"goal:{mid}:{side}:{base_minute}")
        keys.add(f"goal:{mid}:{base_minute}:{side}")  # alternate order
    
    # Legacy scorer|unknown keys (for backward compat with old posts)
    scorer_name = (g.get("scorer") or {}).get("name")
    for fallback in [scorer_name or "unknown", "unknown"]:
        if fallback and minute_raw not in ("?", "", "0"):
            base_minute = re.match(r"(\d+)", minute_raw).group(1) if re.match(r"(\d+)", minute_raw) else minute_raw
            keys.add(f"goal:{mid}:{fallback}:{base_minute}")
    
    return keys


def _goal_already_posted(mid: str, g: dict, idx: int = 0) -> bool:
    """Check if ANY alias key for this goal already exists in _events."""
    for key in _goal_alias_keys(mid, g, idx):
        if key in _events:
            return True
    return False


def _mark_goal_posted(mid: str, g: dict, idx: int = 0):
    """Seal ALL alias keys for this goal in _events when posting (live or late-skip).
    This prevents any other alias from triggering a duplicate later."""
    now = time.time()
    for key in _goal_alias_keys(mid, g, idx):
        if key not in _events:
            _events[key] = now
    _save_state()


def _key_redcard(mid: str, b: dict)     -> str:
    play_id = b.get("_play_id")
    if play_id:
        return f"redcard:{mid}:pid:{play_id}"
    return f"redcard:{mid}:{b.get('player', {}).get('name') or '?'}:{b.get('minute', '?')}"
def _key_halftime(mid: str)             -> str: return f"halftime:{mid}"
def _key_extratime(mid: str)            -> str: return f"extratime:{mid}"
def _key_fulltime(mid: str)             -> str: return f"ft:{mid}"
def _key_motm(mid: str)                 -> str: return f"motm:{mid}"
def _key_var(mid: str, v: dict)         -> str:
    play_id = v.get("_play_id")
    if play_id:
        return f"var:{mid}:pid:{play_id}"
    return f"var:{mid}:{v.get('minute','?')}:{v.get('player','?')}"


def _enrich_missing_scores(goals: list):
    """Pre-enrichment: walk goals in strict (minute, score_total) order to
    calculate missing [h, a] snapshots sequentially. Goals with missing
    scores get their snapshot filled in based on counting all goals up
    to that point chronologically.

    Tie-break by score_total (goals that already carry a known
    snapshot sort by it, unknown ones sort last) so goals sharing the
    same minute still resolve in a stable, score-consistent order
    instead of whatever order the feed happened to list them in.

    Never invents a candidate [h, a] snapshot that collides with a
    snapshot some other goal in this match already has (or was just
    assigned) — that would silently merge two different goals under
    the same dedup key downstream. When a collision would occur, the
    goal is left with score=[] (unscored) rather than given a wrong
    snapshot; it still gets a stable identity from its play id / minute
    for dedup purposes.

    Modifies goals in-place."""
    if not goals:
        return

    def _minute_key(g):
        m = _parse_minute(g.get("minute"))
        return m if m is not None else 9999

    def _score_total(g):
        sc = g.get("score")
        if sc and len(sc) == 2 and sc[0] is not None and sc[1] is not None:
            try:
                return int(sc[0]) + int(sc[1])
            except (ValueError, TypeError):
                pass
        return 9999  # unknown score sorts after known ones at the same minute

    # Sort by (minute, score_total) — strict chronological order for
    # counting, with known snapshots breaking ties at the same minute.
    sorted_goals = sorted(goals, key=lambda g: (_minute_key(g), _score_total(g)))

    # Seed the collision set with every snapshot a goal already
    # explicitly carries, so a computed candidate can never step on one
    # of those either.
    used_snapshots = set()
    for g in sorted_goals:
        sc = g.get("score")
        if sc and len(sc) == 2 and sc[0] is not None and sc[1] is not None:
            try:
                used_snapshots.add((int(sc[0]), int(sc[1])))
            except (ValueError, TypeError):
                pass

    h_count, a_count = 0, 0
    for g in sorted_goals:
        if g.get("_is_shootout"):
            continue  # don't count shootout kicks

        # If this goal already has a score snapshot, sync our counters to it
        sc = g.get("score")
        if sc and len(sc) == 2 and sc[0] is not None and sc[1] is not None:
            try:
                h_count = int(sc[0])
                a_count = int(sc[1])
            except (ValueError, TypeError):
                pass
            continue

        # Missing score: use current count and increment
        candidate = (h_count + 1, a_count) if g.get("isHome") else (h_count, a_count + 1)
        if candidate in used_snapshots:
            # Would invent a score key that collides with another goal's
            # snapshot — prefer leaving this one unscored over guessing
            # wrong and silently merging two different goals.
            g["score"] = []
            continue

        h_count, a_count = candidate
        g["score"] = [h_count, a_count]
        used_snapshots.add(candidate)


# ══════════════════════════════════════════════════════════════════
# DAILY FIXTURE PREVIEW
# ══════════════════════════════════════════════════════════════════

_last_team_fixtures_run: datetime | None = None


def maybe_run_team_fixtures() -> list:
    """Runs team_fixtures.run() at most once every
    config.TEAM_FIXTURES_INTERVAL_MINUTES — it hits SofaScore once per
    watched team (~200+ requests), so it can't run on every POLL_INTERVAL
    tick like the rest of the loop. Returns the newly-published fixtures
    (already normalized match dicts) so main() can fold them straight
    into this tick's active/process_match() pass — after that, they keep
    getting picked up on their own via scraper.get_todays_matches() once
    they go live (see the master-team-id bypass in sofascore.py)."""
    global _last_team_fixtures_run
    if not config.POST_TEAM_FIXTURES:
        return []
    now = datetime.now(timezone.utc)
    if (
        _last_team_fixtures_run is not None
        and (now - _last_team_fixtures_run).total_seconds() < config.TEAM_FIXTURES_INTERVAL_MINUTES * 60
    ):
        return []
    _last_team_fixtures_run = now
    try:
        return team_fixtures.run()
    except Exception as e:
        print(f"[BOT] ⚠️  team_fixtures.run() failed: {e}")
        return []


def maybe_post_preview(matches: list):
    global _last_preview_date
    if not config.POST_DAILY_PREVIEW:
        return
    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    # Fire any time within the preview hour (handles mid-hour restarts)
    if now.hour != config.DAILY_PREVIEW_HOUR or _last_preview_date == today:
        return
    print("[BOT] 📅 Posting daily fixture preview...")
    msg = poster.fmt_daily_preview(matches)
    if poster.post(msg):
        _last_preview_date = today
        _save_state()


# ══════════════════════════════════════════════════════════════════
# PROCESS ONE MATCH
# ══════════════════════════════════════════════════════════════════

def process_match(match: dict):
    mid    = match["id"]
    status = match["status"]
    hname  = match["homeTeam"]["name"]
    aname  = match["awayTeam"]["name"]
    # ── Lineups ───────────────────────────────────────────────────
    # Fetched from SofaScore, gated to fire only once a
    # match is within config.LINEUP_LEAD_MINUTES of kickoff (real-world
    # availability is ~60 min pre-kickoff) — this is what makes lineups
    # post "~1hr before kickoff for every game" instead of being
    # attempted (and failing) the moment a fixture is first seen hours
    # earlier. Still retried every tick inside the window until either
    # lineups are found or the match kicks off.
    if (config.POST_LINEUPS
            and status == "SCHEDULED"
            and not match.get("_full_time_only")
            and match.get("_league_slug")
            and not _already_posted(_key_lineup(mid))):
        mins_to_ko = _minutes_until_kickoff(match)
        if mins_to_ko is not None and mins_to_ko <= config.LINEUP_LEAD_MINUTES:
            now = time.time()
            last_attempt = _last_lineup_attempt.get(mid)
            if last_attempt is None or now - last_attempt >= LINEUP_RETRY_MINUTES * 60:
                _last_lineup_attempt[mid] = now
                lineups = scraper.get_lineup(match["_league_slug"], match.get("_raw_id", mid), hname, aname)
                if lineups:
                    match = {**match, "lineups": lineups}
                    print(f"[BOT] 📋 Lineups: {hname} vs {aname}")
                    _post_if_new(_key_lineup(mid), poster.fmt_lineup(match))

    # ── VAR / disallowed goals ───────────────────────────────────────
    if config.POST_VAR_DISALLOWED:
        for v in match.get("var_events", []):
            key = _key_var(mid, v)
            if not _already_posted(key):
                print(f"[BOT] 🚨 VAR disallowed: {v.get('player')} — {hname} vs {aname}")
                img = _safe_image(
                    graphics.render_card, "var", "❌",
                    f"No Goal — {hname} vs {aname}",
                    [f"{v.get('player','?')} ({poster._minute(v.get('minute','?'))}') — {v.get('reason','VAR Review')}"],
                )
                _post_if_new(key, poster.fmt_var_disallowed(match, v), image_path=img)

    # ── Kick-off ──────────────────────────────────────────────────
    if (config.POST_KICKOFF and status == "IN_PLAY"
            and not match.get("_full_time_only")
            and not _already_posted(_key_kickoff(mid))):
        if not _handle_stale(_key_kickoff(mid), _kickoff_age_minutes(match), f"kickoff — {hname} vs {aname}"):
            kickoff_match = {**match, "score": {
                "halfTime": {"home": None, "away": None},
                "fullTime": {"home": 0, "away": 0},
            }}
            print(f"[BOT] 📌 Kickoff: {hname} vs {aname}")
            img = _safe_image(
                graphics.render_scoreboard_card, "kickoff", hname, aname, 0, 0,
                competition=match.get("_comp_name", ""),
                status_label="KICK-OFF", show_pulse=True,
                home_crest_url=match["homeTeam"].get("crest", ""), away_crest_url=match["awayTeam"].get("crest", ""),
            )
            _post_if_new(_key_kickoff(mid), poster.fmt_kickoff(kickoff_match), image_path=img)

    # ── Goals (scorer + assist, when the feed provides one) ─────────
    # Ordering guarantee (see module-level goal helpers above): goals are
    # only ever turned into a LIVE card in chronological order. A goal
    # that arrives late — after a chronologically NEWER goal has already
    # been posted live — is never posted as a live card at all; it's
    # just marked posted (so it's never retried) and still shows up
    # normally in the half-time/full-time scorer lists, since those read
    # straight off match["goals"] rather than off what got posted live.
    # "Quiet is better than confusing" per the product requirement this
    # implements — a missing live card is far less jarring on the page
    # than the scoreline appearing to go backwards.
    if (config.POST_GOALS and not match.get("_full_time_only")
            and status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "SHOOTOUT", "FINISHED")):
        # Drop VAR-overturned/cancelled goals BEFORE the scoreboard-count
        # check below. SofaScore's incidents feed doesn't always retract
        # a goal once VAR disallows it — the entry just sits in
        # match["goals"] forever with its original score-at-the-time
        # snapshot, even though the real scoreboard reverted. That's
        # exactly what makes _goals_match_scoreboard() mismatch forever:
        # the feed count never comes back down to match. After 5min,
        # _handle_goal_mismatch used to give up and post the ENTIRE
        # array anyway — including the cancelled goal. A real goal's
        # own snapshot can only ever be <= the actual current score (it
        # only moves forward); a snapshot that's HIGHER than the real
        # scoreboard is proof positive that particular goal never
        # actually stood. Filtering it here means the mismatch resolves
        # immediately instead of waiting 5min and then posting it — and
        # it also keeps it out of the half-time/full-time scorer lines
        # below, which read off this same match["goals"].
        _ft = match.get("score", {}).get("fullTime", {})
        _true_h, _true_a = _ft.get("home"), _ft.get("away")
        if _true_h is not None and _true_a is not None:
            _clean_goals = []
            for g in match.get("goals", []):
                sc = g.get("score") or []
                if len(sc) == 2 and sc[0] is not None and sc[1] is not None:
                    try:
                        snap_h, snap_a = int(sc[0]), int(sc[1])
                        if snap_h > _true_h or snap_a > _true_a:
                            scorer_label = (g.get("scorer") or {}).get("name") or "Unknown scorer"
                            print(f"[BOT] 🚫 Dropping cancelled goal — {scorer_label} "
                                  f"{poster._minute(g.get('minute'))}' (snapshot {snap_h}-{snap_a} exceeds "
                                  f"actual scoreboard {_true_h}-{_true_a}, likely VAR-overturned) — {hname} vs {aname}")
                            continue
                    except (TypeError, ValueError):
                        pass
                _clean_goals.append(g)
            if len(_clean_goals) != len(match.get("goals", [])):
                match = {**match, "goals": _clean_goals}

        goals_ok, h_goals, a_goals, h_score, a_score = _goals_match_scoreboard(match)
        hold_off = _handle_goal_mismatch(
            mid, goals_ok, h_goals, a_goals, h_score, a_score, f"{hname} vs {aname}"
        )
        raw_goals = match.get("goals", []) if not hold_off else []

        # Never a live card, regardless of ordering: goals sofascore.py
        # flagged as penalty-shootout kicks (per-kick posts are handled
        # entirely by the one FT-penalties post instead — see poster.py/
        # graphics.py). Also, defensively, any goal seen while the match
        # is already in the SHOOTOUT status that has no real match-clock
        # minute at all — shootout kicks aren't clock-based, so a missing
        # minute at that stage is a strong signal it's a kick the feed
        # didn't explicitly tag as one.
        eligible = []
        for idx, g in enumerate(raw_goals):
            if g.get("_is_shootout"):
                continue
            if status == "SHOOTOUT" and _parse_minute(g.get("minute")) is None:
                continue
            eligible.append((idx, g))

        # PRE-ENRICHMENT: Before sorting/ranking, walk goals in minute-first order
        # to populate missing [h, a] snapshots sequentially so every goal gets
        # a stable score key for deduplication.
        goals_for_enrichment = [g for _, g in eligible]
        _enrich_missing_scores(goals_for_enrichment)

        # Chronological rank, independent of whatever order match["goals"]
        # happens to be in this tick (sofascore.py already sorts it, but
        # re-deriving the order here — rather than trusting list order —
        # is what makes the ordering gate correct even if that ever
        # changes upstream).
        ranked = sorted(eligible, key=lambda pair: _goal_chronological_key(pair[1]))
        ranked_keys = [(idx, g, _key_goal(mid, g, idx)) for idx, g in ranked]
        posted_flags = [_goal_already_posted(mid, g, idx) for idx, g, _ in ranked_keys]

        for rank_pos, (idx, goal, key) in enumerate(ranked_keys):
            if posted_flags[rank_pos]:
                continue

            # Rule: the match is already FINISHED — this goal only
            # showed up now (e.g. via the vanished-match re-fetch, or a
            # late incidents update landing after full time). It must
            # never become a "X' - LIVE" card at this point; just seal
            # it so it can't be retried. It still appears normally in
            # the full-time scorer list, which reads straight off
            # match["goals"] rather than off what got posted live.
            if status == "FINISHED":
                scorer_label = (goal.get("scorer") or {}).get("name") or "Unknown scorer"
                print(f"[BOT] ⏭️  Skipping live card for goal seen after FINISHED — "
                      f"{scorer_label} {poster._minute(goal.get('minute'))}' — {hname} vs {aname} "
                      f"— will still appear in the full-time scorer list")
                _mark_goal_posted(mid, goal, idx)
                posted_flags[rank_pos] = True
                continue

            # Rule: a NEWER goal (higher rank) already posted live means
            # this one arrived late — skip the live card for good. Mark all
            # aliases to prevent retries via any other key path.
            if any(posted_flags[rank_pos + 1:]):
                scorer_label = (goal.get("scorer") or {}).get("name") or "Unknown scorer"
                print(f"[BOT] ⏭️  Skipping late goal (a newer goal already posted) — "
                      f"{scorer_label} {poster._minute(goal.get('minute'))}' — {hname} vs {aname} "
                      f"— will still appear in HT/FT scorer lists")
                _mark_goal_posted(mid, goal, idx)  # seal ALL aliases; never becomes a live card
                posted_flags[rank_pos] = True
                continue

            # Rule: don't post THIS goal live until every earlier-ranked
            # goal has posted — holds off (retried next tick) rather than
            # posting out of order.
            if not all(posted_flags[:rank_pos]):
                continue

            scorer_label = (goal.get("scorer") or {}).get("name") or "Unknown scorer"
            if _handle_stale(key, _goal_event_age_minutes(match, goal.get("minute")), f"{scorer_label} — {hname} vs {aname}"):
                continue

            scorer = (goal.get("scorer") or {}).get("name")
            assist = goal.get("assist", {}).get("name")
            print(f"[BOT] ⚽ Goal: {scorer or '(no scorer data)'}" + (f" (assist: {assist})" if assist else "") + f" — {hname} vs {aname}")
            h_sc, a_sc = _current_goal_score(match, goal)
            scorer_short = poster._short_name(scorer) if scorer else None
            event_line = f"{scorer_short} {poster._minute(goal['minute'])}'" if scorer_short else f"{poster._minute(goal['minute'])}'"
            if assist:
                event_line += f"\n(assist: {poster._short_name(assist)})"
            # Scorer/assist text is drawn under the SCORING team's
            # own crest, not centered across the card — makes it
            # immediately clear whose goal this is at a glance.
            side_kwargs = {"home_event_line": event_line} if goal["isHome"] else {"away_event_line": event_line}
            img = _safe_image(
                graphics.render_scoreboard_card, "goal", hname, aname, h_sc, a_sc,
                competition=match.get("_comp_name", ""),
                status_label=f"{poster._minute(goal['minute'])}' - LIVE", show_pulse=True,
                home_crest_url=match["homeTeam"].get("crest", ""), away_crest_url=match["awayTeam"].get("crest", ""),
                **side_kwargs,
            )
            posted_ok = _post_if_new(key, poster.fmt_goal(match, goal), image_path=img)
            if posted_ok:
                _mark_goal_posted(mid, goal, idx)  # seal ALL aliases on live post
                posted_flags[rank_pos] = True
            time.sleep(2)

    # ── Red cards ─────────────────────────────────────────────────
    if (config.POST_RED_CARDS and not match.get("_full_time_only")
            and status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "SHOOTOUT", "FINISHED")):
        for booking in match.get("bookings", []):
            if booking.get("card") != "RED_CARD":
                continue
            key = _key_redcard(mid, booking)
            if _already_posted(key):
                continue
            rc_label = booking.get("player", {}).get("name") or "Unknown"
            if _handle_stale(key, _goal_event_age_minutes(match, booking.get("minute")), f"{rc_label} — {hname} vs {aname}"):
                continue
            player = booking.get("player", {}).get("name") or "Unknown"
            minute = poster._minute(booking.get("minute", "?"))
            print(f"[BOT] 🟥 Red card: {player} {minute}' — {hname} vs {aname}")
            h_sc, a_sc = _current_score(match)
            event_line = f"{poster._short_name(player)} {minute}'"
            side_kwargs = {"home_event_line": event_line} if booking.get("isHome") else {"away_event_line": event_line}
            img = _safe_image(
                graphics.render_scoreboard_card, "redcard", hname, aname, h_sc or 0, a_sc or 0,
                competition=match.get("_comp_name", ""),
                status_label=f"{minute}' - RED CARD", show_pulse=True,
                home_crest_url=match["homeTeam"].get("crest", ""), away_crest_url=match["awayTeam"].get("crest", ""),
                **side_kwargs,
            )
            _post_if_new(key, poster.fmt_redcard(match, booking), image_path=img)
            time.sleep(2)

    # ── Half time (current score + scorers/assists) ────────────────
    if (config.POST_HALFTIME and status == "PAUSED"
            and not match.get("_full_time_only")
            and not _already_posted(_key_halftime(mid))):
        print(f"[BOT] ⏸️  Half time: {hname} vs {aname}")
        h_sc, a_sc = _current_score(match)
        img = _safe_image(
            graphics.render_scoreboard_card, "halftime", hname, aname, h_sc or 0, a_sc or 0,
            competition=match.get("_comp_name", ""),
            status_label="HALF TIME", show_pulse=False,
            home_event_line=poster.scorers_line(match, side="home"),
            away_event_line=poster.scorers_line(match, side="away"),
            home_crest_url=match["homeTeam"].get("crest", ""), away_crest_url=match["awayTeam"].get("crest", ""),
        )
        _post_if_new(_key_halftime(mid), poster.fmt_halftime(match), image_path=img)

    # ── Extra time ────────────────────────────────────────────────
    # Guarded by _full_time_only like kickoff/goals/redcards/halftime —
    # secondary-tier matches only ever get the one final full-time post,
    # no intermediate updates. (This guard was missing before, so a
    # secondary match that went to extra time would get an extra-time
    # post despite the tier's whole point being "no spam, final score
    # only" — full time itself stays unguarded below, that's the one
    # post secondary matches ARE supposed to get.)
    if (status in ("EXTRA_TIME", "SHOOTOUT") or (
            status == "FINISHED" and match.get("_went_to_et"))) \
            and not match.get("_full_time_only"):
        if not _already_posted(_key_extratime(mid)):
            print(f"[BOT] ⏱️  Extra time: {hname} vs {aname}")
            h_sc, a_sc = _current_score(match)
            img = _safe_image(
                graphics.render_scoreboard_card, "extratime", hname, aname, h_sc or 0, a_sc or 0,
                competition=match.get("_comp_name", ""),
                status_label="EXTRA TIME", show_pulse=False,
                home_event_line=poster.scorers_line(match, side="home"),
                away_event_line=poster.scorers_line(match, side="away"),
                home_crest_url=match["homeTeam"].get("crest", ""), away_crest_url=match["awayTeam"].get("crest", ""),
            )
            _post_if_new(_key_extratime(mid), poster.fmt_extratime(match), image_path=img)

    # ── Full time ─────────────────────────────────────────────────
    if config.POST_FULLTIME and status == "FINISHED" and not _already_posted(_key_fulltime(mid)):
        if match.get("_went_to_penalties"):
            print(f"[BOT] 🏁 Full time (penalties): {hname} vs {aname}")
        elif match.get("_went_to_et"):
            print(f"[BOT] 🏁 Full time (AET): {hname} vs {aname}")
        else:
            print(f"[BOT] 🏁 Full time: {hname} vs {aname}")
        h_sc, a_sc = _current_score(match)
        status_label = "FULL TIME"
        if match.get("_went_to_penalties"):
            status_label = "FULL TIME - PENALTIES"
        elif match.get("_went_to_et"):
            status_label = "FULL TIME - AET"
        img = _safe_image(
            graphics.render_scoreboard_card, "fulltime", hname, aname, h_sc or 0, a_sc or 0,
            competition=match.get("_comp_name", ""),
            status_label=status_label, show_pulse=False,
            # Each team's scorers sit under their own crest instead of
            # one combined line down the center of the card.
            home_event_line=poster.scorers_line(match, side="home"),
            away_event_line=poster.scorers_line(match, side="away"),
            home_crest_url=match["homeTeam"].get("crest", ""), away_crest_url=match["awayTeam"].get("crest", ""),
            # Penalty shootout score (None/None on any normal or AET
            # finish — render_scoreboard_card skips the whole block then).
            home_penalty=match.get("_penalty_home") if match.get("_went_to_penalties") else None,
            away_penalty=match.get("_penalty_away") if match.get("_went_to_penalties") else None,
        )
        _post_if_new(_key_fulltime(mid), poster.fmt_fulltime(match), image_path=img)

    # ── Man of the Match ─────────────────────────────────────────────
    # Fires once, right after full time posts — currently never has
    # data (neither source carries player ratings; see scraper.get_man_of_the_match).
    if (config.POST_MOTM and status == "FINISHED"
            and _already_posted(_key_fulltime(mid))
            and not _already_posted(_key_motm(mid))):
        motm = None
        try:
            motm = scraper.get_man_of_the_match(match)
        except Exception as e:
            print(f"[BOT] ⚠️  MOTM lookup failed: {e}")
        if motm:
            print(f"[BOT] 🌟 Man of the Match: {motm['name']}")
            team = match["homeTeam"] if motm.get("team_side") == "home" else match["awayTeam"]
            opponent = match["awayTeam"] if motm.get("team_side") == "home" else match["homeTeam"]
            img = _safe_image(
                graphics.render_motm_card, motm["name"], team["name"], motm.get("rating"),
                competition=match.get("_comp_name", ""), opponent_name=opponent["name"],
                player_photo_url=motm.get("photo_url", ""), team_crest_url=team.get("crest", ""),
            )
            _post_if_new(_key_motm(mid), poster.fmt_motm(match, motm), image_path=img)
        else:
            # No rating data available (the lineups endpoint didn't
            # return anything usable) — mark as posted anyway so we
            # don't retry every tick forever.
            _mark_posted(_key_motm(mid))


# ══════════════════════════════════════════════════════════════════
# STARTUP — seed finished matches to prevent duplicate posts
# ══════════════════════════════════════════════════════════════════

def _seed_finished(matches: list):
    seeded = 0
    for m in matches:
        if m["status"] != "FINISHED":
            continue
        mid = m["id"]
        for key in (
            _key_fulltime(mid),
            _key_kickoff(mid),
            _key_lineup(mid),
            _key_extratime(mid),
            _key_halftime(mid),
            _key_motm(mid),
        ):
            if key not in _events:
                _events[key] = time.time()
                seeded += 1
        
        # Enrich missing scores before seeding so all goals have valid snapshots
        goals_to_seed = m.get("goals", [])
        _enrich_missing_scores(goals_to_seed)
        
        for idx, g in enumerate(goals_to_seed):
            # Seal ALL alias keys for each goal (not just primary)
            _mark_goal_posted(mid, g, idx)
            seeded += len(_goal_alias_keys(mid, g, idx))
        
        for b in m.get("bookings", []):
            if b.get("card") != "RED_CARD":
                continue
            k = _key_redcard(mid, b)
            if k not in _events:
                _events[k] = time.time()
                seeded += 1
    if seeded:
        _save_state()
        print(f"[STATE] 🌱 Seeded {seeded} keys from already-finished matches")


# ══════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════

def main():
    _load_state()
    _start_keepalive()

    print("[STATE] 🌱 Seeding finished matches on startup...")
    _seed_finished(scraper.get_todays_matches())

    print("=" * 60)
    print("  Match Corna Live Bot — Running")
    print(f"  Poll interval : {config.POLL_INTERVAL}s")
    print(f"  Data source   : SofaScore")
    print(f"  Lineups       : {config.POST_LINEUPS} (via SofaScore, ~{config.LINEUP_LEAD_MINUTES}min pre-kickoff)")
    print(f"  Kick-off      : {config.POST_KICKOFF}")
    print(f"  Stale window  : {config.MAX_EVENT_AGE_MINUTES}min "
          f"({'from .env/host env var' if os.getenv('MAX_EVENT_AGE_MINUTES') else 'code default — not set in .env'}), "
          f"retry up to {STALE_RETRY_WINDOW_MINUTES}min before giving up on a delayed event")
    print(f"  Goals         : {config.POST_GOALS} (assists included when the source provides one)")
    print(f"  Red cards     : {config.POST_RED_CARDS}")
    print(f"  Half time     : {config.POST_HALFTIME}")
    print(f"  VAR/No Goal   : {config.POST_VAR_DISALLOWED} (best-effort — verify against a live match)")
    print(f"  Extra time    : True")
    print(f"  Full time     : {config.POST_FULLTIME}")
    print(f"  Preview       : {config.POST_DAILY_PREVIEW} @ {config.DAILY_PREVIEW_HOUR}:00 UTC")
    print(f"  Watched teams : {config.POST_TEAM_FIXTURES} — {len(team_fixtures.MASTER_TEAMS)} teams, "
          f"checked every {config.TEAM_FIXTURES_INTERVAL_MINUTES}min")
    mode_line = "DEVELOPER 🧪 (nothing will post to Facebook)" if config.DEV_MODE else "ACTIVE 🔴 (posts for real)"
    print(f"  Mode          : {mode_line}")
    print(f"  FB Page ID    : {'SET ✅' if config.FB_PAGE_ID else 'NOT SET'}")
    print("=" * 60)

    tick = 0

    while True:
        try:
            tick += 1
            now = datetime.now(timezone.utc)
            print(f"\n[BOT] ⏰ Tick #{tick} — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")

            matches = scraper.get_todays_matches()

            new_watched_fixtures = maybe_run_team_fixtures()
            if new_watched_fixtures:
                known_ids = {m["id"] for m in matches}
                added = 0
                for fm in new_watched_fixtures:
                    if fm["id"] not in known_ids:
                        matches.append(fm)
                        known_ids.add(fm["id"])
                        added += 1
                if added:
                    print(f"[BOT] +{added} watched-team fixture(s) added from team_fixtures.py")

            if tick == 1:
                # Full schedule dump — once, so you can see everything
                # the bot picked up for today right after a (re)start.
                print(f"[BOT] {len(matches)} matches today:")
                for m in matches:
                    et_tag  = " [ET]"  if m.get("_went_to_et")       else ""
                    pen_tag = " [PEN]" if m.get("_went_to_penalties") else ""
                    print(f"       {m.get('_comp_flag','⚽')} "
                          f"{m['homeTeam']['name']} vs {m['awayTeam']['name']} "
                          f"[{m['status']}{et_tag}{pen_tag}]")
            else:
                # Every later tick — just what's actually live right
                # now, so the log doesn't reprint the whole day's
                # schedule (including kicked-off-hours-ago friendlies)
                # on every single poll.
                live_now = [m for m in matches if m["status"] in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "SHOOTOUT")]
                if live_now:
                    print(f"[BOT] {len(live_now)} match(es) live now:")
                    for m in live_now:
                        et_tag  = " [ET]"  if m.get("_went_to_et")       else ""
                        pen_tag = " [PEN]" if m.get("_went_to_penalties") else ""
                        print(f"       {m.get('_comp_flag','⚽')} "
                              f"{m['homeTeam']['name']} vs {m['awayTeam']['name']} "
                              f"[{m['status']}{et_tag}{pen_tag}]")
                else:
                    print(f"[BOT] {len(matches)} matches today, none live right now")

            maybe_post_preview(matches)

            active = [
                m for m in matches
                if m["status"] in (
                    "SCHEDULED", "IN_PLAY", "PAUSED",
                    "EXTRA_TIME", "SHOOTOUT", "FINISHED"
                )
            ]

            for match in active:
                try:
                    process_match(match)
                except Exception as e:
                    print(f"[BOT] ⚠️  Error on {match.get('id','?')}: {e}")

            if tick % (3600 // config.POLL_INTERVAL) == 0:
                _cleanup_state()

        except KeyboardInterrupt:
            print("\n[BOT] Stopped.")
            break
        except Exception as e:
            print(f"[BOT] ❌ Unexpected error: {e}")

        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
