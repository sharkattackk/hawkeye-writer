import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass(frozen=True)
class ApiConfig:
    """API Gateway (HTTP API) in front of the Lambda, secured by a Cognito user pool."""

    # Only users from this pool can call the API
    user_pool_id: str
    # App clients whose tokens are accepted (the frontend's Cognito app client)
    user_pool_client_ids: list[str]
    # Browser origins allowed to call the API
    cors_origins: list[str] = field(default_factory=list)
    # Route the frontend POSTs to
    path: str = "/invoke"


@dataclass(frozen=True)
class Config:
    app_name: str
    github_owner: str
    github_repo: str
    github_branch: str
    connection_arn: str
    account: str
    region: str
    runtime: str = "python3.13"
    architecture: str = "x86_64"
    memory_mb: int = 256
    timeout_seconds: int = 30
    log_retention_days: int = 30
    function_url: bool = False
    environment: dict = field(default_factory=dict)
    api: ApiConfig | None = None

    @property
    def repo_path(self) -> str:
        return f"{self.github_owner}/{self.github_repo}"


def load_config(path: Path = CONFIG_PATH, require_connection: bool = True) -> Config:
    raw = yaml.safe_load(path.read_text()) or {}
    github = raw.get("github") or {}
    aws = raw.get("aws") or {}
    fn = raw.get("lambda") or {}

    app_name = str(raw.get("app_name", "")).strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", app_name):
        raise ValueError(
            f"config.yaml: app_name '{app_name}' must start with a letter and use "
            "only lowercase letters, numbers and hyphens (max 48 chars)"
        )

    connection_arn = str(github.get("connection_arn", "")).strip()
    if require_connection and not connection_arn.startswith("arn:aws:"):
        raise ValueError(
            "config.yaml: github.connection_arn is not set. Create a GitHub "
            "connection in the AWS console (Developer Tools > Settings > "
            "Connections) and paste its ARN."
        )

    return Config(
        app_name=app_name,
        github_owner=github["owner"],
        github_repo=github.get("repo") or app_name,
        github_branch=github.get("branch", "main"),
        connection_arn=connection_arn,
        account=str(aws["account"]),
        region=aws["region"],
        runtime=fn.get("runtime", "python3.13"),
        architecture=fn.get("architecture", "x86_64"),
        memory_mb=int(fn.get("memory_mb", 256)),
        timeout_seconds=int(fn.get("timeout_seconds", 30)),
        log_retention_days=int(fn.get("log_retention_days", 30)),
        function_url=bool(fn.get("function_url", False)),
        environment={str(k): str(v) for k, v in (fn.get("environment") or {}).items()},
    )
