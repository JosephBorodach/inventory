"""Inventory tracker. See README for design + wiring."""

import asyncio
import json
import logging
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

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = "~/.viam/inventory.json"
SCHEMA_VERSION = 1


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _require_non_empty_string(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{field}` must be a non-empty string")
    return value.strip()


def _require_positive_int(field: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"`{field}` must be a positive integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"`{field}` must be a positive integer")
    ival = int(value)
    if ival <= 0:
        raise ValueError(f"`{field}` must be a positive integer")
    return ival


def _optional_non_neg_int(field: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"`{field}` must be a non-negative integer or null")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"`{field}` must be a non-negative integer or null")
    ival = int(value)
    if ival < 0:
        raise ValueError(f"`{field}` must be a non-negative integer or null")
    return ival


def _validate_deck_pair(page: Any, slot: Any) -> tuple[int | None, int | None]:
    p = _optional_non_neg_int("deck_page", page)
    s = _optional_non_neg_int("deck_slot", slot)
    if (p is None) != (s is None):
        raise ValueError("`deck_page` and `deck_slot` must be provided together or both null")
    return p, s


def _validate_barcode(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("`barcode` must be a non-empty string or null")
    return value.strip()


class Tracker(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("joseph", "inventory"), "tracker")

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._state_sensor: Sensor | None = None
        self._state_sensor_name: str = ""
        self._events_sensor: Sensor | None = None
        self._events_sensor_name: str = ""
        self._state_path: str = ""
        self._state: dict = {"schema_version": SCHEMA_VERSION, "items": []}
        self._state_lock: asyncio.Lock | None = None
        self._boot_task: asyncio.Task | None = None

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
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        attrs = struct_to_dict(config.attributes)
        state_sensor = attrs.get("state_sensor")
        if not isinstance(state_sensor, str) or not state_sensor:
            raise ValueError("`state_sensor` is required")
        deps = [state_sensor]
        events_sensor = attrs.get("events_sensor")
        if events_sensor is not None:
            if not isinstance(events_sensor, str) or not events_sensor:
                raise ValueError("`events_sensor` must be a non-empty string")
            deps.append(events_sensor)
        return deps

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

        self._state = self._load_state()
        self._state_lock = asyncio.Lock()

        if self._boot_task and not self._boot_task.done():
            self._boot_task.cancel()
        try:
            self._boot_task = asyncio.create_task(self._push_state_snapshot())
        except RuntimeError:
            self._boot_task = None

    # -- Persistence --------------------------------------------------

    def _load_state(self) -> dict:
        path = Path(self._state_path).expanduser()
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "items": []}
        try:
            loaded = json.loads(path.read_text())
        except Exception as e:
            LOGGER.warning("failed to load state from %s: %s", path, e)
            return {"schema_version": SCHEMA_VERSION, "items": []}
        if not isinstance(loaded, dict) or not isinstance(loaded.get("items"), list):
            LOGGER.warning("state file %s has bad shape; starting fresh", path)
            return {"schema_version": SCHEMA_VERSION, "items": []}
        loaded.setdefault("schema_version", SCHEMA_VERSION)
        return loaded

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

    # -- Mutations ----------------------------------------------------

    async def _add_item(self, payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        name = _require_non_empty_string("name", payload.get("name"))
        package_qty = _require_positive_int("package_qty", payload.get("package_qty"))
        icon = _require_non_empty_string("icon", payload.get("icon"))
        deck_page, deck_slot = _validate_deck_pair(
            payload.get("deck_page"), payload.get("deck_slot")
        )
        barcode = _validate_barcode(payload.get("barcode"))
        now = _now_iso()
        item = {
            "id": _new_id(),
            "name": name,
            "barcode": barcode,
            "quantity": 0,
            "package_qty": package_qty,
            "icon": icon,
            "deck_page": deck_page,
            "deck_slot": deck_slot,
            "created_at": now,
            "updated_at": now,
        }
        assert self._state_lock is not None
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
        assert self._state_lock is not None
        async with self._state_lock:
            item = self._require_item(item_id)
            new_deck_page = payload["deck_page"] if "deck_page" in payload else item["deck_page"]
            new_deck_slot = payload["deck_slot"] if "deck_slot" in payload else item["deck_slot"]
            deck_page, deck_slot = _validate_deck_pair(new_deck_page, new_deck_slot)
            if "name" in payload:
                item["name"] = _require_non_empty_string("name", payload["name"])
            if "package_qty" in payload:
                item["package_qty"] = _require_positive_int(
                    "package_qty", payload["package_qty"]
                )
            if "icon" in payload:
                item["icon"] = _require_non_empty_string("icon", payload["icon"])
            if "barcode" in payload:
                item["barcode"] = _validate_barcode(payload["barcode"])
            item["deck_page"] = deck_page
            item["deck_slot"] = deck_slot
            item["updated_at"] = _now_iso()
            snapshot = dict(item)
            self._save_state()
        await self._on_change(
            "item_edited", snapshot, delta=0, new_quantity=snapshot["quantity"]
        )
        return {"ok": True, "item": snapshot}

    async def _delete_item(self, payload: Any) -> dict:
        item_id = payload if isinstance(payload, str) else (payload or {}).get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("`id` is required")
        assert self._state_lock is not None
        async with self._state_lock:
            item = self._require_item(item_id)
            snapshot = dict(item)
            self._state["items"] = [i for i in self._state["items"] if i.get("id") != item_id]
            self._save_state()
        await self._on_change(
            "item_deleted", snapshot, delta=-snapshot.get("quantity", 0), new_quantity=0
        )
        return {"ok": True, "id": item_id}

    async def _adjust_quantity(
        self, payload: Any, direction: int, event_type: str
    ) -> dict:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        by_raw = payload.get("by", 1)
        by = _require_positive_int("by", by_raw)
        item_id = str(payload["id"])
        assert self._state_lock is not None
        async with self._state_lock:
            item = self._require_item(item_id)
            delta = direction * by
            new_qty = max(0, int(item.get("quantity", 0)) + delta)
            actual_delta = new_qty - int(item.get("quantity", 0))
            item["quantity"] = new_qty
            item["updated_at"] = _now_iso()
            snapshot = dict(item)
            self._save_state()
        await self._on_change(
            event_type, snapshot, delta=actual_delta, new_quantity=new_qty
        )
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
        assert self._state_lock is not None
        async with self._state_lock:
            item = self._require_item(item_id)
            delta = qty - int(item.get("quantity", 0))
            item["quantity"] = qty
            item["updated_at"] = _now_iso()
            snapshot = dict(item)
            self._save_state()
        await self._on_change(
            "item_quantity_set", snapshot, delta=delta, new_quantity=qty
        )
        return {"ok": True, "item": snapshot}

    # -- State snapshot + events fanout -------------------------------

    async def _on_change(
        self, event_type: str, item_snapshot: dict, delta: int, new_quantity: int
    ) -> None:
        await self._push_state_snapshot()
        await self._push_change_event(event_type, item_snapshot, delta, new_quantity)

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
                {k: v for k, v in item.items() if v is not None}
                for item in self._snapshot_items()
            ],
        }
        try:
            await self._state_sensor.do_command({"command": "push_event", "event": snapshot})
        except Exception as e:
            LOGGER.warning("state snapshot push failed: %s", e)

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
        raise ValueError(f"unknown command: {verb!r}")
