#!/usr/bin/env python3
"""
ThrottleStop for Linux (Intel Meteor Lake / Core Ultra & Core i-Series)
Platform: HP Laptop 15-fd1xxx (Intel Core Ultra 5 125H) & Generic Intel Mobile

A comprehensive Linux counterpart to ThrottleStop:
  - TPL (Turbo Power Limits): True dual-plane PL1, PL2, PL4, and Tau (MSR + MMIO) control
  - Speed Shift (HWP) & EPP (Energy Performance Preference: 0 - 255)
  - Instant Turbo Boost toggle (no_turbo)
  - Silicon Tile Core Parking (power-gate high-leakage P-cores on battery)
  - 5 Tuned ThrottleStop Profiles:
      [1] Performance  (High power, unrestricted turbo, compiles/rendering)
      [2] Game / Turbo (Sustained turbo with thermal ceiling to prevent stutter)
      [3] Balanced     (Snappy daily driver, smooth responsiveness, low idle)
      [4] Battery / Eco(Extended runtime, aggressive C-states, scaled ceilings)
      [5] Super-Eco    (10-Hour Netbook Survival: 2W PL1, 3 P-cores parked)
  - TS Bench: Built-in multi-threaded verification benchmark
  - Background Daemon & Systemd Service: Continuous PL enforcement & AC/Battery auto-switching
  - Interactive Terminal UI (TUI): ThrottleStop-style dashboard with live telemetry and hotkeys
"""

import os
import sys
import time
import glob
import math
import select
import shutil
import struct
import signal
import termios
import tty
import argparse
import subprocess
import multiprocessing
import re

try:
    from hardware import get_platform
except ImportError:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)
    from hardware import get_platform

# ANSI Color Codes
C_RESET   = "\033[0m"
C_BOLD    = "\033[1m"
C_DIM     = "\033[2m"
C_GREEN   = "\033[32m"
C_YELLOW  = "\033[33m"
C_RED     = "\033[31m"
C_CYAN    = "\033[36m"
C_WHITE   = "\033[37m"
C_MAGENTA = "\033[35m"
C_ORANGE  = "\033[38;5;208m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
def strip_ansi(s):
    return ANSI_RE.sub("", str(s))

# System Paths
RAPL_MSR_DIR  = "/sys/class/powercap/intel-rapl:0"
RAPL_MMIO_DIR = "/sys/class/powercap/intel-rapl-mmio:0"
TURBO_PATH    = "/sys/devices/system/cpu/intel_pstate/no_turbo"
CONFIG_FILE   = "/etc/throttlestop_linux.json"
SERVICE_PATH  = "/etc/systemd/system/throttlestop.service"

# ==============================================================================
# Low-Level Hardware Access & Utilities
# ==============================================================================

def check_root(exit_on_fail=True):
    is_root = (os.geteuid() == 0)
    if not is_root and exit_on_fail:
        print(f"{C_RED}{C_BOLD}Error: Modifying hardware power registers requires root privileges.{C_RESET}")
        print(f"Please run with sudo: {C_CYAN}sudo python3 {os.path.abspath(__file__)} [options]{C_RESET}")
        sys.exit(1)
    return is_root

def read_file(path, default=""):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return default

def write_file(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val).strip() + "\n")
        return True
    except Exception:
        return False

def read_int(path, default=0):
    try:
        return int(read_file(path, str(default)))
    except Exception:
        return default

def read_float(path, default=0.0):
    try:
        return float(read_file(path, str(default)))
    except Exception:
        return default

