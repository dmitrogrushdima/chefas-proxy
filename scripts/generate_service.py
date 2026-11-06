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


def service_text(install_dir="/opt/kontur", config="/etc/kontur/kontur.json", env_file="/etc/kontur/kontur.env", user="kontur"):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", user):
        raise ValueError("Invalid service user")
    executable = _path(install_dir.rstrip("/") + "/kontur")
    config_path, environment = _path(config), _path(env_file)
    working = _path(str(PurePosixPath(config).parent))
    return f'''[Unit]
Description=KONTUR Stratum mining proxy
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={working}
EnvironmentFile={environment}
ExecStart={executable} --config {config_path} --control-state=/var/lib/kontur/control.json --history-file=/var/lib/kontur/history.json --compatibility-state=/var/lib/kontur/compatibility.json --log-dir=/var/log/kontur
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=45
LimitNOFILE=65536
UMask=0077
StateDirectory=kontur
LogsDirectory=kontur
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--install-dir", default="/opt/kontur")
    parser.add_argument("--config", default="/etc/kontur/kontur.json")
    parser.add_argument("--env-file", default="/etc/kontur/kontur.env")
    parser.add_argument("--user", default="kontur")
    args = parser.parse_args()
    try:
        text = service_text(args.install_dir, args.config, args.env_file, args.user)
        with Path(args.output).open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
