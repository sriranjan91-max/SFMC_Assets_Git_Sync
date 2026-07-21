from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import AppConfig, ConfigurationError, load_config, load_dotenv
from github_handler import (
    commit_and_publish,
    find_git_executable,
    find_pending_output_changes,
    init_repo,
    verify_repo,
)
from sfmc_handler import SfmcClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetItem:
    asset_id: int
    file_name: str


_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def sanitize_file_name(name: str) -> str:
    sanitized = "".join("_" if ch in _INVALID_FILENAME_CHARS else ch for ch in name)
    return sanitized.strip().strip(".")


def load_assets_from_json(asset_list_path: Path) -> list[AssetItem]:
    payload = json.loads(asset_list_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ConfigurationError("Asset list must be a JSON array")

    assets: list[AssetItem] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ConfigurationError(f"Asset list item #{index + 1} must be an object")
        if "asset_id" not in item or "file_name" not in item:
            raise ConfigurationError(
                f"Asset list item #{index + 1} requires asset_id and file_name"
            )

        asset_id = item["asset_id"]
        file_name = item["file_name"]
        if not isinstance(asset_id, int):
            raise ConfigurationError(f"asset_id at item #{index + 1} must be an integer")
        if not isinstance(file_name, str) or not file_name.strip():
            raise ConfigurationError(f"file_name at item #{index + 1} must be a non-empty string")

        assets.append(AssetItem(asset_id=asset_id, file_name=file_name.strip()))
    return assets


def load_assets_from_csv(asset_list_path: Path) -> list[AssetItem]:
    with asset_list_path.open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        required_columns = {"AssetID", "EmailName"}
        if reader.fieldnames is None or not required_columns.issubset(set(reader.fieldnames)):
            raise ConfigurationError(
                "Assets CSV must have header columns: AssetID, EmailName"
            )

        assets: list[AssetItem] = []
        for index, row in enumerate(reader):
            row_number = index + 2  # account for header row, 1-indexed
            asset_id_raw = (row.get("AssetID") or "").strip()
            email_name_raw = (row.get("EmailName") or "").strip()
            if not asset_id_raw and not email_name_raw:
                continue  # skip blank rows
            if not asset_id_raw or not email_name_raw:
                raise ConfigurationError(
                    f"Row {row_number} requires both AssetID and EmailName"
                )
            try:
                asset_id = int(asset_id_raw)
            except ValueError as exc:
                raise ConfigurationError(
                    f"Row {row_number} has invalid AssetID: {asset_id_raw}"
                ) from exc

            file_name = f"{sanitize_file_name(email_name_raw)}.html"
            assets.append(AssetItem(asset_id=asset_id, file_name=file_name))
    return assets


def load_assets(asset_list_path: Path) -> list[AssetItem]:
    if not asset_list_path.exists():
        raise ConfigurationError(f"Asset list file not found: {asset_list_path}")

    logger.info("Loading asset list from %s...", asset_list_path)
    suffix = asset_list_path.suffix.lower()
    if suffix == ".csv":
        assets = load_assets_from_csv(asset_list_path)
    elif suffix == ".json":
        assets = load_assets_from_json(asset_list_path)
    else:
        raise ConfigurationError(f"Unsupported asset list file type: {suffix}")
    logger.info("Loaded %s asset(s).", len(assets))
    return assets


def write_asset_file(output_dir: Path, asset: AssetItem, html: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / asset.file_name
    file_path.write_text(html, encoding="utf-8")
    return file_path


def sync_once(config: AppConfig) -> int:
    logger.info("Starting SFMC asset sync...")
    verify_repo(config)
    assets = load_assets(config.asset_list_path)
    sfmc_client = SfmcClient(config)

    total = len(assets)
    changed_files: list[Path] = []
    for index, asset in enumerate(assets, start=1):
        logger.info("[%s/%s] Fetching asset %s (%s)...", index, total, asset.asset_id, asset.file_name)
        html = sfmc_client.get_asset_html(asset.asset_id)
        target_file = config.output_dir / asset.file_name

        existing = target_file.read_text(encoding="utf-8") if target_file.exists() else None
        if existing == html:
            continue

        write_asset_file(config.output_dir, asset, html)
        changed_files.append(target_file)
        logger.info("[%s/%s] Changed: %s", index, total, asset.file_name)

    logger.info("Fetched all %s asset(s). %s write(s) performed.", total, len(changed_files))

    # Some asset IDs can map to the same output file name (duplicate/near-duplicate
    # EmailName values, or names differing only by case on case-insensitive
    # filesystems). A later write can silently cancel out an earlier one, so the
    # write count above isn't reliable for deciding what to commit. Ask git what
    # actually differs from HEAD instead of trusting our own write tracking.
    relative_paths = find_pending_output_changes(config)
    if not relative_paths:
        logger.info("No asset changes detected.")
        return 0

    short_ids = [str(asset.asset_id) for asset in assets][:10]
    id_suffix = "" if len(assets) <= 10 else " +more"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    commit_message = f"Sync SFMC assets ({', '.join(short_ids)}{id_suffix}) at {timestamp}"

    commit_and_publish(config, relative_paths, commit_message)

    logger.info("Committed %s changed asset file(s).", len(relative_paths))
    return len(relative_paths)


def commit_pending_changes(config: AppConfig) -> int:
    logger.info("Skipping SFMC fetch; using files already changed on disk...")
    verify_repo(config)

    relative_paths = find_pending_output_changes(config)
    if not relative_paths:
        logger.info("No pending changes found under %s.", config.output_dir)
        return 0

    logger.info("Found %s changed file(s):", len(relative_paths))
    for path in relative_paths:
        logger.info("  - %s", path)

    short_names = [Path(path).stem for path in relative_paths][:10]
    name_suffix = "" if len(relative_paths) <= 10 else " +more"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    commit_message = f"Sync SFMC assets ({', '.join(short_names)}{name_suffix}) at {timestamp}"

    commit_and_publish(config, relative_paths, commit_message)

    logger.info("Committed %s changed asset file(s).", len(relative_paths))
    return len(relative_paths)


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync SFMC email assets into a git repository."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("run-once", help="Run a single asset sync cycle")

    subparsers.add_parser(
        "commit-only",
        help="Commit/push already-changed files under OUTPUT_DIR without contacting SFMC",
    )

    loop_parser = subparsers.add_parser(
        "run-loop", help="Run sync continuously every N hours"
    )
    loop_parser.add_argument("--interval-hours", type=float, default=12.0)

    init_parser = subparsers.add_parser(
        "init-repo", help="Initialize a local git repository for asset storage"
    )
    init_parser.add_argument("--repo-path", required=True)

    return parser.parse_args()


def main() -> None:
    load_dotenv(Path(".env"))
    setup_logging(Path(os.getenv("LOG_FILE", "sfmc_asset_sync.log")))

    args = parse_args()

    try:
        if args.command == "init-repo":
            init_repo(Path(args.repo_path).resolve())
            return

        config = load_config(find_git_executable())

        if args.command == "run-once":
            sync_once(config)
            return

        if args.command == "commit-only":
            commit_pending_changes(config)
            return

        if args.command == "run-loop":
            if args.interval_hours <= 0:
                raise ConfigurationError("--interval-hours must be > 0")
            interval_seconds = args.interval_hours * 3600

            while True:
                started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info("[%s] Starting sync run...", started_at)
                sync_once(config)
                logger.info("Sleeping for %s hour(s)...", args.interval_hours)
                time.sleep(interval_seconds)
            return

        raise ConfigurationError(f"Unknown command: {args.command}")
    except Exception:
        logger.exception("Sync failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
