#!/usr/bin/env python3
"""
Hardware Detection & Abstraction Layer
Platform: Universal HP Laptops & Intel Mobile Platforms
Dynamically detects DMI, CPU architecture, core topology, GPU(s), fan tachometers, and RAPL capabilities.
Zero hardcoded core counts, CPU models, GPU types, or fan numbers.
"""

import os
import glob
import subprocess
import struct
import time
import re
import multiprocessing

def read_sysfs_str(path, default=""):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
    except Exception:
        pass
    return default

def read_sysfs_int(path, default=0):
    val = read_sysfs_str(path, "")
    if val:
        try:
            return int(val)
        except Exception:
            pass
    return default

def parse_cpu_list(s):
    """Parses Linux CPU list format like '0,5' or '1-3,8-11' into a sorted list of ints."""
    res = []
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                lo, hi = map(int, part.split('-'))
                res.extend(range(lo, hi + 1))
            except Exception:
                pass
        else:
            try:
                res.append(int(part))
            except Exception:
                pass
    return sorted(list(set(res)))

def rdmsr(cpu, msr):
    """Reads 64-bit MSR via /dev/cpu/<cpu>/msr."""
    try:
        p = f"/dev/cpu/{cpu}/msr"
        if not os.path.exists(p):
            subprocess.run(["modprobe", "msr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(p, "rb") as f:
            f.seek(msr)
            return struct.unpack("Q", f.read(8))[0]
    except Exception:
        return None

class HardwarePlatform:
    """Dynamically detects all system hardware components."""

    def __init__(self):
        self.dmi = self._detect_dmi()
        self.cpu_info = self._detect_cpu_info()
        self.topology = self._detect_topology()
        self.gpus = self._detect_gpus()
        self.memory = self._detect_memory()
        self.fans = self._detect_fans()
        self.rapl = self._detect_rapl()

    def _detect_dmi(self):
        vendor = read_sysfs_str("/sys/class/dmi/id/sys_vendor", "HP")
        product = read_sysfs_str("/sys/class/dmi/id/product_name", "HP Laptop")
        version = read_sysfs_str("/sys/class/dmi/id/product_version", "")
        board = read_sysfs_str("/sys/class/dmi/id/board_name", "")
        bios = read_sysfs_str("/sys/class/dmi/id/bios_version", "")
        chassis = read_sysfs_str("/sys/class/dmi/id/chassis_type", "")

        is_hp = any(x in vendor.lower() for x in ["hp", "hewlett-packard"])

        return {
            "vendor": vendor,
            "product_name": product if product else "HP Laptop",
            "product_version": version,
            "board_name": board,
            "bios_version": bios,
            "chassis_type": chassis,
            "is_hp": is_hp,
        }

    def _detect_cpu_info(self):
        raw_model = "Intel Processor"
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if line.startswith("model name"):
                        raw_model = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass

        clean_model = raw_model
        for prefix in ["Intel(R) Core(TM) ", "Intel(R) ", "(R)", "(TM)"]:
            clean_model = clean_model.replace(prefix, "")
        clean_model = clean_model.strip()

        # Architecture heuristics
        lower_name = raw_model.lower()
        if "ultra" in lower_name:
            arch = "Meteor Lake"
        elif any(f"14{x:02d}" in lower_name for x in range(100)):
            arch = "Raptor Lake Refresh"
        elif any(f"13{x:02d}" in lower_name for x in range(100)):
            arch = "Raptor Lake"
        elif any(f"12{x:02d}" in lower_name for x in range(100)):
            arch = "Alder Lake"
        elif any(f"11{x:02d}" in lower_name for x in range(100)):
            arch = "Tiger Lake"
        elif any(f"10{x:02d}" in lower_name for x in range(100)):
            arch = "Comet / Ice Lake"
        elif any(f"{x:02d}00" in lower_name for x in range(5, 10)):
            arch = "Core i-Series"
        else:
            arch = "Intel Mobile"

        return {
            "raw_model": raw_model,
            "clean_model": clean_model,
            "arch": arch,
        }

    def _detect_topology(self):
        cpu_dirs = sorted(
            glob.glob("/sys/devices/system/cpu/cpu[0-9]*"),
            key=lambda x: int(os.path.basename(x)[3:])
        )
        total_threads = len(cpu_dirs)
        threads_info = {}

        for p in cpu_dirs:
            cid = int(os.path.basename(p)[3:])
            sib_file = os.path.join(p, "topology/thread_siblings_list")
            sibs = parse_cpu_list(read_sysfs_str(sib_file, str(cid))) if os.path.exists(sib_file) else [cid]
            core_id_file = os.path.join(p, "topology/core_id")
            core_id = read_sysfs_int(core_id_file, cid)
            freq_file = os.path.join(p, "cpufreq/cpuinfo_max_freq")
            max_mhz = read_sysfs_int(freq_file, 0) // 1000
            min_freq_file = os.path.join(p, "cpufreq/cpuinfo_min_freq")
            min_mhz = read_sysfs_int(min_freq_file, 0) // 1000
            threads_info[cid] = {
                "core_id": core_id,
                "siblings": sibs,
                "max_mhz": max_mhz,
                "min_mhz": min_mhz,
                "path": p,
            }

        # Deduplicate physical cores by siblings tuple
        phys_cores = {}
        for cid, info in threads_info.items():
            s_tuple = tuple(info["siblings"])
            if s_tuple not in phys_cores:
                phys_cores[s_tuple] = {
                    "threads": info["siblings"],
                    "primary": info["siblings"][0],
                    "core_id": info["core_id"],
                    "max_mhz": info["max_mhz"],
                    "min_mhz": info["min_mhz"],
                    "smt": len(info["siblings"]) > 1,
                }

        # Determine frequency clusters
        distinct_max_freqs = sorted(list(set(c["max_mhz"] for c in phys_cores.values())), reverse=True)
        distinct_max_freqs = [f for f in distinct_max_freqs if f > 0]
        if not distinct_max_freqs:
            distinct_max_freqs = [3000]

        p_cores = []
        e_cores = []
        lp_cores = []

        if len(distinct_max_freqs) >= 3:
            # 3 tiers: P-Cores, E-Cores, LP-Cores (e.g. Meteor Lake 3-tier)
            freq_p = distinct_max_freqs[0]
            freq_e = distinct_max_freqs[1]
            p_idx = 0
            e_idx = 0
            lp_idx = 0
            for s_tuple, c in sorted(phys_cores.items(), key=lambda item: item[1]["primary"]):
                if c["max_mhz"] >= freq_p:
                    p_cores.append({**c, "name": f"P{p_idx}", "idx": p_idx, "cluster": "P"})
                    p_idx += 1
                elif c["max_mhz"] >= freq_e:
                    e_cores.append({**c, "name": f"E{e_idx}", "idx": e_idx, "cluster": "E"})
                    e_idx += 1
                else:
                    lp_cores.append({**c, "name": f"LP{lp_idx}", "idx": lp_idx, "cluster": "LP"})
                    lp_idx += 1
        elif len(distinct_max_freqs) == 2:
            # 2 tiers: P-Cores & E-Cores (e.g. Alder Lake / Raptor Lake)
            freq_p = distinct_max_freqs[0]
            p_idx = 0
            e_idx = 0
            for s_tuple, c in sorted(phys_cores.items(), key=lambda item: item[1]["primary"]):
                if c["max_mhz"] >= freq_p:
                    p_cores.append({**c, "name": f"P{p_idx}", "idx": p_idx, "cluster": "P"})
                    p_idx += 1
                else:
                    e_cores.append({**c, "name": f"E{e_idx}", "idx": e_idx, "cluster": "E"})
                    e_idx += 1
        else:
            # 1 tier: Homogeneous cores (standard multi-core)
            p_idx = 0
            for s_tuple, c in sorted(phys_cores.items(), key=lambda item: item[1]["primary"]):
                p_cores.append({**c, "name": f"Core {p_idx}", "idx": p_idx, "cluster": "Standard"})
                p_idx += 1

        total_physical = len(phys_cores)
        has_e = len(e_cores) > 0
        has_lp = len(lp_cores) > 0
        has_hybrid = has_e or has_lp
        has_smt = any(c["smt"] for c in phys_cores.values())

        max_p_mhz = max([c["max_mhz"] for c in p_cores], default=4000)
        max_e_mhz = max([c["max_mhz"] for c in e_cores], default=3000) if has_e else 0
        max_lp_mhz = max([c["max_mhz"] for c in lp_cores], default=2000) if has_lp else 0

        summary_str = f"{total_physical}C/{total_threads}T"

        return {
            "total_threads": total_threads,
            "total_physical": total_physical,
            "summary_str": summary_str,
            "threads_info": threads_info,
            "physical_cores": phys_cores,
            "p_cores": p_cores,
            "e_cores": e_cores,
            "lp_cores": lp_cores,
            "has_hybrid": has_hybrid,
            "has_smt": has_smt,
            "has_e": has_e,
            "has_lp": has_lp,
            "max_p_mhz": max_p_mhz,
            "max_e_mhz": max_e_mhz,
            "max_lp_mhz": max_lp_mhz,
        }

    def _detect_gpus(self):
        detected = []
        for card_path in sorted(glob.glob("/sys/class/drm/card[0-9]")):
            c_name = os.path.basename(card_path)
            vendor_id = read_sysfs_str(f"{card_path}/device/vendor", "").lower()
            device_id = read_sysfs_str(f"{card_path}/device/device", "").lower()

            vendor_name = "Unknown"
            if "0x8086" in vendor_id:
                vendor_name = "Intel"
            elif "0x10de" in vendor_id:
                vendor_name = "NVIDIA"
            elif "0x1002" in vendor_id:
                vendor_name = "AMD"

            # Check for multiple GT tiles (e.g. gt0 and gt1 on Meteor Lake)
            has_gt0 = os.path.exists(f"{card_path}/gt/gt0")
            has_gt1 = os.path.exists(f"{card_path}/gt/gt1")

            detected.append({
                "card": c_name,
                "path": card_path,
                "vendor_id": vendor_id,
                "device_id": device_id,
                "vendor_name": vendor_name,
                "has_gt0": has_gt0,
                "has_gt1": has_gt1,
            })

        # Name heuristic
        lspci_out = ""
        try:
            res = subprocess.run(["lspci"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1.0)
            lspci_out = res.stdout
        except Exception:
            pass

        primary_name = "GPU"
        for line in lspci_out.splitlines():
            if any(k in line.lower() for k in ["vga", "3d", "display"]):
                # Extract clean name
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    primary_name = parts[2].strip()
                    # Shorten brackets if present
                    m = re.search(r"\[(.*?)\]", primary_name)
                    if m:
                        primary_name = m.group(1)
                    break

        return {
            "list": detected,
            "primary_name": primary_name,
            "has_dual_gt": any(d["has_gt1"] for d in detected),
        }

    def _detect_memory(self):
        total_kb = 0
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                        break
        except Exception:
            pass
        total_gib = round(total_kb / (1024 * 1024), 1)
        return {
            "total_gib": total_gib,
            "total_kb": total_kb,
        }

    def _detect_fans(self):
        # Scan hwmon fan inputs
        hwmon_fans = []
        for p in sorted(glob.glob("/sys/class/hwmon/hwmon*/fan*_input")):
            # Get hwmon chip name
            hdir = os.path.dirname(p)
            chip_name = read_sysfs_str(f"{hdir}/name", "hwmon")
            fname = os.path.basename(p)
            hwmon_fans.append({
                "path": p,
                "chip": chip_name,
                "file": fname,
            })

        ec_available = os.path.exists("/sys/kernel/debug/ec/ec0/io") or os.path.exists("/dev/shm/ec0_raw") or os.path.exists("/usr/local/bin/hp-ec-dump")

        return {
            "hwmon_fans": hwmon_fans,
            "ec_available": ec_available,
        }

    def _detect_rapl(self):
        msr_path = "/sys/class/powercap/intel-rapl:0"
        mmio_path = "/sys/class/powercap/intel-rapl-mmio:0"
        has_msr = os.path.exists(msr_path)
        has_mmio = os.path.exists(mmio_path)

        has_raw_msr = False
        if os.path.exists("/dev/cpu/0/msr"):
            has_raw_msr = True
        else:
            try:
                subprocess.run(["modprobe", "msr"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                has_raw_msr = os.path.exists("/dev/cpu/0/msr")
            except Exception:
                pass

        return {
            "has_msr": has_msr,
            "has_mmio": has_mmio,
            "has_raw_msr": has_raw_msr,
            "msr_dir": msr_path,
            "mmio_dir": mmio_path,
        }

    def _read_raw_ec(self):
        shm_path = "/dev/shm/ec0_raw"
        if os.path.exists(shm_path):
            try:
                if time.time() - os.path.getmtime(shm_path) < 1.5:
                    with open(shm_path, "rb") as f:
                        data = f.read(256)
                        if len(data) >= 0xB4:
                            return data
            except Exception:
                pass

        if os.path.exists("/usr/local/bin/hp-ec-dump"):
            try:
                res = subprocess.run(["/usr/local/bin/hp-ec-dump"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=0.5)
                if len(res.stdout) >= 0xB4:
                    return res.stdout
            except Exception:
                pass

        ec_io = "/sys/kernel/debug/ec/ec0/io"
        if os.path.exists(ec_io):
            try:
                with open(ec_io, "rb") as f:
                    return f.read(256)
            except Exception:
                pass

        return None

    def read_all_fans(self):
        """Reads all fan tachometers dynamically, returning a list of fan dicts."""
        fans = []

        # 1. Try hp-wmi hwmon fans
        hp_wmi_fans = sorted(glob.glob("/sys/devices/platform/hp-wmi/hwmon/hwmon*/fan*_input"))
        if hp_wmi_fans:
            for idx, p in enumerate(hp_wmi_fans):
                rpm = read_sysfs_int(p, 0)
                if rpm > 0:
                    fans.append({"idx": idx + 1, "rpm": rpm, "source": "hp-wmi"})

        # 2. Try EC directly if hp-wmi returned 0 or no fans
        if not fans:
            raw = self._read_raw_ec()
            if raw and len(raw) >= 0xB4:
                f1 = (raw[0xB1] << 8) | raw[0xB0]
                f2 = (raw[0xB3] << 8) | raw[0xB2]
                if f1 > 0:
                    fans.append({"idx": 1, "rpm": f1, "source": "EC0"})
                if f2 > 0:
                    fans.append({"idx": 2, "rpm": f2, "source": "EC0"})

        # 3. Try any generic hwmon fans
        if not fans:
            for idx, f_entry in enumerate(self.fans["hwmon_fans"]):
                rpm = read_sysfs_int(f_entry["path"], 0)
                if rpm > 0:
                    fans.append({"idx": idx + 1, "rpm": rpm, "source": f_entry["chip"]})

        return fans

    def read_fan_rpm(self):
        """Returns (fan1_rpm, fan2_rpm, source_label)."""
        fans = self.read_all_fans()
        if not fans:
            return 0, 0, "Passive / ACPI"
        f1 = fans[0]["rpm"] if len(fans) > 0 else 0
        f2 = fans[1]["rpm"] if len(fans) > 1 else 0
        src = fans[0]["source"]
        return f1, f2, src

    # Dynamic Core Parking Engine
    def get_parking_modes(self):
        """Generates list of (mode_key, label_str) dynamically based on actual hardware topology."""
        top = self.topology
        modes = []

        # 1. All cores online
        modes.append(("none", f"All Cores Online ({top['summary_str']})"))

        # 2. SMT toggle
        if top["has_smt"]:
            modes.append(("no_smt", f"Disable SMT ({top['total_physical']} Threads Active)"))

        # 3. Hybrid cluster options
        if top["has_hybrid"]:
            p_cnt = len(top["p_cores"])
            p_threads = sum(len(c["threads"]) for c in top["p_cores"])
            modes.append(("pcores_only", f"P-Cores Only ({p_cnt}P / {p_threads}T Active)"))

            if top["has_lp"]:
                lp_cnt = len(top["lp_cores"])
                modes.append(("pcores_lp", f"P-Cores + LP Island ({p_cnt}P + {lp_cnt}LP)"))
                modes.append(("lp_only", f"Low-Power Island Only (CPU 0 + {lp_cnt}LP)"))

            if p_cnt >= 2:
                modes.append(("single_pcore", f"Single P-Core + Efficient Cores"))
                modes.append(("dual_pcore", f"Dual P-Core + Efficient Cores"))
            modes.append(("cluster", f"Compact Cluster Parking"))
        else:
            # Non-hybrid: standard multi-core
            if top["total_physical"] >= 4:
                modes.append(("half_cores", f"Half Cores ({top['total_physical']//2} Cores Active)"))
            if top["total_physical"] >= 2:
                modes.append(("single_core", f"Single Core (Maximum Battery)"))

        modes.append(("custom", "Custom Topology (Individual Toggles)"))
        return modes

    def get_park_offline_cpus(self, mode_key):
        """Calculates the exact set of CPU IDs to offline for a given mode dynamically."""
        top = self.topology
        all_cpus = set(range(top["total_threads"]))
        offline_set = set()

        if mode_key == "none":
            return []

        elif mode_key in ["no_smt", "smt_off", "nosmt"]:
            # Offline sibling threads for physical cores that support SMT
            for c in top["physical_cores"].values():
                if c["smt"]:
                    for s in c["threads"][1:]:
                        if s != 0:
                            offline_set.add(s)

        elif mode_key in ["pcores_only", "p_only"]:
            if top["has_hybrid"]:
                # Offline all E and LP threads
                for c in top["e_cores"]:
                    for t in c["threads"]:
                        if t != 0:
                            offline_set.add(t)
                for c in top["lp_cores"]:
                    for t in c["threads"]:
                        if t != 0:
                            offline_set.add(t)

        elif mode_key in ["pcores_lp", "p_lp"]:
            if top["has_hybrid"]:
                # Offline all E threads
                for c in top["e_cores"]:
                    for t in c["threads"]:
                        if t != 0:
                            offline_set.add(t)

        elif mode_key in ["single_pcore", "1p_8e", "pcores"]:
            if top["has_hybrid"]:
                # Keep P0 primary (CPU 0) online; offline P0 secondary and all other P-cores
                for c in top["p_cores"]:
                    for t in c["threads"]:
                        if t != 0:
                            offline_set.add(t)

        elif mode_key in ["dual_pcore", "2p_8e"]:
            if top["has_hybrid"] and len(top["p_cores"]) >= 2:
                # Keep P0 and P1 online; offline P2, P3, ...
                for c in top["p_cores"][2:]:
                    for t in c["threads"]:
                        if t != 0:
                            offline_set.add(t)

        elif mode_key in ["cluster", "7c"]:
            if top["has_hybrid"]:
                # Keep P0 primary; offline P0 secondary and all other P-cores
                for c in top["p_cores"]:
                    for t in c["threads"]:
                        if t != 0:
                            offline_set.add(t)
                # Offline second half of E-cores
                e_half = len(top["e_cores"]) // 2
                for c in top["e_cores"][e_half:]:
                    for t in c["threads"]:
                        if t != 0:
                            offline_set.add(t)

        elif mode_key in ["lp_only", "lponly", "aggressive"]:
            if top["has_lp"]:
                # Keep CPU 0 and LP cores online; offline everything else
                lp_threads = set()
                for c in top["lp_cores"]:
                    lp_threads.update(c["threads"])
                for cpu_id in all_cpus:
                    if cpu_id != 0 and cpu_id not in lp_threads:
                        offline_set.add(cpu_id)

        elif mode_key == "half_cores":
            # Non-hybrid: offline second half of physical cores
            phys_list = sorted(list(top["physical_cores"].values()), key=lambda x: x["primary"])
            half_pt = max(1, len(phys_list) // 2)
            for c in phys_list[half_pt:]:
                for t in c["threads"]:
                    if t != 0:
                        offline_set.add(t)

        elif mode_key == "single_core":
            for cpu_id in all_cpus:
                if cpu_id != 0:
                    offline_set.add(cpu_id)

        return sorted(list(offline_set))

_platform_instance = None
def get_platform():
    global _platform_instance
    if _platform_instance is None:
        _platform_instance = HardwarePlatform()
    return _platform_instance
