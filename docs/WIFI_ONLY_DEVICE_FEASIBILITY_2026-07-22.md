# WiFi-Only OpenDisplay Device — Feasibility Study

**Date:** 2026-07-22
**Status:** Design-feasibility report (report-only; no code changed)
**Builds on (same `docs/` folder, do not re-derive):**
`WIFI_TRANSPORT_PROPOSAL_2026-07-22.md` (the Proposal; §3.4 port-selected security, §3.6 mDNS spec, §3.6.6 unique_id hazard), plus the four `WIFI_READINESS_*_2026-07-22.md` audits and `FIRMWARE_RAM_FOOTPRINT_2026-07-22.md` (the RAM doc).

**Scope:** Feasibility of a device whose steady-state transport is WiFi only, in two variants:
- **Variant A — BLE fully disabled.** The device never uses BLE; WiFi is the only transport, including for provisioning.
- **Variant B — BLE provisioning-only.** BLE comes up transiently to receive WiFi credentials; once the device has joined WiFi, BLE is torn down and steady state is WiFi-only.

Primary target: mains-powered WiFi display. Secondary: duty-cycled battery.

---

## Executive summary

| | Variant A (BLE disabled) | Variant B (BLE provisioning-only) |
|---|---|---|
| **Verdict** | **Technically possible, not recommended as a product posture.** Requires a new provisioning subsystem (SoftAP/Improv-Serial), a new identity story, and full loss of passive event telemetry — for a payoff that is only RAM + coex headroom, since **every WiFi-capable target already has a BLE radio** (ESP32-S3/C6; there is no BLE-less SKU to save BOM on). Worth keeping only as a **compile-time RAM-reclaim option** for the C6-N4 + TLS corner case. | **Feasible and the recommended shape for a "WiFi display."** Reuses the shipped BLE `0x26 wifi_config` provisioning path, keeps the BLE MAC as the fleet-wide identity anchor, gets the RAM/coex win in steady state, and gains a free re-provisioning story (join failure → BLE comes back). Firmware deltas are small and build directly on the Proposal's Phase 1–2. |

**The one structural fact that frames everything:** WiFi in this ecosystem is ESP32-only, and every ESP32 in scope (S3, C6; even the excluded C3) has a BLE radio on die (`Firmware/platformio.ini` envs; readiness-firmware audit table). "BLE-less hardware" does not exist here. Variant A is therefore purely a *software posture*, and its three costs — the provisioning bootstrap, the identity anchor, and passive event telemetry — are all things BLE currently provides for free. Variant B keeps all three and still removes NimBLE from steady-state RAM.

**Hardest problem cluster (both variants, honestly):**
1. **Bootstrap** — a factory-fresh device with no creds and no BLE has no ingress at all (variant A). Solvable only by SoftAP+captive-portal or Improv-over-Serial, both new subsystems. Variant B dissolves this: the shipped BLE config-write path *is* the bootstrap.
2. **Identity** — HA `unique_id` is the raw uppercase BLE MAC (`config_flow.py:177,224`, no `format_mac`). Variant B keeps it stable across the transition via the `mac` TXT (Proposal §3.6), *contingent on the §3.6.6 case-normalization fix*. Variant A can still use the BT MAC (readable from eFuse without the stack), but a mixed fleet forces the `format_mac` migration to land first.
3. **Event telemetry** — button/touch/battery/reboot reach HA today as *passive BLE adverts* (`coordinator.py:105–150`). WiFi has no passive push channel; mDNS TXT is a poor substitute and HA's zeroconf pipeline does not stream TXT changes to entities. On mains this is recoverable (persistent TCP + a small push extension, or polling). **On battery it is not** — a sleeping WiFi device cannot deliver a button press without a multi-second association. This is the strongest argument that battery devices should keep BLE.

---

## 1. Variant A — BLE fully disabled

### 1.1 What is (and is not) gated on `OD_COMM_MODE_BLE` today

**Nothing gates BLE init.** `OD_COMM_MODE_BLE` (bit 0, `Firmware/include/opendisplay_structs.h:407`) exists in config and is *only ever used in a log line*: `config_parser.cpp:640` prints "BLE: enabled/disabled" — no code path consults it. BLE init is unconditional:

