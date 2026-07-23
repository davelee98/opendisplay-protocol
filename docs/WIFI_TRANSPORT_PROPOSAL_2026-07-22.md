# WiFi Transport — Cross-Repo Implementation Proposal

**Date:** 2026-07-22
**Status:** Proposal (report-only task; no code changed)
**Companion audits (same date, same docs/ folder):**
- `WIFI_READINESS_PROTOCOL_2026-07-22.md` — wire-protocol audit
- `WIFI_READINESS_FIRMWARE_2026-07-22.md` — Firmware (ESP32 + confirming pass over NRF54/Silabs/NRF)
- `WIFI_READINESS_PY_OPENDISPLAY_2026-07-22.md` — py-opendisplay library audit
- `WIFI_READINESS_HA_INTEGRATION_2026-07-22.md` — Home Assistant integration audit

**Goal:** WiFi as a full transport between Home Assistant and OpenDisplay tags. **Both transports are available (listening) at once, but the device services only one client at a time** — there is no concurrent-session requirement (see §8 D5).

### Design principles

These constrain every change below:

1. **Minimize firmware change.** The device already ships a working WiFi transport, so prefer **reusing existing mechanisms** over new subsystems — the shipped LAN server, `encryption_enabled`, the `0x26` credential path, the DIRECT_WRITE image path, and the existing session/eviction logic. Each firmware item (F1–F6) is scoped to the smallest change that works; **new behavior lands host-side (py-opendisplay + HA) wherever it possibly can.** Firmware is the highest-cost, hardest-to-update tier (four repos, per-target toolchains, field devices), so it changes least.
2. **Perfect backward compatibility.** Existing deployments must keep working **byte-for-byte**: never renumber or alter a shipped opcode or struct (the 0x52/0x53 lesson); old firmware silently drops unknown opcodes; old clients ignore new mDNS TXT keys and the new TLS port; an encryption-off tag keeps serving plaintext on 2446 exactly as today. All protocol work is additive ⇒ MINOR bump only. The **one** behavior *removed* — cross-transport response fan-out (dual-delivery) — cannot affect any *correct* client, since no legitimate client relies on receiving responses to commands it never issued; it is a bug fix, not a break (§8 D5).
3. **WiFi is additive to a BLE-capable base** (§1.6 / §8 D1); **one client at a time, request+response over the same pipe, disconnect aborts work** (§8 D5).

---

## 1. Where we actually are (synthesis of the four audits)

The surprise headline from all four audits: **WiFi is not green-field.** The device end of the transport already ships and works:

| Layer | State today |
|---|---|
| **Firmware (ESP32 S3/C3/C6)** | **Working.** `wifi_service.cpp`: WiFi STA + mDNS `_opendisplay._tcp` (TXT `msd`) + single-client TCP server on port **2446**. Inbound `[len:2 LE][payload≤4096]` frames feed the *same* `imageDataWritten()` dispatcher as BLE; responses fan out to both transports. **BLE (NimBLE) + WiFi already run concurrently** in `loop()`. Credentials arrive over BLE via the `wifi_config` TLV `0x26`, gated by `communication_modes` bit 2 (`OD_COMM_MODE_WIFI`). |
| **Other firmwares (NRF54/Silabs/NRF)** | No WiFi hardware; correctly parse-and-skip `0x26`. Out of scope permanently. |
| **Protocol header** | **Silent.** `opendisplay_protocol.h` (v2.1) contains zero WiFi/TCP/mDNS spec — only the `0x26 wifi` config-type comment line. The LAN framing, port, discovery, and session rules exist only as firmware behavior + agents notes. |
| **py-opendisplay (7.13.0)** | **BLE-only.** No IP transport, no zeroconf, no transport abstraction — but the framing/crypto/PIPE layers are pure `bytes`-in/`bytes`-out, and the byte-pipe is isolated in one 439-line `BLEConnection` class. `WifiConfig` provisioning model exists (rides `write_config()` over BLE). An unmerged `LANConnection` exists on PR #89. |
| **HA integration** | **BLE-only.** No zeroconf matcher, no network config-flow step, both connect paths hardcode `ble_device=`. Two forward-looking hooks exist: the transport-agnostic `notify_device_seen(source)` trigger (`delivery.py:16`) and diagnostics already redacting `ssid`/`password`/`server_url`. |

**Conclusion:** the tag listens and advertises; nothing on the host side connects. The critical path runs through **py-opendisplay → HA integration**, with small enabling changes in **firmware** (identity + security + one-client-at-a-time session handling) and a **protocol** promotion so the header remains the "implement a client from this file alone" contract.

### Resolved design questions (settled by the audits)

1. **Role model: HA-as-TCP-client, tag-as-TCP-server.** The firmware only implements the inbound server; `WifiConfig.server_host/server_port` (tag-dials-out) is parsed but dead. **Decision: keep host-connects-in**; mark `server_host/server_port` reserved-again (do not reuse the bytes). This resolves py-opendisplay's open Gap 6.
2. **PIPE_WRITE does not apply over TCP.** The `0x0080–0x0082` SACK/reorder/window machinery exists purely to compensate BLE write-without-response loss. TCP already guarantees ordered, gap-free delivery. **Decision: over the network transport, stream via DIRECT_WRITE `0x0070/71/72` with chunks up to 4094 bytes** (the shipped LAN path). Make this normative in the header.
3. **Identity: the stable cross-transport identity is `device_id` (4 B); the MSD is NOT identity.** The 16-byte MSD (`MsdAdvertisement`, `Firmware/src/display_service.cpp:1707`) is **pure telemetry** — a config-driven sensor/touch area (`dynamic[11]`), chip temperature, battery voltage, and a status byte whose low nibble is a free-running main-loop *liveness counter*. Most of its bytes change every advertisement, and it contains **no `device_id` and no MAC**. So the `msd` mDNS TXT (mirroring 14 of those bytes) is a poor correlation token, and "device_id + MSD as identity" is wrong. Use a stable anchor instead: HA's `unique_id` stays the BLE MAC (no migration), and correlation of a zeroconf discovery to a BLE entry uses a **new firmware `mac` TXT record** (ESPHome-proven pattern; the mDNS name today carries only 3 chip-id bytes). Optionally expose `device_id` in a TXT for a transport-neutral id. MAC and IP are locators.
4. **WiFi security is port-selected by the existing `encryption_enabled` flag — no mandatory encryption, no new policy bit.** The device serves exactly one WiFi mode, chosen by `SecurityConfig.encryption_enabled`:
   - **`encryption_enabled = 0` → unencrypted channel on TCP port 2446.** Plaintext framed opcodes and big-frame DIRECT_WRITE streaming; no handshake, no auth. This is the intended default for a trusted LAN (a no-auth "just push pixels" path), and it is the lowest-RAM option — no TLS buffers on the hot path (matters on C6).
   - **`encryption_enabled = 1` → TLS-PSK channel on TCP port 2447.** The PSK is derived from the existing 16-byte `SecurityConfig` master key (`tls_psk = KDF(master_key, "opendisplay-tls-psk")` via the existing CMAC KDF); the TLS handshake provides mutual auth, so no separate app-layer `AUTHENTICATE` runs over TLS. Port 2446 is **not** served in this mode.
   The port number itself signals the mode; the client and mDNS advert disambiguate without probing. The two modes are mutually exclusive per device — a tag is either plaintext-2446 or TLS-2447. This drops the earlier `require_auth_on_ip` policy: one existing flag is the whole switch.
