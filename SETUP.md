# Polymarket Trader — Credential Setup

## The two issues you're hitting

### 1. "MetaMask API key" error
Polymarket's API error message is misleading. It doesn't literally need MetaMask — it means
the L1 signature it received was invalid. This almost always means your private key is in the
wrong format or is the wrong key entirely.

### 2. "No money" despite having funds on Polymarket
Your balance shows in the Polymarket web app but the bot sees $0. This happens because
Polymarket's web UI shows your **proxy wallet** balance, while the CLOB API checks what's
approved for trading from the **EOA (Externally Owned Account)** linked to your private key.
Both need to match.

---

## Step 1 — Get your private key from MetaMask

1. Open MetaMask
2. Click the account icon (top-right circle) → **Account details**
3. Click **Show private key**
4. Enter your MetaMask password
5. Copy the hex string — it looks like:  
   `a3f8c2...` (64 hex characters, **no spaces, no words**)

> ⚠️ If what you copied contains spaces (e.g. `word1 word2 word3 ...`), that is your
> **seed phrase**, not your private key. Do NOT use a seed phrase here. Go back and
> find "Show private key".

---

## Step 2 — Find your wallet address

In MetaMask, your address is shown at the top (e.g. `0xAbCd...1234`).

Go to [polymarket.com](https://polymarket.com) → Profile → Wallet (or the address shown
in the top-right). **The address shown on Polymarket must match your MetaMask address.**

If they don't match, you logged into Polymarket with a different wallet than the one
whose private key you're using. Export the key from the correct wallet.

---

## Step 3 — Fill in .env

```bash
cp .env.example .env
```

Edit `.env`:

```dotenv
# Your wallet's raw private key — 64 hex chars, with or without 0x prefix
POLY_PRIVATE_KEY=0xa3f8c2...  # paste your full key here

# Leave blank — the bot derives these automatically from POLY_PRIVATE_KEY
POLY_KEY_ID=
POLY_SECRET=
```

---

## Step 4 — Run the diagnostic

```bash
python tests/diagnose.py
```

This tests each layer independently and tells you exactly where things break:

| Check | What it tests |
|-------|--------------|
| 1 | Private key format (length, hex, no spaces) |
| 2 | SDK import (py-clob-client-v2 installed correctly) |
| 3 | L1 client construction (offline) |
| 4 | API key derivation (network to clob.polymarket.com) |
| 5 | Balance check (L2 auth + USDC balance) |
| 6 | Dry-run order signing (no submission) |
| 7 | Public API reachability |

---

## Step 5 — If balance is still $0

The CLOB needs an explicit allowance to spend your USDC. When you deposit through the
Polymarket web UI, it sets this automatically. But if your balance is $0 on the CLOB
while showing funds on the website, run this once from Python:

```python
import os
from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient, BalanceAllowanceParams, AssetType

load_dotenv()
key = os.getenv("POLY_PRIVATE_KEY")

tmp = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=key)
creds = tmp.create_or_derive_api_key()
client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=key, creds=creds)

# This updates the CLOB's view of your available balance:
print(client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)))
```

Then re-run `get_balance_allowance` to confirm it shows your actual balance.

---

## Common error messages and fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `403` or `MetaMask required` | Wrong/malformed private key | Re-export raw hex key from MetaMask |
| `401 Unauthorized` | API key mismatch | Delete derived key from Polymarket dashboard and re-derive |
| `balance: 0` | Allowance not set | Run `update_balance_allowance()` above |
| `ImportError: BUY` | Bug in executor.py (now fixed) | Pull latest code |
| `Connection timeout` | Network / firewall | Check VPN, try different network |
| `socksio not installed` | httpx SOCKS proxy config | `pip install httpx[socks]` or unset SOCKS env vars |

---

## Architecture reminder

```
Your MetaMask wallet (EOA)
    │  private key → signs L1 headers
    ▼
Polymarket CLOB API derives ApiCreds (api_key + api_secret + passphrase)
    │  these creds → signs L2 headers on every order
    ▼
Orders placed against your proxy wallet's USDC balance
```

The private key never leaves your machine — it's used only to sign HTTP request headers.
