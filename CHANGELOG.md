# Changelog

All notable changes. Format based on Keep a Changelog; versions are tags.

## [4.3.0] — 2026-09-26
### Added
- **`brain: "acp"`** — run the whole decision-engine through buddy's own ACP
  client (`claude-agent-acp`, `codex-acp`, …): no API key needed, the coding
  agent's own sign-in is the credential. TOOLCALL/FINAL protocol and prompt
  building shared between CLI and ACP brains
- **`/quota`** — what's left on each connected API. No quota API exists, so
  buddy harvests rate-limit headers from every live response and parses 429
  quota bodies (Google's OpenAI-compat endpoint wraps the error in an array);
  state persists in `~/.buddy/quota.json`. Also visible in `/status` and
  `buddy.py quota`
- First live self-evolution cycle: buddy wrote its own operational playbook
  (`~/.buddy/playbook.md`)
### Fixed
- Test isolation: the suite's `/model add` test rewrote the user's REAL
  `~/.buddy/config.json` (suite runs silently switched providers)
- Cross-provider failover no longer sends the shared main key to a foreign
  endpoint (guaranteed 401); loopback endpoints (ollama) stay keyless-exempt

## [4.2.0] — 2026-09-26
### Added
- Proper packaging: `pip install git+https://github.com/webyter/buddy` installs
  a `buddy` command (pyproject.toml, stdlib-only — zero dependencies)
- Release automation: tags build sdist/wheel and attach them to the GitHub release
- Fresh-install smoke test in CI (clean clone must answer `--help` and import cleanly)
- SECURITY.md (private vulnerability reporting, scope, design invariants)

## [4.1.0] — 2026-09-26
### Fixed
- Full audit sweep (~35 minors): atomic file writes everywhere, locks on every
  read-modify-write, tokens masked in non-tty logs, watcher process-tree kills
  on timeout, TUI rendering never blocked by clipboard or corrupt input,
  memory compaction merges instead of duplicating, ACP replies off the reader
  thread

## [4.0.0] — 2026-09-26
### Security (4 majors, security audit)
- Closed the ~/.ssh grep-walk exfiltration path
- Model config (`/model add`) no longer reachable unattended by the brain
- codex CLI sandbox downgraded from danger-full-access to read-only
- Watcher sensitive-path and file:// URL bypasses refused

### Fixed
- 10 crash/OOM majors: nested-pow multi-GB eval, >4300-digit literal crashes,
  unbounded retries, non-atomic tool reload, broken one-shot timers, paste
  injection in the TUI, repair-cooldown bypass, hung ACP sessions, shared-key
  leak to third-party providers
- Suite passes on clean machines (no stored API key required)

### Added
- GitHub Actions CI (Python 3.10–3.13 matrix + py_compile gate)
- tests/test_security.py: adversarial tests replaying every audit finding

## [3.x]
- Original single-file program, refactored into the buddy_core package:
  Antigravity TUI, MCP support, ACP driver, self-training skills, daemon,
  web UI, offline mode.
