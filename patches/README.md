# Linux Kernel Patches for HP Laptops (`hp-wmi`)

## Background: Why does `fan1_input` report 0 RPM on some HP laptops?

On modern HP laptops (Victus, Omen, Pavilion Gaming, Envy, and 15-series like `15-fd1xxx`), HP's BIOS implements multiple WMI query interfaces:

1. **Legacy Method `GM11` (Query `0x11`)**:
   On newer motherboard boards (such as board ID `8DE1`), HP replaced this method with a dummy stub that returns 4 bytes of `0x00` and exit code `0` (Success). Because it returns code `0`, the Linux `hp-wmi` kernel driver believes the call succeeded, but always reports `0 RPM` via `/sys/devices/platform/hp-wmi/hwmon/hwmon*/fan*_input`.

2. **Victus S Method `GM2D` (Query `0x2D`)**:
   Implemented on newer HP laptops to read the active fan tachometer directly from the Embedded Controller (EC0) registers `RPM1` / `RPM2` (offsets `0xB0-0xB3`), accurately returning fan speed in RPM.

## Included Patch: `0001-platform-x86-hp-wmi-Add-support-for-HP-Laptop-15-fd1.patch`

This patch adds motherboard board ID `8DE1` to `hp_wmi_feature_boards[]` mapped to `victus_s_board_params`, which switches `hp-wmi` to use query `0x2D`.

### How to Find Your Motherboard Board ID
Run:
```bash
cat /sys/class/dmi/id/board_name
```

### Applying the Patch to the Linux Kernel
If your HP laptop reports 0 RPM via `hp-wmi`:
1. Check your board ID as shown above.
2. If your board ID is not yet in `drivers/platform/x86/hp/hp-wmi.c`, add an entry to `hp_wmi_feature_boards[]`:
```c
{
    .matches = { DMI_MATCH(DMI_BOARD_NAME, "YOUR_BOARD_ID") },
    .driver_data = (void *)&victus_s_board_params,
},
```
3. Rebuild and install the module using DKMS or kernel build:
```bash
git apply patches/0001-platform-x86-hp-wmi-Add-support-for-HP-Laptop-15-fd1.patch
```

### Direct EC Fallback (No Kernel Recompilation Required)
`ThrottleStop for Linux` automatically bypasses this issue if the kernel is unpatched! If `hp-wmi` reports 0 RPM, our hardware engine falls back to reading the Embedded Controller directly via `/sys/kernel/debug/ec/ec0/io` (provided by kernel module `ec_sys`).
