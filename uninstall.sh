#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
# Convenience wrapper: ./uninstall.sh [--dry-run]
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" --uninstall "$@"
