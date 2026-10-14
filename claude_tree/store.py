"""캐시(~/.claude-tree/<session>.json), 잠금, 갱신 흐름."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from . import segment
from .transcript import file_stat, load_session

HOME = Path.home() / ".claude-tree"
LOCK_STALE = 15 * 60
CACHE_VERSION = 2       # 정리 단위나 형식이 바뀌면 올린다 → 기존 캐시는 처음부터 다시 정리


def cache_path(sid: str) -> Path:
    return HOME / f"{sid}.json"


def load_cache(sid: str) -> Optional[dict]:
    try:
        return json.loads(cache_path(sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_cache(sid: str, data: dict) -> None:
    HOME.mkdir(exist_ok=True)
    tmp = cache_path(sid).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, cache_path(sid))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


class PidFile:
    """같은 세션을 두 곳에서 동시에 정리하지 않게 한다."""

    def __init__(self, sid: str, kind: str):
        self.path = HOME / f"{sid}.{kind}"

    def held(self) -> bool:
        try:
            pid = int(self.path.read_text())
            fresh = time.time() - self.path.stat().st_mtime < LOCK_STALE
            return _pid_alive(pid) and fresh
        except (OSError, ValueError):
            return False

    def acquire(self) -> bool:
        HOME.mkdir(exist_ok=True)
        if self.path.exists() and not self.held():
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


def _first_changed_turn(cache: dict, turns) -> Optional[int]:
    """이미 정리한 turn 중 rewind 등으로 상태가 바뀐 첫 turn 번호."""
    old_uuids = cache.get("turn_uuids") or []
    old_rewound = set(cache.get("rewound_uuids") or [])
    for i, u in enumerate(old_uuids):
        if i >= len(turns) or turns[i].uuid != u:
            return i + 1
        if turns[i].rewound != (u in old_rewound):
            return turns[i].n
    return None


def _cut_from(nodes, turn_n: int):
    """turn_n을 포함하는 depth-0 노드부터 뒤를 버린다(다시 정리하도록)."""
    cut = len(nodes)
    for i, n in enumerate(nodes):
        if n["depth"] == 0 and n["start_turn"] <= turn_n:
            cut = i
    return nodes[:cut]


def _plan(transcript: Path):
    """지금 transcript와 캐시를 비교해 (세션, 이어서 정리할 노드, 제목, 정리 필요 여부)를 돌려준다."""
    sess = load_session(transcript)
    turns = sess.turns
    cache = load_cache(transcript.stem) or {}
    if cache.get("version") != CACHE_VERSION:
        cache = {}  # 처음이거나 정리 방식이 바뀜 → 처음부터
    nodes = cache.get("nodes") or []
    title = cache.get("session_title") or sess.ai_title
    changed = _first_changed_turn(cache, turns)
    if changed is not None:
        nodes = _cut_from(nodes, changed)  # rewind로 바뀐 구간부터 다시
    stale = not nodes or changed is not None or len(turns) > len(cache.get("turn_uuids") or [])
    return sess, cache, nodes, title, stale


def new_prompt_count(transcript: Path) -> int:
    """아직 tree에 반영되지 않은 사용자 프롬프트 수. 0이면 최신."""
    cache = load_cache(transcript.stem)
    if cache and cache.get("version") == CACHE_VERSION and cache.get("size") == file_stat(transcript)[0]:
        return 0  # 파일이 그대로면 파싱할 필요 없음
    sess, cache, _, _, stale = _plan(transcript)
    if not stale:
        return 0
    done = set(cache.get("turn_uuids") or [])
    return max(1, sum(1 for t in sess.turns if t.kind == "prompt" and t.uuid not in done))


def update(transcript: Path, progress=None) -> str:
    """progress(fraction)는 여러 구간을 정리할 때 진행률(0~1)을 받는다."""
    sid = transcript.stem
    lock = PidFile(sid, "lock")
    if not lock.acquire():
        return "busy"
    try:
        size, mtime = file_stat(transcript)
        sess, _, nodes, title, stale = _plan(transcript)
        turns = sess.turns
        if stale:
            nodes, title = segment.organize(turns, nodes, title, progress=progress)

        save_cache(sid, {
            "version": CACHE_VERSION,
            "session_id": sid,
            "transcript": str(transcript),
            "cwd": sess.cwd,
            "ai_title": sess.ai_title,
            "session_title": title,
            "nodes": nodes,
            "turn_uuids": [t.uuid for t in turns],
            "rewound_uuids": [t.uuid for t in turns if t.rewound],
            "size": size,
            "mtime": mtime,
            "updated_at": time.time(),
        })
        return "updated" if stale else "unchanged"
    finally:
        lock.release()
