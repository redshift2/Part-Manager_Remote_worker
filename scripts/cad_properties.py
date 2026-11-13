#!/usr/bin/env python3
"""Parts Manager CAD_PROPERTIES worker. Usage: python3 cad_properties.py INPUT_DIR OUTPUT_DIR.
Requires FreeCADCmd (or FREECAD_CMD) and a job package containing manifest.json and part.FCStd.
Outputs metadata.json, bom.json and (for assemblies) assembly.asmj.
No server-side PLM changes are made by this script.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

HELPER = r'''import FreeCAD as App
import json
import traceback
from collections import OrderedDict

SOURCE = __SOURCE__
OUTPUT = __OUTPUT__
ERROR = __ERROR__

def safe(value):
    try:
        if value is None: return None
        if isinstance(value, (str, int, float, bool)): return value
        if hasattr(value, 'Value'): return safe(value.Value)
        if hasattr(value, 'Name'): return str(value.Name)
        return str(value)
    except Exception: return None

def property_value(obj, keys):
    if obj is None: return None
    names = set(getattr(obj, 'PropertiesList', []) or [])
    for key in keys:
        if key in names:
            try:
                val = safe(obj.getPropertyByName(key))
                if val not in (None, ''): return val
            except Exception: pass
    return None

def material(obj):
    # FreeCAD material properties vary by workbench/version. Preserve the
    # actual assigned material identifier instead of guessing from appearance.
    for candidate in (obj, getattr(obj, 'LinkedObject', None)):
        if candidate is None: continue
        for key in ('Material', 'MaterialName', 'MaterialCard', 'ShapeMaterial'):
            if key not in (getattr(candidate, 'PropertiesList', []) or []): continue
            try:
                value = candidate.getPropertyByName(key)
                if isinstance(value, dict):
                    return {'property':key, 'name':safe(value.get('Name') or value.get('name')),
                            'uuid':safe(value.get('UUID') or value.get('Uuid')),
                            'density':safe(value.get('Density'))}
                if hasattr(value, 'Material') and isinstance(value.Material, dict):
                    d=value.Material
                    return {'property':key,'name':safe(d.get('Name') or getattr(value,'Label',None)),
                            'uuid':safe(d.get('UUID') or d.get('Uuid')),
                            'density':safe(d.get('Density'))}
                if hasattr(value, 'Label'):
                    return {'property':key,'name':safe(value.Label),'reference':safe(getattr(value,'Name',None))}
                if value not in (None, ''): return {'property':key,'name':safe(value)}
            except Exception: pass
    return None

def shape_volume(obj):
    try:
        shape=getattr(obj,'Shape',None)
        if shape is not None and not shape.isNull() and len(shape.Solids)>0:
            return float(shape.Volume)
    except Exception: pass
    return None

def is_assembly(obj):
    if obj is None: return False
    t=str(getattr(obj,'TypeId',''))
    if t == 'Assembly::AssemblyObject' or t.startswith('Assembly::Assembly'): return True
    if bool(property_value(obj,['IsAssembly'])): return True
    return False

def link_target(obj):
    try:
        if str(getattr(obj,'TypeId',''))=='App::Link':
            return getattr(obj,'LinkedObject',None)
    except Exception: pass
    return None

def children(obj):
    # FreeCAD 1.1 Assembly workbench uses several forms of membership.
    # Group and OutList are often different; inspect both, in order.
    result=[]
    seen=set()
    for prop in ('Group','ElementList','OutList'):
        try:
            value=getattr(obj,prop,[]) or []
            for child in value:
                if child is None or child is obj: continue
                key=id(child)
                if key not in seen:
                    result.append(child); seen.add(key)
        except Exception: pass
    return result

def is_geometry(obj):
    return shape_volume(obj) is not None

def is_container(obj):
    t=str(getattr(obj,'TypeId',''))
    return is_assembly(obj) or t in ('App::Part','App::DocumentObjectGroup','App::LinkGroup')

def part_id(obj):
    for target in (obj,link_target(obj)):
        v=property_value(target,['PlmPartId','PLMPartId','plmPartId','PartId','partId'])
        if v is not None:
            try:
                n=int(v)
                if n>0: return n
            except (TypeError,ValueError): pass
    return None

def transform(obj):
    try:
        p=obj.Placement
        return {'position':[float(p.Base.x),float(p.Base.y),float(p.Base.z)],
                'quaternion':[float(p.Rotation.Q[0]),float(p.Rotation.Q[1]),
                              float(p.Rotation.Q[2]),float(p.Rotation.Q[3])],
                'scale':[1,1,1]}
    except Exception:
        return {'position':[0,0,0], 'quaternion':[0,0,0,1], 'scale':[1,1,1]}

def units(doc):
    try:
        schema=int(App.ParamGet('User parameter:BaseApp/Preferences/Units').GetInt('UserSchema',0))
    except Exception: schema=None
    custom=property_value(doc,['Units','Unit','LengthUnit','GeometryUnits'])
    for obj in doc.Objects:
        if custom: break
        if str(getattr(obj,'Name','')).lower() in ('metadata','documentinfo'):
            custom=property_value(obj,['Units','Unit','LengthUnit','GeometryUnits'])
    return {'geometryLength':'mm','geometryVolume':'mm3',
            'declaredDisplayUnit':str(custom) if custom else None,
            'workerDisplaySchema':schema,
            'note':'FreeCAD geometry is stored in mm; the worker display schema is not necessarily the authoring schema.'}

def occurrence_key(obj):
    pid=part_id(obj)
    if pid is not None: return 'plm:'+str(pid)
    target=link_target(obj) or obj
    # Stable within a CAD document, and shared by repeated App::Link instances.
    return 'cad:'+str(getattr(getattr(target,'Document',None),'Name',''))+':'+str(target.Name)

def component_name(occurrence,target):
    # Linked object's name identifies the actual component, not the occurrence.
    for obj in (target,occurrence):
        for attr in ('Label','Name'):
            value=getattr(obj,attr,None)
            if value: return str(value)
    return 'Unnamed component'

def is_auxiliary(obj):
    t=str(getattr(obj,'TypeId',''))
    name=str(getattr(obj,'Name','')).lower()
    if t.startswith(('App::Origin','PartDesign::CoordinateSystem')): return True
    if any(k in t.lower() for k in ('joint','constraint','datum','origin','coordinate')): return True
    if name in ('origin','constraints','joints','jointgroup'): return True
    return False

def has_component_content(obj,depth=0,visited=None):
    if obj is None or depth>15: return False
    if is_geometry(obj): return True
    if visited is None: visited=set()
    key=id(obj)
    if key in visited: return False
    visited.add(key)
    for child in children(obj):
        if is_auxiliary(child): continue
        if has_component_content(link_target(child) or child,depth+1,visited): return True
    return False

def collect_top_level(root):
    # Traverse structural wrappers only. App::Link is always a single BOM
    # occurrence, even when its target contains an entire nested assembly.
    result=[]
    active=set()
    def walk(node,depth=0):
        if depth>40: raise RuntimeError('Excessively nested assembly containers')
        key=id(node)
        if key in active: return
        active.add(key)
        try:
            for item in children(node):
                if is_auxiliary(item): continue
                target=link_target(item) or item
                if is_auxiliary(target): continue
                if link_target(item) is not None:
                    result.append((item,target))
                elif is_assembly(target):
                    result.append((item,target))
                elif str(getattr(target,'TypeId',''))=='App::Part':
                    # An App::Part holding a solid is one part; an App::Part
                    # containing assemblies/links is an assembly component.
                    if has_component_content(target): result.append((item,target))
                    else: walk(target,depth+1)
                elif is_geometry(target):
                    # Body is a component; don't include its features again.
                    result.append((item,target))
                elif is_container(target) or children(target):
                    walk(target,depth+1)
        finally:
            active.remove(key)
    walk(root)
    # FreeCAD's Assembly object may expose members only through InList.
    return result

def component_is_assembly(occurrence,target):
    if is_assembly(target): return True
    if str(getattr(target,'TypeId',''))=='App::Part':
        return any(is_assembly(link_target(c) or c) or
                   str(getattr(c,'TypeId',''))=='App::Link'
                   for c in children(target))
    return False

def volume_of_part(doc):
    # Count only leaf solids. Bodies and their features must not be summed twice.
    solids=[]
    for obj in doc.Objects:
        if not is_geometry(obj): continue
        if str(getattr(obj,'TypeId',''))=='App::Link': continue
        parents=list(getattr(obj,'InList',[]) or [])
        if any(is_geometry(parent) and parent is not obj for parent in parents): continue
        solids.append(obj)
    if not solids:
        # A FreeCAD Body can have a Tip which is also in the document.
        bodies=[o for o in doc.Objects if str(getattr(o,'TypeId',''))=='PartDesign::Body' and is_geometry(o)]
        solids=bodies
    # Prefer bodies and top-level part containers; skip their features.
    bodies=[o for o in solids if str(getattr(o,'TypeId',''))=='PartDesign::Body']
    if bodies: solids=bodies
    return solids

def saved_assembly_components(source, root_name):
    """Read saved assembly membership and external XLinks without resolving files."""
    import zipfile
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(source) as archive:
        xml = ET.fromstring(archive.read('Document.xml'))
    objects = {o.get('name'): o for o in xml.findall('./ObjectData/Object')}
    types = {o.get('name'): o.get('type','') for o in xml.findall('./Objects/Object')}
    def prop(node, name):
        return node.find('./Properties/Property[@name="%s"]' % name) if node is not None else None
    def label(name):
        p=prop(objects.get(name),'Label')
        e=p.find('String') if p is not None else None
        return e.get('value') if e is not None and e.get('value') else name
    root=objects.get(root_name)
    group=prop(root,'Group')
    if group is None: return []
    links=group.findall('./LinkList/Link')
    items=[]
    for entry in links:
        name=entry.get('value')
        node=objects.get(name)
        if node is None: continue
        typ=types.get(name,'')
        if 'Joint' in typ or 'Origin' in typ or name.lower() in ('joints','constraints'): continue
        target=prop(node,'LinkedObject')
        xlink=target.find('XLink') if target is not None else None
        source_file=xlink.get('file') if xlink is not None else None
        target_name=xlink.get('name') if xlink is not None else None
        if typ!='App::Link' and not source_file: continue
        # App::Link stores its instance transform in LinkPlacement.  Placement
        # may be a proxy of the target object, so it is not authoritative for
        # the link occurrence. Read the saved XML instead of depending on an
        # external document being loadable in headless FreeCAD.
        def saved_placement(property_name):
            p=prop(node,property_name)
            e=p.find('PropertyPlacement') if p is not None else None
            if e is None: return None
            return {'position':[float(e.get(k,'0')) for k in ('Px','Py','Pz')],
                    'quaternion':[float(e.get(k,d)) for k,d in
                                  (('Q0','0'),('Q1','0'),('Q2','0'),('Q3','1'))]}
        link_place=saved_placement('LinkPlacement')
        object_place=saved_placement('Placement')
        selected=link_place if link_place is not None else object_place
        if selected is None:
            position=[0,0,0]; quaternion=[0,0,0,1]
            placement_source='missing'
        else:
            position=selected['position']; quaternion=selected['quaternion']
            placement_source='LinkPlacement' if link_place is not None else 'Placement'
        # ScaleVector is the per-instance link scale. Keep non-uniform scales.
        scale_prop=prop(node,'ScaleVector')
        scale_el=scale_prop.find('PropertyVector') if scale_prop is not None else None
        if scale_el is not None:
            scale=[float(scale_el.get(k,'1')) for k in ('valueX','valueY','valueZ')]
        else:
            uniform=prop(node,'Scale')
            number=uniform.find('Float') if uniform is not None else None
            factor=float(number.get('value','1')) if number is not None else 1.0
            scale=[factor]*3
        # Record both placement properties if they disagree; don't silently
        # substitute an identity transform when the model stores no movement.
        conflict=(link_place is not None and object_place is not None and
                  (any(abs(a-b)>1e-7 for a,b in zip(link_place['position'],object_place['position'])) or
                   any(abs(a-b)>1e-7 for a,b in zip(link_place['quaternion'],object_place['quaternion']))))
        items.append({'name':label(name),'cadObjectName':name,
                      'sourceFile':source_file,'linkedObjectName':target_name,
                      'position':position,'quaternion':quaternion,'scale':scale,
                      'placementSource':placement_source,'placementConflict':conflict,
                      'savedLinkPlacement':link_place,'savedObjectPlacement':object_place})
    return items

def saved_fallback(source, root):
    raw=saved_assembly_components(source,root.Name)
    bom=OrderedDict(); nodes=[]
    for item in raw:
        # External filename plus target object identifies repeated links to one component.
        key='external:'+str(item['sourceFile'] or '')+':'+str(item['linkedObjectName'] or item['cadObjectName'])
        if key not in bom:
            bom[key]={'key':key,'partId':None,'name':item['name'],
                      'cadObjectName':item['linkedObjectName'] or item['cadObjectName'],
                      'type':'unknown','quantity':0,'material':None,
                      'sourceFile':item['sourceFile']}
        bom[key]['quantity']+=1
        placement={k:item[k] for k in ('position','quaternion','scale')}
        nodes.append({'name':item['name'],'cadObjectName':item['cadObjectName'],
                      'componentKey':key,'componentType':'unknown',
                      'partId':None,'id':None,'source':None,
                      'type':'unresolvedComponent','geometryUnits':'mm',
                      'sourceFile':item['sourceFile'],'linkedObjectName':item['linkedObjectName'],
                      'transform':placement,'placementSource':item['placementSource'],
                      'placementConflict':item['placementConflict'],
                      'savedLinkPlacement':item['savedLinkPlacement'],
                      'savedObjectPlacement':item['savedObjectPlacement'],**placement})
    return bom,nodes

def run():
    doc=App.openDocument(SOURCE)
    try:
        roots=[o for o in doc.Objects if is_assembly(o)]
        # A nested assembly can appear as a link target rather than an OutList
        # child. Prefer the assembly not owned by any other assembly/container.
        nested=set()
        for assembly in roots:
            for child in children(assembly):
                target=link_target(child) or child
                if target is not assembly and is_assembly(target): nested.add(id(target))
        top=[o for o in roots if id(o) not in nested]
        root=top[0] if top else (roots[0] if roots else None)
        base={'schemaVersion':1,'jobType':'CAD_TO_META',
              'document':{'name':doc.Name,'label':doc.Label},'units':units(doc),
              'kind':'assembly' if root else 'part'}
        if root:
            bom=OrderedDict()
            nodes=[]
            for occurrence,target in collect_top_level(root):
                nested_assembly=component_is_assembly(occurrence,target)
                key=occurrence_key(occurrence)
                pid=part_id(occurrence)
                name=component_name(occurrence,target)
                if key not in bom:
                    bom[key]={'key':key,'partId':pid,'name':name,
                              'cadObjectName':target.Name,'type':'assembly' if nested_assembly else 'part',
                              'quantity':0,'material':None if nested_assembly else material(target)}
                bom[key]['quantity']+=1
                placement=transform(occurrence)
                node={'name':name,'cadObjectName':occurrence.Name,'componentKey':key,
                      'componentType':'assembly' if nested_assembly else 'part',
                      'partId':pid,'id':pid,'source':'plm' if pid is not None else None,
                      'type':'partInstance' if pid is not None and not nested_assembly else 'unresolvedComponent',
                      'geometryUnits':'mm','transform':placement,**placement}
                nodes.append(node)
            # FreeCADCmd can open an assembly without resolving external App::Link
            # targets. Its live Group may then appear empty; the FCStd XML still
            # records the exact immediate children and their source filenames.
            if not nodes:
                bom,nodes=saved_fallback(SOURCE,root)
            else:
                # Even if FreeCAD resolves links, the saved per-instance
                # LinkPlacement is the reliable persisted placement. Merge it
                # by occurrence name, without changing BOM membership.
                saved={item['cadObjectName']:item for item in saved_assembly_components(SOURCE,root.Name)}
                for node in nodes:
                    record=saved.get(node['cadObjectName'])
                    if record is None: continue
                    placement={k:record[k] for k in ('position','quaternion','scale')}
                    node.update(placement)
                    node['transform']=placement
                    node['placementSource']=record['placementSource']
                    node['placementConflict']=record['placementConflict']
                    node['savedLinkPlacement']=record['savedLinkPlacement']
                    node['savedObjectPlacement']=record['savedObjectPlacement']
                    node.setdefault('sourceFile',record['sourceFile'])
                    node.setdefault('linkedObjectName',record['linkedObjectName'])
            bom_data={'schemaVersion':1,'scope':'immediate-children',
                      'rootAssembly':str(root.Label),'items':list(bom.values())}
            viewer={'format':'PartsManagerAssembly','schemaVersion':2,
                    'name':str(root.Label),'units':'mm','nodes':nodes,
                    'metadata':{'origin':'CAD_TO_META',
                                'note':'Saved App::Link instance placements are preserved. An identity transform means the saved FreeCAD link placement is identity, not an estimated position. Unknown PLM IDs remain null.'}}
            base['assembly']={'name':str(root.Label),'cadObjectName':root.Name,
                              'directOccurrences':len(nodes),'distinctComponents':len(bom),
                              'bomFile':'bom.json','viewerFile':'assembly.asmj',
                              'placementWarnings':[n['name']+': no saved placement' for n in nodes if n.get('placementSource')=='missing']+
                                  [n['name']+': LinkPlacement differs from Placement' for n in nodes if n.get('placementConflict')]}
            write('bom.json',bom_data)
            write('assembly.asmj',viewer)
        else:
            solids=volume_of_part(doc)
            if not solids: raise RuntimeError('No measurable solid geometry in FreeCAD document')
            values=[shape_volume(o) for o in solids]
            total=sum(v for v in values if v is not None)
            materials=[{'cadObjectName':o.Name,'material':material(o),'volumeMm3':shape_volume(o)} for o in solids]
            base['part']={'volume':total,'volumeUnit':'mm3',
                          'volumeMm3':total,'solids':len(solids),'materials':materials,
                          'material':materials[0]['material'] if len(materials)==1 else None,
                          'mass':None,'massNote':'Calculate mass on the PLM server from volume and approved material density.'}
        write('metadata.json',base)
    finally:
        App.closeDocument(doc.Name)

def write(name,data):
    with open(OUTPUT+'/'+name,'w',encoding='utf-8') as f:
        json.dump(data,f,indent=2,ensure_ascii=False)

try: run()
except BaseException:
    with open(ERROR,'w',encoding='utf-8') as f: f.write(traceback.format_exc())
    raise
'''

def main():
    if len(sys.argv)!=3:
        raise SystemExit('Usage: cad_properties.py INPUT_DIR OUTPUT_DIR')
    source, output=map(Path,sys.argv[1:])
    output.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('jobType') not in ('CAD_TO_META', 'CAD_PROPERTIES') or manifest.get('inputFile')!='part.FCStd':
        raise ValueError('Expected CAD_TO_META or CAD_PROPERTIES manifest with inputFile=part.FCStd; received jobType={!r}, inputFile={!r}'.format(manifest.get('jobType'), manifest.get('inputFile')))
    cad=source/'part.FCStd'
    if not cad.is_file(): raise FileNotFoundError(cad)
    helper=output/'_cad_properties_freecad.py'
    error=output/'cad_properties_error.log'
    for name in ('metadata.json','bom.json','assembly.asmj','assembly.json','cad_properties_error.log'):
        (output/name).unlink(missing_ok=True)
    script=HELPER.replace('__SOURCE__',repr(str(cad.resolve()))).replace('__OUTPUT__',repr(str(output.resolve()))).replace('__ERROR__',repr(str(error.resolve())))
    helper.write_text(script,encoding='utf-8')
    try:
        cmd=[os.environ.get('FREECAD_CMD','FreeCADCmd'),str(helper)]
        done=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=900)
        if error.exists(): raise RuntimeError(error.read_text(encoding='utf-8'))
        if done.returncode!=0 or not (output/'metadata.json').is_file():
            raise RuntimeError('FreeCADCmd failed ({}): {}'.format(done.returncode,done.stdout[-8000:]))
        meta=json.loads((output/'metadata.json').read_text(encoding='utf-8'))
        meta['job']={key:manifest.get(key) for key in ('partId','cadVersion','cadSha1','eventId') if key in manifest}
        (output/'metadata.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8')
        print('{} completed: {}'.format(manifest['jobType'], meta['kind']))
    finally:
        helper.unlink(missing_ok=True)
        if not error.exists(): error.unlink(missing_ok=True)

if __name__=='__main__': main()