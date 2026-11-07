# Two-minute hardware-free quickstart

This launches the released TUF Control 0.1.2 interface with simulated laptop data. It does not
need an ASUS laptop, `asusd`, or `supergfxd`. It needs Linux, Python 3.12+, GTK 4, Libadwaita
1.5, PyGObject, and a running graphical session.

![Animated walkthrough of the simulated dashboard, performance controls, and fan curves. The
sidebar identifies this as fake hardware.](demo/quickstart.gif)

## Run it

On Ubuntu 24.04, install the graphical dependencies if needed.

```sh
sudo apt install git dbus python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
```

Run this from a terminal in your graphical session. The temporary files are cleaned up
when you close the demo window.

```sh
(
  set -eu
  demo_dir=$(mktemp -d)
  trap 'rm -r -- "$demo_dir"' EXIT
  git clone --branch v0.1.2 --depth 1 https://github.com/hamza-gouda1/tuf-control.git "$demo_dir/tuf-control"
  export HOME="$demo_dir/home"
  export XDG_CONFIG_HOME="$HOME/.config"
  export XDG_DATA_HOME="$HOME/.local/share"
  export XDG_STATE_HOME="$HOME/.local/state"
  export XDG_CACHE_HOME="$HOME/.cache"
  export GSETTINGS_BACKEND=keyfile
  export TUF_CONTROL_FAKE_ROOT="$demo_dir/fake-root"
  export PYTHONPATH="$demo_dir/tuf-control/src"
  mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME"
  python3 -c 'import os; from tuf_control.backends.fake import build_fake_tree; build_fake_tree(os.environ["TUF_CONTROL_FAKE_ROOT"])'
  dbus-run-session -- python3 -m tuf_control.ui
)
```

The window shows a **Fake hardware (safe)** label. Visit Dashboard, Performance, and Fan curves;
the controls and telemetry use simulated values.

The graphical session display is reused so the window appears on your desktop. D-Bus is private
to this run, settings live under the temporary `HOME`, and the controller selects only the fake
backend while `TUF_CONTROL_FAKE_ROOT` is set. This quickstart does not install the daemon,
GNOME extension, shortcuts, privileged helper, or any system service. It does not validate
hardware compatibility or real firmware behavior.

The walkthrough was rendered from genuine app pages by the release's headless screenshot tool
using its fake hardware tree, temporary `HOME`, private D-Bus session, and headless Wayland
compositor. It is an animated sequence of captures, not a live screen recording.
