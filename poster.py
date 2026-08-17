"""
poster.py — Facebook posting + message formatters
===================================================
Post formats:

  Lineup (FD only):
    📋 LINEUPS | 🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League
    🏟️ Arsenal vs Chelsea
    Arsenal (4-3-3): Raya, White, Saliba...
    Chelsea (4-2-3-1): Sanchez, James...
    #Arsenal #Chelsea #PL #MatchCornaLive

  Kickoff:
    🟢 KICKOFF | 🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League
    🏟️ Arsenal 0 - 0 Chelsea
    #Arsenal #Chelsea #PL #MatchCornaLive

  Goal:
    ⚽ GOAL | 🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League
    🏟️ Arsenal 2 - 1 Chelsea
    Saka ⚽ 67'
    🎯 Odegaard (assist, when available)
    #Arsenal #Chelsea #PL #MatchCornaLive

  Extra Time:
    ⏱️ EXTRA TIME | 🏆 Champions League
    🏟️ Arsenal 1 - 1 Real Madrid
    #Arsenal #RealMadrid #UCL #MatchCornaLive

  Full Time:
    🏁 FULL TIME | 🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League
    🏟️ Arsenal 3 - 1 Chelsea
    Arsenal
    ⚽ Saka 12'  ⚽ Havertz 45'  ⚽ Trossard 89'
    Chelsea
    ⚽ Sterling 34'
    #Arsenal #Chelsea #PL #MatchCornaLive
"""

import time
import re
import requests
import config

_FB_API = "v22.0"   # bump here when Meta deprecates the current version

# ══════════════════════════════════════════════════════════════════
# COUNTRY → FLAG EMOJI
# ══════════════════════════════════════════════════════════════════

