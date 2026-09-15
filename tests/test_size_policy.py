#!/usr/bin/env python3
"""
Tests for custom output sizing.

The 1.x models accept three fixed shapes, so every region was letterboxed into
one of them and rescaled on the way - which is why inpainted patches came back
softer than their surroundings. The 2.x models accept any size with edges
divisible by 16, so the target can follow the region's own aspect ratio and the
content scales into it almost exactly.

Every bound asserted here was read back from the live API; see FORK.md.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coordinate_utils import (
    DEFAULT_SIZE_POLICY,
    FIXED_SHAPES,
    SIZE_GRANULARITY,
    calculate_padding_for_shape,
    choose_target_shape,
    extract_context_with_selection,
    is_shape_allowed,
    validate_context_info,
)

CUSTOM = dict(DEFAULT_SIZE_POLICY)


def _check(target, policy=CUSTOM):
    ok, why = is_shape_allowed(target[0], target[1], policy)
    assert ok, f"{target} rejected: {why}"


def test_legacy_path_unchanged():
    """Without a policy, sizing must behave exactly as before."""
    print("=== Testing legacy path ===")

    for width, height in ((1920, 1080), (1080, 1920), (1000, 1000), (640, 480)):
        shape = choose_target_shape(width, height, None)
        assert shape in FIXED_SHAPES, f"{width}x{height} -> {shape}"
    print("[ok] no policy still yields one of the three fixed shapes")

    disabled = {"custom_sizes": False}
    assert choose_target_shape(1920, 1080, disabled) in FIXED_SHAPES
    print("[ok] a policy with custom_sizes off does too")
    return True


def test_target_follows_source_aspect():
    """The whole point: the target matches the region's shape."""
    print("\n=== Testing aspect preservation ===")

    for width, height in ((1600, 1200), (1000, 1000), (1920, 1080), (900, 1400)):
        tw, th = choose_target_shape(width, height, CUSTOM)
        _check((tw, th))
        source_ratio = width / height
        target_ratio = tw / th
        drift = abs(source_ratio - target_ratio) / source_ratio
        assert drift < 0.03, (
            f"{width}x{height} -> {tw}x{th}: aspect drifted {100*drift:.1f}%"
        )
        print(f"[ok] {width}x{height} -> {tw}x{th} (aspect within {100*drift:.1f}%)")
    return True


def test_edges_are_multiples_of_16():
    """Both edges must be divisible by 16 or the API rejects the request."""
    print("\n=== Testing size granularity ===")

    for width, height in ((1001, 997), (1333, 777), (1919, 1079), (655, 655)):
        tw, th = choose_target_shape(width, height, CUSTOM)
        assert tw % SIZE_GRANULARITY == 0, f"{tw} is not a multiple of 16"
        assert th % SIZE_GRANULARITY == 0, f"{th} is not a multiple of 16"
        _check((tw, th))
    print("[ok] awkward source sizes still produce multiples of 16")
    return True


def test_bounds_are_respected():
    """Edge, pixel budget and aspect limits are all enforced."""
    print("\n=== Testing API bounds ===")

    tw, th = choose_target_shape(8000, 6000, CUSTOM)
    _check((tw, th))
    assert max(tw, th) <= CUSTOM["max_edge"], f"{tw}x{th} exceeds max edge"
    assert tw * th <= CUSTOM["max_pixels"], f"{tw}x{th} exceeds the pixel budget"
    print(f"[ok] an 8000x6000 source clamps to {tw}x{th}")

    tw, th = choose_target_shape(200, 150, CUSTOM)
    _check((tw, th))
    assert tw * th >= CUSTOM["min_pixels"], f"{tw}x{th} is below the minimum"
    print(f"[ok] a tiny 200x150 source grows to {tw}x{th} to clear the minimum")

    # 10:1 is far outside the 3:1 limit and must be pulled back.
    tw, th = choose_target_shape(3000, 300, CUSTOM)
    _check((tw, th))
    ratio = max(tw / th, th / tw)
    assert ratio <= CUSTOM["max_aspect"] + 0.01, f"{tw}x{th} is {ratio:.1f}:1"
    print(f"[ok] a 10:1 source clamps to {tw}x{th} ({ratio:.1f}:1)")
    return True


def test_no_pointless_upscaling():
    """A region already within budget should not be inflated.

    Upscaling costs money and invents detail the source never had.
    """
    print("\n=== Testing upscale avoidance ===")

    for width, height in ((1600, 1200), (2048, 1536), (1024, 1024)):
        tw, th = choose_target_shape(width, height, CUSTOM)
        assert tw <= width + SIZE_GRANULARITY, f"{width}x{height} -> {tw}x{th}"
        assert th <= height + SIZE_GRANULARITY, f"{width}x{height} -> {tw}x{th}"
        print(f"[ok] {width}x{height} -> {tw}x{th}, not enlarged")
    return True


def test_user_budget_limits_resolution():
    """A user ceiling trades resolution for cost and latency."""
    print("\n=== Testing the user's resolution ceiling ===")

    capped = dict(CUSTOM, max_edge=1024, budget_pixels=1024 * 1024)
    tw, th = choose_target_shape(3000, 2000, capped)
    assert max(tw, th) <= 1024, f"{tw}x{th} ignores the ceiling"
    _check((tw, th), capped)
    print(f"[ok] a 1024 ceiling turns 3000x2000 into {tw}x{th}")

    uncapped_w, uncapped_h = choose_target_shape(3000, 2000, CUSTOM)
    assert uncapped_w > tw, "the ceiling should actually reduce the size"
    print(f"[ok] without the ceiling the same source gives {uncapped_w}x{uncapped_h}")
    return True


