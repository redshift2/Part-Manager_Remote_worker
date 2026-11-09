#!/usr/bin/env python3
"""CAD_TO_GLB worker: FreeCAD geometry, per-face colors, high quality mesh.
Usage: python3 convert_glb.py INPUT_DIR OUTPUT_DIR
Requires FreeCADCmd on PATH (or FREECAD_CMD) and trimesh, numpy in worker Python.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HELPER = r'''import FreeCAD as App
import MeshPart
import json
import traceback
import zipfile
import struct
import xml.etree.ElementTree as ET

def saved_appearance_colors(fcstd_path):
    """Read FreeCAD 1.1 GUI ShapeAppearance (v3) from the FCStd ZIP.

    Each material is 36 bytes. The diffuse color is stored as ARGB
    at offset 8 within the first record (offset 4 + 4).
    """
    result = {}
    with zipfile.ZipFile(fcstd_path) as archive:
        if "GuiDocument.xml" not in archive.namelist():
            return result
        root = ET.fromstring(archive.read("GuiDocument.xml"))
        for provider in root.findall(".//ViewProvider"):
            name = provider.get("name")
            if not name:
                continue
            prop = next((p for p in provider.findall("./Properties/Property")
                         if p.get("name") == "ShapeAppearance"), None)
            if prop is None:
                continue
            matlist = prop.find("MaterialList")
            if matlist is None or matlist.get("version") != "3":
                continue
            entry = matlist.get("file")
            if not entry or entry not in archive.namelist():
                continue
            data = archive.read(entry)
            if len(data) < 4:
                continue
            count = struct.unpack_from("<I", data, 0)[0]
            if count == 0 or count > 100000 or len(data) < 4 + count * 36:
                continue
            colors = []
            for i in range(count):
                # Record layout: ambient ARGB (4), diffuse ARGB (4), ...
                alpha, red, green, blue = struct.unpack_from("4B", data, 4 + i * 36 + 4)
                colors.append((red / 255.0, green / 255.0, blue / 255.0))
            result[name] = colors
    return result

def saved_object_colors(fcstd_path):
    """Read uniform ShapeColor from GuiDocument.xml when available."""
    result = {}
    with zipfile.ZipFile(fcstd_path) as archive:
        if "GuiDocument.xml" not in archive.namelist():
            return result
        root = ET.fromstring(archive.read("GuiDocument.xml"))
        for provider in root.findall(".//ViewProvider"):
            name = provider.get("name")
            if not name:
                continue
            for prop in provider.findall("./Properties/Property"):
                if prop.get("name") != "ShapeColor":
                    continue
                value = prop.find("Color")
                if value is None:
                    continue
                try:
                    result[name] = tuple(float(value.get(k)) for k in ("r", "g", "b"))
                except (TypeError, ValueError):
                    pass
    return result


def choose_appearance(obj, saved_colors, uniform_colors, face_count):
    """Use Body or Tip colors, including uniform colors with mismatched face counts."""
    names = [obj.Name]
    # App::Part is often the exported top-level shape. It has no appearance
    # of its own, so look through its nested Body to the final feature.
    if obj.TypeId == 'App::Part':
        for child in getattr(obj, 'Group', []):
            if child.TypeId == 'PartDesign::Body':
                tip = getattr(child, 'Tip', None)
                if tip is not None:
                    names.insert(0, tip.Name)
                names.append(child.Name)
    elif obj.TypeId == 'PartDesign::Body':
        tip = getattr(obj, 'Tip', None)
        if tip is not None:
            names.insert(0, tip.Name)
    candidates = [saved_colors[name] for name in names if saved_colors.get(name)]
    # Exact per-face mapping takes priority. Otherwise preserve a uniform
    # appearance, even if the Body and Tip have different face counts.
    for colors in candidates:
        if len(colors) == face_count:
            return colors, None
    for colors in candidates:
        if len(colors) == 1:
            return colors, None
        if all(all(abs(a-b) < 1/255 for a,b in zip(c, colors[0])) for c in colors):
            return [colors[0]], None
    for name in names:
        if name in uniform_colors:
            return [], uniform_colors[name]
    # FreeCADCmd often has no ViewObject; if the saved appearance has a
    # different face count, a representative saved color is better than gray.
    if candidates:
        from collections import Counter
        colors = candidates[0]
        common = Counter(tuple(round(channel, 3) for channel in color) for color in colors)
        return [common.most_common(1)[0][0]], None
    return [], None


def saved_visibility(fcstd_path):
    """Read saved GUI visibility independently of FreeCADCmd's GUI state."""
    result = {}
    with zipfile.ZipFile(fcstd_path) as archive:
        if "GuiDocument.xml" not in archive.namelist():
            return result
        root = ET.fromstring(archive.read("GuiDocument.xml"))
        for provider in root.findall(".//ViewProvider"):
            name = provider.get("name")
            if not name:
                continue
            prop = next((p for p in provider.findall("./Properties/Property")
                         if p.get("name") == "Visibility"), None)
            if prop is None:
                continue
            value = prop.find("Bool")
            if value is not None:
                result[name] = value.get("value", "true").lower() in ("true", "1")
    return result


