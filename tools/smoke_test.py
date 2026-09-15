#!/usr/bin/env python3
"""
Smoke test for gimp-ai-plus: verify OpenAI image API access and TLS behaviour.

Run this with GIMP's *bundled* Python so it exercises the exact network and
certificate path the plug-in will take inside GIMP:

    Windows:
      $env:OPENAI_API_KEY = "sk-..."
      & "$env:LOCALAPPDATA\\Programs\\GIMP 3\\bin\\python.exe" tools\\smoke_test.py

    Linux / macOS:
      export OPENAI_API_KEY=sk-...
      python3 tools/smoke_test.py

The key is read from the environment only. It is never written to disk, never
echoed, and never included in output.

Checks, in order:
  1. TLS  - does certificate verification actually work here, or is the
            plug-in's CERT_NONE fallback being silently relied on?
  2. Auth - is the key accepted, and which image models does this account
            have access to?
  3. Call - one minimal generation per model, reporting latency and token
            usage so we can calibrate real cost.

Stdlib only, matching the plug-in's zero-dependency constraint.
"""

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://api.openai.com/v1"

# Output-token price per 1M, for rough cost calibration.
# Source: https://developers.openai.com/api/docs/pricing (checked 2026-09-15)
OUTPUT_PRICE_PER_1M = {
    "gpt-image-1": 40.00,
    "gpt-image-1-mini": 8.00,
    "gpt-image-1.5": 32.00,
    "gpt-image-2": 30.00,
    "gpt-image-2.5-flare": 30.00,
    "gpt-image-2.5-sunburst": 30.00,
}

# Baseline model first, then the candidates we want to migrate to.
DEFAULT_MODELS = ["gpt-image-1", "gpt-image-2.5-flare"]

PROMPT = "a single small red circle centered on a plain white background"


def get_api_key():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        print("FAIL  OPENAI_API_KEY is not set in this environment.")
        print()
        print("      PowerShell:  $env:OPENAI_API_KEY = \"sk-...\"")
        print("      cmd.exe:     set OPENAI_API_KEY=sk-...")
        print("      bash:        export OPENAI_API_KEY=sk-...")
        print()
        print("      Set it in the same shell you run this script from.")
        sys.exit(2)
    return key


def check_tls():
    """Does default certificate verification work under this interpreter?

    The plug-in silently falls back to ssl.CERT_NONE on any certificate
    error, which would send the API key over an unverified connection. This
    tells us whether that fallback is a rare edge case or the normal path on
    this machine.
    """
    print("[1/3] TLS certificate verification")
    ctx = ssl.create_default_context()
    print(f"      python           {sys.version.split()[0]}")
    print(f"      openssl          {ssl.OPENSSL_VERSION}")

    paths = ssl.get_default_verify_paths()
    print(f"      ca file          {paths.cafile or '(none)'}")
    print(f"      ca path          {paths.capath or '(none)'}")

    loaded = len(ctx.get_ca_certs())
    print(f"      certs loaded     {loaded}")

    try:
        req = urllib.request.Request(f"{API_BASE}/models")
        # No auth header: a 401 still proves the TLS handshake succeeded.
        urllib.request.urlopen(req, timeout=30, context=ctx)
        print("      verified TLS     OK")
        return True
    except urllib.error.HTTPError:
        print("      verified TLS     OK (handshake succeeded)")
        return True
    except (ssl.SSLError, urllib.error.URLError) as err:
        print(f"      verified TLS     FAILED - {err}")
        print()
        print("      This is the condition that triggers the plug-in's")
        print("      CERT_NONE fallback. Needs fixing before we ship.")
        return False


def check_auth(api_key):
    """Confirm the key works and report which image models are visible."""
    print()
    print("[2/3] Authentication and model access")
    req = urllib.request.Request(f"{API_BASE}/models")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:400]
        print(f"      FAILED  HTTP {err.code}")
        print(f"      {detail}")
        return None
    except Exception as err:
        print(f"      FAILED  {err}")
        return None

    print("      key accepted     OK")
    image_models = sorted(
        m["id"] for m in body.get("data", []) if m.get("id", "").startswith("gpt-image")
    )
    if image_models:
        print("      image models visible to this account:")
        for m in image_models:
            print(f"        - {m}")
    else:
        print("      (no gpt-image models listed; the list endpoint may not")
        print("       enumerate them - the generation calls below are the")
        print("       real test)")
    return image_models


def estimate_cost(model, usage):
    """Rough output cost from reported tokens. Input tokens are negligible here."""
    if not usage:
        return None
    out_tokens = usage.get("output_tokens")
    price = OUTPUT_PRICE_PER_1M.get(model)
    if out_tokens is None or price is None:
        return None
    return out_tokens / 1_000_000 * price


def try_generation(model, api_key, size="1024x1024", quality="low"):
    """One minimal generation. Low quality keeps this a couple of cents."""
    payload = {
        "model": model,
        "prompt": PROMPT,
        "n": 1,
        "size": size,
        "quality": quality,
    }
    req = urllib.request.Request(
        f"{API_BASE}/images/generations", data=json.dumps(payload).encode("utf-8")
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")

    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        elapsed = time.time() - started
        detail = err.read().decode("utf-8", "replace")
        try:
            msg = json.loads(detail)["error"]["message"]
        except Exception:
            msg = detail[:300]
        print(f"      {model:<24} FAILED  HTTP {err.code} after {elapsed:.1f}s")
        print(f"      {'':<24}         {msg}")
        return False
    except Exception as err:
        elapsed = time.time() - started
        print(f"      {model:<24} FAILED  {err} after {elapsed:.1f}s")
        return False

    elapsed = time.time() - started
    data = (body.get("data") or [{}])[0]
    b64 = data.get("b64_json")
    n_bytes = len(base64.b64decode(b64)) if b64 else 0

    usage = body.get("usage")
    cost = estimate_cost(model, usage)
    cost_str = f"~${cost:.4f}" if cost is not None else "n/a"
    tokens = usage.get("output_tokens") if usage else None

    print(
        f"      {model:<24} OK      {elapsed:6.1f}s  "
        f"{n_bytes // 1024:>5} KB  {str(tokens):>6} tok  {cost_str}"
    )
    return True


def main():
    print("gimp-ai-plus smoke test")
    print("=" * 68)

    api_key = get_api_key()

    tls_ok = check_tls()
    check_auth(api_key)

    models = sys.argv[1:] or DEFAULT_MODELS
    print()
    print("[3/3] Minimal generation per model (1024x1024, quality=low)")
    print(f"      {len(models)} call(s), a few cents total")
    print()
    results = {m: try_generation(m, api_key) for m in models}

    print()
    print("=" * 68)
    ok = [m for m, good in results.items() if good]
    bad = [m for m, good in results.items() if not good]
    if ok:
        print(f"reachable: {', '.join(ok)}")
    if bad:
        print(f"FAILED:    {', '.join(bad)}")
    if not tls_ok:
        print("WARNING:   TLS verification failed - see section 1")
    return 0 if (ok and not bad and tls_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
