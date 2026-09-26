#!/usr/bin/env python3
"""buddy v3 — your own personal assistant, now self-training and always-on.

Features:
  - chat with any OpenAI-compatible API, streaming output
  - tools: shell (with safety confirm), files, web search/fetch, memory
    (flat + vector recall), scheduling, self-training playbook
  - MCP servers (stdio)
  - daemon mode: 24/7 scheduler + local web UI
  - vision: attach images in chat

Usage:
  python3 buddy.py setup              # first-run config
  python3 buddy.py key set            # store API key securely
  python3 buddy.py                    # interactive chat
  python3 buddy.py serve              # daemon: scheduler + web UI (port 7616)
  python3 buddy.py install-service    # install systemd user service
  python3 buddy.py mcp add <name> -- <cmd> [args...]
  python3 buddy.py mcp list
  python3 buddy.py acp [list|add|remove|run]
  python3 buddy.py model [list|add|set|remove]
  python3 buddy.py tools [list|add|remove]
  python3 buddy.py service [status|start|stop|restart|logs|install]
  python3 buddy.py introspect [focus] # operational self-awareness & telemetry
  python3 buddy.py upgrade [repo]     # check & apply latest codebase upgrades
  python3 buddy.py fix [issue]        # autonomous bug diagnosis & source code repair
  python3 buddy.py jobs list
  python3 buddy.py web                # print the web UI URL + token

Chat commands: /new /clear /model /tools /acp /service /introspect /memory /forget /jobs /inbox /playbook /skills /evolve /fix /upgrade /wish /mic /image <path> /yolo /quit (/exit)
"""

import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from buddy_core.config import HOME, CONFIG, PORT, load_config, cmd_key_set, setup, web_token
from buddy_core.commands import chat, doctor, first_run, cmd_oauth, cmd_secret, cmd_tools, cmd_model, cmd_introspect, cmd_fix, cmd_upgrade, install_desktop
from buddy_core.sched import install_service, list_jobs, cmd_service
from buddy_core.webui import serve
from buddy_core.mcp import MCPManager
from buddy_core.acp import cmd_acp


def main() -> None:
    argv = sys.argv[1:]

    # Help shortcuts
    if not argv or argv[0] in ("-h", "--help", "help"):
        if not argv:
            if not CONFIG.exists():
                first_run()
            chat()
            return
        print(__doc__)
        sys.exit(0)

    cmd, rest = argv[0], argv[1:]

    if cmd == "setup":
        setup()
    elif cmd == "doctor":
        doctor()
    elif cmd == "key":
        if rest and rest[0] == "set":
            cmd_key_set()
        else:
            print("usage: python3 buddy.py key set")
    elif cmd == "serve":
        HOME.mkdir(parents=True, exist_ok=True)
        if not CONFIG.exists():
            # zero-config: bootstrap a default config instead of blocking
            load_config()
        serve(load_config())
    elif cmd == "web":
        cfg = load_config()
        if cfg.get("web_auth") is False:
            print(f"http://127.0.0.1:{PORT}/")
        else:
            print(f"http://127.0.0.1:{PORT}/#{web_token(cfg)}")
    elif cmd == "install-service":
        install_service()
    elif cmd == "mcp":
        if rest and rest[0] == "add" and len(rest) >= 2:
            name = rest[1]
            args = rest[2:]
            if args and args[0] == "--":
                MCPManager().cmd_add(name, args[1:])
            else:
                print("usage: mcp add <name> -- <command> [args...]")
        else:
            HOME.mkdir(parents=True, exist_ok=True)
            m = MCPManager()
            m.start_all()
            m.cmd_list()
            m.shutdown()
    elif cmd == "jobs":
        print(list_jobs())
    elif cmd == "quota":
        HOME.mkdir(parents=True, exist_ok=True)
        from buddy_core.config import load_config
        from buddy_core.quota import report as _quota_report
        print(_quota_report(load_config()))
    elif cmd == "oauth" and rest:
        HOME.mkdir(parents=True, exist_ok=True)
        cmd_oauth(rest[0])
    elif cmd == "secret":
        HOME.mkdir(parents=True, exist_ok=True)
        cmd_secret(rest)
    elif cmd in ("update", "upgrade"):
        HOME.mkdir(parents=True, exist_ok=True)
        cmd_upgrade(rest)
    elif cmd == "fix":
        HOME.mkdir(parents=True, exist_ok=True)
        cmd_fix(rest)
    elif cmd == "install-desktop":
        install_desktop()
    elif cmd == "tools":
        HOME.mkdir(parents=True, exist_ok=True)
        cmd_tools(rest)
    elif cmd == "acp":
        HOME.mkdir(parents=True, exist_ok=True)
        cmd_acp(rest)
    elif cmd == "model":
        HOME.mkdir(parents=True, exist_ok=True)
        cmd_model(rest)
    elif cmd in ("service", "services"):
        HOME.mkdir(parents=True, exist_ok=True)
        cmd_service(rest)
    elif cmd in ("introspect", "self"):
        HOME.mkdir(parents=True, exist_ok=True)
        cmd_introspect(rest)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
