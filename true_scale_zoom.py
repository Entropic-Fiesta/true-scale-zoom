bl_info = {
    "name": "True Scale Zoom",
    "author": "Claude for J",
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar (N) > View tab  |  View menu",
    "description": "Zoom the viewport so 1 m in the scene is 1 m on your physical screen "
                   "(tuned for MacBook Air M1 2020, with auto-detect and ruler calibration)",
    "category": "3D View",
}

import re
import subprocess
import sys

import bpy
import blf
import gpu
from gpu_extras.batch import batch_for_shader
from bpy.props import BoolProperty, EnumProperty, FloatProperty
from bpy.types import AddonPreferences, Operator, Panel

# ---------------------------------------------------------------------------
# Display hardware: MacBook Air (M1, 2020)
#   13.3" Retina IPS, 2560 x 1600 physical pixels, 227 ppi
#   -> physical active-area width = 2560 / 227 in = 286.4 mm
#
# macOS renders "scaled" modes into a 2x backing buffer and resamples to the
# panel, and Blender's viewport works in backing-buffer pixels. So the number
# of Blender pixels per physical mm depends on the "Looks like" setting in
# System Settings > Displays:
#   Looks like 1440 x 900 (default) -> 2880 px across 286.4 mm
# ---------------------------------------------------------------------------
PANEL_WIDTH_MM = 2560.0 / 227.0 * 25.4

LOOKS_LIKE_ITEMS = [
    ('AUTO', "Auto-detect (macOS)", "Read the current 'Looks like' resolution from macOS"),
    ('1024', "Looks like 1024 × 640", "Larger text"),
    ('1280', "Looks like 1280 × 800", "Native 2x, no resampling"),
    ('1440', "Looks like 1440 × 900", "macOS default for the M1 Air"),
    ('1680', "Looks like 1680 × 1050", "More space"),
    ('CUSTOM', "Custom pixels per mm", "Enter your own value (e.g. for an external monitor)"),
]

_detected = {"width": None, "info": "Not checked yet"}
_draw_handle = None


def prefs():
    return bpy.context.preferences.addons[__name__].preferences


def detect_backing_width(force=False):
    """Return the 2x backing-buffer width of the built-in display, or None."""
    if _detected["width"] is not None and not force:
        return _detected["width"]
    if sys.platform != "darwin":
        _detected["info"] = "Not on macOS - pick a preset"
        return None
    try:
        out = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception as exc:  # noqa: BLE001
        _detected["info"] = f"Detection failed: {exc}"
        return None

    # Group lines into per-display blocks (display names sit at 8-space indent)
    blocks, current = [], None
    for line in out.splitlines():
        if re.match(r"^ {8}\S.*:$", line):
            current = [line]
            blocks.append(current)
        elif current is not None:
            current.append(line)
    blocks = ["\n".join(b) for b in blocks]

    def pick(pred):
        return next((b for b in blocks if pred(b)), None)

    block = (pick(lambda b: "Internal" in b or "Built-in" in b or "Color LCD" in b)
             or pick(lambda b: "Main Display: Yes" in b)
             or (blocks[0] if blocks else None))
    if not block:
        _detected["info"] = "No display info found - pick a preset"
        return None

    m = re.search(r"Looks like:\s*(\d+)\s*x\s*(\d+)", block)
    if not m:
        m = re.search(r"Resolution:\s*(\d+)\s*x\s*(\d+)", block)
        if not m:
            _detected["info"] = "Couldn't parse resolution - pick a preset"
            return None
        width = int(m.group(1))
        _detected["info"] = f"Detected {m.group(1)} × {m.group(2)}"
    else:
        width = int(m.group(1)) * 2  # "Looks like" is in points; backing buffer is 2x
        _detected["info"] = f"Detected: looks like {m.group(1)} × {m.group(2)}"
    _detected["width"] = width
    return width


def px_per_mm():
    p = prefs()
    if p.display_mode == 'CUSTOM':
        base = p.custom_px_per_mm
    else:
        if p.display_mode == 'AUTO':
            w = detect_backing_width() or 2880  # fall back to macOS default
        else:
            w = int(p.display_mode) * 2
        base = w / PANEL_WIDTH_MM
    return base * p.calibration


def unit_scale(scene):
    s = scene.unit_settings.scale_length
    return s if s > 0 else 1.0


def main_view(context):
    """Return (region, rv3d, space) for the main 3D viewport region of the area."""
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        return None, None, None
    space = area.spaces.active
    if context.region and context.region.type == 'WINDOW' and context.region_data:
        return context.region, context.region_data, space
    windows = [r for r in area.regions if r.type == 'WINDOW']
    region = max(windows, key=lambda r: r.width * r.height)
    return region, space.region_3d, space


# Blender ortho view: visible width (along the region's longer side)
# = 2 * view_distance * 36mm / lens   (DEFAULT_SENSOR_WIDTH, zoom factor 2)
def dist_for_scale(region, space, scene, percent):
    px_per_bu = px_per_mm() * 1000.0 * unit_scale(scene) * percent / 100.0
    visible_bu = max(region.width, region.height) / px_per_bu
    return visible_bu * space.lens / 72.0


