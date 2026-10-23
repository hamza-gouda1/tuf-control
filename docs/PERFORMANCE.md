# Performance and accuracy of the monitor

Measured on the owner's laptop: ASUS TUF F16 (FX608JHR), i7-14650HX (24 threads, 8 P + 8 E
cores), Intel Raptor Lake-S UHD iGPU + NVIDIA RTX 5050 Laptop dGPU (driver 580.178.04), one
NVMe SSD, Ubuntu 24.04 (GTK 4.14, libadwaita 1.5, Python 3.12), kernel 7.0 liquorix,
1920×1200 panel at scale 1. Date: 2026-10-23.

## Memory and CPU

`python3 tests/ui/measure.py --idle 60` runs the GUI in its default **real** mode (reads
/proc, /sys, NVML and the system D-Bus daemons, writes nothing) inside a private
`mutter --headless` session, waits on the Dashboard, visits every page, and returns to the
Dashboard. RSS is what `ps -o rss` shows; PSS splits shared library pages between processes.

| | RSS | PSS | CPU (one core) |
|---|---|---|---|
| **TUF Control, Dashboard, idle 60 s** | **107 MB** | 60 MB | **1.2 %** |
| TUF Control, after visiting all 17 pages, back on Dashboard | 116 MB | 69 MB | 1.3 % |
| TUF Control, Processor page (2 Hz) | | | 2.7 % |
| TUF Control, Processes page (600 processes, walk every 2 s) | 119 MB | | 1.3 % |
| Mission Center 1.x flatpak (app + `magpie` helper), same machine | 154 + 56–64 = **~210–218 MB** | | 2.0 % + 7.1 % (ps lifetime average) |
| Empty Python + GTK 4 + libadwaita window (Cairo renderer) | 67 MB | 25 MB | |
| Empty Python + GTK 4 + libadwaita window (GL renderer) | 172 MB | 60 MB | |
| TUF Control with the GL renderer (`TUF_CONTROL_RENDERER=gl`) | 217 MB | 103 MB | 1.0 % |
| … GL, NVIDIA EGL vendor library excluded | 186 MB | 93 MB | 0.9 % |
| Previous TUF Control build (installed copy, GL, all pages eager) | 265 MB | 156 MB | |

So the monitor is about half of Mission Center's resident memory while also being the
laptop control app. The sampler itself costs 4–6 ms per tick (all sources, one thread).

### What dominates, and what was done

- **The GL renderer was the largest item (~110 MB).** GTK's `ngl` renderer maps Mesa's
  `libgallium` and `libLLVM` (45 MB resident) and, through GLVND, NVIDIA's EGL libraries
  (`libnvidia-eglcore`, `libnvidia-gpucomp`, 28 MB). For an app that redraws a few small
  charts once or twice a second this buys nothing, so the app now selects GTK's **Cairo**
  renderer unless `GSK_RENDERER` or `TUF_CONTROL_RENDERER=gl` says otherwise
  (`ui/lean.py`). Idle CPU is within 0.2 % of GL; page switches cost a few % more for a
  moment. On a HiDPI panel software rendering costs more; switch back if it matters.
- **The Python + GTK baseline (~67 MB RSS, half of it shared libraries) cannot be beaten**
  from inside the app. Everything the app adds is about 40 MB: controller, D-Bus proxies,
  the nine dashboard cards, the telemetry store, fonts and icons.
- **Lazy pages.** Pages are imported and built on first visit (`window.LazyPages`).
  Monitor detail pages are destroyed 30 s after you leave them and rebuilt (in tens of ms)
  when you come back; history lives in the shared store, not the page. After page switches
  the window runs `gc.collect()` and glibc `malloc_trim(0)` so freed heap returns to the
  kernel. The Processes page drops its rows and stops the walk when hidden.
- **One timer.** A single `LiveHub` timer drives every source: 1 Hz (a
  `timeout_add_seconds` timer that GLib batches with other wake-ups), 2 Hz while a detail
  page is visible, stopped while the window is hidden or minimised (`Gtk.Window:suspended`).
  Reading runs on the existing worker pool, never two ticks at once; results are committed
  on the main loop.
