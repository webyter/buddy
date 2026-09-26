# 🐶 buddy — autonomous personal assistant with Antigravity TUI

[![CI](https://github.com/webyter/buddy/actions/workflows/ci.yml/badge.svg)](https://github.com/webyter/buddy/actions/workflows/ci.yml)

Buddy is a zero-dependency personal AI coding assistant and autonomous operator in Python 3.10+ (stdlib only).
Features an Antigravity (`agy`)-styled full-screen terminal UI, Google Gemini default (`gemini-flash-latest`, newest stable flash), streaming LLM chat, rich diff inspection, Model Context Protocol (MCP) server support, Agent Client Protocol (ACP) driver, persistent memory, and a 24/7 background daemon.

## Security model

Buddy gives an LLM shell, file and network access on your machine, so the guard
architecture is the product. The invariants, tested adversarially in
[tests/test_security.py](tests/test_security.py):

- **`confirm=None` means DENY.** Every dangerous tool (shell, file write/edit,
  OS updates, email, model/provider config changes) requires a live user
  confirmation callback. The unattended tool path passes `None` — a
  prompt-injected brain cannot execute, edit, or re-point buddy's API endpoint.
- **Dangerous-command guard.** Shell commands matching destructive patterns
  (rm -rf, disk wipes, fork bombs, `curl | sh | python3`, secret exfil via
  `--data @`, scp to remote hosts, chmod 777, distro upgrades…) are blocked
  unless the user confirms.
- **Sensitive-path gate.** `~/.ssh`, `~/.gnupg`, key material and buddy's own
  secrets are never read, searched or watched without confirmation — including
  inside recursive walks.
- **Web UI auth.** Token compared constant-time, carried in the URL *hash*
  (never sent to the server in query strings or referrers); unauthenticated
  serving is refused on anything but loopback.
- **Per-provider keys.** A second AI provider never receives your shared API
  key — it uses its own `api_key_<name>` secret, and an unreadable vault fails
  the call instead of leaking.
- **Self-update with rollback.** Upgrades verify syntax and run the test suite
  before replacing the source; failures roll back automatically.

Least privilege note: when creating GitHub tokens for buddy integrations, grant
only `repo` scope — avoid `delete_repo` unless you specifically need it.


## Quickstart

Buddy defaults to Google Gemini (`gemini-flash-latest` via Google AI Studio — always the newest stable flash).

```sh
# First-time setup (interactive wizard)
python3 buddy.py setup

# Or set your Google AI Studio API key directly
python3 buddy.py key set

# Launch the interactive Antigravity TUI
python3 buddy.py
```

## Zero-config & offline mode

Setup is optional. With no config file, buddy bootstraps one and runs offline;
with no API key (or an exhausted quota), chat turns fall back to offline mode
instead of erroring out. Everything below needs no LLM — some need internet.

```text
! <command>, run <cmd>      shell + git (e.g. `! git status`, `run pytest`)
ls / cat / head / tail      files, grep <pat>, find <pat>, pwd, which <tool>
time, disk, free, cpu, sysinfo, doctor
remember <note>, memory, jobs, inbox, playbook, skills
remind me to X every 30 minutes | at 14:30 | in 10 minutes   (one-shot timers self-delete)
timer 5 minutes; cancel timer <id>
api status                  brain/model/key state (key never shown)
weather <city>, ip, ping <host>
read <url>, define <word>, 100 usd to ghs, wiki <topic>      (keyless APIs)
password 24, uuid, 30 c to f, 100 km to mi
calc 1024 * 768, base64/hex/url encode+decode, sha256 <str>
open https://...
```

Type `help` in any chat for the live list. Failover: on 429/quota errors buddy
retries the turn on another registered model (`buddy.py model add`); offline
tasks above always answer directly.

## CLI Usage

```sh
python3 buddy.py setup              # first-run config wizard
python3 buddy.py key set            # store API key securely (0600)
python3 buddy.py                    # launch interactive chat TUI
python3 buddy.py serve              # 24/7 daemon: scheduler + watchers + web UI (port 7616)
python3 buddy.py install-service    # install systemd user service (Restart=always)
python3 buddy.py install-desktop    # install desktop application launcher
python3 buddy.py model [list|add|set|remove]  # manage LLM models & endpoints
python3 buddy.py acp [list|add|remove|run]    # manage & drive ACP external agents
python3 buddy.py service [status|start|stop]  # background daemon & systemd services
python3 buddy.py introspect [focus]             # operational self-awareness & telemetry
python3 buddy.py upgrade [repo]                 # auto-upgrade codebase with test & rollback protection
python3 buddy.py fix [issue]                    # autonomous bug diagnosis & source code repair
python3 buddy.py mcp add <name> -- <cmd> [args...]   # register an MCP server
python3 buddy.py mcp list           # list configured MCP servers
python3 buddy.py tools list         # list built-in and user custom tools
python3 buddy.py tools add <n> <f>  # install a custom Python tool
python3 buddy.py jobs list          # list scheduled cron/interval jobs
python3 buddy.py oauth <provider>   # OAuth flow for Codex / Claude / Gemini
python3 buddy.py secret set <key>   # store a secret in encrypted/0600 store
python3 buddy.py web                # print web UI URL + access token
python3 buddy.py doctor             # run system diagnostic & self-check
python3 buddy.py update             # update buddy to latest from git
```

## Antigravity TUI

Buddy's terminal interface is modeled after the Google Antigravity (`agy`) CLI:
- **Header**: Chevron logo (`»`), active model, and status indicator.
- **Rules**: Sleek horizontal dividers (`─`).
- **Prompt**: Clean `> ` interactive prompt with auto-resizing viewport.
- **Slash Palette**: Type `/` to open the interactive command palette.
- **Shortcuts Modal**: Press `?` to toggle keybindings and shortcuts view.
- **Execution Modes**: Fast switching between modes (e.g. `accept-edits`, `plan`, `bypass`).
- **Diff Display**: Unified diff rendering for file edits with dual line numbers and color-coded insertions/deletions (opencode-style, also shown in the web UI).
- **Mouse**: the wheel/trackpad scrolls chat out of the box — Buddy only claims the wheel buttons, so drag-select, right-click menu and middle-click paste still belong to your terminal. `F9` switches to the full in-app mouse (drag to highlight + copy, right-click to paste); `Ctrl+O` copies the recent chat to your clipboard.

### Slash Commands in Chat

| Command | Description |
|---|---|
| `/new` | Fresh chat session (clears conversation context) |
| `/model` | Pick a model with ↑/↓ (grouped by provider), or add/remove endpoints |
| `/tools` | List built-in and user custom tools |
| `/acp` | Manage and drive external ACP agents |
| `/service` | View and control background daemon and systemd services |
| `/introspect` | Deep operational self-awareness & telemetry diagnostic |
| `/memory` | Inspect persistent memory |
| `/forget` | Wipe all memory notes |
| `/jobs` | List scheduled background jobs |
| `/inbox` | Read notifications delivered while away |
| `/playbook` | View self-training playbook and operating rules |
| `/skills` | List harvested procedures and learned skills |
| `/evolve` | Trigger a self-improvement cycle |
| `/fix` | Autonomous bug diagnosis and source code repair |
| `/upgrade` | Check and apply latest codebase upgrades with test validation |
| `/theme` | Change color theme |
| `/wish` | Record a capability wish for future self-improvement |
| `/mic` | Voice-to-text recording |
| `/image` | Attach an image file to the turn |
| `/say` | Voice output mode (`off`, `api`, `espeak`) |
| `/yolo` | Toggle command confirmations on/off |
| `/help` | List chat commands |
| `/status` | Full health view: brain/model/mode, memory, jobs, watchers, inbox, errors, disk |
| `/quit` / `/exit` | Exit Buddy |

### Direct Shell Execution & Offline Mode

- **Direct Shell (`! <cmd>`)**: Type `!` followed by any command (e.g. `! git status`, `! ls -la`, `! pytest`, `! python3 buddy.py doctor`) to execute immediately without calling the LLM API. Works in both terminal TUI and Web UI.
- **Offline Fallback**: When the cloud API is offline, rate-limited, or unreachable, Buddy automatically recovers and executes local tasks (shell execution, file reading with `cat`/`read`, file search with `find`/`grep`, system metrics `disk`/`free`/`uptime`, telemetry `tools`/`jobs`/`memory`, and arithmetic `calc 1024*768`) with zero disruption.

## Built-in Tools

- **Shell**: `run_command` — Live streaming output with timeout and user confirmation safety gates.
- **Files**: `read_file`, `write_file`, `edit_file` — Precise file operations and surgical replacements.
- **Search**: `grep` (regex search with line numbers), `find_files`, `list_dir`.
- **Tasks**: `todo_write` & `todo_read` — Task planning and tracking.
- **Web**: `web_search` (DuckDuckGo) & `web_fetch` (page reader), plus optional `browse` (Playwright headless browser).
- **Subagents**: `delegate` — Spawns parallel background workers reporting to the inbox.
- **Automation**: `schedule` (recurring cron / timer), `add_watcher` (change detection on files, URLs, commands).
- **Self-Improvement**: `code_edit` patches his own source code (syntax-verified and auto-backed-up).
- **Dynamic Extension**: `integrate_tool` writes, compiles, and registers custom Python tools into `~/.buddy/tools/`.

## Custom Tools & Integrations

Buddy can integrate any tools you need in three ways:

### 1. In-Chat (Autonomous Tool Integration)
Ask Buddy directly in chat to create and integrate a tool:
> *"Integrate a tool to fetch cryptocurrency prices from CoinGecko"*

Buddy writes the code, verifies the syntax, tests it, and persists it into `~/.buddy/tools/`. The new tool becomes active immediately and across future sessions.

### 2. Custom Python Tools (`~/.buddy/tools/`)
Drop any Python file into `~/.buddy/tools/<name>.py` with a handler function:
```python
def fetch_weather(city: str) -> str:
    """Fetch current weather for a city."""
    return f"Weather in {city}: 21°C, sunny"
```
Or manage via the CLI:
```sh
python3 buddy.py tools list
python3 buddy.py tools add my_tool path/to/my_tool.py
python3 buddy.py tools remove my_tool
```

### 3. Model Context Protocol (MCP) Servers
Integrate any standard MCP stdio tool server:
```sh
python3 buddy.py mcp add github -- npx -y @modelcontextprotocol/server-github
python3 buddy.py mcp list
```
All tools exposed by the MCP server automatically become available to Buddy with the `mcp__<server>__<tool>` prefix.

## ACP (Agent Client Protocol) Integration

Drive external coding agents (e.g. Gemini CLI, Claude Code) over standard ACP JSON-RPC:
```sh
# List configured and host-detected ACP agents
python3 buddy.py acp list

# Register an ACP agent
python3 buddy.py acp add gemini -- gemini --acp
python3 buddy.py acp add claude -- claude --acp

# Drive an ACP agent directly from CLI or chat
python3 buddy.py acp run gemini "Refactor database query logic"
```

## Model Integration

Buddy ships **Google Gemini only** and defaults to `gemini-flash-latest` (an alias that always tracks the newest stable flash, so you never get stranded on a retired model).

`/model` opens a picker grouped by provider — the curated best/popular models first (`gemini-flash-latest`, `gemini-3.8-flash`, `gemini-3.5-flash`, `gemini-2.5-flash`, `gemini-pro-latest`, `gemini-3.1-pro-preview`, `gemini-2.5-pro`), then anything else your key can actually reach, queried live from the endpoint. Move with ↑/↓, pick with Enter.

```sh
# pick a model
python3 buddy.py model                # numbered list
python3 buddy.py model 2              # pick by number
python3 buddy.py model set gemini-pro-latest

# add a provider back (nothing but Google ships by default)
# the 4th argument is that provider's OWN key, so buddy never sends your
# Google key to a third-party host
python3 buddy.py model add deepseek https://api.deepseek.com/v1 deepseek-chat sk-...
python3 buddy.py model add ollama http://localhost:11434/v1 llama3.2
python3 buddy.py model remove deepseek

# or add the provider now and the key later
python3 buddy.py secret set api_key_deepseek
```

Each added provider gets its own key, stored as `api_key_<name>` in the same
encrypted-or-0600 store as your main key (`python3 buddy.py secret set
api_key_<name>`). If you add a provider *without* a key, buddy falls back to the
shared key and `doctor` flags it — so you always know when a credential could
reach a host you didn't intend.

In chat (terminal *and* web) you can also just ask: `buddy change model to gemini 3.8 flash`, or `switch to gemini pro latest`. Model switches take effect immediately — no restart, including for a running daemon, scheduled jobs and Telegram.

In the **web UI** the model chip in the header is a dropdown grouped by provider; choosing one switches instantly and starts a fresh chat (the previous transcript was produced by a different model).

## Speech

Voice is locked to **Gemini 3.8 Flash** (`gemini-3.8-flash-tts` for speech, `gemini-3.8-flash` for transcription) and is deliberately **not user-configurable** — no config key, no `/model` interaction, and `tts_model` in config is ignored. Only the *voice* is selectable, via `/say voice <name>` (OpenAI voice names like `nova`/`alloy` map to Gemini voices; native Gemini names like `Puck`/`Kore` work directly). `/say` switches output mode: `off`, `api`, or `espeak` as a local fallback.

## Services Management

Manage the 24/7 daemon, systemd user service, background watchers, and custom user services:
```sh
python3 buddy.py service status     # check health of daemon, systemd, and watchers
python3 buddy.py service start      # start background service
python3 buddy.py service stop       # stop service
python3 buddy.py service restart    # restart daemon
python3 buddy.py service install    # install systemd user unit (Restart=always)
python3 buddy.py service logs       # inspect systemd service journal
python3 buddy.py service add <n> <s># install user service into ~/.buddy/services/
```
In chat, type `/service` to view active background services.

## Operational Self-Awareness & Introspection

Buddy maintains live metacognitive awareness of his internal cognitive configuration, active toolchains, learned lessons, and operational environment.

- **CLI Introspection**:
  ```sh
  python3 buddy.py introspect              # full diagnostic breakdown
  python3 buddy.py introspect cognitive    # engine, API base, execution mode, safety
  python3 buddy.py introspect knowledge    # long-term memories, skills, playbook lessons
  python3 buddy.py introspect tools        # built-in, custom user tools, ACP & MCP servers
  python3 buddy.py introspect telemetry    # daemon status, active watchers, logged errors
  ```
- **In-Chat Introspection**:
  Type `/introspect` (or `/introspect [focus]`) at any time in chat or command palette.
- **Autonomous Introspection**:
  Buddy has access to the built-in `introspect` tool to evaluate his own cognitive limitations, health, and operational constraints prior to multi-step execution.

## Autonomous Upgrades & Bug Repair

Buddy features built-in self-upgrades and autonomous source code bug diagnosis and repair:

### 1. Codebase Upgrades (`auto_upgrade`)
- **CLI**: `python3 buddy.py upgrade [repo]` (or `python3 buddy.py update`).
- **In-Chat**: `/upgrade [repo]` in chat or slash palette.
- **Background Daemon**: When running 24/7 (`python3 buddy.py serve`), the `AutoUpgrade` daemon thread checks upstream periodically (default every 24h, configured via `"upgrade_hours"` in `config.json`).
- **Safety Gates**:
  - Every downloaded module is verified with `ast.parse` for syntax validity.
  - Previous versions are automatically archived into `~/.buddy/backups/`.
  - The full regression test suite is run post-upgrade (`python3 -m unittest discover -s tests`). If any test fails, Buddy automatically rolls back to the backups to guarantee zero-breakage.
  - Successfully updated modules are hot-reloaded into memory immediately.

### 2. Autonomous Bug Diagnosis & Source Repair (`self_repair`)
- **CLI**: `python3 buddy.py fix [issue]`
- **In-Chat**: `/fix [issue]`
- **Autonomous Tool**: `self_repair(issue=...)` can be called by Buddy during execution.
- **Autonomous Error Reaper**: In daemon mode, `ErrorReaper` detects runtime exceptions in `~/.buddy/errors.log` and triggers self-repair cycles.
- **Multi-Vector Bug Detection**:
  - Scans all codebase files for AST syntax errors.
  - Runs regression tests to identify failing assertions or unexpected exceptions.
  - Analyzes crash logs and tracebacks in `~/.buddy/errors.log`.
  - Accepts targeted user bug descriptions (`/fix <problem>`).
- **Verified Code Edits**:
  - Uses `code_edit` to patch `buddy_core/<module>.py` or `buddy.py`.
  - Verifies fixes against the test suite.
  - Hot-reloads repaired modules with `hot_reload()`.

## Pair with Telegram (Optional)

1. Add `"telegram": true` to `~/.buddy/config.json`.
2. `python3 buddy.py secret set telegram_token` (from @BotFather).
3. `python3 buddy.py serve` (or regular chat).
4. Send a message to your bot — Buddy pairs automatically and delivers notifications to your chat.

## Coding Agent Bridge (Optional)

Buddy can delegate heavy coding tasks to OpenAI Codex CLI:
```sh
npm install -g @openai/codex && codex login --device-auth
```
Buddy will automatically utilize `codex exec` when delegated complex multi-file engineering tasks.

## Themes

Buddy includes 9 color themes: `opencode` (default), `catppuccin`, `dracula`, `gruvbox`, `nord`, `tokyonight`, `vesper`, `aura`, `light`.
Set `"theme": "<name>"` in `~/.buddy/config.json`, or switch in chat with `/theme <name>`. Drop custom JSON palettes into `~/.buddy/themes/` to add your own.

## Background Daemon

```sh
python3 buddy.py serve            # 24/7 scheduler, watchers, telegram, web UI
python3 buddy.py install-service  # systemd user unit with Restart=always
python3 buddy.py install-desktop  # desktop application menu entry
```

Local web UI is served at `http://127.0.0.1:7616/#<token>` (`python3 buddy.py web`). Set `"web_auth": false in ~/.buddy/config.json for a token-free URL (`http://127.0.0.1:7616/`) — loopback only; the daemon refuses LAN binds without a token.

## Files & Storage

All persistent configuration and memory reside in `~/.buddy/`:
`config.json`, `memory.md`, `playbook.md`, `skills/`, `workspace/`, `backups/`, `watchers.json`, `inbox.md`, `outputs/` (full logs of commands whose output was too big to show), `copies/` (fallback when no clipboard tool exists), `logs/` (MCP server stderr).

Every growing store is capped so the 24/7 daemon can't fill your disk: `errors.log` and `inbox.md` are trimmed to their newest entries, vectors to 500 items, sessions to 20, shell history to 500 lines.

## Knowing What Happened

Commands report a verdict, so a failure never hides inside a wall of output:

```
(FAILED: Command exited with code 1.)
<the command's output…>
<shell_metadata>
Command exited with code 1.
</shell_metadata>
```

No banner means the command succeeded. A `FAILED` banner (non-zero exit, timeout, or a safety refusal) also turns the tool row red in the terminal and web UI — Buddy is instructed to diagnose and rerun rather than report success.

### When a model is unavailable

- **Overloaded (`503`, "high demand")** or a per-model rate limit: Buddy silently retries that turn on another model from your catalog and tells you which one it used. Your configured model is not modified.
- **Quota exhausted (`429`, "exceeded your current quota")**: this is the *project's* quota, so every model on the key returns it and switching cannot help. Buddy says so and points at [ai.dev/rate-limit](https://ai.dev/rate-limit) instead of burning retries. Set `"failover": false` in `config.json` to disable the automatic retry entirely.

## Troubleshooting

```sh
python3 buddy.py doctor     # config, key, voice, screenshot/clipboard, API reachability, daemon
python3 buddy.py introspect  # cognitive state, knowledge, toolchain, telemetry
python3 buddy.py service status
```

- **`(API offline: API error 429 …)`** — you hit the provider's rate/quota limit. Wait for the window to reset or raise the plan; switch model with `/model`.
- **`(API offline: API error 503 …)`** — the model is overloaded; retry shortly or pick another.
- **A stale model in the web UI** — the daemon reads `config.json` on every turn, so this shouldn't happen; `python3 buddy.py service restart` if it does.
- **Broken audio/clipboard** — `doctor` names the missing tool; on Wayland install `wl-clipboard` and `grim` (`buddy.py doctor` probes them).

---
MIT License. He's yours.
