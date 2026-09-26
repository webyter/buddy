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
  never execute, write, or reconfigure. This is enforced by an **allowlist**:
  `run_slash_command` refuses anything not classified in `_UNATTENDED_SAFE` or
  `_UNATTENDED_GATED`, so a newly added command is unreachable from the model
  until someone deliberately classifies it.
- Sensitive paths are gated even inside recursive walks. This covers key
  material *and* the plaintext credential stores (`~/.git-credentials`,
  `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.aws/`, `~/.docker/config.json`,
  `~/.config/gh/`, `~/.config/gcloud/`, `~/.kube/config`, `~/.gem/credentials`,
  `~/.azure/`).
- Web tools fetch public destinations only. Loopback, RFC1918, link-local
  (including the `169.254.169.254` metadata endpoint), reserved and multicast
  addresses are refused in `_net_open`, so no caller can reintroduce SSRF.
- Secrets never appear in logs, the web UI URL over non-tty output, or LLM
  context
- Unattended self-modification lands on the `buddy/autonomous` branch, never
  silently in the working tree. The cage verifies HEAD is actually on that
  branch before committing, and restores the user's branch either way.
- A corrupt `config.json` cannot kill a background thread. `load_config()`
  still exits for the CLI; library and daemon callers use
  `load_config_safe()`, and the scheduler/watcher/telegram loops catch
  `BaseException` so a stray `SystemExit` is survivable.

These are tested adversarially in `tests/test_security.py` and
`tests/test_security_fixes.py`. The second file exists because the first one
passed while the guard above was fully bypassable — it now pins each specific
exploit rather than just the happy path.

## Test hermeticity

The suite must never touch your real state or your desktop:

- `BUDDY_HOME` relocates buddy's whole state directory (config, memory,
  sessions, logs). Defaults to `~/.buddy`.
- `BUDDY_NO_AMBIENT_NOTIFY=1` stops `deliver()` shelling out to `notify-send`
  or the webhook/email/Telegram fan-out. The inbox append still happens.

`tests/_hermetic.py` sets both and is imported first by every test module. It
is not optional hygiene: without it each suite run created `config.json`,
`system.json`, `logs/` and `outputs/` in your real `~/.buddy`, and put 80
desktop notifications on your screen.
