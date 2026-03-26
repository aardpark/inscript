"""Session export: chat transcripts, activity logs, codebase snapshots."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import (
    _load_jsonl,
    _load_prompts,
    session_dir,
)
from .replay import _format_duration


def _load_transcript(transcript_path: str | Path) -> list[dict]:
    """Load Claude Code conversation transcript from JSONL."""
    path = Path(transcript_path)
    if not path.exists():
        return []
    entries = []
    with path.open() as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def _extract_text_from_content(content) -> str:
    """Extract readable text from a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _extract_tool_uses(content) -> list[dict]:
    """Extract tool_use blocks from message content."""
    if not isinstance(content, list):
        return []
    tools = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tools.append({
                "name": block.get("name", "?"),
                "id": block.get("id", ""),
                "input": block.get("input", {}),
            })
    return tools


def _extract_tool_results(content) -> dict[str, str]:
    """Extract tool_result blocks from content, keyed by tool_use_id."""
    if not isinstance(content, list):
        return {}
    results = {}
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tid = block.get("tool_use_id", "")
            rc = block.get("content", "")
            text = ""
            if isinstance(rc, str):
                text = rc
            elif isinstance(rc, list):
                parts = []
                for sub in rc:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(sub["text"])
                text = "\n".join(parts)
            results[tid] = text
    return results


def _format_tool_use(tool: dict, result: str | None = None) -> list[str]:
    """Format a single tool use block as markdown lines."""
    name = tool["name"]
    inp = tool["input"]
    lines = []

    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        header = f"**Bash**: {desc}" if desc else "**Bash**"
        lines.append(header)
        lines.append(f"```bash\n{cmd}\n```")
        if result is not None:
            out = result.strip()
            if out:
                out_lines = out.split("\n")
                if len(out_lines) > 20:
                    out = "\n".join(out_lines[:20]) + f"\n... ({len(out_lines) - 20} more lines)"
                lines.append(f"```\n{out}\n```")
    elif name == "Read":
        fp = inp.get("file_path", "?")
        lines.append(f"**Read**: `{fp}`")
    elif name == "Write":
        fp = inp.get("file_path", "?")
        lines.append(f"**Write**: `{fp}`")
    elif name in ("Edit", "MultiEdit"):
        fp = inp.get("file_path", "?")
        lines.append(f"**Edit**: `{fp}`")
    elif name == "Glob":
        pat = inp.get("pattern", "?")
        path = inp.get("path", "")
        loc = f" in `{path}`" if path else ""
        lines.append(f"**Glob**: `{pat}`{loc}")
    elif name == "Grep":
        pat = inp.get("pattern", "?")
        path = inp.get("path", "")
        loc = f" in `{path}`" if path else ""
        lines.append(f"**Grep**: `{pat}`{loc}")
    elif name == "Agent":
        desc = inp.get("description", "?")
        lines.append(f"**Agent**: {desc}")
    elif name == "WebSearch":
        q = inp.get("query", "?")
        lines.append(f"**WebSearch**: {q}")
    elif name == "WebFetch":
        url = inp.get("url", "?")
        lines.append(f"**WebFetch**: `{url}`")
    elif name == "ToolSearch":
        return []
    else:
        summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in inp.items() if k != "type")
        lines.append(f"**{name}**({summary})")

    return lines


def _build_chat_timeline(transcript: list[dict]) -> list[dict]:
    """Build a chronological chat timeline from transcript entries.

    Returns list of dicts with: role, text, tools, tool_results, timestamp.
    Merges consecutive same-role messages.
    """
    all_tool_results: dict[str, str] = {}
    for entry in transcript:
        content = entry.get("message", {}).get("content", [])
        results = _extract_tool_results(content)
        all_tool_results.update(results)

    timeline = []
    for entry in transcript:
        msg_type = entry.get("type")
        if msg_type not in ("user", "assistant"):
            continue
        message = entry.get("message", {})
        content = message.get("content", [])
        text = _extract_text_from_content(content)
        tools = _extract_tool_uses(content) if msg_type == "assistant" else []
        ts = entry.get("timestamp", "")

        if not text and not tools:
            continue

        if timeline and timeline[-1]["role"] == msg_type:
            if text:
                if timeline[-1]["text"]:
                    timeline[-1]["text"] += "\n" + text
                else:
                    timeline[-1]["text"] = text
            timeline[-1]["tools"].extend(tools)
        else:
            timeline.append({
                "role": msg_type,
                "text": text,
                "tools": tools,
                "timestamp": ts,
            })

    for msg in timeline:
        msg["tool_results"] = all_tool_results

    return timeline


