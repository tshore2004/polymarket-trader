#!/usr/bin/env python3
"""
Polymarket bot connectivity diagnostic.
Run: python diagnose.py

Tests each endpoint and the SDK auth in isolation so you can pinpoint
exactly which part is failing before running the full bot.
"""
from __future__ import annotations
import sys
import time
import logging

# Suppress noisy SDK logs during tests
logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

SEP = "-" * 60


def section(title: str) -> None:
    print(f"\n{SEP}\n  {title}\n{SEP}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


# ── Test 1: curl_cffi direct connections ────────────────────────────────────

def test_curl_cffi_direct() -> None:
    section("Test 1: curl_cffi direct connections (Chrome impersonation)")
    try:
        from curl_cffi import requests as cf
    except ImportError:
        fail("curl_cffi not installed — run: pip install 'curl_cffi>=0.7.0'")
        return

    endpoints = [
        ("Gamma API", "https://gamma-api.polymarket.com/markets?limit=1&active=true&closed=false"),
        ("Data API ", "https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=1&offset=0"),
        ("CLOB API ", "https://clob.polymarket.com/book?token_id=21742633143463906290569050155826241533067272736897614950488156847949938836455"),
    ]

    for profile in ("chrome136", "chrome124"):
        print(f"\n  Profile: {profile}")
        for name, url in endpoints:
            try:
                s = cf.Session(impersonate=profile)
                resp = s.get(url, timeout=12)
                ok(f"{name}: HTTP {resp.status_code}")
            except Exception as exc:
                fail(f"{name}: {exc}")
            time.sleep(0.4)


# ── Test 2: plain requests (expected to fail on Windows) ────────────────────

def test_plain_requests() -> None:
    section("Test 2: Plain requests library (expected to fail — Cloudflare blocks it)")
    # Import the real requests before any monkey-patch might be applied
    try:
        import importlib
        real_req = importlib.import_module("requests")
        resp = real_req.get(
            "https://clob.polymarket.com/book?token_id=21742633143463906290569050155826241533067272736897614950488156847949938836455",
            timeout=10,
        )
        ok(f"CLOB with plain requests: HTTP {resp.status_code}")
        info("Cloudflare is NOT blocking plain requests on this machine.")
    except Exception as exc:
        fail(f"CLOB with plain requests: {exc}")
        info("Expected on Windows — this is the root cause of WinError 10054 in py_clob_client_v2.")


# ── Test 3: private key + API credentials ───────────────────────────────────

def test_credentials() -> bool:
    section("Test 3: .env credentials")
    from dotenv import load_dotenv
    import os
    load_dotenv()

    pk = os.getenv("POLY_PRIVATE_KEY", "").strip()
    if not pk:
        fail("POLY_PRIVATE_KEY not set in .env")
        return False

    hex_part = pk.lstrip("0x").lstrip("0X")
    n = len(hex_part)
    if n == 64:
        ok(f"POLY_PRIVATE_KEY: {n} hex chars — correct (32-byte private key)")
    elif n == 40:
        fail(f"POLY_PRIVATE_KEY: {n} hex chars — this is a wallet ADDRESS, not a private key!")
        info("Fix: MetaMask → Account → ⋮ → Account details → Show private key → copy 64-char hex")
        return False
    else:
        fail(f"POLY_PRIVATE_KEY: {n} hex chars — unexpected length (need exactly 64)")
        return False

    api_key = os.getenv("POLY_API_KEY", "")
    api_secret = os.getenv("POLY_API_SECRET", "")
    api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")

    if all([api_key, api_secret, api_passphrase]):
        ok(f"API credentials: all three present (key={api_key[:12]}...)")
    else:
        missing = [k for k, v in [
            ("POLY_API_KEY", api_key),
            ("POLY_API_SECRET", api_secret),
            ("POLY_API_PASSPHRASE", api_passphrase),
        ] if not v]
        fail(f"Missing API credentials: {', '.join(missing)}")
        info("Bot will attempt to derive them from POLY_PRIVATE_KEY — requires network access.")

    return True


# ── Test 4: SDK balance WITHOUT curl_cffi patch ──────────────────────────────

def test_sdk_no_patch() -> bool:
    section("Test 4: SDK balance fetch WITHOUT curl_cffi patch")
    info("Uses py_clob_client_v2 with its default requests transport.")
    from dotenv import load_dotenv
    import os
    load_dotenv()

    pk = os.getenv("POLY_PRIVATE_KEY", "").strip()
    if len(pk.lstrip("0x")) != 64:
        fail("Skipping — invalid private key (see Test 3)")
        return False

    try:
        from py_clob_client_v2 import ClobClient, ApiCreds, BalanceAllowanceParams, AssetType

        api_key = os.getenv("POLY_API_KEY", "")
        api_secret = os.getenv("POLY_API_SECRET", "")
        api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")

        if all([api_key, api_secret, api_passphrase]):
            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        else:
            info("Deriving credentials from private key...")
            tmp = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk)
            creds = tmp.create_or_derive_api_key()
            info(f"Derived key: {creds.api_key[:12]}...")

        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            creds=creds,
            signature_type=1,
        )
        resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        ok(f"Balance response: {resp}")
        return True
    except Exception as exc:
        fail(f"SDK error: {exc}")
        return False


