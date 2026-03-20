"""Interactive TUI explorer and session dashboard."""
from __future__ import annotations

import json
from pathlib import Path

from . import (
    _load_jsonl,
    _load_prompts,
    list_sessions,
    session_dir,
)
from .commands import _format_secs
from .inference import infer_branches
from .replay import _format_duration
from .viz import (
    _diff_summary,
    _load_transcript_responses,
    _viz_short,
    _viz_trunc,
    load_viz_context,
)


def cmd_dashboard():
    """Session picker dashboard. Select a session to explore."""
    import curses

    sessions = list_sessions()
    if not sessions:
        print("No sessions found")
        return

    home = str(Path.home())

    session_info: list[dict] = []
    for s in sessions:
        sid = s["session_id"]
        sdir = session_dir(sid)
        start = s.get("start_time", "?")
        status = s.get("status", "?")
        prompts = _load_prompts(sdir)
        touches = _load_jsonl(sdir / "touches.jsonl")
        summary = {}
        if (sdir / "summary.json").exists():
            try:
                summary = json.loads((sdir / "summary.json").read_text())
            except (json.JSONDecodeError, OSError):
                pass
        last_time = start
        summary_end = summary.get("end_time", "")
        if summary_end:
            last_time = summary_end
        elif prompts:
            last_prompt_ts = prompts[-1].get("ts", "")
            if last_prompt_ts and "T" in start:
                start_hour = int(start.split("T")[1].split(":")[0])
                prompt_hour = int(last_prompt_ts.split(":")[0])
                date = start.split("T")[0]
                if prompt_hour < start_hour:
                    from datetime import datetime as _dt, timedelta as _td
                    d = _dt.fromisoformat(date) + _td(days=1)
                    date = d.strftime("%Y-%m-%d")
                last_time = date + "T" + last_prompt_ts
        dur = ""
        if summary.get("duration_seconds"):
            dur = _format_duration(summary["duration_seconds"])
        total_edits = sum(1 for t in touches if t.get("action") in ("edit", "write"))
        file_edits: dict[str, int] = {}
        for t in touches:
            if t.get("action") in ("edit", "write"):
                f = t.get("file", "?").split("/")[-1]
                file_edits[f] = file_edits.get(f, 0) + 1
        top = sorted(file_edits.items(), key=lambda x: -x[1])[:3]
        top_str = ", ".join(f"{name}" for name, _ in top) if top else ""
        session_info.append({
            "sid": sid, "start": start, "last_time": last_time,
            "status": status, "dur": dur, "prompts": len(prompts),
            "edits": total_edits, "top_files": top_str,
        })

    session_info.sort(key=lambda s: s.get("last_time", ""), reverse=True)

    def _run_dashboard(stdscr):
        curses.curs_set(0)
        curses.set_escdelay(25)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
        cursor = 0
        scroll = 0
        while True:
            stdscr.clear()
            height, width = stdscr.getmaxyx()
            try:
                stdscr.addnstr(0, 0, f"inscript dashboard — {len(session_info)} sessions", width - 1, curses.A_BOLD)
            except curses.error:
                pass
            list_start = 2
            visible_rows = height - 4
            if cursor < scroll:
                scroll = cursor
            if cursor >= scroll + visible_rows:
                scroll = cursor - visible_rows + 1
            for i in range(scroll, min(len(session_info), scroll + visible_rows)):
                si = session_info[i]
                y = list_start + (i - scroll)
                if y >= height - 2:
                    break
                date = si["start"].split("T")[0] if "T" in si["start"] else si["start"][:10]
                time_part = si["start"].split("T")[1][:5] if "T" in si["start"] else ""
                sid_short = si["sid"][:8]
                line = f"  {date} {time_part}  {sid_short}  "
                line += f"{si['dur'] or '-':>6s}  "
                line += f"{si['prompts']:>3} prompts  "
                line += f"{si['edits']:>3} edits"
                if si["top_files"]:
                    remaining = width - len(line) - 4
                    if remaining > 5:
                        line += f"  {si['top_files'][:remaining]}"
                attr = curses.color_pair(5) | curses.A_BOLD if i == cursor else curses.A_NORMAL
                if si["status"] == "active":
                    attr = curses.color_pair(5) | curses.A_BOLD if i == cursor else curses.color_pair(2)
                try:
                    stdscr.addnstr(y, 0, line, width - 1, attr)
                except curses.error:
                    pass
            try:
                stdscr.addnstr(height - 1, 0, " up/dn select  enter> explore  q quit", width - 1, curses.A_DIM | curses.A_REVERSE)
            except curses.error:
                pass
            stdscr.refresh()
            key = stdscr.getch()
            if key == ord("q") or key == 27:
                break
            elif key == curses.KEY_DOWN or key == ord("j"):
                if cursor < len(session_info) - 1:
                    cursor += 1
            elif key == curses.KEY_UP or key == ord("k"):
                if cursor > 0:
                    cursor -= 1
            elif key == ord("\n") or key == curses.KEY_RIGHT:
                selected_sid = session_info[cursor]["sid"]
                _run_explore_inner(stdscr, selected_sid)
            elif key == curses.KEY_HOME or key == ord("g"):
                cursor = 0
            elif key == curses.KEY_END or key == ord("G"):
                cursor = len(session_info) - 1

    curses.wrapper(_run_dashboard)


