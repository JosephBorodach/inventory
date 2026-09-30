"""Open Food Facts lookup + on-disk barcode cache."""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)

OPENFOODFACTS_URL = "https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
OPENFOODFACTS_TIMEOUT_SEC = 8.0


def require_barcode_input(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("`barcode` is required")
    barcode = payload.get("barcode")
    if not isinstance(barcode, str) or not barcode.strip():
        raise ValueError("`barcode` must be a non-empty string")
    return barcode.strip()


def _fetch_openfoodfacts(barcode: str) -> dict | None:
    """Fetch a product from Open Food Facts, or None on any error / not-found.
    Runs synchronously — callers wrap it in asyncio.to_thread."""
    url = OPENFOODFACTS_URL.format(barcode=barcode)
    try:
        with urllib.request.urlopen(url, timeout=OPENFOODFACTS_TIMEOUT_SEC) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        LOGGER.warning("openfoodfacts fetch failed for %s: %s", barcode, e)
        return None
    if not isinstance(data, dict) or data.get("status") != 1:
        return None
    product = data.get("product") or {}
    prefill: dict[str, Any] = {}
    name = product.get("product_name") or product.get("product_name_en")
    if isinstance(name, str) and name.strip():
        prefill["name"] = name.strip()
    brand = product.get("brands")
    if isinstance(brand, str) and brand.strip():
        prefill["brand"] = brand.strip()
    image = product.get("image_url") or product.get("image_front_url")
    if isinstance(image, str) and image.strip():
        prefill["image_url"] = image.strip()
    return prefill or None


class BarcodeLookup:
    def __init__(
        self,
        get_state: Callable[[], dict],
        state_lock: asyncio.Lock,
        save_state: Callable[[], None],
        find_item_by_barcode: Callable[[str], dict | None],
        adjust_quantity: Callable[..., Any],
    ) -> None:
        self._get_state = get_state
        self._state_lock = state_lock
        self._save_state = save_state
        self._find_item_by_barcode = find_item_by_barcode
        self._adjust_quantity = adjust_quantity

    async def lookup(self, payload: Any) -> dict:
        barcode = require_barcode_input(payload)
        prefill = await self._lookup_cached(barcode)
        return {
            "ok": True,
            "found": prefill is not None,
            "barcode": barcode,
            "prefill": prefill or {},
        }

    async def scan(self, payload: Any) -> dict:
        barcode = require_barcode_input(payload)
        matched = self._find_item_by_barcode(barcode)
        if matched is not None:
            by = int(matched.get("package_qty") or 1)
            result = await self._adjust_quantity(
                {"id": matched["id"], "by": by},
                direction=1,
                event_type="item_incremented",
            )
            return {"ok": True, "matched": True, "item": result["item"], "added": by}
        prefill = await self._lookup_cached(barcode)
        return {
            "ok": True,
            "matched": False,
            "barcode": barcode,
            "prefill": prefill or {},
        }

    async def _lookup_cached(self, barcode: str) -> dict | None:
        cache = self._get_state().get("barcode_cache") or {}
        cached = cache.get(barcode)
        if isinstance(cached, dict):
            return cached
        prefill = await asyncio.to_thread(_fetch_openfoodfacts, barcode)
        if prefill is None:
            return None
        async with self._state_lock:
            self._get_state().setdefault("barcode_cache", {})[barcode] = prefill
            self._save_state()
        return prefill
