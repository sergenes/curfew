"""MAC address vendor lookup from the IEEE OUI registry, plus randomized MAC detection.

The OUI table is downloaded on demand (curfew vendors update) and cached in the data
directory. Without the table, lookups return "" and everything else keeps working.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

OUI_URL = "https://standards-oui.ieee.org/oui/oui.csv"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36"
)
_HEX = re.compile(r"[^0-9A-F]")


def normalize_mac(raw: str) -> str:
    """Any separator or none -> 98:DA:C4:00:00:01. Input returned upper cased if not 12 hex digits."""
    hexed = _HEX.sub("", raw.upper())
    if len(hexed) != 12:
        return raw.strip().upper()
    return ":".join(hexed[i : i + 2] for i in range(0, 12, 2))


def is_randomized(mac: str) -> bool:
    """True when the locally administered bit is set: phones with 'private wifi address' do this."""
    hexed = _HEX.sub("", mac.upper())
    if len(hexed) < 2:
        return False
    return bool(int(hexed[1], 16) & 0b10)


class VendorDb:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "oui.csv"
        self._table: dict[str, str] | None = None

    @property
    def available(self) -> bool:
        return self.path.is_file()

    def lookup(self, mac: str) -> str:
        if is_randomized(mac):
            return "(randomized MAC)"
        table = self._load()
        if not table:
            return ""
        prefix = _HEX.sub("", mac.upper())[:6]
        return table.get(prefix, "")

    def _load(self) -> dict[str, str]:
        if self._table is not None:
            return self._table
        self._table = {}
        if not self.path.is_file():
            return self._table
        with self.path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                assignment = (row.get("Assignment") or "").strip().upper()
                org = (row.get("Organization Name") or "").strip()
                if len(assignment) == 6 and org:
                    self._table[assignment] = org
        log.debug("loaded %d OUI entries from %s", len(self._table), self.path)
        return self._table

    async def update(self, client: httpx.AsyncClient | None = None) -> int:
        """Download the IEEE table. Returns the number of entries."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        own = client is None
        client = client or httpx.AsyncClient(timeout=120, follow_redirects=True)
        try:
            # The IEEE site answers 418 to non-browser user agents.
            resp = await client.get(OUI_URL, headers={"User-Agent": BROWSER_UA})
            resp.raise_for_status()
            self.path.write_bytes(resp.content)
        finally:
            if own:
                await client.aclose()
        self._table = None
        return len(self._load())
