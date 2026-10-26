"""Claude-powered root-cause analysis and patch generation.

The model runs a short tool-use loop: it can read more repository files and list
directories, then must finish by calling `submit_diagnosis` with structured output.
Works against the Anthropic API or Claude on Vertex AI (switch via LLM_PROVIDER).
"""

import json
import logging
from typing import Callable

from app.config import Settings
from app.models import Diagnosis, FailureCategory, FailureContext, FileChange, Risk

log = logging.getLogger(__name__)

MAX_TOOL_TURNS = 8
MAX_FILE_CHARS = 40_000

SYSTEM_PROMPT = """You are a senior DevOps and software engineer acting as an autonomous \
CI/CD repair agent. A GitLab pipeline failed. Find the root cause from the job logs and \
source, then propose the smallest correct fix.

Rules:
- Fix the cause, not the symptom. Never delete, skip, weaken or xfail tests, lower coverage \
thresholds, add `allow_failure`, or silence linters to make a pipeline green, unless the test \
itself is demonstrably wrong; if so, say so explicitly in the explanation and set risk to "high".
- Prefer minimal diffs. Do not reformat or refactor unrelated code.
- For dependency conflicts, pin or adjust versions in the manifest; keep lockfiles consistent \
when they are present, and say if a lockfile must be regenerated.
- For memory failures, distinguish a genuine leak in code from an undersized CI job; fix the code \
when the log shows a leak.
- For deployment drift, describe the drift and prefer reconciling configuration-as-code with the \
live state; always set risk to "high" for anything touching infrastructure or deploy config.
- If the failure is transient (runner, network, registry rate limits), set retry_only=true and \
propose no changes.
- If you cannot identify the cause with reasonable confidence, return no changes and a low \
confidence. A wrong patch is worse than no patch.
- Use read_file / list_dir to inspect anything you need before deciding. Always return the \
COMPLETE new content of every changed file, never a diff or a partial snippet.
- Finish by calling submit_diagnosis exactly once."""

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the repository at the failing commit.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative path"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files and folders in a repository directory ('' for the root).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "submit_diagnosis",
        "description": "Submit the final root-cause analysis and proposed fix.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": [c.value for c in FailureCategory]},
                "root_cause": {"type": "string", "description": "One or two sentences."},
                "explanation": {
                    "type": "string",
                    "description": "Markdown: evidence from the logs, why the fix works, any follow-ups.",
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "risk": {"type": "string", "enum": [r.value for r in Risk]},
                "retry_only": {"type": "boolean"},
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "new_content": {"type": "string", "description": "Full file content"},
                        },
                        "required": ["path", "new_content"],
                    },
                },
            },
            "required": ["category", "root_cause", "explanation", "confidence", "risk", "retry_only", "changes"],
        },
    },
]


def build_client(settings: Settings):
    if settings.llm_provider == "vertex":
        from anthropic import AnthropicVertex

        return AnthropicVertex(project_id=settings.vertex_project_id, region=settings.vertex_region)
    from anthropic import Anthropic

    return Anthropic(api_key=settings.anthropic_api_key)


def render_context(ctx: FailureContext) -> str:
    parts = [
        f"# Pipeline failure\nProject: {ctx.project_path}\nRef: {ctx.ref}\nCommit: {ctx.sha[:12]} "
        f"— {ctx.commit_title}\nHeuristic category: {ctx.heuristic_category.value}\n"
        f"Fix attempt: {ctx.attempt}",
    ]
    if ctx.changed_files:
        parts.append("## Files changed in the triggering commit\n" + "\n".join(f"- {p}" for p in ctx.changed_files))
    for job in ctx.jobs:
        parts.append(f"## Failed job `{job.name}` (stage: {job.stage})\n```\n{job.log}\n```")
    for path, content in ctx.files.items():
        parts.append(f"## File: {path}\n```\n{content[:MAX_FILE_CHARS]}\n```")
    if ctx.extra_context:
        parts.append(f"## Runtime context (Cloud Logging)\n```\n{ctx.extra_context}\n```")
    return "\n\n".join(parts)


