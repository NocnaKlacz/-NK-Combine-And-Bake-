bl_info = {
    "name": "NK Combine & Bake (single UV + single material)",
    "author": "NK",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar (N) > NK Bake",
    "description": "Combines the selected objects, creates one shared UV map and bakes "
                   "all materials into one material with basic textures",
    "category": "Material",
}

import math
import os

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       StringProperty)
from bpy.types import Operator, Panel, PropertyGroup

ATLAS_UV = "AtlasUV"
DEFAULT_UV = "__default_uv"
MAX_UV_LAYERS = 8


# --------------------------------------------------------------------------
# Pomocnicze: node tree
# --------------------------------------------------------------------------

def _iter_trees(tree, seen=None):
    """The tree plus all nested node groups."""
    seen = seen if seen is not None else set()
    if tree is None or tree.name_full in seen:
        return
    seen.add(tree.name_full)
    yield tree
    for n in tree.nodes:
        if n.type == 'GROUP' and n.node_tree:
            yield from _iter_trees(n.node_tree, seen)


def _used_uv_names(materials):
    """UV map names explicitly referenced by nodes (UV Map, Normal Map, Tangent...)."""
    names = set()
    for mat in materials:
        if mat is None or mat.node_tree is None:
            continue
        for tree in _iter_trees(mat.node_tree):
            for n in tree.nodes:
                if n.type in {'UVMAP', 'NORMAL_MAP', 'TANGENT'}:
                    name = getattr(n, "uv_map", "")
                    if name:
                        names.add(name)
    return names


def _find_output(nt):
    out = nt.get_output_node('ALL') if hasattr(nt, "get_output_node") else None
    if out is None:
        for n in nt.nodes:
            if n.type == 'OUTPUT_MATERIAL':
                out = n
                break
    if out is None:
        out = nt.nodes.new('ShaderNodeOutputMaterial')
    return out


def _find_principled(out):
    """BFS upstream from the material output to the first Principled BSDF."""
    surf = out.inputs.get('Surface')
    if surf is None or not surf.is_linked:
        return None
    queue = [surf.links[0].from_node]
    seen = set()
    while queue:
        node = queue.pop(0)
        if node.name in seen:
            continue
        seen.add(node.name)
        if node.type == 'BSDF_PRINCIPLED':
            return node
        for s in node.inputs:
            if s.type == 'SHADER' and s.is_linked:
                queue.append(s.links[0].from_node)
    return None


# --------------------------------------------------------------------------
# Material preparation for baking (works on a COPY of the material)
# --------------------------------------------------------------------------

