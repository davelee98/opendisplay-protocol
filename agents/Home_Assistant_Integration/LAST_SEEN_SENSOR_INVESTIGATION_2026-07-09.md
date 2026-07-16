# `last_seen` sensor does not update — root-cause investigation

**Date:** 2026-07-09
**Scope:** `custom_components/opendisplay` (HA integration), Home Assistant core Bluetooth stack (`../core`), `habluetooth==6.26.5`, with corroborating detail from `py-opendisplay` and `Firmware`.
**Status:** Diagnosis only. No source changes were made.

---

## 1. Symptom

- The OpenDisplay device's BLE advertisement is clearly being received by Home
  Assistant: in **Settings → Devices & Services → Bluetooth →
  Advertisement monitor** (`config/bluetooth/advertisement-monitor`), the
  **"updated"** timestamp for the device refreshes and shows it was seen very
  recently.
- Despite this, the **`sensor.<device>_last_seen`** entity does **not** advance.
  It sticks at an old timestamp (or never leaves its initial value).

The two observations look contradictory: the same advertisements that keep the
monitor's "updated" field fresh are apparently *not* driving the `last_seen`
sensor. This document follows the advertisement from the moment core receives it
to the moment the sensor value is computed, and shows exactly where the two
paths diverge.

**TL;DR:** `last_seen` is derived from a `time.time()` snapshot taken *inside the
coordinator's advertisement callback*. Core only invokes that callback for
advertisements that pass **two gates** — a `connectable=True` subscription
filter and an **identical‑payload de‑duplication** short‑circuit. The
advertisement monitor's "updated" field is taken from `_all_history[address].time`,
which core refreshes on **every** received advertisement, *before* both gates.
So the monitor stays fresh while `last_seen` freezes. The sensor is reading the
wrong source.

---

## 2. How `last_seen` is produced today (integration side)

### 2.1 The sensor description

