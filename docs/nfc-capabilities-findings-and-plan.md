# NFC Capabilities — Findings & Implementation Plan

**Status:** research / design (no code changed)
**Date:** 2026-07-17
**Scope:** `opendisplay-protocol` (spec) · `Firmware` · `Firmware_Silabs` · `py-opendisplay` · `Home_Assistant_Integration` (`feat/clean-port`)
**Goal:** validate which NFC protocol features firmware actually implements, then design the `py-opendisplay` client layer and the Home Assistant service-call surface (`services.py` / `services.yaml`) that expose them to YAML.

---

## 0. Executive summary

The BLE wire protocol defines a complete NFC endpoint (`CMD_NFC_ENDPOINT = 0x0083`): read a tag, single-shot write, and a 3-stage chunked write, across five NDEF record types. **Firmware_Silabs is the one true, full implementation** (a real bit-banged-I²C TNB132M Type-3 tag driver with an NDEF encoder *and* decoder). The combined `Firmware` (ESP32 / nRF52840) has **zero** NFC code. `py-opendisplay` **already ships the write path** (v7.13.0) but **not read**, and Home Assistant **cannot reach any of it yet** because it pins the pre-NFC `py-opendisplay==7.12.0` and defines no NFC services.

The work is therefore smaller than it looks: **add NFC read to `py-opendisplay`**, **bump the HA pin**, and **add two thin HA services** (`nfc_write`, `nfc_read`). No firmware change is required for Silabs; `Firmware` NFC is a separate, larger hardware effort (out of scope to implement, documented here as a gap).

### Verdict matrix — what is actually implemented, per layer

| Capability | Protocol spec | `Firmware` (ESP32/nRF52) | `Firmware_Silabs` | `py-opendisplay` 7.13.0 | HA `feat/clean-port` |
|---|:--:|:--:|:--:|:--:|:--:|
| Endpoint `0x0083` dispatch | ✅ | ❌ absent | ✅ | ✅ | ❌ |
| `NFC_SUB_READ` (0x00) | ✅ | ❌ | ✅ | ❌ **missing** | ❌ |
| `NFC_SUB_WRITE` single (0x01) | ✅ | ❌ | ✅ | ✅ | ❌ (pin too old) |
| `NFC_SUB_WRITE_START/DATA/END` (0x10–0x12) | ✅ | ❌ | ✅ | ✅ | ❌ (pin too old) |
| Record types TEXT/URI/WK-RAW/MIME/RAW-NDEF | ✅ | ❌ | ✅ enc+dec | ✅ (write) | ❌ |
| Tag IC driver (TNB132M, Type-3) | — | ❌ | ✅ real | n/a | n/a |
| Field-detect / energy-harvest wake | — | ❌ | ✅ | n/a (config pkt only) | n/a |
| YAML service call | — | — | — | — | ❌ **to build** |

Legend: ✅ implemented · ❌ absent · "pin too old" = code exists upstream but the pinned dependency version predates it.

> **Reference target not in scope but informative:** `Firmware_NRF54` has the full `0x0083` dispatch/framing but its backend (`opendisplay_ble_nfc_read/write`) is a **stub that always returns `false`** (`opendisplay_ble.c:735-751`), so it NACKs every NFC frame. Do **not** treat NRF54 as a working reference — only Silabs is.

---

## 1. The protocol contract (grounding)

Canonical source: `opendisplay-protocol/src/opendisplay_protocol.h`, SECTION 1 (`CMD_NFC_ENDPOINT`, line 561) + SECTION 5 (NFC sub-protocol, lines 678–717).

**Endpoint:** `CMD_NFC_ENDPOINT = 0x0083`, echo byte `RESP_NFC_ENDPOINT = 0x83`. (Moved off `0x0082` in protocol v2 to end the historical collision with `CMD_PIPE_WRITE_END`; old `0x0082` NFC frames are rejected — clean cutover, no alias.)

**Request:** `[0x00][0x83][sub][sub-payload…]`

