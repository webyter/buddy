#!/usr/bin/env python3
"""Mock ACP agent for testing."""
import json
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
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"protocolVersion": 1}
        }), flush=True)

    elif method == "session/new":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"sessionId": "mock-session-123"}
        }), flush=True)

    elif method == "session/prompt":
        prompt_text = ""
        prompt_arr = msg.get("params", {}).get("prompt", [])
        if prompt_arr and len(prompt_arr) > 0:
            prompt_text = prompt_arr[0].get("text", "")

        # Emit the transcript update
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_text",
                    "content": {"text": prompt_text}
                }
            }
        }), flush=True)

        # Acknowledge the prompt completion
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {}
        }), flush=True)
