from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file).

    On Cloud Run, secrets are injected from Secret Manager as env vars.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- GitLab -------------------------------------------------------------
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""  # project/group access token: api + write_repository
    gitlab_webhook_secret: str = ""  # must match "Secret token" on the webhook

    # --- Who writes the fix --------------------------------------------------
    # agent : this service diagnoses failures itself with LLM_PROVIDER
    # duo   : a GitLab Duo Agent Platform flow (triggered on failed pipelines)
    #         writes the fix MR; this service governs it (guardrails, Slack
    #         approval, verification) instead of calling an LLM
    healer: Literal["agent", "duo"] = "agent"
    governed_label: str = "self-heal"
    # MR authors treated as Duo flows (service accounts are named ai-<flow>-<group>)
    duo_author_prefixes: str = "ai-,duo"

    # --- LLM provider ---------------------------------------------------------
    # anthropic | vertex : Claude (paid)
    # gemini             : Google AI Studio key (has a free tier)
    # mock               : no AI at all; canned answer for examples/demo-repo
    llm_provider: Literal["anthropic", "vertex", "gemini", "mock"] = "anthropic"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5-5"
    vertex_project_id: str = ""
    vertex_region: str = "us-east5"
    llm_max_tokens: int = 16000

    # --- Slack approvals ------------------------------------------------------
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel_id: str = ""
    # Comma-separated Slack user IDs allowed to approve. Empty = anyone in channel.
    slack_approver_ids: str = ""

    # --- Safety policy --------------------------------------------------------
    branch_prefix: str = "selfheal/"
    max_attempts: int = 2  # how many fix attempts per original failure
    min_confidence: float = 0.55  # below this, report only, no MR
    max_files_changed: int = 5
    max_patch_bytes: int = 200_000
    max_log_chars: int = 60_000
    # Refs whose fixes always need a human (comma-separated, glob allowed)
    production_refs: str = "main,master,production,release/*"
    # Paths that are never auto-changed without approval
    sensitive_paths: str = (
        ".gitlab-ci.yml,Dockerfile,*.tf,infra/*,deploy/*,k8s/*,helm/*,"
        "**/secrets*,*.env,.env*"
    )
    # Paths the agent may never touch, even with approval
    forbidden_paths: str = ".git/*,**/*.pem,**/*.key,**/id_rsa*"

    # --- Observability --------------------------------------------------------
    gcp_project_id: str = ""
    fetch_cloud_logging_context: bool = False

    dry_run: bool = Field(default=False, description="Analyse only; never push or open MRs")

    @staticmethod
    def _split(value: str) -> list[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    @property
    def production_ref_patterns(self) -> list[str]:
        return self._split(self.production_refs)

    @property
    def sensitive_path_patterns(self) -> list[str]:
        return self._split(self.sensitive_paths)

    @property
    def forbidden_path_patterns(self) -> list[str]:
        return self._split(self.forbidden_paths)

    @property
    def approver_ids(self) -> set[str]:
        return set(self._split(self.slack_approver_ids))


@lru_cache
def get_settings() -> Settings:
    return Settings()
