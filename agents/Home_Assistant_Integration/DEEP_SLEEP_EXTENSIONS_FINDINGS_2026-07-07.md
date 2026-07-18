# OpenDisplay Deep Sleep — Extension Findings: Command Queueing, Firmware Mechanics, Timed Sleep, and WiFi

*2026-07-07 — Four-part analysis produced by parallel investigations of the current code (Firmware, py-opendisplay, HA integration branch `feat/deep-sleep`, protocol spec, and the HA core source as read-only reference). Companion to [DEEP_SLEEP_FINDINGS_2026-07-06.md](DEEP_SLEEP_FINDINGS_2026-07-06.md), [DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md](DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md), [DEEP_SLEEP_WAKE_DELIVERY_2026-07-06.md](DEEP_SLEEP_WAKE_DELIVERY_2026-07-06.md), and [WIFI_ARCHITECTURE_2026-07-06.md](WIFI_ARCHITECTURE_2026-07-06.md). Every code claim carries a file:line citation; the load-bearing new claims (latch-path ACK loss, SleepProfile staleness after resync, LED cutoff, post-disconnect sleep timing) were independently re-verified against source.*

## Executive summary

**Q1 — Which additional command types should be queued for sleepy devices?** (Part 1)
Keep the two implemented slots (image upload, config resync). Add, in priority order: **config write** (queue, latest-wins, no expiry — the strongest new candidate: desired state, and fail-fast would make sleepy tags unconfigurable), **OTA** as the plan's deferred `pending_ota` slot, and an **opt-in `queue: next_wake` mode for LED/buzzer** (find-my-tag; buzzer first — it is firmware-safe today, LED needs a one-line firmware fix or an HA-side connection hold). Reboot only as a drain terminator; 0x0052 as an opt-in drain *epilogue* gated on no power latch; battery reads never (every advertisement already carries fresh battery data). Typed slots beat a generic command list — dedup, drain order, and cross-slot interlocks (config write clears the encryption session; OTA subsumes reboot) are per-type semantics.

**Q2 — Should firmware sleep mechanics change for multi-command wakes?** (Part 2)
For multiple *commands*, no: one connection already gives unlimited commands, and HA drains everything over one session. The real gaps are multi-*connection*: the device sleeps **< 1 s after disconnect** (verified trace), so a second client or a just-missed submission waits a full sleep interval. Recommended, in order: (1) the **LED hold-awake fix** (clamped `ledFlashActive` in `bleActive` — fixes a real cutoff bug), (2) a **~3 s post-disconnect linger window** (~0.04 mAh per *connected* wake, zero on idle wakes), (3) post-reboot grace window, (4) `rebootFlag` RTC persistence. Reject activity-extended advertising (not observable under Bluedroid legacy advertising, and a battery footgun). Defer a hold-awake command — no consumer yet.

**Q3 — "Sleep for X seconds": feasible?** (Part 3)
Yes, cheaply (~40 firmware lines). Recommended shape: **extend 0x0052 with an optional uint16 LE seconds payload** (empty payload = today's behavior; both back-compat directions verified clean), one-shot by construction — the override is a plain global consumed at sleep entry, so no RTC persistence and automatic reversion to configured cadence. Adopt the Silabs deferred-sleep pattern so the ACK actually transmits (fixing, as a side effect, an existing bug: the power-latch path's ACK is queued but never sent because power dies first). NACK a duration on power-latch hardware (physically unhonorable — power is cut, no timer exists). The uint16 ceiling (18.2 h) doubles as the safety cap; firmware clamps the floor to 10 s (wake-storm protection). Quantified payoff: **~2× battery life for hourly-fresh content** via adaptive cadence, ~40 % daily savings on overnight gaps. Transient-config-write emulation is explicitly an anti-pattern (whole-blob flash rewrite, crash leaves a persistent wrong cadence). v1 HA policy: explicit `sleep_after` hint on upload services only, never automatic, capped, and reported to the availability tracker.

