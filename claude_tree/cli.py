"""claude-tree: 세션 작업 흐름을 터미널에 tree로 출력한다.

  claude-tree                 현재 폴더의 가장 최근 세션
  claude-tree -v              노드마다 한 줄 요약도 출력
  claude-tree --pick          세션 목록에서 골라서
  claude-tree --session ID    특정 세션
  claude-tree off             이 세션의 tree 캐시 삭제
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path
from typing import List

from . import segment, store
from .transcript import find_session, list_sessions, load_session

ICON = {"done": "✔", "abandoned": "✗", "rewound": "↩", "active": "●"}
COLOR = {"done": "32", "abandoned": "31", "rewound": "35", "active": "33"}
DIM, RESET = "\033[2m", "\033[0m"


def _local(ts: str):
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def _span(start: str, end: str) -> str:
    """10/14 14:30–16:05, 날짜가 다르면 10/12 14:30–10/14 09:12"""
    s, e = _local(start), _local(end)
    if not s:
        return ""
    head = s.strftime("%m/%d %H:%M")
    if not e or e.strftime("%m/%d %H:%M") == head:
        return head
    return head + "–" + (e.strftime("%H:%M") if e.date() == s.date() else e.strftime("%m/%d %H:%M"))


def _start(ts: str, ref: str) -> str:
    """하위 작업의 시작 시각. 부모와 날짜가 다를 때만 날짜를 붙인다."""
    s, r = _local(ts), _local(ref)
    if not s:
        return ""
    return s.strftime("%H:%M") if r and s.date() == r.date() else s.strftime("%m/%d %H:%M")


def _dates(start: str, end: str) -> str:
    s, e = _local(start), _local(end)
    if not s:
        return ""
    if not e or e.date() == s.date():
        return s.strftime("%m/%d")
    return f"{s:%m/%d}–{e:%m/%d}"


def _pick_session(args) -> Path:
    if args.session:
        p = find_session(args.session)
        if not p:
            sys.exit(f"세션을 찾을 수 없어요: {args.session}")
        return p
    sessions = list_sessions(os.getcwd())
    if not sessions:
        sys.exit(f"이 폴더({os.getcwd()})의 Claude Code 세션이 없어요.")
    if not args.pick:
        return sessions[0]
    shown = sessions[:15]
    for i, p in enumerate(shown, 1):
        cache = store.load_cache(p.stem)
        title = (cache or {}).get("session_title") or _peek_title(p)
        mark = "🌳" if cache else "  "
        when = dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%m/%d %H:%M")
        print(f"{i:2}. {mark} {when}  {title[:50]}")
    try:
        k = int(input("번호: "))
        return shown[k - 1]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        sys.exit("취소했어요.")


def _peek_title(p: Path) -> str:
    title = ""
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                if '"ai-title"' in line:
                    title = json.loads(line).get("aiTitle") or title
    except (OSError, ValueError):
        pass
    return title or p.stem[:8]


def render_tree(sid: str, verbose: bool = False, note: str = "") -> List[str]:
    """출력할 줄 목록. note가 있으면 제목 줄 옆에 흐리게 붙인다."""
    cache = store.load_cache(sid)
    if not cache or not cache.get("nodes"):
        return []
    sess = load_session(Path(cache["transcript"]))
    turns = {t.n: t for t in sess.turns}
    nodes = cache["nodes"]
    ranges = segment.node_ranges(nodes, len(sess.turns))
    current = segment.active_path(nodes)[-1:]
    prompts = len({t.turn for t in sess.turns})

    first, last_ts = sess.turns[0].ts, sess.turns[-1].ts
    now = dt.datetime.now().strftime("%H:%M")
    lines = ["", f"\033[1m{cache['session_title']}\033[0m  "
                 f"{DIM}{_dates(first, last_ts)} · 프롬프트 {prompts}개 · {now} 기준{RESET}"]
    if note:
        lines.append(f"{DIM}{note}{RESET}")

    # 각 노드가 같은 depth의 마지막 형제인지 → 연결선(├─ / └─) 결정
    def is_last(i: int) -> bool:
        for m in nodes[i + 1:]:
            if m["depth"] < nodes[i]["depth"]:
                return True
            if m["depth"] == nodes[i]["depth"]:
                return False
        return True

    open_levels = []  # depth별로 아래에 형제가 더 있는지
    parent_ts = [""] * 3  # depth별 가장 최근 노드의 시작 시각
    for i, (n, (a, b)) in enumerate(zip(nodes, ranges)):
        d, last = n["depth"], is_last(i)
        del open_levels[d:]
        indent = "".join("│  " if more else "   " for more in open_levels)
        branch = "└─ " if last else "├─ "
        open_levels.append(not last)

        st = n["status"]
        title = n["title"]
        if st in ("rewound", "abandoned"):
            title = f"{DIM}{title}{RESET}"
        line = f"{DIM}{indent}{branch}{RESET}\033[{COLOR[st]}m{ICON[st]}{RESET} {title}"
        ts_a = turns[a].ts if a in turns else ""
        parent_ts[d] = ts_a
        if d == 0:
            line += f"  {DIM}{_span(ts_a, turns[b].ts if b in turns else '')}{RESET}"
        else:
            line += f"  {DIM}{_start(ts_a, parent_ts[d - 1])}{RESET}"
        if current and n is current[0]:
            line += "  \033[33m← 지금\033[0m"
        lines.append(line)
        if verbose and n["summary"]:
            has_kids = i + 1 < len(nodes) and nodes[i + 1]["depth"] > d
            cont = indent + ("   " if last else "│  ") + ("│  " if has_kids else "   ")
            lines.append(f"{DIM}{cont}{n['summary']}{RESET}")
    return lines


def _clear() -> None:
    """이전 출력을 지우고(터미널에서만, clear 명령과 같음) 실행한 명령은 다시 보여 준다."""
    if sys.stdout.isatty():
        cmd = " ".join(["claude-tree"] + [shlex.quote(a) for a in sys.argv[1:]])
        sys.stdout.write(f"\033[H\033[2J\033[3J\033[32m❯\033[0m {cmd}\n")
        sys.stdout.flush()


class Spinner:
    """터미널에서만 도는 로딩 표시. 끝나면 자기 줄을 지운다."""
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str):
        self.label, self.extra = label, ""
        self.tty = sys.stdout.isatty()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        start, i = time.time(), 0
        while not self._stop.wait(0.1):
            sec = int(time.time() - start)
            sys.stdout.write(f"\r\033[K\033[36m{self.FRAMES[i % 10]}\033[0m {self.label}{self.extra} {DIM}{sec}s{RESET}")
            sys.stdout.flush()
            i += 1

    def progress(self, fraction: float):
        self.extra = f" {int(fraction * 100)}%" if fraction > 0 else ""

    def __enter__(self):
        if self.tty:
            sys.stdout.write("\033[?25l")  # 커서 숨김
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self.tty:
            self._thread.join()
            sys.stdout.write("\r\033[K\033[?25h")
            sys.stdout.flush()


def show(transcript: Path, verbose: bool) -> None:
    sid = transcript.stem
    pending = store.new_prompt_count(transcript)
    old = render_tree(sid, verbose)
    _clear()
    if not pending:
        print("\n".join(old) if old else "아직 정리할 대화가 없어요.")
        return

    # 이전 tree가 있으면 먼저 보여 주고, 아래에서 갱신
    if old:
        old = render_tree(sid, verbose, note=f"이전 정리예요 · 새 프롬프트 {pending}개 반영 전")
        print("\n".join(old))
        print()  # tree와 로딩 사이 한 줄 띄움
        label = f"새 프롬프트 {pending}개를 정리하는 중…"
    else:
        label = "처음 정리하는 중… (긴 세션은 1~2분 걸려요)"

    result, error = "", ""
    with Spinner(label) as sp:
        try:
            result = store.update(transcript, progress=sp.progress)
            if result == "busy":
                sp.label = "다른 탭에서 정리 중이에요. 끝나기를 기다리는 중…"
                lock = store.PidFile(sid, "lock")
                for _ in range(600):
                    if not lock.held():
                        break
                    time.sleep(0.5)
        except Exception as e:  # Haiku 호출 실패 등: 이전 tree는 그대로 둔다
            error = str(e).splitlines()[0][:200] if str(e) else type(e).__name__

    if error:
        print(f"\033[31m정리하지 못했어요:\033[0m {error}")
        return
    new = render_tree(sid, verbose)
    if not new:
        print("tree를 만들지 못했어요.")
        return
    _clear()
    print("\n".join(new))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="claude-tree", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="show",
                    choices=["show", "off"], help=argparse.SUPPRESS)
    ap.add_argument("sid", nargs="?", help=argparse.SUPPRESS)
    ap.add_argument("--session", help="세션 ID")
    ap.add_argument("--pick", action="store_true", help="세션 목록에서 고르기")
    ap.add_argument("-v", "--verbose", action="store_true", help="노드 요약도 출력")
    args = ap.parse_args(argv)

    if args.sid and not args.session:
        args.session = args.sid
    transcript = _pick_session(args)

    if args.command == "off":
        store.cache_path(transcript.stem).unlink(missing_ok=True)
        print(f"tree 캐시를 지웠어요: {transcript.stem}")
        return
    try:
        show(transcript, args.verbose)
    except KeyboardInterrupt:
        sys.stdout.write("\033[?25h\n")