def no_conclusion(ctx: FailureContext, reason: str = "Model did not reach a conclusion.") -> Diagnosis:
    return Diagnosis(
        category=ctx.heuristic_category,
        root_cause=reason,
        explanation="",
        confidence=0.0,
        risk=Risk.HIGH,
        retry_only=False,
    )


class BaseDiagnoser:
    """Shared repository tools. Subclasses implement diagnose() for one provider."""

    def __init__(
        self,
        settings: Settings,
        read_file: Callable[[str], str | None],
        list_dir: Callable[[str], list[str]],
    ):
        self.settings = settings
        self.read_file = read_file
        self.list_dir = list_dir

    def diagnose(self, ctx: FailureContext) -> Diagnosis:  # pragma: no cover - interface
        raise NotImplementedError

    def _run_tool(self, name: str, args: dict, ctx: FailureContext) -> str:
        path = str(args.get("path", "")).lstrip("/")
        if ".." in path.split("/"):
            return "error: path traversal not allowed"
        if name == "read_file":
            content = ctx.files.get(path) or self.read_file(path)
            if content is None:
                return f"error: {path} not found"
            ctx.files[path] = content  # remember: needed to decide create vs update
            return content[:MAX_FILE_CHARS]
        if name == "list_dir":
            try:
                return "\n".join(self.list_dir(path)) or "(empty)"
            except Exception as exc:  # noqa: BLE001 - surface any API error to the model
                return f"error: {exc}"
        return f"error: unknown tool {name}"


class Diagnoser(BaseDiagnoser):
    """Claude, via the Anthropic API or Vertex AI."""

    def __init__(self, settings, read_file, list_dir, client=None):
        super().__init__(settings, read_file, list_dir)
        self.client = client or build_client(settings)

    def diagnose(self, ctx: FailureContext) -> Diagnosis:
        messages: list[dict] = [{"role": "user", "content": render_context(ctx)}]

        for turn in range(MAX_TOOL_TURNS):
            last_turn = turn == MAX_TOOL_TURNS - 1
            response = self.client.messages.create(
                model=self.settings.claude_model,
                max_tokens=self.settings.llm_max_tokens,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                # On the final turn, force the structured answer.
                tool_choice={"type": "tool", "name": "submit_diagnosis"} if last_turn else {"type": "any"},
                messages=messages,
            )
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            for block in tool_uses:
                if block.name == "submit_diagnosis":
                    return parse_diagnosis(block.input)

            if not tool_uses:
                break
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": self._run_tool(b.name, b.input, ctx),
                        }
                        for b in tool_uses
                    ],
                }
            )

        log.warning("model did not submit a diagnosis", extra={"event": "llm.no_diagnosis"})
        return no_conclusion(ctx)


class GeminiDiagnoser(BaseDiagnoser):
    """Google Gemini via the AI Studio REST API (works with a free-tier key).

    Same tool loop as Claude: the model may call read_file / list_dir, and must finish
    with submit_diagnosis. Model turns are sent back verbatim so Gemini's thought
    signatures are preserved between calls.
    """

    API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, settings, read_file, list_dir, http=None):
        super().__init__(settings, read_file, list_dir)
        import httpx

        self.http = http or httpx.Client(timeout=120)

    def _call(self, body: dict) -> dict:
        import time

        url = self.API.format(model=self.settings.gemini_model)
        headers = {"x-goog-api-key": self.settings.gemini_api_key}
        for attempt in range(3):
            resp = self.http.post(url, json=body, headers=headers)
            if resp.status_code in (429, 500, 503) and attempt < 2:
                # Rate limited (429) or model overloaded (500/503): wait and try again.
                time.sleep(20 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        raise RuntimeError("Gemini API rate limit: try again later (free-tier daily quota may be used up)")

    def diagnose(self, ctx: FailureContext) -> Diagnosis:
        declarations = [{"name": t["name"], "description": t["description"], "parameters": t["input_schema"]} for t in TOOLS]
        contents: list[dict] = [{"role": "user", "parts": [{"text": render_context(ctx)}]}]

        for turn in range(MAX_TOOL_TURNS):
            last_turn = turn == MAX_TOOL_TURNS - 1
            calling = {"mode": "ANY"}
            if last_turn:
                calling["allowedFunctionNames"] = ["submit_diagnosis"]
            data = self._call(
                {
                    "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                    "contents": contents,
                    "tools": [{"functionDeclarations": declarations}],
                    "toolConfig": {"functionCallingConfig": calling},
                    "generationConfig": {"maxOutputTokens": self.settings.llm_max_tokens},
                }
            )
            candidates = data.get("candidates") or []
            if not candidates or "content" not in candidates[0]:
                log.warning("gemini returned no content", extra={"event": "llm.no_diagnosis", "response": str(data)[:500]})
                break
            model_turn = candidates[0]["content"]
            calls = [p["functionCall"] for p in model_turn.get("parts", []) if "functionCall" in p]
            for call in calls:
                if call["name"] == "submit_diagnosis":
                    return parse_diagnosis(call.get("args", {}))
            if not calls:
                break
            contents.append(model_turn)
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": c["name"],
                                "response": {"content": self._run_tool(c["name"], c.get("args", {}), ctx)},
                            }
                        }
                        for c in calls
                    ],
                }
            )

        return no_conclusion(ctx)


