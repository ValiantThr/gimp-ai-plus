"""
OpenAI image API client for gimp-ai-plus.

Standard library only and no GIMP imports, so this module can be unit tested
outside GIMP — the same rule `coordinate_utils.py` follows.

This is the single place the plugin talks to OpenAI. It owns one request
builder, one error handler, one timeout policy and one TLS policy, replacing
three divergent call sites in `gimp-ai-plugin.py`.

TLS note: there is deliberately no certificate-verification fallback. Upstream
retried with `ssl.CERT_NONE` on any certificate error, which sent the API key
over an unverified connection after printing a debug line. GIMP 3.2.6 ships a
working CA bundle (see FORK.md), so verification succeeds normally and that
fallback only ever masked a genuine problem. Certificate failures now raise.
"""

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

API_BASE = "https://api.openai.com/v1"

# One timeout policy. Upstream used 180s for generation, 120s for edits and
# 60s for downloads, with two call sites passing no timeout at all (able to
# hang the worker thread forever). Image generation at high quality and large
# sizes is the slow case, so the default is sized for it.
DEFAULT_TIMEOUT = 180
DOWNLOAD_TIMEOUT = 60

# ---------------------------------------------------------------------------
# Model registry
#
# Every value below was verified against the live API on 2026-09-15, not taken
# from the documentation - which advertises "xhigh" and "max" quality for the
# 2.5 models that the API rejects outright.
#
# Two different probes were needed, and the distinction matters:
#
#   Allowed VALUES come free. Sending an invalid enum ("notaquality") returns
#   a 400 listing the valid ones before anything is generated.
#
#   Whether a model ACCEPTS a parameter at all does not. Enum validation runs
#   first, so an invalid value reports "Supported values are: 'high', 'low'"
#   even for models that reject the parameter entirely. Establishing support
#   requires an otherwise-valid request, which generates an image on the
#   models that do support it.
#
# Conflating the two is what broke the first Phase 2 build: input_fidelity
# looked universally supported and is in fact rejected by every 2.x model.
# ---------------------------------------------------------------------------

# Uniform across every image model.
#   "Invalid value: 'x'. Supported values are: 'low', 'medium', 'high', 'auto'."
QUALITIES = ("low", "medium", "high", "auto")
DEFAULT_QUALITY = "high"

BACKGROUNDS = ("opaque", "transparent", "auto")
INPUT_FIDELITIES = ("high", "low")
MODERATIONS = ("low", "auto")

# The three shapes the gpt-image-1 generation accepts.
FIXED_SIZES = ("1024x1024", "1536x1024", "1024x1536")


class ModelSpec:
    """What one image model accepts, and what it costs.

    `output_price_per_1m` is dollars per million output tokens, used for the
    per-call cost readout. Sizing rules differ by generation: the 1.x models
    take three fixed shapes, the 2.x models take any WIDTHxHEIGHT whose edges
    are multiples of 16, within a pixel budget and aspect limit.
    """

    def __init__(self, model_id, label, summary, custom_sizes=False,
                 max_edge=None, min_pixels=None, max_pixels=None,
                 max_aspect=None, output_price_per_1m=0.0,
                 supports_input_fidelity=False):
        self.model_id = model_id
        self.label = label
        self.summary = summary
        self.custom_sizes = custom_sizes
        self.max_edge = max_edge
        self.min_pixels = min_pixels
        self.max_pixels = max_pixels
        self.max_aspect = max_aspect
        self.output_price_per_1m = output_price_per_1m
        # Only the 1.x line takes input_fidelity, and not even all of it.
        # Sending it elsewhere is a hard 400: "The model 'x' does not support
        # the 'input_fidelity' parameter." Verified per model against the live
        # API - note that probing with an INVALID value is useless here,
        # because enum validation runs before the model-support check and
        # happily reports "Supported values are: 'high' and 'low'" for models
        # that reject the parameter outright.
        self.supports_input_fidelity = supports_input_fidelity

    def __repr__(self):
        return f"<ModelSpec {self.model_id}>"

    def check_size(self, width, height):
        """Is this size acceptable? Returns (ok, reason).

        Mirrors the API's own validation so the plugin can fail early with a
        useful message instead of spending a round trip on a 400.
        """
        if not self.custom_sizes:
            size = f"{width}x{height}"
            if size not in FIXED_SIZES:
                return False, f"{self.model_id} accepts only {', '.join(FIXED_SIZES)}"
            return True, ""

        if width % 16 or height % 16:
            return False, "width and height must both be divisible by 16"
        if self.max_edge and max(width, height) > self.max_edge:
            return False, f"the longest edge must be {self.max_edge} or less"
        pixels = width * height
        if self.min_pixels and pixels < self.min_pixels:
            return False, "below the minimum pixel budget"
        if self.max_pixels and pixels > self.max_pixels:
            return False, "exceeds the maximum pixel budget"
        if self.max_aspect:
            ratio = max(width / height, height / width)
            if ratio > self.max_aspect:
                return False, f"the maximum aspect ratio is {self.max_aspect:g}:1"
        return True, ""

    def cost_for(self, usage):
        """Estimated output cost in dollars, or None when unknown."""
        if not usage:
            return None
        tokens = usage.get("output_tokens")
        if tokens is None or not self.output_price_per_1m:
            return None
        return tokens / 1_000_000 * self.output_price_per_1m


