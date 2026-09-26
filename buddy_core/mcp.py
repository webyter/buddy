"""buddy_core.mcp — MCP (Model Content Protocol) stdio servers and the manager.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
from .config import HOME, MCP_CONFIG

import json
import queue
try:
    import readline  # type: ignore
except ImportError:  # exotic platforms — chat() falls back to plain input()
    readline = None
try:  # command palette: raw-mode tty handling (absent on Windows)
    import select as _select
    import termios as _termios
except ImportError:
    _select = _termios = None
import subprocess
import threading
import time

# ---- original buddy.py lines 1399-1401 --------------------------------

# ==================================================================== MCP ==

# ---- original buddy.py lines 1402-1518 --------------------------------
class MCPServer:
    def __init__(self, name: str, command: list[str]):
        self.name = name
        self.command = command
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.dead: str = ""   # non-empty = unusable, with the reason
        self._id = 0
        self._lock = threading.Lock()
        self._out_q: queue.Queue = queue.Queue(maxsize=1000)
        self._log_file = None
        self._reader: threading.Thread | None = None

    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                try:
                    self._out_q.put_nowait(line)
                except queue.Full:
                    # Notifications nobody is waiting for: drop the oldest
                    # rather than growing without bound in the daemon.
                    try:
                        self._out_q.get_nowait()
                        self._out_q.put_nowait(line)
                    except (queue.Empty, queue.Full):
                        pass
        except Exception:
            pass
        try:
            self._out_q.put_nowait(None)  # EOF sentinel
        except queue.Full:
            pass

    def _rpc(self, method: str, params: dict | None = None, notify: bool = False):
        if self.dead:
            raise RuntimeError(f"MCP server {self.name} is not running "
                               f"({self.dead}) — restart buddy to reload it")
        # Start the budget BEFORE queueing on the lock. One request/response
        # pipe means calls are serialized, so a caller that waited 50s behind
        # a slow call used to get its own fresh 60s on top (N queued callers
        # could pile up to N x 60s). Now the wait counts against the call.
        budget_deadline = time.monotonic() + 60.0
        if not self._lock.acquire(timeout=60.0):
            raise RuntimeError(f"MCP server {self.name}: timed out waiting for "
                               f"a free connection")
        try:
            if self.dead:
                raise RuntimeError(f"MCP server {self.name} is not running "
                                   f"({self.dead}) — restart buddy to reload it")
            if not (self.proc and self.proc.stdin and self.proc.stdout):
                raise RuntimeError(f"MCP server {self.name} has no process")
            msg = {"jsonrpc": "2.0", "method": method}
            if not notify:
                self._id += 1
                msg["id"] = self._id
            if params is not None:
                msg["params"] = params
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._mark_dead(f"pipe closed ({e})")
                raise RuntimeError(f"MCP server {self.name} died: {e}")
            if notify:
                return None
            request_id = self._id
            # total budget started before the lock, so queue time counts
            deadline = budget_deadline
            line = None                          # a chatty server emitting
            while True:                          # notifications must not reset it
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    line = self._out_q.get(timeout=remaining)
                except queue.Empty:
                    break
                if line is None:
                    self._mark_dead("process exited")
                    raise RuntimeError(f"MCP server {self.name} died")
                if not line.strip():
                    continue  # a blank line is NOT an EOF sentinel
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(resp, dict):
                    continue  # tolerate non-JSON-RPC chatter
                if resp.get("id") != request_id:
                    continue
                if "error" in resp:
                    raise RuntimeError(f"MCP {self.name}: {resp['error']}")
                if "result" not in resp:
                    raise RuntimeError(f"MCP {self.name}: malformed response")
                return resp["result"]
            # Timed out. Tear the server down properly: leaving the dead
            # entry registered kept advertising its tools to the model and
            # every later call hit a broken pipe.
            self._mark_dead("timed out after 60s")
            raise RuntimeError(f"MCP server {self.name} timed out and was "
                               f"stopped — restart buddy to reload it")
        finally:
            self._lock.release()

    def _mark_dead(self, reason: str) -> None:
        """Record the failure and drop the tools so nothing keeps calling a
        server that can no longer answer. Idempotent."""
        self.dead = self.dead or reason
        self.tools = []
        try:
            if self.proc is not None:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, ValueError):
            pass
        finally:
            self.proc = None
            if self._log_file:
                try:
                    self._log_file.close()
                except OSError:
                    pass
                self._log_file = None

    def start(self):
        log_dir = HOME / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = open(log_dir / f"mcp-{self.name}.log", "ab")
        try:
            self.proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log_file,
                text=True,
                encoding="utf-8", errors="replace",  # one bad byte must not kill the reader
            )
        except Exception:
            self._log_file.close()  # shutdown() only closes it when proc is set
            self._log_file = None
            raise
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        try:
            self._rpc(
                "initialize",
                {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "buddy", "version": "3.0"},
                },
            )
            self._rpc("notifications/initialized", notify=True)
            result = self._rpc("tools/list", {})
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise RuntimeError(f"MCP {self.name}: invalid tools/list result")
            self.tools = result["tools"]
        except Exception:
            self.shutdown()
            raise

    def call(self, tool: str, args: dict) -> str:
        result = self._rpc("tools/call", {"name": tool, "arguments": args})
        if not isinstance(result, dict):
            raise RuntimeError(f"MCP {self.name}: invalid tools/call result")
        if result.get("isError"):
            raise RuntimeError("MCP tool reported an error")
        parts = result.get("content", [])
        if not isinstance(parts, list):
            return "(no content)"
        return "\n".join(p.get("text", "") for p in parts
                         if isinstance(p, dict) and p.get("type") == "text") or "(no content)"

    def shutdown(self):
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            finally:
                self.proc = None
                if self._log_file:
                    try:
                        self._log_file.close()
                    except OSError:
                        pass
                    self._log_file = None
        if self._reader is not None:
            # stdout is owned by the reader; closing it unblocks the thread
            # so a server shutdown doesn't leak it.
            try:
                if self.proc is None and self._reader.is_alive():
                    self._reader.join(timeout=1)
            except Exception:
                pass
            self._reader = None

# ---- original buddy.py lines 1519-1520 --------------------------------


# ---- original buddy.py lines 1521-1580 --------------------------------
class MCPManager:
    def __init__(self):
        self.servers: dict[str, MCPServer] = {}
        self.error: dict[str, str] = {}

    @property
    def config(self) -> dict:
        if MCP_CONFIG.exists():
            try:
                data = json.loads(MCP_CONFIG.read_text())
                return data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}
        return {}

    def start_all(self):
        for name, spec in self.config.items():
            try:
                if not isinstance(spec, dict) or not spec.get("command"):
                    raise ValueError("entry needs a 'command' string")
                args = spec.get("args") or []
                if isinstance(args, str):
                    args = [args]
                cmd = [str(spec["command"])] + [str(a) for a in args]
                srv = MCPServer(name, cmd)
                srv.start()
                self.servers[name] = srv
            except Exception as e:
                self.error[name] = str(e)

    def all_tools(self) -> list[dict]:
        out = []
        for name, srv in self.servers.items():
            if srv.dead:
                continue  # a dead server must stop being advertised
            for t in srv.tools:
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"mcp__{name}__{t['name']}",
                            "description": t.get("description", ""),
                            "parameters": t.get("inputSchema", {"type": "object"}),
                        },
                    }
                )
        return out

    def call_tool(self, namespaced: str, args: dict) -> str:
        parts = namespaced.split("__", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return f"(malformed MCP tool name: {namespaced!r})"
        _, server, tool = parts
        if server not in self.servers:
            return f"(unknown MCP server: {server!r})"
        srv = self.servers[server]
        if srv.dead:
            return f"(MCP server {server!r} is not running: {srv.dead} — restart buddy to reload it)"
        return srv.call(tool, args)

    def shutdown(self):
        for s in self.servers.values():
            s.shutdown()

    def cmd_add(self, name: str, cmd_argv: list[str]) -> None:
        if not cmd_argv:
            raise SystemExit("usage: buddy.py mcp add <name> -- <command> [args...]")
        cfg = self.config
        cfg[name] = {"command": cmd_argv[0], "args": cmd_argv[1:]}
        MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        MCP_CONFIG.write_text(json.dumps(cfg, indent=2))
        print(f"Added MCP server '{name}'. Restart buddy to load it.")

    def cmd_list(self) -> None:
        for name, spec in self.config.items():
            if not isinstance(spec, dict):
                print(f"{name}: (invalid entry — needs a JSON object) [not started]")
                continue
            args = spec.get("args") or []
            if isinstance(args, str):
                args = [args]
            status = "running" if name in self.servers else self.error.get(name, "not started")
            srv = self.servers.get(name)
            n = len(srv.tools) if srv is not None and not srv.dead else "?"
            if srv is not None and srv.dead:
                status = f"stopped ({srv.dead})"
            cmd = " ".join([str(spec.get("command", "?"))] + [str(a) for a in args])
            print(f"{name}: {cmd} [{status}, tools: {n}]")

