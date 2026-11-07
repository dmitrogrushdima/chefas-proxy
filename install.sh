#!/usr/bin/env bash
# First installation from an extracted, trusted Linux release.
set -euo pipefail

if [[ "${1:-}" == --help ]]; then
    printf '%s\n' 'Usage: sudo bash install.sh [extracted-linux-release-directory]'
    exit 0
fi
if (( $# > 1 || EUID != 0 )); then
    printf '%s\n' 'Run as root with at most one extracted Linux release directory.' >&2
    exit 1
fi
if [[ "$(uname -s)" != Linux || ! -d /run/systemd/system ]]; then
    printf '%s\n' 'A Linux host running systemd is required.' >&2
    exit 1
fi
if [[ "$(uname -m)" != x86_64 ]]; then
    printf '%s\n' 'This release requires Linux x86-64.' >&2
    exit 1
fi
source_dir=$(realpath -e -- "${1:-$(dirname -- "$0")}")
destination=/opt/chefas-proxy
config_dir=/etc/chefas-proxy
unit=/etc/systemd/system/chefas-proxy.service
service_user=chefas-proxy
umask 022

for file in chefas-proxy chefas-proxy.example.json .env.example chefas-proxy.service release-manifest.json LICENSE RELEASE.md; do
    if [[ ! -f "$source_dir/$file" || -L "$source_dir/$file" ]]; then
        printf 'Missing Linux release file: %s\n' "$file" >&2
        exit 1
    fi
done
if [[ ! -d "$source_dir/_internal" || -L "$source_dir/_internal" || ! -x "$source_dir/chefas-proxy" ]]; then
    printf '%s\n' 'The extracted Linux executable and its _internal directory are required.' >&2
    exit 1
fi
if [[ -e "$destination" || -L "$destination" || -e "$unit" || -L "$unit" || -L "$config_dir" ]]; then
    printf '%s\n' 'An installation already exists. Preserve its configuration and use the upgrade procedure.' >&2
    exit 1
fi

install -d -m 0755 /opt
stage=$(mktemp -d /opt/.chefas-proxy-install.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT
install -m 0755 -- "$source_dir/chefas-proxy" "$stage/chefas-proxy"
cp -a -- "$source_dir/_internal" "$stage/_internal"
while IFS= read -r -d '' link; do
    target=$(realpath -e -- "$link")
    if [[ "$target" != "$stage/_internal/"* ]]; then
        printf '%s\n' 'The runtime contains a link outside its own directory.' >&2
        exit 1
    fi
done < <(find "$stage/_internal" -type l -print0)
chown -hR root:root "$stage"
chmod -R u-s,g-s,go-w "$stage"
chmod 0755 "$stage"
install -m 0644 -- "$source_dir/release-manifest.json" "$source_dir/LICENSE" "$source_dir/RELEASE.md" "$stage/"

if ! getent group "$service_user" >/dev/null; then
    groupadd --system "$service_user"
fi
if ! id "$service_user" >/dev/null 2>&1; then
    useradd --system --gid "$service_user" --no-create-home --shell /usr/sbin/nologin "$service_user"
fi
if [[ "$(id -u "$service_user")" == 0 ]]; then
    printf '%s\n' 'The service account must be unprivileged.' >&2
    exit 1
fi
install -d -m 0750 -o root -g "$service_user" "$config_dir"
for file in chefas-proxy.json chefas-proxy.env; do
    if [[ -L "$config_dir/$file" || ( -e "$config_dir/$file" && ! -f "$config_dir/$file" ) ]]; then
        printf 'Unexpected configuration path: %s\n' "$config_dir/$file" >&2
        exit 1
    fi
done
if [[ ! -e "$config_dir/chefas-proxy.json" ]]; then
    install -m 0640 -o root -g "$service_user" -- "$source_dir/chefas-proxy.example.json" "$config_dir/chefas-proxy.json"
fi
if [[ ! -e "$config_dir/chefas-proxy.env" ]]; then
    install -m 0600 -o root -g root -- "$source_dir/.env.example" "$config_dir/chefas-proxy.env"
fi
mv -T -- "$stage" "$destination"
install -m 0644 -- "$source_dir/chefas-proxy.service" "$unit"
systemctl daemon-reload
printf '%s\n' 'Edit /etc/chefas-proxy/chefas-proxy.json and chefas-proxy.env, then run:' \
    '  sudo systemctl enable --now chefas-proxy'
