# Dependencies

Farm users download a standalone package and do not install these files.

- Root `requirements.txt`: runtime dependencies for source development.
- `build.txt`: runtime plus Cython and the native build tools.
- `release.txt`: build tools plus PyInstaller for standalone packages.

Use an isolated virtual environment for releases. Versions are pinned at
the top level; manifests record the actual runtime and file hashes. This is
not a claim of byte-for-byte reproducible builds across operating systems.
