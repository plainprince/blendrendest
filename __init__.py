import bpy
import time
import json
import os

bl_info = {
    "name": "Blendrendest",
    "author": "plainprince",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "Image Editor > Properties Region",
    "description": "Estimate render time for animations and single frames.",
    "category": "Render"
}

# --- Time-Based Activity Messages ---
def _get_activities_path():
    """Get the path to the time_activities.json file."""
    return os.path.join(os.path.dirname(__file__), "time_activities.json")

def _load_time_activities():
    """Load time activities from JSON file."""
    json_path = _get_activities_path()
    
    # Default fallback activities
    default_activities = [
        (5, "Instant Render!"),
        (30, "Perfect time to stretch"),
        (60, "Grab a glass of water"),
        (300, "Do some quick desk exercises"),
        (600, "Go for a short walk"),
        (1800, "Watch an episode of your favorite show"),
        (3600, "Take a power nap"),
        (7200, "Go out for lunch"),
        (86400, "This might take a while - consider optimizing your scene"),
    ]
    
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            activities = []
            for item in data.get("activities", []):
                threshold = item.get("threshold", 0)
                suggestion = item.get("suggestion", "")
                if threshold and suggestion:
                    activities.append((threshold, suggestion))
            # Sort by threshold to ensure proper order
            activities.sort(key=lambda x: x[0])
            return activities if activities else default_activities
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return default_activities

# Load activities at module import time
TIME_ACTIVITIES = _load_time_activities()

def get_activity_for_time(seconds):
    """Get the appropriate activity suggestion based on estimated time."""
    if seconds <= 0:
        return "Instant render!"
    activity = TIME_ACTIVITIES[0][1]
    for threshold, suggestion in TIME_ACTIVITIES:
        if seconds >= threshold:
            activity = suggestion
        else:
            break
    return activity

# --- Pre-Render Estimation Functions ---
def get_scene_complexity(scene):
    """Gather scene complexity metrics for estimation."""
    # Count objects by type
    mesh_count = sum(1 for obj in bpy.data.objects if obj.type == 'MESH' and obj.visible_get())
    light_count = sum(1 for obj in bpy.data.objects if obj.type == 'LIGHT' and obj.visible_get())
    volume_count = sum(1 for obj in bpy.data.objects if obj.type == 'VOLUME' and obj.visible_get())
    
    # Count total vertices in visible meshes
    total_verts = 0
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and obj.visible_get() and obj.data:
            total_verts += len(obj.data.vertices)
    
    # Resolution
    render = scene.render
    res_x = render.resolution_x * (render.resolution_percentage / 100)
    res_y = render.resolution_y * (render.resolution_percentage / 100)
    pixel_count = res_x * res_y
    
    return {
        'mesh_count': mesh_count,
        'light_count': light_count,
        'volume_count': volume_count,
        'total_verts': total_verts,
        'res_x': res_x,
        'res_y': res_y,
        'pixel_count': pixel_count,
    }

def get_addon_preferences():
    """Get addon preferences."""
    return bpy.context.preferences.addons[__name__].preferences

def estimate_cycles_render_time(scene, complexity):
    """Estimate render time for Cycles engine."""
    cycles = scene.cycles
    prefs = get_addon_preferences()
    
    # Base samples
    samples = cycles.samples
    
    # Adaptive sampling adjustment
    if cycles.use_adaptive_sampling:
        noise_threshold = cycles.adaptive_threshold
        # Lower threshold = more samples needed on average
        # Typical scenes converge at ~60-80% of max samples with adaptive
        adaptive_factor = 0.5 + (noise_threshold * 5)  # Range ~0.5 to ~1.0
        effective_samples = samples * adaptive_factor
    else:
        effective_samples = samples
    
    # Fast GI approximation reduces time
    fast_gi_factor = 0.7 if cycles.use_fast_gi else 1.0
    
    # Denoiser adds small overhead but reduces needed samples
    denoiser_factor = 0.9 if cycles.use_denoising else 1.0
    
    # Resolution factor (normalized to 1080p as baseline)
    baseline_pixels = 1920 * 1080
    resolution_factor = complexity['pixel_count'] / baseline_pixels
    
    # Object complexity factor
    # More objects and vertices = more ray intersections
    object_factor = 1.0 + (complexity['mesh_count'] * 0.01) + (complexity['total_verts'] / 1000000)
    
    # Light complexity factor
    # More lights = more shadow rays
    light_factor = 1.0 + (complexity['light_count'] * 0.05)
    
    # Volume factor (volumes are expensive)
    volume_factor = 1.0 + (complexity['volume_count'] * 0.3)
    
    # Base calibration constant (seconds per 100 samples at 1080p baseline)
    # This is a rough estimate - actual time varies wildly by hardware
    calibration = prefs.calibration_factor
    
    # Calculate estimated time
    estimated_time = (
        (effective_samples / 100) *
        resolution_factor *
        object_factor *
        light_factor *
        volume_factor *
        fast_gi_factor *
        denoiser_factor *
        calibration
    )
    
    return max(1, estimated_time)  # Minimum 1 second

