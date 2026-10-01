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
DEFAULT_MANUAL_WATER_ML = 50


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
    async def refresh_music_state(self) -> None:
        if self._music is None:
            self._music_playing = None
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
        # TECH DEBT — see top of file.
        if self._music is not None:
            reserved[deck_key_count - RESERVED_MUSIC_OFFSET] = "music"
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
            "args": [{"command": "feed_now"}],
        }

    def _thermostat_key_config(self) -> dict:
        # Label shows the action, not the current state.
        on = bool(self._thermostat_on)
        target_on = not on
        return {
            "text": "Thermostat ON" if target_on else "Thermostat OFF",
            "color": "green" if target_on else "",
            "text_color": "white" if target_on else "",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "thermostat_toggle"}],
        }

    # TECH DEBT — see top of file.
    def _music_key_config(self) -> dict:
        # Label shows the action, not the current state (matches thermostat).
        playing = bool(self._music_playing)
        target_play = not playing
        return {
            "text": "Music play" if target_play else "Music stop",
            "color": "green" if target_play else "red",
            "text_color": "white",
            "component": self._component_name,
            "method": "do_command",
            "args": [{"command": "music_toggle"}],
        }
