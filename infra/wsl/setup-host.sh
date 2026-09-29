#!/bin/bash
# One-time WSL host setup for Build-Bench development. Run as: sudo bash setup-host.sh
#   - Docker Engine (Ubuntu docker.io) with Docker Hub mirrors and a proxy for ghcr.io
#   - qemu binfmt handlers (aarch64, riscv64) registered at boot; WSL disables systemd-binfmt
#   - bb-winproxy.service: keeps the Windows Clash proxy reachable at 127.0.0.1:7897
set -euo pipefail

[[ $(id -u) -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
U="${SUDO_USER:-snoz}"
H="$(getent passwd "$U" | cut -d: -f6)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY=http://127.0.0.1:7897
MIRRORS=(https://docker.m.daocloud.io https://docker.1ms.run)

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  docker.io docker-buildx qemu-user-static quilt jq poppler-utils python3-venv python3-pip

usermod -aG docker "$U"

# Proxy tunnel service (replaces any manually started instance).
install -m 0755 -o "$U" -g "$U" "$HERE/winproxy-tunnel" "$H/bin/winproxy-tunnel"
cat >/etc/systemd/system/bb-winproxy.service <<EOF
[Unit]
Description=Expose Windows Clash proxy inside WSL at 127.0.0.1:7897
After=network.target

[Service]
User=$U
ExecStart=$H/bin/winproxy-tunnel
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
pkill -u "$U" -f 'bin/winproxy-tunnel' || true
pkill -u "$U" -f 'id_wslproxy_ed25519' || true

# Register qemu handlers directly; restarting systemd-binfmt would also drop WSLInterop.
cat >/usr/local/sbin/bb-register-binfmt <<'EOF'
#!/bin/bash
for a in aarch64 riscv64; do
  [[ -e /proc/sys/fs/binfmt_misc/qemu-$a ]] && continue
  cat /usr/lib/binfmt.d/qemu-$a.conf >/proc/sys/fs/binfmt_misc/register
done
EOF
chmod 0755 /usr/local/sbin/bb-register-binfmt
cat >/etc/systemd/system/bb-binfmt.service <<'EOF'
[Unit]
Description=Register qemu binfmt handlers for Build-Bench cross-arch builds
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/bb-register-binfmt

[Install]
WantedBy=multi-user.target
EOF

mkdir -p /etc/docker /etc/systemd/system/docker.service.d
cat >/etc/docker/daemon.json <<EOF
{
  "registry-mirrors": ["${MIRRORS[0]}", "${MIRRORS[1]}"],
  "log-opts": {"max-size": "50m", "max-file": "3"}
}
EOF
cat >/etc/systemd/system/docker.service.d/proxy.conf <<EOF
[Service]
Environment="HTTP_PROXY=$PROXY" "HTTPS_PROXY=$PROXY" "NO_PROXY=localhost,127.0.0.1,docker.m.daocloud.io,docker.1ms.run"
EOF

systemctl daemon-reload
systemctl enable --now bb-winproxy.service bb-binfmt.service
systemctl enable docker.service
systemctl restart docker.service

echo
echo "binfmt: $(ls /proc/sys/fs/binfmt_misc | tr '\n' ' ')"
docker version --format 'docker server {{.Server.Version}}'
echo "Done. Open a new shell (or re-login) so '$U' picks up the docker group."
