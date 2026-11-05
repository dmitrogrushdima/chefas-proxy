"""Build a source-free runtime directory; never compile extensions in place.

Usage: python build_ext.py [--output dist/kontur] [--generate-only]
Requires requirements-build.txt and a platform C compiler / Python headers.
"""
import argparse
import hashlib
import importlib.metadata
import importlib.machinery
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent
MODULES = ('proxy','router','devfee_scheduler','pow_validator','history_store','stratum_session',
           'upstream_manager','observer','dashboard_server','dashboard_control','secret_store','pilot_logging','shutdown_signals','stratum_codec','hardware_compatibility')
LAUNCHER = '''"""Launch the compiled KONTUR runtime; source fallback is rejected."""
import importlib
from importlib.machinery import EXTENSION_SUFFIXES

MODULES = {modules!r}

def main():
    for name in MODULES:
        module = importlib.import_module(name)
        if not any(str(module.__file__).endswith(suffix) for suffix in EXTENSION_SUFFIXES):
            raise SystemExit("Compiled module required: " + name)
    importlib.import_module('proxy').main()

if __name__ == '__main__':
    main()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT/'bin'/'kontur'))
    parser.add_argument('--generate-only', action='store_true', help='Validate Cython translation without invoking the native compiler')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        parser.error('Output already exists; choose a fresh directory (nothing is deleted automatically)')
    from setuptools import setup, Extension
    from Cython.Build import cythonize
    from Cython.Compiler import Options
    Options.docstrings = False
    build = (ROOT/'build').resolve()
    build.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=build, prefix='release-')).resolve()
    if stage.parent != build:
        raise RuntimeError('Unexpected staging path')
    (build/'sources').mkdir(exist_ok=True)
    for name in MODULES:
        source, destination = ROOT/'src'/(name+'.py'), build/'sources'/(name+'.py')
        if not destination.exists() or source.read_bytes() != destination.read_bytes():
            shutil.copyfile(source, destination)
    # Short relative C/object paths avoid MSVC MAX_PATH duplication of the
    # entire workspace path (including OneDrive and non-ASCII directories).
    os.chdir(build)
    extensions = cythonize([Extension(name, [str(Path('sources')/(name+'.py'))]) for name in MODULES],
        build_dir='cython', compiler_directives={'language_level':3, 'annotation_typing':False,
        'binding':True, 'embedsignature':False, 'emit_code_comments':False})
    if args.generate_only:
        print('Cython translation complete for ' + str(len(extensions)) + ' modules; native artifacts not built')
        return
    setup(name='kontur-core', version='0.1.0rc1', ext_modules=extensions,
          script_args=['build_ext', '--build-lib', str(stage), '--build-temp', 'objects'])
    for name in MODULES:
        if not any((stage/(name+suffix)).is_file() for suffix in importlib.machinery.EXTENSION_SUFFIXES):
            raise RuntimeError('Missing compiled artifact: '+name)
    (stage/'launch.py').write_text(LAUNCHER.format(modules=MODULES), encoding='utf-8')
    (stage/'dashboard').mkdir()
    for asset in ('index.html','app.css','app.js','i18n.js'):
        shutil.copyfile(ROOT/'dashboard'/asset, stage/'dashboard'/asset)
    for filename in ('requirements.txt','.env.example'):
        shutil.copyfile(ROOT/filename,stage/filename)
    for filename in ('start.sh','start.bat'):
        shutil.copy2(ROOT/filename,stage/filename)
    if os.name != 'nt':
        (stage/'start.sh').chmod(0o755)
    manifest = {'python':platform.python_version(),'platform':platform.platform(),'modules':list(MODULES),
                'Cython':importlib.metadata.version('Cython'),
                'source_sha256':{name:hashlib.sha256((build/'sources'/(name+'.py')).read_bytes()).hexdigest() for name in MODULES},
                'files':{str(path.relative_to(stage)).replace('\\','/'): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in stage.rglob('*') if path.is_file()}}
    (stage/'build-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    if any(path.suffix == '.py' and path.name != 'launch.py' for path in stage.rglob('*')):
        raise RuntimeError('Unexpected Python source in release')
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():
        raise RuntimeError('Output appeared during build; staging retained')
    os.rename(stage, output)
    print('Compiled runtime: '+str(output))


if __name__ == '__main__':
    main()
