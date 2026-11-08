"""Packaging policy tests; actual frozen execution is the builder's smoke test."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entry = load_script("standalone_entry")
service = load_script("generate_service")
sys.path.insert(0, str(ROOT / "scripts"))
try:
    builder = load_script("build_release")
finally:
    sys.path.pop(0)


class ReleaseConfigurationTests(unittest.TestCase):
    def config(self, value):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "config.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_public_options_are_values_not_injected_flags(self):
        args = entry.configuration_arguments(self.config({"pool_host": "--debug-stratum", "pool_port": 3333,
              "active_router": True, "tls": False, "allow_cidr": ["192.168.1.0/24"]}))
        self.assertEqual(args, ["--pool-host=--debug-stratum", "--pool-port=3333", "--active-router",
                                "--allow-cidr=192.168.1.0/24"])

    def test_secrets_unknown_fields_and_bad_types_are_rejected(self):
        for value in ({"CHEFAS_PROXY_ADMIN_TOKEN": "secret"}, {"password": "secret"},
                      {"active_router": "false"}, {"listen_port": True}, {"pool_host": {}},
                      {"allow_cidr": "0.0.0.0/0"}, [], {"allow_cidr": ["x"] * 129}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                entry.configuration_arguments(self.config(value))

    def test_operator_config_rejects_fee_and_transparent_overrides(self):
        for value in ({"devfee_worker": "other.worker"}, {"fee_min_work": 999},
                      {"require_devfee": False}, {"TP_MODE": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                entry.configuration_arguments(self.config(value), fee_locked=True)

    def test_oversized_config_and_source_fallback_are_rejected(self):
        path = self.config({"pool_host": "x" * 65536})
        with self.assertRaises(ValueError):
            entry.configuration_arguments(path)
        with self.assertRaises(RuntimeError):
            entry.native_modules()

    def test_config_cli_override_and_state_directory(self):
        path = self.config({"pool_host": "example.invalid", "pool_port": 3333})
        captured = []
        proxy = SimpleNamespace(main=lambda: captured.append((list(sys.argv), Path.cwd())),
                                release_fee_locked=lambda: False)
        original_directory = Path.cwd()
        try:
            with patch.object(sys, "argv", ["chefas-proxy", "--config", str(path), "--pool-port", "4444"]), \
                 patch.object(entry, "native_modules", return_value={}), \
                 patch.object(entry.importlib, "import_module", return_value=proxy):
                entry.main()
        finally:
            os.chdir(original_directory)
        self.assertEqual(captured, [(["chefas-proxy", "--pool-host=example.invalid", "--pool-port=3333", "--pool-port", "4444"], path.parent)])

    def test_invalid_configuration_never_echoes_credential(self):
        path = self.config({"CHEFAS_PROXY_ADMIN_TOKEN": "do-not-print-this-secret"})
        output = io.StringIO()
        with patch.object(sys, "argv", ["chefas-proxy", "--config", str(path)]), \
             patch.object(entry, "native_modules", return_value={}), \
             contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as caught:
            entry.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn("do-not-print-this-secret", output.getvalue())


class ServiceGenerationTests(unittest.TestCase):
    def test_service_uses_isolated_persistent_paths_and_sigterm(self):
        unit = service.service_text()
        self.assertIn("KillSignal=SIGTERM", unit)
        self.assertIn("StateDirectory=chefas-proxy", unit)
        self.assertIn("LogsDirectory=chefas-proxy", unit)
        self.assertIn("--history-file=/var/lib/chefas-proxy/history.json", unit)
        self.assertIn("EnvironmentFile=/etc/chefas-proxy/chefas-proxy.env", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("User=chefas-proxy", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("LimitNOFILE=65535", unit)
        self.assertIn("\nWorkingDirectory=/etc/chefas-proxy\n", unit)
        self.assertIn("--pilot-console", unit)
        self.assertNotIn("--devfee-journal", unit)
        self.assertEqual(unit, (ROOT / "scripts" / "chefas-proxy.service").read_text())

    def test_unit_injection_rejected_and_specifiers_escaped(self):
        for path in ("relative/path", "/opt/x\nUser=root", '/opt/"x', "/opt/../root", "/opt/\\root", "/opt/$HOME", "/opt/\tx"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                service.service_text(install_dir=path)
        for user in ("root\nExecStart=bad", "user name", "-bad"):
            with self.assertRaises(ValueError):
                service.service_text(user=user)
        self.assertIn('ExecStart="/opt/%%h/chefas-proxy"', service.service_text(install_dir="/opt/%h"))


class BuildPolicyTests(unittest.TestCase):
    def test_environment_example_cannot_leak_accidentally_saved_secrets(self):
        builder.validate_example_environment(ROOT / ".env.example")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env.example"
            for content in ("CHEFAS_PROXY_ADMIN_TOKEN=real-secret", "CHEFAS_PROXY_DEVFEE_PASSWORD=real-password",
                            "UNRELATED_API_KEY=real-api-secret"):
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(ValueError):
                    builder.validate_example_environment(path)

    def test_hidden_imports_cover_transitive_core_dependencies(self):
        imports = builder.imported_modules(ROOT.glob("src/*.py"))
        self.assertIn("logging.handlers", imports)
        self.assertIn("cryptography.fernet", imports)
        self.assertIn("aiodns", imports)
        self.assertIn("asyncio", imports)

    def test_inventory_changes_when_artifact_changes_and_no_source_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact = directory / "proxy.cp310-win_amd64.pyd"
            artifact.write_bytes(b"binary")
            before = builder.inventory(directory)
            builder.validate_native_files(directory, ["proxy"])
            artifact.write_bytes(b"changed binary")
            self.assertNotEqual(before, builder.inventory(directory))
            (directory / "proxy.py").write_text("secret = 'uncompiled source'")
            with self.assertRaises(RuntimeError):
                builder.validate_native_files(directory, ["proxy"])

    def test_final_bundle_rejects_private_files_and_allows_ca_certificates(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "ca.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\n")
            builder.validate_native_files(directory, [])
            for name, content in (
                (".env.production", b"TOKEN=secret"),
                ("CHEFAS-PROXY.JSON", b"{}"),
                ("runtime.log", b"runtime details"),
                ("server.pem", b"-----BEGIN EC PRIVATE KEY-----\n"),
            ):
                file = directory / name
                file.write_bytes(content)
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    builder.validate_native_files(directory, [])
                file.unlink()

    def test_existing_output_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            marker = path / "preserve.txt"
            marker.write_text("keep")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/build_release.py"),
                                     "--output", str(path)], capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Output already exists", result.stderr)
            self.assertEqual(marker.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
