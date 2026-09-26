#!/usr/bin/env python3
"""Mock ACP agent that behaves as a decision-engine brain for brain.py tests.

Replies 'FINAL: acp brain online' by default; set ACP_BRAIN_MOCK=toolcall in
the environment to get a TOOLCALL: line instead (the subprocess inherits the
test process's environment).
"""
import json
import os
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue

    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                          "result": {"protocolVersion": 1}}), flush=True)
    elif method == "session/new":
        print(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                          "result": {"sessionId": "mock-brain-1"}}), flush=True)
    elif method == "session/prompt":
        if os.environ.get("ACP_BRAIN_MOCK") == "toolcall":
            reply = 'TOOLCALL: {"name": "get_time", "args": {"tz": "UTC"}}'
        else:
            # multi-line on purpose: the protocol line must win even when the
            # agent wraps it in prose (the parser scans reversed lines)
            reply = "Let me think...\nFINAL: acp brain online"
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                          "params": {"update": {"sessionUpdate": "agent_message_text",
                                                "content": {"text": reply}}}}),
              flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}),
              flush=True)
