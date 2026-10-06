"""Reserved streamdeck keys that fire waterer / feeder / thermostat / music commands."""

# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# TECH DEBT — RIP OUT.
# `music` has no business living in the inventory module. It's here because
# the inventory tracker does full-layout pushes to the streamdeck and
# clobbers any key it doesn't own, so a separate music module can't paint
# its own keys on the same deck without inventory wiping them.
# Proper fix: give the inventory tracker an `external_slots` escape hatch
# (don't paint listed slot indices), then move music_play / music_stop /
# music_component / _music_*_key_config into the joseph:spotify module and
# have it paint its own keys. Delete every music_* mention from this file
# and from tracker.py when that lands.
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

import logging
import time
from collections.abc import Mapping
from typing import Any

from viam.components.generic import Generic
from viam.components.switch import Switch
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.services.generic import Generic as GenericService

LOGGER = logging.getLogger(__name__)

RESERVED_WATER_OFFSET = 3
RESERVED_FEED_OFFSET = 2
RESERVED_THERMOSTAT_OFFSET = 1
RESERVED_MUSIC_OFFSET = 4  # TECH DEBT — see top of file.
# Spotify's GET /me/player lags ~1-2s behind PUT /me/player/{play,pause}.
# Trust the optimistic post-mutation state for this long before letting a
# background refresh overwrite it.
MUSIC_STATE_MUTATION_COOLDOWN_SEC = 10.0
# TECH DEBT — see top of file.
MUSIC_PLAYLISTS_CACHE_SEC = 60.0
# 15-key deck constants.
MUSIC_PLAYLISTS_PER_PAGE_15 = 13
MUSIC_FOCUS_PLAYLISTS_SLOT_15 = 2
MUSIC_FOCUS_ACCOUNT_SLOTS_15 = (1, 3)
MUSIC_PLAYLISTS_BACK_SLOT_15 = 10
MUSIC_PLAYLISTS_NEXT_SLOT_15 = 14
MUSIC_PLAYLISTS_CONTENT_SLOTS_15 = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13)
# 32-key (Stream Deck XL, 4x8) deck constants. Cluster centered on cols 2-6.
MUSIC_FOCUS_BASE_XL = 18
MUSIC_FOCUS_PLAYLISTS_SLOT_XL = 12
MUSIC_FOCUS_ACCOUNT_SLOTS_XL = (11, 13)
FEED_CONFIRM_BACK_SLOT_XL = 19
FEED_CONFIRM_CONFIRM_SLOT_XL = 20
MUSIC_PLAYLISTS_CONTENT_SLOTS_XL = (8, 9, 10, 11, 12, 13, 14, 15)
MUSIC_PLAYLISTS_BACK_SLOT_XL = 18
MUSIC_PLAYLISTS_NEXT_SLOT_XL = 22
FOCUS_ITEM_SLOT_15 = 7
FOCUS_ITEM_SLOT_XL = 12
# 6-key (Stream Deck Mini) deck constants.
#   Row 0:  Back       Playlists   Next
#   Row 1:  Vol -      Play/Stop   Vol +
MUSIC_FOCUS_SLOTS_6 = {
    "back": 0, "playlists": 1, "next": 2,
    "vol_down": 3, "play_stop": 4, "vol_up": 5,
}
#   Row 0:  PL1   PL2   PL3
#   Row 1:  Back  PL4   Next
MUSIC_PLAYLISTS_PER_PAGE_6 = 4
MUSIC_PLAYLISTS_BACK_SLOT_6 = 3
MUSIC_PLAYLISTS_NEXT_SLOT_6 = 5
MUSIC_PLAYLISTS_CONTENT_SLOTS_6 = (0, 1, 2, 4)
DEFAULT_MANUAL_WATER_ML = 50


# TECH DEBT — see top of file.
def _is_mini_deck(deck_key_count: int) -> bool:
    return deck_key_count < 10


def _is_xl_deck(deck_key_count: int) -> bool:
    return deck_key_count >= 24


# TECH DEBT — see top of file.
def _music_slot(deck_key_count: int) -> int:
    if _is_mini_deck(deck_key_count):
        return 0  # Top-left on a mini deck.
    return deck_key_count - RESERVED_MUSIC_OFFSET


