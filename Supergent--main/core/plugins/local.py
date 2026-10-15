"""Reviewed local MCP recipes and WISE-owned dependency environments.

No README-to-shell generation, postinstall hooks, user-profile browser reuse,
or credential scavenging. Installation runs only after explicit code trust.
Versions checked against npm/PyPI on 2026-10-11; updates require a new review.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
from .catalog import identifier

LOCAL_PACKAGES = [
    ('local-filesystem', 'Local files', 'npm', '@modelcontextprotocol/server-filesystem', '1.0.0',
     'https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem', 'Productivity',
     'Read and edit files in a folder you choose.', [('directory', 'Allowed folder', True)], []),
    ('local-playwright', 'Playwright', 'npm', '@playwright/mcp', '0.0.83',
     'https://github.com/microsoft/playwright-mcp', 'Development',
     'Automate an isolated browser session without using your signed-in profile.', [], ['--headless', '--isolated']),
    ('local-git', 'Git repositories', 'pypi', 'mcp-server-git', '2026.10.10',
     'https://github.com/modelcontextprotocol/servers/tree/main/src/git', 'Development',
     'Inspect and work with a local Git repository.', [('repository', 'Repository folder', True)], ['--repository']),
    ('local-fetch', 'Web Fetch', 'pypi', 'mcp-server-fetch', '2026.10.10',
     'https://github.com/modelcontextprotocol/servers/tree/main/src/fetch', 'Research',
     'Retrieve public web pages through the reference MCP server.', [], []),
]


def local_rows():
    rows = []
    for key, name, registry, package, version, docs, category, description, inputs, args in LOCAL_PACKAGES:
        rows.append(dict(id=key, name=name, display_name=name, publisher='Microsoft' if key=='local-playwright' else 'MCP reference servers',
            publisher_id=package, version=version, source='featured', categories=[category], category=category,
            description=description, docs_url=docs, logo='https://playwright.dev/img/playwright-logo.svg' if key=='local-playwright' else '',
            fields=[dict(id=k,name=label,label=label,required=required,secret=False,default='',choices=[],type='directory' if k!='database' else 'file') for k,label,required in inputs],
            recipe=dict(kind='local-package',registry=registry,package=package,version=version,args=args),
            connection_type='local-package',auth_mode='local',download_required=True,unsupported='',
            setup_note='Installs a pinned package in a WISE-owned environment. Runs with your account permissions, not administrator privileges.'))
    return rows


def _run(argv, *, directory, timeout=240):
    # Never forward model/service credentials, proxy settings or package-index overrides.
    keys={'PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','HOME','USERPROFILE','APPDATA','LOCALAPPDATA'}
    env={k:v for k,v in os.environ.items() if k.upper() in keys}
    env.update(PYTHONUTF8='1',PIP_CONFIG_FILE=os.devnull,PIP_DISABLE_PIP_VERSION_CHECK='1')
    # npm treats two references to NUL as a duplicate config on Windows.
    # Distinct empty owned files also prevent accidental use of ~/.npmrc.
    for name in ('npm-user.conf','npm-global.conf'):
        (directory/name).write_text('',encoding='utf-8')
    env.update(NPM_CONFIG_USERCONFIG=str(directory/'npm-user.conf'),NPM_CONFIG_GLOBALCONFIG=str(directory/'npm-global.conf'),NPM_CONFIG_CACHE=str(directory.parent/'npm-cache'))
    kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    # Keep bounded installer diagnostics in the owned runtime, not the UI.
    tail=bytearray()
    import threading
    def drain(pipe):
        while True:
            chunk=pipe.read(4096)
            if not chunk:break
            tail.extend(chunk)
            if len(tail)>64000:del tail[:-64000]
    try:
        process=subprocess.Popen(argv,cwd=directory,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,**kwargs)
        reader=threading.Thread(target=drain,args=(process.stdout,),daemon=True);reader.start()
        try:code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill();process.wait();raise
        finally:
            reader.join(timeout=3)
            (directory/'install.log').write_bytes(bytes(tail))
    except (OSError,subprocess.TimeoutExpired):
        raise ValueError('Local dependency installation could not finish; check internet access and the required runtime, then retry') from None
    if code:
        raise ValueError('Local dependency installation failed; check internet access and the required runtime, then retry')


def prepare_package(entry, root, values):
    """Install only an exact reviewed tuple; untrusted directory metadata cannot select commands."""
    reviewed=next((row for row in local_rows() if row['id']==entry['id']),None)
    if not reviewed or reviewed['recipe']!=entry['recipe']:
        raise ValueError('Unreviewed local package recipe')
    recipe=reviewed['recipe']; args=list(recipe['args'])
    for field in reviewed['fields']:
        value=values.get(field['id'],'')
        target=Path(value).expanduser()
        if not target.is_absolute() or not (target.is_dir() if field['type']=='directory' else target.is_file()):
            raise ValueError('Choose an existing absolute '+field['type']+' path')
        args.append(str(target.resolve()))
    if entry['id']=='local-playwright':
        brave=Path.home()/'AppData/Local/BraveSoftware/Brave-Browser/Application/brave.exe'
        if not brave.is_file():
            raise ValueError('Install Brave before connecting Playwright; WISE does not silently download a browser')
        args += ['--executable-path',str(brave)]
    return install_package(recipe,root,args)


def prepare_registry_package(entry,root,args,env):
    """Use the same isolated installer for a user-trusted registry package.

    The parser validates identities/versions/arguments. Recheck here as this
    function is an execution boundary, not a claim of publisher trust.
    """
    import re
    recipe=entry['recipe'];package=recipe.get('package','');version=recipe.get('version','')
    if recipe.get('registry') not in {'npm','pypi'} or not re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,100}',version):
        raise ValueError('Unsupported package identity')
    pattern=r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*' if recipe['registry']=='npm' else r'[A-Za-z0-9][A-Za-z0-9._-]*'
    if not re.fullmatch(pattern,package): raise ValueError('Unsupported package identity')
    identity=dict(kind='local-package',registry=recipe['registry'],package=package,version=version,args=[])
    result=install_package(identity,root,args)
    result['env']=env
    return result


def install_package(recipe,root,args):
    args=list(args)
    envroot=Path(root)/'mcp-runtimes'/identifier(recipe['package']+'@'+recipe['version'])
    if envroot.is_symlink() or not envroot.resolve().is_relative_to(Path(root).resolve()): raise ValueError('Unsafe dependency environment')
    envroot.mkdir(parents=True,exist_ok=True)
    marker=envroot/'wise-package.json'
    if marker.exists():
        saved=json.loads(marker.read_text(encoding='utf-8'))
        if any(saved.get(k)!=recipe[k] for k in ('registry','package','version')): raise ValueError('Dependency environment identity changed')
    if recipe['registry']=='npm':
        node=shutil.which('node'); npm=shutil.which('npm')
        script=Path(npm).parent/'node_modules/npm/bin/npm-cli.js' if npm else None
        if not node or not script or not script.is_file(): raise ValueError('Install Node.js with npm to use this connection')
        if not marker.is_file():
            _run([node,str(script),'install','--ignore-scripts','--no-audit','--no-fund','--registry=https://registry.npmjs.org',
                  '--prefix',str(envroot),recipe['package']+'@'+recipe['version']],directory=envroot)
        package_root=envroot/'node_modules'/recipe['package']
        manifest=json.loads((package_root/'package.json').read_text(encoding='utf-8'))
        if manifest['name']!=recipe['package'] or manifest['version']!=recipe['version']: raise ValueError('Installed package identity differs from the reviewed version')
        binary=manifest['bin']; binary=next(iter(binary.values())) if isinstance(binary,dict) and len(binary)==1 else binary
        if not isinstance(binary,str): raise ValueError('Package has no unique MCP entry point')
        from .marketplace import package_path
        executable=package_root/package_path(binary)
        if not executable.is_file() or not executable.resolve().is_relative_to(package_root.resolve()): raise ValueError('Invalid package entry point')
        command=node; args=[str(executable.resolve()),*args]
    else:
        python=envroot/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        if not marker.is_file():
            venv.EnvBuilder(with_pip=True).create(envroot)
            _run([str(python),'-m','pip','install','--index-url','https://pypi.org/simple','--only-binary=:all:',recipe['package']+'=='+recipe['version']],directory=envroot)
        command=str(envroot/('Scripts/'+recipe['package']+'.exe' if os.name=='nt' else 'bin/'+recipe['package']))
        if not Path(command).is_file(): raise ValueError('Installed package has no MCP executable')
    if not marker.is_file():
        from .manager import atomic
        atomic(marker,recipe)
    return dict(transport='stdio',command=command,args=args,env={},sandbox=False,init_timeout=60,call_timeout=30)


def local_app_probe(entry):
    """Read-only port reachability is readiness, NEVER a connected/authorized status."""
    if entry['id']!='figma-desktop' or entry['recipe']['url']!='http://127.0.0.1:3845/mcp':
        raise ValueError('Unreviewed application bridge')
    import socket
    try:
        with socket.create_connection(('127.0.0.1',3845),timeout=.5): reachable=True
    except OSError: reachable=False
    return dict(ready=reachable,connected=False,message='Bridge reachable; press Connect to verify MCP and grant access' if reachable
                else 'Open Figma Desktop, enter Dev Mode and enable its MCP server, then check again')
