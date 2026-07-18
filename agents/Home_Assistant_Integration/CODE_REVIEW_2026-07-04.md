# OpenDisplay Home Assistant Integration — Code Review & Remediation Plan

**Date:** 2026-07-04
**Scope:** `custom_components/opendisplay/` @ `feat/clean-port` (v3.0.0-beta.7), cross-referenced against `py-opendisplay` v7.10.0
**Method:** Full manual read of all 11 Python modules, manifest/HACS metadata, and the relevant py-opendisplay source (`device.py`, `battery.py`, `display_palettes.py`).

---

## Executive summary

The integration is in good architectural shape: it follows modern HA patterns (typed `ConfigEntry` runtime data, passive-BLE coordinator, translation-keyed exceptions, reauth flows), and the tricky BLE/OTA edge cases are handled thoughtfully and documented in-code. The review found **5 high-priority issues** (one real performance problem, one latent crash, one dead feature path, two lifecycle/API gaps), **7 medium-priority robustness/design items**, and **7 hygiene items** — the most significant being the complete absence of tests.

Nothing found is a blocker for the current beta, but items 1–5 should land before a stable 3.0.0.

---

## Architecture overview

| Module | Lines | Role |
|---|---|---|
| `__init__.py` | 209 | Entry setup: BLE connect, firmware/config read, device registry, reboot-reload |
| `services.py` | 705 | `upload_image`, `drawcustom`, `activate_led`, `activate_buzzer` |
| `coordinator.py` | 150 | Passive BLE advertisement parsing, button/touch trackers, reboot-flag edge detection |
| `config_flow.py` | 243 | Discovery, user, encryption-key, reauth steps |
| `update.py` | 283 | GitHub release polling + Silabs BLE OTA install |
| `event.py` | 156 | Button/touch event entities |
| `sensor.py` | 134 | Temperature, RSSI, last-seen, battery sensors |
| `image.py` | 63 | Last-uploaded-image entity |
| `entity.py` / `diagnostics.py` / `const.py` | 37/40/5 | Base entity, diagnostics, constants |

Data flow: `__init__` creates the coordinator and `OpenDisplayRuntimeData`; advertisements drive `sensor`/`event`; `services.py` opens active BLE connections for uploads and dispatches the rendered JPEG to `image.py`; the coordinator's reboot-flag edge triggers an entry reload (deferred past any in-flight `upload_image` task).

---

## High-priority findings

### 1. Event-loop blocking during image upload

**Where:** `custom_components/opendisplay/services.py:353-361` → `py-opendisplay/src/opendisplay/device.py:1136`

