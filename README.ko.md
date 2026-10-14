# claude-tree

[English](README.md)

긴 [Claude Code](https://claude.com/claude-code) 세션의 작업 흐름을 한눈에 보여 주는 도구예요.

며칠씩 이어지는 세션은 계획, 곁가지, 막다른 시도, rewind가 뒤섞여요. 오랜만에 돌아오면 위로 스크롤하며 흐름을 다시 파악하는 데 시간이 꽤 걸려요. `claude-tree`는 세션 기록을 읽고 작은 모델(Haiku)에게 작업 단위로 묶게 한 뒤, 결과를 터미널에 tree로 출력해요.

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

`✔` 완료 · `●` 진행 중 · `✗` 포기·보류 · `↩` rewind로 폐기

## 특징

- **세션에 끼어들지 않아요.** 메인 Claude Code 세션은 tree의 존재를 몰라요. `claude-tree`는 기록 파일을 읽기만 해서 세션의 context를 전혀 쓰지 않아요.
- **메시지가 아니라 작업 단위로 묶어요.** 한 작업에 속한 여러 프롬프트는 노드 하나가 돼요. Claude가 혼자 오래 일한 turn은 중간 진행 메시지마다 단계로 나눠요.
- **새로 생긴 부분만 정리해요.** 마지막 실행 이후 추가된 프롬프트만 Haiku에게 보내요. 끝난 단계는 고정하고, 마지막 단계만 새 작업과 함께 다시 정리해요.
- **rewind를 이해해요.** rewind(`Esc Esc`)로 버린 작업이 의미 있는 분량이면 `↩`로 남기고, 단순히 고쳐서 다시 보낸 프롬프트면 빼요. compaction 전후도 이어서 읽어요.

## 필요한 것

- 로그인된 [Claude Code](https://claude.com/claude-code) CLI. Haiku는 `claude -p`로 호출해서 지금 쓰는 Claude 로그인을 그대로 써요. API 키는 필요 없고, 사용량에는 포함돼요.
- Python 3.9 이상 (표준 라이브러리만 사용)
- macOS 또는 Linux

## 설치

```sh
git clone https://github.com/JooseongMo0n/claude-tree.git
ln -s "$PWD/claude-tree/bin/claude-tree" ~/.local/bin/claude-tree   # PATH에 있는 아무 폴더
```

PATH의 `python3`가 3.9보다 오래됐다면 `CLAUDE_TREE_PYTHON`에 다른 파이썬 경로를 지정하세요.

## 사용법

프로젝트 폴더에서, 메인 세션과 **다른 터미널 탭**에서 실행하세요.

```sh
claude-tree              # 이 폴더의 가장 최근 세션
claude-tree -v           # 노드마다 한 줄 요약도 출력
claude-tree --pick       # 최근 세션 목록에서 고르기 (🌳 = 이미 tree가 있음)
claude-tree --session ID # 특정 세션
claude-tree off          # 이 세션의 tree 캐시 삭제
```

- 세션을 처음 정리할 때는 시간이 걸려요(긴 세션은 1~2분). 그다음부터는 이전 tree를 바로 보여 주고, 새 대화를 정리한 뒤 다시 그려요.
- 터미널에서는 실행할 때마다 화면을 지워서(`clear`와 같음) 반복 실행해도 출력이 쌓이지 않아요.
- 메인 세션 안에서 `!claude-tree`로 실행하지 마세요. 출력이 대화에 들어가 context를 차지해요.

## 동작 방식

1. **읽기:** `~/.claude/projects/<프로젝트>/<세션>.jsonl`을 읽어요. 가장 최근 메시지에서 `parentUuid`를 따라 올라가며 현재 가지를 찾고, compaction 경계는 `logicalParentUuid`로 이어요. 이 가지에 없는 프롬프트는 rewind로 버려진 거예요.
2. **압축:** 프롬프트마다 프롬프트 내용, Claude의 최종 응답, 사용한 도구, 수정한 파일을 몇 줄로 줄여요. 긴 turn은 여러 단계로 나눠요.
3. **정리:** Haiku를 호출해요(`claude -p --model haiku --no-session-persistence --tools "" --json-schema …`, hook 끔). Haiku는 depth와 시작 위치가 붙은 노드 목록을 돌려줘요. 한 번에 최대 60단위를 보내요.
4. **캐시:** 결과를 `~/.claude-tree/<세션>.json`에 저장해요. 마지막 큰 단계는 임시로 두고, 다음 실행 때 새 프롬프트와 함께 다시 정리해요.

| 파일 | 역할 |
|---|---|
| `claude_tree/transcript.py` | 기록 파싱, rewind 감지, 단계 나누기 |
| `claude_tree/segment.py` | Haiku 프롬프트, 구간별 정리, 노드 범위 계산 |
| `claude_tree/store.py` | 캐시, 잠금, 갱신 판단 |
| `claude_tree/cli.py` | tree 출력, 스피너, 명령어 |

## 주의할 점

- 세션 기록 형식은 Claude Code의 공식 문서화된 API가 아니에요. 업데이트 후 파싱이 깨지면 `transcript.py`부터 확인하세요.
- 대화 일부(프롬프트, 응답, 파일 이름)가 본인의 Claude Code 로그인을 통해 Haiku로 전송돼요. 그 밖에는 아무것도 밖으로 나가지 않아요.

## 기여

이슈와 PR 모두 환영해요. 이런 아이디어가 있어요.

- 출력 언어 설정
- 몇 분마다 다시 그리는 `--watch` 모드
- 아주 긴 세션을 위한 노드 크기 기준 개선

## 라이선스

[MIT](LICENSE)
