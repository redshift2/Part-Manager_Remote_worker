#!/usr/bin/env python3
"""CAD_TO_X3D worker script. Called with INPUT_DIR OUTPUT_DIR.

Requires FreeCADCmd with FreeCAD, MeshPart and Mesh modules available.
Produces an XML-encoded X3D 3.3 preview without external resources.
"""
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path


FREECAD_HELPER = r'''import FreeCAD as App
import MeshPart
import sys
import xml.etree.ElementTree as ET
import traceback

source = __CAD_SOURCE__
destination = __X3D_DESTINATION__
error_file = __ERROR_FILE__
doc = None
try:
    doc = App.openDocument(source)
    scene = ET.Element('X3D', {'profile': 'Interchange', 'version': '3.3'})
    ET.SubElement(scene, 'head')
    root = ET.SubElement(scene, 'Scene')
    count = 0
    for obj in doc.Objects:
        if not hasattr(obj, 'Shape') or obj.Shape.isNull() or not obj.Shape.Faces:
            continue
        # Keep the visibility convention of the existing GLB worker.
        if hasattr(obj, 'ViewObject') and obj.ViewObject and not obj.ViewObject.Visibility:
            continue
        mesh = MeshPart.meshFromShape(
            Shape=obj.Shape, LinearDeflection=0.15,
            AngularDeflection=0.35, Relative=False)
        if not mesh.CountFacets:
            continue
        coords = []
        indices = []
        # Each facet has three points; keep independent vertices to avoid
        # accidental smoothing or mismatched indices.
        for facet in mesh.Facets:
            start = len(coords)
            for p in facet.Points:
                coords.append('{:.9g} {:.9g} {:.9g}'.format(p[0], p[1], p[2]))
            indices.extend((str(start), str(start + 1), str(start + 2), '-1'))
        shape = ET.SubElement(root, 'Shape')
        appearance = ET.SubElement(shape, 'Appearance')
        ET.SubElement(appearance, 'Material', {'diffuseColor': '0.75 0.78 0.82'})
        geometry = ET.SubElement(shape, 'IndexedFaceSet', {
            'coordIndex': ' '.join(indices), 'solid': 'false', 'creaseAngle': '0'})
        ET.SubElement(geometry, 'Coordinate', {'point': ', '.join(coords)})
        count += 1
    if not count:
        raise RuntimeError('No exportable solid geometry')
    ET.ElementTree(scene).write(destination, encoding='utf-8', xml_declaration=True)
except BaseException:
    with open(error_file, 'w', encoding='utf-8') as error_log:
        error_log.write(traceback.format_exc())
    raise
finally:
    if doc is not None:
        App.closeDocument(doc.Name)
'''


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: convert_x3d.py INPUT_DIR OUTPUT_DIR')
    source, output = map(Path, sys.argv[1:3])
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('jobType') != 'CAD_TO_X3D' or manifest.get('inputFile') != 'part.FCStd':
        raise ValueError('Invalid CAD_TO_X3D manifest')
    cad = source / 'part.FCStd'
    if not cad.is_file():
        raise FileNotFoundError(cad)
    helper = output / 'freecad_x3d_export.py'
    x3d = output / 'model.x3d'
    error_file = output / 'freecad_x3d_error.log'
    try:
        script = (FREECAD_HELPER
                  .replace('__CAD_SOURCE__', repr(str(cad.resolve())))
                  .replace('__X3D_DESTINATION__', repr(str(x3d.resolve())))
                  .replace('__ERROR_FILE__', repr(str(error_file.resolve()))))
        helper.write_text(script, encoding='utf-8')
        freecad_cmd = os.environ.get('FREECAD_CMD', 'FreeCADCmd')
        command = [freecad_cmd, str(helper)]
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=900)
        if error_file.exists():
            raise RuntimeError('FreeCAD X3D export error:\n' + error_file.read_text(encoding='utf-8'))
        if completed.returncode != 0:
            raise RuntimeError('FreeCADCmd exited with code {}:\n{}'.format(
                completed.returncode, completed.stdout[-8000:]))
        import xml.etree.ElementTree as ET
        if not x3d.is_file() or x3d.stat().st_size < 100:
            raise RuntimeError('X3D export failed: FreeCADCmd produced no X3D file. '
                               'Verify that FREECAD_CMD runs Python scripts. FreeCAD output:\n'
                               + completed.stdout[-8000:])
        root = ET.parse(x3d).getroot()
        if root.tag != 'X3D':
            raise RuntimeError('X3D export failed: invalid document root')
        (output / 'conversion.json').write_text(json.dumps({
            'partId': manifest['partId'],
            'cadVersion': manifest['cadVersion'],
            'cadSha1': manifest['cadSha1'],
            'output': 'model.x3d',
            'bytes': x3d.stat().st_size,
        }, indent=2), encoding='utf-8')
        print('CAD_TO_X3D conversion completed')
    finally:
        helper.unlink(missing_ok=True)
        if not error_file.exists():
            error_file.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
