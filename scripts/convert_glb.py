#!/usr/bin/env python3
"""CAD_TO_GLB worker: FreeCAD geometry, per-object colors, high quality mesh.
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

source = __SOURCE__
destination = __DEST__
error_path = __ERROR__
doc = None
try:
    doc = App.openDocument(source)
    objects = []
    for obj in doc.Objects:
        if not hasattr(obj, 'Shape') or obj.Shape.isNull() or not obj.Shape.Faces:
            continue
        view = getattr(obj, 'ViewObject', None)
        if view is not None and not getattr(view, 'Visibility', True):
            continue
        # Avoid exporting a parent shape and its visible child geometry twice.
        mesh = MeshPart.meshFromShape(Shape=obj.Shape, LinearDeflection=0.05,
                                      AngularDeflection=0.15, Relative=False)
        if not mesh.CountFacets:
            continue
        vertices = []
        faces = []
        for facet in mesh.Facets:
            start = len(vertices)
            for point in facet.Points:
                vertices.append([float(point[0]), float(point[1]), float(point[2])])
            faces.append([start, start + 1, start + 2])
        color = getattr(view, 'ShapeColor', (0.75, 0.78, 0.82)) if view else (0.75, 0.78, 0.82)
        # Use per-object appearance; fallback to ShapeColor when no face material.
        objects.append({'name': str(getattr(obj, 'Label', obj.Name)),
                        'vertices': vertices, 'faces': faces,
                        'color': [float(color[i]) for i in range(3)]})
    if not objects:
        raise RuntimeError('No visible solid geometry to export')
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
