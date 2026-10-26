from app import policy
from app.models import Diagnosis, FailedJob, FailureCategory, FailureContext, FileChange, Risk


def ctx(ref="feature/x", stage="test", files=None):
    return FailureContext(
        project_id=1, project_path="g/a", pipeline_id=1, ref=ref, sha="s", commit_title="c",
        jobs=[FailedJob(1, "unit", stage, "")], files=files or {}, original_ref=ref,
    )


def diag(changes, confidence=0.9, risk=Risk.LOW, category=FailureCategory.TEST_FAILURE):
    return Diagnosis(category, "rc", "ex", confidence, risk, False, [FileChange(p, c) for p, c in changes])


def test_low_risk_feature_branch_fix_is_auto(settings):
    d = policy.evaluate(diag([("src/a.py", "x = 1\n")]), ctx(), settings)
    assert d.allowed and not d.needs_approval


def test_production_ref_requires_approval(settings):
    d = policy.evaluate(diag([("src/a.py", "x")]), ctx(ref="main"), settings)
    assert d.allowed and d.needs_approval
    assert any("production" in r for r in d.reasons)


def test_glob_production_ref(settings):
    assert policy.evaluate(diag([("src/a.py", "x")]), ctx(ref="release/2.1"), settings).needs_approval


def test_sensitive_path_and_deploy_stage_require_approval(settings):
    assert policy.evaluate(diag([("Dockerfile", "FROM x")]), ctx(), settings).needs_approval
    assert policy.evaluate(diag([("src/a.py", "x")]), ctx(stage="deploy"), settings).needs_approval


def test_blockers(settings):
    assert not policy.evaluate(diag([]), ctx(), settings).allowed
    assert not policy.evaluate(diag([("src/a.py", "x")], confidence=0.2), ctx(), settings).allowed
    assert not policy.evaluate(diag([("../etc/passwd", "x")]), ctx(), settings).allowed
    assert not policy.evaluate(diag([("certs/server.pem", "x")]), ctx(), settings).allowed
    many = [(f"src/f{i}.py", "x") for i in range(10)]
    assert not policy.evaluate(diag(many), ctx(), settings).allowed


def test_test_weakening_detected(settings):
    old = "def test_a():\n    assert f() == 1\n    assert g() == 2\n"
    skipped = "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert f() == 1\n    assert g() == 2\n"
    gutted = "def test_a():\n    pass\n"
    for new in (skipped, gutted):
        d = policy.evaluate(diag([("tests/test_a.py", new)]), ctx(files={"tests/test_a.py": old}), settings)
        assert d.needs_approval, new
    fine = old.replace("== 2", "== 3")
    d = policy.evaluate(diag([("tests/test_a.py", fine)]), ctx(files={"tests/test_a.py": old}), settings)
    assert not d.needs_approval
