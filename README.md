# ⚽ Match Corna Live — Facebook Football Bot

Posts live football updates to your Facebook page. No paid APIs, no
filler/stats posts — just real match events, present and future only.
Runs entirely on Railway; no Termux/phone step required.

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
| 📅 Daily preview | One compiled post of the WHOLE day's watched-team fixtures (07:00 UTC by default) |
| 📊 Odds card | Pre-match 1X2 pricing, 3-5 games per card, spread across 05:00-10:00 UTC by default |

**Not posted:** cancelled/postponed games, historical scores/stats,
transfer/gossip news, or filler content of any kind.

## Coverage
Coverage is entirely watched-team-driven: a match is only picked up if
one of its teams is listed in `data/teams_master.json`, grouped there
by league. `LEAGUE_*` env vars (see below) switch a whole league's
watched teams on/off at once. To add a new club or competition, add
its team IDs to `data/teams_master.json` — a league name not yet
listed defaults to ON.

## Deploying on Railway
1. Push this repo to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo** → select it.
3. Railway auto-detects `Procfile` and runs `python bot.py` as a
   worker (no public URL needed — this bot doesn't serve web traffic).
4. Add environment variables in Railway's **Variables** tab — this is
   the *only* place settings live; there's no separate config file to
   keep in sync. See "Environment variables" below for the full list.
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

## Environment variables
Everything is controlled from Railway's Variables tab — changing any
of these triggers a restart and takes effect on the bot's next tick.

**Required**
| Variable | Purpose |
|---|---|
| `FB_PAGE_ID` | Your Facebook Page ID |
| `FB_PAGE_ACCESS_TOKEN` | Page access token with `pages_manage_posts` |

**Leagues** — each switches a whole league's watched teams on/off:
`LEAGUE_PREMIER_LEAGUE`, `LEAGUE_LA_LIGA`, `LEAGUE_SERIE_A`,
`LEAGUE_BUNDESLIGA`, `LEAGUE_LIGUE_1`, `LEAGUE_EREDIVISIE`,
`LEAGUE_MLS`, `LEAGUE_BRAZIL_SERIE_A`, `LEAGUE_SAUDI_PRO_LEAGUE`,
`LEAGUE_SOUTH_AFRICAN_PREMIERSHIP`, `LEAGUE_MALAWI_SUPER_LEAGUE`,
`LEAGUE_SUPER_LIG`, `LEAGUE_LIGA_MX`, `LEAGUE_CHAMPIONSHIP`
(each `true`/`false`, default `true`). `LEAGUE_DEFAULT` sets the
fallback for any league not in that list (default `true`).

**Busy-day filter**
| Variable | Purpose |
|---|---|
| `FAVORITES` | `on`/`off` (default `off`) — when on, only clubs listed in `data/favorites.json` post, within leagues still switched ON above |

**What to post** (all default `true` unless noted)
| Variable | Purpose |
|---|---|
| `POST_LINEUPS` | Starting XI post |
| `POST_KICKOFF` | Kick-off post |
| `POST_GOALS` | Goal post |
| `POST_HALFTIME` | Half-time post |
| `POST_RED_CARDS` | Red card post |
| `POST_FULLTIME` | Full-time post |
| `POST_VAR_DISALLOWED` | Disallowed/VAR goal post |
| `POST_DAILY_PREVIEW` | Daily fixture-list preview |
| `POST_TEAM_FIXTURES` | Pre-match "fixture today" alert |

**Daily preview**
| Variable | Purpose |
|---|---|
| `DAILY_PREVIEW_HOUR` | UTC hour to post the preview (default `7`) |

**Goal accuracy**
| Variable | Purpose |
|---|---|
| `GOAL_CONFIRM_SECONDS` | How long a goal must persist in the feed before it's posted live (default `120`) — see "Goal accuracy" below |

**Odds cards**
| Variable | Purpose |
|---|---|
| `ODDS_API_KEY` | Your odds-api.io key — with no key set, odds cards are silently skipped |
| `POST_ODDS` | On/off for odds cards (default `true`) |
| `ODDS_BOOKMAKER` | Which bookmaker's price to use (default `Bet365`) |
| `ODDS_WINDOW_START_HOUR` / `ODDS_WINDOW_END_HOUR` | UTC window odds cards post within (default `5`-`10`) |
| `ODDS_MIN_GAMES_PER_CARD` / `ODDS_MAX_GAMES_PER_CARD` | Games per card (default `3`-`5`) |

**Timing / tuning**
| Variable | Purpose |
|---|---|
| `POLL_INTERVAL` | Seconds between polls (default `60`) |
| `LINEUP_LEAD_MINUTES` | Minutes before KO to start checking for lineups (default `55`) |
| `MIN_POST_GAP` | Minimum seconds between two posts (default `20`) |
| `MAX_POSTS_PER_HOUR` | Post-rate ceiling (default `25`) |
| `MAX_EVENT_AGE_MINUTES` | Drop an event as stale after this many minutes unposted (default `8`) |
| `TEAM_FIXTURES_INTERVAL_MINUTES` | How often to re-check every watched team's next fixture (default `180`) |

**Infra**
| Variable | Purpose |
|---|---|
| `BOT_MODE` | `active` (default) or `developer` — see above |
| `DATA_DIR` | Path to your mounted Volume, e.g. `/data` — see below |
| `PORT` | Railway keep-alive port (default `8080`) |

## Daily preview — how it works
At `DAILY_PREVIEW_HOUR` UTC (default `07:00`), the bot fetches every
watched team's upcoming fixtures fresh, filters that down to today's
matches only, and posts the whole day's list as one compiled preview —
this is a live lookup at posting time, not a running tally built up
from whatever happened to be live earlier in the day, so it correctly
includes matches that haven't kicked off yet even at 7am. Turn it off
with `POST_DAILY_PREVIEW=false`, or move the time with
`DAILY_PREVIEW_HOUR`.

## Goal accuracy — confirmation delay + VAR correction
Two layers, both built on the same principle: **posting a goal a poll
cycle late is fine; posting a goal that gets VAR-overturned is not.**

1. **Before posting** — a newly-seen goal must survive being seen
   again `GOAL_CONFIRM_SECONDS` later (default `120`, i.e. it must
   still be there on the poll after next) before it's allowed to
   become a live "GOAL ⚽" card. Most VAR reviews resolve well inside
   that window, so the large majority of would-be false goals are
   caught here and simply never posted at all — no confusing
   goal-then-correction pair, nothing to clean up. This only delays
   the *live card*; the ordering guarantee below still applies
   underneath it, so a later goal can never jump ahead of an earlier
   one still waiting to be confirmed.
2. **After posting** — on the rare goal that clears confirmation and
   then gets overturned anyway, `sofascore._detect_var_disallowed_goals`
   catches it: SofaScore's incidents feed simply drops the goal once
   VAR overturns it, so comparing this poll's goals against the
   previous poll's catches the disappearance directly (rather than
   trying to infer it from the scoreline, which can look fine again
   once later goals bring the score back in line). This posts a
   "❌ No Goal — VAR Review" correction (`POST_VAR_DISALLOWED`) and
   removes the goal from the half-time/full-time scorer lines.

