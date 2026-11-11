# Build tools

Run commands from the repository root:

```text
python -m pip install -r requirements/release.txt
python scripts/build_release.py
python scripts/verify_release.py --bundle dist/chefas-proxy-windows-x64
```

Use the Linux bundle directory on Linux. Builds are native to their target OS;
existing output is never overwritten. `verify_release.py` uses a temporary
copy and no real ASIC. `verify_linux_install.py` is restricted to disposable
GitHub Actions runners because it installs and starts a systemd service.

[Build and release guide](../docs/release.md).