class _MatBake:
    CHANNEL_SOCKET = {
        'BASE': 'Base Color',
        'METALLIC': 'Metallic',
        'ROUGHNESS': 'Roughness',
        'EMISSION': 'Emission Color',
        'ALPHA': 'Alpha',
    }
    CHANNEL_DEFAULT = {
        'BASE': (0.8, 0.8, 0.8, 1.0),
        'METALLIC': 0.0,
        'ROUGHNESS': 0.5,
        'EMISSION': (0.0, 0.0, 0.0, 1.0),
        'ALPHA': 1.0,
    }

    def __init__(self, mat):
        self.mat = mat
        if mat.node_tree is None:
            mat.use_nodes = True
        elif hasattr(mat, "use_nodes") and not mat.use_nodes:
            mat.use_nodes = True
        self.nt = mat.node_tree
        self.out = _find_output(self.nt)
        surf = self.out.inputs['Surface']
        self.orig_src = surf.links[0].from_socket if surf.is_linked else None
        self.pr = _find_principled(self.out)
        if self.pr is None and not surf.is_linked:
            # empty material -> default Principled using the viewport color
            self.pr = self.nt.nodes.new('ShaderNodeBsdfPrincipled')
            self.pr.inputs['Base Color'].default_value = tuple(mat.diffuse_color)
            self.nt.links.new(self.pr.outputs['BSDF'], surf)
            self.orig_src = self.pr.outputs['BSDF']
        self.surface_node = self.orig_src.node if self.orig_src else None

        self._force_default_uv()
        self.emit = self.nt.nodes.new('ShaderNodeEmission')
        self.img_node = self.nt.nodes.new('ShaderNodeTexImage')
        self.img_node.interpolation = 'Linear'

    def _force_default_uv(self):
        """Explicitly wires __default_uv wherever the material relied on the default UV."""
        for tree in _iter_trees(self.nt):
            uvn = None
            for n in list(tree.nodes):
                if n.type == 'TEX_IMAGE' and not n.inputs['Vector'].is_linked:
                    if uvn is None:
                        uvn = tree.nodes.new('ShaderNodeUVMap')
                        uvn.uv_map = DEFAULT_UV
                    tree.links.new(uvn.outputs['UV'], n.inputs['Vector'])
                elif n.type in {'NORMAL_MAP', 'TANGENT'} and not n.uv_map:
                    n.uv_map = DEFAULT_UV
                elif n.type == 'TEX_COORD':
                    pass

    def set_target(self, image):
        self.img_node.image = image
        for n in self.nt.nodes:
            n.select = False
        self.img_node.select = True
        self.nt.nodes.active = self.img_node

    def _source_socket_for(self, channel):
        node = self.pr or self.surface_node
        if node is None:
            return None
        name = self.CHANNEL_SOCKET[channel]
        sock = node.inputs.get(name)
        if sock is None and channel == 'BASE':
            sock = node.inputs.get('Color')
        if sock is None and channel == 'EMISSION' and node.type == 'EMISSION':
            sock = node.inputs.get('Color')
        return sock

    def route(self, channel):
        """Routes the chosen channel through Emission -> Output (for an EMIT bake)."""
        nt = self.nt
        col_in = self.emit.inputs['Color']
        for l in list(col_in.links):
            nt.links.remove(l)
        sock = self._source_socket_for(channel)
        default = self.CHANNEL_DEFAULT[channel]
        if sock is not None and sock.is_linked:
            nt.links.new(sock.links[0].from_socket, col_in)
        else:
            val = default
            if sock is not None and hasattr(sock, "default_value"):
                dv = sock.default_value
                try:
                    val = tuple(dv)
                except TypeError:
                    val = dv
            if isinstance(val, (int, float)):
                v = float(val)
                val = (v, v, v, 1.0)
            else:
                val = tuple(val) + (1.0,) * (4 - len(val))
            if channel == 'EMISSION' and self.pr is not None:
                st = self.pr.inputs.get('Emission Strength')
                if st is not None and not st.is_linked:
                    s = float(st.default_value)
                    val = (val[0] * s, val[1] * s, val[2] * s, 1.0)
            col_in.default_value = val
        nt.links.new(self.emit.outputs['Emission'], self.out.inputs['Surface'])

    def route_original(self):
        """Przywraca oryginalny shader (do wypieku NORMAL)."""
        if self.orig_src is not None:
            self.nt.links.new(self.orig_src, self.out.inputs['Surface'])


# --------------------------------------------------------------------------
# Diagnostics (console + Text datablock "NK_Bake_Report")
# --------------------------------------------------------------------------

class _Log:
    def __init__(self):
        self.lines = []

    def __call__(self, *args):
        line = " ".join(str(a) for a in args)
        self.lines.append(line)
        print("[NK Bake]", line)

    def flush(self):
        old = bpy.data.texts.get("NK_Bake_Report")
        if old:
            bpy.data.texts.remove(old)
        txt = bpy.data.texts.new("NK_Bake_Report")
        txt.write("\n".join(self.lines))


