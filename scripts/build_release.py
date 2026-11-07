"""Compile and freeze a host-native x64 release. Existing output is never replaced."""
import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile

from generate_service import service_text

ROOT = Path(__file__).resolve().parents[1]


def imported_modules(paths):
    """Cython imports are opaque to PyInstaller; collect their explicit imports."""
    result = set()
    for path in paths:
        for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Import):
                result.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                result.add(node.module)
    return sorted(result)


def release_platform():
    machine = platform.machine().lower()
    if struct.calcsize("P") != 8 or machine not in ("amd64", "x86_64", "x64"):
        raise ValueError("Only native x64 Python on Windows/Linux is supported")
    if sys.platform not in ("win32", "linux"):
        raise ValueError("Build on the target Windows or Linux host; cross-compilation is not supported")
    return "windows-x64" if sys.platform == "win32" else "linux-x64"


def pyinstaller_command(native, work, output):
    manifest = json.loads((native / "build-manifest.json").read_text(encoding="utf-8"))
    modules = manifest["modules"]
    for name in modules:
        digest = hashlib.sha256((ROOT / "src" / (name + ".py")).read_bytes()).hexdigest()
        if digest != manifest["source_sha256"][name]:
            raise RuntimeError("Core source changed during compilation; rebuild from a stable checkout")
    entry = work / "standalone_entry.py"
    template = (ROOT / "scripts" / "standalone_entry.py").read_text(encoding="utf-8")
    template = template.replace("CORE_MODULES = ()", "CORE_MODULES = " + repr(tuple(modules)), 1)
    entry.write_text(template, encoding="utf-8")
    # All core imports must resolve to the already compiled native directory.
    imports = set(modules) | set(imported_modules(ROOT / "src" / (name + ".py") for name in modules))
    command = [sys.executable, "-m", "PyInstaller", "--onedir", "--name", "chefas-proxy",
               "--noupx", "--console", "--optimize", "0", "--paths", str(native),
               "--distpath", str(output), "--workpath", str(work / "pyinstaller"),
               "--specpath", str(work), "--add-data", str(native / "dashboard") + ":dashboard"]
    for name in sorted(imports):
        command.extend(("--hidden-import", name))
    for package in ("aiodns", "pycares", "cryptography", "dotenv"):
        command.extend(("--collect-all", package))
    # Force inclusion of dynamically selected asyncio transports/event loops.
    command.extend(("--collect-submodules", "asyncio"))
    command.append(str(entry))
    return command, manifest


def declared_dependencies(path, seen=None):
    """Resolve this repository's simple pinned requirement files without pip."""
    path = Path(path).resolve()
    seen = set() if seen is None else seen
    if path in seen:
        return {}
    seen.add(path)
    result = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            result.update(declared_dependencies(path.parent / line[3:].strip(), seen))
        else:
            name, version = line.split("==", 1)
            result[name] = version
    return result


def validate_example_environment(path):
    """An accidentally edited .env.example must not turn into a secret leak."""
    from dotenv import dotenv_values
    allowed = {"CHEFAS_PROXY_ADMIN_TOKEN": (None, ""), "CHEFAS_PROXY_SECRETS_KEY": (None, ""),
               "CHEFAS_PROXY_USER_PASSWORD": (None, "", "x"), "CHEFAS_PROXY_DEVFEE_PASSWORD": (None, "", "x")}
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig")
    if any(key not in allowed or value not in allowed[key] for key, value in values.items()):
        raise ValueError(".env.example contains non-placeholder values; release refused")


def inventory(directory):
    """Sorted content hashes aid verification; manifests are not signatures."""
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            # PyInstaller uses shared-library symlinks on Linux; hash link target text.
            digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
            result[path.relative_to(directory).as_posix()] = {"symlink_sha256": digest}
        elif path.is_file():
            with path.open("rb") as handle:
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[path.relative_to(directory).as_posix()] = {"sha256": digest.hexdigest()}
    return result


def validate_native_files(bundle, modules):
    files = list(bundle.rglob("*"))
    for name in modules:
        candidates = [p for p in files if p.is_file() and
                      (p.name.startswith(name + ".") and p.suffix in (".pyd", ".so"))]
        if len(candidates) != 1:
            raise RuntimeError("Missing or ambiguous native core artifact: " + name)
        if any(p.name in (name + ".py", name + ".pyc") for p in files):
            raise RuntimeError("Core source/bytecode fallback found: " + name)
    forbidden = {".env", "chefas-proxy.json", "control.json", "history.json", "compatibility.json", "state", "logs"}
    bundle_root = bundle.resolve()
    for path in files:
        name = path.name.casefold()
        if (name in forbidden or (name.startswith(".env.") and name != ".env.example")
                or path.suffix.casefold() in {".key", ".p12", ".pfx", ".log", ".db", ".sqlite", ".sqlite3"}):
            raise RuntimeError("Unexpected state or secrets in release")
        if not path.resolve().is_relative_to(bundle_root):
            raise RuntimeError("Unexpected link outside the release")
        if path.suffix.casefold() in {".pem", ".crt", ".cer"} and path.is_file():
            if any(line.startswith(b"-----BEGIN ") and b"PRIVATE KEY-----" in line
                   for line in path.read_bytes().splitlines()):
                raise RuntimeError("Unexpected private key in release")


