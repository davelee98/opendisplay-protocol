# WiFi Transport Readiness — Wire-Protocol Audit

**Repo:** `opendisplay-protocol` (canonical header + docs/agents notes)
**Date:** 2026-07-22
**Scope:** Protocol-level readiness for adding **WiFi as a transport between Home Assistant and a display**, with **BLE and WiFi simultaneously active** on one device. Report-only: catalog, transport-coupling analysis, gap analysis, and a protocol-level proposal. No source changed.

References use file paths + line anchors against `src/opendisplay_protocol.h` (canonical header, OD_PROTOCOL_VERSION 2.1), `src/opendisplay_structs.py` (generated config-struct mirror), and the two prior WiFi design notes:
- `agents/Home_Assistant_Integration/WIFI_ARCHITECTURE_2026-07-06.md`
- `agents/py-opendisplay/FINDINGS_WIFI.md`

---

## Executive summary

WiFi is **already partially real in the ecosystem but essentially invisible in the canonical wire-protocol header.** The reference ESP32 firmware (`Firmware/src/wifi_service.cpp`) ships a working LAN transport: device joins an AP, listens on TCP **port 2446**, advertises `_opendisplay._tcp` over mDNS, and feeds a **2-byte little-endian length-prefixed** frame stream into the *exact same* command dispatcher (`imageDataWritten`) that BLE uses. BLE and WiFi run **concurrently** on the device today. py-opendisplay has a `LANConnection` (via un-merged PR #89) and the provisioning-side `WifiConfig` TLV (config packet `0x26`).

But `src/opendisplay_protocol.h` — the "implement a correct client from this file alone" contract — **contains zero WiFi/TCP/mDNS wire specification.** The only WiFi trace in the header is a single comment line listing config-packet type `0x26 wifi` (`opendisplay_protocol.h:243`). The header is written as "the OpenDisplay **BLE** wire-protocol contract" (line 2). Everything transport-specific about WiFi — framing, port, discovery, session semantics, and (critically) **provisioning opcodes and capability/security/identity model** — lives only in firmware, in scattered agents notes, and on opendisplay.org, never in the canonical spec.

**Headline finding:** The opcode/payload framing is genuinely transport-agnostic and runs unchanged over TCP (already proven in firmware). PIPE_WRITE's SACK/reorder machinery is **BLE-specific and unnecessary over TCP**. The real protocol gaps are not in the data path — they are in **(1) provisioning, (2) capability advertisement, (3) security/identity for a non-proximity transport, and (4) simultaneous-transport session rules**, none of which the header specifies. WiFi provisioning today rides the config TLV path (`0x26`) with no status/forget/connection-report opcodes, and the LAN control plane is **completely unauthenticated when security is off** (documented as Firmware audit M2). The recommendation is to promote the LAN transport into the canonical header as a first-class transport annotation, add a small `0x0054–0x005F` **network/session** opcode band for provisioning + status + capability query, and specify an IP-transport security model (the BLE proximity assumption does not hold on IP).

---

## What exists today

### 1. Opcode map (canonical header, `src/opendisplay_protocol.h`)

16-bit big-endian opcodes. Allocated ranges (note the deliberate banding — this matters for the proposal):

| Band | Opcodes | Purpose |
|---|---|---|
| Reboot | `0x000F` CMD_REBOOT (`:257`) | reset |
| Config | `0x0040`–`0x0045` (`:272`–`335`) | CONFIG_READ / WRITE / CHUNK / FIRMWARE_VERSION / READ_MSD / CONFIG_CLEAR |
| Session / power | `0x0050`–`0x0053` (`:355`–`452`) | AUTHENTICATE / ENTER_DFU / POWER_OFF / DEEP_SLEEP |
| Image + peripherals | `0x0070`–`0x0077` (`:465`–`549`) | DIRECT_WRITE_START/DATA/END, refresh notifications 0x73/0x74, LED_ACTIVATE/STOP, PARTIAL_WRITE_START, BUZZER |
| PIPE + NFC | `0x0080`–`0x0083` (`:579`–`651`) | PIPE_WRITE_START/DATA/END sliding window; NFC_ENDPOINT |

**Free / unallocated opcode ranges:** `0x0000`–`0x000E`, `0x0010`–`0x003F`, `0x0046`–`0x004F`, **`0x0054`–`0x006F`**, `0x0078`–`0x007F`, `0x0084`–`0xFFFF`. The `0x0054`–`0x005F` sub-band sits directly adjacent to the session/auth/power band, which is the natural home for a network/session-management family.

### 2. Universal framing (transport-independent by construction)

- REQUEST `[cmd_hi][cmd_lo][payload…]`, opcode BE, payload fields LE unless marked BE (`opendisplay_protocol.h:143-147`).
- RESPONSE/NOTIFICATION `[status][cmd_echo][data…]`; status `0x00` ACK / `0xFF` NACK / `0xFE` AUTH_REQUIRED (`:149-159`, `:662-664`).
- NACK error codes are **opcode-scoped**, never global (`:161-169`, SECTION 4). Same byte reused across handler namespaces.
- Encrypted envelope `[cmd:2 BE][nonce:16][ciphertext][tag:12]`, inner `[len:1][payload]`, AAD = the 2 opcode bytes, AEAD = **AES-128-CCM**, 12-byte tag / 16-byte nonce (`:173-182`, SECTION 8 `:851-859`). Always-plaintext bootstrap commands: AUTHENTICATE `0x50`, FIRMWARE_VERSION `0x43` (and READ_MSD `0x44` on Silabs).
- Auth gating: when security enabled, every command except AUTHENTICATE requires a live session else `[0xFE][cmd_echo]` (`:184-187`).

This framing carries **no GATT/handle/MTU assumption in the byte layout itself** — the only transport coupling is (a) how a message is delimited and (b) how responses arrive (BLE notifications vs. socket reads).

### 3. Config packet structure & the WiFi TLV (`0x26`)

Config is a TLV blob written via CONFIG_WRITE `0x0041` (+ CONFIG_CHUNK `0x0042`), first payload byte = packet type. Header comment maps types (`opendisplay_protocol.h:237-244`): `0x01 system … 0x26 wifi 0x27 security …`. The struct bodies live per-repo (`opendisplay_structs.py`), NOT in the header.

**`WifiConfig` (packet `0x26`, `opendisplay_structs.py:1023-1056`), 160 bytes:**
- `ssid` (32B), `password` (32B), `encryption_type` (1B, enum `WifiEncryptionType` NONE/WEP/WPA/WPA2/WPA3 `:369-376`)
- `server_host` (64B, null-padded) + `server_port` (2B **BIG-ENDIAN**, the one BE field) — promoted out of former reserved for the *pull* model; **firmware parses but never dials out** (dead weight today, WIFI_ARCHITECTURE M-note line 55).
- `reserved` (29B, must-be-zero).

**`SecurityConfig` (packet `0x27`, `:1059-1090`):** `encryption_enabled`, 16B AES master key, `session_timeout_seconds`, policy flags, reset pin. This is the pre-shared key store the auth handshake draws on.

### 4. Capability advertisement mechanisms today

- **BLE manufacturer data:** manufacturer id **9286 / 0x2446**; HA discovery is BLE-advert driven (`iot_class: local_push`).
- **`CommunicationModes` bitfield** (`opendisplay_structs.py:404-409`), a field of SystemConfig: `OD_COMM_MODE_BLE` (bit0), `OD_COMM_MODE_OEPL` (bit1), **`OD_COMM_MODE_WIFI` (bit2)**. **Bits 3–7 reserved.** This is the closest thing to a transport-capability advertisement, but it is a **config field the host writes**, read back only via CONFIG_READ `0x0040` — it is not in a BLE advert and there is no runtime "am I currently associated / what is my IP" report.
- **`CMD_READ_MSD 0x0044`** returns a 16-byte manufacturer-specific data record; the mDNS `msd` TXT record (28 hex chars = 14 bytes) is derived from it for on-network disambiguation.
- **`device_id` (4 bytes)** appears in the AUTHENTICATE challenge (`:342`) and is folded into the AES-CMAC.

### 5. `device_flags` bit allocation (`DeviceFlags`, `opendisplay_structs.py:412-419`)

| Bit | Name | Meaning |
|---|---|---|
| 0 | `OD_DEVICE_FLAG_PWR_PIN` | external display power mgmt |
| 1 | `OD_DEVICE_FLAG_XIAO_INIT` | Seeed XIAO low-power init |
| 2 | `OD_DEVICE_FLAG_WS_PP_INIT` | Waveshare PhotoPainter init |
| 3 | `OD_DEVICE_FLAG_PWR_LATCH` | MOSFET self-hold latch |
| 4 | `OD_DEVICE_FLAG_PWR_LATCH_DFF` | 74AHC1G79 D-FF latch (0x0052 releases it) |
| **5–7** | **reserved (free)** | — |

Note `device_flags` is a **hardware-init** field, not a capability-advert field; its docstring even says bits 5–7 reserved. It is a poor fit for a transport capability bit — `CommunicationModes` is the correct axis and already has WIFI. (The system-prompt task framing about "device_flags bit 5 (0x20) = Channel Sounding" refers to a *firmware build flag*, a distinct `device_flags` usage from this config-struct one — see CLAUDE.md; do not conflate.)

### 6. PIPE_WRITE sliding window (`0x0080`–`0x0082`) and its transport-dependence

The PIPE sub-protocol (SECTION 6, `opendisplay_protocol.h:552-616`, `833-841`) is a QUIC-style reliable-stream layer built on top of BLE **write-without-response**:
- START `0x0080`: 10-byte header (22 w/ partial), negotiates `ver`, `req_window`, `req_ack_every`, `client_max_frame` (2B LE), `total_size` (4B LE); device echoes negotiated maxima (min-rule).
- DATA `0x0081`: `[seq:1][data]`; device replies **SACK** `[0x00][0x81][highest_seen:1][ack_mask:4 LE]` — selective-repeat over a **33-slot reorder queue** (`:594`), ack cadence = negotiated N. Fatal NACK `[0xFF][0x81][err][highest_seen][ack_mask]`.
- END `0x0082`: tail-flush SACK, end-ACK, refresh notification.
- Constants: `PIPE_MAX_FRAME 244` (== GATT write ceiling), `PIPE_FRAME_OVERHEAD 3`, `PIPE_ACK_MASK_BITS 32`.

**Why it exists:** BLE write-without-response can silently drop packets and reorder across connection events, and there is no L2 retransmit — so PIPE re-implements windowing, sequence numbers, selective acknowledgement, and reordering at the application layer. **Over TCP none of this is needed:** TCP already guarantees in-order, gap-free, retransmitted delivery. The `client_max_frame` negotiation, `seq`, `ack_mask`, reorder queue, and window are all pure BLE-loss compensation. This is exactly why the shipped LAN path does **not** use PIPE at all: `@targets` for `0x0080`–`0x0082` is **Firmware only** and the LAN firmware streams via plain DIRECT_WRITE `0x0070/0x0071/0x0072` with a 4094-byte chunk (`FINDINGS_WIFI.md §1.3-1.4`). PIPE and WiFi are today mutually exclusive paths.

### 7. MTU / frame assumptions baked in

- `PIPE_MAX_FRAME 244` and the HA GATT write ceiling of 244 B (memory: HA native GATT client caps at 244, no long writes). Chunk budgets: unencrypted 230 / encrypted 154 (`:189-193`), config chunk 200 (`CONFIG_CHUNK_SIZE`, `:846`), `MAX_RESPONSE_DATA_SIZE 100` (`:849`).
- These are **BLE-derived ceilings.** LAN uses `WIFI_LAN_MAX_PAYLOAD 4096` → `LAN_CHUNK_SIZE 4094` (`FINDINGS_WIFI.md §1.4`, V3). Encrypted budget stays 154 on both transports (AES-CCM framing, not MTU).
- The config-read multi-chunk framing (`~100 B` frames) is transport-independent and works unchanged over LAN (V9).

### 8. Existing WiFi implementation surface (outside the header)

- **Firmware (ESP32 only):** `Firmware/src/wifi_service.cpp`, gated by `TARGET_ESP32`. Enabled by `communication_modes` bit2 **AND** a `wifi_config` TLV with non-empty SSID (`wifi_service.cpp:88-104`). TCP server on `wifiServerPort` default **2446** (`main.h:152`, overridable via TLV bytes 64-65). mDNS `_opendisplay._tcp`, hostname `OD<chipid>.local`, TXT `msd`. Single client (new connection evicts old + clears encryption session), 30 s read timeout, no keepalive. **BLE + WiFi concurrent** (`main.cpp:146-169`). Responses dual-delivered to LAN and BLE.
- **py-opendisplay:** `LANConnection` (TCP, length-prefixed, same `write_command/read_response` surface as BLE) exists on PR #89 / `feature/wifi`, un-merged; `discover_lan_devices()` (zeroconf), `WifiConfig` provisioning model. Transport chosen explicitly at device construction — no auto-fallback.
- **HA integration:** zero WiFi surface — no zeroconf matcher in `manifest.json`, BLE-advert-driven only.

---

## Transport-coupling analysis

### Transport-AGNOSTIC (runs unchanged over a TCP/WebSocket byte stream — already proven)

- **The entire opcode/payload byte layout** (SECTION 1). Firmware feeds LAN bytes into the identical `imageDataWritten` dispatcher (V6). No opcode's request/response bytes change by transport.
- **Config read/write/clear** (`0x0040`–`0x0045`), **firmware version** (`0x0043`), **MSD** (`0x0044`).
- **DIRECT_WRITE image path** (`0x0070/0x0071/0x0072`) + refresh notifications (`0x0073/0x0074`) — the LAN production path; only the *chunk size* differs (`_data_chunk_limit()`), sourced from a transport constant, not from the wire format.
- **Encryption envelope** — AES-128-CCM is transport-blind; the 154-byte encrypted budget is CCM-framing-driven, not MTU-driven, so it is identical on LAN (V6).
- **Auth handshake** (`0x0050`) and its plaintext-bootstrap rule.
- **NACK scoping model** (SECTION 4) and all opcode-scoped error namespaces.
- **The `[status][cmd_echo][data]` response shape**, including unsolicited notifications (refresh 0x73/0x74) — these map to socket reads just as they map to GATT notifications.

### Transport-COUPLED-to-BLE (assumes GATT / notifications / BLE loss / proximity)

- **Message delimiting.** BLE gets framing free from each GATT write; a byte stream does not. LAN re-adds a `[len:2 LE][payload]` frame (`FINDINGS_WIFI.md §1.2`). This framing is **defined only in firmware/docs, never in the header** — a header-level gap.
- **PIPE_WRITE sliding window (`0x0080`–`0x0082`)** — entirely BLE-loss compensation (SACK, seq, reorder queue, window, `client_max_frame` negotiation). Redundant over TCP. Do NOT port to WiFi; use the plain DIRECT_WRITE path with a large chunk instead.
- **MTU-derived constants** — `PIPE_MAX_FRAME 244`, chunk budgets 230/154, `MAX_RESPONSE_DATA_SIZE 100`. LAN uses 4096/4094; these numbers are transport parameters, not wire invariants.
- **Response delivery = GATT notification.** The header describes responses as "notification" (`:149`). Over TCP they are ordinary in-band socket reads. Semantically equivalent, but any doc that says "notification characteristic" is BLE-specific.
- **Connection/session semantics.** BLE session ends on link drop (`disconnected_callback`); LAN session ends on socket EOF or client eviction. The auth session-clear-on-disconnect rule (`_on_ble_disconnect`) has no header-level transport-neutral statement (gap G2 in FINDINGS_WIFI).
- **Identity = BLE MAC.** Over IP the device is a `host:port`; the header's device identity model (MAC + 4-byte device_id + 16-byte MSD) needs a transport-neutral binding.
- **Security-by-proximity.** BLE requires radio range + a paired host adapter — a weak but real physical-presence gate. IP has none: any host on the LAN can reach port 2446. When `encryption_enabled=0`, the LAN control plane is fully open (Firmware audit **M2**).

---

## Gap analysis (protocol-level, for WiFi)

### (a) Provisioning opcodes — MISSING

Today WiFi credentials are delivered **only** by writing the `0x26` WifiConfig TLV inside a normal CONFIG_WRITE (`0x0041`) over BLE, then rebooting. There is **no dedicated provisioning surface**:
- No "set credentials + join now, report result" opcode — provisioning is fire-and-pray via config + reboot.
- No **connection-status** query: a client cannot ask "are you associated? what IP? what RSSI? what error (bad PSK / AP not found)?" There is no runtime WiFi-state report anywhere in the protocol.
- No **forget/clear-credentials** primitive scoped to WiFi — `CONFIG_CLEAR 0x0045` wipes *all* config, and firmware doesn't wipe WiFi creds/AES key until reboot anyway (WIFI_ARCHITECTURE known issue).
- No **credential-storage-status** (are creds present? valid?).
- `server_host`/`server_port` fields exist but are dead (pull model unimplemented).

### (b) Transport capability advertisement — WEAK / INCOMPLETE

- `CommunicationModes` bit2 `OD_COMM_MODE_WIFI` says WiFi is *configured to be supported*, but it is a written config field, readable only via CONFIG_READ. There is **no BLE-advert capability bit** and **no runtime state** (associated / IP / port).
- A client on BLE has no in-protocol way to learn "this device also has a WiFi endpoint at 192.168.x.y:2446" — it must independently browse mDNS. There is no BLE→WiFi handoff descriptor.
- The `device_flags` field (bits 5–7 free) is the wrong axis (hardware-init flags); `CommunicationModes` (bits 3–7 free) is the right one but lacks a *live-state* companion.

### (c) Network transport framing — UNSPECIFIED IN HEADER

- The `[len:2 LE][payload≤4096]` LAN frame, TCP port 2446, mDNS `_opendisplay._tcp` / `msd` TXT, single-client eviction, 30 s timeout, no keepalive — all real in firmware, **none in the canonical header.** A "correct client from this file alone" is impossible for WiFi today.
- No handshake/auth-at-connect spec for TCP (relies on the shared `0x0050` flow, but that isn't stated as the network entry ritual).
- No keepalive / liveness spec (30 s server read timeout is an undocumented firmware constant).
- Open question the header should answer: **PIPE windowing does NOT apply over TCP** — use DIRECT_WRITE with the LAN chunk. This should be normative, not folklore.

### (d) Security — PROXIMITY ASSUMPTION BROKEN

- BLE's implicit physical-presence gate is gone on IP. With `SecurityConfig.encryption_enabled=0`, LAN exposes the **full control plane unauthenticated** (config write, reboot, DFU, power-off) to anyone on the network (Firmware audit **M2**). This is a genuine protocol-security gap, not just an implementation nit.
- The AES-128-CCM session works over TCP unchanged, but the protocol does not **require** auth on the IP transport specifically. Recommendation must state: IP transport SHOULD mandate an authenticated session (or a per-transport policy bit) regardless of the global `encryption_enabled`.
- Provisioning-over-BLE of the PSK is the trust-bootstrap; the header should tie "WiFi enabled" to "security session required on the WiFi transport."

### (e) Identity — MAC-BOUND, NEEDS STABLE DEVICE-ID

- Over BLE the device is its MAC. Over IP it's a `host:port` that can change (DHCP). The stable anchors that already exist — the 4-byte `device_id` (auth challenge) and the 16-byte MSD (`0x0044`, mirrored into the 14-byte mDNS `msd` TXT) — are the right binding, but the protocol never states "device_id / MSD is THE cross-transport identity; MAC and IP are just locators." HA needs this to match a mDNS discovery to a known BLE device.

### (f) Simultaneous BLE + WiFi semantics — UNDER-SPECIFIED

- Firmware already runs both concurrently and **dual-delivers responses** to LAN and BLE (V10) — but the protocol says nothing about it. Open questions with no header answer:
  - **Concurrent sessions:** two independent auth sessions (one per transport) or one shared? Firmware evicts on new LAN client and clears *that* session; BLE session is separate.
  - **Locking:** py-opendisplay has a process-global per-MAC BLE lock (memory: `feat/per-mac-ble-lock`); there's no cross-transport mutual exclusion. Two hosts (one BLE, one WiFi) could interleave DIRECT_WRITE sessions and corrupt a frame buffer.
  - **Which transport wins** for a stateful multi-frame operation (image upload, chunked config): the protocol needs a "one active write-session per device across all transports" rule.
  - **Response fan-out:** dual-delivery means a BLE client sees responses to a WiFi client's commands — the header should either forbid this or define it.

---

## Protocol-level proposal

Design goals: **additive/MINOR wherever possible** (old firmware must keep working), fit the existing banding, and make the header self-sufficient for a WiFi client.

### 0. Back-compat verification (does old firmware ignore unknown opcodes gracefully? — YES)

Confirmed in `Firmware/src/communication.cpp:693-695`: the command `switch` `default:` case logs `"ERROR: Unknown command"` to serial and **sends no response frame** — it silently drops. `commandHandler(...)` returns `nullptr` for unrecognized opcodes (`:530`, `:547-548`). NRF54 similarly falls through its dispatch default. **Implication:** new opcodes are safe as MINOR additions — a pre-WiFi device receiving a new provisioning opcode just drops it (client sees a timeout, not corruption). One caveat: a *silent* drop is indistinguishable from a lost packet; a client should treat "no response within timeout" as "unsupported" and fall back. (A future protocol nicety would be a generic `[0xFF][cmd_echo][UNSUPPORTED]` for unknown opcodes, but that is itself a behavior change and not required.)

### 1. Opcode allocation — new `0x0054`–`0x005F` NETWORK/SESSION band

Place a WiFi/network family in the free `0x0054`–`0x005F` sub-band, adjacent to auth/power/session (`0x0050`–`0x0053`). Sketch:

| Opcode | Name | Dir | Purpose |
|---|---|---|---|
| `0x0054` | `CMD_NET_STATUS` | host→dev | query transport state: `[0x00][0x54][comm_modes:1][wifi_state:1][ipv4:4][port:2 LE][rssi:1]`. `wifi_state` enum: 0 idle, 1 connecting, 2 associated, 3 auth-fail, 4 no-AP, 5 got-IP. Plaintext-readable (like fw-version) so a client can discover the WiFi endpoint over BLE. |
| `0x0055` | `CMD_NET_JOIN` | host→dev | provision + connect now, without a full config rewrite/reboot: `[0x00][0x55][enc_type:1][ssid_len:1][ssid][psk_len:1][psk]`. ACK on accept; async result via a `0x0054`-style notification when association settles. |
| `0x0056` | `CMD_NET_FORGET` | host→dev | wipe WiFi creds + drop association + clear the WiFi-side AES session; leave other config intact. Fills the (b)/(a) forget gap. |
| `0x0057` | `CMD_NET_CAPS` | host→dev | capability descriptor: supported transports bitmap, max LAN frame (echo 4096), whether PIPE applies over IP (0), keepalive interval, whether IP transport requires auth. Lets a client learn WiFi support + the framing parameters **from the protocol**, not from mDNS/firmware lore. |
| `0x0058`–`0x005F` | reserved | — | future: WS upgrade, TLS descriptor, multi-AP, static-IP. |

Rationale for **not** using `device_flags`: it is a hardware-init config field (bits 5–7 reserved per docstring). Transport capability belongs to `CommunicationModes` (already carries `OD_COMM_MODE_WIFI`) plus the new runtime `0x0054/0x0057` reports. If a single advertised bit is still wanted, allocate `CommunicationModes` bit3 for e.g. `OD_COMM_MODE_WIFI_WS` (WebSocket) rather than touching `device_flags`.

### 2. Network transport framing — promote into the header (normative)

Add a SECTION 9 "NETWORK TRANSPORT (LAN)" to `opendisplay_protocol.h` documenting what firmware already does, as constants + a framing block:
- `OD_LAN_TCP_PORT 2446u`, `OD_LAN_MAX_PAYLOAD 4096u`, `OD_LAN_FRAME_HEADER 2u` (`[len:2 LE][payload]`), `OD_LAN_MDNS_SERVICE "_opendisplay._tcp"`, `OD_LAN_READ_TIMEOUT_S 30u`.
- Normative statements: (1) on the network transport the **PIPE 0x0080–0x0082 family MUST NOT be used**; stream images via DIRECT_WRITE `0x0070/0x0071/0x0072` with data chunks up to `OD_LAN_MAX_PAYLOAD − 2`. (2) One client at a time; a new connection evicts the prior and clears its session. (3) The encrypted envelope and 154-byte encrypted chunk budget are identical to BLE. (4) `device_id`/MSD is the cross-transport identity; MAC and IP are locators only.
- Keep macro values simple (integer/string literals) per the header's LANGUAGE/LINKAGE RULE so `gen_python_protocol.py` still parses.

### 3. Config-field extensions

- Keep `WifiConfig` (`0x26`) as the persistent credential store; deprecate-in-place the dead `server_host/server_port` (mark reserved-again in a comment; do not reuse the bytes — old writers may still zero them).
- Optionally add config packet `0x2D` `network_policy`: a `require_auth_on_ip:1` byte + keepalive + allowed-transport mask, so the IP-transport security posture is configurable and readable. (`0x2D` is the next free packet type after `0x2C data_extended`.)

### 4. Security & identity model (for the header's framing prose)

- State that when the WiFi transport is active, an authenticated AES-128-CCM session **SHOULD be required** for all gated commands regardless of the global `encryption_enabled` — closing Firmware audit M2 at the spec level. Encode the policy in `0x2D.require_auth_on_ip`.
- State `device_id` (4B) + MSD (16B, mDNS `msd` = 14B) as the canonical cross-transport identity; MAC/IP are locators. This lets HA correlate a zeroconf discovery with a known device.

### 5. Simultaneous BLE + WiFi rules (normative prose)

- **One active stateful write-session per device across all transports** (image upload, chunked config): a START on one transport while another transport has an in-flight session MUST be NACKed (reuse existing "new START aborts prior" semantics, extended cross-transport). This prevents frame-buffer corruption from concurrent BLE+WiFi uploads and complements py-opendisplay's per-MAC lock with a device-side guard.
- **Independent auth sessions per transport** (matches firmware: LAN eviction clears only the LAN session). Document that responses to a command are delivered on the transport that issued it (i.e., forbid or explicitly scope the current BLE/LAN dual-delivery fan-out; dual-delivery leaks one client's responses to another and should be tightened).

### 6. Versioning / back-compat strategy

- All of the above is **additive** ⇒ **MINOR bump to 2.2** (new opcodes `0x0054`–`0x0057`, new SECTION 9 constants, new config packet `0x2D`, new `CommunicationModes` bit if used). No existing opcode/layout/error changes ⇒ not MAJOR. Follow the header's AGENT INSTRUCTIONS (`:100-113`): set LAST CHANGED, add an "Unreleased (since 2.1)" changelog bullet per item, then roll into a `2.2 (YYYY-MM-DD)` heading and bump `OD_PROTOCOL_VERSION_MINOR`→2 / `_STR`→"2.2".
- **Heed the 0x52/0x53 lesson** (`opendisplay_protocol.h:59-77`, `396-399`, `441-450`): that swap was a *breaking* renumber of a shipped opcode disguised inside a MINOR line and required an in-place correction + explicit "peer on old mapping hits wrong command" warning. For WiFi: **allocate into empty ranges only, never renumber a shipped opcode**, and never reuse a byte value across transports without opcode-scoping. Choose `0x0054+` (verified free) and leave `0x0080`-family untouched.

### 7. Header-change + sync workflow

Per README + CLAUDE.md — edit **only** `src/opendisplay_protocol.h`, then:
```bash
cd opendisplay-protocol
tools/gen_python_protocol.py --write && tools/gen_python_protocol.py --check   # "ok"
tools/gen_js_protocol.py --write     && tools/gen_js_protocol.py --check       # "ok"
tools/sync_protocol_header.py --push                                           # → 4 firmware copies
tools/sync_protocol_header.py --check                                          # "all in sync"
```
New config-struct fields (`network_policy 0x2D`, WifiConfig deprecation comment) go in the per-repo `opendisplay_structs.*` sources + their `structs_model` generators — a separate propagation from the header. Firmware adoption stays deliberate/per-repo (rollout-plan.md). CI drift gates (`--check`) must pass in every consumer before merge.

---

## Appendix — key file/line index

- Canonical header: `src/opendisplay_protocol.h` — opcodes SECTION 1 (`:234-651`), framing (`:140-193`), envelope SECTION 8 (`:851-859`), PIPE SECTION 6 (`:833-841`), config-type comment (`:237-244`), versioning policy (`:16-49`), agent instructions (`:100-113`).
- Config structs: `src/opendisplay_structs.py` — `WifiConfig`/0x26 (`:1023-1056`), `WifiEncryptionType` (`:369-376`), `CommunicationModes` (`:404-409`), `DeviceFlags` (`:412-419`), `SecurityConfig`/0x27 (`:1059-1090`).
- Firmware unknown-opcode drop: `Firmware/src/communication.cpp:693-695`, `:530`, `:547-548`.
- WiFi design notes: `agents/Home_Assistant_Integration/WIFI_ARCHITECTURE_2026-07-06.md`, `agents/py-opendisplay/FINDINGS_WIFI.md` (V1–V11 firmware validation; gaps G1–G3), Firmware audit M2 (`docs/AUDIT_2026-07-19_Firmware.md:97-104`).
