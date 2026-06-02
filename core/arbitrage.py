from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from config import Config
from core.api_client import PolymarketPublicClient
from utils.models import Market, OrderBook, ArbOpportunity

logger = logging.getLogger(__name__)


class ArbDetector:
    """Detects intra-market arbitrage: YES_ask + NO_ask < 1 - fee."""

    def __init__(self, client: PolymarketPublicClient, config: Config) -> None:
        self._client = client
        self._config = config
        # After 2% taker fee, buying both sides costs (p_y + p_n) * 1.02
        # Profitable when: (p_y + p_n) * (1 + fee) < 1.0
        self._breakeven = 1.0 / (1.0 + config.fee_rate)  # ~0.9804

    def scan(self, markets: list[Market]) -> list[ArbOpportunity]:
        # Skip obviously dead markets to cut API calls — volume < $500 rarely have arb
        candidates = [m for m in markets if m.volume >= 500]
        logger.debug("Arb scan: checking %d/%d markets (volume >= $500)", len(candidates), len(markets))

        opportunities: list[ArbOpportunity] = []
        # 3 workers: balances throughput vs. Cloudflare burst detection on clob.polymarket.com.
        # More than 3 parallel new-session connections triggers connection resets on Windows.
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(self._check, m): m for m in candidates}
            for fut in as_completed(futures):
                try:
                    opp = fut.result()
                    if opp is not None:
                        opportunities.append(opp)
                except Exception as exc:
                    logger.debug("Arb check error: %s", exc)

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
        # Net profit per dollar after fee: payout($1) - cost - fee
        net = 1.0 - combined * (1.0 + self._config.fee_rate)
        net_pct = net  # already a fraction of $1

        # Surface even near-arb (net_pct > -min_arb_profit_pct threshold)
        if net_pct < -self._config.min_arb_profit_pct:
            return None

        return ArbOpportunity(
            market=market,
            yes_ask=yes_ask,
            no_ask=no_ask,
            combined_cost=combined,
            net_profit_pct=net_pct,
            yes_ask_liquidity=yes_book.ask_liquidity,
            no_ask_liquidity=no_book.ask_liquidity,
        )

    def score(self, opp: ArbOpportunity) -> float:
        """Return 0–50 arb score. 50 = 5%+ net profit."""
        if not opp.is_profitable:
            # near-arb gets partial score (max 10)
            return max(0.0, (opp.net_profit_pct + self._config.min_arb_profit_pct) /
                       self._config.min_arb_profit_pct * 10)
        # 0.5% net → score 5 … 5%+ net → score 50
        return min(50.0, opp.net_profit_pct / 0.05 * 50)