def is_final_export_object(obj):
    """Reject intermediate features belonging to a PartDesign Body."""
    if not hasattr(obj, "Shape") or obj.Shape.isNull() or not obj.Shape.Faces:
        return False
    if obj.isDerivedFrom("PartDesign::Feature"):
        for parent in obj.InList:
            if parent.TypeId == "PartDesign::Body":
                return False
    # Any shape owned by a PartDesign Body is represented by the Body's
    # evaluated final Shape. Avoid duplicate/intermediate geometry.
    for parent in obj.InListRecursive:
        if parent.TypeId == "PartDesign::Body":
            return False
    # Part::Cut/Fuse/Common etc. contain intermediate operands in their
    # dependency tree. Export only the outermost shape-bearing result.
    for parent in obj.InListRecursive:
        if hasattr(parent, "Shape") and not parent.Shape.isNull() and parent.Shape.Faces:
            return False
    return True


source = __SOURCE__
destination = __DEST__
error_path = __ERROR__
doc = None
try:
    saved_colors = saved_appearance_colors(source)
    uniform_colors = saved_object_colors(source)
    visibility = saved_visibility(source)
    doc = App.openDocument(source)
    objects = []
    # Assemblies have component-level appearances. Export the leaf component
    # shapes rather than the combined Assembly/App::Part shape, which has no
    # meaningful single face-to-material mapping.
    is_assembly = any(o.TypeId == 'Assembly::AssemblyObject' for o in doc.Objects)
    
    print("GLB DEBUG: Assembly detected:", is_assembly, flush=True)

    for obj in doc.Objects:
        if hasattr(obj, "Shape") and not obj.Shape.isNull():
            print(
                "GLB DEBUG:",
                obj.Name,
                obj.Label,
                obj.TypeId,
                flush=True
            )
    if is_assembly:
        candidates = []
        for obj in doc.Objects:
            if obj.TypeId != 'Part::Feature':
                continue
            if not hasattr(obj, 'Shape') or obj.Shape.isNull() or not obj.Shape.Faces:
                continue
            # A feature used as input to another shape-bearing feature is an
            # intermediate, not a separate assembly component.
            if any(p.TypeId == 'PartDesign::Body' or
                   (p.TypeId != 'App::Part' and p.TypeId != 'Assembly::AssemblyObject' and
                    hasattr(p, 'Shape') and not p.Shape.isNull() and p.Shape.Faces)
                   for p in obj.InListRecursive):
                continue
            candidates.append(obj)
        if not candidates:
            raise RuntimeError('Assembly has no exportable component shapes')
        print('GLB assembly: exporting {} individually colored components'.format(len(candidates)))
    else:
        candidates = [o for o in doc.Objects if is_final_export_object(o)]
    for obj in candidates:
        if not visibility.get(obj.Name, True):
            continue
        view = getattr(obj, 'ViewObject', None)
        shape = obj.Shape
        if is_assembly:
            # The feature Shape already includes its own local Placement.
            # getGlobalPlacement() includes the enclosing App::Part placements.
            # Apply ONLY the missing parent transform; applying global placement
            # directly would double-apply the feature's own Placement.
            if not hasattr(obj, 'getGlobalPlacement'):
                raise RuntimeError('Assembly component {} lacks getGlobalPlacement'.format(obj.Name))
            local = obj.Placement
            global_placement = obj.getGlobalPlacement()
            parent_delta = global_placement.multiply(local.inverse())
            shape = shape.copy()
            shape.Placement = parent_delta.multiply(shape.Placement)
            print('GLB PLACEMENT: {} local={} global={} parent_delta={}'.format(
                obj.Name, local, global_placement, parent_delta), flush=True)
        faces = list(shape.Faces)
        diffuse, saved_uniform = choose_appearance(obj, saved_colors, uniform_colors, len(faces))
        default_color = saved_uniform or (getattr(view, 'ShapeColor', (0.75, 0.78, 0.82)) if view else (0.75, 0.78, 0.82))
        if not diffuse and view:
            diffuse = list(getattr(view, 'DiffuseColor', []) or [])
        # DiffuseColor contains one entry per topological face when face colors
        # are assigned; otherwise ShapeColor is the uniform object color.
        # Tessellate each face separately so triangle/material correspondence
        # survives mesh generation. MeshPart.meshFromShape(whole_shape) loses it.
        for face_index, face in enumerate(faces):
            if diffuse and len(diffuse) == len(faces):
                color = diffuse[face_index]
            elif len(diffuse) == 1:
                color = diffuse[0]
            else:
                color = default_color
            mesh = MeshPart.meshFromShape(Shape=face, LinearDeflection=0.05,
                                          AngularDeflection=0.15, Relative=False)
            if not mesh.CountFacets:
                continue
            vertices = []
            triangles = []
            for facet in mesh.Facets:
                start = len(vertices)
                for point in facet.Points:
                    vertices.append([float(point[0]), float(point[1]), float(point[2])])
                triangles.append([start, start + 1, start + 2])
            objects.append({'name': '{}_face_{}'.format(str(getattr(obj, 'Label', obj.Name)), face_index + 1),
                            'vertices': vertices, 'faces': triangles,
                            'color': [float(color[i]) for i in range(3)]})
    if not objects:
        raise RuntimeError('No visible geometry to export')
    with open(destination, 'w', encoding='utf-8') as handle:
        json.dump(objects, handle)