def estimate_eevee_render_time(scene, complexity):
    """Estimate render time for EEVEE engine."""
    eevee = scene.eevee
    prefs = get_addon_preferences()
    
    # EEVEE samples
    samples = eevee.taa_render_samples
    
    # Resolution factor
    baseline_pixels = 1920 * 1080
    resolution_factor = complexity['pixel_count'] / baseline_pixels
    
    # Object complexity (EEVEE is less affected by geometry)
    object_factor = 1.0 + (complexity['mesh_count'] * 0.005)
    
    # Light complexity
    light_factor = 1.0 + (complexity['light_count'] * 0.02)
    
    # EEVEE-specific features
    volumetrics_factor = 1.3 if eevee.use_volumetric_lights else 1.0
    ssr_factor = 1.15 if eevee.use_ssr else 1.0
    ao_factor = 1.05 if eevee.use_gtao else 1.0
    
    # Base calibration for EEVEE (much faster than Cycles)
    calibration = prefs.calibration_factor * 0.1
    
    estimated_time = (
        (samples / 64) *
        resolution_factor *
        object_factor *
        light_factor *
        volumetrics_factor *
        ssr_factor *
        ao_factor *
        calibration
    )
    
    return max(0.5, estimated_time)  # Minimum 0.5 seconds

def estimate_single_frame_time(scene):
    """Estimate render time for a single frame."""
    complexity = get_scene_complexity(scene)
    engine = scene.render.engine
    
    if engine == 'CYCLES':
        return estimate_cycles_render_time(scene, complexity)
    elif engine == 'BLENDER_EEVEE_NEXT' or engine == 'BLENDER_EEVEE':
        return estimate_eevee_render_time(scene, complexity)
    else:
        # Fallback for other engines
        return 5.0  # Default 5 seconds

def estimate_animation_time(scene):
    """Estimate total render time for animation."""
    frame_time = estimate_single_frame_time(scene)
    total_frames = scene.frame_end - scene.frame_start + 1
    return frame_time * total_frames

def get_estimation_breakdown(scene):
    """Get detailed breakdown of estimation factors (for debug mode)."""
    complexity = get_scene_complexity(scene)
    engine = scene.render.engine
    
    breakdown = {
        'engine': engine,
        'resolution': f"{int(complexity['res_x'])}x{int(complexity['res_y'])}",
        'pixels': f"{complexity['pixel_count'] / 1000000:.2f}M",
        'meshes': complexity['mesh_count'],
        'lights': complexity['light_count'],
        'volumes': complexity['volume_count'],
        'vertices': f"{complexity['total_verts'] / 1000:.1f}K",
    }
    
    if engine == 'CYCLES':
        cycles = scene.cycles
        breakdown['samples'] = cycles.samples
        breakdown['adaptive'] = cycles.use_adaptive_sampling
        if cycles.use_adaptive_sampling:
            breakdown['noise_threshold'] = cycles.adaptive_threshold
        breakdown['fast_gi'] = cycles.use_fast_gi
        breakdown['denoiser'] = cycles.use_denoising
    elif engine in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE'):
        eevee = scene.eevee
        breakdown['samples'] = eevee.taa_render_samples
        breakdown['volumetrics'] = eevee.use_volumetric_lights
        breakdown['ssr'] = eevee.use_ssr
        breakdown['ao'] = eevee.use_gtao
    
    return breakdown