**Q4 — WiFi and sleepy-WiFi devices?** (Part 4)
The firmware already ships a full WiFi LAN transport (TCP :2446, mDNS, same command frames) but **a deep-sleeping tag has WiFi off** — WiFi only starts after a BLE central connects, which inverts into a free hybrid: *rendezvous on BLE, bulk-transfer on TCP, zero firmware changes*. For true sleepy-WiFi: mDNS wake-announcement into an HA zeroconf listener (ESPHome's exact pattern) as primary rendezvous, unicast TCP probe as fallback. The deep-sleep framework is transport-agnostic at exactly one seam (`notify_device_seen(source)`); the plan enumerates every other BLE-shaped assumption and phases the work W0–W4: py-opendisplay `Transport` protocol + `TCPConnection` (independent, start anytime) → dual-transport entries keyed on the chip id (mDNS hostname `OD<chipid>` = BLE name) → WiFi presence + transport-neutral availability → firmware check-in wake mode (viability hinges on warm-associate speed; the BLE-summon hybrid is the pivot if it disappoints). Encryption identity is MAC-derived even over TCP, so WiFi-only onboarding of encrypted devices is deferred.

### Defects found during this analysis (independent of the recommendations)

| # | Defect | Where | Severity |
|---|---|---|---|
| 1 | **Config resync never rebuilds the SleepProfile** or re-registers the availability interval — a device-side cadence change leaves HA on stale timing (flapping or 36 h blind spots) | `delivery.py:320-338` vs `__init__.py:297` (PR #1) | Fix in PR #1 — reload entry when resync changes PowerConfig |
| 2 | **Power-latch 0x0052 ACK never transmits** — `sendResponse` only queues; power is cut 100 ms later, before the queue drains | `device_control.cpp:694-699`, `communication.cpp:84-102` | Firmware; fixed by the deferred-sleep pattern (Part 3) |
| 3 | **LED patterns cut off at disconnect** — `ledFlashActive` absent from `bleActive`; affects live commands today, not just queued ones | `main.cpp:169-174`, `device_control.cpp:350-361` | Firmware, ~5 lines with cap (Part 2) |
| 4 | **Silent command-queue overflow** — 5 slots, overflow drops with no NAK to the client | `esp32_ble_callbacks.h:82-90`, `main.h:348` | Latent; cheap NAK hardening |
| 5 | **Stale py-opendisplay docstring** — claims LED ACK comes after the routine finishes; firmware ACKs immediately | `device.py:1013-1014` vs `device_control.cpp:394-397` | Doc fix |
| 6 | **nRF `deep_sleep()` false success** — nRF sends no frame; the tolerant read treats timeout as success | `device_control.cpp:702-704` | Add `FF 52 00 00` on nRF + capability gating |
| 7 | **USB-powered 0x0052 silently no-ops** — skip happens inside `enterDeepSleep()` with no response; client believes the device slept | `main.cpp:276-280` | Fixed by `FF 52 03 00` (Part 3) |

### Consolidated priority list (across all four parts)

1. **Fix defect 1 in the deep-sleep PR** (HA) — small, correctness.
2. **Firmware quick wins, one PR**: LED hold-awake with cap; deferred-sleep ACK pattern (fixes defect 2); post-reboot grace; `rebootFlag` RTC persistence; queue-overflow NAK.
3. **0x0052 duration payload** (firmware + py-opendisplay + spec) with the response codes from Part 3 — enables adaptive cadence, the largest battery lever found.
4. **Config-write queueing** (HA + py-opendisplay session-clear fix) when config editing is exposed.
5. **Post-disconnect linger window** (firmware) — unlocks multi-connection wakes for second clients.
6. **`pending_ota` slot** (HA) per plan D8.
7. **WiFi W0** (py-opendisplay transport abstraction) — independent, can start anytime; W1–W3 follow the phasing in Part 4.
8. **Opt-in `queue: next_wake` for buzzer, then LED** (HA) once the firmware LED fix ships.

---

# Part 1 — Command queueing policy

## Command Queueing Policy for Deep-Sleeping Devices

This section decides, for the **entire** OpenDisplay command surface, which operations should be queued for delivery at the next wake, which should fail fast, and which deserve an opt-in hybrid. It is grounded in the implemented `DeliveryManager` (worktree `custom_components/opendisplay/delivery.py`), the py-opendisplay command surface (`py-opendisplay/src/opendisplay/device.py`, `protocol/commands.py`), the ESP32 firmware handlers (`Firmware/src/communication.cpp`, `device_control.cpp`, `buzzer_control.cpp`, `main.cpp`), and the protocol spec (`opendisplay.org/httpdocs/protocol/ble-flow.html:861-879`).

### The command surface (complete inventory)

| # | Operation | Opcode(s) | py-opendisplay | HA surface today |
|---|---|---|---|---|
| 1 | Image upload (full/fast/partial) | 0x0070/71/72/76 | `upload_image`, `upload_prepared_image` (`device.py:1231,1350`) | `upload_image`/`drawcustom` services — **queued** (latest-wins slot, `delivery.py:172-207`) |
| 2 | Config/firmware read (interrogate) | 0x0040, 0x0043 | `interrogate` (`device.py:821`), `read_firmware_version` (`device.py:884`) | Internal — **queued** as `pending_config_resync` flag (`delivery.py:209-213, 320-338`) |
| 3 | LED activate | 0x0073 | `activate_led` (`device.py:1001`) | `activate_led` service — **fails fast** (`services.py:824`, `_raise_if_sleeping` at `services.py:796-812`) |
| 4 | LED stop | 0x0075 | **not implemented** (absent from `commands.py:12-38`; firmware handler `communication.cpp:593-596`, spec `ble-flow.html:874`) | none |
| 5 | Buzzer activate | 0x0077 | `activate_buzzer` (`device.py:1055`) | `activate_buzzer` service — **fails fast** (`services.py:866`) |
| 6 | Config write | 0x0041/0x0042 | `write_config` (`device.py:1088`) | not exposed |
| 7 | Reboot | 0x000F | `reboot` (`device.py:912`) | not exposed |
| 8 | Deep sleep now | 0x0052 | **not implemented** (plan D9, `DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md:162-164`); firmware handler `device_control.cpp:691-705` | none |
| 9 | DFU trigger / OTA | 0x0051 + AppLoader flash | `trigger_dfu_bootloader` (`device.py:938`), `perform_silabs_ota` (`ota.py:131`) | Update entity — **fails fast** ("device_sleeping_ota", `update.py:170-186`) |
| 10 | Battery / MSD read | 0x0044 (MSD) | not implemented; `battery.py` is pure voltage→SoC math | Battery sensors are **advertisement-fed** (`sensor.py:60,112-113`) — no connection ever needed |
| 11 | Authenticate | 0x0050 | `authenticate` (`device.py:625`) | Session-scoped; never queueable by definition |
| 12 | Clear GATT cache | (proxy-local) | `clear_gatt_cache` (`device.py:978`) | OTA-internal; operates on the proxy, not the tag |
| 13 | Clear config | 0x0045 | not implemented (firmware `communication.cpp:404-416`) | none — recovery/provisioning only |

### Per-command analysis

#### 1. Image upload — queue (latest-wins). Already correct.

- **(a) Delay value:** High. An e-ink frame is *state*, not an event — "the panel should show X" remains true whether it lands in 5 minutes or 18 hours. This is the canonical ESL pattern (`DEEP_SLEEP_FINDINGS_2026-07-06.md:110`).
- **(b) Supersession:** Latest-wins. A newer frame fully supersedes an older one; the implementation drops the previous slot and its expiry timer on submit (`delivery.py:184-186`), mirroring live-upload cancellation (`services.py:530-536`).
- **(c) Ordering:** First in the drain (`delivery.py:294-297`) — correct. Once connected the device stays awake indefinitely (`main.cpp:85-91`), so intra-session order is about coherence, not the window. Upload must precede any queued **config write** for two reasons: the prepared frame was dithered/encoded against the config the panel *currently* has (`services.py:418-429`), and the firmware's config-write path clears the encryption session (see #6), which would break subsequent encrypted upload commands.
- **(d) Firmware risk:** Low. `epdRefreshInProgress` *is* in the sleep-hold predicate (`main.cpp:173`), so a refresh in progress keeps the device awake even if the link drops mid-refresh.
- **(e) Slot design (as implemented, keep):** `PendingUpload` dataclass (`delivery.py:67-85`); dedup = replace-on-submit; priority 1; expiry = `queue_timeout_hours` (default 24 h > 18.2 h max interval, `sleep.py:87-89`, interval bound `models/config.py:207` uint16); events `opendisplay_content_delivered` / `_expired` (`delivery.py:316,366`).

#### 2. Config resync (reads) — queue as a flag. Already correct, one gap.

- **(a)** High value delayed: the cache-based setup (plan D2) *depends* on an eventual resync; reads are only meaningful when the device is awake anyway.
- **(b)** Idempotent boolean — re-reading twice is harmless; a flag is the right dedup ("accumulate" collapses to "true").
- **(c)** After upload (cheap reads on the already-open link, `delivery.py:296-297`). If a config **write** is also queued, the resync should run *after* the write (or be synthesized from the written config) so the cache reflects the new truth.
- **(d)** None — read-only.
- **(e) Gap found:** `_drain_resync` refreshes `runtime.device_config` and the entry cache (`delivery.py:329-337`) but does **not** rebuild the `SleepProfile` or re-register the fallback availability interval. The profile is a frozen dataclass captured once at setup (`__init__.py:251,284`; registration at `__init__.py:297`) and again cached inside the manager at construction (`delivery.py:116`). If the device's own `deep_sleep_time_seconds` changed (e.g. via the web config builder, another central, or a future queued config write), HA keeps computing availability (`sleep.py:74-84`), the freshness gate (`sleep.py:91-105`) and queue expiry from the stale interval. **Recommendation:** when a resync (or config write) observes a changed `PowerConfig`, rebuild the profile and re-call `async_set_fallback_availability_interval` — simplest correct form is `hass.config_entries.async_schedule_reload(entry.entry_id)` after the drain completes, since reload re-derives everything from the fresh cache.

#### 3. LED activate — fail fast by default; **opt-in "at next wake" hybrid** (see below).

- **(a)** Near zero by default: an LED notification ("laundry done") firing 6 hours late is misinformation, worse than an error — the fail-fast rationale in `services.py:796-801` is sound. The **exception** is find-my-tag, where "flash when you next wake" is exactly the desired semantic.
- **(b)** Latest-wins: a newer pattern supersedes an older one (there is one LED runtime state machine per device; `handleLedActivate` stops any running pattern before starting the new one, `device_control.cpp:387`).
- **(c)** Must run **last among content ops** in the drain, and the drain must then *hold the connection open* for the pattern duration — see (d).
- **(d) Firmware risk — verified, and it is the crux:** the LED runtime is an asynchronous state machine ticked by `processLedFlash()` from `loop()` (`device_control.cpp:350-361`; state struct `device_control.cpp:125-150`). `handleLedActivate` ACKs **immediately** after starting the pattern (`device_control.cpp:394-397`) — note the py-opendisplay docstring claiming the firmware "responds only after the LED routine finishes" (`device.py:1013-1014`) is stale against current firmware. Critically, the sleep-hold predicate `bleActive` (`main.cpp:169-174`) tests command/response queues, connected count, advertising-restart, `epdRefreshInProgress` and the WiFi session — **not** `ledFlashActive`/`s_led.active`. So the instant the central disconnects on a battery device, `loop()` falls into the else-branch and calls `enterDeepSleep()` (`main.cpp:197-199`), cutting the pattern off (LEDs are simply unpowered by the sleep). Even today's *live* LED service on a sleepy device that happens to be awake suffers this: `_async_connect_and_run` disconnects right after the ACK (`services.py:854`). **Mitigations:** (i) HA-side — after sending 0x0073, keep the session open for the computed pattern duration (derivable from `LedFlashConfig`: flash counts × loop/inter delays × group repeats, units in `services.py:167-174`), capped at ~10 s; (ii) firmware-side (recommended, one line) — add `ledFlashActive` to the `bleActive` predicate at `main.cpp:169-174`.
- **(e) Slot design:** `PendingEffect(kind="led", instance, flash_config, hold_s, queued_at, expires_at)`; dedup latest-wins per (kind, instance); drain priority after upload/resync, before any config write; **short expiry** — default `max(deep_sleep_time_seconds, 300) + wake_window + slack` (one wake opportunity; a find-my flash two days later is a haunting, not a feature), user-overridable; events `opendisplay_effect_delivered` / `_expired`.

#### 4. LED stop (0x0075) — fail fast / local cancel. Never queue.

- **(a)** Zero: on a sleepy device the pattern is already dead the moment the device sleeps (see #3(d)) — there is nothing on the device to stop by the next wake.
- **(b)** It is a cancellation, not state: its only queued meaning is "clear the pending LED slot locally", which needs no BLE at all.
- **(e)** If py-opendisplay grows `stop_led()` (it currently lacks 0x0075 entirely — `commands.py:12-38` has no LED_STOP while firmware dispatches it at `communication.cpp:593-596`), the HA behavior on a sleeping device should be: cancel any queued LED effect slot (fire `_expired`-style cancel event), succeed without connecting; if the device is awake, send live.

#### 5. Buzzer activate — fail fast by default; same opt-in hybrid as LED, and it is *safer* than LED.

- **(a)** Same as LED: stale notification = noise pollution; find-my-tag = the one good delayed use.
- **(b)** Latest-wins per instance.
- **(c)** With LED, last among content ops. No hold-open logic needed — see (d).
- **(d) Firmware risk — verified low:** unlike the LED, buzzer playback is **fully synchronous inside the command handler**: `handleBuzzerActivate` busy-drives the tone (`buzzer_control.cpp:148-175`, tone loop `buzzer_control.cpp:73-78`) with a hard 5 s total cap (`kBuzzerMaxTotalMs = 5000`, `buzzer_control.cpp:15`) and sends the ACK only after playback (`buzzer_control.cpp:180-181`). `activate_buzzer` awaits that ACK (`device.py:1080-1084`), so the connection is inherently held through the beep and it can never be cut off by disconnect. (Side effect worth noting: playback blocks `loop()`, so it also stalls any concurrent BLE command processing for up to 5 s — schedule the buzzer as the final effect.)
- **(e)** Same `PendingEffect` slot, `kind="buzzer"`, short expiry, no hold needed.

#### 6. Config write (0x0041/42) — **queue, latest-wins**, when exposed. The strongest new-queue candidate.

- **(a)** High: like an image, a config is *desired state* — "the tag should sleep 30 min, invert its LED, join this WiFi" stays valid across any delay. Fail-fast would make sleepy tags effectively unconfigurable (their normal state is unreachable).
- **(b)** Latest-wins over the **whole config blob**: `write_config` serializes and sends a complete `GlobalConfig` (`device.py:1126-1152`; chunking `commands.py:301-360`), so partial merge semantics don't exist at the protocol level. If HA later exposes field-level edits (e.g. a `number` entity for sleep interval), merge them into one pending `GlobalConfig` at queue time, still one slot.
- **(c) Ordering — three verified constraints:**
  1. **After the image upload.** The queued frame was prepared against the pre-write config (see #1c).
  2. **Last content command of the session, or followed by an explicit re-authentication.** On save, the firmware calls `reloadConfigAfterSave()` which **clears the encryption session** and re-inits WiFi (`communication.cpp:26-37`, `clearEncryptionSession()` at `:33`; invoked from both the single-chunk and chunked paths, `communication.cpp:397-399, 443-449`). py-opendisplay keeps encrypting with the now-dead session (`_write`, `device.py:559-569`), so any subsequent command in the same connection fails 0xFE/0xFF (`device.py:605-621`). Anything after the write — a verification resync, a queued reboot, a deep-sleep terminator — must be preceded by `device.authenticate(key)` (or py-opendisplay should clear its session state after `write_config`, mirroring the firmware; library gap worth filing).
  3. **The SleepProfile dependency.** The new config takes effect immediately, no reboot needed (`reloadConfigAfterSave`, and `enterDeepSleep` reads `globalConfig.power_option.deep_sleep_time_seconds` live at `main.cpp:281,300` — the spec's "a reboot command may be sent" at `ble-flow.html:688` is optional). So a queued write that changes `deep_sleep_time_seconds` or `power_mode` changes the device's cadence **from the very next sleep**, while HA's frozen `SleepProfile` (`sleep.py:57-135`), the registered fallback availability interval (`__init__.py:297`), the manager's cached profile (`delivery.py:116`) and the queue expiry all still describe the old cadence. Concrete failure: shorten 12 h → 5 min and HA still tolerates ~36 h of silence before flagging unavailable; lengthen 5 min → 12 h and HA flaps the device unavailable every cycle. **The drain must end a config-write delivery with a profile rebuild + `async_set_fallback_availability_interval` re-registration (entry reload is the simplest correct implementation), and the cache must be rewritten from the written config.**
- **(d)** Firmware risk: moderate but ACKed per chunk (`device.py:1142-1152`), so a mid-write disconnect leaves the old config intact (the chunked buffer is only committed on the final chunk, `communication.cpp:443-450`). A *bad* config (e.g. `deep_sleep_time_seconds` = 18 h with a broken WiFi block) can make the tag nearly unreachable — recommend HA-side validation before queueing, and never auto-retrying a write the firmware NACKed (NACK = deterministic rejection, not a transport fault; distinguish from `BLEConnectionError` in `_deliver`'s error classification, `delivery.py:246-259`).
- **(e) Slot design:** `PendingConfigWrite(config: GlobalConfig, queued_at)`; dedup replace-on-submit; priority after effects (last content op); **no expiry** (desired state should persist until delivered — expiring a config change silently is worse than delivering it late; surface staleness via the pending sensor instead); events `opendisplay_config_delivered` / a `failed` event on NACK. Small enough (≤4 KB TLV, `communication.cpp:320`) to be the first slot worth persisting across HA restarts.

#### 7. Reboot (0x000F) — hybrid: allowed only as a **drain terminator**, never a standalone queued item.

- **(a)** Low as a user command (a sleepy tag effectively "reboots" its radio every wake anyway); meaningful only as "apply/recover after a config write".
- **(b)** Idempotent boolean; dedup = flag.
- **(c)** **Strictly last** — the device resets ~100 ms after receipt with no ACK and drops the link (`communication.cpp:562-566`, `device_control.cpp:97-106`, `build_reboot_command` doc `commands.py:76-85`, `device.py:912-935`). Anything ordered after it is lost. After a config write it additionally needs re-auth first (#6c-2). Mutually exclusive with the deep-sleep terminator and with OTA (which performs its own resets).
- **(d)** Post-reboot the tag does a NORMAL boot (not a deep-sleep wake), runs full setup and re-advertises (`main.cpp:53-80`) — recoverable, and the reboot-edge handling already tolerates it (D7, `__init__.py:315-328`).
- **(e)** `pending_reboot: bool` flag, priority ∞ (terminator), no expiry, drop-on-restart.

#### 8. Deep sleep now (0x0052) — never a queued slot; implement as an **automatic drain epilogue** (opt-in option).

- **(a)** Queueing "sleep" for a sleeping device is semantically void — the only time it can be delivered is when the device is awake *because HA connected to it*, i.e. at the end of a drain. That makes it a drain policy, not a command slot. Value there is real: it returns the tag to sleep immediately instead of letting it idle out the session, saving wake-window/connected time (plan Appendix C Q2, `DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md:314`).
- **(d) Hazard verified:** on hardware with a power-latch DFF configured, 0x0052 does **not** deep-sleep — it powers the device **off** (`handleDeepSleepCommand`, `device_control.cpp:694-699` → `powerLatchPowerOff()`), from which only a physical button returns it. HA must therefore gate the epilogue on knowing the board has no power latch, or firmware should split the semantics. Also requires py-opendisplay work first: 0x0052 is absent from `commands.py` (Phase 0 / D9, plan `:235`), and like reboot it must tolerate the link dropping instead of awaiting an ACK.
- **(e)** Not a slot: an options-flow boolean ("return to sleep after delivery"), executed as the final write of `_drain_once`, mutually exclusive with a queued reboot/OTA.

#### 9. OTA / DFU trigger — hybrid: fail fast today is defensible; the plan's `pending_ota` slot (D8) is the right endgame.

- **(a)** Moderate-to-high: firmware updates are the definition of "fine if late"; the current hard error (`update.py:170-186`) forces the user to babysit a wake window manually, which for a 12 h interval is hostile.
- **(b)** Latest-wins on target version; a re-install request supersedes.
- **(c)** **Terminal and exclusive**: the flow is app-mode connect → `clear_gatt_cache` → `trigger_dfu_bootloader` (no ACK, resets into AppLoader, `device.py:938-976`) → reconnect → flash (`update.py:237-270`). It ends the app-mode session and the device reboots into new firmware, so it must run **after** all other slots have drained (image + config first — they're quick, and users expect their content regardless), and a queued reboot flag is subsumed by it. The wake window only gates the *first* connect; once connected the device stays awake (`main.cpp:85-91`), so the multi-minute flash is safe — the `update.py:171-173` comment's claim that the flash "cannot be driven reliably inside a ~10 s wake window" is overly pessimistic for a wake-triggered start, though the AppLoader-reconnect step does add one more window-independent reconnection risk.
- **(d)** Failure mid-flash strands the device in the AppLoader — but the existing recovery path already handles a device found in AppLoader at the same address (`update.py:234-254`), and the AppLoader does not deep-sleep, so a stranded device is *more* reachable, not less.
- **(e)** `pending_ota(version_tag, queued_at)`; dedup latest-wins; priority: last, exclusive; expiry: none (cancel via the update entity); progress surfaced as "waiting for device wake" extra state per D8 (`DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md:160`). Until implemented, today's fail-fast with clear guidance is the correct interim.

#### 10. Battery / sensor reads — never queue; they are free.

Battery voltage and derived SoC come from every advertisement (`sensor.py:60,112-113`; `battery.py:94-123` is pure math on `advertisement.battery_mv`) — i.e. the device pushes a fresh battery reading *at every wake by construction*. Queueing a connected read would add battery cost for strictly staler-or-equal data, and plan Appendix C Q1 already leans advert-only (`DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md:313`). MSD read (0x0044) likewise duplicates the advertisement payload. **Recommendation: no slot, no service, no change.**

#### 11–13. Authenticate, clear-GATT-cache, clear-config — out of queue scope by nature.

Authentication is a per-session handshake performed inside every drain connection (`delivery.py:290-292` passes the key; firmware gate `communication.cpp:491-497`). `clear_gatt_cache` acts on the proxy (`device.py:978-998`). Clear-config (0x0045) is a provisioning/recovery command that should stay behind explicit awake-device workflows.

### The "flash LED / beep at next wake" opt-in — verdict: **worth it**, as a narrowly-scoped opt-in

**Use case fit.** Find-my-tag is real for ESLs: the tag is dark, silent, and physically lost. "It will beep the next time it checks in" is a semantic the user explicitly wants — the delay is the point of the opt-in, not a defect. With typical intervals (5–60 min, plan §2.3 guidance `:178`) latency is tolerable; at 12–18 h intervals the pending sensor makes the wait honest.

**Feasibility.** Buzzer is safe today (synchronous ACK-after-playback, 5 s cap — #5d). LED requires either the HA-side hold-open (compute pattern duration from `LedFlashConfig`, keep the drain session alive that long) or the one-line firmware fix adding `ledFlashActive` to `bleActive` (`main.cpp:169-174`). Ship buzzer-first if the firmware fix lags.

**Service API.** Extend the existing services rather than adding new ones:

```yaml
service: opendisplay.activate_buzzer   # and activate_led
data:
  device_id: abc123
  queue: next_wake        # "never" (default, today's fail-fast) | "next_wake"
  expire_after: 3600      # optional; default = one sleep interval + wake window + slack
  frequency_hz: 2000
  duration_ms: 500
  repeats: 3
```

Response (`SupportsResponse.OPTIONAL`, same shape as uploads, `services.py:487-494`): `{"status": "delivered" | "queued", "expires_at": …}`. Default `queue: never` preserves the current contract — automations that today rely on the `device_sleeping` error keep working; a find-my dashboard button sets `queue: next_wake`. Delivery/expiry fire `opendisplay_effect_delivered` / `opendisplay_effect_expired` so a script can chase the beep with a phone notification ("tag just beeped — go listen"). Drain placement: after upload/resync, before config write (avoids the post-write re-auth, #6c-2), buzzer last (it blocks the device loop, #5d).

### Typed slots vs. a generic "queued command" list — verdict: **typed slots**, with a derived diagnostics view

The current design (one field per work type, `delivery.py:119-121`) should be kept and extended, not generalized into a `list[QueuedCommand]`:

1. **Dedup rules are per-type semantics, not per-item metadata.** Image = replace whole slot (`delivery.py:184-186`); resync = idempotent flag; config write = replace (possibly merge fields); effects = replace per (kind, instance); reboot = flag; OTA = replace version. A generic list must encode a dedup key + supersession policy per entry — at which point it has reinvented types with worse static guarantees.
2. **Drain priority is a static total order over types** (upload → resync → effects → config write → OTA/reboot terminator), which typed slots express as literally the statement order in `_drain_once` (`delivery.py:294-297` today). A generic list needs a priority field plus invariants ("at most one terminator", "config write invalidates a queued frame's prepared bytes") enforced dynamically.
3. **Cross-slot interlocks are the hard part** — config write ⇒ re-auth before later commands ⇒ profile rebuild after drain; OTA subsumes reboot; effect hold-open extends the session. These are relationships *between* types; a homogeneous list gives them nowhere natural to live.
4. **Diagnostics** don't need a generic store: `DeliverySnapshot` (`delivery.py:96-105`) extends per-slot (kind, queued_at, expires_at, attempts, last_error), and `diagnostics.py` can render the union as a list *view*. Users get "what's pending and why" without the manager storing an unbounded, payload-retaining log (a `PendingUpload.prepared` is hundreds of KB — a list invites accidental accumulation; latest-wins slots bound memory to one frame by construction).
5. **Restart persistence (v2) is type-selective:** config writes are small, declarative, and safe to persist; a prepared image is large and re-derivable by the caller; queued effects must **not** survive a restart (a surprise 2 a.m. beep from a queue restored hours later is a bug report). Typed slots make "persist these two fields, drop those" a one-line decision per type; a generic list forces a uniform policy or per-item flags.

The one thing a generic list does better — arbitrary user-scripted command sequences — is not a real requirement here: the protocol surface is small, closed, and fully enumerable (13 rows above).

### Summary table

| Command | Today's behavior | Recommended | Drain priority | Dedup rule | Rationale |
|---|---|---|---|---|---|
| Image upload (0x0070-72/76) | Queued, latest-wins (`delivery.py:172-207`) | Keep | 1 | Replace slot | Desired state; delay-tolerant by nature; refresh is sleep-held by firmware (`main.cpp:173`) |
| Config resync (0x0040/43 reads) | Queued flag (`delivery.py:209-213`) | Keep + rebuild SleepProfile/availability on changed PowerConfig | 2 | Idempotent flag | Read-only, free on an open link; profile-rebuild gap at `delivery.py:320-338` must be closed |
| LED activate (0x0073) | Fail fast (`services.py:824`) | Hybrid: default fail fast; opt-in `queue: next_wake` + hold connection for pattern (or firmware adds `ledFlashActive` to `main.cpp:169-174`) | 3 (with 4) | Replace per (led, instance) | Late notification is misinformation, except find-my-tag; effect is cut at disconnect today |
| Buzzer activate (0x0077) | Fail fast (`services.py:866`) | Hybrid: same opt-in; inherently safe (ACK after playback, 5 s cap, `buzzer_control.cpp:15,180-181`) | 4 (last effect) | Replace per instance | Same as LED but no cut-off risk; blocks device loop during playback |
| LED stop (0x0075) | Not exposed (missing from py-opendisplay) | Fail fast / local cancel of queued LED slot; never queue | n/a | n/a | Sleep already kills the pattern; queued "stop" is meaningless |
| Config write (0x0041/42) | Not exposed | Queue, latest-wins, no expiry; re-auth after write; end drain with profile rebuild + availability re-registration | 5 (last content op) | Replace whole GlobalConfig | Desired state; firmware clears encryption session on save (`communication.cpp:33`) and applies power config to the very next sleep (`main.cpp:281,300`) |
| Reboot (0x000F) | Not exposed | Hybrid: only as drain terminator flag; never standalone-queued | ∞ (terminator) | Flag | No ACK, drops link (`communication.cpp:562-566`); anything after it is lost |
| Deep sleep (0x0052) | Not implemented in py-opendisplay | Not a slot: opt-in drain epilogue ("sleep after delivery"); gate on no power latch (`device_control.cpp:694-699` powers OFF latched boards) | ∞ (epilogue) | n/a | Only deliverable when HA itself holds the device awake; battery win per plan Q2 |
| OTA / DFU (0x0051 + flash) | Fail fast (`update.py:170-186`) | Hybrid: `pending_ota` slot (plan D8), "waiting for wake" state | Last, exclusive | Replace target version | Delay-tolerant by definition; terminal for the session; AppLoader doesn't sleep so failure is recoverable |
| Battery / MSD read (0x0044) | Advertisement-fed (`sensor.py:60`) | No change — never connect for it | n/a | n/a | Every wake pushes fresh battery data for free |
| Authenticate (0x0050) | Per-connection | Unchanged — session-scoped | Implicit, first | n/a | Handshake, not work |
| Clear GATT cache | OTA-internal (`update.py:247`) | Unchanged | OTA-internal | n/a | Proxy-local operation |
| Clear config (0x0045) | Not exposed | Fail fast if ever exposed; awake-device recovery workflow only | n/a | n/a | Destructive provisioning command; must be deliberate and live |

---

# Part 2 — Firmware sleep mechanics for multi-command wakes

## Firmware Sleep Mechanics — Multi-Command Wake Analysis

### Framing: multiple commands vs. multiple connections

The distinction matters and the code makes it sharp:

- **Multiple commands per connection are already unlimited.** Once a central connects during the wake window, `loop()` leaves the window branch permanently (`Firmware/src/main.cpp:85-91` — `advertising_timeout_active = false`, `fullSetupAfterConnection()`), and the sleep decision moves to the `bleActive` predicate (`main.cpp:169-174`), which includes `pServer->getConnectedCount() > 0`. The device stays awake for the entire session with no idle timer. The HA integration exploits this exactly as intended: `DeliveryManager._drain_once()` opens one `OpenDisplayDevice` session and drains every pending slot in priority order over it (`Home_Assistant_Integration/.claude/worktrees/agent-a7d4325aeb2a1546b/custom_components/opendisplay/delivery.py:265-297`, deadline 30 s at `delivery.py:61`).
- **Multiple connections per wake are effectively impossible today.** The post-disconnect awake time is well under one second (traced below). Any client that wants a second connection in the same wake — or a *second* client (phone app alongside HA) — loses.

So the honest headline: **for the HA-only, single-client case, no firmware change is required to process multiple commands per wake.** The gaps are all multi-connection / multi-client / effect-completion gaps, plus one real bug (LED cutoff).

### Verified: how fast the device sleeps after disconnect

Trace, starting from `MyBLEServerCallbacks::onDisconnect` (`Firmware/src/esp32_ble_callbacks.h:46-56`), which sets `bleRestartAdvertisingPending = true`:

1. Next `loop()` pass: any still-queued commands are processed one per iteration (`main.cpp:107-113`); with no connection and notify disabled, queued responses are **dropped** (`main.cpp:128-132`) — `bleActive` stays true only until the command queue drains.
2. Once queues are empty, `bleRestartAdvertisingPending` triggers `esp32_restart_ble_advertising()` (`main.cpp:135-137` → `ble_init.cpp:165-183`): `delay(100)`, `BLEDevice::startAdvertising()`, `updatemsdata()` (which itself stops/restarts advertising with a `delay(50)`, `display_service.cpp:1343-1355`), and clears the pending flag.
3. **The same loop iteration** then computes `bleActive` (`main.cpp:169-174`): queues empty, connected count 0, restart flag just cleared, no refresh → false → `enterDeepSleep()` (`main.cpp:197-198`).
4. `enterDeepSleep()` (`main.cpp:275-306`) stops advertising, `delay(200)`, `BLEDevice::deinit`, `delay(100)`, `delay(100)`, sleeps.

Net: the post-disconnect advertising burst lasts roughly **150-400 ms** and total awake time after disconnect is **< 1 s**. (Exception: if `epdRefreshInProgress`, the restart is deferred, `ble_init.cpp:174-176`, and `bleActive` holds the device awake until the refresh clears at `display_service.cpp:1747`/`2043`.) There is no re-armed window: `advertising_start_time` is set exactly once per wake in `minimalSetup()` (`main.cpp:253`) and never extended.

---

### Candidate 1 — Post-disconnect linger window (2-5 s) — RECOMMENDED

**Feasibility: high, ~15 lines.** The natural seam is the sleep-entry check at `main.cpp:197`. On disconnect (or, better, when `esp32_restart_ble_advertising()` actually restarts advertising, `ble_init.cpp:178-182`), record `lingerUntil = millis() + LINGER_MS`; in `loop()`'s idle branch, skip `enterDeepSleep()` while `(int32_t)(millis() - lingerUntil) < 0`, substituting a short `idleDelay(50)`. Reusing the existing `advertising_timeout_active` machinery is tempting but wrong — that branch is gated on `woke_from_deep_sleep` (`main.cpp:85`) and re-runs `fullSetupAfterConnection()` (redundant `initWiFi`); a separate deadline variable is cleaner. A reconnect during the linger flips `bleActive` true via connected count, and the next disconnect re-arms the linger — giving unlimited connect cycles per wake, each paying only the linger.

**Config surface:** `PowerOption` has `uint8_t sleep_flags` (bitfield, `Firmware/src/structs.h:49`, already parsed by py-opendisplay at `models/config.py:200,249`) and `uint8_t reserved[7]` (`structs.h:60`) — one reserved byte as `linger_time` in 100 ms units (0 = firmware default, e.g. 3 s; a `sleep_flags` bit to disable entirely) is fully backward compatible: old configs carry zeros.

**Battery:** the linger only costs on wakes where a connection *happened* (idle wakes never reach a disconnect). 3 s × ~50 mA active ≈ **0.04 mAh per delivery session** — noise next to the session itself (a 10 s upload+refresh ≈ 0.15-0.5 mAh) and next to the unavoidable 10 s advertising window (~0.13 mAh per wake, ~36 mAh/day at 5-min cadence). Order of magnitude: < 3% overhead on connected wakes, 0% on idle wakes.

**Risk: low.** One subtlety: HA's coordinator sees the linger-burst advertisements; with the pending-work guard in `notify_device_seen` (`delivery.py:223-231`) HA won't reconnect without a reason, so no ping-pong. The known `rebootFlag` churn interaction (findings doc) actually *improves*: the linger burst is longer, so HA reliably observes the flag-cleared advert, but the underlying fix is still the RTC persistence one.

**What it actually buys:** (a) a second client (phone app) that lost the connection race can follow HA in the same wake; (b) work submitted to `DeliveryManager` seconds after `_drain_once` started (the slot snapshot is taken at `delivery.py:282` — later `submit_upload` calls miss the in-flight drain and today wait a full sleep interval); (c) client-side retry after a flaky GATT session without waiting one interval.

### Candidate 2 — Activity-extended advertising window (scan-request / connect-attempt reset) — REJECT

**Not observable at the application layer with this stack.** The firmware uses the stock arduino-esp32 Bluedroid BLE library (`platformio.ini:5-8` pulls only `bb_epaper`; the `CONFIG_NIMBLE_ENABLED` branches in `ble_init.cpp:157-162` / `esp32_ble_callbacks.h:61-67` are compile-time provisions, not the active config). Bluedroid does not surface SCAN_REQ events to the Arduino API at all. Even under NimBLE, `BLE_GAP_EVENT_SCAN_REQ_RCVD` requires *extended* advertising with scan-request notification enabled — the firmware advertises legacy ADV_IND with scan response explicitly disabled (`display_service.cpp:1352`). Connect *attempts* are only visible as `onConnect`, at which point the window logic is already moot (`main.cpp:86-91`).

Even if observable, it is a bad heuristic: any passive-scanning phone or a second HA instance's active scanner would extend the window on every wake, turning a 10 s window into an unbounded one — the worst possible battery failure mode (every wake, not just connected ones). Candidate 1 delivers the same benefit gated on an actual connection.

### Candidate 3 — "Stay awake / more work pending" protocol handshake — DEFER

**Wire sketch (if ever needed):** a new command, e.g. `0x0053 HOLD_AWAKE`, payload `uint16 seconds` (0 = cancel), response `{0x00, 0x53, 0x00, 0x00}`, dispatched from the command handler in `device_control.cpp` alongside `handleDeepSleepCommand` (`device_control.cpp:691`). Firmware: a `holdAwakeUntil` global; add `(int32_t)(millis() - holdAwakeUntil) < 0` to `bleActive` (`main.cpp:169-174`); clamp to a hard cap (e.g. 300 s) so a buggy client can't flatten a battery. ~25 lines firmware + a py-opendisplay command.

**vs. the reserved `connection_requested` advertisement bit:** these are opposite directions and not substitutes. Bit 2 of the status byte is device→client (`display_service.cpp:1296`, `main.h:127`; parsed as `connection_requested` at `py-opendisplay/src/opendisplay/models/advertisement.py:385`). It cannot carry a client's intent. Its sensible *complementary* use: the firmware sets it in `updatemsdata()` during a linger window or commanded hold, so scanners can distinguish "session window open, reconnects welcome" from a normal wake — cheap (1 line per state transition) and wire-compatible, since the bit is documented reserved and py-opendisplay already parses it.

**Why defer:** the one real client (HA) already batches everything into one connection (P3 in the implementation plan, `delivery.py:9-16`), and Candidate 1 covers follow-up connections without any protocol change or client coordination. A hold-awake command only earns its keep for orchestrated multi-connection flows (config-apply → reboot → verify; multi-client scheduling), none of which exist yet. Building protocol surface ahead of a consumer is how reserved bits become liabilities.

### Candidate 4 — LED-flash / buzzer completion before sleep — RECOMMENDED (LED only; buzzer needs nothing)

**Buzzer: already safe.** `handleBuzzerActivate` is fully synchronous — it plays the entire pattern (hard-capped at 5 s, `buzzer_control.cpp:15,148-178`) inside the command handler, which runs from the command-queue drain in `loop()` (`main.cpp:107-113`) *before* the `bleActive` check. Sleep cannot preempt it.

**LED: genuinely broken.** The LED pattern is an async state machine ticked by `processLedFlash()` (`device_control.cpp:350-361`), driven by `s_led.active`/`ledFlashActive` (`device_control.cpp:388-394`, defined at `main.h:137`) — and **none of that state is in `bleActive`** (`main.cpp:169-174`). Sequence: client sends LED activate, gets its response, disconnects → device sleeps < 1 s later → deep sleep tri-states the GPIOs mid-pattern. The wake-window branch does call `processLedFlash()` (`main.cpp:83`) but the window's hard deadline cuts it off identically.

**Fix (~5 lines):** add `ledFlashActive` to the `bleActive` OR-chain — **with a clamp**, because patterns can be infinite: `grouprepeats == 255` (encoded `ledcfg[10] = 254`) repeats forever (`device_control.cpp:210,245`), and even finite patterns can run minutes (inter-loop delays up to 255 × 100 ms per stage × up to 254 group repeats, `device_control.cpp:108,260`). Recommended predicate: `ledFlashActive && (millis() - ledFlashStartMs < LED_HOLD_AWAKE_CAP_MS)` with a cap of ~15-30 s, recording `ledFlashStartMs` in `handleLedActivate` (`device_control.cpp:388`). Infinite patterns get truncated at the cap — correct, since an infinite pattern on a deep-sleep battery device is a misconfiguration.

**Battery:** bounded by pattern length: a typical 5-10 s notification flash ≈ 0.1-0.15 mAh per activation, only when explicitly commanded. **Backward compatibility:** none affected; purely extends awake time. **Risk: low** with the clamp; without it, a single infinite-LED command bricks the battery.

### Candidate 5 — Other limiters found in the code

- **Command queue: 5 slots × 512 B, silent drop on overflow.** `COMMAND_QUEUE_SIZE 5` (`main.h:348`, mirrored in `esp32_ble_callbacks.h:14`); a full queue logs "dropping command" and sends **no error to the client** (`esp32_ble_callbacks.h:82-90`). `loop()` drains one command per iteration (`main.cpp:107-113`), and long-blocking handlers (buzzer up to 5 s; full EPD refresh tens of seconds) stall the drain while `onWrite` (BLE stack task) keeps filling slots. In practice py-opendisplay's request/response pacing keeps ≤ 1-2 in flight, so this is latent, not active — but any client that pipelines writes will hit it. Cheap hardening: on overflow, push a NAK onto the response queue so the loss is visible. Not a battery issue.
- **Response queue drops on disconnect** (`main.cpp:128-132`): responses to commands processed after the central left are discarded — combined with the < 1 s sleep, a client can never reconnect to collect them. The linger window (Candidate 1) makes this recoverable in principle, though clients treat responses as connection-scoped anyway.
- **`sleep_timeout_ms` is a fixed deadline, not idle-based** (`main.cpp:93-98`, start stamped once at `main.cpp:253`) — confirmed; by design, and fine once Candidate 1 exists.
- **Commanded reboot → ~1 s to sleep.** `deep_sleep_count` is `RTC_DATA_ATTR` (`main.h:295`) and survives `esp_restart()`, so the first-boot grace (`main.cpp:180-196`, requires `deep_sleep_count == 0`) is skipped after a reboot command — no window for the reconnect-and-verify half of a config-apply flow. The findings doc's post-reboot grace window (30-60 s after any non-deep-sleep boot) is the fix; it is also the *real* answer to "OTA needing reconnect cycles" concerns on this target — noting that BLE OTA does not exist on ESP32 firmware anyway ("OTA typically handled via WiFi", `device_control.cpp:685`); the AppLoader reconnect dance is Silabs-only (`update.py:42-49,171`), and Silabs devices don't run this sleep loop (command-driven EM4, no timer wake).
- **Per-connection state resets are benign:** `onConnect` clears `rebootFlag` and the notify-subscribed flag (`esp32_ble_callbacks.h:42-44`); responses queued before the client re-enables CCCD are held, not dropped (`main.cpp:126-127`). No multi-command impact.

---

### Recommendation — minimal prioritized set

| # | Change | Files / functions | Config surface | Back-compat | Battery cost |
|---|--------|-------------------|----------------|-------------|--------------|
| 1 | **LED-flash hold-awake with cap** — add clamped `ledFlashActive` term to `bleActive` | `main.cpp:169-174`; `device_control.cpp:388` (timestamp); `main.h:137` | Hardcoded 15-30 s cap | Fully additive | Pattern-bounded, ~0.1 mAh per commanded flash |
| 2 | **Post-disconnect linger window (~3 s)** — deadline armed in `esp32_restart_ble_advertising()`, checked before `enterDeepSleep()` | `ble_init.cpp:178-183`; `main.cpp:197` | `power_option.reserved[0]` as linger ×100 ms (0 = default 3 s) + a `sleep_flags` disable bit | Old configs → default; old clients unaffected | ~0.04 mAh per *connected* wake only; 0 on idle wakes; < 3% overhead |
| 3 | **Post-reboot grace window (30-60 s)** — extend the first-boot grace to any non-deep-sleep boot | `main.cpp:180-196` condition | Hardcoded | Additive | ~0.5 mAh per commanded reboot (rare) |
| 4 | **`rebootFlag` RTC persistence** (from findings doc — battery, not multi-command, but kills the churn loop that dwarfs all costs above) | `main.h:126`; `enterDeepSleep()`; `setup()` | None | Additive | Saves ~0.5-2 mAh per avoided churn cycle |
| — | Hold-awake command / `connection_requested` signaling | — | — | — | **Defer — no consumer yet** |
| — | Activity-extended advertising | — | — | — | **Reject — not observable (Bluedroid legacy adv), bad heuristic** |

**Bottom line:** multiple *commands* per wake need nothing — the connected-session model plus HA's single-connection drain (`delivery.py`) already handles unbounded work per wake. The defensible firmware changes are the LED completion fix (a real user-visible bug) and a cheap, connection-gated linger window that unlocks multi-*connection* wakes (second clients, post-drain submissions, retry-after-flaky-session) for ~0.04 mAh per delivery. Everything protocol-shaped (hold-awake command, bit 2 signaling) should wait for a concrete second consumer.

---

# Part 3 — A one-shot "sleep for X seconds" command

## Feasibility: a one-shot "sleep for X seconds" command

### Ground truth: how sleep duration is decided today

The ESP32 firmware has exactly one source of sleep duration: the persistent config field `power_option.deep_sleep_time_seconds` (uint16, part of the 30-byte power TLV block — `py-opendisplay/src/opendisplay/models/config.py:207,256`). `enterDeepSleep()` reads it at sleep entry and arms the wake timer with it (`Firmware/src/main.cpp:300-301`):

```cpp
uint64_t sleep_timeout_us = (uint64_t)globalConfig.power_option.deep_sleep_time_seconds * 1000000ULL;
esp_sleep_enable_timer_wakeup(sleep_timeout_us);
```

`enterDeepSleep()` is reached from three call sites, all in the same boot:
1. Advertising-window timeout after a wake (`main.cpp:98-102`),
2. Idle loop when BLE goes quiet (`main.cpp:197-198`),
3. The `0x0052` "deep sleep now" handler (`Firmware/src/device_control.cpp:701`).

Guards at entry: `power_mode != 1` → skip (`main.cpp:276-280`); `deep_sleep_time_seconds == 0` → skip (`main.cpp:281-285`). Timer wake is the **only** wake source armed — no `ext1` button wake (`main.cpp:300-305`), so whatever duration is armed is also the worst-case unreachability window.

Facts that shape the design:

- **The timer is armed at sleep entry, in the same boot that received the command.** A one-shot override therefore needs only a plain (zero-initialized) global — **not** `RTC_DATA_ATTR`. On wake the global re-initializes to 0 and the device naturally reverts to its configured cadence, which is exactly the desired one-shot semantics. RTC memory (`main.h:284-295`, `woke_from_deep_sleep` / `deep_sleep_count` / `displayed_etag`) is only needed for state that must survive *across* the sleep; the override must deliberately *not* survive it.
- **Command framing convention:** 2-byte big-endian opcode + payload (`opendisplay.org/httpdocs/protocol/ble-flow.html:859`); multi-byte payload integers are little-endian (e.g. the config-write `total_size` is parsed LE at `Firmware/src/communication.cpp:377`). The dispatcher already passes `data + 2, len - 2` to payload-carrying handlers (`communication.cpp:552,577,591`); `0x0052` is currently dispatched with **no arguments** (`communication.cpp:605-607`), so any payload sent to today's firmware is silently ignored — an important backward-compat property.
- **Response conventions:** ACK `{0x00, cmd_low, 0x00, 0x00}`, error `{0xFF, cmd_low, [error_code], 0x00}` (`ble-flow.html:886-887`; e.g. LED errors at `Firmware_Silabs/opendisplay_pipe.c:1049-1050`). py-opendisplay's `deep_sleep()` already treats `0xFF52` as "not supported" and raises `ProtocolError` (py-opendisplay branch `feat/deep-sleep-command`, commit `f78908c`, `device.py` diff).
- **An existing ACK bug worth knowing about:** on ESP32, `sendResponse()` only *queues* the notification (`communication.cpp:84-102`), and the queue is drained on a later `loop()` pass (`main.cpp:114-125`) — *after* command processing (`main.cpp:107-113`). The power-latch path of `handleDeepSleepCommand()` queues `{00 52 00 00}` then hard-powers-off 100 ms later (`device_control.cpp:694-699`), so that ACK almost certainly never transmits over BLE. py-opendisplay's fire-and-forget `deep_sleep()` (disconnect == success) already tolerates this, but it means **any design that wants a delivered ACK before sleeping must defer sleep entry to a later loop pass** (the pattern Silabs already uses — see below).
- **Silabs:** `CMD_DEEP_SLEEP` ACKs `{0x00, RESP_DEEP_SLEEP}` and sets `s_pending_deep_sleep` (`Firmware_Silabs/opendisplay_pipe.c:1040-1046`); the main loop then closes the connection and enters **EM4 with button/NFC wake only** — no timer/BURTC wake is armed (`Firmware_Silabs/opendisplay_ble.c:1910-1927`). `deep_sleep_time_seconds` is unused on this target; a duration cannot currently be honored. The handler also ignores `payload_len` entirely, so extra payload bytes are silently accepted.
- **nRF:** `handleDeepSleepCommand()` only logs "not supported" and sends **no frame at all** (`device_control.cpp:702-704`). (Side note: py-opendisplay's `deep_sleep()` treats the resulting read timeout as success — a mild misreport on nRF that a duration-aware client should gate on firmware/target instead.)

### Approach A — extend 0x0052 with an optional duration payload

**Wire format fit.** `0x0052` + optional `uint16 LE` seconds slots perfectly into existing conventions: opcode BE, payload LE, empty payload legal (the spec already has empty-payload commands, e.g. `0x0070` uncompressed start, `ble-flow.html:706`). The encrypted path is unaffected — payload rides inside the AES-CCM envelope like any other command payload (`communication.cpp:499-540`).

**Backward compatibility — both directions are clean:**
- *Old firmware + new payload:* the dispatcher calls `handleDeepSleepCommand()` without passing the payload (`communication.cpp:605-607`), so an old ESP32 sleeps at its configured cadence. Degradation is benign (device still sleeps and wakes normally) but **undetectable in-band** on ESP32-without-latch, because that path sends no ACK either way — the client must gate on firmware version (py-opendisplay already reads it, `device.py:895-909`). Old Silabs ACKs and ignores the extra bytes (`opendisplay_pipe.c:1040-1046`).
- *New firmware + no payload:* `len - 2 == 0` → exact current behavior. No client ever breaks.

**Firmware changes (small, 3 files):** pass the payload through, parse an optional uint16, stash it in a plain global consumed by `enterDeepSleep()`. `RTC_DATA_ATTR` is **not** needed (verified above: the timer is armed before `esp_deep_sleep_start()` at `main.cpp:301-305`, and reverting to configured cadence on the next wake is the intended one-shot behavior).

**Power-latch path — a duration is physically meaningless there.** `powerLatchDffConfigured()` devices release a 74AHC1G79 D-FF that cuts their own power (`device_control.cpp:694-699`, `Firmware/src/power_latch.h:5,23`). With power cut, there is no RTC domain and no timer — the device wakes only via the external latch-set circuit (button). A commanded duration cannot be honored, and silently discarding it would leave the client believing the device will return in X seconds when it may be off for days. The firmware must **NACK a duration on latch hardware** (`0xFF 52 02 00`) and stay awake, letting the client fall back to bare `0x0052` deliberately.

### Approach B — a new "set next sleep duration" command (e.g. 0x0053, unused per `ble-flow.html:861-878`)

Arms the same one-shot override but does **not** sleep; the device sleeps on its own when BLE goes idle (`main.cpp:169-198`) or at the next advertising timeout. Merits: the ACK is *reliably delivered* (the connection stays up, so the queued response drains normally at `main.cpp:114-125`), giving in-band confirmation that the duration was applied — old firmware would hit the `default:` case which logs and sends *nothing* (`communication.cpp:608-611`), so the client detects non-support via ACK timeout. It also composes naturally with the HA flow (drain work → arm duration → disconnect → device sleeps itself).

Drawbacks: a new opcode across four artifacts (two firmware repos, py-opendisplay, protocol spec) for what is semantically a parameter of an existing command; two commands whose interaction needs specification ("what does bare 0x0052 do after 0x0053 armed?" — answer must be "consumes the override", one more rule to document); and the "sleep now for X" case then requires two round trips. The decoupled semantics ("armed but not sleeping yet") also creates a dangling-override state if the client crashes mid-session — harmless (same-boot only) but another edge case.

### Approach C — transient config write (rejected)

Write `deep_sleep_time_seconds` via `0x0041`, restore later. This is structurally awful, for reasons visible in the code:

1. **Write granularity is the whole config blob, persisted to flash every time.** `handleWriteConfig` → `saveConfig(data, len)` deletes and rewrites the entire LittleFS config file (`communication.cpp:395-401`, `Firmware/src/config_parser.cpp:80-125`). There is no field-level write. Real configs (with display blocks) exceed 200 bytes, so each "transient" change is a multi-chunk transfer (`communication.cpp:371-393,418-458`) — meaningful airtime inside a 10 s wake window, twice per cycle (set + restore).
2. **Flash wear.** Hourly adaptive cadence = ~17,500 full-file erase/write cycles per year. LittleFS wear-levels, but the config partition is small and this is gratuitous wear for a value that only needs to live until the next `esp_sleep_enable_timer_wakeup()` call.
3. **Crash-restore hazard — the deal-breaker.** The override is *persistent*: if HA restarts, loses BLE, or the entry unloads between "set 60 s" and "restore 21600 s", the tag is permanently reconfigured to wake every 60 s, silently draining its battery at ~24× the intended rate. The HA queue is explicitly memory-only in v1 (`DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md:152`), so the restore obligation would not even survive a restart.
4. **Race with user config.** The restore must write back a *full* cached blob, clobbering any config change the user made via the toolbox in between — a classic lost-update race with no version check in the protocol.

### Recommendation: Approach A, with the Silabs-style deferred-sleep ACK

Extend `0x0052` with an optional `uint16` payload, and adopt the `s_pending_deep_sleep` pattern from Silabs (`opendisplay_pipe.c:1040-1046` + `opendisplay_ble.c:1910-1927`) on ESP32 so the ACK actually transmits before sleep entry. This gets B's reliable-ACK property inside A's single opcode, and fixes the pre-existing latch-path ACK bug as a side effect. The minimal variant (fire-and-forget, no deferral) also works — py-opendisplay's `deep_sleep()` already tolerates it — but then duration support is only discoverable via firmware version.

**uint16, not uint32, is a feature:** it matches the config field width (`models/config.py:207`), and its 18.2 h ceiling *is* the reachability-risk cap enforced by the wire format itself (see risk analysis below).

#### Wire format

| Frame | Meaning |
|---|---|
| `00 52` (no payload) | Sleep now, configured `deep_sleep_time_seconds` — unchanged |
| `00 52 XX XX` (uint16 LE, seconds) | Sleep now; wake timer armed for X seconds, this cycle only |

Payload validation: `X == 0` → error (would arm a zero timer / bypass the `== 0` disable guard at `main.cpp:281`); firmware clamps `0 < X < 10` up to 10 s (thrash protection). Any payload length other than 0 or 2 → error.

#### Responses

| Frame | Meaning |
|---|---|
| `00 52 00 00` | ACK — sleep (with override if payload present) accepted; link closes next |
| `FF 52 01 00` | Invalid payload (bad length, or X == 0) |
| `FF 52 02 00` | Duration not honorable: power-latch hardware (power will be cut; no timer exists) — device stays awake |
| `FF 52 03 00` | Sleep unavailable: `power_mode != 1` (mains-powered) — device stays awake |
| `FF 52 00 00` / no frame | Existing "not supported" signalling (nRF sends nothing today, `device_control.cpp:702-704`) |

(`FF 52 03 00` also fixes an existing silent quirk: today a bare `0x0052` on a USB-powered board no-ops inside `enterDeepSleep()` at `main.cpp:276-280` with no response, and the fire-and-forget client wrongly concludes the device slept.)

#### Firmware pseudocode diff (ESP32)

```cpp
// main.cpp — near the RTC declarations (main.h:292-295 context), PLAIN global:
// zero on every boot => one-shot by construction; RTC_DATA_ATTR would wrongly
// persist the override across the sleep it configures.
uint32_t sleep_duration_override_s = 0;   // 0 = use config
bool     deep_sleep_pending = false;      // deferred-ACK entry flag

// communication.cpp:605 — pass the payload through:
case 0x0052:
    handleDeepSleepCommand(data + 2, len - 2);   // was: handleDeepSleepCommand()
    break;

// device_control.cpp:691 — parse, validate, defer:
void handleDeepSleepCommand(uint8_t* payload, uint16_t len) {
#ifdef TARGET_ESP32
    uint32_t override_s = 0;
    if (len == 2)      override_s = payload[0] | (payload[1] << 8);   // LE, like 0x41 total_size
    else if (len != 0) { NACK(0xFF,0x52,0x01); return; }
    if (len == 2 && override_s == 0) { NACK(0xFF,0x52,0x01); return; }
    if (override_s && powerLatchDffConfigured()) { NACK(0xFF,0x52,0x02); return; }  // no timer exists after power-off
    if (globalConfig.power_option.power_mode != 1 && !powerLatchDffConfigured())
                       { NACK(0xFF,0x52,0x03); return; }
    if (override_s < 10) override_s = override_s ? 10 : 0;            // clamp floor
    sleep_duration_override_s = override_s;
    ACK(0x00,0x52);                       // queued; drained by loop() before sleep
    deep_sleep_pending = true;            // Silabs s_pending_deep_sleep pattern
#else
    /* nRF: */ NACK(0xFF,0x52,0x00);      // explicit not-supported (new)
#endif
}

// main.cpp loop() — after the response-queue drain (main.cpp:114-134):
if (deep_sleep_pending && responseQueueTail == responseQueueHead) {
    deep_sleep_pending = false;
    if (powerLatchDffConfigured()) { delay(50); powerLatchPowerOff(); }
    else                           enterDeepSleep();
}

// main.cpp:275 enterDeepSleep() — consume the override:
uint32_t effective_s = sleep_duration_override_s
                     ? sleep_duration_override_s
                     : globalConfig.power_option.deep_sleep_time_seconds;
sleep_duration_override_s = 0;                       // defensive; boot re-zeroes anyway
if (effective_s == 0) { /* existing skip, main.cpp:281 */ }
...
esp_sleep_enable_timer_wakeup((uint64_t)effective_s * 1000000ULL);   // main.cpp:300-301
```

Functions touched: `communication.cpp` dispatch (1 line), `handleDeepSleepCommand` (rewritten, ~25 lines), `enterDeepSleep` (~4 lines), `loop()` (~6 lines), `device_control.h` prototype. The override deliberately bypasses only the `deep_sleep_time_seconds == 0` guard (a Silabs-like command-driven-sleep ESP32 config becomes possible) while `power_mode` is enforced at command level with an explicit error instead of a silent skip.

#### py-opendisplay API

```python
# protocol/commands.py
def build_deep_sleep_command(duration_s: int | None = None) -> bytes:
    """0x0052; with duration_s appends uint16 LE one-shot override (10–65535 s).

    Honored on ESP32 firmware >= <version that ships this>; older ESP32 sleeps at its
    configured cadence; Silabs ACKs and ignores (EM4, no timer); nRF unsupported."""
    cmd = CommandCode.DEEP_SLEEP.to_bytes(2, "big")
    if duration_s is None:
        return cmd
    if not 10 <= duration_s <= 0xFFFF:
        raise ValueError(f"duration_s out of range: {duration_s} (10-65535)")
    return cmd + struct.pack("<H", duration_s)

# device.py — extend the existing deep_sleep() (branch feat/deep-sleep-command, commit f78908c)
async def deep_sleep(self, duration_s: int | None = None) -> None:
    ...  # same tolerant read as today, PLUS:
    # 0xFF52 status 0x01/0x02/0x03 -> raise ProtocolError with the specific reason
    # (device stayed awake for 0x02/0x03 — caller may retry bare deep_sleep()).
```

Capability gating: `duration_honored = target is ESP32 and fw >= (major, minor)` using the already-cached firmware version (`device.py:895-909`); when False and `duration_s` is set, log a warning and either strip the payload or raise, per a `strict` flag. CLI: `opendisplay sleep <device> [--for SECONDS]`.

#### Silabs / nRF behavior

- **Silabs:** no change required for graceful behavior — `opendisplay_pipe.c:1040-1046` ACKs and ignores payload; the device enters EM4 with button/NFC wake (`opendisplay_ble.c:1921-1927`). Recommended one-line hardening: if `payload_len == 2`, reply `FF 52 02 00` instead of ACK, since EM4-without-BURTC cannot honor a duration and a silent ACK misleads the client. (EFR32 BURTC *can* wake EM4 on a timer — honoring the duration is a plausible future enhancement, and the wire format already supports it.)
- **nRF:** upgrade the current log-only handler (`device_control.cpp:702-704`) to send `FF 52 00 00`, which also fixes py-opendisplay's false-success on nRF.
- **Protocol spec:** update `ble-flow.html:869` to document the optional payload and the four response frames.

### Use cases — is it worth it?

Energy model used below (order-of-magnitude, consistent with the plan's timing table at `DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md:166-178`): one wake = boot (~0.3 s @ ~60 mA) + 10 s advertising (~15 mA avg) ≈ **0.043 mAh/wake**; deep-sleep floor ~20 µA ≈ 0.48 mAh/day. Daily drain ≈ `0.48 + 0.043 × wakes_per_day` mAh.

#### 1. Adaptive cadence (HA shortens the next sleep when content is coming)

A dashboard tag whose content changes hourly, on a battery budget that wants 6 h sleeps: today the user must choose between 6 h staleness or a permanent 1 h cadence. Quantified on a 1000 mAh cell:

| Policy | Wakes/day | Drain/day | Life | Worst staleness |
|---|---|---|---|---|
| Fixed 6 h | 4 | 0.65 mAh | ~4.2 y | ~6 h |
| Fixed 1 h | 24 | 1.51 mAh | ~1.8 y | ~1 h |
| Adaptive: 6 h base, HA sends `sleep-for` to land the wake at the next known update (3 real updates/day) | ~4–6 | ~0.7 mAh | ~3.9 y | minutes |

Adaptive cadence delivers fixed-1h freshness at fixed-6h battery cost — roughly **2× battery life for hourly-fresh content**. This is the headline justification.

#### 2. "Sleep until just before the next scheduled update" — clock drift

`esp_sleep_enable_timer_wakeup` runs off the RTC slow clock. Two hardware cases:

- **150 kHz internal RC (the ESP32 default, what these boards have):** ±5 % worst case across temperature/voltage; ESP-IDF calibrates it against the main crystal at sleep entry, so ~0.1–1 % is typical at stable temperature, but HA must budget for the worst case on unknown boards.
- **External 32.768 kHz XTAL (if fitted):** ±20 ppm — 0.072 s/h, 0.86 s/12 h. Negligible.

To *guarantee* the device is awake before target time T with drift d, HA commands `X = (T_remaining − fixed_margin) / (1 + d)` (covers a slow clock); a fast clock then wakes it up to `~2d·X` early. Guard bands (`fixed_margin` ≈ 5 s for boot + first advertisement, per plan §2.3):

| Sleep | RC @ 5 % worst | RC @ 1 % typical | XTAL @ 20 ppm |
|---|---|---|---|
| 1 h | command ~3420 s (≈180 s guard); possible early wake ~6 min | ~36 s guard | < 1 s guard |
| 12 h | command ~41 140 s (≈34 min guard); possible early wake ~68 min | ~7 min guard | < 5 s guard |

Two consequences. First, on RC-clock boards a single 12 h "sleep until" is sloppy — up to an hour of slack. The fix is structural and cheap: **re-arm at every wake**. If the device wakes early with nothing queued, the DeliveryManager's device-seen hook fires (`delivery.py:217-231`), HA sends `sleep-for(remaining)` and the error shrinks geometrically (each hop's error ∝ d × remaining; a 12 h target lands within ~3 min after one intermediate hop even at 5 %). Second, there is a semantics gap to design around: **an early wake reverts to the configured cadence, not the remaining time** (the override is one-shot by construction), so "sleep until T" is inherently an HA-side loop, not a single command — which the wake-triggered architecture already supports for free. Recommended HA policy: `guard = max(30 s, 0.02 × X)` for RC boards, `max(5 s, 100 ppm × X)` when a crystal is known.

#### 3. Post-delivery "sleep long now"

Two sub-cases with very different value:

- *"Sleep immediately after drain"* (bare `0x0052`, plan Appendix C-2): worth little on ESP32 — after HA disconnects, `bleActive` goes false and the idle loop calls `enterDeepSleep()` within roughly a loop pass anyway (`main.cpp:169-198`). Savings ≈ a few seconds of awake time per wake. (It *is* the whole story on Silabs, where sleep is command-only.)
- *"Sleep long because I know nothing is coming"* (the duration payload): real money. Tag at 30 min cadence, HA knows the next content is at 07:00 tomorrow (8 h away): one `sleep-for(≈28 500 s)` replaces 15 idle wakes ≈ 0.65 mAh saved — ~40 % of that day's total budget. Generalized: skipping n wakes saves `0.043 × n` mAh against a 0.48 mAh/day floor, so overnight/weekend gaps are where deep-sleep tags earn multi-year lifetimes.

The cost is symmetric: for those 8 h the tag is deaf to ad-hoc `drawcustom` calls, which merely queue (existing `delivery.py:171-207` semantics) but now wait hours instead of minutes. This must be opt-in policy, not default behavior.

#### 4. Risk analysis — a wrong/huge X bricks reachability until the next wake

Because `enterDeepSleep()` arms *only* the timer (`main.cpp:300-305` — no `ext1` button wake), a commanded X is an unconditional communications blackout of exactly X. Layered mitigations, outermost first:

1. **Wire format cap:** uint16 → hard ceiling 65 535 s ≈ **18.2 h**, identical to the worst case already reachable via config. A bug can never exceed what the config field already permits. (This is the decisive argument for uint16 over uint32.)
2. **Firmware sanity clamp:** floor of 10 s (a tiny X is the *opposite* failure — a wake storm that flattens the battery: X = 1 s → ~86 400 wakes/day ≈ 3.7 Ah/day of advertising, dead cell in hours). Reject X = 0 outright (`FF 52 01 00`).
3. **py-opendisplay validation:** `10 ≤ X ≤ 65 535`, raise `ValueError` otherwise.
4. **HA policy cap for *automatic* sends:** `X ≤ min(configured_cadence × 8, 12 h)` and never automatic when `pending_upload` is non-empty (`delivery.py:233-236` already exposes `_has_pending_work()`); a manual service call may use the full range with a confirmation-worthy description in `services.yaml`.
5. **Recommended firmware safety net (plan Phase 4-3, `DEEP_SLEEP_IMPLEMENTATION_PLAN_2026-07-06.md:282`):** arm `esp_sleep_enable_ext1_wakeup` on the button pin in `enterDeepSleep()`. With a button wake, the worst "bricked" outcome degrades to "press the button" — this single change removes most of the residual risk of every use case above and should ship alongside the duration payload.
6. **Availability accounting:** the HA availability horizon is derived from the *configured* cadence (`plan D3`, interval × missed_cycles). A commanded long sleep would falsely flap entities unavailable; the DeliveryManager must report any commanded duration to the sleep profile so the fallback availability interval is temporarily raised to `max(configured, commanded) × missed_cycles`.

#### Should the DeliveryManager send it automatically after each drain?

Not unconditionally. The drain path (`delivery.py:265-297`) is the natural hook — the connection is open, the queue is empty, and `device.deep_sleep(duration)` is one call before the context manager closes — but a *useful* duration requires knowing when the next content arrives, which the manager cannot infer today. Sensible policy ladder:

1. **v1 (ship with the command): explicit hint only.** Add an optional `next_update_in` / `sleep_after` field to `upload_image`/`drawcustom` (service schema in `services.py`); the manager stores it with the `PendingUpload` slot (`delivery.py:67-85`) and, after a successful `_drain_upload` (`delivery.py:299-318`), sends `deep_sleep(min(hint, cap))` over the still-open connection. Automations that know their own schedule (the common case — they triggered the render) opt in per call. Failure handling is trivial: if the sleep command errors, do nothing — the device sleeps its configured cadence anyway.
2. **v2: learned cadence.** The coordinator already timestamps every advertisement; the manager could infer inter-update intervals of *content*, not adverts, and propose durations. Only worth it with real demand — misprediction cost (staleness) is user-visible while the benefit is invisible.
3. **Never automatic:** on latch hardware (`FF 52 02 00` → duration refused), when a reauth is pending (`delivery.py:250-257` pause state), or when anything remains queued.

Guardrails for policy 1: cap per risk item 4 above; skip when `expires_at` of any queued work would elapse during the commanded sleep; update the availability interval per risk item 6; and log the commanded duration in diagnostics (`plan Phase 3-3`) so a "why was my tag silent for 11 hours" report is answerable.

### Bottom line

A one-shot duration is cheap (≈40 firmware lines across `communication.cpp`/`device_control.cpp`/`main.cpp`, a payload parameter on an API that just shipped, one spec paragraph), backward-compatible in both directions by construction, and quantitatively justified: ~2× battery life for hourly-fresh dashboards and ~40 % daily savings on overnight gaps, bounded to an 18.2 h worst case by the uint16 wire format itself. Approach A with the deferred-ACK pattern is the right shape; approach C should be explicitly documented as an anti-pattern so nobody implements it as a "no firmware change needed" shortcut.

---

# Part 4 — WiFi and sleepy-WiFi devices

## Extending the deep-sleep framework to WiFi and sleepy-WiFi devices

*Scope: (a) WiFi transport for OpenDisplay tags in general, (b) deep-sleeping tags that check in over WiFi. Baseline: `docs/WIFI_ARCHITECTURE_2026-07-06.md` (LAN model: device is the TCP server on port 2446, mDNS `_opendisplay._tcp`) and the deep-sleep framework implemented on `feat/deep-sleep` (worktree `.claude/worktrees/agent-a7d4325aeb2a1546b`). All claims re-validated against source, 2026-07-07.*

### 1. Current state (validated)

#### 1.1 Firmware (ESP32 only)

| Capability | Status | Evidence |
|---|---|---|
| STA join + TCP server on port 2446 | Implemented | `Firmware/src/wifi_service.cpp:85-158` (`initWiFi`), `:151` (`wifiServer.begin(wifiServerPort)`) |
| mDNS: hostname `OD<chipid>.local`, service `_opendisplay._tcp`, TXT `msd` | Implemented | `wifi_service.cpp:73-83` (`restartLanService`), `:51-71` (`opendisplay_mdns_update_msd_txt`) |
| TCP framing `[len:2 LE][payload ≤4096]`, same handler as BLE writes | Implemented | Deframe loop `wifi_service.cpp:229-246` → `imageDataWritten(NULL, NULL, …)`; responses one frame each via `send_wifi_lan_frame`, `Firmware/src/communication.cpp:73-81`, called from the shared response path at `communication.cpp:136` |
| Response mirroring TCP↔BLE | Implemented (hazard) | Every response also queued as a BLE notify when a central is connected, `communication.cpp:83-102,136-137` |
| One LAN client at a time; new client replaces old and clears the encryption session | Implemented | `wifi_service.cpp:189-196`; TCP close clears session, `wifi_service.cpp:160-168` |
| LAN session holds the device awake | Implemented | `wifiLanSession` feeds the `bleActive` stay-awake predicate, `Firmware/src/main.cpp:165-174` |
| **WiFi during a deep-sleep wake** | **Absent** | `minimalSetup()` (`main.cpp:245-254`) never calls `initWiFi`; the advertising-window branch (`main.cpp:85-106`) `return`s before `handleWiFiServer()` at `main.cpp:148` is ever reached. WiFi starts only on normal boot (`main.cpp:74`) or after a BLE central connects during a wake (`fullSetupAfterConnection()`, `main.cpp:256-258`) |
| Identity | Chip id derived from efuse MAC | `getChipIdHex()`, `Firmware/src/encryption.cpp:716-741`; same string used for the BLE name `OD<chipid>` (`ble_init.cpp:109`, `display_service.cpp:1339`) and the mDNS hostname (`wifi_service.cpp:74`) |
| mDNS `msd` TXT content | Volatile, **not** an identity | 14 bytes = `msd_payload[2..15]` = dynamic return data + temperature + battery + status (`display_service.cpp:1299-1304`); refreshed continuously (`wifi_service.cpp:51-71`). Good for "is an OpenDisplay device", useless as a stable key |

Known firmware issues relevant here (from `Firmware/FINDINGS.md`): M11 (blocking connect — already mitigated: `setup()` now calls `initWiFi(false)`, `main.cpp:74`), M12 (WiFi never serviced in the wake window — the init side was removed from `minimalSetup`, so today's behavior is "WiFi off while sleepy"), M21 (non-atomic WiFi/touch flags, `FINDINGS.md:302`), L28 (4096-byte LAN frames feed handlers validated only to BLE's 512 B, `FINDINGS.md:426-427`), and clear-config not wiping WiFi creds/AES key until reboot (`FINDINGS.md:330-332`).

#### 1.2 py-opendisplay

- `src/opendisplay/transport/` contains only `connection.py` — `BLEConnection` (Bleak + bleak-retry-connector), `connection.py:23-344`. The transport-facing surface `OpenDisplayDevice` actually uses is small: `connect()`, `disconnect()`, `write_command()` (`connection.py:286`), `read_response(timeout)` (`connection.py:314`), `drain_notifications()` (`connection.py:265`), `is_connected` (`connection.py:341`), `device_name` (`connection.py:63`), `disconnected_callback` (`connection.py:40`), plus BLE-only `clear_cache()`.
- `OpenDisplayDevice.__init__` requires exactly one of `mac_address`/`device_name` (`device.py:415-419`) and `__aenter__` hardcodes `BLEConnection` construction (`device.py:479-486`). No host/port path exists.
- `discovery.py` is BLE-scan only (`discovery.py:20,83`). No mDNS browsing anywhere.
- **Auth identity depends on the MAC**: `device.py:818` derives a 3-byte device identity from `self.mac_address` — a TCP-connected device still needs to know the BLE MAC for encrypted sessions.
- Chunking constants are BLE-shaped: `CHUNK_SIZE = 230`, `ENCRYPTED_CHUNK_SIZE = 154` (`protocol/commands.py:47-48`); the protocol doc allows ~1000 B chunks over LAN (`opendisplay.org/httpdocs/protocol/ble-flow.html:716`).
- Exception names are BLE-branded (`BLEConnectionError`, `BLETimeoutError`) and consumed by name throughout the HA integration (`delivery.py:31-36`, `__init__.py:9-17`).

#### 1.3 HA integration (deep-sleep worktree)

Zero WiFi surface (`manifest.json` declares only the `bluetooth` matcher, manufacturer_id 9286). The deep-sleep framework's BLE-shaped seams, each of which the WiFi work must touch:

1. **Presence**: `OpenDisplayCoordinator` extends `PassiveBluetoothDataUpdateCoordinator` (`coordinator.py:40-51`); "device seen" is fired per parsed BLE advertisement (`coordinator.py:145-146`). The transport-agnostic seam already exists: `DeliveryManager.notify_device_seen(source)` (`delivery.py:223-231`), with the BLE path wrapping it (`delivery.py:218-220`).
2. **Connection dispatch**: `_drain_once()` resolves a `BLEDevice` via `async_ble_device_from_address` and holds `runtime.ble_lock` (`delivery.py:272-293`); setup does the same (`__init__.py:197,221-232`); services hold the same lock (`services.py:354`).
3. **Availability**: `async_set_fallback_availability_interval` — a bluetooth-component API keyed by BLE address (`__init__.py:296-299`) fed by `SleepProfile.availability_interval` (`sleep.py:74-84`).
4. **Sleep model**: `SleepProfile.wake_window_s` is the *BLE advertising* window (`sleep.py:69-71`, firmware `main.cpp:94-96`); `probably_asleep()` is a freshness test on the last BLE advertisement (`sleep.py:91-105`) used by services (`services.py:465,807`).
5. **Identity**: `unique_id` = BLE MAC (`config_flow.py:130,173`); device registry connection is `CONNECTION_BLUETOOTH` (`__init__.py:269`, `delivery.py:418-420`).
6. **Setup-from-cache** (`__init__.py:100-158,200-219`) is already transport-neutral — nothing in `_CachedState` is BLE-specific.

### 2. Sleepy-WiFi physics and the rendezvous choice

#### 2.1 What a WiFi wake cycle looks like

Assuming the firmware gains a "WiFi check-in" wake mode (today it has none — §1.1):

```
timer wake → minimal init (~100-300 ms)
→ WiFi radio on + associate      cold scan: 1-3 s | warm (RTC-cached BSSID+channel): 100-300 ms
→ IP acquisition                 DHCP: 0.5-3 s | static IP / cached lease: ~0 ms
→ mDNS announce (MDNS.begin sends unsolicited announcements) + TCP listener up
→ window open (N seconds): HA hears announcement, connects to :2446, auths (0x0050), drains work
→ session closes / window times out → radio off → esp_deep_sleep_start()
```

Economics vs BLE: BLE advertising during the 10 s window costs ~tens of µA average with ~ms-long mA spikes; ESP32 WiFi in associated/listen state draws ~80-120 mA continuously with 200-300 mA TX peaks, and cold association alone can eat 1-5 s of that. A WiFi wake window is therefore **one to two orders of magnitude more expensive per idle wake** than a BLE one. The compensating advantage is throughput: BLE uploads run stop-and-wait at 230 B/chunk (154 B encrypted — `commands.py:47-48`), so a 48 KB frame is ~210+ round-trips (typically 5-15 s through a proxy), while TCP with ~1000 B chunks (`ble-flow.html:716`) and sub-ms LAN RTTs delivers the same frame in ~1-2 s. Conclusions:

- **Idle wakes must stay cheap.** On dual-radio hardware, BLE advertising remains the right *presence beacon*; WiFi should come up only when there is work (or every Nth wake). The firmware already has the perfect hook: a BLE central connecting during the wake window triggers `fullSetupAfterConnection()` → `initWiFi()` (`main.cpp:89,258`) — i.e. *HA can summon WiFi over BLE today*, enabling a hybrid "rendezvous on BLE, bulk-transfer on TCP" without any new wake mode.
- **Push beats poll.** The device knows when it is awake; HA does not (clock drift over multi-hour sleep intervals makes predicted-window polling miss). Any polling design forces either wide windows (battery cost on device) or frequent probes (noise + still misses).

#### 2.2 Rendezvous options

| Option | How | Verdict |
|---|---|---|
| **A. mDNS announce → HA zeroconf listener → HA connects to device:2446** | Device announces on association (`restartLanService`, `wifi_service.cpp:73-83`); HA runs a service browser and treats the announcement as `notify_device_seen("mdns")` | **Primary.** Matches the implemented firmware model exactly, and is precisely how ESPHome handles sleepy WiFi devices: `ReconnectLogic` is constructed with the shared `zeroconf_instance` (`core/homeassistant/components/esphome/manager.py:944-951`) and, while disconnected, listens for the device's mDNS records so a wake announcement triggers an immediate reconnect instead of a backoff retry. Entities stay available across sleep by policy, not by connection state (`esphome/entity.py:482-487`, `esphome/update.py:164-172`); OTA retries once more for deep-sleep devices (`esphome/update.py:251-256`) — the same "two attempts, wait for available" trick our delivery manager already generalizes. |
| B. Device-initiated TCP check-in to a server in HA (Basic-standard pull model) | Device connects out to `OpenDisplay Server._opendisplay._tcp` | Not implemented on either side (`WIFI_ARCHITECTURE_2026-07-06.md` §2; the parsed-but-dead `server_url`/`server_port` in `config_parser.cpp:449-503`). Best NAT/VLAN traversal, but requires a new HA-hosted server, new firmware client code, and a protocol-role inversion. Defer; keep as the documented answer for multi-VLAN networks. |
| C. HA polls `host:2446` during predicted wake windows | Timer from `SleepProfile.deep_sleep_time_seconds` + last IP | Clock drift makes it unreliable as primary; but a **single TCP SYN to the last-known IP is nearly free**, making it the right *fallback* when mDNS is filtered (no multicast across VLANs) — probe on `notify_device_seen("ble")` too (BLE says awake ⇒ try TCP first). |

**Recommendation: A primary, C fallback** ("mDNS-push with unicast-probe fallback"), plus the BLE-summon hybrid from §2.1 as a free optimization on dual-radio tags. B is a separate future project.

### 3. Layered plan

#### 3.1 py-opendisplay

1. **Extract a `Transport` protocol** (structural, `typing.Protocol`) from the surface listed in §1.2: `connect/disconnect/write_command/read_response/drain_notifications/is_connected/device_name`; `clear_cache()` becomes optional (BLE-proxy-only). `BLEConnection` already satisfies it unchanged.
2. **Add `TCPConnection(host, port=2446, timeout=…)`** speaking the LAN framing. Confirmed compatible: firmware deframes `[len:2 LE][payload]` and hands the payload to the same `imageDataWritten` the BLE write callback uses (`wifi_service.cpp:229-246`), and sends each response as one identical frame (`communication.cpp:73-81,136`) — so `write_command(data)` = write `len(data)` header + data; a background reader task deframes the stream into the same `asyncio.Queue` shape `read_response()` already consumes (`connection.py:314-339`), preserving the stop-and-wait semantics and `drain_notifications()` behavior. Zero-length/oversize frames close the socket on the device side (`wifi_service.cpp:231-235`) — mirror that client-side.
3. **Chunk sizing**: expose `Transport.max_chunk` — keep 230/154 initially even over TCP (L28 warns firmware handlers beyond 512 B are unverified, `FINDINGS.md:426-427`); raise to ~1000 after firmware-side validation. Wins are still large from RTT alone.
4. **Device construction**: allow `OpenDisplayDevice(mac_address=…, host=…, port=…)` or an injected `connection=` object; relax the "exactly one of mac/name" validation (`device.py:415-419`) so `host` is a third addressing mode — **but require the MAC whenever encryption is used**, because the auth identity is MAC-derived (`device.py:818`). `__aenter__` builds `TCPConnection` when `host` is set (`device.py:479-486` becomes a factory). Session/auth/interrogate logic above the connection is transport-independent already.
5. **Exceptions**: introduce transport-neutral `ConnectionFailedError`/`ResponseTimeoutError` with `BLEConnectionError`/`BLETimeoutError` as (deprecated) subclasses/aliases so the HA integration's except-clauses (`delivery.py:246`, `__init__.py:239`) keep working during migration.
6. Optional: `discover_lan_devices()` mDNS browser for CLI parity (HA uses core zeroconf instead).

#### 3.2 HA integration

**Discovery & identity.** Add `"zeroconf": ["_opendisplay._tcp.local."]` to `manifest.json` (pattern: `esphome/manifest.json:24`; a `dhcp` matcher on hostname `OD*` is a possible extra, but note the WiFi STA MAC ≠ BLE MAC on ESP32 — they differ by an efuse offset — so DHCP-MAC matching against `unique_id` is unsafe). `async_step_zeroconf`:
- The stable join key is the **chip id**: mDNS hostname `OD<chipid>.local` (`wifi_service.cpp:74`) equals the BLE-advertised name `OD<chipid>` (`ble_init.cpp:109`) that config entries already carry as `title` (`config_flow.py:154-156`). Persist `chip_id` into `entry.data` at BLE setup (derivable from the device name; add to `_write_cache` payload) so matching is explicit rather than title-parsing.
- Match found → update `entry.data[CONF_HOST]/[CONF_PORT]` (ESPHome-style host tracking) and abort `already_configured`. This makes existing BLE entries **dual-transport** with `unique_id` = BLE MAC unchanged; add `(CONNECTION_NETWORK_MAC or "host")` info to the registry entry alongside `CONNECTION_BLUETOOTH` (`__init__.py:269`). Entry migration: minor-version bump adding `chip_id`/`host` keys, no unique_id change.
- No match → WiFi-only flow: connect via `TCPConnection`, interrogate; the MAC needed for auth/unique_id must come from the device (config/interrogation or a new identity read) — until the protocol exposes it over LAN, WiFi-only onboarding of *encrypted* devices is deferred (open question Q5).

**Presence source.** New `presence.py`: a per-entry `WifiPresence` that (a) registers an `AsyncServiceBrowser`/record listener on HA's shared zeroconf (`zeroconf.async_get_instance`, as ESPHome does at `esphome/config_flow.py:810`) filtered to instance `OD<chipid>._opendisplay._tcp.local.`; on add/update it resolves the current address, stores `last_seen_wifi`, and calls `delivery.notify_device_seen("mdns")` — the seam built for exactly this (`delivery.py:223`); (b) implements the fallback probe: when `notify_device_seen("ble")` fires or a predicted wake nears, attempt one cheap TCP connect to the last-known host and, on success, treat as `notify_device_seen("tcp")`.

**Availability without the bluetooth fallback API.** `async_set_fallback_availability_interval` (`__init__.py:297`) only governs the *bluetooth* availability machinery. Introduce a transport-neutral `PresenceTracker` in runtime data: `last_seen(source)` timestamps + a single `async_call_later(profile.availability_interval)` timer re-armed on any presence event (BLE advertisement via the coordinator hook at `coordinator.py:145-146`, mDNS/TCP via `WifiPresence`); entities read availability from it via dispatcher instead of (only) `coordinator.available`. For dual-transport entries keep the bluetooth fallback interval too — the two mechanisms OR together. This is the ESPHome availability philosophy (deep-sleep ⇒ available by policy, `esphome/entity.py:482-487`) adapted to our interval-based model (`sleep.py:74-84`).

**Connection dispatch.** Add a runtime device factory, e.g. `runtime.async_open_device(use_measured, key) -> OpenDisplayDevice`:
1. If `CONF_HOST` set and (device not sleepy, or `last_seen_wifi` within the WiFi wake window) → `OpenDisplayDevice(mac_address=addr, host=host, …)`.
2. Else/on TCP failure → today's `async_ble_device_from_address` path.

Consumers: `delivery._drain_once` (replace `delivery.py:274-291`), setup interrogation (`__init__.py:221-232`), services (`services.py:354`), config-flow test connection (`config_flow.py:113-124`). Rename `ble_lock` → `device_lock` — it must still be held across *either* transport, both because commands are stop-and-wait and because the firmware serves one session with BLE↔TCP response mirroring (`communication.cpp:83,136-137`): concurrent sessions on the two transports would cross-deliver responses and desync both. Scope change in `_drain_once`: the BLE-device resolution block becomes `device = await runtime.async_open_device(...)` inside the same lock and `asyncio.timeout(DELIVERY_DEADLINE_S)` (`delivery.py:285`).

**SleepProfile.** Add a transport dimension: `wake_window_s` stays the BLE advertising window; add `wifi_window_s` (from the future firmware check-in config, defaulting to the same value) and let `probably_asleep()` take the freshest of the per-source timestamps. No behavioral change for BLE-only devices.

#### 3.3 Firmware asks (separate repo; minimal, ordered)

1. **F1 — WiFi check-in wake mode** (the enabler): when a config flag is set, `minimalSetup()` also calls `initWiFi(false)` and the advertising-window branch (`main.cpp:85-106`) services `handleWiFiServer()` + the association check before its early `return`; an accepted LAN session flips to full mode exactly like a BLE central does at `main.cpp:86-92` (the stay-awake predicate already covers LAN sessions, `main.cpp:165-174`). This is the completion of finding M12's "or service handleWiFiServer inside the advertising branch" option (`FINDINGS.md:240`).
2. **F2 — Fast reconnect**: cache BSSID/channel and static IP or the DHCP lease in RTC memory so the wake-window association is ~200-500 ms, not 1-5 s. Without F2, F1 wakes are painfully expensive; ship together.
3. **F3 — mDNS announce promptly on association** (already implicit in `MDNS.begin` inside `restartLanService`, `wifi_service.cpp:73-83`; verify announcement is actually multicast at wake and consider a couple of repeated announcements for lossy WLANs).
4. **F4 — LAN payload validation to 4096 B** (clears L28) so the client can raise chunk size to ~1000 B.
5. **F5 — Hygiene**: wipe WiFi creds + AES key on clear-config (`FINDINGS.md:330-332`); atomic WiFi flags (M21); optionally a "WiFi every Nth wake" divisor to tune battery cost.

### 4. Phasing

| Phase | Content | Size | Depends on | Acceptance criteria |
|---|---|---|---|---|
| **W0** — py-opendisplay transport | Transport protocol extraction, `TCPConnection`, neutral exceptions, `host=` construction, `max_chunk` | **M** | Nothing (parallel to deep-sleep PR); firmware LAN as shipped | CLI can interrogate + upload to a mains-powered tag over `host:2446` with and without encryption; BLE test suite unchanged; framing fuzz tests (split/coalesced frames) pass |
| **W1** — HA dual-transport for awake devices | zeroconf matcher + `async_step_zeroconf` host-attach, `chip_id` persistence + entry migration, `device_lock` rename, `runtime.async_open_device` dispatch used by services/setup/config-flow | **M** | W0 released; merges cleanly over the deep-sleep PR (touches `__init__.py`, `services.py`) | A BLE entry on a WiFi-enabled mains tag transparently uploads via TCP; unplugging Ethernet/AP falls back to BLE with no user action; no duplicate entries from zeroconf discovery |
| **W2** — WiFi presence for sleepy devices (HA side) | `WifiPresence` (zeroconf listener + unicast probe), `PresenceTracker` availability, `notify_device_seen("mdns"/"tcp")`, `_drain_once` via factory, SleepProfile per-source freshness | **M** | **Deep-sleep PR merged** (delivery manager, SleepProfile, setup-from-cache are its deliverables); W1 | With F1/F2 firmware (or a mains tag simulating wakes), a queued upload drains over TCP at the next mDNS announcement; entities stay available across cycles without the bluetooth fallback API on a WiFi-only network; queued-work events fire as on BLE |
| **W3** — Firmware check-in mode | F1+F2 (+F3 verification), HA honors `wifi_window_s` | **L** (firmware) | Firmware team; W2 for end-to-end test | Battery tag with WiFi check-in enabled: wake→announce→drain→sleep round-trip < ~8 s warm; idle wake (no pending work) closes within the window; battery regression measured and documented |
| **W4** — Optional | WiFi-only onboarding (needs MAC-over-LAN identity read), BLE-summon hybrid (connect BLE → device starts WiFi → bulk over TCP), pull-model server evaluation | **L** | W2/W3; protocol additions | Per-feature |

### 5. Risks and open questions

1. **Two transports racing.** Inside HA the single `device_lock` serializes them, but the firmware's response mirroring (`communication.cpp:136-137`) means *any* other BLE central connected while HA talks TCP receives copies of TCP responses (and a foreign TCP client kicks HA's session, `wifi_service.cpp:189-196`). Mitigation: treat unexpected disconnect/desync as retryable (delivery already does); firmware could suppress mirroring for encrypted sessions.
2. **mDNS across VLANs/NAT.** Multicast rarely crosses VLANs; the unicast probe fallback (option C) covers reachable-but-unannounced devices, and the pull model (option B) remains the documented long-term answer. Document mDNS-reflector caveats.
3. **Encrypted-session parity over TCP.** The protocol says sessions follow BLE rules and are cleared on TCP close (`ble-flow.html:469`; `wifi_service.cpp:163`), and auth identity is MAC-derived (`device.py:818`) — so TCP works only when the MAC is known. Needs an integration test: auth → encrypted upload → disconnect → re-auth over TCP.
4. **IP churn.** DHCP leases change between wakes; `WifiPresence` must always re-resolve from the mDNS answer rather than trusting a stored IP; the stored host is best-effort for the probe path only.
5. **WiFi-only onboarding identity (Q5).** No LAN-visible stable ID except the hostname chip id; the `msd` TXT is volatile telemetry (`display_service.cpp:1299-1304`). Either add a "read identity/MAC" command usable over LAN, or require BLE-first onboarding (recommended for v1).
6. **Frame-size tolerance (L28)** blocks the biggest TCP throughput win until F4 lands; W0 must default to conservative chunks.
7. **Battery economics of F1.** If warm-associate cannot be made fast on real APs (enterprise auth, band-steering), sleepy-WiFi check-in may be unviable and the BLE-summon hybrid (W4) becomes the primary sleepy-WiFi story instead — cheap to pivot since W0-W2 are shared.
8. **Availability semantics drift.** Two availability mechanisms (bluetooth fallback interval + `PresenceTracker`) must never disagree visibly; entities should consume one merged signal, with the bluetooth interval kept only as the input for BLE-sourced last-seen.
