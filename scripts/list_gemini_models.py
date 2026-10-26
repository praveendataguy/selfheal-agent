"""List the Gemini models your API key can use for text generation.

    python scripts/list_gemini_models.py

Reads GEMINI_API_KEY from .env (or the environment). Put one of the printed
names (without the "models/" prefix) in GEMINI_MODEL. Prefer a "flash" model:
those are the ones on the free tier.
"""

import os
import sys
from pathlib import Path

import httpx

key = os.environ.get("GEMINI_API_KEY", "")
env = Path(__file__).resolve().parents[1] / ".env"
if not key and env.exists():
    for line in env.read_text().splitlines():
        if line.startswith("GEMINI_API_KEY="):
            key = line.split("=", 1)[1].strip()
if not key:
    sys.exit("Set GEMINI_API_KEY in .env first.")

resp = httpx.get(
    "https://generativelanguage.googleapis.com/v1beta/models",
    headers={"x-goog-api-key": key},
    params={"pageSize": 200},
    timeout=30,
)
if resp.status_code != 200:
    sys.exit(f"Error {resp.status_code}: {resp.text[:300]}")

for m in resp.json().get("models", []):
    if "generateContent" in m.get("supportedGenerationMethods", []):
        print(m["name"].removeprefix("models/"), "-", m.get("displayName", ""))
