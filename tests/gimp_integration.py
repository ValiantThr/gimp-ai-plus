"""
Integration tests that run INSIDE GIMP.

Do not run this file directly - it needs GIMP's Python and a live Gimp
module. Use tests/run_gimp_tests.py, which launches it via:

    gimp-console-3.2 -idf --batch-interpreter=python-fu-eval \\
        -b "exec(open('tests/gimp_integration.py').read())" --quit

These cover what the pure unit tests cannot: the GEGL and GIMP calls that
build the inpainting mask. Both bugs found during Phase 1 manual testing
lived here, invisible to unit tests and to the plugin's own error handling.

Results are printed with a GIMPTEST| prefix so the outer runner can parse
them regardless of whatever else GIMP writes to stdout.
"""

import importlib.util
import os
import sys
import tempfile

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gimp, Gegl, Gio

MARK = "GIMPTEST|"


def emit(status, name, detail=""):
    print(f"{MARK}{status}|{name}|{detail}")


def repo_root():
    """The checkout this file lives in.

    Injected by run_gimp_tests.py as GIMP_AI_REPO_ROOT: this file is run via
    exec(), so __file__ here is GIMP's own batch plug-in, not this script.
    """
    injected = globals().get("GIMP_AI_REPO_ROOT")
    if injected:
        return injected
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_plugin_module():
    """Import gimp-ai-plugin.py despite the hyphen in its name.

    Safe to import: the module guards Gimp.main() behind __main__.
    """
    root = repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    path = os.path.join(root, "gimp-ai-plugin.py")
    spec = importlib.util.spec_from_file_location("gimp_ai_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_plugin_shim(module):
    """A stand-in carrying the plugin's methods without being a Gimp.PlugIn.

    GimpAIPlugin cannot simply be instantiated here: Gimp.PlugIn's constructor
    asserts on a live wire protocol channel to GIMP core
    (gimp_plug_in_constructed: priv->read_channel != NULL) and aborts the
    process when constructed outside the plug-in lifecycle.

    The image-processing methods do not actually need any of that - they need
    self.config and a few sibling methods. Grafting them onto a plain object
    exercises the real code unmodified. That this is necessary is itself a
    finding: the coordinate and mask logic is entangled with the plug-in
    lifecycle for no reason, and Phase 3 should lift it into a class that does
    not inherit from Gimp.PlugIn.
    """
    class PluginShim:
        def __init__(self):
            self._cancel_requested = False
            self.config = {}

    for name, attr in vars(module.GimpAIPlugin).items():
        if callable(attr) and not name.startswith("__"):
            setattr(PluginShim, name, attr)

    shim = PluginShim()
    try:
        shim.config = shim._load_config()
    except Exception:
        shim.config = {}
    return shim


def make_test_image(width, height, fill="gray"):
    """A flat single-layer image to select within."""
    image = Gimp.Image.new(width, height, Gimp.ImageBaseType.RGB)
    layer = Gimp.Layer.new(
        image, "base", width, height,
        Gimp.ImageType.RGBA_IMAGE, 100.0, Gimp.LayerMode.NORMAL,
    )
    image.insert_layer(layer, None, 0)
    Gimp.context_set_foreground(Gegl.Color.new(fill))
    layer.edit_fill(Gimp.FillType.FOREGROUND)
    return image, layer


def transparent_bounds(png_bytes):
    """Bounding box of the transparent region of a PNG, via GIMP itself.

    Loads the PNG, selects the layer by its alpha (which picks out the
    OPAQUE area), inverts to get the transparent area, and reads the bounds.
    Pure-Python PNG decoding would work but takes tens of seconds on a
    1024x1024 image; GIMP does it in C.

    Returns (x1, y1, x2, y2) or None when nothing is transparent - which is
    exactly the signature of the focused-mask bug.
    """
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        with open(path, "wb") as handle:
            handle.write(png_bytes)
        loaded = Gimp.file_load(
            Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path)
        )
        layer = loaded.get_layers()[0]
        loaded.select_item(Gimp.ChannelOps.REPLACE, layer)
        Gimp.Selection.invert(loaded)
        bounds = Gimp.Selection.bounds(loaded)
        # GIMP 3 returns six values: the PDB success flag first, then
        # (non_empty, x1, y1, x2, y2). Getting this off by one silently
        # yields nonsense like a box of (False,0)-(0,1024), so index
        # explicitly rather than unpacking positionally.
        non_empty = bounds[1]
        x1, y1, x2, y2 = bounds[2], bounds[3], bounds[4], bounds[5]
        size = (loaded.get_width(), loaded.get_height())
        loaded.delete()
        return (x1, y1, x2, y2, size) if non_empty else (None, None, None, None, size)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def build_mask(plugin, image):
    """Run the plugin's real context-extraction and mask-creation path."""
    context_info = plugin._calculate_context_extraction(image)
    if not context_info:
        raise AssertionError("_calculate_context_extraction returned None")
    target_size = max(context_info["target_shape"])
    mask = plugin._create_context_mask(image, context_info, target_size)
    if not mask:
        raise AssertionError("_create_context_mask returned no data")
    return context_info, mask


