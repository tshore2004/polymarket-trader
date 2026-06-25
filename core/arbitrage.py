"""Arbitrage detection — two strategies:

1. IntraMarketArbDetector — YES_ask + NO_ask < 1 - fee on Polymarket (single-exchange).
2. CrossPlatformArbScanner — Polymarket vs Kalshi price discrepancies on matched markets.

Sports matching uses city-based extraction rather than keyword Jaccard because
Kalshi abbreviates team names ("New York M", "Los Angeles D") while Polymarket
uses full names ("New York Mets", "Los Angeles Dodgers").
"""
from __future__ import annotations
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from typing import Optional

from config import Config
from core.api_client import PolymarketPublicClient
from utils.models import Market, ArbOpportunity, KalshiMarket, ArbitrageOpportunity

logger = logging.getLogger(__name__)

_STOPWORDS = {
    # Articles / prepositions / conjunctions
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of",
    "with", "is", "are", "was", "will", "be", "by", "as", "if", "do",
    "this", "that", "it", "its", "who", "which", "what", "how", "all",
    "not", "no", "yes", "over", "from", "up", "out", "per", "via",
    # Political / government role words — these describe the type of question,
    # not the specific subject.  Without stopping them, two PM-candidate markets
    # ("Will Netanyahu be PM?" vs "Will Israel Katz be PM?") share {prime,
    # minister, next, israel} and score Jaccard ≈ 0.57, clearing the 0.45
    # threshold despite asking about completely different people.
    "prime", "minister", "president", "vice", "secretary", "senator",
    "governor", "chancellor", "mayor", "general", "attorney", "justice",
    "next", "new", "current", "former", "remain", "become", "elect",
    "elected", "reelected", "serve", "term", "office",
}

# ── Sports matching helpers ──────────────────────────────────────────────────

# Multi-word city prefixes that must be kept together.
_MULTI_WORD_CITIES = [
    # US cities
    "golden state", "green bay", "kansas city", "las vegas", "los angeles",
    "new england", "new jersey", "new orleans", "new york", "oklahoma city",
    "salt lake", "san antonio", "san diego", "san francisco", "st louis",
    "st. louis", "tampa bay", "twin cities",
    # Soccer clubs (city IS the multi-word name)
    "real madrid", "manchester city", "manchester united", "inter miami",
    "fc barcelona", "atletico madrid", "paris saint germain", "psg",
    "borussia dortmund", "bayer leverkusen", "rb leipzig",
    "west ham", "tottenham hotspur", "crystal palace", "aston villa",
    "newcastle united", "brighton hove", "nottingham forest",
    "porto", "benfica", "sporting cp",
    "ajax amsterdam", "psv eindhoven",
    "real sociedad", "real betis", "athletic bilbao",
    # Cricket clubs / national teams (no prefix needed for single-word nations)
    "new zealand", "south africa", "sri lanka", "west indies",
    "royal challengers", "kolkata knight", "sunrisers hyderabad",
    "punjab kings", "rajasthan royals", "delhi capitals",
    "lucknow super", "gujarat titans", "chennai super",
    "mumbai indians",
]

# Kalshi often truncates team names to a single letter after the city
# ("New York M" for Mets, "Los Angeles D" for Dodgers).  This maps
# (normalised_city, first_letter) → canonical short name so we can align
# with Polymarket's full names.
# NOTE: _expand_team_abbrev in kalshi_client.py resolves most abbreviations
# using the event title before they reach this map.  These entries are the
# fallback for cases where the event title isn't available (e.g. /markets/{ticker}
# refresh calls that return no event context).
_KALSHI_ABBREV_TO_TEAM: dict[tuple[str, str], str] = {
    # ── MLB ──────────────────────────────────────────────────────────────────
    ("arizona", "d"): "diamondbacks",
    ("atlanta", "b"): "braves",
    ("baltimore", "o"): "orioles",
    ("boston", "r"): "red sox",
    ("chicago", "c"): "cubs",
    ("chicago", "w"): "white sox",
    ("cincinnati", "r"): "reds",
    ("cleveland", "g"): "guardians",
    ("colorado", "r"): "rockies",
    ("detroit", "t"): "tigers",
    ("houston", "a"): "astros",
    ("kansas city", "r"): "royals",
    ("los angeles", "a"): "angels",
    ("los angeles", "d"): "dodgers",
    ("miami", "m"): "marlins",
    ("milwaukee", "b"): "brewers",
    ("minnesota", "t"): "twins",
    ("new york", "m"): "mets",
    ("new york", "y"): "yankees",
    ("oakland", "a"): "athletics",
    ("philadelphia", "p"): "phillies",
    ("pittsburgh", "p"): "pirates",
    ("san diego", "p"): "padres",
    ("san francisco", "g"): "giants",
    ("seattle", "m"): "mariners",
    ("st louis", "c"): "cardinals",      # Kalshi normalises "St. Louis" → "st louis"
    ("tampa bay", "r"): "rays",
    ("texas", "r"): "rangers",
    ("toronto", "b"): "blue jays",
    ("washington", "n"): "nationals",
    # ── NBA / WNBA ────────────────────────────────────────────────────────────
    ("atlanta", "h"): "hawks",
    ("boston", "c"): "celtics",
    ("brooklyn", "n"): "nets",
    ("charlotte", "h"): "hornets",
    ("chicago", "b"): "bulls",
    ("cleveland", "c"): "cavaliers",
    ("dallas", "m"): "mavericks",
    ("denver", "n"): "nuggets",
    ("detroit", "p"): "pistons",
    ("golden state", "w"): "warriors",
    ("houston", "r"): "rockets",
    ("indiana", "p"): "pacers",
    ("los angeles", "c"): "clippers",
    ("los angeles", "l"): "lakers",
    ("memphis", "g"): "grizzlies",
    ("miami", "h"): "heat",
    ("milwaukee", "b"): "bucks",
    # ("minnesota", "t") intentionally absent — MLB "twins" (defined above) takes precedence
    ("new orleans", "p"): "pelicans",
    ("new york", "k"): "knicks",
    ("new york", "n"): "nets",
    ("oklahoma city", "t"): "thunder",
    ("orlando", "m"): "magic",
    ("philadelphia", "s"): "76ers",
    ("phoenix", "s"): "suns",
    ("portland", "t"): "trail blazers",
    ("sacramento", "k"): "kings",
    ("san antonio", "s"): "spurs",
    ("toronto", "r"): "raptors",
    ("utah", "j"): "jazz",
    ("washington", "w"): "wizards",
    # ── NFL ──────────────────────────────────────────────────────────────────
    ("arizona", "c"): "cardinals",
    ("buffalo", "b"): "bills",
    ("carolina", "p"): "panthers",
    # ("chicago", "b") intentionally absent — NBA "bulls" (defined above) takes precedence
    ("dallas", "c"): "cowboys",
    ("denver", "b"): "broncos",
    ("green bay", "p"): "packers",
    ("indianapolis", "c"): "colts",
    ("jacksonville", "j"): "jaguars",
    ("kansas city", "c"): "chiefs",
    ("las vegas", "r"): "raiders",
    # ("los angeles", "c") intentionally absent — NBA "clippers" (defined above) takes precedence
    ("los angeles", "r"): "rams",
    ("miami", "d"): "dolphins",
    ("minnesota", "v"): "vikings",
    ("new england", "p"): "patriots",
    ("new orleans", "s"): "saints",
    ("new york", "g"): "giants",
    ("new york", "j"): "jets",
    ("philadelphia", "e"): "eagles",
    ("pittsburgh", "s"): "steelers",
    ("san francisco", "f"): "49ers",
    ("seattle", "s"): "seahawks",
    ("tampa bay", "b"): "buccaneers",
    ("tennessee", "t"): "titans",
    ("washington", "c"): "commanders",
    # ── NHL ──────────────────────────────────────────────────────────────────
    ("boston", "b"): "bruins",
    ("buffalo", "s"): "sabres",
    # ("chicago", "b") intentionally absent — NBA "bulls" (defined above) takes precedence
    ("colorado", "a"): "avalanche",
    ("dallas", "s"): "stars",
    ("detroit", "r"): "red wings",
    ("edmonton", "o"): "oilers",
    ("florida", "p"): "panthers",
    ("los angeles", "k"): "kings",
    ("minnesota", "w"): "wild",
    ("montreal", "c"): "canadiens",
    ("nashville", "p"): "predators",
    ("new jersey", "d"): "devils",
    ("new york", "i"): "islanders",
    ("new york", "r"): "rangers",
    ("ottawa", "s"): "senators",
    ("philadelphia", "f"): "flyers",
    # ("pittsburgh", "p") intentionally absent — MLB "pirates" (defined above) takes precedence
    ("san jose", "s"): "sharks",
    ("seattle", "k"): "kraken",
    ("st louis", "b"): "blues",
    ("tampa bay", "l"): "lightning",
    ("toronto", "m"): "maple leafs",
    ("vancouver", "c"): "canucks",
    ("vegas", "g"): "golden knights",
    # ("washington", "c") intentionally absent — NFL "commanders" (defined above) takes precedence
    # ── MLS Soccer ───────────────────────────────────────────────────────────
    ("atlanta", "u"): "atlanta united",
    ("charlotte", "f"): "charlotte fc",
    ("chicago", "f"): "fire",
    # ("colorado", "r") intentionally absent — MLB "rockies" (defined above) takes precedence
    ("columbus", "c"): "crew",
    ("houston", "d"): "dynamo",
    ("inter", "m"): "inter miami",       # "Inter Miami" → city="inter", hint="m"
    ("los angeles", "f"): "lafc",
    ("los angeles", "g"): "galaxy",
    ("nashville", "s"): "nashville sc",
    ("new england", "r"): "revolution",
    ("new york", "c"): "nycfc",
    # ("new york", "r") intentionally absent — NHL "rangers" (defined above) takes precedence
    ("orlando", "c"): "orlando city",
    ("philadelphia", "u"): "union",
    # ("portland", "t") intentionally absent — NBA "trail blazers" (defined above) takes precedence
    ("real", "s"): "real salt lake",     # "Real Salt Lake" → city="real", hint="s"
    ("san jose", "e"): "earthquakes",
    ("seattle", "s"): "sounders",
    ("toronto", "f"): "toronto fc",
    ("vancouver", "w"): "whitecaps",
    # Additional MLS teams missing from original dict
    ("austin", "f"): "austin fc",
    ("cincinnati", "f"): "fc cincinnati",   # ("cincinnati","r")=reds MLB — no conflict
    ("dallas", "f"): "fc dallas",           # "m"=mavs,"c"=cowboys,"s"=stars — "f" free
    ("minnesota", "u"): "minnesota united", # "t"=twins,"v"=vikings,"w"=wild — "u" free
    ("kansas city", "s"): "sporting kc",    # "r"=royals,"c"=chiefs — "s" free
    ("washington", "d"): "dc united",       # "n"=nationals,"w"=wizards,"c"=commanders — "d" free
}

