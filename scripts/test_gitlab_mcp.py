"""Check that the GitLab MCP server works with your token, and list its tools.

    python scripts/test_gitlab_mcp.py

Reads GITLAB_URL and GITLAB_TOKEN from .env. Prints the available MCP tools.
"""

import json
import sys
from pathlib import Path

import httpx

env = {}
env_file = Path(__file__).resolve().parents[1] / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

base = env.get("GITLAB_URL", "https://gitlab.com").rstrip("/")
token = env.get("GITLAB_TOKEN", "")
if not token:
    sys.exit("GITLAB_TOKEN is empty in .env")

URL = f"{base}/api/v4/mcp"
headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def parse(resp: httpx.Response) -> dict:
    text = resp.text
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return {}
    return resp.json() if text.strip() else {}


with httpx.Client(timeout=30) as http:
    r = http.post(URL, headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "selfheal-agent", "version": "0.2"}},
    })
    print("initialize ->", r.status_code)
    if r.status_code >= 400:
        sys.exit(f"Error: {r.text[:500]}")
    init = parse(r)
    print("server:", init.get("result", {}).get("serverInfo"))
    session = r.headers.get("mcp-session-id")
    if session:
        headers["Mcp-Session-Id"] = session

    http.post(URL, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    r = http.post(URL, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    print("tools/list ->", r.status_code)
    data = parse(r)
    if "error" in data or r.status_code >= 400:
        sys.exit(f"Error: {data.get('error') or r.text[:500]}")
    tools = data.get("result", {}).get("tools", [])
    print(f"\n{len(tools)} tools available:")
    for t in tools:
        print(" -", t["name"])
