<p align="center">
  <img src="assets/ham-logo.png" alt="HAM — Herdr Agent Messenger 마스코트 로고" width="360">
</p>

# HAM — Herdr Agent Messenger

한국어 · [English](README.md)

HAM은 Herdr용 에이전트 라벨링 및 다중 에이전트 메시징 플러그인입니다.
이름 없는 에이전트에 기억하기 쉬운 `color-animal` 이름을 붙이고, 새 세션과
첫 프롬프트를 하나의 작업으로 생성합니다. 로컬 에이전트와 등록된 SSH 호스트의
에이전트는 저장소가 달라도 영속 메일함으로 협업할 수 있습니다.

## 주요 기능

- `blue-otter` 같은 자동 라벨과 색상 표시를 제공합니다.
- `~/.ssh/config`의 명시적 별칭으로 로컬·SSH 에이전트를 탐색합니다.
- 저장소 간 대화 이력과 명시적 처리 기록, `off` / `manual` / `auto` 참여 모드를 제공합니다.
- 키보드 중심 메일함·상태 TUI에서 인라인 FYI 전송, 답장과 확인을 거치는 직접 입력을 제공합니다.
- Codex, Claude Code, omp용 스킬을 짧은 `SKILL.md`와 필요할 때 읽는 `reference.md`로 제공합니다.
- `spawn --message`로 라벨이 있는 세션과 첫 프롬프트를 생성하며, 단독 사용과 정책이 허용하는 기존 요청·대기·읽기·배치·원격 배포 도구도 지원합니다.
- omp와 Claude 수신 런타임은 별도로 선택해 설치합니다. Codex는 작업 경계에서 받은 메일을 명시적으로 읽습니다.
- 정책이 허용하는 기존 에이전트 프롬프트는 수신 pane에 포커스가 있으면 보류하고, 기본 3분 후 **HAM: Held Messages**에서 Push 또는 Cancel을 선택하도록 합니다.
- 에이전트용 CLI는 `status board|list`, `inbox read`, 기존 라우팅 명령에서 간결한 기본 출력과 `--verbose` 진단 정보를 제공합니다. JSON은 불필요한 공백 없이 UTF-8로 출력하며, 반복 조회 대신 범위를 정한 `request-status --wait`를 사용합니다.

사용자가 직접 붙인 이름은 변경하지 않습니다. 에이전트 주소는
`local/blue-otter`, `macbook-pro/purple-koala`처럼 항상 호스트를
포함합니다.

## 요구 사항

- Herdr 0.8.0 이상
- Herdr 호스트는 Python 3.10 이상, `rollout_plugin.py` 실행은 Python 3.11 이상

## 설치

```bash
herdr plugin install zerodice0/herdr-agent-messenger --yes
```

저장소 이름은 `herdr-agent-labels`에서 `herdr-agent-messenger`로 변경됐습니다.
기존 GitHub URL은 새 주소로 리디렉션되므로 기존 GitHub 설치도 계속 업데이트됩니다.
플러그인 ID는 `herdr.agent-messenger`, 표시 이름은 **HAM**으로 유지됩니다.

로컬 개발 환경에서는 현재 체크아웃을 연결할 수 있습니다.

```bash
herdr plugin link "$PWD" --enabled
```

0.7.x에서 업그레이드할 때는 기존 `herdr.agent-labels` ID를 마이그레이션해야
합니다. 구 플러그인을 제거하기 전에 [마이그레이션 가이드](docs/ko/migration.md)를
따르세요.

### 수신 의존성

- **플러그인:** 원장, 메일함, UI와 전송을 담당합니다.
- **스킬:** 에이전트 지침과 CLI 실행기를 제공하며 수신기는 아닙니다.
- **OMP 어댑터:** 네이티브 수신과 조건을 충족하는 action의 자동 깨우기를 제공합니다.
- **Claude 훅:** 턴 경계에서 네이티브 수신을 제공하며 유휴 깨우기는 없습니다.
- **Codex:** 스킬에 따라 받은 메일을 직접 조회하므로 어댑터가 필요하지 않습니다.