def validate_core_archive(bundle, modules):
    """Reject hidden core bytecode as well as loose .py/.pyc files."""
    from PyInstaller.archive.readers import CArchiveReader
    executable = bundle / ("chefas-proxy.exe" if os.name == "nt" else "chefas-proxy")
    archive = CArchiveReader(str(executable))
    for name, entry in archive.toc.items():
        if entry[-1] == "z":
            embedded = archive.open_embedded_archive(name)
            if set(modules).intersection(embedded.toc):
                raise RuntimeError("Core bytecode was unexpectedly included in PYZ")


def smoke_test(bundle):
    executable = bundle / ("chefas-proxy.exe" if os.name == "nt" else "chefas-proxy")
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("PYTHON") or key.startswith("CHEFAS_PROXY_"):
            environment.pop(key)
    # The binary must not need a Python installation or packages on PATH.
    environment["PATH"] = os.path.join(environment.get("SystemRoot", "C:\\Windows"), "System32") if os.name == "nt" else "/usr/bin:/bin"
    for arguments in (("--release-info",), ("--help",)):
        result = subprocess.run([str(executable), *arguments], cwd=bundle, env=environment,
                                text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=45)
        if result.returncode:
            raise RuntimeError("Standalone smoke test failed: " + result.stderr[-4000:])
        if arguments[0] == "--release-info":
            report = json.loads(result.stdout)
            if report.get("frozen") is not True:
                raise RuntimeError("Expected a frozen runtime")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Fresh distribution directory; default dist/chefas-proxy-<platform>")
    parser.add_argument("--skip-smoke-test", action="store_true")
    args = parser.parse_args()
    try:
        target = release_platform()
        output = Path(args.output).resolve() if args.output else ROOT / "dist" / ("chefas-proxy-" + target)
        archive_format = "zip" if os.name == "nt" else "gztar"
        archive_suffix = ".zip" if os.name == "nt" else ".tar.gz"
        archive_path = Path(str(output) + archive_suffix)
        checksum_path = Path(str(archive_path) + ".sha256")
        if output.exists() or archive_path.exists() or checksum_path.exists():
            raise ValueError("Output already exists; choose a fresh directory. Nothing is removed automatically")
        dependencies = declared_dependencies(ROOT / "requirements-release.txt")
        if any(importlib.metadata.version(name) != version for name, version in dependencies.items()):
            raise ValueError("Install the exact pinned requirements-release.txt into the build environment")
        validate_example_environment(ROOT / ".env.example")
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="standalone-", dir=build)).resolve()
        native = work / "native"
        subprocess.run([sys.executable, str(ROOT / "build_ext.py"), "--output", str(native)], cwd=ROOT, check=True)
        command, native_manifest = pyinstaller_command(native, work, work / "dist")
        # Remove developer import paths: a source fallback must not enter the bundle.
        environment = dict(os.environ)
        safe_paths = [str(Path(path).resolve()) for path in sys.path if path and
                      Path(path).resolve() != ROOT and ROOT not in Path(path).resolve().parents]
        environment["PYTHONPATH"] = os.pathsep.join(safe_paths)
        subprocess.run(command, cwd=work, env=environment, check=True)
        bundle = work / "dist" / "chefas-proxy"
        validate_core_archive(bundle, native_manifest["modules"])
        shutil.copyfile(native / ".env.example", bundle / ".env.example")
        shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE")
        shutil.copyfile(ROOT / "docs" / "release.md", bundle / "RELEASE.md")
        example = {"pool_host": "stratum+tcp://POOL_HOST:3333", "listen_host": "127.0.0.1",
                   "listen_port": 3333, "active_router": True, "validate_pow": True,
                   "dashboard_port": 8080}
        (bundle / "chefas-proxy.example.json").write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")
        (bundle / "chefas-proxy.service.example").write_text(service_text(), encoding="utf-8", newline="\n")
        if os.name == "nt":
            (bundle / "start.cmd").write_text('@echo off\ncd /d "%~dp0"\n"%~dp0chefas-proxy.exe" %*\nif errorlevel 1 (\n  echo See RELEASE.md for configuration instructions.\n  pause\n)\n', encoding="ascii", newline="\r\n")
        else:
            starter = bundle / "start.sh"
            starter.write_text('#!/bin/sh\nset -eu\nbase=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\ncd "$base"\nexec "$base/chefas-proxy" "$@"\n', encoding="utf-8", newline="\n")
            starter.chmod(0o755)
        if not args.skip_smoke_test:
            smoke_test(bundle)
        validate_native_files(bundle, native_manifest["modules"])
        manifest = {"format": 1, "target": target, "python": platform.python_version(),
                    "pyinstaller": importlib.metadata.version("pyinstaller"),
                    "declared_dependencies": dependencies,
                    "native_build": native_manifest, "smoke_test": not args.skip_smoke_test,
                    "requirements_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                            for name in ("requirements.txt", "requirements-build.txt", "requirements-release.txt")},
                    "files": inventory(bundle)}
        (bundle / "release-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise ValueError("Output appeared during build; staging retained")
        os.rename(bundle, output)
        if archive_path.exists() or checksum_path.exists():
            raise ValueError("Archive destination appeared during build; distribution directory retained")
        shutil.make_archive(str(output), archive_format, root_dir=output.parent, base_dir=output.name)
        digest = hashlib.sha256()
        with archive_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        with checksum_path.open("x", encoding="ascii") as handle:
            handle.write(digest.hexdigest() + "  " + archive_path.name + "\n")
        print("Standalone release: " + str(output))
        print("Distribution archive: " + str(archive_path))
    except (ValueError, RuntimeError, OSError, importlib.metadata.PackageNotFoundError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
