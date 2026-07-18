# Minimal fix plan — `last_seen` reads the "updated" field

**Date:** 2026-07-09
**Depends on:** [LAST_SEEN_SENSOR_INVESTIGATION_2026-07-09.md](LAST_SEEN_SENSOR_INVESTIGATION_2026-07-09.md)
**Goal:** Make `sensor.<device>_last_seen` reflect the Bluetooth stack's
"last advertisement received" time — the exact value the **Advertisement
monitor's "updated" column** shows — instead of the wall-clock snapshot taken
inside the de-dup-/connectable-gated coordinator callback.

**Non-goal (explicitly out of scope):** the image-upload wake trigger
(`_device_seen_callbacks` → `DeliveryManager`) and the RSSI sensor. Those share
the same root cause but are separate fixes; see "Scope boundaries" below.

---

## 1. What changes, in one sentence

Stop deriving `last_seen` from `coordinator.data.last_seen` (written only in the
gated advertisement callback) and instead read
`async_last_service_info(hass, address, connectable=False).time` — the
`_all_history[address].time` that core refreshes on **every** received
advertisement, before the de-dup short-circuit — and re-render the entity on a
light interval so it tracks that value.

This is the same source, and same `connectable=False` scope, the advertisement
monitor uses ([websocket_api.py:107 / :209](../../core/homeassistant/components/bluetooth/websocket_api.py#L107)),
so the two will agree by construction.

---

## 2. The two-part change (both small, both required)

Part A fixes the **value**; Part B fixes the **refresh**. Part A alone makes the
number correct *when the entity happens to update*, but the entity only updates
on the gated callback today — so if the callback is what's starved (the user's
symptom), the value stays frozen without Part B.

### Part A — repoint the value source

In [`sensor.py`](../custom_components/opendisplay/sensor.py), give `last_seen` its
own `native_value` that reads the stack's last-advertisement time and converts it
from the monotonic clock to a wall-clock `datetime` (the `SensorDeviceClass.TIMESTAMP`
contract requires tz-aware wall time). Representative shape:

```python
from homeassistant.components.bluetooth import async_last_service_info

class OpenDisplayLastSeenSensor(OpenDisplaySensorEntity):
    """last_seen sourced from the bluetooth stack, not the gated callback."""

    @property
    def native_value(self) -> datetime | None:
        info = async_last_service_info(
            self.hass, self.coordinator.address, connectable=False
        )
        if info is None:
            return None
        # info.time is monotonic (monotonic_time_coarse); convert to wall clock
        # with the same offset the advertisement monitor uses.
        wall = info.time + (time.time() - time.monotonic())
        return datetime.fromtimestamp(wall, tz=timezone.utc)
```

- Instantiate this subclass for `_LAST_SEEN_DESCRIPTION` in
  `async_setup_entry` (the other descriptions keep using `OpenDisplaySensorEntity`).
- `_LAST_SEEN_DESCRIPTION.value_fn` becomes unused for this entity; either leave a
  no-op `value_fn=lambda upd: None` or make `value_fn` optional on the dataclass
  (`Callable[...] | None = None`) — the latter is cleaner and touches only the
  description class.
- **Even-more-minimal variant:** read `self.coordinator.last_seen` (the inherited
  `BasePassiveBluetoothCoordinator.last_seen` property,
  [update_coordinator.py:76](../../core/homeassistant/components/bluetooth/update_coordinator.py#L76))
  instead of calling `async_last_service_info` directly. It already does the
  history lookup — but it is scoped to `connectable=True` (`_connectable_history`),
  so it matches the monitor only when the device has a connectable path. Prefer the
  explicit `connectable=False` call above for exact monitor parity.

### Part B — refresh so it actually tracks the field

The entity is push-based (`should_poll = False`); its state is only re-written
when `async_update_listeners()` fires, which is still only the gated callback +
availability transitions. Add the smallest possible independent driver so the new
value is re-read while the device is advertising:

```python
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(seconds=60)
            )
        )

    @callback
    def _async_tick(self, _now) -> None:
        # Only write when the value actually advanced, so a sleeping device
        # (history time frozen) creates no recorder churn.
        new = self.native_value
        if new != self._attr_native_value_cache:   # or compare to last written
            self._attr_native_value_cache = new
            self.async_write_ha_state()
```

- 60 s is comfortable for a disabled-by-default diagnostic sensor; it does not
  need to match the radio frame rate.
- The **changed-value guard is important**: while the device is asleep the history
  time stops advancing, so `native_value` is stable and no state is written — no
  recorder spam. While advertising it advances and writes at most once/60 s.
- If even this timer is unwanted, Part A alone is still a strict improvement (the
  value is correct at every callback/availability update and matches the monitor
  at those moments); it just won't advance during a long de-duped stretch.

---

## 3. Files touched

| File | Change |
|---|---|
| [`custom_components/opendisplay/sensor.py`](../custom_components/opendisplay/sensor.py) | Add `OpenDisplayLastSeenSensor`; use it for `_LAST_SEEN_DESCRIPTION`; new imports (`time`, `async_last_service_info`, `async_track_time_interval`, `timedelta`). Optionally make `value_fn` optional on the description dataclass. |
| `tests/` | Add/adjust unit tests (Section 5). |

No changes to `coordinator.py`, `delivery.py`, `entity.py`, core, or firmware.

---

## 4. Edge cases to handle

1. **Never seen yet** → `async_last_service_info` returns `None` → `native_value`
   returns `None` (entity shows "unknown"). Matches current None-guarding.
2. **Monotonic → wall conversion** — must add `time.time() - time.monotonic()`;
   forgetting it yields timestamps near the epoch. (This is the one real trap.)
3. **Device unavailable but previously seen** — `async_last_service_info` still
   returns the last stored `service_info`, so `last_seen` correctly shows the last
   real sighting rather than going `None`. Desired for a "last seen" sensor.
4. **`connectable=False` vs the coordinator's `connectable=True`** — intentional:
   we want "seen by any scanner," which is what the monitor shows and what the
   symptom is about.

---

## 5. Tests

- **Value/conversion:** seed a fake `service_info` with a known monotonic `time`
  via the bluetooth test helpers (`inject_bluetooth_service_info` /
  `async_last_service_info` monkeypatch) and assert `native_value` equals the
  expected wall-clock `datetime` within tolerance.
- **De-dup case (the regression):** deliver one changed advert, then several
  byte-identical adverts; assert that after a `_async_tick` fire the sensor's
  timestamp has advanced past the first advert's time (proves it no longer needs a
  payload change). Use `async_fire_time_changed` to trigger the interval.
- **Never-seen → None.**
- **Asleep → no churn:** with history time held constant, fire the interval and
  assert `async_write_ha_state` did not record a new state (changed-value guard).

---

## 6. Scope boundaries (what this deliberately does NOT fix)

- **Image-upload wake trigger.** `DeliveryManager` starts a delivery from
  `_device_seen_callbacks`, fired in the *same* gated `else` branch
  ([coordinator.py:145](../custom_components/opendisplay/coordinator.py#L145)).
  This plan does not touch that path — a static-payload wake can still fail to
  trigger delivery. That is a separate, higher-priority fix (most robustly in
  firmware: guarantee the first post-wake advertisement differs). Tracked
  separately.
- **RSSI sensor** has the identical value-source defect; left unchanged here to
  keep the fix minimal. A follow-up can point it at
  `async_last_service_info(...).rssi`.
- **No firmware or core changes.**

---

## 7. Risk & rollback

- **Behavioral change:** `last_seen` now advances on any received advertisement
  (including de-duped frames, via the interval) and reflects non-connectable
  sightings. This is the intended fix; call it out in the changelog. Automations
  keyed on the old (rarely-updating) behavior are unlikely but worth a release
  note.
- **Recorder volume:** bounded by the 60 s interval and the changed-value guard;
  effectively one row per minute only while the device is actively advertising,
  and the entity is disabled by default.
- **Rollback:** revert `sensor.py`; the entity returns to reading
  `coordinator.data.last_seen`. No migration, no persisted state shape change.

---

## 8. Definition of done

- With the device advertising, `sensor.<device>_last_seen` tracks the
  Advertisement monitor's "updated" value within ~60 s.
- With the device asleep, it holds the last real sighting and records nothing new.
- Unit tests in Section 5 pass; `ruff` / type checks clean.
