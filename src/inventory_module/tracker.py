"""Inventory tracker. See README for design + wiring."""

import asyncio
import contextlib
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
from viam.components.switch import Switch
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.services.generic import Generic as GenericService
from viam.utils import struct_to_dict

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "~/.viam/inventory.json"
DEFAULT_DECK_KEY_COUNT = 15
DEFAULT_DECK_REFRESH_SEC = 30
DEFAULT_FOCUS_TIMEOUT_SEC = 60.0
FOCUS_MINUS_SLOT = 6
FOCUS_ITEM_SLOT = 7
FOCUS_PLUS_SLOT = 8
# Reserved slots on the deck are counted back from the end so they always
# land on the bottom-right regardless of deck size: water, feed, thermostat.
RESERVED_WATER_OFFSET = 3
RESERVED_FEED_OFFSET = 2
RESERVED_THERMOSTAT_OFFSET = 1
DEFAULT_MANUAL_WATER_ML = 50
DECK_TEXT_FONT = "NotoEmoji-Regular.ttf"
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


def _validate_deck_pair(
    page: Any, slot: Any, deck_key_count: int = DEFAULT_DECK_KEY_COUNT
) -> tuple[int | None, int | None]:
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


def _threshold_color(item: dict) -> str | None:
    threshold = item.get("threshold")
    if threshold is None:
        return None
    qty = int(item.get("quantity", 0))
    return "green" if qty > threshold else "red"


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
        self._streamdeck: GenericService | None = None
        self._streamdeck_name: str = ""
        self._deck_key_count: int = DEFAULT_DECK_KEY_COUNT
        self._state_path: str = ""
        self._state: dict = _fresh_state()
        # Constructed once so it survives reconfigure — recreating a lock
        # while a mutation might be holding it would break serialization.
        self._state_lock: asyncio.Lock = asyncio.Lock()
        self._boot_task: asyncio.Task | None = None
        self._deck_refresh_task: asyncio.Task | None = None
        self._deck_refresh_sec: float = DEFAULT_DECK_REFRESH_SEC
        self._focus_item_id: str | None = None
        self._focus_timeout_sec: float = DEFAULT_FOCUS_TIMEOUT_SEC
        self._focus_timer_task: asyncio.Task | None = None
        self._waterer: GenericService | None = None
        self._waterer_name: str = ""
        self._feeder: GenericService | None = None
        self._feeder_name: str = ""
        self._thermostat_switch: Switch | None = None
        self._thermostat_switch_name: str = ""
        self._manual_water_ml: int = DEFAULT_MANUAL_WATER_ML
        self._thermostat_on: bool | None = None

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
        streamdeck = attrs.get("streamdeck")
        if streamdeck is not None:
            if not isinstance(streamdeck, str) or not streamdeck:
                raise ValueError("`streamdeck` must be a non-empty string")
            # Optional so the streamdeck can declare a hard dep on this tracker
            # (needed for its key callbacks to be able to reach us) without a
            # circular required-dep loop. Optional deps still trigger our
            # reconfigure when they become available.
            optional.append(streamdeck)
        deck_key_count = attrs.get("deck_key_count")
        if deck_key_count is not None and (
            isinstance(deck_key_count, bool)
            or not isinstance(deck_key_count, int | float)
            or deck_key_count <= 0
        ):
            raise ValueError("`deck_key_count` must be a positive integer")
        for key in ("waterer", "feeder", "thermostat_switch"):
            val = attrs.get(key)
            if val is not None:
                if not isinstance(val, str) or not val:
                    raise ValueError(f"`{key}` must be a non-empty string")
                optional.append(val)
        manual_water_ml = attrs.get("manual_water_ml")
        if manual_water_ml is not None and (
            isinstance(manual_water_ml, bool)
            or not isinstance(manual_water_ml, int | float)
            or manual_water_ml <= 0
        ):
            raise ValueError("`manual_water_ml` must be a positive integer")
        return required, optional

    def reconfigure(
        self,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> None:
        attrs = struct_to_dict(config.attributes)
        self._state_sensor_name = str(attrs["state_sensor"])
        self._events_sensor_name = str(attrs.get("events_sensor") or "")
        self._streamdeck_name = str(attrs.get("streamdeck") or "")
        self._deck_key_count = int(attrs.get("deck_key_count") or DEFAULT_DECK_KEY_COUNT)
        self._state_path = str(attrs.get("state_path") or DEFAULT_STATE_PATH)
        self._waterer_name = str(attrs.get("waterer") or "")
        self._feeder_name = str(attrs.get("feeder") or "")
        self._thermostat_switch_name = str(attrs.get("thermostat_switch") or "")
        self._manual_water_ml = int(attrs.get("manual_water_ml") or DEFAULT_MANUAL_WATER_ML)

        self._state_sensor = None
        self._events_sensor = None
        self._streamdeck = None
        self._waterer = None
        self._feeder = None
        self._thermostat_switch = None
        for name, resource in dependencies.items():
            if name.name == self._state_sensor_name and isinstance(resource, Sensor):
                self._state_sensor = resource
            elif (
                self._events_sensor_name
                and name.name == self._events_sensor_name
                and isinstance(resource, Sensor)
            ):
                self._events_sensor = resource
            elif (
                self._streamdeck_name
                and name.name == self._streamdeck_name
                and isinstance(resource, GenericService)
            ):
                self._streamdeck = resource
            elif (
                self._waterer_name
                and name.name == self._waterer_name
                # Waterer/feeder can be either a Generic *component* or a
                # Generic *service* — most existing modules ship as components.
                and isinstance(resource, Generic | GenericService)
            ):
                self._waterer = resource
            elif (
                self._feeder_name
                and name.name == self._feeder_name
                and isinstance(resource, Generic | GenericService)
            ):
                self._feeder = resource
            elif (
                self._thermostat_switch_name
                and name.name == self._thermostat_switch_name
                and isinstance(resource, Switch)
            ):
                self._thermostat_switch = resource
        if self._state_sensor is None:
            raise RuntimeError(f"state_sensor {self._state_sensor_name!r} not found")
        if self._events_sensor_name and self._events_sensor is None:
            LOGGER.warning(
                "events_sensor %r not found among dependencies; change events will not be pushed",
                self._events_sensor_name,
            )
        if self._streamdeck_name and self._streamdeck is None:
            LOGGER.warning(
                "streamdeck %r not found among dependencies; deck fanout disabled",
                self._streamdeck_name,
            )
        if self._waterer_name and self._waterer is None:
            LOGGER.warning(
                "waterer %r not resolved (need Generic component or service); "
                "reserved water key disabled",
                self._waterer_name,
            )
        if self._feeder_name and self._feeder is None:
            LOGGER.warning(
                "feeder %r not resolved (need Generic component or service); "
                "reserved feed key disabled",
                self._feeder_name,
            )
        if self._thermostat_switch_name and self._thermostat_switch is None:
            LOGGER.warning(
                "thermostat_switch %r not resolved (need Switch component); "
                "reserved thermostat key disabled",
                self._thermostat_switch_name,
            )

        self._state = self._load_state()

        self._focus_item_id = None
        self._cancel_focus_timer()

        if self._boot_task and not self._boot_task.done():
            self._boot_task.cancel()
        try:
            self._boot_task = asyncio.create_task(self._on_boot())
        except RuntimeError:
            self._boot_task = None

        if self._deck_refresh_task and not self._deck_refresh_task.done():
            self._deck_refresh_task.cancel()
        with contextlib.suppress(RuntimeError):
            self._deck_refresh_task = asyncio.create_task(self._deck_refresh_loop())

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
        reserved = self._reserved_slot_map()
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
            payload.get("deck_page"), payload.get("deck_slot"), self._deck_key_count
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
                new_deck_page, new_deck_slot, self._deck_key_count
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
        await self._push_full_deck_layout()

    async def _deck_refresh_loop(self) -> None:
        # Viam doesn't reliably re-fire our reconfigure when an optional
        # dep (streamdeck) becomes available after we booted. Refresh the
        # deck layout on a slow cadence so it self-heals — cheap and
        # idempotent (each push is just an update_display of the current
        # snapshot). No-op when the streamdeck dep isn't resolved yet.
        while True:
            try:
                await asyncio.sleep(self._deck_refresh_sec)
            except asyncio.CancelledError:
                return
            if self._streamdeck is None:
                continue
            await self._push_full_deck_layout()

    async def _on_change(
        self, event_type: str, item_snapshot: dict, delta: int, new_quantity: int
    ) -> None:
        await self._push_state_snapshot()
        await self._push_change_event(event_type, item_snapshot, delta, new_quantity)
        await self._push_full_deck_layout()

    async def _push_state_snapshot(self) -> None:
        if self._state_sensor is None:
            return
        # Viam's SensorReading type doesn't allow None; the sensor's
        # get_readings round-trip coerces null → 0, which would collide
        # with legitimate 0 values (e.g. deck_slot=0). Strip null-valued
        # keys so consumers treat "key absent" as null.
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

    def _deck_key_config(self, item: dict) -> dict:
        # Text is "<name> <count>" — the streamdeck module wraps on spaces,
        # so the count naturally falls onto its own line below the name.
        # Font is intentionally the module default (ASCII-safe); emoji fonts
        # can't render supra-BMP glyphs and product photos look muddy at
        # 72×72, so we lean on the always-visible name + count instead.
        cfg: dict[str, Any] = {
            "text": f"{item.get('name', '')} {int(item.get('quantity', 0))}",
            "component": self.name,
            "method": "do_command",
            "args": [{"command": "press", "id": item["id"]}],
        }
        color = _threshold_color(item)
        if color is not None:
            cfg["color"] = color
            cfg["text_color"] = "white"
        return cfg

    def _slotted_items_on_page(self, page: int = 0) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for item in self._state["items"]:
            slot = item.get("deck_slot")
            item_page = item.get("deck_page")
            if slot is None or item_page != page:
                continue
            if 0 <= slot < self._deck_key_count:
                out[slot] = item
        return out

    def _empty_slot_config(self) -> dict:
        # Empty slot still needs component + method to pass the streamdeck
        # module's key validation, and non-empty text (or an image) so the
        # module doesn't reject with "nothing to display for key". Single
        # space renders visually blank while satisfying both checks.
        # Explicitly send empty color/text_color: the streamdeck module
        # MERGES key updates rather than replacing, so without these the
        # previous key's color (e.g. green from a threshold) would persist.
        return {
            "text": " ",
            "color": "",
            "text_color": "",
            "component": self.name,
            "method": "do_command",
            "args": [{"command": "status"}],
        }

    def _focus_control_key(self, text: str, delta: int, color: str) -> dict:
        return {
            "text": text,
            "color": color,
            "text_color": "white",
            "component": self.name,
            "method": "do_command",
            "args": [{"command": "focus_step", "delta": delta}],
        }

    def _focus_item_key(self, item: dict) -> dict:
        # Item cell in focus mode: blank background (no threshold color) so
        # the red/green of the −/+ buttons reads clearly on either side.
        return {
            "text": f"{item.get('name', '')} {int(item.get('quantity', 0))}",
            "color": "",
            "text_color": "",
            "component": self.name,
            "method": "do_command",
            "args": [{"command": "press", "id": item["id"]}],
        }

    def _focus_deck_keys(self, item: dict) -> dict[str, dict]:
        # Focus mode: only the item, minus, and plus are visible on the deck.
        # Minus is red, plus is green; item background is blank so the
        # controls read clearly. Slots 6/7/8 are the middle-row center on
        # a standard 15-key deck; on smaller decks we clamp so it still fits.
        # Text is plain ASCII "-" and "+" — the U+2212 minus glyph doesn't
        # render in the module's default font.
        item_slot = min(FOCUS_ITEM_SLOT, self._deck_key_count - 1)
        minus_slot = max(0, item_slot - 1)
        plus_slot = min(self._deck_key_count - 1, item_slot + 1)
        keys: dict[str, dict] = {}
        for slot in range(self._deck_key_count):
            keys[str(slot)] = self._empty_slot_config()
        if minus_slot != item_slot:
            keys[str(minus_slot)] = self._focus_control_key("-", -1, "red")
        if plus_slot != item_slot:
            keys[str(plus_slot)] = self._focus_control_key("+", 1, "green")
        keys[str(item_slot)] = self._focus_item_key(item)
        return keys

    def _reserved_slot_map(self) -> dict[int, str]:
        # Reserved slots count back from the end so they always sit on the
        # bottom-right of any deck size. Each slot is only reserved when
        # its dep is actually configured — otherwise the slot stays
        # available for inventory items.
        reserved: dict[int, str] = {}
        n = self._deck_key_count
        if self._waterer is not None:
            reserved[n - RESERVED_WATER_OFFSET] = "water"
        if self._feeder is not None:
            reserved[n - RESERVED_FEED_OFFSET] = "feed"
        if self._thermostat_switch is not None:
            reserved[n - RESERVED_THERMOSTAT_OFFSET] = "thermostat"
        return reserved

    def _water_key_config(self) -> dict:
        return {
            "text": f"Water {self._manual_water_ml}ml",
            "color": "",
            "text_color": "",
            "component": self.name,
            "method": "do_command",
            "args": [{"command": "water_manual"}],
        }

    def _feed_key_config(self) -> dict:
        return {
            "text": "Feed",
            "color": "",
            "text_color": "",
            "component": self.name,
            "method": "do_command",
            "args": [{"command": "feed_now"}],
        }

    def _thermostat_key_config(self) -> dict:
        # Label + color show the ACTION (what pressing will do), not the
        # current state. If it's currently on, the button says "Thermostat
        # OFF" (dark) — press to turn it off. If off, says "Thermostat ON"
        # (green) — press to turn it on.
        on = bool(self._thermostat_on)
        target_on = not on
        return {
            "text": "Thermostat ON" if target_on else "Thermostat OFF",
            "color": "green" if target_on else "",
            "text_color": "white" if target_on else "",
            "component": self.name,
            "method": "do_command",
            "args": [{"command": "thermostat_toggle"}],
        }

    def _reserved_slot_config(self, kind: str) -> dict:
        if kind == "water":
            return self._water_key_config()
        if kind == "feed":
            return self._feed_key_config()
        if kind == "thermostat":
            return self._thermostat_key_config()
        return self._empty_slot_config()

    def _main_deck_keys(self) -> dict[str, dict]:
        slotted = self._slotted_items_on_page(0)
        reserved = self._reserved_slot_map()
        keys: dict[str, dict] = {}
        for slot in range(self._deck_key_count):
            if slot in reserved:
                keys[str(slot)] = self._reserved_slot_config(reserved[slot])
                continue
            item = slotted.get(slot)
            if item is not None:
                keys[str(slot)] = self._deck_key_config(item)
            else:
                keys[str(slot)] = self._empty_slot_config()
        return keys

    async def _push_full_deck_layout(self) -> None:
        if self._streamdeck is None:
            return
        focused = self._find_item(self._focus_item_id) if self._focus_item_id is not None else None
        if focused is None and self._focus_item_id is not None:
            # Focused item was deleted or moved — drop focus quietly.
            self._focus_item_id = None
            self._cancel_focus_timer()
        if focused is None:
            # Only refresh thermostat state for the main layout — the
            # focus layout doesn't show reserved keys.
            await self._refresh_thermostat_state()
        keys = self._focus_deck_keys(focused) if focused is not None else self._main_deck_keys()
        try:
            await self._streamdeck.do_command({"update_display": {"keys": keys}})
        except Exception as e:
            LOGGER.warning("deck layout push failed: %s", e)

    def _cancel_focus_timer(self) -> None:
        if self._focus_timer_task and not self._focus_timer_task.done():
            self._focus_timer_task.cancel()
        self._focus_timer_task = None

    def _arm_focus_timer(self) -> None:
        self._cancel_focus_timer()
        with contextlib.suppress(RuntimeError):
            self._focus_timer_task = asyncio.create_task(self._focus_timeout_loop())

    async def _focus_timeout_loop(self) -> None:
        try:
            await asyncio.sleep(self._focus_timeout_sec)
        except asyncio.CancelledError:
            return
        self._focus_item_id = None
        await self._push_full_deck_layout()

    async def _refresh_thermostat_state(self) -> None:
        if self._thermostat_switch is None:
            self._thermostat_on = None
            return
        try:
            pos = await self._thermostat_switch.get_position()
        except Exception as e:
            LOGGER.warning("thermostat state read failed: %s", e)
            return
        self._thermostat_on = pos == 1

    async def _water_manual(self, _payload: Any) -> dict:
        if self._waterer is None:
            raise RuntimeError("no waterer configured")
        return await self._waterer.do_command(
            {"command": "dispense_ml", "ml": self._manual_water_ml}
        )

    async def _feed_now(self, _payload: Any) -> dict:
        if self._feeder is None:
            raise RuntimeError("no feeder configured")
        return await self._feeder.do_command({"command": "feed_now"})

    async def _thermostat_toggle(self, _payload: Any) -> dict:
        if self._thermostat_switch is None:
            raise RuntimeError("no thermostat_switch configured")
        pos = await self._thermostat_switch.get_position()
        new_pos = 0 if pos == 1 else 1
        await self._thermostat_switch.set_position(new_pos)
        self._thermostat_on = new_pos == 1
        # Re-push the layout so the label flips right away instead of
        # waiting for the next 30s deck refresh cycle.
        await self._push_full_deck_layout()
        return {"ok": True, "position": new_pos}

    async def _focus_step(self, payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        delta_raw = payload.get("delta", 0)
        # Deltas coming back from the streamdeck have round-tripped through
        # Go/structpb and arrive as float64. Accept whole-number floats.
        if isinstance(delta_raw, bool) or not isinstance(delta_raw, int | float):
            raise ValueError("`delta` must be a non-zero integer")
        if isinstance(delta_raw, float) and not delta_raw.is_integer():
            raise ValueError("`delta` must be a non-zero integer")
        delta = int(delta_raw)
        if delta == 0:
            raise ValueError("`delta` must be a non-zero integer")
        if self._focus_item_id is None:
            # Stale key press — treat as no-op rather than an error so the
            # deck's built-in error surface doesn't flash.
            return {"ok": True, "note": "not in focus mode"}
        item_id = self._focus_item_id
        direction = 1 if delta > 0 else -1
        event_type = "item_incremented" if delta > 0 else "item_decremented"
        by = abs(delta)
        result = await self._adjust_quantity(
            {"id": item_id, "by": by}, direction=direction, event_type=event_type
        )
        self._arm_focus_timer()
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
        if len(order) > self._deck_key_count:
            raise ValueError(
                f"`order` has {len(order)} ids but deck only has {self._deck_key_count} keys"
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
        await self._push_full_deck_layout()
        return {"ok": True}

    async def _press(self, payload: Any) -> dict:
        # Item key on the deck. Toggles focus mode: first press zooms in on
        # the item with −/+ controls; pressing the item again exits back to
        # the full grid. −/+ within focus mode go through `focus_step` and
        # reset the auto-return timer.
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        item_id = str(payload["id"])
        self._require_item(item_id)
        if self._focus_item_id == item_id:
            self._focus_item_id = None
            self._cancel_focus_timer()
        else:
            self._focus_item_id = item_id
            self._arm_focus_timer()
        await self._push_full_deck_layout()
        return {"ok": True, "focus_item_id": self._focus_item_id}

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
            "streamdeck": self._streamdeck_name or None,
            "deck_key_count": self._deck_key_count,
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
            return await self._water_manual(command)
        if verb == "feed_now":
            return await self._feed_now(command)
        if verb == "thermostat_toggle":
            return await self._thermostat_toggle(command)
        if verb == "lookup_barcode":
            return await self._lookup_barcode(command)
        if verb == "scan_barcode":
            return await self._scan_barcode(command)
        raise ValueError(f"unknown command: {verb!r}")
