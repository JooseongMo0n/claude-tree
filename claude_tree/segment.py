"""Haiku로 turn 목록을 작업 단위 tree로 정리한다.

tree는 pre-order 평탄 리스트로 저장한다: 각 노드는 depth와 start_turn을 가지며,
노드의 범위는 start_turn부터 '다음에 나오는 depth 이하 노드의 start_turn - 1'까지다.
마지막 depth-0 노드(와 그 하위)는 '임시'로 두고 다음 정리 때 다시 만든다.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .transcript import Turn

MODEL = "haiku"
WINDOW = 60          # 한 번에 Haiku에게 보내는 turn 수
PROMPT_CHARS = 500
REPLY_CHARS = 300
STATUSES = ("done", "abandoned", "rewound", "active")

SYSTEM_PROMPT = """You organize the history of one Claude Code session into a tree of work units, so the user can quickly re-orient after days away.

Input:
- OUTLINE SO FAR: nodes already finalized, in pre-order with depth. Context only; never repeat them.
- TURNS: the turns you must organize. Each turn is one user prompt and Claude's work on it.
  A long turn is split into STEP entries: Claude's own progress inside one user request (the step starts with Claude's progress message). Treat steps like turns; they can be separate nodes.
  Turns marked (REWOUND) were discarded by the user with rewind; they are not part of the live conversation.

Output the nodes that cover the given TURNS, in pre-order, continuing the outline.

Shape (most important): the result is drawn as a mind map (session -> phases -> tasks -> steps), so build a real hierarchy, not a flat list.
- depth 0 = a phase of the session (planning, building X, debugging Y...). Prefer a few phases with children over many flat phases.
- depth 1 = a task inside a phase. A phase usually has 2-6 tasks. A phase with only one task should have no children.
- depth 2 = a notable step inside a task, only when the task clearly has distinct steps.
- In a discussion or design phase, each distinct question or decision is its own task.
- A node is one unit of work you would state in a single line at a standup. Never one node per turn by default.
- A run of small tweaks, fixes and polish on the same feature is ONE node; mention the notable tweaks in its summary.
- Commits, confirmations ("ok", "진행해"), follow-ups and re-tries belong to the work they finish; never a node of their own.
- A digression (investigating a bug, a side question) that later returns to the main task is a child of that task.
- Make a "rewound" node only when a substantial line of work was discarded. A rewound turn that was simply edited and re-sent belongs to the surrounding node.
- Respect the node budget given in the input.
- A node with depth d attaches under the most recent preceding node with depth d-1, including nodes in OUTLINE SO FAR. So you may start at depth 1 or 2 to continue the last outline node.

Fields:
- start_turn: the first turn the node covers. Non-decreasing in output order. The first output node starts at the first given turn. A parent starts at or before its first child.
- title: Korean noun phrase, at most 25 characters, specific (name the concrete thing, not "작업 진행").
- summary: 1-2 Korean sentences in plain 했다체 (never 합니다체): what was done and the outcome or decision. Mention key files or conclusions.
- status: done | abandoned (tried, then given up or reverted by decision) | rewound (made of REWOUND turns) | active (only for the final node(s) when work seems still in progress).
- session_title: the overall topic of the whole session, Korean, at most 30 characters.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "session_title": {"type": "string"},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "depth": {"type": "integer", "minimum": 0, "maximum": 2},
                    "start_turn": {"type": "integer"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                },
                "required": ["depth", "start_turn", "title", "summary", "status"],
            },
        },
    },
    "required": ["session_title", "nodes"],
}


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render_turn(t: Turn) -> str:
    head = f"#{t.n} [{t.ts[:10]}]" + (" (REWOUND)" if t.rewound else "")
    if t.kind == "step":
        lines = [f"{head} STEP (same user request as before)", f"CLAUDE STARTS: {_clip(t.lead, 200)}"]
    else:
        lines = [head, f"USER: {_clip(t.prompt, PROMPT_CHARS)}"]
    if t.reply:
        lines.append(f"CLAUDE: {_clip(t.reply, REPLY_CHARS)}")
    if t.tools:
        lines.append("TOOLS: " + ", ".join(f"{k}×{v}" for k, v in t.tools.items()))
    if t.files:
        lines.append("EDITED: " + ", ".join(t.files[:8]))
    if t.recap:
        lines.append(f"RECAP: {_clip(t.recap, 200)}")
    return "\n".join(lines)


def render_outline(nodes: List[dict]) -> str:
    if not nodes:
        return "(empty)"
    return "\n".join(
        f"{'  ' * n['depth']}- [d{n['depth']} from #{n['start_turn']}] {n['title']}" for n in nodes
    )


