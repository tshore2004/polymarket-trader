"""
Finds the Polymarket proxy wallet address associated with your EOA.

Your $70 is sitting in a proxy wallet (a smart contract Polymarket deployed
for your email account). The EOA from your private key is just the signer —
the actual funds live in the proxy. This script finds that address.

Run from the project root:
    python scripts/find_proxy_wallet.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from curl_cffi import requests as cf

EOA = "0x27F9c239a21A20A6Ba0c654FD2a134F93AF0e94a"
DATA  = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

session = cf.Session(impersonate="chrome120")
session.headers["Accept"] = "application/json"

found = False

# ── 1. Try gamma profile endpoint ────────────────────────────────────────────
print(f"Looking up EOA: {EOA}\n")

try:
    r = session.get(f"{GAMMA}/profiles/{EOA}", timeout=8)
    if r.status_code == 200:
        data = r.json()
        proxy = (
            data.get("proxyWallet")
            or data.get("proxy_wallet")
            or data.get("address")
        )
        if proxy and proxy.lower() != EOA.lower():
            print("=" * 55)
            print("✅  Found your proxy wallet!")
            print(f"    POLY_FUNDER={proxy}")
            print("=" * 55)
            print("\nAdd this to your .env file:")
            print(f"POLY_FUNDER={proxy}")
            print("POLY_SIGNATURE_TYPE=1")
            found = True
        else:
            print(f"Gamma profile response: {data}")
except Exception as e:
    print(f"Gamma profile lookup failed: {e}")

# ── 2. Try data-api leaderboard / positions with EOA ─────────────────────────
if not found:
    try:
        r = session.get(
            f"{DATA}/positions",
            params={"user": EOA, "sizeThreshold": "0"},
            timeout=8,
        )
        if r.status_code == 200:
            positions = r.json()
            if positions:
                print(f"EOA has {len(positions)} positions — EOA IS your account address.")
                print(f"Set POLY_FUNDER={EOA} and POLY_SIGNATURE_TYPE=0 in .env")
                found = True
            else:
                print("EOA has no positions — your funds are definitely in a proxy wallet.")
    except Exception as e:
        print(f"Positions lookup failed: {e}")

# ── 3. Try activity endpoint ──────────────────────────────────────────────────
if not found:
    try:
        r = session.get(
            f"{DATA}/activity",
            params={"user": EOA, "limit": 1},
            timeout=8,
        )
        print(f"Activity endpoint [{r.status_code}]: {r.text[:200]}")
    except Exception as e:
        print(f"Activity lookup failed: {e}")

# ── Fallback instructions ─────────────────────────────────────────────────────
if not found:
    print()
    print("=" * 55)
    print("Could not auto-detect proxy wallet.")
    print("=" * 55)
    print()
    print("Manual steps to find your proxy wallet address:")
    print()
    print("  Option A — Mobile browser:")
    print("    1. Open Safari/Chrome on your iPhone")
    print("    2. Go to polymarket.com and log in with your email")
    print("    3. Tap your profile/avatar")
    print("    4. The URL or profile page will show your wallet address")
    print("       (different from the EOA above)")
    print()
    print("  Option B — Polygonscan:")
    print(f"    1. Go to: https://polygonscan.com/address/{EOA}")
    print("    2. Look at 'Internal Txns' tab")
    print("    3. The proxy wallet contract will appear as a contract")
    print("       you've interacted with (deployed by Polymarket factory)")
    print()
    print("  Option C — Polymarket DevTools:")
    print("    1. Log into polymarket.com on any browser")
    print("    2. Open DevTools → Network tab")
    print("    3. Make any request (click a market)")
    print("    4. Look for the POLY_ADDRESS request header")
    print("       That value IS your proxy wallet / funder address")
    print()
    print("Once you have it, add to .env:")
    print("  POLY_FUNDER=0x<proxy_wallet_address>")
    print("  POLY_SIGNATURE_TYPE=1")
