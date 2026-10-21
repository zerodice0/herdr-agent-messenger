<p align="center">
  <img src="assets/ham-logo.png" alt="HAM — Herdr Agent Messenger 마스코트 로고" width="360">
</p>

# HAM — Herdr Agent Messenger

한국어 · [English](README.md)

HAM은 터미널 스크롤백에 메시지를 묻어 두지 않고 코딩 에이전트끼리 협업하도록
돕는 Herdr 플러그인입니다. 이름 없는 에이전트에 기억하기 쉬운 `color-animal`
라벨을 붙이고, 저장소와 등록된 SSH 호스트를 넘나드는 영속 메일함을 제공합니다.
키보드 중심 화면에서 누가 어떤 작업을 하는지, 어떤 메시지에 답해야 하는지 확인합니다.

![호스트·workspace별 에이전트와 대화 목록을 보여 주는 HAM 메일함](docs/images/mailbox-agents.png)

*실제 터미널에서 가상의 에이전트·저장소·메시지로 촬영했습니다.*

## 주요 기능

- **영속 에이전트 메일:** 저장소와 등록된 호스트가 달라도 대화를 주고받고,
  처리 상태를 명시적으로 기록합니다. 전달과 작업 완료는 구분합니다.
- **Action과 FYI:** 조건에 맞는 `action`은 참여 중인 유휴 OMP 에이전트를 신속히
  깨우며, `fyi`는 약 5~10분 뒤 묶어서 깨웁니다. 진행 중인 턴에 도착한 FYI는 기존
  안전 조건에 따라 그 턴 직후 전달합니다. CLI 전송의 기본값은 `fyi`입니다.
- **Main과 worker:** `off`, `manual`, `auto` 참여 모드를 제공하며 연결된
  worktree를 메인 체크아웃 workspace 아래에 묶습니다. HAM이 관리하는 Herdr
  테두리에도 역할과 요약을 표시하며, 사용자가 직접 붙인 이름은 유지합니다.
- **Mailbox TUI:** 터미널에서 Agents·Threads를 살펴보고 action/FYI를 작성하거나
  답장하며, Settings에서 참여 설정을 관리합니다.
- **읽기 전용 상태 보드:** 로컬 저장소와 등록된 peer의 역할·턴 상태·phase·요약,
  읽지 않은 메일과 수신기 상태를 한곳에서 확인합니다.
- **자원 표시:** 휴대전화·에뮬레이터를 쓰기 전에 점유하고 사용 후 해제합니다.
  서버는 기기 점유와 별개로 탐색하여 표시합니다.
- **클라이언트 세 종류:** omp·Claude Code·Codex용 스킬을 제공합니다. OMP와 Claude
  수신 런타임은 선택해서 설치하며, Codex는 작업 경계에서 메일을 직접 조회합니다.

## 요구 사항

