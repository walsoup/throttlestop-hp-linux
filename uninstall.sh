#!/usr/bin/env bash
# ThrottleStop for Linux - Uninstaller

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo ./uninstall.sh"
    exit 1
fi

echo "==> Uninstalling ThrottleStop for Linux..."

# Stop and disable systemd service
if systemctl is-active --quiet throttlestop.service 2>/dev/null; then
    systemctl stop throttlestop.service || true
fi
if systemctl is-enabled --quiet throttlestop.service 2>/dev/null; then
    systemctl disable throttlestop.service || true
fi
rm -f /etc/systemd/system/throttlestop.service
systemctl daemon-reload 2>/dev/null || true

# Remove binaries and symlinks
rm -f /usr/local/bin/throttlestop
rm -f /usr/local/bin/hp-fan-speed
rm -f /usr/local/bin/hp-telemetry
rm -rf /opt/throttlestop-hp-linux

# Remove desktop entry
rm -f /usr/share/applications/throttlestop.desktop

# Remove modules-load config
rm -f /etc/modules-load.d/throttlestop-hp.conf

echo "✔ ThrottleStop for Linux uninstalled successfully."
