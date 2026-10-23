"""Process setup for agent access: an unprivileged Controller and a root refusal."""
from __future__ import annotations

import os

from ..controller import Controller, default_backends


class RunningAsRoot(RuntimeError):
    pass


def refuse_root() -> None:
    """The agent server runs as the desktop user, never as root."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RunningAsRoot("refusing to run agent access as root: start it as your user")


def agent_controller() -> Controller:
    """The normal backend order, but the sysfs writer never escalates: no pkexec helper,
    so a direct write that needs root fails with PrivilegeRequired instead of a prompt.
    Privileged changes go through asusd/supergfxd exactly as for the GUI."""
    from ..backends.sysfs import SysfsBackend
    from ..backends.util import DirectWriter, FallbackWriter
    backends = default_backends()
    for b in backends:
        if isinstance(b, SysfsBackend) and isinstance(b.writer, FallbackWriter):
            b.writer = DirectWriter()
    return Controller(backends)