except BaseException:
    with open(error_path, 'w', encoding='utf-8') as handle:
        handle.write(traceback.format_exc())
    raise
finally:
    if doc is not None:
        App.closeDocument(doc.Name)
'''


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: convert_glb.py INPUT_DIR OUTPUT_DIR')
    source, output = map(Path, sys.argv[1:3])
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('jobType') != 'CAD_TO_GLB' or manifest.get('inputFile') != 'part.FCStd':
        raise ValueError('Invalid CAD_TO_GLB manifest')
    cad = source / 'part.FCStd'
    if not cad.is_file():
        raise FileNotFoundError(cad)
    helper = output / 'freecad_glb_export.py'
    geometry = output / 'freecad_glb_geometry.json'
    error = output / 'freecad_glb_error.log'
    glb_path = output / 'model.glb'
    for old in (geometry, error, glb_path):
        old.unlink(missing_ok=True)
    script = (HELPER.replace('__SOURCE__', repr(str(cad.resolve())))
                    .replace('__DEST__', repr(str(geometry.resolve())))
                    .replace('__ERROR__', repr(str(error.resolve()))))
    helper.write_text(script, encoding='utf-8')
    try:
        command = [os.environ.get('FREECAD_CMD', 'FreeCADCmd'), str(helper)]
        completed = subprocess.run(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, timeout=900)
        debug_log = output / "glb_debug.log"

        debug_log.write_text(
            "Command: {}\n\n"
            "Return code: {}\n\n"
            "FreeCADCmd output:\n{}\n".format(
                command,
                completed.returncode,
                completed.stdout
            ),
            encoding="utf-8"
        )
        print(
            "GLB DEBUG: FreeCADCmd output:\n",
            completed.stdout,
            flush=True
        )
        if error.is_file():
            raise RuntimeError('FreeCAD mesh export failed:\n' + error.read_text(encoding='utf-8'))
        if completed.returncode != 0 or not geometry.is_file():
            raise RuntimeError('FreeCADCmd returned {}: {}'.format(completed.returncode, completed.stdout[-8000:]))
        import numpy as np
        import trimesh
        scene = trimesh.Scene()
        items = json.loads(geometry.read_text(encoding='utf-8'))
        for index, item in enumerate(items):
            mesh = trimesh.Trimesh(vertices=np.asarray(item['vertices'], dtype=np.float64),
                                   faces=np.asarray(item['faces'], dtype=np.int64),
                                   process=False)
            if mesh.is_empty:
                continue
            # Merge coincident vertices for smooth normals on curved surfaces.
            # Preserve hard edges by using angle-weighted vertex normals only
            # on meshes with non-degenerate triangles.
            mesh.merge_vertices()
            rgb = np.clip(np.asarray(item['color'], dtype=float), 0, 1)
            mesh.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(
                baseColorFactor=[int(round(c * 255)) for c in rgb] + [255],
                metallicFactor=0.12, roughnessFactor=0.68, doubleSided=True))
            scene.add_geometry(mesh, geom_name='part_{}'.format(index),
                               node_name=item['name'] + '_{}'.format(index))
        if not scene.geometry:
            raise RuntimeError('FreeCAD produced no usable triangle meshes')
        data = scene.export(file_type='glb')
        if not isinstance(data, bytes) or data[:4] != b'glTF':
            raise RuntimeError('Invalid GLB output')
        glb_path.write_bytes(data)
        (output / 'conversion.json').write_text(json.dumps({
            'partId': manifest['partId'], 'cadVersion': manifest['cadVersion'],
            'cadSha1': manifest['cadSha1'], 'output': 'model.glb', 'bytes': len(data)
        }, indent=2), encoding='utf-8')
        print('CAD_TO_GLB conversion completed: {} objects, {} bytes'.format(len(scene.geometry), len(data)))
    finally:
        helper.unlink(missing_ok=True)
        geometry.unlink(missing_ok=True)
        if not error.is_file():
            error.unlink(missing_ok=True)


if __name__ == '__main__':
    main()