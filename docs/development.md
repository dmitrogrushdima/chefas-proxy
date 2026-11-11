# Development and native build

The application uses flat runtime modules inside `src/` so the same imports work in source runs and source-free Cython bundles. `launch.py` is the development bootstrap; the generated bundle launcher requires native modules. No compiled modules are written beside source.

```sh
python -m pip install -r requirements.txt
python launch.py --pool-host 127.0.0.1 --pool-port 4444
```

`scripts/preview-dashboard.cmd` is a local Windows preview helper. It has no real pool or miners and must not be used as a production preset.

## Tests

```sh
python -m pip install pytest
python -m pytest -q
python -m unittest discover -s tests -t . -v
```

The pytest command is the release gate and is used by CI on Windows and Linux.
Unittest discovery alone omits the function-based quantization regressions;
use it only as an additional check. The enterprise soak remains opt-in and a
skipped soak is not evidence of measured fleet capacity.

`pytest.ini` selects the tests and source path. The `lab` package also supports `python -m lab.load_test`; it is a synthetic echo experiment, not a farm benchmark.

## Build

```sh
python -m pip install -r requirements/build.txt
python scripts/build_ext.py
```

Windows requires MSVC/Windows SDK; Linux requires a C compiler and Python development headers. Default output: ignored `bin/chefas-proxy/`. An existing output is never overwritten; pass `--output bin/ANOTHER_NAME` for a fresh build. `--generate-only` checks Cython translation without linking. Compiled distributions include 17 native modules, the bootstrap, local UI, launcher scripts, dependency files and a hash manifest, without runtime source, real ENV files, state or logs.

For an executable that bundles Python, use the [standalone release scripts](release.md). Root launchers prefer `dist/chefas-proxy-windows-x64/chefas-proxy.exe` or `dist/chefas-proxy-linux-x64/chefas-proxy` when present, and otherwise use the native development runtime.

To test the compiled CLI, set `CHEFAS_PROXY_TEST_ENTRYPOINT` to its absolute `launch.py`, then run `tests/test_live_devfee_router.py` with `-k "not graceful_proxy_shutdown and not sigterm_closes_live"`. The excluded tests invoke the source entrypoint in-process. Verify manifest hashes and actual import paths; successful compilation alone is insufficient.

Before release: qualify Linux packaging, long-running lossy-network behavior, signed distribution/update integrity and real hardware/pool combinations. Performance targets require measurement; the existing 400-session regression does not certify thousands of ASICs.

## Previous milestone: packaging/compatibility — 1858ccf

| Check | Result |
| --- | --- |
| Final Windows x64 / CPython 3.10 build | 15 native modules; every module path and source/artifact manifest hash verified |
| Native unit regressions | 35 passed, including exact masks, bounded repeated configure, translated headers, profile expiry/persistence and GC |
| Actual compiled launcher integrations | 32 passed; two source-only in-process shutdown tests excluded here and covered in source runs |
| Final native 400-client isolation test | Passed with the original 400 clients and 20-second read timeout |
| Frontend | Chrome at 1440 and 390 px, live local API values, no page-width overflow; Node syntax checks passed |
| Packaging | Root and bundled Windows launchers checked; POSIX shell syntax checked; local Markdown links verified |

Source checks were run in parts: the main run had 161 passes and two timing failures; both failed cases passed on targeted rerun, with the localhost loop check hardened to avoid DNS queuing. Five further targeted source regressions were added/passed after that run. The separate source/debug 400-client run timed out on this Windows host; this is recorded rather than relabeled green or solved by extending the test's deadline. Native results above are for the actual generated `.pyd` runtime, not source fallback. These tests do not establish real-device compatibility or a large-farm performance target.

The concurrent allocator supersedes this milestone's single global fee owner. [Fleet scaling](engineering/fleet-scaling.md) records the implemented reservation model and the remaining shared-upstream integration gate.