# --- Time Formatting Functions ---
def format_time_HHMMSS(sec):
    sec = int(sec)
    days = sec // 86400
    sec %= 86400
    hours = sec // 3600
    sec %= 3600
    minutes = sec // 60
    seconds = sec % 60
    if days > 0:
        return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"
    else:
        return f"{hours:02}:{minutes:02}:{seconds:02}"

def format_time_human(sec):
    sec = int(sec)
    days = sec // 86400
    sec %= 86400
    hours = sec // 3600
    sec %= 3600
    minutes = sec // 60
    seconds = sec % 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds or not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return ", ".join(parts)

# --- Global State Variables ---
_frame_start = {}
_total_start = None
_first_rendered_frame = None
_last_frame_time = 0.0
_last_eta_human = "AWAITING RENDER"
_last_eta_HHMMSS = "AWAITING RENDER"
_progress = 0.0
_is_rendering = False
_total_time = None
_avg_time = None
BAR_LENGTH = 25

# Single frame render state
_single_frame_render = False
_single_frame_start = None
_detected_animation = False  # True once we confirm this is an animation (multiple frames)

# Auto-calibration state
_pre_render_estimate = None  # Stores the estimated time before render starts (for auto-calibration)

# --- Progress Bar Utility ---
def progress_bar(cur, total):
    pct = cur / total if total else 0
    filled = int(pct * BAR_LENGTH)
    bar = "█" * filled + "░" * (BAR_LENGTH - filled)
    return f"|{bar}| {pct*100:5.1f}%"

# --- Header Drawing ---
def draw_header(self, context):
    scene = context.scene
    layout = self.layout
    row = layout.row(align=True)
    row.alignment = 'RIGHT'

    if _is_rendering:
        if _single_frame_render:
            # Single frame render - show countdown (remaining time)
            icon = "RENDER_STILL"
            single_est = estimate_single_frame_time(scene)
            if _single_frame_start:
                elapsed = time.time() - _single_frame_start
                remaining = max(0, single_est - elapsed)
                status = f"Remaining: {format_time_HHMMSS(remaining)}"
            else:
                status = f"Est: {format_time_human(single_est)}"
            pb_text = ""
            alert_flag = False
        elif _first_rendered_frame is not None:
            # Animation render
            current_frame_index = scene.frame_current - _first_rendered_frame + 1
            total_frames = scene.frame_end - _first_rendered_frame + 1
            pb_text = progress_bar(current_frame_index, total_frames)
            if current_frame_index <= 1:
                # First frame - show formula estimate
                anim_time = estimate_animation_time(scene)
                eta_text = format_time_human(anim_time)
                icon = "PREVIEW_RANGE"
                alert_flag = False
            else:
                eta_text = _last_eta_human if _last_eta_human else "Calculating ETA"
                icon = "RENDER_ANIMATION"
                alert_flag = eta_text in {"Calculating ETA", "RENDER STOPPED"}
            status = f"ETA: {eta_text}"
        else:
            status = "Starting render..."
            pb_text = ""
            icon = "TIME"
            alert_flag = True
    else:
        status = _last_eta_human
        pb_text = ""
        icon = "FILE_REFRESH"
        if _last_eta_human.startswith("RENDER COMPLETE"):
            icon = "CHECKMARK"
        elif _last_eta_human == "RENDER STOPPED":
            icon = "CANCEL"
        alert_flag = False

    row.label(text="", icon=icon)
    if pb_text:
        row.label(text=pb_text)
    row.alert = alert_flag
    row.label(text=status)

# --- State Reset and Handler Functions ---
def reset_render_state(persistent_progress, is_single_frame=False, scene=None):
    global _frame_start, _total_start, _first_rendered_frame
    global _last_eta_human, _last_eta_HHMMSS, _progress, _is_rendering, _total_time, _avg_time
    global _single_frame_render, _single_frame_start, _last_frame_time, _detected_animation
    global _pre_render_estimate
    _frame_start.clear()
    _total_start = None
    _last_frame_time = 0.0
    _first_rendered_frame = None
    _progress = 0.0
    _detected_animation = False
    _pre_render_estimate = None
    if not persistent_progress:
        _total_time = None
        _avg_time = None
    _last_eta_human = "Calculating ETA"
    _last_eta_HHMMSS = "Calculating ETA"
    _is_rendering = True
    # Set single frame mode
    _single_frame_render = is_single_frame
    if is_single_frame:
        _single_frame_start = time.time()
    else:
        _single_frame_start = None
        # Store pre-render estimate for auto-calibration (animation only)
        if scene is not None:
            prefs = get_addon_preferences()
            if prefs.auto_calibrate:
                _pre_render_estimate = estimate_animation_time(scene)

