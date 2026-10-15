"""Deterministic MCPB 0.1–0.3 desktop bundle adapter.

Format: https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md
Reuses WISE's archive path validation. This is not a signature trust service:
SHA256 establishes reviewed file identity, not publisher authenticity.
No package scripts are evaluated while inspecting or extracting a bundle.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from .marketplace import package_path

MAX_UPLOAD=64_000_000
MAX_CONTENTS=256_000_000
MAX_STORE=1_024_000_000
TOKEN=re.compile(r'\$\{([^}]+)\}')


def publish_directory(source,destination):
    """Atomic publication; tolerate brief Windows sync/AV sharing locks only.

    Never change ACLs or publish partially extracted content as a fallback.
    """
    for attempt in range(6):
        try:return source.rename(destination)
        except PermissionError as error:
            if os.name!='nt' or getattr(error,'winerror',None) not in {None,5,32,33} or attempt==5:raise
            time.sleep(.05*(2**attempt))


def _text(value, limit=4096):
    if not isinstance(value,str) or len(value)>limit or any(ord(c)<32 for c in value):
        raise ValueError('Invalid bundle text')
    return value


def inspect(data):
    if not 0<len(data)<=MAX_UPLOAD: raise ValueError('Bundle exceeds 64 MB upload limit')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries=archive.infolist()
        if len(entries)>20000: raise ValueError('Too many bundle files')
        files={}; total=0; allpaths=set()
        for entry in entries:
            # ZipInfo silently normalizes Windows backslashes and truncates
            # NULs in filename; validate its original untrusted spelling.
            relative=package_path(entry.orig_filename.rstrip('/'))
            if not relative: raise ValueError('Invalid bundle file path')
            folded=relative.casefold()
            if folded in allpaths: raise ValueError('Duplicate bundle file path')
            allpaths.add(folded)
            mode=entry.external_attr>>16
            if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0,stat.S_IFREG,stat.S_IFDIR}:
                raise ValueError('Bundle links and special files are not supported')
            if entry.flag_bits&1: raise ValueError('Encrypted bundles are not supported')
            if entry.is_dir(): continue
            total+=entry.file_size
            if total>MAX_CONTENTS or entry.file_size>64_000_000: raise ValueError('Expanded bundle exceeds budget')
            files[relative]=entry
        # Also reject file/directory case aliases before touching the filesystem.
        foldedfiles={path.casefold() for path in files}
        for path in allpaths:
            parents=path.split('/')[:-1]
            if any('/'.join(parents[:i]) in foldedfiles for i in range(1,len(parents)+1)):
                raise ValueError('Conflicting bundle file paths')
        member=files.get('manifest.json')
        if not member or member.file_size>1_000_000: raise ValueError('Missing or oversized MCPB manifest.json')
        manifest=json.loads(archive.read(member).decode('utf-8-sig'))
        validate_manifest(manifest,files)
        return manifest,files,total


def validate_manifest(m, files):
    if not isinstance(m,dict) or m.get('manifest_version') not in {'0.1','0.2','0.3'}:
        raise ValueError('WISE currently supports MCPB manifest versions 0.1, 0.2 and 0.3')
    for key in ('name','version','description'): _text(m.get(key),600)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}',m['name']): raise ValueError('Invalid bundle name')
    if not isinstance(m.get('author'),dict): raise ValueError('Missing bundle author')
    _text(m['author'].get('name'),200)
    server=m.get('server',{})
    if not isinstance(server,dict) or server.get('type') not in {'node','python','binary'}: raise ValueError('Unsupported bundle runtime')
    entry=package_path(server.get('entry_point',''))
    if not entry or entry not in files: raise ValueError('Missing bundle entry point')
    compatibility=m.get('compatibility',{})
    if not isinstance(compatibility,dict): raise ValueError('Invalid bundle compatibility')
    if sys.platform not in compatibility.get('platforms',[sys.platform]): raise ValueError('Bundle does not support this operating system')
    settings=m.get('user_config',{})
    if not isinstance(settings,dict) or len(settings)>40: raise ValueError('Too many bundle settings')
    for name,field in settings.items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',name) or not isinstance(field,dict): raise ValueError('Invalid bundle setting')
        if field.get('type') not in {'string','number','boolean','file','directory'}: raise ValueError('Unsupported bundle setting type')
        _text(field.get('title',name),200)
        if field.get('sensitive') and field.get('type')!='string': raise ValueError('Secret settings must be strings')
        if field.get('sensitive') and field.get('default'): raise ValueError('Bundle must not contain default credentials')
    config=server.get('mcp_config',{})
    if not isinstance(config,dict): raise ValueError('Missing bundle MCP configuration')
    overrides=config.get('platform_overrides',{})
    if not isinstance(overrides,dict): raise ValueError('Invalid platform overrides')
    for variant in [config,*overrides.values()]:
        if not isinstance(variant,dict): raise ValueError('Invalid platform configuration')
        for name in variant:
            if name not in {'command','args','env','platform_overrides'}: raise ValueError('Unsupported MCPB configuration: '+name)
        if 'command' in variant: _text(variant['command'])
        if not isinstance(variant.get('args',[]),list) or len(variant.get('args',[]))>100: raise ValueError('Invalid bundle arguments')
        environment=variant.get('env',{})
        if not isinstance(environment,dict) or len(environment)>80: raise ValueError('Invalid bundle environment')
        for key,value in environment.items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key): raise ValueError('Invalid environment name')
            _text(value)
        for value in [variant.get('command',''),*variant.get('args',[]),*environment.values()]:
            _text(value)
            if '${' in TOKEN.sub('',value): raise ValueError('Malformed bundle variable')
            for variable in TOKEN.findall(value):
                if variable not in {'__dirname','HOME','DOCUMENTS','DESKTOP'} and not (variable.startswith('user_config.') and variable[12:] in settings):
                    raise ValueError('Unknown bundle variable')
                if variable.startswith('user_config.') and settings[variable[12:]].get('sensitive') and value in variant.get('args',[]):
                    raise ValueError('Secrets must be in the environment, not process arguments')


def stage(manager,data):
    manifest, files, size=inspect(data)
    digest=hashlib.sha256(data).hexdigest(); key='bundle_'+digest[:24]
    destination=manager.root/'mcp-bundles'/digest
    if not destination.resolve().is_relative_to(manager.root.resolve()): raise ValueError('Unsafe bundle destination')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.is_symlink(): raise ValueError('Unsafe bundle destination')
    if not destination.exists():
        used=0
        for marker in destination.parent.glob('*/.wise-bundle.json'):
            if not marker.resolve().is_relative_to(destination.parent.resolve()) or marker.stat().st_size>4_000_000:
                raise ValueError('Unsafe bundle cache')
            record=json.loads(marker.read_text(encoding='utf-8'))
            used+=record.get('bytes',MAX_CONTENTS)
        if used+size>MAX_STORE: raise ValueError('Local bundle cache exceeds its 1 GB budget')
        temp=Path(tempfile.mkdtemp(prefix='staging-',dir=destination.parent))
        try:
            hashes={}
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for relative,member in files.items():
                    target=temp/relative; target.parent.mkdir(parents=True,exist_ok=True)
                    content=archive.read(member)
                    if len(content)!=member.file_size: raise ValueError('Invalid bundle member size')
                    target.write_bytes(content); hashes[relative]=hashlib.sha256(content).hexdigest()
                    if os.name!='nt' and stat.S_IMODE(member.external_attr>>16)&0o111: target.chmod(0o700)
            # Manifest cannot provide/overwrite this WISE-owned marker.
            if any(p.casefold()=='.wise-bundle.json' for p in files): raise ValueError('Reserved bundle file')
            from .manager import atomic
            atomic(temp/'.wise-bundle.json',dict(sha256=digest,files=hashes,bytes=size))
            publish_directory(temp,destination)
        finally:
            if temp.exists(): shutil.rmtree(temp)
    fields=[]
    for name,f in manifest.get('user_config',{}).items():
        fields.append(dict(id=name,name=f.get('title',name),label=f.get('title',name),required=bool(f.get('required')),secret=bool(f.get('sensitive')),
            default=f.get('default',''),choices=[],type=f['type'],multiple=bool(f.get('multiple')),min=f.get('min'),max=f.get('max')))
    docs=manifest.get('documentation') or manifest.get('homepage') or ''
    if docs:
        from .catalog import public_https
        docs=public_https(docs)
    entry=dict(id=key,name=manifest.get('display_name') or manifest['name'],display_name=manifest.get('display_name') or manifest['name'],
        publisher=manifest['author']['name'],publisher_id=manifest['author']['name'],description=manifest['description'],
        version=manifest['version'],source='bundle',categories=['Local packages'],category='Local packages',logo='',docs_url=docs,fields=fields,
        recipe=dict(kind='bundle',sha256=digest),unsupported='',connection_type='bundle',auth_mode='local',download_required=True,
        setup_note='An imported local bundle. Review its publisher before allowing code to run. SHA256 verifies file identity, not publisher trust.',
        bundle_sha256=digest,bundle_bytes=size)
    with manager.lock:
        if key in manager.installed: return manager.detail(key)
        old={r['id']:r for r in manager.cache.get('items',[])};old[key]=entry
        manager.cache['items']=list(old.values())
        from .manager import atomic
        atomic(manager.root/'plugin_catalog.json',manager.cache)
    return manager.detail(key)


def verify_installed(folder,digest):
    folder=Path(folder)
    if not re.fullmatch(r'[a-f0-9]{64}',digest): raise ValueError('Invalid bundle identity')
    if folder.is_symlink(): raise ValueError('Unsafe bundle folder')
    marker=json.loads((folder/'.wise-bundle.json').read_text(encoding='utf-8'))
    if marker.get('sha256')!=digest: raise ValueError('Bundle identity changed; import it again')
    actual=set()
    for path in folder.rglob('*'):
        if path.is_symlink(): raise ValueError('Bundle contains a link; import it again')
        if path.is_file(): actual.add(path.relative_to(folder).as_posix())
        if len(actual)>20001: raise ValueError('Bundle contents exceed budget')
    if actual!=set(marker['files'])|{'.wise-bundle.json'}: raise ValueError('Bundle contents changed; import it again')
    # Revalidate every installed file before executing, including paths/links.
    for relative,checksum in marker['files'].items():
        target=folder/package_path(relative)
        if target.is_symlink() or not target.resolve().is_relative_to(folder.resolve()) or not target.is_file() or target.stat().st_size>64_000_000:
            raise ValueError('Bundle file changed; import it again')
        if hashlib.sha256(target.read_bytes()).hexdigest()!=checksum: raise ValueError('Bundle file changed; import it again')
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8-sig'));validate_manifest(m,marker['files'])
    return m


def resolve(entry,root,fields):
    digest=entry['recipe']['sha256']
    folder=Path(root)/'mcp-bundles'/digest
    if not folder.resolve().is_relative_to(Path(root).resolve()): raise ValueError('Unsafe bundle folder')
    m=verify_installed(folder,digest)
    settings=m.get('user_config',{}); values={}
    if set(fields)-set(settings): raise ValueError('Unknown bundle settings')
    for name,f in settings.items():
        default=f.get('default',False if f['type']=='boolean' else [] if f.get('multiple') else '')
        if name not in fields:
            def default_value(text):
                if not isinstance(text,str): return text
                def substitute(match):
                    options={'HOME':str(Path.home()),'DOCUMENTS':str(Path.home()/'Documents'),'DESKTOP':str(Path.home()/'Desktop')}
                    if match[1] not in options: raise ValueError('Unsupported default setting variable')
                    return options[match[1]]
                return TOKEN.sub(substitute,text)
            default=[default_value(v) for v in default] if isinstance(default,list) else default_value(default)
        value=fields.get(name,default)
        if f.get('required') and (value=='' or value is None or value==[]): raise ValueError('Missing bundle setting: '+f.get('title',name))
        if not f.get('required') and (value=='' or value is None):
            values[name]=[] if f.get('multiple') else ''
            continue
        items=value if f.get('multiple') else [value]
        if not isinstance(items,list) or len(items)>40: raise ValueError('Invalid multiple setting')
        result=[]
        for item in items:
            kind=f['type']
            if kind=='boolean':
                if not isinstance(item,bool): raise ValueError('Expected boolean setting')
                result.append('true' if item else 'false')
            elif kind=='number':
                if isinstance(item,bool) or not isinstance(item,(int,float)) or not math.isfinite(item): raise ValueError('Expected number setting')
                if f.get('min') is not None and item<f['min'] or f.get('max') is not None and item>f['max']: raise ValueError('Number setting is outside its allowed range')
                result.append(str(item))
            else:
                text=_text(item)
                if '${' in text: raise ValueError('Variables are not accepted as user values')
                if text and kind in {'file','directory'}:
                    target=Path(text).expanduser()
                    if not target.is_absolute() or not (target.is_file() if kind=='file' else target.is_dir()): raise ValueError('Select an existing absolute '+kind+' path')
                    text=str(target.resolve())
                result.append(text)
        values[name]=result if f.get('multiple') else result[0]
    def replace(text,array=False):
        match=TOKEN.fullmatch(text)
        if array and match and match[1].startswith('user_config.') and isinstance(values[match[1][12:]],list): return values[match[1][12:]]
        def sub(match):
            variable=match[1]
            if variable.startswith('user_config.'):
                value=values[variable[12:]]
                if isinstance(value,list): return os.pathsep.join(value)
                return value
            return {'__dirname':str(folder.resolve()),'HOME':str(Path.home()),'DOCUMENTS':str(Path.home()/'Documents'),'DESKTOP':str(Path.home()/'Desktop')}[variable]
        return TOKEN.sub(sub,text)
    server=m['server'];base=server['mcp_config'];override=base.get('platform_overrides',{}).get(sys.platform,{})
    config={**base,**override,'env':{**base.get('env',{}),**override.get('env',{})}}
    command=replace(config.get('command',''))
    entrypoint=(folder/server['entry_point']).resolve()
    if server['type']=='node':
        if command not in {'node','${node}'}: raise ValueError('Node bundles must use the Node runtime')
        command=shutil.which('node') or ''
    elif server['type']=='python':
        if command not in {'python','python3'}: raise ValueError('Python bundles must use the Python runtime')
        command=sys.executable
    else:
        command=str((folder/command).resolve()) if not Path(command).is_absolute() else command
        if Path(command).resolve()!=entrypoint: raise ValueError('Binary bundles must execute their declared entry point')
    if not command: raise ValueError('Required bundle runtime is not installed')
    constraints=m.get('compatibility',{}).get('runtimes',{}).get(server['type'])
    if constraints:
        from packaging.specifiers import SpecifierSet,InvalidSpecifier
        import subprocess
        version='.'.join(map(str,sys.version_info[:3])) if server['type']=='python' else ''
        if server['type']=='node':
            result=subprocess.run([command,'--version'],capture_output=True,text=True,timeout=5,
                **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
            version=result.stdout.strip().removeprefix('v')
        try:
            if not version or version not in SpecifierSet(','.join(str(constraints).split())): raise ValueError('Installed runtime does not satisfy bundle requirements')
        except InvalidSpecifier: raise ValueError('Unsupported runtime version range; use explicit comparison ranges') from None
    args=[]
    for argument in config.get('args',[]):
        value=replace(argument,True);args.extend(value if isinstance(value,list) else [value])
    if server['type']!='binary' and not any(Path(arg).is_absolute() and Path(arg).resolve()==entrypoint or (folder/arg).resolve()==entrypoint for arg in args):
        raise ValueError('Bundle command must launch its declared entry point')
    environment={k:value for k,v in config.get('env',{}).items() if (value:=replace(v))}
    environment['PYTHONDONTWRITEBYTECODE']='1'
    return dict(transport='stdio',command=command,args=args,env=environment,
                cwd=str(folder.resolve()),bundle_sha256=digest,sandbox=False,init_timeout=60,call_timeout=30)