COUNTRY_FLAG = {
    # Europe
    "England": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "Scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "Wales": "🏴󠁧󠁢󠁷󠁬󠁳󠁿",
    "Germany": "🇩🇪", "France": "🇫🇷", "Spain": "🇪🇸", "Italy": "🇮🇹",
    "Portugal": "🇵🇹", "Netherlands": "🇳🇱", "Belgium": "🇧🇪",
    "Croatia": "🇭🇷", "Serbia": "🇷🇸", "Poland": "🇵🇱",
    "Turkey": "🇹🇷", "Ukraine": "🇺🇦", "Switzerland": "🇨🇭",
    "Austria": "🇦🇹", "Denmark": "🇩🇰", "Sweden": "🇸🇪",
    "Norway": "🇳🇴", "Finland": "🇫🇮", "Hungary": "🇭🇺",
    "Czech Republic": "🇨🇿", "Czechia": "🇨🇿", "Slovakia": "🇸🇰",
    "Romania": "🇷🇴", "Greece": "🇬🇷", "Iceland": "🇮🇸",
    "Ireland": "🇮🇪", "Republic of Ireland": "🇮🇪",
    "Northern Ireland": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "Albania": "🇦🇱", "Kosovo": "🇽🇰", "Montenegro": "🇲🇪",
    "Slovenia": "🇸🇮", "Bosnia": "🇧🇦",
    "Bosnia and Herzegovina": "🇧🇦", "Bosnia & Herzegovina": "🇧🇦",
    "Bulgaria": "🇧🇬", "North Macedonia": "🇲🇰",
    "Georgia": "🇬🇪", "Armenia": "🇦🇲", "Azerbaijan": "🇦🇿",
    "Russia": "🇷🇺", "Belarus": "🇧🇾", "Moldova": "🇲🇩",
    "Lithuania": "🇱🇹", "Latvia": "🇱🇻", "Estonia": "🇪🇪",
    "Luxembourg": "🇱🇺", "Malta": "🇲🇹", "Cyprus": "🇨🇾",
    "Israel": "🇮🇱", "Kazakhstan": "🇰🇿",
    "Faroe Islands": "🇫🇴", "Gibraltar": "🇬🇮",
    "San Marino": "🇸🇲", "Andorra": "🇦🇩", "Liechtenstein": "🇱🇮",
    # Americas
    "Brazil": "🇧🇷", "Argentina": "🇦🇷", "Uruguay": "🇺🇾",
    "Colombia": "🇨🇴", "Chile": "🇨🇱", "Peru": "🇵🇪",
    "Ecuador": "🇪🇨", "Bolivia": "🇧🇴", "Paraguay": "🇵🇾",
    "Venezuela": "🇻🇪",
    "USA": "🇺🇸", "United States": "🇺🇸", "Mexico": "🇲🇽",
    "Canada": "🇨🇦", "Costa Rica": "🇨🇷", "Panama": "🇵🇦",
    "Honduras": "🇭🇳", "Jamaica": "🇯🇲", "Haiti": "🇭🇹",
    "Trinidad and Tobago": "🇹🇹", "Trinidad & Tobago": "🇹🇹",
    "El Salvador": "🇸🇻", "Guatemala": "🇬🇹", "Nicaragua": "🇳🇮",
    "Cuba": "🇨🇺", "Curacao": "🇨🇼", "Martinique": "🇲🇶",
    "Aruba": "🇦🇼", "Bermuda": "🇧🇲", "Grenada": "🇬🇩",
    "Guyana": "🇬🇾", "Belize": "🇧🇿", "Suriname": "🇸🇷",
    # Africa
    "Nigeria": "🇳🇬", "Ghana": "🇬🇭", "Senegal": "🇸🇳",
    "Morocco": "🇲🇦", "Egypt": "🇪🇬", "Cameroon": "🇨🇲",
    "Ivory Coast": "🇨🇮", "Cote d'Ivoire": "🇨🇮",
    "South Africa": "🇿🇦", "Tunisia": "🇹🇳", "Algeria": "🇩🇿",
    "Mali": "🇲🇱", "Zambia": "🇿🇲", "Zimbabwe": "🇿🇼",
    "Tanzania": "🇹🇿", "Uganda": "🇺🇬", "Kenya": "🇰🇪",
    "Ethiopia": "🇪🇹", "Congo": "🇨🇬", "DR Congo": "🇨🇩",
    "Guinea": "🇬🇳", "Guinea-Bissau": "🇬🇼",
    "Burkina Faso": "🇧🇫", "Benin": "🇧🇯",
    "Gabon": "🇬🇦", "Angola": "🇦🇴", "Mozambique": "🇲🇿",
    "Rwanda": "🇷🇼", "Liberia": "🇱🇷", "Sierra Leone": "🇸🇱",
    "Gambia": "🇬🇲", "Togo": "🇹🇬", "Niger": "🇳🇪",
    "Namibia": "🇳🇦", "Botswana": "🇧🇼", "Malawi": "🇲🇼",
    "Mauritania": "🇲🇷", "Cape Verde": "🇨🇻",
    "Cape Verde Islands": "🇨🇻",
    "Equatorial Guinea": "🇬🇶", "Sudan": "🇸🇩",
    "South Sudan": "🇸🇸", "Somalia": "🇸🇴",
    "Central African Republic": "🇨🇫",
    "Sao Tome and Principe": "🇸🇹",
    "Comoros": "🇰🇲", "Seychelles": "🇸🇨",
    "Eswatini": "🇸🇿", "Lesotho": "🇱🇸",
    # Asia
    "Japan": "🇯🇵", "South Korea": "🇰🇷", "Korea Republic": "🇰🇷",
    "China": "🇨🇳", "Australia": "🇦🇺",
    "Iran": "🇮🇷", "IR Iran": "🇮🇷",
    "Saudi Arabia": "🇸🇦", "Qatar": "🇶🇦",
    "UAE": "🇦🇪", "United Arab Emirates": "🇦🇪",
    "Iraq": "🇮🇶", "Jordan": "🇯🇴",
    "Oman": "🇴🇲", "Bahrain": "🇧🇭", "Kuwait": "🇰🇼",
    "India": "🇮🇳", "Thailand": "🇹🇭", "Vietnam": "🇻🇳",
    "Indonesia": "🇮🇩", "Malaysia": "🇲🇾", "Philippines": "🇵🇭",
    "Uzbekistan": "🇺🇿", "Tajikistan": "🇹🇯",
    "North Korea": "🇰🇵", "Korea DPR": "🇰🇵",
    "Syria": "🇸🇾", "Lebanon": "🇱🇧", "Palestine": "🇵🇸",
    "Pakistan": "🇵🇰", "Bangladesh": "🇧🇩",
    "Hong Kong": "🇭🇰", "Singapore": "🇸🇬",
    "Sri Lanka": "🇱🇰", "Nepal": "🇳🇵",
    "Myanmar": "🇲🇲", "Cambodia": "🇰🇭",
    "Kyrgyzstan": "🇰🇬", "Turkmenistan": "🇹🇲",
    # Oceania
    "New Zealand": "🇳🇿", "Fiji": "🇫🇯",
    "Papua New Guinea": "🇵🇬", "Solomon Islands": "🇸🇧",
}

