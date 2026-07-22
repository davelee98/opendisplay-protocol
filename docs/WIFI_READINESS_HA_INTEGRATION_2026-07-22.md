# WiFi Transport Readiness — Home Assistant Integration Audit

**Date:** 2026-07-22
**Scope:** `Home_Assistant_Integration/custom_components/opendisplay/` (HACS custom component `opendisplay`, `iot_class: local_push`), audited statically for adding **WiFi as a transport between Home Assistant and the display**, with **BLE and WiFi simultaneously active on one device**.
**Method:** Report-only static reading. No HA instance was run; no source was modified. Cross-referenced against `py-opendisplay`, `Firmware` (ESP32), and the reference-only HA core checkout at `/home/davelee/opendisplay/core`.

---

## Executive summary

- **The integration itself has zero WiFi/IP/network transport code.** Every device interaction goes over HA's Bluetooth stack. The only network I/O in the component is HTTP *downloads* of images/firmware from the internet (`services.py`, `update.py`), not a transport to the tag. This is the expected state and is confirmed below.
- **The ecosystem transport, however, already exists in firmware and in the wire model.** The ESP32 firmware (`Firmware/src/wifi_service.cpp`) implements a **TCP server** that speaks the *same* frame protocol as BLE, and advertises itself over **mDNS as `_opendisplay._tcp.local.`** with a `msd` TXT record carrying the same 14-byte manufacturer-specific data as the BLE advertisement. `py-opendisplay` already models the on-wire credentials (`WifiConfig`, TLV `0x26`) and the capability bit (`communication_modes` bit 2, `OD_COMM_MODE_WIFI`). The transport direction is **HA-as-TCP-client, tag-as-TCP-server**, discovered by zeroconf.
- **What is missing is entirely on the HA + py-opendisplay side:** (1) py-opendisplay has no TCP transport class (its `transport/` package is BLE-only); (2) the manifest has no `zeroconf` block; (3) the config flow has no zeroconf step; (4) there is no transport-selection/failover logic in the delivery path; (5) there is no unification of a BLE-discovered and a WiFi-discovered device into one config entry; (6) no WiFi-credential provisioning flow; (7) no WiFi/IP diagnostics or entities.
- **The single hardest design problem is identity unification:** today `unique_id == BLE MAC`. The mDNS record does *not* currently expose that MAC directly — it exposes a 3-byte chip id in the instance name (`OD<chipIdHex>`) and the 14-byte MSD as a TXT record. ESPHome is the canonical precedent for solving exactly this (WiFi MAC ↔ BLE MAC), and the cleanest fix is a small firmware change to publish the full MAC as a mDNS TXT record.
- **Headline recommendation:** treat WiFi as a *second transport under the existing MAC-keyed device identity*, not a second device. Add a `mac` TXT to the firmware mDNS advert, add a TCP transport to py-opendisplay behind the existing `OpenDisplayDevice` API, add a `zeroconf` manifest block + `async_step_zeroconf` that reconciles onto the existing BLE `unique_id`, store `host`/`port` in `entry.data`, and add a transport-preference selector in the delivery/service connect paths that prefers WiFi (4096-byte frames vs BLE's 244) and falls back to BLE.

---

## What exists today (network-related code catalog)

Grep for `wifi|zeroconf|mdns|dhcp|http|tcp|ip|host|ssid` across `custom_components/opendisplay/` returns **no device-transport networking**. The only matches are:

| File | Match | What it is |
|---|---|---|
| `manifest.json:12` | `"dependencies": ["bluetooth_adapters", "http", "recorder"]` | `http` is a dep for the HA-hosted image signing/serving, not tag transport. |
| `manifest.json:3-6` | `"bluetooth": [{ "connectable": true, "manufacturer_id": 9286 }]` | The **only** discovery matcher today — BLE manufacturer id 9286. |
| `services.py:14,42,55,278-298` | `aiohttp`, `async_get_clientsession`, `async_sign_path`, `get_url` | Downloads an image *from* a URL / media source into HA, then sends it to the tag **over BLE**. Inbound HTTP only. |
| `update.py:10,52-53,164,346-366` | `aiohttp`, GitHub release API, firmware `.gbl`/`.bin` download | OTA firmware **download** from GitHub; the flash to the tag is still BLE. |
| `diagnostics.py:11` | `TO_REDACT = {"ssid", "password", "server_url"}` | **Telling:** diagnostics already redacts WiFi-credential fields because `runtime.device_config` can contain a parsed `WifiConfig` (0x26). The redaction is defensive/forward-looking; no code sets these today. |
| `delivery.py:16` | `"a future WiFi presence tracker could too"` | The delivery trigger `notify_device_seen(source)` was **deliberately written transport-agnostic** (`source: str`), anticipating WiFi. |

**Conclusion for catalog item 1:** There is no existing WiFi/IP transport code in the integration. Confirmed. The only forward-looking hooks are the transport-agnostic delivery trigger and the diagnostics redaction set.

### Ground truth in py-opendisplay (what the library gives us)

- `py-opendisplay/src/opendisplay/transport/` contains **only `connection.py` → `BLEConnection`** (bleak / `bleak_retry_connector`). There is **no TCP/IP transport**. `transport/__init__.py` exports `BLEConnection` alone.
- `WifiConfig` **is** modeled: `models/config.py:673` (TLV `0x26`, 160 bytes) with `ssid`, `password`, `encryption_type`, `server_url` (64-byte), `server_port` (uint16 BE, default **2446**), plus `from_strings(...)` and `to_bytes()`. Parsed in `protocol/config_parser.py` (`_parse_wifi_config`, legacy 65-byte + current 160-byte forms) and serialized in `protocol/config_serializer.py` (`serialize_wifi_config`, emitted as part of the full `GlobalConfig` packet when `config.wifi_config is not None`).
- `WifiEncryption` enum: `models/enums.py:194` (NONE/WEP/WPA/WPA2/WPA3).
- `OpenDisplayDevice.write_config(config)` exists (`device.py:1428`) — meaning **HA can already push a WiFi config to a tag over BLE today** by writing a `GlobalConfig` whose `wifi_config` is set and whose `system_config.communication_modes` has bit 2 on. The provisioning primitive exists at the library level; it is simply not surfaced by the integration.
- The tag's advertised identity is a 14-byte MSD parsed by `parse_advertisement` (`coordinator.py:118`), manufacturer id `MANUFACTURER_ID` (9286).

### Ground truth in firmware (the transport that HA must speak to)

`Firmware/src/wifi_service.cpp` (guarded `#ifdef TARGET_ESP32` — **ESP32 only**; nRF/Silabs firmware have no WiFi):

- **Tag is a TCP server.** `wifiServer.begin(wifiServerPort)` (`:151`), `wifiServer.accept()` (`:187`). Default `wifiServerPort = 2446` (`main.h:147`), overridable from `WifiConfig.server_port` (`config_parser.cpp:519-524`).
- **Framing over TCP = `[uint16 LE length][payload]`** (`:229-246`), and the payload is handed to **`imageDataWritten(...)` — the exact same handler as BLE GATT writes** (`:239`). So the TCP transport reuses the identical protocol frames; the only difference from BLE is the 2-byte length prefix instead of GATT MTU chunking, and a much larger `WIFI_LAN_MAX_PAYLOAD = 4096` (`:17`) vs HA's 244-byte BLE ceiling.
- **mDNS discovery.** `MDNS.begin("OD" + getChipIdHex())` → hostname `OD<chipIdHex>.local` (`:74-79`); `MDNS.addService("opendisplay", "tcp", wifiServerPort)` → **`_opendisplay._tcp.local.`** (`:80`); and a **`msd` TXT record** set to the hex of the 14-byte MSD (`opendisplay_mdns_update_msd_txt`, `:51-71`) — the same MSD the BLE advert carries.
- **Simultaneous BLE+WiFi is a bitmask, not a mode switch.** `system_config.communication_modes` is a bitfield (`opendisplay_structs.h:406-423`): `OD_COMM_MODE_BLE`, `OD_COMM_MODE_OEPL`, `OD_COMM_MODE_WIFI = (1u<<2)`. WiFi is gated on **both** bit 2 set **and** a `0x26` TLV with a non-empty SSID (`wifi_service.cpp:88-104`). BLE and WiFi coexisting is therefore the intended, natively-supported state — matching the task's requirement.
- **Identity link:** `getChipIdHex()` (`encryption.cpp:725`) on ESP32 = `(EfuseMac >> 24) & 0xFFFFFF` — the top 3 bytes of the base MAC. The mDNS instance name embeds only 3 MAC bytes; the full MAC is **not** currently in a TXT record. The `msd` TXT is the only full cross-transport correlation token today.

---

## Current architecture (BLE-only, for reference)

**Discovery / config flow (`config_flow.py`):**
- `async_step_bluetooth(discovery_info)` (`:173`) — BLE manufacturer-id match → `async_set_unique_id(discovery_info.address)` (the MAC) → `_abort_if_unique_id_configured()` → confirm → `_async_test_connection` (BLE connect + `read_firmware_version`).
- `async_step_user` (`:216`) — manual pick from `async_discovered_service_info` filtered by `MANUFACTURER_ID`.
- `async_step_encryption_key` / `async_step_reauth_confirm` — 32-hex-char device key.
- **`unique_id` is the BLE MAC address, everywhere.** `entry.data` holds only `encryption_key` and `cached_state`; there is **no `host`/`port`**.
- Options flow (`OpenDisplayOptionsFlow`, `:373`) — deep-sleep tuning + `blocks_per_ack`/`max_queue_size`. No transport options.

**Device/entity model (`__init__.py`):**
- One config entry per tag. `OpenDisplayRuntimeData` (`:64`) holds `coordinator`, `firmware`, `device_config` (`GlobalConfig`), `is_flex`, `sleep_profile`, a process-global per-MAC `ble_lock`, `partial_state`, and the `DeliveryManager`.
- Device registry entry keyed by `connections={(CONNECTION_BLUETOOTH, address)}` (`:297-307`). **The device identity in the registry is a Bluetooth connection tuple** — this is the second place (besides `unique_id`) that hardcodes BLE identity.
- Capabilities are learned by **live BLE interrogation** (`OpenDisplayDevice.interrogate` → `device.config`), cached into `entry.data[cached_state]` (`_write_cache`, `:124`), and restored for setup-without-connect when a sleepy device is dark (`_cache_setup_if_sleepy`).

**Coordinator (`coordinator.py`):** `PassiveBluetoothDataUpdateCoordinator` — passive BLE advert listener. Parses adverts (RSSI, battery, temperature, touch/button, reboot flag), fans out `device_seen` and `reboot` callbacks. **Purely BLE**; a WiFi presence signal would need a parallel source.

**Delivery pipeline (`delivery.py` + `services.py`):**
- `services._async_send_image` renders (odl-renderer) → `prepare_image` (CPU, executor) → either **live send** via `_async_connect_and_run` or **queue** via `DeliveryManager.submit_upload` for sleepy/dark devices.
- `_async_connect_and_run` (`services.py:321`) is the single live-connect chokepoint: resolves the `BLEDevice`, takes the per-MAC `ble_connection` lock, opens `OpenDisplayDevice(mac_address, ble_device=…)`, runs the action, bounded by `DELIVERY_DEADLINE_S = 600 s`.
- `DeliveryManager._drain_once` (`delivery.py:303`) is the queued-delivery equivalent: waits for a wake advert (`notify_device_seen`), then the same BLE-locked `OpenDisplayDevice` connect + drain. Retry/queueing: latest-wins, `MAX_DELIVERY_ATTEMPTS = 5`, expiry timer.
- **Every connect path constructs `OpenDisplayDevice` with a `ble_device=` kwarg.** This is the fourth and most pervasive BLE-hardcoding.
- `ble_lock.py` — process-global per-MAC `asyncio.Lock` serializing the tag's single BLE link.

**Diagnostics (`diagnostics.py`):** dumps firmware, sleep profile, delivery slot state, and the redacted `device_config`. **RSSI** is exposed as a sensor (`sensor.py:65`, `SIGNAL_STRENGTH_DECIBELS_MILLIWATT`) sourced from the BLE advert.

---

## HA precedents for dual-transport / network discovery

### Zeroconf manifest matcher (the discovery entry point)
A `zeroconf` key in `manifest.json` subscribes the integration to mDNS service types. Examples from the core checkout:
- `esphome/manifest.json`: `"zeroconf": ["_esphomelib._tcp.local."]`
- `wled/manifest.json`: `"zeroconf": ["_wled._tcp.local."]`

Matchers can also filter on TXT-record properties (e.g. `{"type": "_x._tcp.local.", "properties": {"md": "shelly*"}}`). For OpenDisplay the service type is already published by firmware: **`_opendisplay._tcp.local.`**.

### ESPHome — the canonical BLE + WiFi + MAC-identity precedent
ESPHome is the closest architectural analog (a device reachable over WiFi *and* participating in Bluetooth), and its config flow shows exactly how to unify transports under one MAC identity — `core/homeassistant/components/esphome/config_flow.py`:
- `async_step_zeroconf` reads **`discovery_info.properties.get("mac")`** (a TXT record), `format_mac()`-normalizes it, and calls **`async_set_unique_id(mac)`** — i.e. the WiFi discovery lands on the *same* `unique_id` space as everything else (`config_flow.py:319-344`).
- It bridges WiFi and BLE identities explicitly with **`wifi_mac_to_bluetooth_mac(mac_address)`** (`:354`) — on ESP32 the BLE MAC is a fixed offset from the base/WiFi MAC. This is precisely the BLE↔WiFi MAC relationship OpenDisplay needs.
- `_async_validate_mac_abort_configured` (`:369`) updates `host`/`port` on the *existing* entry when a device is rediscovered at a new address, rather than creating a duplicate.
- Manifest declares both `"dependencies": ["bluetooth", …]` and `"zeroconf"` / `"dhcp"` blocks, `iot_class: local_push` — the same class OpenDisplay already uses.

**Key takeaways to copy:** (1) put the MAC in a mDNS TXT record; (2) `async_set_unique_id(mac)` from the zeroconf step so BLE and WiFi collapse onto one entry via `_abort_if_unique_id_configured`; (3) store/refresh `host`+`port` in `entry.data` and *update* the existing entry on rediscovery; (4) keep `bluetooth` and `zeroconf` both listed as discovery sources.

### DHCP discovery (secondary)
`esphome/manifest.json` also carries `"dhcp": [{"registered_devices": true}]`, which re-discovers already-registered devices when their IP changes. Useful as a fallback for OpenDisplay to track IP changes if mDNS is flaky, but mDNS/zeroconf is the primary path since the firmware already advertises it.

---

## Gap analysis

| # | Gap | Where it bites | Severity |
|---|---|---|---|
| G1 | **No TCP transport in py-opendisplay.** `transport/` is BLE-only; `OpenDisplayDevice` takes only `ble_device=`. | Blocks everything; a `TCPConnection` speaking `[len16LE][frame]` (mirroring `wifi_service.cpp`) must be added and selectable in the device constructor. | **Blocker** |
| G2 | **No `zeroconf` block in `manifest.json`.** Only the `bluetooth` matcher exists. | No WiFi discovery at all. Add `"zeroconf": ["_opendisplay._tcp.local."]` and `"dependencies": [… "zeroconf"]` (or `after_dependencies`). | High |
| G3 | **No `async_step_zeroconf` in the config flow.** | A discovered tag can't be onboarded over WiFi, and a WiFi rediscovery of a BLE-configured tag can't update it. | High |
| G4 | **Identity unification.** `unique_id == BLE MAC`; the mDNS advert exposes only a 3-byte chip id + a `msd` TXT, **not the full MAC**. `wifi_service.cpp` has no `mac` TXT. | Without the full MAC (or a reliable MSD→MAC map), HA cannot collapse the WiFi discovery onto the existing BLE entry → duplicate devices. | **High / firmware dependency** |
| G5 | **Device registry keyed on `(CONNECTION_BLUETOOTH, address)` only** (`__init__.py:299`). | A WiFi-only-reachable device still needs this connection tuple for service resolution (`services.py:_get_entry_for_device`). Keep the tuple as identity, add `configuration_url`/`host` as attributes — do **not** switch identity to IP. | Medium |
| G6 | **No transport selection / failover in the delivery path.** `_async_connect_and_run` and `_drain_once` hardcode `async_ble_device_from_address` + `ble_connection` + `ble_device=`. | The core behavioral change: choose WiFi vs BLE per attempt, prefer WiFi when reachable, fall back to BLE. | High |
| G7 | **`ble_lock` is per-MAC and BLE-specific.** | A WiFi upload and a BLE LED flash to the same tag can now genuinely run concurrently (two independent links). Decide whether to keep a single per-MAC lock (serialize everything, simplest, safe for firmware that shares one session) or a per-transport lock. Firmware `clearEncryptionSession()` on new client suggests **the tag still has one logical session** → keep one per-MAC lock. | Medium |
| G8 | **No WiFi provisioning flow.** The primitive exists (`write_config` + `WifiConfig.from_strings` + comm-modes bit 2) but nothing surfaces it. | User must push SSID/password/port to the tag over BLE before WiFi works. Needs either a config-flow step or a service. | Medium |
| G9 | **No WiFi diagnostics/entities.** RSSI sensor is BLE-only; no IP-address entity, no "active transport" indicator, no WiFi signal. | Observability of the new transport. | Low |
| G10 | **Version pins.** `manifest.json` pins `py-opendisplay[silabs-ota]==7.12.0`. | A TCP-transport-capable py-opendisplay release must be cut and the pin bumped (and `epaper-dithering` chain per CLAUDE.md coupling). | Low (process) |
| G11 | **Presence/coordinator is BLE-only.** `notify_device_seen("ble")` already accepts a source; but there is no WiFi liveness signal (mDNS TTL / TCP reachability) feeding it. | Queued delivery to a sleepy-but-WiFi device won't trigger. Note: WiFi-capable ESP32 tags are typically USB/mains-powered and *not* deep-sleeping, so this is lower priority — WiFi tags are usually always-reachable. | Low |
| G12 | **Coexistence semantics undefined.** Firmware replaces the TCP client on a new connection and `clearEncryptionSession()`; a BLE auth session and a TCP session share firmware state. | Must not open BLE + TCP to the same tag concurrently. Reinforces G7's single per-MAC lock. | Medium |

---

## Integration implementation sketch

Design principle: **WiFi is a second transport under the existing MAC-keyed identity, chosen per-connect.** One config entry, one device, one `unique_id` (BLE MAC). No new "WiFi device" concept.

### 0. Firmware prerequisite (small, unblocks G4)
Add a `mac` TXT record to the mDNS advert in `wifi_service.cpp` (`restartLanService`) carrying the full BLE MAC (lowercase, colon-free), mirroring ESPHome. Alternatively, HA correlates via the existing 14-byte `msd` TXT against the BLE MSD, but a `mac` TXT is far more robust and is the ESPHome-proven path. This is the one cross-repo change gating clean unification.

### 1. py-opendisplay: add a TCP transport (G1, blocker)
- New `transport/tcp.py` → `TCPConnection` using `asyncio.open_connection(host, port)`, framing writes as `struct.pack("<H", len) + frame` and reading the same, capped at 4096 (`WIFI_LAN_MAX_PAYLOAD`). Reuse the existing frame/crypto layers unchanged (the firmware calls the identical `imageDataWritten`).
- Extend `OpenDisplayDevice.__init__` to accept a transport selector, e.g. `transport="ble"|"tcp"` with `host`/`port` for TCP, keeping `ble_device=` for BLE. All higher-level methods (`upload_prepared_image`, `write_config`, `activate_led`, …) become transport-agnostic.
- Cut a release; bump the manifest pin (G10).

### 2. `manifest.json` (G2)
```jsonc
"bluetooth": [{ "connectable": true, "manufacturer_id": 9286 }],
"zeroconf": ["_opendisplay._tcp.local."],
"dependencies": ["bluetooth_adapters", "http", "recorder", "zeroconf"],
```

### 3. `config_flow.py` — `async_step_zeroconf` (G3, G4)
```python
async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo):
    mac = discovery_info.properties.get("mac")          # firmware TXT (step 0)
    if mac is None:
        return self.async_abort(reason="mdns_missing_mac")
    mac = format_mac(mac)
    await self.async_set_unique_id(mac)
    host = discovery_info.host
    port = discovery_info.port or 2446
    # If a BLE-configured entry already exists, update its host/port and abort.
    self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
    self._wifi = (host, port)
    return await self.async_step_zeroconf_confirm()
```
- On confirm, probe over TCP (`_async_test_connection` gains a transport arg) and `async_create_entry(data={CONF_HOST: host, CONF_PORT: port, …})`.
- Because `unique_id` is the MAC, a tag already onboarded via BLE simply gets `host`/`port` *added* to its existing entry (the `updates=` path) — **one device, two transports**. Conversely a WiFi-first onboarding later gets its BLE advert matched to the same `unique_id` and deduped by `_abort_if_unique_id_configured`.
- Store transport reachability in `entry.data`: `CONF_HOST`, `CONF_PORT` (absent ⇒ BLE-only, preserving migration).

### 4. Delivery/service transport selection (G6, G7, G12)
Introduce a small transport resolver used by **both** `services._async_connect_and_run` and `delivery._drain_once`:
```python
def _select_transport(hass, entry):
    host = entry.data.get(CONF_HOST)
    if host and _tcp_reachable(host):        # cheap: mDNS-seen recently / last OK
        return ("tcp", host, entry.data[CONF_PORT])
    ble = async_ble_device_from_address(hass, address, connectable=True)
    if ble is not None:
        return ("ble", ble, None)
    return None            # neither → queue (existing sleepy path)
```
- Prefer TCP (4096-byte frames, no wake window, no MTU cap) → fall back to BLE → fall back to queue.
- **Keep the single per-MAC `ble_lock`, rename it conceptually to a per-MAC *device* lock**, and hold it for *either* transport so a BLE op and a TCP op never overlap on one tag (firmware shares one logical/crypto session — `clearEncryptionSession()` on new client, G12). No new lock type needed; just widen its usage in `services.py`/`delivery.py`.
- On a WiFi attempt that fails mid-transfer, record the failure and re-run the resolver (which now returns BLE) on the next attempt — the existing `MAX_DELIVERY_ATTEMPTS` retry loop already provides the retry cadence.

### 5. Provisioning flow (G8)
Add a **service** `opendisplay.configure_wifi` (device_id, ssid, password, encryption, optional port) that:
- resolves the entry, connects **over BLE**, reads current `GlobalConfig`, sets `wifi_config = WifiConfig.from_strings(...)` and `system_config.communication_modes |= OD_COMM_MODE_WIFI`, calls `device.write_config(...)`, then requests a config resync.
- A service (rather than a config-flow step) is preferred because provisioning is a *re-configuration* of an already-onboarded BLE device, and it composes with the existing per-MAC lock and options model. Redact ssid/password in logs (diagnostics already redacts them).

### 6. Entities & diagnostics (G9)
- Add an **IP address** diagnostic sensor (from `entry.data[CONF_HOST]` / last mDNS host) and an **"active transport"** attribute or sensor (`ble` / `wifi`).
- Extend `diagnostics.py` to report `host`, `port`, `communication_modes`, and which transport last succeeded. `TO_REDACT` already covers `ssid`/`password`/`server_url`.
- Keep the BLE RSSI sensor; WiFi tags simply won't advertise BLE RSSI when out of BLE range, which is acceptable (entity goes unknown).

### 7. Migration story
- **Existing BLE-only entries need no migration.** They have no `CONF_HOST`; `_select_transport` returns BLE; behavior is byte-for-byte unchanged. WiFi becomes available for them the moment (a) the user provisions credentials via the new service and (b) the tag's mDNS advert is discovered and its `host`/`port` merged into the existing entry via `async_step_zeroconf`'s `updates=` path.
- No `entry.version` bump is required (additive `entry.data` keys), though bumping the config-entry version and adding an `async_migrate_entry` no-op is a cheap safety net if the `host`/`port` schema later hardens.
- The device registry keeps `(CONNECTION_BLUETOOTH, mac)` as identity (G5); `configuration_url` can point at the tag's `http://<host>` landing page when a host is known.

---

## Appendix — key file references

- Integration: `custom_components/opendisplay/manifest.json`, `config_flow.py:173-272` (BLE steps), `__init__.py:297-317` (device registry + runtime), `services.py:321-451` (`_async_connect_and_run`), `delivery.py:303-341` (`_drain_once`), `coordinator.py` (passive BLE), `ble_lock.py`, `diagnostics.py:11`, `sensor.py:65` (RSSI).
- py-opendisplay: `transport/connection.py` (BLE-only), `models/config.py:673` (`WifiConfig`), `models/enums.py:194` (`WifiEncryption`), `protocol/config_parser.py:525` / `config_serializer.py:541`, `device.py:1428` (`write_config`).
- Firmware (ESP32): `Firmware/src/wifi_service.cpp` (TCP server + mDNS `_opendisplay._tcp` + `msd` TXT), `include/opendisplay_structs.h:406-423` (`communication_modes` bitfield), `src/main.h:47-50,147` (`COMM_MODE_WIFI`, default port 2446), `src/config_parser.cpp:519-524` (server_port wiring), `src/encryption.cpp:725` (`getChipIdHex`).
- HA reference: `core/homeassistant/components/esphome/{manifest.json, config_flow.py:319-421}` (zeroconf→MAC unique_id, `wifi_mac_to_bluetooth_mac`), `core/homeassistant/components/wled/manifest.json` (zeroconf matcher shape).
