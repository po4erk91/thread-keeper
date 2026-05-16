# Reporting issues

thread-keeper runs as a local MCP server with read/write access to
`~/.threadkeeper/db.sqlite` and the per-CLI config files it manages. If
you discover a problem that could let a malicious input escalate
beyond that scope — for example arbitrary code execution via a crafted
transcript, exfiltration of secrets via a tool call, or writes outside
the documented file set — please report it privately so it can be
fixed before public disclosure.

## How to report

Use the GitHub "Report a vulnerability" form on the repo's Security tab:

<https://github.com/po4erk91/thread-keeper/security/advisories/new>

This routes the report directly to the maintainer with no public trace
until coordinated disclosure. Please include:

- thread-keeper version (or commit hash)
- OS + Python version
- Reproduction steps with the minimal payload
- Expected vs. actual behavior
- Any mitigation you've already tried

## Scope

In-scope:

- The thread-keeper package and its hooks
- The `thread-keeper-setup` installer
- Every adapter under `threadkeeper/adapters/`
- The MCP tools registered in `threadkeeper/tools/`

Out of scope:

- Vulnerabilities in upstream dependencies (report to those projects)
- Issues that require pre-existing local code execution on the user's
  machine (thread-keeper trusts its host process by design)
- Behavior of the underlying CLIs themselves (Claude Code, Codex,
  Gemini, Copilot, VS Code) — those have their own programs

## Supported versions

Only the latest minor release on PyPI gets fixes. If you're on an
older tag, the first response will likely be "please upgrade and
retry."

## Response time

This is a small project, but reports are taken seriously. Expect an
acknowledgment within a few days and a fix or written explanation
within two weeks for confirmed issues. If you don't hear back, ping
the maintainer through the same advisory thread.
