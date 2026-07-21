from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class AppConfig:
    sfmc_auth_base_url: str
    sfmc_rest_base_url: str
    sfmc_client_id: str
    sfmc_client_secret: str
    sfmc_account_id: str | None
    local_repo_path: Path
    asset_list_path: Path
    output_dir: Path
    auto_push: bool
    git_author_name: str | None
    git_author_email: str | None
    git_executable: str
    create_pull_request: bool
    github_token: str | None
    pr_base_branch: str | None


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"Invalid .env line: {raw_line}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # Don't let a pre-existing but blank environment variable (e.g. an
        # empty shell var) shadow a real value defined in .env.
        if not os.environ.get(key):
            os.environ[key] = value


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ConfigurationError(f"Invalid boolean value for {name}: {raw}")


def load_config(git_executable: str) -> AppConfig:
    logger.info("Loading configuration from .env...")
    load_dotenv(Path(".env"))

    local_repo_path = Path(get_required_env("LOCAL_REPO_PATH")).resolve()
    if not local_repo_path.exists():
        raise ConfigurationError(f"LOCAL_REPO_PATH does not exist: {local_repo_path}")

    asset_list_raw = os.getenv("ASSET_LIST_PATH", "assets_list.json")
    output_dir_raw = os.getenv("OUTPUT_DIR", r"assets\html")

    asset_list_path = Path(asset_list_raw)
    if not asset_list_path.is_absolute():
        asset_list_path = local_repo_path / asset_list_path

    output_dir = Path(output_dir_raw)
    if not output_dir.is_absolute():
        output_dir = local_repo_path / output_dir

    sfmc_account_id = os.getenv("SFMC_ACCOUNT_ID", "").strip() or None
    git_author_name = os.getenv("GIT_AUTHOR_NAME", "").strip() or None
    git_author_email = os.getenv("GIT_AUTHOR_EMAIL", "").strip() or None
    github_token = os.getenv("GITHUB_TOKEN", "").strip() or None
    pr_base_branch = os.getenv("PR_BASE_BRANCH", "").strip() or None

    config = AppConfig(
        sfmc_auth_base_url=get_required_env("SFMC_AUTH_BASE_URL").rstrip("/"),
        sfmc_rest_base_url=get_required_env("SFMC_REST_BASE_URL").rstrip("/"),
        sfmc_client_id=get_required_env("SFMC_CLIENT_ID"),
        sfmc_client_secret=get_required_env("SFMC_CLIENT_SECRET"),
        sfmc_account_id=sfmc_account_id,
        local_repo_path=local_repo_path,
        asset_list_path=asset_list_path,
        output_dir=output_dir,
        auto_push=parse_bool_env("AUTO_PUSH", default=False),
        git_author_name=git_author_name,
        git_author_email=git_author_email,
        git_executable=git_executable,
        create_pull_request=parse_bool_env("CREATE_PULL_REQUEST", default=False),
        github_token=github_token,
        pr_base_branch=pr_base_branch,
    )
    logger.info(
        "Config loaded. Repo: %s | Assets: %s", config.local_repo_path, config.asset_list_path
    )
    logger.info(
        "Git executable: %s | create_pull_request=%s | auto_push=%s",
        config.git_executable,
        config.create_pull_request,
        config.auto_push,
    )
    return config
