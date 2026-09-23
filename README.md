# inventory

Count keyed items, decrement via web app or physical device. Manages a small
catalog of items (name, quantity, icon, package default, optional barcode,
optional Stream Deck slot), persists to a JSON file on the Pi, publishes the
current state to a `viam:event-queue:sensor` for fast reads, and pushes change
events to a second queue sensor for audit trails and Viam Triggers.

Designed for household consumables — pantry, medications, coffee beans,
batteries — but the mechanics don't care what you're counting.

## Tracker

**Model:** `joseph:inventory:tracker`
**API:** `rdk:component:generic`

### Configuration

```json
{
  "name": "inventory",
  "namespace": "rdk",
  "type": "generic",
  "model": "joseph:inventory:tracker",
  "attributes": {
    "state_sensor": "inventory-state",
    "events_sensor": "events"
  },
  "depends_on": ["inventory-state", "events"]
}
```

| Attribute        | Type   | Required | Description                                                                      |
|------------------|--------|----------|----------------------------------------------------------------------------------|
| `state_sensor`   | string | yes      | Name of a `viam:event-queue:sensor` (`queue_capacity: 1`) holding the snapshot.  |
| `events_sensor`  | string | no       | Optional queue sensor for change-event fanout. Enables audit trail + triggers.   |
| `streamdeck`     | string | no       | Optional `erh:viam-streamdeck:streamdeck-any` service name for deck fanout.      |
| `deck_key_count` | int    | no       | Number of physical keys on the deck. Defaults to `15` (standard Stream Deck).    |
| `state_path`     | string | no       | Override the JSON store path. Defaults to `~/.viam/inventory.json`.              |

The `state_sensor` should be configured with `queue_capacity: 1` and no data
capture — its purpose is to hold the latest snapshot in memory for fast reads.
The `events_sensor` can be configured with data capture on `Readings` to feed
tabular data and Viam Triggers.

### Data model

Items:
```
id            string          UUID, generated on add
name          string          "Eggs"
barcode       string | null   "0016000275270", null if unassigned
quantity      number          current count, floors at 0
package_qty   number          default add amount (e.g. eggs come 12 to a carton)
icon          string          single emoji, e.g. "🥚"
deck_page     integer | null  Stream Deck page index; null = not on deck
deck_slot     integer | null  Stream Deck key index; null = not on deck
created_at    iso timestamp
updated_at    iso timestamp
```

Validation:
- `name`, `icon` are non-empty strings.
- `package_qty` is a positive integer.
- `deck_page` and `deck_slot` are always both set or both null.
- `icon` is expected to be a single emoji grapheme.
- `barcode` is either a non-empty string or null.

### Reading the current state

Frontends call `get_readings` on the `state_sensor`. It returns the newest
pushed snapshot non-destructively:

```json
{
  "kind": "inventory_snapshot",
  "source": "inventory",
  "at": "2026-09-23T18:00:00Z",
  "items": [ {...}, {...} ]
}
```

The tracker pushes a fresh snapshot to `state_sensor` on every mutation. It
also pushes an initial snapshot on module boot so the sensor is populated even
before the first mutation.

### Mutations (DoCommand verbs)

- `add_item({name, package_qty, icon, deck_page?, deck_slot?, barcode?})` — returns the new item. Quantity starts at 0.
- `edit_item({id, name?, package_qty?, icon?, deck_page?, deck_slot?, barcode?})` — partial patch. **Rejects `quantity`**; use the quantity verbs below.
- `delete_item({id})`
- `increment({id, by?})` — default `by = 1`.
- `decrement({id, by?})` — default `by = 1`. Floors at 0.
- `set_quantity({id, quantity})` — for corrections. Quantity must be a non-negative integer.
- `status()` — probe verb. Returns `{kind: "inventory_tracker", state_sensor, events_sensor, item_count}`.

- `press({id})` — Stream Deck callback. Decrement by 1, flash the new count on
  the paired deck key for a few seconds, then revert to the item icon. Per-slot
  cancellable so mashing a key doesn't stack revert timers.

Deferred to a later release: `scan_barcode`, `lookup_barcode`.

## Stream Deck integration

When the tracker's `streamdeck` attribute names an `erh:viam-streamdeck:streamdeck-any`
service, the tracker keeps that deck's keys in sync with the items:

- On boot and after every mutation, it computes each key config and calls the
  deck's `update_display` DoCommand. Items with `deck_page: 0` and a valid
  `deck_slot` appear as keys; empty slots are cleared.
- Each key's callback fires this tracker's `press` DoCommand with the item id,
  so a physical button press decrements the corresponding count.
- After a press, the key briefly shows the new count as text, then reverts to
  the item's icon.

V1 constraints:
- **Single page only.** `deck_page` must be `0` (or `null` for items that don't
  appear on the deck). Multi-page support is deferred — the schema is
  future-proof but this release ignores non-zero `deck_page`.
- **No slot collisions.** Two items cannot share the same `(deck_page, deck_slot)`.
  Attempting to add or edit into an occupied slot raises a clear error.
- **Icons render as emoji.** The tracker sets `text_font: NotoEmoji-Regular.tff`
  (bundled with `erh:viam-streamdeck`) so any single emoji renders directly on
  the key.

### Change events

When `events_sensor` is set, every mutation pushes a change event to it:

```json
{
  "event_type": "item_added" | "item_edited" | "item_deleted" | "item_incremented" | "item_decremented" | "item_quantity_set",
  "source": "inventory",
  "at": "2026-09-23T18:00:00Z",
  "item_id": "<id>",
  "item_name": "Eggs",
  "delta": -1,
  "new_quantity": 23
}
```

Configure the events sensor with data capture on `Readings` (1 Hz is fine — see
the event-queue sensor's design notes) and you get a queryable audit trail plus
a `conditional_data_ingested` trigger surface for future low-stock alerts.

### Durability

Writes go to `~/.viam/inventory.json` via temp-file + `os.rename`, atomic on
POSIX. A power cut mid-write can't corrupt the catalog — you either see the
old contents or the new ones.

## Development

```
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
./.venv/bin/pytest
```