5. **Both transports available, one client at a time (no concurrent sessions).** BLE and WiFi both listen at once, but the device services a single client across both transports; a connection attempt on either transport while a client is active **evicts the prior and clears the (single, global) session** — matching current firmware's LAN-connect behavior, extended to be cross-transport. A single global auth session suffices (no per-transport sessions); responses go to the active client's transport only (ending today's dual-delivery fan-out). See §8 D5.
6. **No WiFi-only device — WiFi is additive to a BLE-capable base.** Every OpenDisplay device is BLE-capable; WiFi is purely additive. This guarantees the BLE MAC is always available as the identity anchor, dissolves the provisioning-bootstrap problem (BLE is always present to deliver credentials), and means BLE+WiFi always coexist — NimBLE cannot be dropped to free RAM, so the C6-N4 TLS budget is met by mbedTLS tuning, not by removing BLE. See §8 D1. (`WIFI_ONLY_DEVICE_FEASIBILITY_2026-07-22.md` is retained as background only.)

### One client at a time — the permanent concurrency model

Per §8 D5, the device services **one client at a time across both transports**; concurrent BLE+WiFi sessions are explicitly out of scope. This resolves the earlier audit disagreement in favour of the simple model:
- The **single per-MAC host lock** (covering both transports) is the **permanent** design — not a phase-1 stopgap. It serializes all access to a tag regardless of transport.
- The firmware keeps its **single global session** and its existing evict-on-new-connect behavior (`clearEncryptionSession()`), extended so a connection on *either* transport evicts any active client on the other. No per-transport sessions, no cross-transport concurrency, no relaxation phase.
- **A request and its response MUST travel over the same pipe.** The response to a command is sent back only on the transport the command arrived on — never fanned out to both (the current dual-delivery behavior is removed). This is a hard requirement, not just an optimization.
- **Dropping a connection aborts its in-progress work.** On disconnect (either transport) the device MUST abort any in-flight stateful operation (image upload / chunked config / pipe), clear the session, and **discard any pending or queued response** for that connection — so a later connection (even a reconnect on the *same* transport) never receives an ACK or response for a command issued in a prior connection. Responses are connection-scoped; a dead connection's responses are dropped, never replayed.

---

## 2. Target architecture

```
                        Home Assistant integration
        config_flow: bluetooth step ──┐  ┌── zeroconf step (_opendisplay._tcp, mac TXT)
                                      ▼  ▼
                     one config entry, unique_id = BLE MAC
                                      │
                 transport resolver (prefer WiFi → BLE → queue)
                     per-MAC device lock (phase 1: both transports)
                                      │
                              py-opendisplay
                 OpenDisplayDevice(transport=…)   ← Transport Protocol
                    ├── BleTransport (ex-BLEConnection; max_frame 244)
                    └── TcpTransport ([len:2 LE] framer; max_frame 4096)
                         ├── plaintext  → :2446   (encryption_enabled = 0)
                         └── TLS-PSK    → :2447   (encryption_enabled = 1)
                                      │ identical opcode frames
              ┌───────────────────────┴───────────────────────┐
        BLE GATT 0x2446                          TCP :2446 (plain) | :2447 (TLS-PSK)
              └───────────────► ESP32 firmware ◄──────────────┘
                    imageDataWritten() — shared opcode dispatch
                    mode chosen by SecurityConfig.encryption_enabled
                    single global session · one client at a time
                    request+response over the SAME pipe (no fan-out)
```

---

## 3. Protocol changes (opendisplay-protocol) — the foundation

All additive ⇒ **MINOR bump 2.1 → 2.2**. Never renumber a shipped opcode (0x52/0x53 lesson). Verified safe: old firmware silently drops unknown opcodes (`Firmware/src/communication.cpp:693-695`), so clients must treat response-timeout as "unsupported" and fall back.

### 3.1 New SECTION 9 — "NETWORK TRANSPORT (LAN)" (normative, documents shipped behavior)

Constants: `OD_LAN_TCP_PORT 2446` (plaintext), `OD_LAN_TLS_PORT 2447` (TLS-PSK), `OD_LAN_MAX_PAYLOAD 4096`, frame = `[len:2 LE][payload]`, `OD_LAN_MDNS_SERVICE "_opendisplay._tcp"`, `OD_LAN_READ_TIMEOUT_S 30`. Normative rules:
1. **Mode is selected by the existing `SecurityConfig.encryption_enabled` field** (config packet `0x27`, already read by firmware `isEncryptionEnabled()` — this is not a new field). `encryption_enabled = 0` → serve **plaintext on 2446 only**. `encryption_enabled = 1` → serve **TLS-PSK on 2447 only**. The two are mutually exclusive; the device never serves both ports at once.
2. PIPE `0x0080–0x0082` **MUST NOT** be used on the network transport; stream via DIRECT_WRITE with chunks ≤ `OD_LAN_MAX_PAYLOAD − 2` (applies to both modes; over TLS the framing is inside the TLS session).
3. One network client at a time; a new connection evicts the prior **and clears only that transport's session**. A client **MAY** hold the connection open persistently across operations — the server keeps a live client connected and drops it only after `OD_LAN_READ_TIMEOUT_S` with no traffic (any traffic, including a keepalive `CMD_NET_STATUS`, resets it; the keepalive interval is advertised in `CMD_NET_CAPS`). HA uses connect-per-delivery; other clients may go persistent (§8 D6).
4. TLS-PSK (2447): PSK derived from the `SecurityConfig` master key via the existing CMAC KDF; prefer an **ECDHE-PSK** ciphersuite (or TLS 1.3) for forward secrecy; the TLS handshake is the authentication, so app-layer `AUTHENTICATE` (0x50) is not used on this port. **Record buffers are asymmetric and small on all targets** (IN ≈ 4 KB / OUT ≈ 1 KB, §8 D2), so a client MUST fragment `SSL_write` to ≤ the device IN size (≤4094 B — already the DIRECT_WRITE chunk size); a larger TLS record will be rejected.
5. Identity: `device_id` (4 B) is the stable cross-transport anchor. The **MSD is telemetry, not identity** — the 16-byte `MsdAdvertisement` (mDNS `msd` TXT = its 14-byte payload) carries volatile sensor/battery/temperature data plus a liveness counter and holds no `device_id` or MAC, so it must not be used to correlate a device across transports. Correlate via the `mac` TXT (below) or an explicitly-exposed `device_id`; MAC/IP are locators.
6. mDNS TXT keys: `msd` (existing), plus new `mac` (full BLE MAC, lowercase hex), `fw` (version), `cm` (communication_modes), `tls` (0/1 — which mode/port is live). The advertised SRV port is 2446 or 2447 to match. **Full DNS-SD record + TXT specification in §3.6.**

### 3.2 New opcode band `0x0054–0x005F` — NETWORK/SESSION (adjacent to auth/power)

| Opcode | Name | Purpose |
|---|---|---|
| `0x0054` | `CMD_NET_STATUS` | Query runtime WiFi state: `[status][0x54][comm_modes:1][wifi_state:1][ipv4:4][port:2 LE][rssi:1]`. `wifi_state`: 0 idle / 1 connecting / 2 associated / 3 auth-fail / 4 no-AP / 5 got-IP. **Plaintext-readable** (like `0x43`) so a BLE client can discover the WiFi endpoint. |
| `0x0055` | `CMD_NET_JOIN` | Provision + join now (no config rewrite/reboot): `[enc_type:1][ssid_len:1][ssid][psk_len:1][psk]`; ACK on accept, async result as a `0x0054`-shaped notification. Also persists to the `0x26` store. |
| `0x0056` | `CMD_NET_FORGET` | Wipe WiFi creds + drop association + clear the WiFi-side session; other config untouched. |
| `0x0057` | `CMD_NET_CAPS` | Capability descriptor: transports bitmap, max LAN frame, PIPE-over-IP=0, keepalive interval, **wifi mode (plaintext-2446 / tls-2447) + active port**. |
| `0x0058–0x5F` | reserved | WS upgrade, extended TLS descriptor, static IP, multi-AP. |

### 3.3 Config + capability fields

- **No new policy field.** WiFi mode is driven entirely by the **pre-existing** `SecurityConfig.encryption_enabled` (packet `0x27`): 0 → plaintext/2446, 1 → TLS-PSK/2447. (The earlier `0x2D network_policy` / `require_auth_on_ip` proposal is dropped — it is unnecessary once the existing flag selects the port/mode. If a keepalive interval is ever wanted, add it to `CMD_NET_CAPS`, not a new config packet.)
- `WifiConfig 0x26`: keep as the persistent credential store; comment-deprecate `server_host/server_port` back to reserved.
- Transport capability lives in **`CommunicationModes`** (bit 2 exists; bits 3–7 free for e.g. WS) — **not** `device_flags` (hardware-init axis, wrong home).

### 3.4 Security & simultaneity (normative prose)

- **WiFi security is opt-in, not mandatory.** The mode is the operator's choice via the existing `SecurityConfig.encryption_enabled` flag: leaving it `0` yields the unencrypted 2446 channel (no auth, no handshake — fine for a trusted LAN); setting it `1` yields TLS-PSK on 2447 (encrypted + mutually authenticated). There is no forced-encryption requirement on the IP transport.
- **Write-only secrets: declined (§8 D4).** The stored AES master key and WiFi PSK are **not** redacted on `CONFIG_READ`; the config blob remains fully readable. (Simplicity over the marginal hardening.)
- **Plaintext channel serves the full control plane (§8 D4).** When encryption is disabled, the 2446 channel serves *all* opcodes in the clear — draw and privileged control alike (`ENTER_DFU`, `CONFIG_WRITE`, `POWER_OFF`, `REBOOT`). No opcode-scoping: the plaintext channel is fully trusted (trusted-LAN posture; also preserves backward compatibility). To protect the control plane, enable encryption → everything moves to TLS-PSK on 2447.
- **One client at a time; request and response over the same pipe.** A single global session is serviced across both transports; a connection on either transport while a client is active **evicts the prior and clears the session**. The response to a command **MUST** be returned on the same transport the command arrived on — never fanned out to both (dual-delivery removed). No per-transport sessions, no concurrent-session support.
- **Disconnect aborts in-progress work.** On a dropped connection the device **MUST** abort any in-flight stateful operation, clear the session, and discard pending/queued responses — so a reconnecting client never receives an ACK for a command from the previous connection. Every operation is scoped to the connection that started it.

### 3.5 Workflow

Edit only `src/opendisplay_protocol.h`; then `gen_python_protocol.py --write/--check`, `gen_js_protocol.py --write/--check`, `sync_protocol_header.py --push` + `--check`. New constants (`OD_LAN_TLS_PORT 2447`) and the mode/port rule go in SECTION 9; no new config packet is needed (mode reuses the existing `SecurityConfig.encryption_enabled`). Changelog: "Unreleased (since 2.1)" bullets → roll to `2.2`.

### 3.6 mDNS advertisement — full specification

Current firmware (`wifi_service.cpp:51-80`) registers a minimal DNS-SD service: hostname `OD<chipIdHex>.local`, service `_opendisplay._tcp` on port 2446, and a single throttled TXT key `msd`. This section specifies the **complete proposed record set**. All of it is additive (new TXT keys + a mode-dependent port); an existing `msd`-only client keeps working.

#### 3.6.1 DNS-SD record set

A conformant device publishes four record types:

| Record | Name | Value / target | Notes |
|---|---|---|---|
| **PTR** | `_opendisplay._tcp.local.` | `<instance>._opendisplay._tcp.local.` | service enumeration; TTL 4500 s |
| **SRV** | `<instance>._opendisplay._tcp.local.` | priority 0, weight 0, **port = 2446 or 2447**, target `<host>.local.` | port MUST match the active mode (§3.6.3); TTL 120 s |
| **TXT** | `<instance>._opendisplay._tcp.local.` | key/value set (§3.6.2) | TTL 120 s |
| **A / AAAA** | `<host>.local.` | device IPv4 (and IPv6 if available) | TTL 120 s |

- **Instance name** `<instance>` = `OD<chipIdHex>` (unchanged; `chipIdHex` = 6 hex chars = top 3 bytes of the chip id). This is a **cosmetic label only** — it carries just 3 of the 6 MAC bytes and can collide; mDNS conflict resolution auto-suffixes duplicates (`OD1A2B3C-2`). Device **identity comes from the `mac` TXT (§3.6.2), never the instance name or hostname.**
- **Hostname** `<host>` = `OD<chipIdHex>` → `OD<chipIdHex>.local.`

#### 3.6.2 TXT record keys

Each entry is a DNS-SD `key=value` string (RFC 6763; ≤255 B each):

| Key | Presence | Value format | Example | Source | Stability |
|---|---|---|---|---|---|
| `mac` | **REQUIRED** (new) | lowercase, colon-separated BLE MAC `xx:xx:xx:xx:xx:xx` | `e8:9f:6d:12:34:56` | **actual advertised BLE address** — `NimBLEDevice::getAddress()` (ESP32) / `Bluefruit.getAddr()` (nRF); **NOT** `ESP.getEfuseMac()` | static |
| `tls` | **REQUIRED** (new) | `0` \| `1` | `1` | `SecurityConfig.encryption_enabled` | semi-static |
| `fw` | recommended (new) | `<major>.<minor>` | `1.4` | `CMD_FIRMWARE_VERSION` | changes on OTA |
| `cm` | recommended (new) | 2 lowercase hex (comm-modes bitmask) | `05` | `SystemConfig.communication_modes` | config-static |
| `id` | optional (new) | 8 lowercase hex (4-byte `device_id`) | `1a2b3c4d` | `device_id` | static |
| `pv` | optional (new) | `<major>.<minor>` protocol version | `2.2` | `OD_PROTOCOL_VERSION` | static |
| `msd` | **REQUIRED** (existing) | 28 lowercase hex (14-byte MSD payload) | `46240011…` | `updatemsdata()` | **volatile telemetry — NOT identity** |

Worst-case TXT size with every key ≈ **95 bytes** — far under the single-datagram limit, so no IP fragmentation (cf. the mDNS-vs-BLE size budget).

#### 3.6.3 Mode/port coupling (ties to §3.4)

The SRV `port` and the `tls` TXT are both derived from `SecurityConfig.encryption_enabled` and **MUST agree**:
- `encryption_enabled = 0` → SRV port **2446**, `tls=0` (plaintext).
- `encryption_enabled = 1` → SRV port **2447**, `tls=1` (TLS-PSK).

The device advertises **exactly one** service instance (one port) at a time. On an `encryption_enabled` change (config write), it re-registers the service on the new port with the updated SRV + `tls`.

#### 3.6.4 Update, throttling, and TTL rules

- **Static keys** (`mac`, `fw`, `cm`, `id`, `pv`, `tls`) are set once at service registration and re-published only on the rare event that changes them (OTA, config write, mode switch).
- **`msd` is volatile** (temperature, battery, liveness-counter nibble) and MUST stay rate-limited — keep the existing ≥400 ms throttle (`opendisplay_mdns_update_msd_txt`). Frequent TXT re-multicast is bandwidth-costly and pointless; passive readers get telemetry from the BLE advert.
- Recommended TTLs: PTR 4500 s; SRV/TXT/A 120 s (RFC 6762 §10 guidance).

#### 3.6.5 Normative requirements

1. `mac` MUST be the device's **actual advertised BLE address** (the AdvA HA sees as `discovery_info.address`), **NOT** the eFuse/WiFi base MAC — those differ by a build-config-dependent offset (+1 or +2 per `CONFIG_ESP32_UNIVERSAL_MAC_ADDRESSES`). Read it from the BLE stack and validate it equals the tag's HA `unique_id`.
2. `mac` MUST be present whenever the WiFi transport is up, in **both** plaintext and TLS modes — identity correlation is needed regardless of mode.
3. The SRV `port` MUST equal 2446 when `tls=0` and 2447 when `tls=1`.

#### 3.6.6 ⚠️ HA unique_id formatting — mandatory reconciliation

**This is the single most likely correctness bug in the whole discovery path.** The `mac` published here is lowercase, colon-separated (`e8:9f:6d:12:34:56`). But HA's existing OpenDisplay config-entry `unique_id` is stored **raw, exactly as BlueZ/bleak reported it — UPPERCASE, colon-separated** (`E8:9F:6D:12:34:56`), because `config_flow.async_step_bluetooth` calls `async_set_unique_id(discovery_info.address)` with **no** `format_mac()` normalization (`config_flow.py:177,224`).

HA identity matching (`async_set_unique_id` + `_abort_if_unique_id_configured`) is a **case-sensitive exact string compare**. So:

- Uppercase `E8:9F:6D:…` (existing BLE unique_id) ≠ lowercase `e8:9f:6d:…` (`format_mac`'d zeroconf mac).
- A mismatch does **NOT** merge the WiFi discovery onto the BLE entry — it **silently creates a duplicate device**.

Note the *only* difference here is letter case (BlueZ already emits colon-separated 17-char form, which `format_mac` merely lowercases — it changes nothing else); but a lone case difference is sufficient to break the match. The `mac` value format published in the TXT is not itself the fix — the reconciliation must happen on the HA side.

**Chosen (§8 D3): option (b) — match the existing raw form, no migration.** The zeroconf step normalizes the `mac` TXT to the **same raw string the BLE path stores** and matches against it, leaving existing `unique_id`s untouched:
- The `mac` TXT is lowercase-colon (§3.6.2); BlueZ's raw address is uppercase-colon; so the zeroconf step **uppercases** the TXT value — `async_set_unique_id(mac.upper())` — producing exactly what `async_step_bluetooth` stores for the same device.
- The BLE and WiFi steps therefore converge on **identical** `unique_id` strings and `_abort_if_unique_id_configured` merges them onto one entry.

Option (a) — a one-way `async_migrate_entry` normalizing everything (and the `(CONNECTION_BLUETOOTH, …)` tuples) to `format_mac`, ESPHome-style — stays available as a **future cleanup** but is **not** done now. Whichever is used, the BLE and WiFi steps MUST produce **identical** `unique_id` strings or MAC-based dedup fails.

---

## 4. Firmware changes (ESP32 envs of `Firmware` only)

Ordered; items F1–F3 are the Phase-1 enablers, F4–F6 are later phases (2–3). Per the design principles, every item is scoped to the smallest **backward-compatible** change — reusing shipped mechanisms and adding nothing an old client or old firmware can't ignore; new behavior lands host-side wherever possible.

- **F1 — mDNS identity/capability TXT** (`wifi_service.cpp:51,73`): implement the full record set + TXT keys per **§3.6** (`mac`, `tls`, `fw`, `cm`, optional `id`/`pv`). Pure additive; unblocks HA identity unification (G4). Note `mac` must be the **actual advertised BLE address**, not the eFuse/WiFi MAC (§3.6.5). *Small.*
- **F2 — Port/mode selection** (`initWiFi()` / `handleWiFiServer()`, `wifi_service.cpp:85,170`): read the existing `SecurityConfig.encryption_enabled` and open **exactly one** listener — plaintext on 2446 when `0`, TLS-PSK on 2447 (mbedTLS, PSK from the master-key KDF) when `1`. Advertise the active port + `tls` TXT in mDNS. Also stop logging SSID/PSK (`config_parser.cpp:544-546`, `wifi_service.cpp:105`). The listener must **allow a persistent client** — keep a live connection open across operations, dropping only after `OD_LAN_READ_TIMEOUT_S` with no traffic (keepalive-bounded, §8 D6). *Plaintext path small; TLS path medium — mbedTLS with the uniform tuned config on **all** targets: asymmetric IN≈4 KB/OUT≈1 KB record buffers + statically pre-allocated buffers (§8 D2). Load-bearing on C6; applied on S3 too for one config, one client contract.*
- **F3 — NET opcodes** (`communication.cpp` dispatch): implement `0x0054/0x0055/0x0056/0x0057`. `0x0054` is the highest-value piece (BLE→WiFi endpoint discovery + provisioning feedback, replacing today's fire-and-pray config+reboot). *Medium.*
- **F4 — One-client-at-a-time + same-pipe response routing** (`communication.cpp:534`, `wifi_service.cpp`): keep the single global session; ensure a connection on *either* transport evicts any active client on the other (extend the existing LAN `clearEncryptionSession()` eviction to be cross-transport). `imageDataWritten` gains an origin tag (the `conn_hdl` arg is already unused) so `sendResponse` returns each response on the **same transport the request arrived on** — never dual-delivering. **On disconnect, abort in-flight transfers (directWrite / pipe / chunked-config state), clear the session, and flush the pending response queue** (`flushResponseQueueToBle` / `esp32_queue_ble_notify_copy`, and the LAN disconnect paths in `wifi_service.cpp`) so a stale ACK from a prior connection is never delivered to a reconnecting client. No per-transport sessions. *Small — much simpler than concurrent sessions; per §8 D5 concurrency is out of scope.*
- **F5 — Power/coex posture**: `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)` after association; gate WiFi bring-up off battery `power_mode` (or an opt-in flag) so deep-sleep tags never associate (~80–100 mA is battery-incompatible); document C3/C6 single-antenna time-sharing. *Small.*
- **F6 (optional) — WiFi OTA**: HTTP-pull or ArduinoOTA behind auth. *Later.*

---

## 5. py-opendisplay changes (the critical-path blocker)

Concentrated in ~4 files + one new subpackage; **non-breaking minor release** (existing `mac_address=`/`ble_device=` call sites keep working).

- **P1 — `Transport` Protocol** (`transport/base.py`): `connect/disconnect/write(data, *, response, drain_stale)/read(timeout)→one logical frame/drain/is_connected`, plus `max_frame` and `supports_write_without_response`. Contract: **`read()` returns exactly one protocol frame** (BLE gets this free per notification; TCP via the framer).
- **P2 — `BleTransport`**: rehome `BLEConnection` behind the Protocol; keep `BLEConnection` exported as an alias; `max_frame=244`.
- **P3 — `TcpTransport`** (`transport/ip.py`, optional `[wifi]` extra): `asyncio.open_connection`, `[len:2 LE]` framer with reassembly into a frame queue; `max_frame=4096`; `response`/`drain_stale` no-ops; `supports_write_without_response=False`. **Implement from scratch** against the `Transport` shape; the `feat/tcp` branch / PR #89 `LANConnection` is **reference-only for ideas**, not a code base to fork (§8 D9).
- **P4 — Wire into `OpenDisplayDevice`**: `_conn` typed as `Transport`; selection in `__aenter__`: explicit `transport=` > `host=`/`port=` → Tcp > `mac_address=`/`ble_device=` → Ble (default). Replace `DEFAULT_MAX_FRAME` uses with `self._conn.max_frame`; gate WWR on the capability flag; rename `_on_ble_disconnect`→`_on_disconnect`. Upload path: when `max_frame > 244` use DIRECT_WRITE with LAN-sized chunks instead of PIPE (mirrors firmware's LAN path).
- **P5 — Provisioning API**: `device.provision_wifi(ssid, password, encryption_type=…)` — patches only the `0x26` TLV + sets comm-modes bit 2 via existing `write_config()`; prefer `CMD_NET_JOIN`/`CMD_NET_STATUS` when firmware advertises them (F3) for join-now + status feedback. Implementable over BLE today.
- **P6 — IP discovery** (`discovery_ip.py`, `[wifi]` extra): zeroconf browse of `_opendisplay._tcp.local.` returning `{name: (host, port, mac, msd)}`.
- **P7 — Generalize naming**: transport-neutral exception aliases (`ConnectionError`/`TimeoutError` superclasses over the BLE-named ones), CLI `--host` + `provision-wifi` + `scan --lan`.
- **Testing**: `FakeTransport` fixture (lets the whole PIPE/crypto suite run without bleak mocks — a standalone win); TCP framer round-trip tests (partial/coalesced/large reads); transport-selection tests; `provision_wifi` writes-only-0x26 test.

---

## 6. Home Assistant integration changes

Principle: **WiFi is a second transport under the existing MAC-keyed identity — one config entry, one device, `unique_id` = BLE MAC.** Existing BLE-only entries need no migration (no `CONF_HOST` ⇒ resolver returns BLE ⇒ behavior unchanged).

- **H1 — manifest**: add `"zeroconf": ["_opendisplay._tcp.local."]`; bump the py-opendisplay pin to the transport-capable release.
- **H2 — config flow**: `async_step_zeroconf` reads the `mac` TXT (F1), `format_mac` → `async_set_unique_id(mac)` → `_abort_if_unique_id_configured(updates={CONF_HOST, CONF_PORT})` so a BLE-onboarded tag just gains `host`/`port` on rediscovery (ESPHome pattern, `core/…/esphome/config_flow.py:319-421`); WiFi-first onboarding creates the entry and later BLE discovery dedupes onto it. TCP-probe in `_async_test_connection`. **Reconcile identity per §8 D3 / §3.6.6 — match the existing raw form: uppercase the `mac` TXT (`mac.upper()`) so the zeroconf `unique_id` equals the raw BLE `unique_id` exactly; no migration.**
- **H3 — transport resolver** used by both `services._async_connect_and_run` and `delivery._drain_once`: prefer TCP when `CONF_HOST` present and recently seen → BLE (`async_ble_device_from_address`) → queue. On mid-transfer WiFi failure, next retry attempt re-resolves (existing `MAX_DELIVERY_ATTEMPTS` cadence handles it). Connection is opened **per delivery** and closed after the unit of work (§8 D6); a future persistent-with-idle-timer mode would be a host-side change only.
- **H4 — lock**: keep the single per-MAC lock and hold it across **both** transports — this is the **permanent** model (one client at a time, §8 D5), not a phase-1 stopgap. No relaxation.
- **H5 — provisioning service** `opendisplay.configure_wifi(device_id, ssid, password, …)`: connect over BLE, `provision_wifi()` (P5), then poll `CMD_NET_STATUS` and surface join success/failure to the user.
- **H6 — observability**: IP-address diagnostic sensor, "active transport" attribute, `host`/`port`/`communication_modes`/last-transport in diagnostics (redaction set already covers creds); optional WiFi liveness feeding `notify_device_seen("wifi")` (the hook already takes a source), low priority since WiFi tags are typically mains-powered and always reachable.

---

## 7. Phasing, ordering, and rollout

**Phase 0 — Spec (opendisplay-protocol):** SECTION 9 (incl. `OD_LAN_TLS_PORT 2447` + the `encryption_enabled`→port/mode rule) + `0x0054–0x0057` + mDNS TXT spec; gen + sync + CI check. No new config packet. Everything downstream implements against this.

**Phase 1 — Minimum viable WiFi transport, plaintext mode (serialize everything):**
1. Firmware F1 (mDNS `mac`/`fw`/`cm`/`tls` TXT) + F2 (open the plaintext 2446 listener when `encryption_enabled = 0`).
2. py-opendisplay P1–P4 + P6 → release (minor, `[wifi]` extra).
3. HA H1–H3 + H4(strict single lock) → pin bump.
   *Exit criteria:* a WiFi-enabled tag (encryption off) is discovered via zeroconf, merged onto its BLE entry, and `drawcustom` uploads over plaintext TCP/2446 with 4094-byte chunks (~17× fewer frames than BLE), falling back to BLE when WiFi is unreachable.

**Phase 1b — TLS-PSK mode (2447):** Firmware serves TLS-PSK on 2447 when `encryption_enabled = 1` (mbedTLS + ECDHE-PSK, buffers tuned per the RAM doc); py-opendisplay TcpTransport connects TLS to 2447 when the advert says `tls=1` (needs Python 3.13+ stdlib PSK, or an `sslpsk`/mbedTLS shim — verify the target HA's Python). The client picks port/mode from the advertised `tls` flag; no handshake on the plaintext path.

**Phase 2 — Provisioning UX + status:** Firmware F3 (`NET_STATUS/JOIN/FORGET/CAPS`), py-opendisplay P5/P7, HA H5–H6. Until F3 ships, provisioning falls back to config-write + reboot.

**Phase 3 — Power posture + polish:** Firmware F4 (cross-transport eviction + same-pipe response routing) + F5 (power posture). No concurrency work — one client at a time is the permanent model (§8 D5). Optional F6 (WiFi OTA).

**Version coupling reminders:** HA `manifest.json` pins exact `py-opendisplay==`; a new py-opendisplay release precedes the HA changes. Header changes go through `sync_protocol_header.py --push/--check` into all four firmware copies (three of them only gain comments/constants they ignore).

### Risks / open items

- **PR #89 / `feat/tcp` `LANConnection`** is reference-only — implement `TcpTransport` from scratch, not a fork (§8 D9). Read the branch for ideas/gotchas first, then write fresh.
- **Silent unknown-opcode drop** means capability probing is timeout-based on old firmware; clients must fall back cleanly (P5/H5 must handle "NET opcodes unsupported").
- **WiFi target scope is C6 + S3 only** (C3 excluded). This isn't enforced today — the only gate is `-DTARGET_ESP32`, which is set on the C3 envs too, so C3 currently compiles/serves WiFi. Add a dedicated `-DOPENDISPLAY_ENABLE_WIFI` on the S3/C6 envs and guard `wifi_service.cpp` on it, to enforce scope and reclaim RAM on C3.
- **TLS on C6-N4 is an objective met by tuning (§8 D2), not a risk of infeasibility.** The *default* mbedTLS is marginal on C6-N4, but the trimmed build — applied uniformly on **all** WiFi targets (asymmetric IN/OUT record buffers, PSK-only, statically pre-allocated buffers) — fits in ~8–12 KB; the remaining work is on-hardware `largest_free_block` validation. The plaintext 2446 path has no such issue; S3's PSRAM makes TLS comfortable.
- **No forced-encryption behavior change.** WiFi mode is opt-in via the pre-existing `encryption_enabled` flag; a tag left with encryption off serves plaintext 2446 exactly as an operator would expect. No existing deployment is forced into auth.
- **Same-pipe response routing (F4)** removes today's cross-transport dual-delivery fan-out: a response now returns only on the transport its request arrived on (§8 D5). Technically an observable behavior change for any client that relied on the fan-out; treated as a bug fix.

---

## 8. Open Design Decisions

Decisions that still need an owner's call — distinct from the settled questions in §1 and the watch-items in §7's risk list. Each is tagged **RESOLVED** or **OPEN**.

### D1 — WiFi-only devices — **RESOLVED: not planned**

**No WiFi-only device is planned. Every OpenDisplay device is BLE-capable; WiFi is purely additive** (see §1.6). Consequences that simplify the rest of the design:
- The **BLE MAC is always present** as the identity anchor (see D3) — `device_id` is never required as a primary identity, only optional telemetry.
- The **provisioning bootstrap problem dissolves** — BLE (`0x26` config-write, later `CMD_NET_JOIN`) is always available to deliver WiFi credentials. No SoftAP/captive-portal/Improv subsystem is needed.
- **BLE + WiFi always coexist**; NimBLE cannot be torn down to reclaim RAM, so the C6-N4 TLS budget must be met by mbedTLS tuning (D2), not by dropping BLE. `WIFI_ONLY_DEVICE_FEASIBILITY_2026-07-22.md` (variants A/B) is retained as background only.

### D2 — TLS on C6-N4 — **RESOLVED: an objective; tune mbedTLS to fit**

**TLS-PSK on C6-N4 (2447) is a target, not an open go/no-go.** `encryption_enabled=1` is a **universal** capability across S3 **and** C6; the C6 build is tuned as needed to fit, rather than declaring TLS S3-only. The **same tuned mbedTLS config is used on ALL WiFi targets** (S3 has headroom but uses the identical config for consistency — one config, one client contract, less RAM everywhere). Per D1, BLE cannot be freed to make room, so the budget is met by trimming mbedTLS. Required trims (the *default* mbedTLS is marginal on C6-N4's no-PSRAM budget):
- **Asymmetric record buffers — on ALL targets (S3 and C6):** `MBEDTLS_SSL_IN_CONTENT_LEN ≈ 4096` / `OUT_CONTENT_LEN ≈ 1024` (device receives large DIRECT_WRITE chunks, sends tiny ACKs) — saves ~27 KB vs the 32 KB default. Applied uniformly, not only where RAM is tight: the sizes are **protocol-derived, not target-derived**. py-opendisplay must `SSL_write` in ≤ IN_CONTENT_LEN chunks (already ≤4094), so the contract is identical on every target, even without RFC 6066 MFL in the Python client.
- PSK-only (compile out X.509/RSA/DHE), single ECDHE-PSK ciphersuite, no session tickets/renegotiation/resumption.
- **Statically pre-allocate the TLS context/buffers at boot — on ALL targets** (a fixed pool via a custom mbedTLS calloc/free), eliminating the handshake-time heap-fragmentation spike. This is the actual C6 risk (not raw total RAM); applying it uniformly keeps one allocation path and removes the fragmentation failure mode on S3 too. Combined with the asymmetric buffers above, this is a single mbedTLS config shared by every WiFi target.
- Estimated **~8–12 KB** vs ~35 KB default peak → fits the measured C6-N4 free RAM with BLE up.

**Remaining work (execution, not a decision):** implement the tuned build and **validate `largest_free_block` on real C6-N4 hardware**. (No zlib-window concern: `OPENDISPLAY_ZLIB_WINDOW_BITS` is commented out on C6/S3 — only `esp32-s3-E1004` enables the 32 KB window — so the compression window is ~512 B on C6 and is not part of the TLS peak.) ECDHE-PSK vs plain-PSK is a fallback lever (drops ECC RAM/CPU at the cost of forward secrecy) only if hardware validation shows it's still tight.

### D3 — Identity: anchor & reconciliation — **RESOLVED**

- **Anchor:** the **BLE MAC** is the cross-transport identity anchor (guaranteed present per D1). The `device_id` / `id` TXT stays optional.
- **Reconciliation — match the existing raw MAC format (no migration, for now).** The existing BLE `unique_id` is stored raw exactly as BlueZ reports it (**uppercase, colon-separated**); the zeroconf step normalizes the `mac` TXT to that **same raw form** and matches against it — no `async_migrate_entry`, existing entries untouched. Since the `mac` TXT is published lowercase-colon (§3.6.2) and BlueZ's raw form is uppercase-colon, the zeroconf step **uppercases** the TXT value (`mac.upper()`) so `async_set_unique_id` / `_abort_if_unique_id_configured` matches the existing BLE entry exactly. **Caveat / future cleanup:** this ties matching to BlueZ's uppercase-colon form (fine — HA runs on BlueZ). Migrating everything to `format_mac` (option a, ESPHome-style) stays available as a later cleanup if a non-BlueZ backend or a cleaner canonical form is ever wanted; not needed now.

### D4 — Plaintext channel scope & secrets hardening — **RESOLVED**

- **Write-only secrets — no.** The master key / WiFi PSK are **not** redacted on `CONFIG_READ`; the stored config stays fully readable. Simplicity over the marginal hardening.
- **Channel scope — full control plane on plaintext.** When encryption is disabled (`encryption_enabled = 0`, port 2446), the device serves the **full control plane in the clear** — draw *and* privileged control (`ENTER_DFU`, `CONFIG_WRITE`, `REBOOT`, `POWER_OFF`). No opcode-scoping: the plaintext channel is fully trusted (trusted-LAN posture, and it preserves perfect backward compatibility — no existing plaintext client loses an opcode it uses). Operators who want the control plane protected **enable encryption**, which moves everything to TLS-PSK on 2447.

### D5 — Concurrent clients — **RESOLVED: not supported**

The device services **one client at a time across both transports** — no concurrent BLE+WiFi sessions, ever (not just v1). Both transports stay *available* (listening), but a connection on either **evicts** any active client on the other. Firm sub-rules:
- A **request and its response travel over the same pipe** — the response is returned only on the transport the request arrived on, never fanned out to both (dual-delivery removed).
- **Dropping a connection aborts its in-progress work** — the device aborts in-flight transfers, clears the session, and discards pending/queued responses, so a reconnecting client (even on the same transport) never receives an ACK for a command from the prior connection. Every operation is connection-scoped; responses are never replayed across connections.
- Consequences: the single per-MAC host lock is the **permanent** model (H4); a single global firmware session suffices; F4 shrinks to cross-transport eviction + same-pipe response routing + abort-on-disconnect (no per-transport sessions); there is no "Phase 3 concurrency" — the goal is transport *choice*, not parallelism.

### D6 — Connection lifecycle — **RESOLVED: HA per-delivery; firmware allows persistent**

Asymmetric by side:
- **HA side — connect-per-delivery.** HA opens a connection per unit of work (an image, or a batch like image+LED), transacts, and closes — reusing the existing `_async_connect_and_run` / `_drain_once` flow (minimize-change), keeping the single client slot free between deliveries (BLE can interleave), allowing device sleep, and needing no keepalive. A full TLS handshake per delivery is negligible against the multi-second e-paper refresh at the low update cadence displays run at. *(Predicated on low cadence: TLS session resumption is disabled for footprint (§8 D2), so per-delivery pays a full handshake each time; if a high-frequency use case ever appears, revisit persistent-or-session-tickets.)*
- **Firmware side — MUST allow persistent.** The LAN server must **not force per-delivery semantics**: a client may hold the connection open across many operations. The device keeps a live client's connection open (the self-extend awake-window already does this) and must not drop a persistent-but-idle client that stays within the **keepalive interval advertised in `CMD_NET_CAPS`** — `OD_LAN_READ_TIMEOUT_S` is the no-keepalive drop bound, reset by any traffic (including a keepalive `CMD_NET_STATUS`). This costs nothing extra (static TLS pre-alloc reserves the buffers regardless) and lets non-HA clients (the CLI, a future app) use a persistent session while HA itself stays per-delivery.
- **Design note — future persistent-with-idle-timer state (allow for it now).** The design **must not foreclose** HA later evolving from strict per-delivery to a **persistent connection governed by a host-side idle timer**: hold the connection open after a delivery and close it only after N seconds of inactivity. This is a middle ground — it amortizes the TLS handshake across bursts of updates, yet still frees the single client slot and lets the device sleep once the timer fires. Because the firmware already *allows* persistent connections (keepalive-bounded, above), this future mode is a **purely host-side change requiring no protocol or firmware change**. Keep both the protocol and the firmware server free of per-delivery-only assumptions so this path stays open.

### D7 — Python TLS-PSK client — **OPEN (verify)**

py-opendisplay's TLS client needs **Python 3.13+** stdlib PSK callbacks, or an `sslpsk`/mbedTLS shim. Verify the target HA runtime's Python; if <3.13, choose a shim (packaging/maintenance cost) or defer TLS (Phase 1b) until 3.13 is baseline. Gates the client half of encrypted WiFi.

### D8 — MVP provisioning path — **RESOLVED: config-write + reboot for MVP; live reconfig later**

- **MVP — reuse existing config-write + reboot.** Provisioning writes the `0x26` `WifiConfig` TLV via `CONFIG_WRITE` (and sets the `communication_modes` WiFi bit) over BLE, then **reboots to apply** — the shipped path, **no new firmware or opcode** (minimize-change). Implementable in py-opendisplay / HA today (P5 / H5). BLE is always the provisioning channel (D1).
- **Later enhancement — live WiFi reconfiguration.** `CMD_NET_JOIN` / `CMD_NET_STATUS` (F3) add join-now **without reboot** plus async association-status feedback (bad-PSK / no-AP / got-IP), with `CMD_NET_FORGET` for a clean credential wipe. Additive; ships when convenient. Until then the MVP config-write+reboot path stands.

### D9 — PR #89 `LANConnection` — **RESOLVED: implement from scratch**

Do **not** reuse the `feat/tcp` branch's `LANConnection` code. Implement P3 `TcpTransport` fresh against the P1 `Transport` shape. The `feat/tcp` branch may be **read as a source of ideas** (framing approach, gotchas, edge cases) but is **not a code base to fork** — prefer a clean implementation.

### Priority

**All product/design decisions are now resolved.** What remains is **execution / verification**, not forks:
- **D2** — implement the uniform tuned mbedTLS build (asymmetric IN/OUT buffers + static pre-alloc, all targets) and validate `largest_free_block` on real C6-N4 hardware.
- **D7** — verify HA's Python version (3.13+ for stdlib TLS-PSK); pick an `sslpsk`/mbedTLS shim or defer the TLS *client* if older. (The plaintext path is unaffected.)

*Resolved:* D1 (no WiFi-only), D2 (TLS-on-C6 is a target), D3 (BLE-MAC anchor; match-existing-raw-format reconciliation, no migration), D4 (write-only-secrets no; plaintext full control plane), D5 (no concurrent clients), D6 (HA per-delivery; firmware allows persistent), D8 (config-write+reboot MVP; live reconfig later), D9 (implement from scratch).
