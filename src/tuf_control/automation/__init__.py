"""Rule-based automation run by the `tuf-controld` user daemon.

config  - ~/.config/tuf-control/config.json (validated, atomic, hot-reloaded)
rules   - Rule / Trigger / Action models
engine  - debounce, hysteresis, priorities, restore
sources - power supply, /proc, clock, profile sampling
actions - Controller-routed action sink
service - start/stop/status of the daemon from the GUI and CLI
"""