def _resolve_session_id(prefix: str) -> str | None:
    """Resolve a session ID prefix to full ID."""
    sdir = session_dir(prefix)
    if (sdir / "meta.json").exists():
        return prefix
    from . import SESSIONS_DIR
    matches = [d.name for d in SESSIONS_DIR.iterdir() if d.is_dir() and d.name.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        import sys
        print(f"Ambiguous prefix '{prefix}', matches: {', '.join(sorted(matches))}", file=sys.stderr)
    return None


def _snapshot_codebase(project_path: str | Path, dest: Path) -> tuple[int, int]:
    """Copy the project working directory into dest, skipping noise.

    Returns (files_copied, bytes_copied).
    """
    import shutil
    import subprocess

    src = Path(project_path)
    if not src.is_dir():
        return 0, 0

    git_files: list[str] | None = None
    try:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=src, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            git_files = [f for f in result.stdout.strip().split("\n") if f]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    SKIP_DIRS = {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        "target", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "dist", "build", ".eggs", ".next", ".nuxt",
        ".turbo", ".cache",
    }
    SKIP_EXTS = {
        ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
        ".o", ".a", ".class", ".jar",
        ".whl", ".tar", ".gz", ".zip", ".bz2",
    }
    MAX_FILE_SIZE = 2_000_000

    files_copied = 0
    bytes_copied = 0

    if git_files is not None:
        for rel in git_files:
            parts = Path(rel).parts
            if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts):
                continue
            fpath = src / rel
            if not fpath.is_file():
                continue
            try:
                size = fpath.stat().st_size
                if size > MAX_FILE_SIZE or fpath.suffix.lower() in SKIP_EXTS:
                    continue
                with fpath.open("rb") as f:
                    if b"\x00" in f.read(512):
                        continue
            except OSError:
                continue
            out_path = dest / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fpath, out_path)
            files_copied += 1
            bytes_copied += size
    else:
        for dirpath, dirnames, filenames in src.walk():
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.endswith(".egg-info")
            ]
            for fname in filenames:
                fpath = dirpath / fname
                try:
                    size = fpath.stat().st_size
                    if size > MAX_FILE_SIZE or fpath.suffix.lower() in SKIP_EXTS:
                        continue
                    with fpath.open("rb") as f:
                        if b"\x00" in f.read(512):
                            continue
                except OSError:
                    continue
                rel = fpath.relative_to(src)
                out_path = dest / rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fpath, out_path)
                files_copied += 1
                bytes_copied += size

    return files_copied, bytes_copied


def cmd_export(session_id: str):
    """Export a session as markdown report."""
    from . import _format_tokens

    resolved = _resolve_session_id(session_id)
    if resolved is None:
        import sys
        print(f"Session {session_id} not found", file=sys.stderr)
        sys.exit(1)
    session_id = resolved
    sdir = session_dir(session_id)

    meta = json.loads((sdir / "meta.json").read_text())
    summary = {}
    summary_file = sdir / "summary.json"
    if summary_file.exists():
        summary = json.loads(summary_file.read_text())

    prompts = _load_prompts(sdir)
    touches = _load_jsonl(sdir / "touches.jsonl")
    diffs = _load_jsonl(sdir / "diffs.jsonl")

    touches_by_prompt: dict[int | None, list[dict]] = {}
    for t in touches:
        touches_by_prompt.setdefault(t.get("prompt_idx"), []).append(t)

    project = meta.get("project", "?")
    print(f"# Session {session_id[:8]}\n")
    print(f"- Project: {project}")
    print(f"- Started: {meta.get('start_time', '?')}")
    if summary.get("duration_seconds"):
        print(f"- Duration: {_format_duration(summary['duration_seconds'])}")
    tok = summary.get("tokens")
    if tok:
        print(f"- Tokens: {_format_tokens(tok.get('total_tokens', 0))}")
    print()

    for p in prompts:
        idx = p.get("idx", 0)
        print(f"## Prompt {idx}: \"{p.get('prompt', '?')[:80]}\"\n")
        pt = touches_by_prompt.get(idx, [])
        if pt:
            for t in pt:
                action = t.get("action", "?")
                f = t.get("file", "?")
                extra = ""
                if t.get("lines_changed"):
                    extra = f" (+{t['lines_changed']} lines)"
                print(f"- {action}: {f}{extra}")
            print()
        pd = [d for d in diffs if d.get("prompt_idx") == idx]
        for d in pd:
            if d.get("old_string") is not None:
                print(f"**{d.get('file', '?')}**")
                print(f"```diff\n- {d['old_string']}\n+ {d.get('new_string', '')}\n```\n")
            elif d.get("is_new"):
                print(f"New file `{d.get('file', '?')}` ({d.get('lines', '?')} lines)\n")


