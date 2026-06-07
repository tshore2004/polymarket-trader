"""
Starred Traders — JSON-backed persistence for tracking favorite traders.

Stores trader addresses, names, and notes in a local JSON file. Starred
traders get priority treatment: their positions are always loaded even if
they fall off the leaderboard, and their consistency grade is displayed.
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "starred_traders.json")


@dataclass
class StarredTrader:
    address: str
    name: str = ""
    note: str = ""
    starred_at: str = ""  # ISO timestamp

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> StarredTrader:
        return cls(
            address=d.get("address", ""),
            name=d.get("name", ""),
            note=d.get("note", ""),
            starred_at=d.get("starred_at", ""),
        )


class StarredTraderStore:
    """Load/save starred traders from a JSON file."""

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._traders: dict[str, StarredTrader] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
            for entry in data:
                st = StarredTrader.from_dict(entry)
                if st.address:
                    self._traders[st.address.lower()] = st
        except Exception as exc:
            logger.warning("Failed to load starred traders: %s", exc)

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump([t.to_dict() for t in self._traders.values()], f, indent=2)
        except Exception as exc:
            logger.warning("Failed to save starred traders: %s", exc)

    def star(self, address: str, name: str = "", note: str = "") -> StarredTrader:
        """Add or update a starred trader."""
        key = address.lower()
        existing = self._traders.get(key)
        if existing:
            if name:
                existing.name = name
            if note:
                existing.note = note
            self._save()
            return existing
        st = StarredTrader(
            address=address,
            name=name,
            note=note,
            starred_at=datetime.now(timezone.utc).isoformat(),
        )
        self._traders[key] = st
        self._save()
        return st

    def unstar(self, address: str) -> bool:
        """Remove a starred trader. Returns True if found."""
        key = address.lower()
        if key in self._traders:
            del self._traders[key]
            self._save()
            return True
        return False

    def is_starred(self, address: str) -> bool:
        return address.lower() in self._traders

    def get_all(self) -> list[StarredTrader]:
        return list(self._traders.values())

    def get_addresses(self) -> set[str]:
        """Return all starred addresses (lowercase)."""
        return set(self._traders.keys())

    def count(self) -> int:
        return len(self._traders)
