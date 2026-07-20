# Audit: epaper-dithering — 2026-07-19

**Repo:** `/home/davelee/opendisplay/epaper-dithering` · **Branch:** `main` · **Commit:** `69a58e49e67bf6c00b8c83397fcf5bf60128cad9` (Merge PR #53, release-please)

**Versions:** Rust core crate 4.0.1 · Python package 5.0.9 (pyproject; PyO3 crate Cargo.toml says 5.0.3 — cosmetic, maturin reads pyproject) · JS package 5.0.9. py-opendisplay pins `epaper-dithering==5.0.9` (`py-opendisplay/pyproject.toml:37`) — pin is current.

**Test status:** `cargo test --workspace` — **54/54 pass** (51 unit + 3 visual-regression against stored `.bin` references). Python `pytest` — **83/84 pass**; the single failure (`test_mismatched_dimensions_raise`) is a *stale local build artifact*: the venv contains a 5.0.7 `_rs.so` predating the height-validation added at `packages/python/src/lib.rs:79-93`; current source raises correctly. Rebuild with `maturin develop` to clear. JS tests not run (bun toolchain), but see Medium-2 for a test-matrix bug found by inspection.

---

## Architecture overview

```
packages/rust/core/    epaper_dithering_core — all algorithms
  algorithms.rs        7 error-diffusion kernels + Bayer 4×4 ordered + direct map
  color_space.rs       sRGB ↔ linear (IEC 61966-2-1 piecewise, f64)
  color_space_lab.rs   OKLab (Ottosson combined matrices), weighted match (wab=1.5)
  tone_map.rs          exposure / saturation / shadows-highlights / DR compression
                       (Reinhard-2004 auto) / gamut compression (hull-edge approx)
  palettes.rs          ColorScheme (firmware u8 contract) + idealized palettes
  measured_palettes.rs CATALOG of 8 measured device palettes (single source of truth)
  lib.rs               DitherConfig, dither() / dither_with_canonical() dispatch
packages/rust/wasm/    wasm-bindgen FFI (dither_image, composite_rgba, measured_palettes)
packages/python/       PyO3 FFI (src/lib.rs) + PIL wrapper (core.py); measured palettes
                       derived from Rust CATALOG at import time — no duplication
packages/javascript/   TS wrapper over bundled WASM; measured palettes DUPLICATED by hand
```

Pipeline per pixel: sRGB u8 → (optional linear-space preprocessing: exposure → saturation → shadows/highlights → tone → gamut) → dither. Error diffusion: match in OKLab (of the clamped, rounded working value), diffuse quantization error in **sRGB space** (f64 buffer, no integer overflow possible); serpentine mirrors kernel dx on odd rows. Ordered dither: zero-mean Bayer threshold applied in sRGB-fraction space, amplitude scaled to palette step for dense grayscale ramps (`ordered_spread`, algorithms.rs:331).

**Output contract:** `Vec<u8>` of palette indices, one byte per pixel, row-major, index order = canonical ColorScheme order. **There is no bit-packing in this repo** — all packing (1/2/4 bpp MSB-first, bitplanes, per-panel code tables, row byte-padding) lives in `py-opendisplay/src/opendisplay/encoding/{images,bitplanes}.py`.

Algorithm math verified: all 7 kernel coefficient sets are the standard published values and sum to 1 (Atkinson intentionally 6/8); serpentine mirroring correct; Bayer matrix zero-mean (tested); sRGB↔linear constants correct with exact u8 round-trip (tested); OKLab matrices are Ottosson's published combined forms with round-trip test; clamping before LUT index precludes out-of-bounds; `f64` error accumulation precludes overflow/wraparound. No row-stride or channel-order errors found (RGB throughout; PIL `tobytes()` on RGB and Canvas RGBA→RGB composite both match).

---

## Findings by severity

### Critical

None.

### High

**H1 — ColorScheme value 7/8 divergence from protocol v2.0 (cross-repo wire-contract conflict).**
- Files: `packages/rust/core/src/palettes.rs:50-51` (`Grayscale8 = 7`, "Reserved: pending firmware value assignment"), `packages/python/src/epaper_dithering/palettes.py:108-124` (`GRAYSCALE_8 = 7`), `packages/javascript/src/palettes.ts:15-16`.
- The canonical protocol (`opendisplay-protocol/src/opendisplay_structs.h:550`) has since assigned **7 = OD_COLOR_SCHEME_SEVEN_COLOR**, explicitly noting "the former COLOR_SCHEME_GRAY8 = 7 was a mistake … and is dropped outright". It also defines **8 = OD_COLOR_SCHEME_BWGBRY_SPLIT** (actively used by `Firmware/src/display_service.cpp:1585` for the E1004 panel) and 100–102 (RGB565/888/16bpc). None of these are representable in epaper-dithering.
- Failure scenario: a device config reporting `color_scheme = 7` (7-color Spectra/ACeP) is decoded by py-opendisplay's `ColorScheme.from_value(7)` as **GRAYSCALE_8** and dithered to an 8-level gray ramp — visually wrong — then `encode_image()` (`py-opendisplay/src/opendisplay/encoding/images.py:88-109`) raises "Unsupported color scheme" because it has no GRAYSCALE_8 branch. A `color_scheme = 8` (bwgbry_split) device fails earlier: `from_value(8)` / Rust `TryFrom<u8>` (`palettes.rs:125-140`) reject it, so upload is impossible.
- Confidence: **Confirmed** (definition divergence and firmware use of value 8 are both in-tree); end-user impact depends on such panels reaching py-opendisplay users — the E1004 already exists in Firmware.

**H2 — PyO3 binding holds the GIL for the entire dither (no `py.allow_threads`).**
- File: `packages/python/src/lib.rs:61-137` (`dither_image`), also `tone_compress`/`gamut_compress` (:170, :184). Zero occurrences of `allow_threads` in the file.
- The full Rust computation — rayon-parallel, potentially hundreds of ms for an 800×480 photo with auto tone+gamut — runs with the GIL held. Because the GIL is process-global, calling this from an executor thread (as HA's `drawcustom` pipeline does) still stalls **every** Python thread, including the Home Assistant event loop, for the whole dither.
- Fix shape (report-only): copy inputs, then `py.allow_threads(|| dither(...))`. `&[u8]` borrows would need conversion to owned buffers first.
- Confidence: **Confirmed** (by inspection; standard PyO3 semantics).

### Medium

**M1 — JS measured palettes are hand-duplicated from the Rust source.**
- File: `packages/javascript/src/palettes.ts:126-245`; comment at :137-141 acknowledges "values here must be kept in sync manually". Python derives its constants from the Rust `CATALOG` via FFI at import (`palettes.py:199-216`); JS does not, despite the wasm `measured_palettes()` export existing (`packages/rust/wasm/src/lib.rs:121`).
- I verified all 8 palettes are **currently in byte-exact sync** with `measured_palettes.rs`. But the next palette recalibration (e.g. a V3 Spectra measurement) will silently drift the JS surface. Failure scenario: same photo dithers differently on opendisplay.org (JS) vs HA (Python).
- Confidence: Confirmed (process risk; no present-day value drift).

**M2 — JS test matrix never exercises non-default dither modes.**
- File: `packages/javascript/tests/dithering.test.ts:19`: `ditherImage(image, ColorScheme.BWR, mode as DitherMode)` passes a raw number where `DitherOptions` (an object) is expected. Destructuring a Number yields `undefined` for every option, so **all nine "produces valid output for mode %s" cases silently run BURKES with defaults**. The suite passes but its per-mode coverage is illusory.
- Confidence: **Confirmed**.

**M3 — wasm `dither_image` canonical-pinning is keyed on a sentinel, and misfires for direct consumers.**
- File: `packages/rust/wasm/src/lib.rs:93`: when `palette_bytes` is non-empty, *any* `scheme_id` that parses (0–7) silently enables canonical pinning against that idealized scheme. The TS wrapper protects itself with the 255 sentinel (`core.ts:91`), but a direct wasm consumer who passes a measured palette and leaves `scheme_id` at 0 gets Mono-canonical pinning (exact `#000000`/`#FFFFFF` pixels emit indices 0/1 with error absorption) without asking. Python's binding is safer: `scheme_id` is a true `Option` (`packages/python/src/lib.rs:106-129`).
- Failure scenario: subtle halo/brightness artifacts near pure-black/white regions for non-wrapper wasm users; API drift between the two FFI surfaces.
- Confidence: Confirmed behavior; Plausible impact (unknown direct-wasm population).

**M4 — Cross-surface output divergence for RGBA input (alpha compositing paths differ).**
- Python composites via PIL integer paste on white (`core.py:20-24`); JS composites in wasm with float `round` per channel (`wasm/src/lib.rs:103-115`), both in nonlinear sRGB. Rounding differences on semi-transparent pixels mean the same RGBA input can produce different palette indices in Python vs JS. Fully opaque or fully transparent pixels are identical.
- Confidence: Confirmed mechanism; Low practical impact (e-paper inputs are usually opaque).

### Low

**L1 — `PaletteImageBuffer` dimensions unvalidated against data length (JS).** `core.ts:57-111` returns `{width: image.width, height: image.height}` from caller-claimed fields while wasm derives height from `data.length`; a short/oversized `data` yields `indices.length ≠ width × height` with no error (wasm validates whole-rows only, not the claimed height — unlike Python, which cross-checks at `python/src/lib.rs:79-93`). Confidence: Confirmed.

**L2 — `composite_rgba` silently truncates buffers whose length is not a multiple of 4** (`wasm/src/lib.rs:104`, `n = len / 4`). Confidence: Confirmed, edge-case.

**L3 — Default inconsistency: `ToneCompression::default()` is `Auto` (`enums.rs:34`) but `DitherConfig::default()` uses `Fixed(0.0)` (`lib.rs:56`).** A Rust user writing `tone: ToneCompression::default()` gets auto tone compression when they meant the documented off default. Bindings are unaffected (they always pass explicit values). Confidence: Confirmed.

**L4 — Palettes with >256 colors silently misbehave.** `Palette::new` (`palettes.rs:20`) caps at ≥2 only; `output[..] = best_idx as u8` (`algorithms.rs:222`) would truncate indices ≥256, and `exact_palette_index` (`algorithms.rs:280`) silently treats such entries as non-exact. No practical palette approaches this; a `<= 256` assert would close it. Confidence: Confirmed, theoretical.

**L5 — Error diffusion diffuses in nonlinear sRGB space** (`algorithms.rs:224-227`) while matching is perceptual (OKLab of linearized value). This is a deliberate, internally consistent design (the ordered-dither issue-#27 comment at `algorithms.rs:341-350` explains sRGB-space uniformity), but it means diffused error does not conserve physical luminance — mid-tone bias is possible on extreme palettes. Design note, not a defect. Confidence: Confirmed-by-design.

**L6 — `gamut_compress` hull approximation** (`tone_map.rs:293-299`): nearest point searched over palette **edges** only; for 4+ color palettes the true nearest hull point can lie on a triangular face. Self-documented known approximation; effect is a slightly conservative compression target. Confidence: Confirmed-by-design.

**L7 — Misplaced doc comment**: "Nearest-color mapping with no dithering" sits above `exact_palette_index` instead of `direct_map` (`algorithms.rs:274-275`). Cosmetic.

---

## Unimplemented-or-partial features

| Item | Where | State |
|---|---|---|
| GRAYSCALE_8 scheme (value 7) | all 3 surfaces (`palettes.rs:51`, `palettes.py:109`, `palettes.ts:16`) | Palette + enum complete, but firmware value 7 has been **reassigned to SEVEN_COLOR** by protocol v2.0; no py-opendisplay encoder exists. Effectively dead-ends (see H1) |
| SEVEN_COLOR (7), BWGBRY_SPLIT (8), RGB565/888/16bpc (100–102) | `opendisplay_structs.h:550-554` | Absent from epaper-dithering entirely |
| MONO_4_26, BWRY_4_2, SOLUM_BWR, HANSHOW_BWR, HANSHOW_BWY palettes | `measured_palettes.rs:108-172` | Placeholder values; five explicit "TODO: measure actual display" |
| wasm `measured_palettes()` consumption in TS | `wasm/src/lib.rs:121`, `palettes.ts:141` | Export exists "for future tooling"; TS still hand-duplicates (M1) |
| Serpentine for Ordered/None modes | `lib.rs:30-31` | Intentionally ignored, documented |
| Rust-core doctest | `lib.rs:19` | ` ```ignore ` — never executed |

---

## Cross-repo observations

1. **Packed-format ownership**: epaper-dithering emits *only* index-per-pixel; every device-format decision (1bpp MSB-first w/ row padding `bitplanes.py:54`, BWR "red sets both planes" `bitplanes.py:45-55`, 4-gray two-plane codes `bitplanes.py:60-93` + `display_palettes.py:35-40`, BWRY 2bpp per-panel yellow/red swap `display_palettes.py:56-61`, BWGBRY 4bpp firmware LUT `0,1,2,3,5,6` skipping 4 `images.py:199`) lives in py-opendisplay and depends on epaper-dithering's **index-order invariant**: measured palettes must keep canonical ColorScheme color order. That invariant is documented (`measured_palettes.rs:7`) and currently holds for all 8 palettes, but nothing enforces it — a CATALOG entry with reordered colors would silently swap inks on the wire. A cheap guard: assert each CATALOG entry's `color_names` matches its `scheme`'s canonical name order in a Rust test.
2. **H1 is the actionable wire-contract bug**: value 7 = Grayscale8 in this repo vs SEVEN_COLOR in `opendisplay_structs.h:550`; value 8 (BWGBRY_SPLIT, live in `Firmware/src/display_service.cpp:1585`) unrepresentable. Any fix must land in epaper-dithering first (it owns the shared `ColorScheme`), then propagate through py-opendisplay's `from_value` consumers and the HA integration pin chain.
3. **Version pins are coherent today**: py-opendisplay `epaper-dithering==5.0.9` (pyproject:37) matches the repo HEAD; HA pins `py-opendisplay==7.12.0` + `odl-renderer==0.5.12` (`manifest.json:18`). No stale-pin exposure for this library. (The 7.12.0-vs-7.13.0 HA gap is a py-opendisplay-side matter, out of scope here.)
4. **Measured-palette routing**: `py-opendisplay/src/opendisplay/display_palettes.py:74-87` maps only 4 panel ICs (35, 39, 33, 55) to measured palettes and — notably — routes panel 35 to **SPECTRA_7_3_6COLOR (v1)**, not the newer V2 measurement, and does not use `SPECTRA_7_3_6COLOR_V2`, `BWRY_4_2`, `HANSHOW_*` at all. Whether v1-over-v2 is deliberate is worth confirming with the calibration notes (`packages/python/docs/CALIBRATION.md`).
5. **H2 (GIL) directly affects the HA integration**: `drawcustom` → odl-renderer → `dither_image` runs with the GIL pinned for the full dither; on large Spectra panels this stalls the HA event loop regardless of executor use. Worth a coordinated fix (allow_threads in epaper-dithering 5.0.10, no consumer changes needed).
