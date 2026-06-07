# Bug Fixes: Stale Picks + Missing Bet Price

Two self-contained issues, both fixable in < 20 lines of code.

---

## Fix 1 — Filter expired picks from `/api/picks`

**Root cause:** `snap.picks` is a cached list from the last scan. After the scan,
markets can resolve (time_category becomes "past") but the cache isn't invalidated.
The endpoint returns them unchanged.

**Fix in `server.py`, inside `api_picks()`:**

Replace the current:
```python
picks_raw = _jsonify(snap.picks)
for raw, pick in zip(picks_raw, snap.picks):
```

With:
```python
active_picks = [p for p in snap.picks
                if p.market.time_category != "past" and not p.market.closed]
picks_raw = _jsonify(active_picks)
for raw, pick in zip(picks_raw, active_picks):
```

That's it — one pre-filter line, then update the zip variable name.

---

## Fix 2 — Expose recommended price in the picks API

**Root cause:** `MarketConsensus` carries `market.tokens[].price` (populated from
`TraderPosition.cur_price` in `_market_from_position`), but `api_picks()` never
serialises that as a top-level field the frontend can use.

**Fix in `server.py`, inside the `api_picks()` for-loop, add:**
```python
from utils.models import Side  # already imported via models but confirm at top of file

dominant_tok = (
    pick.market.yes_token
    if pick.dominant_side == Side.YES
    else pick.market.no_token
)
raw["recommended_price"] = (
    round(dominant_tok.price, 4)
    if dominant_tok and dominant_tok.price > 0
    else None
)
```

---

## Fix 3 — Show "BET YES/NO @ price" in pick cards (HTML)

**Root cause:** The pick card template never renders `recommended_price`.

**Fix in `templates/index.html`:**

### 3a. Main pick card list (around line 548, just after the daily-score+dominant-side block, before the weight bar `<div class="mb-2">`)

Add:
```html
<!-- Bet recommendation -->
<div x-show="pick.recommended_price" class="mb-2">
  <span class="inline-flex items-center gap-1.5 text-sm font-bold px-3 py-1.5 rounded-lg border"
        :class="pick.dominant_side === 'YES'
          ? 'bg-green-950 text-green-300 border-green-700'
          : 'bg-red-950 text-red-300 border-red-700'">
    <span>BET</span>
    <span x-text="pick.dominant_side"></span>
    <span class="font-mono" x-text="'@ ' + pick.recommended_price"></span>
  </span>
</div>
```

### 3b. Sidebar pick cards (around line 127, after the YES/NO confidence bar, before the footer)

Add:
```html
<!-- Bet price -->
<div x-show="pick.recommended_price" class="mb-1">
  <span class="text-xs font-bold font-mono"
        :class="pick.dominant_side === 'YES' ? 'text-green-400' : 'text-red-400'"
        x-text="'BET ' + pick.dominant_side + ' @ ' + pick.recommended_price"></span>
</div>
```

### 3c. "Today's Events" horizontal scroll cards (around line 367, after the side+confidence bar, before the traders footer)

Add:
```html
<!-- Bet price -->
<div x-show="pick.recommended_price" class="mb-2">
  <span class="text-xs font-bold font-mono"
        :class="pick.dominant_side === 'YES' ? 'text-green-400' : 'text-red-400'"
        x-text="'BET ' + pick.dominant_side + ' @ ' + pick.recommended_price"></span>
</div>
```

---

## Summary

| File | Change |
|------|--------|
| `server.py` | Pre-filter `snap.picks` to remove `past`/`closed` before serializing |
| `server.py` | Add `recommended_price` field (dominant token price) to each serialized pick |
| `templates/index.html` | Add "BET YES/NO @ 0.XXXX" display in 3 card locations |

No model changes needed. No new dependencies. The price is already in the data — it
just wasn't being surfaced.
