# Security policy

## Reporting a vulnerability

Report vulnerabilities privately by email to **Hamza Gouda <hraafat357@gmail.com>**.
Please do not open a public GitHub issue for a security report.

Include a short description of the issue, the affected version or commit, and
the steps (or a script) that reproduce it. You will get an acknowledgement
within a few days and status updates while a fix is prepared.

## Sensitive surfaces

- `src/asusius/helper.py` — the privileged sysfs writer run through
  `pkexec`. Its exact-path allow-list, per-file value validation and the
  absence of any shell or subprocess use are security-critical.
- The Agent access surface (`src/asusius/agent/`) — the MCP server, its
  access policy and grants, the approval queue, and the sanitisation of
  untrusted text. Anything that lets an MCP client read or write more, or skip
  an approval, beyond the granted access level is a vulnerability.

## Responsible disclosure

Please give the maintainer a reasonable time (90 days) to release a fix before
disclosing details publicly. Credit in the release notes is given on request.