COMP_HASHTAG = {
    "Premier League":             "PL",
    "Bundesliga":                 "Bundesliga",
    "La Liga":                    "LaLiga",
    "Serie A":                    "SerieA",
    "Ligue 1":                    "Ligue1",
    "Champions League":           "UCL",
    "Europa League":              "UEL",
    "Europa Conference League":   "UECL",
    "FA Cup":                     "FACup",
    "EFL Cup":                    "EFLCup",
    "Championship":               "Championship",
    "Eredivisie":                 "Eredivisie",
    "MLS":                        "MLS",
    "Brasileirao":                "Brasileirao",
    "Liga MX":                    "LigaMX",
    "Belgian Pro League":         "JPL",
    "Saudi Pro League":           "SPL",
    "AFC Champions Elite":        "ACL",
    "CAF Champions League":       "CAFCL",
    "International Friendly":     "Friendly",
    "FIFA World Cup":             "WorldCup",
    "European Championship":      "EURO",
    "UEFA Nations League":        "NationsLeague",
    "Copa America":               "CopaAmerica",
    "Gold Cup":                   "GoldCup",
    "AFCON":                      "AFCON",
    "WC Qualifier Europe":        "WCQ",
    "WC Qualifier Africa":        "WCQ",
    "WC Qualifier CONCACAF":      "WCQ",
    "WC Qualifier South America": "WCQ",
    "WC Qualifier Asia":          "WCQ",
    "WC Qualifier Oceania":       "WCQ",
}


def _comp_tag(comp_name: str) -> str:
    tag = COMP_HASHTAG.get(comp_name, "")
    if not tag:
        tag = comp_name.replace(" ", "")
    return f"#{tag}"


def team_flag(name: str) -> str:
    if name in COUNTRY_FLAG:
        return COUNTRY_FLAG[name]
    for country, flag in COUNTRY_FLAG.items():
        if country.lower() in name.lower():
            return flag
    return ""


def _td(name: str, is_intl: bool) -> str:
    if is_intl:
        f = team_flag(name)
        return f"{f} {name}" if f else name
    return f"🏟️ {name}"


def _scoreline(match: dict, home_sc, away_sc) -> str:
    is_intl = match.get("_is_intl", False)
    h = _td(match["homeTeam"]["name"], is_intl)
    a = _td(match["awayTeam"]["name"], is_intl)
    return f"{h} {home_sc} - {away_sc} {a}"


def _live_scoreline(match: dict) -> str:
    """Current full-time score (used for live events like halftime / red card)."""
    h  = match["score"]["fullTime"].get("home")
    a  = match["score"]["fullTime"].get("away")
    hs  = str(h) if h is not None else "0"
    as_ = str(a) if a is not None else "0"
    return _scoreline(match, hs, as_)


def _final_scoreline(match: dict) -> str:
    return _live_scoreline(match)


def _minute(minute) -> str:
    s = str(minute).strip().rstrip("'")
    if ":" in s:
        s = s.split(":")[0]
    return s


_NAME_PARTICLES = {
    "van", "der", "den", "de", "di", "da", "du", "la", "le",
    "el", "al", "bin", "ibn", "von", "vd", "dos", "das", "do",
}


def _short_name(name: str | None) -> str | None:
    """'{First initial} {Surname}' — e.g. 'Lionel Messi' -> 'L Messi',
    'Lamine Yamal' -> 'L Yamal', 'Virgil van Dijk' -> 'V van Dijk'.
    Lowercase surname particles (van, de, der, ...) immediately before
    the final word are kept attached to the surname instead of being
    swallowed into the initial. Mononyms / single-word names are
    returned unchanged. None/empty input passes through unchanged."""
    if not name:
        return name
    parts = name.strip().split()
    if len(parts) < 2:
        return name
    first, *rest = parts
    surname_parts = [rest[-1]]
    i = len(rest) - 2
    while i >= 0 and (rest[i][:1].islower() or rest[i].lower() in _NAME_PARTICLES):
        surname_parts.insert(0, rest[i])
        i -= 1
    surname = " ".join(surname_parts)
    initial = first[0].upper()
    return f"{initial} {surname}"