- `setup()` calls `ble_init()` on every ESP32 boot (`Firmware/src/main.cpp:117`), which calls `ble_init_esp32(true)` (`ble_init.cpp:203-205,250`): `BLEDevice::init` → server → service 0x2446 → advertising start.
- nRF equivalently: `ble_nrf_stack_init()` at `main.cpp:101`, advertising at `main.cpp:120` (out of scope — no WiFi hardware).
- Contrast with WiFi, which **is** gated on its comm-modes bit: `initWiFi()` returns early unless `communication_modes & COMM_MODE_WIFI` and `wifiConfigured` and a non-empty SSID (`wifi_service.cpp:88-104`).

So the symmetry the config schema implies (per-transport enable bits) is half-implemented: the WiFi bit is honored, the BLE bit is decorative. There is **no compile-time switch either** — `NimBLE-Arduino` is in `lib_deps` for all ESP32 envs (`platformio.ini:8`) and `ble_init.cpp` compiles unguarded. (Analogous to the Proposal's observation that WiFi scope isn't enforced on C3 — the repo has no `-DOPENDISPLAY_ENABLE_WIFI` *or* `-DOPENDISPLAY_ENABLE_BLE` axis.)

### 1.2 What breaks if BLE never initializes

Walking the BLE touchpoints assuming `ble_init()` is simply skipped:

| Subsystem | Behavior with BLE never up | Safe? |
|---|---|---|
| **Discovery (BLE advert)** | No advert → HA's `bluetooth` manifest matcher (manufacturer id 9286, `custom_components/opendisplay/manifest.json:4`) never fires. Discovery must be zeroconf-only (Proposal H1/H2). | By design |
| **`updatemsdata()`** (`display_service.cpp:1707`) | **Not safe as written.** The ESP32 branch guards on `advertisementData != nullptr` (`:1758`) — but `advertisementData` is *statically* initialized to `&globalAdvertisementData` (`main.h:393`), so it is never null even with the stack down. With `pServer == nullptr` the code falls through to `BLEDevice::getAdvertising()` (`:1767`) and then `pAdvertising->stop()/start()` (`:1772,1785`) against an uninitialized NimBLE stack. Needs an explicit "BLE stack up" guard. The mDNS mirror (`opendisplay_mdns_update_msd_txt()`, `:1788`) is independent and keeps working. | **Needs guard** |
| **`pollActivity()` / deep-sleep gates** | Already null-safe: `connCount` reads 0 when `pServer == nullptr` (`main.cpp:203`); `workInFlight` uses `pServer && …` (`main.cpp:414`); `enterDeepSleep` checks `pServer != nullptr` (`main.cpp:524,547`). LAN-session activity keeps the device awake exactly like BLE (`main.cpp:204,405`). | Yes |
| **Response fan-out** | `sendResponse`/`sendResponseUnencrypted` call `send_wifi_lan_frame()` + `esp32_queue_ble_notify_copy()` (`communication.cpp:141-142,228-229`); the BLE copy is a no-op unless a central is connected and subscribed (`communication.cpp:88-89`, `esp32_ble_notify_enabled`, `ble_init.cpp:222-228`). | Yes |
| **Config-write path** | `imageDataWritten` is transport-agnostic (`communication.cpp:534`); config writes work over TCP identically. Only the *first-ever* config (which enables WiFi) has no ingress — that is the bootstrap problem, §3. | Yes (post-bootstrap) |
| **Battery wake path** | **Broken by design for WiFi.** On a deep-sleep wake, `setup()` deliberately skips `initWiFi()` (`main.cpp:123-125`, "wake: WiFi stays deferred to fullSetupAfterConnection()"), and `fullSetupAfterConnection()` — the only wake-path WiFi bring-up — is triggered *exclusively by a BLE connection* (`main.cpp:303-308`). The entire post-wake window loop (`main.cpp:303-339`) is "advertise BLE, wait for a central, sleep on timeout." A WiFi-only battery device would wake, never start WiFi, and go back to sleep. Must be reworked: wake → `initWiFi()` → associate → mDNS announce → wait for TCP connect → sleep on idle. | **Needs rework** |
| **DFU / recovery** | ESP32 DFU is `CMD_ENTER_DFU` (0x0051) over the normal opcode channel — works over TCP. nRF's BLE DFU service is irrelevant (no WiFi). Recovery when WiFi creds are wrong = bricked-until-reflash in variant A unless a SoftAP fallback exists (§3). | Conditional |

