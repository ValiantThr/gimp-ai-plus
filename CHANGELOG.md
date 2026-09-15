# Changelog

All notable changes to gimp-ai-plus are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This project is a fork of [lukaso/gimp-ai](https://github.com/lukaso/gimp-ai),
diverging at upstream `v0.14.0`. Entries below describe changes made in the
fork; upstream's own history is in that repository.

## [1.0.0] - 2026-09-15

First release of the fork.

### Added

- **Model selection.** Six OpenAI image models, chosen in `Filters → AI →
  Settings`: GPT-Image-2.5 Flare (default), 2.5 Sunburst, GPT-Image-2,
  GPT-Image-1.5, GPT-Image-1 and GPT-Image-1 Mini. Upstream was hardcoded to
  `gpt-image-1`. The new default is cheaper per image, faster, and noticeably
  better at both generation and inpainting.
- **Quality setting** — `low`, `medium`, `high` or `auto`. `low` is useful for
  trying prompts out before committing.
- **Native-resolution output.** Requests are now sized to the area being
  worked on rather than squeezed into a fixed 1024 or 1536 shape, which makes
  inpainting visibly sharper. **Max resolution** caps it if you would rather
  spend less.
- **Result edge.** Controls how far an inpainted subject may extend past your
  selection. Subjects rarely fit a selection outline, so this defaults to a
  generous margin; turn it down for retouching and object removal.
- **`Filters → AI → Settings`** as a real menu entry. Previously the settings
  dialog was reachable only through a button inside the inpainting dialog.
- **`Filters → AI → Test Connection`** to check the API key and network
  separately from an actual edit.
- **A log file** at `<GIMP config>/gimp-ai-plugin/gimp-ai-plus.log`, with its
  location shown in Settings. On Windows GIMP discards plug-in output
  entirely, so without this there was no record of what went wrong.
- **Cost display.** Completion messages show the model used and the estimated
  cost of the call.
- **Scripting.** Every procedure accepts its inputs as arguments and runs
  without a dialog, so the plugin can be driven from Python-Fu or a batch
  script. See the README.

### Fixed

- **Inpainting in Focused mode never sent your selection to the API.** The
  mask was built at extract-region size while the selection was in full-image
  coordinates, with no translation between them, so the selection fell outside
  the mask canvas and was discarded. The model received a mask marking nothing,
  ignored it, and regenerated the whole frame — which is why results looked
  unrelated to the selection and refused to blend. Full Image mode extracts
  from (0,0) and was unaffected, so only the default mode was broken.
- **Inpainted subjects were clipped to the selection outline.** The result was
  masked to the selection exactly, so a subject drawn slightly outside it — a
  fly's wings, say — was generated and then hidden.
- **Edits came back mostly transparent.** No `background` parameter was sent,
  so the API's `auto` default applied and the model sometimes returned an
  image around 25% fully transparent, punching holes through the layer.
- **Requests could hang forever.** Two network calls had no timeout and could
  block indefinitely with no way to cancel.
- **API errors were mostly invisible.** Only one of three call sites read the
  error body, so moderation rejections and quota problems surfaced as generic
  failures. Errors now carry the API's own message.
- **TLS verification could be silently disabled.** On any certificate error the
  plugin retried with verification turned off, sending the API key over an
  unverified connection. It now fails loudly instead.
- **The plugin could crash mid-inpaint on Windows** printing an emoji to a
  cp1252 console.

### Changed

- Configuration lives in `<GIMP config>/gimp-ai-plugin/config.json`. The API
  key is stored there in plain text, which is normal for a GIMP plug-in but
  worth knowing; `OPENAI_API_KEY` in the environment works too.
- The plugin is now **three files**: `gimp-ai-plugin.py`, `coordinate_utils.py`
  and `openai_client.py`. All three must be installed together.
- Requires GIMP 3.0.4 or newer. Developed and tested against GIMP 3.2.6.

### Known issues

- The AI may alter parts of the image outside your selection. The result is
  clipped to the selection plus the configured margin, but within that area
  the surrounding pixels come from the model's output.
- Very large images are scaled to fit the API's limits (longest edge 3840px,
  and a maximum pixel budget), so an inpaint on a very large canvas still
  loses some detail.
- Cancellation is checked between major steps, not during an API call in
  progress.