| Sub-command | Byte | Payload |
|---|:--:|---|
| `NFC_SUB_READ` | 0x00 | (none) |
| `NFC_SUB_WRITE` | 0x01 | `[rec_type:1][len:2 BE][payload:len]` (single-shot, ≲120 B app data) |
| `NFC_SUB_WRITE_START` | 0x10 | `[rec_type:1][total_len:2 BE]` (chunk buffer ≤ 512 B) |
| `NFC_SUB_WRITE_DATA` | 0x11 | `[chunk…]` (append; needs active START) |
| `NFC_SUB_WRITE_END` | 0x12 | (none) commit; `received == total` |

**Record types** (`OD_NFC_REC_*`): `TEXT=0`, `URI=1`, `WELL_KNOWN_RAW=2`, `MIME=3`, `RAW_NDEF=4`.
**IC selector** (`OD_NFC_IC_*`): `AUTO=0`, `TNB132M=1`.

**Response** — 3rd byte is an NFC *sub-status* (a distinct field from the opcode echo; it reuses values 0x80/0x81/0x82):
- READ ok: `[0x00][0x83][0x80 READ_DATA][rec_type:1][len:2 BE][data:len]`
- WRITE ok (single / END): `[0x00][0x83][0x81 WRITE_COMMITTED]`
- START / DATA ok: `[0x00][0x83][0x82 CHUNK_ACCEPTED]`

**NACK:** `[0xFF][0x83][0xFF][NFC_ERR_*]`, codes `0x01` malformed · `0x02` read failed · `0x03` tag write failed · `0x04` unknown sub · `0x05` invalid rec_type · `0x06` bad total_len (0 or >512) · `0x07` chunk without active START / wrong connection · `0x08` chunk overflow · `0x09` END length mismatch.

**State:** chunked write is per-connection — START binds the connection; DATA/END from another connection → `0x07`. Buffer 512 B.

---

## 2. Firmware validation — ground truth

### 2.1 `Firmware` (ESP32-S3/C6/C3 + nRF52840) — **NFC entirely absent**

- Repo-wide case-insensitive `nfc` search returns exactly two hits, both inert Nordic board boilerplate: `variants/nrf52840custom/variant.h:81-82` (`PIN_NFC1`/`PIN_NFC2` antenna pins, unreferenced).
- The sole BLE dispatch — `switch(command)` at `src/communication.cpp:583` — has **no `case 0x0083`**. An NFC frame falls to `default:` (`communication.cpp:664`) → logs `Unknown command: 0x83`, does nothing.
- `0x0082` here is **PIPE_WRITE_END** (`communication.cpp:637` → `handlePipeWriteEnd`), not legacy NFC. There is no TNB132M/NT3H/I²C-NFC driver, no NDEF codec, no build flag. Nothing to enable.

**Consequence:** NFC on ESP32/nRF52840 tags is a from-scratch hardware+firmware project (tag IC wiring, I²C driver, NDEF codec, dispatch). Documented as a gap; **not** part of this plan's build work.

### 2.2 `Firmware_Silabs` (EFR32BG22) — **complete, real implementation**

Always compiled in (no SLC component, no `#ifdef` gate); enabled at **runtime** by config packet `0x2A` (`CONFIG_PKT_NFC`).