# TECH DEBT — see top of file.
def _playlists_per_page(deck_key_count: int) -> int:
    if _is_mini_deck(deck_key_count):
        return MUSIC_PLAYLISTS_PER_PAGE_6
    return MUSIC_PLAYLISTS_PER_PAGE_15


_SLOT_INT_FIELDS = {
    "music_focus": (
        "transport_base", "playlists",
        "mini_back", "mini_playlists", "mini_next",
        "mini_vol_down", "mini_play_stop", "mini_vol_up",
    ),
    "feed_confirm": ("back", "confirm"),
    "music_playlists": ("back", "next"),
    "item_focus": ("slot",),
}
_SLOT_LIST_FIELDS = {
    "music_focus": ("accounts",),
    "music_playlists": ("content",),
}


def _is_nonneg_slot_int(v: Any) -> bool:
    # Viam serializes JSON numbers through protobuf Struct, which arrive as
    # float even when the source JSON was an integer literal (1 → 1.0). Accept
    # whole-number floats; reject bools (bool is a subclass of int).
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return v >= 0
    if isinstance(v, float):
        return v.is_integer() and v >= 0
    return False


def _normalize_slots(slots: Mapping) -> dict:
    # _validate_slots lets whole-number floats through; coerce to int here so
    # downstream dict keys built from these values (e.g. {transport_base+0: ...})
    # don't end up as float keys and ship the wrong type to the streamdeck.
    out: dict = {}
    for group, group_val in slots.items():
        if not isinstance(group_val, Mapping):
            continue
        normalized: dict = {}
        for key, val in group_val.items():
            if isinstance(val, float) and val.is_integer():
                normalized[key] = int(val)
            elif isinstance(val, list):
                normalized[key] = [
                    int(x) if isinstance(x, float) and x.is_integer() else x for x in val
                ]
            else:
                normalized[key] = val
        out[group] = normalized
    return out


def _validate_slots(slots: Any) -> None:
    if not isinstance(slots, Mapping):
        raise ValueError("`slots` must be an object")
    for group, group_val in slots.items():
        if group not in _SLOT_INT_FIELDS and group not in _SLOT_LIST_FIELDS:
            raise ValueError(f"`slots.{group}` is not a recognized group")
        if not isinstance(group_val, Mapping):
            raise ValueError(f"`slots.{group}` must be an object")
        for name in _SLOT_INT_FIELDS.get(group, ()):
            if name in group_val and not _is_nonneg_slot_int(group_val[name]):
                raise ValueError(f"`slots.{group}.{name}` must be a non-negative int")
        for name in _SLOT_LIST_FIELDS.get(group, ()):
            if name in group_val:
                val = group_val[name]
                if not isinstance(val, list) or not all(_is_nonneg_slot_int(x) for x in val):
                    raise ValueError(f"`slots.{group}.{name}` must be a list of non-negative ints")


