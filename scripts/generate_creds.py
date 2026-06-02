"""
One-time credential generation script.

Run this ONCE to get your API key, secret, and passphrase:
    python scripts/generate_creds.py

Copy the output into your .env file.
These credentials are deterministic — running it again with the same
private key returns the same credentials (or creates new ones if none exist).

Requirements:
    POLY_PRIVATE_KEY must be set in .env (64-char hex Ethereum private key)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

private_key = os.getenv("POLY_PRIVATE_KEY", "").strip()

if not private_key:
    print("ERROR: POLY_PRIVATE_KEY is not set in .env")
    sys.exit(1)

if " " in private_key:
    print("ERROR: POLY_PRIVATE_KEY looks like a seed phrase (spaces found).")
    print("       You need the raw 64-char hex private key, not a mnemonic.")
    sys.exit(1)

if not private_key.startswith("0x"):
    private_key = f"0x{private_key}"

print(f"Using key: 0x{private_key[2:8]}...{private_key[-4:]}")
print("Connecting to Polymarket CLOB to derive credentials...\n")

try:
    from py_clob_client_v2 import ClobClient
except ImportError:
    print("ERROR: py-clob-client-v2 not installed.")
    print("       Run: pip install py-clob-client-v2")
    sys.exit(1)

try:
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=private_key,
    )
    creds = client.create_or_derive_api_key()
except Exception as e:
    print(f"ERROR: Failed to derive credentials: {e}")
    print()
    print("Common causes:")
    print("  - Wrong/malformed private key (must be 64 hex chars)")
    print("  - Network issue reaching clob.polymarket.com")
    sys.exit(1)

print("=" * 55)
print("SUCCESS — paste these into your .env:")
print("=" * 55)
print(f"POLY_API_KEY={creds.api_key}")
print(f"POLY_API_SECRET={creds.api_secret}")
print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
print("=" * 55)
print()
print("Keep these secret. The passphrase cannot be recovered later.")