[`sensor.py:74`](../custom_components/opendisplay/sensor.py#L74) defines the entity:

```python
_LAST_SEEN_DESCRIPTION = OpenDisplaySensorEntityDescription(
    key="last_seen",
    ...
    value_fn=lambda upd: (
        datetime.fromtimestamp(upd.last_seen, tz=timezone.utc)
        if upd.last_seen is not None
        else None
    ),
)
```

and [`sensor.py:129`](../custom_components/opendisplay/sensor.py#L129) reads it:

```python
@property
def native_value(self):
    if self.coordinator.data is None:
        return None
    return self.entity_description.value_fn(self.coordinator.data)
```

So `native_value` is a pure function of `self.coordinator.data` — an
`OpenDisplayUpdate` — and specifically its `.last_seen` field.

### 2.2 Where `coordinator.data.last_seen` is set

`OpenDisplayUpdate` is a plain dataclass ([`coordinator.py:28`](../custom_components/opendisplay/coordinator.py#L28)).
The **only** place `self.data` (and therefore `last_seen`) is written is inside
the coordinator's advertisement callback,
[`_async_handle_bluetooth_event`](../custom_components/opendisplay/coordinator.py#L104):

```python
else:
    ...
    self.data = OpenDisplayUpdate(
        address=service_info.address,
        advertisement=advertisement,
        rssi=service_info.rssi,
        last_seen=time.time(),          # <-- the ONLY writer of last_seen
        button_events=button_events,
        touch_events=touch_events,
    )
    for device_seen_callback in list(self._device_seen_callbacks):
        device_seen_callback()
super()._async_handle_bluetooth_event(service_info, change)
```

**Consequence:** `last_seen` advances *only when this callback runs and reaches
the `else` branch*. If core never calls `_async_handle_bluetooth_event` for a
given advertisement, `last_seen` does not move — no matter how many
advertisements the radio actually received. The value is a **snapshot of the
last time the callback fired**, not "the last time the device was seen."

> The same is true of the **RSSI** sensor (`value_fn=lambda upd: upd.rssi`) and,
> in principle, temperature/battery — they all read `coordinator.data`, which is
> only rewritten inside this callback. Temperature/battery look correct because
> their *logical* value rarely changes anyway; `last_seen` and RSSI are the two
> that are *expected* to change continuously, so their staleness is what the user
> notices.

The critical question is therefore: **when does core invoke
`_async_handle_bluetooth_event`?**

---

## 3. How the coordinator is wired to core

`OpenDisplayCoordinator` extends `PassiveBluetoothDataUpdateCoordinator`
([`coordinator.py:40`](../custom_components/opendisplay/coordinator.py#L40)), and is
constructed with **`connectable=True`**
([`coordinator.py:45-51`](../custom_components/opendisplay/coordinator.py#L45)):

```python
super().__init__(
    hass, _LOGGER, address,
    BluetoothScanningMode.PASSIVE,
    connectable=True,          # <-- important
)
```

On `async_start()`, the base class registers a callback with core
([`update_coordinator.py:88`](../../core/homeassistant/components/bluetooth/update_coordinator.py#L88)):

```python
async_register_callback(
    self.hass,
    self._async_handle_bluetooth_event,
    BluetoothCallbackMatcher(address=self.address, connectable=self.connectable),  # connectable=True
    self.mode,           # PASSIVE
    ...
)
```

So the coordinator is subscribed with a matcher that requires
**`address == <device>` AND `connectable == True`**. Hold onto the
`connectable=True` — it is **Gate 1**.

When core *does* dispatch a matching advertisement, the base
`PassiveBluetoothDataUpdateCoordinator._async_handle_bluetooth_event`
([`passive_update_coordinator.py:82`](../../core/homeassistant/components/bluetooth/passive_update_coordinator.py#L82))
sets `_available = True` and calls `async_update_listeners()`, which re-reads
`native_value` and pushes new sensor state. The OpenDisplay override runs
*before* `super()`, setting `last_seen` first. That part works correctly — the
problem is purely *whether core calls the callback at all*.

---

## 4. What core does with each advertisement (`habluetooth` manager)

All advertisements from every scanner funnel into
`BluetoothManager._scanner_adv_received`
(`habluetooth/manager.py`, v6.26.5, the version pinned in
`homeassistant/components/bluetooth/manifest.json`). The relevant ordering is:

| Step | Line | Effect |
|---|---|---|
| Apple noise pre-filter | `1082` | Irrelevant here (OpenDisplay uses mfr id `0x2446`) |
| Cross-source RSSI arbitration (`_should_keep_previous_adv`) | `1209` | May drop an advert from a *worse* duplicate source (multi-scanner only) |
| **Write `_connectable_history[address] = service_info`** | `1237` | **Refreshed every connectable advert, BEFORE the gates below** |
| **Write `_all_history[address] = service_info`** | `1239` | **Refreshed every advert, BEFORE the gates below** |
| Advertisement-interval tracking | `1255` | Bookkeeping |
| **Identical-payload short-circuit → `return`** | `1274`–`1304` | **Gate 2** |
| Non-connectable → connectable "upgrade" | `1315`–`1323` | Rescues Gate 1 *iff* a connectable path exists |
| Dispatch to registered callbacks (`_subclass_discover_info`) | `1333` | Where the coordinator/monitor callbacks finally fire |

Two facts from this table are the whole story:

1. **`_all_history[address]` and `_connectable_history[address]` are updated on
   every received advertisement (lines 1237/1239), *before* the gates.** Their
   `.time` field is a fresh monotonic timestamp on every frame.
2. **The registered callbacks (coordinator *and* advertisement monitor) are only
   invoked at line 1333, *after* both gates.**

### 4.1 Gate 2 — identical-payload de-duplication (`manager.py:1274`)

```python
if (
    not (service_info.connectable and old_connectable_service_info is None)
    and old_service_info is not None
    and not (
        (service_info.manufacturer_data != old_service_info.manufacturer_data) or
        (service_info.service_data   != old_service_info.service_data)   or
        (service_info.service_uuids  != old_service_info.service_uuids)  or
        (service_info.name           != old_service_info.name)
    )
):
    return          # <-- no callbacks fire for this advertisement
```

If the advertisement's `manufacturer_data` / `service_data` / `service_uuids` /
`name` are **byte-for-byte identical** to the previous stored advertisement,
core `return`s here and **no callback is dispatched** — but note it already
updated `_all_history`/`_connectable_history` above. This is a deliberate
optimisation: core says "nothing changed, don't wake integrations up." For a
device that re-broadcasts an unchanging payload, the coordinator callback fires
**once** (on the first, changed frame) and then never again until the payload
changes.

### 4.2 Gate 1 — the `connectable` filter (`match.py:399`)

Callbacks that survive Gate 2 reach
`HomeAssistantBluetoothManager._discover_service_info`
([`manager.py:138`](../../core/homeassistant/components/bluetooth/manager.py#L138)),
which calls `match_callbacks` → `ble_device_matches`
([`match.py:399`](../../core/homeassistant/components/bluetooth/match.py#L399)):

```python
def ble_device_matches(matcher, service_info):
    if matcher.get(CONNECTABLE, True) and not service_info.connectable:
        return False        # <-- connectable=True matcher rejects a non-connectable advert
    ...
```

- The coordinator's matcher has **`connectable=True`** → this rejects any
  advertisement that core presents as **non-connectable**.
- An advertisement is presented as non-connectable when the scanner that
  received it cannot originate connections (e.g. a passive-only Bluetooth proxy)
  **and** there is no separately-registered connectable scanner path for the
  device (the "upgrade" at `manager.py:1315` did not apply).

So an advertisement received only via a non-connectable path is **silently
dropped for the coordinator**, even though it updated `_all_history`.

---

## 5. Why the advertisement monitor stays fresh while `last_seen` freezes

The monitor is `bluetooth/subscribe_advertisements`
([`websocket_api.py:204`](../../core/homeassistant/components/bluetooth/websocket_api.py#L204)).
Two differences from the coordinator explain the divergence:

1. **It subscribes with `connectable=False`**
   ([`websocket_api.py:209`](../../core/homeassistant/components/bluetooth/websocket_api.py#L209)):

   ```python
   _AdvertisementSubscription(..., BluetoothCallbackMatcher(connectable=False)).async_start()
   ```

   With `connectable=False`, the `matcher.get(CONNECTABLE, True)` test in
   `ble_device_matches` is falsy, so the connectable check is skipped and the
   monitor matches **both** connectable and non-connectable advertisements. It is
   **not subject to Gate 1.**

2. **On subscribe, core seeds it from history**
   ([`manager.py:248`](../../core/homeassistant/components/bluetooth/manager.py#L248)):

   ```python
   history = self._connectable_history if connectable else self._all_history
   ...
   callback(service_info, BluetoothChange.ADVERTISEMENT)   # immediate replay of the latest frame
   ```

   Since the monitor uses `connectable=False`, this replays the latest
   `_all_history[address]` — whose `.time` is fresh from the most recent frame
   (line 1239). Every time the page (re)subscribes it shows the true
   last-seen time.

The "updated" value the user sees is serialized straight from
`service_info.time` ([`websocket_api.py:107`](../../core/homeassistant/components/bluetooth/websocket_api.py#L107)),
i.e. the timestamp stored in `_all_history` **before** the gates. That is the
authoritative "last advertisement received" clock.

### The divergence, stated plainly

| | Advertisement monitor "updated" | `last_seen` sensor |
|---|---|---|
| Source of the timestamp | `_all_history[address].time` (core, updated at `manager.py:1239`) | `time.time()` snapshot inside the coordinator callback (`coordinator.py:141`) |
| Gate 1 (`connectable`) | **Bypassed** (`connectable=False`) | **Applies** (`connectable=True`) |
| Gate 2 (identical payload) | Bypassed for the seed-from-history replay; also updated pre-gate | **Applies** — callback suppressed on unchanged payloads |
| Net behaviour | Refreshes on every received frame | Only advances when a *changed*, *connectable* advert is dispatched |

Whichever gate is biting in a given deployment, the sensor is reading a value
that is downstream of gates the monitor's clock is upstream of. That is the bug.

---

## 6. Firmware corroboration — why the payload is often unchanged (feeds Gate 2)

The advertised manufacturer data is assembled in
`Firmware/src/display_service.cpp` → `updatemsdata()`
([`display_service.cpp:1283`](../../Firmware/src/display_service.cpp#L1283)). The
16‑byte payload is `0x2446` + 11 dynamic bytes + temperature + battery low byte +
**status byte**, and the status byte packs a 4‑bit loop counter
([`display_service.cpp:1301`](../../Firmware/src/display_service.cpp#L1301)):

```c
uint8_t statusByte = ((batteryVoltage10mv >> 8) & 0x01)
                   | ((rebootFlag & 0x01) << 1)
                   | ((connectionRequested & 0x01) << 2)
                   | ((mloopcounter & 0x0F) << 4);   // loop counter in the payload
```

The counter only advances the *advertised* bytes on alternate calls
([`display_service.cpp:1329`](../../Firmware/src/display_service.cpp#L1329)):

```c
static uint8_t prev_msd_payload[16] = {0xFF};
if (memcmp(prev_msd_payload, msd_payload, 16) == 0) {
    mloopcounter++; mloopcounter &= 0x0F;   // bump counter but DON'T re-advertise
    return;
}
memcpy(prev_msd_payload, msd_payload, 16);
// ... else push the new payload to the radio and restart advertising
```

So the on-air payload changes only when `updatemsdata()` is called, and even then
only every *other* call. Its cadence:

- **Mains / always-on ESP32** (`power_mode != 1`): while idle, `updatemsdata()`
  runs at most **once every 60 s**
  ([`main.cpp:229`](../../Firmware/src/main.cpp#L229)):

  ```c
  static uint32_t lastMsdUpdate = 0;
  if (millis() - lastMsdUpdate >= 60000) { lastMsdUpdate = millis(); updatemsdata(); }
  ```

  Combined with the "every other call" counter logic, the **advertised payload
  changes only about once every ~120 s**. Between changes the radio re-broadcasts
  identical frames, all suppressed by Gate 2. Best case, `last_seen` can only
  ever advance about once every two minutes.

- **Battery / deep-sleep ESP32** (`power_mode == 1`): on wake the device
  advertises for `sleep_timeout_ms` in a tight loop that **does not call
  `updatemsdata()`** ([`main.cpp:110-132`](../../Firmware/src/main.cpp#L110)) —
  it only checks for an incoming connection — then `enterDeepSleep()` powers the
  radio down entirely. So during the whole advertising window the payload is
  **static** (Gate 2 suppresses everything after the first frame), and during
  deep sleep there are no advertisements at all.

The firmware therefore *guarantees* long runs of byte-identical advertisements,
which is exactly what Gate 2 collapses. This is why, even setting the
`connectable` question aside, a callback-derived `last_seen` is doomed to look
"stuck."

---

## 7. Root cause

`last_seen` is computed from a wall-clock timestamp captured inside
`OpenDisplayCoordinator._async_handle_bluetooth_event`. Home Assistant core
only calls that callback for advertisements that **both**:

1. **match the coordinator's `connectable=True` subscription** (Gate 1,
   `match.py:407`), and
2. **carry a payload that differs from the previously stored advertisement**
   (Gate 2, `manager.py:1274`).

The device's advertised payload is unchanged for long stretches (Section 6), and
depending on the scanner topology some or all of its advertisements are
delivered as non-connectable — so the callback is invoked far less often than
the device is actually seen. Meanwhile the advertisement monitor reads
`_all_history[address].time`, which core refreshes on **every** received frame
*before* both gates. The sensor and the monitor are reading two different clocks;
the sensor's clock is the wrong one for a "last seen" semantic.

This is an **integration-side design bug**, not a core or firmware bug. Core's
de-duplication and connectable filtering are working as intended; the integration
is simply sourcing "last seen" from a signal that was never meant to track
per-frame arrival time.

---

## 8. Recommended fix (for a follow-up change — not applied here)

Stop deriving `last_seen` from the callback snapshot. Read the same authoritative
timestamp the advertisement monitor uses.

**Option A — use the coordinator's inherited `last_seen` property.**
`BasePassiveBluetoothCoordinator` already exposes
[`last_seen`](../../core/homeassistant/components/bluetooth/update_coordinator.py#L76):

```python
@property
def last_seen(self) -> float:
    if service_info := async_last_service_info(self.hass, self.address, self.connectable):
        return service_info.time          # monotonic; from _connectable_history (connectable=True)
    return self._last_unavailable_time
```

Because `_connectable_history[address]` is written at `manager.py:1237`
**before** Gate 2, this value is fresh on every *connectable* advertisement and
is **not** subject to the de-dup short-circuit. Have the `last_seen` sensor read
`self.coordinator.last_seen` instead of `coordinator.data.last_seen`.

**Option B — match the monitor exactly (any scanner).** If parity with the
advertisement monitor is desired (i.e. "seen by *any* scanner, connectable or
not"), read `async_last_service_info(hass, address, connectable=False).time`,
which pulls from `_all_history` — the identical source the monitor serializes.
This is the most robust choice if the device is sometimes seen only via a
non-connectable proxy.

**Two implementation details either option must handle:**

1. **`service_info.time` is a monotonic clock, not wall time.** The sensor needs
   a wall-clock `datetime`. Convert with the same offset the monitor uses
   ([`websocket_api.py:135`](../../core/homeassistant/components/bluetooth/websocket_api.py#L135)):
   `wall = service_info.time + (time.time() - time.monotonic())`, then
   `datetime.fromtimestamp(wall, tz=timezone.utc)`.
2. **The sensor must re-render when the value changes.** `async_update_listeners`
   still only runs on the gated callback, so a value that now changes on every
   frame would not necessarily be pushed to the frontend on every frame. For a
   diagnostic "last seen" this is usually acceptable (the value refreshes
   whenever *any* gated update or availability change occurs, which is frequent
   enough), but if precise freshness is required, drive updates from the
   already-present `async_track_unavailable` / a periodic re-read rather than
   only the advertisement callback.

The same reasoning applies to the **RSSI** sensor; core's
`async_last_service_info(...).rssi` is the fresher source there.

---

## 9. How to confirm which gate dominates in this specific deployment

The fix above is correct regardless, but to attribute the freeze precisely:

1. **Enable Bluetooth debug logging** and watch dispatch:
   ```yaml
   logger:
     logs:
       homeassistant.components.bluetooth: debug
       habluetooth: debug
   ```
   Look for the device's address. If you see `_all_history` refreshing but no
   `... match:` dispatch lines, Gate 2 (identical payload) is suppressing.

2. **Check connectable status.** In Developer Tools → Template:
   ```jinja
   {{ (states.sensor.<device>_rssi.entity_id) }}
   {{ integration_entities('bluetooth') }}
   ```
   or inspect the Bluetooth integration **diagnostics** download for the device —
   compare its presence in `connectable_history` vs `all_history`. If it appears
   only in `all_history`, Gate 1 (`connectable=True`) is dropping it and you have
   a non-connectable-only path (a passive proxy with no connectable adapter in
   range).

3. **Watch the loop counter.** Decode `manufacturer_data[0x2446]` byte 15 high
   nibble across successive monitor frames. If it is not advancing, the firmware
   is not calling `updatemsdata()` (asleep / idle window), confirming the
   static-payload driver for Gate 2.

---

## 10. File reference index

Integration (`custom_components/opendisplay/`):
- [`sensor.py:74`](../custom_components/opendisplay/sensor.py#L74) — `_LAST_SEEN_DESCRIPTION` (reads `upd.last_seen`)
- [`sensor.py:129`](../custom_components/opendisplay/sensor.py#L129) — `native_value` reads `coordinator.data`
- [`coordinator.py:28`](../custom_components/opendisplay/coordinator.py#L28) — `OpenDisplayUpdate` dataclass
- [`coordinator.py:45`](../custom_components/opendisplay/coordinator.py#L45) — coordinator constructed `connectable=True`
- [`coordinator.py:104`](../custom_components/opendisplay/coordinator.py#L104) — `_async_handle_bluetooth_event`; `last_seen=time.time()` at [`:141`](../custom_components/opendisplay/coordinator.py#L141)

Home Assistant core (`../core/homeassistant/components/bluetooth/`):
- [`update_coordinator.py:76`](../../core/homeassistant/components/bluetooth/update_coordinator.py#L76) — inherited `last_seen` property (the correct source)
- [`update_coordinator.py:88`](../../core/homeassistant/components/bluetooth/update_coordinator.py#L88) — `async_register_callback` with `connectable=self.connectable`
- [`passive_update_coordinator.py:82`](../../core/homeassistant/components/bluetooth/passive_update_coordinator.py#L82) — base callback → `async_update_listeners`
- [`match.py:399`](../../core/homeassistant/components/bluetooth/match.py#L399) — `ble_device_matches`; connectable reject at [`:407`](../../core/homeassistant/components/bluetooth/match.py#L407)
- [`manager.py:138`](../../core/homeassistant/components/bluetooth/manager.py#L138) — `_discover_service_info` dispatch to callbacks
- [`websocket_api.py:204`](../../core/homeassistant/components/bluetooth/websocket_api.py#L204) — advertisement monitor; `connectable=False` at [`:209`](../../core/homeassistant/components/bluetooth/websocket_api.py#L209); time serialization at [`:107`](../../core/homeassistant/components/bluetooth/websocket_api.py#L107)

`habluetooth==6.26.5` (`manager.py`):
- `:1237` / `:1239` — `_connectable_history` / `_all_history` updated (pre-gate)
- `:1274`–`1304` — identical-payload short-circuit (Gate 2)
- `:1315`–`1323` — non-connectable → connectable upgrade
- `:1333` — dispatch to registered callbacks
- `:248`–`264` — seed-on-subscribe replay from history

Firmware (`../Firmware/src/`):
- [`display_service.cpp:1283`](../../Firmware/src/display_service.cpp#L1283) — `updatemsdata()`; counter in status byte at `:1301`; "don't re-advertise if unchanged" at `:1329`
- [`main.cpp:229`](../../Firmware/src/main.cpp#L229) — 60 s idle MSD cadence (mains)
- [`main.cpp:110`](../../Firmware/src/main.cpp#L110) — deep-sleep wake advertises without calling `updatemsdata()`