## Goal ordering
Goals are always posted in strict chronological (match-minute) order,
re-derived independently every tick rather than trusted from whatever
order the feed happens to return — a goal is never posted live until
every earlier goal in the same match has already posted. A goal that
arrives late (after a chronologically newer one already went out) is
never turned into a live card at all — it still shows up normally in
the half-time/full-time scorer lines, just without a live post, since
a scoreline appearing to "go backwards" live is worse than one quiet
goal.

## Odds cards
Once a day, `odds.py` fetches today's fixtures where BOTH teams are on
the watch-list (data/teams_master.json — same source of truth as
everything else) and pulls a 1X2 (`ODDS_BOOKMAKER`) price for each from
odds-api.io. Matches are grouped into cards of `ODDS_MIN_GAMES_PER_CARD`
to `ODDS_MAX_GAMES_PER_CARD` games (default 3-5), evenly balanced (8
matches becomes two cards of 4, never 5+3). Each card's post time is
spread evenly across `ODDS_WINDOW_START_HOUR`-`ODDS_WINDOW_END_HOUR`
UTC (default 05:00-10:00) rather than all posted at once — the
schedule and each card's matches are fixed the first tick of the day
that lands in the window, and persisted, so a restart mid-window
doesn't rebuild or reshuffle it. With no `ODDS_API_KEY` set, this
feature quietly does nothing. Prices are shown for entertainment only
and are never framed as a tip or pick.

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
config.py           ← All settings, read from Railway env vars
sofascore.py        ← Live match data source
team_fixtures.py    ← Watched-team fixture tracking + daily preview builder
odds.py             ← Pre-match 1X2 odds fetching (odds-api.io) + card scheduling
poster.py           ← Facebook API calls + text formatters
graphics.py         ← Score-card image rendering (Pillow)
scraper.py          ← Shared helpers (flags, national-team detection)
today_matches.py    ← Standalone: preview today's matches per league, no posting
data/teams_master.json ← Watchlist team IDs, grouped by league
data/favorites.json ← Busy-day filter club list (see FAVORITES above)
fonts/              ← Fonts used by graphics.py
Procfile            ← Tells Railway how to start the bot
```

## Checking fixture volume before a busy day
`today_matches.py` gives a quick report of how many matches each
league has today, grouped and counted, so you can decide what's worth
turning off before you get flooded with posts. It needs the same env
vars as the bot (`FB_PAGE_ID`/token aren't required, just the
`LEAGUE_*` ones if you want to check against your current settings) —
run it anywhere Python + `requirements.txt` are installed:
```
pip install -r requirements.txt
python3 today_matches.py
```
