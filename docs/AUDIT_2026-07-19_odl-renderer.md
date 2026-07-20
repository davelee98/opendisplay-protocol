# Audit: odl-renderer — 2026-07-19

**Repo:** `/home/davelee/opendisplay/odl-renderer` · branch `main` · commit `cd37287` (release-please merge, v0.5.12)
**Scope:** report-only deep review — architecture, rendering correctness, input hardening, schema consistency, tests.
**Method:** full source read (~3.4 kLOC), `uv run pytest` + `uv run mypy src`, plus targeted runtime reproductions for every finding marked Confirmed.

---

## Architecture overview

Pipeline position: ODL element list (JSON-ish dicts) → `generate_image()` → full-color RGBA PIL image → (consumer) epaper-dithering → py-opendisplay → firmware.

- **Entry point** `src/odl_renderer/core.py:22` `generate_image(width, height, elements, background, accent_color, session, data_provider, font_dirs)`. Creates an RGBA canvas, a `DrawingContext` (`types.py:106` — img, ColorResolver, CoordinateParser, FontManager, aiohttp session, DataProvider, `pos_y` flow cursor), then dispatches each element.
- **Dispatch** is a decorator registry (`registry.py:16` `@element_handler(ElementType, requires=[...])`). Handlers are async, take `(ctx, element)`, and the decorator validates only *presence* of required keys — no type/shape validation. 17 element types in `types.py:49` (`text, multiline, line, rectangle, rectangle_pattern, polygon, circle, ellipse, arc, icon, dlimg, qrcode, plot, progress_bar, diagram, icon_sequence, debug_grid`), all implemented — no stubs.
- **Coordinates** (`coordinates.py`): pixels or `"NN%"` of canvas; `coerce_number()` tolerates numeric strings (HA templates render everything to strings). Malformed values fall back silently (see M7).
- **Transforms** (`transforms.py`): elegant central design — any element gains `rotation`/`mirror`/`pivot` for free by rendering onto a transparent layer, transforming only a pivot-radius crop, and alpha-compositing back (`core.py:122`). Math (isometry radius bound, ±2 px BICUBIC margin) is correct on review.
- **Fonts/assets** (`fonts.py`, `elements/icons.py`): bundled `ppb.ttf`/`rbm.ttf` + MDI webfont with pre-flattened name→codepoint JSON index loaded at import; process-wide truetype caches; `warmup.py` for executor pre-warming. Sound.
- **Flow layout:** every handler updates `ctx.pos_y`; elements omitting `y` stack below the previous element. Simple and workable, though each handler defines its own bottom-edge semantics.
- **Error contract:** `core.py:109-116` wraps handler exceptions into `ValueError("Element N: …")`. Good idea, but leaks on non-dict elements (H2).

**Assessment:** the architecture is clean and extensible (registry + context + central transforms). The systemic weakness is the absence of a schema-validation layer: each handler ad-hoc-parses its dict, string-coercion is applied inconsistently property-by-property, and failure policy oscillates between silent fallback (colors→white, coords→0), warning-and-skip (unknown icon in a sequence), and hard ValueError (unknown icon in `icon`, unknown element type). HA passes user-authored ODL straight in, so every inconsistency is user-facing.

---

## Findings by severity

### High

**H1 — Plot x-legend/axis loops are unbounded: small `xlegend.interval` freezes the event loop (DoS).**
`elements/visualizations.py:331` validates only `interval > 0`; the loops in `_render_x_axis` (`:483-502`) and `_render_x_labels` (`:516-539`) then step `curr_time += timedelta(seconds=interval)` across the whole plot duration, issuing draw/strftime calls per step. Y-ticks were explicitly clamped for exactly this bug class (`_MAX_Y_TICKS = 100`, `:21` — comment cites “millions of draw calls, freezing the event loop”), but the x direction was not. Measured: 24 h plot, `interval=5` → 1.54 s of synchronous event-loop time; scaling is linear, so `interval=0.01` → ~13 minutes. `duration` is likewise uncapped, so a large `duration` with a normal interval triggers the same blow-up. In HA, `generate_image` runs directly on the event loop (`Home_Assistant_Integration/custom_components/opendisplay/services.py:861`), so this freezes the entire HA instance. **Confidence: Confirmed (measured).** Fix shape: clamp step count like `_clamp_tick_every`.