def current_percent(region, rv3d, space, scene):
    if rv3d is None or rv3d.view_perspective != 'ORTHO' or rv3d.view_distance <= 0:
        return None
    visible_bu = rv3d.view_distance * 72.0 / space.lens
    px_per_bu = max(region.width, region.height) / visible_bu
    return px_per_bu / (px_per_mm() * 1000.0 * unit_scale(scene)) * 100.0


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------
class VIEW3D_OT_true_scale_zoom(Operator):
    bl_idname = "view3d.true_scale_zoom"
    bl_label = "Zoom to True Scale"
    bl_description = "Zoom so scene objects appear at their real physical size on this screen"
    bl_options = {'REGISTER', 'UNDO'}

    percent: FloatProperty(name="Scale %", default=100.0, min=1.0, max=10000.0)
    center: EnumProperty(
        name="Center on",
        items=[('KEEP', "Keep view center", ""),
               ('SELECTION', "Selection", ""),
               ('CURSOR', "3D Cursor", "")],
        default='KEEP')

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context):
        region, rv3d, space = main_view(context)
        if rv3d is None:
            self.report({'ERROR'}, "No 3D viewport found")
            return {'CANCELLED'}

        if rv3d.view_perspective != 'ORTHO':
            rv3d.view_perspective = 'ORTHO'  # true scale needs orthographic

        if self.center == 'CURSOR':
            rv3d.view_location = context.scene.cursor.location.copy()
        elif self.center == 'SELECTION':
            objs = context.selected_objects
            if objs:
                from mathutils import Vector
                pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
                rv3d.view_location = sum(pts, Vector()) / len(pts)

        rv3d.view_distance = dist_for_scale(region, space, context.scene, self.percent)
        context.area.tag_redraw()
        self.report({'INFO'}, f"Viewport at {self.percent:g}% true scale")
        return {'FINISHED'}


class VIEW3D_OT_true_scale_detect(Operator):
    bl_idname = "view3d.true_scale_detect"
    bl_label = "Re-detect Display"
    bl_description = "Read the current macOS display scaling again"

    def execute(self, context):
        detect_backing_width(force=True)
        self.report({'INFO'}, _detected["info"])
        return {'FINISHED'}


class VIEW3D_OT_true_scale_calibrate(Operator):
    bl_idname = "view3d.true_scale_calibrate"
    bl_label = "Apply Measurement"
    bl_description = "Correct the scale using what you measured on the 100 mm ruler"

    def execute(self, context):
        p = prefs()
        if p.measured_mm <= 0:
            self.report({'ERROR'}, "Enter the measured length first")
            return {'CANCELLED'}
        p.calibration *= 100.0 / p.measured_mm
        self.report({'INFO'}, f"Calibration factor now {p.calibration:.4f}")
        p.measured_mm = 100.0
        _redraw_all()
        return {'FINISHED'}


class VIEW3D_OT_true_scale_nudge(Operator):
    bl_idname = "view3d.true_scale_nudge"
    bl_label = "Nudge Calibration"
    bl_description = "Make everything slightly bigger (+) or smaller (-); re-applies the zoom if at true scale"

    mult: FloatProperty(default=1.0)

    def execute(self, context):
        region, rv3d, space = main_view(context)
        before = current_percent(region, rv3d, space, context.scene) if rv3d else None
        p = prefs()
        p.calibration = p.calibration * self.mult
        # Keep the same % after the change, so the view updates live
        if before is not None:
            rv3d.view_distance = dist_for_scale(region, space, context.scene, before)
        _redraw_all()
        return {'FINISHED'}


class VIEW3D_OT_true_scale_reset_cal(Operator):
    bl_idname = "view3d.true_scale_reset_calibration"
    bl_label = "Reset Calibration"

    def execute(self, context):
        prefs().calibration = 1.0
        _redraw_all()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# On-screen 100 mm ruler for checking against a real ruler
# ---------------------------------------------------------------------------
def _shader():
    return gpu.shader.from_builtin('UNIFORM_COLOR' if bpy.app.version >= (4, 0, 0)
                                   else '2D_UNIFORM_COLOR')


def _draw_ruler():
    region = bpy.context.region
    ppm = px_per_mm()
    length = 100.0 * ppm
    x0, y0 = 60.0, 60.0
    if length > region.width - 80:
        return
    coords = [(x0, y0), (x0 + length, y0)]
    for mm in range(0, 101):
        h = 18 if mm % 10 == 0 else (11 if mm % 5 == 0 else 6)
        x = x0 + mm * ppm
        coords += [(x, y0), (x, y0 + h)]

    shader = _shader()
    batch = batch_for_shader(shader, 'LINES', {"pos": coords})
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(1.5)
    shader.bind()
    shader.uniform_float("color", (1.0, 0.85, 0.2, 1.0))
    batch.draw(shader)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')

    blf.color(0, 1.0, 0.85, 0.2, 1.0)
    try:
        blf.size(0, 13 * bpy.context.preferences.system.pixel_size)
    except TypeError:
        blf.size(0, 13, 72)
    blf.position(0, x0, y0 + 26, 0)
    blf.draw(0, "100 mm - measure me with a real ruler")