def render_init_handler(scene):
    global _is_rendering, _total_start, _single_frame_render, _single_frame_start
    global _first_rendered_frame, _last_eta_human, _last_eta_HHMMSS, _progress
    global _total_time, _avg_time, _last_frame_time, _detected_animation, _pre_render_estimate
    
    # If state wasn't set by our operators (user used F12 or Blender menu),
    # we need to detect and initialize properly
    if not _is_rendering:
        # Fresh render not started by our operators - reset everything
        _is_rendering = True
        _total_start = time.time()
        _first_rendered_frame = None
        _last_frame_time = 0.0
        _progress = 0.0
        _total_time = None
        _avg_time = None
        _last_eta_human = "Calculating ETA"
        _last_eta_HHMMSS = "Calculating ETA"
        _detected_animation = False
        _pre_render_estimate = None  # Will be set in render_pre if animation detected
        # Assume single frame mode for native renders (F12)
        # Will switch to animation mode if we detect multiple frames in render_pre
        _single_frame_render = True
        _single_frame_start = time.time()
    elif _total_start is None:
        _total_start = time.time()

def render_pre_handler(scene):
    global _total_start, _first_rendered_frame, _single_frame_render, _detected_animation, _pre_render_estimate
    cur = scene.frame_current
    _frame_start[cur] = time.time()
    if _first_rendered_frame is None:
        _first_rendered_frame = cur
        _total_start = time.time()
    else:
        # We're rendering a second frame - this is definitely an animation
        if not _detected_animation:
            _detected_animation = True
            _single_frame_render = False
            # Capture pre-render estimate for auto-calibration if not already set
            # (handles native Blender animation renders not started by our operator)
            if _pre_render_estimate is None:
                prefs = get_addon_preferences()
                if prefs.auto_calibrate:
                    _pre_render_estimate = estimate_animation_time(scene)
    if _total_start is None:
        _total_start = time.time()

def render_post_handler(scene):
    global _last_frame_time, _last_eta_human, _last_eta_HHMMSS, _progress
    cur = scene.frame_current
    if _first_rendered_frame is None:
        return
    frame_index = cur - _first_rendered_frame + 1
    total_frames = scene.frame_end - _first_rendered_frame + 1
    t = time.time() - _frame_start.get(cur, time.time())
    _last_frame_time = t
    # After first frame completes, we have real timing data - use it!
    remaining = scene.frame_end - cur
    if remaining > 0:
        predicted = _last_frame_time * remaining
        _last_eta_human = format_time_human(predicted)
        _last_eta_HHMMSS = format_time_HHMMSS(predicted)
    else:
        _last_eta_human = "Finishing..."
        _last_eta_HHMMSS = "Finishing..."
    _progress = frame_index / total_frames
    prefs = get_addon_preferences()
    if prefs.show_debug:
        dbg_pb = progress_bar(frame_index, total_frames)
        print(f"[Blendrendest] Frame {cur}: {dbg_pb} | Time: {t:.2f}s | ETA: {_last_eta_human} [{_last_eta_HHMMSS}]")