- **Storage.** One shared time ring (`array('d')`) and one `array('f')` per series, 300
  slots (120 s at 2 Hz, 5 min at 1 Hz): about 4 KB per hundred series-seconds, ~150 KB in
  total on this machine. No per-sample objects, no numpy. A missing reading is NaN and is
  drawn as a gap, never as zero.
- **Drawing.** Charts read the rings in place, downsample to the pixel width (max per
  bucket, so spikes survive), and draw monotone cubic curves (Fritsch–Carlson) that pass
  through every sample and cannot overshoot. They redraw only on a new sample or on hover.
  The 24-CPU grid is one widget, not 24.
- **Cheap sources.** Slow sensors are read less often: extra NVMe sensors (an NVMe admin
  command each, ~0.6 ms), the DIMM sensor (I2C, ~0.9 ms) and the ACPI zone every 5 s,
  the last value held in between. DRM client fds are rediscovered every 10 s (a 17 ms
  `/proc/*/fd` scan); each tick reads one fdinfo per client (~1 ms).

### Keyboard page preview

Before: while the page was visible the preview widget called `queue_draw()` from a frame-clock
tick callback unconditionally, and the software-effect section also pushed a new `KbdEffect`
every 50 ms: ~500 draws/s in the headless harness (the display rate) at **78-80 %** of a core,
even for a static colour. Now (`python3 tests/ui/measure.py --fake --keyboard --idle 15
--renderer cairo`):

| Keyboard page visible | before | after |
|---|---|---|
| hardware effect (static) | 79.8 % (495 draws/s) | **0.9 %** (0 draws/s) |
| software effect, static colour (Accent) | 80.3 % (499/s) | **1.0 %** (0/s) |
| software effect, animated (Rainbow) | 77.9 % (513/s) | **4.7 %** (14.8/s) |

(The Dashboard-idle baseline of the sampler is about 1 %.) What changed (`FrameGate` in
`ui/viewmodels.py`, `KeyboardPreview` in `ui/widgets.py`, `ui/pages/keyboard_effects.py`):
static effects draw once (on change, resize or theme switch); animated ones draw from a
`GLib` timer at 15 fps (10 when the average draw takes over 20 ms) instead of the frame clock;
`set_effect` is a no-op when the effect and brightness are unchanged; key geometry is cached
per size and the colour is computed once per frame (per column or row for the rainbow wave);
the software preview timer runs at 15 fps and backs off to 4 Hz after 30 frames with an
unchanged colour, touching the swatch, hex label and keyboard widget only when the colour
moved; both timers are removed when the page is unmapped or the window is hidden or suspended.

Running the sampler in `tuf-controld` instead of the GUI would not save memory (the GUI
still needs GTK) and would make the daemon wake every second even with no window open, so
the sampler stays in the GUI and stops with it.

## Power rule for the NVIDIA GPU

Any NVML call or `nvidia-smi` run resumes a runtime-suspended GPU. The sampler reads
`/sys/bus/pci/devices/<addr>/power/runtime_status` and `power_state` first (reading these
does not resume the device). Not `active` → reported as *Sleeping (D3cold)*, nothing else is
touched. Awake → NVML (ctypes on `libnvidia-ml.so.1`, kept open only while the GPU is
busy). Awake but idle for three samples (utilisation < 1 %, no compute process) → the NVML
session is closed and polling backs off for 30 s so the driver can suspend it; the page
shows the last reading and its age. `nvidia-smi` is only a fallback when NVML is missing,
and then at most every 5 s. During these measurements the dGPU stayed `active` because other
clients held it (gnome-shell has a context on it, and the previously installed TUF Control
build ran `nvidia-smi` every second); the sleeping path is covered by a unit test with a
suspended runtime state and an NVML stub that fails if called.

## Accuracy: validation against independent tools

