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
    tmp_path: Path,
    with_events: bool = False,
    with_streamdeck: bool = False,
    deck_key_count: int = 15,
    revert_delay_sec: float = 0.01,
) -> tuple[Tracker, RecordingSensor, RecordingSensor | None, RecordingSensor | None]:
    t = Tracker(name="inventory")
    t._state_sensor = RecordingSensor()
    t._state_sensor_name = "state"
    t._events_sensor = RecordingSensor() if with_events else None
    t._events_sensor_name = "events" if with_events else ""
    t._streamdeck = RecordingSensor() if with_streamdeck else None
    t._streamdeck_name = "streamdeck" if with_streamdeck else ""
    t._deck_key_count = deck_key_count
    t._revert_delay_sec = revert_delay_sec
    t._state_path = str(tmp_path / "inventory.json")
    t._state = {"schema_version": 1, "items": []}
    t._state_lock = asyncio.Lock()
    return t, t._state_sensor, t._events_sensor, t._streamdeck


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
    assert item["deck_page"] is None
    assert item["deck_slot"] is None
    assert item["barcode"] is None


async def test_add_item_validates_required_fields(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError):
        await t._add_item({"package_qty": 12, "icon": "🥚"})
    with pytest.raises(ValueError):
        await t._add_item({"name": "Eggs", "icon": "🥚"})
    with pytest.raises(ValueError):
        await t._add_item({"name": "Eggs", "package_qty": 12})
    with pytest.raises(ValueError):
        await t._add_item({"name": "Eggs", "package_qty": 0, "icon": "🥚"})


async def test_add_item_deck_pair_must_be_paired(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError):
        await _add_egg(t, deck_page=0)
    with pytest.raises(ValueError):
        await _add_egg(t, deck_slot=3)
    item = await _add_egg(t, deck_page=0, deck_slot=3)
    assert item["deck_page"] == 0 and item["deck_slot"] == 3


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
    for null_key in ("barcode", "deck_page", "deck_slot"):
        assert null_key not in item, f"expected {null_key} stripped when null"


async def test_state_snapshot_keeps_zero_valued_fields(tmp_path):
    t, state_sensor, _, _ = _make(tmp_path)
    await _add_egg(t, deck_page=0, deck_slot=0)
    latest = [c for c in state_sensor.commands if c.get("command") == "push_event"][-1]["event"]
    item = latest["items"][0]
    assert item["deck_page"] == 0
    assert item["deck_slot"] == 0


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
    monkeypatch.setattr("inventory_module.tracker._fetch_openfoodfacts", fake)
    resp = await t._lookup_barcode({"barcode": "1234"})
    assert resp == {
        "ok": True,
        "found": True,
        "barcode": "1234",
        "prefill": {"name": "Milk", "brand": "Acme"},
    }


