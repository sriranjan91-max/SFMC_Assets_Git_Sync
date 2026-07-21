from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from config import ConfigurationError

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)


class GitCommandError(Exception):
    pass


def find_git_executable() -> str:
    override = os.getenv("GIT_EXECUTABLE", "").strip()
    if override:
        if not Path(override).exists():
            raise ConfigurationError(f"GIT_EXECUTABLE does not exist: {override}")
        return override

    found = shutil.which("git")
    if found:
        return found

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "git.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "git.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    raise ConfigurationError(
        "git executable not found. Install Git, add it to PATH, or set "
        "GIT_EXECUTABLE in .env to the full path of git.exe."
    )


def run_git_command(
    repo_path: Path,
    args: list[str],
    config: AppConfig,
    input_text: str | None = None,
) -> str:
    command = [config.git_executable]
    if config.git_author_name:
        command += ["-c", f"user.name={config.git_author_name}"]
    if config.git_author_email:
        command += ["-c", f"user.email={config.git_author_email}"]
    command.extend(args)

    logger.info("Running git command: %s", " ".join(args))

    # Prevent git from hanging forever on an interactive credential prompt
    # (e.g. push to a remote with no cached credentials); fail fast instead.
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        result = subprocess.run(
            command,
            cwd=repo_path,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(
            f"Git command timed out after 120s ({' '.join(command)}). "
            "This usually means git needs credentials for the remote "
            "(configure a credential helper or embed a token in the remote URL)."
        ) from exc

    if result.returncode != 0:
        raise GitCommandError(
            f"Git command failed ({' '.join(command)}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def git_add_files(repo_path: Path, relative_paths: list[str], config: AppConfig) -> None:
    if not relative_paths:
        return
    # Pass paths via NUL-delimited stdin instead of individual argv entries to
    # avoid the Windows command-line length limit when there are many files.
    nul = "\x00"
    input_text = nul.join(relative_paths) + nul
    run_git_command(
        repo_path,
        ["add", "--pathspec-from-file=-", "--pathspec-file-nul"],
        config,
        input_text=input_text,
    )


def parse_github_remote(remote_url: str) -> tuple[str, str, str]:
    remote_url = remote_url.strip()
    if remote_url.startswith("git@"):
        _, _, rest = remote_url.partition("@")
        host, _, path = rest.partition(":")
    else:
        without_scheme = remote_url.split("://", 1)[-1]
        host, _, path = without_scheme.partition("/")

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = path.split("/")
    if len(parts) < 2 or not host:
        raise ConfigurationError(f"Cannot parse owner/repo from remote URL: {remote_url}")
    owner, repo = parts[-2], parts[-1]
    return host, owner, repo


def github_api_base_url(host: str) -> str:
    if host == "github.com":
        return "https://api.github.com"
    return f"https://{host}/api/v3"


def create_github_pull_request(
    token: str,
    host: str,
    owner: str,
    repo: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> str:
    url = f"{github_api_base_url(host)}/repos/{owner}/{repo}/pulls"
    response = requests.post(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "head": head_branch, "base": base_branch, "body": body},
        timeout=30,
    )
    if response.status_code >= 400:
        raise GitCommandError(
            f"Failed to create pull request: HTTP {response.status_code} - {response.text}"
        )
    data = response.json()
    html_url = data.get("html_url")
    if not isinstance(html_url, str) or not html_url:
        raise GitCommandError("Pull request created but response missing html_url")
    return html_url


def verify_repo(config: AppConfig) -> None:
    git_dir = config.local_repo_path / ".git"
    if not git_dir.exists():
        raise ConfigurationError(
            f"LOCAL_REPO_PATH is not a git repository: {config.local_repo_path}"
        )


def find_pending_output_changes(config: AppConfig) -> list[str]:
    try:
        output_rel = str(config.output_dir.relative_to(config.local_repo_path)).replace("\\", "/")
    except ValueError:
        output_rel = "."

    raw = run_git_command(
        config.local_repo_path,
        [
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--no-renames",
            "--",
            output_rel,
        ],
        config,
    )
    if not raw:
        return []

    paths: list[str] = []
    for entry in raw.split("\x00"):
        if not entry:
            continue
        # Porcelain v1 format is "XY<space>PATH": 2 status characters followed
        # by exactly one space separator. Slice off the 2-character status
        # code, then strip a single leading space rather than assuming a fixed
        # offset, since that's the only part of the format guaranteed stable
        # (--no-renames above also avoids the two-NUL-field rename/copy
        # variant of this format, which has no such prefix on its 2nd field).
        path = entry[2:]
        if path.startswith(" "):
            path = path[1:]
        path = path.strip()
        if path:
            paths.append(path.replace("\\", "/"))
    return paths


def commit_and_publish(config: AppConfig, relative_paths: list[str], commit_message: str) -> None:
    if config.create_pull_request:
        if not config.github_token:
            raise ConfigurationError(
                "GITHUB_TOKEN is required in .env to create a pull request"
            )

        logger.info("Determining base branch...")
        base_branch = config.pr_base_branch or run_git_command(
            config.local_repo_path, ["rev-parse", "--abbrev-ref", "HEAD"], config
        )
        branch_name = f"sfmc-sync-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        logger.info("Base branch: %s | New branch: %s", base_branch, branch_name)

        logger.info("Creating branch %s...", branch_name)
        run_git_command(config.local_repo_path, ["checkout", "-b", branch_name], config)
        try:
            logger.info("Staging %s changed file(s)...", len(relative_paths))
            git_add_files(config.local_repo_path, relative_paths, config)
            logger.info("Committing changes...")
            run_git_command(config.local_repo_path, ["commit", "-m", commit_message], config)
            logger.info("Pushing branch %s to origin...", branch_name)
            run_git_command(
                config.local_repo_path, ["push", "-u", "origin", branch_name], config
            )
        finally:
            logger.info("Switching back to base branch %s...", base_branch)
            run_git_command(config.local_repo_path, ["checkout", base_branch], config)

        logger.info("Resolving GitHub remote details...")
        remote_url = run_git_command(
            config.local_repo_path, ["remote", "get-url", "origin"], config
        )
        host, owner, repo = parse_github_remote(remote_url)
        body_file_limit = 200
        listed_paths = relative_paths[:body_file_limit]
        body_suffix = (
            ""
            if len(relative_paths) <= body_file_limit
            else f"\n- ... and {len(relative_paths) - body_file_limit} more file(s)"
        )
        pr_body = (
            f"Automated SFMC asset sync. {len(relative_paths)} file(s) changed.\n\n"
            "Changed files:\n" + "\n".join(f"- {path}" for path in listed_paths) + body_suffix
        )
        logger.info("Creating pull request on %s/%s/%s...", host, owner, repo)
        pr_url = create_github_pull_request(
            token=config.github_token,
            host=host,
            owner=owner,
            repo=repo,
            head_branch=branch_name,
            base_branch=base_branch,
            title=commit_message,
            body=pr_body,
        )
        logger.info("Created pull request: %s", pr_url)
    else:
        logger.info("Staging %s changed file(s)...", len(relative_paths))
        git_add_files(config.local_repo_path, relative_paths, config)
        logger.info("Committing changes...")
        run_git_command(config.local_repo_path, ["commit", "-m", commit_message], config)

        if config.auto_push:
            logger.info("Pushing to upstream...")
            run_git_command(config.local_repo_path, ["push"], config)


def init_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run([find_git_executable(), "init"], cwd=repo_path, check=True)
    (repo_path / "assets").mkdir(parents=True, exist_ok=True)
    logger.info("Initialized git repo at: %s", repo_path)
