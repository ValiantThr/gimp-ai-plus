# gimp-ai-plus

A maintained fork of [lukaso/gimp-ai](https://github.com/lukaso/gimp-ai), diverging to add
support for OpenAI's current image models.

Upstream's last commit was 2025-11-17 and it hardcodes `gpt-image-1` at three call sites.
This fork replaces that with a model registry, targets GPT-Image-2.5, and reworks the
resolution pipeline that upstream's fixed 1024/1536 shapes made necessary.

Forked from upstream `v0.14.0` (commit `436c37e`), tagged here as `baseline-upstream-v0.14.0`.

## Why a fork rather than PRs

Upstream is unmaintained as of this writing, and the changes needed are structural: the
single 4304-line `gimp-ai-plugin.py` has to be split before the model and resolution work
is tractable. That is not a reviewable PR series against a dormant repo.

## Model landscape as of 2026-09-15

| Model | Text in | Image in | Image out | Notes |
|---|---|---|---|---|
| `gpt-image-2.5-sunburst` | $5.00 | $8.00 | $30.00 | Most capable; precision editing |
| `gpt-image-2.5-flare` | $5.00 | $8.00 | $30.00 | ~50% lower latency; default choice |
| `gpt-image-2` | $5.00 | $8.00 | $30.00 | Snapshot `gpt-image-2-2026-04-21` |
| `gpt-image-1` | $5.00 | $10.00 | $40.00 | **What upstream uses.** Oldest and dearest. |

Prices per 1M tokens, from the [OpenAI pricing page](https://developers.openai.com/api/docs/pricing).
Moving off `gpt-image-1` is a quality *and* cost improvement.

The 2.x models also accept custom `WIDTHxHEIGHT` sizes — both edges multiples of 16, neither
over 3840px, total pixels between 655,360 and 8,294,400, aspect ratio between 1:3 and 3:1.
Upstream is locked to three fixed shapes, so every operation round-trips through a downscale.
Lifting that is the single biggest quality win available.

### Capabilities, verified against the live API

Established by probing, because the documentation is wrong in at least one
place: it advertises `xhigh` and `max` quality for the 2.5 models and the API
rejects both.

| | 1 / 1.5 / 1-mini | 2 / 2.5-flare / 2.5-sunburst |
|---|---|---|
| Sizes | `1024x1024`, `1536x1024`, `1024x1536`, `auto` | any `WIDTHxHEIGHT` |
| `input_fidelity` | 1 and 1.5 only | **rejected** |
| Quality | `low`, `medium`, `high`, `auto` | same |
| `background` | `opaque`, `transparent`, `auto` | same |
| `moderation` | `low`, `auto` | same |

Custom sizes must have both edges divisible by 16, longest edge 3840 or less,
between 655,360 and 8,294,400 pixels, aspect ratio 3:1 or tighter. Each of those
rejections was read back from the API verbatim.

**Two probes are needed, and conflating them breaks things.** Allowed *values*
come free: an invalid enum returns a 400 listing the valid ones before anything
is generated. Whether a model *accepts a parameter at all* does not: enum
validation runs first, so an invalid value reports "Supported values are:
'high' and 'low'" even for models that reject the parameter outright.
Establishing support needs an otherwise-valid request, which generates an image
on the models that do support it. That distinction is exactly what broke the
first Phase 2 build - `input_fidelity` looked universally supported and is in
fact rejected by every 2.x model. The client now omits it per model.

## Verified baseline (2026-09-15)

Established on Windows 11 with GIMP 3.2.6 before changing any plugin code.

| Check | Result |
|---|---|
| Upstream test suite under GIMP's Python 3.14.7 | **Pass** (needs `PYTHONIOENCODING=utf-8`, see below) |
| Unmodified plugin loads in GIMP 3.2.6 | **Pass** — all three procedures register |
| GIMP 3.2 deprecated-API usage | **None** — no v3.2 compatibility work needed |
| Python 3.14 stdlib compatibility | **Pass** — no removed modules in the import list |
| TLS certificate verification | **Pass** — see below; the `CERT_NONE` fallback is unnecessary |
| Live API reachability | **Pass** — both `gpt-image-1` and `gpt-image-2.5-flare` |

### Smoke test, 2026-09-15

`tools/smoke_test.py` under GIMP's bundled Python (3.14.7, OpenSSL 3.6.4):

**TLS verification works.** GIMP 3.2.6 ships its own CA bundle at
`%LOCALAPPDATA%\Programs\GIMP 3\etc\ssl\cert.pem`, and 174 certificates load into a default
`SSLContext`. The handshake against `api.openai.com` succeeds with verification fully enabled.
The plugin's `CERT_NONE` fallback is therefore dead weight on this platform, not a workaround
for a real defect — Phase 1 removes it rather than papering over it with a vendored bundle.
If a certificate error ever does occur it should fail loudly, not silently downgrade.

**Measured, at `1024x1024` / `quality=low`:**

| Model | Latency | PNG | Output tokens | Est. cost |
|---|---|---|---|---|
| `gpt-image-1` (upstream default) | 9.1s | 1086 KB | 272 | ~$0.0109 |
| `gpt-image-2.5-flare` (fork default) | 6.9s | 648 KB | 196 | ~$0.0059 |

The migration target is **46% cheaper and 24% faster** than what upstream hardcodes, before
counting the quality and resolution gains. This is the whole case for the fork in one row.

All image models are visible to the test account: `gpt-image-1`, `-1-mini`, `-1.5`,
`gpt-image-2`, `gpt-image-2.5-flare`, `gpt-image-2.5-sunburst`, plus dated snapshots.

Environment:

- GIMP 3.2.6 installed per-user at `%LOCALAPPDATA%\Programs\GIMP 3`
- Bundled Python 3.14.7
- Plug-in directory is `%APPDATA%\GIMP\3.2\plug-ins\` — **not** the `3.0` path the README and
  `.github/copilot-instructions.md` both claim. Confirmed empirically; GIMP 3.2 uses a `3.2`
  config dir even though its API/ABI version stays at 3.0.

## Issues found during baseline

Found while reading upstream `v0.14.0`. None are regressions from the fork.
Status is updated as phases land.

1. ~~**No timeout on two network calls.**~~ **Fixed in Phase 1.** Both bare
   `urllib.request.urlopen(req)` calls are gone; every request now goes through
   `openai_client`, which always sets a timeout.
2. ~~**TLS verification silently disabled on fallback.**~~ **Fixed in Phase 1.**
   The `CERT_NONE` retry is deleted. The Phase 0 smoke test showed GIMP 3.2.6
   ships a working CA bundle, so verification succeeds normally and the fallback
   only ever masked a genuine problem. Certificate failures now raise.
3. ~~**Duplicated generation logic.**~~ **Fixed in Phase 1.** The threaded copy
   turned out to be dead code and was deleted outright, along with a second dead
   method. It also contained a wait loop whose condition
   `while "success" not in layer_created` was true immediately, so it read the
   result before the main thread had produced it.
4. ~~**API error bodies mostly swallowed.**~~ **Fixed in Phase 1.** `OpenAIError`
   carries status, code and message from the API's own response, with
   `user_message()` for display.
5. **Plugin stdout goes nowhere on Windows.** `pygimp_win.interp` maps `.py`
   plug-ins to `pythonw.exe`, which has no console, so `print()` output is
   discarded in normal GUI use. Debugging requires `gimp-console-3.2.exe`. A real
   logging path is still needed. *(Phase 5.)*
6. ~~**Emoji printed to a cp1252 stdout.**~~ **Fixed in Phase 1.** All `print()`
   output across the plugin and tests is ASCII; the suite now runs on Windows
   with no `PYTHONIOENCODING` workaround. `Gimp.message()` keeps its emoji —
   those go through GLib and are safe.
7. **Missing `set_i18n()` override.** GIMP 3.2 emits nine locale-catalog warnings
   per run without it. *(Phase 5.)*
8. ~~**Dead imports.**~~ **Fixed in Phase 1.** Removed with
   `_create_multipart_data`, whose replacement lives in `openai_client`.
9. **Stale project docs.** `README.md` and `INSTALL.md` are corrected for the
   three-file layout, but `.github/copilot-instructions.md` still describes v0.8,
   names the wrong config path, and claims the API accepts only three fixed
   sizes. *(Phase 3 invalidates the last of those; rewrite then.)*

10. ~~**Focused inpainting never sent the selection.**~~ **Fixed.**
    `_create_full_size_mask_then_scale()` built the mask canvas at extract-region
    size but composited the full-image selection channel without shifting it, so
    in focused mode the selection landed outside the canvas and was clipped away,
    producing an all-black mask that marked nothing. The model then ignored the
    mask and re-rendered the whole frame. Confirmed from debug PNGs: before the
    fix the 1024x1024 mask held 7,168 transparent pixels, all in rows 0-2 and
    1020-1023 (letterbox padding); after, 150,452 in a contiguous antialiased
    ellipse at (293,299)-(730,724). Full-image mode extracts from (0,0) and was
    unaffected, which is why only the default mode was broken. The correct
    implementation already existed in `_create_context_mask()` but is unreachable:
    it early-returns into the broken function whenever `padding_info` is present,
    which every producer sets unconditionally. Phase 3 deleted the unreachable copy:
    keeping a second, divergent mask implementation around is what invited the bug.
11. ~~**Edits came back mostly transparent.**~~ **Fixed.** No `background`
    parameter was sent, so the API default of `auto` applied and the model
    returned RGBA that was only 44.9% fully opaque with 21.7% fully transparent -
    holes punched through the layer on compositing. A/B tested against the real
    API with the user's own input and mask: `background="opaque"` returns RGB with
    no alpha channel at all, at identical token cost. Now the client's default for
    edits; generation is untouched, since a transparent background is a legitimate
    request there.

12. **Image logic is entangled with the plug-in lifecycle.** `GimpAIPlugin`
    cannot be instantiated outside GIMP's plug-in protocol - `Gimp.PlugIn`'s
    constructor asserts on a live wire channel and aborts the process - yet the
    coordinate and mask methods need none of that. `tests/gimp_integration.py`
    works around it by grafting the methods onto a plain object. Phase 3 should
    lift them into a class that does not inherit from `Gimp.PlugIn`.
13. ~~**Procedures are dialog-only.**~~ **Fixed.** All three took `run_mode`
    and ignored it, always showing a GTK dialog. Each now declares its inputs as
    procedure arguments (`prompt`, plus `mode` for inpainting and `use-mask` for
    compositing) and skips the dialog under `RUN_NONINTERACTIVE`. Bad or missing
    arguments return `CALLING_ERROR` rather than `CANCEL`, since without a user
    there is nobody to cancel. This makes the plugin scriptable - batch
    processing was on upstream's own v1.0 wishlist - and allows the full
    pipeline to be tested end to end.

## Roadmap

- **Phase 0 — Baseline.** *(complete)* Fork, verify upstream against GIMP 3.2.6, record findings,
  add `tools/smoke_test.py`.
- **Phase 1 — Client extraction.** *(complete)* `openai_client.py` owns one request
  builder, one error handler, one timeout policy, one TLS policy. Closed issues 1–4, 6
  and 8, and deleted 265 lines of dead networking code.
- **Phase 2 — Model registry and picker.** *(complete)* Per-model capability entries in
  `openai_client.py`, model and quality dropdowns in Settings, default
  `gpt-image-2.5-flare`. Capabilities verified against the live API, not the docs.
- **Phase 3 — Resolution pipeline.** *(complete)* `choose_target_shape()` sizes each request
  from the region's own aspect ratio under the real API constraints, so content is no longer
  letterboxed into one of three fixed shapes. A max-resolution setting trades cost against
  detail. The legacy path is untouched for the 1.x models, which genuinely are fixed-size.
- **Phase 4 — New capabilities.** `background: transparent`, `output_format`, `n > 1` with a
  variant picker, and `stream` + `partial_images` for live preview.
- **Phase 5 — Polish.** Logging behind the existing `debug_mode` flag, per-call cost readout
  from the API's `usage` field, corrected docs, fork CHANGELOG.

## Testing

```bash
# Unit tests, using GIMP's own Python so the interpreter matches production.
"$LOCALAPPDATA/Programs/GIMP 3/bin/python.exe" tests/run_tests.py

# API reachability, TLS, and per-model cost. Needs OPENAI_API_KEY in the environment.
"$LOCALAPPDATA/Programs/GIMP 3/bin/python.exe" tools/smoke_test.py

# Integration tests: real GIMP, real GEGL, no display and no GUI. Slow
# (GIMP scans plug-ins on startup) but it is the only automated cover for the
# mask and coordinate code, where both Phase 1 bugs lived.
"$LOCALAPPDATA/Programs/GIMP 3/bin/python.exe" tests/run_gimp_tests.py

# Confirm the plugin registers in GIMP without launching the GUI.
"$LOCALAPPDATA/Programs/GIMP 3/bin/gimp-console-3.2.exe" \
    -i -d -f --batch-interpreter=plug-in-script-fu-eval -b "(gimp-quit 0)"
```

### Output sizing

Before Phase 3 every request was forced into `1024x1024`, `1536x1024` or
`1024x1536`. A region that matched none of those was letterboxed and scaled
down, which is why inpainted patches came back softer than their surroundings -
a resolution loss that had nothing to do with the model.

`choose_target_shape()` now sizes each request from the region itself:

| Source region | Fixed shapes | Custom sizing |
|---|---|---|
| 1600x1200 | 1536x1024, 171px padding, 0.85x scale | **1600x1200, no padding, 1.0x** |
| 2048x1536 | 1536x1024, downscaled | **2048x1536 unchanged** |
| 8000x6000 | 1536x1024 | 3312x2496 (clamped to the pixel budget) |
| 220x200 | 1024x1024 | 864x784 (grown to clear the minimum) |

Rules enforced: both edges divisible by 16, longest edge 3840 or less, between
655,360 and 8,294,400 pixels, aspect ratio 3:1 or tighter. The scale never
enlarges a region that already fits, since upscaling costs money and invents
detail - except where the API's minimum pixel budget forces it.

**Max resolution** in Settings caps the longest edge (1024/1536/2048, or match
the image). Lower is cheaper and faster. It applies only to GPT-Image-2 and
newer; the 1.x models really are limited to the three fixed shapes, and their
path is unchanged.

### Scripting the plugin

Every procedure runs without a dialog, which is what makes end-to-end testing
possible and also makes the plugin usable from scripts:

```python
proc = Gimp.get_pdb().lookup_procedure("gimp-ai-inpaint")
config = proc.create_config()
config.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
config.set_property("image", image)
config.set_property("prompt", "a small brass gear, centred")
config.set_property("mode", "contextual")   # or "full_image"
result = proc.run(config)
```

Arguments: `gimp-ai-inpaint` takes `prompt` and `mode`, `gimp-ai-layer-generator`
takes `prompt`, `gimp-ai-layer-composite` takes `prompt` and `use-mask`. Leave
`drawables` unset - it is a `GimpCoreObjectArray` that cannot be assigned from
Python, and no procedure reads it; they use `image.get_selected_layers()`.

The live end-to-end test is opt-in because it makes a paid API call:

```bash
GIMP_AI_LIVE_TESTS=1 "$LOCALAPPDATA/Programs/GIMP 3/bin/python.exe" tests/run_gimp_tests.py
```

### How the integration tests work

GIMP 3 registers `python-fu-eval` as a batch interpreter, so plugin code can be
exercised against a live `Gimp` module with no display, no GUI and no
third-party MCP server. `tests/run_gimp_tests.py` launches GIMP that way, finds
the binary per platform, and parses `GIMPTEST|` markers out of GIMP's stdout;
`tests/gimp_integration.py` is the body that runs inside.

Two things cost real time to discover and will bite anyone extending them:

- **`Gegl.init(None)` is required.** The plug-in lifecycle normally does it.
  Without it `Gegl.Node().create_child()` warns
  `g_hash_table_remove_all: assertion 'hash_table != NULL' failed`, the graph
  silently no-ops, and every mask comes out blank at a constant byte size.
- **`Gimp.Selection.bounds()` returns six values**, the PDB success flag first,
  then `(non_empty, x1, y1, x2, y2)`. Unpacking it as five gives plausible
  nonsense rather than an error.

A further guard: `installed_plugin_is_current` compares the checkout against
the copy in GIMP's plug-ins directory. The mask tests import from the repo
while the procedure tests go through the PDB and therefore run the installed
copy; when those diverge the failures are baffling, because a fix plainly
present in the source appears not to work.

Validated against the bug they exist to catch: with the focused-mask translate
disabled 3 of 4 fail, the selection landing jammed at (946,899)-(1024,1024)
instead of (228,228)-(796,796). With the fix in place all 4 pass, the mask
within 2px of its computed position.

## License

MIT, inherited from upstream. See [LICENSE](LICENSE).
