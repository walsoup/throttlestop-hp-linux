# throttlestop-hp-linux

Power limits, Speed Shift, core parking and fan readout for HP laptops with Intel mobile CPUs on Linux. A ThrottleStop-style tool with a GTK window, a terminal dashboard and a CLI.

Developed and tested on an HP 15-fd1xxx (Core Ultra 5 125H, Meteor Lake). Other Intel mobile CPUs and HP models are detected at runtime, but they have not been tested.

## What it does

| Area | What you can set or read |
|---|---|
| Power limits | PL1, PL2, PL4 and Tau, through the MSR and the powercap MMIO zone |
| Speed Shift | Energy Performance Preference (4 presets or 0-255), Dynamic Boost, turbo on/off |
| Core parking | All cores, no SMT, P-cores only, LP island only, single or dual P-core, custom per core |
| Frequency | Per-cluster max frequency for P, E and LP cores |
| Fan | RPM from hp-wmi, or straight from the embedded controller when hp-wmi reports 0 |
| Interfaces | GTK + libhandy GUI, terminal dashboard, CLI, background daemon |

## Profiles

| Flag | PL1 / PL2 | EPP | Cores |
|---|---|---|---|
| `--max` | 65 W / 80 W | performance | all |
| `--performance` | 45 W / 65 W | balance_performance | all |
| `--game` | 29 W / 55 W | balance_performance | all |
| `--balanced` | 25 W / 45 W | balance_power | all |
| `--eco` | 15 W / 22 W | power | all |
| `--supereco` | 2 W / 3.5 W | power | LP island only |

## Install

Needs Python 3.8+ and root for anything that writes to hardware. The GUI also needs GTK 3 and libhandy; without them the launcher falls back to the terminal dashboard.

```bash
# Debian / Ubuntu
sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-handy-1

# Fedora
sudo dnf install python3 python3-gobject gtk3 libhandy

# Arch
sudo pacman -S python python-gobject gtk3 libhandy
```

```bash
git clone https://github.com/walsoup/throttlestop-hp-linux.git
cd throttlestop-hp-linux
sudo ./install.sh
```

The installer copies the files to `/opt/throttlestop-hp-linux`, links `throttlestop` into `/usr/local/bin`, adds a desktop entry and a systemd unit, and loads the `msr` and `ec_sys` modules at boot.

## Use

```bash
throttlestop                # GUI if available, otherwise the terminal dashboard
throttlestop --tui          # terminal dashboard
throttlestop --status       # print current limits and telemetry
```

Dashboard keys: `1`-`6` profile, `T` turbo, `D` dynamic boost, `C` cycle core parking, `B` bench, `R` factory reset, `Q` quit.

```bash
sudo throttlestop --balanced
sudo throttlestop --pl1 35 --pl2 55 --tau 28
sudo throttlestop --epp balance_power
sudo throttlestop --park-cores pcores_only
sudo throttlestop --pcore-max-freq 4000 --ecore-max-freq 3000
sudo throttlestop --reset-freq-limits
sudo throttlestop --revert  # back to stock
python3 fan_speed.py --monitor
```

## Daemon

Applies a profile continuously and switches on AC or battery. Defaults are `max` on AC and `eco` on battery; change them with `--ac-profile` and `--bat-profile`.

```bash
sudo systemctl enable --now throttlestop.service
```

## Fan reads 0 RPM

On some newer HP boards the BIOS returns 0 for the legacy fan query, so hp-wmi shows 0 RPM. This tool reads the embedded controller directly instead, so it works without a patched kernel. If you want `sensors` to show the fan as well, see [patches/README.md](patches/README.md).

## Risks

This writes to CPU power registers and the embedded controller. Raising limits can run the machine hotter than its cooling is built for. `--revert` restores stock settings.

## License

GPL v3. See [LICENSE](LICENSE).