async def test_lookup_barcode_missing_returns_empty_prefill(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr("inventory_module.tracker._fetch_openfoodfacts", _stub_fetch({}))
    resp = await t._lookup_barcode({"barcode": "9999"})
    assert resp["found"] is False
    assert resp["prefill"] == {}


async def test_lookup_barcode_caches_hits(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    fake = _stub_fetch({"1234": {"name": "Milk"}})
    monkeypatch.setattr("inventory_module.tracker._fetch_openfoodfacts", fake)
    await t._lookup_barcode({"barcode": "1234"})
    await t._lookup_barcode({"barcode": "1234"})
    assert fake.calls == ["1234"], "cache should have short-circuited the second call"


async def test_lookup_barcode_does_not_cache_misses(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    fake = _stub_fetch({"1234": None})
    monkeypatch.setattr("inventory_module.tracker._fetch_openfoodfacts", fake)
    await t._lookup_barcode({"barcode": "1234"})
    await t._lookup_barcode({"barcode": "1234"})
    assert fake.calls == ["1234", "1234"], "misses should re-fetch on retry"


async def test_lookup_barcode_requires_barcode(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError):
        await t._lookup_barcode({})
    with pytest.raises(ValueError):
        await t._lookup_barcode({"barcode": ""})


async def test_scan_barcode_known_item_increments_by_package_qty(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr("inventory_module.tracker._fetch_openfoodfacts", _stub_fetch({}))
    item = await _add_egg(t, name="Eggs", package_qty=12, barcode="1234")
    resp = await t._scan_barcode({"barcode": "1234"})
    assert resp["matched"] is True
    assert resp["added"] == 12
    assert resp["item"]["quantity"] == 12
    assert t._find_item(item["id"])["quantity"] == 12


async def test_scan_barcode_unknown_returns_prefill(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr(
        "inventory_module.tracker._fetch_openfoodfacts",
        _stub_fetch({"9999": {"name": "Something", "brand": "Foo"}}),
    )
    resp = await t._scan_barcode({"barcode": "9999"})
    assert resp["matched"] is False
    assert resp["barcode"] == "9999"
    assert resp["prefill"] == {"name": "Something", "brand": "Foo"}


async def test_scan_barcode_unknown_with_no_off_data(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr("inventory_module.tracker._fetch_openfoodfacts", _stub_fetch({}))
    resp = await t._scan_barcode({"barcode": "9999"})
    assert resp["matched"] is False
    assert resp["prefill"] == {}


async def test_barcode_cache_persists_across_load(tmp_path, monkeypatch):
    t, _, _, _ = _make(tmp_path)
    monkeypatch.setattr(
        "inventory_module.tracker._fetch_openfoodfacts",
        _stub_fetch({"1234": {"name": "Milk"}}),
    )
    await t._lookup_barcode({"barcode": "1234"})

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


async def test_add_item_rejects_deck_page_greater_than_zero(tmp_path):
    t, _, _, _ = _make(tmp_path)
    with pytest.raises(ValueError, match="multi-page"):
        await _add_egg(t, deck_page=1, deck_slot=0)


async def test_add_item_rejects_slot_beyond_deck(tmp_path):
    t, _, _, _ = _make(tmp_path, deck_key_count=15)
    with pytest.raises(ValueError, match="deck_slot"):
        await _add_egg(t, deck_page=0, deck_slot=15)


async def test_slot_collision_rejected_on_add(tmp_path):
    t, _, _, _ = _make(tmp_path)
    await _add_egg(t, name="Eggs", deck_page=0, deck_slot=3)
    with pytest.raises(ValueError, match="already assigned"):
        await _add_egg(t, name="Coffee", icon="☕", deck_page=0, deck_slot=3)


async def test_slot_collision_rejected_on_edit(tmp_path):
    t, _, _, _ = _make(tmp_path)
    a = await _add_egg(t, name="Eggs", deck_page=0, deck_slot=3)
    b = await _add_egg(t, name="Coffee", icon="☕", deck_page=0, deck_slot=4)
    with pytest.raises(ValueError, match="already assigned"):
        await t._edit_item({"id": b["id"], "deck_page": 0, "deck_slot": 3})
    assert a["deck_slot"] == 3


async def test_edit_item_can_re_set_own_slot(tmp_path):
    t, _, _, _ = _make(tmp_path)
    item = await _add_egg(t, deck_page=0, deck_slot=3)
    resp = await t._edit_item({"id": item["id"], "deck_page": 0, "deck_slot": 3})
    assert resp["item"]["deck_slot"] == 3


async def test_streamdeck_receives_layout_push_on_add(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True)
    await _add_egg(t, deck_page=0, deck_slot=3)
    updates = [c for c in deck.commands if "update_display" in c]
    assert updates, "expected an update_display call"
    latest_keys = updates[-1]["update_display"]["keys"]
    assert latest_keys["3"]["text"] == "🥚"
    assert latest_keys["3"]["method"] == "do_command"
    assert latest_keys["3"]["component"] == "inventory"
    assert latest_keys["3"]["args"][0] == {"command": "press", "id": mock_item_id(t)}
    assert latest_keys["0"]["text"] == ""


async def test_streamdeck_layout_uses_configured_key_count(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True, deck_key_count=6)
    await _add_egg(t, deck_page=0, deck_slot=2)
    updates = [c for c in deck.commands if "update_display" in c]
    assert updates
    keys = updates[-1]["update_display"]["keys"]
    assert set(keys.keys()) == {"0", "1", "2", "3", "4", "5"}


async def test_press_decrements_and_flashes_and_reverts(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=True, revert_delay_sec=0.05)
    item = await _add_egg(t, deck_page=0, deck_slot=3, name="Eggs", package_qty=12)
    await t._set_quantity({"id": item["id"], "quantity": 24})
    deck.commands.clear()

    resp = await t._press({"id": item["id"]})
    assert resp["item"]["quantity"] == 23

    flash = next(
        (
            c for c in deck.commands
            if c.get("update_display", {}).get("keys", {}).get("3", {}).get("text") == "23"
        ),
        None,
    )
    assert flash is not None, "expected flash update with new count"

    await asyncio.sleep(0.15)

    revert = next(
        (
            c
            for c in deck.commands
            if c.get("update_display", {}).get("keys", {}).get("3", {}).get("text") == "🥚"
        ),
        None,
    )
    assert revert is not None, "expected revert to icon"


async def test_press_floors_at_zero(tmp_path):
    t, _, _, _ = _make(tmp_path, with_streamdeck=True, revert_delay_sec=0.01)
    item = await _add_egg(t, deck_page=0, deck_slot=1)
    resp = await t._press({"id": item["id"]})
    assert resp["item"]["quantity"] == 0


async def test_no_streamdeck_no_deck_calls(tmp_path):
    t, _, _, deck = _make(tmp_path, with_streamdeck=False)
    assert deck is None
    item = await _add_egg(t, deck_page=0, deck_slot=3, name="Eggs", package_qty=12)
    await t._set_quantity({"id": item["id"], "quantity": 5})
    await t._press({"id": item["id"]})


def mock_item_id(tracker: Tracker) -> str:
    return tracker._state["items"][0]["id"]