def render_complete_handler(scene):
    global _last_eta_human, _last_eta_HHMMSS, _is_rendering, _total_time, _avg_time
    global _single_frame_render, _single_frame_start, _detected_animation, _pre_render_estimate
    
    if _single_frame_render:
        # Single frame render completed
        total = time.time() - _single_frame_start if _single_frame_start else 0
        _total_time = total
        _avg_time = total
        _last_eta_human = "RENDER COMPLETE"
        _last_eta_HHMMSS = f"RENDER COMPLETE | Time: {format_time_HHMMSS(total)}"
        prefs = get_addon_preferences()
        if prefs.show_debug:
            print(f"[Blendrendest] Single frame render complete. Time: {total:.2f}s")
        _single_frame_render = False
        _single_frame_start = None
        _detected_animation = False
    else:
        # Animation render completed
        total_frames = scene.frame_end - _first_rendered_frame + 1 if _first_rendered_frame is not None else 0
        total = time.time() - _total_start if _total_start else 0
        avg = total / total_frames if total_frames else 0
        _total_time = total
        _avg_time = avg
        _last_eta_human = "RENDER COMPLETE"
        _last_eta_HHMMSS = f"RENDER COMPLETE | Total: {format_time_HHMMSS(total)}, Avg: {format_time_HHMMSS(avg)}"
        prefs = get_addon_preferences()
        if prefs.show_debug:
            print(f"[Blendrendest] Render complete. Total: {total:.2f}s, Avg: {avg:.2f}s/frame")
        
        # Auto-calibration for animation renders
        if prefs.auto_calibrate and _pre_render_estimate is not None and _pre_render_estimate > 0 and total > 0:
            # Calculate correction factor: how much we need to adjust
            # If estimated was 100s and actual was 200s, correction = 2.0
            correction = total / _pre_render_estimate
            
            # Calculate new calibration factor
            new_cal_factor = prefs.calibration_factor * correction
            
            # Average between old and new for smoother adjustment
            averaged_factor = (prefs.calibration_factor + new_cal_factor) / 2.0
            
            # Clamp to valid range
            averaged_factor = max(0.1, min(50.0, averaged_factor))
            
            if prefs.show_debug:
                print(f"[Blendrendest] Auto-calibrate: Est={_pre_render_estimate:.1f}s, Actual={total:.1f}s, "
                      f"Correction={correction:.3f}, OldCal={prefs.calibration_factor:.3f}, NewCal={averaged_factor:.3f}")
            
            # Update the preference
            prefs.calibration_factor = averaged_factor
        
        _detected_animation = False
        _pre_render_estimate = None
    
    _is_rendering = False

def render_cancel_handler(scene):
    global _last_eta_human, _last_eta_HHMMSS, _is_rendering
    global _single_frame_render, _single_frame_start, _detected_animation, _pre_render_estimate
    _last_eta_human = "RENDER STOPPED"
    _last_eta_HHMMSS = "RENDER STOPPED"
    _is_rendering = False
    _single_frame_render = False
    _single_frame_start = None
    _detected_animation = False
    _pre_render_estimate = None  # Don't calibrate on cancelled renders
    prefs = get_addon_preferences()
    if prefs.show_debug:
        print("[Blendrendest] Render cancelled.")

# --- Operator Definition ---
class RTE_OT_RenderAnimationWithETA(bpy.types.Operator):
    bl_idname = "rte.render_animation_with_eta"
    bl_label = "Render Animation with ETA"
    bl_description = "Render animation and estimate render time"

    def execute(self, context):
        global _is_rendering
        if not _is_rendering:
            prefs = get_addon_preferences()
            register_render_handlers()
            reset_render_state(prefs.persistent_progress, is_single_frame=False, scene=context.scene)
            bpy.ops.render.render('INVOKE_DEFAULT', animation=True)
            return {'FINISHED'}
        else:
            return {'CANCELLED'}

# --- Operators ---
class RTE_OT_RenderSingleWithETA(bpy.types.Operator):
    bl_idname = "rte.render_single_with_eta"
    bl_label = "Render Single Frame"
    bl_description = "Render current frame with time estimation"

    def execute(self, context):
        global _is_rendering
        if not _is_rendering:
            prefs = get_addon_preferences()
            register_render_handlers()
            reset_render_state(prefs.persistent_progress, is_single_frame=True)
            bpy.ops.render.render('INVOKE_DEFAULT', animation=False)
            return {'FINISHED'}
        return {'CANCELLED'}

