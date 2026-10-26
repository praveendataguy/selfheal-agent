"""Thin wrapper over python-gitlab exposing only what the agent needs.

Keeping the surface small makes the orchestrator easy to test with a fake.
"""

import logging
from dataclasses import dataclass

import gitlab
from gitlab.exceptions import GitlabGetError

from app.models import FailedJob, FileChange

log = logging.getLogger(__name__)


@dataclass
class PipelineInfo:
    project_path: str
    ref: str
    sha: str
    commit_title: str
    commit_message: str


@dataclass
class MergeRequestRef:
    iid: int
    web_url: str
    source_branch: str
    target_branch: str


class GitLabClient:
    def __init__(self, url: str, token: str):
        self._gl = gitlab.Gitlab(url, private_token=token, retry_transient_errors=True)

    def _project(self, project_id: int):
        return self._gl.projects.get(project_id, lazy=True)

    # ---------------------------------------------------------------- reading
    def pipeline_info(self, project_id: int, pipeline_id: int) -> PipelineInfo:
        project = self._gl.projects.get(project_id)
        pipeline = project.pipelines.get(pipeline_id)
        commit = project.commits.get(pipeline.sha)
        return PipelineInfo(
            project_path=project.path_with_namespace,
            ref=pipeline.ref,
            sha=pipeline.sha,
            commit_title=commit.title,
            commit_message=commit.message,
        )

    def failed_jobs(self, project_id: int, pipeline_id: int) -> list[FailedJob]:
        project = self._project(project_id)
        pipeline = project.pipelines.get(pipeline_id, lazy=True)
        jobs = []
        for job in pipeline.jobs.list(scope="failed", get_all=True):
            if getattr(job, "allow_failure", False):
                continue
            trace = project.jobs.get(job.id, lazy=True).trace()
            text = trace.decode("utf-8", errors="replace") if isinstance(trace, bytes) else str(trace)
            jobs.append(FailedJob(id=job.id, name=job.name, stage=job.stage, log=text))
        return jobs

    def job_run_count(self, project_id: int, pipeline_id: int, job_name: str) -> int:
        """How many times a job with this name ran in the pipeline (incl. retries)."""
        pipeline = self._project(project_id).pipelines.get(pipeline_id, lazy=True)
        return sum(
            1 for j in pipeline.jobs.list(include_retried=True, get_all=True) if j.name == job_name
        )

    def changed_files(self, project_id: int, sha: str) -> list[str]:
        commit = self._project(project_id).commits.get(sha)
        return [d["new_path"] for d in commit.diff(get_all=True) if not d.get("deleted_file")]

    def read_file(self, project_id: int, path: str, ref: str) -> str | None:
        try:
            raw = self._project(project_id).files.raw(file_path=path, ref=ref)
        except GitlabGetError:
            return None
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

    def list_tree(self, project_id: int, ref: str, path: str = "") -> list[str]:
        items = self._project(project_id).repository_tree(path=path, ref=ref, get_all=True)
        return [i["path"] + ("/" if i["type"] == "tree" else "") for i in items]

    def branch_exists(self, project_id: int, branch: str) -> bool:
        try:
            self._project(project_id).branches.get(branch)
            return True
        except GitlabGetError:
            return False

    def find_open_mr(self, project_id: int, source_branch: str) -> MergeRequestRef | None:
        mrs = self._project(project_id).mergerequests.list(
            state="opened", source_branch=source_branch, get_all=False
        )
        return self._mr_ref(mrs[0]) if mrs else None

    def get_mr(self, project_id: int, mr_iid: int) -> dict:
        mr = self._project(project_id).mergerequests.get(mr_iid)
        return {
            "iid": mr.iid,
            "title": mr.title,
            "description": mr.description or "",
            "labels": list(mr.labels or []),
            "author": (mr.author or {}).get("username", ""),
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "web_url": mr.web_url,
            "state": mr.state,
        }

    def mr_changed_paths(self, project_id: int, mr_iid: int) -> list[tuple[str, bool]]:
        """(new_path, deleted) for every file the MR touches."""
        mr = self._project(project_id).mergerequests.get(mr_iid)
        return [(c["new_path"], bool(c.get("deleted_file"))) for c in mr.changes().get("changes", [])]

    def update_mr(self, project_id: int, mr_iid: int, title: str | None = None, add_labels: list[str] | None = None) -> None:
        mr = self._project(project_id).mergerequests.get(mr_iid)
        if title is not None:
            mr.title = title
        if add_labels:
            mr.labels = sorted(set(mr.labels or []) | set(add_labels))
        mr.save()

    # ---------------------------------------------------------------- writing
    def commit_changes(
        self,
        project_id: int,
        branch: str,
        changes: list[FileChange],
        message: str,
        start_sha: str | None = None,
        existing_paths: set[str] | None = None,
    ) -> str:
        """Commit file changes. With start_sha, the branch is created from that commit."""
        existing_paths = existing_paths or set()
        actions = [
            {
                "action": "update" if c.path in existing_paths else "create",
                "file_path": c.path,
                "content": c.new_content,
            }
            for c in changes
        ]
        payload = {"branch": branch, "commit_message": message, "actions": actions}
        if start_sha:
            payload["start_sha"] = start_sha
        commit = self._project(project_id).commits.create(payload)
        return commit.id

    def open_mr(
        self, project_id: int, source: str, target: str, title: str, description: str, labels: list[str]
    ) -> MergeRequestRef:
        mr = self._project(project_id).mergerequests.create(
            {
                "source_branch": source,
                "target_branch": target,
                "title": title,
                "description": description,
                "labels": labels,
                "remove_source_branch": True,
            }
        )
        return self._mr_ref(mr)

    def comment_mr(self, project_id: int, mr_iid: int, body: str) -> None:
        self._project(project_id).mergerequests.get(mr_iid, lazy=True).notes.create({"body": body})

    def mark_mr_ready(self, project_id: int, mr_iid: int) -> None:
        mr = self._project(project_id).mergerequests.get(mr_iid)
        for prefix in ("Draft: ", "[Draft] ", "(Draft) ", "Draft:"):
            if mr.title.startswith(prefix):
                mr.title = mr.title[len(prefix):].lstrip()
        mr.save()

    def close_mr(self, project_id: int, mr_iid: int) -> None:
        mr = self._project(project_id).mergerequests.get(mr_iid)
        mr.state_event = "close"
        mr.save()

    def delete_branch(self, project_id: int, branch: str) -> None:
        self._project(project_id).branches.delete(branch)

    def run_pipeline(self, project_id: int, ref: str) -> int:
        return self._project(project_id).pipelines.create({"ref": ref}).id

    def retry_job(self, project_id: int, job_id: int) -> None:
        self._project(project_id).jobs.get(job_id, lazy=True).retry()

    @staticmethod
    def _mr_ref(mr) -> MergeRequestRef:
        return MergeRequestRef(
            iid=mr.iid, web_url=mr.web_url, source_branch=mr.source_branch, target_branch=mr.target_branch
        )
