#!/usr/bin/env python3
"""CAD_TO_STEP worker script.

Usage: python3 convert_step.py INPUT_DIR OUTPUT_DIR

Requires FreeCADCmd (or set FREECAD_CMD). Input package must contain
manifest.json and part.FCStd. Output contains part.step and conversion.json.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: convert_step.py INPUT_DIR OUTPUT_DIR')
    source, output = (Path(p).resolve() for p in sys.argv[1:3])
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('jobType') != 'CAD_TO_STEP':
        raise ValueError('Expected CAD_TO_STEP jobType')
    input_name = manifest.get('inputFile', 'part.FCStd')
    if input_name != 'part.FCStd':
        raise ValueError('Unexpected inputFile; expected part.FCStd')
    cad = source / input_name
    if not cad.is_file():
        raise FileNotFoundError(cad)

    step_path = output / 'part.step'
    helper_code = r'''import FreeCAD as App
import Part
import sys

source, destination = sys.argv[-2:]
doc = App.openDocument(source)
try:
    doc.recompute()
    candidates = []
    for obj in doc.Objects:
        if not hasattr(obj, 'Shape'):
            continue
        shape = obj.Shape
        if shape.isNull() or not shape.Faces:
            continue
        view = getattr(obj, 'ViewObject', None)
        if view is not None and not view.Visibility:
            continue
        candidates.append(obj)

    # Avoid exporting both a container and its nested visible children.
    selected = []
    candidate_names = {obj.Name for obj in candidates}
    for obj in candidates:
        parents = list(getattr(obj, 'InListRecursive', []))
        if any(parent.Name in candidate_names and hasattr(parent, 'Shape')
               and not parent.Shape.isNull() and parent.Shape.Faces
               for parent in parents):
            continue
        selected.append(obj)

    if not selected:
        raise RuntimeError('No exportable CAD geometry')
    Part.export(selected, destination)
finally:
    App.closeDocument(doc.Name)
'''
    with tempfile.TemporaryDirectory(prefix='cad-to-step-') as tmp:
        helper = Path(tmp) / 'freecad_step_export.py'
        helper.write_text(helper_code, encoding='utf-8')
        subprocess.run(
            [os.environ.get('FREECAD_CMD', 'FreeCADCmd'), str(helper), str(cad), str(step_path)],
            check=True, timeout=900,
        )
    if not step_path.is_file() or step_path.stat().st_size < 50:
        raise RuntimeError('STEP export did not produce a valid nonempty file')
    with step_path.open('rb') as stream:
        if b'ISO-10303-21' not in stream.read(256):
            raise RuntimeError('Output does not have a STEP Part 21 header')
    metadata = {
        'partId': manifest.get('partId'),
        'cadVersion': manifest.get('cadVersion'),
        'cadSha1': manifest.get('cadSha1'),
        'output': 'part.step',
        'bytes': step_path.stat().st_size,
    }
    (output / 'conversion.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print('CAD_TO_STEP conversion completed')


if __name__ == '__main__':
    main()