**Bottom line:** the transport core is already clean (one dispatcher, null-safe activity/response paths); the two real code changes are the `updatemsdata` stack-up guard and the battery wake path. The dangerous part of variant A is *operational*, not mechanical: no BLE means no out-of-band recovery when WiFi is misconfigured.

### 1.3 A clean switch: config-time vs compile-time

- **Config-time (recommended shape):** make `ble_init()` honor `OD_COMM_MODE_BLE` the way `initWiFi()` honors `OD_COMM_MODE_WIFI` — i.e. skip init when bit 0 is clear *and* the device is provisioned (see the variant-B fallback rule, §2.3, which should apply even here). One firmware image serves both postures; the factory-provisioning path (`OPENDISPLAY_FACTORY_CONFIG_HEX`, `Firmware/scripts/factory_config_gen.py`) can ship WiFi-only configs.
- **Compile-time (`-DOPENDISPLAY_ENABLE_BLE=0`):** the only way to reclaim NimBLE's *static* footprint (its code/bss are in the measured image — RAM doc §"Measured static footprint") in addition to its runtime heap. Pairs naturally with the Proposal's proposed `-DOPENDISPLAY_ENABLE_WIFI` flag (Proposal §Risks). Worth having as a build axis for the C6-N4 + TLS case; not a product default.

### 1.4 What variant A actually buys

From the RAM doc's C6-N4 budget (~211 KB free at boot; BLE+WiFi coexistence floor ~50–105 KB free; TLS handshake spike down to ~10–70 KB):
- **RAM:** removing NimBLE returns its **~30–50 KB working heap** (RAM doc §3) plus, if compiled out, part of the static image. That single change moves "BLE + WiFi + TLS on C6-N4" from **marginal/high-risk** to **comfortable** — the TLS-PSK 2447 mode stops needing aggressive `MBEDTLS_SSL_IN/OUT_CONTENT_LEN` surgery to be safe.
- **Coexistence:** C6 (and C3) are single-antenna; BLE and WiFi time-share the 2.4 GHz radio via the IDF coex arbiter, untuned today (readiness-firmware §Power & coexistence). BLE-off removes the time-share entirely → full WiFi throughput and simpler latency behavior.
- **Power:** modest. Steady BLE advertising is small (duty-cycled TX, sub-mA average) next to an associated WiFi STA (~80–100 mA without modem sleep). The dominant power lever is F5 (`WIFI_PS_MIN_MODEM`) and duty-cycling, not BLE removal.

**What it costs:** §3 (bootstrap), §4 (identity), §5 (telemetry) in full. On mains-powered S3 (PSRAM, dual-core, comfortable RAM per the RAM doc), variant A buys essentially nothing that matters.

---

## 2. Variant B — BLE provisioning-only

### 2.1 Teardown mechanics already exist

`enterDeepSleep()` already performs a full runtime BLE teardown: stop advertising → `BLEDevice::deinit(true)` → `esp32_ble_clear_handles()` (`main.cpp:547-556`; `ble_init.cpp:214-220` nulls `pServer/pService/pTx/pRx` and clears the notify flag). Because every BLE consumer except `updatemsdata` guards on `pServer != nullptr` (§1.2), the same sequence invoked *without* sleeping — i.e. "deinit BLE, keep running on WiFi" — is nearly safe today. Two fixes:

1. **The `updatemsdata` hazard (same as §1.2):** post-deinit, its ESP32 branch would call `BLEDevice::getAdvertising()`/`start()` on a dead stack (`display_service.cpp:1758-1786`). It *does* run again after teardown in variant B (60 s idle refresh, `main.cpp:447-451`; button/touch paths, `device_control.cpp:88,456`, `touch_input.cpp:728`; `msdUpdatePending`, `main.cpp:371-373`). Add a `bleStackUp` flag checked alongside `advertisementData != nullptr`. The mDNS TXT mirror then becomes the sole MSD publication — which is exactly what variant B wants.
2. **Re-init is available:** `ble_init_esp32()` is re-entrant — it clears handles first (`ble_init.cpp:251`) and rebuilds the stack, and the deep-sleep wake path already exercises deinit→(reboot)→init every cycle. A live deinit→reinit cycle (no reboot) is new territory but uses only existing functions; NimBLE 2.x supports `init` after `deinit(true)`.

