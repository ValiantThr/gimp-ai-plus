# Project instructions

Guidance for AI assistants working in this repository.

## Project overview

**gimp-ai-plus** is a GIMP 3 plug-in that adds AI image generation, inpainting
and layer compositing through OpenAI's image API. It is a fork of
[lukaso/gimp-ai](https://github.com/lukaso/gimp-ai), diverging at upstream
`v0.14.0`.

[FORK.md](../FORK.md) is the source of truth for the engineering record:
verified API capabilities, the catalogue of upstream bugs and their status, how
the test harness works, and why particular decisions were made. **Read it before
making changes** rather than re-deriving any of it.

## Architecture

| File | Contains |
|---|---|
| `gimp-ai-plugin.py` | Procedures, GTK dialogs, GIMP/GEGL image work |
| `coordinate_utils.py` | Pure coordinate and sizing maths. **No GIMP imports** |
| `openai_client.py` | All API traffic and the model registry. **No GIMP imports** |

All three are required at runtime and must be installed together. Keep the two
pure modules free of GIMP imports: it is what makes them unit testable outside
GIMP, and the tests depend on it.

## Rules that matter here

**Verify API behaviour against the live API, never from OpenAI's docs.** The
documentation advertises `xhigh` and `max` quality for the 2.5 models; the API
rejects both. Both were nearly shipped in a dropdown.

**Two different probes are needed, and conflating them breaks things.** Sending
an invalid enum value is free and returns the valid set — but enum validation
runs *before* the model-support check, so it cannot tell you whether a model
accepts a parameter at all. `input_fidelity` reported "Supported values are:
'high' and 'low'" for every model while being rejected outright by all the 2.x
ones. Establishing real support needs an otherwise-valid request.

**Model differences belong in the registry, not at call sites.** `ModelSpec`
carries sizing rules, pricing and capability flags; `openai_client` omits
parameters a model will not accept. Callers should not have to know.

**Phase discipline.** When changing one thing, change only that thing. Each
phase of this fork deliberately held everything else constant so a regression
was attributable. The `input_fidelity` break was caught in minutes precisely
because nothing else had moved.

**Automated tests cannot see "the image looks wrong".** Four of the six bugs
fixed in 1.0.0 were found by a human looking at output in GIMP while every test
was green: a mask correct in geometry that marked nothing, a subject clipped by
its own layer mask. If a change affects what an image looks like, look at an
image.

## Coding style

- PEP 8, 4 spaces, docstrings with Args/Returns on non-obvious functions
- Comments explain *why*, especially where behaviour is non-obvious or where a
  workaround exists — those comments are how the next person avoids re-breaking it
- **ASCII only in `print()`.** On Windows GIMP gives plug-in stdout a cp1252
  encoding and an emoji raises `UnicodeEncodeError` mid-operation. `Gimp.message()`
  goes through GLib and is safe.
- No external dependencies. Standard library plus GIMP's own APIs.

## Testing

```bash
python3 tests/run_tests.py          # pure functions, fast, no GIMP needed
python3 tests/run_gimp_tests.py     # real GIMP and GEGL, headless
GIMP_AI_LIVE_TESTS=1 python3 tests/run_gimp_tests.py   # adds a paid API call
```

The integration suite drives real GIMP through `python-fu-eval`, GIMP 3's batch
Python interpreter. Two things will bite you when extending it:

- **`Gegl.init(None)` is required** in batch context. Without it
  `Gegl.Node().create_child()` warns about a NULL hash table, the graph
  silently does nothing, and every mask comes out blank at a constant size.
- **`Gimp.Selection.bounds()` returns six values**, the PDB success flag first,
  then `(non_empty, x1, y1, x2, y2)`. Unpacking it as five yields plausible
  nonsense rather than an error.

Run the GIMP suite before committing changes to masks, coordinates or sizing.

## Platform notes

- Plug-in directory is `<GIMP config>/plug-ins/gimp-ai-plugin/`. On Windows
  with GIMP 3.2 that is `%APPDATA%\GIMP\3.2\plug-ins\` — the version in that
  path follows the GIMP release, not the 3.0 API version.
- Plug-in `print()` output is discarded in normal GUI use on Windows, because
  `.py` plug-ins run under `pythonw.exe`, which has no console. The plugin
  therefore tees output to `<GIMP config>/gimp-ai-plugin/gimp-ai-plus.log`.
  `gimp-console-3.2.exe` also shows it.
- Procedure-level tests exercise the **installed** copy, not the checkout. When
  they diverge, a fix plainly present in the source appears not to work; the
  `installed_plugin_is_current` test exists to catch exactly that.

## Security

- Never commit an API key. `config.json` is gitignored; only
  `config.json.example` is tracked.
- The key is stored in plain text in `config.json`, which is normal for a GIMP
  plug-in. Do not describe it as secure storage.
- TLS verification must stay on. Upstream silently disabled it on certificate
  errors, which sent the key over an unverified connection; that fallback was
  removed deliberately and should not come back.
