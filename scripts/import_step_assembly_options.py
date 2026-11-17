#!/usr/bin/env python3
"""Convert a STEP file to independent FreeCAD parts plus a linked top-level assembly.

Worker usage: python3 import_step_assembly.py INPUT_DIR OUTPUT_DIR
INPUT_DIR contains a STEP file (.step/.stp) and optionally manifest.json.
Runs FreeCADGui in xvfb because STEP import uses the FreeCAD Import workbench.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_SCHEMA_JSON = r'''
{
  "fields": [
    {
      "name": "executionTimeoutSeconds",
      "label": "Execution Timeout (seconds)",
      "type": "integer",
      "default": 3600
    },
    {
      "name": "splitAssembly",
      "label": "Split Assembly into Parts",
      "type": "boolean",
      "default": true
    },
    {
      "name": "reuseIdenticalParts",
      "label": "Reuse Identical Parts",
      "type": "boolean",
      "default": true
    },
    {
      "name": "outputName",
      "label": "Output Assembly Name",
      "type": "text",
      "default": "assembly"
    },
    {
      "name": "prefix",
      "label": "Part Name Prefix",
      "type": "text",
      "default": ""
    },
    {
      "name": "generateReport",
      "label": "Generate Assembly Report",
      "type": "boolean",
      "default": true
    }
  ]
}
'''

MACRO = r'''import FreeCAD as App
import FreeCADGui as Gui
import Import
import Part
from PySide import QtCore
import json
import os
import re
import traceback

SOURCE = __SOURCE__
OUT = __OUT__
RESULT = __RESULT__
OPTIONS = __OPTIONS__


def clean(s):
    s = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(s or 'Part')).strip('._')
    return s[:80] or 'Part'


def vec_data(p):
    return {'position': [p.Base.x, p.Base.y, p.Base.z],
            'quaternion': list(p.Rotation.Q)}


def run():
    documents = []
    try:
        os.makedirs(OUT, exist_ok=True)
        source_doc = App.newDocument('StepImport')
        documents.append(source_doc.Name)
        Import.insert(SOURCE, source_doc.Name)
        source_doc.recompute()
        # Import.insert generally preserves STEP product labels and hierarchical
        # App::Part groups when present. Shape-bearing leaves are occurrences.
        candidates = []
        for obj in source_doc.Objects:
            if not hasattr(obj, 'Shape') or obj.Shape.isNull():
                continue
            if not getattr(obj.Shape, 'Solids', []):
                continue
            children = list(getattr(obj, 'Group', []) or [])
            if any(hasattr(c, 'Shape') and not c.Shape.isNull() for c in children):
                continue  # assembly container, not a separate part
            candidates.append(obj)
        if not candidates:
            raise RuntimeError('STEP import produced no solid geometry')

        # A single compound with multiple solids is split if the STEP importer
        # did not preserve separate product objects.
        instances = []
        for obj in candidates:
            shape = obj.Shape
            solids = list(shape.Solids)
            if OPTIONS['splitAssembly'] and len(candidates) == 1 and len(solids) > 1:
                for index, solid in enumerate(solids, 1):
                    instances.append((obj.Label + '_%03d' % index, solid.copy(), App.Placement()))
            else:
                # FreeCAD shape geometry may already contain object Placement.
                # Keep shape in world coordinates, then normalize to local space.
                instances.append((obj.Label, shape.copy(), App.Placement()))

        assembly = App.newDocument('ImportedAssembly')
        documents.append(assembly.Name)
        root = assembly.addObject('App::Part', 'Assembly')
        root.Label = OPTIONS['outputName']
        # FreeCAD requires the owner document to be saved before a cross-document
        # App::Link can reference an object in another saved document.
        assembly_filename = OPTIONS['outputName'] + '.FCStd'
        assembly_path = os.path.join(OUT, assembly_filename)
        assembly.recompute()
        assembly.saveAs(assembly_path)
        definitions = {}
        entries = []
        used_filenames = {assembly_filename.lower(), 'step_import_result.json', 'assembly_report.json'}
        prefix = OPTIONS['prefix']
        for number, (label, shape, _) in enumerate(instances, 1):
            # Preserve absolute geometry by removing only its bounding-box
            # center translation. Each link places it back at that position.
            center = shape.BoundBox.Center
            local = shape.copy()
            local.translate(App.Vector(-center.x, -center.y, -center.z))
            # Reuse identical geometry, including repeated STEP occurrences.
            # hashCode alone is not sufficient for a collision-proof identity;
            # confirm with isSame/isEqual if FreeCAD exposes it.
            fingerprint = (round(local.Volume, 7), round(local.Area, 7),
                           round(local.BoundBox.XLength, 7),
                           round(local.BoundBox.YLength, 7),
                           round(local.BoundBox.ZLength, 7),
                           local.hashCode())
            part_info = definitions.get(fingerprint) if OPTIONS['reuseIdenticalParts'] else None
            if part_info is None:
                part_name = clean(prefix + label)
                filename = part_name + '.FCStd'
                suffix = 2
                while filename.lower() in used_filenames:
                    filename = '%s_%03d.FCStd' % (part_name, suffix)
                    suffix += 1
                used_filenames.add(filename.lower())
                part_doc = App.newDocument('Part_%03d' % number)
                documents.append(part_doc.Name)
                body = part_doc.addObject('PartDesign::Feature', 'ImportedShape')
                body.Label = prefix + label
                body.Shape = local
                part_doc.recompute()
                path = os.path.join(OUT, filename)
                part_doc.saveAs(path)
                part_info = (part_doc, body, filename)
                definitions[fingerprint if OPTIONS['reuseIdenticalParts'] else number] = part_info
            part_doc, body, filename = part_info
            link = assembly.addObject('App::Link', 'Occurrence_%03d' % number)
            link.Label = prefix + label
            # Both the target part and the owner assembly now have file paths.
            link.setLink(body)
            root.addObject(link)
            link.Placement.Base = center
            entries.append({'name': prefix + label, 'file': filename,
                            'placement': vec_data(link.Placement)})
        assembly.recompute()
        assembly.save()
        report = {'status': 'COMPLETED', 'source': os.path.basename(SOURCE),
                  'assembly': assembly_filename, 'partCount': len(definitions),
                  'occurrenceCount': len(entries), 'occurrences': entries,
                  'scriptOptions': OPTIONS,
                  'note': 'App::Part root with App::Link instances. Keep all FCStd files together in the same directory.'}
        # Always write the machine-readable status file for the job runner.
        with open(RESULT, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2)
        if OPTIONS['generateReport']:
            with open(os.path.join(OUT, 'assembly_report.json'), 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2)
        print('Created %d part files and %d assembly occurrences' %
              (len(definitions), len(entries)), flush=True)
        os._exit(0)
    except BaseException:
        traceback.print_exc()
        os._exit(1)

QtCore.QTimer.singleShot(500, run)
'''


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: import_step_assembly.py INPUT_DIR OUTPUT_DIR')
    input_dir, output_dir = map(Path, sys.argv[1:])
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = input_dir / 'manifest.json'
    manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
    # Worker manifests vary by release. Job parameters may be nested or
    # serialized as JSON strings. Find the submitted scriptOptions wherever
    # the worker placed them, without executing or trusting arbitrary code.
    def find_script_options(node, depth=0):
        if depth > 8:
            return None
        if isinstance(node, str):
            try:
                node = json.loads(node)
            except (ValueError, TypeError):
                return None
        if not isinstance(node, dict):
            return None
        for key in ('scriptOptions', 'script_options'):
            if key in node:
                value = node[key]
                if isinstance(value, str):
                    value = json.loads(value)
                if not isinstance(value, dict):
                    raise ValueError('%s must be a JSON object' % key)
                return value
        for key in ('parameters', 'parametersJson', 'config', 'configuration',
                    'job', 'jobParameters', 'payload', 'options'):
            if key in node:
                found = find_script_options(node[key], depth + 1)
                if found is not None:
                    return found
        return None

    raw_options = find_script_options(manifest)
    if raw_options is None:
        # Support simple standalone manifests that contain option keys directly.
        recognized = ('outputName', 'prefix', 'partPrefix', 'splitAssembly',
                      'reuseIdenticalParts', 'generateReport')
        raw_options = {k: manifest[k] for k in recognized if k in manifest}
    print('Manifest top-level keys: %s' % sorted(manifest.keys()), flush=True)
    print('Submitted STEP script options: %s' %
          json.dumps(raw_options, sort_keys=True), flush=True)
    options = {}
    for key, default in [('splitAssembly', True), ('reuseIdenticalParts', True),
                         ('generateReport', True)]:
        value = raw_options.get(key, default)
        if isinstance(value, str):
            value = value.strip().lower()
            if value not in ('true', 'false', '1', '0', 'on', 'off', 'yes', 'no'):
                raise ValueError('%s must be a boolean' % key)
            value = value in ('true', '1', 'on', 'yes')
        if not isinstance(value, bool):
            raise ValueError('%s must be a boolean' % key)
        options[key] = value
    output_name = raw_options.get('outputName', 'assembly')
    if not isinstance(output_name, str) or not output_name.strip():
        raise ValueError('outputName must be a nonempty string')
    import re
    output_name = re.sub(r'\.FCStd$', '', output_name.strip(), flags=re.IGNORECASE)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', output_name):
        raise ValueError('outputName must be a safe filename (letters, numbers, _, -, .)')
    options['outputName'] = output_name
    prefix = raw_options.get('prefix', raw_options.get('partPrefix', raw_options.get('outputPrefix', '')))
    if prefix is None:
        prefix = ''
    if not isinstance(prefix, str) or len(prefix) > 80:
        raise ValueError('prefix must be a string of at most 80 characters')
    if prefix and not re.fullmatch(r'[A-Za-z0-9_.-]+', prefix):
        raise ValueError('prefix may contain only letters, numbers, _, -, .')
    options['prefix'] = prefix
    timeout_value = raw_options.get('executionTimeoutSeconds', 3600)
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, int) or not 1 <= timeout_value <= 86400:
        raise ValueError('executionTimeoutSeconds must be an integer from 1 to 86400')
    options['executionTimeoutSeconds'] = timeout_value
    print('STEP importer options: ' + json.dumps(options, sort_keys=True), flush=True)
    preferred = manifest.get('inputFile')
    if preferred:
        step = (input_dir / preferred).resolve()
        if not step.is_relative_to(input_dir.resolve()):
            raise ValueError('inputFile must be inside INPUT_DIR')
        if step.suffix.lower() not in ('.stp', '.step'):
            raise ValueError('inputFile must be STEP (.stp/.step)')
    else:
        steps = sorted(p for p in input_dir.rglob('*') if p.suffix.lower() in ('.step', '.stp'))
        if len(steps) != 1:
            raise ValueError('Expected exactly one STEP file, or manifest.json inputFile')
        step = steps[0]
    if not step.is_file():
        raise FileNotFoundError(step)
    result = output_dir / 'step_import_result.json'
    macro = output_dir / '_step_import.FCMacro'
    macro.write_text(MACRO.replace('__SOURCE__', repr(str(step)))
                          .replace('__OUT__', repr(str(output_dir.resolve())))
                          .replace('__RESULT__', repr(str(result.resolve())))
                          .replace('__OPTIONS__', repr(options)))
    cmd = ['xvfb-run', '-a', '-s', '-screen 0 1280x1024x24',
           os.environ.get('FREECAD_GUI', 'FreeCADGui'), str(macro)]
    env = dict(os.environ, QT_QPA_PLATFORM='xcb')
    try:
        completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, env=env,
                                   timeout=options["executionTimeoutSeconds"])
        print(completed.stdout, flush=True)
        if completed.returncode or not result.is_file():
            raise RuntimeError('STEP import failed (exit %s): %s' %
                               (completed.returncode, completed.stdout[-6000:]))
    finally:
        macro.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
