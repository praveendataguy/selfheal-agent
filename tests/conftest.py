from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.approval import NullNotifier
from app.config import Settings
from app.gitlab_client import MergeRequestRef, PipelineInfo
from app.models import FailedJob


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        gitlab_token="t",
        gitlab_webhook_secret="hook-secret",
        anthropic_api_key="k",
        slack_signing_secret="slack-secret",
        production_refs="main,release/*",
    )


@dataclass
class FakeGitLab:
    ref: str = "feature/x"
    commit_message: str = "add parser"
    jobs: list = field(default_factory=list)
    files: dict = field(default_factory=dict)
    run_counts: dict = field(default_factory=dict)
    branches: set = field(default_factory=set)
    open_mrs: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def pipeline_info(self, project_id, pipeline_id):
        return PipelineInfo("group/app", self.ref, "abc123def456", self.commit_message.split("\n")[0], self.commit_message)

    def failed_jobs(self, project_id, pipeline_id):
        return [FailedJob(j.id, j.name, j.stage, j.log) for j in self.jobs]

    def job_run_count(self, project_id, pipeline_id, name):
        return self.run_counts.get(name, 1)

    def changed_files(self, project_id, sha):
        return ["src/parser.py"]

    def read_file(self, project_id, path, ref):
        if (ref, path) in self.branch_files:
            return self.branch_files[(ref, path)]
        return self.files.get(path)

    def list_tree(self, project_id, ref, path=""):
        return sorted(self.files)

    def branch_exists(self, project_id, branch):
        return branch in self.branches

    def find_open_mr(self, project_id, branch):
        return self.open_mrs.get(branch)

    def commit_changes(self, project_id, branch, changes, message, start_sha=None, existing_paths=None):
        self.calls.append(("commit", branch, [c.path for c in changes], message, start_sha, existing_paths))
        self.branches.add(branch)
        return "newsha"

    def open_mr(self, project_id, source, target, title, description, labels):
        mr = MergeRequestRef(7, "https://gitlab.example/group/app/-/merge_requests/7", source, target)
        self.open_mrs[source] = mr
        self.calls.append(("open_mr", source, target, title, labels))
        return mr

    def comment_mr(self, project_id, iid, body):
        self.calls.append(("comment", iid, body))

    def mark_mr_ready(self, project_id, iid):
        self.calls.append(("ready", iid))

    def close_mr(self, project_id, iid):
        self.calls.append(("close", iid))

    def delete_branch(self, project_id, branch):
        self.calls.append(("delete_branch", branch))

    def run_pipeline(self, project_id, ref):
        self.calls.append(("run_pipeline", ref))
        return 999

    def retry_job(self, project_id, job_id):
        self.calls.append(("retry", job_id))

    # Duo governance mode
    mrs: dict = field(default_factory=dict)  # iid -> dict like GitLabClient.get_mr
    mr_paths: dict = field(default_factory=dict)  # iid -> [(path, deleted)]
    branch_files: dict = field(default_factory=dict)  # (branch, path) -> content

    def get_mr(self, project_id, iid):
        return dict(self.mrs[iid])

    def mr_changed_paths(self, project_id, iid):
        return self.mr_paths.get(iid, [])

    def update_mr(self, project_id, iid, title=None, add_labels=None):
        if title is not None:
            self.mrs[iid]["title"] = title
        if add_labels:
            self.mrs[iid]["labels"] = sorted(set(self.mrs[iid]["labels"]) | set(add_labels))
        self.calls.append(("update_mr", iid, title, tuple(add_labels or ())))

    def kinds(self):
        return [c[0] for c in self.calls]


class FakeDiagnoser:
    """Stands in for the Claude-backed Diagnoser."""

    result = None

    def __init__(self, settings, read_file, list_dir):
        self.read_file = read_file

    def diagnose(self, ctx):
        FakeDiagnoser.last_ctx = ctx
        return FakeDiagnoser.result


def tool_use(name, data, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=data, id=id_)


class FakeAnthropic:
    """Minimal stand-in for anthropic.Anthropic().messages.create."""

    def __init__(self, scripted_responses):
        self._responses = list(scripted_responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(content=self._responses.pop(0))


@pytest.fixture
def fake_gitlab():
    return FakeGitLab()


@pytest.fixture
def notifier():
    return NullNotifier()
