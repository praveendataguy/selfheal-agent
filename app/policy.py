"""Guardrails deciding whether a proposed patch may be pushed, and whether a human must approve."""

import fnmatch
import re

from app.config import Settings
from app.models import Diagnosis, FailureCategory, FailureContext, PolicyDecision, Risk

CRITICAL_STAGES = {"deploy", "release", "production", "prod", "rollout"}
TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(_test|\.test|\.spec|_spec)\.\w+$|(^|/)test_[^/]+$")
TEST_WEAKENING_RE = re.compile(
    r"pytest\.mark\.(skip|xfail)|@unittest\.skip|\bit\.skip\(|\bxit\(|\bdescribe\.skip\(|"
    r"\btest\.skip\(|t\.Skip\(|@Disabled|@Ignore|allow_failure:\s*true",
)
ASSERT_RE = re.compile(r"\bassert|\bexpect\(|\bassert[A-Z]\w*\(|\bshould\b")


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path.split("/")[-1], p) for p in patterns)


def weakens_tests(path: str, old: str | None, new: str) -> bool:
    """Heuristic: adds skip markers, or drops a meaningful share of assertions in a test file."""
    is_test = bool(TEST_PATH_RE.search(path)) or path.endswith(".gitlab-ci.yml")
    if not is_test:
        return False
    old = old or ""
    if len(TEST_WEAKENING_RE.findall(new)) > len(TEST_WEAKENING_RE.findall(old)):
        return True
    old_asserts, new_asserts = len(ASSERT_RE.findall(old)), len(ASSERT_RE.findall(new))
    return old_asserts > 0 and new_asserts < old_asserts * 0.8


def evaluate(diagnosis: Diagnosis, ctx: FailureContext, settings: Settings) -> PolicyDecision:
    blockers: list[str] = []
    approval: list[str] = []

    if diagnosis.retry_only:
        return PolicyDecision(allowed=False, needs_approval=False, reasons=["transient failure: retry only"])
    if not diagnosis.changes:
        blockers.append("no changes proposed")
    if diagnosis.confidence < settings.min_confidence:
        blockers.append(f"confidence {diagnosis.confidence:.2f} below {settings.min_confidence}")
    if len(diagnosis.changes) > settings.max_files_changed:
        blockers.append(f"{len(diagnosis.changes)} files changed (max {settings.max_files_changed})")
    total = sum(len(c.new_content.encode()) for c in diagnosis.changes)
    if total > settings.max_patch_bytes:
        blockers.append(f"patch is {total} bytes (max {settings.max_patch_bytes})")

    for change in diagnosis.changes:
        p = change.path
        if p.startswith("/") or ".." in p.split("/") or not p:
            blockers.append(f"invalid path: {p!r}")
            continue
        if matches(p, settings.forbidden_path_patterns):
            blockers.append(f"forbidden path: {p}")
        if matches(p, settings.sensitive_path_patterns):
            approval.append(f"touches sensitive path {p}")
        if weakens_tests(p, ctx.files.get(p), change.new_content):
            approval.append(f"may weaken tests in {p}")

    if any(fnmatch.fnmatch(ctx.original_ref or ctx.ref, pat) for pat in settings.production_ref_patterns):
        approval.append(f"target ref {ctx.original_ref or ctx.ref} is production")
    if diagnosis.risk == Risk.HIGH:
        approval.append("model rated risk high")
    if diagnosis.category == FailureCategory.DEPLOYMENT_DRIFT:
        approval.append("deployment drift")
    if any(j.stage.lower() in CRITICAL_STAGES for j in ctx.jobs):
        approval.append("failure is in a deploy/release stage")

    return PolicyDecision(allowed=not blockers, needs_approval=bool(approval), reasons=blockers + approval)
