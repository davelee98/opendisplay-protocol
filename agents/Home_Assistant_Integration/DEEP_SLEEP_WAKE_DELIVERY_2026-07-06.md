# OpenDisplay Deep Sleep — Wake-Triggered Delivery Mechanism

*2026-07-06 — How the Home Assistant integration delivers queued content to a deep-sleeping device the moment it becomes reachable again. Describes the implementation on branch `feat/deep-sleep` ([PR #1](https://github.com/davelee98/Home_Assistant_Integration/pull/1)). Companion to [DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md](DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md) (decision D4) and [DEEP_SLEEP_FINDINGS_2026-07-06.md](DEEP_SLEEP_FINDINGS_2026-07-06.md).*

## TL;DR

When Home Assistant sees a sleeping device advertise, and there is queued work for it, the integration immediately opens one BLE connection and drains all pending work over that single session. Latest-wins deduplication, single-connection batching to conserve battery, and a hard expiry deadline as the failure floor.

---

## The problem this solves

A deep-sleeping ESP32 tag is dark almost all the time. It wakes on a timer, advertises for a short window (~10 s, `sleep_timeout_ms`), and — if nothing connects — returns to sleep. The only signal that the device is reachable is that advertisement arriving in Home Assistant's Bluetooth stack. So the delivery machinery is built around a single idea: **treat every advertisement as a rendezvous opportunity, and connect within it only when there is work to do.**

---

## 1. The trigger — every advertisement is a "device seen" event

The integration already ran a passive coordinator (`OpenDisplayCoordinator`) that receives every advertisement, so that is the tap point — no new Bluetooth registration was needed.

Immediately after each advertisement is successfully parsed, the coordinator fires a new callback set (`coordinator.py`, in `_async_handle_bluetooth_event`):

```python
for device_seen_callback in list(self._device_seen_callbacks):
    device_seen_callback()
```

Subscribers register via `async_subscribe_device_seen()`, which deliberately mirrors the existing `async_subscribe_reboot()` pattern and returns an unsubscribe callback.

Key property: this fires on **every** advertisement, not just an availability edge. A tag that wakes, isn't caught in time, and wakes again will fire it once per cycle — giving delivery repeated chances at successive wake windows rather than a single shot.

---

## 2. The consumer — `DeliveryManager` arms on that callback

At entry setup, the manager subscribes to the coordinator's device-seen callback. Every advertisement then reaches `notify_device_seen()`:

```python
def notify_device_seen(self, source: str = "ble") -> None:
    if self._delivering or not self._has_pending_work():
        return
    self._delivering = True
    self._delivery_task = self._entry.async_create_background_task(
        self._hass, self._deliver(), f"opendisplay_delivery_{self._address}"
    )
```

Two guards keep this cheap and correct:

- **No pending work → return immediately.** This is the common case; most advertisements from an idle tag do nothing.
- **Delivery already in flight (`self._delivering`) → return.** A burst of advertisements within one wake window cannot spawn overlapping connections.

`source` is the transport-agnostic seam. Today the BLE coordinator calls it with `"ble"`; a future WiFi/mDNS presence tracker calls the same method with `"mdns"` and nothing else changes.

---

## 3. The drain — one connection, all work, priority order

`_drain_once()` performs the delivery. Its structure reflects the physics of the device:

1. **Resolve the encryption key.** A malformed stored key starts a reauth flow and bails.
2. **Acquire the per-entry `ble_lock`.** This is the same lock service calls and OTA use, so wake-delivery can never race a manual upload on the same tag.
3. **Resolve a fresh `BLEDevice`** from the very advertisement that just woke the tag. If it is already `None`, the window has aged out → record a failed attempt and wait for the next wake.
4. **Open one `OpenDisplayDevice` session** under `asyncio.timeout(DELIVERY_DEADLINE_S)` (30 s) and drain every pending slot in priority order:
   - **Upload first** (user-visible content).
   - **Config resync second** (cheap firmware/config reads over the already-open link, refreshing the cached config).

```python
async with (
    asyncio.timeout(DELIVERY_DEADLINE_S),
    OpenDisplayDevice(
        mac_address=self._address,
        ble_device=ble_device,
        config=runtime.device_config,
        use_measured_palettes=use_measured,
        encryption_key=key,
    ) as device,
):
    if upload is not None and not upload.paused:
        await self._drain_upload(device, upload)
    if self._pending_config_resync:
        await self._drain_resync(device)
```

**Why one connection drains everything:** the firmware keeps the tag awake for as long as a BLE central is connected. So the ~10 s advertising window only needs to cover *connection establishment* — once connected, there is effectively unlimited time to transfer the image and read config. The 30 s deadline caps a single wake's attempt so that one slow/bad wake cannot bleed into the next scheduled one.

---

## 4. Failure handling — retries and the backup timer

`_deliver()` wraps the drain and classifies outcomes:

| Outcome | Handling |
|---|---|
| Connection / timeout failure (slept mid-connect, window missed) | `_register_attempt_failure`: increment `attempts`, keep work queued, retry on the next `device_seen`. |
| Auth failure (bad/rotated key) | Pause the slot and start reauth — stops doomed retries until the user re-authenticates. |
| Protocol / device error | Record the failure, keep queued, retry next wake. |
| Success | Clear the slot, fire `opendisplay_content_delivered`, re-timestamp the image entity. |

**The backup timer.** When an upload is submitted, `async_call_later` arms a deadline for `queue_timeout_s` (default **24 h**, safely above the ~18 h maximum sleep interval). If no wake ever succeeds in delivering it, `_expire_upload` fires the `opendisplay_content_expired` bus event and drops the frame. This is the "give up after X hours and surface a failure" backstop.

Latest-wins deduplication: submitting a new upload cancels the previously queued frame and its deadline timer, so only the newest image survives — matching the existing live-upload semantics (a fresh snapshot supersedes a stale one).

---

## 5. What the user sees while content is queued

- **On queue** (`submit_upload`): the rendered frame is pushed to the Display Content **image entity immediately**, so it shows the *intended* image (not what is currently on the panel), and pending state is pushed to the **"Update pending" binary sensor** (attributes: `queued_at`, `expires_at`, `attempts`). The originating service call returns `{"status": "queued", "expires_at": …}`.
- **On delivery**: fires `opendisplay_content_delivered`, clears the pending state, and re-timestamps the image to when the frame actually landed on the panel.
- **On expiry**: fires `opendisplay_content_expired` and clears the pending state with `last_error = "expired"`.

Automations can branch on the service response or key off either bus event.

---

## End-to-end sequence

```
Automation → upload_image (device asleep)
   │
   ├─ render + prepare_image (CPU work, done once)
   ├─ freshness gate: adverts stale → device provably asleep
   ├─ DeliveryManager.submit_upload()
   │     ├─ arm 24h expiry timer
   │     ├─ image entity shows intended frame (pending: true)
   │     └─ binary_sensor "Update pending" → on
   └─ service returns {status: "queued"}
        ⋮  (device sleeps N minutes)
Device timer-wakes → advertises
   │
Coordinator._async_handle_bluetooth_event → device_seen callbacks
   │
DeliveryManager.notify_device_seen("ble")
   ├─ pending work? yes.  already delivering? no.
   └─ background task: _drain_once()
        ├─ acquire ble_lock
        ├─ resolve fresh BLEDevice from this advertisement
        ├─ open ONE OpenDisplayDevice session (≤30s)
        │    ├─ upload_prepared_image()        ← device stays awake while connected
        │    └─ config resync (if requested)
        ├─ cancel expiry timer, clear slot
        ├─ fire opendisplay_content_delivered
        └─ image re-timestamped, binary_sensor → off
```

---

## Design properties (summary)

- **Advertisement-driven, not timer-driven.** Connections are attempted at the exact moment the device is known reachable, so the first attempt starts ~1 s into the wake window and the library's default connection budget is ample. HA's blind setup-retry backoff is never relied on for delivery.
- **One connection per wake.** All pending work batches into a single session; battery cost is dominated by connected time, so this minimizes it.
- **Latest-wins.** Only the newest frame is kept, consistent with live-upload semantics.
- **Transport-agnostic.** `notify_device_seen(source)` is the single entry point; BLE today, mDNS/WiFi later, no change to the drain logic.
- **Bounded failure.** Per-wake 30 s deadline; per-item 24 h expiry with an explicit failure event.
- **v1 limitation:** the queue is in-memory, so a Home Assistant restart drops a pending (not-yet-delivered) upload. The binary sensor goes off honestly. Persisting prepared frames to a `Store` is a documented v2 option.

## Source map

| Concern | Location |
|---|---|
| Device-seen callback fired per advertisement | `coordinator.py` — `_async_handle_bluetooth_event`, `async_subscribe_device_seen` |
| Queue, wake handling, drain, expiry | `delivery.py` — `DeliveryManager` |
| Freshness gate + queue-on-failure + response data | `services.py` |
| Pending frame + attributes | `image.py` |
| "Update pending" entity | `binary_sensor.py` |
| Sleep detection + timings | `sleep.py` — `SleepProfile` |
