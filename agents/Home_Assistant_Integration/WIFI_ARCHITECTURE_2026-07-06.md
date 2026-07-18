# OpenDisplay WiFi Mode — Architecture Findings

*Survey of the workspace repos (Firmware, py-opendisplay, Home_Assistant_Integration, opendisplay.org) — 2026-07-06*

## TL;DR

- **There is no OpenDisplay server inside Home Assistant today.** The HA integration is BLE-only.
- In the WiFi architecture the reference firmware actually implements, the **"server" lives on the device itself**: the display joins the AP, listens on TCP port 2446, and advertises itself over mDNS. HA/py-opendisplay would be the TCP *client* — but neither has a TCP transport yet.
- A second, inverted "pull-based" model (display connects out to an *OpenDisplay Server* that could be hosted in HA) exists in the Basic standard's WiFi appendix, but **nothing in the workspace implements it**.

## Two WiFi models in the protocol docs

The docs on opendisplay.org describe two different architectures:

### 1. LAN transport — device is the TCP server (implemented)

Documented in `opendisplay.org/httpdocs/protocol/ble-flow.html`, section "LAN (Wi‑Fi) transport". This is what the reference ESP32 firmware ships.

- The device joins the access point as a station and **listens as a TCP server on port 2446**.
- It advertises via mDNS:
  - Hostname: `OD<chipid>.local`
  - Service type: `_opendisplay._tcp`
  - TXT record `msd`: 28 lowercase hex chars (14 bytes) derived from the manufacturer-specific data, for disambiguating multiple devices on one network.
- The client browses mDNS, connects, and sends **exactly the same protocol bytes as BLE characteristic writes**, framed as:

  ```
  [payload_length: 2 bytes, little-endian][payload: 1–4096 bytes]
  ```

- Partial frames are buffered until complete; multiple frames may arrive in one socket read.
- One client session at a time — a new TCP connection replaces the previous one.
- Encryption sessions follow the same rules as BLE.

### 2. Pull-based model — device is the TCP client (documented only)

The Basic standard's WiFi appendix (`opendisplay.org/httpdocs/protocol/basic-standard.html`, "WiFi Communication") inverts the roles:

- The **display acts as a TCP client** and connects out to an "OpenDisplay Server".
- The server advertises `_opendisplay._tcp.local.` via mDNS (e.g. `OpenDisplay Server._opendisplay._tcp.local.`), default port 2446, with an optional `ip` TXT record as a resolution workaround.
- This is the only model where a server component would live *inside* Home Assistant — and no repo in the workspace implements it (neither the server side nor the device side).

## Firmware (ESP32 only)

The entire implementation is `Firmware/src/wifi_service.cpp`, gated by `#ifdef TARGET_ESP32`. The Silabs/nRF firmware (`Firmware_Silabs`) has no WiFi.

**Enablement** (`initWiFi()`, `wifi_service.cpp:85-158`):
- Requires bit 2 of `communication_modes` in system config (`COMM_MODE_WIFI`) **and** a TLV packet type 0x26 (`wifi_config`) with a non-empty SSID. Credentials (SSID, password, encryption type) are provisioned over BLE once; the device joins the AP after reboot.
- On association it starts the TCP listener on `wifiServerPort` (default 2446) and registers mDNS (`restartLanService()`).

**Data path** (`handleWiFiServer()`, `wifi_service.cpp:170-247`):
- Accepts one client (new client replaces the previous, clearing the encryption session).
- Buffers the TCP stream into an 8 KB `tcpReceiveBuffer`, deframes the 2-byte length-prefixed frames, and hands each payload to `imageDataWritten()` — **the same handler the BLE write callback uses**, invoked with NULL connection handles.
- Responses return via `send_wifi_lan_frame()` in `communication.cpp`, and are mirrored to BLE only when a central is also connected.

**Legacy wrinkle:** `config_parser.cpp:449-503` still parses `server_url` / `server_port` out of the wifi_config packet's reserved bytes — fields from the pull-based model — but `wifiClient.connect()` is never called outbound. The parsed URL is currently dead weight.

**Known issues** (from `Firmware/FINDINGS.md`):
- **M11** — `setup()` blocks up to ~34 s on WiFi connect (`initWiFi()` defaults to `waitForConnection = true`; the async path in `handleWiFiServer()` already handles late association).
- **M12** — WiFi radio started on every deep-sleep wake but never serviced in the advertising-window branch.
- **M21** — non-atomic read-modify-write of shared touch/WiFi flags.
- **L28** — LAN frames up to 4096 B feed handlers that BLE caps at 512 B (framing math correct; downstream tolerance unverified).
- Clear-config over BLE does not wipe WiFi credentials or the AES key until reboot (`config_parser.cpp:141` finding).

## py-opendisplay

- `transport/connection.py` is **BLE-only** (Bleak + bleak-retry-connector). There is no TCP/LAN transport class.
- What exists is the **provisioning side**: `WifiConfig` in `models/config.py` (TLV 0x26 — SSID, password, encryption type, plus the pull-model `server_url` / `server_port` fields defaulting to 2446), with serializer/parser support, so a client can write WiFi credentials to a device over BLE.
- The repo is checked out on a `feature/wifi` branch, but it has **no commits beyond main** — the TCP client transport appears planned but not started.

## Home Assistant integration

Zero WiFi surface:

- `manifest.json` declares only Bluetooth discovery (manufacturer ID 9286 / 0x2446), depends on `bluetooth_adapters`, and has **no `zeroconf` matcher**. `iot_class` is `local_push` (BLE advertisements). The `http` dependency is unrelated to device transport.
- Grepping the whole integration for wifi/tcp/lan/zeroconf finds nothing. The coordinator listens to HA's bluetooth advertisement callbacks; all image/config/service traffic goes through py-opendisplay's BLE connection.

## What WiFi support in HA would actually take

Under the current (LAN-transport) architecture the work is **client-side, not server-side**:

1. **py-opendisplay**: add a TCP transport speaking the length-prefixed framing, presenting the same interface as `BLEConnection` so `device.py` works over either path.
2. **HA integration**: add a `zeroconf: _opendisplay._tcp.local.` matcher to `manifest.json` so HA's shared zeroconf instance discovers devices and routes them into the config flow; use the mDNS `msd` TXT record to match discoveries to known devices.
3. No server needs to be hosted inside HA at all — that would only be required if the Basic standard's pull model were adopted, which the reference firmware doesn't speak.
