"""
scraper.py — Match Corna Live data layer
========================================
Sole source: SofaScore (via sofascore.py). ESPN was removed as a data
source — its per-league scoreboard fetches (dozens of sequential HTTP
calls every poll) were the main cause of the bot posting events more
than 3 minutes late. This module now just hosts the shared helpers
(national-team detection, competition flag lookup, staleness filter)
that sofascore.py reuses, plus the public get_todays_matches()/
get_lineup() entry points bot.py calls.

EXTRA TIME & PENALTIES:
  _went_to_et        → True if match went to extra time
  _went_to_penalties → True if decided by penalty shootout
  _penalty_home/away → Shootout score
"""

import re
import unicodedata
from datetime import datetime, timezone


# ══════════════════════════════════════════════════════════════════
# COUNTRY / NATIONAL TEAM DETECTION
# ══════════════════════════════════════════════════════════════════

COUNTRIES = {
    "Albania","Andorra","Armenia","Austria","Azerbaijan","Belarus",
    "Belgium","Bosnia","Bosnia & Herzegovina","Bosnia and Herzegovina",
    "Bulgaria","Croatia","Cyprus","Czech Republic","Czechia",
    "Denmark","England","Estonia","Faroe Islands","Finland","France",
    "Georgia","Germany","Gibraltar","Greece","Hungary","Iceland",
    "Ireland","Republic of Ireland","Northern Ireland",
    "Israel","Italy","Kazakhstan","Kosovo","Latvia",
    "Liechtenstein","Lithuania","Luxembourg","Malta","Moldova",
    "Montenegro","Netherlands","North Macedonia","Norway","Poland",
    "Portugal","Romania","Russia","Football Union of Russia",
    "San Marino","Scotland","Serbia","Slovakia","Slovenia","Spain",
    "Sweden","Switzerland","Turkey","Türkiye","Ukraine","Wales",
    "Argentina","Aruba","Bahamas","Barbados","Belize","Bermuda",
    "Bolivia","Brazil","Canada","Cayman Islands","Chile","Colombia",
    "Costa Rica","Cuba","Curacao","Dominican Republic","Ecuador",
    "El Salvador","Grenada","Guatemala","Guyana","Haiti","Honduras",
    "Jamaica","Martinique","Mexico","Nicaragua","Panama","Paraguay",
    "Peru","Puerto Rico","St. Kitts and Nevis","St Kitts and Nevis",
    "St. Lucia","St Lucia","Suriname","Trinidad and Tobago",
    "Trinidad & Tobago","Turks and Caicos","Uruguay",
    "USA","United States","Venezuela","Virgin Islands",
    "Antigua and Barbuda","Dominica","Saint Vincent and the Grenadines",
    "Montserrat","Anguilla",
    "Algeria","Angola","Benin","Botswana","Burkina Faso","Burundi",
    "Cameroon","Cape Verde","Cape Verde Islands","Central African Republic",
    "Chad","Comoros","Congo","DR Congo","Djibouti","Egypt",
    "Equatorial Guinea","Eritrea","Ethiopia","Gabon","Gambia","Ghana",
    "Guinea","Guinea-Bissau","Ivory Coast","Cote d'Ivoire",
    "Kenya","Lesotho","Liberia","Libya","Madagascar","Malawi","Mali",
    "Mauritania","Mauritius","Morocco","Mozambique","Namibia","Niger",
    "Nigeria","Rwanda","Sao Tome and Principe","Senegal","Seychelles",
    "Sierra Leone","Somalia","South Africa","South Sudan","Sudan",
    "Swaziland","Eswatini","Tanzania","Togo","Tunisia","Uganda",
    "Zambia","Zimbabwe",
    "Afghanistan","Bahrain","Bangladesh","Bhutan","Brunei","Cambodia",
    "China","Chinese Taipei","Taiwan","Guam","Hong Kong","India",
    "Indonesia","Iran","IR Iran","Iraq","Japan","Jordan","Kuwait",
    "Kyrgyzstan","Laos","Lebanon","Macau","Malaysia","Maldives",
    "Mongolia","Myanmar","Nepal","North Korea","Korea DPR",
    "Oman","Pakistan","Palestine","Philippines","Qatar","Saudi Arabia",
    "Singapore","South Korea","Korea Republic","Sri Lanka","Syria",
    "Tajikistan","Thailand","Timor-Leste","Turkmenistan",
    "UAE","United Arab Emirates","Uzbekistan","Vietnam","Yemen",
    "American Samoa","Australia","Cook Islands","Fiji","New Caledonia",
    "New Zealand","Papua New Guinea","Samoa","Solomon Islands",
    "Tahiti","Tonga","Vanuatu",
}

CLUB_INDICATORS = [
    " fc"," cf"," sc"," ac"," bc"," bk"," sk"," fk"," nk",
    " united"," city"," town"," rovers"," wanderers"," athletic",
    " albion"," hotspur"," villa"," palace"," wednesday"," county",
    " forest"," rangers"," celtic"," thistle"," sporting"," benfica",
    " porto"," ajax"," psv"," feyenoord"," madrid"," barcelona",
    " atletico"," sevilla"," valencia"," juventus"," milan"," inter",
    " napoli"," roma"," lazio"," munich"," dortmund"," leverkusen",
    " frankfurt"," paris"," lyon"," marseille"," monaco",
    " arsenal"," chelsea"," liverpool"," tottenham",
    " galatasaray"," fenerbahce"," besiktas",
]


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


