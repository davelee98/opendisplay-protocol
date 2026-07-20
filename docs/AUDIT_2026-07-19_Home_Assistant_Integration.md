# Audit — Home_Assistant_Integration (`custom_components/opendisplay`)

**Repo:** `/home/davelee/opendisplay/Home_Assistant_Integration`
**Branch:** `feat/per-mac-ble-lock`
**HEAD audited:** `07d4976` — *feat(ble): process-global per-MAC connection lock with contention WARNING*
**Manifest version:** `3.0.0-beta.7` — pins `py-opendisplay[silabs-ota]==7.12.0`, `odl-renderer==0.5.12`
**Method:** static analysis only (no build, no test run, no HA). Sibling pins verified read-only against tags `v7.12.0` (py-opendisplay) and `odl-renderer-v0.5.12`.
**Prior pass incorporated:** manifest pins py-opendisplay==7.12.0 while sibling checkout is 7.13.0 (verified by prior audit pass).

---

## Architecture overview

Top-of-pipeline HA custom component (`iot_class: local_push`, BLE manufacturer id 9286) that wires `odl-renderer` (ODL → PIL) + `py-opendisplay` (BLE upload) and exposes services `upload_image`, `drawcustom`, `activate_led`, `activate_buzzer`.

- **`ble_lock.py`** — this branch's feature. Process-global per-MAC `asyncio.Lock` registry (`WeakValueDictionary`) + `ble_connection(address, purpose)` async CM that serializes every host-side connect to a tag and WARNs on contention.
- **`__init__.py`** — entry setup: live interrogation (firmware+config) or cache-only setup for dark sleepy tags; device-registry population; delivery-manager wiring; reboot-edge reload; unload drain.
- **`coordinator.py`** — passive BLE advert coordinator; parses adverts, tracks `last_seen`, fires device-seen + reboot-edge callbacks.
- **`delivery.py`** — `DeliveryManager`: latest-wins per-type queue, one-connection-per-wake drain, retry cap, time-based expiry, auth-pause.
- **`sleep.py`** — pure `SleepProfile`: `is_sleepy`, `availability_interval`, `probably_asleep(last_seen)`.
- **`services.py`** — schema validation, executor-offloaded render/dither/encode, live-vs-queue routing, probe-before-queue, LED/buzzer.
- **`update.py`** — GitHub-polled firmware update entity; BLE OTA only for EFR32BG22 (Silabs AppLoader).
- **`config_flow.py`** — BLE discovery, encryption-key step, reauth, options (sleep/queue/pipe tuning).

**Per-MAC lock feature assessment: correct.** All 5 connect sites are wrapped (`__init__.py:242`, `config_flow.py:160`, `delivery.py:311`, `services.py:409`, `update.py:249`); the OTA nested app-mode + AppLoader connects run under the outer lock (non-reentrant, as intended). Get-or-create is atomic (no `await` between `get` and insert). `WeakValueDictionary` keeps locks bound to the live event loop; `runtime_data.ble_lock` holds the strong ref keeping the lock alive for the entry's lifetime; `_HOLDERS` is popped in a `finally` (no unbounded growth). No BLE connect path bypasses the lock.

---

## Findings by severity

### Critical
None.

### High
None.

### Medium

**M1 — Entry unload / reboot-reload can block up to ~600 s on an in-flight service call.**
`__init__.py:410` (`async_unload_entry`) and `__init__.py:390` (`_async_reload_after_reboot`) drain the per-MAC lock with an **unbounded** `async with runtime.ble_lock: pass` (no `asyncio.timeout`). Before the drain, only `runtime.upload_task` is cancelled — and that field is set **exclusively** by `upload_image` (`services.py:639`). An in-flight `drawcustom`, `activate_led`, `activate_buzzer`, or an OTA install holds the lock but is *not* cancelled, so the drain waits until that operation's own `DELIVERY_DEADLINE_S`/`OTA_INSTALL_DEADLINE_S` (both 600 s) elapses.
*Failure scenario:* a `drawcustom` to a slow/wedged link is running; the user removes/reloads the entry (or the tag advertises a reboot edge on a non-sleepy device) → unload/reload stalls up to ~10 min, tripping HA's "waiting to unload" machinery. Waiting is deliberate for OTA (don't tear down mid-flash), but the total absence of a ceiling on the drain is the defect.
*Confidence:* Confirmed (traced).

### Low

**L2 — `drawcustom` uploads are untracked and have no latest-wins / coalescing.**
`services.py:634-639`: only `upload_image` records its task in `runtime_data.upload_task`. Two concurrent `drawcustom` calls to the same device serialize on the ble_lock (each emitting a contention WARNING) instead of superseding, and neither can be cancelled on unload (feeds M1). Rapid automation loops therefore queue behind the full per-op deadline rather than dropping stale frames. `upload_image` does supersede correctly (`services.py:633-639`).
*Confidence:* Confirmed.

**L3 — `_pending_config_resync` has no attempt cap or expiry.**
`delivery.py`: uploads are bounded by `MAX_DELIVERY_ATTEMPTS` (5) and time-based expiry, but a pending config resync (`delivery.py:226`, drained at `:340`/`:364`) retries on every wake forever for a permanently unreachable device. Cheap and idempotent, so low impact, but asymmetric with the upload path.
*Confidence:* Confirmed.

