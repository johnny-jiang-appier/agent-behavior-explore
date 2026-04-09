"""Central configuration loaded from .env."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    orchestrator_url: str = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8888")
    campaign_agent_url: str = os.environ.get("CAMPAIGN_AGENT_URL", "http://localhost:8777")
    artifact_origin: str = os.environ.get("ARTIFACT_ORIGIN", "http://host.docker.internal:8888")
    app_name: str = os.environ.get("APP_NAME", "multi_agent")
    user_id: str = os.environ.get("USER_ID", "")
    eam_project_id: str = os.environ.get("EAM_PROJECT_ID", "")
    use_real_jwt: bool = os.environ.get("USE_REAL_JWT", "false").lower() in ("1", "true", "yes")
    litellm_model: str = os.environ.get("LITELLM_MODEL", "")
    litellm_review_model: str = os.environ.get("LITELLM_REVIEW_MODEL", "")
    litellm_api_key: str = os.environ.get("LITELLM_API_KEY", "")
    litellm_api_base: str = os.environ.get("LITELLM_API_BASE", "")
    langfuse_project_id: str = os.environ.get("LANGFUSE_PROJECT_ID", "")
    ojm_api_host: str = os.environ.get("OJM_API_HOST", "")


_cfg: Config | None = None


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