**H2 — Non-dict element (or non-list `elements`) escapes the ValueError contract as raw `AttributeError`.**
`core.py:88` `element.get("visible", True)` executes *before* the try block at `:91`. Payloads `["hello"]`, `[42]`, `[null]`, or `elements="string"` raise `AttributeError: 'str' object has no attribute 'get'` — bypassing the documented `Raises: ValueError` contract and the `"Element N"` context. HA drawcustom passes `call.data["payload"]` verbatim, so a YAML typo (string instead of mapping) surfaces as an unhandled AttributeError. **Confidence: Confirmed (reproduced all four cases).**

**H3 — `parse_colors=True` + `align` + `anchor` double-shifts text.**
`elements/text.py:352-401` `calculate_segment_positions` applies the `align` offset (`:378-387`) **and then** the anchor's horizontal component (`:389-395`) to the same x. `align="center"` with `anchor="mm"` subtracts `total_width/2` twice: measured start_x 48 where the correct centered position is 74 (width 51). On the non-parse path Pillow treats `anchor` as the position semantics and `align` only as multi-line internal justification, so the two paths disagree and the parse_colors path is objectively wrong when both are set. **Confidence: Confirmed (measured).**

### Medium

**M1 — No timeout or size cap on remote image fetch (`dlimg`).**
`media_loader.py:97-122` `_load_from_http` issues `session.get(url)` with no `timeout=` and reads the entire body unbounded (`await response.read()`). With the HA-shared session passed in (`services.py:868`), a tarpit or endless-stream URL stalls the service call (and holds the render) indefinitely; a huge body is buffered wholesale into memory. PIL's decompression-bomb guard only helps after the download. **Confidence: Confirmed (code path; no-timeout behavior by inspection).**

**M2 — SSRF-shaped source handling for `dlimg`.**
`media_loader.py:59-68`: any `http(s)://` URL is fetched (internal IPs, link-local metadata endpoints) and any absolute file path is opened (`_load_from_file`, `:128`) — success/failure and PIL error details are reflected back in the ValueError message, enabling internal-network/file probing from user-authored ODL. In practice the ODL author is an HA admin, which mitigates severity, but the renderer offers no hook (allowlist/deny-internal) for a consumer to restrict it. **Confidence: Confirmed (by design/inspection).**

**M3 — Alpha in colors is accepted but semantically broken end-to-end.**
`colors.py` parses `#RGBA`/`#RRGGBBAA` (`:118-132`), but ImageDraw *writes* RGBA values rather than blending them: a `#ff000080` rectangle over white leaves pixel `(255,0,0,128)` (measured), i.e. no visual blend on the canvas; downstream both HA dry-run (`services.py:260`) and py-opendisplay encoding (`py-opendisplay/src/opendisplay/encoding/images.py:41`) do `convert("RGB")`, which **discards** alpha — the element renders fully opaque. So semi-transparent colors silently render as opaque instead of blended. Alpha does work where `alpha_composite` is used (dlimg `elements/media.py:159`, transforms). **Confidence: Confirmed (measured).**

**M4 — Plot `low`/`high` as strings crash (`TypeError` → wrapped ValueError).**
`elements/visualizations.py:626` `min_v, max_v = element.get("low"), element.get("high")` — never coerced. `_process_entity_segments:267` then does `min(min_v, min(all_values))` → `'<' not supported between float and str`. Every comparable numeric field in the codebase tolerates template-rendered strings via `coerce_number`; this one doesn't, and HA templates are the primary authoring surface. **Confidence: Confirmed (reproduced).**

**M5 — QR `bgcolor: null` renders black-on-black (unscannable).**
`elements/media.py:74` `bgcolor = ctx.colors.resolve(element.get("bgcolor", "white")) or BLACK` — the fallback constant is `BLACK`, not `WHITE`. With `bgcolor: null` in YAML (`.get` returns None → resolve returns None), the QR background becomes black; measured corner pixel `(0,0,0,255)`. Same-line pattern for `color` at `:73` is correct only by coincidence. **Confidence: Confirmed (reproduced).**

