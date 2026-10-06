import asyncio
import json
from pathlib import Path

import pytest

from inventory_module.tracker import Tracker


class RecordingSensor:
    def __init__(self):
        self.commands: list[dict] = []

    async def do_command(self, cmd, **kwargs):
        self.commands.append(cmd)
        return {"ok": True}


class RecordingSwitch:
    def __init__(self, position: int = 0):
        self.position = position
        self.set_calls: list[int] = []

    async def get_position(self, **kwargs):
        return self.position

    async def set_position(self, position: int, **kwargs):
        self.set_calls.append(position)
        self.position = position


def _make(
    tmp_path: Path,
    with_events: bool = False,
    with_streamdeck: bool = False,
    deck_key_count: int = 15,
) -> tuple[Tracker, RecordingSensor, RecordingSensor | None, RecordingSensor | None]:
    t = Tracker(name="inventory")
    t._state_sensor = RecordingSensor()
    t._state_sensor_name = "state"
    t._events_sensor = RecordingSensor() if with_events else None
    t._events_sensor_name = "events" if with_events else ""
    t._deck._streamdeck = RecordingSensor() if with_streamdeck else None
    t._deck._streamdeck_name = "streamdeck" if with_streamdeck else ""
    t._deck._deck_key_count = deck_key_count
    t._devices = [{"name": "kitchen", "streamdeck": "streamdeck", "key_count": deck_key_count}]
    t._state_path = str(tmp_path / "inventory.json")
    t._state = {"schema_version": 1, "items": []}
    return t, t._state_sensor, t._events_sensor, t._deck._streamdeck


def _btn(slot: int, device: str = "kitchen") -> dict:
    return {"device": device, "slot": slot}


async def _add_egg(t: Tracker, **overrides) -> dict:
    payload = {"name": "Eggs", "package_qty": 12, "icon": "🥚", **overrides}
    resp = await t._add_item(payload)
    return resp["item"]