def _diag_material_nodes(log, mat):
    if mat is None or mat.node_tree is None:
        log("  material", mat.name if mat else None, ": no node tree")
        return
    log("  material", repr(mat.name), "explicit UV names:", sorted(_used_uv_names([mat])))
    shown = 0
    for tree in _iter_trees(mat.node_tree):
        tag = "" if tree is mat.node_tree else f"[group {tree.name}] "
        counts = {}
        for n in tree.nodes:
            counts[n.type] = counts.get(n.type, 0) + 1
            if n.type == 'TEX_IMAGE' and shown < 25:
                shown += 1
                vec = n.inputs['Vector']
                if vec.is_linked:
                    fn = vec.links[0].from_node
                    src = f"{fn.type}.{vec.links[0].from_socket.name}"
                    if fn.type == 'UVMAP':
                        src += f"({fn.uv_map})"
                else:
                    src = "DEFAULT-UV"
                outs = [l.to_node.type + "." + l.to_socket.name
                        for o in n.outputs for l in o.links][:3]
                log("    ", tag + "tex", repr(n.image.name if n.image else None),
                    "<-", src, "->", outs)
            elif n.type in {'UVMAP', 'NORMAL_MAP'}:
                log("    ", tag + n.type, "uv_map =", repr(n.uv_map))
        interesting = {k: v for k, v in counts.items()
                       if k in {'MAPPING', 'TEX_COORD', 'GROUP', 'BUMP', 'NORMAL_MAP',
                                'BSDF_PRINCIPLED', 'MIX_SHADER', 'ATTRIBUTE'}}
        log("    ", tag + "node counts:", interesting)


def _diag_mesh(log, me, title):
    n = len(me.polygons)
    log("==", title, "| polys:", n, "| slots:",
        [m.name if m else None for m in me.materials])
    mi = [0] * n
    me.polygons.foreach_get("material_index", mi)
    degs = {}
    for l in me.uv_layers:
        buf = _get_uv(l)
        deg = _degenerate_polys(me, buf)
        degs[l.name] = deg
        us, vs = buf[0::2], buf[1::2]
        bounds = (round(min(us), 3), round(max(us), 3),
                  round(min(vs), 3), round(max(vs), 3)) if us else None
        log("  UV", repr(l.name), "active_render:", l.active_render,
            "active:", l.active, "| degenerate faces:", sum(deg), "/", n,
            "| u/v bounds:", bounds)
    for si, mat in enumerate(me.materials):
        idx = [i for i in range(n) if mi[i] == si]
        if not idx:
            continue
        used = sorted(_used_uv_names([mat])) if mat else []
        parts = []
        for name, deg in degs.items():
            d = sum(1 for i in idx if deg[i])
            if d or name in used or name == DEFAULT_UV:
                parts.append(f"{name}: {d}/{len(idx)} degenerate")
        log("  slot", si, repr(mat.name if mat else None), "faces:", len(idx),
            "| uses explicit:", used, "|", "; ".join(parts))


# --------------------------------------------------------------------------
# Przygotowanie geometrii
# --------------------------------------------------------------------------

def _copy_uv(src_layer, dst_layer):
    n = len(src_layer.uv)
    buf = [0.0] * (n * 2)
    src_layer.uv.foreach_get("vector", buf)
    dst_layer.uv.foreach_set("vector", buf)


def _make_baked_copy(context, obj, depsgraph, fallback_mat):
    """Object copy: modifiers applied, transform baked into the mesh,
    UVs reduced to the used ones + one 'default' layer (__default_uv)."""
    ev = obj.evaluated_get(depsgraph)
    me = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True,
                                         depsgraph=depsgraph)
    mats = [s.material or fallback_mat for s in obj.material_slots]
    # materials.clear() resets every face's material_index to 0, so keep and restore it
    mat_idx = [0] * len(me.polygons)
    me.polygons.foreach_get("material_index", mat_idx)
    me.materials.clear()
    for m in mats:
        me.materials.append(m)
    if not mats:
        mats = [fallback_mat]
        me.materials.append(fallback_mat)
    last = len(mats) - 1
    me.polygons.foreach_set("material_index", [min(i, last) for i in mat_idx])

    mw = obj.matrix_world.copy()
    me.transform(mw)
    if mw.determinant() < 0:
        me.flip_normals()

    uvs = me.uv_layers
    used = _used_uv_names(mats)
    data = {}
    if len(uvs):
        src = next((l for l in uvs if l.active_render), uvs[0])
        default = _get_uv(src)
        others = [_get_uv(l) for l in uvs if l.name != src.name]
        data[DEFAULT_UV] = _repair_default(me, default, others)
        for l in uvs:
            if l.name in used:
                data[l.name] = _get_uv(l)

    new = bpy.data.objects.new(obj.name + "_bakecopy", me)
    context.collection.objects.link(new)
    return new, data