# City-free nickname → team name lookup for teams identified by nickname alone
# (e.g. Kalshi yes_sub_title="A's" with no city prefix).
_KALSHI_NICKNAME_TO_TEAM: dict[str, str] = {
    # MLB
    "as": "athletics",    # "A's" normalises to "as" after apostrophe strip
    "a's": "athletics",
    # Cricket national teams (Kalshi may use full country name without city prefix)
    "india": "india",
    "australia": "australia",
    "england": "england",
    "pakistan": "pakistan",
    "bangladesh": "bangladesh",
    "new zealand": "new zealand",
    "south africa": "south africa",
    "sri lanka": "sri lanka",
    "west indies": "west indies",
    "afghanistan": "afghanistan",
    "zimbabwe": "zimbabwe",
    "ireland": "ireland",
}

# Sports-related categories on Kalshi.
_SPORTS_CATEGORIES = {"sports", "baseball", "basketball", "football", "hockey",
                      "soccer", "tennis", "mma", "cricket", "rugby", "esports"}

# MLB team codes (from Kalshi KXMLBGAME ticker) → canonical lowercase team name.
# The last word of each name is used as the Polymarket search keyword.
_MLB_TEAM_CODES: dict[str, str] = {
    "ARI": "arizona diamondbacks", "AZ": "arizona diamondbacks",
    "ATL": "atlanta braves",
    "BAL": "baltimore orioles",
    "BOS": "boston red sox",
    "CHC": "chicago cubs",
    "CWS": "chicago white sox",
    "CIN": "cincinnati reds",
    "CLE": "cleveland guardians",
    "COL": "colorado rockies",
    "DET": "detroit tigers",
    "HOU": "houston astros",
    "KC":  "kansas city royals", "KCR": "kansas city royals",
    "LAA": "los angeles angels",
    "LAD": "los angeles dodgers",
    "MIA": "miami marlins",
    "MIL": "milwaukee brewers",
    "MIN": "minnesota twins",
    "NYM": "new york mets",
    "NYY": "new york yankees",
    "OAK": "oakland athletics", "ATH": "athletics",
    "PHI": "philadelphia phillies",
    "PIT": "pittsburgh pirates",
    "SD":  "san diego padres", "SDP": "san diego padres",
    "SEA": "seattle mariners",
    "SF":  "san francisco giants", "SFG": "san francisco giants",
    "STL": "st. louis cardinals",
    "TB":  "tampa bay rays", "TBR": "tampa bay rays",
    "TEX": "texas rangers",
    "TOR": "toronto blue jays",
    "WSH": "washington nationals",
}


def _parse_mlb_game_codes(ticker: str) -> tuple[str, str] | None:
    """Extract (yes_team_code, no_team_code) from a KXMLBGAME ticker.

    Format: KXMLBGAME-{YYMMMDD}{HHMM}{T1}{T2}-{YES_CODE}
    e.g. 'KXMLBGAME-26JUN231840NYYDET-NYY' → ('NYY', 'DET')
         (year=2026, month=JUN, day=23, time=1840, teams=NYY+DET, yes=NYY)
    Returns None if the ticker isn't a recognised MLB game format.
    """
    m = re.match(r"KXMLBGAME-\d{2}[A-Z]{3}\d{2}\d{4}([A-Z]+)-([A-Z]+)$", ticker)
    if not m:
        return None
    combined, yes_code = m.group(1), m.group(2)
    if combined.startswith(yes_code):
        no_code = combined[len(yes_code):]
    elif combined.endswith(yes_code):
        no_code = combined[: -len(yes_code)]
    else:
        return None
    return (yes_code, no_code) if no_code else None


_MONTH_MAP = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def _parse_mlb_game_date(ticker: str):
    """Extract the game date (UTC midnight) from a KXMLBGAME ticker.

    Kalshi ticker format: KXMLBGAME-{YYMMMDD}{HHMM}...
    'KXMLBGAME-26JUN211410...' → datetime(2026, 6, 21, tzinfo=utc)
    Returns None if the ticker doesn't match the expected format.
    """
    m = re.match(r"KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})\d{4}", ticker)
    if not m:
        return None
    yr, mon, day = int(m.group(1)), _MONTH_MAP.get(m.group(2)), int(m.group(3))
    if not mon:
        return None
    try:
        from datetime import datetime as _dt
        return _dt(2000 + yr, mon, day, tzinfo=timezone.utc)
    except ValueError:
        return None