def expected_mask_box(context_info, sel):
    """Where the selection should land in mask coordinates.

    Selection is in image coordinates; the mask canvas is the extract region
    scaled to the target shape and then padded. So: shift by the extract
    region origin, scale, then add the padding offset.
    """
    ex, ey = context_info["extract_region"][0], context_info["extract_region"][1]
    padding = context_info["padding_info"]
    scale = padding["scale_factor"]
    pad_left, pad_top = padding["padding"][0], padding["padding"][1]
    sx1, sy1, sx2, sy2 = sel
    return (
        (sx1 - ex) * scale + pad_left,
        (sy1 - ey) * scale + pad_top,
        (sx2 - ex) * scale + pad_left,
        (sy2 - ey) * scale + pad_top,
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_mask_marks_the_selection(plugin):
    """The selection must reach the mask as a transparent region.

    Regression test for the Phase 1 bug: the mask canvas is built at
    extract-region size while the selection channel is in full-image
    coordinates. Without a translate the selection is composited at absolute
    coordinates, falls off the smaller canvas and is clipped away entirely,
    leaving a fully opaque mask that marks nothing for inpainting.
    """
    name = "mask_marks_the_selection"
    image, _ = make_test_image(1000, 1000)
    sel = (400, 380, 640, 620)
    image.select_rectangle(
        Gimp.ChannelOps.REPLACE, sel[0], sel[1], sel[2] - sel[0], sel[3] - sel[1]
    )

    context_info, mask = build_mask(plugin, image)

    # The test is only meaningful if the extract region is actually offset -
    # at origin (0,0) the missing translate would be a no-op and the bug
    # would not reproduce.
    ex, ey = context_info["extract_region"][0], context_info["extract_region"][1]
    if ex == 0 and ey == 0:
        image.delete()
        emit("FAIL", name, f"extract region is at origin ({ex},{ey}); test cannot detect the bug")
        return False

    x1, y1, x2, y2, size = transparent_bounds(mask)
    image.delete()

    if x1 is None:
        emit("FAIL", name,
             f"mask is fully opaque - selection never reached it "
             f"(extract region origin was ({ex},{ey}))")
        return False

    exp = expected_mask_box(context_info, sel)
    tol = 12  # antialiasing and rounding through the scale step
    deltas = [abs(a - b) for a, b in zip((x1, y1, x2, y2), exp)]
    if max(deltas) > tol:
        emit("FAIL", name,
             f"transparent box ({x1},{y1})-({x2},{y2}) but expected "
             f"~({exp[0]:.0f},{exp[1]:.0f})-({exp[2]:.0f},{exp[3]:.0f}); "
             f"max delta {max(deltas):.1f} > {tol}")
        return False

    emit("PASS", name,
         f"transparent box ({x1},{y1})-({x2},{y2}) in {size[0]}x{size[1]}, "
         f"extract origin ({ex},{ey}), max delta {max(deltas):.1f}px")
    return True


def test_mask_not_mostly_transparent(plugin):
    """A small selection must not blow out into a mostly-transparent mask.

    Guards the opposite failure: if the selection were composited at the
    wrong scale the hole could swallow the canvas, making the model
    regenerate everything.
    """
    name = "mask_not_mostly_transparent"
    image, _ = make_test_image(1200, 900)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 700, 500, 120, 100)

    context_info, mask = build_mask(plugin, image)
    x1, y1, x2, y2, size = transparent_bounds(mask)
    image.delete()

    if x1 is None:
        emit("FAIL", name, "mask is fully opaque - selection never reached it")
        return False

    area = (x2 - x1) * (y2 - y1)
    canvas = size[0] * size[1]
    fraction = area / canvas
    if fraction > 0.5:
        emit("FAIL", name, f"transparent region covers {100*fraction:.1f}% of the mask")
        return False

    emit("PASS", name,
         f"transparent region is {100*fraction:.1f}% of the mask "
         f"({x2-x1}x{y2-y1} of {size[0]}x{size[1]})")
    return True