def _poly_ranges(me):
    n = len(me.polygons)
    starts = [0] * n
    me.polygons.foreach_get("loop_start", starts)
    ends = starts[1:] + [len(me.loops)]
    return starts, ends


def _degenerate_polys(me, buf, eps=1e-7):
    """True for polygons whose UV corners all collapse to (almost) one point."""
    starts, ends = _poly_ranges(me)
    res = []
    for s, e in zip(starts, ends):
        us = buf[2 * s:2 * e:2]
        vs = buf[2 * s + 1:2 * e:2]
        res.append(not us or (max(us) - min(us) < eps and max(vs) - min(vs) < eps))
    return res


def _repair_default(me, default, others):
    """Faces with collapsed UVs in the default layer take their UVs from the
    first other layer that has valid data for that face."""
    bad = _degenerate_polys(me, default)
    if not any(bad):
        return default
    starts, ends = _poly_ranges(me)
    alt = [(buf, _degenerate_polys(me, buf)) for buf in others]
    out = list(default)
    for i, is_bad in enumerate(bad):
        if not is_bad:
            continue
        for buf, deg in alt:
            if not deg[i]:
                out[2 * starts[i]:2 * ends[i]] = buf[2 * starts[i]:2 * ends[i]]
                break
    return out


def _get_uv(layer):
    buf = [0.0] * (len(layer.uv) * 2)
    layer.uv.foreach_get("vector", buf)
    return buf


def _normalize_uvs(pairs):
    """Every copy gets an IDENTICAL set and order of UV layers
    (missing ones zero-filled), so join cannot mix layers up."""
    names = [DEFAULT_UV]
    for _, data in pairs:
        for n in sorted(data):
            if n not in names:
                names.append(n)
    names = names[:MAX_UV_LAYERS - 1]  # miejsce na AtlasUV
    for obj, data in pairs:
        me = obj.data
        while len(me.uv_layers):
            me.uv_layers.remove(me.uv_layers[0])
        for n in names:
            layer = me.uv_layers.new(name=n, do_init=False)
            buf = data.get(n)
            if buf is not None:
                layer.uv.foreach_set("vector", buf)
    return names


def _select_slots(obj, slots):
    bpy.ops.mesh.select_all(action='DESELECT')
    for si in slots:
        obj.active_material_index = si
        bpy.ops.object.material_slot_select()


