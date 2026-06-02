from __future__ import annotations
import logging
import time
from typing import Optional
from config import Config
from utils.models import Signal, Side, TradeResult
from utils.display import prompt_trade, show_trade_result
from rich.console import Console

logger = logging.getLogger(__name__)


class TradeExecutor:
    """
    Semi-automated executor.

    Auth flow (matches Polymarket official example):
      private_key → create_or_derive_api_key() → ApiCreds(key_id, secret, passphrase)
      → ClobClient ready to sign + submit orders

    The Key ID and Secret Key that Polymarket support references are the
    api_key and api_secret fields of ApiCreds, derived from your private key.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._clob = self._init_clob(config)

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init_clob(self, config: Config):
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds
        except ImportError:
            logger.error(
                "py-clob-client-v2 not installed. Run: pip install py-clob-client-v2"
            )
            return None

        try:
            has_full_creds = all([config.api_key, config.api_secret, config.api_passphrase])

            if has_full_creds:
                # Fast path: pre-existing credentials supplied in .env — no L1 round-trip needed.
                creds = ApiCreds(
                    api_key=config.api_key,
                    api_secret=config.api_secret,
                    api_passphrase=config.api_passphrase,
                )
                logger.info("Using pre-supplied API credentials. Key: %s...", creds.api_key[:12])
            else:
                # Derive credentials from the private key (L1 auth — requires network).
                tmp = ClobClient(
                    host="https://clob.polymarket.com",
                    chain_id=config.chain_id,
                    key=config.private_key,
                )
                creds = tmp.create_or_derive_api_key()
                logger.info("Derived API credentials. Key: %s...", creds.api_key[:12])

            # Build the full L2 client.
            # signature_type and funder are required for non-EOA wallets:
            #   1 = POLY_PROXY  (email/iOS login via Privy / Magic Link)
            #   3 = POLY_1271   (new deposit wallet, for new API-only users)
            kwargs = dict(
                host="https://clob.polymarket.com",
                chain_id=config.chain_id,
                key=config.private_key,
                creds=creds,
                signature_type=config.signature_type,
            )
            if config.funder:
                kwargs["funder"] = config.funder

            client = ClobClient(**kwargs)
            return client

        except Exception as exc:
            logger.error(
                "CLOB client init failed: %s\n"
                "Check POLY_PRIVATE_KEY format and that all three API credential vars are set "
                "(POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE).",
                exc,
            )
            return None

    # ── Public ────────────────────────────────────────────────────────────────

    def get_balance(self) -> Optional[float]:
        """Return USDC balance in dollars available for trading, or None if unavailable.

        Retries up to 3 times on WinError 10054 / connection-reset (curl 35) errors,
        which Cloudflare commonly triggers on Windows for the first connection attempt.
        """
        if self._clob is None:
            logger.warning(
                "get_balance: CLOB client is None — auth failed at startup. "
                "Check your POLY_PRIVATE_KEY format and network connectivity."
            )
            return None

        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        last_exc = None
        for attempt in range(3):
            try:
                resp = self._clob.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                logger.debug("Raw balance response: %s", resp)
                raw_str = (
                    resp.get("balance")
                    or resp.get("collateralBalance")
                    or resp.get("availableBalance")
                    or "0"
                )
                return float(raw_str)
            except Exception as exc:
                last_exc = exc
                is_reset = any(x in str(exc) for x in ("10054", "35", "reset", "forcibly"))
                if is_reset and attempt < 2:
                    wait = 2.0 * (attempt + 1)
                    logger.debug("Connection reset on balance check — retrying in %.1fs (attempt %d/3)", wait, attempt + 1)
                    time.sleep(wait)
                    continue
                break
        logger.warning("Balance fetch failed: %s", last_exc)
        return None

    def present(self, console: Console, sig: Signal, index: int) -> Optional[bool]:
        """Display signal panel and prompt user. Returns True=execute, False=skip, None=quit."""
        from utils.display import show_signal
        show_signal(console, sig, index)
        return prompt_trade(console, sig, self._size_for(sig))

    def size_for(self, sig: Signal) -> float:
        """Public access to computed bet size."""
        return self._size_for(sig)

    def execute(self, sig: Signal, console: Console) -> list[TradeResult]:
        """Place order for a confirmed signal and print results."""
        size = self._size_for(sig)
        results: list[TradeResult] = []

        token = (
            sig.market.yes_token
            if sig.recommended_side == Side.YES
            else sig.market.no_token
        )
        if token:
            res = self._place_order(
                sig.market.question, token.token_id, "BUY",
                sig.recommended_price, size,
            )
            results.append(res)
            show_trade_result(console, res)

        return results

    # ── Private ───────────────────────────────────────────────────────────────

    def _size_for(self, sig: Signal) -> float:
        """Scale bet $min→$max linearly by combined score 0→100."""
        ratio = min(sig.combined_score / 100.0, 1.0)
        size = self._config.min_bet_size + (
            self._config.max_bet_size - self._config.min_bet_size
        ) * ratio
        return round(size, 2)

    def _place_order(
        self,
        question: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> TradeResult:
        if self._clob is None:
            return TradeResult(
                success=False,
                market_question=question,
                side=side,
                price=price,
                size=size,
                error="CLOB client not initialized — check credentials and pip install.",
            )
        try:
            from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY, SELL

            # Fetch tick size and neg-risk from the CLOB for this token.
            # create_and_post_order auto-fetches these if options is None,
            # but we fetch explicitly so we can log them and handle errors cleanly.
            try:
                tick_size = str(self._clob.get_tick_size(token_id))
            except Exception:
                tick_size = "0.01"  # safe default for most binary markets

            try:
                neg_risk = self._clob.get_neg_risk(token_id)
            except Exception:
                neg_risk = False   # False for standard binary markets

            logger.debug(
                "Order params — token: %s  tick: %s  neg_risk: %s",
                token_id[:12], tick_size, neg_risk,
            )

            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                size=round(size, 2),
                side=BUY if side == "BUY" else SELL,
            )

            resp = self._clob.create_and_post_order(
                order_args,
                options=PartialCreateOrderOptions(
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                ),
            )

            order_id = resp.get("orderID") or resp.get("id") or str(resp)
            return TradeResult(
                success=True,
                market_question=question,
                side=side,
                price=price,
                size=size,
                order_id=order_id,
            )
        except Exception as exc:
            logger.error("Order failed — %s: %s", question[:50], exc)
            return TradeResult(
                success=False,
                market_question=question,
                side=side,
                price=price,
                size=size,
                error=str(exc),
            )