def _hashtags(match: dict) -> str:
    h    = match["homeTeam"]["name"].replace(" ", "")
    a    = match["awayTeam"]["name"].replace(" ", "")
    comp = _comp_tag(match.get("_comp_name", ""))
    return f"#{h} #{a} {comp} #MatchCornaLive"


def _comp_header(match: dict) -> str:
    flag = match.get("_comp_flag", "⚽")
    name = match.get("_comp_name", "Football")
    return f"{flag} {name}"


# ══════════════════════════════════════════════════════════════════
# SHARED SCREENSHOT-STYLE FORMATTER
# ══════════════════════════════════════════════════════════════════
# 🚩 {Header}
#
# {body lines}
#
# Source: {source}      ← only for stats-style posts (omitted for match events)
# #tag1 #tag2 #tag3

def _build_post(header: str, body: list[str], hashtags: list[str], source: str | None = None, marker: str = "🚩") -> str:
    lines = [f"{marker} {header}"]
    if body:
        lines.append("")
        lines.extend(body)
    if source:
        lines.append("")
        lines.append(f"Source: {source}")
    lines.append("")
    lines.append(" ".join(f"#{t}" if not t.startswith('#') else t for t in hashtags[:3]))
    return "\n".join(lines)


def _match_hashtags(match: dict) -> list[str]:
    """Exactly 3 tags: team names + competition tag."""
    home = match["homeTeam"]["name"].replace(" ", "")
    away = match["awayTeam"]["name"].replace(" ", "")
    comp = _comp_tag(match.get("_comp_name", "")).lstrip("#")
    return [comp, home, away]


# ══════════════════════════════════════════════════════════════════
# RATE LIMITING
# ══════════════════════════════════════════════════════════════════

_last_post_time  = 0.0
_posts_this_hour = 0
_hour_start      = time.time()


def _rate_ok() -> bool:
    global _posts_this_hour, _hour_start
    now = time.time()
    if now - _hour_start > 3600:
        _posts_this_hour = 0
        _hour_start      = now
    if _posts_this_hour >= config.MAX_POSTS_PER_HOUR:
        print(f"[POSTER] ⚠️  Hourly limit ({config.MAX_POSTS_PER_HOUR}) reached")
        return False
    gap = config.MIN_POST_GAP - (now - _last_post_time)
    if gap > 0:
        time.sleep(gap)
    return True


# ══════════════════════════════════════════════════════════════════
# FACEBOOK POSTING
# ══════════════════════════════════════════════════════════════════

def post(message: str) -> bool:
    """Post a text update to the Facebook page feed."""
    global _last_post_time, _posts_this_hour
    if config.DEV_MODE or not config.FB_PAGE_ID:
        reason = "BOT_MODE=developer" if config.DEV_MODE else "FB_PAGE_ID not set"
        print(f"\n{'='*50}\n[FB POST — not sent, {reason}]\n{message}\n{'='*50}\n")
        return True
    if not _rate_ok():
        return False
    try:
        r = requests.post(
            f"https://graph.facebook.com/{_FB_API}/{config.FB_PAGE_ID}/feed",
            data={"message": message, "access_token": config.FB_PAGE_ACCESS_TOKEN},
            timeout=15,
        )
        if r.status_code == 200:
            _last_post_time   = time.time()
            _posts_this_hour += 1
            print(f"[POSTER] ✅ Posted! id={r.json().get('id','?')}")
            return True
        err = r.json().get("error", {})
        print(f"[POSTER] ❌ {r.status_code}: {err.get('message', r.text[:120])}")
        return False
    except Exception as e:
        print(f"[POSTER] ❌ {e}")
        return False