def _unwrap_atlas(context, obj, margin, angle, mode, init_uv=None, groups=None):
    """Creates AtlasUV. REPACK: original UV islands, equalized density, tight
    packing. SMART: fresh Smart UV Project unwrap."""
    me = obj.data
    layer = me.uv_layers.new(name=ATLAS_UV, do_init=False)
    me.uv_layers.active = layer
    repack = mode == 'REPACK' and init_uv is not None
    if repack:
        layer.uv.foreach_set("vector", init_uv)
    ts = context.scene.tool_settings
    prev_sync = ts.use_uv_select_sync
    prev_sel_mode = tuple(ts.mesh_select_mode)
    ts.use_uv_select_sync = True
    ts.mesh_select_mode = (False, False, True)
    bad = _degenerate_polys(me, init_uv) if repack else []
    if any(bad):
        me.polygons.foreach_set("select", bad)
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        if any(bad):
            # faces without usable UVs: unwrap them first, then repack everything
            bpy.ops.uv.smart_project(angle_limit=math.radians(angle),
                                     island_margin=margin, area_weight=0.0,
                                     correct_aspect=True, scale_to_bounds=False)
        # every group of material slots gets its own 0-1 layout (own textures)
        groups = groups or [None]
        for slots in groups:
            if slots is None:
                bpy.ops.mesh.select_all(action='SELECT')
            else:
                _select_slots(obj, slots)
            done = False
            if repack:
                try:
                    bpy.ops.uv.average_islands_scale()
                    bpy.ops.uv.pack_islands(rotate=True, margin=margin)
                    done = True
                except RuntimeError as e:
                    print("[NK Bake] repack failed, falling back to Smart UV:", e)
            if not done:
                bpy.ops.uv.smart_project(angle_limit=math.radians(angle),
                                         island_margin=margin,
                                         area_weight=0.0,
                                         correct_aspect=True,
                                         scale_to_bounds=False)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        ts.use_uv_select_sync = prev_sync
        ts.mesh_select_mode = prev_sel_mode


# --------------------------------------------------------------------------
# Final material
# --------------------------------------------------------------------------

def _build_final_material(name, images, use_alpha):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (700, 0)
    pr = nt.nodes.new('ShaderNodeBsdfPrincipled')
    pr.location = (400, 0)
    nt.links.new(pr.outputs['BSDF'], out.inputs['Surface'])
    uvn = nt.nodes.new('ShaderNodeUVMap')
    uvn.uv_map = ATLAS_UV
    uvn.location = (-500, 0)

    sockets = {
        'BASE': 'Base Color', 'METALLIC': 'Metallic', 'ROUGHNESS': 'Roughness',
        'EMISSION': 'Emission Color', 'ALPHA': 'Alpha',
    }
    y = 300
    for ch, img in images.items():
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = img
        tex.label = ch.title()
        tex.location = (-200, y)
        y -= 300
        nt.links.new(uvn.outputs['UV'], tex.inputs['Vector'])
        if ch == 'NORMAL':
            nm = nt.nodes.new('ShaderNodeNormalMap')
            nm.uv_map = ATLAS_UV
            nm.location = (150, y + 300)
            nt.links.new(tex.outputs['Color'], nm.inputs['Color'])
            nt.links.new(nm.outputs['Normal'], pr.inputs['Normal'])
        else:
            src = tex.outputs['Color']
            nt.links.new(src, pr.inputs[sockets[ch]])
            if ch == 'EMISSION':
                pr.inputs['Emission Strength'].default_value = 1.0
    if 'ALPHA' in images and hasattr(mat, "surface_render_method"):
        mat.surface_render_method = 'DITHERED'
    return mat


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------

class NKBakeSettings(PropertyGroup):
    result_name: StringProperty(name="Name", default="Combined")
    resolution: EnumProperty(
        name="Resolution",
        items=[(str(r), f"{r} px", "") for r in (512, 1024, 2048, 4096, 8192)],
        default="2048")
    samples: IntProperty(name="Samples", default=4, min=1, max=256,
                         description="Cycles samples used for baking (edge anti-aliasing)")
    bake_margin: IntProperty(name="Bake Margin (px)", default=4, min=0, max=128)
    uv_mode: EnumProperty(
        name="UV Layout",
        items=[('REPACK', "Repack original UVs",
                "Keeps the original UV islands, equalizes texel density and packs tightly"),
               ('SMART', "Smart UV Project", "Fresh unwrap (more, smaller islands)")],
        default='REPACK')
    island_margin: IntProperty(name="Island Margin (px)", default=6, min=0, max=64)
    angle: FloatProperty(name="Smart UV Angle", default=66.0, min=1.0, max=89.0)
    bake_base: BoolProperty(name="Base Color", default=True)
    bake_metallic: BoolProperty(name="Metallic", default=True)
    bake_roughness: BoolProperty(name="Roughness", default=True)
    bake_normal: BoolProperty(name="Normal", default=True)
    bake_emission: BoolProperty(name="Emission", default=False)
    bake_alpha: BoolProperty(name="Alpha", default=False)
    num_materials: IntProperty(
        name="Split into Materials", default=1, min=1, max=16,
        description="Split the model into this many materials, each with its own "
                    "texture set (more effective resolution). Original material "
                    "slots are grouped by surface area")
    hide_originals: BoolProperty(name="Hide Originals", default=True)
    output_dir: StringProperty(
        name="Texture Folder", subtype='DIR_PATH', default="",
        description="If empty, textures are packed into the .blend file")


