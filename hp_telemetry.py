#!/usr/bin/env python3
"""
HP Hardware Telemetry & Component Power Dashboard
Platform: HP Laptop 15-fd1xxx (Intel Core Ultra 5 125H Meteor Lake-P)

Fixed UI:
- dynamic terminal width
- ANSI-aware padding/truncation
- resize-safe live mode
- compact fallback for narrow terminals
"""

import sys
import os
import time
import glob
import json
import argparse
import subprocess
import shutil
import re
import struct
import ctypes

try:
    from hardware import get_platform
except ImportError:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)
    from hardware import get_platform

EC_IO_PATH = "/sys/kernel/debug/ec/ec0/io"

def rdmsr(cpu, msr):
    """Read 64-bit MSR via /dev/cpu/<cpu>/msr."""
    try:
        p = f"/dev/cpu/{cpu}/msr"
        if not os.path.exists(p):
            subprocess.run(["modprobe", "msr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(p, "rb") as f:
            f.seek(msr)
            return struct.unpack("Q", f.read(8))[0]
    except Exception:
        return None

# ANSI colors
C_RESET   = "\033[0m"
C_BOLD    = "\033[1m"
C_DIM     = "\033[2m"
C_RED     = "\033[31m"
C_GREEN   = "\033[32m"
C_YELLOW  = "\033[33m"
C_BLUE    = "\033[34m"
C_MAGENTA = "\033[35m"
C_CYAN    = "\033[36m"
C_WHITE   = "\033[37m"
C_ORANGE  = "\033[38;5;208m"

ANSI_RE = re.compile("\x1b\\[[0-9;]*m")


def strip_ansi(s):
    return ANSI_RE.sub("", str(s))


def vlen(s):
    return len(strip_ansi(s))


def truncate_visible(s, width):
    """Truncate by visible width, preserving ANSI escapes as much as possible."""
    s = str(s)
    if width <= 0:
        return ""
    if vlen(s) <= width:
        return s

    out = []
    visible = 0
    i = 0

    while i < len(s) and visible < width:
        ch = s[i]

        if ch == "\x1b":
            j = i + 1
            while j < len(s) and s[j] != "m":
                j += 1
            if j < len(s):
                out.append(s[i:j + 1])
                i = j + 1
            else:
                break
            continue

        out.append(ch)
        visible += 1
        i += 1

    return "".join(out) + C_RESET


def pad(s, width, align="left"):
    """ANSI-aware pad/truncate."""
    if width <= 0:
        return ""

    s = truncate_visible(s, width)
    gap = max(0, width - vlen(s))

    if align == "center":
        left = gap // 2
        right = gap - left
    elif align == "right":
        left = gap
        right = 0
    else:
        left = 0
        right = gap

    return " " * left + s + " " * right


def term_columns():
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80


def color_temp(temp):
    if temp is None:
        return f"{C_DIM}N/A{C_RESET}"

    try:
        temp = float(temp)
    except Exception:
        return f"{C_DIM}N/A{C_RESET}"

    if temp < 55:
        return f"{C_GREEN}{temp:4.1f}°C{C_RESET}"
    elif temp < 75:
        return f"{C_YELLOW}{temp:4.1f}°C{C_RESET}"
    elif temp < 90:
        return f"{C_ORANGE}{temp:4.1f}°C{C_RESET}"
    else:
        return f"{C_RED}{C_BOLD}{temp:4.1f}°C{C_RESET}"


def color_watts(w, max_ref=35.0):
    if w is None:
        return f"{C_DIM}N/A{C_RESET}"

    try:
        w = float(w)
    except Exception:
        return f"{C_DIM}N/A{C_RESET}"

    if w < 10:
        return f"{C_GREEN}{w:5.2f} W{C_RESET}"
    elif w < 25:
        return f"{C_YELLOW}{w:5.2f} W{C_RESET}"
    elif w < 45:
        return f"{C_ORANGE}{w:5.2f} W{C_RESET}"
    else:
        return f"{C_RED}{C_BOLD}{w:5.2f} W{C_RESET}"


def color_rpm(rpm):
    try:
        rpm = int(rpm)
    except Exception:
        return f"{C_DIM}N/A{C_RESET}"

    if rpm == 0:
        return f"{C_DIM}0 RPM (Idle/Off){C_RESET}"
    elif rpm < 2500:
        return f"{C_CYAN}{rpm} RPM (Quiet){C_RESET}"
    elif rpm < 4000:
        return f"{C_YELLOW}{rpm} RPM (Active){C_RESET}"
    else:
        return f"{C_RED}{C_BOLD}{rpm} RPM (High){C_RESET}"


def bar(pct, width=16, fill_color=C_CYAN):
    try:
        pct = float(pct)
    except Exception:
        pct = 0.0

    width = max(0, int(width))
    if width == 0:
        return ""

    pct = max(0.0, min(100.0, pct))
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    empty = width - filled

    return f"{fill_color}{'█' * filled}{C_DIM}{'░' * empty}{C_RESET}"


BOX_DASH = "─"


def box_top(width, title=""):
    width = max(2, int(width))
    if title:
        t = f"╭─ {title} "
        fill = BOX_DASH * max(0, width - vlen(t) - 1)
        return f"{C_BOLD}{C_CYAN}{t}{fill}╮{C_RESET}"
    return f"{C_BOLD}{C_CYAN}╭{BOX_DASH * (width - 2)}╮{C_RESET}"


def box_mid(width, title=""):
    width = max(2, int(width))
    if title:
        t = f"├─ {title} "
        fill = BOX_DASH * max(0, width - vlen(t) - 1)
        return f"{C_BOLD}{C_CYAN}{t}{fill}┤{C_RESET}"
    return f"{C_BOLD}{C_CYAN}├{BOX_DASH * (width - 2)}┤{C_RESET}"


def box_bottom(width):
    width = max(2, int(width))
    return f"{C_BOLD}{C_CYAN}╰{BOX_DASH * (width - 2)}╯{C_RESET}"


def box_line(content, width):
    inner = max(1, int(width) - 2)
    return f"{C_CYAN}│{C_RESET}" + pad(content, inner) + f"{C_CYAN}│{C_RESET}"


def format_bytes(b):
    try:
        b = float(b)
    except Exception:
        return "0 B"
    if b < 1024:
        return f"{b:.0f} B"
    elif b < 1024 ** 2:
        return f"{b / 1024:.1f} KiB"
    elif b < 1024 ** 3:
        return f"{b / (1024 ** 2):.1f} MiB"
    else:
        return f"{b / (1024 ** 3):.2f} GiB"


def split_line(left, right, width, gap=1):
    """Left/right aligned line with ANSI-aware truncation."""
    if width <= 0:
        return ""

    right = truncate_visible(right, width)
    right_len = vlen(right)

    if right_len + gap > width:
        return truncate_visible(right, width)

    max_left = max(0, width - right_len - gap)
    left = truncate_visible(left, max_left)
    left_len = vlen(left)

    spaces = max(gap, width - left_len - right_len)
    return left + " " * spaces + right


def fit_columns(desired, minimum, available):
    """Shrink column widths to fit available visible width."""
    n = len(desired)
    if available <= 0:
        return [0] * n

    minimum = [max(0, int(m)) for m in minimum]
    desired = [max(minimum[i], int(desired[i])) for i in range(n)]

    if sum(minimum) > available:
        widths = minimum[:]
        total = sum(widths)
        for i in range(n - 1, -1, -1):
            if total <= available:
                break
            cut = min(widths[i], total - available)
            widths[i] -= cut
            total -= cut
        return widths

    widths = desired[:]
    while sum(widths) > available:
        candidates = [i for i in range(n) if widths[i] > minimum[i]]
        if not candidates:
            break
        idx = max(candidates, key=lambda i: widths[i] - minimum[i])
        widths[idx] -= 1

    return widths


def make_row(cols, widths, aligns=None):
    if aligns is None:
        aligns = ["left"] * len(cols)

    parts = []
    for c, wd, al in zip(cols, widths, aligns):
        if wd <= 0:
            continue
        parts.append(pad(c, wd, al))

    if not parts:
        return ""

    return " " + " ".join(parts)


def make_sep(widths):
    parts = ["─" * wd for wd in widths if wd > 0]
    if not parts:
        return ""
    return " " + " ".join(parts)


class TelemetryCollector:
    def __init__(self):
        self._ensure_ec()
        self.gpu_card = self._find_gpu_card()
        self.last_sample_time = None
        self.last_rapl_energy = {}
        self.last_rc6_ms = 0
        self.last_gt1_rc6_ms = 0
        self.last_nvme_stat = (0, 0)
        self.last_npu_busy_us = None
        self.last_cstates = {}
        self.last_net_bytes = (0, 0)
        self.last_stat_ctxt = None
        self.last_stat_intr = None
        self.last_pmc_pkg_cstates = {}

        # GPU energy via perf_event_open
        self.gpu_perf_fd = -1
        self.gpu_perf_scale = 2.3283064365386962890625e-10
        self.last_gpu_energy_val = None
        self._init_gpu_perf()

        # Per-core load tracking
        self.last_cpu_stats = {}
        self.current_cpu_loads = {}
        self.current_overall_cpu_load = 0.0

        # Throttle tracking
        self.last_pkg_throttle = None
        self.current_throttle_delta = 0

        # Warm up first differential reading
        self._sample_deltas()

    def _init_gpu_perf(self):
        try:
            SYS_perf_event_open = 298
            class PerfEventAttr(ctypes.Structure):
                _fields_ = [
                    ("type", ctypes.c_uint32),
                    ("size", ctypes.c_uint32),
                    ("config", ctypes.c_uint64),
                    ("sample_period", ctypes.c_uint64),
                    ("sample_type", ctypes.c_uint64),
                    ("read_format", ctypes.c_uint64),
                    ("flags", ctypes.c_uint64),
                    ("wakeup_events", ctypes.c_uint32),
                    ("bp_type", ctypes.c_uint32),
                    ("bp_addr", ctypes.c_uint64),
                    ("bp_len", ctypes.c_uint64),
                    ("branch_sample_type", ctypes.c_uint64),
                    ("sample_regs_user", ctypes.c_uint64),
                    ("sample_stack_user", ctypes.c_uint32),
                    ("clockid", ctypes.c_int32),
                    ("sample_regs_intr", ctypes.c_uint64),
                    ("aux_watermark", ctypes.c_uint32),
                    ("sample_max_stack", ctypes.c_uint16),
                    ("reserved", ctypes.c_uint16),
                ]

            pmu_type = 34
            if os.path.exists("/sys/bus/event_source/devices/power/type"):
                with open("/sys/bus/event_source/devices/power/type") as f:
                    pmu_type = int(f.read().strip())

            config = 0x04
            ev_path = "/sys/bus/event_source/devices/power/events/energy-gpu"
            if os.path.exists(ev_path):
                with open(ev_path) as f:
                    content = f.read().strip()
                    if "event=" in content:
                        config = int(content.split("=")[1], 16)

            scale_path = "/sys/bus/event_source/devices/power/events/energy-gpu.scale"
            if os.path.exists(scale_path):
                with open(scale_path) as f:
                    self.gpu_perf_scale = float(f.read().strip())

            libc = ctypes.CDLL(None)
            attr = PerfEventAttr()
            attr.type = pmu_type
            attr.size = ctypes.sizeof(PerfEventAttr)
            attr.config = config
            self.gpu_perf_fd = libc.syscall(SYS_perf_event_open, ctypes.byref(attr), -1, 0, -1, 0)
        except Exception:
            self.gpu_perf_fd = -1

    def _ensure_ec(self):
        if not os.path.exists(EC_IO_PATH):
            try:
                subprocess.run(
                    ["modprobe", "ec_sys"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except Exception:
                pass

    def _find_gpu_card(self):
        for path in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
            if os.path.exists(os.path.join(path, "gt_act_freq_mhz")):
                return os.path.basename(path)

        for cand in ("card1", "card0"):
            if os.path.exists(f"/sys/class/drm/{cand}/gt_act_freq_mhz"):
                return cand

        return None

    def _gpu_path(self, rel):
        if not self.gpu_card:
            return None
        return f"/sys/class/drm/{self.gpu_card}/{rel}"

    def _read_nvme_sectors(self):
        for stat in sorted(glob.glob("/sys/block/nvme*n1/stat")):
            try:
                with open(stat) as f:
                    parts = f.read().split()
                if len(parts) > 6:
                    return int(parts[2]), int(parts[6])
            except Exception:
                pass
        return (0, 0)

    def _read_rc6(self):
        paths = []

        if self.gpu_card:
            paths.append(f"/sys/class/drm/{self.gpu_card}/gt/gt0/rc6_residency_ms")
            paths.append(f"/sys/class/drm/{self.gpu_card}/gt/rc6_residency_ms")

        paths.extend(sorted(glob.glob("/sys/class/drm/card*/gt/gt*/rc6_residency_ms")))

        seen = set()
        for p in paths:
            if p in seen:
                continue
            seen.add(p)
            try:
                with open(p) as f:
                    return int(f.read().strip())
            except Exception:
                pass

        return 0

    def _read_rapl_raw(self):
        domains = {
            "package": "/sys/class/powercap/intel-rapl:0/energy_uj",
            "core": "/sys/class/powercap/intel-rapl:0:0/energy_uj",
            "uncore": "/sys/class/powercap/intel-rapl:0:1/energy_uj",
        }

        res = {}
        for name, path in domains.items():
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        res[name] = int(f.read().strip())
                except Exception:
                    pass

        return res

    def _sample_deltas(self):
        now = time.time()
        rapl_now = self._read_rapl_raw()
        rc6_now = self._read_rc6()
        nvme_now = self._read_nvme_sectors()

        # GT1 media rc6
        gt1_rc6_now = 0
        gt1_rc6_p = "/sys/class/drm/card1/gt/gt1/rc6_residency_ms"
        if os.path.exists(gt1_rc6_p):
            try:
                with open(gt1_rc6_p) as f:
                    gt1_rc6_now = int(f.read().strip())
            except Exception:
                pass

        # NPU busy
        npu_busy_now = 0
        npu_busy_p = "/sys/class/accel/accel0/device/npu_busy_time_us"
        if os.path.exists(npu_busy_p):
            try:
                with open(npu_busy_p) as f:
                    npu_busy_now = int(f.read().strip())
            except Exception:
                pass

        # CPU Core C-States
        cstates_now = {}
        cpu_count = 0
        for cpu_dir in glob.glob("/sys/devices/system/cpu/cpu[0-9]*"):
            cpu_count += 1
            for st_dir in glob.glob(os.path.join(cpu_dir, "cpuidle/state*")):
                try:
                    with open(os.path.join(st_dir, "name")) as f1, open(os.path.join(st_dir, "time")) as f2:
                        name = f1.read().strip()
                        cstates_now[name] = cstates_now.get(name, 0) + int(f2.read().strip())
                except Exception:
                    pass

        # Net IO
        net_now = (0, 0)
        wlo1_rx = "/sys/class/net/wlo1/statistics/rx_bytes"
        wlo1_tx = "/sys/class/net/wlo1/statistics/tx_bytes"
        if os.path.exists(wlo1_rx) and os.path.exists(wlo1_tx):
            try:
                with open(wlo1_rx) as f1, open(wlo1_tx) as f2:
                    net_now = (int(f1.read().strip()), int(f2.read().strip()))
            except Exception:
                pass

        # PMC Core Package C-States (Meteor Lake)
        pmc_pkg_now = {}
        pmc_path = "/sys/kernel/debug/pmc_core/package_cstate_show"
        if os.path.exists(pmc_path):
            try:
                with open(pmc_path) as f:
                    for line in f:
                        if ":" in line:
                            k, v = line.split(":", 1)
                            pmc_pkg_now[k.strip()] = int(v.strip())
            except Exception:
                pass

        # System Context Switches & Interrupts (/proc/stat)
        stat_ctxt = 0
        stat_intr = 0
        if os.path.exists("/proc/stat"):
            try:
                with open("/proc/stat") as f:
                    for line in f:
                        if line.startswith("ctxt "):
                            stat_ctxt = int(line.split()[1])
                        elif line.startswith("intr "):
                            stat_intr = int(line.split()[1])
            except Exception:
                pass

        power = {}
        rc6_pct = 100.0
        gt1_rc6_pct = 100.0
        npu_busy_pct = 0.0
        cstate_pcts = {"C10": 0.0, "C6": 0.0, "C1E": 0.0, "POLL": 0.0, "deep_sleep": 0.0}
        net_io = {
            "rx_kbs": 0.0,
            "tx_kbs": 0.0,
            "rx_kbps": 0.0,
            "tx_kbps": 0.0,
            "rx_bytes_delta": 0,
            "tx_bytes_delta": 0,
        }
        nvme_io = {"read_mbs": 0.0, "write_mbs": 0.0}
        sys_stats = {
            "ctxt_per_sec": 0.0,
            "intr_per_sec": 0.0,
            "ctxt_total": stat_ctxt,
            "intr_total": stat_intr,
        }
        pmc_data = {
            "raw": pmc_pkg_now,
            "pcts": {},
            "c8_pct": 0.0,
            "c10_pct": 0.0,
            "c2_pct": 0.0,
            "c8_s": (pmc_pkg_now.get("Package C8", 0) or 0) / 1e6,
            "c10_s": (pmc_pkg_now.get("Package C10", 0) or 0) / 1e6,
            "c8_raw_us": pmc_pkg_now.get("Package C8", 0) or 0,
            "c10_raw_us": pmc_pkg_now.get("Package C10", 0) or 0,
        }

        # Read GPU energy raw counter
        gpu_raw_val = None
        if self.gpu_perf_fd is not None and self.gpu_perf_fd >= 0:
            try:
                raw_bytes = os.read(self.gpu_perf_fd, 8)
                if len(raw_bytes) == 8:
                    gpu_raw_val = struct.unpack("<Q", raw_bytes)[0]
            except Exception:
                pass

        # Per-core CPU loads from /proc/stat
        cpu_stats_now = {}
        if os.path.exists("/proc/stat"):
            try:
                with open("/proc/stat") as f:
                    for line in f:
                        if line.startswith("cpu") and line[3:4].isdigit():
                            parts = line.split()
                            c_id = int(parts[0][3:])
                            times = [int(x) for x in parts[1:]]
                            idle = times[3] + (times[4] if len(times) > 4 else 0)
                            total = sum(times)
                            cpu_stats_now[c_id] = (idle, total)
            except Exception:
                pass

        # CPU Throttling raw event count
        pkg_th_events = 0
        th_p = "/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count"
        if os.path.exists(th_p):
            try:
                with open(th_p) as f:
                    pkg_th_events = int(f.read().strip())
            except Exception:
                pass

        if self.last_sample_time is not None:
            dt = now - self.last_sample_time
            if dt > 0.05:
                # RAPL wattage
                for name, e2 in rapl_now.items():
                    e1 = self.last_rapl_energy.get(name)
                    if e1 is not None and e2 >= e1:
                        power[name] = (e2 - e1) / dt / 1e6

                # GPU wattage via perf
                gpu_w = 0.0
                if gpu_raw_val is not None and self.last_gpu_energy_val is not None:
                    d_joules = (gpu_raw_val - self.last_gpu_energy_val) * self.gpu_perf_scale
                    if d_joules >= 0:
                        gpu_w = d_joules / dt
                power["gpu"] = gpu_w

                # Per-core CPU loads
                if self.last_cpu_stats and cpu_stats_now:
                    tot_d_idle = 0
                    tot_d_total = 0
                    for cid, (n_idle, n_total) in cpu_stats_now.items():
                        p_idle, p_total = self.last_cpu_stats.get(cid, (n_idle, n_total))
                        d_idle = max(0, n_idle - p_idle)
                        d_total = max(0, n_total - p_total)
                        load = (100.0 * (1.0 - d_idle / d_total)) if d_total > 0 else 0.0
                        self.current_cpu_loads[cid] = max(0.0, min(100.0, load))
                        tot_d_idle += d_idle
                        tot_d_total += d_total
                    if tot_d_total > 0:
                        self.current_overall_cpu_load = max(0.0, min(100.0, 100.0 * (1.0 - tot_d_idle / tot_d_total)))

                # CPU Throttling delta
                if self.last_pkg_throttle is not None:
                    self.current_throttle_delta = max(0, pkg_th_events - self.last_pkg_throttle)

                # RC6 sleep %
                dt_ms = dt * 1000.0
                d_rc6 = rc6_now - self.last_rc6_ms
                rc6_pct = min(100.0, max(0.0, (d_rc6 / dt_ms) * 100.0))

                if self.last_gt1_rc6_ms > 0:
                    d_gt1 = gt1_rc6_now - self.last_gt1_rc6_ms
                    gt1_rc6_pct = min(100.0, max(0.0, (d_gt1 / dt_ms) * 100.0))

                # NPU busy %
                if self.last_npu_busy_us is not None:
                    d_npu = max(0, npu_busy_now - self.last_npu_busy_us)
                    npu_busy_pct = min(100.0, max(0.0, (d_npu / (dt * 1e6)) * 100.0))

                # C-State %
                if self.last_cstates and cpu_count > 0:
                    win_us = dt * cpu_count * 1e6
                    for cname, cur_t in cstates_now.items():
                        prev_t = self.last_cstates.get(cname, 0)
                        d_us = max(0, cur_t - prev_t)
                        cstate_pcts[cname] = min(100.0, max(0.0, (d_us / win_us) * 100.0))
                    cstate_pcts["deep_sleep"] = min(
                        100.0,
                        cstate_pcts.get("C10", 0.0) + cstate_pcts.get("C6", 0.0)
                    )

                # Net IO (throughput kbps & kbs)
                if self.last_net_bytes != (0, 0):
                    drx = max(0, net_now[0] - self.last_net_bytes[0])
                    dtx = max(0, net_now[1] - self.last_net_bytes[1])
                    net_io["rx_bytes_delta"] = drx
                    net_io["tx_bytes_delta"] = dtx
                    net_io["rx_kbps"] = (drx * 8.0) / (dt * 1000.0)
                    net_io["tx_kbps"] = (dtx * 8.0) / (dt * 1000.0)
                    net_io["rx_kbs"] = (drx / dt) / 1024.0
                    net_io["tx_kbs"] = (dtx / dt) / 1024.0

                # System context switches & interrupts per second
                if self.last_stat_ctxt is not None:
                    d_ctxt = max(0, stat_ctxt - self.last_stat_ctxt)
                    sys_stats["ctxt_per_sec"] = d_ctxt / dt
                if self.last_stat_intr is not None:
                    d_intr = max(0, stat_intr - self.last_stat_intr)
                    sys_stats["intr_per_sec"] = d_intr / dt

                # PMC package c-state residency % (usec counters)
                if self.last_pmc_pkg_cstates:
                    win_us = dt * 1e6
                    for cname, cur_us in pmc_pkg_now.items():
                        prev_us = self.last_pmc_pkg_cstates.get(cname, 0)
                        d_us = max(0, cur_us - prev_us)
                        pct = min(100.0, max(0.0, (d_us / win_us) * 100.0))
                        pmc_data["pcts"][cname] = pct
                    pmc_data["c8_pct"] = pmc_data["pcts"].get("Package C8", 0.0)
                    pmc_data["c10_pct"] = pmc_data["pcts"].get("Package C10", 0.0)
                    pmc_data["c2_pct"] = pmc_data["pcts"].get("Package C2", 0.0)

                # NVMe throughput
                dr = nvme_now[0] - self.last_nvme_stat[0]
                dw = nvme_now[1] - self.last_nvme_stat[1]
                nvme_io["read_mbs"] = (dr * 512) / 1024 / 1024 / dt
                nvme_io["write_mbs"] = (dw * 512) / 1024 / 1024 / dt

        self.last_gpu_energy_val = gpu_raw_val
        self.last_cpu_stats = cpu_stats_now
        self.last_pkg_throttle = pkg_th_events

        self.last_sample_time = now
        self.last_rapl_energy = rapl_now
        self.last_rc6_ms = rc6_now
        self.last_gt1_rc6_ms = gt1_rc6_now
        self.last_nvme_stat = nvme_now
        self.last_npu_busy_us = npu_busy_now
        self.last_cstates = cstates_now
        self.last_net_bytes = net_now
        self.last_stat_ctxt = stat_ctxt
        self.last_stat_intr = stat_intr
        self.last_pmc_pkg_cstates = pmc_pkg_now

        return (
            power,
            rc6_pct,
            gt1_rc6_pct,
            nvme_io,
            npu_busy_pct,
            cstate_pcts,
            net_io,
            sys_stats,
            pmc_data,
        )

    def read_uptime(self):
        path = "/proc/uptime"
        if not os.path.exists(path):
            return {"seconds": 0.0, "formatted": "00:00:00:00"}
        try:
            with open(path) as f:
                val = float(f.read().split()[0])
            total_s = int(val)
            days = total_s // 86400
            hours = (total_s % 86400) // 3600
            mins = (total_s % 3600) // 60
            secs = total_s % 60
            formatted = f"{days:02d}:{hours:02d}:{mins:02d}:{secs:02d}"
            return {"seconds": val, "formatted": formatted}
        except Exception:
            return {"seconds": 0.0, "formatted": "00:00:00:00"}

    def read_ec(self):
        raw = None
        if os.path.exists(EC_IO_PATH):
            try:
                with open(EC_IO_PATH, "rb") as f:
                    data = f.read(256)
                    if len(data) >= 0xB4:
                        raw = data
            except PermissionError:
                pass
            except Exception:
                pass

        if not raw or len(raw) < 0xB4:
            shm_path = "/dev/shm/ec0_raw"
            if os.path.exists(shm_path):
                try:
                    if time.time() - os.path.getmtime(shm_path) < 1.5:
                        with open(shm_path, "rb") as f:
                            data = f.read(256)
                            if len(data) >= 0xB4:
                                raw = data
                except Exception:
                    pass

        if not raw or len(raw) < 0xB4:
            if os.path.exists("/usr/local/bin/hp-ec-dump"):
                try:
                    res = subprocess.run(["/usr/local/bin/hp-ec-dump"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=0.5)
                    if res.returncode == 0 and len(res.stdout) >= 0xB4:
                        raw = res.stdout
                except Exception:
                    pass

        if not raw or len(raw) < 0xB4:
            if os.path.exists("/dev/shm/ec0_raw"):
                try:
                    with open("/dev/shm/ec0_raw", "rb") as f:
                        data = f.read(256)
                        if len(data) >= 0xB4:
                            raw = data
                except Exception:
                    pass

        if not raw or len(raw) < 0xB4:
            return None

        try:
            fan1 = (raw[0xB1] << 8) | raw[0xB0]
            fan2 = (raw[0xB3] << 8) | raw[0xB2]
            adph = raw[0xA3] >> 4

            watt_map = {
                1: 40, 2: 65, 3: 90, 4: 120, 5: 135,
                6: 150, 7: 180, 8: 200, 9: 230, 10: 280, 11: 330
            }

            return {
                "fan1_rpm": fan1,
                "fan2_rpm": fan2,
                "ec_temp_rtmp": raw[0x58],
                "tnt1_temp": raw[0x59],
                "tnt2_temp": raw[0x49],
                "tnt3_temp": raw[0x4A],
                "adapter_watts": watt_map.get(adph, None),
                "battery_cycles": raw[0x32],
            }
        except Exception:
            return None

    def read_gpu(self):
        gpu = {
            "gt0": {"act_mhz": 0, "cur_mhz": 0, "max_mhz": 2200, "throttles": [], "throttle_status": False},
            "gt1": {"act_mhz": 0, "cur_mhz": 0, "max_mhz": 1300, "throttles": [], "throttle_status": False},
            "act_mhz": 0,
            "cur_mhz": 0,
            "max_mhz": 2200,
        }

        card = self.gpu_card or "card1"
        for gt_name, def_max in [
            ("gt0", 2200),
            ("gt1", 1300),
        ]:
            gt_path = f"/sys/class/drm/{card}/gt/{gt_name}"
            if os.path.exists(gt_path):
                for fld, fname in [
                    ("act_mhz", "rps_act_freq_mhz"),
                    ("cur_mhz", "rps_cur_freq_mhz"),
                    ("max_mhz", "rps_max_freq_mhz"),
                ]:
                    p = os.path.join(gt_path, fname)
                    if os.path.exists(p):
                        try:
                            with open(p) as f:
                                gpu[gt_name][fld] = int(f.read().strip())
                        except Exception:
                            pass
                if not gpu[gt_name]["max_mhz"]:
                    gpu[gt_name]["max_mhz"] = def_max

                status_p = os.path.join(gt_path, "throttle_reason_status")
                if os.path.exists(status_p):
                    try:
                        with open(status_p) as f:
                            gpu[gt_name]["throttle_status"] = (f.read().strip() == "1")
                    except Exception:
                        pass

                for reason in ["thermal", "pl1", "pl2", "pl4", "prochot", "ratl", "vr_tdc"]:
                    p = os.path.join(gt_path, f"throttle_reason_{reason}")
                    if os.path.exists(p):
                        try:
                            with open(p) as f:
                                if f.read().strip() == "1":
                                    gpu[gt_name]["throttles"].append(reason.upper())
                        except Exception:
                            pass

        mem = self._read_gpu_memory()
        gpu["memory"] = mem
        gpu["gt0"]["memory_mb"] = mem.get("allocated_mb", 0.0)

        gpu["act_mhz"] = gpu["gt0"]["act_mhz"]
        gpu["cur_mhz"] = gpu["gt0"]["cur_mhz"]
        gpu["max_mhz"] = gpu["gt0"]["max_mhz"]
        return gpu

    def _read_gpu_memory(self):
        """Read GPU memory allocation (dynamic GEM memory & stolen framebuffer)."""
        card_num = None
        if self.gpu_card and self.gpu_card.startswith("card"):
            card_num = self.gpu_card.replace("card", "")

        debugfs_paths = []
        if card_num:
            debugfs_paths.append(f"/sys/kernel/debug/dri/{card_num}/i915_gem_objects")
        debugfs_paths.extend([
            "/sys/kernel/debug/dri/1/i915_gem_objects",
            "/sys/kernel/debug/dri/0000:00:02.0/i915_gem_objects",
            "/sys/kernel/debug/dri/0/i915_gem_objects",
        ])
        debugfs_paths.extend(glob.glob("/sys/kernel/debug/dri/*/i915_gem_objects"))

        seen_paths = set()
        for p in debugfs_paths:
            if p in seen_paths or not os.path.exists(p):
                continue
            seen_paths.add(p)
            try:
                with open(p, "r") as f:
                    content = f.read()

                allocated_bytes = 0
                stolen_bytes = 0

                # Match: "1071 shrinkable [1 free] objects, 963235840 bytes"
                m = re.search(r"objects,\s*(\d+)\s+bytes", content)
                if m:
                    allocated_bytes = int(m.group(1))
                else:
                    m2 = re.search(r"(\d+)\s+bytes", content)
                    if m2:
                        allocated_bytes = int(m2.group(1))

                # Match: "stolen-local: total:0x0000000008000000 bytes" or decimal
                m_stolen = re.search(r"stolen[^\n]*total:\s*(0x[0-9a-fA-F]+|\d+)", content)
                if m_stolen:
                    s_val = m_stolen.group(1)
                    stolen_bytes = int(s_val, 16) if s_val.startswith("0x") else int(s_val)

                allocated_mb = allocated_bytes / (1024 * 1024)
                stolen_mb = stolen_bytes / (1024 * 1024)
                return {
                    "allocated_mb": round(allocated_mb, 1),
                    "stolen_mb": round(stolen_mb, 1),
                    "total_mb": round(allocated_mb + stolen_mb, 1),
                    "source": "debugfs",
                }
            except Exception:
                pass

        # Fallback: procfs client fdinfo
        clients = {}
        try:
            for fpath in glob.glob("/proc/[0-9]*/fdinfo/[0-9]*"):
                try:
                    with open(fpath, "r") as f:
                        lines = f.readlines()
                    cid = None
                    mem_kib = 0
                    is_i915 = False
                    for line in lines:
                        if "drm-driver:" in line and "i915" in line:
                            is_i915 = True
                        elif "drm-client-id:" in line:
                            parts = line.split()
                            if len(parts) >= 2:
                                cid = parts[1]
                        elif "drm-total-system0:" in line or "drm-resident-system0:" in line:
                            parts = line.split()
                            if len(parts) >= 2:
                                val = int(parts[1])
                                if val > mem_kib:
                                    mem_kib = val
                    if is_i915 and cid:
                        clients[cid] = max(clients.get(cid, 0), mem_kib)
                except Exception:
                    continue
        except Exception:
            pass

        if clients:
            tot_kib = sum(clients.values())
            allocated_mb = tot_kib / 1024.0
            return {
                "allocated_mb": round(allocated_mb, 1),
                "stolen_mb": 128.0,
                "total_mb": round(allocated_mb + 128.0, 1),
                "source": "fdinfo",
            }

        return {"allocated_mb": 0.0, "stolen_mb": 0.0, "total_mb": 0.0, "source": "none"}

    def read_display(self):
        disp = {"pct": 100.0, "watts": 2.5}

        bright_paths = glob.glob("/sys/class/backlight/*/brightness")
        if not bright_paths:
            return disp

        bpath = bright_paths[0]
        mpath = os.path.join(os.path.dirname(bpath), "max_brightness")

        try:
            with open(bpath) as f:
                cur = int(f.read().strip())
            with open(mpath) as f:
                mx = int(f.read().strip())

            pct = (cur / mx) * 100.0 if mx > 0 else 100.0
            disp["pct"] = pct
            disp["watts"] = 0.8 + (2.0 * (pct / 100.0))
        except Exception:
            pass

        return disp

    def read_cpu_freqs(self):
        platform = get_platform()
        top = platform.topology
        total_threads = top.get("total_threads", 18)
        freqs = []

        for i in range(total_threads):
            path = f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq"
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        freqs.append(int(f.read().strip()) // 1000)
                except Exception:
                    freqs.append(0)
            else:
                freqs.append(0)

        # Dynamic Physical P-Cores
        p_cores = []
        for c_def in top.get("p_cores", []):
            threads = c_def.get("threads", [0])
            t_freqs = [freqs[t] for t in threads if t < len(freqs)]
            cur_f = max(t_freqs) if t_freqs else 0
            cur_l = sum(self.current_cpu_loads.get(t, 0.0) for t in threads) / max(1, len(threads))
            
            is_online = any(
                (t == 0) or (os.path.exists(f"/sys/devices/system/cpu/cpu{t}/online") and open(f"/sys/devices/system/cpu/cpu{t}/online").read().strip() == "1")
                for t in threads
            )
            p_cores.append({
                "name": c_def.get("name", f"P{len(p_cores)}"),
                "mhz": cur_f,
                "load_pct": cur_l,
                "max_mhz": c_def.get("max_mhz", 4500),
                "threads": threads,
                "online": is_online,
                "core_id": c_def.get("core_id", 0),
                "temp_c": 0.0,
                "watts": 0.0,
            })

        # Dynamic Physical E-Cores
        e_cores = []
        for c_def in top.get("e_cores", []):
            t = c_def.get("primary", 0)
            cur_f = freqs[t] if t < len(freqs) else 0
            cur_l = self.current_cpu_loads.get(t, 0.0)
            is_online = (os.path.exists(f"/sys/devices/system/cpu/cpu{t}/online") and open(f"/sys/devices/system/cpu/cpu{t}/online").read().strip() == "1")
            e_cores.append({
                "name": c_def.get("name", f"E{len(e_cores)}"),
                "mhz": cur_f,
                "load_pct": cur_l,
                "max_mhz": c_def.get("max_mhz", 3600),
                "thread": t,
                "online": is_online,
                "core_id": c_def.get("core_id", 0),
                "temp_c": 0.0,
                "watts": 0.0,
            })

        # Dynamic Physical LP E-Cores
        lp_cores = []
        for c_def in top.get("lp_cores", []):
            t = c_def.get("primary", 0)
            cur_f = freqs[t] if t < len(freqs) else 0
            cur_l = self.current_cpu_loads.get(t, 0.0)
            is_online = (os.path.exists(f"/sys/devices/system/cpu/cpu{t}/online") and open(f"/sys/devices/system/cpu/cpu{t}/online").read().strip() == "1")
            lp_cores.append({
                "name": c_def.get("name", f"LP{len(lp_cores)}"),
                "mhz": cur_f,
                "load_pct": cur_l,
                "max_mhz": c_def.get("max_mhz", 2500),
                "thread": t,
                "online": is_online,
                "core_id": c_def.get("core_id", 0),
                "temp_c": 0.0,
                "watts": 0.0,
            })

        p_avg = sum(c["mhz"] for c in p_cores if c.get("online", True)) / max(1, len([c for c in p_cores if c.get("online", True)])) if p_cores else 0.0
        e_avg = sum(c["mhz"] for c in e_cores if c.get("online", True)) / max(1, len([c for c in e_cores if c.get("online", True)])) if e_cores else 0.0
        lp_avg = sum(c["mhz"] for c in lp_cores if c.get("online", True)) / max(1, len([c for c in lp_cores if c.get("online", True)])) if lp_cores else 0.0
        active_freqs = [freqs[i] for i in range(len(freqs)) if (i == 0 or (os.path.exists(f"/sys/devices/system/cpu/cpu{i}/online") and open(f"/sys/devices/system/cpu/cpu{i}/online").read().strip() == "1"))]
        all_avg = sum(active_freqs) / len(active_freqs) if active_freqs else 0.0

        return {
            "all": freqs,
            "p_cores": p_cores,
            "e_cores": e_cores,
            "lp_cores": lp_cores,
            "p_avg": p_avg,
            "e_avg": e_avg,
            "lp_avg": lp_avg,
            "all_avg": all_avg,
            "overall_load": self.current_overall_cpu_load,
            "loads": self.current_cpu_loads,
        }

    def read_power_limits(self):
        limits = {
            "pl1": 0.0, "pl2": 0.0, "pl4": 0.0, "tau": 0.0, "epp": "N/A",
            "effective_pl1": 0.0, "effective_pl2": 0.0, "effective_pl4": 0.0,
            "mmio_pl1": 0.0, "mmio_pl2": 0.0, "mmio_pl4": 0.0, "mmio_tau": 0.0,
            "msr_pl1": 0.0, "msr_pl2": 0.0, "msr_pl4": 0.0, "msr_tau": 0.0,
            "msr_pl1_enabled": True, "msr_pl1_clamped": True,
            "msr_pl2_enabled": True, "msr_pl2_clamped": True,
            "msr_locked": False,
            "mmio_active": False,
            "dptf_override": False,
            "thermald_running": False,
            "turbo": True,
            "dynamic_boost": False,
            "source": "MMIO",
            "details": "",
        }

        # 1. Read hardware MSR 0x610 (MSR_PKG_POWER_LIMIT) & MSR 0x606 (MSR_RAPL_POWER_UNIT)
        val_unit = rdmsr(0, 0x606)
        val_limit = rdmsr(0, 0x610)

        power_unit = 0.125
        time_unit = 0.0009765625
        if val_unit is not None:
            power_unit = 1.0 / (1 << (val_unit & 0xF))
            time_unit = 1.0 / (1 << ((val_unit >> 16) & 0xF))

        if val_limit is not None:
            # PL1 (bits 14:0)
            pl1_raw = val_limit & 0x7FFF
            limits["msr_pl1"] = round(pl1_raw * power_unit, 1)
            limits["msr_pl1_enabled"] = bool((val_limit >> 15) & 1)
            limits["msr_pl1_clamped"] = bool((val_limit >> 16) & 1)
            t_bits1 = (val_limit >> 17) & 0x7F
            y1 = t_bits1 & 0x1F
            z1 = (t_bits1 >> 5) & 0x3
            limits["msr_tau"] = round((1.0 + z1 / 4.0) * (2 ** y1) * time_unit, 2)

            # PL2 (bits 46:32)
            pl2_raw = (val_limit >> 32) & 0x7FFF
            limits["msr_pl2"] = round(pl2_raw * power_unit, 1)
            limits["msr_pl2_enabled"] = bool((val_limit >> 47) & 1)
            limits["msr_pl2_clamped"] = bool((val_limit >> 48) & 1)
            t_bits2 = (val_limit >> 49) & 0x7F
            y2 = t_bits2 & 0x1F
            z2 = (t_bits2 >> 5) & 0x3
            limits["msr_pl2_tau"] = round((1.0 + z2 / 4.0) * (2 ** y2) * time_unit, 2)

            limits["msr_locked"] = bool((val_limit >> 63) & 1)
        else:
            # Fallback to sysfs intel-rapl:0
            msr_base = "/sys/class/powercap/intel-rapl:0"
            if not os.path.exists(msr_base):
                msr_base = "/sys/class/powercap/intel-rapl/intel-rapl:0"
            if os.path.exists(msr_base):
                try:
                    with open(f"{msr_base}/constraint_0_power_limit_uw") as f:
                        limits["msr_pl1"] = round(int(f.read().strip()) / 1e6, 1)
                except Exception:
                    pass
                try:
                    with open(f"{msr_base}/constraint_1_power_limit_uw") as f:
                        limits["msr_pl2"] = round(int(f.read().strip()) / 1e6, 1)
                except Exception:
                    pass
                try:
                    with open(f"{msr_base}/constraint_0_time_window_us") as f:
                        limits["msr_tau"] = round(int(f.read().strip()) / 1e6, 2)
                except Exception:
                    pass

        # Also check PL4 from intel-rapl:0
        try:
            with open("/sys/class/powercap/intel-rapl:0/constraint_2_power_limit_uw") as f:
                limits["msr_pl4"] = round(int(f.read().strip()) / 1e6, 1)
        except Exception:
            pass

        # 2. Read MMIO from intel-rapl-mmio:0
        mmio_base = "/sys/class/powercap/intel-rapl-mmio:0"
        if not os.path.exists(mmio_base):
            mmio_base = "/sys/class/powercap/intel-rapl-mmio/intel-rapl-mmio:0"
        if os.path.exists(mmio_base):
            try:
                with open(f"{mmio_base}/enabled") as f:
                    limits["mmio_active"] = (f.read().strip() == "1")
            except Exception:
                limits["mmio_active"] = True

            try:
                with open(f"{mmio_base}/constraint_0_power_limit_uw") as f:
                    limits["mmio_pl1"] = round(int(f.read().strip()) / 1e6, 1)
            except Exception:
                pass
            try:
                with open(f"{mmio_base}/constraint_1_power_limit_uw") as f:
                    limits["mmio_pl2"] = round(int(f.read().strip()) / 1e6, 1)
            except Exception:
                pass
            try:
                with open(f"{mmio_base}/constraint_2_power_limit_uw") as f:
                    limits["mmio_pl4"] = round(int(f.read().strip()) / 1e6, 1)
            except Exception:
                pass
            try:
                with open(f"{mmio_base}/constraint_0_time_window_us") as f:
                    limits["mmio_tau"] = round(int(f.read().strip()) / 1e6, 2)
            except Exception:
                pass

        # 3. Check thermald and DPTF state
        try:
            res = subprocess.check_output(["systemctl", "is-active", "thermald"], text=True, stderr=subprocess.DEVNULL).strip()
            limits["thermald_running"] = (res == "active")
        except Exception:
            limits["thermald_running"] = False

        if limits["mmio_active"] and limits["mmio_pl1"] > 0 and limits["msr_pl1"] > 0:
            limits["dptf_override"] = (limits["mmio_pl1"] < limits["msr_pl1"])

        # Hardware enforces min(MSR, MMIO) for active power clamping
        pl1_cands = [v for v in (limits["mmio_pl1"] if limits["mmio_active"] else None, limits["msr_pl1"]) if v is not None and v > 0]
        pl2_cands = [v for v in (limits["mmio_pl2"] if limits["mmio_active"] else None, limits["msr_pl2"]) if v is not None and v > 0]
        pl4_cands = [v for v in (limits["mmio_pl4"] if limits["mmio_active"] else None, limits["msr_pl4"]) if v is not None and v > 0]

        limits["effective_pl1"] = min(pl1_cands) if pl1_cands else 35.0
        limits["effective_pl2"] = min(pl2_cands) if pl2_cands else 51.0
        limits["effective_pl4"] = min(pl4_cands) if pl4_cands else 120.0

        limits["pl1"] = limits["effective_pl1"]
        limits["pl2"] = limits["effective_pl2"]
        limits["pl4"] = limits["effective_pl4"]

        if limits["mmio_active"] and limits["mmio_pl1"] > 0 and limits["mmio_pl1"] <= limits["effective_pl1"]:
            limits["source"] = "MMIO"
            limits["tau"] = limits["mmio_tau"] if limits["mmio_tau"] > 0 else limits["msr_tau"]
        else:
            limits["source"] = "MSR"
            limits["tau"] = limits["msr_tau"] if limits["msr_tau"] > 0 else 32.0

        epp_p = "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"
        if os.path.exists(epp_p):
            try:
                with open(epp_p) as f:
                    limits["epp"] = f.read().strip()
            except Exception:
                pass

        turbo_p = "/sys/devices/system/cpu/intel_pstate/no_turbo"
        if os.path.exists(turbo_p):
            try:
                with open(turbo_p) as f:
                    limits["turbo"] = (f.read().strip() != "1")
            except Exception:
                limits["turbo"] = True

        hwp_boost_p = "/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost"
        if os.path.exists(hwp_boost_p):
            try:
                with open(hwp_boost_p) as f:
                    limits["dynamic_boost"] = (f.read().strip() == "1")
            except Exception:
                limits["dynamic_boost"] = False

        # Build informative details summary
        lock_txt = "Locked" if limits["msr_locked"] else "Unlocked"
        clamp_txt = "Clamped" if limits["msr_pl1_clamped"] else "Unclamped"
        if limits["source"] == "MMIO" and limits["msr_pl1"] > limits["effective_pl1"]:
            delta = limits["msr_pl1"] - limits["effective_pl1"]
            limits["details"] = f"MMIO override active (-{delta:.0f}W vs MSR {limits['msr_pl1']:.0f}W) | MSR {lock_txt}, {clamp_txt}"
        else:
            limits["details"] = f"MSR 0x610 active (PL1: {limits['msr_pl1']:.0f}W, {lock_txt}, {clamp_txt})"

        return limits

    def read_memory(self):
        if not os.path.exists("/proc/meminfo"):
            return None
        mem = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        mem[k.strip()] = int(v.strip().split()[0]) * 1024
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", 0)
            free = mem.get("MemFree", 0)
            buffers = mem.get("Buffers", 0)
            cached = mem.get("Cached", 0)
            used = max(0, total - avail)
            used_pct = (used / total * 100.0) if total > 0 else 0.0

            swap_total = mem.get("SwapTotal", 0)
            swap_free = mem.get("SwapFree", 0)
            swap_used = max(0, swap_total - swap_free)
            swap_pct = (swap_used / swap_total * 100.0) if swap_total > 0 else 0.0

            return {
                "total_b": total,
                "used_b": used,
                "avail_b": avail,
                "free_b": free,
                "buffers_b": buffers,
                "cached_b": cached,
                "used_pct": used_pct,
                "swap_total_b": swap_total,
                "swap_used_b": swap_used,
                "swap_free_b": swap_free,
                "swap_pct": swap_pct,
            }
        except Exception:
            return None

    def read_npu(self):
        npu_dir = "/sys/class/accel/accel0/device"
        if not os.path.exists(npu_dir):
            return None

        def _rd(fname):
            p = os.path.join(npu_dir, fname)
            if os.path.exists(p):
                try:
                    with open(p) as o:
                        return o.read().strip()
                except Exception:
                    pass
            return ""

        state = _rd("power_state") or "Unknown"
        cur_mhz = int(_rd("npu_current_frequency_mhz") or 0)
        max_mhz = int(_rd("npu_max_frequency_mhz") or 1400)
        mem_bytes = int(_rd("npu_memory_utilization") or 0)

        return {
            "state": state,
            "cur_mhz": cur_mhz,
            "max_mhz": max_mhz,
            "mem_mb": mem_bytes / (1024 * 1024),
        }

    def read_wireless(self):
        data = {"link": "N/A", "level_dbm": None, "quality": "N/A", "power_save": "N/A"}
        if os.path.exists("/proc/net/wireless"):
            try:
                with open("/proc/net/wireless") as f:
                    for line in f:
                        if "wlo" in line or "wlan" in line:
                            parts = line.split()
                            if len(parts) >= 4:
                                q_val = parts[2].rstrip(".")
                                l_val = parts[3].rstrip(".")
                                data["quality"] = f"{q_val}/70"
                                data["level_dbm"] = f"{l_val} dBm"
                                break
            except Exception:
                pass

        try:
            res = subprocess.check_output(
                ["iwconfig", "wlo1"],
                text=True,
                stderr=subprocess.DEVNULL
            )
            if "Power Management:on" in res:
                data["power_save"] = "Enabled"
            elif "Power Management:off" in res:
                data["power_save"] = "Disabled"
        except Exception:
            pass

        return data

    def read_thermals(self):
        data = {"board": {}, "cores": {}, "ssd": None, "package": None}

        zone_names = {
            "SEN1": "Skin/Palmrest",
            "SEN2": "VRM Power Stage",
            "SEN3": "Aux Ambient",
            "acpitz": "ACPI Zone 0",
            "TCPU": "ACPI CPU",
            "TCPU_PCI": "PCIe Proximity",
        }

        for tz in glob.glob("/sys/class/thermal/thermal_zone*"):
            try:
                with open(f"{tz}/type") as f:
                    ztype = f.read().strip()
                with open(f"{tz}/temp") as f:
                    raw_temp = int(f.read().strip())

                val = raw_temp / 1000.0 if raw_temp > 200 else float(raw_temp)

                if ztype in zone_names:
                    data["board"][zone_names[ztype]] = val
            except Exception:
                pass

        for lf in glob.glob("/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_label"):
            try:
                with open(lf) as f:
                    lbl = f.read().strip()
                with open(lf[:-5] + "input") as f:
                    tval = int(f.read().strip()) / 1000.0

                if "Package" in lbl:
                    data["package"] = tval
                else:
                    data["cores"][lbl] = tval
            except Exception:
                pass

        for nvme_temp in glob.glob("/sys/class/hwmon/hwmon*/temp1_input"):
            try:
                name_file = os.path.join(os.path.dirname(nvme_temp), "name")
                if os.path.exists(name_file):
                    with open(name_file) as f:
                        if "nvme" in f.read():
                            with open(nvme_temp) as tf:
                                data["ssd"] = int(tf.read().strip()) / 1000.0
                            break
            except Exception:
                pass

        return data

    def read_throttle(self):
        th = {}
        base = "/sys/devices/system/cpu/cpu0/thermal_throttle"

        files = {
            "pkg_events": "package_throttle_count",
            "core_events": "core_throttle_count",
            "total_time_ms": "package_throttle_total_time_ms",
        }

        for k, fn in files.items():
            path = os.path.join(base, fn)
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        th[k] = int(f.read().strip())
                except Exception:
                    th[k] = 0

        return th

    def read_battery(self):
        path = None
        for p in sorted(glob.glob("/sys/class/power_supply/BAT*")):
            if os.path.isdir(p):
                path = p
                break

        if not path:
            return None

        bat = {}

        attrs = [
            "status",
            "capacity",
            "voltage_now",
            "power_now",
            "energy_now",
            "energy_full",
            "energy_full_design",
            "cycle_count",
        ]

        for a in attrs:
            p = os.path.join(path, a)
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        val = f.read().strip()

                    if a in ["capacity", "cycle_count"]:
                        bat[a] = int(val)
                    elif a in ["voltage_now", "power_now", "energy_now", "energy_full", "energy_full_design"]:
                        bat[a] = int(val) / 1e6
                    else:
                        bat[a] = val
                except Exception:
                    pass

        if "energy_full" in bat and "energy_full_design" in bat and bat["energy_full_design"] > 0:
            bat["health_pct"] = (bat["energy_full"] / bat["energy_full_design"]) * 100.0

        st = str(bat.get("status", "")).lower()
        p_now = float(bat.get("power_now", 0.0) or 0.0)
        e_now = float(bat.get("energy_now", 0.0) or 0.0)
        e_full = float(bat.get("energy_full", 0.0) or 0.0)

        if st == "discharging" and p_now > 0.1 and e_now > 0:
            hrs = e_now / p_now
            h = int(hrs)
            m = int((hrs - h) * 60)
            bat["time_remaining_str"] = f"{h}h {m:02d}m remaining on bat"
        elif st == "charging" and p_now > 0.1 and e_full > e_now:
            hrs = (e_full - e_now) / p_now
            h = int(hrs)
            m = int((hrs - h) * 60)
            bat["time_remaining_str"] = f"{h}h {m:02d}m until full (100%)"
        elif st == "full":
            bat["time_remaining_str"] = "Fully Charged (AC Powered)"
        else:
            bat["time_remaining_str"] = None

        return bat

    def read_power_sources(self, ec_data=None, bat_data=None):
        mains_online = False
        usb_psy_online = False
        usb_info = {}

        # Physical Type-C partner detection: Linux typec class creates a port*-partner
        # directory only when a cable/partner device is physically plugged into the port.
        has_typec_partner = bool(
            glob.glob("/sys/class/typec/*partner*") or
            glob.glob("/sys/class/typec/port*/*partner*")
        )

        bat_status = str(bat_data.get("status", "")).lower() if bat_data else ""
        is_bat_charging = (bat_status == "charging")

        for ps in sorted(glob.glob("/sys/class/power_supply/*")):
            name = os.path.basename(ps)
            if re.match(r"^BAT\d*$", name):
                continue

            def _read(fname):
                p = os.path.join(ps, fname)
                if os.path.exists(p):
                    try:
                        with open(p) as f:
                            return f.read().strip()
                    except Exception:
                        pass
                return None

            psy_type = (_read("type") or "").lower()
            online_str = _read("online")
            is_online = (online_str == "1")
            status = (_read("status") or "").lower()

            v_u = _read("voltage_now")
            v_val = float(v_u) / 1e6 if v_u and v_u.isdigit() else 0.0

            c_u = _read("current_now") or _read("current_max")
            c_val = float(c_u) / 1e6 if c_u and c_u.isdigit() else 0.0

            usb_type = _read("usb_type") or ""

            if psy_type == "mains" or "adp" in name.lower() or "acad" in name.lower():
                if is_online:
                    mains_online = True
            elif psy_type == "usb" or "usbc" in name.lower() or "ucsi" in name.lower():
                if is_online:
                    usb_psy_online = True
                usb_info = {
                    "name": name,
                    "voltage": v_val,
                    "current": c_val,
                    "watts": v_val * c_val if (v_val > 0 and c_val > 0) else None,
                    "usb_type": usb_type,
                    "status": status,
                    "online": is_online,
                }

        # A USB power source is truly connected if:
        # - The power_supply sysfs explicitly reports online: 1, OR
        # - A Type-C partner is physically attached AND status indicates charging, OR
        # - Battery is actively charging while barrel jack (ADP1) is offline
        usb_online = False
        if usb_psy_online:
            usb_online = True
        elif has_typec_partner and (usb_info.get("status") in ["charging"] or is_bat_charging):
            usb_online = True
        elif is_bat_charging and not mains_online:
            usb_online = True

        if not usb_online:
            usb_info = {}
        else:
            # Check /sys/class/typec/port* for USB Power Delivery status only when USB is online
            for tc in sorted(glob.glob("/sys/class/typec/port*")):
                mode_file = os.path.join(tc, "power_operation_mode")
                if os.path.exists(mode_file):
                    try:
                        with open(mode_file) as f:
                            mode = f.read().strip().lower()
                        usb_info["mode"] = mode
                        break
                    except Exception:
                        pass

            # Check hwmon for ucsi if voltage wasn't reported in sysfs power_supply
            if not usb_info.get("voltage") or usb_info["voltage"] == 0.0:
                for hw in glob.glob("/sys/class/hwmon/hwmon*"):
                    name_p = os.path.join(hw, "name")
                    if os.path.exists(name_p):
                        try:
                            with open(name_p) as f:
                                hname = f.read().strip()
                            if "ucsi" in hname.lower():
                                in0_p = os.path.join(hw, "in0_input")
                                if os.path.exists(in0_p):
                                    with open(in0_p) as f:
                                        mv = float(f.read().strip())
                                    usb_info["voltage"] = mv / 1000.0
                        except Exception:
                            pass

        # Build power source description
        mains_str = None
        if mains_online:
            ad_w = ec_data.get("adapter_watts") if ec_data else None
            mains_str = f"{ad_w} W (HP Smart Pin / Barrel)" if ad_w else "AC Adapter (Mains)"

        usb_str = None
        if usb_online:
            w = usb_info.get("watts")
            v = usb_info.get("voltage", 0.0)
            c = usb_info.get("current", 0.0)
            mode = usb_info.get("mode", "")
            utype = usb_info.get("usb_type", "")

            if w and w > 0:
                usb_str = f"{w:.0f} W (USB-C PD: {v:.1f} V @ {c:.2f} A)"
            elif v > 0:
                usb_str = f"USB-C PD ({v:.1f} V)"
            elif "delivery" in mode or "pd" in utype.lower():
                usb_str = "USB-C Power Delivery"
            elif mode in ["3.0a", "1.5a"]:
                usb_str = f"USB-C ({mode.upper()})"
            else:
                usb_str = "USB-C Connected"

        is_charging = bat_data and str(bat_data.get("status", "")).lower() == "charging"
        ac_online = mains_online or usb_online or is_charging

        if mains_str and usb_str:
            desc = f"{mains_str} + {usb_str}"
        elif mains_str:
            desc = mains_str
        elif usb_str:
            desc = usb_str
        elif is_charging:
            desc = "AC Connected (Charging)"
        else:
            desc = "Unplugged (On Battery)"

        return {
            "mains_online": mains_online,
            "usb_online": usb_online,
            "usb_info": usb_info,
            "ac_online": ac_online,
            "description": desc,
        }

    def collect(self):
        power, rc6_pct, gt1_rc6_pct, nvme_io, npu_busy_pct, cstates, net_io, sys_stats, pmc = self._sample_deltas()

        uptime = self.read_uptime()
        ec = self.read_ec()
        gpu = self.read_gpu()
        npu = self.read_npu()
        wireless = self.read_wireless()
        disp = self.read_display()
        freqs = self.read_cpu_freqs()
        limits = self.read_power_limits()
        thermals = self.read_thermals()
        throttle = self.read_throttle()
        battery = self.read_battery()
        power_sources = self.read_power_sources(ec_data=ec, bat_data=battery)

        if battery:
            battery["ac_online"] = power_sources["ac_online"]

        # Annotate per-core thermals and estimated power draw in freqs
        core_temps = thermals.get("cores", {}) if thermals else {}
        pkg_temp = thermals.get("package", 50.0) if thermals else 50.0
        pkg_w_raw = float(power.get("package", 0.0) or 0.0)
        core_w_raw = float(power.get("core", 0.0) or (pkg_w_raw * 0.7 if pkg_w_raw > 0 else 0.0))

        # Distribute core power across active cores according to relative load and clock frequency
        all_cores = freqs.get("p_cores", []) + freqs.get("e_cores", []) + freqs.get("lp_cores", [])
        weights = []
        for c in all_cores:
            cid = c.get("core_id", 0)
            c["temp_c"] = core_temps.get(f"Core {cid}", pkg_temp)
            if c.get("online", True):
                w_factor = ((c.get("load_pct", 0.0) / 100.0) * ((c.get("mhz", 400) / 1000.0) ** 1.8)) + 0.02
                weights.append(w_factor)
            else:
                weights.append(0.0)

        tot_weight = sum(weights)
        for idx, c in enumerate(all_cores):
            if c.get("online", True) and tot_weight > 0 and core_w_raw > 0:
                c["watts"] = round(core_w_raw * (weights[idx] / tot_weight), 2)
            else:
                c["watts"] = 0.0

        p_temps = [c["temp_c"] for c in freqs.get("p_cores", []) if c.get("online", True)]
        e_temps = [c["temp_c"] for c in freqs.get("e_cores", []) if c.get("online", True)]
        lp_temps = [c["temp_c"] for c in freqs.get("lp_cores", []) if c.get("online", True)]
        freqs["p_avg_temp"] = round(sum(p_temps) / len(p_temps), 1) if p_temps else pkg_temp
        freqs["e_avg_temp"] = round(sum(e_temps) / len(e_temps), 1) if e_temps else pkg_temp
        freqs["lp_avg_temp"] = round(sum(lp_temps) / len(lp_temps), 1) if lp_temps else pkg_temp

        freqs["p_tot_w"] = round(sum(c.get("watts", 0.0) for c in freqs.get("p_cores", [])), 2)
        freqs["e_tot_w"] = round(sum(c.get("watts", 0.0) for c in freqs.get("e_cores", [])), 2)
        freqs["lp_tot_w"] = round(sum(c.get("watts", 0.0) for c in freqs.get("lp_cores", [])), 2)

        # Annotate active GPU load & frequencies
        gpu_load = max(0.0, min(100.0, 100.0 - rc6_pct))
        gt1_load = max(0.0, min(100.0, 100.0 - gt1_rc6_pct))
        if gpu:
            gpu["gt0"]["rc6_pct"] = rc6_pct
            gpu["gt0"]["load_pct"] = gpu_load
            gpu["gt0"]["is_awake"] = (rc6_pct < 95.0) or (gpu["gt0"]["act_mhz"] > 0) or (gpu_load >= 5.0)
            gpu["gt0"]["freq_mhz"] = gpu["gt0"]["act_mhz"] if gpu["gt0"]["act_mhz"] > 0 else gpu["gt0"]["cur_mhz"]

            gpu["gt1"]["rc6_pct"] = gt1_rc6_pct
            gpu["gt1"]["load_pct"] = gt1_load
            gpu["gt1"]["is_awake"] = (gt1_rc6_pct < 95.0) or (gpu["gt1"]["act_mhz"] > 0) or (gt1_load >= 5.0)
            gpu["gt1"]["freq_mhz"] = gpu["gt1"]["act_mhz"] if gpu["gt1"]["act_mhz"] > 0 else gpu["gt1"]["cur_mhz"]

            gpu["rc6_pct"] = rc6_pct
            gpu["load_pct"] = gpu_load
            gpu["is_awake"] = gpu["gt0"]["is_awake"]
            gpu["freq_mhz"] = gpu["gt0"]["freq_mhz"]

        # Annotate NPU load & awake state
        if npu:
            npu["busy_pct"] = npu_busy_pct
            npu["load_pct"] = npu_busy_pct
            npu["is_awake"] = (npu.get("state") != "D3hot") or (npu.get("cur_mhz", 0) > 0) or (npu_busy_pct >= 1.0)

        # Realistic iGPU power calculation:
        # Meteor Lake Xe-LPG graphics tile is physically decoupled from the CPU uncore RAPL domain.
        # When awake or active at 800MHz base clock, the Xe graphics tile consumes ~1.8W baseline,
        # scaling with frequency (up to 1200MHz) and active compute residency.
        perf_gpu_w = float(power.get("gpu", 0.0) or 0.0)

        act_f = int(gpu.get("gt0", {}).get("act_mhz", 0) or gpu.get("act_mhz", 0) or 0) if gpu else 0
        cur_f = int(gpu.get("gt0", {}).get("cur_mhz", 0) or gpu.get("cur_mhz", 0) or 0) if gpu else 0
        freq_mhz = act_f if act_f > 0 else (cur_f if cur_f > 0 else 800)

        # Active fraction from RC6 residency
        act_ratio = max(0.0, min(1.0, (100.0 - rc6_pct) / 100.0))
        is_awake = (gpu and gpu.get("gt0", {}).get("is_awake")) or (act_ratio > 0.02)

        # Base awake power at 800MHz is ~1.8W, scaling with frequency
        f_ratio = max(0.5, freq_mhz / 800.0)
        base_awake_w = 1.8 * f_ratio

        # Dynamic compute power (up to ~10-12W under full 3D/compute load at 1200MHz)
        load_frac = max(act_ratio, (gpu_load / 100.0) if gpu else 0.0)
        dyn_compute_w = 10.0 * ((freq_mhz / 1200.0) ** 2) * load_frac

        # Idle RC6 power-gated leakage is ~0.06W
        sleep_w = 0.06 * (rc6_pct / 100.0)

        if is_awake:
            modeled_gpu_w = (base_awake_w * act_ratio) + dyn_compute_w + (sleep_w * (1.0 - act_ratio))
            if act_ratio > 0.05 or act_f > 0:
                modeled_gpu_w = max(modeled_gpu_w, 1.6 * f_ratio)
        else:
            modeled_gpu_w = sleep_w + (base_awake_w * act_ratio)

        # Use maximum of functional hardware counter (if functional >= 0.5W) and modeled power
        gpu_w = max(perf_gpu_w, modeled_gpu_w) if perf_gpu_w >= 0.5 else modeled_gpu_w
        if gpu:
            gpu["watts"] = gpu_w

        pkg_w = float(power.get("package", 0.0) or 0.0)
        core_w = float(power.get("core", 0.0) or 0.0)
        uncore_w = float(power.get("uncore", 0.0) or 0.0)

        fan_rpm = int(ec.get("fan1_rpm", 0) or 0) if ec else 0
        fan_w = (fan_rpm / 1000.0) ** 1.8 * 0.15 if fan_rpm > 0 else 0.0

        nvme_w = 0.3 + min(3.0, (nvme_io["read_mbs"] + nvme_io["write_mbs"]) * 0.005)

        disp_w = float(disp.get("watts", 0.0) or 0.0)

        # Rough board overhead estimates
        ram_w = 1.0 + (pkg_w * 0.08)
        board_loss_w = (pkg_w + disp_w) * 0.12

        total_est_w = pkg_w + disp_w + nvme_w + fan_w + ram_w + board_loss_w

        battery_rail_w = None
        battery_charge_w = None
        if battery:
            pw = battery.get("power_now")
            st = str(battery.get("status", "")).lower()
            if pw is not None and pw > 0:
                if st == "discharging":
                    battery_rail_w = float(pw)
                elif st == "charging":
                    battery_charge_w = float(pw)

        breakdown = {
            "cpu_package_w": pkg_w,
            "cpu_cores_w": core_w,
            "uncore_w": uncore_w,
            "gpu_w": gpu_w,
            "gpu_rc6_pct": rc6_pct,
            "gpu_load_pct": gpu_load,
            "gt1_rc6_pct": gt1_rc6_pct,
            "gt1_load_pct": gt1_load,
            "npu_busy_pct": npu_busy_pct,
            "display_w": disp_w,
            "nvme_w": nvme_w,
            "fan_w": fan_w,
            "ram_w": ram_w,
            "board_loss_w": board_loss_w,
            "total_estimated_w": total_est_w,
            "battery_rail_w": battery_rail_w,
            "battery_charge_w": battery_charge_w,
        }

        memory = self.read_memory()

        return {
            "timestamp": time.time(),
            "uptime": uptime,
            "breakdown": breakdown,
            "power_limits": limits,
            "ec": ec,
            "gpu": gpu,
            "npu": npu,
            "wireless": wireless,
            "cstates": cstates,
            "pmc": pmc,
            "sys_stats": sys_stats,
            "net_io": net_io,
            "disp": disp,
            "freqs": freqs,
            "thermals": thermals,
            "throttle": throttle,
            "battery": battery,
            "power_sources": power_sources,
            "nvme_io": nvme_io,
            "memory": memory,
        }


def render_power_section(data, inner):
    out = []

    b = data.get("breakdown", {})
    pkg = float(b.get("cpu_package_w", 0.0) or 0.0)
    core = float(b.get("cpu_cores_w", 0.0) or 0.0)
    uncore = float(b.get("uncore_w", 0.0) or 0.0)
    disp_w = float(b.get("display_w", 0.0) or 0.0)
    nvme_w = float(b.get("nvme_w", 0.0) or 0.0)
    fan_w = float(b.get("fan_w", 0.0) or 0.0)
    board_w = float(b.get("ram_w", 0.0) or 0.0) + float(b.get("board_loss_w", 0.0) or 0.0)
    tot_w = float(b.get("total_estimated_w", 0.0) or 0.0)
    rail_w = b.get("battery_rail_w")
    rc6 = float(b.get("gpu_rc6_pct", 0.0) or 0.0)
    gt1_rc6 = float(b.get("gt1_rc6_pct", 0.0) or 0.0)
    npu_pct = float(b.get("npu_busy_pct", 0.0) or 0.0)

    gpu = data.get("gpu", {})
    npu = data.get("npu") or {}
    disp = data.get("disp", {})
    nio = data.get("nvme_io", {})
    ec = data.get("ec") or {}
    lim = data.get("power_limits", {})

    def pct_of(x):
        return (x / tot_w * 100.0) if tot_w > 0 else 0.0

    pkg_pct = pct_of(pkg)
    gpu_w = float(b.get("gpu_w", 0.0) or gpu.get("watts", 0.0) or 0.0)
    gpu_pct = pct_of(gpu_w)

    # GT0 Render Tile
    act = int(gpu.get("gt0", {}).get("act_mhz", 0) or gpu.get("act_mhz", 0) or 0)
    cur = int(gpu.get("gt0", {}).get("cur_mhz", 0) or gpu.get("cur_mhz", 0) or 0)
    gpu_mem = float(gpu.get("memory", {}).get("allocated_mb", 0.0) or gpu.get("gt0", {}).get("memory_mb", 0.0) or 0.0)
    if gpu_mem >= 1024:
        vram_str = f"{gpu_mem / 1024.0:.1f} GB VRAM"
        vram_short = f"{gpu_mem / 1024.0:.1f}G"
    elif gpu_mem > 0:
        vram_str = f"{gpu_mem:.0f} MB VRAM"
        vram_short = f"{gpu_mem:.0f}M"
    else:
        vram_str = ""
        vram_short = ""

    gpu_load = max(0.0, min(100.0, 100.0 - rc6))
    gpu_awake = (rc6 < 95.0) or (act > 0) or (gpu_load >= 5.0) or (gpu_w >= 0.1)
    freq0 = act if act > 0 else cur

    if not gpu_awake:
        if vram_short:
            gpu_state = f"Idle Sleep (RC6, {vram_short})"
        else:
            gpu_state = "Idle Sleep (RC6)"
        gpu_power_str = color_watts(gpu_w) if gpu_w >= 0.05 else f"{C_DIM}<0.05 W{C_RESET}"
    else:
        v_part = f", {vram_short}" if vram_short else ""
        gpu_state = f"Active {freq0}MHz ({gpu_load:.0f}%{v_part})"
        gpu_power_str = color_watts(gpu_w)

    # GT1 Media Engine Tile
    act1 = int(gpu.get("gt1", {}).get("act_mhz", 0) or 0)
    cur1 = int(gpu.get("gt1", {}).get("cur_mhz", 0) or 0)
    gt1_load = max(0.0, min(100.0, 100.0 - gt1_rc6))
    gt1_awake = (gt1_rc6 < 95.0) or (act1 > 0) or (gt1_load >= 5.0)
    freq1 = act1 if act1 > 0 else cur1
    if not gt1_awake:
        gt1_state = "Idle Sleep (RC6)"
    else:
        gt1_state = f"Active ({freq1} MHz)"

    # NPU Tile
    npu_state = npu.get("state", "D3hot")
    npu_mhz = int(npu.get("cur_mhz", 0) or 0)
    npu_mem = float(npu.get("mem_mb", 0.0) or 0.0)
    npu_awake = (npu_state != "D3hot") or (npu_mhz > 0) or (npu_pct >= 1.0)
    if not npu_awake:
        npu_desc = f"Standby (D3hot, {npu_mem:.0f}M)" if npu_mem > 0 else "Standby (D3hot)"
    else:
        npu_desc = f"Active {npu_mhz}MHz ({npu_pct:.0f}%)"

    disp_state = f"Backlight {float(disp.get('pct', 0.0) or 0.0):.0f}%"
    nvme_state = f"R:{float(nio.get('read_mbs', 0.0) or 0.0):.1f} W:{float(nio.get('write_mbs', 0.0) or 0.0):.1f} MB/s"

    fan_rpm = int(ec.get("fan1_rpm", 0) or 0)
    fan_state = f"{fan_rpm} RPM (EC0)" if fan_rpm > 0 else "0 RPM (Stopped)"

    desired = [25, 27, 9, 13]
    minimum = [16, 16, 8, 8]
    available = max(0, inner - len(desired))
    widths = fit_columns(desired, minimum, available)

    use_table = (
        inner >= 62
        and all(w > 0 for w in widths)
        and sum(widths) <= available
    )

    if use_table:
        align = ["left", "left", "right", "right"]

        out.append(
            f"{C_DIM}"
            + make_row(["Component", "Activity / State", "Power", "% Share"], widths, align)
            + C_RESET
        )
        out.append(f"{C_DIM}{make_sep(widths)}{C_RESET}")

        def share_cell(val):
            if val is None:
                return ""
            return bar(val, max(4, widths[3]))

        rows = [
            make_row(
                [
                    f"{C_BOLD}CPU Total Package (RAPL){C_RESET}",
                    "All Tiles Combined",
                    color_watts(pkg),
                    share_cell(pkg_pct),
                ],
                widths,
                align,
            ),
            make_row(
                [
                    f"  ├─ {C_CYAN}IA Compute Cores{C_RESET}",
                    "4P + 8E Active Cores",
                    color_watts(core),
                    "",
                ],
                widths,
                align,
            ),
            make_row(
                [
                    f"  └─ {C_CYAN}SoC / Uncore / NPU{C_RESET}",
                    "Ring, IMC, LP-Island",
                    color_watts(uncore),
                    "",
                ],
                widths,
                align,
            ),
            make_row(
                [
                    "Intel Arc Graphics (GT0)",
                    gpu_state,
                    gpu_power_str,
                    share_cell(gpu_pct),
                ],
                widths,
                align,
            ),
            make_row(
                [
                    "Intel Media Engine (GT1)",
                    gt1_state,
                    f"{C_DIM}(Shared){C_RESET}",
                    "",
                ],
                widths,
                align,
            ),
            make_row(
                [
                    "Intel AI Boost NPU",
                    npu_desc,
                    f"{C_DIM}(in SoC){C_RESET}",
                    "",
                ],
                widths,
                align,
            ),
            make_row(
                [
                    "Display Panel + LEDs",
                    disp_state,
                    color_watts(disp_w),
                    share_cell(pct_of(disp_w)),
                ],
                widths,
                align,
            ),
            make_row(
                [
                    "Micron NVMe Gen4 SSD",
                    nvme_state,
                    color_watts(nvme_w),
                    share_cell(pct_of(nvme_w)),
                ],
                widths,
                align,
            ),
            make_row(
                [
                    "Cooling Fan Blower",
                    fan_state,
                    color_watts(fan_w),
                    "",
                ],
                widths,
                align,
            ),
            make_row(
                [
                    "LPDDR5x RAM + VRM Losses",
                    "Power delivery conv.",
                    color_watts(board_w),
                    share_cell(pct_of(board_w)),
                ],
                widths,
                align,
            ),
        ]

        out.extend(rows)

    else:
        out.append(split_line(f"{C_BOLD}CPU Package{C_RESET} ({pkg_pct:.0f}%)", color_watts(pkg), inner))
        out.append(f"  IA cores {color_watts(core)} | uncore {color_watts(uncore)}")
        out.append(f"Arc GPU GT0: {gpu_state} [{gpu_power_str}]")
        out.append(f"Media GT1: {gt1_state} {C_DIM}(Shared){C_RESET}")
        out.append(f"NPU: {npu_desc} {C_DIM}(in SoC){C_RESET}")
        out.append(split_line(f"Display ({disp_state})", color_watts(disp_w), inner))
        out.append(split_line(f"NVMe ({nvme_state})", color_watts(nvme_w), inner))
        out.append(split_line(f"Fan ({fan_state})", color_watts(fan_w), inner))
        out.append(split_line("RAM + VRM losses", color_watts(board_w), inner))

    out.append(f"{C_DIM} {'─' * max(1, inner - 2)}{C_RESET}")

    chg_w = b.get("battery_charge_w")
    if rail_w:
        total_label = "Total Battery Rail Draw"
        total_val = float(rail_w)
        right = f"{color_watts(total_val)} ({total_val:.2f} W total)"
    elif chg_w:
        total_label = "Total Wall/AC Estimate"
        total_val = tot_w + float(chg_w)
        right = f"{color_watts(total_val)} ({tot_w:.1f} W load + {float(chg_w):.1f} W charge)"
    else:
        total_label = "Total Wall/AC Estimate"
        total_val = tot_w
        right = f"{color_watts(total_val)} ({total_val:.2f} W total)"
    left = f" {C_BOLD}{total_label}{C_RESET}"
    out.append(split_line(left, right, inner))

    pl1 = float(lim.get("pl1", 35.0) or 35.0)
    pl2 = float(lim.get("pl2", 51.0) or 51.0)
    pl4 = float(lim.get("pl4", 120.0) or 120.0)
    tau = float(lim.get("tau", 32.0) or 32.0)
    src = lim.get("source", "MMIO")
    epp = lim.get("epp", "N/A")

    cap_pct = min(100.0, (pkg / pl1 * 100.0)) if pl1 > 0 else 0.0
    headroom_w = max(0.0, pl1 - pkg)

    bar_width = max(4, min(10, inner - 62))
    col = C_GREEN if cap_pct < 75 else (C_YELLOW if cap_pct < 95 else C_RED)
    if inner >= 82:
        left = f" Limits [{src}]: PL1 {pl1:.0f}W (Sustained) | PL2 {pl2:.0f}W ({tau:.0f}s) | PL4 {pl4:.0f}W"
    else:
        left = f" Limits [{src}]: PL1 {pl1:.0f}W | PL2 {pl2:.0f}W ({tau:.0f}s)"
    right = f"[{bar(cap_pct, bar_width, col)}] {cap_pct:4.1f}% PL1 ({headroom_w:.1f}W free)"
    out.append(split_line(left, right, inner))

    return out

def render_cpu_section(data, inner):
    out = []

    fr = data.get("freqs", {})
    p_cores = fr.get("p_cores", [])
    e_cores = fr.get("e_cores", [])
    lp_cores = fr.get("lp_cores", [])
    p_avg = float(fr.get("p_avg", 0.0) or 0.0)
    e_avg = float(fr.get("e_avg", 0.0) or 0.0)
    lp_avg = float(fr.get("lp_avg", 0.0) or 0.0)
    all_avg = float(fr.get("all_avg", 0.0) or 0.0)
    overall_load = float(fr.get("overall_load", 0.0) or 0.0)

    b = data.get("breakdown", {})
    pkg_w = float(b.get("cpu_package_w", 0.0) or 0.0)

    # Top line: Overall CPU utilization meter & Package power
    cpu_col = C_GREEN if overall_load < 60 else (C_YELLOW if overall_load < 85 else C_RED)
    cpu_bar = bar(overall_load, 8, cpu_col)
    left_top = f"Overall CPU: [{cpu_bar}] {overall_load:4.1f}%"
    right_top = f"Pkg Power: {color_watts(pkg_w)} | All-Core Avg: {all_avg:4.0f} MHz"
    out.append(split_line(left_top, right_top, inner))

    # Helper to render a single core cell
    def render_core_cell(core, width=24):
        name = core.get("name", "C")
        mhz = int(core.get("mhz", 0) or 0)
        load = float(core.get("load_pct", 0.0) or 0.0)
        temp = float(core.get("temp_c", 0.0) or 0.0)
        watts = float(core.get("watts", 0.0) or 0.0)
        online = core.get("online", True)
        if not online:
            return pad(f"{C_DIM}{name:3s} PARKED{C_RESET}", width, "left")

        c_col = C_GREEN if load < 60 else (C_YELLOW if load < 85 else C_RED)
        b_str = bar(load, 4, c_col)
        t_col = C_GREEN if temp < 60 else (C_YELLOW if temp < 85 else C_RED)
        temp_str = f"{t_col}{temp:2.0f}°C{C_RESET}" if temp > 0 else ""
        pwr_str = f"{watts:3.1f}W" if watts > 0 else ""

        if width >= 32:
            txt = f"{C_BOLD}{C_CYAN}{name:3s}{C_RESET} {mhz:4d}M {temp_str} {pwr_str} [{b_str}] {load:2.0f}%"
        elif width >= 26:
            txt = f"{C_BOLD}{C_CYAN}{name:3s}{C_RESET} {mhz:4d}M {temp_str} [{b_str}] {load:2.0f}%"
        else:
            txt = f"{C_BOLD}{C_CYAN}{name:3s}{C_RESET} {mhz:4d}M [{b_str}] {load:2.0f}%"
        return pad(txt, width, "left")

    # 3 columns layout if inner >= 74, else 2 columns if inner >= 50, else 1 column
    if inner >= 74:
        col_w = (inner - 2) // 3
        # Column 1: P0, P1, P2, P3, P-Avg
        c1_items = [render_core_cell(c, col_w) for c in p_cores]
        p_avg_temp = float(fr.get("p_avg_temp", 0.0) or 0.0)
        p_tot_w = float(fr.get("p_tot_w", 0.0) or 0.0)
        if p_avg_temp > 0 and col_w >= 28:
            p_avg_txt = f"{C_DIM}P-Avg:{C_RESET} {p_avg:4.0f}MHz ({p_avg_temp:.0f}°C, {p_tot_w:.1f}W)"
        elif p_avg_temp > 0:
            p_avg_txt = f"{C_DIM}P-Avg:{C_RESET} {p_avg:4.0f}MHz ({p_avg_temp:.0f}°C)"
        else:
            p_avg_txt = f"{C_DIM}P-Avg:{C_RESET} {p_avg:4.0f}MHz"
        c1_items.append(pad(p_avg_txt, col_w, "left"))

        # Column 2: E0, E1, E2, E3, LP0
        c2_items = [render_core_cell(e_cores[i], col_w) for i in range(min(4, len(e_cores)))]
        if len(lp_cores) > 0:
            c2_items.append(render_core_cell(lp_cores[0], col_w))
        else:
            c2_items.append(pad("", col_w))

        # Column 3: E4, E5, E6, E7, LP1
        c3_items = [render_core_cell(e_cores[i], col_w) for i in range(4, min(8, len(e_cores)))]
        if len(lp_cores) > 1:
            c3_items.append(render_core_cell(lp_cores[1], col_w))
        else:
            c3_items.append(pad("", col_w))

        for i in range(max(len(c1_items), len(c2_items), len(c3_items))):
            s1 = c1_items[i] if i < len(c1_items) else " " * col_w
            s2 = c2_items[i] if i < len(c2_items) else " " * col_w
            s3 = c3_items[i] if i < len(c3_items) else " " * col_w
            out.append(truncate_visible(f"{s1} {s2} {s3}", inner))

    elif inner >= 50:
        col_w = (inner - 1) // 2
        all_cores = p_cores + e_cores + lp_cores
        half = (len(all_cores) + 1) // 2
        for i in range(half):
            s1 = render_core_cell(all_cores[i], col_w)
            s2 = render_core_cell(all_cores[i + half], col_w) if (i + half < len(all_cores)) else ""
            out.append(truncate_visible(f"{s1} {s2}", inner))
    else:
        for c in p_cores + e_cores + lp_cores:
            out.append(truncate_visible(render_core_cell(c, inner), inner))

    # Compute Tiles (GPU & NPU) with friendly status
    gpu = data.get("gpu", {})
    npu = data.get("npu") or {}
    gt0 = gpu.get("gt0", {})
    gt1 = gpu.get("gt1", {})
    b = data.get("breakdown", {})
    rc6 = float(b.get("gpu_rc6_pct", 0.0) or 0.0)
    gt1_rc6 = float(b.get("gt1_rc6_pct", 0.0) or 0.0)
    gpu_w = float(b.get("gpu_w", 0.0) or gpu.get("watts", 0.0) or 0.0)

    g0_load = float(gt0.get("load_pct", 0.0) or 0.0)
    g0_freq = int(gt0.get("freq_mhz", 0) or 0)
    g0_awake = gt0.get("is_awake", False) or (gpu_w >= 0.1)

    g1_load = float(gt1.get("load_pct", 0.0) or 0.0)
    g1_freq = int(gt1.get("freq_mhz", 0) or 0)
    g1_awake = gt1.get("is_awake", False)

    npu_awake = npu.get("is_awake", False)
    npu_mhz = int(npu.get("cur_mhz", 0) or 0)
    npu_load = float(npu.get("load_pct", 0.0) or 0.0)

    if g0_awake:
        g0_txt = f"{C_GREEN}{g0_freq}MHz ({g0_load:.0f}%){C_RESET}"
    else:
        g0_txt = f"{C_DIM}Sleep (RC6){C_RESET}"

    if g1_awake:
        g1_txt = f"{C_GREEN}{g1_freq}MHz ({g1_load:.0f}%){C_RESET}"
    else:
        g1_txt = f"{C_DIM}Sleep (RC6){C_RESET}"

    accel_parts = []
    gpu_label = get_platform().gpus.get("primary_name", "GPU")
    if gt1.get("present", False) or g1_freq > 0:
        accel_parts.append(f"{gpu_label} GT0: {g0_txt}")
        accel_parts.append(f"Media GT1: {g1_txt}")
    else:
        accel_parts.append(f"{gpu_label}: {g0_txt}")
    if npu.get("present", False) or npu_awake or npu_mhz > 0:
        accel_parts.append(f"NPU: {npu_txt}")
    accel_line = " | ".join(accel_parts)
    out.append(truncate_visible(accel_line, inner))

    # Deep Sleep C-States
    cst = data.get("cstates", {})
    c10 = float(cst.get("C10", 0.0) or 0.0)
    c6 = float(cst.get("C6", 0.0) or 0.0)
    c1e = float(cst.get("C1E", 0.0) or 0.0)
    deep_sleep = float(cst.get("deep_sleep", 0.0) or (c10 + c6))

    bar_w = max(4, min(14, inner - 46))
    col = C_GREEN if deep_sleep >= 75 else (C_YELLOW if deep_sleep >= 45 else C_RED)
    cstate_bar = bar(deep_sleep, bar_w, col)
    left_cst = f"Core Sleep States: C10 {c10:.0f}% | C6 {c6:.0f}% | C1E {c1e:.0f}%"
    right_cst = f"[{cstate_bar}] {deep_sleep:4.1f}% Deep Sleep"
    out.append(split_line(left_cst, right_cst, inner))

    # PMC Package C-States
    pmc = data.get("pmc", {})
    c8_pct = float(pmc.get("c8_pct", 0.0) or 0.0)
    c10_pct = float(pmc.get("c10_pct", 0.0) or 0.0)
    c2_pct = float(pmc.get("c2_pct", 0.0) or 0.0)
    c8_s = float(pmc.get("c8_s", 0.0) or 0.0)
    c10_s = float(pmc.get("c10_s", 0.0) or 0.0)
    pkg_deep = c8_pct + c10_pct

    pkg_col = C_GREEN if pkg_deep >= 50 else (C_YELLOW if pkg_deep > 0 else C_DIM)
    pkg_bar_w = max(4, min(10, inner - 54))
    pkg_bar = bar(pkg_deep, pkg_bar_w, pkg_col)

    if c2_pct > 0 and pkg_deep == 0:
        left_pmc = f"PMC Package C-States: C8 {c8_pct:.1f}% | C10 {c10_pct:.1f}% (C2 {c2_pct:.0f}%)"
    else:
        left_pmc = f"PMC Package C-States: C8 {c8_pct:.1f}% | C10 {c10_pct:.1f}%"

    if c8_s > 0 or c10_s > 0:
        right_pmc = f"[{pkg_bar}] {pkg_deep:4.1f}% (C8:{c8_s:.0f}s C10:{c10_s:.0f}s)"
    else:
        right_pmc = f"[{pkg_bar}] {pkg_deep:4.1f}% Pkg Deep"
    out.append(split_line(left_pmc, right_pmc, inner))

    # System Activity & Uptime
    sys_st = data.get("sys_stats", {})
    ctxt = float(sys_st.get("ctxt_per_sec", 0.0) or 0.0)
    intr = float(sys_st.get("intr_per_sec", 0.0) or 0.0)
    uptime = data.get("uptime", {})
    uptime_str = uptime.get("formatted", "00:00:00:00")

    left_sys = f"Sys Activity: {int(ctxt):,} ctxt/s | {int(intr):,} intr/s"
    right_sys = f"Uptime: {C_BOLD}{uptime_str}{C_RESET}"
    out.append(split_line(left_sys, right_sys, inner))

    return out


def render_memory_section(data, inner):
    out = []
    mem = data.get("memory")
    if not mem:
        out.append(f"{C_DIM}Memory telemetry unavailable (/proc/meminfo){C_RESET}")
        return out

    total_b = mem.get("total_b", 0)
    used_b = mem.get("used_b", 0)
    avail_b = mem.get("avail_b", 0)
    free_b = mem.get("free_b", 0)
    buff_b = mem.get("buffers_b", 0)
    cache_b = mem.get("cached_b", 0)
    used_pct = mem.get("used_pct", 0.0)

    sw_tot = mem.get("swap_total_b", 0)
    sw_used = mem.get("swap_used_b", 0)
    sw_free = mem.get("swap_free_b", 0)
    sw_pct = mem.get("swap_pct", 0.0)

    ram_col = C_GREEN if used_pct < 60 else (C_YELLOW if used_pct < 80 else (C_ORANGE if used_pct < 90 else C_RED))
    sw_col = C_GREEN if sw_pct < 20 else (C_YELLOW if sw_pct < 50 else C_RED)

    bar_w = max(6, min(14, (inner - 48) // 2))

    # Line 1: RAM bar + used / total
    left_ram = f"RAM:  [{bar(used_pct, bar_w, ram_col)}] {used_pct:4.1f}%"
    right_ram = f"Used: {format_bytes(used_b)} / {format_bytes(total_b)}"
    out.append(split_line(left_ram, right_ram, inner))

    # Line 2: RAM breakdown
    breakdown = (
        f"{C_DIM}Available: {C_RESET}{format_bytes(avail_b)} | "
        f"{C_DIM}Free: {C_RESET}{format_bytes(free_b)} | "
        f"{C_DIM}Buffers: {C_RESET}{format_bytes(buff_b)} | "
        f"{C_DIM}Cache: {C_RESET}{format_bytes(cache_b)}"
    )
    out.append(truncate_visible(breakdown, inner))

    # Line 3: Swap
    if sw_tot > 0:
        left_sw = f"Swap: [{bar(sw_pct, bar_w, sw_col)}] {sw_pct:4.1f}%"
        right_sw = f"Used: {format_bytes(sw_used)} / {format_bytes(sw_tot)} ({C_DIM}Free: {format_bytes(sw_free)}{C_RESET})"
        out.append(split_line(left_sw, right_sw, inner))
    else:
        out.append(f"{C_DIM}Swap: Disabled (No swap partition/file){C_RESET}")

    return out


def render_thermal_section(data, inner):
    out = []

    thm = data.get("thermals", {})
    ec = data.get("ec") or {}
    board = thm.get("board", {})
    lim = data.get("power_limits", {})
    b = data.get("breakdown", {})

    pkg_t = color_temp(thm.get("package"))
    ssd_t = color_temp(thm.get("ssd"))
    ec_t = color_temp(ec.get("ec_temp_rtmp"))
    vrm_t = color_temp(board.get("VRM Power Stage"))

    out.append(f"CPU Package: {pkg_t} | NVMe SSD: {ssd_t} | EC Core: {ec_t} | VRM: {vrm_t}")

    palm_t = color_temp(board.get("Skin/Palmrest"))
    amb_t = color_temp(board.get("Aux Ambient"))
    acpi_t = color_temp(board.get("ACPI CPU"))
    fan1 = int(ec.get("fan1_rpm", 0) or 0)
    fan2 = int(ec.get("fan2_rpm", 0) or 0)
    fan_w = float(b.get("fan_w", 0.0) or 0.0)

    if fan1 > 0 and fan2 > 0:
        c1 = C_CYAN if fan1 < 2500 else (C_YELLOW if fan1 < 4000 else C_RED)
        c2 = C_CYAN if fan2 < 2500 else (C_YELLOW if fan2 < 4000 else C_RED)
        fan_txt = f"{c1}{fan1} RPM{C_RESET} / {c2}{fan2} RPM{C_RESET} ({fan_w:.1f}W)"
    elif fan1 > 0:
        c1 = C_CYAN if fan1 < 2500 else (C_YELLOW if fan1 < 4000 else C_RED)
        fan_txt = f"{c1}{fan1} RPM{C_RESET} ({fan_w:.1f}W)"
    elif fan2 > 0:
        c2 = C_CYAN if fan2 < 2500 else (C_YELLOW if fan2 < 4000 else C_RED)
        fan_txt = f"{c2}{fan2} RPM{C_RESET} ({fan_w:.1f}W)"
    else:
        fan_txt = f"{C_DIM}0 RPM (Off/Passive){C_RESET}"

    out.append(f"Palmrest: {palm_t} | Ambient: {amb_t} | ACPI: {acpi_t} | Fan: {fan_txt}")

    # CPU Throttling Diagnostics
    th = data.get("throttle", {})
    events = int(th.get("pkg_events", 0) or 0)
    ms = int(th.get("total_time_ms", 0) or 0)
    delta_events = int(th.get("delta_events", 0) or 0)
    pkg_w = float(b.get("cpu_package_w", 0.0) or 0.0)
    pl1 = float(lim.get("pl1", 35.0) or 35.0)
    cur_temp = float(thm.get("package", 0.0) or 0.0)

    if delta_events > 0 or cur_temp >= 98.0:
        cpu_th_status = f"{C_RED}{C_BOLD}Active PROCHOT Thermal Clamp{C_RESET}"
    elif pkg_w >= pl1 + 1.0:
        cpu_th_status = f"{C_YELLOW}Active PL1 Power Clamp ({pl1:.0f}W limit){C_RESET}"
    else:
        cpu_th_status = f"{C_GREEN}Optimal (Unconstrained){C_RESET}"

    trip_col = C_GREEN if events == 0 else C_YELLOW
    left_cpu_th = f"CPU Throttle: {cpu_th_status}"
    if inner >= 86:
        right_cpu_th = f"Trips: {trip_col}{events:,}{C_RESET} events ({ms:,} ms since boot)"
    else:
        right_cpu_th = f"Trips: {trip_col}{events:,}{C_RESET} ({ms//1000:,}s)"
    out.append(split_line(left_cpu_th, right_cpu_th, inner))

    # GPU Throttling Diagnostics
    gpu = data.get("gpu", {})
    gt0_th = gpu.get("gt0", {}).get("throttles", [])
    gt1_th = gpu.get("gt1", {}).get("throttles", [])
    all_gpu_th = list(set(gt0_th + gt1_th))
    gt0_active = gpu.get("gt0", {}).get("throttle_status", False)
    gt1_active = gpu.get("gt1", {}).get("throttle_status", False)
    gpu_active = gt0_active or gt1_active

    if all_gpu_th and gpu_active:
        reasons = []
        for r in all_gpu_th:
            if r == "PL4":
                reasons.append("PL4 Peak Guard (GPU Current Spike Protection)")
            elif r == "RATL":
                reasons.append("RATL (Running Avg Temp Budget)")
            elif r == "THERMAL":
                reasons.append("Thermal Limit")
            elif r == "PROCHOT":
                reasons.append("Bi-dir PROCHOT")
            elif r == "PL1":
                reasons.append("PL1 Sustained Limit")
            elif r == "PL2":
                reasons.append("PL2 Burst Limit")
            else:
                reasons.append(r)
        gpu_th_status = f"{C_ORANGE}{', '.join(reasons)}{C_RESET}"
    elif all_gpu_th and not gpu_active:
        gpu_th_status = f"{C_GREEN}Optimal{C_RESET} {C_DIM}(transient peak limiter reset){C_RESET}"
    else:
        gpu_th_status = f"{C_GREEN}Optimal (Unconstrained / Boost Ready){C_RESET}"

    left_gpu_th = f"GPU Throttle: {gpu_th_status}"
    out.append(truncate_visible(left_gpu_th, inner))

    return out


def render_network_section(data, inner):
    out = []

    net = data.get("wireless", {})
    nio = data.get("net_io", {})

    q = net.get("quality", "N/A")
    lvl = net.get("level_dbm") or "N/A"
    ps = net.get("power_save", "N/A")

    rx_kbps = float(nio.get("rx_kbps", 0.0) or 0.0)
    tx_kbps = float(nio.get("tx_kbps", 0.0) or 0.0)
    rx_kbs = float(nio.get("rx_kbs", 0.0) or 0.0)
    tx_kbs = float(nio.get("tx_kbs", 0.0) or 0.0)

    if inner >= 82:
        if rx_kbps >= 10000.0 or tx_kbps >= 10000.0:
            speed_str = f"RX: {rx_kbps/1000.0:5.1f} Mbps ({rx_kbs/1024.0:.1f} MB/s) | TX: {tx_kbps/1000.0:5.1f} Mbps ({tx_kbs/1024.0:.1f} MB/s)"
        else:
            speed_str = f"RX: {rx_kbps:5.1f} kbps ({rx_kbs:.1f} KB/s) | TX: {tx_kbps:5.1f} kbps ({tx_kbs:.1f} KB/s)"
    else:
        if rx_kbps >= 10000.0 or tx_kbps >= 10000.0:
            speed_str = f"RX: {rx_kbps/1000.0:5.1f} Mbps | TX: {tx_kbps/1000.0:5.1f} Mbps"
        else:
            speed_str = f"RX: {rx_kbps:5.1f} kbps | TX: {tx_kbps:5.1f} kbps"

    link_str = f"Signal: {lvl} ({q}) | PwrSave: {ps}"
    out.append(split_line(link_str, speed_str, inner))

    return out


def render_battery_section(data, inner):
    out = []

    bat = data.get("battery") or {}
    ec = data.get("ec") or {}
    pwr = data.get("power_sources") or {}

    ad_str = pwr.get("description")
    if not ad_str:
        if bat.get("ac_online"):
            ad_w = ec.get("adapter_watts")
            ad_str = f"{ad_w} W (HP Smart Pin Detected)" if ad_w else "AC Connected"
        else:
            ad_str = "Unplugged (On Battery)"

    out.append(split_line("Power Source:", f"{C_BOLD}{ad_str}{C_RESET}", inner))
    out.append(split_line("Battery Cycles:", f"{C_BOLD}{bat.get('cycle_count', 'N/A')}{C_RESET}", inner))

    cap = int(bat.get("capacity", 0) or 0)
    st = str(bat.get("status", "Unknown"))
    v = float(bat.get("voltage_now", 0.0) or 0.0)
    pw = float(bat.get("power_now", 0.0) or 0.0)

    if pw > 0:
        if st.lower() == "charging":
            st_disp = f"{st} +{pw:.1f} W"
        elif st.lower() == "discharging":
            st_disp = f"{st} -{pw:.1f} W"
        else:
            st_disp = f"{st} {pw:.1f} W"
    else:
        st_disp = st

    charge_right = f"[{bar(cap, 16, C_GREEN if cap > 20 else C_RED)}] {cap:3d}% ({st_disp}) {v:.2f} V"
    out.append(split_line("Charge:", charge_right, inner))

    rem = bat.get("time_remaining_str")
    if rem:
        out.append(split_line("Runtime Estimate:", f"{C_CYAN}{C_BOLD}{rem}{C_RESET}", inner))

    health = bat.get("health_pct")
    if health is not None:
        full = float(bat.get("energy_full", 0.0) or 0.0)
        design = float(bat.get("energy_full_design", 0.0) or 0.0)
        out.append(f"Health: {float(health):.1f}% ({full:.1f} Wh / {design:.1f} Wh)")
    else:
        out.append("Health: N/A")

    return out


def render_ui(data, width=None):
    term_w = int(width if width is not None else term_columns())

    if term_w < 30:
        return f"terminal too narrow ({term_w} cols); resize to at least 30"

    w = min(max(30, term_w), 120)
    inner = w - 2

    lines = []

    platform = get_platform()
    title = f"{platform.dmi['product_name'].upper()} HARDWARE TELEMETRY MONITOR"
    uptime_fmt = data.get("uptime", {}).get("formatted", "")
    lines.append(box_top(w, title if inner >= 60 else "HP TELEMETRY"))

    bios_str = f" | BIOS {platform.dmi['bios_version']}" if platform.dmi['bios_version'] else ""
    sub_detail = f"{platform.cpu_info['clean_model']} ({platform.topology['summary_str']} {platform.cpu_info['arch']}){bios_str}"
    if uptime_fmt and inner >= 70:
        sub = f"{sub_detail} | Up: {uptime_fmt}"
    else:
        sub = sub_detail

    lines.append(box_line(pad(f"{C_DIM}{sub}{C_RESET}", inner, "center"), w))

    # 1. CPU & Cores
    lines.append(box_mid(w, f"CPU & CORES [{platform.cpu_info['clean_model']} {platform.topology['summary_str']}]"))
    for content in render_cpu_section(data, inner):
        lines.append(box_line(content, w))

    # 2. Memory & Swap
    lines.append(box_mid(w, "MEMORY & SWAP"))
    for content in render_memory_section(data, inner):
        lines.append(box_line(content, w))

    # 3. Component Power Sips & Limits
    lines.append(box_mid(w, "REAL-TIME COMPONENT POWER SIPPING (Watts)"))
    for content in render_power_section(data, inner):
        lines.append(box_line(content, w))

    # 4. Thermals & Throttling
    lines.append(box_mid(w, "THERMAL SENSORS & THROTTLING DIAGNOSTICS"))
    for content in render_thermal_section(data, inner):
        lines.append(box_line(content, w))

    # 5. Network
    lines.append(box_mid(w, "WIRELESS & NETWORK CONNECTIVITY (wlo1)"))
    for content in render_network_section(data, inner):
        lines.append(box_line(content, w))

    # 6. Battery & Power Delivery
    lines.append(box_mid(w, "BATTERY & POWER DELIVERY"))
    for content in render_battery_section(data, inner):
        lines.append(box_line(content, w))

    lines.append(box_bottom(w))

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="HP Hardware Telemetry & Component Power Dashboard"
    )
    parser.add_argument("--once", action="store_true", help="Print a single snapshot and exit")
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds (default: 1.0)"
    )
    parser.add_argument("--json", action="store_true", help="Output raw telemetry as JSON")

    args = parser.parse_args()
    args.interval = max(0.1, float(args.interval))

    collector = TelemetryCollector()

    if args.json:
        time.sleep(0.2)
        data = collector.collect()
        print(json.dumps(data, indent=2))
        return

    if args.once or not sys.stdout.isatty():
        time.sleep(0.3)
        data = collector.collect()
        print(render_ui(data, term_columns()))
        return

    # Let differential sensors get a usable first delta.
    time.sleep(0.2)

    # Alternate screen + hidden cursor for live TUI.
    sys.stdout.write("\033[?1049h\033[?25l")
    last_width = term_columns()

    try:
        while True:
            start = time.time()

            data = collector.collect()
            width = term_columns()
            last_width = width

            out = render_ui(data, width)

            sys.stdout.write("\033[H")
            sys.stdout.write(out)
            sys.stdout.write(
                f"\n{C_DIM}Live updating every {args.interval:.1f}s. "
                f"Resize handled. Ctrl+C exits.{C_RESET}\n"
            )
            sys.stdout.write("\033[J")
            sys.stdout.flush()

            deadline = start + args.interval

            # Sleep in small slices so resize is noticed quickly.
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break

                time.sleep(min(0.05, remaining))

                if term_columns() != last_width:
                    break

    except KeyboardInterrupt:
        pass

    finally:
        sys.stdout.write("\033[?25h\033[0m\033[?1049l\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
