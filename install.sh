#!/usr/bin/env bash
# ThrottleStop for Linux - System-wide Installer for HP Laptops

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo ./install.sh"
    exit 1
fi

echo "==> Installing ThrottleStop for Linux..."

INSTALL_DIR="/opt/throttlestop-hp-linux"
mkdir -p "$INSTALL_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Copy application files
cp "$SCRIPT_DIR/power_profile.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/hp_telemetry.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/hardware.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/fan_speed.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/fan_profiler.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/throttlestop" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/throttlestop" "$INSTALL_DIR/power_profile.py" "$INSTALL_DIR/hp_telemetry.py" "$INSTALL_DIR/fan_speed.py" "$INSTALL_DIR/fan_profiler.py"

# Symlink to /usr/local/bin
ln -sf "$INSTALL_DIR/throttlestop" /usr/local/bin/throttlestop
ln -sf "$INSTALL_DIR/fan_speed.py" /usr/local/bin/hp-fan-speed
ln -sf "$INSTALL_DIR/hp_telemetry.py" /usr/local/bin/hp-telemetry

# Install desktop entry
if [ -d /usr/share/applications ]; then
    cp "$SCRIPT_DIR/desktop/throttlestop.desktop" /usr/share/applications/
    chmod 644 /usr/share/applications/throttlestop.desktop
    echo "✔ Installed desktop launcher"
fi

# Install systemd service
if [ -d /etc/systemd/system ]; then
    cp "$SCRIPT_DIR/systemd/throttlestop.service" /etc/systemd/system/
    chmod 644 /etc/systemd/system/throttlestop.service
    systemctl daemon-reload
    echo "✔ Installed systemd service (enable with: sudo systemctl enable --now throttlestop.service)"
fi

# Load required kernel modules
modprobe msr 2>/dev/null || true
modprobe ec_sys write_support=1 2>/dev/null || modprobe ec_sys 2>/dev/null || true

# Add modules to /etc/modules-load.d/
mkdir -p /etc/modules-load.d
echo -e "msr\nec_sys" > /etc/modules-load.d/throttlestop-hp.conf

echo ""
echo "✔ Installation complete!"
echo "Run 'throttlestop' in your terminal or launch 'ThrottleStop HP' from your application menu."
