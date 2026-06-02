"""NewsSentimentAnalyzer — matches trending news headlines against Polymarket questions.

Uses free RSS feeds by default (no API key required).
Set NEWS_API_KEY in .env to also query NewsAPI.org for richer US/global coverage.
"""
from __future__ import annotations
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Config

from utils.models import Market

logger = logging.getLogger(__name__)

# Common English stopwords — excluded from keyword matching
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "shall", "can", "need", "dare", "ought", "used", "as", "if", "then",
    "than", "when", "where", "which", "who", "that", "this", "these", "those",
    "it", "its", "he", "she", "they", "we", "you", "i", "me", "him", "her",
    "them", "us", "my", "your", "his", "our", "their", "what", "how", "why",
    "all", "each", "every", "both", "few", "more", "most", "other", "some",
    "such", "no", "not", "only", "same", "so", "too", "very", "just", "said",
    "about", "after", "before", "between", "into", "through", "during",
    "over", "under", "again", "further", "once", "here", "there",
    "by", "from", "up", "out", "off", "while", "although", "because", "since",
    "until", "unless", "though", "however", "therefore", "thus", "new", "say",
    "says", "see", "get", "got", "make", "made", "one", "two", "three", "four",
    "five", "six", "seven", "eight", "nine", "ten", "per", "via", "amid",
}

_RSS_FETCH_TIMEOUT = 5   # per-feed HTTP timeout in seconds
_RSS_TOTAL_TIMEOUT = 8   # wall-clock budget for all feeds combined

# Free RSS feeds — no API key required
_RSS_SOURCES = [
    ("https://feeds.bbci.co.uk/news/rss.xml", "BBC"),
    ("https://rss.cnn.com/rss/edition.rss", "CNN"),
    ("https://feeds.reuters.com/reuters/topNews", "Reuters"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "NYT"),
    ("https://feeds.nbcnews.com/nbcnews/public/news", "NBC"),
    ("https://feeds.washingtonpost.com/rss/national", "WaPo"),
]


class NewsSentimentAnalyzer:
    def __init__(self, config: "Config") -> None:
        self._config = config
        self._headlines: list[str] = []
        self._keyword_freq: dict[str, int] = {}
        self._last_refresh: float = 0.0

    def refresh(self) -> None:
        """Fetch fresh headlines if the refresh interval has elapsed."""
        now = time.monotonic()
        if now - self._last_refresh < self._config.news_refresh:
            return
        self._last_refresh = now

        headlines: list[str] = []

        if self._config.news_api_key:
            headlines.extend(self._fetch_newsapi())

        headlines.extend(self._fetch_rss())

        if not headlines:
            logger.warning("News: no headlines fetched — news signals disabled this cycle.")
            return

        self._headlines = headlines
        self._keyword_freq = _build_keyword_freq(headlines)
        logger.info(
            "News: %d headlines fetched, %d unique keywords extracted.",
            len(headlines), len(self._keyword_freq),
        )

    def score_market(self, market: Market) -> tuple[float, str]:
        """Return (score 0–50, explanation). Score 0 means no news relevance."""
        if not self._keyword_freq:
            return 0.0, ""

        q_tokens = set(re.findall(r"[a-z]{3,}", market.question.lower())) - _STOPWORDS
        if not q_tokens:
            return 0.0, ""

        matched: dict[str, int] = {
            tok: self._keyword_freq[tok]
            for tok in q_tokens
            if tok in self._keyword_freq
        }
        if not matched:
            return 0.0, ""

        # Coverage = fraction of question keywords appearing in headlines
        coverage = len(matched) / max(len(q_tokens), 1)
        # Frequency score = total headline hits (capped)
        freq_score = min(sum(matched.values()) / 10.0, 1.0)
        raw = (0.5 * coverage + 0.5 * freq_score) * 50.0
        score = round(min(raw, 50.0), 2)

        top_kw = sorted(matched.items(), key=lambda x: -x[1])[:3]
        kw_str = ", ".join(f'"{k}" ({v}×)' for k, v in top_kw)
        explanation = (
            f"News match: {kw_str} trending — "
            f"{len(matched)}/{len(q_tokens)} question keywords found in "
            f"{len(self._headlines)} headlines"
        )
        return score, explanation

    def get_signals(self, markets: list[Market]) -> list[tuple[Market, float, str]]:
        """Return (market, score, explanation) for all markets with news relevance."""
        if not self._keyword_freq:
            return []
        results = [
            (m, *self.score_market(m))
            for m in markets
        ]
        return sorted(
            [(m, s, e) for m, s, e in results if s > 0],
            key=lambda x: -x[1],
        )

    # ── Fetch helpers ─────────────────────────────────────────────────────────

    def _fetch_rss(self) -> list[str]:
        try:
            import feedparser
        except ImportError:
            logger.warning(
                "feedparser not installed — RSS news disabled. "
                "Run: pip install feedparser"
            )
            return []

        from curl_cffi import requests as cf_requests

        def _one(url_source: tuple[str, str]) -> list[str]:
            url, source = url_source
            try:
                resp = cf_requests.get(url, timeout=_RSS_FETCH_TIMEOUT)
                feed = feedparser.parse(resp.text)
                results = []
                for entry in feed.entries[:20]:
                    title = entry.get("title", "")
                    summary = re.sub(r"<[^>]+>", " ", entry.get("summary", "") or "")
                    if title:
                        results.append(f"{title} {summary}")
                logger.debug("News RSS %s: %d entries", source, len(feed.entries))
                return results
            except Exception as exc:
                logger.debug("RSS feed %s failed: %s", source, exc)
                return []

        headlines: list[str] = []
        with ThreadPoolExecutor(max_workers=len(_RSS_SOURCES)) as pool:
            futures = {pool.submit(_one, pair): pair[1] for pair in _RSS_SOURCES}
            try:
                for fut in as_completed(futures, timeout=_RSS_TOTAL_TIMEOUT):
                    try:
                        headlines.extend(fut.result())
                    except Exception:
                        pass
            except TimeoutError:
                logger.debug("RSS fetch timed out — using partial results")

        return headlines

    def _fetch_newsapi(self) -> list[str]:
        try:
            from curl_cffi import requests as cf_requests
            resp = cf_requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={
                    "country": "us",
                    "pageSize": 50,
                    "apiKey": self._config.news_api_key,
                },
                impersonate="chrome120",
                timeout=8,
            )
            data = resp.json()
            return [
                f"{a.get('title', '')} {a.get('description', '') or ''}"
                for a in data.get("articles", [])
                if a.get("title")
            ]
        except Exception as exc:
            logger.debug("NewsAPI fetch failed: %s", exc)
            return []


def _build_keyword_freq(headlines: list[str]) -> dict[str, int]:
    freq: dict[str, int] = {}
    for h in headlines:
        for tok in re.findall(r"[a-z]{3,}", h.lower()):
            if tok not in _STOPWORDS:
                freq[tok] = freq.get(tok, 0) + 1
    return freq
