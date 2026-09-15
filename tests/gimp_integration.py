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

    # Methods AND class-level constants: the plugin keeps things like
    # PROCESSING_MODES and DEFAULT_RESULT_MARGIN on the class, and methods
    # that read them fail with AttributeError if only callables are copied.
    for name, attr in vars(module.GimpAIPlugin).items():
        if name.startswith("__"):
            continue
        if callable(attr) or not hasattr(attr, "__get__"):
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


def transparent_fraction(png_bytes, sample=192):
    """What fraction of the mask is transparent, by area rather than extent.

    A bounding box is a poor measure here: custom sizing can leave a 1px
    padding column, and a box spanning it reads as 100% of the canvas however
    small the actual hole is. This scales the mask down in GIMP and counts
    pixels, which is both accurate and quick - decoding a full-size PNG in
    pure Python takes tens of seconds.
    """
    import zlib

    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    small_fd, small_path = tempfile.mkstemp(suffix=".png")
    os.close(small_fd)
    try:
        with open(path, "wb") as handle:
            handle.write(png_bytes)
        loaded = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path))
        loaded.scale(sample, sample)
        Gimp.file_save(
            Gimp.RunMode.NONINTERACTIVE, loaded,
            Gio.File.new_for_path(small_path), None,
        )
        loaded.delete()

        data = open(small_path, "rb").read()
        # Minimal PNG reader: header for geometry, IDAT for pixels.
        import struct

        pos, idat, hdr = 8, bytearray(), None
        while pos < len(data):
            (length,) = struct.unpack(">I", data[pos:pos + 4])
            ctype = data[pos + 4:pos + 8]
            if ctype == b"IHDR":
                w, h, depth, color = struct.unpack(">IIBB", data[pos + 8:pos + 18])
                hdr = (w, h, color)
            elif ctype == b"IDAT":
                idat += data[pos + 8:pos + 8 + length]
            elif ctype == b"IEND":
                break
            pos += 12 + length

        w, h, color = hdr
        channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
        if channels is None or color not in (4, 6):
            return 0.0  # no alpha channel at all means nothing is transparent

        raw = zlib.decompress(bytes(idat))
        stride = w * channels
        prev = bytearray(stride)
        transparent = 0
        offset = 0
        for _ in range(h):
            filt = raw[offset]
            offset += 1
            line = bytearray(raw[offset:offset + stride])
            offset += stride
            if filt == 1:
                for i in range(channels, stride):
                    line[i] = (line[i] + line[i - channels]) & 0xFF
            elif filt == 2:
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif filt == 3:
                for i in range(stride):
                    left = line[i - channels] if i >= channels else 0
                    line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
            elif filt == 4:
                for i in range(stride):
                    a = line[i - channels] if i >= channels else 0
                    b = prev[i]
                    c = prev[i - channels] if i >= channels else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[i] = (line[i] + pr) & 0xFF
            for x in range(w):
                if line[x * channels + channels - 1] < 128:
                    transparent += 1
            prev = line
        return transparent / float(w * h)
    finally:
        for temp in (path, small_path):
            try:
                os.unlink(temp)
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
    fraction = transparent_fraction(mask)
    image.delete()

    if x1 is None:
        emit("FAIL", name, "mask is fully opaque - selection never reached it")
        return False
    if fraction < 0.001:
        emit("FAIL", name, "mask has no meaningful transparent area")
        return False
    if fraction > 0.5:
        emit("FAIL", name,
             f"transparent region covers {100*fraction:.1f}% of the mask by area")
        return False

    emit("PASS", name,
         f"transparent area is {100*fraction:.1f}% of the mask "
         f"(bbox {x2-x1}x{y2-y1} of {size[0]}x{size[1]})")
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


def call_procedure(name, image, layer, **args):
    """Invoke one of our procedures non-interactively. Returns the PDB status."""
    proc = Gimp.get_pdb().lookup_procedure(name)
    if proc is None:
        raise AssertionError(f"{name} is not registered")
    config = proc.create_config()
    config.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
    config.set_property("image", image)
    # "drawables" is deliberately left unset. It is a GimpCoreObjectArray,
    # which cannot be assigned from Python here - a list, a tuple and a bare
    # layer all raise TypeError - and none of the run_* methods read it; they
    # use image.get_selected_layers() instead. Its default is None.
    for key, value in args.items():
        config.set_property(key, value)
    result = proc.run(config)
    return result.index(0)


