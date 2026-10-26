from dataclasses import dataclass, field
from enum import Enum


class FailureCategory(str, Enum):
    TEST_FAILURE = "test_failure"
    DEPENDENCY_CONFLICT = "dependency_conflict"
    BUILD_ERROR = "build_error"
    LINT_ERROR = "lint_error"
    MEMORY = "memory"  # OOM / leak / exit 137
    DEPLOYMENT_DRIFT = "deployment_drift"
    INFRA_FLAKE = "infra_flake"  # runner/network issue: retry, don't patch
    UNKNOWN = "unknown"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class FailedJob:
    id: int
    name: str
    stage: str
    log: str  # trimmed, most relevant section


@dataclass
class FailureContext:
    project_id: int
    project_path: str
    pipeline_id: int
    ref: str
    sha: str
    commit_title: str
    jobs: list[FailedJob]
    files: dict[str, str] = field(default_factory=dict)  # path -> content at sha
    changed_files: list[str] = field(default_factory=list)  # changed in triggering commit
    heuristic_category: FailureCategory = FailureCategory.UNKNOWN
    extra_context: str = ""  # e.g. Cloud Logging excerpts
    attempt: int = 1
    original_ref: str = ""


@dataclass
class FileChange:
    path: str
    new_content: str


@dataclass
class Diagnosis:
    category: FailureCategory
    root_cause: str
    explanation: str
    confidence: float
    risk: Risk
    retry_only: bool  # transient failure: just retry the job
    changes: list[FileChange] = field(default_factory=list)


@dataclass
class PolicyDecision:
    allowed: bool
    needs_approval: bool
    reasons: list[str] = field(default_factory=list)