`_async_send_image` calls `device.upload_image(img, ...)`. Inside py-opendisplay, `upload_image` calls `self._prepare_image(...)` **synchronously** within the async method — that is the full fit → dither → bitplane-encode → zlib-compress pipeline (NumPy/PIL, hundreds of ms for a large color panel such as a 7.3" Spectra). This runs on the Home Assistant event loop and will stall every other integration for the duration. HA dev guidelines require CPU-bound work in the executor.

py-opendisplay explicitly provides the offload path for exactly this consumer pattern:

- module-level **sync** `prepare_image(...)` (`device.py:151`) — safe to run in an executor, needs only the cached `GlobalConfig`/capabilities, no connection
- `await device.upload_prepared_image(prepared_tuple, ...)` (`device.py:1178`) — BLE-only I/O

**Fix:** in `_async_send_image`, run `prepare_image` via `hass.async_add_executor_job(...)` (passing the display config already held in `entry.runtime_data.device_config`), then call `upload_prepared_image` inside `_async_connect_and_run`. Bonus: preparation happens *before* the BLE connection opens, shortening connection hold time and battery cost on the device.

### 2. Internal/relative URL support is dead code

**Where:** `custom_components/opendisplay/services.py:261-264` and `services.py:386-393`

`_async_download_image` contains a branch for non-`http(s)` URLs that signs the path (`get_url(hass) + async_sign_path(...)`) — clearly intended to support internal HA paths like `/api/camera_proxy/camera.foo`. But this branch is unreachable from `upload_image`:

- The schema (`services.py:114-116`) accepts `cv.url` or a media-selector dict. A relative path may pass `cv.url` (it is permissive), but then—
- The allowlist check at `services.py:386` rejects **any** string not passing `hass.config.is_allowed_external_url(...)`. A relative path can never be in `allowlist_external_urls`, so it always raises `url_not_allowed` before download.

(The other caller, the media-source branch at `services.py:415`, passes `media.url` which for local media sources is already a signed absolute URL or a `media.path`.)

**Fix (choose one):**
- **Support internal paths properly:** skip the `is_allowed_external_url` check when `url.startswith("/")` (internal paths are same-origin and get short-lived signed access; the allowlist is meant for *external* URLs), keeping the check for absolute external URLs. This makes "push a camera snapshot to the display" work without allowlisting.
- **Or delete the signing branch** and document that only allowlisted external URLs and media-source items are accepted.

The first option matches the apparent intent (`use_content_user=True` was chosen deliberately).

### 3. Battery sensor crashes on advertisement without battery data

**Where:** `custom_components/opendisplay/sensor.py:112-114` → `py-opendisplay/src/opendisplay/battery.py:94-123`

The battery-percent `value_fn` calls `voltage_to_percent(upd.advertisement.battery_mv, capacity_estimator)`. `battery_mv` is `int | None` (advertisements can omit it), but `voltage_to_percent` type-hints `voltage_mv: int` and passes it straight into `_interpolate(...)` with no None guard — a `None` raises `TypeError` inside the entity state machine, producing an error-logged unavailable state on every advertisement.

**Fix:** guard in the lambda:

```python
value_fn=lambda upd: (
    voltage_to_percent(upd.advertisement.battery_mv, capacity_estimator)
    if upd.advertisement.battery_mv is not None
    else None
),
```

(Optionally also fix upstream in py-opendisplay to accept `int | None`.)

### 4. `drawcustom` uploads bypass the upload-task bookkeeping

**Where:** `custom_components/opendisplay/services.py:395-401` (registration, `upload_image` only) vs. `services.py:620-629` (`drawcustom`, none); consumer at `custom_components/opendisplay/__init__.py:180-195` and `__init__.py:202-205`

`entry.runtime_data.upload_task` drives three behaviors: cancel-on-supersede (a newer upload cancels the older), reboot-reload deferral (`_async_reload_after_reboot` waits for the in-flight upload), and cancel-on-unload. Only `_async_upload_image` registers itself. A `drawcustom` upload is invisible to all three, so:

- a reboot-flag edge mid-drawcustom reloads the entry and tears down the connection under the upload;
- two rapid `drawcustom` calls to the same device race instead of superseding;
- unload does not cancel it.

**Fix:** extract the register/supersede/clear block from `_async_upload_image` into a small helper (e.g. `async def _run_as_upload_task(entry, coro)`) and wrap the per-device upload in `_drawcustom_for_device` with it too. Note `drawcustom` targets multiple devices — the task slot is per-entry, so this composes fine.

### 5. Update entity uses the deprecated int `in_progress` API

**Where:** `custom_components/opendisplay/update.py:183` and `update.py:86`

`_on_progress` sets `self._attr_in_progress = new_pct` (an int). Since HA 2024.11, `in_progress` must be a bool and the percentage belongs in `_attr_update_percentage`; the int form's deprecation window has closed well before the integration's minimum HA of 2026.4 (`hacs.json`). Progress will not display (or will warn/fail) on target HA versions.

Also, `should_poll = True` shadows the base-class *property* with a class attribute. It works via attribute-lookup precedence but is fragile; the HA-idiomatic form is `_attr_should_poll = True`.

**Fix:**

```python
_attr_should_poll = True
...
self._attr_in_progress = True
self._attr_update_percentage = new_pct   # in _on_progress
...
self._attr_in_progress = False
self._attr_update_percentage = None      # in finally
```

---

## Medium-priority findings

### 6. `drawcustom` aggregate errors always raised as `ServiceValidationError`

`services.py:549-560` collects per-device failures (including BLE connection failures wrapped as `HomeAssistantError`) and re-raises them all as `ServiceValidationError` — which HA presents as "you called the service wrong" and excludes from some retry/trace semantics. **Fix:** raise `HomeAssistantError` for the aggregate unless every underlying failure was a `ServiceValidationError`. Additionally, devices are processed serially; each has its own BLE link, so `asyncio.gather` over `_drawcustom_for_device` would cut multi-device latency (bounded by adapter slots — consider a small semaphore).

### 7. Duplicated device-entry resolution

`_get_entry_for_device` (`services.py:202-235`) and `_get_entry_for_device_id` (`services.py:480-508`) are ~90 % identical. **Fix:** `_get_entry_for_device(call)` should become `return _get_entry_for_device_id(call.hass, call.data[ATTR_DEVICE_ID])`.

### 8. `image.py` duplicates base-entity wiring and loses state on restart

`OpenDisplayImageEntity` (`image.py:26-41`) hand-builds `DeviceInfo` and unique_id instead of extending `OpenDisplayEntity` (it can't trivially, because `ImageEntity.__init__` needs `hass` and the base is coordinator-bound — but that constraint deserves a comment, or a thin mixin for the DeviceInfo/unique_id pattern). More user-visible: `_image_bytes` is memory-only, so the entity goes blank on every HA restart **and every entry reload — including the automatic reboot-triggered reload**, which makes the displayed image disappear from HA whenever the device reboots even though the panel still shows it. **Fix:** persist the last JPEG (e.g. `hass.config.path(f".storage/opendisplay.{address}.jpg")` written in the executor, loaded in `async_added_to_hass`), or explicitly document the limitation.

### 9. Duplicated event-entity update logic

`OpenDisplayEventEntity` and `OpenDisplayTouchEventEntity` (`event.py:114-156`) share the same `_last_processed_data` guard and differ only in event matching/payload. **Fix:** a shared base with `_matches(event)` / `_event_payload(event)` hooks, or a single class parameterized by the description.

### 10. `upload_task` typing narrowing

`services.py:395` assigns `asyncio.current_task()` (typed `Task | None`) into `runtime_data.upload_task`. Inside a running service handler it is never `None`, but an `assert current is not None` documents that and satisfies type checkers.

### 11. GitHub latest-release fetch is per-entity, per-load

`update.py:133-137` fetches on every `async_added_to_hass`. Every reboot-triggered entry reload re-fetches, and N devices sharing an IC type each poll the same repo — against a 60 req/h unauthenticated limit shared with everything else on the host IP. **Fix:** a module-level TTL cache keyed by repo (e.g. 6 h, matching `SCAN_INTERVAL`), so all entities and reloads share one fetch.

### 12. `use_measured_palettes` defaults differ between services

`upload_image` defaults `True` (`services.py:126`); `drawcustom` defaults `False` (`services.py:144`, deliberate per commit `c020686` — measured palettes distort flat UI colors that drawcustom renders). The asymmetry is defensible but surprising; make sure `services.yaml` field descriptions and `docs/drawcustom/supported_types.md` state both defaults and *why* they differ.

---

## Low-priority / hygiene

13. **Committed bytecode:** `custom_components/opendisplay/__pycache__/services.cpython-312.pyc` is checked in. Remove it and add `__pycache__/` to `.gitignore`.
14. **Stale `requirements.txt`:** dev-tooling only, pins `ruff==0.0.292` (2023-era), and does not reflect the manifest runtime deps. Refresh it (or rename to `requirements_dev.txt` and update `scripts/lint`).
15. **Dependency drift:** manifest pins `py-opendisplay[silabs-ota]==7.9.0`; the library is at 7.10.0 (data_extended packet parsing, ZIPXL 512-byte compression window, new Seeed board types). Bump for beta.8. Also `services.py:14` imports `epaper_dithering` directly without declaring it in manifest `requirements` — it arrives transitively today, but either declare it or import `ColorScheme` from `opendisplay`, which re-exports it.
16. **`_LOGGER` defined mid-file** at `services.py:435`. Move to the top with the other module-level names.
17. **`services.py` is 705 lines** mixing schemas/coercion, HTTP download, media resolution, recorder history, and four handlers. After items 1/4/7 land, split into e.g. `services/__init__.py` (registration), `services/image.py`, `services/drawcustom.py`, `services/schemas.py`.
18. **No tests.** There is no `tests/` directory at all, despite `Quality_Scale.md` ambitions. See the testing strategy below.
19. **Upstream:** py-opendisplay's `__version__ = "0.1.0"` (`src/opendisplay/__init__.py:83`) is stale vs. the packaged 7.10.0 — never key compatibility checks off it; fix upstream.

---

## Positive observations

- `OpenDisplayRuntimeData` + `type OpenDisplayConfigEntry = ConfigEntry[...]` is the current best-practice pattern, used consistently.
- Correct passive-BLE coordinator usage; sensors/events are advertisement-driven with zero polling.
- The reboot-flag edge detection (`coordinator.py:129-150`) and its deferral past in-flight uploads is well reasoned and unusually well documented.
- Auth failures consistently funnel into `async_start_reauth` / `ConfigEntryAuthFailed` from services, setup, and config flow alike.
- Diagnostics redact `ssid`/`password`/`server_url` and hex-encode bytes.
- All user-facing errors are translation-keyed.
- The Silabs OTA path (`update.py:200-242`) handles proxy GATT-cache staleness and the already-in-AppLoader recovery case, with honest comments about why nRF OTA is not offered over proxies.
- Legacy service-value coercion (`_dither_value`, `_refresh_type_value`) keeps old automations working while moving to named values.

---

## Remediation roadmap

### Phase 1 — correctness & performance (before 3.0.0 stable)
| # | Item | Effort |
|---|---|---|
| 1 | Offload `prepare_image` to executor + `upload_prepared_image` | M |
| 3 | None-guard battery percent | XS |
| 5 | `update_percentage` API + `_attr_should_poll` | S |
| 4 | Shared upload-task wrapper for `drawcustom` | S |
| 2 | Decide & fix internal-URL path (support or remove) | S |

### Phase 2 — robustness & design
| # | Item | Effort |
|---|---|---|
| 6 | Aggregate error type + parallel multi-device drawcustom | S |
| 7 | Deduplicate entry resolution | XS |
| 11 | Shared TTL cache for GitHub release lookups | S |
| 8 | Persist last image across restarts/reloads | M |
| 9 | Merge event-entity update logic | S |
| 10 | Narrow `current_task()` typing | XS |
| 12 | Document `use_measured_palettes` defaults | XS |

### Phase 3 — hygiene & tests
| # | Item | Effort |
|---|---|---|
| 13 | Remove committed `.pyc`, gitignore `__pycache__` | XS |
| 14 | Fix `requirements.txt` / lint tooling | XS |
| 15 | Bump py-opendisplay to 7.10.0; declare or drop `epaper_dithering` import | XS |
| 16 | Move `_LOGGER` to top of `services.py` | XS |
| 18 | Bootstrap test suite (below) | L |
| 17 | Split `services.py` (after Phase 1) | M |

## Testing strategy (item 18)

Everything below is testable without BLE hardware using `pytest-homeassistant-custom-component` and mocked `OpenDisplayDevice`:

1. **Pure functions first (cheapest, highest density):** `_dither_value` / `_refresh_type_value` legacy coercion, `_rgb_to_led_color`, `_ms_to_*_delay`, `_format_firmware_version` (the minor×10 cases), `diagnostics._asdict` bytes/nesting.
2. **Coordinator:** reboot-flag edge matrix (None→True no fire, False→True fire once, True→True no fire, legacy advert leaves state), button/touch event propagation into `OpenDisplayUpdate`.
3. **Config flow:** bluetooth discovery happy path, `AuthenticationRequiredError` → encryption-key step, bad key format, reauth with and without key removal.
4. **Services:** schema validation, device targeting by label/area, allowlist rejection, upload-task supersede/cancel semantics (fake slow upload).
5. **Setup/unload:** `ConfigEntryNotReady` on unreachable device, `ConfigEntryAuthFailed` on bad stored key, platform selection for flex vs. base vs. touch-only.

CI: add a `pytest` job alongside the existing hassfest/HACS workflows.

---

## Out of scope for this review

Implementing the fixes; the py-opendisplay-internal review beyond what the integration exercises; firmware repos.