**M6 — `smooth_steps` and other plot ints are unclamped/uncoerced.**
`elements/visualizations.py:557` `steps = plot_config.get("smooth_steps", 10)` — a large value multiplies every segment's point count (`_smooth_segment` emits ~`len(points)×steps` coords); `steps=10**7` on a 3-point series builds tens of millions of tuples on the event loop (memory + CPU DoS, same class as H1). Also uncoerced: `line width` (`:554`), `point_size` (`:585`), arc `start_angle`/`end_angle`/`width` (`shapes.py:262-271`), text `spacing` (`text.py:50`), all of which raise deep in PIL if template-rendered as strings. **Confidence: Confirmed for the mechanism (code); blow-up not executed.**

**M7 — Silent-zero coordinate fallback masks authoring errors.**
`coordinates.py:73,77`: any unparseable coordinate/size (`"abc"`, `"10 px"`, `None`) silently becomes `0` — the element renders at the origin or with zero size and no log line. Combined with unknown colors silently rendering white (`colors.py:154`), a typo'd dashboard renders "successfully" but wrong, which on a battery e-paper tag costs a physical refresh cycle to discover. Contrast: unknown element *type* and unknown *icon name* are hard errors — policy is inconsistent. **Confidence: Confirmed (code).**

**M8 — Visual snapshot suite fails on a clean checkout: 12 of 454 tests.**
`uv sync --all-extras && uv run pytest` → **12 failed, 442 passed** (Pillow 12.0.0 from `uv.lock`). All failures are byte-exact PNG snapshot comparisons involving text rasterization (`tests/visual/test_text_rendering.py` basic/colors/sizes, `test_transform_rendering.py` rotated text, `test_color_rendering.py` gray markup, `test_layouts.py` dashboard, `test_plot_rendering.py` axes/legend). Baselines were evidently generated under a different FreeType/Pillow build; byte-exact PNG snapshots of rasterized text are inherently environment-fragile (the repo even carries `imagehash` for perceptual comparison but these tests don't use it). Consequence: local contributors can't distinguish real regressions from environment noise. `mypy --strict`: clean (17 files). **Confidence: Confirmed (run).**

### Low

**L1 — `coerce_number` accepts `"nan"`/`"inf"`** (`coordinates.py:25` — `float("nan")` parses), propagating non-finite values into geometry; most sinks then raise (`int(nan)` → ValueError, wrapped) but e.g. `max(0, nan)` comparisons silently misbehave (progress `"nan"` clamps to 0 by comparison-order luck, `visualizations.py:718`). Confirmed.

**L2 — Font-name path traversal.** `fonts.py:120` joins a user-supplied relative name into `font_dirs` without normalization — `"../../anything"` escapes the configured directory; and any absolute path (`:107`) is attempted as a font. Only load-attempt + error-message oracle, no content disclosure. Confirmed (code).

**L3 — Unbounded process-global caches.** `fonts.py:20` `_truetype_cache` keyed by (path, size), `icons.py:48` `_mdi_font_cache` keyed by size, `colors.py:7` `_warned_color_tokens`: hostile ODL cycling sizes/color tokens grows memory for the life of the HA process. Slow, bounded-by-effort leak. Confirmed (code).

**L4 — `progress_bar` edge cases.** Bar ≤4 px tall/wide with `show_percentage: true` computes a non-positive font size (`visualizations.py:754`) → font-load ValueError with a misleading "Failed to load built-in font" message. Unknown `direction` silently draws no fill (`:739-746`). Percentage text color heuristic (`:769`) picks `background` when progress >50, which is invisible over the *unfilled* half it may straddle. Confirmed (code).

**L5 — `diagram` element bypasses the coordinate system.** `visualizations.py:793-796` uses `element["x"]`/`element["height"]` raw — no percentage support, no string coercion (a `"10"` from a template → `TypeError` → wrapped ValueError), unlike every other element; it also has no `y` property (implicitly flows from `ctx.pos_y`). Bar-scan inconsistency: malformed bar entries are skipped in the max-value scan (`:833` `continue`) but raise in the draw loop (`:874`). Confirmed (code).

**L6 — `multiline` strips newlines from the value** (`text.py:184` `.replace("\n","")`) before splitting on the delimiter — surprising when the delimiter *is* `\n`-adjacent, and `delimiter: ""` raises `ValueError: empty separator` (wrapped). Confirmed (code).

**L7 — Wrapping never splits over-long words.** `_wrap_to_width` (`text.py:255`) and legacy `get_wrapped_text` (`:293`) place an over-long word on its own line still exceeding `max_width` — overflows the box. A user-supplied `anchor` with a vertical `t`/`b`/`s` component combined with wrapping that produces multiline text raises Pillow's multiline-anchor ValueError. Confirmed (code).

**L8 — `debug_grid` div-by-zero / range errors.** `spacing: 0` → `range() arg 3 must not be zero`; `label_step: 0` → `ZeroDivisionError` (`elements/debug.py:49-63`) — both wrapped into ValueError but with raw messages. Confirmed (code).

**L9 — `dlimg` `rotate` uncoerced.** `elements/media.py:124,134`: string `"90"` is truthy then `-rotate` raises TypeError (wrapped). Inconsistent with `coerce_number` use two lines up. Confirmed (code).

**L10 — `icon_sequence` degrades silently.** Unknown icon names are warn-and-skip (`icons.py:169`) while the single `icon` element hard-fails (`:98`); a string passed as `icons` iterates per-character and typically renders nothing but warnings. Confirmed (code).

---

## Unimplemented / partial features

| Item | Location | Status |
|---|---|---|
| All 17 `ElementType`s | `types.py:49` / `elements/` | Implemented — no stubbed element types |
| `polygon` `width` (outline width) | `shapes.py:171` | Commented out — silently ignored if supplied |
| `arc` `width` with `fill` (pie slice) | `shapes.py:276` | `width` ignored on the filled path (only outline-arc uses it) |
| Color alpha channel | `colors.py:118-132` | Parsed but not blended and dropped downstream (M3) — effectively decorative |
| `debug_grid` color debug helper | `elements/debug.py:70` | `TODO: maybe add a debug function for colors?` — only TODO in src |
| `get_wrapped_text` | `text.py:293` | Legacy duplicate of `_wrap_to_width`, still exported/callable |
| Schema validation layer | — | Absent by design; `requires=[...]` checks key presence only |

---

## Cross-repo observations

1. **Version pin is current and consistent.** `Home_Assistant_Integration/custom_components/opendisplay/manifest.json` pins `odl-renderer==0.5.12`; `src/odl_renderer/__init__.py:23` `__version__ = "0.5.12"`, matching commit `cd37287`. No drift.
2. **Image mode/size contract:** `generate_image` returns RGBA at exactly (width, height); HA computes the canvas from the display's native pixel grid with axis transposition for 90/270 rotation (`services.py:846-870`) and py-opendisplay flattens via `convert("RGB")` (`py-opendisplay/src/opendisplay/encoding/images.py:41`) before epaper-dithering. Structurally sound, **but** the RGB flatten is what makes color alpha silently lossy (M3) — either odl-renderer should pre-composite alpha onto the canvas or document alpha as unsupported.
3. **Event-loop coupling:** HA awaits `generate_image` directly on the loop (`services.py:861`) — all rendering CPU (and H1/M6 DoS loops) block the entire HA instance, unlike the dither/encode stage which HA explicitly offloads to an executor (`services.py:474` comment). Consider rendering in an executor too, or fixing the unbounded loops renderer-side (both, ideally).
4. **HA passes `call.data["payload"]` verbatim** — no schema pre-validation in the integration either, so H2's AttributeError escape and every silent-fallback behavior (M7) is directly user-facing in drawcustom. The `Raises: ValueError` contract in `core.py:56` is what HA would need to rely on for clean service-call errors; H2 breaks it.
5. **Shared-session timeout responsibility is unowned:** odl-renderer sets no request timeout (M1) and HA's `async_get_clientsession` session imposes none per-request — neither side bounds a `dlimg` fetch. One of the two must own it; renderer-side (`aiohttp.ClientTimeout` + max-bytes read) protects all consumers.

---

*Auditor note: no repository files were modified; all reproductions ran against the installed package in the uv environment.*
