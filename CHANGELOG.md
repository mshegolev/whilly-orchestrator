# Changelog

All notable changes to Whilly Orchestrator will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.2.0] - 2026-04-22

### Added
- **Layered config** (`whilly.toml` + OS keyring) with `whilly --config {show,path,edit,migrate}`. Five-layer precedence *defaults < user TOML < repo TOML < .env < shell env < CLI flags*. Secrets live in the OS keyring and never hit disk plaintext (PRs #177, #178, #179, #180, #181, #195).
- **Jira full lifecycle** — `whilly --from-jira ABC-123` source adapter and `JiraBoardClient` that drives Jira transitions in lock-step with task status. stdlib only, no `requests` dependency (#191, #192).
- **GitHub Projects v2 live sync** — cards move `Todo → In Progress → In Review → Done` as tasks run; `whilly --ensure-board-statuses` creates any missing columns; post-merge hook lands cards in Done (#183, #184, #190, #192).
- **`claude_handoff` backend** — delegate any task to an interactive Claude Code session or human operator via file-based RPC. New task statuses `blocked` and `human_loop` with matching board columns (#187).
- **New CLI flags**: `--from-issue owner/repo#N`, `--from-jira ABC-123`, `--from-issues-project <url>`, `--handoff-{list,show,complete}`, `--post-merge <plan>`, `--ensure-board-statuses`, `--config {show,path,edit,migrate}` (#179, #184, #186, #190, #191).
- **Audio announcements** include the task title + classification ("Фичу: X" / "Баг: Y" / …) instead of the generic "Задача готова" (#182).
- **Cross-platform CI** — Windows and macOS runners alongside Ubuntu × 3.10/3.11/3.12 (#179).
- **Documentation site** on GitHub Pages (https://mshegolev.github.io/whilly-orchestrator/) with a step-by-step `Getting-Started` walkthrough and fully annotated `whilly.example.toml` (#193, #194, #195).
- Board bootstrap helpers: `scripts/populate_board.py`, `scripts/move_project_card.py` (#185, #190).

### Fixed
- `ExternalIntegrationManager.is_integration_available(name)` — interactive GitHub menu no longer prints "Ошибка проверки интеграций" on every invocation (#170).
- `--from-github all` now actually fetches every open issue — previously the CLI passed `None` and `generate_tasks_from_github` silently re-applied default labels (#174).
- Centralised `gh` auth env — `WHILLY_GH_TOKEN`, `WHILLY_GH_PREFER_KEYRING`, `[github].token` resolved in one place; fixes stale `GITHUB_TOKEN` shadowing keyring auth across seven subprocess call sites (#177).
- `ProjectBoardClient._load_meta` paginates `items(first: 100)` — boards with 100+ cards previously returned HTTP 400 and every live-sync transition failed (#186 drive-by).
- Log files always opened as UTF-8 — Windows cp1252 default was crashing on the Cyrillic preamble (#179 drive-by).
- `termios` / `tty` imports guarded for Windows compatibility (#179 drive-by).
- `claude_handoff` sync `run(timeout=0)` no longer enters a hot loop — `timeout=0` now means "no wait" instead of falling back to the default (#187 drive-by).

### Changed
- `WhillyConfig.from_env()` is now a thin wrapper over `load_layered()` — every existing caller transparently gets TOML support without code changes.
- `scripts/move_project_card.py` refactored to a 25-line wrapper around `ProjectBoardClient` (#183 drive-by).
- `whilly.example.toml` expanded to 36 top-level keys + 3 nested sections with Linux-man-style per-field annotations (#195).
- `.env` loader emits a one-time deprecation warning (silence with `WHILLY_SUPPRESS_DOTENV_WARNING=1`); run `whilly --config migrate` to convert existing `.env` into `whilly.toml` and push tokens into the OS keyring (#179).

### Deprecated
- `.env` support. Still functional — migrate with `whilly --config migrate`.

### Packaging
- Subpackages (`whilly.agents`, `whilly.sources`, `whilly.sinks`, `whilly.workflow`, `whilly.classifier`, `whilly.hierarchy`, `whilly.quality`) now actually ship in the wheel — previous builds silently dropped them (#173).
- New runtime dependencies: `platformdirs>=4.0`, `keyring>=24.0`, `tomli>=2.0` on Python 3.10 (stdlib `tomllib` on 3.11+).

### Tests
- 490 → **643 passing** (+153 new). Full suite runs on Linux / macOS / Windows on every PR.

## [3.1.0] - 2026-04-20

### Added
- 🛡️ **Self-Healing System** — Automatically detects, analyzes, and fixes code errors
  - Smart error detection via traceback pattern analysis  
  - Automated fixes for `NameError`, `ImportError`, `TypeError`
  - Auto-restart with exponential backoff strategy (max 3 retries)
  - Learning from historical error patterns in logs
  - Recovery suggestions for complex issues
- **New Scripts**:
  - `scripts/whilly_with_healing.py` — Self-healing wrapper with auto-restart
  - `scripts/sync_task_status.py` — Task status synchronization utility
  - `scripts/check_status_sync.py` — Status consistency monitoring
- **New Modules**:
  - `whilly/self_healing.py` — Core error analysis and auto-fix engine
  - `whilly/recovery.py` — Task status recovery and validation
- **Documentation**:
  - `docs/Self-Healing-Guide.md` — Comprehensive self-healing documentation
  - Updated README.md with self-healing features

### Fixed
- Fixed `NameError: name 'config' is not defined` in `wait_and_collect_subprocess`
- Fixed task status synchronization issues after orchestrator crashes
- Improved error handling in external task integrations

### Changed
- Enhanced README.md with self-healing system overview
- Updated project description to include self-healing capabilities
- Improved error reporting with structured analysis

### Technical Details
- Added `config` parameter to `wait_and_collect_subprocess` function signature
- Implemented pattern-based error detection using regex and AST analysis
- Created recovery mechanisms for task status inconsistencies
- Added exponential backoff retry logic with intelligent error categorization

## [3.0.0] - 2026-04-19

### Added
- Initial release of Whilly Orchestrator
- Continuous agent loop with Claude CLI integration
- Rich TUI dashboard with live progress monitoring
- Parallel execution via tmux panes and git worktrees
- Task decomposer for oversized tasks
- PRD wizard for interactive requirement generation
- TRIZ analyzer for contradiction analysis
- State store for persistent task management
- GitHub Issues and Jira integration
- Workshop kit for HackSprint1

### Features
- JSON-based task planning and execution
- Budget monitoring and cost tracking  
- Deadlock detection and recovery
- Authentication error handling
- Workspace isolation and cleanup
- External task closing automation

---

## Release Links

- [3.1.0](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3.1.0) - Self-Healing System Release
- [3.0.0](https://github.com/mshegolev/whilly-orchestrator/releases/tag/v3.0.0) - Initial Release