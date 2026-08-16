"""
today_matches.py — standalone report of today's matches for every team
on your watchlist (data/teams_master.json), grouped by league, with a
per-league match count. Doesn't post anything, doesn't touch state
files, doesn't need the bot running — just a quick look before you
decide which LEAGUE_* toggles to flip.

Run it from the same folder as bot.py:
    python3 today_matches.py
"""
from datetime import datetime

import config
import team_fixtures
from sofascore import _norm_status

print("[+] Fetching today's fixtures for all watched teams...")
raw_fixtures = team_fixtures.fetch_fixtures()
details = [team_fixtures.extract_details(e) for e in raw_fixtures]
details = [d for d in details if d is not None]
today = team_fixtures.filter_today(details)

# Group by league — using whichever side is on the watchlist (usually
# both are, since most fixtures here are between two watched teams in
# the same domestic league; if only one side is watched — e.g. a
# continental match — this still finds a league via that side).
by_league: dict[str, list[dict]] = {}
for fx in today:
    entry = team_fixtures.MASTER_TEAMS.get(fx["home_team_id"]) or team_fixtures.MASTER_TEAMS.get(fx["away_team_id"])
    league = entry["league"] if entry else fx["tournament_name"]
    by_league.setdefault(league, []).append(fx)

if not today:
    print("[i] No watched-team fixtures found for today.")
else:
    total = len(today)
    print(f"\n{total} match(es) today across {len(by_league)} league(s):\n")

    # Leagues with the most matches today first — the ones actually
    # worth thinking about switching off to cut down on post volume.
    for league in sorted(by_league, key=lambda l: -len(by_league[l])):
        matches = by_league[league]
        is_on = config.WATCHED_LEAGUES.get(league, config.WATCHED_LEAGUES_DEFAULT)
        state = "ON " if is_on else "OFF"
        print(f"── {league} — {len(matches)} match(es) — currently [{state}] ──")

        for fx in sorted(matches, key=lambda f: f["kickoff_ts"]):
            norm, _, _ = _norm_status(fx["status"])
            kickoff_local = fx["kickoff_utc"].astimezone(team_fixtures.MALAWI_TZ).strftime("%H:%M")
            live_tag = f" [{norm}]" if norm in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "SHOOTOUT") else ""
            print(f"   {kickoff_local}  {fx['home_name']} vs {fx['away_name']}{live_tag}")
        print()

    print("=" * 60)
    print("Summary (busiest leagues first):")
    for league in sorted(by_league, key=lambda l: -len(by_league[l])):
        is_on = config.WATCHED_LEAGUES.get(league, config.WATCHED_LEAGUES_DEFAULT)
        print(f"  {len(by_league[league]):>2} match(es) — {league} — currently {'ON' if is_on else 'OFF'}")
    print("=" * 60)
    print("To turn a league off: edit set_leagues.sh (or add/edit the")
    print("matching LEAGUE_*=false line directly in .env), then restart the bot.")
