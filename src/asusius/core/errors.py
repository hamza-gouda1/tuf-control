# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
class AsusiusError(Exception):
    """Base class for all errors raised by this package."""


class ValidationError(AsusiusError, ValueError):
    """A value is outside the allowed bounds or malformed."""


class Unsupported(AsusiusError):
    """The backend does not implement this operation."""


class BackendUnavailable(AsusiusError):
    """The backend cannot be used right now (daemon missing, file absent)."""


class BackendError(AsusiusError):
    """The backend was reachable but the operation failed (timeout, bad output)."""


class PrivilegeRequired(BackendError):
    """The write needs privileges the current process lacks."""
