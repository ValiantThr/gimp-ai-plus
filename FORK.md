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

## Verified baseline (2026-09-15)

Established on Windows 11 with GIMP 3.2.6 before changing any plugin code.

| Check | Result |
|---|---|
| Upstream test suite under GIMP's Python 3.14.7 | **Pass** (needs `PYTHONIOENCODING=utf-8`, see below) |
| Unmodified plugin loads in GIMP 3.2.6 | **Pass** — all three procedures register |
| GIMP 3.2 deprecated-API usage | **None** — no v3.2 compatibility work needed |
| Python 3.14 stdlib compatibility | **Pass** — no removed modules in the import list |

Environment:

- GIMP 3.2.6 installed per-user at `%LOCALAPPDATA%\Programs\GIMP 3`
- Bundled Python 3.14.7
- Plug-in directory is `%APPDATA%\GIMP\3.2\plug-ins\` — **not** the `3.0` path the README and
  `.github/copilot-instructions.md` both claim. Confirmed empirically; GIMP 3.2 uses a `3.2`
  config dir even though its API/ABI version stays at 3.0.

## Issues found during baseline

Recorded here so they are not rediscovered later. None are regressions from the fork; all
are present in upstream `v0.14.0`.

1. **No timeout on two network calls.** `gimp-ai-plugin.py:3852` and `:3987` call
   `urllib.request.urlopen(req)` directly, bypassing the `_make_url_request()` helper. A
   stalled connection hangs the worker thread with no cancel path.
2. **TLS verification silently disabled on fallback.** `_make_url_request()` at `:137` retries
   with `check_hostname = False` and `ssl.CERT_NONE` on any certificate error, sending the API
   key over an unverified connection with only a debug print. GIMP's bundled Python on Windows
   often lacks a usable CA bundle, so this may be the normal path rather than an edge case —
   `tools/smoke_test.py` determines which.
3. **Duplicated generation logic.** `_call_openai_generation()` at `:1676` and the threaded
   copy at `:3950` have diverged in size defaults and error handling.
4. **API error bodies mostly swallowed.** Only the edits path at `:3059` reads
   `HTTPError.read()`; generation failures surface as generic errors.
5. **Plugin stdout goes nowhere on Windows.** `pygimp_win.interp` maps `.py` plug-ins to
   `pythonw.exe`, which has no console, so all 339 `print()` calls are discarded in normal GUI
   use. Debugging requires `gimp-console-3.2.exe`. A real logging path is needed.
6. **Emoji printed to a cp1252 stdout.** `gimp-ai-plugin.py:2997` prints `"DEBUG: ✅ ..."` on
   the inpaint happy path, and `tests/run_tests.py` does the same throughout — which is why the
   suite cannot run on Windows without `PYTHONIOENCODING=utf-8`. The 18 emoji `Gimp.message()`
   calls are safe; those go through GLib, not stdout.
7. **Missing `set_i18n()` override.** GIMP 3.2 emits nine locale-catalog warnings per run
   without it.
8. **Dead imports.** The three `email.mime.*` imports at `:2682` are unused; multipart bodies
   are hand-rolled with a uuid boundary.
9. **Stale project docs.** `.github/copilot-instructions.md` describes v0.8, names the wrong
   config path, and states the API accepts only three fixed sizes — no longer true for the 2.x
   models. It needs rewriting for this fork.

## Roadmap

- **Phase 0 — Baseline.** *(done)* Fork, verify upstream against GIMP 3.2.6, record findings,
  add `tools/smoke_test.py`.
- **Phase 1 — Client extraction.** Split out an `openai_client` module with one request
  builder, one error handler, one timeout policy, one TLS policy. Fixes issues 1–4.
- **Phase 2 — Model registry and picker.** Per-model capability entries; model and quality
  dropdowns in Settings. Default to `gpt-image-2.5-flare`.
- **Phase 3 — Resolution pipeline.** Replace fixed shapes in `coordinate_utils.py` with
  arbitrary multiple-of-16 sizing under the real API constraints; rewrite the affected tests.
- **Phase 4 — New capabilities.** `background: transparent`, `output_format`, `n > 1` with a
  variant picker, and `stream` + `partial_images` for live preview.
- **Phase 5 — Polish.** Logging behind the existing `debug_mode` flag, per-call cost readout
  from the API's `usage` field, corrected docs, fork CHANGELOG.

## Testing

```bash
# Unit tests, using GIMP's own Python so the interpreter matches production.
# PYTHONIOENCODING is required on Windows until issue 6 is fixed.
PYTHONIOENCODING=utf-8 "$LOCALAPPDATA/Programs/GIMP 3/bin/python.exe" tests/run_tests.py

# API reachability, TLS, and per-model cost. Needs OPENAI_API_KEY in the environment.
"$LOCALAPPDATA/Programs/GIMP 3/bin/python.exe" tools/smoke_test.py

# Confirm the plugin registers in GIMP without launching the GUI.
"$LOCALAPPDATA/Programs/GIMP 3/bin/gimp-console-3.2.exe" \
    -i -d -f --batch-interpreter=plug-in-script-fu-eval -b "(gimp-quit 0)"
```

## License

MIT, inherited from upstream. See [LICENSE](LICENSE).
