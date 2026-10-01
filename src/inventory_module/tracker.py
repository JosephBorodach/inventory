"""Inventory tracker. See README for design + wiring."""

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from viam.components.generic import Generic
from viam.components.sensor import Sensor
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

from .barcode import BarcodeLookup
from .deck import DeckRenderer
from .dispatcher import HomeActionDispatcher

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "~/.viam/inventory.json"
SCHEMA_VERSION = 1


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


def _validate_button(value: Any, devices: dict[str, int | None]) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("`button` must be an object with `device` and `slot`")
    device = _coerce_stripped_string("button.device", value.get("device"))
    if device not in devices:
        raise ValueError(
            f"`button.device` {device!r} is not declared; known devices: {sorted(devices)}"
        )
    slot = _coerce_int("button.slot", value.get("slot"), min_value=0)
    key_count = devices[device]
    if key_count is not None and slot >= key_count:
        raise ValueError(f"`button.slot` must be 0..{key_count - 1} for device {device!r}")
    return {"device": device, "slot": slot}


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


def _validate_routine(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("`routine` must be an object with `interval_days`")
    interval_days = _require_positive_int("routine.interval_days", value.get("interval_days"))
    last = value.get("last_done_at")
    if last is not None and not isinstance(last, str):
        raise ValueError("`routine.last_done_at` must be an ISO string or null")
    if isinstance(last, str) and last:
        try:
            datetime.fromisoformat(last)
        except ValueError as e:
            raise ValueError(f"`routine.last_done_at` is not a valid ISO timestamp: {e}") from e
    return {"interval_days": interval_days, "last_done_at": last if last else None}


def _routine_actionable(routine: dict | None, now: datetime | None = None) -> bool:
    if routine is None:
        return False
    last_iso = routine.get("last_done_at")
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    now = now or datetime.now(UTC)
    return (now - last) >= timedelta(days=int(routine.get("interval_days", 1)))


def _parse_devices(attrs: dict) -> list[dict]:
    raw = attrs.get("devices")
    if raw is None:
        streamdeck = attrs.get("streamdeck")
        if not streamdeck:
            return []
        return [
            {
                "name": "kitchen",
                "streamdeck": _coerce_stripped_string("streamdeck", streamdeck),
                "key_count": int(attrs.get("deck_key_count") or 15),
            }
        ]
    if not isinstance(raw, list):
        raise ValueError("`devices` must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("each entry in `devices` must be an object")
        name = _coerce_stripped_string("devices[].name", entry.get("name"))
        if name in seen:
            raise ValueError(f"duplicate device name {name!r}")
        seen.add(name)
        # Virtual devices (grouping only, no physical Stream Deck) omit both.
        streamdeck_raw = entry.get("streamdeck")
        key_count_raw = entry.get("key_count")
        if streamdeck_raw is None and key_count_raw is None:
            out.append({"name": name, "streamdeck": None, "key_count": None})
            continue
        if streamdeck_raw is None or key_count_raw is None:
            raise ValueError(
                f"device {name!r}: `streamdeck` and `key_count` must both be set, "
                "or both omitted for a virtual (grouping-only) device"
            )
        streamdeck = _coerce_stripped_string("devices[].streamdeck", streamdeck_raw)
        key_count = _require_positive_int("devices[].key_count", key_count_raw)
        out.append({"name": name, "streamdeck": streamdeck, "key_count": key_count})
    return out


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
        self._devices: list[dict] = []
        self._dispatcher = HomeActionDispatcher(self.name)
        self._deck = DeckRenderer(
            self.name, lambda: self._state["items"], self._find_item, self._dispatcher
        )
        self._barcode = BarcodeLookup(
            lambda: self._state,
            self._state_lock,
            self._save_state,
            self._find_item_by_barcode,
            self._adjust_quantity,
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
        for d in _parse_devices(attrs):
            sd = d["streamdeck"]
            if sd and sd not in optional:
                optional.append(sd)
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
        self._devices = _parse_devices(attrs)
        self._dispatcher.reconfigure(attrs, dependencies)
        self._deck.reconfigure(attrs, dependencies)

        self._state = self._load_state()
        self._migrate_items()

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

    def _devices_map(self) -> dict[str, int | None]:
        return {d["name"]: d["key_count"] for d in self._devices}

    def _button_owner(self, button: dict | None) -> dict | None:
        if button is None:
            return None
        for item in self._state["items"]:
            b = item.get("button")
            if b and b.get("device") == button["device"] and b.get("slot") == button["slot"]:
                return item
        return None

    def _reject_reserved_button(self, button: dict) -> None:
        reserved = self._dispatcher.reserved_slot_map_for(
            button["device"], self._devices_map().get(button["device"], 0)
        )
        if button["slot"] in reserved:
            raise ValueError(
                f"deck slot {button['slot']} is reserved for {reserved[button['slot']]}; "
                f"pick another slot"
            )

    def _migrate_items(self) -> None:
        dirty = False
        for item in self._state["items"]:
            if "button" in item:
                continue
            legacy_slot = item.get("deck_slot")
            legacy_page = item.get("deck_page")
            if legacy_slot is not None and legacy_page == 0 and "kitchen" in self._devices_map():
                item["button"] = {"device": "kitchen", "slot": int(legacy_slot)}
            else:
                item["button"] = None
            item.pop("deck_page", None)
            item.pop("deck_slot", None)
            dirty = True
        if dirty:
            self._save_state()

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
        raw_pkg = payload.get("package_qty")
        package_qty = _require_positive_int("package_qty", raw_pkg) if raw_pkg is not None else None
        routine = _validate_routine(payload.get("routine"))
        if package_qty is None and routine is None:
            raise ValueError("item must have at least one of `package_qty` or `routine`")
        raw_icon = payload.get("icon")
        icon = _require_non_empty_string("icon", raw_icon) if raw_icon not in (None, "") else ""
        button = _validate_button(payload.get("button"), self._devices_map())
        if button is not None:
            if self._button_owner(button) is not None:
                raise ValueError(f"button {button} is already assigned to another item")
            self._reject_reserved_button(button)
        barcode = _validate_barcode(payload.get("barcode"))
        image = _validate_image(payload.get("image"))
        threshold = _validate_threshold(payload.get("threshold"))
        now = _now_iso()
        item = {
            "id": _new_id(),
            "name": name,
            "barcode": barcode,
            "quantity": 0 if package_qty is not None else None,
            "package_qty": package_qty,
            "icon": icon,
            "image": image,
            "threshold": threshold,
            "button": button,
            "routine": routine,
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
            new_button = payload["button"] if "button" in payload else item.get("button")
            button = _validate_button(new_button, self._devices_map())
            if button is not None:
                owner = self._button_owner(button)
                if owner is not None and owner.get("id") != item_id:
                    raise ValueError(f"button {button} is already assigned to another item")
                self._reject_reserved_button(button)
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
            if "routine" in payload:
                item["routine"] = _validate_routine(payload["routine"])
            item["button"] = button
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
            if item.get("package_qty") is None:
                raise ValueError(f"item {item_id!r} has no supply tracking; can't adjust quantity")
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
            if item.get("package_qty") is None:
                raise ValueError(f"item {item_id!r} has no supply tracking; can't set quantity")
            delta = qty - int(item.get("quantity", 0))
            item["quantity"] = qty
            item["updated_at"] = _now_iso()
            snapshot = dict(item)
            self._save_state()
        await self._on_change("item_quantity_set", snapshot, delta=delta, new_quantity=qty)
        return {"ok": True, "item": snapshot}

    async def _set_routine(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        item_id = str(payload["id"])
        routine = _validate_routine(payload.get("routine"))
        if routine is None:
            raise ValueError("`routine` is required (use clear_routine to remove)")
        async with self._state_lock:
            item = self._require_item(item_id)
            item["routine"] = routine
            item["updated_at"] = _now_iso()
            snapshot = dict(item)
            self._save_state()
        await self._on_change(
            "item_routine_set", snapshot, delta=0, new_quantity=snapshot.get("quantity") or 0
        )
        return {"ok": True, "item": snapshot}

    async def _clear_routine(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        item_id = str(payload["id"])
        async with self._state_lock:
            item = self._require_item(item_id)
            if item.get("package_qty") is None:
                raise ValueError(f"item {item_id!r} would have no capabilities; delete it instead")
            item["routine"] = None
            item["updated_at"] = _now_iso()
            snapshot = dict(item)
            self._save_state()
        await self._on_change(
            "item_routine_cleared", snapshot, delta=0, new_quantity=snapshot.get("quantity") or 0
        )
        return {"ok": True, "item": snapshot}

    async def _mark_routine_done(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        item_id = str(payload["id"])
        now = _now_iso()
        async with self._state_lock:
            item = self._require_item(item_id)
            routine = item.get("routine")
            if routine is None:
                raise ValueError(f"item {item_id!r} has no routine")
            routine["last_done_at"] = now
            # Hybrid items: one press = "I used one" = mark done + decrement.
            if item.get("package_qty") is not None:
                current = int(item.get("quantity") or 0)
                item["quantity"] = max(0, current - 1)
            item["updated_at"] = now
            snapshot = dict(item)
            self._save_state()
        await self._on_change(
            "item_routine_done",
            snapshot,
            delta=-1 if item.get("package_qty") is not None else 0,
            new_quantity=snapshot.get("quantity") or 0,
        )
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
        # Reassign under one lock — sequential edit_item calls would hit
        # spurious slot-collision errors when swapping two items.
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        devices = self._devices_map()
        device = payload.get("device") or "kitchen"
        device = _coerce_stripped_string("device", device)
        if device not in devices:
            raise ValueError(f"device {device!r} is not declared; known devices: {sorted(devices)}")
        key_count = devices[device]
        order = payload.get("order")
        if not isinstance(order, list):
            raise ValueError("`order` must be a list of item ids")
        if key_count is not None and len(order) > key_count:
            raise ValueError(
                f"`order` has {len(order)} ids but device {device!r} only has {key_count} keys"
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
                b = item.get("button")
                if b and b.get("device") == device and item.get("id") not in keep:
                    item["button"] = None
                    item["updated_at"] = now
            for slot, item_id in enumerate(order):
                item = self._require_item(item_id)
                item["button"] = {"device": device, "slot": slot}
                item["updated_at"] = now
            self._save_state()
        await self._push_state_snapshot()
        await self._deck.push_layout()
        return {"ok": True}

    async def _press(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        item_id = str(payload["id"])
        item = self._require_item(item_id)
        if item.get("routine") is not None:
            return await self._mark_routine_done({"id": item_id})
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
        if verb == "set_routine":
            return await self._set_routine(command)
        if verb == "clear_routine":
            return await self._clear_routine(command)
        if verb == "mark_routine_done":
            return await self._mark_routine_done(command)
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
            return await self._barcode.lookup(command)
        if verb == "scan_barcode":
            return await self._barcode.scan(command)
        raise ValueError(f"unknown command: {verb!r}")
