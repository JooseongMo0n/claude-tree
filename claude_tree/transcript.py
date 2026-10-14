"""Claude Code 세션 transcript(.jsonl)를 읽어 turn 목록으로 압축한다.

turn = 사용자가 직접 입력한 프롬프트 하나 + 그 뒤 Claude의 응답/도구 사용.
rewind로 버려진 가지의 프롬프트도 시간순 위치에 rewound=True로 포함한다.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

PROJECTS_DIR = Path.home() / ".claude" / "projects"

TREE_TYPES = {"user", "assistant", "attachment", "system"}
NOISE_PREFIXES = (
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<task-notification>",
    "[Request interrupted",
    "<bash-",
)
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
PASTE_RE = re.compile(r"</?pasted_content[^>]*>")
SLASH_RE = re.compile(r"^/[\w:-]+(\s[^\n]{0,80})?$")  # 명령어 한 줄 입력(/compact 등)
FILE_KEYS = ("file_path", "notebook_path")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# 긴 turn을 단계로 나누는 기준: 도구를 이만큼 이상 쓴 turn만 나누고,
# 한 단계가 도구를 STEP_MIN_TOOLS번 이상 쓴 뒤 Claude가 진행 메시지를 남기면 새 단계로 본다.
SPLIT_MIN_TOOLS = 8
STEP_MIN_TOOLS = 2
STEP_MIN_CHARS = 15


@dataclass
class Turn:
    """정리의 최소 단위. 보통 프롬프트 하나(kind="prompt")이고,
    긴 turn은 Claude의 진행 메시지마다 단계(kind="step")로 나뉜다."""
    n: int
    uuid: str
    ts: str
    prompt: str
    rewound: bool = False
    kind: str = "prompt"
    turn: int = 0       # 몇 번째 사용자 프롬프트에 속하는지
    lead: str = ""      # 단계의 시작이 된 Claude의 진행 메시지
    reply: str = ""
    tools: Dict[str, int] = field(default_factory=dict)
    files: List[str] = field(default_factory=list)
    recap: str = ""  # Claude Code가 남긴 away_summary가 있으면

    @property
    def discarded_unanswered(self) -> bool:
        """rewind로 버려졌고 Claude가 아무 작업도 안 한 turn (고쳐서 다시 보낸 프롬프트 등)."""
        return self.kind == "prompt" and self.rewound and not self.reply and not self.tools

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Session:
    session_id: str
    path: Path
    ai_title: str
    cwd: str
    turns: List[Turn]


def project_dir_for(cwd: str) -> Path:
    # macOS는 한글 경로를 NFD(자모 분리)로 돌려줄 수 있지만 Claude Code는 NFC 기준으로 폴더를 만든다
    for form in ("NFC", "NFD"):
        d = PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", unicodedata.normalize(form, cwd))
        if d.is_dir():
            return d
    return d


def list_sessions(cwd: str) -> List[Path]:
    d = project_dir_for(cwd)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def find_session(session_id: str) -> Optional[Path]:
    hits = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _text_of(content) -> Optional[str]:
    """사용자가 입력한 텍스트만 꺼낸다. tool_result면 None."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_result":
            return None
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") == "image":
            texts.append("[이미지]")
    return "\n".join(texts) if texts else None


def _clean(text: str) -> str:
    text = REMINDER_RE.sub("", text)
    text = PASTE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _is_real_prompt(rec: dict) -> Optional[str]:
    if rec.get("type") != "user" or rec.get("isSidechain"):
        return None
    if rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    text = _text_of((rec.get("message") or {}).get("content"))
    if text is None:
        return None
    text = _clean(text)
    if not text or text.startswith(NOISE_PREFIXES) or SLASH_RE.match(text):
        return None
    return text


def _short_path(p: str, cwd: str) -> str:
    if cwd and p.startswith(cwd.rstrip("/") + "/"):
        return p[len(cwd.rstrip("/")) + 1:]
    return p.replace(str(Path.home()), "~")