- **Dispatch:** `opendisplay_pipe.c:1152` `case CMD_NFC_ENDPOINT → handle_nfc_endpoint()` (handler `pipe.c:912-1065`), reached only after the security gate (`pipe.c:1076-1081`) — **NFC requires an authenticated session when encryption is configured.**
- **All five sub-commands implemented** (READ `pipe.c:923`, WRITE `:941`, START `:972`, DATA `:1003`, END `:1033`), with all nine `NFC_ERR_*` codes and all three status codes reachable (per-code line map verified).
- **All five record types, encode *and* decode.** Encoder `od_nfc_write_record_raw` (`opendisplay_ble.c:1252-1410`) builds proper NDEF: Text (`D1 01 len 54 02 'e' 'n' …`, lang forced "en"), URI (`… 55 00 …`, no abbreviation), Well-Known-Raw, MIME (`D2 …`), Raw-NDEF verbatim. Decoder `od_nfc_read_record_raw` (`ble.c:1076-1250`) parses the first record back into the same record-type taxonomy; non-SR records rejected.
- **Tag IC:** TNB132M NFC-Forum **Type-3** tag over **bit-banged I²C** (`ble.c:402-479` primitives; Type-3 paged block read `:481`, write `:633`; AIB @ dev `0x48` sub `0`, NDEF data @ dev `0x40`; prime sequence `od_nfc_tnb132m_prime_type3` `:747`). `OD_NFC_IC_AUTO` maps to the TNB132M path (no autodetect). Optional power-GPIO; pins parked as inputs after each op to save power.
- **Field-detect / energy-harvest wake:** `od_nfc_field_detect_*` (`ble.c:781-947`) — polling or GPIO-IRQ, debounce, EM4 deep-sleep wake arm (`:905`), state+scan-counter surfaced into the advertisement. (Configured via the `0x2A` packet; not part of the `0x0083` request surface.)
- **Enforced limits (important — they differ from the spec's soft notes):**
  - Chunk staging buffer **512 B**; START rejects `total_len==0 || >512` → `0x06`.
  - **READ is capped at 128 B** (`s_od_nfc_read_data[128]`, `ble.c:130`; `ln==0 || ln>128` → read fails → `0x02`) — *not* the 238 B the response header could theoretically carry.
  - Per-record NDEF payload capped at 255 (single-byte SR length); single-write OTA bounded by `OD_PIPE_MAX_PAYLOAD=244`. No literal 120 B constant is enforced by firmware — the 120 is a *client-side* conservative inline threshold (see §3).
- **Chunk state** is a single static global `s_nfc_write_chunk` (`pipe.c:42-51`) — one in-flight chunked write per device; START binds `.connection`; reset on init/disconnect (`pipe.c:1254`).

**Silabs is the reference for wire behaviour.** Every client and HA design decision below is validated against it.

---

## 3. `py-opendisplay` — current state & proposed architecture

### 3.1 What already exists (v7.13.0)

The **write path is fully implemented** (commit `1d3051d` "feat: add write_nfc device methods", released in **7.13.0** — confirmed *not* in 7.12.0):

- Constants: `CommandCode.NFC_ENDPOINT = 0x0083` and `NFC_SUB_*`, `NFC_INLINE_MAX=120`, `NFC_CHUNK_SIZE=120`, `NFC_WRITE_MAX_TOTAL=512` (`protocol/commands.py:48,82-90`); status/error map `NFC_STATUS_*` + `NFC_ERROR_MESSAGES` (`protocol/responses.py:462-480`); `NfcRecordType`/`NfcIcType`/`NfcFieldDetectMode` (`models/enums.py:127-150`).
- Frame builders `build_nfc_write_inline/start/data/end_command` (`protocol/commands.py:457-546`) — e.g. inline emits `[0x00][0x83][0x01][rec_type][len:2 BE][payload]`, byte-for-byte matching the Silabs handler.
- Device methods on `OpenDisplayDevice`: `write_nfc(rec_type, payload)` plus `write_nfc_url/_text/_mime` (`device.py:1303-1374`), stop-and-wait chunking, `@_serialized` (per-device command lock) so the whole multi-frame sequence is atomic. First-read timeout is remapped to `NfcNotSupportedError` (silent old firmware); `TIMEOUT_NFC_WRITE=15.0 s` for the EEPROM commit.
- Typed exceptions `NfcWriteError(error_code)`, `NfcNotSupportedError` (`exceptions.py`), re-exported top-level.
- Tests: `tests/unit/test_device_nfc_write.py` (+ builder/validator/packet-ordering unit tests) using a hand-rolled `_FakeConnection`.

### 3.2 Gaps to fill

1. **`NFC_SUB_READ` (0x00) is defined but not built** — no `build_nfc_read_command`, no read device method, no read-response parser.
2. **No `opendisplay nfc` CLI subcommand** (nice-to-have; not required for HA).

### 3.3 Proposed design — add the read path (mirror the write path exactly)

**A. Builder** (`protocol/commands.py`, next to the write builders):
```python
def build_nfc_read_command() -> bytes:
    return CommandCode.NFC_ENDPOINT.to_bytes(2, "big") + bytes([NFC_SUB_READ])
```
Export via `protocol/__init__.py __all__`.

**B. Response parser** (`protocol/responses.py`, beside `validate_nfc_response`):
```python
def parse_nfc_read_response(frame: bytes) -> tuple[NfcRecordType, bytes]:
    # ok:  [0x00][0x83][0x80][rec_type:1][len:2 BE][data:len]
    # nack:[0xFF][0x83][0xFF][err]  -> raise NfcReadError(err, NFC_ERROR_MESSAGES[err])
```
Validate: status byte `0x00`, echo `0x83`, sub-status `NFC_STATUS_READ_DATA (0x80)`, then `len` matches the trailing bytes. Cap-aware: firmware returns ≤128 B, so no reassembly is needed — **read is a single request/response, not chunked.**

**C. Device method** (`device.py`, `@_serialized`, guard `self._connection is None`):
```python
async def read_nfc(self) -> NfcRecord:            # NfcRecord = dataclass(record_type, payload)
    await self._write(build_nfc_read_command())
    frame = await self._read_nfc(self.TIMEOUT_NFC_WRITE)   # reuse first-read→NfcNotSupportedError remap
    return parse_nfc_read_response(frame)
```
Add a small `NfcRecord` dataclass in `models/` (or return `tuple[NfcRecordType, bytes]`) and, optionally, convenience decoders (`as_text()`, `as_uri()`) that interpret the payload — but keep decoding minimal since firmware already returns the payload in record-type-tagged form.

**D. Exception:** add `NfcReadError(ProtocolError)` carrying `error_code: int | None` (parallel to `NfcWriteError`); re-export via `opendisplay/__init__.py`.

**E. Tests:** `tests/unit/test_device_nfc_read.py` using the `_FakeConnection` template — script a `0x80` read-data frame and each `NFC_ERR_*` NACK; assert exact request bytes and the decoded `(record_type, payload)`. Encrypted-path test reusing `encrypt_command` to build a genuine device→host frame.

**F. (Optional) CLI:** `_add_nfc_parser` / `_cmd_nfc` in `cli.py` following the `_add_sleep_parser`/`_cmd_sleep` template (`nfc read`, `nfc write --text/--url/--mime`), registered in `main`.

**Release:** cut a new `py-opendisplay` (release-please; next tag e.g. **7.14.0**) carrying `read_nfc`. Strict-typed (`mypy --strict`, `from __future__ import annotations`), line length 120.

### 3.4 Contract notes the design must honour

- **Auth:** when the tag has encryption configured, the Silabs NFC handler is behind the session gate. `OpenDisplayDevice` already applies the AES-128-CCM envelope transparently once authenticated — the HA layer passes the stored `encryption_key`, so no NFC-specific crypto work is needed.
- **Inline vs chunked threshold:** keep the client's `NFC_INLINE_MAX=120` (well under the 244 B frame ceiling and the firmware's 255 B per-record cap). Payloads >120 B use START/DATA/END automatically.
- **Read cap:** surface firmware's 128 B read limit in docs; a longer tag payload returns `NFC_ERR_READ_FAILED (0x02)` → `NfcReadError`.

