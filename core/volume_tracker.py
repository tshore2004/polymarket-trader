"""VolumeTracker --detects unusual volume spikes and price movements between scan cycles."""
from __future__ import annotations
from utils.models import Market

_MIN_VOLUME_FOR_SPIKE = 5_000.0   # ignore tiny markets (< $5k total volume)


class VolumeTracker:
    def __init__(self, spike_threshold: float = 1.5, price_move_threshold: float = 0.05) -> None:
        self._spike_threshold = spike_threshold
        self._price_move_threshold = price_move_threshold
        self._prev_volumes: dict[str, float] = {}
        self._prev_prices: dict[str, float] = {}   # YES token price per market

    def update(self, markets: list[Market]) -> None:
        """Store current snapshot --call AFTER get_spikes/get_price_moves."""
        for m in markets:
            self._prev_volumes[m.condition_id] = m.volume
            yes = m.yes_token
            if yes and yes.price > 0:
                self._prev_prices[m.condition_id] = yes.price

    def get_spikes(self, markets: list[Market]) -> list[tuple[Market, float, str]]:
        """Return markets where volume grew by at least spike_threshold since last scan.

        Returns list of (market, ratio, explanation) sorted by ratio descending.
        """
        results = []
        for m in markets:
            prev = self._prev_volumes.get(m.condition_id, 0.0)
            if prev < _MIN_VOLUME_FOR_SPIKE or m.volume <= 0:
                continue
            ratio = m.volume / prev
            if ratio >= self._spike_threshold:
                explanation = (
                    f"Volume spiked {ratio:.1f}x since last scan "
                    f"(${prev:,.0f} -> ${m.volume:,.0f}) --"
                    "unusual activity suggests informed positioning"
                )
                results.append((m, ratio, explanation))
        return sorted(results, key=lambda x: -x[1])

    def get_high_volume_markets(self, markets: list[Market]) -> list[tuple[Market, float, str]]:
        """First-run fallback: rank top 25% by absolute volume when no spike history exists.

        Returns (market, score 0–8, explanation). Score is proportional to volume
        within the top quartile, so the highest-volume market always scores 8.
        """
        eligible = [m for m in markets if m.volume >= _MIN_VOLUME_FOR_SPIKE]
        if not eligible:
            return []
        sorted_by_vol = sorted(eligible, key=lambda m: m.volume, reverse=True)
        cutoff = max(1, len(sorted_by_vol) // 4)
        top_markets = sorted_by_vol[:cutoff]
        max_vol = top_markets[0].volume
        results = []
        for m in top_markets:
            score = round(min(8.0, (m.volume / max_vol) * 8.0), 2)
            explanation = (
                f"High-volume market (${m.volume:,.0f} total) — active trading market"
            )
            results.append((m, score, explanation))
        return results

    def get_price_moves(self, markets: list[Market]) -> list[tuple[Market, float, str]]:
        """Return markets where YES price moved >= price_move_threshold since last scan.

        Returns list of (market, delta, explanation) sorted by abs(delta) descending.
        """
        results = []
        for m in markets:
            prev_price = self._prev_prices.get(m.condition_id)
            yes = m.yes_token
            if prev_price is None or yes is None or yes.price <= 0:
                continue
            delta = yes.price - prev_price
            if abs(delta) >= self._price_move_threshold:
                direction = "up" if delta > 0 else "down"
                explanation = (
                    f"YES price moved {direction} {abs(delta)*100:.1f}% "
                    f"({prev_price:.3f} -> {yes.price:.3f}) since last scan --"
                    "price discovery in progress"
                )
                results.append((m, delta, explanation))
        return sorted(results, key=lambda x: -abs(x[1]))
