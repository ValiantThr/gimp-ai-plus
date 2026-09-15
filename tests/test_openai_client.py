#!/usr/bin/env python3
"""
Test suite for openai_client.

Covers the pure parts — multipart encoding, PNG header parsing, payload
normalisation, error parsing and response extraction. No network access: the
one test that exercises the request path substitutes a fake urlopen.
"""

import base64
import io
import json
import sys
import os
import urllib.error

# Add parent directory to path so we can import openai_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openai_client
from openai_client import (
    ImageResult,
    OpenAIError,
    OpenAIImageClient,
    as_png_bytes,
    build_multipart,
    png_info,
)


def _fake_png(width, height, color_type=6):
    """Minimal bytes that look like a PNG far enough in to parse an IHDR."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, color_type])
        + b"\x00" * 16
    )


def test_png_info():
    """PNG dimensions and colour type are read out of the IHDR."""
    print("=== Testing png_info ===")

    info = png_info(_fake_png(1536, 1024))
    assert info["width"] == 1536, f"width was {info['width']}"
    assert info["height"] == 1024, f"height was {info['height']}"
    assert info["format"] == "RGBA", f"format was {info['format']}"
    print("[ok] parses width, height and RGBA colour type")

    assert png_info(_fake_png(64, 64, color_type=0))["format"] == "L"
    assert png_info(_fake_png(64, 64, color_type=2))["format"] == "RGB"
    print("[ok] recognises greyscale and RGB")

    assert png_info(b"not a png at all") is None
    assert png_info(b"\x89PNG") is None, "truncated header must not parse"
    assert png_info(None) is None
    assert png_info("a string") is None
    print("[ok] rejects non-PNG, truncated and non-bytes input")
    return True


def test_as_png_bytes():
    """Image payloads arrive as raw bytes or base64; both normalise."""
    print("\n=== Testing as_png_bytes ===")

    raw = _fake_png(8, 8)
    assert as_png_bytes(raw) == raw
    print("[ok] bytes pass through unchanged")

    encoded = base64.b64encode(raw).decode("ascii")
    assert as_png_bytes(encoded) == raw
    print("[ok] base64 strings are decoded")

    assert as_png_bytes(bytearray(raw)) == raw
    print("[ok] bytearray is normalised to bytes")

    try:
        as_png_bytes(12345)
    except TypeError:
        print("[ok] rejects unsupported types")
    else:
        raise AssertionError("expected TypeError for int input")
    return True


def test_build_multipart_single():
    """A single image plus mask produces well-formed multipart."""
    print("\n=== Testing build_multipart (single image) ===")

    body, content_type = build_multipart(
        {"model": "gpt-image-1", "size": "1024x1024"},
        {
            "image": ("image.png", b"IMAGEDATA", "image/png"),
            "mask": ("mask.png", b"MASKDATA", "image/png"),
        },
    )

    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=")[1]
    print(f"[ok] content type declares boundary {boundary[:8]}...")

    assert b'name="model"' in body and b"gpt-image-1" in body
    assert b'name="size"' in body and b"1024x1024" in body
    print("[ok] scalar fields are present")

    assert b'name="image"; filename="image.png"' in body
    assert b"IMAGEDATA" in body
    assert b'name="mask"; filename="mask.png"' in body
    assert b"MASKDATA" in body
    print("[ok] image and mask parts are present with filenames")

    assert body.endswith(f"--{boundary}--\r\n".encode())
    print("[ok] body is closed with the terminating boundary")

    # A single image must NOT use the array syntax.
    assert b'name="image[]"' not in body
    print("[ok] single image does not use image[] syntax")
    return True


def test_build_multipart_array():
    """Multiple reference images use the image[] array syntax."""
    print("\n=== Testing build_multipart (image array) ===")

    body, content_type = build_multipart(
        {"model": "gpt-image-1"},
        {
            "image": [
                ("image_0.png", b"LAYER0", "image/png"),
                ("image_1.png", b"LAYER1", "image/png"),
                ("image_2.png", b"LAYER2", "image/png"),
            ],
            "mask": ("mask.png", b"MASKDATA", "image/png"),
        },
    )

    assert body.count(b'name="image[]"') == 3, "expected three image[] parts"
    print("[ok] each image is sent as an image[] part")

    for payload in (b"LAYER0", b"LAYER1", b"LAYER2"):
        assert payload in body, f"missing {payload!r}"
    print("[ok] all layer payloads are present")

    # The mask is a single part and keeps its plain name.
    assert b'name="mask"; filename="mask.png"' in body
    assert b'name="mask[]"' not in body
    print("[ok] mask stays a single non-array part")
    return True


def test_binary_safety():
    """Payloads containing boundary-like or non-UTF8 bytes survive intact."""
    print("\n=== Testing binary safety ===")

    nasty = bytes(range(256)) + b"\r\n--notaboundary\r\n"
    body, content_type = build_multipart(
        {"prompt": "test"}, {"image": ("i.png", nasty, "image/png")}
    )
    assert nasty in body, "binary payload was altered"
    print("[ok] arbitrary binary payload is preserved byte for byte")
    return True


def test_error_parsing():
    """OpenAI error bodies become useful messages rather than raw JSON."""
    print("\n=== Testing error parsing ===")

    body = json.dumps(
        {"error": {"message": "Your prompt was rejected", "code": "moderation_blocked"}}
    )
    err = OpenAIImageClient._parse_error(body, 400)
    assert err.message == "Your prompt was rejected", err.message
    assert err.code == "moderation_blocked"
    assert err.status == 400
    print("[ok] extracts message, code and status from a JSON error body")

    assert "content filter" in err.user_message()
    print("[ok] moderation errors get a human-readable message")

    assert "API key" in OpenAIImageClient._parse_error("{}", 401).user_message()
    print("[ok] 401 explains where to fix the key")

    plain = OpenAIImageClient._parse_error("<html>502 Bad Gateway</html>", 502)
    assert plain.status == 502
    assert "502" in str(plain)
    print("[ok] non-JSON error bodies still yield a usable error")
    return True


def test_response_extraction():
    """Images are pulled out of both base64 and URL response shapes."""
    print("\n=== Testing response extraction ===")

    payload = {"data": [{"b64_json": base64.b64encode(b"PNGBYTES").decode("ascii")}]}
    images = OpenAIImageClient._extract_images(payload)
    assert images == [b"PNGBYTES"], images
    print("[ok] decodes b64_json entries")

    urls = OpenAIImageClient._extract_images({"data": [{"url": "https://example/i.png"}]})
    assert urls == ["https://example/i.png"]
    print("[ok] passes URL entries through for the caller to download")

    multi = OpenAIImageClient._extract_images(
        {"data": [{"b64_json": base64.b64encode(bytes([i])).decode()} for i in range(4)]}
    )
    assert len(multi) == 4, f"expected 4 images, got {len(multi)}"
    print("[ok] handles n>1 responses (needed for the Phase 4 variant picker)")

    for bad, label in (({"data": []}, "empty data"), ({}, "missing data")):
        try:
            OpenAIImageClient._extract_images(bad)
        except OpenAIError:
            pass
        else:
            raise AssertionError(f"expected OpenAIError for {label}")
    print("[ok] raises rather than returning nothing")
    return True


def test_image_result():
    """ImageResult exposes the common single-image case conveniently."""
    print("\n=== Testing ImageResult ===")

    result = ImageResult([b"one", b"two"], usage={"output_tokens": 272})
    assert result.image == b"one"
    assert len(result) == 2
    assert result.usage["output_tokens"] == 272
    print("[ok] .image returns the first image, len() the count, usage preserved")

    assert ImageResult([]).image is None
    print("[ok] empty result yields None rather than raising")
    return True


def test_no_key_rejected():
    """Constructing a client without a key fails loudly and early."""
    print("\n=== Testing missing API key ===")

    for empty in (None, ""):
        try:
            OpenAIImageClient(empty)
        except OpenAIError:
            pass
        else:
            raise AssertionError(f"expected OpenAIError for {empty!r}")
    print("[ok] refuses to build a client without a key")
    return True


def test_http_error_surfaces(monkey=None):
    """A failed request raises OpenAIError carrying the API's own message.

    Upstream read the error body on only one of three call sites; this pins
    the behaviour for all of them.
    """
    print("\n=== Testing HTTP error surfacing ===")

    body = json.dumps({"error": {"message": "Billing hard limit reached"}}).encode()

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            "https://api.openai.com/v1/images/generations",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(body),
        )

    original = openai_client.urllib.request.urlopen
    openai_client.urllib.request.urlopen = fake_urlopen
    try:
        client = OpenAIImageClient("sk-test")
        try:
            client.generate("a cat")
        except OpenAIError as err:
            assert err.status == 429, f"status was {err.status}"
            assert "Billing hard limit" in err.message, err.message
            assert "Rate limited" in err.user_message()
            print("[ok] error body reaches the caller with status and message")
        else:
            raise AssertionError("expected OpenAIError")
    finally:
        openai_client.urllib.request.urlopen = original
    return True


def test_request_shape():
    """The generation request carries the expected payload and auth header."""
    print("\n=== Testing request shape ===")

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "data": [{"b64_json": base64.b64encode(b"IMG").decode()}],
                    "usage": {"output_tokens": 196},
                }
            ).encode()

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.get_header("Authorization")
        captured["timeout"] = timeout
        captured["verified"] = context.verify_mode
        return FakeResponse()

    original = openai_client.urllib.request.urlopen
    openai_client.urllib.request.urlopen = fake_urlopen
    try:
        client = OpenAIImageClient("sk-test")
        result = client.generate("a red circle", size="1536x1024", quality="high")
    finally:
        openai_client.urllib.request.urlopen = original

    assert captured["url"].endswith("/images/generations"), captured["url"]
    assert captured["body"]["prompt"] == "a red circle"
    assert captured["body"]["size"] == "1536x1024"
    assert captured["body"]["quality"] == "high"
    assert captured["auth"] == "Bearer sk-test"
    print("[ok] posts prompt, size, quality and bearer auth to the right endpoint")

    assert captured["timeout"] == openai_client.DEFAULT_TIMEOUT
    print(f"[ok] a timeout is always set ({openai_client.DEFAULT_TIMEOUT}s)")

    import ssl as _ssl

    assert captured["verified"] == _ssl.CERT_REQUIRED, "TLS verification must be on"
    print("[ok] TLS certificate verification is required, with no fallback")

    assert result.image == b"IMG"
    assert result.usage["output_tokens"] == 196
    print("[ok] decodes the image and preserves usage")
    return True


def run_all_tests():
    """Run every openai_client test, returning True if all passed."""
    print("Running openai_client Tests")
    print("=" * 60)

    tests = [
        test_png_info,
        test_as_png_bytes,
        test_build_multipart_single,
        test_build_multipart_array,
        test_binary_safety,
        test_error_parsing,
        test_response_extraction,
        test_image_result,
        test_no_key_rejected,
        test_http_error_surfaces,
        test_request_shape,
    ]

    failures = []
    for test in tests:
        try:
            test()
        except Exception as err:
            failures.append((test.__name__, err))
            print(f"[FAIL] {test.__name__}: {err}")

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} of {len(tests)} openai_client tests FAILED")
        for name, err in failures:
            print(f"  - {name}: {err}")
        return False

    print(f"All {len(tests)} openai_client tests passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if run_all_tests() else 1)