def test_installed_plugin_is_current(plugin):
    """The installed plugin must match the checkout.

    Half these tests import the plugin from the repo; the procedure-level
    ones go through the PDB and therefore exercise whatever GIMP has
    registered, which is the copy in the plug-ins directory. When those
    diverge the failures are baffling - a fix that is plainly present in the
    source appears not to work.
    """
    name = "installed_plugin_is_current"
    import hashlib

    installed_dir = os.path.join(Gimp.directory(), "plug-ins", "gimp-ai-plugin")
    mismatched, missing = [], []
    for filename in ("gimp-ai-plugin.py", "coordinate_utils.py", "openai_client.py"):
        source = os.path.join(repo_root(), filename)
        target = os.path.join(installed_dir, filename)
        if not os.path.exists(target):
            missing.append(filename)
            continue
        digest = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
        if digest(source) != digest(target):
            mismatched.append(filename)

    if missing or mismatched:
        detail = []
        if missing:
            detail.append(f"not installed: {', '.join(missing)}")
        if mismatched:
            detail.append(f"stale: {', '.join(mismatched)}")
        detail.append(f"copy the repo files to {installed_dir} and re-run")
        emit("FAIL", name, "; ".join(detail))
        return False

    emit("PASS", name, f"installed copy matches the checkout ({installed_dir})")
    return True


def test_result_margin_scales_with_selection(plugin):
    """The result mask grows past the selection, proportionally.

    A subject drawn into an ellipse gets its wings and legs clipped by the
    ellipse outline. The margin keeps that overspill visible. It scales with
    the selection so a small edit is not swamped and a large one is not
    swallowed.
    """
    name = "result_margin_scales_with_selection"

    def context(x1, y1, x2, y2):
        return {"selection_bounds": (x1, y1, x2, y2), "has_selection": True}

    original = dict(plugin.config)
    try:
        plugin.config["result_margin"] = 25
        small = plugin._get_result_margin_px(context(100, 100, 300, 260))   # 200x160
        large = plugin._get_result_margin_px(context(0, 0, 1600, 1200))     # 1600x1200
        if small <= 0 or large <= small:
            emit("FAIL", name, f"margins do not scale: small={small}, large={large}")
            return False

        # Bounds: never zero for a real selection, never runaway.
        tiny = plugin._get_result_margin_px(context(0, 0, 10, 8))
        huge = plugin._get_result_margin_px(context(0, 0, 8000, 6000))
        if tiny < 4:
            emit("FAIL", name, f"tiny selection got an unusable margin of {tiny}px")
            return False
        if huge > 256:
            emit("FAIL", name, f"huge selection got a runaway margin of {huge}px")
            return False

        plugin.config["result_margin"] = 0
        strict = plugin._get_result_margin_px(context(100, 100, 300, 260))
        if strict != 0:
            emit("FAIL", name, f"strict setting gave {strict}px, expected 0")
            return False

        plugin.config["result_margin"] = -1
        unclipped = plugin._get_result_margin_px(context(100, 100, 300, 260))
        if unclipped != -1:
            emit("FAIL", name, f"no-clip setting gave {unclipped}, expected -1")
            return False

        # A corrupt config value must not break an edit.
        plugin.config["result_margin"] = "nonsense"
        fallback = plugin._get_result_margin_px(context(100, 100, 300, 260))
        if fallback <= 0:
            emit("FAIL", name, f"bad config value gave {fallback}")
            return False
    finally:
        plugin.config = original

    emit("PASS", name,
         f"200x160 selection -> {small}px, 1600x1200 -> {large}px, "
         f"strict 0px, no-clip -1, bad value falls back to {fallback}px")
    return True