_COUNTRIES_NORM = {_strip_accents(c).lower(): c for c in COUNTRIES}

# International auto-detection is scoped to SENIOR MEN'S national teams
# only — ESPN labels women's and youth/age-group national sides with a
# suffix on the country name (e.g. "Nigeria Women", "Spain U23",
# "England U-20", "USA Olympic"), while both sides of a genuine senior
# match are just the plain country name. Checked as whole-word/suffix
# matches (not bare substrings) so this can't accidentally trip on a
# country whose name happens to contain one of these tokens.
_NON_SENIOR_KEYWORDS = re.compile(
    r"\b(women|womens|w|girls?|"
    r"u1[0-9]|u2[0-3]|u-1[0-9]|u-2[0-3]|"
    r"olympic|youth|junior|reserves?)\b",
    re.IGNORECASE,
)


def is_national_team(name: str) -> bool:
    if not name:
        return False
    name_clean = name.strip()
    if _NON_SENIOR_KEYWORDS.search(name_clean):
        return False
    if name_clean in COUNTRIES:
        return True
    name_lower = _strip_accents(name_clean).lower()
    if name_lower in _COUNTRIES_NORM:
        return True
    if any(ind in f" {name_lower} " or name_lower.endswith(ind.strip())
           for ind in CLUB_INDICATORS):
        return False
    for country_norm in _COUNTRIES_NORM:
        if country_norm in name_lower:
            return True
    return False


def is_international_match(match: dict) -> bool:
    h = match.get("homeTeam", {}).get("name", "")
    a = match.get("awayTeam", {}).get("name", "")
    return is_national_team(h) and is_national_team(a)


# ══════════════════════════════════════════════════════════════════
# COMPETITION FLAG HELPER
# ══════════════════════════════════════════════════════════════════

_FLAG_MAP = {
    "Champions League": "🏆", "Premier League": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "Club Friendly": "🤝",
    "Bundesliga": "🇩🇪", "La Liga": "🇪🇸", "Serie A": "🇮🇹",
    "Ligue 1": "🇫🇷", "World Cup": "🌍", "Friendly": "🤝",
    "European Championship": "🇪🇺", "Nations League": "🏆",
    "Europa League": "🟠", "Conference": "🟢", "FA Cup": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "Copa America": "🌎", "CONCACAF": "🌎", "Gold Cup": "🌎",
    "AFCON": "🌍", "Africa Cup": "🌍", "CAF": "🌍",
    "Asian": "🌏", "Qualifier": "🌍", "MLS": "🇺🇸",
    "Brasileirao": "🇧🇷", "Liga MX": "🇲🇽",
    "Championship": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "Eredivisie": "🇳🇱",
    "Belgian Pro League": "🇧🇪", "Saudi Pro League": "🇸🇦",
    "AFC Champions": "🌏", "CAF Champions": "🌍",
    "Women's Champions": "🏆", "Women's Super": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
}


def _comp_flag(comp_name: str) -> str:
    comp_lower = comp_name.lower()
    for k, v in _FLAG_MAP.items():
        if k.lower() in comp_lower:
            return v
    return "⚽"


def get_man_of_the_match(match: dict) -> dict | None:
    """
    Man of the Match lookup. Neither of our sources exposes per-player
    ratings today, so this always returns None (caller skips the MOTM
    post rather than guessing). Kept as a stable hook for a future
    source that does carry ratings.
    """
    return None


def get_lineup(league_slug: str, event_id: str, home_name: str = "", away_name: str = "") -> list[dict]:
    """Fetch starting XI for both teams for one match. Returns [] if not
    yet published or on any error. May return a list with just ONE
    team's lineup if only one side has released theirs yet — caller
    (poster.fmt_lineup) handles that by marking the other side
    "Pending..." rather than dropping it.

    home_name/away_name are passed straight through to
    sofascore.get_lineup, which needs them to tag each side correctly
    (see that function's docstring for why)."""
    try:
        import sofascore
        return sofascore.get_lineup(event_id, home_name, away_name)
    except Exception as e:
        print(f"[SCRAPER] SofaScore lineup fetch skipped: {e}")
        return []



# ══════════════════════════════════════════════════════════════════
# STALENESS FILTER
# ══════════════════════════════════════════════════════════════════

def _is_stale_finished(match: dict) -> bool:
    """True if a FINISHED match kicked off more than 6 hours ago."""
    if match.get("status") != "FINISHED":
        return False
    utc_str = match.get("utcDate", "")
    if not utc_str:
        return False
    try:
        ko      = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - ko).total_seconds() / 3600
        return elapsed > 6
    except Exception:
        return False


def _drop_stale(matches: list[dict]) -> list[dict]:
    before  = len(matches)
    matches = [m for m in matches if not _is_stale_finished(m)]
    dropped = before - len(matches)
    if dropped:
        print(f"[SCRAPER] Dropped {dropped} stale finished match(es)")
    return matches


# ══════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════

def get_todays_matches() -> list[dict]:
    """
    Public entry point. SofaScore is the sole live-match source (ESPN
    was removed — its per-league scoreboard polling was the main cause
    of events posting more than 3 minutes late).
    """
    try:
        import sofascore
        matches = sofascore.get_live_matches()
    except Exception as e:
        print(f"[SCRAPER] SofaScore fetch failed: {e}")
        matches = []

    return _drop_stale(matches)
