"""Frozen runtime bootstrap. Build injects the authoritative native module list."""
import argparse
import base64
import importlib
from importlib.machinery import EXTENSION_SUFFIXES
import json
import os
import secrets
from pathlib import Path
import sys

CORE_MODULES = ()  # Replaced in the staged entry point; never import src/ here.
RELEASE_VERSION = '0.1.0-rc.3'


def setup_files(directory):
    """Create public configuration and private random credentials without replacing files."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "chefas-proxy.json"
    environment = directory / ".env"
    if not config.exists():
        example = {"pool_host": "stratum+tcp://POOL_HOST:3333", "listen_port": 3333,
                   "active_router": True, "aggregate": True, "validate_pow": True,
                   "dashboard_port": 8080}
        try:
            with config.open("x", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(example, indent=2) + "\n")
        except FileExistsError:
            pass
    if not environment.exists():
        values = ("# Private CHEFAS credentials. Do not share this file.\n"
                  "CHEFAS_PROXY_ADMIN_TOKEN=" + secrets.token_urlsafe(32) + "\n"
                  "CHEFAS_PROXY_SECRETS_KEY=" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode() + "\n"
                  "CHEFAS_PROXY_USER_PASSWORD=x\n")
        try:
            descriptor = os.open(environment, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(values)
    return config, environment
CONFIG_OPTIONS = {
    "tp_mode", "pool_host", "pool_port", "listen_host", "listen_port", "active_router", "aggregate",
    "aggregate_worker_mode", "aggregate_shard_size", "aggregate_vardiff", "tenant_profiles",
    "validate_pow", "devfee_host", "devfee_port", "devfee_worker",
    "devfee_worker_template", "max_fee_window_seconds", "fee_cooldown_seconds", "fee_overdraft",
    "backup_host", "backup_port", "dashboard_port", "tls", "tls_ca_file",
    "failover_job_timeout", "failover_max_rtt", "failover_recovery",
    "max_connections", "connect_timeout", "miner_timeout", "handshake_timeout",
    "max_miner_message", "idle_timeout", "allow_cidr", "allow_any_miner",
    "require_devfee", "fee_grace_seconds", "fee_min_work", "reconnect_min_seconds",
    "devfee_jitter_seconds", "devfee_connect_interval", "job_ttl", "job_gc_interval",
    "hot_switch_mode", "asic_extranonce2_min", "asic_extranonce2_max",
    "env_file", "log_dir", "log_level", "debug_stratum", "control_state",
    "history_file", "compatibility_state", "devfee_journal",
}
BOOLEAN_OPTIONS = {
    "tp_mode", "active_router", "aggregate", "aggregate_vardiff", "validate_pow", "tls", "allow_any_miner", "require_devfee", "debug_stratum",
}


def configuration_arguments(path, *, fee_locked=False):
    """Only public CLI settings belong here; secrets stay in ENV / .env."""
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("Configuration exceeds 64 KiB")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(config, dict) and "TP_MODE" in config:
        if "tp_mode" in config:
            raise ValueError("Duplicate TP mode setting")
        config["tp_mode"] = config.pop("TP_MODE")
    if not isinstance(config, dict) or not set(config).issubset(CONFIG_OPTIONS):
        raise ValueError("Unknown configuration key; passwords and tokens belong in ENV")
    if fee_locked and any(key.startswith(("devfee", "fee_", "max_fee_window")) or
                          key in ("tp_mode", "require_devfee") for key in config):
        raise ValueError("Fee policy and transparent mode are locked")
    result = []
    for key, value in config.items():
        option = "--" + key.replace("_", "-")
        if key in BOOLEAN_OPTIONS:
            if not isinstance(value, bool):
                raise ValueError("Boolean option requires true or false")
            if value:
                result.append(option)
        elif key == "allow_cidr":
            if not isinstance(value, list) or len(value) > 128 or any(not isinstance(v, str) for v in value):
                raise ValueError("allow_cidr requires a bounded list of networks")
            for network in value:
                result.append(option + "=" + network)
        else:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError("Option requires a string or number")
            # An equals-sign keeps values beginning with '--' from becoming flags.
            result.append(option + "=" + str(value))
    return result


def native_modules():
    if not getattr(sys, "frozen", False) or not CORE_MODULES:
        raise RuntimeError("This entry point requires a built standalone release")
    modules = {}
    bundle = Path(sys._MEIPASS).resolve()
    for name in CORE_MODULES:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        if bundle not in path.parents or not any(str(path).endswith(suffix) for suffix in EXTENSION_SUFFIXES):
            raise RuntimeError("Native bundled module required: " + name)
        modules[name] = path.name
    return modules


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--release-info", action="store_true")
    parser.add_argument("--setup", action="store_true")
    known, remainder = parser.parse_known_args()
    try:
        modules = native_modules()
        if known.release_info:
            print(json.dumps({"version": RELEASE_VERSION, "frozen": True,
                              "python": sys.version.split()[0], "native_modules": modules}, sort_keys=True))
            return
        base = Path(sys.executable).resolve().parent
        if known.setup:
            if known.config or remainder:
                raise ValueError("Setup does not accept routing arguments")
            setup_files(base)
            print("Setup complete. Edit chefas-proxy.json with your BTC pool. Admin credentials are in .env.")
            return
        path = Path(known.config).resolve() if known.config else base / "chefas-proxy.json"
        options = []
        if "--help" in remainder or "-h" in remainder:
            options = []
        elif known.config or path.exists():
            options = configuration_arguments(path, fee_locked=importlib.import_module("proxy").release_fee_locked())
            # State and dotenv paths are relative to the selected configuration.
            os.chdir(path.parent)
        elif not remainder:
            raise ValueError("Create chefas-proxy.json from chefas-proxy.example.json, or supply --pool-host and --pool-port")
        sys.argv = [sys.argv[0], *options, *remainder]
    except (OSError, ValueError, RuntimeError, ImportError):
        # Never interpolate config contents or credentials into startup errors.
        print("Release startup failed. Check native files and public chefas-proxy.json settings; secrets belong in ENV. See RELEASE.md.", file=sys.stderr)
        raise SystemExit(2) from None
    importlib.import_module("proxy").main()


if __name__ == "__main__":
    main()