async def test_add_item_generates_id_and_starts_at_zero(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    assert item["id"]
    assert item["quantity"] == 0
    assert item["name"] == "Eggs"
    assert item["package_qty"] == 12
    assert item["icon"] == "🥚"
    assert item["button"] is None
    assert item["barcode"] is None


async def test_add_item_validates_required_fields(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError):
        await t._add_item({"package_qty": 12, "icon": "🥚"})
    with pytest.raises(ValueError):
        await t._add_item({"name": "Eggs", "package_qty": 0, "icon": "🥚"})
    # Icon is optional now — omitting it (or sending "") is fine.
    resp = await t._add_item({"name": "Salt", "package_qty": 1})
    assert resp["item"]["icon"] == ""


async def test_add_item_button_shape(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError, match="button.device"):
        await _add_egg(t, button={"slot": 3})
    with pytest.raises(ValueError, match="button.slot"):
        await _add_egg(t, button={"device": "kitchen"})
    with pytest.raises(ValueError, match="not declared"):
        await _add_egg(t, button={"device": "bathroom", "slot": 0})
    item = await _add_egg(t, button=_btn(3))
    assert item["button"] == {"device": "kitchen", "slot": 3}


async def test_edit_item_partial_patch(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._edit_item({"id": item["id"], "name": "Free-range eggs"})
    assert resp["item"]["name"] == "Free-range eggs"
    assert resp["item"]["package_qty"] == 12


async def test_edit_item_rejects_quantity(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    with pytest.raises(ValueError, match="quantity"):
        await t._edit_item({"id": item["id"], "quantity": 5})


async def test_edit_item_unknown_id(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError):
        await t._edit_item({"id": "nope", "name": "x"})


async def test_delete_item_removes(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    await t._delete_item({"id": item["id"]})
    assert t._find_item(item["id"]) is None


async def test_increment_default_by_one(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._adjust_quantity({"id": item["id"]}, direction=1, event_type="item_incremented")
    assert resp["item"]["quantity"] == 1


async def test_increment_by_n(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._adjust_quantity(
        {"id": item["id"], "by": 12}, direction=1, event_type="item_incremented"
    )
    assert resp["item"]["quantity"] == 12


async def test_decrement_floors_at_zero(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._adjust_quantity(
        {"id": item["id"], "by": 5}, direction=-1, event_type="item_decremented"
    )
    assert resp["item"]["quantity"] == 0


async def test_set_quantity_direct(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._set_quantity({"id": item["id"], "quantity": 24})
    assert resp["item"]["quantity"] == 24


async def test_state_persists_across_load(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t, name="Coffee", icon="☕", package_qty=1)
    await t._set_quantity({"id": item["id"], "quantity": 3})

    t2 = Tracker(name="inventory")
    t2._state_path = t._state_path
    loaded = t2._load_state()
    assert len(loaded["items"]) == 1
    assert loaded["items"][0]["name"] == "Coffee"
    assert loaded["items"][0]["quantity"] == 3


async def test_atomic_write_uses_tempfile(tmp_path):
    t, _, _, _ = _make(tmp_path)
    await _add_egg(t)
    path = Path(t._state_path)
    assert path.exists()
    tmp = path.with_suffix(path.suffix + ".tmp")
    assert not tmp.exists()
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert len(payload["items"]) == 1


async def test_load_state_quarantines_unreadable_file(tmp_path):
    # Corrupt JSON: file exists but isn't parseable. We must NOT silently
    # overwrite it — the user's inventory could be sitting in the bad file.
    t, _, _, _ = _make(tmp_path)
    path = Path(t._state_path)
    path.write_text("{not valid json")
    loaded = t._load_state()
    assert loaded == {"schema_version": 1, "items": []}
    assert not path.exists(), "corrupt file should have been moved aside"
    corrupt_backups = list(path.parent.glob(f"{path.name}.corrupt-*"))
    assert len(corrupt_backups) == 1
    assert corrupt_backups[0].read_text() == "{not valid json"


async def test_load_state_quarantines_bad_shape_file(tmp_path):
    # File is valid JSON but the wrong shape (items not a list). Same
    # protection applies.
    t, _, _, _ = _make(tmp_path)
    path = Path(t._state_path)
    path.write_text('{"items": "not a list"}')
    loaded = t._load_state()
    assert loaded == {"schema_version": 1, "items": []}
    assert not path.exists()
    assert len(list(path.parent.glob(f"{path.name}.corrupt-*"))) == 1


async def test_migrate_items_rewrites_legacy_deck_slot(tmp_path):
    t, _, _, _ = _make(tmp_path)
    t._state["items"] = [
        {"id": "a", "name": "A", "deck_page": 0, "deck_slot": 3},
        {"id": "b", "name": "B", "deck_page": None, "deck_slot": None},
        {"id": "c", "name": "C"},
    ]
    t._migrate_items()
    a, b, c = t._state["items"]
    assert a["button"] == {"device": "kitchen", "slot": 3}
    assert "deck_page" not in a and "deck_slot" not in a
    assert b["button"] is None
    assert c["button"] is None


async def test_migrate_items_is_idempotent(tmp_path):
    t, _, _, _ = _make(tmp_path)
    t._state["items"] = [{"id": "a", "name": "A", "button": {"device": "kitchen", "slot": 5}}]
    t._migrate_items()
    assert t._state["items"][0]["button"] == {"device": "kitchen", "slot": 5}


async def test_state_snapshot_pushed_on_mutation(tmp_path):
    t, state_sensor, _, _ = _make(tmp_path)
    await _add_egg(t)
    push_events = [c for c in state_sensor.commands if c.get("command") == "push_event"]
    assert push_events, "expected a push_event to the state sensor"
    latest = push_events[-1]["event"]
    assert latest["kind"] == "inventory_snapshot"
    assert len(latest["items"]) == 1


async def test_state_snapshot_strips_null_fields(tmp_path):
    t, state_sensor, _, _ = _make(tmp_path)
    await _add_egg(t)
    latest = [c for c in state_sensor.commands if c.get("command") == "push_event"][-1]["event"]
    item = latest["items"][0]
    for null_key in ("barcode", "button"):
        assert null_key not in item, f"expected {null_key} stripped when null"


async def test_state_snapshot_keeps_zero_valued_fields(tmp_path):
    t, state_sensor, _, _ = _make(tmp_path)
    await _add_egg(t, button=_btn(0))
    latest = [c for c in state_sensor.commands if c.get("command") == "push_event"][-1]["event"]
    item = latest["items"][0]
    assert item["button"] == {"device": "kitchen", "slot": 0}


async def test_change_event_pushed_when_events_sensor_configured(tmp_path):
    t, _, events_sensor, _ = _make(tmp_path, with_events=True)
    item = await _add_egg(t)
    await t._adjust_quantity(
        {"id": item["id"], "by": 12}, direction=1, event_type="item_incremented"
    )
    types = [c["event"]["event_type"] for c in events_sensor.commands]
    assert "item_added" in types
    assert "item_incremented" in types


async def test_no_events_pushed_when_events_sensor_missing(tmp_path):
    t, _, _, _ = _make(tmp_path, with_events=False)
    await _add_egg(t)


# -- barcode lookup + scan ---------------------------------------------


def _stub_fetch(mapping: dict[str, dict | None]):
    """Return a fake _fetch_openfoodfacts that reads from a dict + counts calls."""
    calls: list[str] = []

    def fake(barcode: str):
        calls.append(barcode)
        return mapping.get(barcode)

    fake.calls = calls
    return fake


async def test_lookup_barcode_found_returns_prefill(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    fake = _stub_fetch({"1234": {"name": "Milk", "brand": "Acme"}})
    monkeypatch.setattr("inventory_module.barcode._fetch_openfoodfacts", fake)
    resp = await t._barcode.lookup({"barcode": "1234"})
    assert resp == {
        "ok": True,
        "found": True,
        "barcode": "1234",
        "prefill": {"name": "Milk", "brand": "Acme"},
    }


async def test_lookup_barcode_missing_returns_empty_prefill(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr("inventory_module.barcode._fetch_openfoodfacts", _stub_fetch({}))
    resp = await t._barcode.lookup({"barcode": "9999"})
    assert resp["found"] is False
    assert resp["prefill"] == {}


async def test_lookup_barcode_caches_hits(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    fake = _stub_fetch({"1234": {"name": "Milk"}})
    monkeypatch.setattr("inventory_module.barcode._fetch_openfoodfacts", fake)
    await t._barcode.lookup({"barcode": "1234"})
    await t._barcode.lookup({"barcode": "1234"})
    assert fake.calls == ["1234"], "cache should have short-circuited the second call"


async def test_lookup_barcode_does_not_cache_misses(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    fake = _stub_fetch({"1234": None})
    monkeypatch.setattr("inventory_module.barcode._fetch_openfoodfacts", fake)
    await t._barcode.lookup({"barcode": "1234"})
    await t._barcode.lookup({"barcode": "1234"})
    assert fake.calls == ["1234", "1234"], "misses should re-fetch on retry"


async def test_lookup_barcode_requires_barcode(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError):
        await t._barcode.lookup({})
    with pytest.raises(ValueError):
        await t._barcode.lookup({"barcode": ""})


async def test_scan_barcode_known_item_increments_by_package_qty(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr("inventory_module.barcode._fetch_openfoodfacts", _stub_fetch({}))
    item = await _add_egg(t, name="Eggs", package_qty=12, barcode="1234")
    resp = await t._barcode.scan({"barcode": "1234"})
    assert resp["matched"] is True
    assert resp["added"] == 12
    assert resp["item"]["quantity"] == 12
    assert t._find_item(item["id"])["quantity"] == 12


async def test_scan_barcode_unknown_returns_prefill(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr(
        "inventory_module.barcode._fetch_openfoodfacts",
        _stub_fetch({"9999": {"name": "Something", "brand": "Foo"}}),
    )
    resp = await t._barcode.scan({"barcode": "9999"})
    assert resp["matched"] is False
    assert resp["barcode"] == "9999"
    assert resp["prefill"] == {"name": "Something", "brand": "Foo"}


async def test_scan_barcode_unknown_with_no_off_data(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr("inventory_module.barcode._fetch_openfoodfacts", _stub_fetch({}))
    resp = await t._barcode.scan({"barcode": "9999"})
    assert resp["matched"] is False
    assert resp["prefill"] == {}


async def test_barcode_cache_persists_across_load(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr(
        "inventory_module.barcode._fetch_openfoodfacts",
        _stub_fetch({"1234": {"name": "Milk"}}),
    )
    await t._barcode.lookup({"barcode": "1234"})

    t2 = Tracker(name="inventory")
    t2._state_path = t._state_path
    loaded = t2._load_state()
    assert loaded.get("barcode_cache", {}).get("1234") == {"name": "Milk"}


async def test_status_returns_probe_kind(tmp_path):
    t, _, _, _ = _make(tmp_path)
    resp = await t._status()
    assert resp["kind"] == "inventory_tracker"
    assert resp["state_sensor"] == "state"
    assert resp["item_count"] == 0


# -- streamdeck fanout --------------------------------------------------


async def test_add_item_rejects_slot_beyond_deck(tmp_path):
    t, _, _, _ = _make(tmp_path, deck_key_count=15)
    with pytest.raises(ValueError, match="button.slot"):
        await _add_egg(t, button=_btn(15))


async def test_slot_collision_rejected_on_add(tmp_path):
    t, _, _, _ = _make(tmp_path)
    await _add_egg(t, name="Eggs", button=_btn(3))
    with pytest.raises(ValueError, match="already assigned"):
        await _add_egg(t, name="Coffee", icon="☕", button=_btn(3))


async def test_slot_collision_rejected_on_edit(tmp_path):
    t, _, _, _ = _make(tmp_path)
    a = await _add_egg(t, name="Eggs", button=_btn(3))
    b = await _add_egg(t, name="Coffee", icon="☕", button=_btn(4))
    with pytest.raises(ValueError, match="already assigned"):
        await t._edit_item({"id": b["id"], "button": _btn(3)})
    assert a["button"]["slot"] == 3


async def test_edit_item_can_re_set_own_slot(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t, button=_btn(3))
    resp = await t._edit_item({"id": item["id"], "button": _btn(3)})
    assert resp["item"]["button"]["slot"] == 3


async def test_streamdeck_receives_layout_push_on_add(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    await _add_egg(t, button=_btn(3))
    updates = [c for c in deck.commands if "update_display" in c]
    assert updates, "expected an update_display call"
    latest_keys = updates[-1]["update_display"]["keys"]
    # Text is "<name> <count>" — the streamdeck module wraps on spaces so
    # the count naturally falls onto its own line below the name.
    assert latest_keys["3"]["text"] == "Eggs 0"
    assert "image" not in latest_keys["3"]
    assert latest_keys["3"]["method"] == "do_command"
    assert latest_keys["3"]["component"] == "inventory"
    assert latest_keys["3"]["args"][0] == {"command": "press", "id": mock_item_id(t)}
    assert latest_keys["0"]["text"] == " "


async def test_streamdeck_layout_uses_configured_key_count(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True, deck_key_count=6)
    await _add_egg(t, button=_btn(2))
    updates = [c for c in deck.commands if "update_display" in c]
    assert updates
    keys = updates[-1]["update_display"]["keys"]
    assert set(keys.keys()) == {"0", "1", "2", "3", "4", "5"}


async def test_press_enters_focus_mode(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3), name="Eggs", package_qty=12)
    await t._set_quantity({"id": item["id"], "quantity": 24})
    deck.commands.clear()

    resp = await t._press({"id": item["id"]})
    assert resp["focus_item_id"] == item["id"]

    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    # Item shown at slot 7 with -/+ at 6/8; every other slot blank.
    assert keys["7"]["text"] == "Eggs 24"
    assert keys["7"]["color"] == ""  # blank bg on the item cell
    assert keys["6"]["text"] == "-"
    assert keys["6"]["color"] == "red"
    assert keys["6"]["args"][0] == {"command": "focus_step", "delta": -1}
    assert keys["8"]["text"] == "+"
    assert keys["8"]["color"] == "seagreen"
    assert keys["8"]["args"][0] == {"command": "focus_step", "delta": 1}
    for other in ("0", "1", "2", "3", "4", "5", "9", "10", "11", "12", "13", "14"):
        assert keys[other]["text"] == " ", f"slot {other} should be blank"
        # Blank slots explicitly reset color so residual green/red doesn't
        # linger from the previous main-view render.
        assert keys[other]["color"] == "", f"slot {other} should clear color"


async def test_press_twice_exits_focus(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3), name="Eggs", package_qty=12)
    await t._press({"id": item["id"]})
    deck.commands.clear()

    resp = await t._press({"id": item["id"]})
    assert resp["focus_item_id"] is None

    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    # Item back at slot 3, everything else blank (only one item on the deck).
    assert keys["3"]["text"] == "Eggs 0"


async def test_focus_step_increments_focused_item(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3), name="Eggs", package_qty=12)
    await t._press({"id": item["id"]})
    deck.commands.clear()

    resp = await t._focus_step({"delta": 1})
    assert resp["item"]["quantity"] == 1

    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["7"]["text"] == "Eggs 1"


async def test_focus_step_decrements_focused_item(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3), name="Eggs", package_qty=12)
    await t._set_quantity({"id": item["id"], "quantity": 5})
    await t._press({"id": item["id"]})
    resp = await t._focus_step({"delta": -1})
    assert resp["item"]["quantity"] == 4


async def test_focus_step_no_op_outside_focus(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3), name="Eggs", package_qty=12)
    await t._set_quantity({"id": item["id"], "quantity": 10})
    resp = await t._focus_step({"delta": 1})
    assert resp == {"ok": True, "note": "not in focus mode"}
    assert t._find_item(item["id"])["quantity"] == 10


async def test_focus_step_accepts_float_delta_from_streamdeck(tmp_path):
    # Deltas round-tripped through the streamdeck's gRPC path come back as
    # float64. -1.0 and 1.0 must be accepted or the −/+ keys silently no-op.
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3), name="Eggs", package_qty=12)
    await t._set_quantity({"id": item["id"], "quantity": 5})
    await t._press({"id": item["id"]})
    resp = await t._focus_step({"delta": -1.0})
    assert resp["item"]["quantity"] == 4
    resp = await t._focus_step({"delta": 1.0})
    assert resp["item"]["quantity"] == 5


async def test_focus_step_rejects_zero_or_bool_delta(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3))
    await t._press({"id": item["id"]})
    with pytest.raises(ValueError):
        await t._focus_step({"delta": 0})
    with pytest.raises(ValueError):
        await t._focus_step({"delta": True})


async def test_focus_auto_returns_after_timeout(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    t._deck._focus_timeout_sec = 0.05
    item = await _add_egg(t, button=_btn(3), name="Eggs", package_qty=12)
    await t._press({"id": item["id"]})
    # Wait for the auto-return to fire.
    await asyncio.sleep(0.15)
    assert t._deck.focused_item_id is None
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["text"] == "Eggs 0"


async def test_focus_step_resets_timeout(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    t._deck._focus_timeout_sec = 0.15
    item = await _add_egg(t, button=_btn(3), name="Eggs", package_qty=12)
    await t._press({"id": item["id"]})
    # Repeatedly step before timeout — focus should persist.
    for _ in range(3):
        await asyncio.sleep(0.05)
        await t._focus_step({"delta": 1})
    assert t._deck.focused_item_id == item["id"]


async def test_focus_drops_when_focused_item_deleted(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3))
    await t._press({"id": item["id"]})
    assert t._deck.focused_item_id == item["id"]
    await t._delete_item({"id": item["id"]})
    # Next deck push notices the item is gone and clears focus.
    await t._deck.push_layout()
    assert t._deck.focused_item_id is None


async def test_no_streamdeck_no_deck_calls(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=False)
    assert deck is None
    item = await _add_egg(t, button=_btn(3), name="Eggs", package_qty=12)
    await t._set_quantity({"id": item["id"], "quantity": 5})
    await t._press({"id": item["id"]})


def mock_item_id(tracker: Tracker) -> str:
    return tracker._state["items"][0]["id"]


# -- reserved slots: water / feed / thermostat --------------------------


def _with_reserved(
    tmp_path,
    *,
    waterer=True,
    feeder=True,
    thermostat=True,
    thermostat_position: int = 0,
    manual_water_ml: int = 50,
):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    d = t._dispatcher
    if waterer:
        d._waterer = RecordingSensor()
        d._waterer_name = "waterer"
    if feeder:
        d._feeder = RecordingSensor()
        d._feeder_name = "feeder"
    if thermostat:
        d._thermostat_switch = RecordingSwitch(position=thermostat_position)
        d._thermostat_switch_name = "thermostat"
    d._manual_water_ml = manual_water_ml
    return t, deck


async def test_reserved_slots_render_when_deps_configured(tmp_path):
    t, deck = _with_reserved(tmp_path, thermostat_position=1)
    # Fresh layout push — main mode, no items.
    await t._deck.push_layout()
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    # Slots 12/13/14 on a 15-key deck are water/feed/thermostat.
    assert keys["12"]["text"].startswith("Water")
    assert keys["12"]["args"][0] == {"command": "water_manual"}
    assert keys["13"]["text"] == "Feed"
    assert keys["13"]["args"][0] == {"command": "feed_show_confirm"}
    # Label shows the ACTION (press to turn it OFF), because thermostat is
    # currently on (thermostat_position=1 in the fixture).
    assert keys["14"]["text"] == "Thermostat OFF"
    assert keys["14"]["color"] == ""
    assert keys["14"]["args"][0] == {"command": "thermostat_toggle"}


async def test_reserved_slots_absent_when_no_deps(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    await t._deck.push_layout()
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    # No deps configured → those slots are just empty, available for items.
    for slot in ("12", "13", "14"):
        assert keys[slot]["text"] == " "


async def test_reserved_slot_map_partial(tmp_path):
    # Only feeder configured → only slot 13 reserved.
    t, deck = _with_reserved(tmp_path, waterer=False, thermostat=False)
    await t._deck.push_layout()
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["12"]["text"] == " "
    assert keys["13"]["text"] == "Feed"
    assert keys["14"]["text"] == " "


async def test_thermostat_off_shows_turn_on_action(tmp_path):
    # Current state = off → label shows the action (press to turn ON), green.
    t, deck = _with_reserved(tmp_path, waterer=False, feeder=False, thermostat_position=0)
    await t._deck.push_layout()
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["14"]["text"] == "Thermostat ON"
    assert keys["14"]["color"] == "seagreen"


async def test_water_manual_calls_waterer_with_configured_ml(tmp_path):
    t, _ = _with_reserved(tmp_path, feeder=False, thermostat=False, manual_water_ml=75)
    await t._dispatcher.water_manual({})
    assert t._dispatcher._waterer.commands == [{"command": "dispense_ml", "ml": 75}]


async def test_feed_now_calls_feeder(tmp_path):
    t, _ = _with_reserved(tmp_path, waterer=False, thermostat=False)
    await t._dispatcher.feed_now({})
    assert t._dispatcher._feeder.commands == [{"command": "feed_now"}]


async def test_thermostat_toggle_flips_position(tmp_path):
    t, deck = _with_reserved(tmp_path, waterer=False, feeder=False, thermostat_position=0)
    resp = await t._thermostat_toggle_and_repaint({})
    assert resp["position"] == 1
    assert t._dispatcher._thermostat_switch.set_calls == [1]
    # Layout re-push shows the new label immediately — now that it's on,
    # the action label flips to "press to turn OFF".
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["14"]["text"] == "Thermostat OFF"


async def test_reserved_slot_rejects_item_assignment(tmp_path):
    t, _ = _with_reserved(tmp_path)
    with pytest.raises(ValueError, match="reserved for water"):
        await _add_egg(t, button=_btn(12))
    with pytest.raises(ValueError, match="reserved for feed"):
        await _add_egg(t, button=_btn(13))
    with pytest.raises(ValueError, match="reserved for thermostat"):
        await _add_egg(t, button=_btn(14))


async def test_reserved_slot_reject_on_edit(tmp_path):
    t, _ = _with_reserved(tmp_path)
    item = await _add_egg(t, button=_btn(3))
    with pytest.raises(ValueError, match="reserved for water"):
        await t._edit_item({"id": item["id"], "button": _btn(12)})


async def test_reserved_slot_hides_preexisting_item(tmp_path):
    # An item was assigned to slot 12 BEFORE the waterer dep was added. The
    # reserved config now wins on render — item's DB record is intact but
    # the deck shows the water key.
    t, _ = _with_reserved(tmp_path, feeder=False, thermostat=False)
    # Bypass validation to plant an item on a would-be reserved slot.
    t._dispatcher._waterer = None
    await _add_egg(t, button=_btn(12), name="Ghost")
    t._dispatcher._waterer = RecordingSensor()
    keys = t._deck._main_layout()
    assert keys["12"]["text"].startswith("Water")


async def test_do_command_wires_new_verbs(tmp_path):
    t, _ = _with_reserved(tmp_path, thermostat_position=0)
    r = await t.do_command({"command": "water_manual"})
    assert r["ok"] is True
    r = await t.do_command({"command": "feed_now"})
    assert r["ok"] is True
    r = await t.do_command({"command": "thermostat_toggle"})
    assert r["position"] == 1


# -- reorder_deck ------------------------------------------------------


async def test_reorder_deck_swaps_slots_atomically(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    a = await _add_egg(t, name="Aspirin", button=_btn(0))
    b = await _add_egg(t, name="Bread", button=_btn(1))
    c = await _add_egg(t, name="Cola", button=_btn(2))
    resp = await t.do_command(
        {
            "command": "reorder_deck",
            "order": [c["id"], a["id"], b["id"]],
        }
    )
    assert resp == {"ok": True}
    assert t._find_item(c["id"])["button"]["slot"] == 0
    assert t._find_item(a["id"])["button"]["slot"] == 1
    assert t._find_item(b["id"])["button"]["slot"] == 2


async def test_reorder_deck_can_swap_two_items(tmp_path):
    # Sequential edit_item calls would fail here (collision when moving A into
    # B's slot). Reorder does it atomically.
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    a = await _add_egg(t, name="A", button=_btn(0))
    b = await _add_egg(t, name="B", button=_btn(1))
    await t.do_command({"command": "reorder_deck", "order": [b["id"], a["id"]]})
    assert t._find_item(a["id"])["button"]["slot"] == 1
    assert t._find_item(b["id"])["button"]["slot"] == 0


async def test_reorder_deck_removes_items_not_in_order(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    a = await _add_egg(t, name="A", button=_btn(0))
    b = await _add_egg(t, name="B", button=_btn(1))
    await t.do_command({"command": "reorder_deck", "order": [b["id"]]})
    assert t._find_item(a["id"])["button"] is None
    assert t._find_item(b["id"])["button"]["slot"] == 0


async def test_reorder_deck_can_add_previously_unslotted(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    a = await _add_egg(t, name="A", button=_btn(0))
    b = await _add_egg(t, name="B")  # not on deck
    await t.do_command({"command": "reorder_deck", "order": [a["id"], b["id"]]})
    assert t._find_item(b["id"])["button"] == {"device": "kitchen", "slot": 1}


async def test_reorder_deck_rejects_unknown_id(tmp_path):
    t, _, _, _ = _make(tmp_path)
    await _add_egg(t, button=_btn(0))
    with pytest.raises(ValueError):
        await t.do_command({"command": "reorder_deck", "order": ["nope"]})


async def test_reorder_deck_rejects_duplicates(tmp_path):
    t, _, _, _ = _make(tmp_path)
    a = await _add_egg(t, button=_btn(0))
    with pytest.raises(ValueError, match="duplicate"):
        await t.do_command({"command": "reorder_deck", "order": [a["id"], a["id"]]})


async def test_reorder_deck_rejects_overflow(tmp_path):
    t, _, _, _ = _make(tmp_path, deck_key_count=2)
    ids = [(await _add_egg(t, name=f"X{i}"))["id"] for i in range(3)]
    with pytest.raises(ValueError, match="only has 2 keys"):
        await t.do_command({"command": "reorder_deck", "order": ids})


async def test_reorder_deck_pushes_layout(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    a = await _add_egg(t, name="A", button=_btn(0))
    b = await _add_egg(t, name="B", button=_btn(1))
    deck.commands.clear()
    await t.do_command({"command": "reorder_deck", "order": [b["id"], a["id"]]})
    updates = [c for c in deck.commands if "update_display" in c]
    assert updates, "expected a layout push after reorder"
    keys = updates[-1]["update_display"]["keys"]
    assert keys["0"]["text"].startswith("B ")
    assert keys["1"]["text"].startswith("A ")


# -- threshold + deck color -------------------------------------------


async def test_add_item_threshold_defaults_to_null(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    assert item["threshold"] is None


async def test_add_item_accepts_threshold(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t, threshold=5)
    assert item["threshold"] == 5


async def test_add_item_rejects_negative_threshold(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError, match="threshold"):
        await _add_egg(t, threshold=-1)


async def test_edit_item_can_set_and_clear_threshold(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    r = await t._edit_item({"id": item["id"], "threshold": 3})
    assert r["item"]["threshold"] == 3
    r = await t._edit_item({"id": item["id"], "threshold": None})
    assert r["item"]["threshold"] is None
    # Empty string also clears — matches CLI/MCP null coercion behavior.
    r = await t._edit_item({"id": item["id"], "threshold": 7})
    r = await t._edit_item({"id": item["id"], "threshold": ""})
    assert r["item"]["threshold"] is None


async def test_deck_key_color_above_threshold_is_cleared(tmp_path):
    # Well-stocked items used to paint seagreen; now they clear back to the
    # deck's default appearance. The clear must be explicit ("") rather than
    # omitted, because the streamdeck module merges key updates.
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3), threshold=2)
    await t._set_quantity({"id": item["id"], "quantity": 5})
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["color"] == ""
    assert keys["3"]["text_color"] == ""


async def test_deck_key_color_at_threshold_is_red(tmp_path):
    # At-or-below the threshold is red — the threshold acts as "you should
    # already be reordering when you hit this level."
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3), threshold=2)
    await t._set_quantity({"id": item["id"], "quantity": 2})
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["color"] == "red"


async def test_deck_key_color_below_threshold_is_red(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3), threshold=2)
    await t._set_quantity({"id": item["id"], "quantity": 1})
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["color"] == "red"


async def test_deck_key_no_color_when_threshold_unset(tmp_path):
    # Items without a threshold render in the deck's default appearance. We
    # still emit color/text_color explicitly so a prior red/green from this
    # slot gets cleared on the next push.
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    await _add_egg(t, button=_btn(3))
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["color"] == ""
    assert keys["3"]["text_color"] == ""


# -- routine capability ------------------------------------------------


async def _add_routine_item(t, name="Moisturizer", interval_days=1, package_qty=None, **overrides):
    payload = {"name": name, "routine": {"interval_days": interval_days}, **overrides}
    if package_qty is not None:
        payload["package_qty"] = package_qty
    resp = await t._add_item(payload)
    return resp["item"]


async def test_add_item_requires_supply_or_routine(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError, match="at least one of"):
        await t._add_item({"name": "Nothing"})


async def test_add_item_routine_only_omits_supply_fields(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_routine_item(t)
    assert item["routine"] == {"interval_days": 1, "last_done_at": None}
    assert item["package_qty"] is None
    assert item["quantity"] is None


async def test_add_item_hybrid_has_both_blocks(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_routine_item(t, name="Vitamins", package_qty=30)
    assert item["package_qty"] == 30
    assert item["quantity"] == 0
    assert item["routine"]["interval_days"] == 1


async def test_adjust_quantity_rejects_routine_only(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_routine_item(t)
    with pytest.raises(ValueError, match="no supply tracking"):
        await t._adjust_quantity({"id": item["id"]}, direction=1, event_type="item_incremented")


async def test_set_routine_sets_the_block(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._set_routine({"id": item["id"], "routine": {"interval_days": 3}})
    assert resp["item"]["routine"] == {"interval_days": 3, "last_done_at": None}


async def test_clear_routine_removes_but_supply_stays(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_routine_item(t, name="Vitamins", package_qty=30)
    await t._clear_routine({"id": item["id"]})
    fresh = t._find_item(item["id"])
    assert fresh["routine"] is None
    assert fresh["package_qty"] == 30


async def test_clear_routine_rejects_when_it_would_leave_no_capabilities(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_routine_item(t)
    with pytest.raises(ValueError, match="no capabilities"):
        await t._clear_routine({"id": item["id"]})


async def test_mark_routine_done_sets_last_done_at(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_routine_item(t)
    resp = await t._mark_routine_done({"id": item["id"]})
    assert resp["item"]["routine"]["last_done_at"]


async def test_mark_routine_done_on_hybrid_also_decrements(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_routine_item(t, name="Vitamins", package_qty=30)
    await t._set_quantity({"id": item["id"], "quantity": 10})
    resp = await t._mark_routine_done({"id": item["id"]})
    assert resp["item"]["quantity"] == 9
    assert resp["item"]["routine"]["last_done_at"]


async def test_press_on_routine_item_marks_done(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    item = await _add_routine_item(t, button=_btn(3))
    resp = await t._press({"id": item["id"]})
    assert resp["item"]["routine"]["last_done_at"]


async def test_press_on_supply_only_still_enters_focus(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True)
    item = await _add_egg(t, button=_btn(3))
    resp = await t._press({"id": item["id"]})
    assert resp["focus_item_id"] == item["id"]


async def test_deck_routine_never_done_is_green(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    await _add_routine_item(t, button=_btn(3))
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["color"] == "seagreen"


async def test_deck_routine_recently_done_is_cleared(tmp_path):
    # Completed routines used to paint gray, which read as "broken." They
    # now clear back to the deck's default appearance, same as well-stocked
    # supply items.
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    item = await _add_routine_item(t, button=_btn(3))
    await t._mark_routine_done({"id": item["id"]})
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["color"] == ""


async def test_deck_routine_overdue_is_green(tmp_path):
    from datetime import UTC, datetime, timedelta

    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    long_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    item = await _add_routine_item(t, button=_btn(3), interval_days=3)
    item["routine"]["last_done_at"] = long_ago
    await t._deck.push_layout()
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["color"] == "seagreen"


async def test_deck_routine_only_text_omits_count(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    await _add_routine_item(t, name="Moisturizer", button=_btn(3))
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["text"] == "Moisturizer"


async def test_deck_hybrid_text_includes_count(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    item = await _add_routine_item(t, name="Vitamins", package_qty=30, button=_btn(3))
    await t._set_quantity({"id": item["id"], "quantity": 12})
    keys = [c for c in deck.commands if "update_display" in c][-1]["update_display"]["keys"]
    assert keys["3"]["text"] == "Vitamins 12"