def cmd_chat_export(session_id: str, output_dir: str | None = None, snapshot: bool = False):
    """Export a complete session bundle: chat, notes, diffs, linked files."""
    import shutil

    resolved = _resolve_session_id(session_id)
    if resolved is None:
        import sys
        print(f"Session {session_id} not found", file=sys.stderr)
        sys.exit(1)
    session_id = resolved
    sdir = session_dir(session_id)
    meta_file = sdir / "meta.json"

    meta = json.loads(meta_file.read_text())
    summary = {}
    summary_file = sdir / "summary.json"
    if summary_file.exists():
        summary = json.loads(summary_file.read_text())

    if output_dir:
        out = Path(output_dir)
    else:
        out = Path.cwd() / f"inscript-export-{session_id[:8]}"
    out.mkdir(parents=True, exist_ok=True)

    prompts = _load_prompts(sdir)
    touches = _load_jsonl(sdir / "touches.jsonl")
    diffs = _load_jsonl(sdir / "diffs.jsonl")
    notes = _load_jsonl(sdir / "notes.jsonl")
    commits = _load_jsonl(sdir / "commits.jsonl")

    touches_by_prompt: dict[int | None, list[dict]] = {}
    for t in touches:
        touches_by_prompt.setdefault(t.get("prompt_idx"), []).append(t)
    diffs_by_prompt: dict[int | None, list[dict]] = {}
    for d in diffs:
        diffs_by_prompt.setdefault(d.get("prompt_idx"), []).append(d)

    transcript_path = meta.get("transcript_path")
    transcript = _load_transcript(transcript_path) if transcript_path else []
    chat_timeline = _build_chat_timeline(transcript)

    all_files = set()
    for t in touches:
        f = t.get("file")
        if f:
            all_files.add(f)

    # Write chat.md
    chat_lines = [f"# Chat — Session {session_id[:8]}\n"]
    chat_lines.append(f"- **Project**: {meta.get('project', '?')}")
    chat_lines.append(f"- **Started**: {meta.get('start_time', '?')}")
    if summary.get("end_time"):
        chat_lines.append(f"- **Ended**: {summary['end_time']}")
    if summary.get("duration_seconds"):
        chat_lines.append(f"- **Duration**: {_format_duration(summary['duration_seconds'])}")
    chat_lines.append("")

    if chat_timeline:
        for msg in chat_timeline:
            role = msg["role"]
            text = msg["text"].strip()
            tools = msg["tools"]
            tool_results = msg.get("tool_results", {})
            if role == "user":
                chat_lines.append(f"## You\n")
                if text:
                    chat_lines.append(text)
                chat_lines.append("")
            else:
                chat_lines.append(f"## Claude\n")
                if text:
                    chat_lines.append(text)
                    chat_lines.append("")
                if tools:
                    for tool in tools:
                        result = tool_results.get(tool.get("id"))
                        fmt = _format_tool_use(tool, result)
                        if fmt:
                            chat_lines.extend(fmt)
                            chat_lines.append("")
                chat_lines.append("")
    else:
        chat_lines.append("*(No transcript found — showing prompts only)*\n")
        for p in prompts:
            chat_lines.append(f"## Prompt {p.get('idx', '?')} — {p.get('ts', '')}\n")
            chat_lines.append(p.get("prompt", ""))
            chat_lines.append("")

    (out / "chat.md").write_text("\n".join(chat_lines))

    # Write activity.md
    act_lines = [f"# Activity Log — Session {session_id[:8]}\n"]
    if prompts:
        for p in prompts:
            idx = p.get("idx", 0)
            act_lines.append(f"## Prompt {idx}: \"{p.get('prompt', '?')[:80]}\"\n")
            act_lines.append(f"*{p.get('ts', '')}*\n")
            pt = touches_by_prompt.get(idx, [])
            if pt:
                act_lines.append("| Action | File | Details |")
                act_lines.append("|--------|------|---------|")
                for t in pt:
                    details = ""
                    if t.get("lines_changed"):
                        details = f"{t['lines_changed']} lines"
                    elif t.get("lines"):
                        details = f"{t['lines']} lines"
                    act_lines.append(f"| {t.get('action', '')} | `{t.get('file', '')}` | {details} |")
                act_lines.append("")
            pd = diffs_by_prompt.get(idx, [])
            for d in pd:
                if d.get("old_string") is not None:
                    act_lines.append(f"**`{d.get('file', '?')}`**")
                    act_lines.append(f"```diff\n- {d['old_string']}\n+ {d.get('new_string', '')}\n```\n")
                elif d.get("is_new"):
                    act_lines.append(f"**`{d.get('file', '?')}`** — new file ({d.get('lines', '?')} lines)\n")
    (out / "activity.md").write_text("\n".join(act_lines))

    # Write notes.md
    if notes:
        note_lines = [f"# Notes — Session {session_id[:8]}\n"]
        for n in notes:
            note_lines.append(f"### {n.get('ts', '')}")
            if n.get("prompt_idx") is not None:
                note_lines.append(f"*Prompt {n['prompt_idx']}*")
            if n.get("ref"):
                note_lines.append(f"*Ref: `{n['ref']}`*")
            note_lines.append("")
            note_lines.append(n.get("text", ""))
            note_lines.append("")
        (out / "notes.md").write_text("\n".join(note_lines))

    # Write commits.md
    if commits:
        commit_lines = [f"# Commits — Session {session_id[:8]}\n"]
        for c in commits:
            commit_lines.append(f"### `{c.get('hash', '?')[:8]}` — {c.get('message', '?')}\n")
            commit_lines.append(f"*{c.get('ts', '')}* | Prompt {c.get('prompt_idx', '?')}")
            if c.get("contributing_prompts"):
                commit_lines.append(f"Contributing prompts: {c['contributing_prompts']}")
            if c.get("files"):
                commit_lines.append(f"Files: {', '.join(f'`{f}`' for f in c['files'])}")
            commit_lines.append("")
        (out / "commits.md").write_text("\n".join(commit_lines))

    # Copy linked files
    files_dir = out / "files"
    copied = 0
    for fpath_str in sorted(all_files):
        fpath = Path(fpath_str)
        if not fpath.exists() or not fpath.is_file():
            continue
        try:
            size = fpath.stat().st_size
            if size > 1_000_000:
                continue
            with fpath.open("rb") as f:
                if b"\x00" in f.read(512):
                    continue
        except OSError:
            continue
        rel = fpath.name
        dest = files_dir / rel
        if dest.exists():
            rel = f"{fpath.parent.name}_{fpath.name}"
            dest = files_dir / rel
        files_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fpath, dest)
        copied += 1

    # Snapshot codebase
    snap_files = 0
    snap_bytes = 0
    if snapshot:
        project_path = meta.get("project")
        if project_path and Path(project_path).is_dir():
            snap_dir = out / "codebase"
            snap_files, snap_bytes = _snapshot_codebase(project_path, snap_dir)

    # Write meta.json
    export_meta = {
        "session_id": session_id,
        "project": meta.get("project"),
        "start_time": meta.get("start_time"),
        "end_time": summary.get("end_time"),
        "duration_seconds": summary.get("duration_seconds"),
        "prompt_count": len(prompts),
        "files_touched": len(all_files),
        "files_copied": copied,
        "notes_count": len(notes),
        "commits_count": len(commits),
        "has_transcript": bool(chat_timeline),
        "snapshot": snapshot,
        "snapshot_files": snap_files,
        "snapshot_bytes": snap_bytes,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / "meta.json").write_text(json.dumps(export_meta, indent=2))

    print(f"Exported to {out}/")
    print(f"  chat.md       — full conversation ({len(chat_timeline)} messages)")
    print(f"  activity.md   — file activity + diffs ({len(prompts)} prompts)")
    if notes:
        print(f"  notes.md      — {len(notes)} notes")
    if commits:
        print(f"  commits.md    — {len(commits)} commits")
    print(f"  files/        — {copied} file snapshots")
    if snapshot:
        def _fmt_bytes(n: int) -> str:
            if n < 1024: return f"{n}B"
            if n < 1024**2: return f"{n/1024:.1f}KB"
            return f"{n/1024**2:.1f}MB"
        print(f"  codebase/     — {snap_files} files ({_fmt_bytes(snap_bytes)})")
    print(f"  meta.json     — export metadata")