---

## 4. `Home_Assistant_Integration` — proposed service design

Target branch **`feat/clean-port`** (base for feature branches). All idioms below are lifted from the existing `activate_buzzer` / `activate_led` services.

### 4.0 Prerequisite — bump the dependency pin

`custom_components/opendisplay/manifest.json` currently pins `py-opendisplay[silabs-ota]==7.12.0`, which **predates `write_nfc`**. Bump to the release carrying `read_nfc` (≥ **7.14.0** per §3.3):
```json
"requirements": ["py-opendisplay[silabs-ota]==7.14.0", "odl-renderer==0.5.12"],
```
Without this bump, *no* NFC service can work — even write.

### 4.1 Two new services

- **`opendisplay.nfc_write`** — write a record to the tag. Returns `None`.
- **`opendisplay.nfc_read`** — read the tag; returns the record via `SupportsResponse.OPTIONAL`.

### 4.2 `services.py`

Register in `async_setup_services` (string-literal names — no `SERVICE_*` const in this codebase):
```python
hass.services.async_register(DOMAIN, "nfc_write", _async_nfc_write, schema=SCHEMA_NFC_WRITE)
hass.services.async_register(
    DOMAIN, "nfc_read", _async_nfc_read, schema=SCHEMA_NFC_READ,
    supports_response=SupportsResponse.OPTIONAL,
)
```