class HomeActionDispatcher:
    def __init__(self, component_name: str) -> None:
        self._component_name = component_name
        self._waterer: GenericService | None = None
        self._waterer_name: str = ""
        self._feeder: GenericService | None = None
        self._feeder_name: str = ""
        self._thermostat_switch: Switch | None = None
        self._thermostat_switch_name: str = ""
        # TECH DEBT — see top of file.
        self._music: GenericService | None = None
        self._music_name: str = ""
        self._music_playing: bool | None = None
        self._music_last_mutation_at: float = 0.0
        self._music_playlists: list[dict] = []
        self._music_playlists_fetched_at: float = 0.0
        self._music_playlists_page: int = 0
        self._music_accounts: list[str] = []
        self._music_active_account: str = ""
        self._manual_water_ml: int = DEFAULT_MANUAL_WATER_ML
        self._thermostat_on: bool | None = None
        self._slots: dict = {}

    @staticmethod
    def validate_config_attrs(attrs: dict) -> list[str]:
        optional: list[str] = []
        # `music` is TECH DEBT — see top of file.
        for key in ("waterer", "feeder", "thermostat_switch", "music"):
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
        slots = attrs.get("slots")
        if slots is not None:
            _validate_slots(slots)
        return optional

    def reconfigure(self, attrs: dict, dependencies: Mapping[ResourceName, ResourceBase]) -> None:
        self._waterer_name = str(attrs.get("waterer") or "")
        self._feeder_name = str(attrs.get("feeder") or "")
        self._thermostat_switch_name = str(attrs.get("thermostat_switch") or "")
        self._music_name = str(attrs.get("music") or "")  # TECH DEBT — see top of file.
        self._manual_water_ml = int(attrs.get("manual_water_ml") or DEFAULT_MANUAL_WATER_ML)
        self._slots = _normalize_slots(attrs.get("slots") or {})

        self._waterer = None
        self._feeder = None
        self._thermostat_switch = None
        self._music = None  # TECH DEBT — see top of file.
        for name, resource in dependencies.items():
            if (
                self._waterer_name
                and name.name == self._waterer_name
                # Waterer/feeder can be either a Generic component or a service.
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
            elif (
                self._music_name
                and name.name == self._music_name
                and isinstance(resource, Generic | GenericService)
            ):
                self._music = resource

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
        if self._music_name and self._music is None:
            LOGGER.warning(
                "music %r not resolved (need Generic component or service); "
                "reserved music keys disabled",
                self._music_name,
            )

    async def water_manual(self, _payload: Any) -> dict:
        if self._waterer is None:
            raise RuntimeError("no waterer configured")
        return await self._waterer.do_command(
            {"command": "dispense_ml", "ml": self._manual_water_ml}
        )

    async def feed_now(self, _payload: Any) -> dict:
        if self._feeder is None:
            raise RuntimeError("no feeder configured")
        return await self._feeder.do_command({"command": "feed_now"})

    async def thermostat_toggle(self, _payload: Any) -> dict:
        if self._thermostat_switch is None:
            raise RuntimeError("no thermostat_switch configured")
        pos = await self._thermostat_switch.get_position()
        new_pos = 0 if pos == 1 else 1
        await self._thermostat_switch.set_position(new_pos)
        self._thermostat_on = new_pos == 1
        return {"ok": True, "position": new_pos}

    # TECH DEBT — see top of file.
    async def music_play(self, _payload: Any) -> dict:
        if self._music is None:
            raise RuntimeError("no music component configured")
        LOGGER.info("music_play: dispatching start to %r", self._music_name)
        try:
            result = await self._music.do_command({"command": "start"})
        except Exception as e:
            LOGGER.error("music_play failed: %s", e)
            raise
        self._music_playing = True
        self._music_last_mutation_at = time.monotonic()
        LOGGER.info("music_play ok: %s", result)
        return result

    # TECH DEBT — see top of file.
    async def music_stop(self, _payload: Any) -> dict:
        if self._music is None:
            raise RuntimeError("no music component configured")
        LOGGER.info("music_stop: dispatching stop to %r", self._music_name)
        try:
            result = await self._music.do_command({"command": "stop"})
        except Exception as e:
            LOGGER.error("music_stop failed: %s", e)
            raise
        self._music_playing = False
        self._music_last_mutation_at = time.monotonic()
        LOGGER.info("music_stop ok: %s", result)
        return result

    # TECH DEBT — see top of file.
    async def music_toggle(self, payload: Any) -> dict:
        # Base the decision on the last known state; falls back to a status
        # probe when we've never seen one so we don't double-start.
        playing = self._music_playing
        if playing is None:
            await self.refresh_music_state()
            playing = bool(self._music_playing)
        if playing:
            return await self.music_stop(payload)
        return await self.music_play(payload)

    # TECH DEBT — see top of file.
    async def music_volume_up(self, _payload: Any) -> dict:
        if self._music is None:
            raise RuntimeError("no music component configured")
        return await self._music.do_command({"command": "volume_up"})

    # TECH DEBT — see top of file.
    async def music_volume_down(self, _payload: Any) -> dict:
        if self._music is None:
            raise RuntimeError("no music component configured")
        return await self._music.do_command({"command": "volume_down"})

    # TECH DEBT — see top of file.
    async def music_next(self, _payload: Any) -> dict:
        if self._music is None:
            raise RuntimeError("no music component configured")
        return await self._music.do_command({"command": "next"})

    # TECH DEBT — see top of file.
    async def music_set_account(self, payload: Any) -> dict:
        if self._music is None:
            raise RuntimeError("no music component configured")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be {name: ...}")
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("`name` must be a non-empty string")
        LOGGER.info("music_set_account: dispatching set_account %r", name)
        try:
            result = await self._music.do_command({"command": "set_account", "name": name})
        except Exception as e:
            LOGGER.error("music_set_account failed: %s", e)
            raise
        self._music_active_account = name
        self._music_last_mutation_at = time.monotonic()
        return result

    # TECH DEBT — see top of file.
    async def refresh_music_playlists(self, force: bool = False) -> None:
        if self._music is None:
            return
        if (
            not force
            and self._music_playlists
            and time.monotonic() - self._music_playlists_fetched_at < MUSIC_PLAYLISTS_CACHE_SEC
        ):
            return
        try:
            result = await self._music.do_command({"command": "playlists"})
        except Exception as e:
            LOGGER.warning("music playlists fetch failed: %s", e)
            return
        if isinstance(result, Mapping):
            items = result.get("items")
            if isinstance(items, list):
                self._music_playlists = [
                    p for p in items if isinstance(p, dict) and p.get("uri") and p.get("name")
                ]
                self._music_playlists_fetched_at = time.monotonic()

    # TECH DEBT — see top of file.
    async def music_play_playlist(self, payload: Any) -> dict:
        if self._music is None:
            raise RuntimeError("no music component configured")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object with `context_uri`")
        uri = payload.get("context_uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError("`context_uri` must be a non-empty string")
        LOGGER.info("music_play_playlist: %s", uri)
        try:
            result = await self._music.do_command({"command": "start", "context_uri": uri})
        except Exception as e:
            LOGGER.error("music_play_playlist failed: %s", e)
            raise
        self._music_playing = True
        self._music_last_mutation_at = time.monotonic()
        return result

    # TECH DEBT — see top of file.
    def _music_playlists_per_page(self, deck_key_count: int) -> int:
        override = self._slots.get("music_playlists") or {}
        content = override.get("content")
        if isinstance(content, list):
            return len(content) or 1
        return _playlists_per_page(deck_key_count)

    # TECH DEBT — see top of file.
    def music_playlists_page_count(self, deck_key_count: int) -> int:
        per_page = self._music_playlists_per_page(deck_key_count)
        n = len(self._music_playlists)
        if n == 0:
            return 1
        return (n + per_page - 1) // per_page

    # TECH DEBT — see top of file.
    def music_playlists_reset_page(self) -> None:
        self._music_playlists_page = 0

    # TECH DEBT — see top of file.
    def music_playlists_advance_page(self, deck_key_count: int) -> int:
        pages = self.music_playlists_page_count(deck_key_count)
        self._music_playlists_page = (self._music_playlists_page + 1) % pages
        return self._music_playlists_page

    # TECH DEBT — see top of file.
    async def refresh_music_state(self) -> None:
        if self._music is None:
            self._music_playing = None
            self._music_accounts = []
            self._music_active_account = ""
            return
        if (
            time.monotonic() - self._music_last_mutation_at
            < MUSIC_STATE_MUTATION_COOLDOWN_SEC
        ):
            return
        try:
            status = await self._music.do_command({"command": "status"})
        except Exception as e:
            LOGGER.warning("music state read failed: %s", e)
            return
        if isinstance(status, Mapping):
            self._music_playing = bool(status.get("is_playing"))
            accounts = status.get("accounts")
            if isinstance(accounts, list):
                self._music_accounts = [
                    a.get("name") for a in accounts
                    if isinstance(a, dict) and isinstance(a.get("name"), str) and a.get("name")
                ]
            active = status.get("active_account")
            if isinstance(active, str):
                self._music_active_account = active

    async def refresh_thermostat_state(self) -> None:
        if self._thermostat_switch is None:
            self._thermostat_on = None
            return
        try:
            pos = await self._thermostat_switch.get_position()
        except Exception as e:
            LOGGER.warning("thermostat state read failed: %s", e)
            return
        self._thermostat_on = pos == 1

    def reserved_slot_map(self, deck_key_count: int) -> dict[int, str]:
        # Count back from the end so slots always sit on the bottom-right.
        reserved: dict[int, str] = {}
        if self._waterer is not None:
            reserved[deck_key_count - RESERVED_WATER_OFFSET] = "water"
        if self._feeder is not None:
            reserved[deck_key_count - RESERVED_FEED_OFFSET] = "feed"
        if self._thermostat_switch is not None:
            reserved[deck_key_count - RESERVED_THERMOSTAT_OFFSET] = "thermostat"
        # TECH DEBT — see top of file. Mini decks anchor Music top-left.
        if self._music is not None:
            reserved[_music_slot(deck_key_count)] = "music"
        return reserved

    def reserved_slot_map_for(self, device: str, key_count: int) -> dict[int, str]:
        return self.reserved_slot_map(key_count)

    def item_focus_slot(self, deck_key_count: int) -> int:
        default = FOCUS_ITEM_SLOT_XL if _is_xl_deck(deck_key_count) else FOCUS_ITEM_SLOT_15
        override = self._slots.get("item_focus") or {}
        return override.get("slot", default)

    def reserved_slot_config(self, kind: str) -> dict | None:
        if kind == "water":
            return self._water_key_config()
        if kind == "feed":
            return self._feed_key_config()
        if kind == "thermostat":
            return self._thermostat_key_config()
        # TECH DEBT — see top of file.
        if kind == "music":
            return self._music_key_config()
        return None

    def _water_key_config(self) -> dict:
        return {
            "text": f"Water {self._manual_water_ml}ml",
            "color": "",
            "text_color": "",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "water_manual"}],
        }

    def _feed_key_config(self) -> dict:
        return {
            "text": "Feed",
            "color": "",
            "text_color": "",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "feed_show_confirm"}],
        }

    def feed_confirm_key_configs(self, deck_key_count: int) -> dict[int, dict]:
        if _is_xl_deck(deck_key_count):
            default_back = FEED_CONFIRM_BACK_SLOT_XL
            default_confirm = FEED_CONFIRM_CONFIRM_SLOT_XL
        else:
            default_back = deck_key_count - RESERVED_WATER_OFFSET
            default_confirm = deck_key_count - RESERVED_FEED_OFFSET
        override = self._slots.get("feed_confirm") or {}
        back_slot = override.get("back", default_back)
        confirm_slot = override.get("confirm", default_confirm)
        return {
            back_slot: {
                "text": "Back",
                "color": "red",
                "text_color": "white",
                "component": self._component_name,
                "method": "do_command",
                "args": [{"command": "feed_cancel"}],
            },
            confirm_slot: {
                "text": "Confirm",
                "color": "seagreen",
                "text_color": "white",
                "component": self._component_name,
                "method": "do_command",
                "args": [{"command": "feed_now"}],
            },
        }

    def _thermostat_key_config(self) -> dict:
        # Label shows the action, not the current state.
        on = bool(self._thermostat_on)
        target_on = not on
        return {
            "text": "Thermostat ON" if target_on else "Thermostat OFF",
            "color": "seagreen" if target_on else "",
            "text_color": "white" if target_on else "",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "thermostat_toggle"}],
        }

    # TECH DEBT — see top of file.
    def _music_key_config(self) -> dict:
        # Tapping opens a focus "drawer" with Back / - / Play-Stop / + / Next.
        return {
            "text": "Music",
            "color": "",
            "text_color": "",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "music_focus_enter"}],
        }

    # TECH DEBT — see top of file.
    def music_focus_key_configs(self, deck_key_count: int) -> dict[int, dict]:
        playing = bool(self._music_playing)
        toggle_target_play = not playing
        toggle_text = "Play" if toggle_target_play else "Stop"
        toggle_color = "seagreen" if toggle_target_play else "red"
        back = {
            "text": "Back", "color": "", "text_color": "",
            "component": self._component_name, "method": "do_command",
            "args": [{"command": "music_focus_exit"}],
        }
        vol_down = {
            "text": "Vol -", "color": "", "text_color": "",
            "component": self._component_name, "method": "do_command",
            "args": [{"command": "music_volume_down"}],
        }
        play_stop = {
            "text": toggle_text, "color": toggle_color, "text_color": "white",
            "component": self._component_name, "method": "do_command",
            "args": [{"command": "music_toggle"}],
        }
        vol_up = {
            "text": "Vol +", "color": "", "text_color": "",
            "component": self._component_name, "method": "do_command",
            "args": [{"command": "music_volume_up"}],
        }
        next_track = {
            "text": "Next", "color": "", "text_color": "",
            "component": self._component_name, "method": "do_command",
            "args": [{"command": "music_next"}],
        }
        playlists = {
            "text": "Playlists", "color": "", "text_color": "",
            "component": self._component_name, "method": "do_command",
            "args": [{"command": "music_playlists_enter"}],
        }

        if _is_mini_deck(deck_key_count):
            mini_override = self._slots.get("music_focus") or {}
            s = {
                role: mini_override.get(f"mini_{role}", default)
                for role, default in MUSIC_FOCUS_SLOTS_6.items()
            }
            return {
                s["back"]: back,
                s["playlists"]: playlists,
                s["next"]: next_track,
                s["vol_down"]: vol_down,
                s["play_stop"]: play_stop,
                s["vol_up"]: vol_up,
            }

        if _is_xl_deck(deck_key_count):
            default_base = MUSIC_FOCUS_BASE_XL
            default_playlists = MUSIC_FOCUS_PLAYLISTS_SLOT_XL
            default_accounts = MUSIC_FOCUS_ACCOUNT_SLOTS_XL
        else:
            default_base = 5
            default_playlists = MUSIC_FOCUS_PLAYLISTS_SLOT_15
            default_accounts = MUSIC_FOCUS_ACCOUNT_SLOTS_15
        override = self._slots.get("music_focus") or {}
        base = override.get("transport_base", default_base)
        playlists_slot = override.get("playlists", default_playlists)
        account_slots = tuple(override.get("accounts", default_accounts))

        out: dict[int, dict] = {
            base + 0: back,
            base + 1: vol_down,
            base + 2: play_stop,
            base + 3: vol_up,
            base + 4: next_track,
            playlists_slot: playlists,
        }
        if len(self._music_accounts) >= 2:
            for slot, account in zip(
                account_slots, self._music_accounts[:2], strict=False
            ):
                is_active = account == self._music_active_account
                out[slot] = {
                    "text": account,
                    "color": "seagreen" if is_active else "",
                    "text_color": "white" if is_active else "",
                    "component": self._component_name,
                    "method": "do_command",
                    "args": [{"command": "music_set_account", "name": account}],
                }
        return out

    # TECH DEBT — see top of file.
    def music_playlists_key_configs(self, deck_key_count: int) -> dict[int, dict]:
        if _is_mini_deck(deck_key_count):
            default_content = MUSIC_PLAYLISTS_CONTENT_SLOTS_6
            default_back = MUSIC_PLAYLISTS_BACK_SLOT_6
            default_next = MUSIC_PLAYLISTS_NEXT_SLOT_6
        elif _is_xl_deck(deck_key_count):
            default_content = MUSIC_PLAYLISTS_CONTENT_SLOTS_XL
            default_back = MUSIC_PLAYLISTS_BACK_SLOT_XL
            default_next = MUSIC_PLAYLISTS_NEXT_SLOT_XL
        else:
            default_content = MUSIC_PLAYLISTS_CONTENT_SLOTS_15
            default_back = MUSIC_PLAYLISTS_BACK_SLOT_15
            default_next = MUSIC_PLAYLISTS_NEXT_SLOT_15
        override = self._slots.get("music_playlists") or {}
        content_slots = tuple(override.get("content", default_content))
        back_slot = override.get("back", default_back)
        next_slot = override.get("next", default_next)
        per_page = len(content_slots)
        page_count = self.music_playlists_page_count(deck_key_count)
        page = max(0, min(self._music_playlists_page, page_count - 1))
        start = page * per_page
        page_items = self._music_playlists[start : start + per_page]
        out: dict[int, dict] = {}
        for i, slot in enumerate(content_slots):
            if i >= len(page_items):
                break
            p = page_items[i]
            out[slot] = {
                "text": str(p.get("name") or "?"),
                "color": "",
                "text_color": "",
                "component": self._component_name,
                "method": "do_command",
                "args": [{
                    "command": "music_play_playlist",
                    "context_uri": p.get("uri"),
                }],
            }
        out[back_slot] = {
            "text": "Back",
            "color": "",
            "text_color": "",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "music_playlists_exit"}],
        }
        next_label = f"Next {page + 1}/{page_count}" if page_count > 1 else "Next"
        out[next_slot] = {
            "text": next_label,
            "color": "",
            "text_color": "",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "music_playlists_next_page"}],
        }
        return out