# --------------------------------------------------------------------------
# Operator
# --------------------------------------------------------------------------

class NKBAKE_OT_combine(Operator):
    bl_idname = "nkbake.combine"
    bl_label = "Combine & Bake"
    bl_description = ("Creates one object with a shared UV map "
                      "and a single material with baked textures from the selected objects")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        s = context.scene.nk_bake
        scene = context.scene
        view_layer = context.view_layer
        sources = [o for o in context.selected_objects if o.type == 'MESH']
        if not sources:
            self.report({'ERROR'}, "Select at least one mesh object")
            return {'CANCELLED'}

        channels = [c for c, flag in (
            ('BASE', s.bake_base), ('METALLIC', s.bake_metallic),
            ('ROUGHNESS', s.bake_roughness), ('NORMAL', s.bake_normal),
            ('EMISSION', s.bake_emission), ('ALPHA', s.bake_alpha)) if flag]
        if not channels:
            self.report({'ERROR'}, "Enable at least one channel to bake")
            return {'CANCELLED'}

        res = int(s.resolution)
        prev_engine = scene.render.engine
        prev_samples = scene.cycles.samples
        prev_active = view_layer.objects.active
        prev_mode = prev_active.mode if prev_active else 'OBJECT'
        if prev_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        fallback_mat = bpy.data.materials.new("__nk_fallback")
        fallback_mat.use_nodes = True
        temp_mats = [fallback_mat]
        target = None
        n_groups = 1
        dup_meshes = []
        images = {}
        wm = context.window_manager
        wm.progress_begin(0, len(channels) + 2)
        log = _Log()
        log("Blender", bpy.app.version_string, "| sources:", [o.name for o in sources],
            "| res:", res, "| uv_mode:", s.uv_mode)

        try:
            seen_mats = set()
            for o in sources:
                _diag_mesh(log, o.data, f"SOURCE {o.name}")
                for m in o.data.materials:
                    if m is not None and m.name not in seen_mats:
                        seen_mats.add(m.name)
                        _diag_material_nodes(log, m)

            # 1. object copies
            depsgraph = context.evaluated_depsgraph_get()
            pairs = [_make_baked_copy(context, o, depsgraph, fallback_mat) for o in sources]
            _normalize_uvs(pairs)
            dups = [p[0] for p in pairs]
            dup_meshes = [d.data for d in dups]

            # 2. join
            for o in view_layer.objects:
                o.select_set(False)
            for d in dups:
                d.select_set(True)
            view_layer.objects.active = dups[0]
            bpy.ops.object.join()
            target = view_layer.objects.active
            target.name = s.result_name
            target.data.name = s.result_name
            me = target.data
            _diag_mesh(log, me, "JOINED (before atlas)")
            wm.progress_update(1)

            # split material slots into balanced groups (by surface area)
            n_poly = len(me.polygons)
            slot_of_poly = [0] * n_poly
            me.polygons.foreach_get("material_index", slot_of_poly)
            areas = [0.0] * n_poly
            me.polygons.foreach_get("area", areas)
            slot_area = {}
            for si, a in zip(slot_of_poly, areas):
                slot_area[si] = slot_area.get(si, 0.0) + a
            n_groups = max(1, min(s.num_materials, len(slot_area)))
            if s.num_materials > len(slot_area):
                self.report({'WARNING'}, f"Only {len(slot_area)} source material slots "
                                         f"in use - splitting into {n_groups}")
            loads = [0.0] * n_groups
            slot_group = {}
            for si, a in sorted(slot_area.items(), key=lambda kv: -kv[1]):
                g = loads.index(min(loads))
                slot_group[si] = g
                loads[g] += a
            groups = [sorted(si for si, g in slot_group.items() if g == k)
                      for k in range(n_groups)]
            log("split groups (slots) / surface area:", groups,
                [round(x, 3) for x in loads])

            # 3. shared UV: unwrap on a mesh with a SINGLE layer (AtlasUV),
            #    so nothing overwrites the original UVs, then restore them
            old_names = [l.name for l in me.uv_layers]
            saved = {n: _get_uv(me.uv_layers[n]) for n in old_names}
            while len(me.uv_layers):
                me.uv_layers.remove(me.uv_layers[0])
            _unwrap_atlas(context, target, s.island_margin / res, s.angle, s.uv_mode,
                          saved.get(DEFAULT_UV), groups if n_groups > 1 else None)
            atlas = _get_uv(me.uv_layers[ATLAS_UV])
            while len(me.uv_layers):
                me.uv_layers.remove(me.uv_layers[0])
            for n in old_names:
                lay = me.uv_layers.new(name=n, do_init=False)
                lay.uv.foreach_set("vector", saved[n])
            lay = me.uv_layers.new(name=ATLAS_UV, do_init=False)
            lay.uv.foreach_set("vector", atlas)
            me.uv_layers.active = me.uv_layers[ATLAS_UV]
            if DEFAULT_UV in me.uv_layers:
                me.uv_layers[DEFAULT_UV].active_render = True
            _diag_mesh(log, me, "READY TO BAKE (after atlas)")
            wm.progress_update(2)

            # 4. material copies on the slots (originals untouched)
            cache = {}
            preps = []
            for si, slot in enumerate(target.material_slots):
                g = slot_group.get(si, 0)
                orig = slot.material or fallback_mat
                key = (orig.name, g)
                if key not in cache:
                    cp = orig.copy()
                    temp_mats.append(cp)
                    cache[key] = (_MatBake(cp), g)
                    preps.append(cache[key])
                slot.material = cache[key][0].mat

            # 5. scena do wypieku
            scene.render.engine = 'CYCLES'
            scene.cycles.samples = s.samples
            for o in view_layer.objects:
                o.select_set(False)
            target.select_set(True)
            view_layer.objects.active = target

            for i, ch in enumerate(channels):
                is_color = ch in {'BASE', 'EMISSION'}
                for g in range(n_groups):
                    tag = f"_{g + 1}" if n_groups > 1 else ""
                    img = bpy.data.images.new(
                        f"{s.result_name}{tag}_{ch.lower()}", res, res,
                        alpha=(ch == 'ALPHA'), float_buffer=False)
                    img.colorspace_settings.name = 'sRGB' if is_color else 'Non-Color'
                    if ch == 'NORMAL':
                        img.generated_color = (0.5, 0.5, 1.0, 1.0)
                    elif ch == 'BASE':
                        img.generated_color = (0.5, 0.5, 0.5, 1.0)
                    else:
                        img.generated_color = (0.0, 0.0, 0.0, 1.0)
                    images[(g, ch)] = img

                for p, g in preps:
                    p.set_target(images[(g, ch)])
                    if ch == 'NORMAL':
                        p.route_original()
                    else:
                        p.route(ch)

                kwargs = dict(type='NORMAL' if ch == 'NORMAL' else 'EMIT',
                              margin=s.bake_margin, margin_type='EXTEND',
                              use_clear=True, use_selected_to_active=False,
                              target='IMAGE_TEXTURES', uv_layer=ATLAS_UV)
                if ch == 'NORMAL':
                    kwargs['normal_space'] = 'TANGENT'
                bpy.ops.object.bake(**kwargs)
                wm.progress_update(3 + i)

            # 6. mesh cleanup: only AtlasUV + one material
            # remove by fresh lookup: references held across a remove() go stale
            while True:
                extra = next((l.name for l in me.uv_layers if l.name != ATLAS_UV), None)
                if extra is None:
                    break
                me.uv_layers.remove(me.uv_layers[extra])
            me.uv_layers[ATLAS_UV].active = True
            me.uv_layers[ATLAS_UV].active_render = True
            log("final UV layers:", [l.name for l in me.uv_layers])
            me.materials.clear()
            for g in range(n_groups):
                gimgs = {ch: images[(g, ch)] for ch in channels}
                name = s.result_name + "_Material" + (f"_{g + 1}" if n_groups > 1 else "")
                me.materials.append(_build_final_material(name, gimgs, 'ALPHA' in gimgs))
            me.polygons.foreach_set("material_index",
                                    [slot_group.get(si, 0) for si in slot_of_poly])
            me.update()

            # 7. zapis / pakowanie tekstur
            out_dir = bpy.path.abspath(s.output_dir) if s.output_dir else ""
            for img in images.values():
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)
                    img.filepath_raw = os.path.join(out_dir, f"{img.name}.png")
                    img.file_format = 'PNG'
                    img.save()
                else:
                    img.pack()

            if s.hide_originals:
                for o in sources:
                    o.hide_set(True)

            self.report({'INFO'}, f"Done: '{target.name}' - {len(images)} textures ({n_groups} material(s)) at {res}px")
            return {'FINISHED'}

        except Exception as e:  # noqa: BLE001
            self.report({'ERROR'}, f"Error: {e}")
            log("EXCEPTION:", repr(e))
            if target is not None:
                bpy.data.objects.remove(target, do_unlink=True)
            for img in images.values():
                bpy.data.images.remove(img)
            return {'CANCELLED'}

        finally:
            if bpy.context.object and bpy.context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for m in temp_mats:
                try:
                    bpy.data.materials.remove(m)
                except Exception:  # noqa: BLE001
                    pass
            scene.render.engine = prev_engine
            scene.cycles.samples = prev_samples
            wm.progress_end()
            try:
                log.flush()
                print("[NK Bake] Full report saved in Text editor: NK_Bake_Report")
            except Exception:  # noqa: BLE001
                pass
            for m in dup_meshes:
                try:
                    if m.users == 0:
                        bpy.data.meshes.remove(m)
                except (ReferenceError, RuntimeError):
                    pass


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