Schemas (module-level `vol.Schema`, mirroring `SCHEMA_ACTIVATE_BUZZER`):
```python
SCHEMA_NFC_WRITE = vol.Schema({
    vol.Required(ATTR_DEVICE_ID): cv.string,
    vol.Required("record_type", default="text"):
        vol.In(["text", "uri", "well_known_raw", "mime", "raw_ndef"]),
    vol.Required("payload"): cv.string,          # UTF-8 text / URI; base64 for binary rec types
})
SCHEMA_NFC_READ = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})
```

Write handler (follows `_async_activate_buzzer` exactly — resolve, gate, run through the shared runner):
```python
async def _async_nfc_write(call: ServiceCall) -> None:
    entry = _get_entry_for_device(call)
    _raise_if_nfc_unsupported(entry, call.data[ATTR_DEVICE_ID])   # gate like no_buzzers/no_leds
    _raise_if_sleeping(entry, call.data[ATTR_DEVICE_ID])
    rec = _RECORD_TYPE_MAP[call.data["record_type"]]              # -> NfcRecordType
    payload = _decode_payload(call.data["record_type"], call.data["payload"])  # str→utf-8 / base64

    async def _write(device: OpenDisplayDevice) -> None:
        await device.write_nfc(rec, payload)

    await _async_connect_and_run(call.hass, entry, _write)
```

Read handler needs a return value. `_async_connect_and_run` returns `None`, so capture via a closure list (or add a return-passing variant of the runner):
```python
async def _async_nfc_read(call: ServiceCall) -> ServiceResponse:
    entry = _get_entry_for_device(call)
    _raise_if_nfc_unsupported(entry, call.data[ATTR_DEVICE_ID])
    _raise_if_sleeping(entry, call.data[ATTR_DEVICE_ID])
    captured: list[NfcRecord] = []

    async def _read(device: OpenDisplayDevice) -> None:
        captured.append(await device.read_nfc())

    await _async_connect_and_run(call.hass, entry, _read)
    rec = captured[0]
    return {"record_type": rec.record_type.name.lower(),
            "payload": _encode_payload(rec)}      # text/uri → str, binary → base64
```

Reuse the existing error mapping in `_async_connect_and_run` (`services.py:420-451`): timeout/BLE/`OpenDisplayError` → `HomeAssistantError("upload_error")`; auth failures → reauth + `authentication_error`. Add NFC-specific mapping only if desired (e.g. `NfcReadError` → a new `nfc_read_error` translation key). **Do not** construct `OpenDisplayDevice` directly and **do not** use the executor — NFC calls are async client calls inside `action(device)`.

**Capability gate `_raise_if_nfc_unsupported`:** mirror `no_leds`/`no_buzzers`. The tag's NFC config arrives in config packet `0x2A`; expose it on `entry.runtime_data.device_config` (e.g. `device_config.nfc` / `.nfc_configs`) and raise `ServiceValidationError(translation_key="no_nfc")` when absent. (If `device_config` does not yet parse `0x2A`, that parse is a small precursor task — `py-opendisplay` already models `NfcIcType`/`NfcFieldDetectMode`.)

