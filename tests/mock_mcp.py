#!/usr/bin/env python3
"""Mock MCP server for testing: offers one echo tool."""
import json, sys

for line in sys.stdin:
    msg = json.loads(line)
    if msg["method"] == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}}), flush=True)
    elif msg["method"] == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
            {"name": "echo", "description": "Echo back text",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}
        ]}}), flush=True)
    elif msg["method"] == "tools/call":
        text = msg["params"]["arguments"]["text"]
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "echo: " + text}]}}), flush=True)
