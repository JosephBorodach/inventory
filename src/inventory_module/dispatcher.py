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
        return optional

    def reconfigure(self, attrs: dict, dependencies: Mapping[ResourceName, ResourceBase]) -> None:
        self._waterer_name = str(attrs.get("waterer") or "")
        self._feeder_name = str(attrs.get("feeder") or "")
        self._thermostat_switch_name = str(attrs.get("thermostat_switch") or "")
        self._music_name = str(attrs.get("music") or "")  # TECH DEBT — see top of file.
        self._manual_water_ml = int(attrs.get("manual_water_ml") or DEFAULT_MANUAL_WATER_ML)

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
    def music_playlists_page_count(self, deck_key_count: int) -> int:
        per_page = _playlists_per_page(deck_key_count)
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
        # Reserved home-action keys live on the kitchen deck only.
        if device != "kitchen":
            return {}
        return self.reserved_slot_map(key_count)

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
        back_slot = deck_key_count - RESERVED_WATER_OFFSET
        confirm_slot = deck_key_count - RESERVED_FEED_OFFSET
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
            s = MUSIC_FOCUS_SLOTS_6
            return {
                s["back"]: back,
                s["playlists"]: playlists,
                s["next"]: next_track,
                s["vol_down"]: vol_down,
                s["play_stop"]: play_stop,
                s["vol_up"]: vol_up,
            }

        # 15-key default: center row for transport, top row for playlists/accounts.
        base = 5
        out: dict[int, dict] = {
            base + 0: back,
            base + 1: vol_down,
            base + 2: play_stop,
            base + 3: vol_up,
            base + 4: next_track,
            MUSIC_FOCUS_PLAYLISTS_SLOT_15: playlists,
        }
        if len(self._music_accounts) >= 2:
            for slot, account in zip(
                MUSIC_FOCUS_ACCOUNT_SLOTS_15, self._music_accounts[:2], strict=False
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
            content_slots = MUSIC_PLAYLISTS_CONTENT_SLOTS_6
            back_slot = MUSIC_PLAYLISTS_BACK_SLOT_6
            next_slot = MUSIC_PLAYLISTS_NEXT_SLOT_6
            per_page = MUSIC_PLAYLISTS_PER_PAGE_6
        else:
            content_slots = MUSIC_PLAYLISTS_CONTENT_SLOTS_15
            back_slot = MUSIC_PLAYLISTS_BACK_SLOT_15
            next_slot = MUSIC_PLAYLISTS_NEXT_SLOT_15
            per_page = MUSIC_PLAYLISTS_PER_PAGE_15
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