# ── Test 5: SDK balance WITH curl_cffi patch ─────────────────────────────────

def test_sdk_with_patch() -> None:
    section("Test 5: SDK balance fetch WITH curl_cffi patch")
    info("Replaces sys.modules['requests'] with curl_cffi, then re-tests SDK.")

    import types
    from curl_cffi import requests as _cf_req

    mod = types.ModuleType("requests")
    for attr in dir(_cf_req):
        try:
            setattr(mod, attr, getattr(_cf_req, attr))
        except Exception:
            pass

    class _Session(_cf_req.Session):
        def __init__(self, *a, **kw):
            kw.setdefault("impersonate", "chrome136")
            super().__init__(*a, **kw)

    mod.Session = _Session
    for method in ("get", "post", "put", "patch", "delete", "head", "options"):
        orig = getattr(_cf_req, method, None)
        if orig:
            def _wrap(fn):
                def _inner(*a, **kw):
                    kw.setdefault("impersonate", "chrome136")
                    return fn(*a, **kw)
                return _inner
            setattr(mod, method, _wrap(orig))

    # Unload py_clob modules so they re-import against the patched requests
    to_unload = [k for k in sys.modules if k.startswith("py_clob")]
    for k in to_unload:
        del sys.modules[k]
    sys.modules["requests"] = mod

    from dotenv import load_dotenv
    import os
    load_dotenv()

    pk = os.getenv("POLY_PRIVATE_KEY", "").strip()
    if len(pk.lstrip("0x")) != 64:
        fail("Skipping — invalid private key (see Test 3)")
        return

    try:
        from py_clob_client_v2 import ClobClient, ApiCreds, BalanceAllowanceParams, AssetType

        api_key = os.getenv("POLY_API_KEY", "")
        api_secret = os.getenv("POLY_API_SECRET", "")
        api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")

        if all([api_key, api_secret, api_passphrase]):
            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        else:
            info("Deriving credentials from private key...")
            tmp = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk)
            creds = tmp.create_or_derive_api_key()
            info(f"Derived key: {creds.api_key[:12]}...")

        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            creds=creds,
            signature_type=1,
        )
        resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        ok(f"Balance response: {resp}")
        ok("curl_cffi patch WORKS -- the fix in main.py will resolve balance fetch errors.")
    except Exception as exc:
        fail(f"SDK with patch error: {exc}")
        info("The patch may have an API incompatibility with this SDK version.")
        info("Check: pip show py-clob-client-v2 and curl_cffi for version details.")


# ── Test 6: CLOB concurrent requests ────────────────────────────────────────

def test_clob_concurrent() -> None:
    section("Test 6: CLOB concurrent requests (simulates arb detector)")
    info("Sends 6 parallel CLOB orderbook requests to check for rate-limit resets.")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from curl_cffi import requests as cf

    token_ids = [
        "21742633143463906290569050155826241533067272736897614950488156847949938836455",
        "48331043336612883890938759509493159234755048973500640148014422747788308965732",
        "64533579809297525579033609963634939501013332859992608996100633472507000251907",
        "15540133404064485946536607974212890170021691204987131841181394872998839987451",
        "69236923620077691027083946871148646972011131466059644796654161903044801437426",
        "71321045679252212594626385532706912750332728571942532289631379312455583992563",
    ]

    def fetch(token_id: str) -> tuple[str, int | str]:
        s = cf.Session(impersonate="chrome136")
        try:
            resp = s.get(
                f"https://clob.polymarket.com/book?token_id={token_id}",
                timeout=12,
            )
            return token_id[:12], resp.status_code
        except Exception as exc:
            return token_id[:12], str(exc)[:80]

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch, tid): tid for tid in token_ids}
        for fut in as_completed(futures):
            tid_short, result = fut.result()
            if isinstance(result, int) and result == 200:
                ok(f"token {tid_short}...: HTTP {result}")
            else:
                fail(f"token {tid_short}...: {result}")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Polymarket Bot — Connectivity Diagnostic")
    print("=" * 60)

    test_curl_cffi_direct()
    test_plain_requests()
    test_credentials()
    test_sdk_no_patch()
    test_sdk_with_patch()
    test_clob_concurrent()

    print(f"\n{'=' * 60}")
    print("Diagnostic complete. Fix any ✗ items before running the bot.")
