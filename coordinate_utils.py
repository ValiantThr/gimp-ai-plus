"""
Pure coordinate transformation utilities for GIMP AI Plugin.

These functions contain no GIMP dependencies and can be unit tested independently.
All coordinate calculations for context extraction, masking, and placement are here.
"""


FIXED_SHAPES = ((1024, 1024), (1536, 1024), (1024, 1536))

# Defaults matching what the gpt-image-2.x models accept. Verified against the
# live API; see FORK.md. A policy of None means "legacy fixed shapes".
DEFAULT_SIZE_POLICY = {
    'custom_sizes': True,
    'max_edge': 3840,
    'min_pixels': 655360,
    'max_pixels': 8294400,
    'max_aspect': 3.0,
    'budget_pixels': None,   # user's own ceiling, to trade cost for quality
}

SIZE_GRANULARITY = 16


def _round_to_granularity(value, granularity=SIZE_GRANULARITY, round_up=True):
    """Round to a multiple of the API's size granularity, never below one step."""
    if round_up:
        stepped = -(-int(value) // granularity) * granularity
    else:
        stepped = (int(value) // granularity) * granularity
    return max(granularity, stepped)


def choose_target_shape(width, height, policy=None):
    """
    Pick the API output size for a source region.

    With a custom-size policy the target follows the source's own aspect
    ratio, so the content scales into it almost exactly and padding is at most
    one 16px step per axis. That is the whole point of this function: the
    legacy behaviour forced every region into one of three fixed shapes, which
    meant letterboxing the content and throwing away resolution on the way.

    The scale is chosen to avoid upscaling where possible - sending a region
    larger than it really is costs money and invents detail - subject to the
    API's minimum pixel budget, which may force an upscale for small regions.

    Args:
        width, height: source region size
        policy: dict as DEFAULT_SIZE_POLICY, or None for legacy fixed shapes

    Returns:
        tuple: (target_width, target_height)
    """
    if width <= 0 or height <= 0:
        return (1024, 1024)

    if not policy or not policy.get('custom_sizes'):
        return get_optimal_openai_shape(width, height)

    max_edge = policy.get('max_edge') or 3840
    min_pixels = policy.get('min_pixels') or 0
    max_pixels = policy.get('max_pixels') or (max_edge * max_edge)
    max_aspect = policy.get('max_aspect') or 0
    budget = policy.get('budget_pixels')
    if budget:
        max_pixels = min(max_pixels, budget)

    src_w, src_h = float(width), float(height)

    # An extreme source cannot be represented exactly; clamp to the widest
    # allowed ratio and let the usual padding absorb the difference.
    if max_aspect:
        ratio = src_w / src_h
        if ratio > max_aspect:
            src_h = src_w / max_aspect
        elif ratio < 1.0 / max_aspect:
            src_w = src_h * max_aspect

    scale = 1.0
    pixels = src_w * src_h
    if pixels > max_pixels:
        scale = (max_pixels / pixels) ** 0.5
    longest = max(src_w, src_h) * scale
    if longest > max_edge:
        scale *= max_edge / longest
    # Only now consider growing: the minimum budget is a hard API floor.
    if min_pixels and (src_w * scale) * (src_h * scale) < min_pixels:
        scale = (min_pixels / (src_w * src_h)) ** 0.5

    target_w = _round_to_granularity(src_w * scale)
    target_h = _round_to_granularity(src_h * scale)

    # Rounding up can push a dimension past an edge limit; step back down.
    target_w = min(target_w, _round_to_granularity(max_edge, round_up=False))
    target_h = min(target_h, _round_to_granularity(max_edge, round_up=False))

    # Shrink a step at a time if rounding up crossed the pixel ceiling.
    while target_w * target_h > max_pixels and target_w > SIZE_GRANULARITY \
            and target_h > SIZE_GRANULARITY:
        if target_w >= target_h:
            target_w -= SIZE_GRANULARITY
        else:
            target_h -= SIZE_GRANULARITY

    # ...and grow if we are under the floor.
    while min_pixels and target_w * target_h < min_pixels:
        if target_w <= target_h and target_w + SIZE_GRANULARITY <= max_edge:
            target_w += SIZE_GRANULARITY
        elif target_h + SIZE_GRANULARITY <= max_edge:
            target_h += SIZE_GRANULARITY
        else:
            break

    return (int(target_w), int(target_h))


def is_shape_allowed(target_width, target_height, policy=None):
    """Would the API accept this size? Returns (ok, reason)."""
    if not policy or not policy.get('custom_sizes'):
        if (target_width, target_height) not in FIXED_SHAPES:
            return False, f"target_shape must be one of {list(FIXED_SHAPES)}"
        return True, ""

    if target_width <= 0 or target_height <= 0:
        return False, "dimensions must be positive"
    if target_width % SIZE_GRANULARITY or target_height % SIZE_GRANULARITY:
        return False, f"both edges must be divisible by {SIZE_GRANULARITY}"
    max_edge = policy.get('max_edge')
    if max_edge and max(target_width, target_height) > max_edge:
        return False, f"the longest edge must be {max_edge} or less"
    pixels = target_width * target_height
    if policy.get('min_pixels') and pixels < policy['min_pixels']:
        return False, "below the minimum pixel budget"
    max_pixels = policy.get('max_pixels')
    if max_pixels and pixels > max_pixels:
        return False, "exceeds the maximum pixel budget"
    max_aspect = policy.get('max_aspect')
    if max_aspect:
        ratio = max(target_width / target_height, target_height / target_width)
        if ratio > max_aspect:
            return False, f"the maximum aspect ratio is {max_aspect:g}:1"
    return True, ""


def get_optimal_openai_shape(width, height):
    """
    Select optimal OpenAI shape based on image dimensions.

    Legacy fixed-shape selection, still correct for the gpt-image-1 family,
    which accepts only these three sizes. Models that take custom sizes go
    through choose_target_shape() instead.

    Args:
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        tuple: (target_width, target_height) - one of (1024, 1024), (1536, 1024), (1024, 1536)
    """
    if width <= 0 or height <= 0:
        return (1024, 1024)  # Default to square for invalid dimensions
        
    aspect_ratio = width / height
    
    if aspect_ratio > 1.3:
        # Landscape orientation
        return (1536, 1024)
    elif aspect_ratio < 0.77:
        # Portrait orientation  
        return (1024, 1536)
    else:
        # Square or near-square
        return (1024, 1024)


def calculate_padding_for_shape(current_width, current_height, target_width, target_height):
    """
    Calculate padding needed to fit content into target OpenAI shape.
    
    Args:
        current_width: Current content width
        current_height: Current content height
        target_width: Target width (1024 or 1536)
        target_height: Target height (1024 or 1536)
        
    Returns:
        dict: {
            'scale_factor': Applied scaling factor,
            'scaled_size': (scaled_width, scaled_height),
            'padding': (left, top, right, bottom)
        }
    """
    # Calculate scale to fit within target
    scale_x = target_width / current_width
    scale_y = target_height / current_height
    scale = min(scale_x, scale_y)
    
    # Scale dimensions
    scaled_width = int(current_width * scale)
    scaled_height = int(current_height * scale)
    
    # Calculate padding to center
    pad_left = (target_width - scaled_width) // 2
    pad_top = (target_height - scaled_height) // 2
    pad_right = target_width - scaled_width - pad_left
    pad_bottom = target_height - scaled_height - pad_top
    
    return {
        'scale_factor': scale,
        'scaled_size': (scaled_width, scaled_height),
        'padding': (pad_left, pad_top, pad_right, pad_bottom)
    }


def extract_context_with_selection(img_width, img_height, sel_x1, sel_y1, sel_x2, sel_y2, 
                                  mode='focused', has_selection=True, size_policy=None):
    """
    Extract context region around selection for inpainting with optimal shape.
    
    Args:
        img_width: Source image width
        img_height: Source image height
        sel_x1, sel_y1, sel_x2, sel_y2: Selection bounds
        mode: 'focused' for partial extraction, 'full' for whole image
        has_selection: Whether there's an active selection
        size_policy: dict as DEFAULT_SIZE_POLICY for models that accept custom
            sizes, or None for the legacy three fixed shapes
        
    Returns:
        dict: Context extraction parameters with optimal shape
    """
    if not has_selection:
        # No selection - use center area
        target_shape = choose_target_shape(img_width, img_height, size_policy)
        # Create a default selection in center
        size = min(img_width, img_height, 512)
        sel_x1 = (img_width - size) // 2
        sel_y1 = (img_height - size) // 2
        sel_x2 = sel_x1 + size
        sel_y2 = sel_y1 + size
        
    sel_width = sel_x2 - sel_x1
    sel_height = sel_y2 - sel_y1
    
    if mode == 'full':
        # Send entire image with mask
        target_shape = choose_target_shape(img_width, img_height, size_policy)
        padding_info = calculate_padding_for_shape(img_width, img_height, 
                                                  target_shape[0], target_shape[1])
        return {
            'mode': 'full',
            'selection_bounds': (sel_x1, sel_y1, sel_x2, sel_y2),
            'extract_region': (0, 0, img_width, img_height),
            'target_shape': target_shape,
            'needs_padding': True,
            'padding_info': padding_info,
            'has_selection': has_selection
        }
    
    # Focused mode: extract region around selection
    # Calculate context padding (30-50% of selection, min 50px, max 300px)
    context_pad = max(50, min(300, int(max(sel_width, sel_height) * 0.4)))
    
    # Initial context bounds
    ctx_x1 = sel_x1 - context_pad
    ctx_y1 = sel_y1 - context_pad
    ctx_x2 = sel_x2 + context_pad
    ctx_y2 = sel_y2 + context_pad
    
    # Smart boundary handling: prefer not to extend beyond image
    if ctx_x1 < 0:
        shift = -ctx_x1
        ctx_x1 = 0
        ctx_x2 = min(img_width, ctx_x2 + shift)
    if ctx_y1 < 0:
        shift = -ctx_y1
        ctx_y1 = 0
        ctx_y2 = min(img_height, ctx_y2 + shift)
    if ctx_x2 > img_width:
        shift = ctx_x2 - img_width
        ctx_x2 = img_width
        ctx_x1 = max(0, ctx_x1 - shift)
    if ctx_y2 > img_height:
        shift = ctx_y2 - img_height
        ctx_y2 = img_height
        ctx_y1 = max(0, ctx_y1 - shift)
    
    ctx_width = ctx_x2 - ctx_x1
    ctx_height = ctx_y2 - ctx_y1
    
    # Determine optimal shape for context
    target_shape = choose_target_shape(ctx_width, ctx_height, size_policy)
    target_aspect = target_shape[0] / target_shape[1]
    current_aspect = ctx_width / ctx_height if ctx_height > 0 else 1.0
    
    # Try to extend extract region to match target aspect ratio.
    # With a custom-size policy the target already follows the region's own
    # aspect, so there is nothing to reconcile - and grabbing extra pixels
    # purely to fill a fixed shape would waste resolution rather than save it.
    custom = bool(size_policy and size_policy.get('custom_sizes'))
    if not custom and abs(current_aspect - target_aspect) > 0.01:
        if target_aspect > current_aspect:
            # Need wider region: extend horizontally if possible
            target_width = int(ctx_height * target_aspect)
            width_diff = target_width - ctx_width
            
            # Try to extend equally on both sides
            left_extend = width_diff // 2
            right_extend = width_diff - left_extend
            
            new_ctx_x1 = max(0, ctx_x1 - left_extend)
            new_ctx_x2 = min(img_width, ctx_x2 + right_extend)
            
            # If we hit boundaries, try to extend more on the available side
            if new_ctx_x1 == 0 and new_ctx_x2 < img_width:
                # Hit left boundary, extend right more
                remaining = target_width - (new_ctx_x2 - new_ctx_x1)
                new_ctx_x2 = min(img_width, new_ctx_x2 + remaining)
            elif new_ctx_x2 == img_width and new_ctx_x1 > 0:
                # Hit right boundary, extend left more  
                remaining = target_width - (new_ctx_x2 - new_ctx_x1)
                new_ctx_x1 = max(0, new_ctx_x1 - remaining)
                
            ctx_x1, ctx_x2 = new_ctx_x1, new_ctx_x2
            
        else:
            # Need taller region: extend vertically if possible
            target_height = int(ctx_width / target_aspect)
            height_diff = target_height - ctx_height
            
            # Try to extend equally on both sides
            top_extend = height_diff // 2
            bottom_extend = height_diff - top_extend
            
            new_ctx_y1 = max(0, ctx_y1 - top_extend)
            new_ctx_y2 = min(img_height, ctx_y2 + bottom_extend)
            
            # If we hit boundaries, try to extend more on the available side
            if new_ctx_y1 == 0 and new_ctx_y2 < img_height:
                # Hit top boundary, extend bottom more
                remaining = target_height - (new_ctx_y2 - new_ctx_y1)
                new_ctx_y2 = min(img_height, new_ctx_y2 + remaining)
            elif new_ctx_y2 == img_height and new_ctx_y1 > 0:
                # Hit bottom boundary, extend top more
                remaining = target_height - (new_ctx_y2 - new_ctx_y1)
                new_ctx_y1 = max(0, new_ctx_y1 - remaining)
                
            ctx_y1, ctx_y2 = new_ctx_y1, new_ctx_y2
    
    # Recalculate final dimensions
    ctx_width = ctx_x2 - ctx_x1
    ctx_height = ctx_y2 - ctx_y1
    
    if custom:
        # The region may have shifted against image boundaries above.
        target_shape = choose_target_shape(ctx_width, ctx_height, size_policy)

    padding_info = calculate_padding_for_shape(ctx_width, ctx_height,
                                              target_shape[0], target_shape[1])
    
    return {
        'mode': 'focused',
        'selection_bounds': (sel_x1, sel_y1, sel_x2, sel_y2),
        'extract_region': (ctx_x1, ctx_y1, ctx_width, ctx_height),
        'selection_in_extract': (
            sel_x1 - ctx_x1,
            sel_y1 - ctx_y1,
            sel_x2 - ctx_x1,
            sel_y2 - ctx_y1
        ),
        'target_shape': target_shape,
        'needs_padding': ctx_width != target_shape[0] or ctx_height != target_shape[1],
        'padding_info': padding_info,
        'has_selection': has_selection
    }


def calculate_result_placement(result_shape, original_shape, context_info):
    """
    Calculate placement for AI result back into original image.
    
    Args:
        result_shape: (width, height) of AI result
        original_shape: (width, height) of original image
        context_info: Context extraction info used for generation
        
    Returns:
        dict: Placement parameters
    """
    if context_info['mode'] == 'full':
        # Full image mode: scale entire result to original size
        scale_x = original_shape[0] / result_shape[0]
        scale_y = original_shape[1] / result_shape[1]
        
        return {
            'placement_mode': 'replace',
            'scale': (scale_x, scale_y),
            'position': (0, 0),
            'size': original_shape
        }
    else:
        # Focused mode: scale and position extract region
        extract_region = context_info['extract_region']
        target_shape = context_info['target_shape']
        
        # Calculate scale from result back to extract size
        scale_x = extract_region[2] / target_shape[0]
        scale_y = extract_region[3] / target_shape[1]
        
        return {
            'placement_mode': 'composite',
            'scale': (scale_x, scale_y),
            'position': (extract_region[0], extract_region[1]),
            'size': (extract_region[2], extract_region[3])
        }


def calculate_scale_from_shape(source_shape, target_shape):
    """
    Calculate scaling factors between two shapes.
    
    Args:
        source_shape: (width, height) tuple
        target_shape: (width, height) tuple
        
    Returns:
        dict: {
            'scale_x': Horizontal scale factor,
            'scale_y': Vertical scale factor,
            'uniform_scale': Min of scale_x and scale_y (preserves aspect ratio)
        }
    """
    scale_x = target_shape[0] / source_shape[0] if source_shape[0] > 0 else 1.0
    scale_y = target_shape[1] / source_shape[1] if source_shape[1] > 0 else 1.0
    
    return {
        'scale_x': scale_x,
        'scale_y': scale_y,
        'uniform_scale': min(scale_x, scale_y)
    }




def calculate_mask_coordinates(context_info, target_size):
    """
    Calculate mask coordinates for selection within extract region.

    Args:
        context_info: Context extraction info from extract_context_with_selection()
        target_size: Target size for the mask (e.g. 1024)

    Returns:
        dict with mask coordinates
    """
    if not context_info['has_selection']:
        # Create center circle mask for no selection case
        center = target_size // 2
        radius = target_size // 4
        return {
            'mask_type': 'circle',
            'center_x': center,
            'center_y': center,
            'radius': radius,
            'target_size': target_size
        }

    # Get extract region info
    sel_x1, sel_y1, sel_x2, sel_y2 = context_info['selection_bounds']
    ext_x1, ext_y1, ext_width, ext_height = context_info['extract_region']

    # Calculate selection position within the extract region
    sel_in_ext_x1 = sel_x1 - ext_x1
    sel_in_ext_y1 = sel_y1 - ext_y1
    sel_in_ext_x2 = sel_x2 - ext_x1
    sel_in_ext_y2 = sel_y2 - ext_y1

    # Scale to target size (use the larger dimension for scale factor)
    scale = target_size / max(ext_width, ext_height)
    mask_sel_x1 = int(sel_in_ext_x1 * scale)
    mask_sel_y1 = int(sel_in_ext_y1 * scale)
    mask_sel_x2 = int(sel_in_ext_x2 * scale)
    mask_sel_y2 = int(sel_in_ext_y2 * scale)

    # Ensure coordinates are within bounds
    mask_sel_x1 = max(0, min(target_size - 1, mask_sel_x1))
    mask_sel_y1 = max(0, min(target_size - 1, mask_sel_y1))
    mask_sel_x2 = max(0, min(target_size, mask_sel_x2))
    mask_sel_y2 = max(0, min(target_size, mask_sel_y2))

    return {
        'mask_type': 'rectangle',
        'x1': mask_sel_x1,
        'y1': mask_sel_y1,
        'x2': mask_sel_x2,
        'y2': mask_sel_y2,
        'target_size': target_size,
        'scale_factor': scale
    }


def calculate_placement_coordinates(context_info):
    """
    Calculate where to place the AI result back in the original image.
    
    Args:
        context_info: Context extraction info from extract_context_with_selection()
        
    Returns:
        dict with placement coordinates
    """
    ctx_x1, ctx_y1, ctx_width, ctx_height = context_info['extract_region']
    
    return {
        'paste_x': ctx_x1,
        'paste_y': ctx_y1, 
        'result_width': ctx_width,
        'result_height': ctx_height
    }


def validate_context_info(context_info, size_policy=None):
    """
    Validate that context_info contains all required fields with valid values.
    
    Args:
        context_info: Context info dict to validate
        
    Returns:
        tuple: (is_valid: bool, error_message: str)
    """
    required_fields = [
        'selection_bounds', 'extract_region', 'target_shape', 'has_selection'
    ]
    
    for field in required_fields:
        if field not in context_info:
            return False, f"Missing required field: {field}"
    
    # Validate selection bounds
    sel_bounds = context_info['selection_bounds']
    if len(sel_bounds) != 4:
        return False, "selection_bounds must have 4 values (x1, y1, x2, y2)"
    
    sel_x1, sel_y1, sel_x2, sel_y2 = sel_bounds
    if sel_x2 <= sel_x1 or sel_y2 <= sel_y1:
        return False, "Invalid selection bounds: x2 <= x1 or y2 <= y1"
    
    # Validate extract region
    extract_region = context_info['extract_region']
    if len(extract_region) != 4:
        return False, "extract_region must have 4 values (x1, y1, width, height)"
    
    ext_x1, ext_y1, ext_width, ext_height = extract_region
    if ext_width <= 0 or ext_height <= 0:
        return False, "Extract region dimensions must be positive"
    
    # Validate that extract region contains selection (for focused mode)
    if context_info.get('mode') == 'focused':
        ext_x2 = ext_x1 + ext_width
        ext_y2 = ext_y1 + ext_height
        
        if not (ext_x1 <= sel_x1 and ext_y1 <= sel_y1 and ext_x2 >= sel_x2 and ext_y2 >= sel_y2):
            return False, "Extract region must contain the selection"
    
    # Validate target shape
    target_shape = context_info['target_shape']
    if not isinstance(target_shape, tuple) or len(target_shape) != 2:
        return False, "target_shape must be a tuple of (width, height)"
    ok, reason = is_shape_allowed(target_shape[0], target_shape[1], size_policy)
    if not ok:
        return False, f"target_shape {target_shape} is invalid: {reason}"
    
    return True, ""


def check_coordinate_properties(img_width, img_height, sel_x1, sel_y1, sel_x2, sel_y2):
    """
    Test that coordinate calculations satisfy expected mathematical properties.

    Returns:
        dict with test results
    """
    context_info = extract_context_with_selection(img_width, img_height, sel_x1, sel_y1, sel_x2, sel_y2)
    target_shape = context_info['target_shape']
    target_size = max(target_shape)
    mask_coords = calculate_mask_coordinates(context_info, target_size)
    placement = calculate_placement_coordinates(context_info)

    # Test validation
    is_valid, error_msg = validate_context_info(context_info)

    results = {
        'validation_passed': is_valid,
        'validation_error': error_msg,
        'context_contains_selection': True,  # Will be checked below
        'mask_coordinates_valid': True,
        'placement_covers_selection': True
    }

    if not is_valid:
        return results

    # Check that extract region contains selection
    ext_x1, ext_y1, ext_width, ext_height = context_info['extract_region']
    ext_x2, ext_y2 = ext_x1 + ext_width, ext_y1 + ext_height

    results['context_contains_selection'] = (
        ext_x1 <= sel_x1 and ext_y1 <= sel_y1 and
        ext_x2 >= sel_x2 and ext_y2 >= sel_y2
    )

    # Check mask coordinates are within bounds
    if mask_coords['mask_type'] == 'rectangle':
        results['mask_coordinates_valid'] = (
            0 <= mask_coords['x1'] < target_size and
            0 <= mask_coords['y1'] < target_size and
            0 < mask_coords['x2'] <= target_size and
            0 < mask_coords['y2'] <= target_size and
            mask_coords['x1'] < mask_coords['x2'] and
            mask_coords['y1'] < mask_coords['y2']
        )
    
    # Check that placement would cover selection
    paste_x, paste_y = placement['paste_x'], placement['paste_y']
    result_width, result_height = placement['result_width'], placement['result_height']
    
    results['placement_covers_selection'] = (
        paste_x <= sel_x1 and paste_y <= sel_y1 and
        paste_x + result_width >= sel_x2 and paste_y + result_height >= sel_y2
    )
    
    return results