def _run_explore_inner(stdscr, session_id: str):
    ctx = load_viz_context(session_id)
    if ctx is None:
        return
    _explore_loop(stdscr, ctx)


def cmd_explore(session_id: str | None):
    import curses
    ctx = load_viz_context(session_id)
    if ctx is None:
        return
    curses.wrapper(lambda stdscr: _explore_loop(stdscr, ctx))


def _explore_loop(stdscr, ctx: dict):
    """The explore TUI main loop."""
    import curses

    prompts = ctx["prompts"]
    touches = ctx["touches"]
    touches_by_prompt = ctx["touches_by_prompt"]
    diffs = ctx["diffs"]
    detour_prompts = ctx["detour_prompts"]
    decision_prompts = ctx.get("decision_prompts", set())
    inferred = ctx["inferred"]
    project_letter = ctx["project_letter"]
    summary = ctx["summary"]
    home = ctx["home"]
    _infer_project = ctx["_infer_project"]

    all_prompts = list(enumerate(prompts))
    if not all_prompts:
        return

    file_order: list[str] = []
    file_set: set[str] = set()
    for t in touches:
        f = t.get("file", "?")
        if f not in file_set:
            file_order.append(f)
            file_set.add(f)

    detour_reason: dict[int, str] = {}
    for ib in inferred:
        for idx in range(ib["start_idx"], ib["end_idx"] + 1):
            detour_reason[idx] = ib["reason"]

    transcript_path = ctx["meta"].get("transcript_path", "")
    responses = _load_transcript_responses(transcript_path, prompts)

    PANELS = ["Files", "Chat", "Diffs"]

    def _run_inner():
        curses.curs_set(0)
        curses.set_escdelay(25)
        curses.use_default_colors()

        def safe_addstr(y, x, text, maxw=None, attr=0):
            h, w = stdscr.getmaxyx()
            if y < 0 or y >= h or x >= w:
                return
            if maxw is None:
                maxw = w - x - 1
            maxw = max(0, min(maxw, w - x - 1))
            if maxw <= 0:
                return
            try:
                stdscr.addnstr(y, x, text, maxw, attr)
            except curses.error:
                pass

        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_GREEN, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(8, curses.COLOR_YELLOW, -1)
        curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_YELLOW)

        cursor = 0
        panel = 0
        panel_scroll = 0
        focus = 0
        view_mode = 0

        grid_data: dict[str, dict[int, str]] = {}
        for t in touches:
            f = t.get("file", "?")
            pidx = t.get("prompt_idx")
            if pidx is None:
                continue
            proj = _infer_project(t)
            letter = project_letter.get(proj, "?")
            is_edit = t.get("action", "") in ("edit", "write")
            char = letter.upper() if is_edit else letter.lower()
            if f not in grid_data:
                grid_data[f] = {}
            existing = grid_data[f].get(pidx)
            if existing is None or is_edit:
                grid_data[f][pidx] = char

        while True:
            stdscr.clear()
            height, width = stdscr.getmaxyx()
            pos, prompt = all_prompts[cursor]
            idx = prompt.get("idx", 0)
            pts = touches_by_prompt.get(idx, [])
            has_files = bool(pts)
            has_chat = bool(responses.get(idx, []))
            prompt_diffs_exist = any(d.get("prompt_idx") == idx for d in diffs)
            panel_has_content = [has_files, has_chat, prompt_diffs_exist]

            sid = ctx["session_id"][:8] if len(ctx["session_id"]) > 16 else ctx["session_id"]
            dur = ""
            if summary.get("duration_seconds"):
                dur = f" ({_format_duration(summary['duration_seconds'])})"
            total_edits = sum(1 for t in touches if t.get("action") in ("edit", "write"))
            header_text = f"{sid}{dur} | {len(file_order)} files, {total_edits} edits, {len(prompts)} prompts"
            safe_addstr(0, 0, header_text, width - 1, curses.A_BOLD)

            bar_y = 2
            label = "> " if focus == 0 else "  "
            bar_tokens: list[tuple[str, int, bool]] = []
            for ai, (apos, ap) in enumerate(all_prompts):
                aidx = ap.get("idx", 0)
                has_activity = bool(touches_by_prompt.get(aidx))
                is_notable = has_activity or aidx in decision_prompts
                num = str(aidx + 1)
                if ai == cursor:
                    if aidx in detour_prompts:
                        attr = curses.color_pair(6)
                    elif aidx in decision_prompts:
                        attr = curses.color_pair(9)
                    else:
                        attr = curses.color_pair(5)
                    bar_tokens.append((f">{num}", attr | curses.A_BOLD, True))
                elif not is_notable:
                    bar_tokens.append((" ·", curses.A_DIM, False))
                elif aidx in detour_prompts:
                    bar_tokens.append((f" {num}", curses.color_pair(3), False))
                elif aidx in decision_prompts:
                    bar_tokens.append((f" {num}", curses.color_pair(8), False))
                else:
                    bar_tokens.append((f" {num}", curses.A_NORMAL, False))

            token_positions: list[int] = []
            x = 0
            cursor_token_start = 0
            cursor_token_end = 0
            for i, (text, attr, is_cur) in enumerate(bar_tokens):
                token_positions.append(x)
                if is_cur:
                    cursor_token_start = x
                    cursor_token_end = x + len(text)
                x += len(text)

            bar_width = width - len(label) - 1
            scroll = 0
            if cursor_token_end > bar_width:
                scroll = cursor_token_start - bar_width // 3
                scroll = max(0, min(scroll, x - bar_width))

            safe_addstr(bar_y, 0, label, width - 1, curses.A_DIM)
            for i, (text, attr, is_cur) in enumerate(bar_tokens):
                tok_start = token_positions[i]
                tok_end = tok_start + len(text)
                if tok_end <= scroll:
                    continue
                if tok_start - scroll + len(label) >= width - 1:
                    break
                visible_start = max(0, scroll - tok_start)
                visible_text = text[visible_start:]
                draw_x = len(label) + tok_start - scroll + visible_start
                if draw_x < len(label):
                    draw_x = len(label)
                if draw_x < width - 1:
                    safe_addstr(bar_y, draw_x, visible_text, width - draw_x - 1, attr)

            if scroll > 0:
                safe_addstr(bar_y, len(label), "<", 1, curses.A_DIM)
            if x - scroll > bar_width:
                safe_addstr(bar_y, width - 2, ">", 1, curses.A_DIM)

            if view_mode == 1:
                grid_y = 4
                label_w = min(35, width // 3)
                cell_w = 2
                cols_visible = (width - label_w - 2) // cell_w
                cursor_prompt_idx = all_prompts[cursor][1].get("idx", 0)
                all_idxs = [pp.get("idx", 0) for pp in prompts]
                cursor_col = all_idxs.index(cursor_prompt_idx) if cursor_prompt_idx in all_idxs else 0
                grid_scroll_x = max(0, cursor_col - cols_visible // 3)
                visible_idxs = all_idxs[grid_scroll_x:grid_scroll_x + cols_visible]
                h_line = " " * label_w + "  "
                for vidx in visible_idxs:
                    n = vidx + 1
                    if vidx == cursor_prompt_idx:
                        h_line += str(n % 100).rjust(2)
                    elif n == 1 or n % 5 == 0:
                        h_line += str(n % 100).rjust(2)
                    elif vidx in detour_prompts:
                        h_line += " ×"
                    elif vidx in decision_prompts:
                        h_line += " ?"
                    else:
                        h_line += " ·"
                safe_addstr(grid_y, 0, h_line, attr=curses.A_DIM)
                safe_addstr(grid_y + 1, 0, " " * label_w + "  " + "──" * len(visible_idxs))
                max_file_rows = height - grid_y - 4
                for fi, f in enumerate(file_order[:max_file_rows]):
                    if grid_y + 2 + fi >= height - 2:
                        break
                    short = _viz_short(f, home)
                    lbl = _viz_trunc(short, label_w)
                    row = lbl + "  "
                    for vidx in visible_idxs:
                        char = grid_data.get(f, {}).get(vidx, "·")
                        if vidx == cursor_prompt_idx:
                            row += f">{char}"
                        elif vidx in detour_prompts:
                            row += f"│{char}"
                        else:
                            row += f" {char}"
                    safe_addstr(grid_y + 2 + fi, 0, row)
                legend_y = min(grid_y + 2 + min(len(file_order), max_file_rows) + 1, height - 3)
                parts = []
                for p_name in ctx["project_list"]:
                    letter = project_letter[p_name]
                    name = p_name.split("/")[-1]
                    parts.append(f"{letter}={name}")
                safe_addstr(legend_y, 0, "  " + "  ".join(parts) + "  UPPER=edit  lower=read", attr=curses.A_DIM)
            else:
                info_y = 4
                prompt_label = f"Prompt {idx + 1}/{len(prompts)}"
                if idx in detour_prompts:
                    prompt_label += f"  [detour: {detour_reason.get(idx, '?')}]"
                if idx in decision_prompts:
                    prompt_label += "  [decision point]"
                safe_addstr(info_y, 0, prompt_label, width - 1, curses.A_BOLD)
                prompt_text = prompt.get("prompt", "")
                line = 0
                remaining = prompt_text
                while remaining and line < 2:
                    chunk = remaining[:width - 4]
                    safe_addstr(info_y + 1 + line, 2, chunk, width - 3)
                    remaining = remaining[width - 4:]
                    line += 1
                tag = prompt.get("tag")
                if tag:
                    safe_addstr(info_y + 1 + line, 2, f"tag: {tag}", width - 3, curses.A_DIM)
                    line += 1
                tab_y = info_y + 2 + line + 1
                tab_indicator = "> " if focus == 1 else "  "
                content_indicator = "> " if focus == 2 else "  "
                safe_addstr(tab_y, 0, tab_indicator, width - 1)
                tab_x = len(tab_indicator)
                for pi, pname in enumerate(PANELS):
                    if pi == panel and focus == 1:
                        safe_addstr(tab_y, tab_x, f"[{pname}]", width - tab_x - 1, curses.color_pair(7))
                    elif pi == panel:
                        safe_addstr(tab_y, tab_x, f"[{pname}]", width - tab_x - 1, curses.A_NORMAL)
                    else:
                        safe_addstr(tab_y, tab_x, f" {pname} ", width - tab_x - 1, curses.A_DIM)
                    tab_x += len(pname) + 3
                safe_addstr(tab_y + 1, 0, content_indicator.rstrip(), width - 1, curses.A_DIM)
                content_y = tab_y + 2
                max_rows = height - content_y - 2
                content_lines = [0, 0, 0]
                if has_files:
                    _fa: dict[str, list] = {}
                    for t in pts:
                        _fa.setdefault(t.get("file", "?"), []).append(t)
                    content_lines[0] = len(_fa)
                if has_chat:
                    _ww = max(1, width - 6)
                    _cl = 0
                    for _ptype, _ptext in responses.get(idx, []):
                        if _ptype == "tool":
                            _cl += 1
                        else:
                            for _rl in _ptext.split("\n"):
                                _cl += max(1, (len(_rl) + _ww - 1) // _ww)
                            if _ptype == "thinking":
                                _cl += 2
                    content_lines[1] = _cl
                if prompt_diffs_exist:
                    _dlc = 0
                    for d in diffs:
                        if d.get("prompt_idx") != idx:
                            continue
                        if d.get("old_string") is not None:
                            _dlc += 2
                            _ol = d["old_string"].split("\n")
                            _nl = d.get("new_string", "").split("\n")
                            _shown = 0
                            for _oi, _o in enumerate(_ol):
                                _n = _nl[_oi] if _oi < len(_nl) else None
                                if _o != _n:
                                    _dlc += 2 if _n is not None else 1
                                    _shown += 1
                                    if _shown >= 3:
                                        break
                            if len(_nl) > len(_ol):
                                _dlc += min(3 - _shown, len(_nl) - len(_ol))
                            _dlc += 1
                        elif d.get("is_new"):
                            _dlc += 2
                    content_lines[2] = _dlc
                max_scroll = max(0, content_lines[panel] - max(1, max_rows))
                if panel_scroll > max_scroll:
                    panel_scroll = max_scroll
                if panel == 0:
                    if not pts:
                        safe_addstr(content_y, 2, "(no file activity)", width - 3, curses.A_DIM)
                    else:
                        file_actions: dict[str, list[dict]] = {}
                        for t in pts:
                            file_actions.setdefault(t.get("file", "?"), []).append(t)
                        entries = list(file_actions.items())
                        visible = entries[panel_scroll:panel_scroll + max(1, max_rows)]
                        for fi, (f, actions) in enumerate(visible):
                            if content_y + fi >= height - 2:
                                break
                            proj = _infer_project(actions[0])
                            letter = project_letter.get(proj, "?")
                            reads = sum(1 for a in actions if a.get("action") == "read")
                            edits = sum(1 for a in actions if a.get("action") in ("edit", "write"))
                            lines_changed = sum(a.get("lines_changed", a.get("lines", 0)) for a in actions)
                            parts = []
                            if reads:
                                parts.append(f"{reads}r")
                            if edits:
                                parts.append(f"{edits}e")
                            if lines_changed:
                                parts.append(f"{lines_changed}L")
                            short = _viz_short(f, home)
                            if len(short) > width - 22:
                                short = _viz_trunc(short, width - 22)
                            detail = " ".join(parts)
                            attr = curses.color_pair(4) if edits else curses.A_NORMAL
                            safe_addstr(content_y + fi, 2, f"[{letter}] {short}  ({detail})", width - 3, attr)
                elif panel == 1:
                    parts = responses.get(idx, [])
                    if not parts:
                        safe_addstr(content_y, 2, "(no response in transcript)", width - 3, curses.A_DIM)
                    else:
                        wrap_width = width - 6
                        chat_rendered: list[tuple[str, int]] = []
                        for ptype, ptext in parts:
                            if ptype == "thinking":
                                chat_rendered.append(("-- thinking --", curses.color_pair(1)))
                                for raw_line in ptext.split("\n"):
                                    while len(raw_line) > wrap_width:
                                        chat_rendered.append((raw_line[:wrap_width], curses.color_pair(1)))
                                        raw_line = raw_line[wrap_width:]
                                    chat_rendered.append((raw_line, curses.color_pair(1)))
                                chat_rendered.append(("", curses.A_NORMAL))
                            elif ptype == "text":
                                for raw_line in ptext.split("\n"):
                                    while len(raw_line) > wrap_width:
                                        chat_rendered.append((raw_line[:wrap_width], curses.A_BOLD))
                                        raw_line = raw_line[wrap_width:]
                                    chat_rendered.append((raw_line, curses.A_BOLD))
                            elif ptype == "tool":
                                chat_rendered.append((f"  > {ptext}", curses.A_DIM))
                        visible = chat_rendered[panel_scroll:panel_scroll + max(1, max_rows)]
                        for li, (text, attr) in enumerate(visible):
                            if content_y + li >= height - 2:
                                break
                            safe_addstr(content_y + li, 2, text, width - 3, attr)
                elif panel == 2:
                    prompt_diffs = [d for d in diffs if d.get("prompt_idx") == idx]
                    if not prompt_diffs:
                        safe_addstr(content_y, 2, "(no diffs)", width - 3, curses.A_DIM)
                    else:
                        diff_lines: list[tuple[str, int]] = []
                        for d in prompt_diffs:
                            f = _viz_short(d.get("file", "?"), home)
                            fname = f.split("/")[-1]
                            if d.get("old_string") is not None:
                                old = d["old_string"]
                                new = d.get("new_string", "")
                                old_lc = old.count("\n") + 1
                                new_lc = new.count("\n") + 1
                                diff_lines.append((f"{fname} ({old_lc} -> {new_lc} lines):", curses.A_BOLD))
                                diff_lines.append((f"  {_diff_summary(old, new)}", curses.A_NORMAL))
                                old_ls = old.split("\n")
                                new_ls = new.split("\n")
                                shown = 0
                                for oi, ol in enumerate(old_ls):
                                    nl = new_ls[oi] if oi < len(new_ls) else None
                                    if ol != nl:
                                        diff_lines.append((f"  - {ol[:width-8]}", curses.color_pair(3)))
                                        if nl is not None:
                                            diff_lines.append((f"  + {nl[:width-8]}", curses.color_pair(4)))
                                        shown += 1
                                        if shown >= 3:
                                            break
                                if len(new_ls) > len(old_ls) and shown < 3:
                                    for nl in new_ls[len(old_ls):len(old_ls) + 3 - shown]:
                                        diff_lines.append((f"  + {nl[:width-8]}", curses.color_pair(4)))
                                diff_lines.append(("", curses.A_NORMAL))
                            elif d.get("is_new"):
                                diff_lines.append((f"{fname}: new file ({d.get('lines', '?')} lines)", curses.A_BOLD))
                                diff_lines.append(("", curses.A_NORMAL))
                        visible = diff_lines[panel_scroll:panel_scroll + max(1, max_rows)]
                        for li, (text, attr) in enumerate(visible):
                            if content_y + li >= height - 2:
                                break
                            safe_addstr(content_y + li, 2, text, width - 3, attr)

            if focus == 0:
                footer = " <-/-> prompts  dn> panels  v grid  c copy  q quit"
            elif focus == 1:
                if panel_has_content[panel]:
                    footer = " <-/-> panels  dn> content  esc> timeline  q quit"
                else:
                    footer = " <-/-> panels  esc> timeline  q quit"
            else:
                footer = " up/dn scroll  esc> timeline  q quit"
            safe_addstr(height - 1, 0, footer, width - 1, curses.A_DIM | curses.A_REVERSE)
            stdscr.refresh()

            key = stdscr.getch()
            if key == ord("q"):
                break
            elif key == ord("v"):
                view_mode = 1 - view_mode
            elif key == ord("c"):
                import subprocess
                ref = f"{ctx['session_id']}:{idx + 1}"
                try:
                    subprocess.run(["pbcopy"], input=ref.encode(), check=True, timeout=2)
                    safe_addstr(height - 1, 0, f" copied: {ref}", width - 1, curses.A_REVERSE)
                    stdscr.refresh()
                    curses.napms(500)
                except (subprocess.SubprocessError, FileNotFoundError):
                    pass
            elif key == curses.KEY_DOWN or key == ord("j"):
                if focus == 0:
                    focus = 1
                    panel_scroll = 0
                elif focus == 1:
                    if panel_has_content[panel]:
                        focus = 2
                        panel_scroll = 0
                else:
                    panel_scroll += 1
            elif key == 27 or key == ord("t"):
                if focus > 0:
                    focus = 0
                    panel = 0
                    panel_scroll = 0
            elif key == curses.KEY_UP or key == ord("k"):
                if focus == 2:
                    if panel_scroll > 0:
                        panel_scroll -= 1
                    else:
                        focus = 1
                elif focus == 1:
                    focus = 0
            elif key == curses.KEY_SRIGHT or key == ord("L"):
                if focus == 0:
                    cursor = min(cursor + 5, len(all_prompts) - 1)
                    panel_scroll = 0
            elif key == curses.KEY_SLEFT or key == ord("H"):
                if focus == 0:
                    cursor = max(cursor - 5, 0)
                    panel_scroll = 0
            elif key == curses.KEY_RIGHT or key == ord("l"):
                if focus == 0:
                    if cursor < len(all_prompts) - 1:
                        cursor += 1
                        panel_scroll = 0
                elif focus == 1:
                    panel = (panel + 1) % len(PANELS)
                    panel_scroll = 0
            elif key == curses.KEY_LEFT or key == ord("h"):
                if focus == 0:
                    if cursor > 0:
                        cursor -= 1
                        panel_scroll = 0
                elif focus == 1:
                    panel = (panel - 1) % len(PANELS)
                    panel_scroll = 0
            elif key == curses.KEY_HOME or key == ord("g"):
                cursor = 0
                panel_scroll = 0
                focus = 0
            elif key == curses.KEY_END or key == ord("G"):
                cursor = len(all_prompts) - 1
                panel_scroll = 0
                focus = 0

    _run_inner()
