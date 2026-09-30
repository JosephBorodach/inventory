"""Inventory tracker. See README for design + wiring."""

import asyncio
import json
import logging
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from viam.components.generic import Generic
from viam.components.sensor import Sensor
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

from .deck import DeckRenderer
from .dispatcher import HomeActionDispatcher

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "~/.viam/inventory.json"
SCHEMA_VERSION = 1

OPENFOODFACTS_URL = "https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
OPENFOODFACTS_TIMEOUT_SEC = 8.0


def _fresh_state() -> dict:
    return {"schema_version": SCHEMA_VERSION, "items": []}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _coerce_int(field: str, value: Any, *, min_value: int, err_msg: str | None = None) -> int:
    err = err_msg or (
        f"`{field}` must be a {'positive' if min_value > 0 else 'non-negative'} integer"
    )
    # bool is an int subclass; whole-number floats round-trip from proto as double.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(err)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(err)
    ival = int(value)
    if ival < min_value:
        raise ValueError(err)
    return ival


def _coerce_stripped_string(field: str, value: Any, err_msg: str | None = None) -> str:
    err = err_msg or f"`{field}` must be a non-empty string"
    if not isinstance(value, str) or not value.strip():
        raise ValueError(err)
    return value.strip()


def _require_non_empty_string(field: str, value: Any) -> str:
    return _coerce_stripped_string(field, value)


def _require_positive_int(field: str, value: Any) -> int:
    return _coerce_int(field, value, min_value=1)


def _optional_non_neg_int(field: str, value: Any) -> int | None:
    if value is None:
        return None
    return _coerce_int(
        field, value, min_value=0, err_msg=f"`{field}` must be a non-negative integer or null"
    )


def _validate_deck_pair(page: Any, slot: Any, deck_key_count: int) -> tuple[int | None, int | None]:
    p = _optional_non_neg_int("deck_page", page)
    s = _optional_non_neg_int("deck_slot", slot)
    if (p is None) != (s is None):
        raise ValueError("`deck_page` and `deck_slot` must be provided together or both null")
    if p is not None and p != 0:
        raise ValueError("`deck_page` must be 0 in v1; multi-page not yet supported")
    if s is not None and s >= deck_key_count:
        raise ValueError(f"`deck_slot` must be 0..{deck_key_count - 1}")
    return p, s


def _validate_barcode(value: Any) -> str | None:
    if value is None:
        return None
    return _coerce_stripped_string(
        "barcode", value, err_msg="`barcode` must be a non-empty string or null"
    )


def _validate_threshold(value: Any) -> int | None:
    # Empty string is accepted as null — CLI/MCP paths coerce proto null to "".
    if value is None or value == "":
        return None
    return _coerce_int(
        "threshold",
        value,
        min_value=0,
        err_msg="`threshold` must be a non-negative integer or null",
    )


def _validate_image(value: Any) -> str | None:
    # Empty string is accepted as null — see _validate_threshold for why.
    if value is None or value == "":
        return None
    return _coerce_stripped_string("image", value, err_msg="`image` must be a string or null")


