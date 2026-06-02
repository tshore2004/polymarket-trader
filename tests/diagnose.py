"""
Polymarket CLOB API — layered diagnostic script.

Run from the project root:
    python tests/diagnose.py

Each check is independent. It stops and prints a clear fix if something fails.
No trades are ever placed — this is read-only.
"""
from __future__ import annotations
import os
import sys

# ── allow running from either the project root or the tests/ dir ──────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Force UTF-8 output on Windows so emoji print correctly.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

PASS = "  ✅"
FAIL = "  ❌"
WARN = "  ⚠️ "


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 1 — private key format
# ─────────────────────────────────────────────────────────────────────────────
def check_private_key() -> str | None:
    """
    Returns the normalised private key (with 0x prefix) or exits.
    """
    print("\n[1] Private key format")
    raw = os.getenv("POLY_PRIVATE_KEY", "").strip()

    if not raw:
        print(FAIL, "POLY_PRIVATE_KEY is missing from .env")
        print(
            "     Fix: export your MetaMask private key (Account details → Show private key)\n"
            "     and put it in .env as:  POLY_PRIVATE_KEY=0x<64 hex chars>"
        )
        return None

    # Seed phrase / mnemonic — cannot be used directly
    if " " in raw:
        print(FAIL, "POLY_PRIVATE_KEY looks like a seed phrase (contains spaces).")
        print(
            "     Fix: you need the RAW PRIVATE KEY, not the mnemonic.\n"
            "     In MetaMask: Account details → Show private key → copy the hex string."
        )
        return None

    # Normalise: ensure 0x prefix
    key = raw if raw.startswith("0x") else f"0x{raw}"
    hex_part = key[2:]

    if len(hex_part) != 64:
        print(FAIL, f"Private key hex is {len(hex_part)} chars; expected 64.")
        print("     Fix: make sure you copied the full key with no extra spaces.")
        return None

    try:
        int(hex_part, 16)
    except ValueError:
        print(FAIL, "Private key contains non-hex characters.")
        return None

    print(PASS, f"Key format OK  (0x{hex_part[:6]}…{hex_part[-4:]})")
    return key


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 2 — SDK import
# ─────────────────────────────────────────────────────────────────────────────
def check_sdk_import():
    print("\n[2] SDK import (py-clob-client-v2)")
    try:
        from py_clob_client_v2 import ClobClient, OrderArgs, Side, BalanceAllowanceParams, AssetType  # noqa: F401
        print(PASS, "py_clob_client_v2 imports OK")
        return True
    except ImportError as exc:
        print(FAIL, f"Import failed: {exc}")
        print("     Fix: pip install py-clob-client-v2")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 3 — L1 client construction (no network needed)
