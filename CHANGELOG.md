# Changelog

## 0.9.1 (2026-03-26)

**Bug fixes:**
- Fix: config file was never loaded (tomllib got bytes instead of string, silently fell back to defaults)
- Fix: `log()` and `replay()` returned empty for completed sessions (session ID prefix not resolved to full UUID)
- Fix: `_format_tokens` used before import in viz.py (NameError on sessions with token data)
- Fix: `session_dir` missing from hook.py imports (NameError on overlap detection)
- Fix: hook `main()` could crash on unhandled exceptions (hooks must never crash)
- Fix: redundant `except (OSError, Exception)` clause

**New features:**
- `message()` tool now includes assistant response text from Claude Code transcripts
- New `thread()` tool — cross-session timeline grouped by session, time-windowed (default 30min)
- Path traversal guard on `session_dir()` (prevents `../` escaping .inscript directory)

**Portability:**
- Cross-platform PPID handling (Windows fallback)
- Cross-platform path detection (`Path.is_absolute()` instead of `startswith("/")`)
- Venv-aware `inscript setup` (absolute paths in venv, bare names for system installs)
- `tomli` fallback for Python 3.10 (tomllib is 3.11+)
- `anthropic` moved to optional dependency (`pip install inscript[reflect]`)

**Code quality:**
- Split commands.py (1538 lines) into commands.py (598), export.py (563), permissions.py (286)
- Consolidated duplicate `_append_jsonl` into `__init__.py`
- Removed 13 unused imports, 1 dead function, 2 dead constants
- Removed orphaned `reflect.py` module
- Archived `categories` and `concepts` from MCP tools (still available as CLI)

## 0.9.0 (2026-03-26)

Yanked — contained proprietary integration references.

## 0.8.0 (2026-03-25)

Yanked — contained proprietary integration references.

## 0.7.0 (2026-03-18)

Yanked — contained proprietary integration references.

## 0.6.0 (2026-03-18)

Yanked — contained proprietary integration references.

## 0.5.0 (2026-03-17)

- Session handoff context on SessionStart
- Behavioral branch/detour inference
- Cross-session overlap detection
- Interactive TUI explorer (`inscript explore`)

## 0.4.0 (2026-03-17)

- Tag system for organizing work within sessions
- Time tracking by tag
- Session branches (scoped detours)
- Export sessions as markdown

## 0.3.0 (2026-03-17)

- Diff recording (configurable via `store_diffs`)
- Prompt indexing with timestamps
- Session summary with token usage from transcripts

## 0.2.0 (2026-03-17)

- Per-process session tracking (multiple Claude Code instances)
- Retention policy and cleanup command
- Session visualization heatmap (`inscript viz`)

## 0.1.0 (2026-03-17)

- Initial release
- Session recording via Claude Code hooks (SessionStart, PostToolUse, Stop)
- File touch tracking (read/edit/write)
- Basic CLI: `inscript log`, `inscript status`