def test_procedures_declare_arguments(plugin):
    """Each procedure must expose the arguments that make it scriptable."""
    name = "procedures_declare_arguments"
    expected = {
        "gimp-ai-inpaint": {"prompt", "mode"},
        "gimp-ai-layer-generator": {"prompt"},
        "gimp-ai-layer-composite": {"prompt", "use-mask"},
    }
    missing = []
    found = {}
    for proc_name, wanted in expected.items():
        proc = Gimp.get_pdb().lookup_procedure(proc_name)
        if proc is None:
            missing.append(f"{proc_name}: not registered")
            continue
        names = {arg.name for arg in proc.get_arguments()}
        found[proc_name] = sorted(names - {"run-mode", "image", "drawables"})
        absent = wanted - names
        if absent:
            missing.append(f"{proc_name}: missing {sorted(absent)}")

    if missing:
        emit("FAIL", name, "; ".join(missing))
        return False

    detail = "; ".join(f"{k} {v}" for k, v in sorted(found.items()))
    emit("PASS", name, detail)
    return True


def test_noninteractive_rejects_empty_prompt(plugin):
    """An empty prompt must fail cleanly, before any API call.

    Non-interactively there is no user to cancel, so a missing argument is a
    calling error - and must not hang waiting on a dialog that will never be
    shown. This test costs nothing: it returns before the network.
    """
    name = "noninteractive_rejects_empty_prompt"
    image, layer = make_test_image(600, 600)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 100, 100, 200, 200)
    try:
        status = call_procedure("gimp-ai-inpaint", image, layer, prompt="", mode="contextual")
    finally:
        image.delete()

    if status != Gimp.PDBStatusType.CALLING_ERROR:
        emit("FAIL", name, f"expected CALLING_ERROR, got {status}")
        return False
    emit("PASS", name, "empty prompt returns CALLING_ERROR without contacting the API")
    return True


def test_noninteractive_rejects_bad_mode(plugin):
    """An unknown mode must be refused rather than silently defaulted."""
    name = "noninteractive_rejects_bad_mode"
    image, layer = make_test_image(600, 600)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, 100, 100, 200, 200)
    try:
        status = call_procedure(
            "gimp-ai-inpaint", image, layer, prompt="a fly", mode="sideways"
        )
    finally:
        image.delete()

    if status != Gimp.PDBStatusType.CALLING_ERROR:
        emit("FAIL", name, f"expected CALLING_ERROR, got {status}")
        return False
    emit("PASS", name, "unknown mode returns CALLING_ERROR")
    return True


def test_live_inpaint_end_to_end(plugin):
    """A real inpaint, start to finish, adding a layer to the image.

    Opt-in: costs money and needs a key. Enable with
        GIMP_AI_LIVE_TESTS=1 and OPENAI_API_KEY set.
    """
    name = "live_inpaint_end_to_end"
    if os.environ.get("GIMP_AI_LIVE_TESTS") != "1":
        emit("SKIP", name, "set GIMP_AI_LIVE_TESTS=1 to run (makes a paid API call)")
        return True
    if not os.environ.get("OPENAI_API_KEY"):
        emit("SKIP", name, "OPENAI_API_KEY is not set")
        return True

    image, layer = make_test_image(1024, 1024, fill="gray")
    image.select_ellipse(Gimp.ChannelOps.REPLACE, 380, 380, 264, 264)
    before = len(image.get_layers())
    try:
        status = call_procedure(
            "gimp-ai-inpaint", image, layer,
            prompt="a small brass gear, centred", mode="contextual",
        )
        after = len(image.get_layers())
    finally:
        image.delete()

    if status != Gimp.PDBStatusType.SUCCESS:
        emit("FAIL", name, f"expected SUCCESS, got {status}")
        return False
    if after <= before:
        emit("FAIL", name, f"no layer added: {before} before, {after} after")
        return False
    emit("PASS", name, f"full inpaint succeeded, layers {before} -> {after}")
    return True


TESTS = [
    test_installed_plugin_is_current,
    test_mask_marks_the_selection,
    test_mask_not_mostly_transparent,
    test_ellipse_selection_is_preserved,
    test_mask_matches_image_dimensions,
    test_result_margin_scales_with_selection,
    test_procedures_declare_arguments,
    test_noninteractive_rejects_empty_prompt,
    test_noninteractive_rejects_bad_mode,
    test_live_inpaint_end_to_end,
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
