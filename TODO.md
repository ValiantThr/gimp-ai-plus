# Roadmap

Current state and what is worth doing next. For the detailed engineering
record — verified API capabilities, the catalogue of upstream bugs, and how the
test harness works — see [FORK.md](FORK.md).

## Done

- Model registry and picker; default `gpt-image-2.5-flare`
- Native-resolution output instead of fixed 1024/1536 shapes
- Non-interactive procedures, so the plugin is scriptable
- Headless integration tests that run real GIMP with no display
- Log file, Settings and Test Connection menu entries, cost display
- Six upstream bugs fixed — see [CHANGELOG.md](CHANGELOG.md)

## Next

**Multiple variants per request.** The API can return several images for one
prompt, and the client already decodes them. Showing them and letting the user
pick would suit generation especially.

**Streaming previews.** The API supports partial images during generation
(`stream` with `partial_images`). The progress dialog currently shows a spinner
and an elapsed counter; it could show the image resolving instead.

**Transparent backgrounds for generation.** `background: transparent` is
supported on every model and the client already exposes it. Generating a
subject on transparency would be genuinely useful in a layer-based editor.
Edits deliberately force `opaque` and should keep doing so.

**Lift the image logic out of `Gimp.PlugIn`.** The coordinate and mask code
needs nothing from the plug-in lifecycle, but lives on a class that cannot be
instantiated outside it — the tests work around this by grafting methods onto a
plain object. Separating them would make the core directly testable.

**Additional providers.** The client is a single module with one request
builder; another backend would slot in beside it rather than through it.

## Testing

```bash
# Fast: pure functions, no GIMP required
python3 tests/run_tests.py

# Slower: real GIMP, real GEGL, no display needed
python3 tests/run_gimp_tests.py

# Add a live inpaint against the API (costs a few cents)
GIMP_AI_LIVE_TESTS=1 python3 tests/run_gimp_tests.py
```

Run the GIMP suite before committing anything that touches masks, coordinates
or sizing. Those areas have produced every serious bug in this codebase, and
none of them were visible to the unit tests.

## Contributing

Issues and pull requests: https://github.com/ValiantThr/gimp-ai-plus

Worth knowing before changing behaviour: several of the bugs fixed in 1.0.0
were invisible to automated tests and were found by looking at output in GIMP.
If a change affects what an image looks like, look at an image.