# --- Panel Drawing Function ---
def draw_main_panel(layout, context):
    """Shared panel drawing logic for all panel locations."""
    scene = context.scene
    prefs = get_addon_preferences()

    # --- Pre-Render Estimation Box ---
    if not _is_rendering:
        est_box = layout.box()
        est_box.label(text="Pre-Render Estimate", icon='PREVIEW_RANGE')
        
        # Single frame estimate
        single_time = estimate_single_frame_time(scene)
        est_box.label(text=f"Single Frame: {format_time_human(single_time)}")
        
        # Animation estimate
        total_frames = scene.frame_end - scene.frame_start + 1
        anim_time = estimate_animation_time(scene)
        est_box.label(text=f"Animation ({total_frames} frames): {format_time_human(anim_time)}")
        
        # Activity suggestions for both single frame and animation
        activity_box = est_box.box()
        activity_box.label(text="While rendering you could:", icon='TIME')
        
        # Single frame activity
        single_activity = get_activity_for_time(single_time)
        row = activity_box.row()
        row.label(text="", icon='RENDER_STILL')
        row.label(text=single_activity)
        
        # Animation activity (only show if different from single frame)
        anim_activity = get_activity_for_time(anim_time)
        row = activity_box.row()
        row.label(text="", icon='RENDER_ANIMATION')
        row.label(text=anim_activity)
        
        # Estimation breakdown (if enabled in preferences)
        if prefs.show_estimation_breakdown:
            breakdown = get_estimation_breakdown(scene)
            breakdown_box = est_box.box()
            breakdown_box.label(text="Estimation Factors:", icon='VIEWZOOM')
            col = breakdown_box.column(align=True)
            col.scale_y = 0.8
            for key, value in breakdown.items():
                col.label(text=f"{key}: {value}")

    # --- Render Buttons ---
    row = layout.row(align=True)
    row.scale_y = 1.5
    row.enabled = not _is_rendering
    
    if _is_rendering:
        row.operator(RTE_OT_RenderAnimationWithETA.bl_idname, text="RENDERING...", icon='RENDER_ANIMATION')
    else:
        row.operator(RTE_OT_RenderAnimationWithETA.bl_idname, text="Animation", icon='RENDER_ANIMATION')
        row.operator(RTE_OT_RenderSingleWithETA.bl_idname, text="Frame", icon='RENDER_STILL')

    # --- Info Box (during/after render) ---
    info_box = layout.box()
    info_box.label(text="Render Status", icon='INFO')

    status_icon = "FILE_REFRESH"
    if _is_rendering:
        if _single_frame_render:
            # Show countdown for single frame render
            single_time = estimate_single_frame_time(scene)
            info_box.label(text=f"Estimated: {format_time_human(single_time)}", icon='PREVIEW_RANGE')
            info_box.label(text="STATUS: RENDERING FRAME", icon=status_icon)
            if _single_frame_start:
                elapsed = time.time() - _single_frame_start
                remaining = max(0, single_time - elapsed)
                info_box.label(text=f"Remaining: {format_time_HHMMSS(remaining)}")
                info_box.label(text=f"Elapsed: {format_time_HHMMSS(elapsed)}")
                # Activity suggestion based on remaining time
                info_box.label(text=get_activity_for_time(remaining), icon='TIME')
        elif _first_rendered_frame is not None:
            current_frame_index = scene.frame_current - _first_rendered_frame + 1
            total_frames = scene.frame_end - _first_rendered_frame + 1
            status_display_text = f"RENDERING FRAME {current_frame_index} OF {total_frames}"
            info_box.label(text=f"STATUS: {status_display_text}", icon=status_icon)
            
            if current_frame_index <= 1:
                # First frame - show formula estimate like single frame
                anim_time = estimate_animation_time(scene)
                info_box.label(text=f"Estimated: {format_time_human(anim_time)}", icon='PREVIEW_RANGE')
                if _total_start:
                    elapsed = time.time() - _total_start
                    info_box.label(text=f"Elapsed: {format_time_HHMMSS(elapsed)}")
                info_box.label(text=get_activity_for_time(anim_time), icon='TIME')
            else:
                info_box.label(text=f"ETA: [{_last_eta_HHMMSS}]")
                # Activity suggestion during render
                if _last_frame_time > 0:
                    remaining_frames = scene.frame_end - scene.frame_current
                    remaining_time = _last_frame_time * remaining_frames
                    info_box.label(text=get_activity_for_time(remaining_time), icon='TIME')
        else:
            info_box.label(text="STATUS: Starting...", icon=status_icon)
    else:
        # Not currently rendering - show last status
        if _last_eta_human.startswith("RENDER COMPLETE"):
            status_icon = "CHECKMARK"
            status_text = "STATUS: RENDER COMPLETE"
        elif _last_eta_human == "RENDER STOPPED":
            status_icon = "CANCEL"
            status_text = "STATUS: RENDER STOPPED"
        elif _last_eta_human == "AWAITING RENDER":
            status_icon = "FILE_REFRESH"
            status_text = "STATUS: Ready to render"
        else:
            status_icon = "FILE_REFRESH"
            status_text = f"STATUS: {_last_eta_human}"
        info_box.label(text=status_text, icon=status_icon)

    if _total_time is not None:
        info_box.label(text=f"Total Time: {format_time_HHMMSS(_total_time)}", icon='SORTTIME')
    if _avg_time is not None:
        info_box.label(text=f"Avg Time/Frame: {format_time_HHMMSS(_avg_time)}", icon='CLOCK')

