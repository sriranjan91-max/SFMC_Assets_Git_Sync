# SFMC Email Asset Git Sync (Python)

This project pulls HTML for a configured list of SFMC Content Builder assets and syncs updates into a git repository, either as a direct commit or as a pull request for review.  
It can run once, run continuously on an interval, or just push already-fetched local changes without contacting SFMC.

## Project structure

- `main.py` — CLI entry point, config/asset-list loading, orchestration, and logging setup.
- `sfmc_handler.py` — all SFMC API interaction (OAuth token fetch/cache, asset HTML retrieval).
- `github_handler.py` — all git and GitHub operations (running git commands, branching, committing, pushing, and creating pull requests via the GitHub REST API).
- `config.py` — shared `.env` loading and the `AppConfig` definition used by all modules.

## What it does

1. Reads the asset list (`Assets.csv` or `assets_list.json`) from your repo.
2. Calls the SFMC API for each asset ID (OAuth token is fetched once and cached/refreshed automatically).
3. Extracts HTML content from the asset response and writes it to a `.html` file in `OUTPUT_DIR` when it differs from what's already on disk.
4. After all assets are processed, asks `git status` what actually changed relative to `HEAD` (the authoritative source of truth — see [Change detection](#change-detection-and-duplicate-file-names) below) and commits only those files.
5. Either commits directly to the current branch (optionally pushing), or creates a branch + pull request for review, depending on `CREATE_PULL_REQUEST`.

## Prerequisites

- Python 3.10+
- Git installed (auto-detected via `PATH`, common install locations, or `GIT_EXECUTABLE`)
- SFMC Installed Package credentials with access to the Content Builder API
- A local git repo (with an `origin` remote if using the pull request workflow) where assets should be stored

## Setup

1. Install dependencies:

```powershell
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill in the values:

```powershell
Copy-Item .env.example .env
```

3. Point `ASSET_LIST_PATH` at your asset list file (see [Asset list format](#asset-list-format)) and update it with your SFMC asset IDs.

## Configuration reference (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `SFMC_AUTH_BASE_URL` | Yes | — | SFMC auth tenant base URL |
| `SFMC_REST_BASE_URL` | Yes | — | SFMC REST tenant base URL |
| `SFMC_CLIENT_ID` | Yes | — | Installed Package client ID |
| `SFMC_CLIENT_SECRET` | Yes | — | Installed Package client secret |
| `SFMC_ACCOUNT_ID` | No | (none) | MID for the target Business Unit, if needed |
| `LOCAL_REPO_PATH` | Yes | — | Path to the local git repo that stores assets |
| `ASSET_LIST_PATH` | No | `assets_list.json` | CSV or JSON asset list; relative to `LOCAL_REPO_PATH` unless absolute |
| `OUTPUT_DIR` | No | `assets\html` | Where `.html` files are written; relative to `LOCAL_REPO_PATH` unless absolute |
| `AUTO_PUSH` | No | `false` | Push after a direct commit (only used when `CREATE_PULL_REQUEST=false`) |
| `CREATE_PULL_REQUEST` | No | `false` | Use the branch + pull request workflow instead of a direct commit |
| `GITHUB_TOKEN` | If `CREATE_PULL_REQUEST=true` | (none) | Personal access token with `repo` scope, used to open the PR |
| `PR_BASE_BRANCH` | No | current checked-out branch | Branch the PR targets |
| `GIT_EXECUTABLE` | No | auto-detected | Full path to `git.exe`, if it isn't discoverable automatically |
| `LOG_FILE` | No | `sfmc_asset_sync.log` | Path to the log file written on every run |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | No | repo's existing git identity | Commit author identity, if the repo doesn't already have one configured |

## Asset list format

The asset list can be either a CSV or a JSON file. Set `ASSET_LIST_PATH` in `.env` to point at whichever one you use (relative paths are resolved against `LOCAL_REPO_PATH`).

### CSV format (`Assets.csv`)

```csv
AssetID,EmailName
443722,TST_PRM_EML_ii_Chatbooks_Mothers Day
450578,CRM-7462_PRM_EML_ii_FY26_InktoAIP_Aged_D12
```

- `AssetID` is the numeric SFMC Content Builder asset ID.
- `EmailName` is used to derive the output file name (sanitized for invalid filename characters, saved as `<EmailName>.html`).

### JSON format (`assets_list.json`)

```json
[
  { "asset_id": 123456, "file_name": "welcome_email.html" },
  { "asset_id": 234567, "file_name": "promo_email.html" }
]
```

## Usage

### Run one sync

```powershell
python main.py run-once
```

### Commit/push/PR already-fetched changes without contacting SFMC

```powershell
python main.py commit-only
```

### Run continuously every 12 hours

```powershell
python main.py run-loop --interval-hours 12
```

### Create local repo skeleton (optional)

```powershell
python main.py init-repo --repo-path C:\path\to\sfmc-email-assets-repo
```

## Committing changes: direct commit vs. pull request

By default (`CREATE_PULL_REQUEST=false`), changed assets are committed directly to whatever branch is checked out in `LOCAL_REPO_PATH`, and pushed only if `AUTO_PUSH=true`.

Set `CREATE_PULL_REQUEST=true` to instead:

1. Create a new branch named `sfmc-sync-<UTC timestamp>` off the current branch (or `PR_BASE_BRANCH` if set).
2. Commit the changed asset files there.
3. Push the branch to `origin`.
4. Open a pull request (via the GitHub REST API, compatible with github.com and GitHub Enterprise Server) targeting `PR_BASE_BRANCH` (or the branch that was checked out before the sync ran, if left blank).
5. Switch the local working copy back to the base branch.

Requires `GITHUB_TOKEN` (a personal access token with `repo` scope) in `.env`. The target owner/repo/host are parsed automatically from the `origin` remote URL (works with both `github.com` and GitHub Enterprise Server hosts).

## Change detection and duplicate file names

`run-once` decides what to commit by asking `git status` what actually differs from `HEAD` after all assets are fetched — not by counting how many files it wrote during the fetch loop. This matters because **multiple SFMC asset IDs can map to the same output file name**, either because:

- Two rows in the asset list have the identical `EmailName` (or `file_name` in JSON), or
- Two `EmailName` values differ only by case (e.g. `... - Test.html` vs `... - test.html`), which collide on case-insensitive filesystems (Windows/macOS default).

When this happens, whichever asset is processed **last** in the asset list "wins" and its content is what ends up on disk and in git; the other asset's content for that run is silently overwritten. Using real `git` diffs as the source of truth avoids false-positive commit attempts (and empty, failing commits) caused by this, but it doesn't fix the underlying data collision — if you need every colliding asset preserved as a separate file, rename them uniquely in the asset list (e.g. append the `AssetID`) or file an issue if you want this handled automatically.

## Git executable

`git` is located automatically via `PATH`, common Windows install locations, or the `GIT_EXECUTABLE` variable in `.env` if you want to pin an exact path.

## Logging

Every run logs info/warning/error messages to both the console and a log file (default `sfmc_asset_sync.log`, configurable via `LOG_FILE` in `.env`). Each log line is tagged with the originating module (`config`, `sfmc_handler`, `github_handler`, or `__main__`) for easier troubleshooting. Unhandled errors are logged with a full traceback before the process exits with a non-zero status.

## Scheduling

For production, prefer OS scheduler:

- Windows: Task Scheduler (every 12 hours)
- Linux: cron / systemd timer

Use `run-once` in scheduled jobs.