class MockDiagnoser(BaseDiagnoser):
    """No AI calls. Recognises the planted bug in examples/demo-repo so the whole
    GitLab -> MR -> Slack flow can be tested for free; anything else is reported
    as 'not diagnosed' so it never pushes a made-up fix."""

    DEMO_FILE = "src/pricing.py"
    DEMO_BUG = "percent / 10)"

    def diagnose(self, ctx: FailureContext) -> Diagnosis:
        source = ctx.files.get(self.DEMO_FILE) or self.read_file(self.DEMO_FILE)
        if source and self.DEMO_BUG in source:
            ctx.files[self.DEMO_FILE] = source
            fixed = source.replace(self.DEMO_BUG, "percent / 100)").replace(
                "    # Bug planted for the demo: divides by 10 instead of 100.\n", ""
            )
            return Diagnosis(
                category=FailureCategory.TEST_FAILURE,
                root_cause="apply_discount() divides the percentage by 10 instead of 100.",
                explanation=(
                    "_Mock mode: canned diagnosis for the demo repo, no AI was called._\n\n"
                    "`test_ten_percent_off` expects 200 at 10% off to be 180, but "
                    "`price * (1 - percent / 10)` gives 0. Dividing by 100 converts the "
                    "percentage correctly; the test is right and is left unchanged."
                ),
                confidence=0.95,
                risk=Risk.LOW,
                retry_only=False,
                changes=[FileChange(self.DEMO_FILE, fixed)],
            )
        return no_conclusion(ctx, "Mock mode only recognises the demo repo bug; switch LLM_PROVIDER to analyse this failure.")


def make_diagnoser(settings: Settings, read_file, list_dir) -> BaseDiagnoser:
    if settings.llm_provider == "gemini":
        return GeminiDiagnoser(settings, read_file, list_dir)
    if settings.llm_provider == "mock":
        return MockDiagnoser(settings, read_file, list_dir)
    return Diagnoser(settings, read_file, list_dir)


def parse_diagnosis(data: dict | str) -> Diagnosis:
    if isinstance(data, str):
        data = json.loads(data)
    try:
        category = FailureCategory(data.get("category", "unknown"))
    except ValueError:
        category = FailureCategory.UNKNOWN
    try:
        risk = Risk(data.get("risk", "high"))
    except ValueError:
        risk = Risk.HIGH
    return Diagnosis(
        category=category,
        root_cause=str(data.get("root_cause", "")).strip(),
        explanation=str(data.get("explanation", "")).strip(),
        confidence=max(0.0, min(1.0, float(data.get("confidence", 0)))),
        risk=risk,
        retry_only=bool(data.get("retry_only", False)),
        changes=[
            FileChange(path=str(c["path"]).lstrip("/"), new_content=str(c["new_content"]))
            for c in data.get("changes", [])
            if c.get("path")
        ],
    )