# --- Panel Definitions ---
class RTE_PT_Panel(bpy.types.Panel):
    bl_label = "Blendrendest"
    bl_idname = "RTE_PT_panel"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'Blendrendest'

    def draw(self, context):
        draw_main_panel(self.layout, context)


class RTE_PT_Panel_3DView(bpy.types.Panel):
    bl_label = "Blendrendest"
    bl_idname = "RTE_PT_panel_3dview"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Blendrendest'

    def draw(self, context):
        draw_main_panel(self.layout, context)

class RTE_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    
    calibration_factor: bpy.props.FloatProperty(
        name="Calibration Factor",
        description="Adjust estimation accuracy for your hardware (higher = longer estimates)",
        default=2.0,
        min=0.1,
        max=50.0,
        soft_min=0.5,
        soft_max=10.0
    )
    auto_calibrate: bpy.props.BoolProperty(
        name="Auto-Calibrate",
        description="Automatically adjust calibration factor after completed animation renders based on actual vs estimated time",
        default=True
    )
    show_debug: bpy.props.BoolProperty(
        name="Console Debug",
        description="Print render progress info to the console",
        default=False
    )
    persistent_progress: bpy.props.BoolProperty(
        name="Persistent Progress",
        description="Continue progress from where it was left off after cancelling render",
        default=False
    )
    show_estimation_breakdown: bpy.props.BoolProperty(
        name="Show Estimation Breakdown",
        description="Display detailed breakdown of estimation factors in the panel",
        default=False
    )
    
    def draw(self, context):
        layout = self.layout
        
        layout.label(text="Estimation Settings", icon='PREVIEW_RANGE')
        box = layout.box()
        box.prop(self, "calibration_factor")
        box.prop(self, "auto_calibrate")
        box.prop(self, "show_estimation_breakdown")
        
        layout.label(text="Render Settings", icon='RENDER_ANIMATION')
        box = layout.box()
        box.prop(self, "persistent_progress")
        box.prop(self, "show_debug")

classes = (
    RTE_AddonPreferences,
    RTE_OT_RenderAnimationWithETA,
    RTE_OT_RenderSingleWithETA,
    RTE_PT_Panel,
    RTE_PT_Panel_3DView,
)

# --- Handler Registration Functions ---
def register_render_handlers():
    # Avoid duplicate handlers
    if render_init_handler not in bpy.app.handlers.render_init:
        bpy.app.handlers.render_init.append(render_init_handler)
        bpy.app.handlers.render_pre.append(render_pre_handler)
        bpy.app.handlers.render_post.append(render_post_handler)
        bpy.app.handlers.render_complete.append(render_complete_handler)
        bpy.app.handlers.render_cancel.append(render_cancel_handler)

def unregister_render_handlers():
    if render_init_handler in bpy.app.handlers.render_init:
        bpy.app.handlers.render_init.remove(render_init_handler)
        bpy.app.handlers.render_pre.remove(render_pre_handler)
        bpy.app.handlers.render_post.remove(render_post_handler)
        bpy.app.handlers.render_complete.remove(render_complete_handler)
        bpy.app.handlers.render_cancel.remove(render_cancel_handler)

# --- Registration ---
def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.IMAGE_HT_header.append(draw_header)
    register_render_handlers()

def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
    bpy.types.IMAGE_HT_header.remove(draw_header)
    unregister_render_handlers()

if __name__ == "__main__":
    register()