# Unified exclusion pattern for Polymarket prop/spread markets.
# Applied consistently across ALL three index builders (_build_poly_index,
# _build_city_index, _build_token_city_index) to prevent run-line / O-U /
# prop markets from being matched against Kalshi moneyline markets.
_POLY_PROP_EXCLUSION_RE = re.compile(
    r"(spread|\bo/u\b|over/under|innings|inning|strikeout|home run|"
    r"extra innings|nrfi|run scored|run line|\bou\b|total runs|"
    r"first \d|player prop|\+\d\.5|\-\d\.5|"
    r"exact score|first goal scorer|"
    r"both teams|btts|total goals?|clean sheet|first goal|anytime|"
    r"corners?|yellow card|red card|to score|\d+\.?\d*\s*goals?|"
    r"wickets?|run[s]?\s+total|top\s+batter|top\s+bowler|man\s+of\s+the\s+match|"
    r"major league cricket)",
    re.IGNORECASE,
)

# Kalshi prop/spread markets whose yes_sub_title reveals a non-moneyline contract.
# These should never be city-pair matched against Polymarket winner markets.
# Checked against (yes_sub_title + title) to catch props in either field.
_KALSHI_PROP_RE = re.compile(
    r"(by more than|by at least|wins by|clean sheet|first goal|anytime|"
    r"\d+\.?\d*\s*goals?|total goals?|spread|\+\d|\-\d|"
    r"both teams|btts|over\s+\d|under\s+\d|corners?|yellow card|red card|"
    # Cricket props
    r"wickets?|run[s]?\s+total|century|centuries|top\s+batter|top\s+bowler|"
    r"first\s+wicket|man\s+of\s+the\s+match|highest\s+score)",
    re.IGNORECASE,
)

# Tournament-progression markets ("Will X advance from Group Stage?") — checked against
# yes_sub_title ONLY.  Game event titles legitimately contain "Group Stage" or "Round of 16"
# as contextual labels; filtering on the full title would drop all group-stage game markets.
_KALSHI_TOURNAMENT_PROP_RE = re.compile(
    r"(advance|qualify|reach\s+the|make\s+the|group\s+stage|round\s+of)",
    re.IGNORECASE,
)


def _normalise_city(text: str) -> str:
    """Lowercase + strip punctuation for city comparison."""
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def _extract_vs_sides(text: str) -> tuple[str, str] | None:
    """Split 'Team A vs[.] Team B [Winner?]' into (side_a, side_b)."""
    # Remove trailing "Winner?" or "winner" and whitespace
    cleaned = re.sub(r"\s*winner\s*\??$", "", text, flags=re.IGNORECASE).strip()
    # Split on ' vs ', ' vs. ', ' – ', ' — ', ' - ', or ' @ ' (away @ home format)
    parts = re.split(r"\s+(?:vs\.?|–|—|-|@)\s+", cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return None


def _side_to_city(side: str) -> str:
    """Extract the city portion from a team name like 'Cincinnati Reds' → 'cincinnati'.

    Handles multi-word cities ('New York Yankees' → 'new york') and
    Kalshi abbreviations ('New York M' → 'new york').
    """
    norm = _normalise_city(side)
    # Check for known multi-word cities first.
    for mc in _MULTI_WORD_CITIES:
        if norm.startswith(mc):
            return mc
    # Fall back to first word (handles single-word cities like 'Boston', 'Philadelphia').
    words = norm.split()
    return words[0] if words else norm


def _side_to_team_hint(side: str) -> str:
    """Extract team-name hint from a side string.

    'Cincinnati Reds' → 'reds'
    'New York M'      → 'm'   (Kalshi abbreviation)
    'Philadelphia'    → ''    (no team name, just city)
    """
    norm = _normalise_city(side)
    city = _side_to_city(side)
    remainder = norm[len(city):].strip()
    return remainder


def _resolve_kalshi_team(city: str, hint: str) -> str:
    """Resolve a Kalshi abbreviated team hint to a full team name if possible.

    ('new york', 'm') → 'mets'
    ('boston', 'red sox') → 'red sox'  (already full)
    ('philadelphia', '') → ''  (city-only, can't resolve)
    """
    if len(hint) <= 1 and hint:
        resolved = _KALSHI_ABBREV_TO_TEAM.get((city, hint), "")
        if resolved:
            return resolved
    return hint


def _cities_from_text(text: str) -> frozenset[str]:
    """Extract a set of city names from a 'X vs Y' text."""
    sides = _extract_vs_sides(text)
    if not sides:
        return frozenset()
    return frozenset((_side_to_city(sides[0]), _side_to_city(sides[1])))


def _sports_match_confidence(
    poly_q: str, kalshi_title: str,
    poly_end: "Optional[object]" = None,
    kalshi_close: "Optional[object]" = None,
) -> float:
    """Match sports markets by city pairs + optional date proximity.

    Returns 0.85-0.95 if both city names match (boosted by date proximity),
    0.0 otherwise.
    """
    poly_cities = _cities_from_text(poly_q)
    kalshi_cities = _cities_from_text(kalshi_title)
    if len(poly_cities) < 2 or len(kalshi_cities) < 2:
        return 0.0
    if poly_cities != kalshi_cities:
        return 0.0

    # Both city pairs match — base confidence 0.85
    conf = 0.85

    # Date proximity: boost if within 36h, hard-reject if > 96h.
    # Kalshi close_time = game start; Poly end_date = resolution, often 2-4 days later.
    # 60h was too tight and rejected valid same-game pairs where Poly settles conservatively
    # (e.g. NBA/NHL playoff markets set to resolve Thursday for a Tuesday game = 48-72h gap).
    # 96h (4 days) gives enough room for conservative Poly settlement dates while still
    # rejecting games that are genuinely in a different series slot (>4 days apart).
    if poly_end and kalshi_close:
        try:
            pe = poly_end if poly_end.tzinfo else poly_end.replace(tzinfo=timezone.utc)
            kc = kalshi_close if kalshi_close.tzinfo else kalshi_close.replace(tzinfo=timezone.utc)
            gap_h = abs((pe - kc).total_seconds()) / 3600
            if gap_h <= 36:
                conf = 0.95
            elif gap_h > 96:
                return 0.0  # dates too far apart — different game entirely
        except Exception:
            pass

    return conf


def _align_poly_token_to_kalshi(
    pm: Market, km: KalshiMarket
) -> tuple[float, float, str] | None:
    """Return (poly_aligned_price, kalshi_yes_price, poly_outcome_label) with same-team alignment.

    For sports markets where Polymarket outcomes are team names (not Yes/No),
    we need to figure out which Poly token corresponds to Kalshi's YES side
    so the arb calculation compares apples to apples.

    Uses km.yes_sub_title (the Kalshi YES-side label, e.g. "New York M") as
    the primary source for identifying the team.  Falls back to parsing
    the first side of the "vs" title if yes_sub_title is empty.

    Returns None if alignment can't be determined (falls back to default).
    """
    if len(pm.tokens) < 2:
        return None

    # Determine Kalshi YES side from yes_sub_title (authoritative) or title fallback
    yes_label = km.yes_sub_title.strip() if km.yes_sub_title else ""
    if not yes_label:
        # Fallback: guess from title (only works for the first side of "vs")
        kalshi_sides = _extract_vs_sides(km.title)
        if not kalshi_sides:
            return None
        yes_label = kalshi_sides[0]

    kalshi_yes_city = _side_to_city(yes_label)
    kalshi_hint = _side_to_team_hint(yes_label)
    kalshi_team = _resolve_kalshi_team(kalshi_yes_city, kalshi_hint)

    poly_outcomes = [(t.outcome, t.price) for t in pm.tokens]

    # Strategy 1: match by city name
    for outcome, price in poly_outcomes:
        if _side_to_city(outcome) == kalshi_yes_city:
            # If city matches but there are multiple teams from the same city,
            # verify by team name if available
            if kalshi_team:
                if kalshi_team in _normalise_city(outcome):
                    return (price, km.yes_price, outcome)
                # Same city but different team — keep looking
                continue
            return (price, km.yes_price, outcome)

    # Strategy 2: match by resolved team name alone (handles edge cases)
    if kalshi_team:
        for outcome, price in poly_outcomes:
            if kalshi_team in _normalise_city(outcome):
                return (price, km.yes_price, outcome)

    # Strategy 3: nickname lookup then substring match.
    #
    # Some Kalshi yes_sub_titles are pure nicknames without a city prefix
    # (e.g. "A's" → normalised "as").  Two-step resolution:
    #   a) Check _KALSHI_NICKNAME_TO_TEAM for known city-free nicknames and
    #      search Poly outcomes for the resolved full name.
    #   b) If the raw label is ≥4 chars (long enough to be unambiguous), try
    #      it as a direct substring of each Poly outcome — and vice versa.
    #      The 4-char floor prevents "as" (from "A's") from matching "los angeles".
    raw_label = re.sub(r"[^a-z0-9 ]", "", yes_label.lower()).strip()

    # 3a: explicit nickname map
    resolved_nickname = _KALSHI_NICKNAME_TO_TEAM.get(raw_label) or _KALSHI_NICKNAME_TO_TEAM.get(yes_label.lower())
    if resolved_nickname:
        for outcome, price in poly_outcomes:
            if resolved_nickname in re.sub(r"[^a-z0-9 ]", "", outcome.lower()):
                return (price, km.yes_price, outcome)

    # 3b: substring fallback (only for labels ≥6 chars to avoid false matches).
    # 4-char floor was too low: short soccer team names like "Fire", "Real", "City"
    # would cross-match unrelated teams in different leagues via substring.
    # 6-char floor keeps full names (e.g. "Angels", "Royals") while blocking short ones.
    if len(raw_label) >= 6:
        for outcome, price in poly_outcomes:
            norm_outcome = re.sub(r"[^a-z0-9 ]", "", outcome.lower()).strip()
            if len(norm_outcome) < 6:
                continue
            if raw_label in norm_outcome or norm_outcome in raw_label:
                return (price, km.yes_price, outcome)

    return None


# -- Intra-market arb (YES_ask + NO_ask < 1) --

class IntraMarketArbDetector:
    """Detects intra-market arbitrage: YES_ask + NO_ask < 1 - fee."""

    def __init__(self, client: PolymarketPublicClient, config: Config) -> None:
        self._client = client
        self._config = config

    def scan(self, markets: list[Market]) -> list[ArbOpportunity]:
        candidates = [m for m in markets if m.volume >= 500]
        logger.debug("Intra-arb scan: checking %d/%d markets", len(candidates), len(markets))

        opportunities: list[ArbOpportunity] = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(self._check, m): m for m in candidates}
            for fut in as_completed(futures):
                try:
                    opp = fut.result()
                    if opp is not None:
                        opportunities.append(opp)
                except Exception as exc:
                    logger.debug("Intra-arb check error: %s", exc)

        return sorted(opportunities, key=lambda o: o.net_profit_pct, reverse=True)

    def _check(self, market: Market) -> Optional[ArbOpportunity]:
        yes_tok = market.yes_token
        no_tok = market.no_token
        if not yes_tok or not no_tok:
            return None

        try:
            yes_book = self._client.get_orderbook(yes_tok.token_id)
            no_book = self._client.get_orderbook(no_tok.token_id)
        except Exception as exc:
            logger.debug("Orderbook fetch failed for %s: %s", market.condition_id, exc)
            return None

        yes_ask = yes_book.best_ask
        no_ask = no_book.best_ask
        if yes_ask is None or no_ask is None:
            return None

        combined = yes_ask + no_ask
        net = 1.0 - combined * (1.0 + self._config.fee_rate)

        if net < -self._config.min_arb_profit_pct:
            return None

        return ArbOpportunity(
            market=market,
            yes_ask=yes_ask,
            no_ask=no_ask,
            combined_cost=combined,
            net_profit_pct=net,
            yes_ask_liquidity=yes_book.ask_liquidity,
            no_ask_liquidity=no_book.ask_liquidity,
        )

    def score(self, opp: ArbOpportunity) -> float:
        if not opp.is_profitable:
            return max(0.0, (opp.net_profit_pct + self._config.min_arb_profit_pct) /
                       self._config.min_arb_profit_pct * 10)
        return min(50.0, opp.net_profit_pct / 0.05 * 50)


