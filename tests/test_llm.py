from app.llm import Diagnoser
from app.models import FailedJob, FailureCategory, FailureContext, Risk
from tests.conftest import FakeAnthropic, tool_use


def make_ctx():
    return FailureContext(
        project_id=1, project_path="g/a", pipeline_id=5, ref="feature/x", sha="abc", commit_title="c",
        jobs=[FailedJob(1, "unit", "test", "FAILED tests/test_p.py::test_x")],
    )


def test_tool_loop_reads_file_then_submits(settings):
    client = FakeAnthropic(
        [
            [tool_use("read_file", {"path": "src/p.py"}, "tu_1")],
            [
                tool_use(
                    "submit_diagnosis",
                    {
                        "category": "test_failure",
                        "root_cause": "off-by-one in parse()",
                        "explanation": "...",
                        "confidence": 0.86,
                        "risk": "low",
                        "retry_only": False,
                        "changes": [{"path": "/src/p.py", "new_content": "fixed\n"}],
                    },
                    "tu_2",
                )
            ],
        ]
    )
    ctx = make_ctx()
    d = Diagnoser(settings, read_file=lambda p: "original\n", list_dir=lambda p: [], client=client).diagnose(ctx)

    assert d.category == FailureCategory.TEST_FAILURE
    assert d.risk == Risk.LOW and d.confidence == 0.86
    assert d.changes[0].path == "src/p.py"  # leading slash stripped
    assert ctx.files["src/p.py"] == "original\n"  # read files are remembered
    # second request carries the tool result back to the model
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["content"] == "original\n"
    assert client.requests[0]["model"] == settings.claude_model


def test_path_traversal_rejected_in_tools(settings):
    dg = Diagnoser(settings, read_file=lambda p: "secret", list_dir=lambda p: [], client=object())
    assert dg._run_tool("read_file", {"path": "../../etc/passwd"}, make_ctx()).startswith("error")
