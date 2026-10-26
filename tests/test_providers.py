from pathlib import Path
from types import SimpleNamespace

from app.llm import GeminiDiagnoser, MockDiagnoser, make_diagnoser
from app.models import FailedJob, FailureCategory, FailureContext
from app.orchestrator import Orchestrator

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo-repo"


def make_ctx():
    return FailureContext(
        project_id=1, project_path="g/a", pipeline_id=5, ref="feature/x", sha="abc", commit_title="c",
        jobs=[FailedJob(1, "unit", "test", "FAILED tests/test_pricing.py::test_ten_percent_off")],
    )


class FakeHttp:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def post(self, url, json, headers):
        self.requests.append((url, json, headers))
        status, body = self.replies.pop(0)
        return SimpleNamespace(status_code=status, json=lambda: body, text=str(body))


def fn_call(name, args):
    return {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}, "thoughtSignature": "sig"}]}}]}


def test_gemini_tool_loop(settings):
    settings.gemini_api_key = "free-key"
    http = FakeHttp(
        [
            (200, fn_call("read_file", {"path": "src/pricing.py"})),
            (200, fn_call("submit_diagnosis", {
                "category": "test_failure", "root_cause": "divides by 10", "explanation": "e",
                "confidence": 0.9, "risk": "low", "retry_only": False,
                "changes": [{"path": "src/pricing.py", "new_content": "fixed"}],
            })),
        ]
    )
    d = GeminiDiagnoser(settings, lambda p: "source", lambda p: [], http=http).diagnose(make_ctx())

    assert d.root_cause == "divides by 10" and d.changes[0].new_content == "fixed"
    url, body, headers = http.requests[1]
    assert settings.gemini_model in url and headers["x-goog-api-key"] == "free-key"
    # model turn echoed back verbatim (keeps thought signature), then our tool result
    assert body["contents"][1]["parts"][0]["thoughtSignature"] == "sig"
    assert body["contents"][2]["parts"][0]["functionResponse"]["response"]["content"] == "source"


def test_gemini_error_is_raised(settings):
    http = FakeHttp([(400, {"error": "bad model"})])
    try:
        GeminiDiagnoser(settings, lambda p: None, lambda p: [], http=http).diagnose(make_ctx())
    except RuntimeError as exc:
        assert "400" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_mock_fixes_demo_bug_and_leaves_test_alone(settings):
    source = (DEMO / "src" / "pricing.py").read_text()
    d = MockDiagnoser(settings, lambda p: source if p == "src/pricing.py" else None, lambda p: []).diagnose(make_ctx())
    assert d.category == FailureCategory.TEST_FAILURE and d.confidence >= 0.9
    fixed = d.changes[0].new_content
    assert "percent / 100)" in fixed and [c.path for c in d.changes] == ["src/pricing.py"]
    # the fixed code really passes the demo test
    ns = {}
    exec(fixed, ns)
    assert ns["apply_discount"](200.0, 10) == 180.0 and ns["apply_discount"](99.99, 0) == 99.99


def test_mock_never_invents_fixes(settings):
    d = MockDiagnoser(settings, lambda p: None, lambda p: []).diagnose(make_ctx())
    assert d.changes == [] and d.confidence == 0.0


def test_factory_picks_provider(settings):
    for provider, cls in (("gemini", GeminiDiagnoser), ("mock", MockDiagnoser)):
        settings.llm_provider = provider
        assert isinstance(make_diagnoser(settings, lambda p: None, lambda p: []), cls)


def test_mock_end_to_end_opens_mr(settings, fake_gitlab, notifier):
    settings.llm_provider = "mock"
    fake_gitlab.jobs = [SimpleNamespace(id=1, name="unit", stage="test",
                                        log="FAILED tests/test_pricing.py::test_ten_percent_off - assert 0.0 == 180.0")]
    fake_gitlab.files = {"src/pricing.py": (DEMO / "src" / "pricing.py").read_text()}

    result = Orchestrator(settings, fake_gitlab, notifier).heal(1, 42)

    assert result.startswith("patched:")
    assert next(c for c in fake_gitlab.calls if c[0] == "commit")[2] == ["src/pricing.py"]