class NKBAKE_PT_panel(Panel):
    bl_label = "Combine & Bake"
    bl_idname = "NKBAKE_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "NK Bake"

    def draw(self, context):
        s = context.scene.nk_bake
        col = self.layout.column(align=True)
        col.prop(s, "result_name")
        col.prop(s, "resolution")
        col.prop(s, "num_materials")
        col.prop(s, "samples")
        col.prop(s, "bake_margin")

        box = self.layout.box()
        box.label(text="UV")
        box.prop(s, "uv_mode")
        box.prop(s, "island_margin")
        if s.uv_mode == 'SMART':
            box.prop(s, "angle")

        box = self.layout.box()
        box.label(text="Channels")
        grid = box.grid_flow(columns=2, align=True)
        for p in ("bake_base", "bake_metallic", "bake_roughness",
                  "bake_normal", "bake_emission", "bake_alpha"):
            grid.prop(s, p)

        self.layout.prop(s, "hide_originals")
        self.layout.prop(s, "output_dir")
        self.layout.operator("nkbake.combine", icon='RENDER_RESULT')


classes = (NKBakeSettings, NKBAKE_OT_combine, NKBAKE_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.nk_bake = bpy.props.PointerProperty(type=NKBakeSettings)


def unregister():
    del bpy.types.Scene.nk_bake
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