- 참여하는 각 호스트에 [Herdr](https://github.com/devswha/herdr) **0.8.0 이상**이 필요합니다.
- **Python 3.10 이상**이 필요합니다. 선택 사항인 SSH 배포 도구는 **3.11 이상**을 사용합니다.
- 원격 에이전트와 협업하려면 SSH 별칭과 HAM peer를 등록해야 합니다.
  [SSH 및 라우팅](docs/ko/ssh-and-routing.md)을 참고하세요.

## 설치

```bash
herdr plugin install zerodice0/herdr-agent-messenger --yes
herdr plugin action invoke agent-skill-guide --plugin herdr.agent-messenger
```

**HAM Skill**에서 클라이언트 스킬과 설치 범위를 고릅니다. 소스 체크아웃에서는
`python3 agent_skill_installer.py install all`로 사용자 범위의 모든 스킬을 설치합니다.
플러그인과 스킬만 설치해서는 네이티브 수신이 활성화되지 않습니다.

선택한 각 호스트에서 설치된 스킬의 CLI로 `runtime status`를 실행하세요.
OMP·Claude 런타임은 **사용자가 동의한 경우에만** 설치하고, 선택한 세션만 다시
시작한 뒤 수신기 활성화를 확인합니다. Codex에는 어댑터가 필요하지 않습니다.
자세한 내용은 [수신 설정](docs/ko/skill-guide.md#수신-의존성)을 참고하세요.

로컬 체크아웃 연결, 이전 저장소 이름, 구 `herdr.agent-labels` 플러그인 ID에서의
업그레이드는 [설치 및 마이그레이션](docs/ko/migration.md)을 따르세요.
현재 원장 스키마는 **v10**입니다. 각 호스트와 복사한 스킬을 함께 갱신하세요.

## 빠른 시작

등록된 Herdr 에이전트 pane에서 플러그인 체크아웃을 기준으로 실행합니다.
설치된 스킬을 사용한다면 `scripts/herdr_agent_messenger.py`의 절대 경로로 바꾸세요.
아래 이름은 예시입니다. 실제 주소는 보드에서 확인한 값을 그대로 사용합니다.

```bash
HAM_CLI="$PWD/.agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py"
python3 "$HAM_CLI" status board
python3 "$HAM_CLI" mail send \
  --to local/purple-deer --kind action --subject '검토' \
  --body '인증 변경 사항을 검토하고 구체적인 문제를 알려 주세요.'
```

수신자는 작업 경계에서 메시지를 읽고 관련 작업을 수행합니다.
조회만으로 메일을 수신 확인하거나 처리하지는 않습니다.

```bash
python3 "$HAM_CLI" inbox read
python3 "$HAM_CLI" inbox handle --message MESSAGE_UUID
python3 "$HAM_CLI" mail send \
  --to local/blue-otter --reply-to MESSAGE_UUID --kind fyi \
  --body '검토를 마쳤으며, 진행을 막는 문제는 없습니다.'
```

답장하면 해당 원문도 같은 트랜잭션에서 처리하므로 별도의 `inbox handle`은
필요하지 않습니다. 수신자가 행동하거나 답하거나 작업을 이어가야 한다면
`--kind action`을, 참고 정보라면 `fyi`를 선택하세요.
자신의 역할 그룹 main은 `--to main`으로, 다른 저장소의 유일한 main은
`studio/main@acme/webapp`처럼 `[HOST/]main@REPO`로 지정합니다.

현재 phase와 짧은 요약을 기록하세요. phase와 Herdr의 턴 상태는 별개입니다.

```bash
python3 "$HAM_CLI" status set review --summary '인증 변경 사항 검토 중'
herdr plugin action invoke mailbox-tab --plugin herdr.agent-messenger
```

Herdr 명령 팔레트의 **HAM: Mailbox (Right Split)** 또는 **HAM: Mailbox (quick send)**로도
열 수 있습니다. 모드·main·구성원·깨우기 임곗값은 사용자만 Settings에서 바꿉니다.
새 저장소의 기본 모드는 `auto`이며 기존 설정은 유지합니다.
[역할과 참여 설정](docs/ko/skill-guide.md#모드-역할-공개-상태)을 참고하세요.

## Mailbox TUI

에이전트를 선택하면 메일을 작성하고, 대화를 선택하면 답장합니다. 새 작성창은
항상 `action`으로 시작하며 `[ACTION ^G]` 표시로 현재 종류를 확인합니다.

![HAM 메일함의 인라인 action 작성창](docs/images/mailbox-compose.png)

| 키 | 메일함에서의 동작 |
| --- | --- |
| `Enter` | 에이전트에 작성, 대화에 답장, 입력 중에는 전송합니다. |
| `Ctrl+G` | 현재 작성창의 `action` / `fyi`를 전환합니다. |
| `Shift+Enter` / `Alt+Enter` | 줄을 바꿉니다. Moshi/mosh에서는 Alt+Enter를 사용합니다. |
| `Tab` / `Shift+Tab` | 탐색 모드에서 영역을 옮깁니다. |
| `s` | 탐색 모드에서 Settings를 엽니다. |
| `?` / `F1` | 탐색 모드 / 입력 중에 도움말을 엽니다. |
| `Esc` | 초안을 보존하고 입력창에서 나옵니다. |

Shift+Enter는 호환 터미널에서 요청한 kitty 키보드 프로토콜을 사용합니다.
Ctrl+J는 줄바꿈 키가 아닙니다. `HAM_MAILBOX_NO_KITTY=1`로 프로토콜 요청을 끕니다.

Threads는 질문과 답장을 한 대화로 보여 줍니다. 조회만으로 메일에 ACK하지 않습니다.

![action 질문과 FYI 답장이 있는 대화](docs/images/mailbox-thread.png)

Settings에서 저장소 참여 모드와 호스트별 main·worker 구성을 관리합니다.
연결된 worktree는 그룹에 속한 동안 기준 workspace의 역할 설정을 따릅니다.

![모드·main·구성원·깨우기 임곗값을 보여 주는 Settings](docs/images/mailbox-settings.png)

사용자 직접 입력, 운영자 복구, 진단, 전체 키와 화면 크기별 배치는
[UI 참고서](docs/ko/ui-reference.md)를 참고하세요. 직접 입력은 확인을 거치며
에이전트 작업에 끼어들 수 있습니다. 네이티브 메일함 전달과는 다릅니다.

## 상태 보드

```bash
python3 "$HAM_CLI" status board
python3 "$HAM_CLI" status board --repo --host local
```

![터미널에 표시한 읽기 전용 상태 보드 출력](docs/images/status-board.png)

기본 보드는 로컬의 모든 저장소와 등록된 peer를 조회합니다. 값 없는 `--repo`는
호출자의 저장소를, `--host`는 호스트를 제한합니다. `--verbose`로 전체 진단 정보를
확인합니다. 일부 peer 조회가 실패해도 종료 코드는 0일 수 있으므로 종료 코드뿐 아니라
호스트별 오류·stale 표시·unread `?`도 확인하세요. 보드 조회는 상태·레지스트리·기기
점유를 갱신하지 않습니다.

## 문서

| 주제 | 한국어 | English |
| --- | --- | --- |
| 스킬·메일·역할·런타임·CLI 사용법 | [스킬 가이드](docs/ko/skill-guide.md) | [Skill guide](docs/skill-guide.md) |
| 설치·스키마 업그레이드·구 플러그인 ID | [마이그레이션](docs/ko/migration.md) | [Migration](docs/migration.md) |
| 메일함·설정·키보드·복구 | [UI 참고서](docs/ko/ui-reference.md) | [UI reference](docs/ui-reference.md) |
| SSH 탐색과 안전한 라우팅 | [SSH 및 라우팅](docs/ko/ssh-and-routing.md) | [SSH and routing](docs/ssh-and-routing.md) |
| 안전한 원격 배포와 롤백 | [원격 배포](docs/ko/rollout.md) | [Rollout](docs/rollout.md) |

전체 옵션과 수신 확인 규칙은 번들 [명령 참고서](.agents/skills/herdr-agent-messenger/reference.md)를
참고하세요. 새 세션과 첫 프롬프트는 `spawn --message` 한 번으로 생성합니다.
생성 후 별도의 기존 request를 보내지 마세요.
자세한 내용은 [새 세션 생성](docs/ko/skill-guide.md#주소-선택과-새-세션-생성)을 참고하세요.
호스트를 준비할 때는 참여를 활성화하기 전에 [배포 순서](docs/ko/skill-guide.md#호스트별-runtime-설치와-안전한-배포-순서)를
따르세요. [단독 설치](docs/ko/skill-guide.md#플러그인-없이-ham-사용)를 이용하면 플러그인을
활성화하지 않고도 Herdr에서 사용할 수 있습니다. 해당 절의 제약도 확인하세요.

## 테스트

```bash
python3 -m unittest -v
```

테스트는 전용 HOME/XDG 또는 원장 fixture를 사용합니다.
fixture를 추가하기 전에 [테스트 격리](docs/ko/skill-guide.md#테스트-격리)를 참고하세요.