**L4 — Wall-clock vs monotonic-clock mismatch in sleep/expiry math.**
`sleep.py:107` (`probably_asleep`) and `delivery.py:203` (`expires_at = time.time() + queue_timeout_s`) use wall-clock `time.time()` — matching `coordinator.last_seen` (`coordinator.py:141`, also `time.time()`), so the freshness comparison is internally consistent — but the *actual* expiry timer is `async_call_later` (`delivery.py:396`), which is monotonic. A system clock step (NTP correction, RTC-less boot) skews `probably_asleep` freshness decisions and makes the reported `expires_at` diverge from when the timer really fires. No off-by-one found; `probably_asleep` uses a clean strict `>` (`sleep.py:108`).
*Confidence:* Confirmed (behavioral), Plausible (real-world clock-jump trigger).

**L5 — Diagnostics redaction is field-name based; a secret in `GlobalConfig` under an unexpected key would leak.**
`diagnostics.py:11` `TO_REDACT = {"ssid", "password", "server_url"}` and `_asdict` (`:14`) dumps the entire interrogated `device_config` with all `bytes` hex-encoded. Redaction matches only those exact field names; any secret-bearing TLV field named otherwise (e.g. a WiFi PSK/provisioning token under a different key) is emitted. The **encryption key is safe** — diagnostics dumps `runtime.device_config`/`firmware`, never `entry.data` (which holds `CONF_ENCRYPTION_KEY`).
*Confidence:* Plausible (depends on `GlobalConfig` field naming; not confirmed to contain such a field at 7.12.0).

---

## Positive confirmations (no action needed)

- **Executor discipline is correct.** All CPU-heavy work is offloaded: `prepare_image` (`services.py:489`), PIL JPEG encode (`services.py:515`, `:874`), disk image load (`services.py:649`), font-dir `os.path.isdir` scan (`services.py:869`), recorder history (`services.py:697`). `generate_image` is async. No blocking render/dither/PIL/file-I/O runs on the event loop. (`config_from_json`/`config_to_json` run on the loop in `_load_cache`/`_write_cache` but are lightweight TLV parsing.)
- **All 5 BLE connect sites are lock-wrapped; none bypass** (see architecture note).
- **`upload_image` latest-wins composes with the lock without deadlock** — the superseded task releases the lock before the new one acquires it (`services.py:633-639`).
- **All `translation_key=` references resolve** in `strings.json` (`exceptions` block covers every one).
- **`services.yaml` ↔ registered handlers match**: 4 services (`upload_image`, `drawcustom`, `activate_led`, `activate_buzzer`); schemas and yaml fields agree, incl. `tone_compression`/`measured_palette`. `SCHEMA_DRAWCUSTOM` uses `extra=vol.REMOVE_EXTRA` to drop legacy keys.
- **Config flow**: unique-id set + `_abort_if_unique_id_configured` guards duplicates; reauth handled with optional key clear; probe bounded by `CONNECT_PROBE_DEADLINE_S`.

---

## Unimplemented / partial features

| Feature | State | Evidence |
|---|---|---|
| Persistent delivery queue | Memory-only; a HA restart drops a pending upload | `delivery.py:16` (documented "Memory-only in v1") |
| `drawcustom` supersede/coalesce | Not implemented (only `upload_image` supersedes) | L2 |
| Config-resync give-up | No cap/expiry (retries forever) | L3 |
| Bounded unload/reload drain | No timeout around the lock drain | M1 |
| Deep-sleep / power-off / NFC commands | Not sent by the integration at all (relies on firmware autonomous deep sleep; reads config/adverts only) | grep: no `deep_sleep`/`power_off`/`nfc` write calls |
| BLE OTA | EFR32BG22 only; nRF/ESP32 get release-note visibility only | `update.py:63` `_OTA_INSTALL_IC_TYPES` |

---

## Cross-repo observations

- **Pins are internally consistent and safe.** Every py-opendisplay symbol the integration imports/calls exists at exactly `v7.12.0`: `OpenDisplayDevice.__init__(timeout=, max_attempts=, blocks_per_ack=, max_queue_size=, config=, use_measured_palettes=)`, `prepare_image(config=, dither_mode=, compress=, tone=, fit=, rotate=)`, `upload_prepared_image(prepared, refresh_mode=, state=)`, `activate_led`/`activate_buzzer`, `BuzzerActivateConfig.single_tone`, `LedFlashConfig`/`LedFlashStep`, `perform_silabs_ota`, `firmware_ota_asset`/`firmware_release_repo`, `config_from_json`/`config_to_json`, `PartialState`, `RefreshMode`/`DitherMode`/`FitMode`/`Rotation`/`ColorScheme`, `MANUFACTURER_ID`. `odl-renderer` `generate_image(width,height,elements,background,accent_color,session,data_provider,font_dirs)` matches `odl-renderer-v0.5.12` exactly. **No call reaches into any 7.13.0-only surface.**
- **The v2.1 opcode swap (0x0052 = POWER_OFF / 0x0053 = DEEP_SLEEP) and the 7.12.0 "deep sleep sent as 0x0052" bug do NOT affect this integration.** It never issues a deep-sleep, power-off, or NFC-write command — deep sleep is entirely firmware-autonomous (timer wake), and `SleepProfile` only *mirrors* the firmware entry condition (`sleep.py:14-19`) to decide queue-vs-live. The 7.12.0 deep-sleep opcode defect and the NFC-write-landed-in-7.13.0 gap are both unreachable from this consumer at the current pin.
- **244 B GATT write ceiling** is a library/framing concern; the integration only surfaces `blocks_per_ack`/`max_queue_size` tuning (1–32) via options and delegates framing to py-opendisplay. No integration-level violation.
- **HA 244 B / pipe-window options** (`CONF_BLOCKS_PER_ACK`, `CONF_MAX_QUEUE_SIZE`) are plumbed consistently across `services.py`, `delivery.py`, and `config_flow.py`.
