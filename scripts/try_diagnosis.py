"""Run the AI diagnosis on examples/demo-repo locally, with no GitLab needed.

    python scripts/try_diagnosis.py

Uses LLM_PROVIDER / keys from .env. With gemini this uses about 2-4 requests of
your free daily quota. Nothing is pushed anywhere; it only prints the result.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.llm import make_diagnoser  # noqa: E402
from app.models import FailedJob, FailureCategory, FailureContext  # noqa: E402

DEMO = ROOT / "examples" / "demo-repo"

FAKE_LOG = """$ pytest -q
F.                                                                       [100%]
=================================== FAILURES ===================================
_____________________________ test_ten_percent_off _____________________________

    def test_ten_percent_off():
>       assert apply_discount(200.0, 10) == 180.0
E       assert 0.0 == 180.0
E        +  where 0.0 = apply_discount(200.0, 10)

tests/test_pricing.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_pricing.py::test_ten_percent_off - assert 0.0 == 180.0
1 failed, 1 passed in 0.03s
ERROR: Job failed: exit code 1
"""


def read_file(path: str):
    target = (DEMO / path).resolve()
    if DEMO.resolve() not in target.parents or not target.is_file():
        return None
    return target.read_text(encoding="utf-8")


def list_dir(path: str):
    folder = (DEMO / path).resolve()
    if not folder.is_dir():
        return []
    return sorted(p.name + ("/" if p.is_dir() else "") for p in folder.iterdir())


settings = get_settings()
model = {"gemini": settings.gemini_model, "mock": "-"}.get(settings.llm_provider, settings.claude_model)
print(f"Provider: {settings.llm_provider}   Model: {model}\nThinking...\n")

ctx = FailureContext(
    project_id=0, project_path="local/demo-repo", pipeline_id=1, ref="feature/demo", sha="local",
    commit_title="demo", jobs=[FailedJob(1, "unit", "test", FAKE_LOG)],
    files={"tests/test_pricing.py": read_file("tests/test_pricing.py")},
    changed_files=["src/pricing.py"], heuristic_category=FailureCategory.TEST_FAILURE,
)
d = make_diagnoser(settings, read_file, list_dir).diagnose(ctx)

print(f"Category:   {d.category.value}")
print(f"Confidence: {d.confidence:.0%}   Risk: {d.risk.value}")
print(f"Root cause: {d.root_cause}\n")
for change in d.changes:
    print(f"--- proposed new {change.path} ---\n{change.new_content}")
if not d.changes:
    print("No file changes proposed.")
