# Audit report: deep-sleep implementation across both outstanding PRs

**Date:** 2026-07-08
**Auditor:** Claude (fresh audit; no reference to pre-existing findings docs)

**Scope.** The state audited is the merge of each fork's single outstanding PR onto its
base, built in isolated worktrees:

- `davelee98/Home_Assistant_Integration` PR #1 (`feat/deep-sleep` → `feat/clean-port`,
  +2,245/−72 lines)
- `davelee98/py-opendisplay` PR #1 (`feat/deep-sleep-command` → `main`, +299/−1)

Both merged cleanly with no conflicts. Findings were derived from the code, the two
firmware repos (`Firmware`, `Firmware_Silabs`), and the HA core/habluetooth sources.
All line references are to the PR branches.

**Verdict.** The design is sound and most of it is verifiably correct against the
firmware. One confirmed data-loss race in the delivery manager (reproduced with a
failing test), one robustness gap (unbounded retries), and a handful of lower-severity
issues. Test suites pass: 20/20 HA integration tests, 510/510 py-opendisplay unit tests.

## What the PRs do

- **py-opendisplay**: adds command `0x0052` (`build_deep_sleep_command`),
  `OpenDisplayDevice.deep_sleep()` which tolerates the link dropping mid-command, a
  `PowerOption.deep_sleep_enabled` property, and a CLI `sleep` subcommand.
- **HA integration**: makes the integration deep-sleep aware. A `SleepProfile`
  (sleep.py) resolves options + device power config into `is_sleepy` / availability
  horizon / freshness predicate. A `DeliveryManager` (delivery.py) queues a prepared
  image (latest-wins) and drains it over one BLE connection when the coordinator sees a
  wake advertisement. Setup can run entirely from a persisted config cache when a
  sleepy device is dark. Options flow (sleep mode auto/on/off, missed cycles, queue
  timeout), an "update pending" binary sensor, image-entity pending attributes, service
  responses (`delivered`/`queued`), fail-fast guards for LED/buzzer/OTA on sleeping
  devices, and a fallback availability interval keep entities alive across sleep cycles.

Notably, the integration **never calls the new `deep_sleep()` command** — the two PRs
are decoupled. That is correct: firmware re-enters deep sleep on its own after
disconnect (`Firmware/src/main.cpp:197`), and the manifest pin
`py-opendisplay==7.11.1` is unaffected since nothing imports the new API.

## Findings

### 1. HIGH — In-flight delivery silently discards an image queued during the drain

`delivery.py:299-310`. `_drain_once` captures `upload = self._pending_upload` before
connecting; on success `_drain_upload` unconditionally sets
`self._pending_upload = None`. If `submit_upload` replaces the slot while the old image
is mid-transfer (the drain can run up to `DELIVERY_DEADLINE_S = 30 s`; the freshness
gate happily queues during that window since the wake gate is ~15 s), the *new* image
is wiped without any event, log line, or state — the user's latest frame never reaches
the panel and nothing reports it. Confirmed with a repro test (see
`docs/test_race_repro_audit.py`; it imports `delivery.py`, so it only runs on the
`feat/deep-sleep` branch — copy it into `tests/` there): the new submission's
`pending=True` becomes `pending=False, last_error=None` when the old drain completes. Fix shape: guard the
clear with `if self._pending_upload is upload:` (and re-check pending work after the
drain so the new frame is delivered on the same wake).

### 2. MEDIUM — No retry cap or backoff on failed deliveries