# ─────────────────────────────────────────────────────────────────────────────
def check_l1_client(private_key: str):
    print("\n[3] L1 ClobClient construction (offline — no network)")
    try:
        from py_clob_client_v2 import ClobClient
        tmp = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
        )
        print(PASS, "ClobClient (L1) constructed successfully")
        return tmp
    except Exception as exc:
        print(FAIL, f"ClobClient construction failed: {exc}")
        print(
            "     This is unexpected — construction should be offline.\n"
            "     Check that the SDK is correctly installed."
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 4 — API key derivation (requires network + CLOB reachability)
# ─────────────────────────────────────────────────────────────────────────────
def check_api_key_derivation(l1_client):
    print("\n[4] API key derivation (network required)")
    try:
        creds = l1_client.create_or_derive_api_key()
        print(PASS, f"API key derived:  key_id={creds.api_key[:12]}…")
        print(
            f"     api_key        = {creds.api_key}\n"
            f"     api_secret     = {creds.api_secret[:8]}…\n"
            f"     api_passphrase = {creds.api_passphrase[:8]}…\n"
        )
        print(
            WARN,
            "Save these in your .env as POLY_KEY_ID / POLY_SECRET / POLY_API_PASSPHRASE\n"
            "     (passphrase field is not in config yet — see instructions).",
        )
        return creds
    except Exception as exc:
        print(FAIL, f"API key derivation failed: {exc}")
        err = str(exc).lower()
        if "metamask" in err or "browser" in err or "403" in err or "401" in err:
            print(
                "     Likely cause: wrong private key or signature mismatch.\n"
                "     Make sure POLY_PRIVATE_KEY is your wallet's actual private key,\n"
                "     NOT an API key, passphrase, or copied from the Polymarket UI.\n"
                "     MetaMask: Account details → Show private key → paste the hex string."
            )
        elif "connection" in err or "timeout" in err or "ssl" in err:
            print(
                "     Likely cause: network issue reaching clob.polymarket.com.\n"
                "     Check your internet connection and try again."
            )
        else:
            print(f"     Raw error: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 5 — L2 client construction and balance check
# ─────────────────────────────────────────────────────────────────────────────
def check_balance(private_key: str, creds):
    print("\n[5] Balance check (L2 auth)")
    try:
        from py_clob_client_v2 import ClobClient, BalanceAllowanceParams, AssetType
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            creds=creds,
        )
        resp = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(PASS, "Balance response received")
        print(f"     Raw response: {resp}")

        balance_str = (
            resp.get("balance")
            or resp.get("collateralBalance")
            or resp.get("availableBalance")
            or "0"
        )
        balance = float(balance_str)
        print(PASS, f"USDC balance available for trading: ${balance:.2f}")

        if balance == 0:
            print(
                WARN,
                "Balance is $0.00 — possible causes:\n"
                "     a) You haven't deposited USDC to Polymarket yet.\n"
                "     b) Your Polymarket account uses a DIFFERENT wallet address than\n"
                "        the private key in .env.  Log into polymarket.com, go to\n"
                "        Profile → Wallet and check the displayed address matches:\n"
                f"        {_address_from_key(private_key)}\n"
                "     c) You deposited via the Polymarket web UI but the CLOB allowance\n"
                "        hasn't been set — run update_balance_allowance() once.",
            )
        return client, balance
    except Exception as exc:
        print(FAIL, f"Balance check failed: {exc}")
        return None, 0.0


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 6 — dry-run order build (no submission)
# ─────────────────────────────────────────────────────────────────────────────
def check_order_build(client):
    print("\n[6] Dry-run order build (NOT submitted)")
    # Use a well-known test token — this is just for signing; no order is posted.
    DUMMY_TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
    try:
        from py_clob_client_v2 import OrderArgs, Side
        order_args = OrderArgs(
            token_id=DUMMY_TOKEN,
            price=0.50,
            size=5.0,
            side=Side.BUY,
        )
        signed = client.create_order(order_args)
        print(PASS, "Order signed successfully (not submitted)")
        print(f"     Order salt: {getattr(signed, 'salt', 'N/A')}")
        print(f"     Signature:  {str(getattr(signed, 'signature', 'N/A'))[:24]}…")
    except Exception as exc:
        print(FAIL, f"Order build failed: {exc}")
        print(
            "     This means order signing is broken even before submission.\n"
            f"     Error detail: {exc}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 7 — public API reachability (no auth)
# ─────────────────────────────────────────────────────────────────────────────
def check_public_api():
    print("\n[7] Public API reachability (no auth)")
    import urllib.request
    urls = [
        "https://clob.polymarket.com/time",
        "https://gamma-api.polymarket.com/markets?limit=1&active=true&closed=false",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read(200).decode()
            print(PASS, f"  {url}  →  {body[:80]}")
        except Exception as exc:
            print(FAIL, f"  {url}  →  {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# CHECK 8 — data-api.polymarket.com (leaderboard endpoint)
# ─────────────────────────────────────────────────────────────────────────────
def check_data_api():
    """
    Diagnose the leaderboard curl(35) errors.

    The leaderboard endpoint is PUBLIC — credential issues cannot cause this.
    curl(35) / WinError 10054 = Cloudflare rejected the TLS connection.
    data-api.polymarket.com uses stricter bot detection than gamma-api.
    The bot now sends Origin/Referer headers and cycles through all browser
    TLS profiles. This check tells you whether any profile gets through.
    """
    print("\n[8] data-api.polymarket.com — leaderboard connectivity")
    print("     (PUBLIC endpoint — credential issues CANNOT cause curl(35) errors)")

    LB_URL = "https://data-api.polymarket.com/v1/leaderboard"
    LB_PARAMS = {"timePeriod": "MONTH", "orderBy": "PNL", "limit": "5", "offset": "0"}

    BROWSER_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://polymarket.com",
        "Referer": "https://polymarket.com/leaderboard",
    }

    try:
        from curl_cffi import requests as cf_requests
    except ImportError:
        print(WARN, "curl_cffi not installed — skipping profile tests")
        return

    import time as _time
    profiles = ["chrome136", "chrome124", "chrome120", "edge101"]
    any_profile_worked = False
    for profile in profiles:
        try:
            s = cf_requests.Session(impersonate=profile)
            s.headers.update(BROWSER_HEADERS)
            resp = s.get(LB_URL, params=LB_PARAMS, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            count = len(data) if isinstance(data, list) else len(data.get("data", []))
            print(PASS, f"curl_cffi [{profile}] OK — {count} traders returned")
            any_profile_worked = True
            break
        except Exception as exc:
            err = str(exc)
            is_reset = "35" in err or "reset" in err.lower() or "10054" in err
            tag = "CF/reset" if is_reset else f"HTTP {exc}"
            print(WARN, f"curl_cffi [{profile}] failed ({tag}): {err[:90]}")
        _time.sleep(1.5)

    if any_profile_worked:
        print(PASS, "Leaderboard is reachable. The bot's header fix should resolve the issue.")
    else:
        print()
        print("  ── Diagnosis: all profiles blocked ────────────────────────────")
        print("  Cloudflare is blocking data-api.polymarket.com from your IP/ISP.")
        print("  This is a network-level block, not a credential issue.")
        print()
        print("  Options:")
        print("    a) VPN or residential proxy — routes around the block.")
        print("    b) Cloud VM (AWS/GCP/DigitalOcean) — most cloud IPs are unblocked.")
        print("    c) Arb-only mode: set LEADERBOARD_MIN_PROFIT=9999999 in .env")
        print("       to skip leaderboard. Arb uses clob.polymarket.com (less filtered).")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _address_from_key(private_key: str) -> str:
    """Derive the Ethereum address from a private key."""
    try:
        from eth_account import Account
        acct = Account.from_key(private_key)
        return acct.address
    except Exception:
        return "<could not derive address — install eth-account>"


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(" Polymarket CLOB Diagnostic")
    print("=" * 60)

    check_public_api()
    check_data_api()

    if not check_sdk_import():
        sys.exit(1)

    private_key = check_private_key()
    if private_key is None:
        sys.exit(1)

    l1_client = check_l1_client(private_key)
    if l1_client is None:
        sys.exit(1)

    creds = check_api_key_derivation(l1_client)
    if creds is None:
        sys.exit(1)

    client, balance = check_balance(private_key, creds)
    if client is None:
        sys.exit(1)

    if balance > 0:
        check_order_build(client)

    print("\n" + "=" * 60)
    print(" Diagnostic complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