# Newest first: this is also the order shown in the settings dialog.
MODEL_ORDER = (
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2",
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image-1-mini",
)

_CUSTOM = dict(
    custom_sizes=True,
    max_edge=3840,
    min_pixels=655360,
    max_pixels=8294400,
    max_aspect=3.0,
)

MODELS = {
    "gpt-image-2.5-flare": ModelSpec(
        "gpt-image-2.5-flare", "GPT-Image-2.5 Flare",
        "Fast, high quality. The best default for most work.",
        output_price_per_1m=30.0, **_CUSTOM,
    ),
    "gpt-image-2.5-sunburst": ModelSpec(
        "gpt-image-2.5-sunburst", "GPT-Image-2.5 Sunburst",
        "Most capable; more precision and control over edits. Slower.",
        output_price_per_1m=30.0, **_CUSTOM,
    ),
    "gpt-image-2": ModelSpec(
        "gpt-image-2", "GPT-Image-2",
        "Previous generation. Superseded by 2.5 at the same price.",
        output_price_per_1m=30.0, **_CUSTOM,
    ),
    "gpt-image-1.5": ModelSpec(
        "gpt-image-1.5", "GPT-Image-1.5",
        "Older generation; fixed sizes only.",
        output_price_per_1m=32.0, supports_input_fidelity=True,
    ),
    "gpt-image-1": ModelSpec(
        "gpt-image-1", "GPT-Image-1",
        "What upstream used. Oldest and the most expensive per image.",
        output_price_per_1m=40.0, supports_input_fidelity=True,
    ),
    "gpt-image-1-mini": ModelSpec(
        "gpt-image-1-mini", "GPT-Image-1 Mini",
        "Cheapest. Useful for drafts and for testing without spending much.",
        output_price_per_1m=8.0,
    ),
}

DEFAULT_MODEL = "gpt-image-2.5-flare"


def get_model(model_id):
    """Look up a model, falling back to the default for anything unknown.

    An unrecognised id in a config file should not break the plugin: models
    come and go, and a stale setting is not worth a crash.
    """
    return MODELS.get(model_id) or MODELS[DEFAULT_MODEL]


def model_labels():
    """(model_id, label, summary) in display order, for the settings dialog."""
    return [
        (mid, MODELS[mid].label, MODELS[mid].summary)
        for mid in MODEL_ORDER
        if mid in MODELS
    ]

USER_AGENT = "gimp-ai-plus"