`PendingUpload.attempts` is incremented (`delivery.py:371-381`) but never compared to a
limit, and `notify_device_seen` re-triggers on **every parsed advertisement**.
Consequences: (a) a persistently failing transfer retries on every wake until the 24 h
expiry — with a 5-minute sleep interval that is ~288 doomed connect+transfer cycles,
each costing the tag battery; (b) within one wake window a fast failure (e.g. "device
not connectable" from a passive-only advertisement) is retried on each advertisement,
potentially several per second, inflating `attempts` and churning state dispatches. A
max-attempts cap (the upstream draft PR used 3) and/or a per-wake cooldown would bound
both.

### 3. MEDIUM — `deep_sleep()` reports success on devices that did not sleep

Verified against both firmware trees: no firmware ever emits the `0xFF52`
"not supported" frame the library checks for (`device.py:990`) — the ESP32-family
dispatcher ignores the command on non-ESP32 targets with no response
(`device_control.cpp:700-703`), and the Silabs handler always ACKs. Worse, an ESP32
*without* a power latch whose config has `power_mode != BATTERY` or
`deep_sleep_time_seconds == 0` **refuses to sleep and sends nothing**
(`main.cpp:275-285`), which the library's read-timeout path logs as "device is
sleeping". So on nRF targets and on non-battery ESP32s, `deep_sleep()` and the CLI
`sleep` command return success while the device stays awake. Callers could be protected
by checking `config.power.deep_sleep_enabled` before sending (the property added in
this same PR), and the `0xFF52` branch is currently dead code.

### 4. LOW — Fallback availability interval is never cleared

`__init__.py` calls `async_set_fallback_availability_interval(hass, address, …)` only
when `is_sleepy`. habluetooth stores it in a plain dict with no unset path used here,
so after switching sleep mode ON → OFF (entry reloads via `OptionsFlowWithReload`), a
stale multi-hour interval persists until HA restarts, delaying "unavailable" detection
for a now-always-on device. Conversely there's no obvious core API to remove it — worth
at least re-setting a sane value when not sleepy.

### 5. LOW — Forcing sleep mode ON without device sleep config yields a 70 s availability window

With `SLEEP_MODE_ON` but `deep_sleep_time_seconds == 0`,
`availability_interval = 0 × missed_cycles + 10 s + 60 s = 70 s` (`sleep.py:79-89`) —
shorter than habluetooth's default fallback, so the override makes flapping *more*
likely on the edge-case devices it exists for. A floor (e.g. never below the default
fallback) would be safer.

### 6. LOW — Queued content is dropped without an event on reload

Documented as memory-only for restarts, but an options-flow save also reloads the
entry: `async_shutdown` cancels everything and the queued frame vanishes with no
`opendisplay_content_expired` event and no final pending-state dispatch, so automations
watching the binary sensor never learn the frame was lost.

### 7. NITS

- `binary_sensor.py`: push-driven entity doesn't set `_attr_should_poll = False`; also
  a natural candidate for `EntityCategory.DIAGNOSTIC`.
- `services.py`: `except asyncio.CancelledError: return {"status": "superseded"…}` also
  swallows shutdown cancellation, not just latest-wins supersession (pre-existing
  pattern, now with a response attached).
- Delivery success and expiry can double-fire if the deadline lands mid-drain (expired
  event followed by delivered event for the same slot). Cosmetic.

## Validated as correct

- **Sleep predicate parity**: `deep_sleep_enabled`
  (`power_mode == BATTERY(=1) and deep_sleep_time_seconds > 0`) exactly mirrors the
  firmware's autonomous sleep condition (`main.cpp:197`), and `PowerMode.BATTERY == 1`
  matches.
- **Wake window parity**: `DEFAULT_WAKE_WINDOW_MS = 10_000` matches the firmware's
  advertising-timeout default (`main.cpp:94-96`); `wake_window_s` correctly falls back
  when `sleep_timeout_ms == 0`.
- **Rendezvous design**: device-seen fires only on successfully parsed advertisements;
  the device stays awake while connected and re-sleeps after disconnect, so no explicit
  sleep command is needed.
- **Timebase consistency**: `last_seen = time.time()` in the coordinator matches
  `probably_asleep`'s clock; `last_seen=None` (fresh cache setup) safely queues.
- **Concurrency between paths**: live service uploads and the delivery drain both hold
  the same `runtime.ble_lock`, so they cannot fight over the single BLE link.
- **ACK formats**: Silabs replies `{0x00, 0x52}` and ESP32 power-latch replies
  `{0x00, 0x52, 0x00, 0x00}` — both pass `validate_ack_response`; the encrypted-session
  write/read paths handle the short plaintext error frames correctly, and
  `AuthenticationRequiredError` propagates rather than being masked as sleep.
- **Cache round-trip**: `config_to_json`/`config_from_json` preserve `power_mode`,
  `sleep_timeout_ms`, and `deep_sleep_time_seconds`; `FirmwareVersion` is a TypedDict,
  so storing/reloading it as a plain dict is valid; cache writes are change-detected to
  avoid store churn.
- **Availability API**: `async_set_fallback_availability_interval` signature and
  address keying verified against HA core + habluetooth; `entry.unique_id` always
  originates from `service_info.address`, so the key matches scanner lookups.
- **Auth failure handling**: delivery pauses the queued upload, starts reauth once, and
  skips paused work in later drains.
- **Options flow**: `OptionsFlowWithReload` without a manual update listener is the
  correct modern pattern; schema bounds are sensible.
- **Tests**: 20/20 HA integration tests and 510/510 py-opendisplay unit tests pass on
  the merged trees; the six new `deep_sleep()` tests cover ACK, latch-ACK, NACK,
  write-drop, read-timeout, and read-disconnect paths.

The one thing to insist on before merging is finding #1 — a confirmed, silent
data-loss path in the feature's core mechanism. #2 and #3 are worth fixing in the same
pass; the rest are polish.
