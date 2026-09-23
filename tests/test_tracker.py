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


def _make(
    tmp_path: Path, with_events: bool = False
) -> tuple[Tracker, RecordingSensor, RecordingSensor | None]:
    t = Tracker(name="inventory")
    t._state_sensor = RecordingSensor()
    t._state_sensor_name = "state"
    t._events_sensor = RecordingSensor() if with_events else None
    t._events_sensor_name = "events" if with_events else ""
    t._state_path = str(tmp_path / "inventory.json")
    t._state = {"schema_version": 1, "items": []}
    t._state_lock = asyncio.Lock()
    return t, t._state_sensor, t._events_sensor


async def _add_egg(t: Tracker, **overrides) -> dict:
    payload = {"name": "Eggs", "package_qty": 12, "icon": "🥚", **overrides}
    resp = await t._add_item(payload)
    return resp["item"]


async def test_add_item_generates_id_and_starts_at_zero(tmp_path):
    t, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    assert item["id"]
    assert item["quantity"] == 0
    assert item["name"] == "Eggs"
    assert item["package_qty"] == 12
    assert item["icon"] == "🥚"
    assert item["deck_page"] is None
    assert item["deck_slot"] is None
    assert item["barcode"] is None


async def test_add_item_validates_required_fields(tmp_path):
    t, _, _ = _make(tmp_path)
    with pytest.raises(ValueError):
        await t._add_item({"package_qty": 12, "icon": "🥚"})
    with pytest.raises(ValueError):
        await t._add_item({"name": "Eggs", "icon": "🥚"})
    with pytest.raises(ValueError):
        await t._add_item({"name": "Eggs", "package_qty": 12})
    with pytest.raises(ValueError):
        await t._add_item({"name": "Eggs", "package_qty": 0, "icon": "🥚"})


async def test_add_item_deck_pair_must_be_paired(tmp_path):
    t, _, _ = _make(tmp_path)
    with pytest.raises(ValueError):
        await _add_egg(t, deck_page=0)
    with pytest.raises(ValueError):
        await _add_egg(t, deck_slot=3)
    item = await _add_egg(t, deck_page=0, deck_slot=3)
    assert item["deck_page"] == 0 and item["deck_slot"] == 3


async def test_edit_item_partial_patch(tmp_path):
    t, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._edit_item({"id": item["id"], "name": "Free-range eggs"})
    assert resp["item"]["name"] == "Free-range eggs"
    assert resp["item"]["package_qty"] == 12


async def test_edit_item_rejects_quantity(tmp_path):
    t, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    with pytest.raises(ValueError, match="quantity"):
        await t._edit_item({"id": item["id"], "quantity": 5})


async def test_edit_item_unknown_id(tmp_path):
    t, _, _ = _make(tmp_path)
    with pytest.raises(ValueError):
        await t._edit_item({"id": "nope", "name": "x"})


async def test_delete_item_removes(tmp_path):
    t, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    await t._delete_item({"id": item["id"]})
    assert t._find_item(item["id"]) is None


async def test_increment_default_by_one(tmp_path):
    t, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._adjust_quantity({"id": item["id"]}, direction=1, event_type="item_incremented")
    assert resp["item"]["quantity"] == 1


async def test_increment_by_n(tmp_path):
    t, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._adjust_quantity(
        {"id": item["id"], "by": 12}, direction=1, event_type="item_incremented"
    )
    assert resp["item"]["quantity"] == 12


async def test_decrement_floors_at_zero(tmp_path):
    t, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._adjust_quantity(
        {"id": item["id"], "by": 5}, direction=-1, event_type="item_decremented"
    )
    assert resp["item"]["quantity"] == 0


async def test_set_quantity_direct(tmp_path):
    t, _, _ = _make(tmp_path)
    item = await _add_egg(t)
    resp = await t._set_quantity({"id": item["id"], "quantity": 24})
    assert resp["item"]["quantity"] == 24


async def test_state_persists_across_load(tmp_path):
    t, _, _ = _make(tmp_path)
    item = await _add_egg(t, name="Coffee", icon="☕", package_qty=1)
    await t._set_quantity({"id": item["id"], "quantity": 3})

    t2 = Tracker(name="inventory")
    t2._state_path = t._state_path
    loaded = t2._load_state()
    assert len(loaded["items"]) == 1
    assert loaded["items"][0]["name"] == "Coffee"
    assert loaded["items"][0]["quantity"] == 3


async def test_atomic_write_uses_tempfile(tmp_path):
    t, _, _ = _make(tmp_path)
    await _add_egg(t)
    path = Path(t._state_path)
    assert path.exists()
    tmp = path.with_suffix(path.suffix + ".tmp")
    assert not tmp.exists()
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert len(payload["items"]) == 1


async def test_state_snapshot_pushed_on_mutation(tmp_path):
    t, state_sensor, _ = _make(tmp_path)
    await _add_egg(t)
    push_events = [c for c in state_sensor.commands if c.get("command") == "push_event"]
    assert push_events, "expected a push_event to the state sensor"
    latest = push_events[-1]["event"]
    assert latest["kind"] == "inventory_snapshot"
    assert len(latest["items"]) == 1


async def test_state_snapshot_strips_null_fields(tmp_path):
    t, state_sensor, _ = _make(tmp_path)
    await _add_egg(t)
    latest = [c for c in state_sensor.commands if c.get("command") == "push_event"][-1]["event"]
    item = latest["items"][0]
    for null_key in ("barcode", "deck_page", "deck_slot"):
        assert null_key not in item, f"expected {null_key} stripped when null"


async def test_state_snapshot_keeps_zero_valued_fields(tmp_path):
    t, state_sensor, _ = _make(tmp_path)
    await _add_egg(t, deck_page=0, deck_slot=0)
    latest = [c for c in state_sensor.commands if c.get("command") == "push_event"][-1]["event"]
    item = latest["items"][0]
    assert item["deck_page"] == 0
    assert item["deck_slot"] == 0


async def test_change_event_pushed_when_events_sensor_configured(tmp_path):
    t, _, events_sensor = _make(tmp_path, with_events=True)
    item = await _add_egg(t)
    await t._adjust_quantity(
        {"id": item["id"], "by": 12}, direction=1, event_type="item_incremented"
    )
    types = [c["event"]["event_type"] for c in events_sensor.commands]
    assert "item_added" in types
    assert "item_incremented" in types


async def test_no_events_pushed_when_events_sensor_missing(tmp_path):
    t, _, _ = _make(tmp_path, with_events=False)
    await _add_egg(t)


async def test_status_returns_probe_kind(tmp_path):
    t, _, _ = _make(tmp_path)
    resp = await t._status()
    assert resp["kind"] == "inventory_tracker"
    assert resp["state_sensor"] == "state"
    assert resp["item_count"] == 0