def call_haiku(prompt: str, timeout: int = 240) -> dict:
    cmd = [
        "claude", "-p",
        "--model", MODEL,
        "--no-session-persistence",
        "--tools", "",
        "--strict-mcp-config",
        "--settings", json.dumps({"disableAllHooks": True}),
        "--system-prompt", SYSTEM_PROMPT,
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
    ]
    # 프로젝트 CLAUDE.md 등이 섞이지 않도록 중립 디렉토리에서 실행
    cwd = Path.home() / ".claude-tree"
    cwd.mkdir(exist_ok=True)
    out = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd
    )
    if out.returncode != 0:
        raise RuntimeError(f"claude -p 실패: {out.stderr.strip()[:500]}")
    res = json.loads(out.stdout)
    if res.get("is_error") or not isinstance(res.get("structured_output"), dict):
        raise RuntimeError(f"Haiku 응답 오류: {str(res.get('result'))[:500]}")
    return res["structured_output"]


def _sanitize(new: List[dict], first: int, last: int, prev_depth: int) -> List[dict]:
    """모델 출력이 규칙을 어겨도 tree가 깨지지 않게 보정한다."""
    out = []
    cur_start = first
    max_depth = prev_depth + 1
    for i, n in enumerate(new):
        depth = max(0, min(int(n.get("depth", 0)), 2, max_depth))
        start = int(n.get("start_turn", cur_start))
        start = first if i == 0 else min(max(start, cur_start), last)
        status = n.get("status") if n.get("status") in STATUSES else "done"
        out.append({
            "depth": depth,
            "start_turn": start,
            "title": (n.get("title") or "(제목 없음)").strip()[:40],
            "summary": (n.get("summary") or "").strip(),
            "status": status,
        })
        cur_start = start
        max_depth = depth + 1
    return out


def split_frozen(nodes: List[dict], boundary_cap: int) -> Tuple[List[dict], int]:
    """(고정할 노드들, 다시 정리할 첫 turn)을 돌려준다.

    기본: 마지막 depth-0 노드부터 임시. 그 구간이 너무 길면(boundary_cap 초과)
    마지막 노드 하나만 임시로 둬서 창이 무한히 커지지 않게 한다.
    """
    if not nodes:
        return [], 1
    last_top = max(i for i, n in enumerate(nodes) if n["depth"] == 0)
    if nodes[-1]["start_turn"] - nodes[last_top]["start_turn"] >= boundary_cap:
        last_top = len(nodes) - 1
    frozen = [dict(n) for n in nodes[:last_top]]
    for n in frozen:
        if n["status"] == "active":
            n["status"] = "done"
    return frozen, nodes[last_top]["start_turn"]


def organize(
    turns: List[Turn], nodes: List[dict], session_title: str, progress=None
) -> Tuple[List[dict], str]:
    """기존 tree(nodes)에 이어서 turns 전체가 덮이도록 정리한다."""
    if not turns:
        return nodes, session_title
    last_n = turns[-1].n
    prev_first, prev_end = 0, 0
    first_n = split_frozen(nodes, WINDOW // 2)[1]
    while True:
        frozen, start = split_frozen(nodes, WINDOW // 2)
        if start <= prev_first:
            # 한 노드가 창 전체를 덮어 진전이 없으면, 지금까지를 모두 고정하고 다음 turn부터
            frozen, start = split_frozen(nodes + [{"depth": 0, "start_turn": prev_end + 1}], 1 << 30)
        window = [t for t in turns if t.n >= start][:WINDOW]
        if not window:
            return nodes, session_title
        end = window[-1].n
        if progress:
            progress((window[0].n - first_n) / max(1, last_n - first_n + 1))
        shown = [t for t in window if not t.discarded_unanswered]
        lo, hi = max(3, len(shown) // 5), max(5, len(shown) // 2)
        prompt = (
            f"SESSION TITLE SO FAR: {session_title or '(none)'}\n\n"
            f"OUTLINE SO FAR:\n{render_outline(frozen)}\n\n"
            f"NODE BUDGET: about {lo}-{hi} nodes in total for these {len(shown)} turns.\n\n"
            f"TURNS #{window[0].n}–#{end}:\n\n"
            + "\n\n".join(render_turn(t) for t in shown)
        )
        res = call_haiku(prompt)
        prev_depth = frozen[-1]["depth"] if frozen else -1
        new = _sanitize(res.get("nodes") or [], window[0].n, end, prev_depth)
        if not new:
            new = [{"depth": 0, "start_turn": window[0].n,
                    "title": "(정리 실패)", "summary": "", "status": "done"}]
        nodes = frozen + new
        session_title = (res.get("session_title") or session_title).strip()
        if end >= last_n:
            return nodes, session_title
        prev_first, prev_end = window[0].n, end


def node_ranges(nodes: List[dict], last_turn: int) -> List[Tuple[int, int]]:
    ranges = []
    for i, n in enumerate(nodes):
        end = last_turn
        for m in nodes[i + 1:]:
            if m["depth"] <= n["depth"]:
                end = m["start_turn"] - 1
                break
        ranges.append((n["start_turn"], max(end, n["start_turn"])))
    return ranges


def active_path(nodes: List[dict]) -> List[dict]:
    """마지막 노드에서 루트까지의 경로 (breadcrumb용)."""
    path: List[Optional[dict]] = [None, None, None]
    for n in nodes:
        path[n["depth"]] = n
        for d in range(n["depth"] + 1, 3):
            path[d] = None
    return [n for n in path if n]