class OpenAIError(Exception):
    """An API call failed.

    Carries the HTTP status and the parsed error body when there is one, so
    callers can show the user what OpenAI actually said instead of a generic
    failure. Upstream read the error body on only one of its three call sites.
    """

    def __init__(self, message, status=None, code=None, body=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.body = body

    def __str__(self):
        if self.status is not None:
            return f"HTTP {self.status}: {self.message}"
        return self.message

    def user_message(self):
        """A short single-line form suitable for Gimp.message()."""
        if self.code == "moderation_blocked":
            return f"Request rejected by content filter: {self.message}"
        if self.status == 401:
            return "API key rejected. Check Filters > AI > Settings."
        if self.status == 429:
            return f"Rate limited or out of quota: {self.message}"
        return str(self)


def png_info(data):
    """Read width, height and colour type straight out of a PNG's IHDR.

    Returns None if `data` is not a PNG or the header is truncated. Used to
    verify that an image and its mask agree on dimensions before spending an
    API call on them.
    """
    if not isinstance(data, (bytes, bytearray)):
        return None
    if not data.startswith(b"\x89PNG") or len(data) <= 25:
        return None
    color_types = {0: "L", 2: "RGB", 3: "P", 4: "LA", 6: "RGBA"}
    color_type = data[25]
    return {
        "width": int.from_bytes(data[16:20], "big"),
        "height": int.from_bytes(data[20:24], "big"),
        "color_type": color_type,
        "format": color_types.get(color_type, f"Unknown({color_type})"),
    }


def as_png_bytes(data):
    """Normalise image payloads to bytes.

    The plugin passes base64 strings in some paths and raw bytes in others.
    """
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return base64.b64decode(data)
    raise TypeError(f"expected bytes or base64 str, got {type(data).__name__}")


def build_multipart(fields, files):
    """Build a multipart/form-data body.

    `fields` maps name to a scalar. `files` maps name to either a single
    (filename, data, content_type) tuple or, for the `image` key, a list of
    them — which is sent using the `image[]` array syntax the edits endpoint
    expects for multi-reference composites.

    Returns (body_bytes, content_type_header_value).
    """
    boundary = uuid.uuid4().hex
    body = b""

    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()

    for key, file_data in files.items():
        entries = file_data if isinstance(file_data, list) else [file_data]
        name = "image[]" if (key == "image" and isinstance(file_data, list)) else key
        for filename, data, content_type in entries:
            body += f"--{boundary}\r\n".encode()
            body += (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'.encode()
            )
            body += f"Content-Type: {content_type}\r\n\r\n".encode()
            body += data
            body += b"\r\n"

    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class ImageResult:
    """A successful image response.

    `images` holds decoded PNG bytes, already base64-decoded. `usage` is the
    API's token accounting when present, which Phase 5 turns into a per-call
    cost readout.
    """

    def __init__(self, images, usage=None, raw=None):
        self.images = images
        self.usage = usage
        self.raw = raw or {}

    @property
    def image(self):
        """The first image, for the common single-result case."""
        return self.images[0] if self.images else None

    def __len__(self):
        return len(self.images)


class OpenAIImageClient:
    """Talks to the OpenAI image endpoints.

    Pass a `log` callable to receive progress and diagnostic lines; the plugin
    wires this to its debug output. Defaults to discarding them, which keeps
    this module usable from tests and from tools/smoke_test.py.
    """

    def __init__(self, api_key, base_url=API_BASE, timeout=DEFAULT_TIMEOUT, log=None):
        if not api_key:
            raise OpenAIError("No API key configured")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._log = log or (lambda _msg: None)

    # -- internals ---------------------------------------------------------

    def _auth_headers(self):
        return {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": USER_AGENT,
        }

    @staticmethod
    def _ssl_context():
        """One TLS policy: verify, always."""
        return ssl.create_default_context()

    @staticmethod
    def _parse_error(raw_body, status):
        """Pull OpenAI's error message out of a failure body."""
        try:
            parsed = json.loads(raw_body)
            err = parsed.get("error", {})
            return OpenAIError(
                err.get("message") or raw_body[:300],
                status=status,
                code=err.get("code") or err.get("type"),
                body=raw_body,
            )
        except (ValueError, AttributeError):
            return OpenAIError(raw_body[:300] or "Unknown error", status=status, body=raw_body)

    def _send(self, url, data, content_type, timeout=None):
        """Issue one request and return the decoded JSON body.

        Every network failure mode converges here, so callers get OpenAIError
        and nothing else.
        """
        headers = self._auth_headers()
        headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(
                req, timeout=timeout or self._timeout, context=self._ssl_context()
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            body = ""
            try:
                body = err.read().decode("utf-8", "replace")
            except Exception:
                pass
            error = self._parse_error(body, err.code)
            self._log(f"API error {err.code}: {error.message}")
            raise error from err
        except ssl.SSLCertVerificationError as err:
            raise OpenAIError(
                "TLS certificate verification failed. The connection was not "
                "trusted, so the request was abandoned rather than retried "
                f"without verification. ({err})"
            ) from err
        except urllib.error.URLError as err:
            raise OpenAIError(f"Network error: {err.reason}") from err
        except OSError as err:
            # Covers socket.timeout, which subclasses OSError.
            raise OpenAIError(f"Request failed: {err}") from err

        try:
            return json.loads(raw)
        except ValueError as err:
            raise OpenAIError(f"Malformed response from API: {raw[:200]}") from err

    @staticmethod
    def _extract_images(payload):
        """Decode the images out of a generations/edits response."""
        entries = payload.get("data") or []
        if not entries:
            raise OpenAIError("API returned no image data")

        images = []
        for entry in entries:
            if "b64_json" in entry:
                images.append(base64.b64decode(entry["b64_json"]))
            elif "url" in entry:
                # Older models answered with a URL. Signalled explicitly so
                # the caller can download rather than silently getting fewer
                # images than it asked for.
                images.append(entry["url"])
        if not images:
            raise OpenAIError("API response contained neither b64_json nor url")
        return images

    # -- public API --------------------------------------------------------

    def generate(self, prompt, model=DEFAULT_MODEL, size="1024x1024",
                 quality="high", n=1, timeout=None, **extra):
        """POST /images/generations."""
        payload = {
            "model": model,
            "prompt": prompt,
            "n": n,
            "size": size,
            "quality": quality,
        }
        payload.update(extra)

        self._log(f"generation: model={model} size={size} quality={quality}")
        result = self._send(
            f"{self._base_url}/images/generations",
            json.dumps(payload).encode("utf-8"),
            "application/json",
            timeout=timeout,
        )
        images = self._extract_images(result)
        self._log(f"generation: received {len(images)} image(s)")
        return ImageResult(images, usage=result.get("usage"), raw=result)

    def edit(self, images, prompt, mask=None, model=DEFAULT_MODEL,
             size="1024x1024", quality="high", n=1, moderation="low",
             input_fidelity="high", background="opaque", timeout=None,
             **extra):
        """POST /images/edits.

        `images` is bytes or a base64 str for single-image inpainting, or a
        list of either for multi-reference compositing. `mask` is PNG bytes
        whose transparent region marks the area to regenerate.

        `background` defaults to "opaque" rather than the API's "auto".
        Under "auto" the model may decide an edit warrants a transparent
        background and return an RGBA image that is only ~45% opaque -
        measured against a real inpaint, 21.7% of pixels came back fully
        transparent. Compositing that back into GIMP punches holes through
        the layer. "opaque" returns RGB with no alpha channel at all, at
        identical token cost. Generation is left alone; a transparent
        background is a legitimate thing to ask for there.
        """
        spec = get_model(model)
        fields = {
            "model": model,
            "prompt": prompt,
            "n": str(n),
            "size": size,
            "quality": quality,
            "moderation": moderation,
            "background": background,
        }
        # Sent only where the model accepts it; elsewhere it is a hard 400.
        if input_fidelity and spec.supports_input_fidelity:
            fields["input_fidelity"] = input_fidelity
        elif input_fidelity:
            self._log(f"{model} does not accept input_fidelity; omitting it")
        fields.update({k: str(v) for k, v in extra.items()})

        files = {}
        if isinstance(images, list):
            files["image"] = [
                (f"image_{i}.png", as_png_bytes(img), "image/png")
                for i, img in enumerate(images)
            ]
        else:
            files["image"] = ("image.png", as_png_bytes(images), "image/png")

        if mask is not None:
            files["mask"] = ("mask.png", as_png_bytes(mask), "image/png")

        body, content_type = build_multipart(fields, files)
        count = len(files["image"]) if isinstance(files["image"], list) else 1
        self._log(
            f"edit: model={model} size={size} quality={quality} "
            f"images={count} mask={'yes' if mask else 'no'} body={len(body)} bytes"
        )

        result = self._send(
            f"{self._base_url}/images/edits", body, content_type, timeout=timeout
        )
        out = self._extract_images(result)
        self._log(f"edit: received {len(out)} image(s)")
        return ImageResult(out, usage=result.get("usage"), raw=result)

    def verify(self, timeout=30):
        """Check the key and the connection. Returns available image model ids.

        Uses the models endpoint, which generates nothing and so costs
        nothing. That makes it safe to offer as a button: it distinguishes a
        rejected key from a blocked network from a working setup, which is
        most of what a support conversation is trying to establish.
        """
        req = urllib.request.Request(
            f"{self._base_url}/models", headers=self._auth_headers()
        )
        try:
            with urllib.request.urlopen(
                req, timeout=timeout, context=self._ssl_context()
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = ""
            try:
                body = err.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise self._parse_error(body, err.code) from err
        except ssl.SSLCertVerificationError as err:
            raise OpenAIError(
                f"TLS certificate verification failed: {err}"
            ) from err
        except urllib.error.URLError as err:
            raise OpenAIError(f"Could not reach the API: {err.reason}") from err
        except OSError as err:
            raise OpenAIError(f"Could not reach the API: {err}") from err
        except ValueError as err:
            raise OpenAIError(f"Malformed response from the API: {err}") from err

        return sorted(
            entry["id"]
            for entry in payload.get("data", [])
            if str(entry.get("id", "")).startswith("gpt-image")
        )

    def download(self, url, timeout=DOWNLOAD_TIMEOUT):
        """Fetch bytes from a result URL, under the same TLS and error policy."""
        self._log(f"download: {url}")
        return download_bytes(url, timeout=timeout)


def download_bytes(url, timeout=DOWNLOAD_TIMEOUT):
    """Fetch bytes from a URL under the same TLS and error policy as the client.

    A module function rather than a client method because result URLs are
    pre-signed and need no credentials — callers should not have to hold an
    API key just to download an image the API already handed them.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(
            req, timeout=timeout, context=ssl.create_default_context()
        ) as response:
            return response.read()
    except ssl.SSLCertVerificationError as err:
        raise OpenAIError(f"TLS certificate verification failed: {err}") from err
    except urllib.error.HTTPError as err:
        raise OpenAIError(f"Download failed: HTTP {err.code}", status=err.code) from err
    except (urllib.error.URLError, OSError) as err:
        raise OpenAIError(f"Download failed: {err}") from err
