"""Generate a systemd unit; do not install files, create users, or start services."""
import argparse
from pathlib import Path, PurePosixPath
import re


def _path(value):
    if not isinstance(value, str) or not value.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("An absolute POSIX path is required")
    path = PurePosixPath(value)
    if ".." in path.parts or '"' in value or "\\" in value or "$" in value:
        raise ValueError("Unsafe service path")
    # systemd performs %-specifier expansion, even inside quoted values.
    return '"' + str(path).replace("%", "%%") + '"'


def service_text(install_dir="/opt/chefas-proxy", config="/etc/chefas-proxy/chefas-proxy.json", env_file="/etc/chefas-proxy/chefas-proxy.env", user="chefas-proxy"):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", user):
        raise ValueError("Invalid service user")
    executable = _path(install_dir.rstrip("/") + "/chefas-proxy")
    config_path, environment = _path(config), _path(env_file)
    working = _path(str(PurePosixPath(config).parent))
    template = Path(__file__).with_name('chefas-proxy.service').read_text(encoding='utf-8')
    replacements = {
        'User': user, 'Group': user, 'WorkingDirectory': working,
        'EnvironmentFile': environment,
        'ExecStart': (f'{executable} --config {config_path} --env-file= '
                      '--control-state=/var/lib/chefas-proxy/control.json '
                      '--history-file=/var/lib/chefas-proxy/history.json '
                      '--compatibility-state=/var/lib/chefas-proxy/compatibility.json '
                      '--pilot-console --log-dir=/var/log/chefas-proxy'),
    }
    lines = []
    for line in template.splitlines():
        key = line.partition('=')[0]
        lines.append(f'{key}={replacements[key]}' if key in replacements else line)
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--install-dir", default="/opt/chefas-proxy")
    parser.add_argument("--config", default="/etc/chefas-proxy/chefas-proxy.json")
    parser.add_argument("--env-file", default="/etc/chefas-proxy/chefas-proxy.env")
    parser.add_argument("--user", default="chefas-proxy")
    args = parser.parse_args()
    try:
        text = service_text(args.install_dir, args.config, args.env_file, args.user)
        with Path(args.output).open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
