# Changelog

All notable changes. Format based on Keep a Changelog; versions are tags.

## [4.3.1] — 2026-09-26
### Security
- **Fixed: arbitrary command execution from the model (no confirmation).**
  `run_slash_command` gated the unattended (`confirm=None`) path with a
  *denylist*, so only unrecognised commands were refused. `/acp` was reachable
  and its `add` subcommand persists an arbitrary command line that
  `acp.py` later spawns via `subprocess.Popen([cmd] + args)` — no shell needed,
  so the `DANGEROUS` blocklist never applied. Two tool calls
  (`/acp add pwn -- <cmd>`, then `/acp pwn hi`) gave full RCE from a single
  prompt injection, with no user prompt and no yolo mode. `run_slash_command`
  is now an **allowlist** (`_UNATTENDED_SAFE` / `_UNATTENDED_GATED`): anything
  unclassified is refused, so a newly added command is unreachable from the
  model until deliberately classified. `/acp add` additionally confirms, and
  only the read-only `/acp` list form stays unattended
- **Fixed: `/upgrade` rewrote buddy's entire source with no confirmation.**
  `self_update` downloads all 17 modules from `raw.githubusercontent.com` (no
  signature or hash check, only `ast.parse`) and hot-reloads them in-process.
  Its confirm gate only triggered for a model-supplied repo slug, so the
  default-repo path ran unprompted. It now always requires a human
- **Fixed: unguarded plaintext credential reads.** `_sensitive_path_reason`
  matched only SSH/PGP material, so `~/.git-credentials`, `~/.netrc`,
  `~/.npmrc`, `~/.pypirc`, `~/.aws/`, `~/.docker/config.json`,
  `~/.config/gh/`, `~/.config/gcloud/`, `~/.kube/config`, `~/.gem/credentials`
  and `~/.azure/` were readable with `confirm=None` — straight into LLM
  context and the persisted session JSON. Matched on whole path components, so
  `notes-about-netrc.txt` is not a false positive
- **Fixed: SSRF in the web tools.** `_norm_url` returned early for anything
  already `http(s)://`, so its loopback/LAN logic never applied to a full URL:
  `web_fetch` would retrieve `169.254.169.254` metadata or any RFC1918 host and
  return the body to the model. Policy now lives in `_net_open` (lowest layer,
  so no caller can reintroduce it) and refuses loopback, private, link-local,
  reserved, multicast and unspecified addresses
- **Fixed: `command` watchers were an unattended shell primitive.**
  `add_watcher` had no `confirm` parameter at all, so the model could register
  a `shell=True` watcher the daemon re-ran every 30s forever, gated only by a
  blocklist that `wget … ; sh …` walks straight through
- **Fixed: the self-evolution cage stopped caging after the first cycle.**
  `git checkout -b buddy/autonomous`'s return code was ignored, so from cycle 2
  the commit landed unattended edits on the user's working branch — the exact
  outcome SECURITY.md promises cannot happen. The cage now uses `checkout -B`,
  verifies HEAD before committing, and always restores the user's branch
- **Fixed: a corrupt `config.json` silently killed daemon threads.**
  `load_config()`'s `sys.exit(1)` is a `BaseException`, so the
  scheduler/watcher/telegram loops' `except Exception` let it through: the
  thread died with no log line and no supervisor while the web server kept
  serving. Added `load_config_safe()`; the loops now catch `BaseException`

### Fixed
- **The test suite sprayed 80 desktop notifications on every run.**
  `deliver()` shelled out to `notify-send` unconditionally and
  `test_inbox_is_bounded` calls it in an 80-iteration loop, so each run put 80
  `job N` popups full of X's on the user's screen. New
  `BUDDY_NO_AMBIENT_NOTIFY=1` gate, set for the suite
- **Test isolation, properly.** Commit 8716756 patched `CONFIG` to a tempdir
  for one test, but the suite still created `config.json`, `system.json`,
  `logs/` and `outputs/` in the real `~/.buddy` on every run. New `BUDDY_HOME`
  relocates the whole state directory; `tests/_hermetic.py` sets it (and the
  notify gate) in every test module
- `notify_email` was dead code — `email_send` returned "cancelled by user"
  whenever `confirm is None`, and the router discarded the result, so the
  configured channel never received anything. Added an explicit
  `operator_channel` for operator-configured destinations
- `run_command` could ignore its `timeout`: if the child closed stdout early
  the reader finished, `expired` was False, and a bare `p.wait()` blocked for
  the child's whole lifetime. Reaping is now deadline-bounded
- `publish_site` and `auto_install` accepted a `confirm` and ignored it;
  `agent.py` was listing `publish_site` in `_SENSITIVE_PARALLEL` to keep a
  "confirm prompt" on the main thread for a prompt that could never appear
- `_trim_inbox` appended its note outside every `## ` header, so each trim
  absorbed and re-emitted the previous one — one copy accumulated per trim
- `webui._maybe_key_setup` popped the pending API key *before* comparing it, so
  the documented "re-send the key to confirm" branch could never fire. The map
  was also mutated from request threads with no lock
- `remember()` appended forever (`memory_compact` only prunes digest blocks
  older than 30 days) while `_memory_context()` re-read the whole file on every
  prompt build. Fact lines are now capped at `MAX_FACT_LINES`
- Unclosed child pipes on MCP shutdown (`ResourceWarning: unclosed file` at
  `mcp.py`)
- Leaked log file handle when starting the daemon via `Popen`
- `quota.report` raised `ValueError` on a `quota.json` key without a `|`
  separator, breaking `/quota` and `buddy.py quota`

### Added
- `tests/test_security_fixes.py` — 41 adversarial regression tests, one per
  defect above. `tests/test_security.py` passed while all of these were
  exploitable, so the new file pins the exploits themselves
- `load_config_safe()` for background/long-lived callers
- `BUDDY_HOME` and `BUDDY_NO_AMBIENT_NOTIFY` environment overrides

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
