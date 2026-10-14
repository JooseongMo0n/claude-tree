# claude-tree

[한국어](README.ko.md)

See the flow of a long [Claude Code](https://claude.com/claude-code) session at a glance.

A session that runs for days drifts through planning, digressions, dead ends and rewinds. When you come back, scrolling up to re-orient takes a while. `claude-tree` reads the session transcript, asks a small model (Haiku) to group it into units of work, and prints the result as a tree in your terminal.

```
❯ claude-tree

Claude Code 작업 흐름 트리와 메모리 개선  10/14 · 프롬프트 22개 · 17:03 기준
├─ ✔ tree 기능 설계 회의  10/14 14:38–15:02
│  ├─ ✔ rewind·fork와 history 범위 정리  14:38
│  └─ ✔ 표시 위치와 갱신 방식 결정  14:55
├─ ✔ claude-tree v1 구현과 웹 시도  10/14 15:04–15:26
│  ├─ ✔ claude-tree v1 구현  15:04
│  └─ ✗ 마인드맵 웹 화면 시도  15:20
├─ ✔ 터미널 tree 전환과 상태줄 정리  10/14 15:43–15:50
│  ├─ ✔ 웹 viewer 제거와 터미널 출력  15:43
│  └─ ✔ 상태줄 cache 표시와 순서 정리  15:48
├─ ✔ 세션 경로 NFC 불일치 수정  10/14 15:55
├─ ✔ tree 로딩 표시와 갱신 흐름 개선  10/14 15:58–16:56
│  ├─ ✔ 스피너와 이전 tree 유지 흐름  15:58
│  └─ ✔ 화면 지우기와 시간 표시 다듬기  16:07
└─ ● claude-tree 코드 위치와 공개 준비  10/14 17:00–17:01  ← 지금
```

`✔` done · `●` in progress · `✗` abandoned · `↩` discarded by rewind

> **Note:** titles, summaries and messages are currently in Korean. Making the language configurable is a welcome contribution.

## Features

- **Stays out of your session.** The main Claude Code session never knows about the tree. `claude-tree` only reads the transcript file, so it uses none of your session's context.
- **Units of work, not messages.** Several prompts that belong to one task become one node. A long autonomous turn is split into steps at Claude's progress messages.
- **Incremental.** Only prompts added since the last run are sent to Haiku. Finished phases are frozen; the latest phase is re-organized with new work.
- **Knows about rewinds.** Work you discarded with rewind (`Esc Esc`) is kept as `↩` when it was substantial and dropped when it was just an edited prompt. Compactions are followed transparently.

## Requirements

- [Claude Code](https://claude.com/claude-code) CLI, logged in. Haiku is called through `claude -p`, so it uses your existing Claude login (no API key needed) and counts toward your usage.
- Python 3.9+ (standard library only)
- macOS or Linux

## Install

```sh
git clone https://github.com/JooseongMo0n/claude-tree.git
ln -s "$PWD/claude-tree/bin/claude-tree" ~/.local/bin/claude-tree   # any directory on your PATH
```

Set `CLAUDE_TREE_PYTHON` if `python3` on your PATH is older than 3.9.

## Usage

Run it in a **separate terminal tab** from the project directory:

```sh
claude-tree              # tree of the most recent session in this directory
claude-tree -v           # also print a one-line summary per node
claude-tree --pick       # choose from recent sessions (🌳 = already has a tree)
claude-tree --session ID # a specific session
claude-tree off          # delete this session's cached tree
```

- The first run on a session takes a while (long sessions: 1–2 minutes). Later runs show the previous tree immediately, update it, and redraw.
- In a terminal, each run clears the screen (like `clear`) so repeated runs don't pile up.
- Don't run it from inside the main session with `!claude-tree`: the output would land in the conversation and use context.

## How it works

1. **Parse** `~/.claude/projects/<project>/<session>.jsonl`. Follow the live branch from the latest message back through `parentUuid` (and `logicalParentUuid` across compactions). Prompts not on that branch were discarded by rewind.
2. **Condense** each prompt into a few lines: the prompt, Claude's final reply, tools used, files edited. Long turns become several steps.
3. **Organize** with Haiku (`claude -p --model haiku --no-session-persistence --tools "" --json-schema …`, hooks disabled). Haiku returns a pre-order list of nodes with depth and start position. At most 60 units are sent per call.
4. **Cache** the result in `~/.claude-tree/<session>.json`. The last top-level phase stays provisional and is redone together with new prompts next time.

| File | Role |
|---|---|
| `claude_tree/transcript.py` | Transcript parsing, rewind detection, step splitting |
| `claude_tree/segment.py` | Haiku prompt, windowed organizing, node ranges |
| `claude_tree/store.py` | Cache, lock, update decisions |
| `claude_tree/cli.py` | Tree rendering, spinner, CLI |

## Caveats

- The transcript format is not a documented Claude Code API. If an update breaks parsing, `transcript.py` is the place to look.
- Excerpts of your conversation (prompts, replies, file names) are sent to Haiku through your own Claude Code login. Nothing else leaves your machine.

## Contributing

Issues and pull requests are welcome. Some ideas:

- Configurable output language
- `--watch` mode that redraws every few minutes
- Better granularity heuristics for very long sessions

## License

[MIT](LICENSE)
