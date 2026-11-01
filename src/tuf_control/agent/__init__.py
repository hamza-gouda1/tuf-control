# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Agent access: a door into TUF Control for MCP clients and scripts.

policy    - access levels, the owner's grants (agent.json), the kill switch
approvals - the per-call approval queue shared by the server, the GUI and the CLI
audit     - append-only JSONL audit log with rotation
sanitize  - untrusted text from the system (process names, labels) made safe to hand over
schema    - the small JSON Schema subset every tool argument is checked against
tools     - the tool catalogue and the gate every call goes through
observe   - telemetry and process snapshots for the read tools
mcp       - Model Context Protocol server, JSON-RPC 2.0 over stdio (stdlib only)
notify    - optional desktop notification with Approve once / Deny

Safety is enforced in `tools.AgentService.call`; see docs/AGENT_API.md.
"""
