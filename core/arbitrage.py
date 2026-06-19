"""Arbitrage detection — two strategies:

1. IntraMarketArbDetector — YES_ask + NO_ask < 1 - fee on Polymarket (single-exchange).
2. CrossPlatformArbScanner — Polymarket vs Kalshi price discrepancies on matched markets.
"""
from __future__ import annotations
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from config import Config
from core.api_client import PolymarketPublicClient
from utils.models import Market, ArbOpportunity, KalshiMarket, ArbitrageOpportunity

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of",
    "with", "is", "are", "was", "will", "be", "by", "as", "if", "do",
    "this", "that", "it", "its", "who", "which", "what", "how", "all",
    "not", "no", "yes", "over", "from", "up", "out", "per", "via",
}


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
    intersection = pk & kk
    union = pk | kk
    return len(intersection) / len(union)


class CrossPlatformArbScanner:
    """Finds price discrepancies between matched Polymarket and Kalshi markets."""

    MATCH_THRESHOLD = 0.45  # lowered from 0.55 for broader coverage

    def __init__(self, config: Config) -> None:
        self._config = config

    def find_opportunities(
        self,
        poly_markets: list[Market],
        kalshi_markets: list[KalshiMarket],
    ) -> list[ArbitrageOpportunity]:
        threshold = getattr(self._config, "arb_match_threshold", self.MATCH_THRESHOLD)
        index = self._build_poly_index(poly_markets)
        opportunities: list[ArbitrageOpportunity] = []

        for km in kalshi_markets:
            if km.time_category == "past":
                continue
            if km.yes_price <= 0 or km.no_price <= 0:
                continue

            best_match, best_conf = self._best_poly_match_indexed(km, index)
            if best_match is None or best_conf < threshold:
                continue

            if best_match.closed or not best_match.active:
                continue
            if best_match.time_category == "past":
                continue

            yes_tok = best_match.yes_token
            no_tok = best_match.no_token
            if not yes_tok or not no_tok:
                continue

            poly_yes = yes_tok.price
            poly_no = no_tok.price
            if poly_yes <= 0 or poly_no <= 0:
                continue

            kalshi_yes = km.yes_price
            kalshi_no = km.no_price
            fee = self._config.fee_rate
            end_date = best_match.end_date
            time_cat = best_match.time_category

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
                        time_category=time_cat,
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
                        time_category=time_cat,
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
                    time_category=time_cat,
                ))

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
        """Build keyword → [Market] inverted index for O(k) candidate lookup."""
        index: dict[str, list[Market]] = defaultdict(list)
        for pm in poly_markets:
            for kw in _keywords(pm.question):
                index[kw].append(pm)
        return index

    def _best_poly_match_indexed(
        self, km: KalshiMarket, index: dict[str, list[Market]]
    ) -> tuple[Optional[Market], float]:
        """Find best Poly match using inverted index — only computes Jaccard on candidates."""
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
