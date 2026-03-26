"""Permission profile generation from session tool usage."""
from __future__ import annotations

import json
from pathlib import Path

from . import (
    SESSIONS_DIR,
    _load_jsonl,
    active_session,
    session_dir,
)
from .export import _resolve_session_id


def _generalize_bash_command(cmd: str) -> str | None:
    """Turn a specific Bash command into a generalized permission pattern."""
    cmd = cmd.strip()
    if not cmd:
        return None

    if " && " in cmd:
        segments = [s.strip() for s in cmd.split(" && ")]
        trivial = {"export", "cd", "source"}
        for seg in reversed(segments):
            first = seg.split()[0].split("/")[-1] if seg.split() else ""
            if first not in trivial:
                cmd = seg
                break
        else:
            cmd = segments[0]

    for sep in (" || ", " | ", " ; "):
        if sep in cmd:
            cmd = cmd.split(sep)[0].strip()

    parts = cmd.split()
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        parts.pop(0)
    if not parts:
        return None

    binary = parts[0]

    if binary.startswith("#") or binary.startswith("\\"):
        return None
    if binary == "printf" and "jsonrpc" in cmd:
        return None
    if binary in ("for", "do", "done", "fi", "then", "else", "elif", "esac", "while", "case", "if"):
        return None

    basename = binary.split("/")[-1]

    SAFE_WILDCARDS = {
        "ls", "wc", "find", "grep", "cat", "head", "tail", "du", "df",
        "echo", "pwd", "which", "file", "stat", "readlink", "basename",
        "dirname", "sort", "uniq", "cut", "tr", "tee", "diff", "comm",
        "tree", "env", "printenv", "date", "uname", "whoami", "id",
        "pdfinfo", "pdftotext", "pdftoppm", "jq", "xargs",
        "time", "timeout", "jobs", "ps",
        "sed", "awk", "tput", "printf", "osascript",
    }
    if basename in SAFE_WILDCARDS:
        return f"Bash({basename}:*)"

    if basename == "git":
        return "Bash(git:*)"
    if basename == "gh":
        return "Bash(gh:*)"
    if basename == "cargo":
        return "Bash(cargo:*)"

    if basename in ("pip", "pip3", "pipx"):
        if len(parts) > 1 and parts[1] in ("install", "show", "list", "index", "uninstall"):
            return f"Bash({basename} {parts[1]}:*)"
        return f"Bash({basename}:*)"
    if basename == "brew":
        return "Bash(brew:*)"

    if basename.startswith("python"):
        return f"Bash({basename}:*)"

    if basename in ("rsync", "cp", "mv", "mkdir", "chmod", "touch", "rm"):
        return f"Bash({basename}:*)"
    if basename in ("curl", "wget"):
        return f"Bash({basename}:*)"
    if basename == "open":
        return "Bash(open:*)"
    if basename in ("kill", "pkill"):
        return f"Bash({basename}:*)"
    if basename in ("rustc", "rustup"):
        return f"Bash({basename}:*)"
    if basename in ("source", "export"):
        return f"Bash({basename}:*)"
    if basename == "inscript":
        return "Bash(inscript:*)"

    if binary.startswith("./"):
        return None

    if ".venv/bin/" in binary or "/venv/bin/" in binary:
        return f"Bash({binary}:*)"

    if basename == "cd":
        return "Bash(cd:*)"

    if len(parts) > 1:
        return f"Bash({binary} {parts[1]}:*)"
    return f"Bash({binary}:*)"


def _generalize_file_path(path: str, project: str | None) -> str:
    """Turn a specific file path into a Read/Write permission pattern."""
    if project and path.startswith(project):
        return f"/{project}/**"
    if path.startswith("/tmp"):
        return "/tmp/**"
    if path.startswith("/private/tmp"):
        return "/private/tmp/**"
    if path.startswith("/var/folders") or path.startswith("/private/var/folders"):
        return "/private/tmp/**"
    home = str(Path.home())
    if path.startswith(home):
        rel = path[len(home):].lstrip("/").lstrip("\\")
        parts = rel.split("/")
        if len(parts) >= 2:
            return f"/{home}/{parts[0]}/**"
        return f"/{home}/**"
    return f"/{path}"