def _redraw_all():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _toggle_ruler(self, context):
    global _draw_handle
    if self.true_scale_show_ruler and _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_ruler, (), 'WINDOW', 'POST_PIXEL')
    elif not self.true_scale_show_ruler and _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    _redraw_all()


# ---------------------------------------------------------------------------
# Preferences + UI
# ---------------------------------------------------------------------------
class TrueScalePreferences(AddonPreferences):
    bl_idname = __name__

    display_mode: EnumProperty(name="Display", items=LOOKS_LIKE_ITEMS, default='AUTO')
    custom_px_per_mm: FloatProperty(
        name="Pixels per mm", default=2880 / PANEL_WIDTH_MM, min=0.1, max=100.0, precision=4)
    calibration: FloatProperty(
        name="Calibration", default=1.0, min=0.5, max=2.0, precision=4,
        description="Fine correction factor from ruler measurement")
    measured_mm: FloatProperty(
        name="Measured", default=100.0, min=1.0, max=500.0, precision=1, unit='NONE',
        description="Length of the on-screen '100 mm' ruler as measured with a real ruler (mm)")

    def draw(self, context):
        draw_settings(self.layout)


def draw_settings(layout):
    p = prefs()
    col = layout.column(align=True)
    col.prop(p, "display_mode", text="")
    if p.display_mode == 'CUSTOM':
        col.prop(p, "custom_px_per_mm")
    elif p.display_mode == 'AUTO':
        row = col.row(align=True)
        row.label(text=_detected["info"], icon='DESKTOP')
        row.operator("view3d.true_scale_detect", text="", icon='FILE_REFRESH')
    layout.label(text=f"{px_per_mm():.3f} px/mm  ·  MacBook Air M1 panel {PANEL_WIDTH_MM:.1f} mm wide")


class VIEW3D_PT_true_scale(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "View"
    bl_label = "True Scale Zoom"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        region, rv3d, space = main_view(context)

        op = layout.operator("view3d.true_scale_zoom", text="Zoom to 100%", icon='ZOOM_ALL')
        op.percent, op.center = 100.0, wm.true_scale_center
        row = layout.row(align=True)
        for pct in (25, 50, 200, 400):
            o = row.operator("view3d.true_scale_zoom", text=f"{pct}%")
            o.percent, o.center = pct, wm.true_scale_center
        layout.prop(wm, "true_scale_center", text="Center")

        pct = current_percent(region, rv3d, space, context.scene) if rv3d else None
        box = layout.box()
        box.label(text=f"Current: {pct:.1f}% of real size" if pct else
                  "Current: perspective (not true scale)", icon='INFO')

        layout.separator()
        layout.label(text="Display")
        draw_settings(layout)

        layout.separator()
        p = prefs()
        cal = layout.box()
        cal.label(text="Calibrate", icon='DRIVER_DISTANCE')

        # Manual: type the factor directly (drag, or click and type e.g. 1.111)
        r = cal.row(align=True)
        r.prop(p, "calibration", text="Factor")
        r.operator("view3d.true_scale_reset_calibration", text="", icon='LOOP_BACK')
        r = cal.row(align=True)
        for label, mult in (("-1%", 0.99), ("-0.1%", 0.999), ("+0.1%", 1.001), ("+1%", 1.01)):
            o = r.operator("view3d.true_scale_nudge", text=label)
            o.mult = mult

        # From a measurement: '100 mm on screen measured X mm'
        r = cal.row(align=True)
        r.prop(p, "measured_mm", text="100 mm measured as")
        r.operator("view3d.true_scale_calibrate", text="", icon='CHECKMARK')
        cal.prop(wm, "true_scale_show_ruler", text="Show 100 mm ruler")


def menu_func(self, context):
    self.layout.separator()
    self.layout.operator("view3d.true_scale_zoom", text="Zoom to True Scale (100%)",
                         icon='ZOOM_ALL').percent = 100.0


classes = (
    TrueScalePreferences,
    VIEW3D_OT_true_scale_zoom,
    VIEW3D_OT_true_scale_detect,
    VIEW3D_OT_true_scale_calibrate,
    VIEW3D_OT_true_scale_nudge,
    VIEW3D_OT_true_scale_reset_cal,
    VIEW3D_PT_true_scale,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.true_scale_show_ruler = BoolProperty(
        name="Show Ruler", default=False, update=_toggle_ruler)
    bpy.types.WindowManager.true_scale_center = EnumProperty(
        name="Center on",
        items=[('KEEP', "Keep", "Keep current view center"),
               ('SELECTION', "Selection", "Center on selected objects"),
               ('CURSOR', "3D Cursor", "Center on the 3D cursor")],
        default='KEEP')
    bpy.types.VIEW3D_MT_view.append(menu_func)


def unregister():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    bpy.types.VIEW3D_MT_view.remove(menu_func)
    del bpy.types.WindowManager.true_scale_center
    del bpy.types.WindowManager.true_scale_show_ruler
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