def post_photo(image_path: str, caption: str = "") -> bool:
    """Upload a photo to the Facebook page."""
    global _last_post_time, _posts_this_hour
    if config.DEV_MODE or not config.FB_PAGE_ID:
        reason = "BOT_MODE=developer" if config.DEV_MODE else "FB_PAGE_ID not set"
        print(f"\n{'='*50}\n[FB PHOTO — not sent, {reason}] {image_path}\n{caption}\n{'='*50}\n")
        return True
    if not _rate_ok():
        return False
    try:
        with open(image_path, "rb") as img_file:
            r = requests.post(
                f"https://graph.facebook.com/{_FB_API}/{config.FB_PAGE_ID}/photos",
                data={"caption": caption, "access_token": config.FB_PAGE_ACCESS_TOKEN},
                files={"source": img_file},
                timeout=60,
            )
        if r.status_code == 200:
            _last_post_time   = time.time()
            _posts_this_hour += 1
            print(f"[POSTER] ✅ Photo posted! id={r.json().get('id','?')}")
            return True
        err = r.json().get("error", {})
        print(f"[POSTER] ❌ Photo {r.status_code}: {err.get('message', r.text[:120])}")
        return False
    except FileNotFoundError:
        print(f"[POSTER] ❌ Image not found: {image_path}")
        return False
    except Exception as e:
        print(f"[POSTER] ❌ Photo error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════
# MESSAGE FORMATTERS
# ══════════════════════════════════════════════════════════════════

def fmt_daily_preview(matches: list) -> str:
    from datetime import datetime, timezone
    now     = datetime.now(timezone.utc)
    day_num  = now.strftime("%d").lstrip("0") or "0"
    date_str = f"{now.strftime('%A')} {day_num} {now.strftime('%B')}"

    if not matches:
        return (
            f"📅 Today's Fixtures | {date_str}\n"
            "No big matches today. Check back tomorrow!\n"
            "#MatchCornaLive"
        )

    sorted_m = sorted(matches, key=lambda m: m.get("utcDate", ""))
    by_comp: dict[str, list] = {}
    for m in sorted_m:
        comp = m.get("_comp_name", "Football")
        by_comp.setdefault(comp, []).append(m)

    lines = [f"📅 Today's Fixtures | {date_str}"]
    for comp, comp_matches in by_comp.items():
        flag    = comp_matches[0].get("_comp_flag", "⚽")
        is_intl = comp_matches[0].get("_is_intl", False)
        lines.append(f"{flag} {comp}")
        for m in comp_matches:
            h = _td(m["homeTeam"]["name"], is_intl)
            a = _td(m["awayTeam"]["name"], is_intl)
            try:
                ko = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
                t  = ko.strftime("%H:%M")
            except Exception:
                t = "TBD"
            lines.append(f"{h} vs {a} ({t})")

    lines.append("#MatchCornaLive")
    return "\n".join(lines)


def _surname(full_name: str) -> str:
    """
    Last "word" of a player's display name, for compact lineup posts
    ("Saka" not "Bukayo Saka"). Keeps multi-part surnames intact for
    common patterns instead of chopping them to one token:
      - "Van Dijk" -> "Van Dijk" (particle + surname, capitalized particle)
      - "de Bruyne" -> "de Bruyne" (lowercase particle + surname)
      - "Alisson" -> "Alisson" (single name, mononym)
    Falls back to the full string unchanged if it's already a single
    word, or if the name is empty/unusual.
    """
    if not full_name:
        return full_name
    parts = full_name.strip().split()
    if len(parts) <= 1:
        return full_name.strip()
    _PARTICLES = {"van", "de", "der", "den", "da", "di", "dos", "das",
                  "el", "al", "bin", "ibn", "mc", "mac", "st", "st."}
    if len(parts) >= 2 and parts[-2].lower().strip(".") in _PARTICLES:
        return " ".join(parts[-2:])
    return parts[-1]


def fmt_lineup(match: dict) -> str:
    is_intl = match.get("_is_intl", False)
    h_name = match["homeTeam"]["name"]
    a_name = match["awayTeam"]["name"]
    h = _td(h_name, is_intl)
    a = _td(a_name, is_intl)
    lines = [
        f"📋 LINEUPS | {_comp_header(match)}",
        f"{h} vs {a}",
    ]
    # Index whichever lineups we do have by team name, so a lineup that
    # arrives for only one side (very common — the away/smaller side
    # often confirms later) is shown as released, and the other side is
    # explicitly marked "Pending..." instead of silently vanishing from
    # the post (which reads as if that team had no lineup at all, or
    # worse, as if only one team were even playing).
    by_team = {lu.get("team", ""): lu for lu in match.get("lineups", [])}

    def _match_lineup(team_name: str):
        if team_name in by_team:
            return by_team[team_name]
        # The lineup feed's "team" display name doesn't always match our
        # normalized team name exactly (short forms, accents) — fall
        # back to a loose substring match either direction.
        for key, lu in by_team.items():
            if key and (key in team_name or team_name in key):
                return lu
        return None

    any_released = False
    for team_name in (h_name, a_name):
        lu = _match_lineup(team_name)
        if lu is None:
            lines.append(f"{team_name}: Pending...")
            continue
        any_released = True
        formation = lu.get("formation", "")
        players = [_surname(p["player"].get("name", "?")) for p in lu.get("startXI", [])]
        if players:
            header = f"{team_name} ({formation})" if formation else team_name
            lines.append(f"{header}: {', '.join(players)}")
        else:
            lines.append(f"{team_name}: Pending...")

    if not any_released:
        # Shouldn't normally happen (process_match only posts once at
        # least one side's lineup is found), but never post a caption
        # that looks like a lineup update with zero actual lineup info.
        return ""

    lines.append(_hashtags(match))
    return "\n".join(lines)


def _plain_score_line(match: dict, home_sc, away_sc) -> str:
    """Plain team names + tight score, matching the screenshot style
    (no per-team flags in the header line — flags are used in ranked lists)."""
    h = match["homeTeam"]["name"]
    a = match["awayTeam"]["name"]
    return f"{h} {home_sc}-{away_sc} {a}"


def _goal_line(g: dict) -> str:
    """'{Player} {minute}\'' — or, when SofaScore's incident data didn't
    include a player name (common on lower-data leagues/friendlies),
    just '{minute}\'' with no name at all. This used to fall back to
    the SCORING TEAM'S name standing in as a fake player (e.g. a post
    literally reading "CD Eldense 25'"), which reads as a real name at
    a glance and is misleading. Better to openly show a goal happened
    with no attribution than to imply the club itself is who scored."""
    name = _short_name((g.get("scorer") or {}).get("name"))
    line = f"{name} {_minute(g['minute'])}'" if name else f"{_minute(g['minute'])}'"
    assist = g.get("assist", {}).get("name")
    if assist:
        line += f" (assist: {_short_name(assist)})"
    return line


def fmt_kickoff(match: dict) -> str:
    header = f"Live: {_plain_score_line(match, 0, 0)} — Kickoff!"
    return _build_post(header, [], _match_hashtags(match))


def fmt_goal(match: dict, goal: dict) -> str:
    scorer = _short_name((goal.get("scorer") or {}).get("name"))
    minute = _minute(goal["minute"])

    sc = goal.get("score", [])
    if sc and len(sc) == 2 and sc[0] is not None:
        h_sc, a_sc = sc[0], sc[1]
    else:
        h_sc, a_sc = 0, 0
        for g in match.get("goals", []):
            if g["isHome"]:
                h_sc += 1
            else:
                a_sc += 1
            if g is goal:
                break

    header = f"Live: {_plain_score_line(match, h_sc, a_sc)}"
    body = [f"⚽ Goal: {scorer} ({minute}')"] if scorer else [f"⚽ Goal ({minute}')"]
    assist = goal.get("assist", {}).get("name")
    if assist:
        body.append(f"🎯 Assist: {_short_name(assist)}")

    return _build_post(header, body, _match_hashtags(match))


def fmt_extratime(match: dict) -> str:
    return "\n".join([
        f"⏱️ EXTRA TIME | {_comp_header(match)}",
        _final_scoreline(match),
        _hashtags(match),
    ])


def fmt_halftime(match: dict) -> str:
    """
    Half Time: Arsenal 1-0 Chelsea
    ⚽ Arsenal: Saka 34' (assist: Odegaard)
    #Arsenal #Chelsea #PL #MatchCornaLive
    """
    header = f"Half Time: {_plain_score_line(match, *_current_score(match))}"
    body = []

    home_goals = [g for g in match.get("goals", []) if     g["isHome"]]
    away_goals = [g for g in match.get("goals", []) if not g["isHome"]]

    if home_goals:
        body.append(f"⚽ {match['homeTeam']['name']}: " + ", ".join(_goal_line(g) for g in home_goals))
    if away_goals:
        body.append(f"⚽ {match['awayTeam']['name']}: " + ", ".join(_goal_line(g) for g in away_goals))
    if not body:
        body.append("No goals yet")

    return _build_post(header, body, _match_hashtags(match), marker="⏸️")


def fmt_redcard(match: dict, booking: dict) -> str:
    """
    Live: Arsenal 1-0 Chelsea
    🟥 Mbappe 60'
    #Arsenal #Chelsea #PL #MatchCornaLive
    """
    header = f"Live: {_plain_score_line(match, *_current_score(match))}"
    player = _short_name(booking.get("player", {}).get("name")) or "Unknown"
    minute = _minute(booking.get("minute", "?"))
    body = [f"🟥 {player} {minute}'"]
    return _build_post(header, body, _match_hashtags(match), marker="🟥")



def scorers_line(match: dict, side: str | None = None) -> str:
    """Compact 'Player 27'\\nPlayer 61'' string of goals in the match,
    sorted by minute — for embedding directly on the scoreboard card
    image (graphics.render_scoreboard_card's home_event_line/
    away_event_line params), separate from the fuller per-team
    breakdown fmt_fulltime() uses in the post caption itself.

    `side` optionally filters to just "home" or "away" goals, so the
    card can draw each team's scorers under that team's own crest
    instead of one combined line down the center. Lines are newline-
    joined (not space-joined) so multiple scorers on the same side
    stack vertically under a narrower per-crest column."""
    def _sort_key(g: dict):
        try:
            return int(_minute(g["minute"]))
        except (TypeError, ValueError):
            return 0
    goals = match.get("goals", [])
    if side == "home":
        goals = [g for g in goals if g["isHome"]]
    elif side == "away":
        goals = [g for g in goals if not g["isHome"]]
    goals = sorted(goals, key=_sort_key)
    def _line(g: dict) -> str:
        name = _short_name((g.get("scorer") or {}).get("name"))
        return f"{name} {_minute(g['minute'])}'" if name else f"{_minute(g['minute'])}'"
    return "\n".join(_line(g) for g in goals)


def fmt_fulltime(match: dict) -> str:
    prefix = "Full Time"
    if match.get("_went_to_penalties"):
        prefix = "Full Time (Penalties)"
    elif match.get("_went_to_et"):
        prefix = "Full Time (AET)"

    header = f"{prefix}: {_plain_score_line(match, *_current_score(match))}"
    body = []

    if match.get("_went_to_penalties"):
        ph, pa = match.get("_penalty_home"), match.get("_penalty_away")
        if ph is not None and pa is not None:
            winner = match["homeTeam"]["name"] if ph > pa else match["awayTeam"]["name"]
            body.append(f"🏆 {winner} win {ph}-{pa} on penalties")
            body.append("")

    home_goals = [g for g in match.get("goals", []) if     g["isHome"]]
    away_goals = [g for g in match.get("goals", []) if not g["isHome"]]

    if home_goals:
        body.append(f"⚽ {match['homeTeam']['name']}: " + ", ".join(_goal_line(g) for g in home_goals))
    if away_goals:
        body.append(f"⚽ {match['awayTeam']['name']}: " + ", ".join(_goal_line(g) for g in away_goals))

    return _build_post(header, body, _match_hashtags(match), marker="🏁")


def fmt_motm(match: dict, motm: dict) -> str:
    """
    🌟 Man of the Match: L. Messi (Argentina)
    Rating: 9.2 | Argentina 3-1 Switzerland

    #Argentina #ManOfTheMatch
    """
    team = match["homeTeam"]["name"] if motm.get("team_side") == "home" else match["awayTeam"]["name"]
    header = f"Man of the Match: {motm['name']} ({team})"
    body = [f"⭐ {_plain_score_line(match, *_current_score(match))}"]
    if motm.get("rating") is not None:
        body.append(f"📊 Rating: {motm['rating']}")

    tags = _match_hashtags(match) + ["ManOfTheMatch"]
    return _build_post(header, body, tags, marker="🌟")


def _current_score(match: dict) -> tuple:
    h = match["score"]["fullTime"].get("home")
    a = match["score"]["fullTime"].get("away")
    return (h if h is not None else 0, a if a is not None else 0)


def fmt_var_disallowed(match: dict, var_event: dict) -> str:
    """
    🚩 No Goal — VAR Review: Germany vs Paraguay

    🚨 Disallowed: J. Tah (105') — Goal Disallowed
    Reason: Foul in the build-up

    #PL #VAR #NoGoal
    """
    header = f"No Goal — VAR Review: {match['homeTeam']['name']} vs {match['awayTeam']['name']}"
    minute = _minute(var_event.get("minute", "?"))
    body = [f"🚨 Disallowed: {var_event['player']} ({minute}') — {var_event.get('team', '')}"]
    reason = var_event.get("reason", "")
    if reason and reason.lower() not in ("var review", ""):
        body.append(f"Reason: {reason}")

    tags = [_comp_tag(match.get("_comp_name", "")).lstrip("#"), "VAR", "NoGoal"]

    return _build_post(header, body, tags, marker="❌")