def _require_barcode_input(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("`barcode` is required")
    return _coerce_stripped_string("barcode", payload.get("barcode"))


def _emoji_to_image_name(icon: str) -> str | None:
    """Return the Twemoji-style PNG filename for an emoji, or None for BMP text.

    Base-plane characters (< U+10000) render fine as text; supra-BMP ones
    (most modern emoji) don't and need to be rendered via image. The name is
    built from the first codepoint of the icon — good enough for the single-
    codepoint emojis this tracker's UI encourages; multi-codepoint sequences
    (ZWJ, skin-tone modifiers) would need per-item mapping and aren't needed
    yet.
    """
    if not icon:
        return None
    codepoint = ord(icon[0])
    if codepoint < 0x10000:
        return None
    return f"{codepoint:x}.png"


def _fetch_openfoodfacts(barcode: str) -> dict | None:
    """Fetch a product from Open Food Facts.

    Returns a prefill dict with `name`, `brand`, `image_url` when the product
    exists, or None on any error or when the product isn't found. Runs
    synchronously — callers wrap it in asyncio.to_thread.
    """
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


class Tracker(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("joseph", "inventory"), "tracker")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._state_sensor: Sensor | None = None
        self._state_sensor_name: str = ""
        self._events_sensor: Sensor | None = None
        self._events_sensor_name: str = ""
        self._state_path: str = ""
        self._state: dict = _fresh_state()
        # Constructed once so it survives reconfigure — recreating a lock
        # while a mutation might be holding it would break serialization.
        self._state_lock: asyncio.Lock = asyncio.Lock()
        self._boot_task: asyncio.Task | None = None
        self._dispatcher = HomeActionDispatcher(self.name)
        self._deck = DeckRenderer(
            self.name, lambda: self._state["items"], self._find_item, self._dispatcher
        )

    @classmethod
    def new(
        cls,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> "Tracker":
        t = cls(config.name)
        t.reconfigure(config, dependencies)
        return t

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> tuple[Sequence[str], Sequence[str]]:
        attrs = struct_to_dict(config.attributes)
        state_sensor = attrs.get("state_sensor")
        if not isinstance(state_sensor, str) or not state_sensor:
            raise ValueError("`state_sensor` is required")
        required = [state_sensor]
        optional: list[str] = []
        events_sensor = attrs.get("events_sensor")
        if events_sensor is not None:
            if not isinstance(events_sensor, str) or not events_sensor:
                raise ValueError("`events_sensor` must be a non-empty string")
            required.append(events_sensor)
        optional.extend(DeckRenderer.validate_config_attrs(attrs))
        optional.extend(HomeActionDispatcher.validate_config_attrs(attrs))
        return required, optional

    def reconfigure(
        self,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> None:
        attrs = struct_to_dict(config.attributes)
        self._state_sensor_name = str(attrs["state_sensor"])
        self._events_sensor_name = str(attrs.get("events_sensor") or "")
        self._state_path = str(attrs.get("state_path") or DEFAULT_STATE_PATH)

        self._state_sensor = None
        self._events_sensor = None
        for name, resource in dependencies.items():
            if name.name == self._state_sensor_name and isinstance(resource, Sensor):
                self._state_sensor = resource
            elif (
                self._events_sensor_name
                and name.name == self._events_sensor_name
                and isinstance(resource, Sensor)
            ):
                self._events_sensor = resource
        if self._state_sensor is None:
            raise RuntimeError(f"state_sensor {self._state_sensor_name!r} not found")
        if self._events_sensor_name and self._events_sensor is None:
            LOGGER.warning(
                "events_sensor %r not found among dependencies; change events will not be pushed",
                self._events_sensor_name,
            )
        self._dispatcher.reconfigure(attrs, dependencies)
        self._deck.reconfigure(attrs, dependencies)

        self._state = self._load_state()

        if self._boot_task and not self._boot_task.done():
            self._boot_task.cancel()
        try:
            self._boot_task = asyncio.create_task(self._on_boot())
        except RuntimeError:
            self._boot_task = None

    # -- Persistence --------------------------------------------------

    def _load_state(self) -> dict:
        path = Path(self._state_path).expanduser()
        if not path.exists():
            return _fresh_state()
        try:
            loaded = json.loads(path.read_text())
        except Exception as e:
            self._quarantine_corrupt_state(path, f"unreadable: {e}")
            return _fresh_state()
        if not isinstance(loaded, dict) or not isinstance(loaded.get("items"), list):
            self._quarantine_corrupt_state(path, "bad shape")
            return _fresh_state()
        loaded.setdefault("schema_version", SCHEMA_VERSION)
        return loaded

    def _quarantine_corrupt_state(self, path: Path, reason: str) -> None:
        # Rename the bad file out of the way BEFORE returning empty state,
        # so the next _save_state() doesn't overwrite it with []. Without
        # this, any transient JSON corruption would silently wipe the
        # user's inventory and there'd be no way to recover.
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        backup = path.with_suffix(f"{path.suffix}.corrupt-{ts}")
        try:
            path.rename(backup)
            LOGGER.error(
                "state file %s is corrupt (%s); moved to %s and starting fresh",
                path,
                reason,
                backup,
            )
        except OSError as e:
            LOGGER.error(
                "state file %s is corrupt (%s) and could not be moved aside (%s); "
                "starting fresh — original file WILL BE OVERWRITTEN on next save",
                path,
                reason,
                e,
            )

    def _save_state(self) -> None:
        path = Path(self._state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(path)

    # -- Item helpers -------------------------------------------------

    def _find_item(self, item_id: str) -> dict | None:
        for item in self._state["items"]:
            if item.get("id") == item_id:
                return item
        return None

    def _require_item(self, item_id: str) -> dict:
        item = self._find_item(item_id)
        if item is None:
            raise ValueError(f"no item with id={item_id!r}")
        return item

    def _snapshot_items(self) -> list[dict]:
        return [dict(item) for item in self._state["items"]]

    def _slot_owner(self, page: int | None, slot: int | None) -> dict | None:
        if page is None or slot is None:
            return None
        for item in self._state["items"]:
            if item.get("deck_page") == page and item.get("deck_slot") == slot:
                return item
        return None

    def _reject_reserved_slot(self, slot: int) -> None:
        reserved = self._dispatcher.reserved_slot_map(self._deck.key_count)
        if slot in reserved:
            raise ValueError(
                f"deck slot {slot} is reserved for {reserved[slot]}; pick another slot"
            )

    def _find_item_by_barcode(self, barcode: str) -> dict | None:
        for item in self._state["items"]:
            if item.get("barcode") == barcode:
                return item
        return None

    # -- Mutations ----------------------------------------------------

    async def _add_item(self, payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        name = _require_non_empty_string("name", payload.get("name"))
        package_qty = _require_positive_int("package_qty", payload.get("package_qty"))
        raw_icon = payload.get("icon")
        icon = _require_non_empty_string("icon", raw_icon) if raw_icon not in (None, "") else ""
        deck_page, deck_slot = _validate_deck_pair(
            payload.get("deck_page"), payload.get("deck_slot"), self._deck.key_count
        )
        if deck_slot is not None and self._slot_owner(deck_page, deck_slot) is not None:
            raise ValueError(
                f"deck slot page={deck_page} slot={deck_slot} is already assigned to another item"
            )
        if deck_slot is not None:
            self._reject_reserved_slot(deck_slot)
        barcode = _validate_barcode(payload.get("barcode"))
        image = _validate_image(payload.get("image"))
        threshold = _validate_threshold(payload.get("threshold"))
        now = _now_iso()
        item = {
            "id": _new_id(),
            "name": name,
            "barcode": barcode,
            "quantity": 0,
            "package_qty": package_qty,
            "icon": icon,
            "image": image,
            "threshold": threshold,
            "deck_page": deck_page,
            "deck_slot": deck_slot,
            "created_at": now,
            "updated_at": now,
        }
        async with self._state_lock:
            self._state["items"].append(item)
            self._save_state()
        await self._on_change("item_added", item, delta=0, new_quantity=0)
        return {"ok": True, "item": item}

    async def _edit_item(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        if "quantity" in payload:
            raise ValueError(
                "`quantity` is not editable via edit_item; use increment/decrement/set_quantity"
            )
        item_id = str(payload["id"])
        async with self._state_lock:
            item = self._require_item(item_id)
            new_deck_page = payload["deck_page"] if "deck_page" in payload else item["deck_page"]
            new_deck_slot = payload["deck_slot"] if "deck_slot" in payload else item["deck_slot"]
            deck_page, deck_slot = _validate_deck_pair(
                new_deck_page, new_deck_slot, self._deck.key_count
            )
            if deck_slot is not None:
                owner = self._slot_owner(deck_page, deck_slot)
                if owner is not None and owner.get("id") != item_id:
                    raise ValueError(
                        f"deck slot page={deck_page} slot={deck_slot} is already "
                        f"assigned to another item"
                    )
                self._reject_reserved_slot(deck_slot)
            if "name" in payload:
                item["name"] = _require_non_empty_string("name", payload["name"])
            if "package_qty" in payload:
                item["package_qty"] = _require_positive_int("package_qty", payload["package_qty"])
            if "icon" in payload:
                raw_icon = payload["icon"]
                item["icon"] = (
                    _require_non_empty_string("icon", raw_icon)
                    if raw_icon not in (None, "")
                    else ""
                )
            if "barcode" in payload:
                item["barcode"] = _validate_barcode(payload["barcode"])
            if "image" in payload:
                item["image"] = _validate_image(payload["image"])
            if "threshold" in payload:
                item["threshold"] = _validate_threshold(payload["threshold"])
            item["deck_page"] = deck_page
            item["deck_slot"] = deck_slot
            item["updated_at"] = _now_iso()
            snapshot = dict(item)
            self._save_state()
        await self._on_change("item_edited", snapshot, delta=0, new_quantity=snapshot["quantity"])
        return {"ok": True, "item": snapshot}

    async def _delete_item(self, payload: Any) -> dict:
        item_id = payload if isinstance(payload, str) else (payload or {}).get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("`id` is required")
        async with self._state_lock:
            item = self._require_item(item_id)
            snapshot = dict(item)
            self._state["items"] = [i for i in self._state["items"] if i.get("id") != item_id]
            self._save_state()
        await self._on_change(
            "item_deleted", snapshot, delta=-snapshot.get("quantity", 0), new_quantity=0
        )
        return {"ok": True, "id": item_id}

    async def _adjust_quantity(self, payload: Any, direction: int, event_type: str) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        by_raw = payload.get("by", 1)
        by = _require_positive_int("by", by_raw)
        item_id = str(payload["id"])
        async with self._state_lock:
            item = self._require_item(item_id)
            delta = direction * by
            new_qty = max(0, int(item.get("quantity", 0)) + delta)
            actual_delta = new_qty - int(item.get("quantity", 0))
            item["quantity"] = new_qty
            item["updated_at"] = _now_iso()
            snapshot = dict(item)
            self._save_state()
        await self._on_change(event_type, snapshot, delta=actual_delta, new_quantity=new_qty)
        return {"ok": True, "item": snapshot}

    async def _set_quantity(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        qty_raw = payload.get("quantity")
        if isinstance(qty_raw, bool) or not isinstance(qty_raw, int | float):
            raise ValueError("`quantity` must be a non-negative integer")
        if isinstance(qty_raw, float) and not qty_raw.is_integer():
            raise ValueError("`quantity` must be a non-negative integer")
        qty = int(qty_raw)
        if qty < 0:
            raise ValueError("`quantity` must be a non-negative integer")
        item_id = str(payload["id"])
        async with self._state_lock:
            item = self._require_item(item_id)
            delta = qty - int(item.get("quantity", 0))
            item["quantity"] = qty
            item["updated_at"] = _now_iso()
            snapshot = dict(item)
            self._save_state()
        await self._on_change("item_quantity_set", snapshot, delta=delta, new_quantity=qty)
        return {"ok": True, "item": snapshot}

    # -- State snapshot + events fanout -------------------------------

    async def _on_boot(self) -> None:
        await self._push_state_snapshot()
        await self._deck.push_layout()

    async def _on_change(
        self, event_type: str, item_snapshot: dict, delta: int, new_quantity: int
    ) -> None:
        await self._push_state_snapshot()
        await self._push_change_event(event_type, item_snapshot, delta, new_quantity)
        await self._deck.push_layout()

    async def _push_state_snapshot(self) -> None:
        if self._state_sensor is None:
            return
        # SensorReading doesn't allow None; get_readings coerces null → 0,
        # which would collide with legitimate 0 (e.g. deck_slot=0). Strip
        # nulls so consumers treat "key absent" as null.
        snapshot = {
            "kind": "inventory_snapshot",
            "source": self.name,
            "at": _now_iso(),
            "items": [
                {k: v for k, v in item.items() if v is not None} for item in self._snapshot_items()
            ],
        }
        try:
            await self._state_sensor.do_command({"command": "push_event", "event": snapshot})
        except Exception as e:
            LOGGER.warning("state snapshot push failed: %s", e)

    async def _thermostat_toggle_and_repaint(self, payload: Any) -> dict:
        result = await self._dispatcher.thermostat_toggle(payload)
        await self._deck.push_layout()
        return result

    async def _focus_step(self, payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        delta_raw = payload.get("delta", 0)
        # Streamdeck round-trips deltas through structpb — arrive as float64.
        if isinstance(delta_raw, bool) or not isinstance(delta_raw, int | float):
            raise ValueError("`delta` must be a non-zero integer")
        if isinstance(delta_raw, float) and not delta_raw.is_integer():
            raise ValueError("`delta` must be a non-zero integer")
        delta = int(delta_raw)
        if delta == 0:
            raise ValueError("`delta` must be a non-zero integer")
        item_id = self._deck.focused_item_id
        if item_id is None:
            # Stale press after focus timed out; no-op so the deck doesn't flash red.
            return {"ok": True, "note": "not in focus mode"}
        direction = 1 if delta > 0 else -1
        event_type = "item_incremented" if delta > 0 else "item_decremented"
        result = await self._adjust_quantity(
            {"id": item_id, "by": abs(delta)}, direction=direction, event_type=event_type
        )
        self._deck.rearm_focus_timer()
        return result

    async def _reorder_deck(self, payload: Any) -> dict:
        # Atomic slot reassignment. Given an ordered list of item IDs, assign
        # slots 0..N-1 to them and clear deck_page/deck_slot on any items
        # that were previously on this page but are absent from `order` (so
        # dragging an item OUT of the deck section works too). We do this in
        # one lock so we can't hit spurious slot-collision errors mid-reorder
        # the way sequential edit_item calls would.
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        page_raw = payload.get("page", 0)
        page = _optional_non_neg_int("page", page_raw) or 0
        if page != 0:
            raise ValueError("`page` must be 0 in v1; multi-page not yet supported")
        order = payload.get("order")
        if not isinstance(order, list):
            raise ValueError("`order` must be a list of item ids")
        if len(order) > self._deck.key_count:
            raise ValueError(
                f"`order` has {len(order)} ids but deck only has {self._deck.key_count} keys"
            )
        if len(set(order)) != len(order):
            raise ValueError("`order` contains duplicate ids")
        async with self._state_lock:
            for item_id in order:
                if not isinstance(item_id, str) or not item_id:
                    raise ValueError("every entry in `order` must be a non-empty string id")
                self._require_item(item_id)
            keep = set(order)
            now = _now_iso()
            for item in self._state["items"]:
                if item.get("deck_page") == page and item.get("id") not in keep:
                    item["deck_page"] = None
                    item["deck_slot"] = None
                    item["updated_at"] = now
            for slot, item_id in enumerate(order):
                item = self._require_item(item_id)
                item["deck_page"] = page
                item["deck_slot"] = slot
                item["updated_at"] = now
            self._save_state()
        await self._push_state_snapshot()
        await self._deck.push_layout()
        return {"ok": True}

    async def _press(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        item_id = str(payload["id"])
        self._require_item(item_id)
        new_focus = await self._deck.press(item_id)
        return {"ok": True, "focus_item_id": new_focus}

    async def _push_change_event(
        self, event_type: str, item: dict, delta: int, new_quantity: int
    ) -> None:
        if self._events_sensor is None:
            return
        event = {
            "event_type": event_type,
            "source": self.name,
            "at": _now_iso(),
            "item_id": item.get("id"),
            "item_name": item.get("name"),
            "delta": delta,
            "new_quantity": new_quantity,
        }
        try:
            await self._events_sensor.do_command({"command": "push_event", "event": event})
        except Exception as e:
            LOGGER.warning("change event push failed: %s", e)

    # -- Barcode lookup + scan --------------------------------------

    async def _lookup_barcode_cached(self, barcode: str) -> dict | None:
        cache = self._state.get("barcode_cache") or {}
        cached = cache.get(barcode)
        if isinstance(cached, dict):
            return cached
        prefill = await asyncio.to_thread(_fetch_openfoodfacts, barcode)
        if prefill is None:
            return None
        async with self._state_lock:
            self._state.setdefault("barcode_cache", {})[barcode] = prefill
            self._save_state()
        return prefill

    async def _lookup_barcode(self, payload: Any) -> dict:
        barcode = _require_barcode_input(payload)
        prefill = await self._lookup_barcode_cached(barcode)
        return {
            "ok": True,
            "found": prefill is not None,
            "barcode": barcode,
            "prefill": prefill or {},
        }

    async def _scan_barcode(self, payload: Any) -> dict:
        barcode = _require_barcode_input(payload)
        matched = self._find_item_by_barcode(barcode)
        if matched is not None:
            by = int(matched.get("package_qty") or 1)
            adjust = await self._adjust_quantity(
                {"id": matched["id"], "by": by},
                direction=1,
                event_type="item_incremented",
            )
            return {
                "ok": True,
                "matched": True,
                "item": adjust["item"],
                "added": by,
            }
        prefill = await self._lookup_barcode_cached(barcode)
        return {
            "ok": True,
            "matched": False,
            "barcode": barcode,
            "prefill": prefill or {},
        }

    # -- do_command --------------------------------------------------

    async def _status(self) -> dict:
        return {
            "kind": "inventory_tracker",
            "state_sensor": self._state_sensor_name,
            "events_sensor": self._events_sensor_name or None,
            "streamdeck": self._deck.streamdeck_name or None,
            "deck_key_count": self._deck.key_count,
            "item_count": len(self._state["items"]),
        }

    async def do_command(
        self,
        command: Mapping[str, Any],
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        verb = command.get("command")
        if verb == "status":
            return await self._status()
        if verb == "add_item":
            return await self._add_item(command.get("item") or command)
        if verb == "edit_item":
            return await self._edit_item(command.get("item") or command)
        if verb == "delete_item":
            return await self._delete_item(command)
        if verb == "increment":
            return await self._adjust_quantity(command, direction=1, event_type="item_incremented")
        if verb == "decrement":
            return await self._adjust_quantity(command, direction=-1, event_type="item_decremented")
        if verb == "set_quantity":
            return await self._set_quantity(command)
        if verb == "reorder_deck":
            return await self._reorder_deck(command)
        if verb == "press":
            return await self._press(command)
        if verb == "focus_step":
            return await self._focus_step(command)
        if verb == "water_manual":
            return await self._dispatcher.water_manual(command)
        if verb == "feed_now":
            return await self._dispatcher.feed_now(command)
        if verb == "thermostat_toggle":
            return await self._thermostat_toggle_and_repaint(command)
        if verb == "lookup_barcode":
            return await self._lookup_barcode(command)
        if verb == "scan_barcode":
            return await self._scan_barcode(command)
        raise ValueError(f"unknown command: {verb!r}")
