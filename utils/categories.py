"""Market category and subcategory detection from tags and question text."""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.models import Market

# Sport-specific tag slugs the Polymarket Gamma API may provide
_SPORT_TAG_SLUGS = {
    "nfl", "nba", "mlb", "nhl",
    "soccer", "mls", "epl", "premier-league", "champions-league",
    "tennis", "atp", "wta",
    "golf", "pga",
    "ufc", "mma", "boxing",
    "esports", "gaming",
    "cricket", "rugby", "formula-1", "formula1", "f1",
    "nascar", "olympics",
}

# Question-text keyword fallbacks when only a generic "sports" tag exists
_QUESTION_SPORT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("nfl", ["nfl", "quarterback", "touchdown", "super bowl", "chiefs", "eagles", "patriots",
             "cowboys", "rams", "packers", "49ers", "broncos", "ravens", "bills", "dolphins"]),
    ("nba", ["nba", "lakers", "celtics", "warriors", "bulls", "heat", "nets", "knicks",
             "bucks", "suns", "nuggets", "sixers", "cavs", "basketball"]),
    ("mlb", ["mlb", "baseball", "yankees", "dodgers", "red sox", "cubs", "mets", "braves",
             "astros", "world series", "home run"]),
    ("nhl", ["nhl", "hockey", "penguins", "rangers", "bruins", "blackhawks", "maple leafs",
             "canadiens", "stanley cup"]),
    ("soccer", ["soccer", "mls", "premier league", "champions league", "world cup", "fifa",
                "epl", "la liga", "bundesliga", "serie a", "ligue 1", "euro",
                "manchester", "chelsea", "arsenal", "liverpool", "barcelona", "real madrid"]),
    ("tennis", ["tennis", "wimbledon", "us open", "french open", "australian open",
                "atp", "wta", "grand slam", "djokovic", "federer", "nadal", "serena"]),
    ("golf", ["golf", "pga", "masters", "ryder cup", "open championship", "birdie", "eagle"]),
    ("ufc", ["ufc", "mma", "boxing", "fight", "knockout", "bout", "heavyweight", "lightweight"]),
    ("esports", ["esports", "league of legends", "valorant", "csgo", "dota", "overwatch",
                 "fortnite tournament", "rocket league"]),
    ("formula-1", ["formula 1", "formula1", "f1", "grand prix", "ferrari", "red bull racing",
                   "mercedes amg", "verstappen", "hamilton"]),
]

# Top-level category tag slugs
_POLITICS_TAGS = {"politics", "elections", "government", "policy", "congress", "senate", "president"}
_CRYPTO_TAGS = {"crypto", "cryptocurrency", "bitcoin", "btc", "ethereum", "eth", "solana", "defi", "nft"}
_ENTERTAINMENT_TAGS = {"entertainment", "music", "movies", "television", "celebrity", "awards", "oscars"}
_SCIENCE_TAGS = {"science", "technology", "ai", "artificial-intelligence", "space"}
_ECONOMICS_TAGS = {"economics", "economy", "finance", "stocks", "market"}

CATEGORIES = ["sports", "politics", "crypto", "entertainment", "other"]
SPORTS_SUBCATS = ["nfl", "nba", "mlb", "nhl", "soccer", "tennis", "golf", "ufc", "esports", "formula-1", "other-sports"]

CATEGORY_ICONS: dict[str, str] = {
    "sports": "trophy",
    "nfl": "football",
    "nba": "basketball",
    "mlb": "baseball",
    "nhl": "hockey",
    "soccer": "soccer",
    "tennis": "tennis",
    "golf": "golf",
    "ufc": "boxing",
    "esports": "gaming",
    "formula-1": "f1",
    "other-sports": "sport",
    "politics": "politics",
    "crypto": "crypto",
    "entertainment": "entertainment",
    "other": "chart",
}

# Emoji equivalents for use in rich terminal output
CATEGORY_EMOJI: dict[str, str] = {
    "sports": "🏆", "nfl": "🏈", "nba": "🏀", "mlb": "⚾",
    "nhl": "🏒", "soccer": "⚽", "tennis": "🎾", "golf": "⛳",
    "ufc": "🥊", "esports": "🎮", "formula-1": "🏎", "other-sports": "🏅",
    "politics": "🏛", "crypto": "🪙", "entertainment": "🎬", "other": "📊",
}


def detect_market_category(market: "Market") -> tuple[str, str]:
    """Return (category, subcategory) for a market.

    Uses market.tags first; falls back to keyword scan of market.question.
    subcategory is a sport slug for sports, empty string otherwise.
    """
    tags = {t.lower().strip() for t in (market.tags or [])}
    question_lower = market.question.lower()

    # Check for explicit sport tag slugs
    for sport_slug in _SPORT_TAG_SLUGS:
        if sport_slug in tags:
            return ("sports", _normalise_sport(sport_slug))

    # Generic sports tag — try question keyword fallback
    if tags & {"sports", "sport"}:
        subcat = _detect_sport_from_question(question_lower)
        return ("sports", subcat)

    # Politics
    if tags & _POLITICS_TAGS:
        return ("politics", "")

    # Crypto
    if tags & _CRYPTO_TAGS:
        subcat = ""
        if "bitcoin" in tags or "btc" in tags:
            subcat = "bitcoin"
        elif "ethereum" in tags or "eth" in tags:
            subcat = "ethereum"
        return ("crypto", subcat)

    # Entertainment
    if tags & _ENTERTAINMENT_TAGS:
        return ("entertainment", "")

    # Last resort: keyword scan of question for sports detection even without tags
    subcat = _detect_sport_from_question(question_lower)
    if subcat:
        return ("sports", subcat)

    return ("other", "")


def _normalise_sport(slug: str) -> str:
    """Map API slug variants to canonical subcategory names."""
    _aliases: dict[str, str] = {
        "premier-league": "soccer", "champions-league": "soccer",
        "epl": "soccer", "mls": "soccer",
        "atp": "tennis", "wta": "tennis",
        "pga": "golf",
        "mma": "ufc", "boxing": "ufc",
        "gaming": "esports",
        "formula1": "formula-1", "f1": "formula-1",
        "nascar": "formula-1",
    }
    return _aliases.get(slug, slug)


def _detect_sport_from_question(question_lower: str) -> str:
    """Scan question text for sport keywords. Returns subcategory or ''."""
    for sport, keywords in _QUESTION_SPORT_KEYWORDS:
        for kw in keywords:
            if kw in question_lower:
                return sport
    return ""
