# Security Policy

## Reporting a vulnerability

Buddy executes shell commands, edits files, and runs unattended on your
machine — take vulnerabilities here seriously.

**Please do NOT open a public issue for a security problem.** Use GitHub's
private vulnerability reporting (Security → Report a vulnerability) on
[webyter/buddy](https://github.com/webyter/buddy), or contact the maintainer
directly. You'll get credit in the release notes unless you'd rather not.

Expect a response within 7 days and a fix or a documented mitigation for
anything confirmed.

## Scope

In scope:
- Guard bypasses: anything that lets the model execute dangerous shell, edit
  sensitive files, or mutate config without a user confirmation
- Sensitive-path leaks (~/.ssh, ~/.gnupg, key material, buddy's own secrets)
- Web UI auth bypass or token disclosure
- Sandbox escapes in the ACP/codex integration
- Anything that destroys user data (memory, sessions, jobs)

Out of scope: a user explicitly enabling `"yolo": true` or `web_auth: false`
is a documented operator choice; reports that only work against those settings
will be triaged but are lower priority by design.

## Design invariants (what we promise)

- `confirm=None` means DENY on every dangerous tool — the unattended path can
  never execute, write, or reconfigure
- Sensitive paths are gated even inside recursive walks
- Secrets never appear in logs, the web UI URL over non-tty output, or LLM
  context
- Unattended self-modification lands on the `buddy/autonomous` branch, never
  silently in the working tree

These are tested adversarially in `tests/test_security.py`.
