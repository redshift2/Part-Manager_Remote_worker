#!/usr/bin/env python3
"""Export existing FreeCAD TechDraw pages to PDF.

Usage: python3 convert_drawing_pdf.py INPUT_DIR OUTPUT_DIR
Input: manifest.json, part.FCStd. Output: drawing-*.pdf, drawing_result.json.
No TechDraw pages is a successful, non-error result.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HELPER = r'''import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore
import json
import os
import re
import traceback

source = __SOURCE__
output = __OUTPUT__
result_path = __RESULT__

# TechDraw GUI items (dimensions, annotations and centerlines) can be
# populated asynchronously after opening a document.  Export only after
# activating the page and giving the Qt event loop time to paint it.
WAIT_MS = 2500

def export_pages():
    doc = None
    exit_code = 0
    try:
        doc = App.openDocument(source)
        doc.recompute()
        pages = [o for o in doc.Objects if o.TypeId in ('TechDraw::DrawPage', 'TechDraw::DrawPagePy')]
        result = {'status': 'COMPLETED', 'message': 'No drawing created', 'drawings': []}
        if pages:
            import TechDrawGui
            from PySide import QtWidgets
            for index, page in enumerate(pages, 1):
                # Show the actual TechDraw GUI page rather than exporting from
                # a document that has never displayed its drawing scene.
                Gui.activeDocument().getObject(page.Name).show()
                doc.recompute()
                # Process queued GUI/TechDraw updates before the export.
                deadline = __import__('time').monotonic() + WAIT_MS / 1000.0
                while __import__('time').monotonic() < deadline:
                    QtWidgets.QApplication.processEvents()
                    __import__('time').sleep(0.05)
                name = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(page.Label)).strip('._') or 'Drawing'
                filename = 'drawing-{:03d}-{}.pdf'.format(index, name[:70])
                target = os.path.join(output, filename)
                TechDrawGui.exportPageAsPdf(page, target)
                if not os.path.isfile(target) or os.path.getsize(target) == 0:
                    raise RuntimeError('Empty PDF for ' + page.Label)
                with open(target, 'rb') as f:
                    if not f.read(5).startswith(b'%PDF-'):
                        raise RuntimeError('Invalid PDF for ' + page.Label)
                result['drawings'].append({'pageName': page.Name, 'pageLabel': page.Label,
                                           'file': filename, 'bytes': os.path.getsize(target)})
            result['message'] = '{} drawing(s) created'.format(len(result['drawings']))
        with open(result_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2)
        print(result['message'], flush=True)
    except BaseException:
        traceback.print_exc()
        exit_code = 1
    finally:
        if doc is not None:
            try:
                App.closeDocument(doc.Name)
            except Exception:
                pass
        os._exit(exit_code)

QtCore.QTimer.singleShot(1000, export_pages)
'''


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: convert_drawing_pdf.py INPUT_DIR OUTPUT_DIR')
    source, output = map(Path, sys.argv[1:3])
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('inputFile') != 'part.FCStd':
        raise ValueError('Expected manifest inputFile=part.FCStd')
    if manifest.get('jobType') != 'CAD_TO_DRW':
        raise ValueError('Expected jobType CAD_TO_DRW')
    cad = source / 'part.FCStd'
    if not cad.is_file():
        raise FileNotFoundError(cad)
    result_path = output / 'drawing_result.json'
    helper_path = output / '_freecad_pdf_export.FCMacro'
    for old in output.glob('drawing-*.pdf'):
        old.unlink()
    result_path.unlink(missing_ok=True)
    helper_path.write_text(HELPER.replace('__SOURCE__', repr(str(cad.resolve())))
                                  .replace('__OUTPUT__', repr(str(output.resolve())))
                                  .replace('__RESULT__', repr(str(result_path.resolve()))), encoding='utf-8')
    try:
        cmd = ['xvfb-run', '-a', '-s', '-screen 0 1280x1024x24',
               os.environ.get('FREECAD_GUI', 'FreeCADGui'), str(helper_path)]
        env = os.environ.copy()
        env['QT_QPA_PLATFORM'] = 'xcb'
        process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, timeout=900, env=env)
        print(process.stdout, flush=True)
        if process.returncode != 0 or not result_path.is_file():
            raise RuntimeError('FreeCAD drawing export failed (exit {}): {}'.format(
                process.returncode, process.stdout[-6000:]))
        result = json.loads(result_path.read_text(encoding='utf-8'))
        result.update({k: manifest[k] for k in ('partId', 'cadVersion', 'cadSha1') if k in manifest})
        result_path.write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(result['message'], flush=True)
    finally:
        helper_path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