`PYTHONPATH=src python3 tests/hardware/validate_telemetry.py --markdown` (read-only; it
starts two busy loops and an unprivileged disk reader for the first 10 s so the windows
are not idle). Result on the machine above: **44 checks, 0 failures**. Physical interfaces
shown; the virtual ones (docker0, virbr0, a bridge, enp109s0 unplugged) also matched.

| Source | Check | Ours | Reference | Tolerance | Result |
|---|---|---|---|---|---|
| CPU | total % vs same-window /proc/stat (max of 10) | diff 0.04 | | 0.5 pt | OK |
| CPU | per-core % vs same-window /proc/stat (max) | diff 0.76 | | 1 pt (1 jiffy) | OK |
| CPU | total % vs `top -bn2` (mean of 3) | diff 0.25 | | 5 pt | OK |
| CPU | average clock vs /proc/cpuinfo (mean of 10) | 2167 MHz | 2263 MHz | 15 % | OK |
| Disk | nvme0n1 read rate vs /sys/block/nvme0n1/stat | rel. err 0.02 % | | 5 % | OK |
| Network | wlp108s0 rx/tx vs /sys/class/net/*/statistics | rel. err 0 % | | 5 % | OK |
| Memory | total, used, available, free, buff/cache, swap vs `free -b` | identical | | 64 MiB | OK |
| Sensors | CPU fan / GPU fan vs `sensors -j` | 6200 / 2800 rpm | 6200 / 2800 | 300 rpm | OK |
| Sensors | CPU package, NVMe composite vs `sensors -j` | 67.0 / 42.85 °C | 67.0 / 42.85 | 3 / 2 °C | OK |
| NVIDIA | util, VRAM used/total, temp, power, clocks, P-state vs `nvidia-smi` | 0 %, 11.94/8151 MiB, 49 °C, 3.44 W, 180/405 MHz, P8 | 0 %, 12/8151, 49, 3.44, 180/405, P8 | 10 pt, 32 MiB, 2 °C, 2 W, 30 MHz, exact | OK |
| iGPU | requested clock vs sysfs; actual within [0, RP0]; busy ≤ awake | 533 MHz | 533 MHz | | OK |
| Battery | charge, voltage, power, energy full vs `upower -i` | 90 %, 16.92 V, 0 W, 79.17 Wh | 90, 16.93, 0, 79.17 | 1 %, 0.15 V, 1.5 W, 0.1 Wh | OK |
| Processes | RSS of the 5 largest vs `ps -o rss` | max diff 0 KiB | | 512 KiB | OK |

Discrepancies found and fixed while validating:

- NVMe temperature was 5 °C behind `sensors` under load because all NVMe sensors were held
  for 5 s. The composite sensor (the one shown on the cards) is now read every tick.
- The clock comparison was first made right after the load stopped (clocks falling) and
  failed; it is now made from paired reads inside the sampling windows.
- `gt_act_freq_mhz` flips between 0 (GT asleep in RC6) and the running clock between two
  reads, so it is range-checked instead of compared; the requested clock is compared.
- NVML memory uses `nvmlDeviceGetMemoryInfo_v2`, which matches `nvidia-smi` (the v1 call
  counts ~450 MiB of driver reservation as used).

Known limits, stated in the UI where they apply:

- iGPU utilisation is the render engine time from DRM fdinfo of processes this user may
  read (the desktop session); system-wide PMU counters need `perf_event_paranoid <= 0`.
  A client that exits between two ticks loses its last interval.
- CPU package power (RAPL) is root-only since kernel 5.10; the app says so instead of
  showing a number.
- `/proc/pressure` does not exist on this kernel (PSI not built in or `psi=0`): shown as
  not available.
- The firmware reports no maximum fan speed, so "% of max" is relative to the highest
  speed seen this session, and labelled that way. NVML reports no fan on laptop GPUs; the
  ASUS `gpu_fan` reading is shown instead.
- Per-process disk I/O is only readable for your own processes; per-process network use
  would need root (eBPF) and is not shown.