### 2.2 State that assumes BLE stays up

- The wake-window loop (`main.cpp:303-339`) and `bleRestartAdvertisingPending` machinery (`ble_init.cpp:230-248`, `main.cpp:314,375`) — inert once `pServer` is null, but the *wake-path WiFi bring-up dependency* (§1.2, battery row) must be fixed for any battery WiFi posture.
- The MSD `CONNECTION_REQUESTED` status bit (`display_service.cpp:1728`) and HA's wake-advert-driven delivery (`delivery.py:235`, "device advertised (it is awake now)") — see §5.
- Provisioning feedback: today `0x26` provisioning is config-write + `reloadConfigAfterSave()` → `initWiFi()` (readiness-firmware §Provisioning). Fire-and-pray: the host learns nothing about join success before teardown. **Do not tear down BLE until join success is confirmed** — this is exactly what the Proposal's F3 opcodes are for (`CMD_NET_JOIN 0x0055` + async `CMD_NET_STATUS 0x0054`-shaped result, Proposal §3.2). Variant B should be sequenced *after* F3.

### 2.3 Trigger and re-provisioning (the state machine)

Recommended lifecycle, which doubles as the variant-A config-time rule:

```
BOOT:
  provisioned = (COMM_MODE_WIFI set) && wifiConfigured && ssid non-empty   [wifi_service.cpp:88-104]
  wifi_only   = provisioned && !(communication_modes & OD_COMM_MODE_BLE)
  if !wifi_only:            ble_init() as today
  if wifi_only:             ble_init() anyway (provisioning safety), start WiFi
STEADY:
  on WiFi got-IP + (grace period, e.g. 60 s with no BLE central):
      if wifi_only → stop advertising, BLEDevice::deinit(true), clear handles, set bleStackUp=false
FALLBACK (re-provisioning):
  on association failure N consecutive retries / WL_NO_SSID_AVAIL / auth-fail,
  or on CMD_NET_FORGET (0x0056) over TCP:
      → ble_init_esp32(true): BLE advertising returns until re-provisioned + joined
```

- **Trigger = join success (got-IP), not credential receipt.** Tearing down on `0x26` receipt would strand the device if the SSID is wrong. Got-IP + grace window lets the provisioning host confirm via `CMD_NET_STATUS` over BLE, then observe the mDNS advert appear, before BLE goes away.
- **Re-enablement is automatic**: creds rot (AP moves, password change) → association fails → BLE advertising resumes → the device is re-discoverable by HA's bluetooth matcher and re-provisionable via the existing H5 flow. This mirrors the Improv/ESPHome pattern (provisioning transport available iff not successfully on WiFi) and *eliminates variant B's recovery risk*. It also means the fallback BLE advert is a useful "WiFi is broken" distress beacon HA can alert on.
- The `wifi_only` predicate above gives the `communication_modes` combination its natural meaning — **bit 0 clear + bit 2 set = WiFi-only steady state** — with the normative footnote that BLE MUST still come up when unprovisioned or join-failed (§7).

**Verdict: variant B is a small, low-risk delta** on top of the Proposal: the F3 opcodes (already planned), one teardown call sequence that already exists, the `updatemsdata` guard, the fallback rule, and (for battery) the wake-path rework shared with variant A.

---

## 3. The provisioning bootstrap problem

A factory-fresh WiFi-only device has no creds; in variant A it has no BLE either. Options, assessed against this hardware (ESP32-S3/C6, e-paper, sometimes sealed/battery) and HA's onboarding model:

| Option | Firmware cost | HA fit | Assessment |
|---|---|---|---|
| **BLE provisioning (variant B)** — the shipped `0x26` path, ideally + F3 `NET_JOIN/STATUS` | **~0** (exists: `config_parser.cpp:457`, `communication.cpp` CONFIG_WRITE; F3 planned) | Excellent — HA bluetooth-discovers the tag (manufacturer id 9286) and the Proposal's H5 `configure_wifi` service does the rest | **Winner.** No new subsystem, feedback-capable with F3, automatic fallback (§2.3). Requires only that the BLE radio exists — which it always does. |
| **Improv-over-BLE** (standardized wrapper for the same thing) | Small–medium (Improv GATT service + state machine alongside/instead of 0x2446) | **Best-in-class**: HA core auto-discovers Improv devices by service UUID and runs a guided credential flow with zero integration code (`core/homeassistant/components/improv_ble/manifest.json` — matcher on service UUID `00467768-…`) | Attractive *interop* upgrade to variant B: unprovisioned device advertises Improv, HA (or any Improv app) provisions it, then normal OpenDisplay flow. Consider as a later polish; the proprietary path already works. |
| **SoftAP + captive portal** | Medium (WiFi AP mode + DNS hijack + minimal HTTP form; ESPHome-style) | Indirect — HA can't discover a device that's its own AP; user joins `OD-XXXX` AP by hand; post-join, zeroconf (or a `dhcp` manifest matcher, cf. `core/…/components/*/manifest.json` dhcp entries) picks it up | The only *self-contained* variant-A bootstrap. Standard consumer-device UX, but a genuinely new firmware subsystem (AP mode, HTTP server, portal page) with its own RAM/attack surface, and no display-side confirmation UX beyond the e-paper itself. Required if variant A ever ships as a posture without factory pre-provisioning. |
| **Improv-over-Serial (USB)** | Small (serial protocol handler) | Good for bench/first-boot — ESPHome's web-flash + Improv-Serial pattern; not an HA-core discovery, but a documented onboarding page can drive it from the browser | Excellent *factory/developer* channel; useless for a sealed battery device or a wall-mounted panel without exposed USB. Complement, not primary. |
| **WPS** | Small | None | **Reject** — deprecated, insecure, widely disabled on modern APs. |
| **QR / NFC-assisted** | NFC: only Silabs has real NFC (no WiFi — wrong chip family; NFC ground truth per project memory). QR: display can *show* a QR (e.g. its SoftAP name) but can't *read* one | n/a | Not standalone. QR-on-epaper is a nice UX layer *on top of* SoftAP ("scan to join the setup AP"). |
| **Factory pre-provisioning** | 0 (`OPENDISPLAY_FACTORY_CONFIG_HEX` build-time config exists) | n/a | Viable for managed fleet/commercial deployments (creds known at install time); a non-answer for consumer onboarding. |

**Recommendation:** Variant B's BLE bootstrap is the answer; there is no scenario in this hardware line where paying for SoftAP is forced, because the BLE radio is always present. If a marketing-level "BLE-free" SKU is ever mandated anyway, implement **SoftAP + captive portal with a QR on the e-paper**, and accept the added subsystem. Improv-over-BLE is the best future refinement (free HA onboarding UX); Improv-Serial is worth adding opportunistically for the factory/dev bench.

---

## 4. HA discovery & identity

With no BLE advert, discovery is zeroconf-only: manifest `"zeroconf": ["_opendisplay._tcp.local."]` + `async_step_zeroconf` (Proposal H1/H2; ESPHome precedent `core/…/esphome/manifest.json:24`). Identity is where the variants diverge:

### 4.1 Variant B — identity stays the BLE MAC (stable across the transition)

The device has a BLE MAC forever, whether or not the stack is up. The `mac` TXT (Proposal §3.6.2, REQUIRED) carries it, so:
- BLE-provisioned first: `async_step_bluetooth` sets `unique_id` = MAC (`config_flow.py:177,224`); later zeroconf discovery of the same MAC merges host/port onto the same entry (`_abort_if_unique_id_configured(updates=…)`, Proposal H2). **Identity is continuous across BLE-provisioning → WiFi-steady-state — yes**, with two conditions:
  1. **§3.6.6 must land**: raw-uppercase BLE `unique_id` vs `format_mac`'d lowercase zeroconf MAC is a silent-duplicate bug. Normalize both sides (migration option (a)).
  2. **The `mac` TXT must be publishable after BLE deinit.** Read `NimBLEDevice::getAddress()` once while the stack is up (provisioning boot) and cache/persist it — or use `esp_read_mac(ESP_MAC_BT)`, which applies the correct universal-MAC offset without the stack. Either way, honor §3.6.5: the value must equal the AdvA HA saw during provisioning; validating once at first teardown (stack address == computed address) is cheap insurance.