# -- Cross-platform arb (Polymarket vs Kalshi) --

def _keywords(text: str) -> set[str]:
    tokens = re.findall(r"[a-z]{3,}", text.lower())
    return {t for t in tokens if t not in _STOPWORDS}


def _match_confidence(poly_q: str, kalshi_q: str) -> float:
    pk = _keywords(poly_q)
    kk = _keywords(kalshi_q)
    if not pk or not kk:
        return 0.0
    base = len(pk & kk) / len(pk | kk)
    # Penalize mismatched numeric tokens (e.g. GDP 2024 vs GDP 2025, party vs score)
    poly_nums = set(re.findall(r'\d+\.?\d*', poly_q))
    kalshi_nums = set(re.findall(r'\d+\.?\d*', kalshi_q))
    if poly_nums and kalshi_nums and not (poly_nums & kalshi_nums):
        base *= 0.3
    return base


class CrossPlatformArbScanner:
    """Finds price discrepancies between matched Polymarket and Kalshi markets.

    Uses three matching strategies:
    - **Sports (head-to-head)**: city-pair matching on 'A vs B' question format.
    - **Sports (single-team)**: matches Kalshi's YES-side city against Polymarket
      binary markets where the YES token outcome IS the team name (e.g. "Cubs win?").
    - **Non-sports**: keyword Jaccard similarity (original approach).
    """

    MATCH_THRESHOLD = 0.45
    SPORTS_MATCH_THRESHOLD = 0.80  # city-pair matches score >= 0.85
    SINGLE_TEAM_THRESHOLD = 0.79   # require date proximity (boosts to 0.80) to avoid cross-sport city matches

    def __init__(self, config: Config) -> None:
        self._config = config
        self.last_run_stats: dict = {}  # populated after each find_opportunities call

    def find_opportunities(
        self,
        poly_markets: list[Market],
        kalshi_markets: list[KalshiMarket],
    ) -> list[ArbitrageOpportunity]:
        threshold = getattr(self._config, "arb_match_threshold", self.MATCH_THRESHOLD)
        kw_index = self._build_poly_index(poly_markets)
        city_index = self._build_city_index(poly_markets)
        token_city_index = self._build_token_city_index(poly_markets)
        opportunities: list[ArbitrageOpportunity] = []

        _stats: dict = {
            "kalshi_total": len(kalshi_markets),
            "poly_total": len(poly_markets),
            "skipped_past": 0,
            "skipped_invalid_price": 0,
            "skipped_price_sum_low": 0,
            "skipped_prop": 0,
            "skipped_no_poly_match": 0,
            "skipped_subtitle_mismatch": 0,
            "skipped_alignment_failed": 0,
            "skipped_settled": 0,
            "skipped_mlb_date": 0,
            "skipped_date_gap": 0,
            "matched_pairs": 0,
            "opportunities": 0,
        }

        for km in kalshi_markets:
            if km.time_category == "past":
                _stats["skipped_past"] += 1
                continue
            if km.yes_price <= 0 or km.no_price <= 0:
                _stats["skipped_invalid_price"] += 1
                continue
            # Skip multi-outcome tournament markets (e.g. "Will X win the WC?")
            # where each team has its own YES price and no_price is another winner
            # price rather than the binary complement.  In a proper binary market,
            # yes + no must exceed 0.5 even with wide spreads.
            if km.yes_price + km.no_price < 0.5:
                _stats["skipped_price_sum_low"] += 1
                logger.debug("ARB skip [price_sum_low] %s yes=%.3f no=%.3f sum=%.3f",
                             km.ticker, km.yes_price, km.no_price,
                             km.yes_price + km.no_price)
                continue

            is_sports = km.category.lower() in _SPORTS_CATEGORIES

            # Skip Kalshi spread/prop markets — yes_sub_title reveals the contract type
            # (e.g. "Sweden wins by more than 2.5 goals" on a "Netherlands vs. Sweden"
            # title).  City-pair matching would score these 0.85 against ANY same-game
            # Poly market regardless of market type, producing false arbs.
            if is_sports:
                prop_text = (km.yes_sub_title or "") + " " + km.title
                if _KALSHI_PROP_RE.search(prop_text):
                    _stats["skipped_prop"] += 1
                    logger.debug("ARB skip [prop] %s subtitle=%r", km.ticker, km.yes_sub_title)
                    continue
                # Tournament-progression patterns are checked against yes_sub_title ONLY.
                # Event titles for game markets legitimately contain "Group Stage" /
                # "Round of 16" as context labels — checking the full title would
                # incorrectly drop all World Cup / playoff game markets.
                if _KALSHI_TOURNAMENT_PROP_RE.search(km.yes_sub_title or ""):
                    _stats["skipped_prop"] += 1
                    logger.debug("ARB skip [tournament_prop] %s subtitle=%r", km.ticker, km.yes_sub_title)
                    continue

            # Try sports matching first for sports-category markets
            best_match: Optional[Market] = None
            best_conf = 0.0
            sports_matched = False

            single_team_match = False
            if is_sports:
                best_match, best_conf = self._best_sports_match(km, city_index)
                if best_match and best_conf >= self.SPORTS_MATCH_THRESHOLD:
                    sports_matched = True

            # Fallback: single-team binary markets ("Will the Cubs win?")
            if is_sports and not sports_matched:
                st_match, st_conf = self._best_sports_match_single_team(km, token_city_index)
                if st_match and st_conf >= self.SINGLE_TEAM_THRESHOLD:
                    best_match = st_match
                    best_conf = st_conf
                    sports_matched = True
                    single_team_match = True

            # Fall back to keyword Jaccard
            if not sports_matched:
                best_match, best_conf = self._best_poly_match_indexed(km, kw_index)
                if best_match is None or best_conf < threshold:
                    _stats["skipped_no_poly_match"] += 1
                    logger.debug("ARB skip [no_match] %s best_conf=%.2f threshold=%.2f", km.ticker, best_conf or 0.0, threshold)
                    continue
                # For non-sports markets, if yes_sub_title names a specific entity
                # (person, candidate, country), the FULL normalised subtitle must
                # appear as a phrase in the matched Poly question.  Word-level checks
                # fail when the subtitle contains ambiguous words that are also part
                # of the question for a different reason (e.g. "Israel" in
                # "Israel Katz" matching "Prime Minister of Israel").
                if not is_sports and km.yes_sub_title:
                    sub_norm = re.sub(r"[^a-z0-9 ]", " ", km.yes_sub_title.lower()).strip()
                    sub_words = sub_norm.split()
                    is_meaningful = len(sub_words) >= 2 or (
                        len(sub_words) == 1 and len(sub_words[0]) >= 5
                    )
                    if is_meaningful:
                        poly_q_norm = re.sub(r"[^a-z0-9 ]", " ", best_match.question.lower())
                        if sub_norm not in poly_q_norm:
                            _stats["skipped_subtitle_mismatch"] += 1
                            logger.debug("ARB skip [subtitle] %s subtitle=%r not in poly %r", km.ticker, km.yes_sub_title, best_match.question[:60])
                            continue
                # For sports Kalshi markets that fell through to Jaccard, reject if
                # event dates are too far apart — prevents World Cup winner markets
                # from matching Poly draw/prop markets for the same teams.
                if is_sports and best_match.end_date and km.close_time:
                    try:
                        pe = best_match.end_date if best_match.end_date.tzinfo else best_match.end_date.replace(tzinfo=timezone.utc)
                        kc = km.close_time if km.close_time.tzinfo else km.close_time.replace(tzinfo=timezone.utc)
                        if abs((pe - kc).total_seconds()) > 120 * 3600:
                            continue
                    except Exception:
                        pass
                # For non-sports markets, reject if resolution dates differ by > 365 days.
                # Catches tickers that share a name but resolve at completely different times
                # (e.g. KXNEXTISRAELPM closing 2045 matched to a Poly market closing 2026).
                if not is_sports and best_match.end_date and km.close_time:
                    try:
                        pe = best_match.end_date if best_match.end_date.tzinfo else best_match.end_date.replace(tzinfo=timezone.utc)
                        kc = km.close_time if km.close_time.tzinfo else km.close_time.replace(tzinfo=timezone.utc)
                        if abs((pe - kc).total_seconds()) > 365 * 24 * 3600:
                            _stats["skipped_date_gap"] += 1
                            logger.debug("ARB skip [date_gap] %s gap_days=%.0f", km.ticker, abs((pe - kc).total_seconds()) / 86400)
                            continue
                    except Exception:
                        pass

            if best_match is None:
                continue
            if best_match.closed or not best_match.active:
                continue
            if best_match.time_category == "past":
                continue

            yes_tok = best_match.yes_token
            no_tok = best_match.no_token
            if not yes_tok or not no_tok:
                continue

            # Skip Poly markets where either token is priced below 2¢ — that
            # indicates the market has already resolved or is essentially settled.
            # A 0.7¢ price on an "upcoming" game means a previous game in the same
            # series already resolved and the matcher picked the wrong Poly market.
            if yes_tok.price < 0.02 or no_tok.price < 0.02:
                _stats["skipped_settled"] += 1
                logger.debug("ARB skip [settled] %s poly %r yes=%.3f no=%.3f", km.ticker, best_match.question[:50], yes_tok.price, no_tok.price)
                continue

            # For MLB Kalshi markets, verify the Poly end_date is consistent with
            # the actual game date encoded in the ticker.  The ticker KXMLBGAME-26JUN21…
            # tells us the game is June 21; if Poly's end_date is > 14 days after the
            # game date the market is almost certainly for a different game in the series.
            if is_sports:
                game_dt = _parse_mlb_game_date(km.ticker)
                if game_dt and best_match.end_date:
                    pe = best_match.end_date if best_match.end_date.tzinfo else best_match.end_date.replace(tzinfo=timezone.utc)
                    days_after_game = (pe - game_dt).total_seconds() / 86400
                    # Poly end_date must be on or after the game date and within 7 days.
                    # MLB games settle 1-2 days after the game, but Poly markets are often
                    # set conservatively (4-7 days out). >7 days means a different game in
                    # the same series was matched. Markets ending before game_dt are past.
                    if not (0 <= days_after_game <= 7):
                        _stats["skipped_mlb_date"] += 1
                        logger.debug("ARB skip [mlb_date] %s days_after_game=%.1f", km.ticker, days_after_game)
                        continue

            _stats["matched_pairs"] += 1
            logger.debug("ARB match %s → poly %r conf=%.2f", km.ticker, best_match.question[:60], best_conf)
            fee = self._config.fee_rate
            end_date = best_match.end_date
            time_cat = best_match.time_category

            # For sports matches, align team prices to avoid false arbs.
            # Kalshi YES = specific team wins; we need the matching Poly token.
            aligned = None
            poly_same_label = ""  # outcome name of the "same team" Poly token
            if single_team_match:
                # Poly YES token == Kalshi YES team (single-team binary market)
                yes_tok_price = best_match.yes_token.price if best_match.yes_token else 0.0
                yes_tok_outcome = best_match.yes_token.outcome if best_match.yes_token else ""
                if yes_tok_price > 0:
                    aligned = (yes_tok_price, km.yes_price, yes_tok_outcome)
            elif sports_matched or is_sports:
                # Always attempt alignment for sports Kalshi markets — even ones that
                # fell through to Jaccard matching.  Nickname-only labels like "A's"
                # (← Athletics) have no extractable city so they fail city-pair and
                # single-team matching, but Jaccard finds the right Poly market.
                # Without alignment the non-sports YES/NO formula runs and produces
                # false arbs (e.g. Poly Angels YES + Kalshi A's NO both pay if
                # Angels win — it's a correlated double bet, not an arb).
                aligned = _align_poly_token_to_kalshi(best_match, km)

            # Kalshi YES-side label for display (expanded abbreviations)
            kalshi_yes_label = km.yes_sub_title.strip() if km.yes_sub_title else ""

            # Safety net: if Kalshi market is sports but alignment couldn't determine
            # which Poly token corresponds to the Kalshi YES team, SKIP entirely.
            # The non-sports YES/NO formula is structurally wrong for sports markets
            # where each platform may use a different team as its YES side.
            # "sports_matched" alone is insufficient — alignment must also be gated
            # on is_sports so that Jaccard-matched sports markets are protected too.
            if is_sports and aligned is None:
                _stats["skipped_alignment_failed"] += 1
                logger.debug("ARB skip [alignment] %s no poly token matches Kalshi YES label %r", km.ticker, km.yes_sub_title)
                continue

            if aligned:
                # aligned = (poly_same_team_price, kalshi_yes_price, poly_outcome_label)
                poly_same, k_yes, poly_same_label = aligned
                # Determine the other Poly token's outcome name
                poly_opp_label = next(
                    (t.outcome for t in best_match.tokens if t.outcome != poly_same_label), ""
                )
                _poly_opp_token = next(
                    (t for t in best_match.tokens if t.outcome != poly_same_label), None
                )
                poly_opp = _poly_opp_token.price if _poly_opp_token else 1.0 - poly_same
                k_no = km.no_price

                # Determine whether poly_same is the YES or NO token so action
                # labels are correct.  When Kalshi's YES team is the NO-side on
                # Polymarket the action must read "BUY NO", not "BUY YES".
                _poly_yes_tok = best_match.yes_token
                _poly_same_is_yes = bool(
                    _poly_yes_tok and _poly_yes_tok.outcome == poly_same_label
                )

                # Strategy 1: buy same-team on Poly + buy NO on Kalshi
                cost1 = poly_same + k_no
                if cost1 * (1 + fee) < 1.0:
                    roi = 1.0 / (cost1 * (1 + fee)) - 1.0
                    if roi >= self._config.arb_min_roi:
                        opportunities.append(ArbitrageOpportunity(
                            question=best_match.question,
                            poly_ticker=best_match.condition_id,
                            kalshi_ticker=km.ticker,
                            poly_action="BUY YES" if _poly_same_is_yes else "BUY NO",
                            kalshi_action="BUY NO",
                            poly_price=round(poly_same, 4),
                            kalshi_price=round(k_no, 4),
                            roi_pct=round(roi * 100, 2),
                            arb_type="TRUE_ARB",
                            match_confidence=round(best_conf, 3),
                            poly_end_date=end_date,
                            kalshi_close_time=km.close_time,
                            time_category=time_cat,
                            poly_outcome=poly_same_label,
                            kalshi_outcome=kalshi_yes_label,
                        ))
                        continue

                # Strategy 2: buy other-team on Poly + buy YES on Kalshi
                cost2 = poly_opp + k_yes
                if cost2 * (1 + fee) < 1.0:
                    roi = 1.0 / (cost2 * (1 + fee)) - 1.0
                    if roi >= self._config.arb_min_roi:
                        opportunities.append(ArbitrageOpportunity(
                            question=best_match.question,
                            poly_ticker=best_match.condition_id,
                            kalshi_ticker=km.ticker,
                            poly_action="BUY NO" if _poly_same_is_yes else "BUY YES",
                            kalshi_action="BUY YES",
                            poly_price=round(poly_opp, 4),
                            kalshi_price=round(k_yes, 4),
                            roi_pct=round(roi * 100, 2),
                            arb_type="TRUE_ARB",
                            match_confidence=round(best_conf, 3),
                            poly_end_date=end_date,
                            kalshi_close_time=km.close_time,
                            time_category=time_cat,
                            poly_outcome=poly_opp_label,
                            kalshi_outcome=kalshi_yes_label,
                        ))
                        continue

                # Soft arb: price gap on same team
                gap = abs(poly_same - k_yes)
                if gap >= self._config.arb_soft_min_edge:
                    cheaper_on_poly = poly_same < k_yes
                    opportunities.append(ArbitrageOpportunity(
                        question=best_match.question,
                        poly_ticker=best_match.condition_id,
                        kalshi_ticker=km.ticker,
                        poly_action=(
                            ("BUY YES" if _poly_same_is_yes else "BUY NO") if cheaper_on_poly
                            else ("BUY NO" if _poly_same_is_yes else "BUY YES")
                        ),
                        kalshi_action="BUY NO" if cheaper_on_poly else "BUY YES",
                        poly_price=round(poly_same, 4),
                        kalshi_price=round(k_yes, 4),
                        roi_pct=round(-gap * 100, 2),
                        arb_type="SOFT_ARB",
                        match_confidence=round(best_conf, 3),
                        poly_end_date=end_date,
                        kalshi_close_time=km.close_time,
                        time_category=time_cat,
                        poly_outcome=poly_same_label if cheaper_on_poly else poly_opp_label,
                        kalshi_outcome=kalshi_yes_label,
                    ))

            else:
                # Non-sports or alignment failed — use original YES/NO logic
                poly_yes = yes_tok.price
                poly_no = no_tok.price
                if poly_yes <= 0 or poly_no <= 0:
                    continue

                kalshi_yes = km.yes_price
                kalshi_no = km.no_price

                # Outcome labels for display — skip generic "YES"/"NO" literals
                _generic = {"yes", "no"}
                yes_outcome = yes_tok.outcome if yes_tok.outcome.lower() not in _generic else ""
                no_outcome = no_tok.outcome if no_tok.outcome.lower() not in _generic else ""
                kalshi_yes_label = km.yes_sub_title.strip() if km.yes_sub_title else ""

                arb_yes_poly = poly_yes + kalshi_no
                arb_yes_kalshi = kalshi_yes + poly_no

                if arb_yes_poly * (1 + fee) < 1.0:
                    roi = 1.0 / (arb_yes_poly * (1 + fee)) - 1.0
                    if roi >= self._config.arb_min_roi:
                        opportunities.append(ArbitrageOpportunity(
                            question=best_match.question,
                            poly_ticker=best_match.condition_id,
                            kalshi_ticker=km.ticker,
                            poly_action="BUY YES",
                            kalshi_action="BUY NO",
                            poly_price=round(poly_yes, 4),
                            kalshi_price=round(kalshi_no, 4),
                            roi_pct=round(roi * 100, 2),
                            arb_type="TRUE_ARB",
                            match_confidence=round(best_conf, 3),
                            poly_end_date=end_date,
                            kalshi_close_time=km.close_time,
                            time_category=time_cat,
                            poly_outcome=yes_outcome,
                            kalshi_outcome=kalshi_yes_label,
                        ))
                        continue

                if arb_yes_kalshi * (1 + fee) < 1.0:
                    roi = 1.0 / (arb_yes_kalshi * (1 + fee)) - 1.0
                    if roi >= self._config.arb_min_roi:
                        opportunities.append(ArbitrageOpportunity(
                            question=best_match.question,
                            poly_ticker=best_match.condition_id,
                            kalshi_ticker=km.ticker,
                            poly_action="BUY NO",
                            kalshi_action="BUY YES",
                            poly_price=round(poly_no, 4),
                            kalshi_price=round(kalshi_yes, 4),
                            roi_pct=round(roi * 100, 2),
                            arb_type="TRUE_ARB",
                            match_confidence=round(best_conf, 3),
                            poly_end_date=end_date,
                            kalshi_close_time=km.close_time,
                            time_category=time_cat,
                            poly_outcome=no_outcome,
                            kalshi_outcome=kalshi_yes_label,
                        ))
                        continue

                yes_gap = abs(poly_yes - kalshi_yes)
                if yes_gap >= self._config.arb_soft_min_edge:
                    cheaper_on_poly = poly_yes < kalshi_yes
                    opportunities.append(ArbitrageOpportunity(
                        question=best_match.question,
                        poly_ticker=best_match.condition_id,
                        kalshi_ticker=km.ticker,
                        poly_action="BUY YES" if cheaper_on_poly else "BUY NO",
                        kalshi_action="BUY YES" if not cheaper_on_poly else "BUY NO",
                        poly_price=round(poly_yes, 4),
                        kalshi_price=round(kalshi_yes, 4),
                        roi_pct=round(-yes_gap * 100, 2),
                        arb_type="SOFT_ARB",
                        match_confidence=round(best_conf, 3),
                        poly_end_date=end_date,
                        kalshi_close_time=km.close_time,
                        time_category=time_cat,
                        poly_outcome=yes_outcome if cheaper_on_poly else no_outcome,
                        kalshi_outcome=kalshi_yes_label,
                    ))

        # One Kalshi market can match multiple Poly markets via different paths.
        # Keep only the highest-confidence Poly match per Kalshi ticker.
        seen_k: dict[str, ArbitrageOpportunity] = {}
        for opp in opportunities:
            existing = seen_k.get(opp.kalshi_ticker)
            if existing is None or opp.match_confidence > existing.match_confidence:
                seen_k[opp.kalshi_ticker] = opp
        opportunities = list(seen_k.values())

        # Also dedup by Poly market: same Poly market matched to N Kalshi markets
        # (e.g., same spread offered as multiple Kalshi tickers) → keep best one.
        # Priority: TRUE_ARB over SOFT_ARB, then highest |roi_pct|.
        seen_p: dict[str, ArbitrageOpportunity] = {}
        for opp in opportunities:
            existing = seen_p.get(opp.poly_ticker)
            if existing is None:
                seen_p[opp.poly_ticker] = opp
            elif opp.arb_type == "TRUE_ARB" and existing.arb_type != "TRUE_ARB":
                seen_p[opp.poly_ticker] = opp
            elif opp.arb_type == existing.arb_type and abs(opp.roi_pct) > abs(existing.roi_pct):
                seen_p[opp.poly_ticker] = opp
        opportunities = list(seen_p.values())
        _stats["opportunities"] = len(opportunities)
        logger.info(
            "ARB scan: %d Kalshi × %d Poly → %d matched, %d opps "
            "(skipped: no_match=%d prop=%d subtitle=%d align=%d settled=%d price_sum=%d mlb_date=%d)",
            _stats["kalshi_total"], _stats["poly_total"],
            _stats["matched_pairs"], _stats["opportunities"],
            _stats["skipped_no_poly_match"], _stats["skipped_prop"],
            _stats["skipped_subtitle_mismatch"], _stats["skipped_alignment_failed"],
            _stats["skipped_settled"], _stats["skipped_price_sum_low"],
            _stats["skipped_mlb_date"],
        )
        self.last_run_stats = _stats

        true_arbs = sorted(
            [o for o in opportunities if o.arb_type == "TRUE_ARB"],
            key=lambda o: o.roi_pct, reverse=True,
        )
        soft_arbs = sorted(
            [o for o in opportunities if o.arb_type == "SOFT_ARB"],
            key=lambda o: abs(o.roi_pct), reverse=True,
        )
        return true_arbs + soft_arbs

    def _build_poly_index(self, poly_markets: list[Market]) -> dict[str, list[Market]]:
        """Build keyword -> [Market] inverted index for O(k) candidate lookup.

        Excludes spread, O/U, and prop markets — Kalshi's sports markets are all
        moneylines, so matching a Poly spread to a Kalshi winner market is a false
        arb (different contracts, structurally different prices).
        """
        index: dict[str, list[Market]] = defaultdict(list)
        for pm in poly_markets:
            if _POLY_PROP_EXCLUSION_RE.search(pm.question):
                continue
            for kw in _keywords(pm.question):
                index[kw].append(pm)
        return index

    def _build_city_index(self, poly_markets: list[Market]) -> dict[frozenset[str], list[Market]]:
        """Build city-pair -> [Market] index for sports matching.

        Only indexes markets whose question looks like 'Team A vs Team B'
        (moneyline-style).  Spread / O-U / run-line markets contain extra tokens
        that pollute city extraction and aren't comparable to Kalshi's binary
        winner markets — matching them produces structurally wrong prices.
        """
        index: dict[frozenset[str], list[Market]] = defaultdict(list)
        for pm in poly_markets:
            if _POLY_PROP_EXCLUSION_RE.search(pm.question):
                continue
            cities = _cities_from_text(pm.question)
            if len(cities) >= 2:
                index[cities].append(pm)
        return index

    def _best_sports_match(
        self, km: KalshiMarket, city_index: dict[frozenset[str], list[Market]]
    ) -> tuple[Optional[Market], float]:
        """Find best Poly match for a sports Kalshi market using city-pair lookup."""
        kalshi_cities = _cities_from_text(km.title)
        if len(kalshi_cities) < 2:
            return None, 0.0

        candidates = city_index.get(kalshi_cities, [])
        if not candidates:
            return None, 0.0

        # For MLB game tickers, narrow to the exact team matchup using the
        # 2-3 letter codes embedded in the ticker (e.g. BALLAA → BAL vs LAA).
        # City pairs like {los angeles, baltimore} match both Angels and Dodgers;
        # the ticker codes are ground truth and resolve the ambiguity.
        mlb_codes = _parse_mlb_game_codes(km.ticker)
        if mlb_codes:
            yes_code, no_code = mlb_codes
            yes_name = _MLB_TEAM_CODES.get(yes_code, "")
            no_name = _MLB_TEAM_CODES.get(no_code, "")
            if yes_name and no_name:
                yes_kw = yes_name.split()[-1]  # "angels", "dodgers", "guardians" …
                no_kw = no_name.split()[-1]
                precise = [
                    pm for pm in candidates
                    if yes_kw in pm.question.lower() and no_kw in pm.question.lower()
                ]
                if precise:
                    candidates = precise  # Narrowed to exact matchup only

        # For MLB, if multiple candidates remain, prefer the one whose Poly end_date
        # is closest to game_date + 3 days (typical MLB settlement lag).
        # This prevents picking a resolved previous-series game over the upcoming one.
        game_dt = _parse_mlb_game_date(km.ticker)
        if game_dt and len(candidates) > 1:
            def _date_score(pm: Market) -> float:
                if not pm.end_date:
                    return 999.0
                pe = pm.end_date if pm.end_date.tzinfo else pm.end_date.replace(tzinfo=timezone.utc)
                days = (pe - game_dt).total_seconds() / 86400
                return abs(days - 3)  # Target: ~3 days after game
            candidates = sorted(candidates, key=_date_score)

        best: Optional[Market] = None
        best_conf = 0.0
        for pm in candidates:
            conf = _sports_match_confidence(
                pm.question, km.title,
                poly_end=pm.end_date,
                kalshi_close=km.close_time,
            )
            if conf > best_conf:
                best_conf = conf
                best = pm
        return best, best_conf

    def _best_poly_match_indexed(
        self, km: KalshiMarket, index: dict[str, list[Market]]
    ) -> tuple[Optional[Market], float]:
        """Find best Poly match using inverted index -- only computes Jaccard on candidates."""
        candidates: dict[str, Market] = {}
        for kw in _keywords(km.title):
            for pm in index.get(kw, []):
                candidates[pm.condition_id] = pm
        best: Optional[Market] = None
        best_conf = 0.0
        for pm in candidates.values():
            conf = _match_confidence(pm.question, km.title)
            if conf > best_conf:
                best_conf = conf
                best = pm
        return best, best_conf

    def _build_token_city_index(self, poly_markets: list[Market]) -> dict[str, list[Market]]:
        """Build city -> [Market] index from YES-token outcome names.

        Captures Polymarket binary win markets like "Will the Cubs win?" where
        the YES token outcome is the team name ("Chicago Cubs") — these aren't
        captured by _build_city_index because the question has no 'vs' separator.

        Excludes spread/O-U/prop markets — single-team city matching fires on
        same-city teams in different sports (e.g., Mariners spread → Seahawks NFL),
        and spread prices are structurally incomparable to Kalshi moneylines.
        """
        index: dict[str, list[Market]] = defaultdict(list)
        for pm in poly_markets:
            if _POLY_PROP_EXCLUSION_RE.search(pm.question):
                continue
            # Index by city for ALL non-Yes/No outcome tokens so both teams in a
            # "Cubs vs Mets" binary are discoverable regardless of which token is tokens[0].
            indexed_cities: set[str] = set()
            for tok in pm.tokens:
                if not tok.outcome or tok.outcome.lower() in ("yes", "no", "1", "0", "true", "false"):
                    continue
                city = _side_to_city(tok.outcome)
                if city and len(city) >= 3 and city not in indexed_cities:
                    index[city].append(pm)
                    indexed_cities.add(city)
        return index

    def _best_sports_match_single_team(
        self, km: KalshiMarket, token_city_index: dict[str, list[Market]]
    ) -> tuple[Optional[Market], float]:
        """Match a Kalshi sports market to a single-team Polymarket binary market.

        Extracts the Kalshi YES-side city (from yes_sub_title or the first side of
        the 'vs' title) and looks for Polymarket markets whose YES token outcome city
        matches.  Confidence 0.80 when close times are within 36h, 0.70 otherwise.

        For MLB game tickers, also filters by the full team nickname (e.g. "yankees"
        not just "new york") and verifies the opposing team appears in the Poly
        question — preventing Yankees→Mets confusion and wrong-series matches.
        """
        yes_label = km.yes_sub_title.strip() if km.yes_sub_title else ""
        if not yes_label:
            sides = _extract_vs_sides(km.title)
            if sides:
                yes_label = sides[0]
        if not yes_label:
            return None, 0.0

        kalshi_yes_city = _side_to_city(yes_label)
        if not kalshi_yes_city or len(kalshi_yes_city) < 3:
            return None, 0.0

        candidates = token_city_index.get(kalshi_yes_city, [])
        if not candidates:
            return None, 0.0

        # For MLB tickers, narrow candidates using exact team names from ticker codes.
        # This prevents same-city confusion (Yankees vs Mets, Angels vs Dodgers) and
        # wrong-opponent matches (SEA/PIT matched to PIT/COL via single city lookup).
        mlb_codes = _parse_mlb_game_codes(km.ticker)
        if mlb_codes:
            yes_code, no_code = mlb_codes
            yes_name = _MLB_TEAM_CODES.get(yes_code, "")
            no_name = _MLB_TEAM_CODES.get(no_code, "")
            if yes_name:
                yes_kw = yes_name.split()[-1]  # e.g. "yankees", "pirates", "rangers"
                candidates = [pm for pm in candidates if yes_kw in pm.question.lower()]
            if no_name and candidates:
                no_kw = no_name.split()[-1]   # e.g. "tigers", "mariners"
                strict = [pm for pm in candidates if no_kw in pm.question.lower()]
                if strict:
                    candidates = strict
                else:
                    # Opponent is known from the ticker but appears in no Poly market.
                    # Falling back to any YES-team market here produces false arbs
                    # (e.g. CINPIT matched to "Brewers vs Reds" when no "Pirates" Poly
                    # market exists today).  Return no match instead.
                    return None, 0.0

        if not candidates:
            return None, 0.0

        # For non-MLB sports with a "vs" title (e.g. KXCFLGAME), extract the opponent
        # city and require it to appear in the Poly question.  Without this, a CFL
        # "Toronto vs Saskatchewan" market matches to "Blue Jays vs X" because
        # "toronto" is in the token city index for baseball markets too.
        if not mlb_codes:
            kalshi_sides = _extract_vs_sides(km.title)
            if kalshi_sides:
                no_side_city = _side_to_city(kalshi_sides[1])
                if no_side_city and len(no_side_city) >= 3:
                    strict_no = [
                        pm for pm in candidates
                        if no_side_city in pm.question.lower()
                        or any(no_side_city in t.outcome.lower() for t in pm.tokens)
                    ]
                    if strict_no:
                        candidates = strict_no
                    else:
                        return None, 0.0

        # For non-MLB abbreviated labels (e.g. "Chicago B", "Pittsburgh P"), narrow
        # candidates by the resolved team name so same-city teams aren't confused.
        if not mlb_codes:
            label_parts = yes_label.split()
            if len(label_parts) >= 2 and len(label_parts[-1]) == 1:
                hint = label_parts[-1].lower()
                resolved_team = _resolve_kalshi_team(kalshi_yes_city, hint)
                if resolved_team and resolved_team != hint:
                    team_narrow = [
                        pm for pm in candidates
                        if resolved_team in pm.question.lower()
                        or any(resolved_team in t.outcome.lower() for t in pm.tokens)
                    ]
                    if team_narrow:
                        candidates = team_narrow

        best: Optional[Market] = None
        best_conf = 0.0
        for pm in candidates:
            # Default: 0.80 (MLB code + team-name filtering is already precise enough).
            # Hard-reject only if settlement dates differ by > 120h — that's a different
            # game/series entirely.  The old 36h "boost" window was too tight: Poly
            # typically resolves 1 day after the game while Kalshi settles ~3 days after,
            # so the same-game gap is ~48h, outside the old 36h window → conf stuck at
            # 0.70 → missed the 0.79 threshold → all single-team matches dropped.
            conf = 0.80
            if pm.end_date and km.close_time:
                try:
                    pe = pm.end_date if pm.end_date.tzinfo else pm.end_date.replace(tzinfo=timezone.utc)
                    kc = km.close_time if km.close_time.tzinfo else km.close_time.replace(tzinfo=timezone.utc)
                    gap_h = abs((pe - kc).total_seconds()) / 3600
                    if gap_h > 120:
                        conf = 0.0  # different game/series entirely — hard reject
                except Exception:
                    pass
            if conf > best_conf:
                best_conf = conf
                best = pm
        return best, best_conf
