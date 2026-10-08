#!/usr/bin/env python3
"""
HP Laptop Fan Speed & EC Monitor
Reads fan speeds from HP-WMI hwmon or directly from the Embedded Controller (EC0).
Works across HP Omen, Victus, Pavilion, Envy, EliteBook, and 15-series laptops.
"""

import sys
import os
import time
import argparse
import json
import subprocess

try:
    from hardware import get_platform
except ImportError:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)
    from hardware import get_platform

def dump_ec_table(raw_ec):
    """Outputs a formatted 16x16 hex dump of EC memory (256 bytes)."""
    print("      00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F")
    print("   -------------------------------------------------")
    for row in range(0, 256, 16):
        hex_vals = " ".join(f"{raw_ec[row + col]:02X}" for col in range(8))
        hex_vals2 = " ".join(f"{raw_ec[row + col]:02X}" for col in range(8, 16))
        ascii_chars = "".join(chr(raw_ec[row + col]) if 32 <= raw_ec[row + col] <= 126 else "." for col in range(16))
        print(f"{row:02X}:  {hex_vals}  {hex_vals2}  |{ascii_chars}|")

def main():
    parser = argparse.ArgumentParser(description="HP Laptop Fan Speed & Embedded Controller Utility")
    parser.add_argument("-m", "--monitor", action="store_true", help="Continuously monitor fan speed in real time")
    parser.add_argument("-i", "--interval", type=float, default=1.0, help="Sampling interval in seconds (default: 1.0)")
    parser.add_argument("-j", "--json", action="store_true", help="Output telemetry in JSON format")
    parser.add_argument("--dump-ec", action="store_true", help="Display 256-byte hex dump of Embedded Controller (EC) memory")
    args = parser.parse_args()

    platform = get_platform()

    if args.dump_ec:
        raw = platform._read_raw_ec()
        if not raw:
            print("Error: Could not read Embedded Controller. Ensure 'ec_sys' module is loaded or run with root permissions.", file=sys.stderr)
            sys.exit(1)
        print(f"--- HP Embedded Controller (EC0) Raw Dump [{platform.dmi['product_name']}] ---")
        dump_ec_table(raw)
        f1, f2, src = platform.read_fan_rpm()
        print(f"\nTachometer interpretation: Fan 1 = {f1} RPM, Fan 2 = {f2} RPM ({src})")
        sys.exit(0)

    if args.json:
        f1, f2, src = platform.read_fan_rpm()
        out = {
            "model": platform.dmi["product_name"],
            "board": platform.dmi["board_name"],
            "fan1_rpm": f1,
            "fan2_rpm": f2,
            "source": src,
            "timestamp": time.time(),
        }
        print(json.dumps(out, indent=2))
        return

    if args.monitor:
        print(f"Monitoring fan speed on {platform.dmi['product_name']} ({platform.dmi['board_name']})... [Press Ctrl+C to exit]\n")
        try:
            while True:
                f1, f2, src = platform.read_fan_rpm()
                fan2_str = f" | Fan 2: {f2:4d} RPM" if f2 > 0 else ""
                t_str = time.strftime("%H:%M:%S")
                print(f"\r[{t_str}] Fan 1: {f1:4d} RPM{fan2_str}  (Source: {src})", end="", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return

    # One-shot report
    f1, f2, src = platform.read_fan_rpm()
    print(f"Platform: {platform.dmi['product_name']} (Board ID: {platform.dmi['board_name']}, BIOS: {platform.dmi['bios_version']})")
    print(f"Fan 1 Speed: {f1} RPM")
    if f2 > 0:
        print(f"Fan 2 Speed: {f2} RPM")
    print(f"Tachometer Source: {src}")

if __name__ == "__main__":
    main()
