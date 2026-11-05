# Pilot hardening and native build

This is a pilot candidate. No Antminer/Whatsminer firmware or commercial pool has been certified by the synthetic tests.

## Install and secrets

Use Python 3.10+ in a dedicated virtual environment:

```sh
python -m pip install -r requirements.txt
```

The tested runtime dependencies are pinned in requirements.txt. Only the explicit `--env-file` is loaded (default `.env` in the working directory). Server ENV takes priority, secret interpolation is disabled, UTF-8 with or without BOM is accepted. `--env-file=` disables dotenv loading. Generate independent values locally:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the first value as `KONTUR_ADMIN_TOKEN` and the second as `KONTUR_SECRETS_KEY` in server ENV or a private `.env`. Never put them in JSON, Git, screenshots or a release archive. Restrict the ENV file/key backups and state directory to the service account, including Windows ACLs and any cloud sync of the workspace. Losing the encryption key prevents recovery of encrypted pool passwords. A key readable by the same OS account is not protection against that compromised account.

`KONTUR_USER_PASSWORD` and `KONTUR_DEVFEE_PASSWORD` are optional initial passwords. The CLI `--devfee-password` has been removed to avoid process-list leakage. The existing User Pool account override is still configured in the authenticated routing UI; an empty override forwards each ASIC's own credentials in memory.

Persisted route passwords use authenticated Fernet ciphertext, bound to pool role, host, port and worker. Alternatively, with no encryption key, a password exactly matching its role's ENV value is persisted as an ENV reference. Empty passwords and the public default `x` use scheme markers with no password value. A custom password with neither a key nor matching ENV is rejected before committing the configuration. There is no plaintext fallback.

Existing plaintext route state is migrated atomically during startup only when it can be encrypted or represented safely. No plaintext backup is created. With a missing/wrong key startup fails before opening listeners, preserving the original file for operator recovery; supply the correct ENV/key and restart. Migration cannot erase plaintext copies already held by filesystem snapshots, external backups or cloud sync. Persisted encrypted routes override initial ENV pool-password values on restart; ENV-reference routes read their current ENV value. Administrative tokens remain ENV-only; Observer tokens remain stored as hashes.

Reference: [Fernet authenticated encryption](https://cryptography.io/en/latest/fernet/), [dotenv ENV precedence](https://bbc2.github.io/python-dotenv/).

## Logs

```sh
python proxy.py --pool-host POOL --pool-port PORT --active-router --log-dir logs --log-level INFO
```

The named application logger uses a bounded 2048-record QueueHandler and a single QueueListener thread; disk I/O and rotation happen outside the asyncio loop. Files are UTF-8 JSON lines at `logs/proxy.log`, with a 50 MiB rotation threshold and three archives (approximately 200 MiB total plus a final record). Use a separate log directory per process. Queue overflow drops log records instead of blocking mining; `logging_dropped` in the process metrics and minute summary reports the count. These counters are not persisted. Disk-full/stalled storage can still cause lost logs.

INFO: session start/close, route changes, one-minute accepted-work/hashrate summary, orderly stop. WARNING: upstream disconnects, pool/local rejects and required-fee block. ERROR: startup/configuration and history failures. Packet traces require **both** `--log-level DEBUG` and `--debug-stratum` and operate only on the active router. They include notify and submit data, local/upstream job IDs, ntime/nonce/version bits. Submit worker identifiers are masked; authorize packets and HTTP Authorization headers are never dumped. Registered secrets are redacted before queueing, including encoded strings and exception messages. Packet fields are capped at 16 KiB with an explicit truncation flag. Debug logs may still contain mining-job metadata, so keep them local and review before sharing. They are not byte-exact protocol captures.

Reference: [Python QueueHandler and rotation](https://docs.python.org/3/library/logging.handlers.html).

## Orderly stop

SIGINT and SIGTERM request a cooperative stop, rather than abruptly cancelling the event loop. Windows uses signal.signal fallback; SIGBREAK is also accepted. The listener is closed, an immediate history checkpoint is initiated, active-router miners receive client.reconnect and all miner sockets close, session tasks finish/cancel, the final history checkpoint is written and logs drain. Previous signal handlers are restored. Windows TerminateProcess / taskkill / `Popen.terminate()` are forced termination, not POSIX SIGTERM delivery. SIGKILL and forced termination can lose changes after the last 60-second checkpoint.

## Compile

```sh
python -m pip install -r requirements-build.txt
python build_ext.py --output dist/kontur-pilot
```

MSVC C++ tools and Windows SDK are required on Windows; a C compiler and Python development headers on Linux. The script translates all 14 runtime modules, including the bounded Stratum codec, and builds platform-native .pyd/.so files. It stages sources/C/object files under ignored build/, uses short relative object paths for Windows, never overwrites the source modules and refuses an existing output directory. `--generate-only` validates Cython translation without a native compiler.

The output contains only compiled runtime modules, `launch.py`, UI assets, requirements, `.env.example` and a hash manifest. Real ENV files, keys, state, logs and generated C are excluded by an allowlist. Install runtime requirements in the deployment environment and run `python launch.py ...` from that directory. The launcher rejects source fallback. Artifacts require matching OS, architecture and Python ABI; a Windows CPython 3.10 .pyd is not a Linux .so or a standalone executable. Build and test again on the actual pilot platform. Do not treat a successful C compilation as a successful ASIC test.

Compilation raises the effort to modify the implementation but does not make DevFee non-removable by an owner controlling the interpreter, files or process memory. The manifest hashes are an integrity inventory, not a digital signature. Keep the source repository private if commercial distribution requires source confidentiality.

Reference: [Cython source and extension compilation](https://cython.readthedocs.io/en/latest/src/userguide/source_files_and_compilation.html).

## Tests

```sh
python -m unittest discover -s tests -v
```

For the live CLI integration suite against compiled modules, set `KONTUR_TEST_ENTRYPOINT` to the absolute path to the generated launch.py, then run tests/test_live_devfee_router.py with pytest. This variable is used only by the test fixture, not by the product. Inspect the build-manifest source hashes against the source revision used for the build.