def test_padding_collapses():
    """The headline improvement, measured.

    Under fixed shapes a 1600x1200 region is letterboxed into 1536x1024 and
    loses most of its height to padding. With custom sizes the target matches
    the region, so padding is at most one 16px step.
    """
    print("\n=== Testing padding reduction ===")

    width, height = 1600, 1200

    legacy_shape = choose_target_shape(width, height, None)
    legacy = calculate_padding_for_shape(width, height, *legacy_shape)
    legacy_padding = sum(legacy["padding"])

    custom_shape = choose_target_shape(width, height, CUSTOM)
    custom = calculate_padding_for_shape(width, height, *custom_shape)
    custom_padding = sum(custom["padding"])

    print(f"     fixed shapes : {width}x{height} -> {legacy_shape}, "
          f"{legacy_padding}px padding, scale {legacy['scale_factor']:.3f}")
    print(f"     custom sizes : {width}x{height} -> {custom_shape}, "
          f"{custom_padding}px padding, scale {custom['scale_factor']:.3f}")

    assert custom_padding < legacy_padding, "custom sizing should reduce padding"
    assert custom_padding <= 2 * SIZE_GRANULARITY, (
        f"padding should be at most a step per axis, got {custom_padding}px"
    )
    print(f"[ok] padding drops from {legacy_padding}px to {custom_padding}px")

    assert custom["scale_factor"] > legacy["scale_factor"], (
        "custom sizing should preserve more of the source resolution"
    )
    gain = custom["scale_factor"] / legacy["scale_factor"]
    print(f"[ok] {gain:.2f}x more source resolution reaches the model")
    return True


def test_extraction_uses_the_policy():
    """The policy must reach the context extraction, not just the helper."""
    print("\n=== Testing extraction with a policy ===")

    legacy = extract_context_with_selection(
        2000, 1500, 800, 600, 1200, 900, mode="focused", has_selection=True
    )
    assert legacy["target_shape"] in FIXED_SHAPES
    ok, why = validate_context_info(legacy)
    assert ok, why
    print(f"[ok] without a policy: {legacy['target_shape']}")

    custom = extract_context_with_selection(
        2000, 1500, 800, 600, 1200, 900, mode="focused", has_selection=True,
        size_policy=CUSTOM,
    )
    shape = custom["target_shape"]
    _check(shape)
    ok, why = validate_context_info(custom, CUSTOM)
    assert ok, why
    print(f"[ok] with a policy: {shape}, and it validates")

    region = custom["extract_region"]
    region_ratio = region[2] / region[3]
    shape_ratio = shape[0] / shape[1]
    assert abs(region_ratio - shape_ratio) / region_ratio < 0.05, (
        f"region {region[2]}x{region[3]} vs target {shape}"
    )
    print(f"[ok] target tracks the extract region {region[2]}x{region[3]}")

    full = extract_context_with_selection(
        1800, 1200, 0, 0, 0, 0, mode="full", has_selection=False, size_policy=CUSTOM
    )
    _check(full["target_shape"])
    print(f"[ok] full-image mode honours it too: {full['target_shape']}")
    return True


def test_degenerate_inputs():
    """Invalid sizes must not raise."""
    print("\n=== Testing degenerate inputs ===")

    for width, height in ((0, 0), (-100, 200), (0, 500)):
        shape = choose_target_shape(width, height, CUSTOM)
        assert shape == (1024, 1024), f"{width}x{height} -> {shape}"
    print("[ok] zero and negative dimensions fall back to 1024x1024")
    return True


def test_is_shape_allowed_rejections():
    """Validation mirrors each rejection the API actually returns."""
    print("\n=== Testing shape validation ===")

    for (w, h), fragment in (
        ((13, 17), "divisible by 16"),
        ((4096, 1024), "longest edge"),
        ((512, 512), "minimum pixel budget"),
        ((3840, 3840), "maximum pixel budget"),
        ((256, 3072), "aspect ratio"),
    ):
        ok, why = is_shape_allowed(w, h, CUSTOM)
        assert not ok, f"{w}x{h} should be rejected"
        assert fragment in why, f"{w}x{h}: expected {fragment!r}, got {why!r}"
    print("[ok] each rejection matches the API's own wording")

    ok, _ = is_shape_allowed(2048, 2048, CUSTOM)
    assert ok, "2048x2048 is accepted live"
    ok, _ = is_shape_allowed(1024, 1024, None)
    assert ok, "1024x1024 is valid under the legacy policy"
    ok, _ = is_shape_allowed(2048, 2048, None)
    assert not ok, "the legacy policy allows only the three fixed shapes"
    print("[ok] accepts what the API accepts, under both policies")
    return True


def run_all_tests():
    print("Running Size Policy Tests")
    print("=" * 60)

    tests = [
        test_legacy_path_unchanged,
        test_target_follows_source_aspect,
        test_edges_are_multiples_of_16,
        test_bounds_are_respected,
        test_no_pointless_upscaling,
        test_user_budget_limits_resolution,
        test_padding_collapses,
        test_extraction_uses_the_policy,
        test_degenerate_inputs,
        test_is_shape_allowed_rejections,
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
        print(f"{len(failures)} of {len(tests)} size policy tests FAILED")
        for name, err in failures:
            print(f"  - {name}: {err}")
        return False
    print(f"All {len(tests)} size policy tests passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if run_all_tests() else 1)
