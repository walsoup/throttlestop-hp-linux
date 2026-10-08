#!/usr/bin/env python3
"""
HP BIOS Fan Curve & Thermal Profiler
Benchmark and mapping tool for HP Laptop 15-fd1xxx

Progressively stresses the Intel Core Ultra 5 125H while sampling:
- Real-time Fan RPM directly from the Embedded Controller (EC0)
- Intel RAPL Package Wattage
- CPU Package & Core Temperatures
- Hardware Thermal Throttling Events

Generates:
1. Live real-time terminal telemetry during test
2. Summary of fan kick-in & shutoff thresholds (hysteresis)
3. Step-by-step RPM levels
4. ASCII graph of Temperature vs Fan RPM
5. Detailed CSV export (fan_curve_profile.csv)
"""

import sys
import os
import time
import csv
import math
import argparse
import multiprocessing
import subprocess

try:
    from hardware import get_platform
except ImportError:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)
    from hardware import get_platform

EC_IO_PATH = "/sys/kernel/debug/ec/ec0/io"

# Colors
C_RESET  = "\033[0m"
C_BOLD   = "\033[1m"
C_DIM    = "\033[2m"
C_RED    = "\033[31m"
C_GREEN  = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE   = "\033[34m"
C_MAGENTA= "\033[35m"
C_CYAN   = "\033[36m"
C_WHITE  = "\033[37m"

def color_temp(t):
    if t < 55: return f"{C_GREEN}{t:4.1f}°C{C_RESET}"
    elif t < 75: return f"{C_YELLOW}{t:4.1f}°C{C_RESET}"
    elif t < 90: return f"\033[38;5;208m{t:4.1f}°C{C_RESET}"
    else: return f"{C_RED}{C_BOLD}{t:4.1f}°C{C_RESET}"

def color_watts(w):
    if w < 15: return f"{C_GREEN}{w:5.1f}W{C_RESET}"
    elif w < 35: return f"{C_YELLOW}{w:5.1f}W{C_RESET}"
    else: return f"{C_RED}{C_BOLD}{w:5.1f}W{C_RESET}"

def color_rpm(rpm):
    if rpm == 0: return f"{C_DIM}0 RPM (Idle/Off){C_RESET}"
    elif rpm < 2600: return f"{C_CYAN}{rpm} RPM (Level 1){C_RESET}"
    elif rpm < 3800: return f"{C_YELLOW}{rpm} RPM (Level 2){C_RESET}"
    else: return f"{C_RED}{C_BOLD}{rpm} RPM (Level 3/Max){C_RESET}"

def stress_worker(stop_flag):
    # Compute intensive loop
    while not stop_flag.is_set():
        _ = [x * x for x in range(2000)]