def cmd_permissions(session_id: str | None = None, apply: bool = False):
    """Analyze tool usage and generate a permissions profile."""
    sessions_to_check = []
    if session_id:
        resolved = _resolve_session_id(session_id)
        if resolved:
            sessions_to_check = [resolved]
        else:
            import sys
            print(f"Session {session_id} not found", file=sys.stderr)
            return
    else:
        for sdir in SESSIONS_DIR.iterdir():
            if sdir.is_dir() and (sdir / "meta.json").exists():
                sessions_to_check.append(sdir.name)

    bash_commands: list[str] = []
    read_paths: set[str] = set()
    write_paths: set[str] = set()
    web_domains: set[str] = set()
    has_web_search = False
    projects: set[str] = set()

    for sid in sessions_to_check:
        sdir = session_dir(sid)
        meta_file = sdir / "meta.json"
        if not meta_file.exists():
            continue
        meta = json.loads(meta_file.read_text())
        project = meta.get("project")
        if project:
            projects.add(project)

        transcript_path = meta.get("transcript_path")
        if not transcript_path or not Path(transcript_path).exists():
            continue

        with Path(transcript_path).open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    if name == "Bash":
                        cmd = inp.get("command", "")
                        if cmd:
                            bash_commands.append(cmd)
                    elif name == "Read":
                        fp = inp.get("file_path", "")
                        if fp:
                            read_paths.add(fp)
                    elif name in ("Write", "Edit"):
                        fp = inp.get("file_path", "")
                        if fp:
                            write_paths.add(fp)
                    elif name == "WebFetch":
                        url = inp.get("url", "")
                        if url:
                            try:
                                from urllib.parse import urlparse
                                domain = urlparse(url).netloc
                                if domain:
                                    web_domains.add(domain)
                            except Exception:
                                pass
                    elif name == "WebSearch":
                        has_web_search = True

    perms: set[str] = set()

    for cmd in bash_commands:
        p = _generalize_bash_command(cmd)
        if p:
            perms.add(p)

    read_dirs: set[str] = set()
    for fp in read_paths:
        for proj in projects:
            pat = _generalize_file_path(fp, proj)
            read_dirs.add(pat)
            break
        else:
            read_dirs.add(_generalize_file_path(fp, None))
    for d in read_dirs:
        perms.add(f"Read({d})")

    write_dirs: set[str] = set()
    for fp in write_paths:
        for proj in projects:
            pat = _generalize_file_path(fp, proj)
            write_dirs.add(pat)
            break
        else:
            write_dirs.add(_generalize_file_path(fp, None))
    for d in write_dirs:
        perms.add(f"Edit({d})")
        perms.add(f"Write({d})")

    for domain in web_domains:
        perms.add(f"WebFetch(domain:{domain})")
    if has_web_search:
        perms.add("WebSearch")

    def _dedup_path_perms(perm_set: set[str]) -> set[str]:
        result = set(perm_set)
        to_remove = set()
        path_perms = [p for p in result if "/**)" in p]
        for p1 in path_perms:
            prefix1 = p1.split("(")[1].rstrip("/**)")
            tool1 = p1.split("(")[0]
            for p2 in path_perms:
                if p1 == p2:
                    continue
                prefix2 = p2.split("(")[1].rstrip("/**)")
                tool2 = p2.split("(")[0]
                if tool1 == tool2 and prefix2.startswith(prefix1 + "/"):
                    to_remove.add(p2)
        return result - to_remove

    perms = _dedup_path_perms(perms)
    sorted_perms = sorted(perms)

    if apply:
        settings_path = Path.home() / ".claude" / "settings.local.json"
        settings = {}
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())

        existing = set(settings.get("permissions", {}).get("allow", []))
        merged = sorted(existing | perms)

        settings.setdefault("permissions", {})["allow"] = merged
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        new_count = len(perms - existing)
        print(f"Applied {new_count} new permissions ({len(merged)} total) to {settings_path}")
    else:
        print(f"# Permissions profile ({len(sorted_perms)} rules from {len(sessions_to_check)} sessions)\n")
        print(f"# Projects: {', '.join(sorted(projects))}")
        print(f"# Bash: {len([p for p in sorted_perms if p.startswith('Bash')])} rules")
        print(f"# File: {len([p for p in sorted_perms if p.startswith(('Read', 'Edit', 'Write'))])} rules")
        print(f"# Web: {len([p for p in sorted_perms if p.startswith(('Web'))])} rules")
        print()
        for p in sorted_perms:
            print(p)
        print(f"\n# Run with --apply to write to ~/.claude/settings.local.json")
