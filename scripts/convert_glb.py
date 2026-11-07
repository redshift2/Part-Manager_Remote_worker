#!/usr/bin/env python3
"""CAD_TO_GLB worker script. Called with INPUT_DIR OUTPUT_DIR.
Requires FreeCADCmd and trimesh installed in the worker Python environment.
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    source, output = map(Path, sys.argv[1:3])
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((source / 'manifest.json').read_text())
    if manifest.get('jobType') != 'CAD_TO_GLB' or manifest.get('inputFile') != 'part.FCStd':
        raise ValueError('Invalid CAD_TO_GLB manifest')
    cad = source / 'part.FCStd'
    if not cad.is_file():
        raise FileNotFoundError(cad)
    mesh_file = output / 'intermediate.obj'
    helper = output / 'freecad_mesh_export.py'
    helper.write_text('''import FreeCAD as App
import MeshPart
import Mesh
import sys
source, destination = sys.argv[-2:]
doc = App.openDocument(source)
try:
    meshes = []
    for obj in doc.Objects:
        if not hasattr(obj, "Shape") or obj.Shape.isNull() or not obj.Shape.Faces:
            continue
        # Ignore nested duplicates only when explicitly marked invisible.
        if hasattr(obj, "ViewObject") and obj.ViewObject and not obj.ViewObject.Visibility:
            continue
        mesh = MeshPart.meshFromShape(Shape=obj.Shape, LinearDeflection=0.15, AngularDeflection=0.35, Relative=False)
        if mesh.CountFacets:
            meshes.append(mesh)
    if not meshes:
        raise RuntimeError("No exportable solid geometry")
    combined = Mesh.Mesh()
    for mesh in meshes:
        combined.addMesh(mesh)
    combined.write(destination)
finally:
    App.closeDocument(doc.Name)
''')
    freecad_cmd = os.environ.get('FREECAD_CMD', 'FreeCADCmd')
    subprocess.run([freecad_cmd, str(helper), str(cad), str(mesh_file)], check=True, timeout=900)
    import trimesh
    model = trimesh.load(str(mesh_file), force='scene', process=False)
    glb = model.export(file_type='glb')
    if not isinstance(glb, bytes) or glb[:4] != b'glTF':
        raise RuntimeError('GLB export failed')
    (output / 'model.glb').write_bytes(glb)
    (output / 'conversion.json').write_text(json.dumps({
        'partId': manifest['partId'], 'cadVersion': manifest['cadVersion'],
        'cadSha1': manifest['cadSha1'], 'output': 'model.glb', 'bytes': len(glb)
    }, indent=2))
    mesh_file.unlink(missing_ok=True)
    helper.unlink(missing_ok=True)
    print('CAD_TO_GLB conversion completed')


if __name__ == '__main__':
    main()