### 4.3 `services.yaml`

```yaml
nfc_write:
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: opendisplay
    record_type:
      required: true
      default: text
      selector:
        select:
          translation_key: nfc_record_type
          options: [text, uri, well_known_raw, mime, raw_ndef]
    payload:
      required: true
      example: "https://opendisplay.org"
      selector:
        text:

nfc_read:
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: opendisplay
```

### 4.4 Translations & tests

- Add `nfc_write` / `nfc_read` blocks under `"services"` in **both** `strings.json` and `translations/en.json` (name + description + per-field text), the `nfc_record_type` select option labels, and any new `"exceptions"` keys (`no_nfc`, optionally `nfc_read_error`).
- Tests in `tests/test_services.py` reusing `_make_env` / `_device_ctx_factory` / `_patches`, with `device.write_nfc = AsyncMock()` and `device.read_nfc = AsyncMock(return_value=NfcRecord(...))`, asserting `.assert_awaited_once()` and (for read) the returned `ServiceResponse` shape. Add `device_config` an NFC capability so the gate passes.

### 4.5 Example user YAML (the deliverable's end goal)

```yaml
# Write a URL to the tag
action: opendisplay.nfc_write
data:
  device_id: !input tag_device
  record_type: uri
  payload: "https://opendisplay.org/tag/42"

# Read the tag and use the result
action: opendisplay.nfc_read
target: {}
data:
  device_id: !input tag_device
response_variable: tag
```

---

## 5. Rollout, sequencing & risks

### Sequencing (dependency order)
1. **`py-opendisplay`** — add `read_nfc` + parser + `NfcReadError` + tests (+ optional CLI). Release **7.14.0**. *(Independent; ships first.)*
2. **`Home_Assistant_Integration` (`feat/clean-port` → feature branch)** — bump manifest pin to `==7.14.0`; parse `0x2A` NFC capability onto `device_config` (if not already); add `nfc_write` + `nfc_read` services, schemas, `services.yaml`, translations, tests.
3. **Hardware validation** — against a **Silabs** tag only (the sole working firmware): write each record type, read it back, exercise the chunked path (>120 B), and the error frames (bad rec_type, oversize, read-empty).

### Risks / watch-items
- **Only Silabs works.** `nfc_write`/`nfc_read` against ESP32/nRF52840 (`Firmware`) or nRF54 tags will fail — `Firmware` NACKs `Unknown command`, nRF54 NACKs from its stub. The capability gate (`_raise_if_nfc_unsupported`, driven by the `0x2A` config presence) is what keeps users from hitting this; ensure only Silabs tags advertise NFC config. Consider surfacing `NfcNotSupportedError` distinctly.
- **Auth required.** On encrypted tags, NFC needs a live session; HA's existing key handling covers this, but a tag with no stored key will reauth-loop — same behaviour as other gated services.
- **Read size cap 128 B** (firmware), inline write threshold 120 B (client) — document both; larger reads return `0x02`.
- **Single in-flight chunked write per device** (Silabs static state) — the client's `@_serialized` per-device lock already prevents concurrent chunked writes from HA.
- **`0x0082` legacy:** none of this touches the retired v1 opcode; the clean cutover is already in the shipped protocol header.
- **`Firmware` NFC** (ESP32/nRF52840) remains an open hardware gap — out of scope here; if pursued it needs a tag IC + I²C driver + NDEF codec + `0x0083` dispatch, using Silabs as the behavioural reference.

### Definition of done
- `py-opendisplay` 7.14.0 released with `read_nfc` + green tests; `mypy --strict`/`ruff`/`prek` clean.
- HA services `nfc_write`/`nfc_read` callable from YAML, gated on capability, with translations and tests; manifest pinned to 7.14.0.
- End-to-end verified on a Silabs tag: write→read round-trip for TEXT and URI, one chunked (>120 B) write, and at least one error path.