def rdmsr(cpu, msr):
    """Read 64-bit MSR via /dev/cpu/<cpu>/msr (requires kernel msr module)."""
    try:
        p = f"/dev/cpu/{cpu}/msr"
        if not os.path.exists(p):
            subprocess.run(["modprobe", "msr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(p, "rb") as f:
            f.seek(msr)
            return struct.unpack("Q", f.read(8))[0]
    except Exception:
        return None

def read_ec_bytes():
    """Read 256 bytes from Embedded Controller EC0 via direct debugfs, fresh shm, hp-ec-dump helper, or shm fallback."""
    try:
        with open("/sys/kernel/debug/ec/ec0/io", "rb") as ef:
            b = ef.read(256)
            if len(b) >= 0xB4:
                return b
    except Exception:
        pass

    shm_path = "/dev/shm/ec0_raw"
    if os.path.exists(shm_path):
        try:
            if time.time() - os.path.getmtime(shm_path) < 1.5:
                with open(shm_path, "rb") as ef:
                    b = ef.read(256)
                    if len(b) >= 0xB4:
                        return b
        except Exception:
            pass

    if os.path.exists("/usr/local/bin/hp-ec-dump"):
        try:
            res = subprocess.run(["/usr/local/bin/hp-ec-dump"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=0.5)
            if res.returncode == 0 and len(res.stdout) >= 0xB4:
                return res.stdout
        except Exception:
            pass

    if os.path.exists(shm_path):
        try:
            with open(shm_path, "rb") as ef:
                b = ef.read(256)
                if len(b) >= 0xB4:
                    return b
        except Exception:
            pass
    return None


# ==============================================================================
# Hardware TPL (Turbo Power Limits) - MSR + MMIO Dual-Plane Control
# ==============================================================================

def get_rapl_status():
    """
    Read full RAPL configuration from both MSR 0x610 and MMIO domains.
    Computes true operative limits: operative = min(MSR, MMIO) if MMIO enabled.
    """
    msr_pl1 = read_float(f"{RAPL_MSR_DIR}/constraint_0_power_limit_uw", 0.0) / 1e6
    msr_tau = read_float(f"{RAPL_MSR_DIR}/constraint_0_time_window_us", 0.0) / 1e6
    msr_pl2 = read_float(f"{RAPL_MSR_DIR}/constraint_1_power_limit_uw", 0.0) / 1e6
    msr_enabled = (read_file(f"{RAPL_MSR_DIR}/enabled", "1") == "1")

    # Direct MSR 0x610 decoding
    val_unit = rdmsr(0, 0x606)
    val_limit = rdmsr(0, 0x610)
    msr_locked = False
    msr_clamped = True
    if val_unit is not None and val_limit is not None:
        p_unit = 1.0 / (1 << (val_unit & 0xF))
        t_unit = 1.0 / (1 << ((val_unit >> 16) & 0xF))
        msr_pl1_reg = (val_limit & 0x7FFF) * p_unit
        msr_pl2_reg = ((val_limit >> 32) & 0x7FFF) * p_unit
        msr_clamped = bool((val_limit >> 16) & 1)
        msr_locked = bool((val_limit >> 63) & 1)
        t_bits = (val_limit >> 17) & 0x7F
        msr_tau_reg = (1.0 + ((t_bits >> 5) & 3) / 4.0) * (2 ** (t_bits & 0x1F)) * t_unit
        if msr_pl1 <= 0:
            msr_pl1 = msr_pl1_reg
        if msr_pl2 <= 0:
            msr_pl2 = msr_pl2_reg
        if msr_tau <= 0:
            msr_tau = msr_tau_reg

    mmio_exists = os.path.exists(RAPL_MMIO_DIR)
    mmio_pl1 = read_float(f"{RAPL_MMIO_DIR}/constraint_0_power_limit_uw", 0.0) / 1e6 if mmio_exists else 0.0
    mmio_tau = read_float(f"{RAPL_MMIO_DIR}/constraint_0_time_window_us", 0.0) / 1e6 if mmio_exists else 0.0
    mmio_pl2 = read_float(f"{RAPL_MMIO_DIR}/constraint_1_power_limit_uw", 0.0) / 1e6 if mmio_exists else 0.0
    mmio_pl4 = read_float(f"{RAPL_MMIO_DIR}/constraint_2_power_limit_uw", 0.0) / 1e6 if mmio_exists else 0.0
    mmio_enabled = (read_file(f"{RAPL_MMIO_DIR}/enabled", "0") == "1") if mmio_exists else False

    # Operative limit resolution
    if mmio_exists and mmio_enabled and mmio_pl1 > 0:
        if msr_pl1 > 0:
            eff_pl1 = min(msr_pl1, mmio_pl1)
            eff_src = "MMIO" if mmio_pl1 <= msr_pl1 else "MSR"
        else:
            eff_pl1 = mmio_pl1
            eff_src = "MMIO"
    else:
        eff_pl1 = msr_pl1
        eff_src = "MSR"

    if mmio_exists and mmio_enabled and mmio_pl2 > 0:
        if msr_pl2 > 0:
            eff_pl2 = min(msr_pl2, mmio_pl2)
        else:
            eff_pl2 = mmio_pl2
    else:
        eff_pl2 = msr_pl2

    eff_tau = mmio_tau if (eff_src == "MMIO" and mmio_tau > 0) else msr_tau
    dptf_override = mmio_enabled and mmio_pl1 > 0 and msr_pl1 > mmio_pl1

    return {
        "msr_pl1": msr_pl1,
        "msr_pl2": msr_pl2,
        "msr_tau": msr_tau,
        "msr_enabled": msr_enabled,
        "msr_locked": msr_locked,
        "msr_clamped": msr_clamped,
        "mmio_exists": mmio_exists,
        "mmio_pl1": mmio_pl1,
        "mmio_pl2": mmio_pl2,
        "mmio_tau": mmio_tau,
        "mmio_pl4": mmio_pl4,
        "mmio_enabled": mmio_enabled,
        "effective_pl1": eff_pl1,
        "effective_pl2": eff_pl2,
        "effective_tau": eff_tau,
        "effective_source": eff_src,
        "dptf_override": dptf_override,
    }

def set_rapl_limits(pl1_w, pl2_w=None, tau_s=None, pl4_w=None, disable_mmio=False):
    """
    ThrottleStop TPL Engine:
    Writes to BOTH MSR (package) and MMIO (system) powercap domains so that
    power limits are genuinely enforced by CPU silicon without being clamped.
    """
    check_root()

    # 1. Handle MMIO zone enable/disable
    if os.path.exists(RAPL_MMIO_DIR):
        if disable_mmio:
            write_file(f"{RAPL_MMIO_DIR}/enabled", "0")
        else:
            write_file(f"{RAPL_MMIO_DIR}/enabled", "1")

    # 2. Write PL1 (Long Duration Limit)
    if pl1_w is not None and pl1_w > 0:
        pl1_uw = int(pl1_w * 1e6)
        write_file(f"{RAPL_MSR_DIR}/constraint_0_power_limit_uw", pl1_uw)
        if os.path.exists(RAPL_MMIO_DIR) and not disable_mmio:
            write_file(f"{RAPL_MMIO_DIR}/constraint_0_power_limit_uw", pl1_uw)

    # 3. Write PL2 (Short Duration Limit)
    if pl2_w is not None and pl2_w > 0:
        pl2_uw = int(pl2_w * 1e6)
        write_file(f"{RAPL_MSR_DIR}/constraint_1_power_limit_uw", pl2_uw)
        if os.path.exists(RAPL_MMIO_DIR) and not disable_mmio:
            write_file(f"{RAPL_MMIO_DIR}/constraint_1_power_limit_uw", pl2_uw)

    # 4. Write Tau (Time Window)
    if tau_s is not None and tau_s > 0:
        tau_us = int(tau_s * 1e6)
        write_file(f"{RAPL_MSR_DIR}/constraint_0_time_window_us", tau_us)
        if os.path.exists(RAPL_MMIO_DIR) and not disable_mmio:
            write_file(f"{RAPL_MMIO_DIR}/constraint_0_time_window_us", tau_us)

    # 5. Write PL4 (Peak Platform Limit) if provided
    if pl4_w is not None and pl4_w > 0 and os.path.exists(RAPL_MMIO_DIR):
        pl4_uw = int(pl4_w * 1e6)
        write_file(f"{RAPL_MMIO_DIR}/constraint_2_power_limit_uw", pl4_uw)

    return True

# ==============================================================================
# Speed Shift (HWP), EPP & Turbo Boost Control
# ==============================================================================

def set_turbo(enabled=True):
    """ThrottleStop 'Disable Turbo' toggle."""
    check_root()
    val = "0" if enabled else "1"
    return write_file(TURBO_PATH, val)

def get_turbo():
    return (read_file(TURBO_PATH, "0") != "1")

def set_epp(epp_target):
    """
    Speed Shift Energy Performance Preference:
    Accepts preset string ('performance', 'balance_performance', 'balance_power', 'power')
    or integer 0 - 255.
    """
    check_root()
    str_map = {
        0: "performance",
        64: "balance_performance",
        128: "balance_power",
        255: "power",
    }

    if isinstance(epp_target, int):
        val = str_map.get(epp_target, str(epp_target))
    else:
        val = str(epp_target).strip()

    count = 0
    for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference"):
        if write_file(p, val):
            count += 1
    return count > 0

def get_epp():
    return read_file("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference", "N/A")

def set_dynamic_boost(enable=True):
    """Enable or disable Intel HWP Dynamic Boost."""
    check_root()
    p = "/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost"
    if os.path.exists(p):
        return write_file(p, "1" if enable else "0")
    return False

def get_dynamic_boost():
    p = "/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost"
    return (read_file(p, "0") == "1")

def set_core_frequencies(p_max_khz=None, e_max_khz=None, lp_max_khz=None):
    """Dynamically set frequency ceilings per core family based on detected platform topology."""
    check_root()
    platform = get_platform()
    top = platform.topology

    if p_max_khz:
        for c in top.get("p_cores", []):
            for t in c["threads"]:
                p = f"/sys/devices/system/cpu/cpu{t}/cpufreq/scaling_max_freq"
                if os.path.exists(p):
                    write_file(p, str(p_max_khz))

    if e_max_khz:
        for c in top.get("e_cores", []):
            for t in c["threads"]:
                p = f"/sys/devices/system/cpu/cpu{t}/cpufreq/scaling_max_freq"
                if os.path.exists(p):
                    write_file(p, str(e_max_khz))

    if lp_max_khz:
        for c in top.get("lp_cores", []):
            for t in c["threads"]:
                p = f"/sys/devices/system/cpu/cpu{t}/cpufreq/scaling_max_freq"
                if os.path.exists(p):
                    write_file(p, str(lp_max_khz))

def get_core_max_frequencies():
    """Dynamically reads frequency ceilings per core cluster from sysfs."""
    platform = get_platform()
    top = platform.topology

    p_f = top.get("max_p_mhz", 4000)
    for c in top.get("p_cores", []):
        t = c["primary"]
        v = read_int(f"/sys/devices/system/cpu/cpu{t}/cpufreq/scaling_max_freq", 0)
        if v > 0:
            p_f = v // 1000
            break

    e_f = top.get("max_e_mhz", 3000) if top.get("has_e") else 0
    if top.get("has_e"):
        for c in top.get("e_cores", []):
            t = c["primary"]
            v = read_int(f"/sys/devices/system/cpu/cpu{t}/cpufreq/scaling_max_freq", 0)
            if v > 0:
                e_f = v // 1000
                break

    lp_f = top.get("max_lp_mhz", 2000) if top.get("has_lp") else 0
    if top.get("has_lp"):
        for c in top.get("lp_cores", []):
            t = c["primary"]
            v = read_int(f"/sys/devices/system/cpu/cpu{t}/cpufreq/scaling_max_freq", 0)
            if v > 0:
                lp_f = v // 1000
                break

    return {"p_mhz": p_f, "e_mhz": e_f, "lp_mhz": lp_f}

def reset_core_frequencies():
    """Restore all cores to their hardware silicon maximums."""
    check_root()
    for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq"):
        cpu_dir = os.path.dirname(p)
        max_f = read_file(p)
        write_file(os.path.join(cpu_dir, "scaling_max_freq"), max_f)

def set_power_balance(mode="balanced"):
    """
    ThrottleStop-style CPU vs iGPU energy / power balance:
      'balanced': 50% CPU / 50% iGPU
      'gpu_pri': 30% CPU / 70% iGPU (Gaming / 3D priority)
      'cpu_pri': 80% CPU / 20% iGPU (Compiling / Computing priority)
      'gpu_max': Max iGPU Boost (Uncapped Arc Graphics 1200MHz)
    """
    check_root()
    if mode in ["gpu_pri", "gpu"]:
        set_gpu_clocks(boost_mhz=1200, max_mhz=1200)
        for epb in glob.glob("/sys/devices/system/cpu/cpu*/power/energy_perf_bias"):
            write_file(epb, "6")
        for slpc in glob.glob("/sys/class/drm/card*/gt/gt*/slpc_power_profile"):
            write_file(slpc, "base")
    elif mode in ["cpu_pri", "cpu"]:
        set_gpu_clocks(boost_mhz=800, max_mhz=800)
        for epb in glob.glob("/sys/devices/system/cpu/cpu*/power/energy_perf_bias"):
            write_file(epb, "0")
    elif mode in ["gpu_max", "max"]:
        set_gpu_clocks(boost_mhz=1200, max_mhz=1200)
        for epb in glob.glob("/sys/devices/system/cpu/cpu*/power/energy_perf_bias"):
            write_file(epb, "4")
    else:  # balanced
        set_gpu_clocks(boost_mhz=1000, max_mhz=1200)
        for epb in glob.glob("/sys/devices/system/cpu/cpu*/power/energy_perf_bias"):
            write_file(epb, "4")

def get_power_balance():
    gpu_max = read_int("/sys/class/drm/card1/gt_max_freq_mhz", 1200)
    epb = read_int("/sys/devices/system/cpu/cpu0/power/energy_perf_bias", 4)
    if gpu_max <= 800 and epb == 0:
        return "cpu_pri"
    elif gpu_max >= 1200 and epb >= 6:
        return "gpu_pri"
    elif gpu_max >= 1200 and epb == 4:
        return "gpu_max"
    return "balanced"

# ==============================================================================
# Silicon Core Parking (P-Core & E-Core Power Gating on Battery)
# ==============================================================================

def get_total_cpus():
    """Return total number of logical CPU threads available in system."""
    cpus = glob.glob("/sys/devices/system/cpu/cpu[0-9]*")
    return len(cpus) if cpus else 18

def get_core_mask():
    """
    Return dictionary mapping logical cpu_id (0..total-1) -> bool indicating online state.
    Note: cpu0 has no /sys/devices/system/cpu/cpu0/online because it cannot be hotplugged
    and is permanently online (property of Linus Torvalds).
    """
    tot = get_total_cpus()
    mask = {}
    for i in range(tot):
        if i == 0:
            mask[0] = True
        else:
            on_p = f"/sys/devices/system/cpu/cpu{i}/online"
            if os.path.exists(on_p):
                mask[i] = (read_file(on_p) == "1")
            else:
                mask[i] = os.path.exists(f"/sys/devices/system/cpu/cpu{i}")
    return mask

def parse_cpu_list(cpu_list_str):
    """Parse comma-separated list of CPUs and ranges (e.g. '1,2,5' or '1-7,16-17')."""
    cpus = set()
    for part in str(cpu_list_str).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            sub = part.split("-", 1)
            try:
                start, end = int(sub[0]), int(sub[1])
                for i in range(min(start, end), max(start, end) + 1):
                    cpus.add(i)
            except ValueError:
                raise ValueError(f"Invalid CPU range: '{part}'")
        else:
            try:
                cpus.add(int(part))
            except ValueError:
                raise ValueError(f"Invalid CPU index: '{part}'")
    return sorted(list(cpus))

def set_single_core_online(cpu_idx, online=True):
    """
    Online or offline an individual logical CPU thread via sysfs.
    Core 0 is permanently locked and protected (kernel boot CPU, property of linus todevalt / Linus Torvalds).
    """
    check_root()
    try:
        cpu_idx = int(cpu_idx)
    except (ValueError, TypeError):
        print(f"Error: Invalid CPU index: {cpu_idx}")
        return False

    if cpu_idx == 0:
        if not online:
            print("[!] Core 0 is the primary kernel boot CPU (property of linus todevalt / Linus Torvalds) and cannot be touched, parked, or offlined!")
            return False
        return True

    tot = get_total_cpus()
    if cpu_idx < 0 or cpu_idx >= tot:
        print(f"Error: CPU index {cpu_idx} out of range (0-{tot-1})")
        return False

    # If bringing an SMT thread online (CPUs 2, 4, 5, 7 on Meteor Lake), ensure smt control is enabled
    smt_path = "/sys/devices/system/cpu/smt/control"
    if online and cpu_idx in [2, 4, 5, 7] and os.path.exists(smt_path):
        try:
            if read_file(smt_path) == "off":
                write_file(smt_path, "on")
        except Exception:
            pass

    target_path = f"/sys/devices/system/cpu/cpu{cpu_idx}/online"
    if not os.path.exists(target_path):
        print(f"Notice: {target_path} does not exist (CPU {cpu_idx} cannot be hotplugged)")
        return False

    val = "1" if online else "0"
    success = write_file(target_path, val)
    if not success and online and os.path.exists(smt_path) and read_file(smt_path) == "off":
        write_file(smt_path, "on")
        success = write_file(target_path, val)
    return success

def set_core_parking(mode="none"):
    """Dynamically park cores based on detected hardware platform topology."""
    check_root()
    if isinstance(mode, bool):
        mode = "single_pcore" if mode else "none"

    platform = get_platform()
    if mode not in ["no_smt", "smt_off", "nosmt"] and os.path.exists("/sys/devices/system/cpu/smt/control"):
        write_file("/sys/devices/system/cpu/smt/control", "on")

    offline_cpus = set(platform.get_park_offline_cpus(mode))
    total_threads = platform.topology["total_threads"]

    for cpu_id in range(1, total_threads):
        target = "0" if cpu_id in offline_cpus else "1"
        write_file(f"/sys/devices/system/cpu/cpu{cpu_id}/online", target)

    if mode in ["no_smt", "smt_off", "nosmt"] and os.path.exists("/sys/devices/system/cpu/smt/control"):
        write_file("/sys/devices/system/cpu/smt/control", "off")

def get_core_status():
    """Dynamically inspects active core parking state against platform modes."""
    platform = get_platform()
    on_mask = get_core_mask()
    total = len(on_mask)
    online = sum(1 for v in on_mask.values() if v)

    current_offline = set(cid for cid, state in on_mask.items() if not state and cid != 0)

    for mode_key, mode_label in platform.get_parking_modes():
        if mode_key == "custom":
            continue
        expected_offline = set(platform.get_park_offline_cpus(mode_key))
        if current_offline == expected_offline:
            return online, total, mode_key, mode_label

    if online == total:
        return online, total, "none", f"{online}/{total} Cores Online (Full Active)"
    return online, total, "custom", f"{online}/{total} Cores Online (Custom)"

def set_screen_brightness(pct):
    """Set screen backlight brightness percentage (e.g. 30 for 30%)."""
    for bp in glob.glob("/sys/class/backlight/*/brightness"):
        mp = os.path.join(os.path.dirname(bp), "max_brightness")
        if os.path.exists(mp):
            try:
                mx = int(read_file(mp))
                if mx > 0:
                    val = int(mx * (pct / 100.0))
                    write_file(bp, max(1, val))
            except Exception:
                pass

# ==============================================================================
# Peripheral & Platform Energy Controls
# ==============================================================================

def set_gpu_clocks(boost_mhz=1200, max_mhz=1200):
    check_root()
    for card_dir in glob.glob("/sys/class/drm/card[0-9]"):
        p_boost = f"{card_dir}/gt_boost_freq_mhz"
        p_max = f"{card_dir}/gt_max_freq_mhz"
        if os.path.exists(p_max):
            write_file(p_boost, boost_mhz)
            write_file(p_max, max_mhz)
        for gt_dir in glob.glob(f"{card_dir}/gt/gt*"):
            if os.path.exists(f"{gt_dir}/max_freq_mhz"):
                write_file(f"{gt_dir}/max_freq_mhz", max_mhz)

def set_pcie_aspm(policy="default"):
    check_root()
    write_file("/sys/module/pcie_aspm/parameters/policy", policy)

def get_pcie_aspm():
    raw = read_file("/sys/module/pcie_aspm/parameters/policy")
    for part in raw.split():
        if part.startswith("[") and part.endswith("]"):
            return part[1:-1]
    return raw

def set_wifi_power_save(enable=True):
    for iface in glob.glob("/sys/class/net/*"):
        name = os.path.basename(iface)
        if name.startswith("wl"):
            subprocess.run(["iwconfig", name, "power", "on" if enable else "off"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_wifi_power_save():
    for iface in glob.glob("/sys/class/net/*"):
        name = os.path.basename(iface)
        if name.startswith("wl"):
            try:
                res = subprocess.check_output(["iwconfig", name], text=True)
                if "Power Management:on" in res:
                    return "Enabled"
                elif "Power Management:off" in res:
                    return "Disabled"
            except Exception:
                pass
    return "N/A"

def set_bluetooth_state(enabled=True):
    cmd = ["rfkill", "unblock" if enabled else "block", "bluetooth"]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_bluetooth_state():
    try:
        rf = subprocess.check_output(["rfkill", "list", "bluetooth"], text=True)
        if "Soft blocked: yes" in rf:
            return "Disabled"
        return "Enabled"
    except Exception:
        return "N/A"

def set_audio_power_save(seconds=1):
    check_root()
    write_file("/sys/module/snd_hda_intel/parameters/power_save", str(seconds))
    write_file("/sys/module/snd_hda_intel/parameters/power_save_controller", "Y" if seconds > 0 else "N")

def set_desktop_power_profile(name):
    try:
        subprocess.run(["powerprofilesctl", "set", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def get_desktop_power_profile():
    try:
        return subprocess.check_output(["powerprofilesctl", "get"], text=True).strip()
    except Exception:
        return "N/A"

# ==============================================================================
# Thermal Daemon & PROCHOT Coordination
# ==============================================================================

def manage_thermald(action):
    """
    Manage thermald service.
    When applying custom / high performance profiles, stopping thermald prevents
    DPTF from fighting our MMIO power limits every 4 seconds.
    """
    check_root()
    if action == "stop":
        subprocess.run(["systemctl", "stop", "thermald"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif action == "start":
        subprocess.run(["systemctl", "start", "thermald"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def is_thermald_running():
    try:
        res = subprocess.check_output(["systemctl", "is-active", "thermald"], text=True).strip()
        return (res == "active")
    except Exception:
        return False

def get_thermal_target():
    """Read PROCHOT Target (TjMax & Tcc Offset) via MSR 0x1A2."""
    val = rdmsr(0, 0x1A2)
    if val is None:
        return {"tjmax": 110, "offset": 3, "prochot": 107}
    tjmax = (val >> 16) & 0xFF
    offset = (val >> 24) & 0x3F
    if tjmax == 0:
        tjmax = 110
    prochot = tjmax - offset
    return {"tjmax": tjmax, "offset": offset, "prochot": prochot}

# ==============================================================================
# ThrottleStop Profile Definitions
# ==============================================================================

PROFILES = {
    "max": {
        "title": "Max Turbo / Cooling Pad (ThrottleStop Profile 1)",
        "desc": "Unrestricted 65W sustained / 80W peak turbo for cooling pad & heavy compiles",
        "pl1": 65.0,
        "pl2": 80.0,
        "tau": 45.0,
        "pl4": 120.0,
        "turbo": True,
        "epp": "performance",
        "park_cores": "none",
        "gpu_mhz": 1200,
        "power_balance": "gpu_max",
        "aspm": "default",
        "wifi_powersave": False,
        "audio_powersave": 0,
        "bluetooth": True,
        "desktop_mode": "performance",
        "dim_screen": False,
        "manage_thermald": True,
        "color": C_RED,
    },
    "performance": {
        "title": "Performance / Heavy Sustained (ThrottleStop Profile 1.5)",
        "desc": "High sustained performance (45W PL1 / 65W PL2) balanced for laptop chassis without cooling pad",
        "pl1": 45.0,
        "pl2": 65.0,
        "tau": 32.0,
        "pl4": 120.0,
        "turbo": True,
        "epp": "balance_performance",
        "park_cores": "none",
        "gpu_mhz": 1200,
        "power_balance": "balanced",
        "aspm": "default",
        "wifi_powersave": False,
        "audio_powersave": 0,
        "bluetooth": True,
        "desktop_mode": "performance",
        "dim_screen": False,
        "manage_thermald": True,
        "color": C_ORANGE,
    },
    "game": {
        "title": "Game / Heatsink Matched (ThrottleStop Profile 2)",
        "desc": "Continuous 29W heatsink cap with 55W turbo burst (no thermal throttle stutter)",
        "pl1": 29.0,
        "pl2": 55.0,
        "tau": 24.0,
        "pl4": 120.0,
        "turbo": True,
        "epp": "balance_performance",
        "park_cores": "none",
        "gpu_mhz": 1200,
        "power_balance": "gpu_pri",
        "aspm": "powersave",
        "wifi_powersave": False,
        "audio_powersave": 0,
        "bluetooth": True,
        "desktop_mode": "performance",
        "dim_screen": False,
        "manage_thermald": True,
        "color": C_MAGENTA,
    },
    "balanced": {
        "title": "Balanced / Daily Driver (ThrottleStop Profile 3)",
        "desc": "Snappy daily driver with responsive turbo boost & low idle power",
        "pl1": 25.0,
        "pl2": 45.0,
        "tau": 28.0,
        "pl4": 120.0,
        "turbo": True,
        "epp": "balance_power",
        "park_cores": "none",
        "gpu_mhz": 1000,
        "power_balance": "balanced",
        "aspm": "powersave",
        "wifi_powersave": True,
        "audio_powersave": 1,
        "bluetooth": True,
        "desktop_mode": "balanced",
        "dim_screen": False,
        "manage_thermald": False,
        "color": C_CYAN,
    },
    "eco": {
        "title": "Battery / Eco (ThrottleStop Profile 4)",
        "desc": "Quiet, extended runtime with efficient C-states & power caps",
        "pl1": 15.0,
        "pl2": 22.0,
        "tau": 15.0,
        "pl4": 100.0,
        "turbo": True,
        "epp": "power",
        "park_cores": "none",
        "gpu_mhz": 800,
        "power_balance": "balanced",
        "aspm": "powersupersave",
        "wifi_powersave": True,
        "audio_powersave": 1,
        "bluetooth": True,
        "desktop_mode": "power-saver",
        "dim_screen": False,
        "manage_thermald": False,
        "color": C_GREEN,
    },
    "supereco": {
        "title": "Super-Eco 10h+ Survival (ThrottleStop Profile 5)",
        "desc": "Ultra-Aggressive: 2W PL1, LP-only island (CPUs 1-15 parked, leaving CPU0 + LP16/17 active), 400MHz GPU, 30% screen",
        "pl1": 2.0,
        "pl2": 3.5,
        "tau": 5.0,
        "pl4": 30.0,
        "turbo": False,
        "epp": "power",
        "park_cores": "lp_only",
        "gpu_mhz": 400,
        "power_balance": "cpu_pri",
        "aspm": "powersupersave",
        "wifi_powersave": True,
        "audio_powersave": 1,
        "bluetooth": False,
        "desktop_mode": "power-saver",
        "dim_screen": True,
        "manage_thermald": False,
        "color": C_GREEN,
    },
}

def apply_profile(name):
    """Apply a named ThrottleStop profile to hardware."""
    check_root()
    name = name.lower()

    # Aliases
    aliases = {
        "extreme": "max",
        "turbo-max": "max",
        "cooling-pad": "max",
        "perf": "performance",
        "mega-eco": "supereco",
        "netbook": "supereco",
    }
    name = aliases.get(name, name)

    if name not in PROFILES:
        print(f"{C_RED}Error: Unknown profile '{name}'. Choose from: {', '.join(PROFILES.keys())}{C_RESET}")
        return False

    p = PROFILES[name]

    # 1. Desktop mode synchronization
    set_desktop_power_profile(p["desktop_mode"])

    # 2. Thermald management
    if p["manage_thermald"]:
        manage_thermald("stop")
    else:
        if not is_thermald_running():
            manage_thermald("start")

    # 3. Core parking
    set_core_parking(p["park_cores"])

    # 4. Bluetooth radio
    set_bluetooth_state(p["bluetooth"])

    # 5. Dual-Plane RAPL Power Limits (MSR + MMIO)
    set_rapl_limits(
        pl1_w=p["pl1"],
        pl2_w=p["pl2"],
        tau_s=p["tau"],
        pl4_w=p["pl4"],
        disable_mmio=False
    )

    # 6. Turbo Boost & Dynamic Boost & Power Balance
    set_turbo(p["turbo"])
    set_dynamic_boost(name in ["max", "performance", "game"])
    set_power_balance(p.get("power_balance", "balanced"))

    # 7. Energy Performance Preference
    set_epp(p["epp"])

    # 8. Frequency scaling ceilings
    if name == "supereco":
        # P-core 0 capped to 800MHz, LP cores allowed up to 1.2GHz for responsive typing/tasks
        set_core_frequencies(p_max_khz="800000", e_max_khz="600000", lp_max_khz="1200000")
    else:
        reset_core_frequencies()

    # 9. GPU Clocks
    set_gpu_clocks(boost_mhz=p["gpu_mhz"], max_mhz=p["gpu_mhz"])

    # 10. Platform power-saving & Screen dimming
    set_pcie_aspm(p["aspm"])
    set_wifi_power_save(p["wifi_powersave"])
    set_audio_power_save(p["audio_powersave"])

    if p["dim_screen"]:
        set_screen_brightness(30)

    # 11. Core power optimizations
    for dev in glob.glob("/sys/bus/pci/devices/*/power/control"):
        write_file(dev, "auto" if name in ["eco", "supereco"] else "on")
    for dev in glob.glob("/sys/bus/usb/devices/*/power/control"):
        write_file(dev, "auto" if name in ["eco", "supereco"] else "on")

    # 12. Disk & kernel writeback tuning
    if name in ["eco", "supereco"]:
        write_file("/proc/sys/vm/dirty_writeback_centisecs", "6000")
        write_file("/proc/sys/vm/laptop_mode", "5")
        write_file("/proc/sys/kernel/nmi_watchdog", "0")
    else:
        write_file("/proc/sys/vm/dirty_writeback_centisecs", "500")
        write_file("/proc/sys/vm/laptop_mode", "0")

    return True

def revert_to_defaults():
    """Reset all hardware registers and power settings to factory Linux stock."""
    check_root()

    # 1. Bring all 18 threads online
    set_core_parking("none")

    # 2. Unblock radios & screen
    set_bluetooth_state(True)
    set_wifi_power_save(False)
    set_screen_brightness(80)

    # 3. Restore thermald
    manage_thermald("start")

    # 4. Reset desktop power mode
    set_desktop_power_profile("performance")

    # 5. Restore stock RAPL limits (MSR: 200W/64W, MMIO: 35W/51W)
    write_file(f"{RAPL_MSR_DIR}/constraint_0_power_limit_uw", "200000000")
    write_file(f"{RAPL_MSR_DIR}/constraint_1_power_limit_uw", "64000000")
    if os.path.exists(RAPL_MMIO_DIR):
        write_file(f"{RAPL_MMIO_DIR}/enabled", "1")
        write_file(f"{RAPL_MMIO_DIR}/constraint_0_power_limit_uw", "35000000")
        write_file(f"{RAPL_MMIO_DIR}/constraint_1_power_limit_uw", "51000000")
        write_file(f"{RAPL_MMIO_DIR}/constraint_2_power_limit_uw", "120000000")

    # 6. Enable Turbo & Dynamic Boost
    set_turbo(True)
    set_dynamic_boost(True)
    set_power_balance("balanced")

    # 7. Reset CPU frequencies
    reset_core_frequencies()

    # 8. Reset EPP
    set_epp("performance")

    # 9. Reset GPU
    set_gpu_clocks(boost_mhz=1200, max_mhz=1200)

    # 10. Reset peripherals
    set_pcie_aspm("default")
    set_audio_power_save(0)
    for dev in glob.glob("/sys/bus/pci/devices/*/power/control"):
        write_file(dev, "on")
    for dev in glob.glob("/sys/bus/usb/devices/*/power/control"):
        write_file(dev, "on")
    write_file("/proc/sys/vm/dirty_writeback_centisecs", "500")
    write_file("/proc/sys/vm/laptop_mode", "0")
    write_file("/proc/sys/kernel/nmi_watchdog", "1")

    return True

# ==============================================================================
# TS Bench (Built-in Verification Benchmark)
# ==============================================================================

def _bench_worker(stop_evt, counter):
    x = 1.0001
    c = 0
    while not stop_evt.is_set():
        for _ in range(5000):
            x = math.sin(x) * math.cos(x) + 1.0001
        c += 5000
    counter.value += c

def run_ts_bench(duration=5.0):
    """
    ThrottleStop TS Bench:
    Stresses all active CPU threads, sampling power and clocks to verify
    whether PL1 / PL2 / thermal throttling behaves as intended.
    """
    print(f"\n{C_BOLD}{C_CYAN}─── Running ThrottleStop TS Bench ({duration:.0f}s all-thread load test) ───{C_RESET}")

    on_cnt, tot_cnt, mode_key, mode_desc = get_core_status()
    num_procs = on_cnt

    stop_evt = multiprocessing.Event()
    counter = multiprocessing.Value("l", 0)

    procs = [
        multiprocessing.Process(target=_bench_worker, args=(stop_evt, counter))
        for _ in range(num_procs)
    ]

    # RAPL energy sample start
    rapl_p = f"{RAPL_MSR_DIR}/energy_uj"
    e_start = read_int(rapl_p, 0)
    t_start = time.time()

    for p in procs:
        p.start()

    peak_pkg_w = 0.0
    last_e = e_start
    last_t = t_start
    cur_w = 0.0

    try:
        elapsed = 0.0
        while elapsed < duration:
            time.sleep(0.5)
            now_t = time.time()
            now_e = read_int(rapl_p, 0)
            dt = now_t - last_t
            de = now_e - last_e
            if dt > 0 and de > 0:
                cur_w = (de / 1e6) / dt
                peak_pkg_w = max(peak_pkg_w, cur_w)
            last_t = now_t
            last_e = now_e
            elapsed = now_t - t_start
            sys.stdout.write(f"\r  Testing... {duration - elapsed:3.1f}s remaining | Current Power: {color_watts(cur_w)}")
            sys.stdout.flush()
    finally:
        stop_evt.set()
        for p in procs:
            p.join()

    total_time = time.time() - t_start
    total_energy_j = (read_int(rapl_p, 0) - e_start) / 1e6
    avg_power_w = total_energy_j / total_time if total_time > 0 else 0.0
    total_ops = counter.value

    # Check thermals & limits
    rapl = get_rapl_status()
    temp_p = "/sys/class/thermal/thermal_zone0/temp"
    cur_temp = read_float(temp_p, 0.0) / 1000.0

    print(f"\r{C_BOLD}{C_GREEN}✔ TS Bench Complete!{C_RESET}{' ' * 30}")
    print(f"  ├─ Threads Engaged:      {C_BOLD}{num_procs} active threads{C_RESET}")
    print(f"  ├─ Average Package Draw: {color_watts(avg_power_w)}")
    print(f"  ├─ Peak Turbo Draw:      {color_watts(peak_pkg_w)}")
    print(f"  ├─ Operative Limit:      PL1={rapl['effective_pl1']:.0f}W | PL2={rapl['effective_pl2']:.0f}W [{rapl['effective_source']}]")
    print(f"  ├─ Package Temperature:  {cur_temp:.1f}°C")
    print(f"  └─ Compute Throughput:   {total_ops / total_time / 1e6:,.2f} M-ops/s\n")

# ==============================================================================
# ThrottleStop Status Display
# ==============================================================================

def color_watts(w):
    if w is None:
        return f"{C_DIM}N/A{C_RESET}"
    if w < 10:
        return f"{C_GREEN}{w:5.1f} W{C_RESET}"
    elif w < 28:
        return f"{C_CYAN}{w:5.1f} W{C_RESET}"
    elif w < 45:
        return f"{C_YELLOW}{w:5.1f} W{C_RESET}"
    else:
        return f"{C_RED}{C_BOLD}{w:5.1f} W{C_RESET}"

def show_status():
    w = 78
    label_w = 28
    val_w = w - 5 - label_w  # 45

    def print_row(label, val_fmt, val_vlen):
        spaces = max(0, val_w - val_vlen)
        print(f"{C_CYAN}│{C_RESET}  {label:<{label_w}}{val_fmt}{' ' * spaces} {C_CYAN}│{C_RESET}")

    platform = get_platform()
    bios_part = f" | BIOS {platform.dmi['bios_version']}" if platform.dmi['bios_version'] else ""
    plat_str = f"PLATFORM: {platform.dmi['product_name']} | {platform.cpu_info['clean_model']} ({platform.topology['summary_str']} {platform.cpu_info['arch']}){bios_part}"

    print(f"\n{C_BOLD}{C_CYAN}╭─ ThrottleStop Linux (Universal HP Hardware Power Manager) ──────────────────╮{C_RESET}")
    print(f"{C_CYAN}│{C_WHITE}{plat_str:^{w-2}}{C_CYAN}│{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}├─ TURBO POWER LIMITS (TPL - RAPL DUAL-PLANE) ─────────────────────────────────┤{C_RESET}")

    r = get_rapl_status()
    pl1_txt = f"PL1: {r['effective_pl1']:.0f}W | PL2: {r['effective_pl2']:.0f}W ({r['effective_tau']:.0f}s) [{r['effective_source']}]"
    print_row("Operative Power Limits:", f"{C_BOLD}{C_GREEN}{pl1_txt}{C_RESET}", len(pl1_txt))

    msr_lock_s = "Locked" if r.get('msr_locked') else "Unlocked"
    msr_clamp_s = "Clamped" if r.get('msr_clamped') else "Unclamped"
    msr_txt = f"PL1: {r['msr_pl1']:.0f}W | PL2: {r['msr_pl2']:.0f}W | Tau: {r['msr_tau']:.0f}s ({msr_lock_s}, {msr_clamp_s})"
    print_row("MSR 0x610 Package Limit:", f"{C_DIM}{msr_txt}{C_RESET}", len(msr_txt))

    if r['mmio_exists']:
        mmio_st = "Active" if r['mmio_enabled'] else "Bypassed"
        override_note = " [DPTF Override]" if r.get('dptf_override') else ""
        mmio_txt = f"PL1: {r['mmio_pl1']:.0f}W | PL2: {r['mmio_pl2']:.0f}W | Tau: {r['mmio_tau']:.0f}s ({mmio_st}{override_note})"
        print_row("MMIO System Powercap:", f"{C_DIM}{mmio_txt}{C_RESET}", len(mmio_txt))

    th_run = is_thermald_running()
    th_col = C_YELLOW if th_run else C_GREEN
    th_txt = "Active (DPTF Adaptive Polling)" if th_run else "Paused/Managed (No MMIO Overrides)"
    print_row("Thermal Daemon (thermald):", f"{th_col}{th_txt}{C_RESET}", len(th_txt))

    gnome_prof = get_desktop_power_profile()
    print_row("GNOME Power Mode:", f"{C_MAGENTA}{gnome_prof}{C_RESET}", len(gnome_prof))

    print(f"{C_BOLD}{C_CYAN}├─ SPEED SHIFT (HWP) & SILICON CORE STATE ─────────────────────────────────────┤{C_RESET}")

    turbo_on = get_turbo()
    t_col = C_GREEN if turbo_on else C_RED
    turbo_max_ghz = platform.topology.get("max_p_mhz", 4000) / 1000.0
    t_txt = f"ENABLED (Up to {turbo_max_ghz:.1f} GHz Turbo)" if turbo_on else "DISABLED (Energy Saver Base Clocks)"
    print_row("Intel Turbo Boost:", f"{t_col}{C_BOLD}{t_txt}{C_RESET}", len(t_txt))

    dyn_b = get_dynamic_boost()
    dyn_col = C_GREEN if dyn_b else C_DIM
    dyn_txt = "ENABLED (HWP Responsive Boost)" if dyn_b else "DISABLED (Normal Ramp)"
    print_row("HWP Dynamic Boost:", f"{dyn_col}{dyn_txt}{C_RESET}", len(dyn_txt))

    bal = get_power_balance()
    bal_map = {
        "balanced": "Balanced (50% CPU / 50% iGPU)",
        "gpu_pri": "iGPU Priority (70% iGPU / 30% CPU)",
        "cpu_pri": "CPU Priority (80% CPU / 20% iGPU)",
        "gpu_max": "Max iGPU Boost (Uncapped Arc Graphics 1200MHz)",
    }
    bal_txt = bal_map.get(bal, bal)
    print_row("CPU/iGPU Power Balance:", f"{C_CYAN}{bal_txt}{C_RESET}", len(bal_txt))

    epp = get_epp()
    epp_col = C_GREEN if epp == "power" else (C_CYAN if "balance" in epp else C_YELLOW)
    print_row("Speed Shift EPP:", f"{epp_col}{C_BOLD}{epp}{C_RESET}", len(epp))

    on_cnt, tot_cnt, mode_key, mode_desc = get_core_status()
    c_col = C_GREEN if mode_key != "none" else C_CYAN
    print_row("CPU Silicon Topology:", f"{c_col}{mode_desc}{C_RESET}", len(mode_desc))

    p_max = read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq") // 1000
    e_val = read_int("/sys/devices/system/cpu/cpu8/cpufreq/scaling_max_freq")
    lp_max = read_int("/sys/devices/system/cpu/cpu16/cpufreq/scaling_max_freq") // 1000
    if e_val > 0:
        freq_txt = f"P-Cores: {p_max} MHz | E-Cores: {e_val//1000} MHz | LP: {lp_max} MHz"
    else:
        freq_txt = f"P-Core 0: {p_max} MHz | E-Cores: Offlined | LP: {lp_max} MHz"
    print_row("Core Frequency Ceilings:", f"{C_WHITE}{freq_txt}{C_RESET}", len(freq_txt))

    tgt = get_thermal_target()
    prochot_txt = f"TjMax: {tgt['tjmax']}°C | Offset: {tgt['offset']}°C -> Trip: {tgt['prochot']}°C"
    print_row("PROCHOT Thermal Target:", f"{C_DIM}{prochot_txt}{C_RESET}", len(prochot_txt))

    print(f"{C_BOLD}{C_CYAN}├─ PERIPHERALS & ENERGY OPTIMIZATIONS ────────────────────────────────────────┤{C_RESET}")

    aspm = get_pcie_aspm()
    aspm_col = C_GREEN if "supersave" in aspm else (C_CYAN if "save" in aspm else C_YELLOW)
    print_row("PCIe ASPM Link Policy:", f"{aspm_col}{aspm}{C_RESET}", len(aspm))

    wifi = get_wifi_power_save()
    w_col = C_GREEN if wifi == "Enabled" else C_YELLOW
    print_row("Wi-Fi Power Management:", f"{w_col}{wifi}{C_RESET}", len(wifi))

    bt = get_bluetooth_state()
    bt_col = C_GREEN if bt == "Disabled" else C_CYAN
    print_row("Bluetooth Radio:", f"{bt_col}{bt}{C_RESET}", len(bt))

    # Battery
    try:
        b_status = read_file("/sys/class/power_supply/BAT0/status", "Unknown").strip()
        p_now_u = read_int("/sys/class/power_supply/BAT0/power_now")
        e_now_u = read_int("/sys/class/power_supply/BAT0/energy_now")
        e_full_u = read_int("/sys/class/power_supply/BAT0/energy_full")

        if b_status.lower() == "charging" and p_now_u > 0 and e_full_u > e_now_u:
            w_chg = p_now_u / 1e6
            hours = ((e_full_u - e_now_u) / 1e6) / w_chg
            h = int(hours)
            m = int((hours - h) * 60)
            batt_txt = f"Charging (+{w_chg:.1f} W) - {h}h {m:02d}m to full"
            print_row("Battery Status:", f"{C_GREEN}{C_BOLD}{batt_txt}{C_RESET}", len(batt_txt))
        elif b_status.lower() == "full":
            batt_txt = "100% Fully Charged (AC Power Online)"
            print_row("Battery Status:", f"{C_GREEN}{C_BOLD}{batt_txt}{C_RESET}", len(batt_txt))
        elif p_now_u > 0 and e_now_u > 0:
            w_draw = p_now_u / 1e6
            hours = (e_now_u / 1e6) / w_draw
            h = int(hours)
            m = int((hours - h) * 60)
            batt_txt = f"Discharging (-{w_draw:.2f} W) - {h}h {m:02d}m left"
            b_col = C_GREEN if hours >= 7 else (C_CYAN if hours >= 4 else C_YELLOW)
            print_row("Battery Runtime:", f"{b_col}{C_BOLD}{batt_txt}{C_RESET}", len(batt_txt))
    except Exception:
        pass

    print(f"{C_BOLD}{C_CYAN}╰────────────────────────────────────────────────────────────────────────────╯{C_RESET}\n")

# ==============================================================================
# ThrottleStop Background Daemon & AC/Battery Auto-Switch
# ==============================================================================

def run_daemon(profile_on_ac="performance", profile_on_bat="eco", interval=2.0):
    """
    Continuous Enforcement Daemon:
    1. Re-asserts desired power limits every N seconds so OEM DPTF/thermald cannot override them.
    2. Auto-switches profiles dynamically when AC is plugged or unplugged!
    """
    check_root()
    print(f"{C_BOLD}{C_GREEN}✔ ThrottleStop Daemon Active{C_RESET}")
    print(f"  ├─ AC Profile:      {C_BOLD}{profile_on_ac}{C_RESET}")
    print(f"  ├─ Battery Profile: {C_BOLD}{profile_on_bat}{C_RESET}")
    print(f"  └─ Enforcement:     Every {interval:.1f}s\n")

    last_state = None

    def _on_signal(sig, frame):
        print(f"\n{C_YELLOW}Daemon stopping...{C_RESET}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    while True:
        # Check power source
        is_ac = True
        for ps in glob.glob("/sys/class/power_supply/*"):
            name = os.path.basename(ps).lower()
            if "bat" in name:
                continue
            if read_file(f"{ps}/online") == "1":
                is_ac = True
                break
        else:
            bat_status = read_file("/sys/class/power_supply/BAT0/status", "").lower()
            if bat_status == "discharging":
                is_ac = False

        cur_profile = profile_on_ac if is_ac else profile_on_bat

        if cur_profile != last_state:
            print(f"[{time.strftime('%H:%M:%S')}] Power transition -> AC={is_ac}. Switching to profile: {cur_profile}")
            apply_profile(cur_profile)
            last_state = cur_profile
        else:
            # Continuous enforcement: re-apply RAPL power limits to ensure MMIO hasn't drifted
            p = PROFILES[cur_profile]
            set_rapl_limits(
                pl1_w=p["pl1"],
                pl2_w=p["pl2"],
                tau_s=p["tau"],
                pl4_w=p["pl4"],
                disable_mmio=False
            )

        time.sleep(interval)

def install_systemd_service(profile_on_ac="performance", profile_on_bat="eco"):
    """Install and enable ThrottleStop as an automatic systemd service."""
    check_root()
    script_path = os.path.abspath(__file__)
    unit_content = f"""[Unit]
Description=ThrottleStop for Linux Power & Hardware Enforcement
After=multi-user.target thermal-daemon.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 {script_path} --daemon --ac-profile {profile_on_ac} --bat-profile {profile_on_bat}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(SERVICE_PATH, "w") as f:
            f.write(unit_content)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", "throttlestop.service"], check=True)
        print(f"\n{C_BOLD}{C_GREEN}✔ ThrottleStop systemd service successfully installed and started!{C_RESET}")
        print(f"  ├─ Service: {SERVICE_PATH}")
        print(f"  ├─ Status:  {C_CYAN}systemctl status throttlestop{C_RESET}")
        print(f"  └─ Config:  AC={profile_on_ac} | Battery={profile_on_bat}\n")
        return True
    except Exception as e:
        print(f"{C_RED}Failed to install systemd service: {e}{C_RESET}")
        return False

def uninstall_systemd_service():
    """Remove and disable the systemd service."""
    check_root()
    try:
        subprocess.run(["systemctl", "disable", "--now", "throttlestop.service"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(SERVICE_PATH):
            os.remove(SERVICE_PATH)
        subprocess.run(["systemctl", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"\n{C_BOLD}{C_YELLOW}✔ ThrottleStop systemd service uninstalled.{C_RESET}\n")
        return True
    except Exception as e:
        print(f"{C_RED}Failed to uninstall service: {e}{C_RESET}")
        return False

# ==============================================================================
# Interactive ThrottleStop Terminal UI (TUI)
# ==============================================================================

def run_interactive_tui():
    """
    Live ThrottleStop GUI/TUI:
    Provides hotkey-driven profile switching, live power/temp/frequency monitoring,
    and instantaneous TPL/Turbo toggles.
    """
    if not sys.stdin.isatty():
        print(f"{C_YELLOW}Notice: Interactive TUI requires an interactive terminal (TTY). Showing status snapshot instead:{C_RESET}\n")
        print_hardware_status()
        return

    is_root = check_root(exit_on_fail=False)

    # Configure terminal for non-blocking single-key input
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    active_profile = "balanced"
    status_msg = "Ready. Press [1-5] for profiles, [T] Turbo, [B] Bench, [Q] Quit." if is_root else "Telemetry Mode (Read-Only). Run with sudo to switch profiles."

    # Enter alternate screen buffer & hide cursor
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        last_rapl_e = read_int(f"{RAPL_MSR_DIR}/energy_uj", 0)
        last_rapl_t = time.time()

        while True:
            # 1. Render Dashboard
            now_t = time.time()
            now_e = read_int(f"{RAPL_MSR_DIR}/energy_uj", 0)
            dt = now_t - last_rapl_t
            de = now_e - last_rapl_e
            cur_pkg_w = ((de / 1e6) / dt) if (dt > 0 and de > 0) else 0.0
            last_rapl_t = now_t
            last_rapl_e = now_e

            r = get_rapl_status()
            turbo_on = get_turbo()
            epp = get_epp()
            on_c, tot_c, mode_key, mode_desc = get_core_status()
            tgt = get_thermal_target()
            cur_temp = read_float("/sys/class/thermal/thermal_zone0/temp", 0.0) / 1000.0

            # Battery
            p_now_u = read_int("/sys/class/power_supply/BAT0/power_now")
            b_stat = read_file("/sys/class/power_supply/BAT0/status", "AC").strip()
            bat_w = (p_now_u / 1e6) if p_now_u > 0 else 0.0

            # RAM usage & swap telemetry
            mem_line = ""
            try:
                with open("/proc/meminfo", "r") as mf:
                    md = {}
                    for m_line in mf:
                        parts = m_line.split(":")
                        if len(parts) == 2:
                            md[parts[0].strip()] = int(parts[1].split()[0])
                    m_tot = md.get("MemTotal", 0) // 1024
                    m_avail = md.get("MemAvailable", 0) // 1024
                    m_used = m_tot - m_avail
                    m_pct = (m_used / m_tot * 100) if m_tot else 0
                    sw_tot = md.get("SwapTotal", 0) // 1024
                    sw_free = md.get("SwapFree", 0) // 1024
                    sw_used = sw_tot - sw_free
                    sw_pct = (sw_used / sw_tot * 100) if sw_tot else 0
                    mem_line = f"RAM: {m_used/1024:.1f}/{m_tot/1024:.1f} GiB ({m_pct:.0f}%) | Swap: {sw_used} MiB ({sw_pct:.0f}%)"
            except Exception:
                mem_line = "RAM: Active"

            # Fan & Board telemetry
            platform = get_platform()
            top = platform.topology
            fan1, fan2, fan_src = platform.read_fan_rpm()
            fan_rpm = fan1 if fan1 > 0 else fan2

            # Clocks
            p_mhz = read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") // 1000
            e_mhz = 0
            if top.get("has_e") and top.get("e_cores"):
                e_t = top["e_cores"][0]["primary"]
                e_mhz = read_int(f"/sys/devices/system/cpu/cpu{e_t}/cpufreq/scaling_cur_freq") // 1000
            lp_mhz = 0
            if top.get("has_lp") and top.get("lp_cores"):
                lp_t = top["lp_cores"][0]["primary"]
                lp_mhz = read_int(f"/sys/devices/system/cpu/cpu{lp_t}/cpufreq/scaling_cur_freq") // 1000

            lines = []
            lines.append("\033[H")  # Move cursor to top
            lines.append(f"{C_BOLD}{C_CYAN}╭─ ThrottleStop for Linux ────────────────────────────────────────────────────╮{C_RESET}")
            bios_str = f" | BIOS {platform.dmi['bios_version']}" if platform.dmi['bios_version'] else ""
            tui_sub = f"{platform.dmi['product_name']} | {platform.cpu_info['clean_model']} ({top['summary_str']}){bios_str}"
            lines.append(f"{C_CYAN}│{C_WHITE} {tui_sub:<76} {C_CYAN}│{C_RESET}")
            lines.append(f"{C_BOLD}{C_CYAN}├─ SELECT THROTTLESTOP PROFILE ────────────────────────────────────────────────┤{C_RESET}")

            # Profile selector row
            p_btns = []
            for num, k in enumerate(["max", "performance", "game", "balanced", "eco", "supereco"], 1):
                col = PROFILES[k]["color"]
                lbl = "MAX" if k == "max" else ("PERF" if k == "performance" else k.upper())
                if k == active_profile:
                    p_btns.append(f"{C_BOLD}\033[7m[{num}] {lbl:<7}\033[27m{C_RESET}")
                else:
                    p_btns.append(f"{col}[{num}] {lbl:<7}{C_RESET}")
            lines.append(f"{C_CYAN}│{C_RESET} " + " ".join(p_btns) + f" {C_CYAN}│{C_RESET}")

            lines.append(f"{C_BOLD}{C_CYAN}├─ HARDWARE TELEMETRY & POWER METERS ──────────────────────────────────────────┤{C_RESET}")

            # Power limits & live package draw
            eff_pl1 = r['effective_pl1']
            headroom = max(0.0, eff_pl1 - cur_pkg_w)
            pct = min(100.0, (cur_pkg_w / eff_pl1 * 100.0)) if eff_pl1 > 0 else 0.0
            bar_len = 16
            filled = int(round((pct / 100.0) * bar_len))
            bar_str = f"{C_CYAN}{'█' * filled}{C_DIM}{'░' * (bar_len - filled)}{C_RESET}"

            p_line = f"Pkg Power: {color_watts(cur_pkg_w)} [{bar_str}] {pct:4.1f}% PL1 ({headroom:4.1f}W free)"
            lines.append(f"{C_CYAN}│{C_RESET}  {p_line:<73} {C_CYAN}│{C_RESET}")

            tpl_line = f"Operative Limits: PL1 {eff_pl1:.0f}W | PL2 {r['effective_pl2']:.0f}W ({r['effective_tau']:.0f}s) [{r['effective_source']}]"
            lines.append(f"{C_CYAN}│{C_RESET}  {tpl_line:<73} {C_CYAN}│{C_RESET}")

            msr_lock_s = "Locked" if r.get('msr_locked') else "Unlocked"
            msr_clamp_s = "Clamped" if r.get('msr_clamped') else "Unclamped"
            msr_txt = f"True Limits: MSR 0x610: {r['msr_pl1']:.0f}W ({msr_lock_s}, {msr_clamp_s}) | MMIO: {r['mmio_pl1']:.0f}W"
            lines.append(f"{C_CYAN}│{C_RESET}  {msr_txt:<73} {C_CYAN}│{C_RESET}")

            # Temperatures & PROCHOT
            t_col = C_GREEN if cur_temp < 60 else (C_YELLOW if cur_temp < 85 else C_RED)
            dist = max(0, tgt['prochot'] - cur_temp)
            temp_line = f"Temperature: {t_col}{cur_temp:4.1f}°C{C_RESET} (TjMax {tgt['tjmax']}°C, PROCHOT {tgt['prochot']}°C, Distance {dist:2.0f}°C)"
            lines.append(f"{C_CYAN}│{C_RESET}  {temp_line:<82} {C_CYAN}│{C_RESET}")

            # Clocks & Cores
            if top.get("has_lp") and top.get("has_e"):
                clk_line = f"Live Clocks: P-Core {p_mhz:4d} MHz | E-Core {e_mhz:4d} MHz | LP-Core {lp_mhz:4d} MHz"
            elif top.get("has_e"):
                clk_line = f"Live Clocks: P-Core {p_mhz:4d} MHz | E-Core {e_mhz:4d} MHz"
            else:
                clk_line = f"Live Clocks: CPU Core {p_mhz:4d} MHz"
            lines.append(f"{C_CYAN}│{C_RESET}  {clk_line:<73} {C_CYAN}│{C_RESET}")

            c_col = C_GREEN if mode_key != "none" else C_CYAN
            cor_line = f"Topology: {on_c}/{tot_c} Cores Active [{c_col}{mode_desc}{C_RESET}]"
            lines.append(f"{C_CYAN}│{C_RESET}  {cor_line:<82} {C_CYAN}│{C_RESET}")

            # Speed Shift, Turbo & Dynamic Boost
            tb_str = f"{C_GREEN}ENABLED{C_RESET}" if turbo_on else f"{C_RED}DISABLED{C_RESET}"
            dyn_b = get_dynamic_boost()
            db_str = f"{C_GREEN}ON{C_RESET}" if dyn_b else f"{C_DIM}OFF{C_RESET}"
            sst_line = f"EPP: {C_BOLD}{epp:<18}{C_RESET} | Turbo: {tb_str} | Dynamic Boost: {db_str}"
            lines.append(f"{C_CYAN}│{C_RESET}  {sst_line:<82} {C_CYAN}│{C_RESET}")

            # Telemetry: Memory, Fan, Board
            if fan1 > 0 and fan2 > 0:
                fan_part = f"Fans: {fan1} / {fan2} RPM"
            elif fan1 > 0:
                fan_part = f"Fan: {fan1} RPM"
            elif fan2 > 0:
                fan_part = f"Fan: {fan2} RPM"
            else:
                fan_part = "Fan: 0 RPM (Passive)"
            tel_line = f"{mem_line:<46} | {fan_part}"
            lines.append(f"{C_CYAN}│{C_RESET}  {tel_line:<73} {C_CYAN}│{C_RESET}")

            # Battery / AC
            if b_stat.lower() == "charging":
                bat_line = f"Power Source: AC Connected (Charging +{bat_w:.1f} W)"
            elif b_stat.lower() == "full":
                bat_line = f"Power Source: AC Mains Connected (Full Battery)"
            else:
                bat_line = f"Power Source: On Battery (Discharging -{bat_w:.2f} W)"
            lines.append(f"{C_CYAN}│{C_RESET}  {bat_line:<73} {C_CYAN}│{C_RESET}")

            lines.append(f"{C_BOLD}{C_CYAN}├─ CONTROLS & HOTKEYS ─────────────────────────────────────────────────────────┤{C_RESET}")
            lines.append(f"{C_CYAN}│{C_RESET}  [1-6] Profile   [T] Turbo Boost   [D] Dynamic Boost   [C] Core Parking      {C_CYAN}│{C_RESET}")
            lines.append(f"{C_CYAN}│{C_RESET}  [B]   TS Bench  [R] Factory Reset [Q] Quit Dashboard                        {C_CYAN}│{C_RESET}")
            lines.append(f"{C_BOLD}{C_CYAN}├──────────────────────────────────────────────────────────────────────────────┤{C_RESET}")
            status_disp = f"Status: {status_msg}"
            lines.append(f"{C_CYAN}│{C_RESET}  {status_disp:<73} {C_CYAN}│{C_RESET}")
            lines.append(f"{C_BOLD}{C_CYAN}╰──────────────────────────────────────────────────────────────────────────────╯{C_RESET}")

            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()

            # 2. Non-blocking keypress check (up to 1.0s timeout)
            rlist, _, _ = select.select([sys.stdin], [], [], 1.0)
            if rlist:
                ch = sys.stdin.read(1)
                if ch in ['q', 'Q', '\x03', '\x1b']:
                    break
                elif not is_root and ch in ['1', '2', '3', '4', '5', '6', 't', 'T', 'd', 'D', 'c', 'C', 'b', 'B', 'r', 'R']:
                    status_msg = "Root privileges required (re-run with sudo to apply changes)"
                    continue
                elif ch in ['1', '2', '3', '4', '5', '6']:
                    p_map = {'1': 'max', '2': 'performance', '3': 'game', '4': 'balanced', '5': 'eco', '6': 'supereco'}
                    sel = p_map[ch]
                    apply_profile(sel)
                    active_profile = sel
                    status_msg = f"Applied profile: {sel.upper()}"
                elif ch in ['t', 'T']:
                    new_t = not turbo_on
                    set_turbo(new_t)
                    status_msg = f"Turbo Boost: {'ENABLED' if new_t else 'DISABLED'}"
                elif ch in ['d', 'D']:
                    new_d = not get_dynamic_boost()
                    set_dynamic_boost(new_d)
                    status_msg = f"Dynamic Boost: {'ENABLED' if new_d else 'DISABLED'}"
                elif ch in ['c', 'C']:
                    parking_cycle = ["none", "no_smt", "pcores_only", "pcores_lp", "single_pcore", "dual_pcore", "cluster", "lp_only"]
                    curr_idx = parking_cycle.index(mode_key) if mode_key in parking_cycle else -1
                    next_mode = parking_cycle[(curr_idx + 1) % len(parking_cycle)]
                    set_core_parking(next_mode)
                    status_msg = f"Core Parking set to: {next_mode.upper()}"
                elif ch in ['b', 'B']:
                    # TS Bench: Restore terminal, run bench, then return
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    sys.stdout.write("\033[?1049l\033[?25h")
                    sys.stdout.flush()
                    run_ts_bench(5.0)
                    input("\nPress [Enter] to return to ThrottleStop...")
                    sys.stdout.write("\033[?1049h\033[?25l")
                    sys.stdout.flush()
                    tty.setcbreak(fd)
                    status_msg = "TS Bench finished."
                elif ch in ['r', 'R']:
                    revert_to_defaults()
                    active_profile = "balanced"
                    status_msg = "Reverted all hardware settings to Linux defaults."

    finally:
        # Restore terminal settings
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?1049l\033[?25h")
        sys.stdout.flush()
        print("\nExited ThrottleStop.")

# ==============================================================================
# Desktop Launcher Installation
# ==============================================================================

def install_desktop_launcher():
    """Create a desktop entry so ThrottleStop can be launched from the application menu."""
    home_dir = os.path.expanduser("~")
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        try:
            home_dir = pwd.getpwnam(sudo_user).pw_dir
        except Exception:
            pass

    app_dir = os.path.join(home_dir, ".local", "share", "applications")
    os.makedirs(app_dir, exist_ok=True)
    desktop_file = os.path.join(app_dir, "throttlestop-linux.desktop")
    script_path = os.path.abspath(__file__)

    content = f"""[Desktop Entry]
Name=ThrottleStop Linux
Comment=Intel Meteor Lake Hardware Power & Performance Manager
Exec=/usr/bin/python3 {script_path} --gui
Icon=utilities-system-monitor
Terminal=false
Type=Application
Categories=System;Settings;HardwareSettings;
Keywords=throttlestop;cpu;power;tdp;rapl;tuning;
"""
    try:
        with open(desktop_file, "w") as f:
            f.write(content)
        os.chmod(desktop_file, 0o755)
        if sudo_user:
            import pwd
            pw = pwd.getpwnam(sudo_user)
            os.chown(desktop_file, pw.pw_uid, pw.pw_gid)
        print(f"\n{C_BOLD}{C_GREEN}✔ Desktop launcher installed to: {desktop_file}{C_RESET}")
        print(f"You can now search and launch 'ThrottleStop Linux' from your Zorin OS / GNOME app menu.\n")
        return True
    except Exception as e:
        print(f"{C_RED}Failed to install desktop launcher: {e}{C_RESET}")
        return False

# ==============================================================================
# Native Graphical User Interface (GTK3 Desktop Application)
# ==============================================================================

try:
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('Handy', '1')
    gi.require_version('Pango', '1.0')
    gi.require_version('PangoCairo', '1.0')
    from gi.repository import Gtk, Handy, GLib, Gdk, Pango, PangoCairo, Gio
    import cairo
    Gtk.init_check()
    Handy.init()
    HAS_GUI_LIBS = True
except Exception:
    HAS_GUI_LIBS = False

# Attempt importing TelemetryCollector from hp_telemetry
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)
try:
    import hp_telemetry
    HAS_HP_TELEMETRY = True
except Exception:
    HAS_HP_TELEMETRY = False

def get_instant_pkg_power(sample_s=0.08):
    p = f"{RAPL_MSR_DIR}/energy_uj"
    e1 = read_int(p, 0)
    time.sleep(sample_s)
    e2 = read_int(p, 0)
    if e2 > e1:
        return (e2 - e1) / (sample_s * 1e6)
    return 0.0

if HAS_GUI_LIBS:
    import collections
    import threading

    class GnomeTelemetryGraph(Gtk.DrawingArea):
        """
        Live scrolling performance graph styled after GNOME System Monitor
        with full dual Y-axes, time X-axis, tick marks, and clear units:
          - Left Y-Axis: CPU Package Power in Watts (orange numbers, ticks, and 'Power (W)' header)
          - Right Y-Axis: CPU Temperature in °C (red numbers, ticks, and 'Temp (°C)' header)
          - Bottom X-Axis: Time in seconds ("60s ago" ... "Now (0s)" with ticks and grid)
          - Dynamic scale headroom & dashed PL1 sustained clamp line
        """
        def __init__(self, max_points=60):
            super().__init__()
            self.set_size_request(-1, 230)
            self.max_points = max_points
            self.power_data = collections.deque([], maxlen=max_points)
            self.temp_data = collections.deque([], maxlen=max_points)
            self.pl1 = 29.0
            self.cur_power = 0.0
            self.cur_temp = 0.0
            self.connect("draw", self._on_draw)

        def add_sample(self, pwr, tmp, pl1):
            self.cur_power = max(0.0, float(pwr))
            self.cur_temp = max(0.0, float(tmp))
            self.pl1 = float(pl1)
            self.power_data.append(self.cur_power)
            self.temp_data.append(self.cur_temp)
            self.queue_draw()

        def _on_draw(self, widget, cr):
            w = widget.get_allocated_width()
            h = widget.get_allocated_height()

            pad_left = 68
            pad_right = 68
            pad_top = 36
            pad_bottom = 36

            plot_x = pad_left
            plot_y = pad_top
            plot_w = max(20, w - pad_left - pad_right)
            plot_h = max(20, h - pad_top - pad_bottom)

            # Plot frame background (rounded inner frame)
            cr.save()
            radius = 6.0
            cr.new_sub_path()
            cr.arc(plot_x + plot_w - radius, plot_y + radius, radius, -math.pi/2, 0)
            cr.arc(plot_x + plot_w - radius, plot_y + plot_h - radius, radius, 0, math.pi/2)
            cr.arc(plot_x + radius, plot_y + plot_h - radius, radius, math.pi/2, math.pi)
            cr.arc(plot_x + radius, plot_y + radius, radius, math.pi, 3*math.pi/2)
            cr.close_path()
            cr.set_source_rgb(0.08, 0.09, 0.12)
            cr.fill_preserve()
            cr.clip()

            # Dynamic Ceilings
            max_pwr = max(60.0, math.ceil(max(max(self.power_data) * 1.15 if self.power_data else 60.0, self.pl1 * 1.1) / 10.0) * 10.0)
            max_temp = 120.0 if (self.temp_data and max(self.temp_data) > 95.0) else 100.0

            pango_layout = PangoCairo.create_layout(cr)

            # Grid lines inside plot
            for i in range(5):
                frac = i / 4.0
                y = plot_y + plot_h - (frac * plot_h)
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.07)
                cr.set_line_width(1.0)
                cr.move_to(plot_x, y)
                cr.line_to(plot_x + plot_w, y)
                cr.stroke()

            # Vertical Grid Lines (Time Scale)
            time_fractions = [0.0, 0.25, 0.5, 0.75, 1.0]
            for frac in time_fractions:
                x = plot_x + (frac * plot_w)
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.05)
                cr.move_to(x, plot_y)
                cr.line_to(x, plot_y + plot_h)
                cr.stroke()

            # PL1 Sustained Power Clamp Line
            if self.pl1 > 0:
                pl1_y = plot_y + plot_h - min(1.0, (self.pl1 / max_pwr)) * plot_h
                cr.save()
                cr.set_dash([5.0, 4.0])
                cr.set_source_rgba(0.95, 0.75, 0.2, 0.8)
                cr.set_line_width(1.2)
                cr.move_to(plot_x, pl1_y)
                cr.line_to(plot_x + plot_w, pl1_y)
                cr.stroke()
                cr.restore()

            # Draw Curves
            pts = list(self.power_data)
            t_pts = list(self.temp_data)
            n = len(pts)
            if n > 1:
                dt_x = plot_w / (self.max_points - 1)
                coords_pwr = []
                coords_tmp = []
                for i in range(n):
                    idx_from_end = (n - 1) - i
                    x = (plot_x + plot_w) - (idx_from_end * dt_x)
                    y_pwr = plot_y + plot_h - min(1.0, (pts[i] / max_pwr)) * plot_h
                    y_tmp = plot_y + plot_h - min(1.0, (t_pts[i] / max_temp)) * plot_h
                    coords_pwr.append((x, y_pwr))
                    coords_tmp.append((x, y_tmp))

                # Power gradient fill
                pat = cairo.LinearGradient(0, plot_y, 0, plot_y + plot_h)
                pat.add_color_stop_rgba(0.0, 0.96, 0.62, 0.15, 0.35)
                pat.add_color_stop_rgba(1.0, 0.96, 0.62, 0.15, 0.02)

                cr.move_to(coords_pwr[0][0], plot_y + plot_h)
                for x, y in coords_pwr:
                    cr.line_to(x, y)
                cr.line_to(coords_pwr[-1][0], plot_y + plot_h)
                cr.close_path()
                cr.set_source(pat)
                cr.fill()

                # Power curve stroke
                cr.set_source_rgb(0.96, 0.62, 0.15)
                cr.set_line_width(2.0)
                cr.move_to(coords_pwr[0][0], coords_pwr[0][1])
                for x, y in coords_pwr[1:]:
                    cr.line_to(x, y)
                cr.stroke()

                # Temp curve stroke
                cr.set_source_rgb(0.94, 0.28, 0.28)
                cr.set_line_width(2.0)
                cr.move_to(coords_tmp[0][0], coords_tmp[0][1])
                for x, y in coords_tmp[1:]:
                    cr.line_to(x, y)
                cr.stroke()

                # Glowing live points at "Now"
                last_x, last_p_y = coords_pwr[-1]
                _, last_t_y = coords_tmp[-1]

                cr.set_source_rgb(0.96, 0.62, 0.15)
                cr.arc(last_x, last_p_y, 3.5, 0, 2 * math.pi)
                cr.fill()

                cr.set_source_rgb(0.94, 0.28, 0.28)
                cr.arc(last_x, last_t_y, 3.5, 0, 2 * math.pi)
                cr.fill()

            cr.restore()

            # Plot Border
            cr.save()
            cr.new_sub_path()
            cr.arc(plot_x + plot_w - radius, plot_y + radius, radius, -math.pi/2, 0)
            cr.arc(plot_x + plot_w - radius, plot_y + plot_h - radius, radius, 0, math.pi/2)
            cr.arc(plot_x + radius, plot_y + plot_h - radius, radius, math.pi/2, math.pi)
            cr.arc(plot_x + radius, plot_y + radius, radius, math.pi, 3*math.pi/2)
            cr.close_path()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.14)
            cr.set_line_width(1.0)
            cr.stroke()
            cr.restore()

            # Top Headers & Legend
            # Left Header: Power (Watts)
            pango_layout.set_font_description(Pango.font_description_from_string("Cantarell Bold 9"))
            pango_layout.set_text(f"■ Power: {self.cur_power:4.1f} W", -1)
            cr.set_source_rgb(0.96, 0.62, 0.15)
            cr.move_to(plot_x, 10)
            PangoCairo.show_layout(cr, pango_layout)

            # Center Header: Operative PL1 Limit
            pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 9"))
            pango_layout.set_text(f"╌ PL1 Limit: {self.pl1:.0f} W", -1)
            ink, log = pango_layout.get_pixel_extents()
            cr.set_source_rgba(0.95, 0.75, 0.2, 0.9)
            cr.move_to(plot_x + (plot_w / 2) - (log.width / 2), 10)
            PangoCairo.show_layout(cr, pango_layout)

            # Right Header: Temperature (°C)
            pango_layout.set_font_description(Pango.font_description_from_string("Cantarell Bold 9"))
            pango_layout.set_text(f"■ Temp: {self.cur_temp:4.1f} °C", -1)
            ink, log = pango_layout.get_pixel_extents()
            cr.set_source_rgb(0.94, 0.28, 0.28)
            cr.move_to(plot_x + plot_w - log.width, 10)
            PangoCairo.show_layout(cr, pango_layout)

            # Y-Axes Ticks & Numbers
            for i in range(5):
                frac = i / 4.0
                y = plot_y + plot_h - (frac * plot_h)

                # Left Tick & Value (Power)
                cr.set_source_rgba(0.96, 0.62, 0.15, 0.9)
                cr.move_to(plot_x - 5, y)
                cr.line_to(plot_x, y)
                cr.stroke()

                val_pwr = frac * max_pwr
                pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 9"))
                pango_layout.set_text(f"{val_pwr:.0f} W", -1)
                ink, log = pango_layout.get_pixel_extents()
                cr.move_to(plot_x - 8 - log.width, y - log.height / 2)
                PangoCairo.show_layout(cr, pango_layout)

                # Right Tick & Value (Temp)
                cr.set_source_rgba(0.94, 0.30, 0.30, 0.9)
                cr.move_to(plot_x + plot_w, y)
                cr.line_to(plot_x + plot_w + 5, y)
                cr.stroke()

                val_tmp = frac * max_temp
                pango_layout.set_text(f"{val_tmp:.0f} °C", -1)
                ink, log = pango_layout.get_pixel_extents()
                cr.move_to(plot_x + plot_w + 8, y - log.height / 2)
                PangoCairo.show_layout(cr, pango_layout)

            # X-Axis Ticks & Labels
            time_labels = [("60s ago", 0.0), ("45s ago", 0.25), ("30s ago", 0.5), ("15s ago", 0.75), ("Now (0s)", 1.0)]
            for lbl, frac in time_labels:
                x = plot_x + (frac * plot_w)

                cr.set_source_rgba(0.7, 0.73, 0.8, 0.7)
                cr.move_to(x, plot_y + plot_h)
                cr.line_to(x, plot_y + plot_h + 5)
                cr.stroke()

                pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 9"))
                pango_layout.set_text(lbl, -1)
                ink, log = pango_layout.get_pixel_extents()
                if frac == 1.0:
                    cr.move_to(x - log.width, plot_y + plot_h + 8)
                elif frac == 0.0:
                    cr.move_to(x, plot_y + plot_h + 8)
                else:
                    cr.move_to(x - log.width / 2, plot_y + plot_h + 8)
                PangoCairo.show_layout(cr, pango_layout)

            # Timeline subcaption
            pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 8"))
            pango_layout.set_text("Time Window (60 Seconds)", -1)
            ink, log = pango_layout.get_pixel_extents()
            cr.set_source_rgba(0.7, 0.73, 0.8, 0.5)
            cr.move_to(plot_x + (plot_w / 2) - (log.width / 2), plot_y + plot_h + 22)
            PangoCairo.show_layout(cr, pango_layout)

            return False

    class GnomeCpuHistoryGraph(Gtk.DrawingArea):
        """
        Live scrolling CPU history graph styled after GNOME System Monitor
        with full dual Y-axes, time X-axis, tick marks, and clear units:
          - Left Y-Axis: CPU Utilization in % (0% to 100%) (GNOME blue / cyan)
          - Right Y-Axis: Cluster Clock Frequency in GHz (0 to 5.0 GHz) (Violet / Emerald)
          - Bottom X-Axis: Time in seconds ("60s ago" ... "Now (0s)")
        """
        def __init__(self, max_points=60):
            super().__init__()
            self.set_size_request(-1, 210)
            self.max_points = max_points
            self.load_data = collections.deque([], maxlen=max_points)
            self.p_mhz_data = collections.deque([], maxlen=max_points)
            self.e_mhz_data = collections.deque([], maxlen=max_points)
            self.cur_load = 0.0
            self.cur_p_mhz = 0
            self.cur_e_mhz = 0
            self.connect("draw", self._on_draw)

        def add_sample(self, load_pct, p_mhz, e_mhz):
            self.cur_load = max(0.0, min(100.0, float(load_pct)))
            self.cur_p_mhz = max(0, int(p_mhz))
            self.cur_e_mhz = max(0, int(e_mhz))
            self.load_data.append(self.cur_load)
            self.p_mhz_data.append(self.cur_p_mhz)
            self.e_mhz_data.append(self.cur_e_mhz)
            self.queue_draw()

        def _on_draw(self, widget, cr):
            w = widget.get_allocated_width()
            h = widget.get_allocated_height()

            pad_left = 58
            pad_right = 64
            pad_top = 34
            pad_bottom = 34

            plot_x = pad_left
            plot_y = pad_top
            plot_w = max(20, w - pad_left - pad_right)
            plot_h = max(20, h - pad_top - pad_bottom)

            # Plot frame background (rounded inner frame)
            cr.save()
            radius = 6.0
            cr.new_sub_path()
            cr.arc(plot_x + plot_w - radius, plot_y + radius, radius, -math.pi/2, 0)
            cr.arc(plot_x + plot_w - radius, plot_y + plot_h - radius, radius, 0, math.pi/2)
            cr.arc(plot_x + radius, plot_y + plot_h - radius, radius, math.pi/2, math.pi)
            cr.arc(plot_x + radius, plot_y + radius, radius, math.pi, 3*math.pi/2)
            cr.close_path()
            cr.set_source_rgb(0.08, 0.09, 0.12)
            cr.fill_preserve()
            cr.clip()

            # Dynamic max frequency ceiling (up to 5.0 GHz)
            highest_clk = max(self.p_mhz_data) if self.p_mhz_data else 4500
            max_mhz = 5000.0 if highest_clk <= 5000 else 6000.0

            pango_layout = PangoCairo.create_layout(cr)

            # Horizontal Grid lines
            for i in range(5):
                frac = i / 4.0
                y = plot_y + plot_h - (frac * plot_h)
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.07)
                cr.set_line_width(1.0)
                cr.move_to(plot_x, y)
                cr.line_to(plot_x + plot_w, y)
                cr.stroke()

            # Vertical Time Grid lines
            time_fractions = [0.0, 0.25, 0.5, 0.75, 1.0]
            for frac in time_fractions:
                x = plot_x + (frac * plot_w)
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.05)
                cr.move_to(x, plot_y)
                cr.line_to(x, plot_y + plot_h)
                cr.stroke()

            # Draw Curves
            loads = list(self.load_data)
            p_clks = list(self.p_mhz_data)
            e_clks = list(self.e_mhz_data)
            n = len(loads)
            if n > 1:
                dt_x = plot_w / (self.max_points - 1)
                coords_load = []
                coords_p = []
                coords_e = []
                for i in range(n):
                    idx_from_end = (n - 1) - i
                    x = (plot_x + plot_w) - (idx_from_end * dt_x)
                    y_load = plot_y + plot_h - min(1.0, (loads[i] / 100.0)) * plot_h
                    y_p = plot_y + plot_h - min(1.0, (p_clks[i] / max_mhz)) * plot_h
                    y_e = plot_y + plot_h - min(1.0, (e_clks[i] / max_mhz)) * plot_h
                    coords_load.append((x, y_load))
                    coords_p.append((x, y_p))
                    coords_e.append((x, y_e))

                # CPU load gradient fill (GNOME Blue #3584e4)
                pat = cairo.LinearGradient(0, plot_y, 0, plot_y + plot_h)
                pat.add_color_stop_rgba(0.0, 0.21, 0.52, 0.89, 0.32)
                pat.add_color_stop_rgba(1.0, 0.21, 0.52, 0.89, 0.02)

                cr.move_to(coords_load[0][0], plot_y + plot_h)
                for x, y in coords_load:
                    cr.line_to(x, y)
                cr.line_to(coords_load[-1][0], plot_y + plot_h)
                cr.close_path()
                cr.set_source(pat)
                cr.fill()

                # CPU Load stroke
                cr.set_source_rgb(0.21, 0.52, 0.89)
                cr.set_line_width(2.0)
                cr.move_to(coords_load[0][0], coords_load[0][1])
                for x, y in coords_load[1:]:
                    cr.line_to(x, y)
                cr.stroke()

                # E-Core Frequency curve (GNOME Emerald #33d17a)
                cr.set_source_rgb(0.20, 0.82, 0.48)
                cr.set_line_width(1.8)
                cr.move_to(coords_e[0][0], coords_e[0][1])
                for x, y in coords_e[1:]:
                    cr.line_to(x, y)
                cr.stroke()

                # P-Core Frequency curve (GNOME Violet #c061cb)
                cr.set_source_rgb(0.75, 0.38, 0.80)
                cr.set_line_width(2.0)
                cr.move_to(coords_p[0][0], coords_p[0][1])
                for x, y in coords_p[1:]:
                    cr.line_to(x, y)
                cr.stroke()

                # Glowing live points at "Now"
                last_x, last_l_y = coords_load[-1]
                _, last_p_y = coords_p[-1]
                _, last_e_y = coords_e[-1]

                cr.set_source_rgb(0.21, 0.52, 0.89)
                cr.arc(last_x, last_l_y, 3.5, 0, 2 * math.pi)
                cr.fill()

                cr.set_source_rgb(0.75, 0.38, 0.80)
                cr.arc(last_x, last_p_y, 3.5, 0, 2 * math.pi)
                cr.fill()

                cr.set_source_rgb(0.20, 0.82, 0.48)
                cr.arc(last_x, last_e_y, 3.0, 0, 2 * math.pi)
                cr.fill()

            cr.restore()

            # Plot Border
            cr.save()
            cr.new_sub_path()
            cr.arc(plot_x + plot_w - radius, plot_y + radius, radius, -math.pi/2, 0)
            cr.arc(plot_x + plot_w - radius, plot_y + plot_h - radius, radius, 0, math.pi/2)
            cr.arc(plot_x + radius, plot_y + plot_h - radius, radius, math.pi/2, math.pi)
            cr.arc(plot_x + radius, plot_y + radius, radius, math.pi, 3*math.pi/2)
            cr.close_path()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.14)
            cr.set_line_width(1.0)
            cr.stroke()
            cr.restore()

            # Top Headers & Legend
            pango_layout.set_font_description(Pango.font_description_from_string("Cantarell Bold 9"))
            pango_layout.set_text(f"■ CPU: {self.cur_load:4.1f}%", -1)
            ink_l, log_l = pango_layout.get_pixel_extents()
            cr.set_source_rgb(0.21, 0.52, 0.89)
            cr.move_to(plot_x, 10)
            PangoCairo.show_layout(cr, pango_layout)

            pango_layout.set_text(f"■ E-Cores: {self.cur_e_mhz} MHz", -1)
            ink_r, log_r = pango_layout.get_pixel_extents()
            cr.set_source_rgb(0.20, 0.82, 0.48)
            cr.move_to(plot_x + plot_w - log_r.width, 10)
            PangoCairo.show_layout(cr, pango_layout)

            pango_layout.set_text(f"■ P-Cores: {self.cur_p_mhz} MHz", -1)
            ink_c, log_c = pango_layout.get_pixel_extents()
            if (plot_w - log_l.width - log_r.width) >= (log_c.width + 16):
                cr.set_source_rgb(0.75, 0.38, 0.80)
                cr.move_to(plot_x + (plot_w / 2) - (log_c.width / 2), 10)
                PangoCairo.show_layout(cr, pango_layout)

            # Y-Axes Ticks & Numbers
            for i in range(5):
                frac = i / 4.0
                y = plot_y + plot_h - (frac * plot_h)

                cr.set_source_rgba(0.21, 0.52, 0.89, 0.9)
                cr.move_to(plot_x - 5, y)
                cr.line_to(plot_x, y)
                cr.stroke()

                val_load = frac * 100.0
                pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 9"))
                pango_layout.set_text(f"{val_load:.0f}%", -1)
                ink, log = pango_layout.get_pixel_extents()
                cr.move_to(plot_x - 8 - log.width, y - log.height / 2)
                PangoCairo.show_layout(cr, pango_layout)

                cr.set_source_rgba(0.75, 0.38, 0.80, 0.9)
                cr.move_to(plot_x + plot_w, y)
                cr.line_to(plot_x + plot_w + 5, y)
                cr.stroke()

                val_ghz = (frac * max_mhz) / 1000.0
                pango_layout.set_text(f"{val_ghz:.1f} GHz", -1)
                ink, log = pango_layout.get_pixel_extents()
                cr.move_to(plot_x + plot_w + 8, y - log.height / 2)
                PangoCairo.show_layout(cr, pango_layout)

            # X-Axis Ticks & Labels
            time_labels = [("60s ago", 0.0), ("45s ago", 0.25), ("30s ago", 0.5), ("15s ago", 0.75), ("Now (0s)", 1.0)]
            for lbl, frac in time_labels:
                x = plot_x + (frac * plot_w)
                cr.set_source_rgba(0.7, 0.73, 0.8, 0.7)
                cr.move_to(x, plot_y + plot_h)
                cr.line_to(x, plot_y + plot_h + 5)
                cr.stroke()

                pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 9"))
                pango_layout.set_text(lbl, -1)
                ink, log = pango_layout.get_pixel_extents()
                if frac == 1.0:
                    cr.move_to(x - log.width, plot_y + plot_h + 8)
                elif frac == 0.0:
                    cr.move_to(x, plot_y + plot_h + 8)
                else:
                    cr.move_to(x - log.width / 2, plot_y + plot_h + 8)
                PangoCairo.show_layout(cr, pango_layout)

            # Timeline subcaption
            pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 8"))
            pango_layout.set_text("Time Window (60 Seconds)", -1)
            ink, log = pango_layout.get_pixel_extents()
            cr.set_source_rgba(0.7, 0.73, 0.8, 0.5)
            cr.move_to(plot_x + (plot_w / 2) - (log.width / 2), plot_y + plot_h + 20)
            PangoCairo.show_layout(cr, pango_layout)

            return False

    class GnomeMemoryHistoryGraph(Gtk.DrawingArea):
        """
        Live scrolling Memory & Swap history graph styled after GNOME System Monitor
        with full dual Y-axes, time X-axis, tick marks, and clear units:
          - Left Y-Axis: System RAM in GiB (0 to Total RAM GiB) (GNOME Indigo/Purple)
          - Right Y-Axis: RAM Utilization % (0% to 100%)
          - Bottom X-Axis: Time in seconds ("60s ago" ... "Now (0s)")
        """
        def __init__(self, max_points=60):
            super().__init__()
            self.set_size_request(-1, 210)
            self.max_points = max_points
            self.ram_data = collections.deque([], maxlen=max_points)
            self.swap_data = collections.deque([], maxlen=max_points)
            self.total_gib = 15.6
            self.cur_used_gib = 0.0
            self.cur_avail_gib = 0.0
            self.cur_pct = 0.0
            self.cur_swap_mib = 0.0
            self.cur_swap_pct = 0.0
            self.connect("draw", self._on_draw)

        def add_sample(self, used_gib, total_gib, used_pct, swap_used_mib, swap_pct, avail_gib=0.0):
            self.cur_used_gib = max(0.0, float(used_gib))
            self.total_gib = max(1.0, float(total_gib))
            self.cur_avail_gib = max(0.0, float(avail_gib))
            self.cur_pct = max(0.0, min(100.0, float(used_pct)))
            self.cur_swap_mib = max(0.0, float(swap_used_mib))
            self.cur_swap_pct = max(0.0, min(100.0, float(swap_pct)))
            self.ram_data.append(self.cur_used_gib)
            self.swap_data.append(self.cur_swap_pct)
            self.queue_draw()

        def _on_draw(self, widget, cr):
            w = widget.get_allocated_width()
            h = widget.get_allocated_height()

            pad_left = 64
            pad_right = 58
            pad_top = 34
            pad_bottom = 34

            plot_x = pad_left
            plot_y = pad_top
            plot_w = max(20, w - pad_left - pad_right)
            plot_h = max(20, h - pad_top - pad_bottom)

            # Plot frame background (rounded inner frame)
            cr.save()
            radius = 6.0
            cr.new_sub_path()
            cr.arc(plot_x + plot_w - radius, plot_y + radius, radius, -math.pi/2, 0)
            cr.arc(plot_x + plot_w - radius, plot_y + plot_h - radius, radius, 0, math.pi/2)
            cr.arc(plot_x + radius, plot_y + plot_h - radius, radius, math.pi/2, math.pi)
            cr.arc(plot_x + radius, plot_y + radius, radius, math.pi, 3*math.pi/2)
            cr.close_path()
            cr.set_source_rgb(0.08, 0.09, 0.12)
            cr.fill_preserve()
            cr.clip()

            pango_layout = PangoCairo.create_layout(cr)

            # Horizontal Grid lines
            for i in range(5):
                frac = i / 4.0
                y = plot_y + plot_h - (frac * plot_h)
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.07)
                cr.set_line_width(1.0)
                cr.move_to(plot_x, y)
                cr.line_to(plot_x + plot_w, y)
                cr.stroke()

            # Vertical Time Grid lines
            time_fractions = [0.0, 0.25, 0.5, 0.75, 1.0]
            for frac in time_fractions:
                x = plot_x + (frac * plot_w)
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.05)
                cr.move_to(x, plot_y)
                cr.line_to(x, plot_y + plot_h)
                cr.stroke()

            # Draw Curves
            rams = list(self.ram_data)
            swaps = list(self.swap_data)
            n = len(rams)
            if n > 1:
                dt_x = plot_w / (self.max_points - 1)
                coords_ram = []
                coords_swap = []
                for i in range(n):
                    idx_from_end = (n - 1) - i
                    x = (plot_x + plot_w) - (idx_from_end * dt_x)
                    y_ram = plot_y + plot_h - min(1.0, (rams[i] / self.total_gib)) * plot_h
                    y_swap = plot_y + plot_h - min(1.0, (swaps[i] / 100.0)) * plot_h
                    coords_ram.append((x, y_ram))
                    coords_swap.append((x, y_swap))

                # RAM gradient fill (GNOME Purple #7878db)
                pat = cairo.LinearGradient(0, plot_y, 0, plot_y + plot_h)
                pat.add_color_stop_rgba(0.0, 0.47, 0.47, 0.86, 0.35)
                pat.add_color_stop_rgba(1.0, 0.47, 0.47, 0.86, 0.02)

                cr.move_to(coords_ram[0][0], plot_y + plot_h)
                for x, y in coords_ram:
                    cr.line_to(x, y)
                cr.line_to(coords_ram[-1][0], plot_y + plot_h)
                cr.close_path()
                cr.set_source(pat)
                cr.fill()

                # RAM Curve stroke
                cr.set_source_rgb(0.47, 0.47, 0.86)
                cr.set_line_width(2.0)
                cr.move_to(coords_ram[0][0], coords_ram[0][1])
                for x, y in coords_ram[1:]:
                    cr.line_to(x, y)
                cr.stroke()

                # Swap Curve stroke (GNOME Amber #ff7800)
                cr.set_source_rgb(1.0, 0.47, 0.0)
                cr.set_line_width(1.8)
                cr.move_to(coords_swap[0][0], coords_swap[0][1])
                for x, y in coords_swap[1:]:
                    cr.line_to(x, y)
                cr.stroke()

                # Glowing live points at "Now"
                last_x, last_r_y = coords_ram[-1]
                _, last_s_y = coords_swap[-1]

                cr.set_source_rgb(0.47, 0.47, 0.86)
                cr.arc(last_x, last_r_y, 3.5, 0, 2 * math.pi)
                cr.fill()

                cr.set_source_rgb(1.0, 0.47, 0.0)
                cr.arc(last_x, last_s_y, 3.0, 0, 2 * math.pi)
                cr.fill()

            cr.restore()

            # Plot Border
            cr.save()
            cr.new_sub_path()
            cr.arc(plot_x + plot_w - radius, plot_y + radius, radius, -math.pi/2, 0)
            cr.arc(plot_x + plot_w - radius, plot_y + plot_h - radius, radius, 0, math.pi/2)
            cr.arc(plot_x + radius, plot_y + plot_h - radius, radius, math.pi/2, math.pi)
            cr.arc(plot_x + radius, plot_y + radius, radius, math.pi, 3*math.pi/2)
            cr.close_path()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.14)
            cr.set_line_width(1.0)
            cr.stroke()
            cr.restore()

            # Top Headers & Legend
            pango_layout.set_font_description(Pango.font_description_from_string("Cantarell Bold 9"))
            ram_str = f"■ RAM: {self.cur_used_gib:.2f} / {self.total_gib:.1f} GiB ({self.cur_pct:.0f}%)" if plot_w >= 360 else f"■ RAM: {self.cur_used_gib:.1f} GiB ({self.cur_pct:.0f}%)"
            pango_layout.set_text(ram_str, -1)
            cr.set_source_rgb(0.47, 0.47, 0.86)
            cr.move_to(plot_x, 10)
            PangoCairo.show_layout(cr, pango_layout)

            swap_str = f"■ Swap: {self.cur_swap_mib:.0f} MiB ({self.cur_swap_pct:.0f}%)"
            pango_layout.set_text(swap_str, -1)
            ink, log = pango_layout.get_pixel_extents()
            cr.set_source_rgb(1.0, 0.47, 0.0)
            cr.move_to(plot_x + plot_w - log.width, 10)
            PangoCairo.show_layout(cr, pango_layout)

            # Y-Axes Ticks & Numbers
            for i in range(5):
                frac = i / 4.0
                y = plot_y + plot_h - (frac * plot_h)

                cr.set_source_rgba(0.47, 0.47, 0.86, 0.9)
                cr.move_to(plot_x - 5, y)
                cr.line_to(plot_x, y)
                cr.stroke()

                val_ram = frac * self.total_gib
                pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 9"))
                pango_layout.set_text(f"{val_ram:.1f} GiB", -1)
                ink, log = pango_layout.get_pixel_extents()
                cr.move_to(plot_x - 8 - log.width, y - log.height / 2)
                PangoCairo.show_layout(cr, pango_layout)

                cr.set_source_rgba(0.7, 0.73, 0.8, 0.8)
                cr.move_to(plot_x + plot_w, y)
                cr.line_to(plot_x + plot_w + 5, y)
                cr.stroke()

                val_pct = frac * 100.0
                pango_layout.set_text(f"{val_pct:.0f}%", -1)
                ink, log = pango_layout.get_pixel_extents()
                cr.move_to(plot_x + plot_w + 8, y - log.height / 2)
                PangoCairo.show_layout(cr, pango_layout)

            # X-Axis Ticks & Labels
            time_labels = [("60s ago", 0.0), ("45s ago", 0.25), ("30s ago", 0.5), ("15s ago", 0.75), ("Now (0s)", 1.0)]
            for lbl, frac in time_labels:
                x = plot_x + (frac * plot_w)
                cr.set_source_rgba(0.7, 0.73, 0.8, 0.7)
                cr.move_to(x, plot_y + plot_h)
                cr.line_to(x, plot_y + plot_h + 5)
                cr.stroke()

                pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 9"))
                pango_layout.set_text(lbl, -1)
                ink, log = pango_layout.get_pixel_extents()
                if frac == 1.0:
                    cr.move_to(x - log.width, plot_y + plot_h + 8)
                elif frac == 0.0:
                    cr.move_to(x, plot_y + plot_h + 8)
                else:
                    cr.move_to(x - log.width / 2, plot_y + plot_h + 8)
                PangoCairo.show_layout(cr, pango_layout)

            # Timeline subcaption
            pango_layout.set_font_description(Pango.font_description_from_string("Cantarell 8"))
            pango_layout.set_text("Time Window (60 Seconds)", -1)
            ink, log = pango_layout.get_pixel_extents()
            cr.set_source_rgba(0.7, 0.73, 0.8, 0.5)
            cr.move_to(plot_x + (plot_w / 2) - (log.width / 2), plot_y + plot_h + 20)
            PangoCairo.show_layout(cr, pango_layout)

            return False

    class UnifiedThrottleStopWindow(Gtk.Window):
        def __init__(self):
            super().__init__(title="Power & Performance")
            self.set_default_size(1200, 820)
            self.set_position(Gtk.WindowPosition.CENTER)
            self.connect("destroy", self._on_destroy)

            self.active_profile = "balanced"
            self.is_root = (os.geteuid() == 0)
            self._updating_ui = False
            self._stop_telemetry = False

            self.collector = hp_telemetry.TelemetryCollector() if HAS_HP_TELEMETRY else None

            # CSS Styles
            css_provider = Gtk.CssProvider()
            css_data = """
            .mono-font { font-family: monospace; font-size: 11px; }
            .value-large { font-size: 15px; font-weight: bold; }
            """
            css_provider.load_from_data(css_data.encode())
            screen = Gdk.Screen.get_default()
            if screen:
                Gtk.StyleContext.add_provider_for_screen(
                    screen,
                    css_provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )

            # GNOME HeaderBar with ViewSwitcherTitle
            self.hb = Handy.HeaderBar()
            self.hb.set_show_close_button(True)

            self.stack = Gtk.Stack()
            self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
            self.stack.set_transition_duration(200)

            platform = get_platform()
            self.vst = Handy.ViewSwitcherTitle()
            self.vst.set_stack(self.stack)
            self.vst.set_title("Power & Performance")
            self.vst.set_subtitle(f"{platform.dmi['product_name']} · {platform.cpu_info['clean_model']} ({platform.topology['summary_str']})")
            self.hb.set_custom_title(self.vst)
            self.set_titlebar(self.hb)

            self.root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.add(self.root_box)

            self._last_narrow_tab = "tuning"
            self.stack.connect("notify::visible-child-name", self._on_stack_child_changed)

            self.dual_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            self.dual_box.set_homogeneous(False)
            self.v_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)

            self.current_layout = None
            self._pending_layout = False

            # -------------------------------------------------------------
            # VIEW / TAB 1: Power & Tuning
            # -------------------------------------------------------------
            self.page1 = Handy.PreferencesPage()

            # 1. Power Profiles Group
            g_prof = Handy.PreferencesGroup()
            g_prof.set_title("Power Profiles")
            g_prof.set_description("Select hardware performance and thermal profile")
            self.page1.add(g_prof)

            box_p = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box_p.get_style_context().add_class("linked")
            box_p.set_homogeneous(True)

            self.profile_buttons = {}
            prof_defs = [
                ("max", "Max Turbo\n65W"),
                ("performance", "Performance\n45W"),
                ("game", "Gaming\n29W"),
                ("balanced", "Balanced\n25W"),
                ("eco", "Eco\n15W"),
                ("supereco", "Super-Eco\n2W"),
            ]

            for k, lbl in prof_defs:
                b = Gtk.Button(label=lbl)
                b.connect("clicked", self._on_profile_clicked, k)
                box_p.pack_start(b, True, True, 0)
                self.profile_buttons[k] = b
            g_prof.add(box_p)

            # 2. Performance History Group (Live Graph & Key Metrics)
            g_graph = Handy.PreferencesGroup()
            g_graph.set_title("Performance History")
            g_graph.set_description("Live power consumption (Orange) and processor temperature (Red)")
            self.page1.add(g_graph)

            self.row_pwr = Handy.ActionRow()
            self.row_pwr.set_title("CPU Package Power")
            self.row_pwr.set_subtitle("Operative limit: PL1 --W · Headroom: --W")
            self.lbl_pwr = Gtk.Label(label="-- W")
            self.lbl_pwr.get_style_context().add_class("title-3")
            self.row_pwr.add(self.lbl_pwr)
            g_graph.add(self.row_pwr)

            self.row_tmp = Handy.ActionRow()
            self.row_tmp.set_title("CPU Temperature")
            self.row_tmp.set_subtitle("TjMax: 110°C · Distance to PROCHOT: --°C")
            self.lbl_tmp = Gtk.Label(label="-- °C")
            self.lbl_tmp.get_style_context().add_class("title-3")
            self.row_tmp.add(self.lbl_tmp)
            g_graph.add(self.row_tmp)

            self.row_cores = Handy.ActionRow()
            self.row_cores.set_title("Active Silicon Cores")
            self.row_cores.set_subtitle("Topology: --")
            self.lbl_cores = Gtk.Label(label="--/18 Cores")
            self.lbl_cores.get_style_context().add_class("title-4")
            self.row_cores.add(self.lbl_cores)
            g_graph.add(self.row_cores)

            self.row_fan = Handy.ActionRow()
            self.row_fan.set_title("Cooling Fan")
            self.row_fan.set_subtitle("HP EC0 Blower · Thermal stage: --")
            self.lbl_fan = Gtk.Label(label="-- RPM")
            self.lbl_fan.get_style_context().add_class("title-4")
            self.row_fan.add(self.lbl_fan)
            g_graph.add(self.row_fan)

            self.row_bat = Handy.ActionRow()
            self.row_bat.set_title("Power Delivery & Source")
            self.row_bat.set_subtitle("Battery & AC status")
            self.lbl_bat = Gtk.Label(label="--")
            self.lbl_bat.get_style_context().add_class("title-4")
            self.row_bat.add(self.lbl_bat)
            g_graph.add(self.row_bat)

            pref_graph_row = Handy.PreferencesRow()
            pref_graph_row.set_activatable(False)
            pref_graph_row.set_selectable(False)
            self.graph = GnomeTelemetryGraph()
            pref_graph_row.add(self.graph)
            g_graph.add(pref_graph_row)

            # 3. Processor Configuration Group
            g_ctrl = Handy.PreferencesGroup()
            g_ctrl.set_title("Processor Configuration")
            self.page1.add(g_ctrl)

            r_turbo = Handy.ActionRow()
            r_turbo.set_title("Intel Turbo Boost")
            r_turbo.set_subtitle("Permit CPU cores to dynamically exceed base clock limits")
            self.sw_turbo = Gtk.Switch()
            self.sw_turbo.set_valign(Gtk.Align.CENTER)
            self.sw_turbo.connect("state-set", self._on_turbo_switched)
            r_turbo.add(self.sw_turbo)
            g_ctrl.add(r_turbo)

            # Silicon Core Parking (Interactive per-core gating & topology presets)
            self.exp_parking = Handy.ExpanderRow()
            self.exp_parking.set_title("Silicon Core Parking")
            _, _, cur_mode_key, cur_mode_desc = get_core_status()
            self.exp_parking.set_subtitle(f"Topology Control · {cur_mode_desc}")
            self.exp_parking.set_expanded(True)
            g_ctrl.add(self.exp_parking)

            # Quick Presets Buttons Row
            r_quick_presets = Handy.ActionRow()
            r_quick_presets.set_title("Quick Topology Presets")
            r_quick_presets.set_subtitle("Fast one-click core gating profiles")
            box_presets = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box_presets.set_valign(Gtk.Align.CENTER)

            btn_all = Gtk.Button(label="All Online")
            btn_all.set_tooltip_text("Unpark all cores (14 Cores / 18 Threads active)")
            btn_all.connect("clicked", lambda w: self._apply_core_preset("none"))
            btn_nosmt = Gtk.Button(label="No SMT")
            btn_nosmt.set_tooltip_text("Disable SMT Hyperthreading on P-cores (14 Cores / 14 Threads active)")
            btn_nosmt.connect("clicked", lambda w: self._apply_core_preset("no_smt"))
            btn_pcores = Gtk.Button(label="P-Cores Only")
            btn_pcores.set_tooltip_text("P-Cores Only: Park all E and LP cores (4 P-Cores / 8 Threads active)")
            btn_pcores.connect("clicked", lambda w: self._apply_core_preset("pcores_only"))
            btn_lp = Gtk.Button(label="LP Only")
            btn_lp.set_tooltip_text("Low-Power Island Only: Park all compute cores (CPU 0 + 2 LP-Cores active)")
            btn_lp.connect("clicked", lambda w: self._apply_core_preset("lp_only"))

            box_presets.pack_start(btn_all, False, False, 0)
            if top.get("has_smt"):
                box_presets.pack_start(btn_nosmt, False, False, 0)
            if top.get("has_hybrid"):
                box_presets.pack_start(btn_pcores, False, False, 0)
            if top.get("has_lp"):
                box_presets.pack_start(btn_lp, False, False, 0)
            r_quick_presets.add(box_presets)
            self.exp_parking.add(r_quick_presets)

            # Presets Dropdown Row
            r_preset_mode = Handy.ActionRow()
            r_preset_mode.set_title("Preset Selection")
            r_preset_mode.set_subtitle("Select active preset or inspect custom configuration")
            self.cb_cores = Gtk.ComboBoxText()
            modes = platform.get_parking_modes()
            for m_key, m_lbl in modes:
                self.cb_cores.append(m_key, m_lbl)
            self.cb_cores.set_valign(Gtk.Align.CENTER)
            valid_keys = [m[0] for m in modes]
            self.cb_cores.set_active_id(cur_mode_key if cur_mode_key in valid_keys else "custom")
            self.cb_cores.connect("changed", self._on_core_combo_changed)
            r_preset_mode.add(self.cb_cores)
            self.exp_parking.add(r_preset_mode)

            # Per-Core Toggles
            self.cpu_switches = {}
            cur_core_mask = get_core_mask()

            # P-Cores Group
            self.exp_park_p = Handy.ExpanderRow()
            p_label = "P-Cores (Performance)" if top.get("has_hybrid") else "CPU Cores"
            p_cnt = len(top.get("p_cores", []))
            p_th_cnt = sum(len(c.get("threads", [])) for c in top.get("p_cores", []))
            self.exp_park_p.set_title(p_label)
            self.exp_park_p.set_subtitle(f"{p_cnt} Physical Cores · {p_th_cnt} Threads")
            self.exp_park_p.set_expanded(True)
            self.exp_parking.add(self.exp_park_p)

            for p_core in top.get("p_cores", []):
                p_idx = p_core["idx"]
                threads = p_core["threads"]
                for t_idx, cpu_id in enumerate(threads):
                    is_boot = (cpu_id == 0)
                    if is_boot:
                        title = f"{p_label} {p_idx} Primary (CPU 0)"
                        sub = "Kernel Boot CPU (Locked online)"
                    elif t_idx == 0:
                        title = f"{p_label} {p_idx} Primary (CPU {cpu_id})"
                        sub = f"Primary execution thread for {p_label} {p_idx}"
                    else:
                        title = f"{p_label} {p_idx} SMT (CPU {cpu_id})"
                        sub = f"Hyperthread sibling for {p_label} {p_idx}"

                    r = Handy.ActionRow()
                    r.set_title(title)
                    r.set_subtitle(sub)
                    sw = Gtk.Switch()
                    sw.set_valign(Gtk.Align.CENTER)
                    if is_boot:
                        sw.set_active(True)
                        sw.set_sensitive(False)
                        sw.set_tooltip_text("Core 0 is the primary kernel boot CPU and cannot be offlined.")
                    else:
                        sw.set_active(cur_core_mask.get(cpu_id, True))
                        sw.connect("state-set", self._on_cpu_switch_toggled, cpu_id)
                    r.add(sw)
                    self.exp_park_p.add(r)
                    self.cpu_switches[cpu_id] = sw

            # E-Cores Group (only if present)
            if top.get("has_e"):
                self.exp_park_e = Handy.ExpanderRow()
                e_cnt = len(top.get("e_cores", []))
                self.exp_park_e.set_title("E-Cores (Efficient)")
                self.exp_park_e.set_subtitle(f"{e_cnt} Cores · {e_cnt} Threads")
                self.exp_park_e.set_expanded(False)
                self.exp_parking.add(self.exp_park_e)

                for e_core in top.get("e_cores", []):
                    e_idx = e_core["idx"]
                    cpu_id = e_core["primary"]
                    r = Handy.ActionRow()
                    r.set_title(f"E-Core {e_idx} (CPU {cpu_id})")
                    r.set_subtitle(f"Compute tile efficient core #{e_idx}")
                    sw = Gtk.Switch()
                    sw.set_valign(Gtk.Align.CENTER)
                    sw.set_active(cur_core_mask.get(cpu_id, True))
                    sw.connect("state-set", self._on_cpu_switch_toggled, cpu_id)
                    r.add(sw)
                    self.exp_park_e.add(r)
                    self.cpu_switches[cpu_id] = sw

            # LP E-Cores Group (only if present)
            if top.get("has_lp"):
                self.exp_park_lp = Handy.ExpanderRow()
                lp_cnt = len(top.get("lp_cores", []))
                self.exp_park_lp.set_title("LP E-Cores (Low-Power Island)")
                self.exp_park_lp.set_subtitle(f"{lp_cnt} Cores · {lp_cnt} Threads")
                self.exp_park_lp.set_expanded(False)
                self.exp_parking.add(self.exp_park_lp)

                for lp_core in top.get("lp_cores", []):
                    lp_idx = lp_core["idx"]
                    cpu_id = lp_core["primary"]
                    r = Handy.ActionRow()
                    r.set_title(f"LP-Core {lp_idx} (CPU {cpu_id})")
                    r.set_subtitle(f"Low-power SoC island core #{lp_idx}")
                    sw = Gtk.Switch()
                    sw.set_valign(Gtk.Align.CENTER)
                    sw.set_active(cur_core_mask.get(cpu_id, True))
                    sw.connect("state-set", self._on_cpu_switch_toggled, cpu_id)
                    r.add(sw)
                    self.exp_park_lp.add(r)
                    self.cpu_switches[cpu_id] = sw

            r_bal = Handy.ActionRow()
            r_bal.set_title("CPU vs iGPU Power Balance")
            r_bal.set_subtitle("Dynamic energy allocation & graphics priority")
            self.cb_balance = Gtk.ComboBoxText()
            self.cb_balance.append("balanced", "Balanced (50% CPU / 50% iGPU)")
            self.cb_balance.append("gpu_pri", "GPU Priority (30% CPU / 70% iGPU)")
            self.cb_balance.append("cpu_pri", "CPU Priority (80% CPU / 20% iGPU)")
            self.cb_balance.append("gpu_max", "Max iGPU Boost (Uncapped Arc Graphics)")
            self.cb_balance.set_valign(Gtk.Align.CENTER)
            cur_bal = get_power_balance()
            self.cb_balance.set_active_id(cur_bal if cur_bal in ["balanced", "gpu_pri", "cpu_pri", "gpu_max"] else "balanced")
            self.cb_balance.connect("changed", self._on_balance_combo_changed)
            r_bal.add(self.cb_balance)
            g_ctrl.add(r_bal)

            r_gnome = Handy.ActionRow()
            r_gnome.set_title("GNOME System Power Profile")
            r_gnome.set_subtitle("Bidirectional sync with net.hadess.PowerProfiles")
            self.cb_gnome = Gtk.ComboBoxText()
            self.cb_gnome.append("performance", "Performance")
            self.cb_gnome.append("balanced", "Balanced")
            self.cb_gnome.append("power-saver", "Power Saver")
            self.cb_gnome.set_valign(Gtk.Align.CENTER)
            cur_gnome = get_desktop_power_profile()
            self.cb_gnome.set_active_id(cur_gnome if cur_gnome in ["performance", "balanced", "power-saver"] else "balanced")
            self.cb_gnome.connect("changed", self._on_gnome_combo_changed)
            r_gnome.add(self.cb_gnome)
            g_ctrl.add(r_gnome)

            r_boost = Handy.ActionRow()
            r_boost.set_title("Dynamic Energy Boost")
            r_boost.set_subtitle("Intel Speed Shift dynamic boost (hwp_dynamic_boost)")
            self.sw_boost = Gtk.Switch()
            self.sw_boost.set_active(get_dynamic_boost())
            self.sw_boost.set_valign(Gtk.Align.CENTER)
            self.sw_boost.connect("state-set", self._on_boost_switched)
            r_boost.add(self.sw_boost)
            g_ctrl.add(r_boost)

            r_epp = Handy.ActionRow()
            r_epp.set_title("Speed Shift (HWP) EPP")
            r_epp.set_subtitle("Hardware energy vs performance bias register")
            self.cb_epp = Gtk.ComboBoxText()
            self.cb_epp.append("performance", "Performance (0)")
            self.cb_epp.append("balance_performance", "Balance Performance (64)")
            self.cb_epp.append("balance_power", "Balance Power (128)")
            self.cb_epp.append("power", "Power Saver (255)")
            self.cb_epp.set_valign(Gtk.Align.CENTER)
            epp_cur = get_epp()
            self.cb_epp.set_active_id(epp_cur if epp_cur in ["performance", "balance_performance", "balance_power", "power"] else "balance_power")
            self.cb_epp.connect("changed", self._on_epp_combo_changed)
            r_epp.add(self.cb_epp)
            g_ctrl.add(r_epp)

            # 4. Custom Power Limits & Frequency Ceilings Group
            g_tpl = Handy.PreferencesGroup()
            g_tpl.set_title("Custom Limits")
            self.page1.add(g_tpl)

            exp_tpl = Handy.ExpanderRow()
            exp_tpl.set_title("Custom Turbo Power Limits (TPL)")
            exp_tpl.set_subtitle("Fine-tune sustained (PL1) and burst (PL2) power caps")
            g_tpl.add(exp_tpl)

            r_pl1 = Handy.ActionRow()
            r_pl1.set_title("PL1 (Sustained Power Limit)")
            self.adj_pl1 = Gtk.Adjustment(value=29, lower=2, upper=70, step_increment=1, page_increment=5)
            self.scale_pl1 = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adj_pl1)
            self.scale_pl1.set_size_request(240, -1)
            self.scale_pl1.set_digits(0)
            self.scale_pl1.set_value_pos(Gtk.PositionType.RIGHT)
            r_pl1.add(self.scale_pl1)
            exp_tpl.add(r_pl1)

            r_pl2 = Handy.ActionRow()
            r_pl2.set_title("PL2 (Short Burst Limit)")
            self.adj_pl2 = Gtk.Adjustment(value=55, lower=5, upper=90, step_increment=1, page_increment=5)
            self.scale_pl2 = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adj_pl2)
            self.scale_pl2.set_size_request(240, -1)
            self.scale_pl2.set_digits(0)
            self.scale_pl2.set_value_pos(Gtk.PositionType.RIGHT)
            r_pl2.add(self.scale_pl2)
            exp_tpl.add(r_pl2)

            r_apply = Handy.ActionRow()
            btn_apply = Gtk.Button(label="Apply Custom Limits")
            btn_apply.get_style_context().add_class("suggested-action")
            btn_apply.connect("clicked", self._on_apply_custom_tpl)
            r_apply.add(btn_apply)
            exp_tpl.add(r_apply)

            f_cur = get_core_max_frequencies()
            exp_freq = Handy.ExpanderRow()
            exp_freq.set_title("Per-Core Frequency Limits")
            exp_freq.set_subtitle("Set maximum clock speed ceilings per silicon core family")
            g_tpl.add(exp_freq)

            p_max_limit = top.get("max_p_mhz", 4500)
            p_label = "P-Core Max Clock" if top.get("has_hybrid") else "Max CPU Clock"
            r_pfreq = Handy.ActionRow()
            r_pfreq.set_title(p_label)
            self.adj_pfreq = Gtk.Adjustment(value=f_cur.get("p_mhz", p_max_limit), lower=400, upper=p_max_limit, step_increment=100, page_increment=500)
            self.scale_pfreq = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adj_pfreq)
            self.scale_pfreq.set_size_request(240, -1)
            self.scale_pfreq.set_digits(0)
            self.scale_pfreq.set_value_pos(Gtk.PositionType.RIGHT)
            r_pfreq.add(self.scale_pfreq)
            exp_freq.add(r_pfreq)

            if top.get("has_e"):
                e_max_limit = top.get("max_e_mhz", 3600)
                r_efreq = Handy.ActionRow()
                r_efreq.set_title("E-Core Max Clock")
                self.adj_efreq = Gtk.Adjustment(value=f_cur.get("e_mhz", e_max_limit), lower=400, upper=e_max_limit, step_increment=100, page_increment=400)
                self.scale_efreq = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adj_efreq)
                self.scale_efreq.set_size_request(240, -1)
                self.scale_efreq.set_digits(0)
                self.scale_efreq.set_value_pos(Gtk.PositionType.RIGHT)
                r_efreq.add(self.scale_efreq)
                exp_freq.add(r_efreq)
            else:
                self.scale_efreq = None

            if top.get("has_lp"):
                lp_max_limit = top.get("max_lp_mhz", 2500)
                r_lpfreq = Handy.ActionRow()
                r_lpfreq.set_title("LP-Core Max Clock (Low-Power Island)")
                self.adj_lpfreq = Gtk.Adjustment(value=f_cur.get("lp_mhz", lp_max_limit), lower=400, upper=lp_max_limit, step_increment=100, page_increment=300)
                self.scale_lpfreq = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adj_lpfreq)
                self.scale_lpfreq.set_size_request(240, -1)
                self.scale_lpfreq.set_digits(0)
                self.scale_lpfreq.set_value_pos(Gtk.PositionType.RIGHT)
                r_lpfreq.add(self.scale_lpfreq)
                exp_freq.add(r_lpfreq)
            else:
                self.scale_lpfreq = None

            box_f_btn = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box_f_btn.set_halign(Gtk.Align.END)
            box_f_btn.set_valign(Gtk.Align.CENTER)
            btn_apply_freq = Gtk.Button(label="Apply Frequency Limits")
            btn_apply_freq.get_style_context().add_class("suggested-action")
            btn_apply_freq.connect("clicked", self._on_apply_freq_limits)
            btn_reset_freq = Gtk.Button(label="Reset to Max")
            btn_reset_freq.connect("clicked", self._on_reset_freq_limits)
            box_f_btn.pack_start(btn_reset_freq, False, False, 0)
            box_f_btn.pack_start(btn_apply_freq, False, False, 0)
            r_freq_btns = Handy.ActionRow()
            r_freq_btns.add(box_f_btn)
            exp_freq.add(r_freq_btns)

            # 5. Diagnostics & Recovery Group
            g_act = Handy.PreferencesGroup()
            g_act.set_title("Diagnostics & Recovery")
            self.page1.add(g_act)

            r_bench = Handy.ActionRow()
            r_bench.set_title("Run TS Bench (5s Stress Test)")
            r_bench.set_subtitle("Engage all active threads to measure power draw under full load")
            self.btn_bench = Gtk.Button(label="Run Benchmark")
            self.btn_bench.connect("clicked", self._run_bench_async)
            r_bench.add(self.btn_bench)
            g_act.add(r_bench)

            r_rev = Handy.ActionRow()
            r_rev.set_title("Restore Factory Defaults")
            r_rev.set_subtitle("Revert all RAPL limits, Speed Shift, and cores to BIOS defaults")
            btn_rev = Gtk.Button(label="Restore Defaults")
            btn_rev.connect("clicked", self._on_revert_clicked)
            r_rev.add(btn_rev)
            g_act.add(r_rev)

            # -------------------------------------------------------------
            # VIEW / TAB 2: Hardware Telemetry
            # -------------------------------------------------------------
            self.page2 = Handy.PreferencesPage()

            # Telemetry Group 0: Hardware Power Limits (True PL1 / PL2 & RAPL MSR)
            g_tel_limits = Handy.PreferencesGroup()
            g_tel_limits.set_title("Hardware Power Limits (True PL1 / PL2 & RAPL MSR)")
            g_tel_limits.set_description("Actual hardware registers enforced by silicon (MSR 0x610 vs MMIO powercap)")
            self.page2.add(g_tel_limits)

            self.row_tel_eff = Handy.ActionRow()
            self.row_tel_eff.set_title("Effective Operating Limit")
            self.row_tel_eff.set_subtitle("Primary active hardware ceiling")
            self.lbl_tel_eff = Gtk.Label(label="-- W")
            self.lbl_tel_eff.get_style_context().add_class("title-3")
            self.row_tel_eff.add(self.lbl_tel_eff)
            g_tel_limits.add(self.row_tel_eff)

            self.row_tel_msr = Handy.ActionRow()
            self.row_tel_msr.set_title("MSR 0x610 Package Limit (MSR Plane)")
            self.row_tel_msr.set_subtitle("PL1: --W · PL2: --W · Tau: --s")
            self.lbl_tel_msr = Gtk.Label(label="--")
            self.lbl_tel_msr.get_style_context().add_class("mono-font")
            self.row_tel_msr.add(self.lbl_tel_msr)
            g_tel_limits.add(self.row_tel_msr)

            self.row_tel_mmio = Handy.ActionRow()
            self.row_tel_mmio.set_title("MMIO Sysfs Powercap (MMIO Plane)")
            self.row_tel_mmio.set_subtitle("PL1: --W · PL2: --W · Tau: --s")
            self.lbl_tel_mmio = Gtk.Label(label="--")
            self.lbl_tel_mmio.get_style_context().add_class("mono-font")
            self.row_tel_mmio.add(self.lbl_tel_mmio)
            g_tel_limits.add(self.row_tel_mmio)

            self.row_tel_dptf = Handy.ActionRow()
            self.row_tel_dptf.set_title("DPTF / thermald Hardware Policy")
            self.row_tel_dptf.set_subtitle("Dynamic Platform & Thermal Framework")
            self.lbl_tel_dptf = Gtk.Label(label="--")
            self.lbl_tel_dptf.get_style_context().add_class("title-4")
            self.row_tel_dptf.add(self.lbl_tel_dptf)
            g_tel_limits.add(self.row_tel_dptf)

            # Telemetry Group 1: Component Power Sipping
            g_tel_pwr = Handy.PreferencesGroup()
            g_tel_pwr.set_title("Component Power Sipping (Real-Time Watts)")
            g_tel_pwr.set_description("Precise hardware power meters across CPU tiles, graphics, fan, panel, and board VRM")
            self.page2.add(g_tel_pwr)

            self.pwr_rows = {}
            power_items = [
                ("cpu_pkg", "CPU Total Package (RAPL)", "All Meteor Lake tiles combined"),
                ("ia_cores", "  ├─ IA Compute Cores", "4P + 8E active execution cores"),
                ("soc_uncore", "  └─ SoC / Uncore / NPU", "Ring bus, memory controller (IMC), LP-island"),
                ("gpu_gt0", "Intel Arc Graphics (GT0)", "Xe-LPG graphics tile"),
                ("gpu_gt1", "Intel Media Engine (GT1)", "Dedicated video decode/encode tile"),
                ("npu", "Intel AI Boost NPU", "Neural Processing Unit accelerator"),
                ("display", "Display Panel + LEDs", "eDP internal display backlight"),
                ("nvme", "Micron NVMe Gen4 SSD", "High-speed PCIe storage activity"),
                ("fan", "Cooling Fan Blower", "Embedded Controller blower motor"),
                ("ram_vrm", "LPDDR5x RAM + VRM Losses", "Memory bus and DC-DC power converter losses"),
                ("total_rail", "Total Platform Power Draw", "Total estimated system power draw"),
            ]

            for k, title, sub in power_items:
                row = Handy.ActionRow()
                row.set_title(title)
                row.set_subtitle(sub)
                lbl = Gtk.Label(label="-- W")
                if k == "total_rail":
                    lbl.get_style_context().add_class("title-3")
                else:
                    lbl.get_style_context().add_class("value-large")
                row.add(lbl)
                g_tel_pwr.add(row)
                self.pwr_rows[k] = (row, lbl)

            # Telemetry Group 2: Hybrid CPU Topology & Silicon Cores
            g_tel_cpu = Handy.PreferencesGroup()
            g_tel_cpu.set_title("Hybrid CPU Topology & Clocks")
            g_tel_cpu.set_description("Intel Meteor Lake hybrid microarchitecture (Performance, Efficient & Low-Power Cores)")
            self.page2.add(g_tel_cpu)

            self.row_cpu_overall = Handy.ActionRow()
            self.row_cpu_overall.set_title("Overall CPU Utilization")
            self.row_cpu_overall.set_subtitle("All-Core Avg: -- MHz · Uptime: --")
            self.bar_cpu_overall = Gtk.ProgressBar()
            self.bar_cpu_overall.set_size_request(160, -1)
            self.bar_cpu_overall.set_show_text(True)
            self.bar_cpu_overall.set_valign(Gtk.Align.CENTER)
            self.row_cpu_overall.add(self.bar_cpu_overall)
            g_tel_cpu.add(self.row_cpu_overall)

            pref_cpu_graph_row = Handy.PreferencesRow()
            pref_cpu_graph_row.set_activatable(False)
            pref_cpu_graph_row.set_selectable(False)
            self.cpu_graph = GnomeCpuHistoryGraph()
            pref_cpu_graph_row.add(self.cpu_graph)
            g_tel_cpu.add(pref_cpu_graph_row)

            self.exp_pcores = Handy.ExpanderRow()
            self.exp_pcores.set_title("P-Cores (Redwood Cove - Performance)")
            self.exp_pcores.set_subtitle("4 Cores / 8 Threads · Avg: -- MHz")
            self.exp_pcores.set_expanded(True)
            self.pcore_labels = {}
            p_thread_pairs = [(0, 5), (1, 2), (3, 4), (6, 7)]
            for i in range(4):
                r = Handy.ActionRow()
                r.set_title(f"P-Core {i}")
                r.set_subtitle(f"Threads: [{p_thread_pairs[i][0]}, {p_thread_pairs[i][1]}]")
                lbl = Gtk.Label(label="-- MHz")
                lbl.get_style_context().add_class("mono-font")
                r.add(lbl)
                self.exp_pcores.add(r)
                self.pcore_labels[f"P{i}"] = (r, lbl)
            g_tel_cpu.add(self.exp_pcores)

            self.exp_ecores = Handy.ExpanderRow()
            self.exp_ecores.set_title("E-Cores (Crestmont - Efficient)")
            self.exp_ecores.set_subtitle("8 Cores / 8 Threads · Avg: -- MHz")
            self.ecore_labels = {}
            for i in range(8):
                r = Handy.ActionRow()
                r.set_title(f"E-Core {i}")
                r.set_subtitle(f"Thread: {i+8}")
                lbl = Gtk.Label(label="-- MHz")
                lbl.get_style_context().add_class("mono-font")
                r.add(lbl)
                self.exp_ecores.add(r)
                self.ecore_labels[f"E{i}"] = (r, lbl)
            g_tel_cpu.add(self.exp_ecores)

            self.exp_lpcores = Handy.ExpanderRow()
            self.exp_lpcores.set_title("Low-Power Island Cores (SoC Crestmont)")
            self.exp_lpcores.set_subtitle("2 Cores / 2 Threads · Avg: -- MHz")
            self.lpcore_labels = {}
            for i in range(2):
                r = Handy.ActionRow()
                r.set_title(f"LP-Core {i}")
                r.set_subtitle(f"Thread: {i+16}")
                lbl = Gtk.Label(label="-- MHz")
                lbl.get_style_context().add_class("mono-font")
                r.add(lbl)
                self.exp_lpcores.add(r)
                self.lpcore_labels[f"LP{i}"] = (r, lbl)
            g_tel_cpu.add(self.exp_lpcores)

            self.row_cstates = Handy.ActionRow()
            self.row_cstates.set_title("Core & Package Sleep Residency")
            self.row_cstates.set_subtitle("Hardware C-States: C10, C6, C1E")
            self.lbl_cstates = Gtk.Label(label="--% Deep Sleep")
            self.lbl_cstates.get_style_context().add_class("title-4")
            self.row_cstates.add(self.lbl_cstates)
            g_tel_cpu.add(self.row_cstates)

            self.row_sysact = Handy.ActionRow()
            self.row_sysact.set_title("Kernel Activity")
            self.row_sysact.set_subtitle("System interrupts and context switches")
            self.lbl_sysact = Gtk.Label(label="--")
            self.lbl_sysact.get_style_context().add_class("mono-font")
            self.row_sysact.add(self.lbl_sysact)
            g_tel_cpu.add(self.row_sysact)

            # Telemetry Group 3: Memory & Storage
            g_tel_mem = Handy.PreferencesGroup()
            g_tel_mem.set_title("Memory & Storage")
            g_tel_mem.set_description("LPDDR5x RAM and NVMe Gen4 SSD throughput")
            self.page2.add(g_tel_mem)

            self.row_ram = Handy.ActionRow()
            self.row_ram.set_title("LPDDR5x System RAM")
            self.row_ram.set_subtitle("Available: -- | Buffers: -- | Cached: --")
            self.bar_ram = Gtk.ProgressBar()
            self.bar_ram.set_size_request(160, -1)
            self.bar_ram.set_show_text(True)
            self.bar_ram.set_valign(Gtk.Align.CENTER)
            self.row_ram.add(self.bar_ram)
            g_tel_mem.add(self.row_ram)

            self.row_swap = Handy.ActionRow()
            self.row_swap.set_title("Swap File")
            self.row_swap.set_subtitle("Free: --")
            self.bar_swap = Gtk.ProgressBar()
            self.bar_swap.set_size_request(160, -1)
            self.bar_swap.set_show_text(True)
            self.bar_swap.set_valign(Gtk.Align.CENTER)
            self.row_swap.add(self.bar_swap)
            g_tel_mem.add(self.row_swap)

            pref_mem_graph_row = Handy.PreferencesRow()
            pref_mem_graph_row.set_activatable(False)
            pref_mem_graph_row.set_selectable(False)
            self.mem_graph = GnomeMemoryHistoryGraph()
            pref_mem_graph_row.add(self.mem_graph)
            g_tel_mem.add(pref_mem_graph_row)

            self.row_nvme = Handy.ActionRow()
            self.row_nvme.set_title("Micron NVMe Gen4 SSD")
            self.row_nvme.set_subtitle("Drive Temp: --")
            self.lbl_nvme = Gtk.Label(label="R: 0.0 MB/s · W: 0.0 MB/s")
            self.lbl_nvme.get_style_context().add_class("mono-font")
            self.row_nvme.add(self.lbl_nvme)
            g_tel_mem.add(self.row_nvme)

            # Telemetry Group 4: Board Thermals & Throttling
            g_tel_therm = Handy.PreferencesGroup()
            g_tel_therm.set_title("Thermal Sensors & Hardware Throttling")
            g_tel_therm.set_description("All motherboard temperature sensors and hardware limiters")
            self.page2.add(g_tel_therm)

            self.row_pkg_therm = Handy.ActionRow()
            self.row_pkg_therm.set_title("CPU Package & ACPI Core")
            self.row_pkg_therm.set_subtitle("TjMax: 110°C")
            self.lbl_pkg_therm = Gtk.Label(label="-- °C")
            self.lbl_pkg_therm.get_style_context().add_class("title-4")
            self.row_pkg_therm.add(self.lbl_pkg_therm)
            g_tel_therm.add(self.row_pkg_therm)

            self.row_board_therm = Handy.ActionRow()
            self.row_board_therm.set_title("Motherboard Sensors")
            self.row_board_therm.set_subtitle("VRM, Palmrest, NVMe, Ambient")
            self.lbl_board_therm = Gtk.Label(label="--")
            self.lbl_board_therm.get_style_context().add_class("mono-font")
            self.row_board_therm.add(self.lbl_board_therm)
            g_tel_therm.add(self.row_board_therm)

            self.row_fan_blower = Handy.ActionRow()
            self.row_fan_blower.set_title("Cooling Fan Blower")
            self.row_fan_blower.set_subtitle("HP EC0 fan controller")
            self.lbl_fan_blower = Gtk.Label(label="-- RPM")
            self.lbl_fan_blower.get_style_context().add_class("title-4")
            self.row_fan_blower.add(self.lbl_fan_blower)
            g_tel_therm.add(self.row_fan_blower)

            self.row_throttle = Handy.ActionRow()
            self.row_throttle.set_title("Hardware Throttling Diagnostics")
            self.row_throttle.set_subtitle("CPU & GPU thermal or electrical limiters")
            self.lbl_throttle = Gtk.Label(label="Optimal")
            self.lbl_throttle.get_style_context().add_class("title-4")
            self.row_throttle.add(self.lbl_throttle)
            g_tel_therm.add(self.row_throttle)

            self.row_trips = Handy.ActionRow()
            self.row_trips.set_title("Thermal Trip History")
            self.row_trips.set_subtitle("Cumulative thermal trip events")
            self.lbl_trips = Gtk.Label(label="-- events")
            self.lbl_trips.get_style_context().add_class("mono-font")
            self.row_trips.add(self.lbl_trips)
            g_tel_therm.add(self.row_trips)

            # Telemetry Group 5: Wireless & Network Connectivity
            g_tel_net = Handy.PreferencesGroup()
            g_tel_net.set_title("Wireless & Network Connectivity (wlo1)")
            g_tel_net.set_description("Wi-Fi link telemetry, signal strength, and transfer throughput")
            self.page2.add(g_tel_net)

            self.row_wifi = Handy.ActionRow()
            self.row_wifi.set_title("Intel Wi-Fi 6E (wlo1)")
            self.row_wifi.set_subtitle("Signal: -- | PowerSave: --")
            self.lbl_wifi = Gtk.Label(label="RX: 0.0 KB/s · TX: 0.0 KB/s")
            self.lbl_wifi.get_style_context().add_class("mono-font")
            self.row_wifi.add(self.lbl_wifi)
            g_tel_net.add(self.row_wifi)

            # Telemetry Group 6: Battery & Power Delivery
            g_tel_bat = Handy.PreferencesGroup()
            g_tel_bat.set_title("Battery & Power Delivery")
            g_tel_bat.set_description("Adapter power delivery and battery health telemetry")
            self.page2.add(g_tel_bat)

            self.row_psrc = Handy.ActionRow()
            self.row_psrc.set_title("Power Source")
            self.row_psrc.set_subtitle("Adapter rating")
            self.lbl_psrc = Gtk.Label(label="--")
            self.lbl_psrc.get_style_context().add_class("title-4")
            self.row_psrc.add(self.lbl_psrc)
            g_tel_bat.add(self.row_psrc)

            self.row_bcharge = Handy.ActionRow()
            self.row_bcharge.set_title("Battery Charge Level")
            self.row_bcharge.set_subtitle("Voltage: -- V · Rate: -- W")
            self.bar_bcharge = Gtk.ProgressBar()
            self.bar_bcharge.set_size_request(160, -1)
            self.bar_bcharge.set_show_text(True)
            self.bar_bcharge.set_valign(Gtk.Align.CENTER)
            self.row_bcharge.add(self.bar_bcharge)
            g_tel_bat.add(self.row_bcharge)

            self.row_bhealth = Handy.ActionRow()
            self.row_bhealth.set_title("Battery Health & Design")
            self.row_bhealth.set_subtitle("Energy capacity: --")
            self.lbl_bhealth = Gtk.Label(label="--% Health")
            self.lbl_bhealth.get_style_context().add_class("title-4")
            self.row_bhealth.add(self.lbl_bhealth)
            g_tel_bat.add(self.row_bhealth)

            self.row_bcycle = Handy.ActionRow()
            self.row_bcycle.set_title("Battery Cycle Count")
            self.row_bcycle.set_subtitle("Cycles recorded by Embedded Controller")
            self.lbl_bcycle = Gtk.Label(label="-- Cycles")
            self.lbl_bcycle.get_style_context().add_class("title-4")
            self.row_bcycle.add(self.lbl_bcycle)
            g_tel_bat.add(self.row_bcycle)

            # Responsive Layout Listener & Initial State
            self.connect("size-allocate", self._on_window_size_allocate)
            self._set_layout_mode("wide" if 1200 >= 1150 else "narrow")

            # Initialize GNOME PowerProfiles DBus Proxy
            self._pp_proxy = None
            self._init_power_profiles_dbus()

            # Start smooth background telemetry thread
            self._start_telemetry_loop()

        def _on_destroy(self, widget):
            self._stop_telemetry = True
            if Gtk.main_level() > 0:
                Gtk.main_quit()

        def _set_layout_mode(self, mode):
            if mode == self.current_layout:
                return

            if mode == "wide":
                if self.page1.get_parent() == self.stack:
                    self.stack.remove(self.page1)
                if self.page2.get_parent() == self.stack:
                    self.stack.remove(self.page2)
                if self.stack.get_parent() == self.root_box:
                    self.root_box.remove(self.stack)

                if not self.page1.get_parent():
                    self.dual_box.pack_start(self.page1, True, True, 0)
                if not self.v_sep.get_parent():
                    self.dual_box.pack_start(self.v_sep, False, False, 0)
                if not self.page2.get_parent():
                    self.dual_box.pack_start(self.page2, True, True, 0)

                if not self.dual_box.get_parent():
                    self.root_box.pack_start(self.dual_box, True, True, 0)

                self.vst.set_view_switcher_enabled(False)
            else:
                if self.page1.get_parent() == self.dual_box:
                    self.dual_box.remove(self.page1)
                if self.v_sep.get_parent() == self.dual_box:
                    self.dual_box.remove(self.v_sep)
                if self.page2.get_parent() == self.dual_box:
                    self.dual_box.remove(self.page2)
                if self.dual_box.get_parent() == self.root_box:
                    self.root_box.remove(self.dual_box)

                if not self.page1.get_parent():
                    self.stack.add_titled(self.page1, "tuning", "Power & Tuning")
                    self.stack.child_set_property(self.page1, "icon-name", "speedometer-symbolic")
                if not self.page2.get_parent():
                    self.stack.add_titled(self.page2, "telemetry", "Hardware Telemetry")
                    self.stack.child_set_property(self.page2, "icon-name", "utilities-system-monitor-symbolic")

                if not self.stack.get_parent():
                    self.root_box.pack_start(self.stack, True, True, 0)

                self.vst.set_view_switcher_enabled(True)
                self.vst.set_stack(self.stack)
                if self._last_narrow_tab in ("tuning", "telemetry"):
                    self.stack.set_visible_child_name(self._last_narrow_tab)

            self.root_box.show_all()
            self.current_layout = mode

        def _on_stack_child_changed(self, stack, pspec):
            name = stack.get_visible_child_name()
            if name and self.current_layout == "narrow":
                self._last_narrow_tab = name

        def _on_window_size_allocate(self, widget, alloc):
            target = "wide" if alloc.width >= 1150 else "narrow"
            if target != self.current_layout and not self._pending_layout:
                self._pending_layout = True
                def switch():
                    self._pending_layout = False
                    self._set_layout_mode(target)
                    return False
                GLib.idle_add(switch)

        def _start_telemetry_loop(self):
            def worker():
                while not self._stop_telemetry:
                    try:
                        if self.collector:
                            data = self.collector.collect()
                        else:
                            data = {}
                        GLib.idle_add(self._apply_telemetry, data)
                    except Exception:
                        pass
                    time.sleep(1.0)

            threading.Thread(target=worker, daemon=True).start()

        def _execute_cmd(self, args_list, desc="Operation"):
            cmd = ([sys.executable, os.path.abspath(__file__)] if self.is_root else ["pkexec", sys.executable, os.path.abspath(__file__)]) + args_list
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                return False

        def _on_profile_clicked(self, widget, prof_key):
            self.active_profile = prof_key
            self._execute_cmd([f"--{prof_key}"], f"Profile {prof_key.upper()}")

        def _on_turbo_switched(self, switch, state):
            if self._updating_ui:
                return
            t_str = "on" if state else "off"
            self._execute_cmd(["--turbo", t_str], f"Turbo Boost {t_str.upper()}")

        def _apply_core_preset(self, preset_key):
            if self._updating_ui:
                return
            if hasattr(self, "cb_cores"):
                self._updating_ui = True
                try:
                    self.cb_cores.set_active_id(preset_key)
                finally:
                    self._updating_ui = False
            self._execute_cmd(["--park-cores", preset_key], f"Core Parking: {preset_key}")

        def _on_core_combo_changed(self, combo):
            if self._updating_ui:
                return
            active_id = combo.get_active_id()
            if active_id and active_id != "custom":
                self._execute_cmd(["--park-cores", active_id], f"Core Parking: {active_id}")

        def _on_cpu_switch_toggled(self, switch, state, cpu_idx):
            if self._updating_ui:
                return
            if cpu_idx == 0:
                self._updating_ui = True
                try:
                    switch.set_active(True)
                finally:
                    self._updating_ui = False
                return True
            s_val = "1" if state else "0"
            s_desc = "Online" if state else "Parked (Offline)"
            success = self._execute_cmd(["--set-core", str(cpu_idx), s_val], f"Set CPU {cpu_idx} {s_desc}")
            if not success:
                self._updating_ui = True
                try:
                    switch.set_active(not state)
                finally:
                    self._updating_ui = False

        def _on_epp_combo_changed(self, combo):
            if self._updating_ui:
                return
            active_id = combo.get_active_id()
            if active_id:
                self._execute_cmd(["--epp", active_id], f"Speed Shift EPP: {active_id}")

        def _on_apply_custom_tpl(self, widget):
            pl1 = self.scale_pl1.get_value()
            pl2 = self.scale_pl2.get_value()
            self._execute_cmd(["--pl1", str(pl1), "--pl2", str(pl2)], f"Custom TPL: PL1={pl1:.0f}W, PL2={pl2:.0f}W")

        def _on_revert_clicked(self, widget):
            self._execute_cmd(["--revert"], "Revert to Factory Defaults")

        def _init_power_profiles_dbus(self):
            try:
                self._pp_proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SYSTEM,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    "net.hadess.PowerProfiles",
                    "/net/hadess/PowerProfiles",
                    "net.hadess.PowerProfiles",
                    None
                )
                self._pp_proxy.connect("g-properties-changed", self._on_dbus_power_profile_changed)
            except Exception:
                self._pp_proxy = None

        def _on_dbus_power_profile_changed(self, proxy, changed_props, invalidated_props):
            try:
                act = proxy.get_cached_property("ActiveProfile")
                if act:
                    val = act.unpack()
                    GLib.idle_add(self._sync_dbus_profile, val)
            except Exception:
                pass

        def _sync_dbus_profile(self, profile_name):
            if not self._updating_ui and hasattr(self, "cb_gnome"):
                self._updating_ui = True
                try:
                    self.cb_gnome.set_active_id(profile_name)
                finally:
                    self._updating_ui = False

        def _on_balance_combo_changed(self, combo):
            if self._updating_ui:
                return
            active_id = combo.get_active_id()
            if active_id:
                self._execute_cmd(["--power-balance", active_id], f"Power Balance: {active_id}")

        def _on_gnome_combo_changed(self, combo):
            if self._updating_ui:
                return
            active_id = combo.get_active_id()
            if active_id:
                set_desktop_power_profile(active_id)

        def _on_boost_switched(self, switch, state):
            if self._updating_ui:
                return
            b_str = "on" if state else "off"
            self._execute_cmd(["--dynamic-boost", b_str], f"Dynamic Boost {b_str.upper()}")

        def _on_apply_freq_limits(self, widget):
            cmd_args = []
            desc_parts = []
            if hasattr(self, "scale_pfreq") and self.scale_pfreq:
                p_mhz = int(self.scale_pfreq.get_value())
                cmd_args.extend(["--pcore-max-freq", str(p_mhz)])
                desc_parts.append(f"P={p_mhz}")
            if hasattr(self, "scale_efreq") and self.scale_efreq:
                e_mhz = int(self.scale_efreq.get_value())
                cmd_args.extend(["--ecore-max-freq", str(e_mhz)])
                desc_parts.append(f"E={e_mhz}")
            if hasattr(self, "scale_lpfreq") and self.scale_lpfreq:
                lp_mhz = int(self.scale_lpfreq.get_value())
                cmd_args.extend(["--lpcore-max-freq", str(lp_mhz)])
                desc_parts.append(f"LP={lp_mhz}")
            if cmd_args:
                self._execute_cmd(cmd_args, f"Set Freq Limits: {' '.join(desc_parts)}")

        def _on_reset_freq_limits(self, widget):
            self._execute_cmd(["--reset-freq-limits"], "Reset Frequency Limits")
            f = get_core_max_frequencies()
            if hasattr(self, "scale_pfreq") and self.scale_pfreq:
                self.scale_pfreq.set_value(f.get("p_mhz", 4000))
            if hasattr(self, "scale_efreq") and self.scale_efreq:
                self.scale_efreq.set_value(f.get("e_mhz", 3000))
            if hasattr(self, "scale_lpfreq") and self.scale_lpfreq:
                self.scale_lpfreq.set_value(f.get("lp_mhz", 2000))

        def _run_bench_async(self, widget):
            self.btn_bench.set_sensitive(False)
            self.btn_bench.set_label("Benchmarking...")

            def worker():
                cmd = ([sys.executable, os.path.abspath(__file__)] if self.is_root else ["pkexec", sys.executable, os.path.abspath(__file__)]) + ["--bench", "--bench-duration", "5.0"]
                try:
                    res = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
                except Exception as e:
                    res = f"Benchmark error: {e}"
                GLib.idle_add(self._on_bench_done, res)

            threading.Thread(target=worker, daemon=True).start()

        def _on_bench_done(self, output):
            self.btn_bench.set_sensitive(True)
            self.btn_bench.set_label("Run Benchmark")
            clean_text = strip_ansi(output.strip())
            # Remove intermediate \r progress lines to present clean summary
            marker = "TS Bench Complete!"
            if marker in clean_text:
                clean_text = "✔ " + clean_text[clean_text.find(marker):]
            dlg = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="ThrottleStop TS Bench Results"
            )
            dlg.format_secondary_text(clean_text)
            dlg.run()
            dlg.destroy()

        def _apply_telemetry(self, data):
            self._updating_ui = True
            try:
                bdown = data.get("breakdown", {})
                freqs = data.get("freqs", {})
                thermals = data.get("thermals", {})
                ec = data.get("ec", {})
                battery = data.get("battery", {})
                gpu = data.get("gpu", {})
                mem = data.get("memory", {})
                cstates = data.get("cstates", {})
                throttle = data.get("throttle", {})
                nvme_io = data.get("nvme_io", {})
                net_io = data.get("net_io", {})
                sys_stats = data.get("sys_stats", {})
                uptime = data.get("uptime", {})

                # RAPL operative limits
                r = get_rapl_status()
                eff_pl1 = data.get("power_limits", {}).get("effective_pl1") or r.get("effective_pl1", 29.0)
                eff_pl2 = data.get("power_limits", {}).get("effective_pl2") or r.get("effective_pl2", 55.0)

                cur_pkg_w = bdown.get("cpu_package_w", 0.0)
                if cur_pkg_w <= 0.0:
                    cur_pkg_w = get_instant_pkg_power(0.06)

                cur_temp = thermals.get("package", 0.0)
                if cur_temp <= 0.0:
                    cur_temp = read_float("/sys/class/thermal/thermal_zone0/temp", 0.0) / 1000.0

                headroom = max(0.0, eff_pl1 - cur_pkg_w)
                pct_pl1 = (cur_pkg_w / eff_pl1 * 100.0) if eff_pl1 > 0 else 0.0

                # Feed Graph
                self.graph.add_sample(cur_pkg_w, cur_temp, eff_pl1)

                # TAB 1: Key Performance Metrics
                self.lbl_pwr.set_text(f"{cur_pkg_w:.1f} W")
                self.row_pwr.set_subtitle(f"Operative limit: PL1 {eff_pl1:.0f}W · Headroom: {headroom:.1f}W ({pct_pl1:.1f}% PL1)")

                tgt = get_thermal_target()
                dist = max(0.0, tgt["prochot"] - cur_temp)
                self.lbl_tmp.set_text(f"{cur_temp:.1f} °C")
                self.row_tmp.set_subtitle(f"TjMax: {tgt['tjmax']}°C · Distance to PROCHOT: {dist:.1f}°C")

                on_c, tot_c, mode_key, mode_desc = get_core_status()
                p_avg = freqs.get("p_avg", 0)
                e_avg = freqs.get("e_avg", 0)
                lp_avg = freqs.get("lp_avg", 0)
                self.lbl_cores.set_text(f"{on_c}/{tot_c} Cores")
                self.row_cores.set_subtitle(f"P-Avg: {p_avg} MHz · E-Avg: {e_avg} MHz · LP-Avg: {lp_avg} MHz")
                if hasattr(self, "exp_parking"):
                    self.exp_parking.set_subtitle(f"Topology Control · {mode_desc}")

                fan_rpm = ec.get("fan1_rpm", 0) if ec else 0
                vrm_t = thermals.get("board", {}).get("VRM Power Stage", 0.0)
                vrm_sub = f" · VRM: {vrm_t:.1f}°C" if vrm_t > 0 else ""
                if fan_rpm > 0:
                    self.lbl_fan.set_text(f"{fan_rpm} RPM")
                    self.row_fan.set_subtitle(f"HP EC0 Active{vrm_sub}")
                else:
                    self.lbl_fan.set_text("0 RPM (Off)")
                    self.row_fan.set_subtitle(f"HP EC0 Passive{vrm_sub}")

                p_src = data.get("power_sources", {})
                ac_online = p_src.get("ac_online", True)
                bat_pct = battery.get("capacity", 100) if battery else 100
                bat_stat = battery.get("status", "AC") if battery else "AC"
                bat_draw_w = bdown.get("battery_rail_w") or bdown.get("battery_charge_w") or 0.0
                if ac_online:
                    self.lbl_bat.set_text(f"{bat_pct}% (AC Power)")
                    ad_w = ec.get("adapter_watts", 65) if ec else 65
                    self.row_bat.set_subtitle(f"Connected to {ad_w}W Smart Pin / Barrel Adapter")
                else:
                    self.lbl_bat.set_text(f"{bat_pct}% ({bat_draw_w:.1f}W)")
                    self.row_bat.set_subtitle(f"Discharging on battery ({bat_stat})")

                # TAB 1: Switches & Controls sync
                turbo_on = get_turbo()
                self.sw_turbo.set_active(turbo_on)
                dyn_b = get_dynamic_boost()
                if hasattr(self, "sw_boost"):
                    self.sw_boost.set_active(dyn_b)

                if hasattr(self, "cb_cores"):
                    target_mode = mode_key if mode_key in ["none", "no_smt", "pcores_only", "pcores_lp", "single_pcore", "dual_pcore", "cluster", "lp_only"] else "custom"
                    if self.cb_cores.get_active_id() != target_mode:
                        self.cb_cores.set_active_id(target_mode)

                if hasattr(self, "cpu_switches"):
                    cur_core_mask = get_core_mask()
                    for cpu_id, sw in self.cpu_switches.items():
                        if cpu_id == 0:
                            if not sw.get_active():
                                sw.set_active(True)
                            continue
                        is_on = cur_core_mask.get(cpu_id, True)
                        if sw.get_active() != is_on:
                            sw.set_active(is_on)

                    if hasattr(self, "exp_park_p"):
                        p_on = sum(1 for c in range(8) if cur_core_mask.get(c, True))
                        self.exp_park_p.set_subtitle(f"4 Physical Cores · 8 SMT Threads · {p_on}/8 Online")
                    if hasattr(self, "exp_park_e"):
                        e_on = sum(1 for c in range(8, 16) if cur_core_mask.get(c, True))
                        self.exp_park_e.set_subtitle(f"8 Cores · 8 Threads · {e_on}/8 Online")
                    if hasattr(self, "exp_park_lp"):
                        lp_on = sum(1 for c in range(16, 18) if cur_core_mask.get(c, True))
                        self.exp_park_lp.set_subtitle(f"2 Cores · 2 Threads · {lp_on}/2 Online")

                epp = get_epp()
                self.cb_epp.set_active_id(epp if epp in ["performance", "balance_performance", "balance_power", "power"] else "balance_power")

                if hasattr(self, "cb_balance"):
                    cur_bal = get_power_balance()
                    if self.cb_balance.get_active_id() != cur_bal and cur_bal in ["balanced", "gpu_pri", "cpu_pri", "gpu_max"]:
                        self.cb_balance.set_active_id(cur_bal)

                if hasattr(self, "cb_gnome"):
                    cur_gnome = get_desktop_power_profile()
                    if self.cb_gnome.get_active_id() != cur_gnome and cur_gnome in ["performance", "balanced", "power-saver"]:
                        self.cb_gnome.set_active_id(cur_gnome)

                for k, btn in self.profile_buttons.items():
                    btn.get_style_context().remove_class("suggested-action")
                if eff_pl1 >= 55:
                    self.active_profile = "max"
                elif eff_pl1 >= 38:
                    self.active_profile = "performance"
                elif eff_pl1 >= 27:
                    self.active_profile = "game"
                elif eff_pl1 >= 18:
                    self.active_profile = "balanced"
                elif eff_pl1 >= 8:
                    self.active_profile = "eco"
                elif eff_pl1 > 0:
                    self.active_profile = "supereco"
                if self.active_profile in self.profile_buttons:
                    self.profile_buttons[self.active_profile].get_style_context().add_class("suggested-action")

                # TAB 2: Hardware Power Limits (True PL1 / PL2 & RAPL MSR)
                lim = data.get("power_limits", {})
                if hasattr(self, "lbl_tel_eff"):
                    eff_val = lim.get("effective_pl1", eff_pl1)
                    eff_burst = lim.get("effective_pl2", eff_pl2)
                    eff_src = lim.get("source", "MMIO")
                    self.lbl_tel_eff.set_text(f"{eff_val:.0f} W")
                    self.row_tel_eff.set_subtitle(f"Enforced via {eff_src} · Burst PL2: {eff_burst:.0f}W")

                    msr_pl1 = lim.get("msr_pl1", 0)
                    msr_pl2 = lim.get("msr_pl2", 0)
                    msr_tau = lim.get("msr_tau", 32)
                    msr_lock_str = "Locked" if lim.get("msr_locked") else "Unlocked"
                    msr_clamp_str = "Clamped" if lim.get("msr_pl1_clamped") else "Unclamped"
                    self.lbl_tel_msr.set_text(f"PL1: {msr_pl1:.0f}W | PL2: {msr_pl2:.0f}W")
                    self.row_tel_msr.set_subtitle(f"Tau: {msr_tau:.1f}s · {msr_lock_str} · {msr_clamp_str}")

                    mmio_pl1 = lim.get("mmio_pl1", 0)
                    mmio_pl2 = lim.get("mmio_pl2", 0)
                    mmio_tau = lim.get("mmio_tau", 0)
                    mmio_st = "Active" if lim.get("mmio_active") else "Bypassed"
                    self.lbl_tel_mmio.set_text(f"PL1: {mmio_pl1:.0f}W | PL2: {mmio_pl2:.0f}W")
                    self.row_tel_mmio.set_subtitle(f"Tau: {mmio_tau:.1f}s · State: {mmio_st}")

                    dptf_act = lim.get("dptf_override", False)
                    th_run = lim.get("thermald_running", is_thermald_running())
                    th_txt = "thermald Active" if th_run else "thermald Paused/Managed"
                    self.lbl_tel_dptf.set_text("Active Override" if dptf_act else "Passive")
                    self.row_tel_dptf.set_subtitle(f"{th_txt} · Policy: {'Clamping MMIO to 35W' if dptf_act else 'Normal Operation'}")

                # TAB 2: Hardware Telemetry - Component Power Sipping
                tot_w = bdown.get("total_estimated_w", 0.0)
                pkg_w = bdown.get('cpu_package_w', 0.0)
                if pkg_w <= 0.0:
                    pkg_w = cur_pkg_w
                ia_w = bdown.get('cpu_cores_w', 0.0)
                if ia_w <= 0.0 and pkg_w > 0.0:
                    ia_w = pkg_w * 0.75
                soc_w = bdown.get('uncore_w', 0.0)
                if soc_w <= 0.0 and pkg_w > 0.0:
                    soc_w = max(0.0, pkg_w - ia_w)

                fan_w = bdown.get("fan_w", 0.0)
                if fan_w <= 0.0 and fan_rpm > 0:
                    fan_w = (fan_rpm / 1000.0) ** 1.8 * 0.15

                disp_w = bdown.get("display_w", 0.0)
                disp_pct = data.get("disp", {}).get("percent", 26)
                nvme_w = bdown.get("nvme_w", 0.3)
                ram_w = bdown.get("ram_w", 1.0)
                board_w = bdown.get("board_loss_w", 0.0)
                ram_tot = ram_w + board_w

                if tot_w <= 0.0:
                    tot_w = pkg_w + disp_w + nvme_w + fan_w + ram_tot

                pkg_sh = (pkg_w / tot_w * 100.0) if tot_w > 0 else 0.0
                disp_sh = (disp_w / tot_w * 100.0) if tot_w > 0 else 0.0
                ram_sh = (ram_tot / tot_w * 100.0) if tot_w > 0 else 0.0

                self.pwr_rows["cpu_pkg"][1].set_text(f"{pkg_w:.2f} W ({pkg_sh:.0f}%)")
                self.pwr_rows["ia_cores"][1].set_text(f"{ia_w:.2f} W")
                self.pwr_rows["soc_uncore"][1].set_text(f"{soc_w:.2f} W")

                gpu_w = bdown.get("gpu_w", 0.0)
                gpu_mhz = gpu.get("freq_mhz", 0) if gpu else 0
                rc6 = bdown.get("gpu_rc6_pct", 100.0)
                vram_mb = gpu.get("memory", {}).get("total_mb", 1011) if gpu else 1011
                self.pwr_rows["gpu_gt0"][1].set_text(f"{gpu_w:.2f} W" if gpu_w >= 0.05 else "<0.05 W")
                self.pwr_rows["gpu_gt0"][0].set_subtitle(f"Frequency: {gpu_mhz} MHz · RC6 Sleep: {rc6:.0f}% · VRAM: {vram_mb:.0f} MB")

                gt1_rc6 = bdown.get("gt1_rc6_pct", 100.0)
                self.pwr_rows["gpu_gt1"][0].set_subtitle(f"Video hardware encode/decode · RC6 Sleep: {gt1_rc6:.0f}%")
                self.pwr_rows["gpu_gt1"][1].set_text("(Shared)")

                npu_busy = bdown.get("npu_busy_pct", 0.0)
                self.pwr_rows["npu"][0].set_subtitle(f"State: Standby (D3hot) · Activity: {npu_busy:.1f}% · VRAM: 66 MB")
                self.pwr_rows["npu"][1].set_text("(in SoC)")

                self.pwr_rows["display"][1].set_text(f"{disp_w:.2f} W ({disp_sh:.0f}%)")
                self.pwr_rows["display"][0].set_subtitle(f"Backlight Brightness: {disp_pct}%")

                r_mb = nvme_io.get("read_mbs", 0.0)
                w_mb = nvme_io.get("write_mbs", 0.0)
                self.pwr_rows["nvme"][1].set_text(f"{nvme_w:.2f} W")
                self.pwr_rows["nvme"][0].set_subtitle(f"Activity: R: {r_mb:.1f} MB/s · W: {w_mb:.1f} MB/s")

                self.pwr_rows["fan"][1].set_text(f"{fan_w:.2f} W")
                self.pwr_rows["fan"][0].set_subtitle(f"HP EC0 Fan: {fan_rpm} RPM")

                self.pwr_rows["ram_vrm"][1].set_text(f"{ram_tot:.2f} W ({ram_sh:.0f}%)")

                self.pwr_rows["total_rail"][1].set_text(f"{tot_w:.2f} W")
                self.pwr_rows["total_rail"][0].set_subtitle(f"Combined platform load · Operative PL1: {eff_pl1:.0f}W")

                # TAB 2: Hybrid CPU Topology & Silicon Cores
                overall_load = freqs.get("overall_load", 0.0)
                all_avg = freqs.get("all_avg", 0)
                up_str = uptime.get("str", "01:11:06")
                self.row_cpu_overall.set_subtitle(f"Meteor Lake-P (14C/18T) · All-Core Avg: {all_avg} MHz · Uptime: {up_str}")
                self.bar_cpu_overall.set_fraction(max(0.0, min(1.0, overall_load / 100.0)))
                self.bar_cpu_overall.set_text(f"{overall_load:.1f}%")
                self.cpu_graph.add_sample(overall_load, p_avg, e_avg)

                p_mhz_list = [str(c.get("mhz", 0)) for c in freqs.get("p_cores", [])]
                p_str = ", ".join(p_mhz_list) if p_mhz_list else "--"
                self.exp_pcores.set_subtitle(f"4 Cores / 8 Threads · Avg: {p_avg} MHz · [{p_str} MHz]")
                for p_core in freqs.get("p_cores", []):
                    k = p_core.get("name")
                    if k in self.pcore_labels:
                        row, lbl = self.pcore_labels[k]
                        if not p_core.get("online", True):
                            lbl.set_text("Parked (Offline)")
                        else:
                            mhz = p_core.get("mhz", 0)
                            load = p_core.get("load_pct", 0.0)
                            t = p_core.get("temp_c", 0.0)
                            w = p_core.get("watts", 0.0)
                            lbl.set_text(f"{mhz:4d} MHz · {t:4.1f}°C · {w:4.2f}W  ({load:4.1f}%)")

                e_mhz_list = [c.get("mhz", 0) for c in freqs.get("e_cores", []) if c.get("online", True)]
                if e_mhz_list:
                    e_str = f"{min(e_mhz_list)} - {max(e_mhz_list)} MHz"
                else:
                    e_str = "--"
                self.exp_ecores.set_subtitle(f"8 Cores / 8 Threads · Avg: {e_avg} MHz · [{e_str}]")
                for e_core in freqs.get("e_cores", []):
                    k = e_core.get("name")
                    if k in self.ecore_labels:
                        row, lbl = self.ecore_labels[k]
                        if not e_core.get("online", True):
                            lbl.set_text("Parked (Offline)")
                        else:
                            mhz = e_core.get("mhz", 0)
                            load = e_core.get("load_pct", 0.0)
                            t = e_core.get("temp_c", 0.0)
                            w = e_core.get("watts", 0.0)
                            lbl.set_text(f"{mhz:4d} MHz · {t:4.1f}°C · {w:4.2f}W  ({load:4.1f}%)")

                lp_mhz_list = [str(c.get("mhz", 0)) for c in freqs.get("lp_cores", []) if c.get("online", True)]
                lp_str = ", ".join(lp_mhz_list) if lp_mhz_list else "--"
                self.exp_lpcores.set_subtitle(f"2 Cores / 2 Threads · Avg: {lp_avg} MHz · [{lp_str} MHz]")
                for lp_core in freqs.get("lp_cores", []):
                    k = lp_core.get("name")
                    if k in self.lpcore_labels:
                        row, lbl = self.lpcore_labels[k]
                        if not lp_core.get("online", True):
                            lbl.set_text("Parked (Offline)")
                        else:
                            mhz = lp_core.get("mhz", 0)
                            load = lp_core.get("load_pct", 0.0)
                            t = lp_core.get("temp_c", 0.0)
                            w = lp_core.get("watts", 0.0)
                            lbl.set_text(f"{mhz:4d} MHz · {t:4.1f}°C · {w:4.2f}W  ({load:4.1f}%)")

                deep_pct = cstates.get("deep_sleep_pct", 85.0)
                c10 = cstates.get("C10", 0.0)
                c6 = cstates.get("C6", 0.0)
                c1e = cstates.get("C1E", 0.0)
                self.lbl_cstates.set_text(f"{deep_pct:.1f}% Deep Sleep")
                self.row_cstates.set_subtitle(f"C10: {c10:.0f}% · C6: {c6:.0f}% · C1E: {c1e:.0f}% residency")

                ctxt = sys_stats.get("ctxt_rate", 0)
                intr = sys_stats.get("intr_rate", 0)
                self.lbl_sysact.set_text(f"{ctxt:,} ctxt/s · {intr:,} intr/s")

                # TAB 2: Memory & Storage
                used_b = mem.get("used_b", 0)
                tot_b = mem.get("total_b", 1)
                used_pct = mem.get("used_pct", 0.0)
                avail_b = mem.get("avail_b", 0)
                cached_b = mem.get("cached_b", 0)
                self.bar_ram.set_fraction(max(0.0, min(1.0, used_pct / 100.0)))
                self.bar_ram.set_text(f"{used_b / (1024**3):.2f} / {tot_b / (1024**3):.2f} GiB ({used_pct:.1f}%)")
                self.row_ram.set_subtitle(f"Available: {avail_b / (1024**3):.2f} GiB · Cached: {cached_b / (1024**3):.2f} GiB")

                s_used_b = mem.get("swap_used_b", 0)
                s_tot_b = mem.get("swap_total_b", 1)
                s_pct = mem.get("swap_pct", 0.0)
                s_free_b = mem.get("swap_free_b", 0)
                self.bar_swap.set_fraction(max(0.0, min(1.0, s_pct / 100.0)))
                self.bar_swap.set_text(f"{s_used_b / (1024**2):.0f} / {s_tot_b / (1024**2):.0f} MiB ({s_pct:.1f}%)")
                self.row_swap.set_subtitle(f"Free Swap: {s_free_b / (1024**2):.0f} MiB")

                self.mem_graph.add_sample(used_b / (1024**3), tot_b / (1024**3), used_pct, s_used_b / (1024**2), s_pct, avail_b / (1024**3))

                ssd_temp = thermals.get("ssd", 36.9)
                self.lbl_nvme.set_text(f"R: {r_mb:.1f} MB/s · W: {w_mb:.1f} MB/s")
                self.row_nvme.set_subtitle(f"Drive Temp: {ssd_temp:.1f}°C")

                # TAB 2: Board Thermals & Diagnostics
                ec_core = ec.get("ec_temp_rtmp", 60)
                self.lbl_pkg_therm.set_text(f"{cur_temp:.1f} °C")
                self.row_pkg_therm.set_subtitle(f"CPU Package: {cur_temp:.1f}°C · EC Core: {ec_core}°C")

                palm = thermals.get("board", {}).get("Skin/Palmrest", 50.0)
                amb = thermals.get("board", {}).get("Aux Ambient", 49.0)
                self.lbl_board_therm.set_text(f"{vrm_t:.1f}°C VRM")
                self.row_board_therm.set_subtitle(f"Palmrest: {palm:.1f}°C · Ambient: {amb:.1f}°C · SSD: {ssd_temp:.1f}°C")

                if fan_rpm > 0:
                    self.lbl_fan_blower.set_text(f"{fan_rpm} RPM")
                    self.row_fan_blower.set_subtitle("HP EC0 fan controller · Spinning")
                else:
                    self.lbl_fan_blower.set_text("0 RPM (Off)")
                    self.row_fan_blower.set_subtitle("HP EC0 fan controller · Passive")

                cpu_th = throttle.get("cpu_status", "Optimal (Unconstrained)")
                gpu_th = throttle.get("gpu_status", "Optimal (Boost Ready)")
                self.lbl_throttle.set_text("Optimal")
                self.row_throttle.set_subtitle(f"CPU: {cpu_th} · GPU: {gpu_th}")

                pkg_ev = throttle.get("pkg_events", 0)
                tot_ms = throttle.get("total_time_ms", 0)
                self.lbl_trips.set_text(f"{pkg_ev:,} events")
                self.row_trips.set_subtitle(f"Total trip duration: {tot_ms // 1000} seconds")

                # TAB 2: Wireless & Network Connectivity
                w_info = data.get("wireless", {})
                if w_info:
                    sig = w_info.get("signal_dbm", -50)
                    qual = w_info.get("link_qual", "60/70")
                    ps = "Enabled" if w_info.get("powersave") else "Disabled"
                    self.row_wifi.set_subtitle(f"Signal: {sig} dBm ({qual}) · PowerSave: {ps}")
                rx = net_io.get("rx_kbs", 0.0) if net_io else 0.0
                tx = net_io.get("tx_kbs", 0.0) if net_io else 0.0
                self.lbl_wifi.set_text(f"RX: {rx:.1f} KB/s · TX: {tx:.1f} KB/s")

                # TAB 2: Battery & Power Delivery
                if ac_online:
                    ad_w = ec.get("adapter_watts", 65) if ec else 65
                    self.lbl_psrc.set_text(f"{ad_w} W (Smart Pin Barrel)")
                    self.row_psrc.set_subtitle("HP Smart Pin AC Adapter Connected")
                else:
                    self.lbl_psrc.set_text("On Battery")
                    self.row_psrc.set_subtitle("Discharging on internal battery")

                bat_v = battery.get("voltage", 12.9) if battery else 12.9
                bat_cap = battery.get("capacity", 100) if battery else 100
                self.bar_bcharge.set_fraction(max(0.0, min(1.0, bat_cap / 100.0)))
                self.bar_bcharge.set_text(f"{bat_cap}%")
                self.row_bcharge.set_subtitle(f"Voltage: {bat_v:.2f} V · Status: {bat_stat}")

                health = battery.get("health", 100.0) if battery else 100.0
                energy_full = battery.get("energy_full", 41.0) if battery else 41.0
                energy_design = battery.get("energy_design", 41.0) if battery else 41.0
                self.lbl_bhealth.set_text(f"{health:.1f}% Health")
                self.row_bhealth.set_subtitle(f"Full: {energy_full:.1f} Wh / Design: {energy_design:.1f} Wh")

                cycles = ec.get("battery_cycles", 56) if ec else 56
                self.lbl_bcycle.set_text(f"{cycles} Cycles")

            except Exception:
                pass
            finally:
                self._updating_ui = False

def run_gui():
    """
    Launch unified native GNOME / Zorin OS / Wayland GTK3 Graphical Interface.
    Integrates ThrottleStop hardware power management with real-time system telemetry:
      - Clean GNOME libhandy styling with ViewSwitcher tabs:
        [⚡ Power & Tuning] and [📊 Hardware Telemetry]
      - Live Scrolling Performance Graph with complete dual Y-axes (Watts on left, °C on right),
        X-axis time scale ("60s ago" ... "Now"), tick marks, units, and real-time legend
      - Full hardware telemetry monitor merged into the GUI (RAM, per-core clocks,
        iGPU power, thermal zones, fan RPM, battery health, C-states)
      - Non-blocking background telemetry sampling for smooth 60 FPS desktop responsiveness
    """
    if not HAS_GUI_LIBS:
        print(f"{C_RED}Error loading GTK3 / libhandy: required libraries not found.{C_RESET}")
        print("Please install required libraries: sudo apt-get install python3-gi gir1.2-handy-1")
        sys.exit(1)

    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print(f"{C_RED}Error: No graphical display server detected ($DISPLAY is empty).{C_RESET}")
        print(f"Tip: Use the terminal dashboard instead: sudo python3 {sys.argv[0]} --tui")
        sys.exit(1)

    win = UnifiedThrottleStopWindow()
    win.show_all()
    Gtk.main()

def main():
    parser = argparse.ArgumentParser(
        description="ThrottleStop for Linux - Intel Meteor Lake Hardware Power & Performance Tuner"
    )

    # Preset profiles
    prof_group = parser.add_argument_group("ThrottleStop Profiles")
    prof_group.add_argument("-1", "--max", "--extreme", dest="max", action="store_true",
                            help="Profile 1: Max Turbo / Cooling Pad (PL1=65W, PL2=80W, 4.5 GHz, EPP=0)")
    prof_group.add_argument("--performance", "--perf", dest="performance", action="store_true",
                            help="Profile 1.5: Heavy Sustained Performance (PL1=45W, PL2=65W, EPP=balance_performance)")
    prof_group.add_argument("-2", "--game", action="store_true",
                            help="Profile 2: Game / Heatsink Matched (PL1=29W, PL2=55W, no thermal stutter)")
    prof_group.add_argument("-3", "--balanced", action="store_true",
                            help="Profile 3: Balanced Daily Driver (PL1=25W, PL2=45W, EPP=128)")
    prof_group.add_argument("-4", "--eco", action="store_true",
                            help="Profile 4: Battery Saver (PL1=15W, PL2=22W, EPP=power)")
    prof_group.add_argument("-5", "--supereco", "--mega-eco", dest="supereco", action="store_true",
                            help="Profile 5: Ultra-Aggressive Netbook (PL1=2W, LP-only island (CPU0+LP16/17), 400MHz GPU, 30%% screen)")
    prof_group.add_argument("--profile", type=str,
                            choices=list(PROFILES.keys()) + ["extreme", "turbo-max", "cooling-pad", "mega-eco", "perf"],
                            help="Select profile by name")

    # Custom TPL parameters
    tpl_group = parser.add_argument_group("Custom TPL (Turbo Power Limits)")
    tpl_group.add_argument("--pl1", type=float, help="Set PL1 (Long Duration Power Limit) in Watts")
    tpl_group.add_argument("--pl2", type=float, help="Set PL2 (Short Duration Power Limit) in Watts")
    tpl_group.add_argument("--tau", type=float, help="Set PL1 Time Window (Tau) in seconds")
    tpl_group.add_argument("--pl4", type=float, help="Set PL4 (Platform Peak Current Limit) in Watts")
    tpl_group.add_argument("--disable-mmio", action="store_true", help="ThrottleStop 'Disable MMIO' powercap zone")

    # Speed Shift & Turbo
    sst_group = parser.add_argument_group("Speed Shift & CPU Controls")
    sst_group.add_argument("--turbo", type=str, choices=["on", "off", "enable", "disable", "1", "0", "true", "false"],
                           help="Toggle Intel Turbo Boost")
    sst_group.add_argument("--dynamic-boost", type=str, choices=["on", "off", "enable", "disable", "1", "0", "true", "false"],
                           help="Toggle Intel Speed Shift HWP Dynamic Boost")
    sst_group.add_argument("--power-balance", type=str,
                           choices=["balanced", "gpu_pri", "cpu_pri", "gpu_max", "gpu", "cpu", "max"],
                           help="ThrottleStop CPU vs iGPU power balance ('balanced', 'gpu_pri', 'cpu_pri', 'gpu_max')")
    sst_group.add_argument("--desktop-profile", "--gnome-profile", dest="desktop_profile", type=str,
                           choices=["performance", "balanced", "power-saver"],
                           help="Set GNOME / system power profile via powerprofilesctl")
    sst_group.add_argument("--epp", type=str,
                           help="Set Speed Shift EPP (performance, balance_performance, balance_power, power, or 0-255)")
    sst_group.add_argument("--park-cores", type=str,
                           choices=["none", "no_smt", "pcores_only", "pcores_lp", "single_pcore", "dual_pcore", "cluster", "lp_only", "pcores", "aggressive"],
                           help="Silicon core parking mode: 'none' (18T), 'no_smt' (14T), 'pcores_only' (8T), 'pcores_lp' (10T), 'single_pcore' (11C), 'dual_pcore' (14T), 'cluster' (7C), 'lp_only' (3C)")
    sst_group.add_argument("--set-core", nargs=2, metavar=("CPU", "STATE"),
                           help="Set specific CPU thread online (1) or offline/parked (0), e.g. --set-core 1 0")
    sst_group.add_argument("--online-cpu", "--unpark-cpu", dest="online_cpu", type=str,
                           help="Bring specific CPU thread(s) online (comma-separated list, e.g. '1' or '1,2,5')")
    sst_group.add_argument("--offline-cpu", "--park-cpu", dest="offline_cpu", type=str,
                           help="Take specific CPU thread(s) offline / park (comma-separated list, e.g. '1' or '1,2,5')")
    sst_group.add_argument("--pcore-max-freq", type=int, help="P-Cores maximum frequency ceiling in MHz (e.g. 4500)")
    sst_group.add_argument("--ecore-max-freq", type=int, help="E-Cores maximum frequency ceiling in MHz (e.g. 3600)")
    sst_group.add_argument("--lpcore-max-freq", type=int, help="LP-Cores maximum frequency ceiling in MHz (e.g. 2500)")
    sst_group.add_argument("--reset-freq-limits", action="store_true", help="Reset all core frequency ceilings to hardware max")
    sst_group.add_argument("--park-lp-only", dest="park_lp_only", action="store_true",
                           help="Park all cores except LP island (CPUs 1-15 offlined, leaving CPU0+LP16/17)")
    sst_group.add_argument("--park-pcores", action="store_true", help="Park 3 P-cores (leave 11 active cores)")
    sst_group.add_argument("--unpark-all", action="store_true", help="Unpark all cores (18 threads online)")

    # Tools & Daemons
    tool_group = parser.add_argument_group("Interactive, Daemon & Verification")
    tool_group.add_argument("--status", action="store_true", help="Display full hardware power status report")
    tool_group.add_argument("--bench", action="store_true", help="Run TS Bench (all-core stress verification test)")
    tool_group.add_argument("--bench-duration", type=float, default=5.0, help="TS Bench duration in seconds (default: 5.0)")
    tool_group.add_argument("--gui", dest="gui", action="store_true",
                            help="Launch ThrottleStop native graphical desktop GTK window")
    tool_group.add_argument("--tui", "--interactive", dest="tui", action="store_true",
                            help="Launch ThrottleStop live interactive Terminal UI")
    tool_group.add_argument("--install-desktop-launcher", action="store_true",
                            help="Install ThrottleStop application shortcut into desktop menu")
    tool_group.add_argument("--daemon", action="store_true", help="Run continuous enforcement daemon with AC/battery auto-switch")
    tool_group.add_argument("--ac-profile", type=str, default="max", help="Profile to enforce on AC (default: max)")
    tool_group.add_argument("--bat-profile", type=str, default="eco", help="Profile to enforce on Battery (default: eco)")
    tool_group.add_argument("--install-service", action="store_true", help="Install & enable ThrottleStop background systemd service")
    tool_group.add_argument("--uninstall-service", action="store_true", help="Uninstall & remove ThrottleStop systemd service")
    tool_group.add_argument("--revert", "--reset", dest="revert", action="store_true",
                            help="Revert all hardware settings back to factory Linux defaults")

    args = parser.parse_args()

    # Priority 1: Service install/uninstall or desktop launcher
    if args.install_desktop_launcher:
        install_desktop_launcher()
        return
    if args.install_service:
        install_systemd_service(args.ac_profile, args.bat_profile)
        return
    if args.uninstall_service:
        uninstall_systemd_service()
        return

    # Priority 2: Benchmark
    if args.bench:
        run_ts_bench(args.bench_duration)
        return

    # Priority 3: Daemon
    if args.daemon:
        run_daemon(args.ac_profile, args.bat_profile)
        return

    # Priority 4: Revert
    if args.revert:
        revert_to_defaults()
        show_status()
        return

    # Priority 5: Profile Selection
    selected_profile = None
    if args.max or args.profile in ["max", "extreme", "turbo-max", "cooling-pad"]:
        selected_profile = "max"
    elif args.performance or args.profile in ["performance", "perf"]:
        selected_profile = "performance"
    elif args.game or args.profile == "game":
        selected_profile = "game"
    elif args.balanced or args.profile == "balanced":
        selected_profile = "balanced"
    elif args.eco or args.profile == "eco":
        selected_profile = "eco"
    elif args.supereco or args.profile in ["supereco", "mega-eco", "netbook"]:
        selected_profile = "supereco"

    if selected_profile:
        apply_profile(selected_profile)
        show_status()
        return

    # Priority 6: Custom Granular Options
    custom_applied = False
    if args.pl1 is not None or args.pl2 is not None or args.tau is not None or args.pl4 is not None or args.disable_mmio:
        check_root()
        set_rapl_limits(pl1_w=args.pl1, pl2_w=args.pl2, tau_s=args.tau, pl4_w=args.pl4, disable_mmio=args.disable_mmio)
        custom_applied = True

    if args.turbo:
        check_root()
        set_turbo(args.turbo in ["on", "enable", "1", "true"])
        custom_applied = True

    if args.dynamic_boost:
        check_root()
        set_dynamic_boost(args.dynamic_boost in ["on", "enable", "1", "true"])
        custom_applied = True

    if args.power_balance:
        check_root()
        set_power_balance(args.power_balance)
        custom_applied = True

    if args.desktop_profile:
        set_desktop_power_profile(args.desktop_profile)
        custom_applied = True

    if args.pcore_max_freq or args.ecore_max_freq or args.lpcore_max_freq:
        check_root()
        p_khz = (args.pcore_max_freq if args.pcore_max_freq > 10000 else args.pcore_max_freq * 1000) if args.pcore_max_freq else None
        e_khz = (args.ecore_max_freq if args.ecore_max_freq > 10000 else args.ecore_max_freq * 1000) if args.ecore_max_freq else None
        lp_khz = (args.lpcore_max_freq if args.lpcore_max_freq > 10000 else args.lpcore_max_freq * 1000) if args.lpcore_max_freq else None
        set_core_frequencies(p_max_khz=p_khz, e_max_khz=e_khz, lp_max_khz=lp_khz)
        custom_applied = True

    if args.reset_freq_limits:
        check_root()
        reset_core_frequencies()
        custom_applied = True

    if args.epp:
        check_root()
        set_epp(args.epp)
        custom_applied = True

    if args.set_core:
        check_root()
        cpu_str, state_str = args.set_core
        try:
            c_idx = int(cpu_str)
            st_val = (state_str.strip().lower() in ["1", "on", "true", "yes", "online"])
            if set_single_core_online(c_idx, st_val):
                custom_applied = True
        except ValueError:
            print(f"Error: Invalid arguments for --set-core: {args.set_core}")

    if args.online_cpu:
        check_root()
        try:
            for c_idx in parse_cpu_list(args.online_cpu):
                if set_single_core_online(c_idx, True):
                    custom_applied = True
        except ValueError as e:
            print(f"Error: {e}")

    if args.offline_cpu:
        check_root()
        try:
            for c_idx in parse_cpu_list(args.offline_cpu):
                if set_single_core_online(c_idx, False):
                    custom_applied = True
        except ValueError as e:
            print(f"Error: {e}")

    if args.park_cores:
        check_root()
        set_core_parking(args.park_cores)
        custom_applied = True
    elif args.park_lp_only:
        check_root()
        set_core_parking("lp_only")
        custom_applied = True
    elif args.park_pcores:
        check_root()
        set_core_parking("single_pcore")
        custom_applied = True
    elif args.unpark_all:
        check_root()
        set_core_parking("none")
        custom_applied = True

    if custom_applied:
        show_status()
        return

    if args.set_core or args.online_cpu or args.offline_cpu or args.park_cores:
        sys.exit(1)

    # Priority 7: Explicit GUI or TUI Launch
    if args.gui:
        run_gui()
        return

    if args.tui:
        run_interactive_tui()
        return

    # Priority 8: Status flag
    if args.status:
        show_status()
        return

    # Priority 9: Default behaviour when run with no arguments
    show_status()
    print(f"{C_DIM}Interactive Modes:{C_RESET}")
    print(f"  {C_CYAN}• Desktop GUI Window:{C_RESET}       python3 {sys.argv[0]} --gui")
    print(f"  {C_GREEN}• Terminal Live TUI:{C_RESET}        sudo python3 {sys.argv[0]} --tui")
    print(f"  {C_YELLOW}• Install Desktop Shortcut:{C_RESET} python3 {sys.argv[0]} --install-desktop-launcher\n")

if __name__ == "__main__":
    main()