### 4.2 Variant A — the anchor should *still* be the BT MAC

Even never-advertised, the chip has a well-defined Bluetooth MAC derived from eFuse (`ESP_MAC_BT`). Candidate anchors:
- **BT MAC (recommended):** keeps *one* identity namespace across the whole fleet — a variant-A device and a BLE device use the same `unique_id` scheme, and a device later re-flashed to a BLE-capable posture keeps its identity. The name "BLE MAC" becomes a misnomer ("device primary MAC") but nothing breaks.
- **WiFi MAC:** discoverable via DHCP matchers, but creates a second namespace offset from the BT MAC by a build-config-dependent delta (§3.6.5's +1/+2 warning) — a footgun for fleet tooling. Reject.
- **`device_id` (4 B):** transport-neutral and already proposed as an optional `id` TXT, but only 4 bytes, operator-assigned, and collision-prone across fleets; keep as a secondary correlation key, not the `unique_id`. Reject as anchor.

Config-flow: `async_step_zeroconf` reads `mac` TXT → `format_mac` → `async_set_unique_id`. **The §3.6.6 hazard applies with full force**: a mixed fleet (existing raw-uppercase BLE entries + new lowercase zeroconf entries) makes the `async_migrate_entry` normalization a *prerequisite*, not a nicety — otherwise the same physical device provisioned first over BLE and later WiFi-only forks into two HA devices. Make `mac` REQUIRED in the TXT (it already is in §3.6.5) and treat a missing `mac` as a non-onboardable advert rather than falling back to `device_id` (a fallback would create the second namespace by the back door).

### 4.3 The bigger HA-side gap: the coordinator/availability model is BLE-shaped

Beyond the config flow, the integration's *runtime* identity of a device is a Bluetooth passive coordinator: updates enter only via `_async_handle_bluetooth_event` parsing `manufacturer_data[MANUFACTURER_ID]` (`coordinator.py:105-120`), availability via bluetooth's unseen-timeout (`coordinator.py:96`), and sleep/wake inference from last-advert age (`sleep.py:97-99`). A WiFi-only device **never produces a bluetooth event**, so with today's classes it would sit permanently unavailable with empty sensors. This is a structural change *beyond* Proposal H1–H6: the coordinator needs a non-BLE update source (mDNS liveness / TCP polling) feeding the same `OpenDisplayData`, and availability needs to key off network reachability. The transport-agnostic `notify_device_seen(source)` hook (`delivery.py:16,235`) is the right seam and already takes a source tag. Effort: moderate; the entity layer (`sensor.py:49,62` reading `upd.advertisement.*`) can stay unchanged if the WiFi source synthesizes the same `AdvertisementData` model from MSD bytes (`msd` TXT or `CMD_READ_MSD`).

---

## 5. Telemetry implications & gaps (honest assessment)

What the passive BLE advert delivers to HA today, and where each lands WiFi-only:

| Signal | Today (BLE advert) | WiFi-only replacement | Quality |
|---|---|---|---|
| Battery mV, chip temp | MSD bytes → `sensor.py:49,62` | `msd` mDNS TXT (already mirrored, 400 ms-throttled — `wifi_service.cpp:51-71`) via an integration-run `AsyncServiceBrowser` watching TXT updates; or poll `CMD_READ_MSD` (0x0044, `communication.cpp:644`, plaintext-readable) over TCP | **OK on mains.** Note: HA's *manifest* zeroconf pipeline only fires discoveries; continuous TXT-change consumption needs custom browser code in the integration. |
| Reboot edge (`rebootFlag`) | MSD status bit, edge-detected (`coordinator.py:151-170`) | Same MSD bit via TXT watch/poll; also implicitly visible as an mDNS service re-registration | OK |
| Device-seen / wake signal (drives queued delivery, `delivery.py:235`) | Advert appearance = "awake now" | mDNS announcement on association (device multicasts on service registration, `wifi_service.cpp:73-83`) → `notify_device_seen("wifi")` | OK — mains devices are always-seen anyway; battery devices announce on each wake (after association latency) |
| **Button/touch events** | `dynamic[11]` changes in the advert, tracked per-advert (`coordinator.py:131-135`, `event.py`); firmware pushes an immediate advert + boost on the event (`device_control.cpp:456-458`, `touch_input.cpp:728`); latency ≈ tens of ms | (a) TXT-update watch: the event *does* reach the TXT within 400 ms **if associated** — but mDNS TXT re-multicast per keypress is unicast-storm-adjacent and delivery is best-effort; (b) persistent TCP + device→host push: needs **new protocol work** (an unsolicited notification frame — note `sendResponse` can already write to a connected LAN client unprompted, so the wire mechanics exist; what's missing is a subscribe/push contract); (c) polling `CMD_READ_MSD`: latency = poll interval, and edge events between polls can be *lost* (MSD carries current state + a 4-bit counter, not a queue) | **Mains: viable but degraded** without (b); recommend (b) as a small Phase-3+ protocol addition ("MSD push on change over an open LAN session"). **Battery: genuinely broken** — an asleep device isn't associated; a button press must wake → associate (~1.5–3 s) → announce before HA hears anything. A 3-second button is a bad button. |

**Honest summary:** the mains story closes to "good" with one modest protocol addition (unsolicited MSD-changed push over an open TCP session — the LAN write path `send_wifi_lan_frame`, `communication.cpp:78`, already supports it mechanically). The battery story does **not** close: low-latency events are architecturally a BLE-advert strength, and no WiFi mechanism at this power budget replaces them. A battery device with buttons/touch should keep BLE (i.e., not be WiFi-only at all, or be variant B with BLE *retained* for advert telemetry — which is then just the Proposal's dual-transport device, not a WiFi-only one).

---

## 6. Power & duty-cycled battery

- **Mains (primary target):** trivially fine. Both variants remove nothing needed; apply F5 (`esp_wifi_set_ps(WIFI_PS_MIN_MODEM)`) for idle-current hygiene. The device self-holds awake on a live LAN session (`pollActivity`, `main.cpp:204,219`) and never deep-sleeps on mains (`enterDeepSleep` bails when `power_mode != 1`, `main.cpp:514`).
- **Battery duty cycle:** the cycle is wake → boot (~hundreds of ms; wake path skips display init, `main.cpp:103-113`) → **associate + DHCP: ~1.5–3 s at ~80–100 mA** (readiness-firmware §Power: association current is the budget-killer; today firmware treats WiFi as mains-only) → mDNS announce → host connects & transacts (TCP at 4094-byte frames is *fast* — seconds for a full image vs minutes over BLE) → idle-hold expiry → deep sleep. Energy per wake is dominated by association + awake window; this is battery-viable **only at low wake cadence** (a few wakes/day: fine; every few minutes: not). BLE's advertising-window wake is an order of magnitude cheaper *when no transfer happens* (the common case), which is why the current firmware's posture exists.
- **Prerequisite either way:** the wake path currently starts WiFi only after a BLE connect (`main.cpp:124,303-308`) — any battery-WiFi posture needs the wake path reworked to associate unconditionally (and, ideally, use a static IP / fast-reconnect to shave association time).
- **Is variant B the battery sweet spot?** For a *display-only* battery device (no buttons/touch, content pushed on a schedule): **yes** — provision once over BLE, then wake→associate→pull/receive→sleep with no BLE cost at all, and the §2.3 fallback covers cred rot. For a battery device *with* input events: **no** — per §5 the passive advert is irreplaceable; that device should stay dual-transport (the Proposal's device), using BLE for adverts/events and WiFi for bulk.

---

## 7. Protocol / library / firmware deltas (beyond the Proposal)

Everything below is *additive to* the Proposal's Phase 0–3; nothing conflicts with it.

**Protocol (`opendisplay-protocol`), minor additions to the planned SECTION 9:**
1. **Define the WiFi-only comm-modes combination normatively:** `communication_modes` with bit 0 (`OD_COMM_MODE_BLE`) clear and bit 2 (`OD_COMM_MODE_WIFI`) set = *WiFi-only steady state*, with the mandatory rider: **BLE MUST still be brought up when the device is unprovisioned or WiFi association persistently fails** (§2.3 fallback). No new bit needed — the existing pair encodes it; the semantics just need writing down (today bit 0 is dead letter, `config_parser.cpp:640`).
2. **`CMD_NET_CAPS` (0x0057)** gains a "BLE posture" field: BLE active / provisioning-only (currently torn down) / disabled-by-build.
3. **mDNS:** the `cm` TXT (already in §3.6.2) is sufficient for HA to recognize WiFi-only (`cm=04`). Optionally add `prov=1` while in fallback-provisioning mode so tooling can distinguish "healthy WiFi-only" from "WiFi-only in distress, BLE re-enabled."
4. **(For mains event telemetry, later)** a small device→host push contract: unsolicited MSD-changed frame over an established LAN session (mechanically already possible via `sendResponse` → `send_wifi_lan_frame`, `communication.cpp:78,141`; needs an opcode + subscribe semantics).

**Firmware (`Firmware`, ESP32 envs):**
- Gate `ble_init()` on the §2.3 state machine (today unconditional, `main.cpp:117`).
- `bleStackUp` guard in `updatemsdata()`'s ESP32 branch (`display_service.cpp:1758`) — required by both variants.
- Teardown-on-join-success + fallback-re-init (§2.3), sequenced after F3 (`NET_JOIN/STATUS`) so provisioning is confirmable before teardown.
- Cache the BT MAC (via `esp_read_mac(ESP_MAC_BT)` or stack read at provisioning) so the `mac` TXT (F1) survives BLE deinit (§4.1).
- Battery-posture wake-path rework: associate on wake without waiting for a BLE connect (`main.cpp:124,303-308`).
- Optional `-DOPENDISPLAY_ENABLE_BLE` build axis for the variant-A RAM-reclaim build (mirrors the proposed `-DOPENDISPLAY_ENABLE_WIFI`).

**py-opendisplay:** nothing beyond P1–P7 except: `provision_wifi()` (P5) should optionally clear comm-modes bit 0 when the caller requests WiFi-only, and `discovery_ip` (P6) should surface `cm`/`prov` so callers can tell a WiFi-only device from a dual-transport one.

**HA integration:** beyond H1–H6, the two real items are (a) the **non-BLE coordinator/availability source** (§4.3 — the largest single HA delta for WiFi-only support), and (b) the `format_mac` migration (§3.6.6 / H2) promoted from "recommended" to **prerequisite** for any WiFi-first onboarding.

---

## 8. Recommendation

1. **Adopt variant B as the "WiFi display" product posture** for mains-powered devices: BLE for bootstrap + automatic distress-fallback, WiFi-only in steady state. It is a small, low-risk extension of the existing Proposal — sequence it as **Phase 2.5** (after F3/H5 provisioning feedback, before or alongside Phase 3), comprising the §2.3 state machine, the `updatemsdata` guard, the cached-MAC TXT, and the comm-modes semantics (§7.1).
2. **Do not productize variant A.** Its only tangible benefit (NimBLE RAM + coex removal) is real but obtainable in variant B's steady state, while its unique costs (SoftAP bootstrap subsystem, no out-of-band recovery) buy nothing on hardware that always has a BLE radio. Keep it only as an optional `-DOPENDISPLAY_ENABLE_BLE=0` build axis, primarily as the escape hatch for the C6-N4 + TLS RAM corner (RAM doc verdict).
3. **Land the identity prerequisites first:** the §3.6.6 `format_mac` migration and the REQUIRED `mac` TXT (F1) precede any WiFi-first onboarding; without them a mixed fleet silently forks devices.
4. **Scope battery honestly:** WiFi-only battery is viable *only* for display-only, low-cadence devices (variant B), and the wake path must be reworked first. Battery devices with buttons/touch should remain dual-transport — the passive BLE advert is architecturally irreplaceable for low-latency events (§5).
5. **For mains event telemetry**, plan the small MSD-push-over-LAN protocol addition (§7.4); until then, TXT-watch or `CMD_READ_MSD` polling is an acceptable interim.

*End of report.*
