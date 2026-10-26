from types import SimpleNamespace

from app.models import Diagnosis, FailureCategory, FileChange, Risk
from app.orchestrator import Orchestrator
from tests.conftest import FakeDiagnoser

TEST_LOG = 'FAILED tests/test_parser.py::test_dates - AssertionError\nFile "/builds/group/app/src/parser.py", line 9'


def payload(status="failed", ref="feature/x", message=""):
    return {
        "object_kind": "pipeline",
        "object_attributes": {"id": 42, "status": status, "ref": ref},
        "project": {"id": 1, "path_with_namespace": "group/app"},
        "commit": {"message": message},
    }


def job(name="unit", stage="test", log=TEST_LOG, id_=100):
    return SimpleNamespace(id=id_, name=name, stage=stage, log=log)


def good_fix(path="src/parser.py", risk=Risk.LOW):
    return Diagnosis(FailureCategory.TEST_FAILURE, "Date parser ignores timezone", "details", 0.9, risk, False,
                     [FileChange(path, "fixed\n")])


def orch(settings, gl, notifier):
    return Orchestrator(settings, gl, notifier, diagnoser_factory=FakeDiagnoser)


def test_feature_branch_failure_opens_mr_without_approval(settings, fake_gitlab, notifier):
    fake_gitlab.jobs = [job()]
    fake_gitlab.files = {"src/parser.py": "old\n", "tests/test_parser.py": "assert x\n"}
    FakeDiagnoser.result = good_fix()

    result = orch(settings, fake_gitlab, notifier).handle_pipeline_event(payload())

    assert result.startswith("patched:")
    commit = next(c for c in fake_gitlab.calls if c[0] == "commit")
    _, branch, paths, message, start_sha, existing = commit
    assert branch == "selfheal/feature-x-42"
    assert start_sha == "abc123def456" and existing == {"src/parser.py"}
    assert "Self-Heal-Attempt: 1" in message and "Self-Heal-Target: feature/x" in message
    assert "[skip ci]" not in message
    mr = next(c for c in fake_gitlab.calls if c[0] == "open_mr")
    assert mr[2] == "feature/x" and not mr[3].startswith("Draft")
    assert notifier.sent == []
    # context gathering picked up files referenced in the log
    assert "src/parser.py" in FakeDiagnoser.last_ctx.files


def test_main_branch_failure_requires_slack_approval(settings, fake_gitlab, notifier):
    fake_gitlab.ref = "main"
    fake_gitlab.jobs = [job()]
    fake_gitlab.files = {"src/parser.py": "old\n"}
    FakeDiagnoser.result = good_fix()

    result = orch(settings, fake_gitlab, notifier).handle_pipeline_event(payload(ref="main"))

    assert result.startswith("awaiting approval")
    message = next(c for c in fake_gitlab.calls if c[0] == "commit")[3]
    assert "[skip ci]" in message  # patch must not run before a human says so
    assert next(c for c in fake_gitlab.calls if c[0] == "open_mr")[3].startswith("Draft: ")
    req = notifier.sent[0]
    assert req.target_ref == "main" and req.mr_iid == 7 and req.branch == "selfheal/main-42"


def test_duplicate_webhook_is_ignored(settings, fake_gitlab, notifier):
    fake_gitlab.branches.add("selfheal/feature-x-42")
    assert orch(settings, fake_gitlab, notifier).heal(1, 42).startswith("ignored")


def test_infra_flake_retries_once_without_llm(settings, fake_gitlab, notifier):
    fake_gitlab.jobs = [job(log="ERROR: Job failed (system failure): no space left on device")]
    FakeDiagnoser.result = None  # would crash if the model were called
    assert orch(settings, fake_gitlab, notifier).heal(1, 42) == "retried: transient failure"
    assert ("retry", 100) in fake_gitlab.calls