def load_session(path: Path) -> Session:
    records: Dict[str, dict] = {}
    order: List[str] = []
    ai_title = ""
    cwd = ""
    leaf = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = rec.get("type")
            if t == "ai-title":
                ai_title = rec.get("aiTitle") or ai_title
                continue
            if t not in TREE_TYPES or not rec.get("uuid") or rec.get("isSidechain"):
                continue
            records[rec["uuid"]] = rec
            order.append(rec["uuid"])
            cwd = rec.get("cwd") or cwd
            if t in ("user", "assistant"):
                leaf = rec["uuid"]

    def parent(u: str) -> Optional[str]:
        r = records.get(u) or {}
        return r.get("parentUuid") or r.get("logicalParentUuid")  # compaction 경계를 넘어 연결

    # 현재 살아 있는 가지 = 파일에서 마지막 메시지부터 부모를 따라 올라간 경로
    active = set()
    u = leaf
    while u and u not in active:
        active.add(u)
        u = parent(u)

    # 모든 실제 프롬프트(살아 있는 가지 + 버려진 가지)를 시간순으로
    prompts = []
    for u in order:
        text = _is_real_prompt(records[u])
        if text is not None:
            prompts.append((records[u].get("timestamp", ""), u, text))
    prompts.sort(key=lambda x: x[0])
    turn_of: Dict[str, Turn] = {}
    heads: List[Turn] = []
    for i, (ts, u, text) in enumerate(prompts, 1):
        t = Turn(n=0, uuid=u, ts=ts, prompt=text, rewound=u not in active, turn=i)
        heads.append(t)
        turn_of[u] = t

    # 각 기록을 가장 가까운 조상 프롬프트(turn)에 귀속
    owner_cache: Dict[str, Optional[Turn]] = {}

    def owner(u: str) -> Optional[Turn]:
        chain = []
        cur: Optional[str] = u
        found = None
        while cur:
            if cur in owner_cache:
                found = owner_cache[cur]
                break
            if cur in turn_of:
                found = turn_of[cur]
                break
            chain.append(cur)
            cur = parent(cur)
            if len(chain) > 100000:
                break
        for c in chain:
            owner_cache[c] = found
        return found

    # turn마다 Claude의 활동을 순서대로 모은다: ("text", 글, rec) / ("tool", 이름, 파일들, rec)
    events: Dict[str, list] = {t.uuid: [] for t in heads}
    for u in order:
        rec = records[u]
        t = owner(u)
        if t is None:
            continue
        if rec["type"] == "system" and rec.get("subtype") == "away_summary":
            c = rec.get("content")
            t.recap = c if isinstance(c, str) else t.recap
        if rec["type"] != "assistant":
            continue
        for b in (rec.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                events[t.uuid].append(("text", b["text"].strip(), [], rec))
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                files = []
                if b.get("name") in EDIT_TOOLS:
                    for k in FILE_KEYS:
                        if isinstance(inp.get(k), str):
                            files.append(_short_path(inp[k], rec.get("cwd") or cwd))
                events[t.uuid].append(("tool", b.get("name", "?"), files, rec))

    turns: List[Turn] = []
    for head in heads:
        turns.extend(_split_steps(head, events[head.uuid]))
    for i, t in enumerate(turns, 1):
        t.n = i

    return Session(
        session_id=path.stem,
        path=path,
        ai_title=ai_title,
        cwd=cwd,
        turns=turns,
    )


def _split_steps(head: Turn, evs: list) -> List[Turn]:
    """turn 하나를 단계들로 나눈다. 짧은 turn은 그대로 하나."""
    n_tools = sum(1 for e in evs if e[0] == "tool")
    can_split = not head.rewound and n_tools >= SPLIT_MIN_TOOLS
    last_tool = max((i for i, e in enumerate(evs) if e[0] == "tool"), default=-1)
    steps = [head]
    used = 0  # 현재 단계에서 쓴 도구 수
    for i, (kind, val, files, rec) in enumerate(evs):
        cur = steps[-1]
        if kind == "text":
            if (can_split and i < last_tool and used >= STEP_MIN_TOOLS
                    and len(val) >= STEP_MIN_CHARS):
                steps.append(Turn(
                    n=0, uuid=rec["uuid"], ts=rec.get("timestamp", head.ts), prompt="",
                    kind="step", turn=head.turn, lead=val,
                ))
                used = 0
            else:
                cur.reply = val  # 단계의 마지막 텍스트 = 그 단계의 결과
        else:
            used += 1
            cur.tools[val] = cur.tools.get(val, 0) + 1
            for f in files:
                if f not in cur.files:
                    cur.files.append(f)
    return steps


def file_stat(path: Path):
    st = os.stat(path)
    return st.st_size, st.st_mtime
