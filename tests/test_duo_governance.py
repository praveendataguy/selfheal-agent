"""Duo mode: a GitLab Duo flow writes the fix MR; this service governs it."""

from types import SimpleNamespace

import pytest

from app.orchestrator import Orchestrator

OLD = "def apply_discount(price, percent):\n    return round(price * (1 - percent / 10), 2)\n"
NEW = OLD.replace("/ 10", "/ 100")


@pytest.fixture
def duo(settings, fake_gitlab, notifier):
    settings.healer = "duo"
    return Orchestrator(settings, fake_gitlab, notifier), fake_gitlab, notifier


def add_mr(gl, iid=5, target="feature/x", source="duo/fix-pipeline", author="ai-fix-pipeline-group",
           paths=(("src/pricing.py", False),), new=NEW, labels=()):
    gl.mrs[iid] = {
        "iid": iid, "title": "Fix discount calculation", "description": "Duo explanation",
        "labels": list(labels), "author": author, "source_branch": source, "target_branch": target,
        "web_url": f"https://gitlab.example/mr/{iid}", "state": "opened",
    }
    gl.mr_paths[iid] = list(paths)
    for path, _ in paths:
        gl.branch_files[(source, path)] = new
        gl.branch_files[(target, path)] = OLD


def mr_event(iid=5, action="open"):
    return {"object_kind": "merge_request", "project": {"id": 1, "path_with_namespace": "group/app"},
            "object_attributes": {"iid": iid, "action": action}}


def test_safe_duo_fix_on_feature_branch_is_verified_automatically(duo):
    orch, gl, notifier = duo
    add_mr(gl)
    result = orch.handle_merge_request_event(mr_event())
    assert result.startswith("verifying")
    assert ("run_pipeline", "duo/fix-pipeline") in gl.calls
    assert "self-heal" in gl.mrs[5]["labels"] and "duo-flow" in gl.mrs[5]["labels"]
    assert notifier.sent == []


def test_duo_fix_targeting_main_waits_for_slack(duo):
    orch, gl, notifier = duo
    add_mr(gl, target="main")
    result = orch.handle_merge_request_event(mr_event())
    assert result.startswith("awaiting approval")
    assert gl.mrs[5]["title"].startswith("Draft: ")
    assert "run_pipeline" not in gl.kinds()
    req = notifier.sent[0]
    assert req.branch == "duo/fix-pipeline" and req.target_ref == "main"


def test_duo_fix_that_skips_tests_needs_approval(duo):
    orch, gl, notifier = duo
    old_test = "def test_a():\n    assert f() == 1\n    assert g() == 2\n"
    add_mr(gl, paths=(("tests/test_a.py", False),), new="def test_a():\n    pass\n")
    gl.branch_files[("feature/x", "tests/test_a.py")] = old_test
    assert orch.handle_merge_request_event(mr_event()).startswith("awaiting approval")


def test_duo_fix_touching_secrets_is_blocked(duo):
    orch, gl, notifier = duo
    add_mr(gl, paths=(("certs/server.pem", False),))
    assert orch.handle_merge_request_event(mr_event()).startswith("blocked")
    assert "self-heal::blocked" in gl.mrs[5]["labels"]
    assert "run_pipeline" not in gl.kinds()


def test_human_mrs_are_ignored(duo):
    orch, gl, _ = duo
    add_mr(gl, author="praveendataguy", source="feature/y")
    assert orch.handle_merge_request_event(mr_event()) == "ignored: not a Duo fix MR"


def test_merge_request_events_ignored_in_agent_mode(settings, fake_gitlab, notifier):
    assert Orchestrator(settings, fake_gitlab, notifier).handle_merge_request_event(mr_event()).startswith("ignored")


def test_verification_success_and_failure_are_reported(duo):
    orch, gl, notifier = duo
    add_mr(gl, labels=("self-heal",))
    gl.open_mrs["duo/fix-pipeline"] = SimpleNamespace(iid=5, web_url="u", source_branch="duo/fix-pipeline", target_branch="feature/x")
    ok = {"object_attributes": {"id": 9, "status": "success", "ref": "duo/fix-pipeline"}, "project": {"id": 1}}
    assert orch.handle_pipeline_event(ok) == "verified"
    bad = {"object_attributes": {"id": 10, "status": "failed", "ref": "duo/fix-pipeline"}, "project": {"id": 1}}
    assert orch.handle_pipeline_event(bad) == "fix failed verification"
    assert "verified" in notifier.sent[0] and "failed verification" in notifier.sent[1]


def test_failures_outside_fix_mrs_are_left_to_the_duo_flow(duo):
    orch, gl, _ = duo
    p = {"object_attributes": {"id": 11, "status": "failed", "ref": "main"}, "project": {"id": 1}}
    assert orch.handle_pipeline_event(p) == "left to Duo flow"
    assert "commit" not in gl.kinds()


def test_slack_approval_works_for_duo_branch_names(duo):
    orch, gl, _ = duo
    add_mr(gl, target="main", labels=("self-heal",))
    orch.handle_decision(True, 1, 5, "duo/fix-pipeline", "alice")
    assert ("run_pipeline", "duo/fix-pipeline") in gl.calls


def test_slack_approval_refuses_ungoverned_branch(duo):
    orch, gl, _ = duo
    add_mr(gl, target="main")  # no self-heal label yet
    with pytest.raises(ValueError):
        orch.handle_decision(True, 1, 5, "duo/fix-pipeline", "mallory")


def test_fix_pipeline_flow_mr_recognised_by_branch_and_title(duo):
    # Duo flows run with composite identity, so the author is the human user
    orch, gl, _ = duo
    add_mr(gl, author="praveendataguy", source="duo/fix/pricing-discount-percent-1")
    gl.mrs[5]["title"] = "Draft: [FixPipeline] https://gitlab.example/pipelines/1"
    assert orch.handle_merge_request_event(mr_event(action="reopen")).startswith("verifying")
