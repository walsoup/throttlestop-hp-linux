# ThrottleStop for Linux (`throttlestop-hp-linux`)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux%20x86__64-orange.svg)](#)
[![Hardware](https://img.shields.io/badge/Hardware-HP%20Laptops%20%26%20Intel%20Mobile-blue.svg)](#)
[![Python](https://img.shields.io/badge/Python-3.8%2B-green.svg)](#)

A comprehensive hardware power manager, thermal tuning utility, and live telemetry monitor tailored for **HP laptops** (Omen, Victus, Pavilion, Envy, Spectre, EliteBook, and 14/15/17 series) and modern **Intel mobile architectures** (Meteor Lake / Core Ultra, Raptor Lake, Alder Lake, Tiger Lake, and legacy Core i-series).

Designed to be the complete Linux counterpart to Windows' **ThrottleStop**.

---

## ⚡ Key Capabilities

- **Zero Hardcoding & Dynamic Hardware Engine**: Automatically discovers CPU model, core architecture (P-cores, E-cores, Low-Power island cores, or standard multi-core), thread counts, silicon frequency ceilings, GPU(s), fan configuration (0, 1, or 2+ fans), and DMI board information.
- **Dual-Plane RAPL Power Limits (TPL)**:
  - Direct hardware MSR `0x610` (`MSR_PKG_POWER_LIMIT`) programming via `/dev/cpu/0/msr`.
  - System MMIO powercap override (`/sys/class/powercap/intel-rapl-mmio:0`) with automatic DPTF / `thermald` clamp negotiation.
  - True PL1, PL2, PL4, and Tau time window enforcement.
- **Intel Speed Shift (HWP) & EPP**:
  - Energy Performance Preference (EPP) adjustment across 4 predefined modes (`performance`, `balance_performance`, `balance_power`, `power`) or raw hex values (`0` to `255`).
  - Intel Speed Shift Dynamic Boost toggling.
  - Instant Turbo Boost hardware switch (`no_turbo`).
- **Dynamic Silicon Core Parking**:
  - Adaptive presets that dynamically configure themselves for your exact CPU topology:
    - **All Cores Online**: Full multi-threaded throughput.
    - **No SMT**: Disables hyperthread siblings to cut heat and latency.
    - **P-Cores Only**: Parks high-efficiency compute cores to maximize single-thread cache affinity.
    - **P-Cores + LP Island**: Keeps P-cores and SoC low-power cores active while offlining E-cores.
    - **Single P-Core / Dual P-Core**: Runs 1 or 2 P-cores alongside E-cores.
    - **Low-Power Island Only (LP-Only)**: Parks all compute cores for ultra-low power consumption (2W–4W floor).
    - **Custom**: Granular per-core toggle switches.
- **HP Embedded Controller (EC) & WMI Fan Monitoring**:
  - Reads live fan tachometer RPMs directly from `hp-wmi` hwmon or the Embedded Controller (`EC0`) registers (`0xB0-0xB3`), bypassing the classic HP ACPI 0 RPM bug.
- **Triple Interface**:
  1. **Adaptive GTK / Libhandy GUI**: Responsive 2-column wide layout, live SVG/Cairo power & temperature graphs, and GNOME `power-profiles-daemon` DBus integration.
  2. **Interactive Terminal UI (TUI)**: Fast curses-like dashboard with real-time hotkeys (`[1-6]`, `[T]`, `[D]`, `[C]`, `[B]`, `[R]`).
  3. **CLI Engine & Daemon**: Scriptable commands and continuous background systemd service with AC/Battery auto-switching.

---

## 📊 Pre-Tuned Hardware Profiles

| Profile | Target PL1 / PL2 | EPP Mode | Core Parking | Typical Use Case |
|---|---|---|---|---|
| **Max Turbo** | `65W` / `115W` | `performance` | All Online | Heavy compiling, 3D rendering, benchmarks |
| **Performance** | `45W` / `65W` | `balance_performance` | All Online | Demanding multitasking, sustained burst |
| **Gaming** | `29W` / `45W` | `balance_performance` | All Online (GPU prioritized) | Stable gaming without thermal stuttering |
| **Balanced** | `25W` / `35W` | `balance_power` | All Online | Daily driver, web browsing, smooth thermals |
| **Eco** | `15W` / `25W` | `power` | All Online (Dimmed) | Battery preservation, video playback |
| **Super-Eco** | `2W` / `3.5W` | `power` | LP Island / Single Core | Extreme survival mode (10+ hours battery life) |

---

## 💻 Supported Hardware

- **HP Laptop Families**:
  - HP Omen (15, 16, 17, Transcend)
  - HP Victus (15, 16)
  - HP Pavilion & Pavilion Gaming (14, 15, 16)
  - HP Envy & Envy x360 (13, 14, 15, 16, 17)
  - HP Spectre & Spectre x360 (13, 14, 15, 16)
  - HP EliteBook & ProBook (600, 800, 1000 series)
  - HP 14 / 15 / 17 series (e.g. `15-fd1xxx`, `15-dy`, `15-dw`, etc.)
- **Processor Generations**:
  - Intel Core Ultra (Meteor Lake-H, Meteor Lake-U)
  - 13th & 14th Gen Intel Core (Raptor Lake, Raptor Lake Refresh)
  - 12th Gen Intel Core (Alder Lake)
  - 10th & 11th Gen Intel Core (Ice Lake, Tiger Lake, Comet Lake)
  - Legacy Intel Core i-series (Skylake through Coffee Lake)

---

## 🚀 Installation & Quick Start

### 1. Prerequisites
Ensure Python 3.8+ and required kernel modules are available:

**Ubuntu / Debian / Linux Mint / Zorin OS**:
```bash
sudo apt update
sudo apt install python3 python3-pip
# For graphical GTK / Libhandy GUI:
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-handy-1
```

**Fedora**:
```bash
sudo dnf install python3 python3-gobject gtk3 libhandy
```

**Arch Linux / Manjaro**:
```bash
sudo pacman -S python python-gobject gtk3 libhandy
```

### 2. Install System-Wide
Clone this repository and run the installer:
```bash
git clone https://github.com/walsoup/throttlestop-hp-linux.git
cd throttlestop-hp-linux
sudo ./install.sh
```

This installs:
- Binary launcher to `/usr/local/bin/throttlestop`
- Systemd service to `/etc/systemd/system/throttlestop.service`
- Desktop entry to `/usr/share/applications/throttlestop.desktop`
- Auto-loads `msr` and `ec_sys` kernel modules on boot

---

## 🕹️ Usage

### Launch GUI
```bash
throttlestop --gui
```
*(Or launch **ThrottleStop HP** from your desktop application launcher)*

### Interactive Terminal Dashboard (TUI)
```bash
throttlestop --tui
```
**Interactive Hotkeys**:
- `[1-6]`: Switch profile (Max, Performance, Gaming, Balanced, Eco, Super-Eco)
- `[T]`: Toggle Intel Turbo Boost
- `[D]`: Toggle Speed Shift Dynamic Boost
- `[C]`: Cycle Core Parking modes
- `[B]`: Run built-in multi-threaded verification benchmark (TS Bench)
- `[R]`: Revert to factory stock defaults
- `[Q]`: Quit

### Command-Line Interface (CLI)

```bash
# Check current hardware status & telemetry
throttlestop --status

# Apply a profile
sudo throttlestop --performance
sudo throttlestop --game
sudo throttlestop --balanced
sudo throttlestop --eco
sudo throttlestop --supereco

# Custom Turbo Power Limits (TPL)
sudo throttlestop --pl1 35 --pl2 55 --tau 28

# Energy Performance Preference (EPP)
sudo throttlestop --epp performance
sudo throttlestop --epp 128

# Core Parking
sudo throttlestop --park-cores no_smt
sudo throttlestop --park-cores pcores_only
sudo throttlestop --park-cores none

# Frequency Ceilings
sudo throttlestop --pcore-max-freq 4000 --ecore-max-freq 3000
sudo throttlestop --reset-freq-limits

# Power Balance (CPU vs iGPU allocation)
sudo throttlestop --power-balance gpu_pri
sudo throttlestop --power-balance balanced

# Standalone Fan & EC Monitor
python3 fan_speed.py --monitor
python3 fan_speed.py --dump-ec
```

### Background Daemon & AC/Battery Auto-Switching
Enable continuous automated management via systemd:
```bash
sudo systemctl enable --now throttlestop.service
```

---

## 🛠️ HP Kernel WMI Fan Patch (Optional)

On certain HP models with motherboard IDs not yet in the upstream Linux kernel, `hp-wmi` may report 0 RPM because the BIOS returns 0 on legacy query `0x11`.

- `throttlestop-hp-linux` automatically bypasses this issue by reading EC registers directly.
- If you wish to patch the kernel driver so standard `sensors` reads your fan, see [`patches/README.md`](patches/README.md) and the included patch [`patches/0001-platform-x86-hp-wmi-Add-support-for-HP-Laptop-15-fd1.patch`](patches/0001-platform-x86-hp-wmi-Add-support-for-HP-Laptop-15-fd1.patch).

---

## 📄 License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
