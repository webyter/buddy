"""buddy_core.acp — Agent Client Protocol client.

Lets buddy drive external coding agents (Claude Code via claude-agent-acp,
Gemini CLI, etc.) that speak ACP: JSON-RPC 2.0 over stdio, newline-delimited.

Configure agents in buddy's config:

    "acp_agents": {
        "claude": {"command": "claude-agent-acp", "args": []}
    }

The brain gets one tool: acp(agent, prompt, cwd) — spawns the agent process,
handshakes (initialize -> session/new -> session/prompt), collects the agent's
messages (and tool-call updates) until it stops, returns the transcript.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

from .util import _log_error

ACP_PROTOCOL_VERSION = 1
PROMPT_TIMEOUT = 600  # seconds — coding agents can think for a long time


class ACPError(Exception):
    pass


_ID_SEQ = [0]

def _next_id() -> int:
    _ID_SEQ[0] += 1
    return _ID_SEQ[0]


def _default_request_result(msg: dict):
    """Safe default answers for inbound agent→client requests. Permission
    asks are DENIED (buddy is unattended); anything else gets a JSON-RPC
    method-not-supported error. Never answering hangs the agent session."""
    method = str(msg.get("method") or "")
    if method == "session/request_permission":
        return {"outcome": {"outcome": "cancel"}}
    return {"_buddy_error": {"code": -32601, "message": f"not supported: {method}"}}


class _ACPSession:
    """One spawned agent process. A single reader thread owns stdout and
    dispatches responses to waiters and updates to the transcript."""

    def __init__(self, command: str, args: list[str], cwd: str):
        try:
            self.proc = subprocess.Popen(
                [command] + list(args),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, cwd=cwd or None,
                encoding="utf-8", errors="replace",  # one bad byte must not kill the reader
            )
        except OSError as e:
            raise ACPError(f"couldn't launch ACP agent '{command}': {e}")
        self._lock = threading.Lock()
        self._responses: dict[int, dict] = {}
        self.transcript: list[str] = []
        self._closed = False
        self._eof = False
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        # This thread is the ONLY consumer of proc.stdout. If it raises,
        # no further response is ever read and every request() then polls
        # to its deadline — one junk line used to hang a call for ~11 min.
        # So: never let a single malformed line escape, and never die.
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue  # tolerate banners / stray output
                if not isinstance(msg, dict):
                    continue  # JSON scalar/array (e.g. a bare progress number)
                try:
                    if "id" in msg and ("result" in msg or "error" in msg):
                        with self._lock:
                            self._responses[msg["id"]] = msg
                    elif "id" in msg and "method" in msg:
                        # inbound agent→client REQUEST (e.g. session/
                        # request_permission): never answering it hangs the
                        # agent until the full request timeout. Reply at once
                        # with a safe default instead of dropping the frame.
                        self._reply(msg["id"], _default_request_result(msg))
                    elif msg.get("method") == "session/update":
                        u = (msg.get("params") or {}).get("update") or {}
                        if not isinstance(u, dict):
                            continue
                        kind = u.get("sessionUpdate")
                        if kind == "agent_message_text":
                            self.transcript.append(
                                (u.get("content") or {}).get("text") or "")
                        elif kind == "tool_call":
                            title = u.get("title") or u.get("kind") or "tool"
                            self.transcript.append(f"[tool: {title}]")
                except Exception:
                    continue  # a bad frame must not stop the reader
        except Exception:
            pass
        finally:
            with self._lock:
                self._eof = True

    def _reply(self, rid, result):
        """Answer an inbound agent→client request on the stdout side."""
        def _w():
            try:
                self.proc.stdin.write(json.dumps(
                    {"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass  # stream is dead; request() will surface its own error
        # a full agent stdin buffer blocks write() — if this ran on the
        # reader thread it would stall stdout draining too (deadlock)
        threading.Thread(target=_w, daemon=True).start()

    def request(self, method: str, params: dict, timeout: float = 30) -> dict:
        rid = _next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise ACPError(f"agent stream broke during {method}: {e}")
        # monotonic, not time.time(): an NTP/DST step must not extend a
        # 600s prompt wait arbitrarily.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                resp = self._responses.pop(rid, None)
            if resp is not None:
                if "error" in resp:
                    raise ACPError(f"{method} failed: {resp['error']}")
                result = resp.get("result")
                return result if isinstance(result, dict) else {}
            if self._closed or self.proc.poll() is not None:
                raise ACPError(f"agent exited during {method}")
            if self._eof:
                # stdout closed: no response can ever arrive, so fail now
                # instead of polling to the deadline.
                raise ACPError(f"agent closed its output during {method} "
                               f"(no response)")
            time.sleep(0.02)
        raise ACPError(f"{method} timed out after {timeout}s")

    def drain(self, quiet_for: float = 0.25, max_wait: float = 2.0) -> None:
        """Wait until no new transcript line has arrived for `quiet_for`
        seconds (up to `max_wait` total), so the caller can read the agent's
        complete answer instead of whatever happened to be buffered."""
        deadline = time.monotonic() + max_wait
        last = -1
        while time.monotonic() < deadline:
            with self._lock:
                n = len(self.transcript)
            if n == last:
                return  # quiet
            last = n
            time.sleep(quiet_for)

    def close(self):
        self._closed = True
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        if self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except Exception:
                pass
        # Join the reader: it blocks on stdout, so closing the pipe above
        # ends it. Without this every acp call leaked a daemon thread.
        if self._reader.is_alive():
            self._reader.join(timeout=2.0)


def run_acp_agent(command: str, args: list[str], prompt: str, cwd: str,
                  timeout: float = PROMPT_TIMEOUT) -> str:
    """Full ACP session against one agent: initialize, new session, one prompt."""
    sess = _ACPSession(command, args, cwd)
    try:
        sess.request("initialize", {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
        }, timeout=30)
        session = sess.request("session/new", {"cwd": cwd or ".", "mcpServers": []},
                               timeout=30)
        sid = session.get("sessionId")
        if not sid:
            raise ACPError("session/new returned no sessionId")
        sess.request("session/prompt",
                     {"sessionId": sid,
                      "prompt": [{"type": "text", "text": prompt}]},
                     timeout=timeout)
    finally:
        # Drain trailing session/update frames before reaping. A fixed
        # sleep(0.2) was the only guarantee that the agent's final answer
        # had landed: anything larger than the pipe buffer (64KB) could not
        # drain in 0.2s, so `acp` silently returned a truncated reply that
        # the model then treated as complete. Wait for the transcript to go
        # quiet instead, with a hard ceiling.
        sess.drain(quiet_for=0.25, max_wait=2.0)
        sess.close()
        text = "\n".join(t for t in sess.transcript if t).strip()
    return text or "(ACP agent returned no message text)"


def list_agents(cfg: dict) -> str:
    agents = cfg.get("acp_agents") or {}
    if not agents:
        return ("(no ACP agents configured — add config \"acp_agents\": "
                "{\"claude\": {\"command\": \"claude-agent-acp\", \"args\": []}})")
    return "\n".join(f"{name}: {a.get('command', '?')} {' '.join(a.get('args') or [])}"
                     for name, a in agents.items())


def add_acp_agent(cfg: dict, name: str, command: str, args: list[str] | None = None) -> str:
    from .config import _save_config
    if "acp_agents" not in cfg or not isinstance(cfg["acp_agents"], dict):
        cfg["acp_agents"] = {}
    cfg["acp_agents"][name] = {"command": command, "args": args or []}
    _save_config(cfg)
    return f"ACP agent '{name}' configured: {command} {' '.join(args or [])}".strip()


def remove_acp_agent(cfg: dict, name: str) -> str:
    from .config import _save_config
    agents = cfg.get("acp_agents", {})
    if name in agents:
        del agents[name]
        _save_config(cfg)
        return f"ACP agent '{name}' removed."
    return f"ACP agent '{name}' not found."


def acp_tool(cfg: dict, agent: str, prompt: str, cwd: str = "", command: str = "", args: list[str] | None = None) -> str:
    if agent == "add" and command:
        name = prompt.strip() or "custom"
        return add_acp_agent(cfg, name, command, args or [])
    if agent == "remove":
        return remove_acp_agent(cfg, prompt.strip())
    agents = cfg.get("acp_agents") or {}
    if not agent or agent in ("list", "agents"):
        return list_agents(cfg)
    spec = agents.get(agent)
    if not spec:
        return f"(unknown ACP agent '{agent}' — configured: {', '.join(agents) or 'none'})"
    cmd = spec.get("command")
    if not cmd:
        return f"(ACP agent '{agent}' has no \"command\" in config)"
    try:
        return run_acp_agent(cmd, spec.get("args") or [], prompt, cwd or "")
    except ACPError as e:
        _log_error(e)
        return f"(ACP agent '{agent}' failed: {e})"


def cmd_acp(rest: list[str]) -> None:
    from .config import load_config
    cfg = load_config()
    sub = rest[0] if rest else "list"
    if sub == "list":
        agents = cfg.get("acp_agents") or {}
        print("Configured ACP agents:")
        if agents:
            for name, a in agents.items():
                cmd_str = f"{a.get('command', '?')} {' '.join(a.get('args') or [])}".strip()
                print(f"  • {name}: {cmd_str}")
        else:
            print("  (none configured)")
        import shutil
        detected = []
        if shutil.which("gemini"):
            detected.append("gemini (command: gemini --acp)")
        if shutil.which("claude-agent-acp"):
            detected.append("claude (command: claude-agent-acp)")
        elif shutil.which("claude"):
            detected.append("claude (command: claude --acp)")
        if detected:
            print("\nDetected available on host:")
            for d in detected:
                print(f"  • {d}")
            print("\nTo add: python3 buddy.py acp add <name> -- <command> [args...]")
    elif sub == "add":
        if len(rest) >= 4 and rest[2] == "--":
            name = rest[1]
            cmd = rest[3]
            args = rest[4:]
            print(add_acp_agent(cfg, name, cmd, args))
        else:
            print("usage: python3 buddy.py acp add <name> -- <command> [args...]")
    elif sub == "remove" and len(rest) >= 2:
        print(remove_acp_agent(cfg, rest[1]))
    elif sub == "run" and len(rest) >= 3:
        name = rest[1]
        prompt = " ".join(rest[2:])
        print(f"Driving ACP agent '{name}'...")
        res = acp_tool(cfg, name, prompt)
        print(res)
    else:
        print("usage: python3 buddy.py acp [list | add <name> -- <command> [args...] | remove <name> | run <name> <prompt>]")
