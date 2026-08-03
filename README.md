# Orchestrator

Orchestrator is a Windows-hosted Python gateway that lets an authenticated Telegram user ask questions about configured repositories, schedule read-only code and pipeline analysis, approve scoped changes, and review a local branch before Orchestrator performs a push and creates an always-draft pull request.

The implementation intentionally keeps workflow control outside Codex. Codex runs are disposable subprocesses; Telegram input, job state, approvals, Git operations, persistence, and provider calls belong to Orchestrator.

## Safety boundaries

- SQLite and direct Windows execution; no Docker, Kubernetes, Redis, web dashboard, or public webhook is required.
- Normal Telegram interaction is conversational; slash commands are not required.
- Write jobs use isolated Git worktrees and configured allowed/forbidden paths.
- Codex never receives push authority. Only the Git manager can push.
- Every push requires an explicit Telegram approval bound to the exact current HEAD and a non-expired approval record.
- Direct default-branch pushes and force pushes are refused.
- GitHub and Azure DevOps pull requests are created as drafts. Automatic merge and Azure completion are not implemented.
- Secrets are referenced by environment-variable name and are never written to YAML, SQLite, prompts, logs, or PR descriptions.

## Requirements

- Windows 10/11
- Python 3.12 or newer (tested locally with Python 3.14)
- Git on `PATH`
- Codex CLI on `PATH` for real analyses
- A locally cloned repository for each configured repository
- Optional: Telegram Bot API credentials, GitHub token, Azure DevOps PAT, and OpenAI transcription key

## Install

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,telegram]"
Copy-Item .env.example .env
```

Set secrets in the process environment or with a secret manager. Do not commit `.env`.

## Configure

1. Copy `config/orchestrator.example.yaml` to `config/orchestrator.yaml`.
2. Copy and edit `config/repositories.example.yaml` and the project example under `config/projects/`.
3. Copy and edit `config/schedules.example.yaml` if scheduled jobs are wanted.
4. Create external Codex profiles outside target repositories, for example:

```text
C:\orchestrator\profiles\example-project\
  AGENTS.md
  config.toml
  prompts\
  skills\
```

The project `codex.profile_path` becomes `CODEX_HOME` for that disposable run. No `AGENTS.md`, `.codex`, or Orchestrator files are copied into analyzed repositories.

### Telegram

Create a bot with BotFather. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, and `TELEGRAM_ALLOWED_CHAT_IDS`. Numeric IDs are required; usernames are not accepted as authentication. Configure separate conversation and status chat IDs with `TELEGRAM_CONVERSATION_CHAT_ID` and `TELEGRAM_STATUS_CHAT_ID`.

The gateway persists inbound updates and every meaningful outbound message, including reply context, job, project, branch, phase, and HEAD. Replies to approval messages therefore resolve deterministically. Voice messages are downloaded to the configured temporary directory and sent through the configured transcription adapter; state-changing voice requests must be confirmed by the surrounding workflow before mutation.

### Codex and models

Configure the installed executable, allowed model names, and reasoning efforts in YAML. Model identifiers are configurable strings; no undocumented model name is required by the code. The default example uses a placeholder Luna name that must be changed to the installed model identifier. The runner requests JSON output and validates the `result_type` schema.

### SQLite

The default database is `data/orchestrator.db`. SQLite foreign keys, WAL mode, and a busy timeout are enabled. The SQLAlchemy metadata is also exposed to Alembic under `alembic/`; schema initialization is safe to run repeatedly.

### Repositories, projects, and schedules

A repository is a local clone and provider identity. A project is a logical scope inside a repository and can use a different working directory, Codex profile, path policy, validations, permissions, and pipeline mapping. This supports several projects in one monorepo.

Schedules use five-field cron expressions and the configured timezone (default `America/Montevideo`). APScheduler is the in-process scheduler; Windows Task Scheduler is only used to start the process.

### GitHub and Azure DevOps

Use `provider: github` with `owner`, `repository`, and `GITHUB_TOKEN`, or `provider: azure_devops` with organization, project, repository ID, and `AZURE_DEVOPS_PAT`. Credentials are read only when a provider request is made. Provider clients use request timeouts, return actionable errors, detect existing open PRs by source branch, and verify the provider response is a draft.

### Transcription

Set `telegram.transcription.provider` to `openai`, configure the model and `OPENAI_API_KEY`, and install the `transcription` extra. Use `none` to disable voice transcription. Secrets are never requested through Telegram.

## Run manually

```powershell
python -m orchestrator
```

The application initializes the database, persists config metadata, recovers interrupted jobs, loads schedules, starts workers, starts Telegram long polling when a bot token is present, and emits startup status. Without a bot token it still starts the local scheduler/worker runtime and logs that Telegram is disabled.

## Windows Task Scheduler at startup

Create a task that runs whether or not a user is logged in:

1. Open **Task Scheduler** → **Create Task**.
2. On **General**, select **Run whether user is logged on or not** and use an account that can access the repositories and secret environment.
3. On **Triggers**, add **At startup** and optionally a short delay.
4. On **Actions**, use `C:\path\to\repo\.venv\Scripts\python.exe` with argument `-m orchestrator` and start in the repository directory.
5. On **Conditions**, clear the requirement for AC power if appropriate.
6. On **Settings**, enable restart on failure and stop the task only after an explicit operational policy.

Codex CLI and Git must be available to that task account. A future Windows service wrapper can call the same `Application.run_forever()` lifecycle.

## Verification

```powershell
python -m pytest
python -m ruff check .
python -m mypy src
```

Tests mock Telegram/provider/Codex boundaries and do not need real external credentials. Real GitHub, Azure DevOps, Telegram, and Codex service connectivity has not been claimed or required by the test suite.

## Operational flow

1. A schedule or natural Telegram message creates an idempotent SQLite job.
2. A worker claims it and executes a disposable read-only Codex process for analysis.
3. Proposals are persisted and must be approved before a write job is created.
4. The write job creates a branch/worktree, runs Codex in workspace-write mode, enforces scope, runs validations, records commits and file changes, and sends a push manifest.
5. A reply such as `Push it` is accepted only when it replies to the exact approval message and the current HEAD equals the approved HEAD.
6. Orchestrator pushes the branch and creates a draft PR through the configured provider. It never merges or publishes the draft automatically.

## Known limitations of this first version

- Provider and Telegram adapters are implemented and mockable, but live services were not exercised without credentials.
- Codex CLI flags vary by installation; `codex.exec`, JSON output, sandbox modes, and model names are configurable through the runner/settings, but installations should be verified with `codex --help`.
- Azure DevOps uses its draft pull-request field; repository policies may still require manual publication.
- The current worker defaults to a single persistent SQLite queue and conservative write concurrency. More advanced cross-process locking can be added without changing the domain state machine.
- Semantic proposal revalidation and richer inline-button/callback flows are extension points; security-sensitive approvals remain programmatic and reply-bound.
- Validation commands are trusted project configuration. Keep them reviewed and do not configure commands that deploy, inspect credentials, or mutate production.

