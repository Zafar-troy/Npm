# ⚽ Match Corna Live — Facebook Football Bot

Posts live football updates to your Facebook page. No paid APIs, no
filler/stats posts — just real match events, present and future only.

## What gets posted
| Event | Example |
|-------|---------|
| 📋 Lineup | Starting XI ~1hr before KO (when available) |
| ▶️ Kick-off | Scoreboard card — team badges + KICK-OFF ribbon |
| ⚽ Goal | Scorer, minute, live score |
| 🟥 Red card | Player + minute, posted the moment it's detected |
| ⏸️ Half time | Current score + scorers/assists so far |
| ⏱️ Extra time | Notifies when ET starts (knockout matches) |
| 🏁 Full time | Final score + all goals. AET/Penalties labelled |
| 📅 Daily preview | One compiled post of today's fixtures (9AM UTC) |

**Not posted:** cancelled/postponed games, historical scores/stats,
transfer/gossip news, or filler content of any kind — this build only
covers real live match data from SofaScore.

## Coverage
13 leagues on the watchlist (each independently toggleable — see
below): Premier League, La Liga, Serie A, Bundesliga, Ligue 1,
Eredivisie, MLS, Brazil Série A, Saudi Pro League, South African
Premiership, Malawi Super League, Süper Lig, Liga MX — plus Champions
League/Europa/Conference League, World Cup, AFCON, and FA Cup, which
have no on/off toggle and are always covered.

## Deploying on Railway
1. Push this repo to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo** → select it.
3. Railway auto-detects `Procfile` and runs `python bot.py` as a
   worker (no public URL needed — this bot doesn't serve web traffic).
4. Add environment variables in Railway's **Variables** tab (not a
   `.env` file — Railway injects real env vars, which `config.py`
   reads automatically either way): `FB_PAGE_ID`,
   `FB_PAGE_ACCESS_TOKEN`, `BOT_MODE` (see below). See
   `.env.example` for the full list with explanations.
5. **Add a Volume** (Railway project → Settings → Volumes) mounted at
   e.g. `/data`, then set the env var `DATA_DIR=/data`. Without this,
   the bot's "what have I already posted" tracking resets on every
   redeploy, causing duplicate reposts — see "Persistent state" below.
6. Deploy. Check the logs for the startup banner — confirm
   `Mode: ACTIVE 🔴` and `FB Page ID: SET ✅` before you walk away.

### Testing safely before going live
Set `BOT_MODE=developer` in Railway's Variables tab. The bot runs
exactly the same — fetching, tracking, full console logs — but
nothing actually reaches Facebook; the logs show what *would* have
posted instead. Flip to `BOT_MODE=active` (or remove the var — active
is the default) once you're confident. A typo in this value fails
safe into developer mode, so a mistake here can never accidentally go
live.

## Controlling leagues from GitHub
Edit `leagues.json` (repo root) directly on GitHub — web editor or
the mobile app both work — set any league `true`/`false`, commit.
Railway's GitHub auto-deploy picks up the change and redeploys with
the new choices; no need to touch Railway's Variables tab for this.

```json
{
  "Premier League": true,
  "Serie A": false,
  ...
}
```

A league left out of `leagues.json` (or the whole file deleted) falls
back to its `LEAGUE_*` env var, so partial edits are always safe.
`today_matches.py` (run it locally: `python3 today_matches.py`) gives
a quick report of how many matches each league has today, grouped and
counted, so you can decide what's actually worth switching off before
you get flooded with posts on a busy day.

Note: every `leagues.json` edit triggers a Railway redeploy, which
(without a persistent Volume — see above) would reset posted-event
tracking and risk duplicate reposts. Set up the Volume first if you
plan to toggle leagues often, especially mid-matchday.

## Persistent state
The bot tracks what it's already posted in `state_v2.json` and
`data/team_fixtures_state.json` so it never reposts the same
kickoff/goal/fixture twice. Without a mounted Volume, these reset on
every Railway redeploy (code push, env var change, restart) — the
bot will treat everything as new again on the next tick, causing
duplicate posts for anything already live. Fix: Railway Volume +
`DATA_DIR` env var pointing at it (step 5 above).

## Files
```
bot.py              ← Run this — main loop, event detection, posting logic
config.py           ← All settings (env vars + leagues.json)
leagues.json         ← Edit this on GitHub to toggle leagues on/off
sofascore.py        ← Live match data source
team_fixtures.py    ← Watched-team fixture tracking (232 teams, data/teams_master.json)
poster.py           ← Facebook API calls + text formatters
graphics.py         ← Score-card image rendering (Pillow)
scraper.py          ← Shared helpers (flags, national-team detection)
today_matches.py    ← Standalone: preview today's matches per league, no posting
data/teams_master.json ← Watchlist team IDs, grouped by league
fonts/              ← Fonts used by graphics.py
Procfile            ← Tells Railway how to start the bot
.env.example        ← Template for local/Termux use (copy to .env)
```

## Local / Termux use
Same as Railway, minus the GitHub auto-deploy step:
```
cp .env.example .env
nano .env      # fill in FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN
pip install -r requirements.txt
python3 -u bot.py
```
