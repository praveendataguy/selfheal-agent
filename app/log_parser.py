"""Turn raw GitLab job traces into a compact, high-signal excerpt for the model."""

import re

from app.models import FailureCategory

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07")
SECTION_RE = re.compile(r"section_(start|end):\d+:[\w\-.]+(\[[^\]]*\])?\r?")
ERROR_HINT_RE = re.compile(
    r"(error|failed|failure|exception|traceback|fatal|panic|assert|"
    r"killed|oom|cannot|could not|not found|conflict|denied|exit code)",
    re.IGNORECASE,
)

# (category, patterns) — first match wins, ordered most-specific first.
CATEGORY_RULES: list[tuple[FailureCategory, list[str]]] = [
    (
        FailureCategory.INFRA_FLAKE,
        [
            r"no space left on device",
            r"runner system failure",
            r"connection reset by peer",
            r"TLS handshake timeout",
            r"(429|503) Service Unavailable|Too Many Requests",
            r"Temporary failure in name resolution",
            r"ERROR: Job failed \(system failure\)",
        ],
    ),
    (
        FailureCategory.MEMORY,
        [
            r"exit code 137",
            r"OOMKilled",
            r"out of memory",
            r"JavaScript heap out of memory",
            r"java\.lang\.OutOfMemoryError",
            r"MemoryError",
            r"LeakSanitizer|memory leak",
        ],
    ),
    (
        FailureCategory.DEPENDENCY_CONFLICT,
        [
            r"ResolutionImpossible",
            r"Could not find a version that satisfies",
            r"ERESOLVE",
            r"peer dep",
            r"version solving failed",
            r"conflicting dependencies",
            r"ModuleNotFoundError|Cannot find module",
            r"go: .* requires",
        ],
    ),
    (
        FailureCategory.DEPLOYMENT_DRIFT,
        [
            r"terraform plan.*(changes|drift)",
            r"Objects have changed outside of Terraform",
            r"helm.*(UPGRADE FAILED|has no deployed releases)",
            r"field is immutable",
            r"ImagePullBackOff|ErrImagePull",
            r"kubectl.*(diff|apply).*(error|conflict)",
        ],
    ),
    (
        FailureCategory.TEST_FAILURE,
        [
            r"=+ .*failed.* =+",  # pytest summary
            r"FAILED .*::",
            r"AssertionError",
            r"Tests?:\s+\d+ failed",  # jest
            r"--- FAIL:",  # go test
            r"\[ERROR\] Tests run:.*Failures: [1-9]",  # maven
        ],
    ),
    (
        FailureCategory.LINT_ERROR,
        [r"\bflake8\b|\bruff\b|\beslint\b|\bmypy\b|golangci-lint", r"\bE\d{3}\b.*\.py"],
    ),
    (
        FailureCategory.BUILD_ERROR,
        [r"SyntaxError", r"compilation failed", r"error TS\d+", r"cannot find symbol", r"undefined reference"],
    ),
]

# Paths like src/foo/bar.py:42  or  File "app/x.py", line 3  or  ./lib/a.ts(10,5)
PATH_RE = re.compile(
    r"""(?:File\s+")?(?P<path>(?:\./)?[A-Za-z0-9_\-./]+?\.(?:py|js|jsx|ts|tsx|go|java|kt|rb|rs|cs|php|
    yml|yaml|json|toml|cfg|ini|txt|lock|tf|gradle|xml|mod))(?:"|:\d+|\(\d+)""",
    re.VERBOSE,
)
IGNORED_PATH_PARTS = ("site-packages/", "node_modules/", "/usr/", "/opt/", "dist-packages/", ".venv/")


def clean(raw: str) -> str:
    text = ANSI_RE.sub("", raw)
    text = SECTION_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def excerpt(raw: str, max_chars: int = 60_000, context: int = 15) -> str:
    """Keep the tail of the log plus windows around error-looking lines."""
    lines = clean(raw).split("\n")
    if sum(len(line) + 1 for line in lines) <= max_chars:
        return "\n".join(lines)

    keep: set[int] = set(range(max(0, len(lines) - 200), len(lines)))  # tail
    for i, line in enumerate(lines):
        if ERROR_HINT_RE.search(line):
            keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))

    out: list[str] = []
    prev = -2
    for i in sorted(keep):
        if i != prev + 1:
            out.append("... [snip] ...")
        out.append(lines[i])
        prev = i
    text = "\n".join(out)
    # Still too long: keep the end, which almost always holds the real error.
    return text if len(text) <= max_chars else "... [truncated] ...\n" + text[-max_chars:]


def classify(log: str) -> FailureCategory:
    for category, patterns in CATEGORY_RULES:
        if any(re.search(p, log, re.IGNORECASE) for p in patterns):
            return category
    return FailureCategory.UNKNOWN


def referenced_paths(log: str, limit: int = 15) -> list[str]:
    """Repository-relative file paths mentioned in a log, most recent last-seen first."""
    seen: dict[str, None] = {}
    for m in PATH_RE.finditer(log):
        path = m.group("path")
        if path.startswith("./"):
            path = path[2:]
        if path.startswith("/") or any(p in path for p in IGNORED_PATH_PARTS):
            # Absolute paths: try to strip a CI build dir prefix like /builds/group/project/
            if path.startswith("/builds/"):
                parts = path.split("/")
                path = "/".join(parts[4:]) if len(parts) > 4 else ""
            else:
                continue
        if path:
            seen.pop(path, None)
            seen[path] = None
    return list(reversed(seen))[:limit]