플러그인과 스킬만 설치해서는 네이티브 수신 준비가 끝나지 않습니다.
[클라이언트 스킬](#클라이언트-스킬-설치)을 설치한 뒤 **선택한 각 호스트**의
플러그인 또는 체크아웃 루트에서 읽기 전용 상태 요약을 확인하세요.

```bash
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py runtime status
# 사용자가 동의한 경우에만, 이 호스트에서 실제로 쓰는 클라이언트에 설치합니다.
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py runtime install omp
```

Claude 훅은 `omp` 대신 `claude`를 지정합니다. 스킬 설치기는 누락된 런타임의
설치를 제안하며 기본 선택은 **Not now(나중에)**입니다. 비대화형 명령의
`install all --runtime omp|claude|all`은 명시적 동의이며, 의존성을 자동으로
설치하지는 않습니다. 소유권 충돌이 있는 파일은 덮어쓰지 마세요. 사용자가 선택한
세션만 다시 시작하고 `extension_present=1`을 확인하세요. 확인하지 못하면
**설치됨, 활성화 대기**로 보고합니다. 파일 설치는 활성화의 증거가 아닙니다.
플러그인 로드는 클라이언트 설정을 바꾸지 않으며 새 저장소의 기본 `auto` 모드도
유지됩니다. 자세한 내용은 [스킬 가이드](docs/ko/skill-guide.md#수신-의존성)를 참고하세요.

## 사용

에이전트는 저장소가 같거나 달라도 [영속 메일함](#영속-메일함)으로 협업합니다.
[HAM TUI](docs/ko/ui-reference.md)는 화면 크기에 맞춰 Agents, Threads, 읽기 전용
Detail 영역을 배치하며 `s`로 Settings를 엽니다. **HAM: Mailbox (Right Split)**,
**HAM: Mailbox (Tab)** 또는 **HAM: Mailbox (quick send)**로 열 수 있고,
팝업은 전송 성공 후 닫힙니다. NAV에서 Tab·Shift+Tab, 좌우 방향키나 h/l로
영역을 옮기고 상하 방향키나 j/k로 행을 고릅니다. Agents 행에서 Enter로 인라인
FYI 입력창을 열고 Enter로 즉시 전송하며, Threads의 Enter는 답장을 엽니다.
Shift+Enter는 메일함이 요청한 kitty 키보드 프로토콜로 줄을 바꿉니다.
kitty 호환 터미널에서는 herdr를 통해 동작하며 Moshi/mosh에서는 Alt+Enter를
사용하세요. Ctrl+J는 줄바꿈 키가 아닙니다. `HAM_MAILBOX_NO_KITTY=1`로 요청을 끕니다.
Esc는 초안을 보존합니다. Ctrl+D로 전환하는 직접 입력은 확인을 거치며 수신자의
작업에 끼어들 수 있습니다. 에이전트 간 메일이나 native 전달과는 다릅니다.
NAV의 `?`와 입력 중 F1은 도움말을, `e`는 진단 정보를 엽니다. Settings의
`m`·Space로 선택한 에이전트의 main·구성원 지정을 바꿀 때와 Threads의
`w`·`t`·`a`·`n`을 실행할 때는 확인을 거칩니다. `a`·`n`에는 증거도 필요합니다.
Agents는 호스트별로 묶으며 stale 호스트의 unread는 `?`입니다. Detail은 전체 요약과
상태 갱신·관찰 시각을 구분해 보여 주며, 조회만으로 메일에 ACK하지 않습니다.

기존 **Send Prompt to Agents** 팝업의 코디네이터 위임과 다중 수신자 직접 전송은
참여 정책이 허용하는 경우에 사용할 수 있습니다.

Codex에서는 `$ham`을 입력한 뒤 **HAM — Herdr Agent Messenger**를 선택합니다.
Claude Code에서는 `/herdr-agent-messenger`를 명시적으로 호출하거나 자연어로
HAM 작업을 요청할 수 있습니다. omp에서는 스킬을 설치한 뒤 자연어로 요청하거나
`skill://herdr-agent-messenger`를 읽도록 지시합니다.

```text
$ham local/yellow-falcon에게 dxp-ui에서 사용하는 언어 라벨을 물어봐.
응답을 기다린 다음 현재 작업과 기본값을 그 답변에 맞춰줘.
```

주소에는 항상 호스트를 포함하세요. 기존 탐색·프롬프트 전송 명령은 현재 기기에
`local/LABEL`을, 원격 기기에 설정된 SSH 별칭을 사용합니다. 메일함 명령은 등록된
정규 호스트 이름도 받습니다. 라벨만 쓰면 다른 호스트의 에이전트와 충돌하거나
AI 클라이언트 자체 서브에이전트로 오인될 수 있습니다.

### 새 세션

새 에이전트 세션이 필요하면 HAM에 생성을 명시적으로 요청하세요.
`spawn --message`는 라벨이 있는 세션을 만들고 첫 프롬프트 작업을 영속적으로
기록한 뒤, 대화형 세션이 준비되면 한 번 제출합니다. 세션을 먼저 만든 뒤
별도의 기존 `request`로 첫 프롬프트를 보내지 마세요. 체크아웃에서는 다음과
같이 실행합니다.

```bash
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py spawn \
  --kind omp --cwd "$PWD" --placement tab --message '이 프로젝트를 검토해 주세요.'
```

호출하는 기기의 UTF-8 파일을 읽으려면 대신 `--message-file PATH`를 사용하세요.
에이전트 인수를 전달할 때는 HAM 옵션을 `--` 앞에 둡니다. 메시지 옵션을 모두
생략하면 첫 프롬프트 없이 세션만 만듭니다. 기존 라벨이 없다는 이유로 새 세션을
자동 생성하지는 않습니다. 직접 `herdr agent start <name>`을 실행하면 HAM의
라벨 선택과 메타데이터 처리를 우회합니다.

첫 프롬프트의 수락 상태가 `unknown`이면 이미 제출됐을 수 있습니다. 작업 ID와
pane을 보존하고, 메시지를 다시 보내거나 대체 세션을 만들지 마세요. 터미널의
입력 수락은 메일함의 네이티브 전달이나 모델의 성공적인 처리를 뜻하지 않습니다.

### 정책이 허용하는 기존 프롬프트

`manual` 또는 `auto`에서 같은 저장소의 송신자와 수신자가 모두 참여하면
기존 `send`, `request`, `batch`는 `use_mailbox`로 거부합니다. 배치는 모든
수신자를 먼저 확인하므로, 정책 거부나 조회 실패가 하나라도 있으면 아무것도
제출하지 않습니다. 이는 실수 방지 장치이지 보안 경계는 아닙니다.
비참여 통신에는 다음 보류·관찰 규칙을 포함한 기존 절차를 사용할 수 있습니다.

CLI 메시지의 수신자 pane에 포커스가 있으면 전송을 보류하므로 다시 보내지
마세요. 기본 보류 제한인 3분이 지나면 송신 호스트의 **HAM: Held Messages**에서
Push 또는 Cancel을 선택하세요. 보류 후 전송하거나 Push로 전송하면 worker가
요청을 관찰하다가 종료 상태가 되거나 제출 후 30분이 지나면 관찰을 끝냅니다.
저장된 결과는 `request-status`로 읽으며, 30분 뒤에도 이 명령으로 관찰을
이어갈 수 있습니다. 자기 자신에게 보내면 송신자 pane에 포커스가 있어도
`recipient_is_sender`로 즉시 실패하며 보류하지 않습니다.

`request-status --wait MS`는 `state`, `phase`, `error_code` 중 하나가 바뀌거나,
요청이 종료 상태가 되거나, 제한 시간이 지나면 반환합니다. 응답 텍스트가
늘어나는 것만으로는 대기를 끝내지 않습니다. 간결한 결과의 `response.truncated`는
출력 한도나 읽기 실패로 응답 텍스트 자체가 잘렸을 때만 `true`입니다. 오래된
이력을 생략했거나 기준 화면과의 연속성을 잃었다는 이유만으로는 설정하지 않습니다.
잘못된 보류 제한을 나타내는 `invalid_defer_timeout`을 비롯한 오류와 전체 옵션은
[명령 참고서](.agents/skills/herdr-agent-messenger/reference.md)를 참고하세요.

### 클라이언트 스킬 설치

Herdr 명령 팔레트의 **HAM Skill**에서 번들 스킬을 설치·갱신하거나 다음 명령을
실행합니다.

```bash
herdr plugin action invoke agent-skill-guide --plugin herdr.agent-messenger
```

설치 대상은 `codex`, `claude`, `omp`, `all`입니다. omp의 System 설치 경로는
`~/.omp/agent/skills/herdr-agent-messenger`이며, Project 설치 경로는
`<root>/.omp/skills/herdr-agent-messenger`입니다.

HAM 플러그인을 활성화하지 않고 Herdr와 스킬만 사용할 수도 있습니다. 소스 기반
단독 설치 방법과 제약은 [스킬 가이드](docs/ko/skill-guide.md#플러그인-없이-ham-사용)를
참고하세요.

## 영속 메일함

에이전트는 터미널에 프롬프트를 직접 넣는 대신 저장소 간 영속 메일을 사용할 수
있습니다. 로컬 에이전트와 등록된 peer 사이에는 별도의 저장소 허용 목록이 없으며,
peer 등록이 신뢰 경계입니다. 등록된 Herdr 에이전트 pane에서 다음과 같이 실행합니다.

```bash
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py mail send \
  --to local/blue-owl --kind action --subject '검토' --body '이 프로젝트를 검토해 주세요.'
# 수신자는 작업 경계에서 조회합니다.
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py inbox read
# 관련 작업을 마친 뒤 처리 상태를 기록합니다.
python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py inbox handle \
  --message MESSAGE_UUID
```

`fyi`는 참고 정보이며, `action`은 조건에 맞으면 Herdr의 `turn_state`가 `idle` 또는 `done`인 omp 세션에서 턴을
시작할 수 있습니다. FYI만으로는 턴을 시작하지 않습니다. 같은 pane을 쓰는
서브에이전트가 읽어도 조회는 전달·처리 상태를 바꾸지 않습니다. 명시적 처리는
대기 중인 메일을 네이티브 전달 시각 없이 처리 완료로 바꿀 수 있습니다.
현재 수신자에게 온 메일에 답장하면 해당 원문도 같은 트랜잭션에서 처리됩니다.
`mail show --thread UUID`로 대화를 확인하고,
`mail withdraw --message UUID`로 철회를 요청합니다. 이미 전달 claim이
생성된 메일을 단순히 회수할 수는 없습니다.

대화의 시작 저장소(`threads.repo_id` / `messages.repo_id`)는 바뀌지 않습니다.
각 메시지는 발신 시점의 `sender_repo_id`를, 각 전달은 해석된 수신자의
`recipient_repo_id`를 저장합니다. terminal에서 다른 저장소를 실행하면 시작
저장소가 아니라 이 **수신 저장소 스냅샷**과 비교해 `recipient_changed`로
보류합니다. 받은 메일 조회·처리·네이티브 claim·unread도 전달의 수신 저장소를
기준으로 합니다. 대화에서 메일을 보내거나 받은 참여자는 저장소가 달라도
시작 저장소를 바꾸지 않고 답장할 수 있습니다.

자동 턴 시작 조건은 그대로입니다. 수신자 저장소의 모드·임곗값과 수신자의 역할,
idle/done 상태, 수신자별 시간당 한도, coalescing과 max hop을 모두 적용합니다.
저장소 간 전송이 이 조건을 우회하지는 않습니다. 기존 `use_mailbox` 정책도
같은 저장소의 참여 송수신자에만 적용합니다.

### 저장소 이력과 상태 보드 조회

위의 번들 CLI 경로 뒤에 다음 읽기 전용 명령을 붙입니다.

```text
mail threads [--repo REPO_ID] [--limit N] [--cursor TOKEN]
mail direct-list [--repo REPO_ID] [--limit N] [--cursor TOKEN]
status board [--repo [ID]] [--host local|CANONICAL|REGISTERED_SSH_ALIAS] [--verbose]
```

이력 조회는 기본적으로 정체성을 검증한 호출자의 저장소를 선택합니다.
`--repo REPO_ID`를 명시하면 Herdr에서 호출자 정체성을 조회하지 않습니다.
대화의 시작 저장소, 메시지의 발신 저장소 또는 전달의 수신 저장소가 선택한
저장소이면 대화 목록에 포함하므로 양쪽에서 저장소 간 대화를 찾을 수 있습니다.
이 원장에 남아 있는 원격 pending 사본도 포함하지만 모든 호스트의 이력을
복제하지는 않습니다. `mail direct-list`는 기존처럼 선택한 저장소의 사용자
직접 입력 이력만 조회합니다. 직접 전송 권한이나 저장소 간 메일 목록이 아니라
수락 증거를 읽는 기능입니다. `--limit`은 기본 100개이며 1~1000개를 받습니다. 결과에는 `repo_id`,
`threads` 또는 `submissions`, 마지막 페이지에서 `null`인 `next_cursor`가 있습니다.
HMAC 서명된 불투명 cursor는 원장·쿼리·저장소에 귀속되므로 그대로 전달하세요.
정렬은 `updated_at DESC`, UUID 오름차순이며 OFFSET 대신 keyset 페이지를 씁니다.
고정된 스냅샷이 아니므로 조회 사이의 갱신으로 기록이 경계 앞으로 이동할 수
있습니다. 최신 갱신을 보려면 첫 페이지부터 다시 조회하세요. 변조하거나 범위가
다른 token을 쓰면 `invalid_cursor`로 실패합니다.

보드는 기본적으로 로컬의 모든 저장소와 등록된 모든 peer를 조회합니다.
값 없는 `--repo`는 호출자의 저장소를, `--repo ID`는 지정한 저장소를 선택합니다.
`--host`는 로컬 원장 또는 등록된 정규 호스트·SSH 별칭 하나만 선택합니다.
결과는 `{repo_id,agents,hosts}`이며 주소·workspace·repo·역할·턴·phase·summary·unread·수신
상태와 기기·서버 개수를 간결하게 표시합니다. `hosts[].agents` 중복은 없으며 호스트별
오류·stale·unread와 자원 개수·탐색 오류는 유지합니다. `--verbose`는 전체 레지스트리·상태·
자원 필드와 `status_updated_at`(상태 갱신), `last_seen`(레지스트리 관찰)을 복원합니다.
간결한 `status board` / `status list` 행의 `repo`는 짧은 저장소 이름이므로
verbose 출력 없이도 다른 저장소의 대상을 구분할 수 있습니다.
Python·TUI·peer export의 전체 구조는 바뀌지 않습니다. 조회는 레지스트리를 수집하거나
상태·표시·claim을 바꾸지 않으며 peer에도 `refresh=false`를 전달합니다.
peer 조회에 실패하면 `hosts`에 구조화된 오류와 `stale:true`, unread `?`를 남깁니다.
이전 스냅샷이 없는 CLI는 에이전트 행을 만들어 내지 않습니다. TUI는 검증된 이전
행을 stale로 유지할 수 있습니다. 기존 `list`와 로컬 `status list`는 더 좁은
범위의 조회로 유지됩니다.
peer도 이 읽기 API를 지원해야 합니다. 이전 버전은 `refresh=false`를 무시하고
기존 레지스트리 수집을 할 수 있습니다. 일부 peer에 실패해도 종료 코드는 0이므로
호스트별 `error`, `stale`, unread를 확인하세요. `--host` 대상이 등록되지 않았거나 모호하면
`peer_not_configured`와 종료 코드 1을 반환합니다.

세 명령 모두 `-h` / `--help`를 지원하며 JSON 객체 하나를 반환합니다. 세 명령 중
보드만 `--verbose`를 지원합니다. `status list`와 `inbox read`도 간결한 기본 출력에
`--verbose`를 붙이면 전체 진단 정보를 반환합니다. inbox는 ID·발신자·본문·상태·답장
문맥을 유지하며 조회로 ACK하지 않습니다. verbose를 포함한 CLI JSON은 불필요한 공백 없이
UTF-8로 출력합니다. 도메인 오류는 `{"error":{"code":"CODE","message":"TEXT"}}`와 종료 코드
1을 반환합니다. 사용법 오류는 종료 코드 2입니다. 조회는 메일을 확인 처리하지
않으며 설정 변경, 사용자 작성·직접 전송, 운영자 ACK를 우회하는 공개 명령도
제공하지 않습니다. inbox·Threads·전송 outbox·상태·unread의 구분은
[참고서 용어](.agents/skills/herdr-agent-messenger/reference.md#mailbox-terminology)를 참고하세요.

### 기기 점유와 서버 표시

위의 번들 CLI 경로 뒤에 다음 명령을 붙입니다.

```text
device claim phone|emu DEVICE [--label TEXT] [--force]
device release [phone|emu|all] [DEVICE]
device list
```

기기를 쓰기 전에 점유하고 사용을 마치면 해제합니다. `release`의 기본값은 `all`이며
호출자 자신의 점유만 해제합니다. 종류나 기기를 지정하면 해제 범위를 좁힙니다.
에뮬레이터 번호는 `emulator-N`으로 정규화합니다. 점유는 상속된 pane ID가 아니라
검증된 호스트·terminal·세션 generation에 귀속됩니다. 같은 기기를 다시 점유하면
라벨을 갱신합니다. 다른 활성 소유자가 있으면 `device_claimed`로 거부하며,
`--force`는 **사용자가 명시적으로 지시했을 때만** 사용합니다. 소유자가 비활성이거나
이전 generation이면 stale로 판단하여 교체하고 `replaced_stale:true`를 반환합니다.
Herdr 갱신 실패는 오류이지 소유자가 사라졌다는 근거가 아닙니다. adb 연결 여부와
점유 소유자의 활성 여부도 별개입니다.

`device list`와 `status board`의 자원 조회는 읽기 전용입니다. 레지스트리 갱신,
stale 점유 정리, 토큰 쓰기를 하지 않습니다. verbose 보드·peer JSON에는 독립적인
`devices`(유효한 점유)와 `servers` 목록이 추가되며, 호스트 항목에는
`resources:{servers_unowned:[],dead_serve:[],stale_devices:[],error:null}`이 추가됩니다.
간결한 CLI에서는 이 목록을 개수로 표시하며 `--verbose`로 전체 행을 조회할 수 있습니다.
서버는 점유하지 않고 현재 사용자의 리스너와 Tailscale Serve 경로에서 탐색합니다.
이전 peer에 `devices`, `servers`, `resources` 중 어느 필드가 없으면 해당 정보만
알 수 없음(`?`)으로 표시하며, 빈 목록으로 해석하지 않습니다.

사이드바 토큰은 정확히 `phone=📱 <공백으로 연결한 라벨>`과
`emu=🤖 <공백으로 연결한 라벨>` 형식이며, 점유가 없으면 해당 키를 지웁니다.
`status set`도 이 표시를 유지합니다. 제품은 기존 기기 JSON을 가져오지 않습니다.
`heavy_slot.py`는 별도 스크립트로 그대로 유지하며 HAM은 슬롯을 읽거나 표시하지
않습니다. JSON 필드는 [스킬 가이드](docs/ko/skill-guide.md#기기-점유와-자원-표시),
화면 표시는 [UI 참고서](docs/ko/ui-reference.md#기기와-서버-표시)를 참고하세요.

### 참여 모드와 역할

참여 설정은 에이전트용 변경 명령이 아니라 TUI의 Settings 화면에서 선택합니다.

이 버전부터 HAM이 처음 발견한 저장소는 `auto`로 시작합니다. 같은 저장소의 모든
에이전트가 참여하며, 조건에 맞는 대기 중 action 메일은 OMP의 새 턴을 자동으로
시작할 수 있습니다. 같은 저장소의 기존 `send`·`request`·`batch` 대신 메일을
사용해야 합니다. 이미 알려진 저장소는 기존 설정을 유지하며, 저장된 모드가 없으면
변경할 때까지 `off`로 남습니다. 호스트 로컬(`local:`) 저장소에도 같은 기본값을
적용하지만 설정을 다른 호스트로 전파하지 않습니다. 읽기 전용 명령은 참여 설정을
초기화하지 않습니다.

모드·home·깨우기 임곗값은 저장소 전체에 적용합니다. main과 worker 구성원은
호스트별 역할 범위 `(repo_id, 정규 호스트, anchor_workspace_id)`로 정합니다.
연결된 worktree workspace는 같은 호스트에서 Herdr `worktree.repo_key`가 같고
현재 열려 있는 비연결 workspace를 기준 workspace(anchor)로 삼습니다. 후보가
여럿이면 결정적인 순서로 하나를 선택합니다. 메타데이터가 없거나 기준 workspace가
열려 있지 않으면 자체 범위를 유지합니다. 일반 workspace를 git common directory로
묶지는 않습니다. 기준 workspace에 활성 에이전트가 없으면 연결된 에이전트가 하나만
남아도 자동 main이 되지 않습니다. 기준 workspace의 main·members 설정을 사용하며,
연결된 workspace의 기존 설정은 변경하지 않고 그룹에 속한 동안만 무시합니다.
그룹에서 분리되면 기존 설정이 다시 적용됩니다. 역할 설정은 해당 호스트에만
저장하며 복제하지 않습니다. peer는 상태 내보내기로 각 호스트가 계산한 역할을 받습니다.

| 모드 | 역할과 자동 동작 |
| --- | --- |
| `off` | 역할, 자동 턴 시작, 참여 정책에 따른 기존 전송 거부가 없습니다. 명시적 받은 메일 조회와 일반 사용자 턴 경계에서의 수신은 가능합니다. |
| `manual` | 역할 그룹 안의 활성 에이전트를 명시적 main으로 지정하며, 선택한 그룹 구성원 중 main을 제외한 에이전트가 worker가 됩니다. 비멤버도 일반 턴 경계에서 수신할 수 있지만 자동으로 턴을 시작하지 않습니다. |
| `auto` | 기준 workspace 설정의 유효한 명시적 main이 우선합니다. 없으면 기준 workspace에 활성 에이전트가 있을 때만 자동으로 선출합니다. 그룹의 활성 에이전트가 하나이면 그 에이전트가, 여럿이면 메인 체크아웃의 활성 에이전트가 정확히 하나일 때 그 에이전트가 main이 됩니다. 그 외에는 main이 없습니다. 나머지 그룹 에이전트는 main 유무와 관계없이 worker가 됩니다. 그룹에 속하지 않은 workspace는 자체 선출 규칙을 유지합니다. |

사이드바와 HAM 소유 pane 테두리는 각 에이전트의 현재 호스트별 역할 그룹에
따른 역할을 표시합니다. 테두리는 `[메인] alias · summary` 또는 `[워커] alias · summary`로
표시합니다. 역할이나 요약이 없으면 해당 부분을 생략합니다. 별칭을 요약 앞에 두므로
Herdr가 긴 내용을 잘라도 별칭보다 요약이 먼저 잘립니다. `status clear`는 phase와
summary만 지우고 별칭과 현재 역할은 유지합니다. 사용자가 직접 지정한 테두리는
보존합니다. Agents의 `display_agent`는 주소로 쓸 별칭을 맨 뒤에 유지하며,
phase는 HAM의 `$recap` 토큰에 따로 표시합니다.

home은 처음 등록할 때(이미 알려진 저장소라면 참여를 처음 활성화할 때) 제안하는
정규 호스트이며, 동시에 제안된 값은 정규 호스트 이름 순서에 따라 수렴합니다.
Settings에서는 읽기 전용으로 표시됩니다. 정규 호스트 이름과 UUID는 SSH 별칭이나 호스트
이름과 구별됩니다. 에이전트는 안정적인 터미널 슬롯과 세션 세대로 식별하므로
pane을 옮겨도 슬롯은 유지됩니다. 이전 세대의 불확실한 claim은 자동 재전송하지
않습니다. TUI에서 증거를 남기고 수행하는 운영자 판정은 감사 기록이며,
네이티브 전달 증거나 작업 완료를 뜻하지 않습니다.

### 원격 전송과 수신 런타임

수신자는 `local/LABEL`, `CANONICAL_HOST/LABEL`, 등록된 `SSH_ALIAS/LABEL`,
`main` 또는 `[HOST/]main@REPO`로 지정합니다. 라벨은 선택한 호스트의 모든
저장소에서 찾습니다. 없으면 `agent_not_found`, 여럿이면 후보 주소와 저장소를
포함한 `agent_ambiguous`로 실패하므로 임의로 선택하지 마세요.

`--to main`은 송신자의 현재 호스트·저장소·기준 workspace 역할 그룹의 main을
선택합니다. 연결된 workspace에서도 같은 main을 사용하며 다른 그룹이나 home으로 대체하지 않습니다.
`--to main@dxp-api`는 로컬에서, `--to beta/main@org/dxp-api`는 등록된 beta
호스트에서 해당 저장소의 main을 선택합니다. REPO는 정확한 repo ID를 우선하며,
그 외에는 경로의 끝 구성요소가 유일하게 일치해야 합니다. 저장소가 여러 개이면
`repo_ambiguous`로 실패합니다. 선택한 호스트에서 서로 다른 역할 그룹을 기준으로
현재 main이 정확히 하나여야 합니다. 없으면 `main_unresolved`, 여러 그룹에 있으면
기준 workspace ID 목록과 함께 `main_ambiguous`로 실패합니다.
모든 주소 형식은 보낼 때 구체적인 terminal 대상을 저장합니다.
주소를 명시한 메일은 workspace와 저장소 경계를 모두 넘을 수 있습니다.

각 호스트에서 `ledger setup --name HOST`로 정체성을 고정한 뒤 양쪽에서
`ledger peer add --ssh-alias ALIAS`로 연결을 등록합니다. 전송 상태는
`ledger doctor`와 `ledger outbox`로 확인합니다. 모델을 호출하지 않는 독립
flock worker는 일시적 오류를 같은 메시지 ID로 재시도합니다. 영구 오류는
명시적으로 재시도할 수 있도록 표시합니다. 모델 폴링이나 데이터베이스 복제는
하지 않습니다.

스키마 v8은 기존 메시지의 저장소로 발신·수신 저장소 스냅샷을 채워 같은 저장소의
기존 동작과 불변 해시를 유지합니다. 새 불변 wire payload에는 발신·수신 저장소
스냅샷이 모두 들어갑니다. 참여 호스트를 함께 업그레이드하세요. 정확한 옛 필드만
받는 구버전 수신자는 v8 payload를 `invalid_message`로 거부합니다. 메일은 수신자
쪽에서 자동 보류되는 것이 아니라 발신 outbox에 실패 상태로 보존됩니다.
메시지·항목 ID를 유지하고 업그레이드 후 `ledger retry --item OUTBOX_ITEM_UUID`로
수동 재시도하세요. 새 메일로 다시 보내지 마세요.

스키마 v9는 에이전트가 없는 기준 workspace를 포함해 현재 열린 Herdr workspace의
메타데이터를 호스트별 `workspaces` 테이블에 추가합니다. 기존 workspace 설정은
이동하거나 삭제하지 않습니다. `repo show`의 `workspace_id`는 기준 workspace이며,
`requested_workspace_id`는 원래 workspace입니다. 프로토콜은 1을 유지하며 역할 범위
내보내기 메타데이터는 선택 사항입니다. 이전 peer도 계산된 역할을 받아 볼 수 있습니다.
이전 코드는 스키마 v9 원장을 열 수 없으므로 각 호스트의 HAM 프로세스와 복사한
스킬을 함께 갱신하고 다시 시작해야 합니다. 다운그레이드에는 백업한 DB가 필요합니다.
[스키마 v9 마이그레이션·배포 안내](docs/ko/migration.md#worktree-역할-그룹스키마-v9)를 참고하세요.
기존 [스키마 v7 안내](docs/ko/migration.md#workspace-역할-마이그레이션스키마-v7)는
과거 기록이며, workspace별 독립 역할 규칙은 v9의 그룹 규칙으로 대체됐습니다.

스킬 설치는 수신 런타임을 **자동으로 설치하지 않습니다**. 설치기의 명시적 선택이나
플래그로 동의하거나, 각 호스트에서 `runtime install omp` 또는
`runtime install claude`를 별도로 실행합니다.
같은 대상의 `runtime status`로 상태를 확인하고 `runtime uninstall`로 제거합니다.
선택한 세션만 다시 시작하세요. 설치기는 HAM 확장이나 훅 항목만 관리하며,
관련 없는 Herdr·클라이언트 설정은 보존합니다.

OMP 시작 중 `native_session_pending`은 Herdr가 아직 native 세션 경로를 보고하지
않았다는 뜻입니다. HAM은 정체성을 바꾸지 않고 정해진 횟수 안에서 등록을 재시도합니다.
등록에 성공하면 HAM의 대기·사용 불가 표시를 지우며, 재시도를 소진하면 사용 불가
상태를 표시합니다. 이미 보고된 경로가 실제로 다르면 계속 `stale_session`으로 거부합니다.

- **omp:** Herdr의 루트 세션은 일반 턴 경계에서 메일을 받을 수 있습니다.
  action으로 자동 턴을 시작하려면 역할, 지원되는 빈 입력창, 입력 유휴 시간,
  OMP 런타임 유휴 상태, Herdr의 `idle` 또는 `done` 상태, 설정된 묶음 처리·빈도 제한 조건을 모두 충족해야 합니다.
  포커스만으로 메일을 보류하지는 않습니다. ACK는 같은 세션의 활성 분기
  저널에서 정확한 메일 묶음을 관찰했다는 뜻이지, 제공자의 수락이나 모델의
  이해를 뜻하지 않습니다.
- **Claude Code:** 제한된 `SessionStart`와 `UserPromptSubmit` 훅에서 수신합니다.
  Stop·유휴 훅으로 턴을 시작하지 않으며 코드 worker도 Claude 턴을 만들지
  않습니다. 훅 출력만으로는 전달이 아닙니다. 네이티브 ACK는 두 증거 경로를
  지원합니다. 하나는 SDK의 system/request blob에 정확한 메일 묶음이 있고
  새 요청 추적과 연관된 경우(`claude:api-request:`)입니다. 다른 하나는 타입이
  지정된 네이티브 훅의 원본·렌더링 기록이 정확한 claim 바이트·해시와 일치하고
  인과관계를 입증하는 네이티브 요청 기록이 있는 경우(`claude:accepted-journal:`)입니다.
  후자는 호스트 컨텍스트에 적용됐다는 증거이며, 제공자에게 전송된 정확한
  바이트나 제공자의 증명, 모델의 자기 보고·이해를 뜻하지 않습니다.
  두 경로 모두 `confirmation_source=native`를 사용하며, 운영자 판정은
  `operator`로 구별합니다.
- **Codex:** 작업 경계에서 `inbox read`로 읽고 관련 작업을 수행한 뒤
  `inbox handle`로 명시적으로 처리합니다. 자동 수신 어댑터나 턴 시작 기능은 없습니다.

전달과 처리는 다릅니다. 사용자 직접 입력은 터미널의 수락 상태를 별도로
기록하며 네이티브 메일함 전달로 표시하지 않습니다. 직접 입력의 헤더는
`Message from user (HAM):`입니다.

Claude Code 2.1.301의 제한된 실제 실행에서 프로덕션 SessionStart 컨텍스트와
현재 세대 worker가 영속 기록한 `delivered` 상태를 확인했습니다. 해당 기록은
`via=hook`, 네이티브 확인, `claude:accepted-journal:` 증거를 포함합니다.
동일한 등록 프로세스에서 worker가 실행되고 action·FYI 메일이 대기하는 동안,
manual-worker와 auto-main 각각 6.02초의 유휴 구간에 추가 주 턴이나 claim이
생기지 않았습니다. 보조 `quota_check` / `prompt_suggestion` 요청은 별개이므로
제공자 통신이 전혀 없었다는 뜻은 아닙니다. 이 실행에서는 Herdr가 세션 UUID를
이미 알고 있었습니다. 콜드 시작의 미보고 상태에서 수행하는 바인딩 CAS는
단위 검증만 통과했으며 실제 실행으로 입증하지는 않았습니다. 이 제한된 관찰은
전체 런타임이나 제공자의 성공을 뜻하지 않습니다.

전체 옵션과 정체성·저장 위치·수신 확인·복구·런타임 설치 규칙은
[메일함 명령 참고서](.agents/skills/herdr-agent-messenger/reference.md#local-mailbox)를,
설정·빠른 전송·답장·불확실한 claim 복구 조작은 [UI 참고서](docs/ko/ui-reference.md)를 참고하세요.

## 배포 순서

먼저 **각 호스트의 플러그인**을 해당 프로토콜을 지원하는 코드로 업그레이드합니다.
그다음 일치하는 스킬과 수신 런타임을 준비하고 나서 참여 정책을 활성화하세요.
수신 준비 전에 기존 협업 전송부터 거부되는 상황을 피해야 합니다. Codex는
런타임 어댑터 대신 스킬을 통한 명시적 조회 절차를 사용합니다. 마지막으로
사용자가 전역 단축키 연결 여부를 선택하고 상태·recap 규칙을 이전합니다.
전역 단축키는 자동으로 바꾸지 않습니다. 이는 배포 절차 안내이며, 호스트나
세션의 업그레이드를 이미 마쳤다는 뜻은 아닙니다.
자세한 절차는 [원격 배포 가이드](docs/ko/rollout.md)를 참고하세요.

## 문서

| 주제 | 한국어 | English |
| --- | --- | --- |
| 스킬 설치와 프롬프트 작성 | [스킬 가이드](docs/ko/skill-guide.md) | [Skill guide](docs/skill-guide.md) |
| 설치 및 0.7.x 마이그레이션 | [마이그레이션](docs/ko/migration.md) | [Migration](docs/migration.md) |
| SSH 탐색과 라우팅 계약 | [SSH 및 라우팅](docs/ko/ssh-and-routing.md) | [SSH and routing](docs/ssh-and-routing.md) |
| 팝업 동작과 키보드 조작 | [UI 참고서](docs/ko/ui-reference.md) | [UI reference](docs/ui-reference.md) |
| 안전한 원격 배포 | [원격 배포](docs/ko/rollout.md) | [Rollout](docs/rollout.md) |

## 테스트

```bash
python3 -m unittest -v
```