def test_low_confidence_reports_only(settings, fake_gitlab, notifier):
    fake_gitlab.jobs = [job()]
    d = good_fix()
    d.confidence = 0.2
    FakeDiagnoser.result = d
    result = orch(settings, fake_gitlab, notifier).heal(1, 42)
    assert result.startswith("reported:")
    assert "commit" not in fake_gitlab.kinds()
    assert "Root cause" in notifier.sent[0]


def test_dry_run_never_pushes(settings, fake_gitlab, notifier):
    settings.dry_run = True
    fake_gitlab.jobs = [job()]
    FakeDiagnoser.result = good_fix()
    assert orch(settings, fake_gitlab, notifier).heal(1, 42).startswith("dry-run")
    assert fake_gitlab.calls == []


def test_second_attempt_commits_on_same_branch(settings, fake_gitlab, notifier):
    branch = "selfheal/feature-x-42"
    fake_gitlab.ref = branch
    fake_gitlab.commit_message = (
        "fix: x\n\nSelf-Heal-Attempt: 1\nSelf-Heal-Target: feature/x\nSelf-Heal-Source-Pipeline: 42\n"
    )
    fake_gitlab.open_mrs[branch] = SimpleNamespace(iid=7, web_url="u", source_branch=branch, target_branch="feature/x")
    fake_gitlab.jobs = [job()]
    FakeDiagnoser.result = good_fix()

    result = orch(settings, fake_gitlab, notifier).heal(1, 43)

    assert result.startswith("patched")
    commit = next(c for c in fake_gitlab.calls if c[0] == "commit")
    assert commit[1] == branch and commit[4] is None  # continue on top of the branch
    assert "Self-Heal-Attempt: 2" in commit[3]
    assert "open_mr" not in fake_gitlab.kinds() and "comment" in fake_gitlab.kinds()


def test_attempt_limit_stops_and_escalates(settings, fake_gitlab, notifier):
    branch = "selfheal/feature-x-42"
    fake_gitlab.ref = branch
    fake_gitlab.commit_message = "fix\n\nSelf-Heal-Attempt: 2\nSelf-Heal-Target: feature/x\n"
    assert orch(settings, fake_gitlab, notifier).heal(1, 44) == "stopped: attempt limit"
    assert "gave up" in notifier.sent[0]


def test_human_commit_on_selfheal_branch_is_left_alone(settings, fake_gitlab, notifier):
    fake_gitlab.ref = "selfheal/feature-x-42"
    fake_gitlab.commit_message = "tweak by a human"
    assert orch(settings, fake_gitlab, notifier).heal(1, 45).startswith("ignored")


def test_success_on_selfheal_branch_notifies(settings, fake_gitlab, notifier):
    branch = "selfheal/feature-x-42"
    fake_gitlab.open_mrs[branch] = SimpleNamespace(iid=7, web_url="u", source_branch=branch, target_branch="x")
    p = payload(status="success", ref=branch, message="fix\n\nSelf-Heal-Attempt: 1\n")
    assert orch(settings, fake_gitlab, notifier).handle_pipeline_event(p) == "verified"
    assert "verified" in notifier.sent[0]


def test_approve_and_reject(settings, fake_gitlab, notifier):
    o = orch(settings, fake_gitlab, notifier)
    o.handle_decision(True, 1, 7, "selfheal/main-42", "alice")
    assert ("ready", 7) in fake_gitlab.calls and ("run_pipeline", "selfheal/main-42") in fake_gitlab.calls
    o.handle_decision(False, 1, 7, "selfheal/main-42", "bob")
    assert ("close", 7) in fake_gitlab.calls and ("delete_branch", "selfheal/main-42") in fake_gitlab.calls


def test_decision_refuses_non_selfheal_branch(settings, fake_gitlab, notifier):
    import pytest

    with pytest.raises(ValueError):
        orch(settings, fake_gitlab, notifier).handle_decision(False, 1, 7, "main", "mallory")
    assert fake_gitlab.calls == []
