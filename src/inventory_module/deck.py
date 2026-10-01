"""Streamdeck layout + focus mode."""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.services.generic import Generic as GenericService

from .dispatcher import HomeActionDispatcher

LOGGER = logging.getLogger(__name__)

DEFAULT_DECK_KEY_COUNT = 15
DEFAULT_DECK_REFRESH_SEC = 30
DEFAULT_FOCUS_TIMEOUT_SEC = 60.0
FOCUS_ITEM_SLOT = 7


def validate_deck_key_count(attrs: dict) -> None:
    v = attrs.get("deck_key_count")
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, int | float) or v <= 0:
        raise ValueError("`deck_key_count` must be a positive integer")


class DeckRenderer:
    def __init__(
        self,
        component_name: str,
        items_getter: Callable[[], list[dict]],
        find_item: Callable[[str], dict | None],
        dispatcher: HomeActionDispatcher,
    ) -> None:
        self._component_name = component_name
        self._items_getter = items_getter
        self._find_item = find_item
        self._dispatcher = dispatcher
        self._streamdeck: GenericService | None = None
        self._streamdeck_name: str = ""
        self._deck_key_count: int = DEFAULT_DECK_KEY_COUNT
        self._deck_refresh_sec: float = DEFAULT_DECK_REFRESH_SEC
        self._deck_refresh_task: asyncio.Task | None = None
        self._focus_item_id: str | None = None
        self._focus_timeout_sec: float = DEFAULT_FOCUS_TIMEOUT_SEC
        self._focus_timer_task: asyncio.Task | None = None

    @staticmethod
    def validate_config_attrs(attrs: dict) -> list[str]:
        optional: list[str] = []
        streamdeck = attrs.get("streamdeck")
        if streamdeck is not None:
            if not isinstance(streamdeck, str) or not streamdeck:
                raise ValueError("`streamdeck` must be a non-empty string")
            # Optional so the streamdeck can hard-depend on the tracker (needed
            # for its key callbacks to reach us) without a circular required-dep
            # loop. Optional deps still trigger reconfigure when they resolve.
            optional.append(streamdeck)
        validate_deck_key_count(attrs)
        return optional

    def reconfigure(self, attrs: dict, dependencies: Mapping[ResourceName, ResourceBase]) -> None:
        self._streamdeck_name = str(attrs.get("streamdeck") or "")
        self._deck_key_count = int(attrs.get("deck_key_count") or DEFAULT_DECK_KEY_COUNT)

        self._streamdeck = None
        for name, resource in dependencies.items():
            if (
                self._streamdeck_name
                and name.name == self._streamdeck_name
                and isinstance(resource, GenericService)
            ):
                self._streamdeck = resource
        if self._streamdeck_name and self._streamdeck is None:
            LOGGER.warning(
                "streamdeck %r not found among dependencies; deck fanout disabled",
                self._streamdeck_name,
            )

        self._focus_item_id = None
        self._cancel_focus_timer()

        if self._deck_refresh_task and not self._deck_refresh_task.done():
            self._deck_refresh_task.cancel()
        with contextlib.suppress(RuntimeError):
            self._deck_refresh_task = asyncio.create_task(self._deck_refresh_loop())

    @property
    def key_count(self) -> int:
        return self._deck_key_count

    @property
    def streamdeck_name(self) -> str:
        return self._streamdeck_name

    @property
    def focused_item_id(self) -> str | None:
        return self._focus_item_id

    async def push_layout(self) -> None:
        if self._streamdeck is None:
            return
        focused = self._find_item(self._focus_item_id) if self._focus_item_id is not None else None
        if focused is None and self._focus_item_id is not None:
            self._focus_item_id = None
            self._cancel_focus_timer()
        if focused is None:
            await self._dispatcher.refresh_thermostat_state()
            # TECH DEBT — see dispatcher.py top-of-file banner.
            await self._dispatcher.refresh_music_state()
        keys = self._focus_layout(focused) if focused is not None else self._main_layout()
        try:
            await self._streamdeck.do_command({"update_display": {"keys": keys}})
        except Exception as e:
            LOGGER.warning("deck layout push failed: %s", e)

    async def press(self, item_id: str) -> str | None:
        if self._focus_item_id == item_id:
            self._focus_item_id = None
            self._cancel_focus_timer()
        else:
            self._focus_item_id = item_id
            self._arm_focus_timer()
        await self.push_layout()
        return self._focus_item_id

    def rearm_focus_timer(self) -> None:
        self._arm_focus_timer()

    async def _deck_refresh_loop(self) -> None:
        # Viam doesn't reliably re-fire reconfigure when an optional dep
        # (streamdeck) becomes available after we booted. Poll to self-heal.
        while True:
            try:
                await asyncio.sleep(self._deck_refresh_sec)
            except asyncio.CancelledError:
                return
            if self._streamdeck is None:
                continue
            await self.push_layout()

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
        await self.push_layout()

    def _slotted_items(self, device: str = "kitchen") -> dict[int, dict]:
        out: dict[int, dict] = {}
        for item in self._items_getter():
            b = item.get("button")
            if not b or b.get("device") != device:
                continue
            slot = b.get("slot")
            if isinstance(slot, int) and 0 <= slot < self._deck_key_count:
                out[slot] = item
        return out

    def _item_key(self, item: dict, *, color: str | None) -> dict:
        name = item.get("name", "")
        has_supply = item.get("package_qty") is not None
        text = f"{name} {int(item.get('quantity') or 0)}" if has_supply else name
        cfg: dict[str, Any] = {
            "text": text,
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "press", "id": item["id"]}],
        }
        if color is not None:
            cfg["color"] = color
            cfg["text_color"] = "white"
        return cfg

    def _empty_slot(self) -> dict:
        # Empty slot needs component + method to pass the streamdeck module's
        # key validation, and non-empty text so it doesn't reject with
        # "nothing to display for key". The streamdeck module MERGES key
        # updates rather than replacing, so we explicitly clear color too —
        # otherwise a previous key's green/red would persist here.
        return {
            "text": " ",
            "color": "",
            "text_color": "",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "status"}],
        }

    def _focus_control(self, text: str, delta: int, color: str) -> dict:
        return {
            "text": text,
            "color": color,
            "text_color": "white",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "focus_step", "delta": delta}],
        }

    def _focus_item(self, item: dict) -> dict:
        cfg = self._item_key(item, color=None)
        cfg["color"] = ""
        cfg["text_color"] = ""
        return cfg

    def _main_layout(self) -> dict[str, dict]:
        slotted = self._slotted_items("kitchen")
        reserved = self._dispatcher.reserved_slot_map(self._deck_key_count)
        keys: dict[str, dict] = {}
        for slot in range(self._deck_key_count):
            if slot in reserved:
                keys[str(slot)] = (
                    self._dispatcher.reserved_slot_config(reserved[slot]) or self._empty_slot()
                )
                continue
            item = slotted.get(slot)
            if item is not None:
                keys[str(slot)] = self._item_key(item, color=_item_color(item))
            else:
                keys[str(slot)] = self._empty_slot()
        return keys

    def _focus_layout(self, item: dict) -> dict[str, dict]:
        # Clamp so −/+/item still fit on smaller decks.
        item_slot = min(FOCUS_ITEM_SLOT, self._deck_key_count - 1)
        minus_slot = max(0, item_slot - 1)
        plus_slot = min(self._deck_key_count - 1, item_slot + 1)
        keys: dict[str, dict] = {}
        for slot in range(self._deck_key_count):
            keys[str(slot)] = self._empty_slot()
        if minus_slot != item_slot:
            keys[str(minus_slot)] = self._focus_control("-", -1, "red")
        if plus_slot != item_slot:
            keys[str(plus_slot)] = self._focus_control("+", 1, "green")
        keys[str(item_slot)] = self._focus_item(item)
        return keys


def _threshold_color(item: dict) -> str | None:
    threshold = item.get("threshold")
    if threshold is None:
        return None
    qty = int(item.get("quantity") or 0)
    return "green" if qty > threshold else "red"


def _item_color(item: dict) -> str | None:
    # Routine actionability trumps threshold — "press me now" beats "getting low".
    routine = item.get("routine")
    if routine is not None:
        last_iso = routine.get("last_done_at")
        actionable = True
        if last_iso:
            try:
                last = datetime.fromisoformat(last_iso)
                actionable = (datetime.now(UTC) - last) >= timedelta(
                    days=int(routine.get("interval_days", 1))
                )
            except ValueError:
                actionable = True
        return "green" if actionable else "gray"
    return _threshold_color(item)