class HardwareSampler:
    def __init__(self):
        self._ensure_ec()
        self.last_rapl_time = time.time()
        self.last_energy = self._read_energy()

    def _ensure_ec(self):
        if not os.path.exists(EC_IO_PATH):
            subprocess.run(["modprobe", "ec_sys"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _read_energy(self):
        p = "/sys/class/powercap/intel-rapl:0/energy_uj"
        try:
            with open(p) as f:
                return int(f.read().strip())
        except Exception:
            return 0

    def sample(self):
        now = time.time()
        # 1. RAPL Watts
        e_now = self._read_energy()
        dt = now - self.last_rapl_time
        watts = 0.0
        if dt > 0.05 and e_now >= self.last_energy:
            watts = (e_now - self.last_energy) / dt / 1e6
        self.last_rapl_time = now
        self.last_energy = e_now

        # 2. Fan RPM & EC Temperature
        fan1, fan2, src = get_platform().read_fan_rpm()
        fan_rpm = fan1 if fan1 > 0 else fan2
        ec_temp = 0
        raw = get_platform()._read_raw_ec()
        if raw and len(raw) >= 0x60:
            ec_temp = raw[0x58]

        # 3. CPU Package & Discrete Temperatures
        pkg_temp = 0.0
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                raw_t = int(f.read().strip())
                pkg_temp = raw_t / 1000.0 if raw_t > 200 else float(raw_t)
        except Exception:
            pkg_temp = float(ec_temp)

        # 4. Throttling count
        throttled = False
        th_events = 0
        try:
            with open("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count") as f:
                th_events = int(f.read().strip())
        except Exception:
            pass

        return {
            "timestamp": now,
            "watts": watts,
            "pkg_temp": pkg_temp,
            "ec_temp": ec_temp,
            "fan_rpm": fan_rpm,
            "throttle_events": th_events
        }

def render_ascii_curve(history):
    # Filter points where temperature and RPM exist
    valid = [h for h in history if h["pkg_temp"] > 0]
    if not valid:
        return "No data to plot."

    min_t = min(h["pkg_temp"] for h in valid)
    max_t = max(h["pkg_temp"] for h in valid)
    max_rpm = max(h["fan_rpm"] for h in valid)
    if max_rpm == 0:
        max_rpm = 4000

    min_t_round = int(math.floor(min_t / 5.0) * 5)
    max_t_round = int(math.ceil(max_t / 5.0) * 5)
    if max_t_round - min_t_round < 10:
        max_t_round = min_t_round + 20

    plot_w = 48
    plot_h = 12

    grid = [[" " for _ in range(plot_w)] for _ in range(plot_h)]

    for h in valid:
        t = h["pkg_temp"]
        rpm = h["fan_rpm"]
        direction = h.get("phase", "heat")

        # Map T to X
        x = int(((t - min_t_round) / (max_t_round - min_t_round)) * (plot_w - 1))
        x = max(0, min(plot_w - 1, x))

        # Map RPM to Y (0 at bottom, plot_h-1 at top)
        y = int((rpm / max_rpm) * (plot_h - 1))
        y = max(0, min(plot_h - 1, y))
        y_inv = (plot_h - 1) - y

        char = "▲" if direction == "heat" else "▼"
        color = C_YELLOW if direction == "heat" else C_CYAN
        grid[y_inv][x] = f"{color}{char}{C_RESET}"

    out = []
    out.append(f"  {C_BOLD}RPM vs. Temperature Hysteresis Map{C_RESET} ({C_YELLOW}▲ Heat-up{C_RESET} / {C_CYAN}▼ Cool-down{C_RESET})")
    for r in range(plot_h):
        rpm_val = int(max_rpm * (plot_h - 1 - r) / (plot_h - 1))
        row_str = "".join(grid[r])
        out.append(f"  {rpm_val:4d} ┤{row_str}│")
    
    # X axis
    out.append(f"       └{'─' * plot_w}┘")
    t_labels = f"        {min_t_round:.0f}°C"
    spacing = plot_w - len(f"{min_t_round:.0f}°C") - len(f"{max_t_round:.0f}°C") - 4
    t_labels += " " * max(1, spacing) + f"{max_t_round:.0f}°C"
    out.append(t_labels)
    return "\n".join(out)

def run_benchmark(quick=False):
    if os.geteuid() != 0:
        print(f"{C_RED}Error: Profiling fan speeds requires root privileges to read the Embedded Controller.{C_RESET}")
        print("Please re-run with: sudo /home/wal/work/fan/fan_profiler.py")
        sys.exit(1)

    sampler = HardwareSampler()
    
    # Timing stages
    if quick:
        stages = [
            ("Idle Baseline", 0, 5, "Measuring resting state"),
            ("Light Load", 4, 10, "Warming up (4 threads)"),
            ("Medium Load", 8, 12, "Mid-range ramp (8 threads)"),
            ("Peak Load", 18, 15, "Full Turbo stress (all 18 threads)"),
            ("Cool-down", 0, 20, "Monitoring fan hysteresis & recovery")
        ]
    else:
        stages = [
            ("Idle Baseline", 0, 8, "Measuring resting state"),
            ("Light Load", 4, 15, "Warming up (4 threads)"),
            ("Medium Load", 8, 18, "Mid-range ramp (8 threads)"),
            ("Peak Load", 18, 25, "Full Turbo stress (all 18 threads)"),
            ("Cool-down", 0, 30, "Monitoring fan hysteresis & recovery")
        ]

    total_duration = sum(s[2] for s in stages)

    platform = get_platform()
    hw_str = f"Hardware: {platform.cpu_info['clean_model']} ({platform.topology['summary_str']}) | Motherboard {platform.dmi['board_name'] or 'HP'}"
    print(f"{C_BOLD}{C_CYAN}╔{'═' * 70}╗{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}║{C_WHITE}      HP LAPTOP BIOS FAN CURVE & THERMAL STRESS PROFILER             {C_CYAN}║{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}║{C_DIM}      {hw_str:<63}{C_CYAN}║{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}╚{'═' * 70}╝{C_RESET}\n")

    print(f"Profiling plan: {len(stages)} stages, ~{total_duration} seconds total duration.")
    print("Press Ctrl+C at any time to abort safely.\n")

    history = []
    t_start = time.time()
    
    initial_sample = sampler.sample()
    init_th_count = initial_sample["throttle_events"]
    
    fan_kick_temp = None
    fan_stop_temp = None
    observed_rpms = set()

    for stage_name, num_threads, duration, desc in stages:
        phase = "cool" if stage_name == "Cool-down" else "heat"
        print(f"\n{C_BOLD}▶ STAGE: {C_YELLOW}{stage_name}{C_RESET} ({desc}) [{duration}s]")
        
        # Launch workers if load needed
        workers = []
        stop_event = multiprocessing.Event()
        if num_threads > 0:
            for _ in range(num_threads):
                p = multiprocessing.Process(target=stress_worker, args=(stop_event,))
                p.start()
                workers.append(p)

        stage_end = time.time() + duration
        try:
            while time.time() < stage_end:
                time.sleep(0.5)
                sample = sampler.sample()
                sample["stage"] = stage_name
                sample["phase"] = phase
                sample["elapsed"] = time.time() - t_start
                history.append(sample)

                rpm = sample["fan_rpm"]
                temp = sample["pkg_temp"]
                watts = sample["watts"]

                if rpm > 0:
                    observed_rpms.add(rpm)
                    if fan_kick_temp is None and phase == "heat":
                        fan_kick_temp = temp

                if phase == "cool" and rpm == 0 and fan_stop_temp is None and fan_kick_temp is not None:
                    fan_stop_temp = temp

                # Live status line
                bar_str = "█" * int(min(18, (rpm / 5000) * 18)) if rpm > 0 else "░" * 18
                th_diff = sample["throttle_events"] - init_th_count
                th_badge = f"{C_RED}[THROTTLING]{C_RESET}" if th_diff > 0 else ""
                
                status = f"\r  ⏱  {sample['elapsed']:4.1f}s | Pkg: {color_temp(temp)} | Power: {color_watts(watts)} | Fan: {color_rpm(rpm):<28} [{C_CYAN}{bar_str:<18}{C_RESET}] {th_badge}"
                sys.stdout.write(status)
                sys.stdout.flush()

        finally:
            if workers:
                stop_event.set()
                for p in workers:
                    p.join(timeout=1.0)
                    if p.is_alive():
                        p.terminate()

    print("\n\n" + f"{C_BOLD}{C_GREEN}✔ Test Complete! Compiling thermal analysis...{C_RESET}\n")

    # Export to CSV
    csv_path = "/home/wal/work/fan/fan_curve_profile.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Elapsed_Sec", "Stage", "Phase", "Package_Temp_C", "EC_Temp_C", "RAPL_Watts", "Fan_RPM", "Throttle_Events"])
        for h in history:
            writer.writerow([
                f"{h['elapsed']:.2f}",
                h["stage"],
                h["phase"],
                f"{h['pkg_temp']:.1f}",
                h["ec_temp"],
                f"{h['watts']:.2f}",
                h["fan_rpm"],
                h["throttle_events"]
            ])

    # Analysis
    max_temp = max(h["pkg_temp"] for h in history)
    max_watts = max(h["watts"] for h in history)
    max_rpm = max(h["fan_rpm"] for h in history)
    total_throttles = history[-1]["throttle_events"] - init_th_count

    # Cluster observed RPMs into steps (within +/- 150 RPM)
    sorted_rpms = sorted(list(observed_rpms))
    steps = []
    for r in sorted_rpms:
        if not steps or r - steps[-1][-1] > 200:
            steps.append([r])
        else:
            steps[-1].append(r)
    rpm_levels = [int(sum(cluster) / len(cluster)) for cluster in steps]

    w_box = 72
    print(f"{C_BOLD}{C_CYAN}┌{'─' * (w_box - 2)}┐{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}│{C_WHITE}{' HP BIOS THERMAL BEHAVIOR SUMMARY ':^{w_box-2}}{C_CYAN}│{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}├{'─' * (w_box - 2)}┤{C_RESET}")
    
    kick_str = f"{fan_kick_temp:.1f}°C" if fan_kick_temp else "Fan was already running"
    print(f"{C_CYAN}│{C_RESET}  Fan Activation Temp (Kick-in):     {C_BOLD}{kick_str:<32}{C_RESET} {C_CYAN}│{C_RESET}")
    
    stop_str = f"{fan_stop_temp:.1f}°C" if fan_stop_temp else "Did not reach idle stop within window"
    print(f"{C_CYAN}│{C_RESET}  Fan Deactivation Temp (Shutoff):    {C_BOLD}{stop_str:<32}{C_RESET} {C_CYAN}│{C_RESET}")

    if fan_kick_temp and fan_stop_temp:
        hys = abs(fan_kick_temp - fan_stop_temp)
        print(f"{C_CYAN}│{C_RESET}  Hysteresis Delta:                  {C_BOLD}{f'{hys:.1f}°C delay to prevent oscillation':<32}{C_RESET} {C_CYAN}│{C_RESET}")

    print(f"{C_CYAN}│{C_RESET}  Observed Fan Steps / Tiers:        {C_BOLD}{f'{len(rpm_levels)} distinct speed tiers':<32}{C_RESET} {C_CYAN}│{C_RESET}")
    for idx, lvl in enumerate(rpm_levels):
        tier_lbl = f"Tier {idx + 1}: ~{lvl} RPM"
        print(f"{C_CYAN}│{C_RESET}    ├─ {tier_lbl:<63} {C_CYAN}│{C_RESET}")

    print(f"{C_CYAN}│{C_RESET}  Peak Package Temperature:          {color_temp(max_temp):<40} {C_CYAN}│{C_RESET}")
    print(f"{C_CYAN}│{C_RESET}  Peak Package Power (PL2 Burst):    {color_watts(max_watts):<40} {C_CYAN}│{C_RESET}")
    print(f"{C_CYAN}│{C_RESET}  Maximum Fan Speed Achieved:        {color_rpm(max_rpm):<40} {C_CYAN}│{C_RESET}")
    
    th_stat = f"{C_RED}{total_throttles} events{C_RESET}" if total_throttles > 0 else f"{C_GREEN}None (Clean thermal headroom){C_RESET}"
    print(f"{C_CYAN}│{C_RESET}  Thermal Throttling Trip Events:    {th_stat:<40} {C_CYAN}│{C_RESET}")
    print(f"{C_CYAN}│{C_RESET}  Raw Data Logged To:                {C_DIM}{csv_path:<32}{C_RESET} {C_CYAN}│{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}└{'─' * (w_box - 2)}┘{C_RESET}\n")

    # Display Graph
    print(render_ascii_curve(history))
    print(f"\n{C_DIM}Tip: You can inspect the complete time-series data at {csv_path}{C_RESET}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HP BIOS Fan Curve & Thermal Stress Profiler")
    parser.add_argument("--quick", action="store_true", help="Run a faster ~45s benchmark instead of the full ~80s test")
    args = parser.parse_args()
    run_benchmark(quick=args.quick)