def test_ellipse_selection_is_preserved(plugin):
    """An elliptical selection must not come back as its bounding rectangle."""
    name = "ellipse_selection_is_preserved"
    image, _ = make_test_image(1000, 1000)
    sel = (300, 300, 700, 700)
    image.select_ellipse(
        Gimp.ChannelOps.REPLACE, sel[0], sel[1], sel[2] - sel[0], sel[3] - sel[1]
    )

    context_info, mask = build_mask(plugin, image)
    x1, y1, x2, y2, size = transparent_bounds(mask)
    image.delete()

    if x1 is None:
        emit("FAIL", name, "mask is fully opaque - selection never reached it")
        return False

    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        emit("FAIL", name, f"degenerate transparent box {w}x{h}")
        return False
    if abs(w - h) > max(w, h) * 0.1:
        emit("FAIL", name, f"expected a roughly square box, got {w}x{h}")
        return False

    # Shape alone is not enough: with the translate disabled a shifted ellipse
    # can still produce a square box, so this test passed against the bug until
    # position was asserted too.
    exp = expected_mask_box(context_info, sel)
    tol = 12
    deltas = [abs(a - b) for a, b in zip((x1, y1, x2, y2), exp)]
    if max(deltas) > tol:
        emit("FAIL", name,
             f"box ({x1},{y1})-({x2},{y2}) but expected "
             f"~({exp[0]:.0f},{exp[1]:.0f})-({exp[2]:.0f},{exp[3]:.0f}); "
             f"max delta {max(deltas):.1f} > {tol}")
        return False

    emit("PASS", name,
         f"elliptical selection produced a {w}x{h} region at the right place "
         f"(max delta {max(deltas):.1f}px)")
    return True


def test_mask_matches_image_dimensions(plugin):
    """Mask and input image must agree on size, or the API rejects the edit."""
    name = "mask_matches_image_dimensions"
    image, _ = make_test_image(1400, 1000)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 500, 300, 300, 250)

    context_info, mask = build_mask(plugin, image)
    success, message, image_data = plugin._extract_context_region(image, context_info)
    image.delete()

    if not success:
        emit("FAIL", name, f"_extract_context_region failed: {message}")
        return False

    import base64
    from openai_client import png_info

    image_bytes = base64.b64decode(image_data) if isinstance(image_data, str) else image_data
    img_info = png_info(image_bytes)
    mask_info = png_info(mask)

    if not img_info or not mask_info:
        emit("FAIL", name, "could not parse PNG headers")
        return False

    if (img_info["width"], img_info["height"]) != (mask_info["width"], mask_info["height"]):
        emit("FAIL", name,
             f"image is {img_info['width']}x{img_info['height']} but mask is "
             f"{mask_info['width']}x{mask_info['height']}")
        return False

    emit("PASS", name,
         f"image and mask agree at {img_info['width']}x{img_info['height']} "
         f"(image {img_info['format']}, mask {mask_info['format']})")
    return True


TESTS = [
    test_mask_marks_the_selection,
    test_mask_not_mostly_transparent,
    test_ellipse_selection_is_preserved,
    test_mask_matches_image_dimensions,
]


def main():
    print(f"{MARK}BEGIN|{len(TESTS)}|")

    # GEGL must be initialised before any Gegl.Node() graph will work. The
    # plug-in lifecycle normally does this, but python-fu-eval batch context
    # does not, and the failure is silent and confusing: create_child() warns
    # "g_hash_table_remove_all: assertion 'hash_table != NULL' failed" and the
    # graph then no-ops, producing a blank mask of a constant size.
    try:
        Gegl.init(None)
    except Exception as err:
        emit("FAIL", "gegl_init", f"{type(err).__name__}: {err}")

    try:
        module = load_plugin_module()
        plugin = make_plugin_shim(module)
    except Exception as err:
        emit("FAIL", "load_plugin", f"{type(err).__name__}: {err}")
        print(f"{MARK}END|0|{len(TESTS)}")
        return

    passed = 0
    for test in TESTS:
        try:
            if test(plugin):
                passed += 1
        except Exception as err:
            import traceback
            emit("FAIL", test.__name__, f"{type(err).__name__}: {err}")
            traceback.print_exc()
    print(f"{MARK}END|{passed}|{len(TESTS)}")


main()
