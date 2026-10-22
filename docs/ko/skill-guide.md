# HAM 에이전트 스킬

한국어 · [English](../skill-guide.md) · [README](../../README.ko.md)

HAM은 저장소 간 영속 메일과 조정 상태를 관리하고, `spawn`으로 새 Herdr 에이전트
세션을 시작합니다. 기존 정책이 허용하는 에이전트에는 프롬프트 전송과 응답 수집도
사용할 수 있습니다. Codex, Claude Code, omp에서 사용하는 번들 스킬의 정식 이름은
`herdr-agent-messenger`이며, **HAM**은 짧은 사용자 표시 이름입니다.

번들 스킬은 일반적인 작업을 안내하는 짧은
[SKILL.md](../../.agents/skills/herdr-agent-messenger/SKILL.md)와 필요할 때 읽는
[reference.md](../../.agents/skills/herdr-agent-messenger/reference.md#command-and-flag-reference)로
나뉩니다. 전체 옵션과 오류 코드 표는 `reference.md`에 있습니다.

호출자 기준 `spawn`, `--message-file`, `--body-file`, `--destination`에는
프로젝트 cwd에서 복사본이나 번들의 `scripts/herdr_agent_messenger.py` 절대 경로를
사용하세요. 실행기는 플러그인 루트에서 고정된 패키지를 불러온 뒤
`python3 -m ham --cwd <CALLER_DIRECTORY> cli ...`로 호출자 cwd를 복원합니다.
직접 실행하려면 `cd /ABS/PLUGIN_ROOT && python3 -m ham --cwd /ABS/CALLER cli ...`를 사용하세요.
아래 모듈 예제는 의도적으로 플러그인 루트에서 실행하므로 상대 경로도 그 루트를
기준으로 합니다. [패키지 구조와 cwd](migration.md#python-패키지-구조)를 참고하세요.

## 설치와 클라이언트별 호출

플러그인에는 Codex용 `.agents/skills/herdr-agent-messenger`와 Claude Code용
`.claude/skills/herdr-agent-messenger`가 포함됩니다. 저장소 체크아웃에서는 각
클라이언트가 프로젝트 스킬을 발견합니다. omp는 아래 설치기로 설치하세요.

모든 workspace에서 사용하려면 Herdr 명령 팔레트에서 **HAM Skill**을 열고
Codex, Claude 또는 omp 대상을 **System** 범위로 설치하세요. **Project** 범위는
현재 Herdr workspace에만 적용됩니다. 설치 경로는 다음과 같습니다.

| 클라이언트 | System 경로 | Project 경로 |
| --- | --- | --- |
| Codex | `~/.agents/skills/herdr-agent-messenger` | `<root>/.agents/skills/herdr-agent-messenger` |
| Claude Code | `~/.claude/skills/herdr-agent-messenger` | `<root>/.claude/skills/herdr-agent-messenger` |
| omp | `~/.omp/agent/skills/herdr-agent-messenger` | `<root>/.omp/skills/herdr-agent-messenger` |

Agent Messenger에서 `Ctrl+G`를 눌러 같은 설치 화면을 열 수 있고, `?`를 누르면
호출 가이드를 볼 수 있습니다. 상태 배지의 의미는 다음과 같습니다.

- `✓`는 최신 상태입니다.
- `↑`는 갱신할 수 있는 상태입니다.
- `○`는 설치되지 않은 상태입니다.
- `!`는 충돌하는 파일이 있는 상태입니다.

설치기는 같은 이름의 관련 없는 스킬 디렉터리를 덮어쓰지 않으며, 관리 대상 파일의
경로에 심볼릭 링크가 있으면 교체를 중단합니다. 새 파일을 임시 디렉터리에 모두
복사한 뒤 기존 설치를 교체하므로 복사가 실패해도 이전 설치를 유지합니다.
교체가 실패하면 이전 설치로 복구하며, 복구까지 실패하면 오류에 보존한 백업 경로를
표시합니다. 사용자가 추가한 파일은 유지하고 바이트코드와 `.DS_Store`는 제외합니다.
새 스킬이 보이지 않으면 에이전트 세션을 새로 시작하세요.

셸에서도 설치 화면을 열 수 있습니다.

```bash
herdr plugin action invoke agent-skill-guide --plugin herdr.agent-messenger
```

체크아웃 또는 설치된 플러그인 루트에서는 다음 명령으로 번들 스킬 경로를 확인하고
System 범위로 설치할 수 있습니다. 설치 대상은 `codex`, `claude`, `omp`, `all`이며,
`all`은 세 클라이언트 모두를 설치합니다.

```bash
python3 -m ham messenger skill-path
python3 -m ham skill-installer install omp
python3 -m ham skill-installer install all
```

설치기는 스킬 파일을 복사합니다. 플러그인이 Herdr에 등록되어 있지 않다면 복사한
스킬만으로 체크아웃을 찾을 수 없으므로 아래의
[플러그인 없는 설치 방법](#플러그인-없이-ham-사용)에 따라 심볼릭 링크를 사용하세요.

### 수신 의존성

- **플러그인:** 원장, 메일함, UI와 전송을 담당합니다.
- **스킬:** 에이전트 지침과 CLI 실행기를 제공하며 수신기는 아닙니다.
- **OMP 어댑터:** 네이티브 수신과 조건을 충족하는 action·FYI의 자동 깨우기를 제공합니다.
- **Claude 훅:** 턴 경계에서 네이티브 수신을 제공하며 유휴 깨우기는 없습니다.
- **Codex:** 스킬에 따라 받은 메일을 직접 조회하므로 어댑터가 필요하지 않습니다.

플러그인과 스킬을 설치·갱신한 뒤 **선택한 모든 호스트**에서 읽기 전용 상태
요약을 확인하세요. cwd는 플러그인 루트로 지정합니다.

```bash
python3 -m ham cli runtime status
# 사용자가 동의한 경우에만, 실제로 쓰는 클라이언트(omp 또는 claude)에 설치합니다.
python3 -m ham cli runtime install omp
```

요약의 `clients`는 런타임 소유권과 충돌, `sessions`는 활성 로컬 세션의 수신기
등록 수, `ledger`는 원장의 사용 가능 여부와 오류, `next_steps`는 권장 조치를
보여 줍니다. 원장이 없거나 구버전이어도 읽기 과정에서 생성·업그레이드하지 않고
그 상태를 보고합니다. `runtime status omp|claude`는 기존 클라이언트별 출력 형식을
유지합니다. Codex의 직접 조회 방식은 의도된 동작이며 수신기 누락 오류가 아닙니다.

스킬 설치에 성공하면 대화형 설치기는 두 런타임의 상태를 보여 주고 누락된
런타임의 설치를 제안합니다. 기본 선택은 **Not now(나중에)**입니다. 동의하기 전에
대상 경로와 기록할 파일을 확인하세요. 비대화형 설치는 누락된 런타임과 정확한
설치 명령을 포함한 최종 요약을 **stderr**에 출력하므로 stdout은 섞이지 않습니다.
아래의 선택적 플래그는 스킬 대상과 별개로 지정한 런타임의 설치에 명시적으로
동의한다는 뜻입니다.

```bash
python3 -m ham skill-installer install omp --runtime omp
python3 -m ham skill-installer install all --runtime all
```

`--runtime`은 `omp`, `claude`, `all`을 받습니다. 생략하면 런타임을 자동 설치하지
않습니다. 충돌을 보고하되 관련 없는 파일이나 수정된 파일을 교체하지 않습니다.
다시 시작해야 할 세션을 알리고 사용자가 선택한 세션만 다시 시작하세요.
이후 수신기 등록(`extension_present=1`)을 확인하거나 **설치됨, 활성화 대기**로
보고합니다. 플러그인과 스킬 설치만으로 수신 준비가 끝났다고 판단하지 마세요.
파일 소유권은 활성화나 컨텍스트 수신의 증거가 아닙니다. 플러그인 로드는
클라이언트 설정을 쓰지 않습니다. 새 저장소의 기본 `auto` 모드는 유지되며
런타임 설치 동의로 모드를 바꾸지 않습니다.

### 클라이언트별 호출

클라이언트별 호출 방법은 다음과 같습니다.

- **Codex CLI와 IDE 확장**에서는 `$ham`을 입력한 뒤 스킬 선택기에서
  **HAM (Herdr Agent Messenger)**를 선택하세요. `$ham`은 검색어이며 정식 스킬
  이름은 계속 `herdr-agent-messenger`입니다.
- **Claude Code**에서는 자연어 요청으로 스킬을 자동 선택하거나
  `/herdr-agent-messenger`로 명시적으로 호출하세요. `$ham`은 Claude 명령이 아닙니다.
- **omp**에서는 위 경로에 설치한 뒤 자연어로 HAM 작업을 요청하거나
  `skill://herdr-agent-messenger`를 읽도록 지시하세요.

```text
$ham local/yellow-falcon에게 현재 변경을 검토해달라고 요청해.
응답을 기다린 뒤 필요한 후속 작업을 정리해줘.
```

### 과거 Claude 스킬 관찰과 설치 범위 주의사항

아래 내용은 2026-10-16에 winmini의 Claude Code **2.1.301**과 Herdr **0.9.5**에서
관찰한 **이전 프롬프트 전송 방식의 기록**입니다
([이슈 #5](https://github.com/zerodice0/herdr-agent-messenger/issues/5)).
스킬 설치·발견 기록이지 현재 메일함 수신 절차나 native runtime 접수 증거가 아닙니다.

| 확인 항목 | 관찰한 결과 |
| --- | --- |
| 사용자 설치 | 사용자 범위 설치기(현재는 플러그인 루트의 `python3 -m ham skill-installer install claude`)로 설치한 뒤 체크아웃 밖에서 `/herdr-agent-messenger`가 `(user)`로 표시되었습니다. |
| 프로젝트 설치 | **HAM Skill** → **Project / Claude**에서 전체 번들을 복사했습니다. 사용자 설치 전 해당 프로젝트에서는 명령이 `(project)`로 표시되었습니다. |
| 체크아웃 브리지 | 사용자 사본이 없을 때 `.claude/skills/herdr-agent-messenger/SKILL.md`를 따라 정본인 `.agents/skills/herdr-agent-messenger/SKILL.md`를 읽었습니다. |
| 명령 실행 | 사용자·프로젝트·체크아웃 범위 모두 번들 CLI의 `list --host local`이 종료 코드 0으로 실행되었습니다. |
| 과거 새 세션 | 당시에는 생성과 요청을 나눠 실행하여 상관관계가 확인된 완료 응답을 재전송 없이 수집했습니다. 현재 최초 지시에는 이 절차 대신 원자적인 `spawn --message`를 사용합니다. |
| 자연어 선택 | HAM을 언급한 요청과 실제 `local/<label>` 주소만 언급한 요청을 각각 새 세션에서 실행했을 때 `Skill` 도구를 선택했습니다. 사용자 설치 전 체크아웃과 설치 후 체크아웃 밖에서 모두 확인했습니다. |

**같은 이름의 스킬이 있을 때:** 이 Claude Code 버전은 사용자 사본과
프로젝트·체크아웃 사본이 함께 있으면 **사용자 사본**을 선택했습니다.
체크아웃에서 시작했다는 이유만으로 브리지를 읽었다고 판단하지 마세요.
불러온 스킬 경로를 확인하고, 브리지는 같은 이름의 사용자 사본을 설치하기 전에
검증하세요. 설치 상태를 바꾼 뒤에는 클라이언트를 새로 시작하세요.

**대화형 시작:** Claude의 폴더 신뢰 확인이나 첫 실행 UI 질문이
HAM `spawn --kind claude`의 준비 상태 확인을 막을 수 있습니다.
당시 관찰에서는 `agent_start_failed` / `agent_not_ready`와 함께 생성한 탭을
닫았다는 결과가 반환되었습니다. 자동으로 시작하기 전에 해당 디렉터리에서
대화형으로 신뢰 확인을 처리하고, 다시 시도하기 전에 정리 결과를 확인하세요.
출력 모드는 신뢰 확인을 건너뛰므로 `claude -p`의 성공만으로 대화형 시작까지
성공했다고 판단할 수는 없습니다.

**호스트 인증 관찰:** winmini에서는 `claude.ai`의 Max 구독으로 로그인되어
있었습니다. macbook-pro와 mac-studio에서는 비GUI SSH 세션이 macOS 키체인에
접근할 수 없어(`User interaction is not allowed`) `loggedIn: false`가
반환되었습니다. 이 호스트·세션 조건을 스킬 누락이나 Claude 인증 전반의 실패로
해석하지 마세요.

## 메일함 명령과 정체성

작업 중인 에이전트의 프롬프트를 방해하지 않고 메시지를 남기려면 메일함을 사용하세요.
수신 런타임은 자연스러운 사용자 입력 경계에서 메시지를 받을 수 있습니다. 아래
조건을 만족하는 OMP action 메일이나 FYI는 자동으로
턴을 시작할 수 있습니다. 등록된 Herdr 에이전트 pane에서 cwd를 플러그인 루트로 지정해 실행합니다.

```bash
python3 -m ham cli mail send --to local/blue-owl --kind action --body '경계 조건을 검토해 주세요.'
python3 -m ham cli inbox read
python3 -m ham cli inbox handle --message MESSAGE_UUID
python3 -m ham cli mail show --thread THREAD_UUID
```

`mail send`는 `--to local/LABEL`, 등록된 canonical 호스트나 SSH 별칭의 주소,
`--to main` 또는 `--to [HOST/]main@REPO`와 `--body TEXT` / `--body-file PATH` 중 하나를 받습니다.
본문은 비어 있지 않은 UTF-8이며 최대
16 KiB입니다. `--kind fyi|action`의 기본값은 `fyi`이고,
`--subject TEXT`는 새 대화 제목을 정합니다(UTF-8 기준 최대 1024바이트).
`--reply-to UUID`는 기존 대화와 이어지고 hop을 1 늘립니다. 답장자가 수신자인
queued/delivered 원문은 답장 저장과 같은 트랜잭션에서 handled로 바뀝니다.
접수된 로컬 메일은 보통 queued로 기다리지만 원격 사본은 pending일 수 있으며,
철회나 terminal 종료로 claim 없이 최종 상태가 될 수도 있습니다.
어느 종류든 `mail send` 자체가 모델 턴을 시작하지는 않습니다.

로컬 호스트나 등록된 peer에서는 저장소가 달라도 메일을 주고받을 수 있습니다.
별도의 저장소 허용 목록은 없으며 peer 등록이 신뢰 경계입니다. 대화의 시작 저장소
(`threads.repo_id` / `messages.repo_id`)는 바뀌지 않습니다. 스키마 v8의
`messages.sender_repo_id`는 발신 시점의 저장소를,
`deliveries.recipient_repo_id`는 주소를 해석한 수신자의 당시 저장소를 저장합니다.
기존 행은 메시지의 저장소로 두 스냅샷을 채우며 기존 불변 해시는 바꾸지 않습니다.
새 불변 wire payload에는 두 저장소 스냅샷이 모두 들어가고, 라우팅된 전달에도
수신 저장소를 포함합니다. 답장자는 대화에서 메일을 보내거나 받은 참여자여야
하며, 그렇지 않으면 `not_participant`로 실패합니다. 시작 저장소와 같을 필요는 없습니다.

저장소 간 수신과 자동 턴 시작에는 **수신자 저장소**의 설정과 수신자의 역할을
적용합니다. 모드, idle/done 상태, 수신자별 시간당 한도, coalescing, max hop 조건은
그대로입니다. 기존 협업 정책도 바뀌지 않으므로 `use_mailbox`는 같은 저장소의
참여 송수신자에만 적용합니다.

질문의 답이나 결정에 따라 수신자가 행동하거나 답하거나 작업을 이어가야 하면
`--kind action`을, 참고 정보라면 `fyi`를 선택하세요. 조건을 충족하는 action은
신속히 깨우며, FYI는 약 5~10분 뒤 묶어서 참여 중인 유휴 OMP를 깨웁니다. 턴 진행 중
도착한 FYI는 그 턴 직후 전달합니다. 기존 깨우기 안전 조건은 그대로 적용합니다.

전송에 성공해도 참고용 경고가 나올 수 있으며, 기본 종류인 `fyi`는 바뀌지 않습니다.
`recipient_no_receiver`와 경고 조건은
[참고서의 경고 목록](../../.agents/skills/herdr-agent-messenger/reference.md#local-mailbox)을
참고하세요.

`inbox read`는 `--message UUID`, `--all`, `--limit N`, `--verbose`를 받습니다.
기본 한도는 100개이며 1~1000개를 지정할 수 있습니다. 기본 출력은 ID·발신자·본문·상태·
답장 문맥을 유지하며 `--verbose`는 전체 전달 진단 정보를 반환합니다. 조회는 상태·로그·초인종을
바꾸지 않으며, 같은 pane을 쓰는 서브에이전트도 같습니다.
`inbox handle --message UUID`는 여러 번 지정할 수 있고 전체를 검증한 뒤
한꺼번에 handled로 바꿉니다. queued 메시지를 처리해도 delivered 시각을 만들지
않습니다. `mail withdraw --message UUID`는 아직 claim되지 않은 메시지를
철회합니다. claim이나 철회 이외의 최종 결과가 먼저 확정되면 `withdrawn:false`를
반환하지만, 성공한 철회를 반복하면 `withdrawn:true`와 최초 수신 확인·시각을
유지합니다. `mail show --thread UUID`는 대화를, `repo show`는 저장소 설정을 조회합니다.

`mail dismiss --message UUID [--message UUID ...] --reason TEXT`는 이 호스트에서
발신한 `undeliverable` 메일의 발신자 확인 의무를 정리합니다. 공백뿐인 사유는
허용하지 않습니다. 같은 ID는 한 번만 처리하고, 전체 ID를 검증한 뒤 한꺼번에
기록합니다. 권한은 발신 당시 저장소의 원래 terminal을 사용하는 검증된 세션
또는 원래 발신자의 호스트 로컬 역할 그룹에서 현재 활성 상태인 main에만 있습니다.
같은 terminal에서 재시작한 세션도 허용하며 연결된 워크트리는 기존 anchor 역할
규칙을 따릅니다. 같은 저장소의 worker나 다른 그룹의 main에는 권한이 없습니다.
`off` 모드에서는 main의 대리 권한이 없고, 발신자·그룹 증거가 없으면 거부합니다.
v11에서 새로 보내는 메일은 발신 당시 식별 정보와 anchor를 로컬
`messages.sender_context`에 보존하므로 발신 에이전트와 연결된 workspace가
사라져도 원래 anchor의 main이 정리할 수 있습니다. 이 정보는 payload 해시나
원격 envelope에 넣지 않습니다. 기존 v10 메일은 이 정보가 없으므로 남아 있는
발신자·workspace 기록으로 그룹을 증명할 수 있을 때만 main 권한을 허용합니다.
과거 그룹 증거가 사라졌다면 같은 저장소의 main을 임의로 고르지 않고 거부합니다.

재시도해도 최초 감사 기록의 실행자·시각·사유를 바꾸지 않습니다. 본문이 90일 뒤
삭제되어도 감사 기록을 유지하며, 권한을 확인한 재시도는 본문을 복원하지 않고
같은 기록을 반환합니다. 기본·상세 inbox 조회와 대화 조회의 `dismissal`은 정리
전에는 null이고 이후에는 감사 객체입니다. 고정 필드는 `message_id`, `actor_alias`,
`actor_terminal_id`, `reason`, `dismissed_at`입니다. 실제 상태는 `undeliverable`로
유지하며 실패 증거·네이티브 수신 확인·최종 시각·갱신 시각·보존 기한을 바꾸지
않습니다. ACK나 handled 수신 확인, 깨우기, 원격 정리 통지를 만들지 않으며
발신 호스트의 원장에만 반영합니다. 대화의 모든 메일이 실제 handled이거나 정리된
경우에만 Handled 그룹에 표시합니다. 미정리·withdrawn 메일이나 새 답장이 있으면
Active에 남습니다. TUI에는 정리 단축키를 추가하지 않습니다.

### 저장소 이력과 읽기 전용 보드

```text
mail threads [--repo REPO_ID] [--limit N] [--cursor TOKEN]
mail direct-list [--repo REPO_ID] [--limit N] [--cursor TOKEN]
status board [--repo [ID]] [--host local|CANONICAL|REGISTERED_SSH_ALIAS] [--verbose]
```

위의 `python3 "$HAM_CLI"` 뒤에 붙여 실행합니다. 각 명령은 `-h` / `--help`를
지원하며 JSON 객체 하나를 반환합니다. 세 명령 중 보드만 `--verbose`를 지원합니다.
verbose를 포함한 CLI JSON은 불필요한 공백 없이 UTF-8로 출력합니다. 도메인 오류는
`{"error":{"code":"CODE","message":"TEXT"}}`와 종료 코드 1을 사용합니다.
사용법 오류는 종료 코드 2입니다. 조회는 메일 확인, 설정·상태·last_seen·표시·claim
변경을 하지 않으며 사용자 작성·직접 전송·운영자 ACK 우회 명령을 제공하지 않습니다.

이력은 기본적으로 정체성을 검증한 호출자의 저장소를 선택합니다.
`--repo REPO_ID`를 명시하면 Herdr에서 호출자 정체성을 가져오지 않습니다.
`--limit`은 기본 100개이며 1~1000개를 받습니다. 결과는
`{repo_id,threads,next_cursor}` 또는 `{repo_id,submissions,next_cursor}`이고,
`next_cursor:null`이면 마지막 페이지입니다. `mail threads`는 대화의 시작 저장소,
메시지의 발신 저장소 또는 전달의 수신 저장소가 선택한 저장소인 대화를 포함합니다.
양쪽에서 참여 대화를 찾을 수 있으며, 이 로컬 원장에 남아 있는 원격 pending 사본도
포함합니다. `mail direct-list`는 기존처럼 선택한 저장소의 `via=direct` 사용자
입력 이력이며 `spawn_initial`이나 저장소 간 메일 목록이 아닙니다. 두 목록 모두
모든 호스트의 이력을 복제하지는 않습니다. HMAC 서명된 불투명 cursor는 그대로 전달하세요. 원장·쿼리·저장소에
귀속되므로 변조하거나 다른 범위에서 쓰면 `invalid_cursor`로 실패합니다.
`updated_at DESC`, UUID 오름차순의 keyset 페이지이며 OFFSET을 쓰지 않습니다.
고정된 스냅샷이 아니므로 동시 갱신으로 아직 읽지 않은 행이 경계 앞으로 이동할 수
있습니다. 최신 갱신을 보려면 첫 페이지부터 다시 조회하세요.

`status board`는 기본적으로 로컬의 모든 저장소와 등록된 모든 peer를 조회합니다.
값 없는 `--repo`는 호출자의 저장소를, `--repo ID`는 지정한 저장소를 선택합니다.
`--host`는 로컬 또는 등록된 정규 호스트·SSH 별칭 하나로 범위를 좁힙니다.
결과 `{repo_id,agents,hosts}`는 주소·호스트·workspace·repo와 역할·턴·phase·summary·unread·
stale·수신 상태, 기기·서버 개수를 간결하게 표시합니다. 호스트는 중복 에이전트 행 없이
오류·unread·stale와 자원 개수·탐색 오류를 유지합니다. `--verbose`는 전체 레지스트리·
상태·자원 필드와 시각·정체성·배치 정보, `hosts[].agents`를 복원합니다.
`status list --verbose`도 전체 로컬 행을 복원합니다. Python·TUI·peer `status-export`의
전체 구조는 바뀌지 않습니다. 상태 갱신 시각과 레지스트리 관찰 시각은 별개입니다.
간결한 board/list 행의 `repo`는 짧은 저장소 이름이므로 다른 저장소의 대상을 구분할 수 있습니다.
어느 호스트의 상태·표시·claim도 갱신하지 않으며 peer에도 `refresh=false`를 요청합니다.
실패한 peer는
구조화된 오류와 unread `?`를 남깁니다. 이전 행이 없는 CLI는 행을 만들어 내지
않습니다. TUI는 검증된 같은 호스트의 이전 스냅샷을 stale로 유지할 수 있습니다.
기존 `list`와 로컬 `status list`는 더 좁은 탐색·상태 조회로 유지됩니다.
원격 읽기 전용 동작을 보장하려면 peer도 이 읽기 API 버전이어야 합니다.
이전 버전 peer는 `refresh=false`를 무시하고 기존 레지스트리 수집을 할 수 있습니다.
일부 peer 조회에 실패해도 종료 코드는 0이므로 호스트별 `error`, `stale`,
unread를 확인하세요. 알 수 없거나 모호한 `--host`는 `peer_not_configured`와
종료 코드 1을 반환합니다.

### 기기 점유와 자원 표시

`python3 "$HAM_CLI"` 뒤에 다음 명령을 붙입니다.

```text
device claim phone|emu DEVICE [--label TEXT] [--force]
device release [phone|emu|all] [DEVICE]
device list
```

- 실기기(`phone`)나 에뮬레이터(`emu`)를 쓰기 전에 점유하고 사용을 마치면 해제합니다.
  에뮬레이터 번호는 `emulator-N`으로 정규화합니다. `--label`을 생략하면 실기기는
  조회 가능한 Android 모델명 또는 기기 ID를, 에뮬레이터는 번호를 사용합니다.
  Android가 아닌 기기에도 라벨을 명시할 수 있습니다.
- `release`의 기본값은 `all`이며 **자신의 점유만** 모두 해제합니다. 종류와 선택적
  기기 ID로 범위를 좁힐 수 있지만 다른 에이전트의 점유는 해제하지 않습니다.
  해제에는 force 옵션이 없습니다.
- 점유·해제는 Herdr를 갱신하고 terminal lock 안에서 호출자의 안정적인 terminal과
  세션 generation을 검증합니다. 상속된 `HERDR_PANE_ID`만으로는 충분하지 않습니다.
  정체성을 확인하지 못하거나 갱신이 실패하면 변경을 거부합니다.
- 활성 소유자는 현재 호스트·terminal generation의 활성 에이전트입니다. 자기 기기를
  다시 점유하면 라벨을 갱신합니다. 다른 활성 소유자가 있으면 소유자 정보와 함께
  `device_claimed`로 거부합니다. `--force`는 **사용자가 명시적으로 지시했을 때만**
  사용하여 교체합니다. 비활성 소유자나 이전 generation의 점유는 stale이므로
  force 없이 교체할 수 있으며 `replaced_stale:true`를 반환합니다.
  stale 점유는 변경 경로에서만 정리하고 조회에서는 정리하지 않습니다.

명령은 `-h` / `--help`를 지원하며 `--verbose`는 없습니다. JSON 객체 하나를 반환하고
메일함 명령과 같은 도메인·사용법 오류 규칙을 따릅니다.

| 명령 | 결과 |
| --- | --- |
| `device claim` | `{device,replaced,replaced_stale}`이며 `replaced`는 이전 점유 또는 `null`입니다. |
| `device release` | `{released:[...]}`입니다. |
| `device list` | `{devices:[{device_id,kind,label,claimed_at,holder,stale,attached}]}`입니다. |

`holder`에는 `{agent_id,alias,pane_id,terminal_id,generation,active,live}`가 있습니다.
`attached`는 adb 연결 여부입니다. Android 상태가 `device`이면 `true`, 아니면
`false`이며, adb를 사용할 수 없거나 iOS UDID처럼 adb 형식이 아닌 ID이면 `null`입니다.
연결 여부는 점유 소유자의 활성 여부와 별개입니다. `device list`는 읽기 전용이므로
레지스트리 갱신, 토큰 쓰기, stale 정리나 디렉터리 생성을 하지 않습니다.

`status board`는 이 자원 필드를 요청하되 기본 출력에서는 목록을 개수로 표시합니다.
`--verbose`로 읽기 전용 전체 행을 조회할 수 있습니다. Status-export는 wire payload에
`resources:true`가 있을 때만 `servers`와 호스트 `resources`를 포함합니다.
기본 export, 전송 정책 검사와 수신자 조회에서는 서버를 탐색하지 않습니다.

- 에이전트 `devices`: 현재 소유한 유효한 점유 행(`stale:false`)입니다.
- 에이전트 `servers`: `{port,bind,pid,process,cwd,started_at,urls,inherited}` 행입니다.
  Linux와 macOS에서 현재 사용자의 리스너와 Tailscale Serve 경로를 탐색합니다.
  활성 에이전트의 pane으로 소유자를 연결하며 플러그인, adb와 에이전트 런타임의
  리스너는 제외합니다. `inherited:true`는 프로세스가 현재 세션보다 먼저 시작됐다는
  뜻이지 현재 세션이 시작했다는 뜻이 아닙니다.
- 호스트 `resources`: `{servers_unowned:[],dead_serve:[],stale_devices:[],error:null}`입니다.
  `servers_unowned`는 리스너는 있지만 에이전트 소유자가 없는 Serve 프록시 대상,
  `dead_serve`는 대상 리스너가 없는 프록시 경로, `stale_devices`는 stale 점유입니다.
  대상 리스너의 활성 여부를 확인할 때는 다른 사용자의 리스너도 포함합니다.
  탐색 오류는 `resources.error`에 기록합니다.

각 자원 필드는 서로 독립적인 선택 필드입니다. 이전 peer에 `devices`, `servers`,
`resources` 중 어느 필드가 없으면 해당 정보만 알 수 없음(`?`)으로 표시하며,
빈 목록으로 해석하지 않습니다. 보드와 TUI 조회는 레지스트리를 갱신하거나 점유를
정리하거나 토큰을 바꾸지 않습니다. 간단한 개수와 Devices/Servers 상세 표시는
[UI 참고서](ui-reference.md#기기와-서버-표시)를 참고하세요.

HAM의 사이드바 값은 정확히 `phone=📱 <공백으로 연결한 라벨>`과
`emu=🤖 <공백으로 연결한 라벨>`이며 점유가 없으면 해당 키를 지웁니다.
점유·해제와 상태 표시는 강제 교체된 이전 활성 소유자를 포함해 이 값을 맞춥니다.
Herdr 토큰은 source별로 격리되지 않은 pane 단위의 평면 맵입니다.
제품은 기존 기기 JSON을 가져오지 않으며 일회성 이전은 제품 밖에서 수행합니다.
`heavy_slot.py`는 별도 스크립트로 그대로 유지하며 HAM은 무거운 작업 슬롯을 읽거나
표시하지 않습니다.

### 메일함 용어

- **Mailbox / inbox:** 안정적인 수신 terminal 슬롯에 남기는 영속 메일이며 터미널
  입력이 아닙니다. `inbox read`는 현재 호스트·호출자 terminal을 확인하고 전달의
  수신 저장소 스냅샷이 현재 저장소와 일치하는 메일을 조회합니다.
  `queued`, `delivering`, `delivered`를 기본으로 읽습니다. `--all`은 같은 수신자의
  보존된 최종 상태까지 포함하고 `--message UUID`도 최종 메시지를 선택할 수 있습니다.
  조회는 ACK가 아닙니다.
- **Thread:** 첫 메시지의 UUID가 대화 UUID입니다. `--reply-to`는 원문의 대화·제목을
  이어받고 hop을 1 늘립니다. `mail show --thread UUID`는 받은 메일만이 아니라
  송신 기록도 포함한, UUID를 아는 보존 대화를 읽습니다.
- **TUI Threads:** 저장소별 송신·수신 대화와 직접 입력 결과, 호스트 전체의
  pending/failed OUTBOX 작업을 함께 보여 줍니다. 하나의 inbox·채팅이나 원격 이력
  복제본이 아닙니다. **Outbox**는 FIFO 전송 작업(`message`, `receipt`, `setting`,
  `withdraw`; `pending/sent/failed/cancelled`)이며 보낸 메일 폴더가 아닙니다.
- **메시지 상태:** `pending`은 수신자의 확정 결과를 기다리는 사본, `queued`는
  claim 전 대기, `delivering`은 적용 여부가 불확실할 수 있는 예약 claim입니다.
  `delivered`는 네이티브 또는 감사 기록을 남긴 운영자 증거로 적용을 확인한 상태이지
  처리나 이해를 뜻하지 않습니다. `handled`, `withdrawn`, `undeliverable`은 최종
  상태입니다. `confirmation_source=native|operator`를 구분하세요. 직접 입력의
  `submitting/accepted/unknown/failed`는 전송 수락이며 메일함 ACK가 아닙니다.
- **Unread:** 실제로 글을 봤는지가 아니라 전달의 수신 저장소별 `queued` +
  `delivering` 개수입니다. 대화의 시작 저장소를 기준으로 세지 않으며 delivered
  상태로 미처리된 메일은 제외합니다. `fyi`/`action`은 상태가 아니라 종류입니다.

호스트 정체성은 원장에 저장된 canonical 이름과 고정 UUID입니다. 기기 hostname,
`local`, SSH 별칭과 구분하세요. `local`은 현재 원장을, 등록된 이름과 별칭은
검증된 peer를 가리킵니다. 안정적인 terminal ID가 수신 슬롯을 식별하며,
native session 정체성이 generation을 정합니다. pane 이동은 위치만 바꿉니다.

CLI는 프로세스 조상과 Herdr의 공개 `pane process-info`가 반환한 shell PID를
대조해 이동한 terminal을 찾습니다. stdin TTY나 상속한 terminal 변수를 증거로
삼지 않으며, 정체성을 확인할 수 없으면 옛 pane ID로 추측하지 않고 거절합니다.
표시·상태 revision만 바뀌어서는 generation이 증가하지 않습니다. 전달에 저장된
발신 당시 수신 저장소의 새 세션에는 queued 메일이 이어질 수 있습니다. 다른
저장소나 shell이 terminal을 차지하면 `recipient_changed`로 보류합니다. 비교
대상은 발신자나 대화의 시작 저장소가 아니라 수신 저장소 스냅샷입니다. 접수 여부를
모르는 이전 generation의 claim은 자동 재전송하지 않습니다.

Git HTTPS/SSH/scp origin은 host와 경로를 기준으로 정규화합니다. origin 이름을
바꿀 때 정체성을 유지하려면 미리
`git config ham.repo-id 'git:host/stable/repo'`를 지정하세요.
기본 저장 위치는 `${XDG_STATE_HOME:-~/.local/state}/herdr-agent-messenger/ledger`입니다.
테스트에는 `HERDR_AGENT_LEDGER_DIR`를 쓰고, 새 원장의 고정 host 이름은
`HERDR_AGENT_CANONICAL_HOST`로 지정할 수 있습니다.
내부 서비스인 `ledger doctor`, `ledger reconcile`, `ledger event`,
`ledger retention`, `ledger agent-session`, `ledger delivery claim|started|release`의
JSON과 복구·증거 계약은 [참고서](../../.agents/skills/herdr-agent-messenger/reference.md#local-mailbox)에
정리되어 있습니다. 이 명령들은 runtime이나 전역 규칙을 설치하지 않습니다.
기존 prompt 전송과 Held Messages는 비참여 트래픽에 남으며, 조정에 참여하는
같은 저장소의 트래픽에는 아래 메일함 정책이 적용됩니다.

### 원격 메일과 코드 전용 worker

원격 메일을 쓰기 전에 양쪽의 고정 호스트 정체성과 SSH 연결을 등록합니다.

```bash
# alpha에서 실행합니다. beta에서는 --name beta와 alpha의 SSH 별칭을 씁니다.
python3 "$HAM_CLI" ledger setup --name alpha
python3 "$HAM_CLI" ledger peer add --ssh-alias beta-ssh --canonical-name beta
python3 "$HAM_CLI" ledger peer list
python3 "$HAM_CLI" mail send --to beta-ssh/blue-owl --body '준비되면 검토해 주세요.'
python3 "$HAM_CLI" ledger doctor
python3 "$HAM_CLI" ledger outbox
python3 "$HAM_CLI" ledger worker status
# 영구 오류의 원인을 고친 뒤 같은 메시지·항목 ID로 명시적으로 재시도합니다.
python3 "$HAM_CLI" ledger retry --item OUTBOX_ITEM_UUID
```

기존 HAM 허용 목록에 있는 구체적인 SSH 별칭만 받습니다. 원격 root는
구조화된 `result.plugins[0].plugin_root`에서 읽고, protocol 1과 고정 UUID를
검사합니다. 설치된 구버전 checkout을 새 프로토콜 endpoint로 취급하지 않습니다.
등록된 canonical 이름과 SSH 별칭 모두 같은 peer를 가리킵니다.
원격 에이전트 상태는 실시간 조회하며 로컬 AGENTS에 복제하지 않습니다.

라벨은 해당 호스트의 모든 저장소에서 찾습니다. 없으면 `agent_not_found`,
여럿이면 후보 주소와 저장소를 포함한 `agent_ambiguous`로 실패합니다.
v8 메일을 주고받기 전에 참여 호스트를 함께 업그레이드하세요. 정확한 옛 필드만
받는 구버전 수신자는 새 payload를 `invalid_message`로 거부합니다. 메일은
수신자 쪽에서 자동 보류되는 것이 아니라 발신 outbox에 실패 상태로 보존됩니다.
메시지·항목 ID를 유지하고 업그레이드 후 `ledger retry --item OUTBOX_ITEM_UUID`로
수동 재시도하세요. 새 메일로 다시 보내지 마세요. 기존 `recipient_changed` 보류는
발신 저장소가 아니라 수신자의 점유 상태를 기준으로 유지됩니다.

독립적인 코드 전용 worker는 원장별 flock을 잡고 peer별 FIFO 재시도(최대 5분 간격),
수신 확인, 닫힌 terminal 확인과 보존 기간 정리를 처리합니다. 시작한 CLI가
끝나도 유지되며 남은 유지보수 작업이 없으면 종료합니다. Herdr 조회 실패를
terminal 종료로 취급하지 않습니다. 영구 실패 항목은 오류를 보여 주고 다음 항목을
막지 않으며, 한 호스트의 일시적 오류도 다른 목적지를 막지 않습니다.
명시적 재시도는 기존 메시지·항목 ID를 유지합니다. ACK 유실 뒤 동일한 메시지를
재접수하면 현재 상태를 돌려줍니다. 본문을 보존 기간 뒤 삭제해도 중복 방지 정보가
남으므로 같은 ID를 다른 본문에 사용할 수 없습니다. 커밋 뒤 초인종 쓰기가 실패하면
저장은 성공하며 `notification_failed`, `committed:true` 경고가 나옵니다.
새 메시지로 다시 보내지 마세요.

`--to main`은 보낼 때 송신 호스트에서
`(repo_id, 정규 호스트, anchor_workspace_id)`를 기준으로 해석합니다. 연결된 workspace도
같은 역할 그룹의 main을 사용합니다. 변경 불가능한 메시지에는 구체적인 terminal 대상을
저장합니다. 그룹에 main이 없으면 오류로 실패하며 다른 그룹의 main이나 저장소 home으로
대체하지 않습니다. `HOST/LABEL`을 명시한 메일은 workspace와 저장소 경계를 모두
넘을 수 있습니다. 수신 확인과 철회는 저장된 대상을 따릅니다.

`--to [HOST/]main@REPO`는 지정한 호스트에서 저장소의 main을 선택합니다.
HOST의 기본값은 로컬입니다. 예를 들어 `--to main@dxp-api` 또는
`--to beta/main@org/dxp-api`를 사용합니다. 정확한 repo ID가 우선하며, 그 외에는
경로의 끝 구성요소가 유일하게 일치해야 합니다. 여러 저장소가 일치하면
`repo_ambiguous`로 실패합니다. 선택한 호스트에서 서로 다른 역할 그룹을 기준으로
현재 main이 정확히 하나여야 합니다. 없거나 저장소를 찾지 못하면 `main_unresolved`,
main이 있는 그룹이 여럿이면 기준 workspace ID 목록과 함께 `main_ambiguous`로
실패합니다. 원격 선택에는 내보낸 계산된 역할과 선택적 역할 범위 메타데이터를
사용합니다. `main`과 마찬가지로 보낼 때 구체적인 terminal 대상을 저장하며
main을 새로 정하거나 home으로 대체하지 않습니다. main이 여러 개이면 구체적인
`HOST/LABEL`을 선택하세요.
home은 처음 활성화한 canonical 호스트이며 사용자 UI에서 읽기 전용입니다.
workspace main을 선출하거나 중계하지는 않습니다. mode/main/members/thresholds
변경은 사용자 UI 서비스 전용이며 agent CLI에는 `repo set`이나 home 변경 명령이
없습니다. 로컬 auto 초기화는 모든 peer의 접속을 기다리지 않습니다. 동시에 생긴
home 제안은 관찰된 뒤 결정적인 규칙으로 수렴합니다.

격리 검증에는 `peer add --remote-root /ABS/WORK_COPY
--remote-ledger-dir /ABS/PRIVATE/ledger --remote-socket /SHORT/PRIVATE.sock`을
사용합니다. 로컬 원장·소켓·HOME/XDG·기존 request 경로도 모두 격리하세요.
`HERDR_AGENT_LEDGER_AUTOSTART=0`은 암시적인 worker 시작을 막고 명시적인 원격
원장에도 전달합니다. `ledger worker once|run|ensure`는 계속 직접 실행할 수 있습니다.
정확한 JSON·오류·격리 계약은 [영문 참고서](../../.agents/skills/herdr-agent-messenger/reference.md#hosts-fifo-transport-and-worker)에
있습니다. 이 명령들은 runtime이나 전역 설정을 설치하지 않습니다.

### 모드, 역할, 공개 상태

이 버전부터 HAM이 처음 발견한 저장소는 **auto**로 시작합니다. 같은 저장소의 모든
에이전트가 참여하며, 조건에 맞는 대기 중 action 메일은 OMP의 새 턴을 자동으로
시작할 수 있습니다. 같은 저장소의 기존 `send`·`request`·`batch` 대신 메일을
사용해야 합니다. 이미 알려진 저장소는 기존 설정을 유지하며, 저장된 모드가 없으면
변경할 때까지 **off**로 남습니다. 호스트 로컬(`local:`) 저장소에도 같은 기본값을
적용하지만 설정을 전파하지 않습니다. 읽기 전용 명령은 기본값을 저장하지 않습니다.
off에서는 main/worker 역할, 자동 깨우기, 참여 트래픽의 기존 전송 제한이 없습니다.
명시적 조회와 자연스러운 사용자 입력 경계의 수신은 허용합니다.

모드·home·깨우기 임곗값은 저장소 전체에 적용합니다. main과 members는
`(repo_id, 정규 호스트, anchor_workspace_id)`별로 정합니다. 연결된 worktree workspace는
같은 호스트에서 Herdr `worktree.repo_key`가 같고 현재 열려 있는 비연결 workspace를
기준 workspace(anchor)로 삼습니다. 후보가 여럿이면 결정적인 순서로 하나를 선택합니다.
기준 workspace에는 에이전트가 없어도 됩니다. 메타데이터나 열린 기준 workspace가
없으면 자체 범위를 유지하며, git common directory로 일반 workspace를 묶지 않습니다.
서로 다른 호스트의 workspace ID를 같은 범위로 보지 않습니다. 그룹에 속한 동안에는
기준 workspace의 main·members 설정만 적용합니다. 연결된 workspace의 설정은
이동·복사·삭제하지 않고 보존하며, 그룹에서 분리되면 다시 적용합니다.

- **manual:** 해당 범위 안의 활성 에이전트를 명시적 main으로 지정하며, 선택한
  members 중 main을 제외한 에이전트가 worker입니다. 비회원도 자연스러운 경계에서
  수신할 수 있지만 자동으로 깨우지는 않습니다.
- **auto:** 기준 workspace 설정의 활성 명시적 main이 우선하며, 그룹 안의 어느
  workspace에 있어도 됩니다. 없으면 기준 workspace에 활성 에이전트가 있어야
  자동으로 선출합니다. 그룹의 활성 에이전트가 하나이면 그 에이전트를, 여럿이면
  메인 체크아웃의 활성 에이전트가 정확히 하나일 때 그 에이전트를 main으로 정합니다.
  그 외에는 main이 없습니다. 특히 열린 기준 workspace에 에이전트가 없으면 연결된
  에이전트가 하나뿐이어도 자동 main이 없습니다. 나머지 그룹 에이전트는 worker이며,
  그룹에 속하지 않은 workspace는 자체 선출 규칙을 유지합니다.

지정한 에이전트가 비활성화되거나 범위 밖으로 이동하면 설정을 다시 쓰지 않고
조회 시 그 지정을 무시합니다. auto에서는 위의 자동 선출 규칙을 적용합니다.
main·members 설정은 해당 호스트에만 저장하며 복제하지 않습니다. peer는 원래 호스트가
계산한 역할을 받아 봅니다. 프로토콜은 1을 유지하고 역할 범위 내보내기 메타데이터는
선택 사항입니다. 스키마 v9는 에이전트가 없는 기준 workspace를 포함한 열린 workspace의
메타데이터를 추가하며 기존 설정은 이동하지 않습니다. 같은 호스트 원장을 사용하는 HAM
프로세스는 함께 업그레이드하고 다시 시작해야 합니다. 이전 코드는 v9 DB를 열 수 없으며,
다운그레이드에는 백업한 DB가 필요합니다. OMP·Claude 수신 계약은 바뀌지 않습니다.
[현재 마이그레이션·배포 안내](migration.md#worktree-역할-그룹스키마-v9)를 참고하세요.
v7 절은 과거 기록이며 연결된 workspace의 독립 역할 규칙은 v9로 대체됐습니다.

스키마 v10은 기본값이 0인 `deliveries.fyi_turn_end`를 추가합니다. 기존 v9 전달은
보수적으로 표시하지 않습니다. 이전 enqueue 코드가 표시를 누락하지 않도록 원장과
CLI·복사한 스킬을 함께 갱신하세요. 확장 계약은 바뀌지 않으며 이 정책 때문에
확장을 다시 시작할 필요는 없습니다. 다만 [패키지 전환](migration.md#python-패키지-구조)에는 다섯 호스트 모두에서 활성 런타임 재설치와 수신기·hook 세션 및 HAM worker·TUI 재시작이 필요합니다.
[턴 종료 후 FYI 마이그레이션](migration.md#턴-종료-후-fyi스키마-v10)을 참고하세요.
유휴 FYI의 지연 깨우기는 기존 로컬 전달 시각을 사용합니다. 갱신 전에 queued였던
메일도 수신자 호스트의 로컬 enqueue 시각으로부터 300초 뒤 깨우기 대상이 됩니다.
스키마나 설정을 추가하거나 데이터를 마이그레이션할 필요는 없습니다.
300초는 깨우기 대상이 되는 최소 대기 시간이며, 그 시각의 전달을 보장하지 않습니다.
유휴 FYI는 보통 이후의 일반 이벤트나 5분 간격 안전 확인에서 깨우므로 다른 조건을
충족하면 enqueue 후 약 5~10분이 걸립니다.

스키마 v11은 로컬 `mail_dismissals`와 nullable `messages.sender_context`를
추가하며 기존 v10 payload와 설정을 보존합니다. 같은 호스트 원장을 쓰는 HAM 프로세스는 함께 갱신하고 다시 시작해야
합니다. 읽기 전용 열기는 마이그레이션하지 않고, 이전 코드는 v11을 열 수 없습니다.
다운그레이드에는 업그레이드 전 백업이 필요합니다. 프로토콜 1과 수신 런타임 계약은
바뀌지 않습니다. [발신자 정리 마이그레이션](migration.md#발신자-정리스키마-v11)을 참고하세요.

공개 상태는 조정 모드와 별개로 사용할 수 있습니다.

```bash
python3 "$HAM_CLI" status set review --summary '메일함 경계를 검토하고 있습니다'
python3 "$HAM_CLI" status list --repo
python3 "$HAM_CLI" status list --repo 'git:host/stable/repo'
python3 "$HAM_CLI" status clear
python3 "$HAM_CLI" repo show
```

`repo show`는 호출자의 호스트, 기준 workspace인 `workspace_id`, 원래 workspace인
`requested_workspace_id`, 해당 범위의 main·members와 저장소 전체의 모드를 보여 줍니다.
`status board`와 사이드바·테두리는 각 에이전트의 호스트별 역할 그룹에 따른 역할을 표시합니다.

`status set PHASE --summary TEXT`는 호출자의 phase와 summary를 갱신하며 Herdr의
`turn_state`는 바꾸지 않습니다. phase는 `$recap`에 표시합니다. HAM 소유 pane
테두리는 `[메인] alias · summary` 또는 `[워커] alias · summary`로 표시하며,
없는 부분은 생략합니다. 별칭을 요약 앞에 두므로 Herdr가 폭에 맞춰 내용을 자를 때
요약이 먼저 잘립니다. `status clear`는 phase와 summary만 지우고 별칭과 현재
역할은 유지합니다. 사용자가 지정한 기존 테두리는 보존합니다. Agents에서는 역할을
주소로 쓸 별칭 앞에 표시하며, off에서도 역할 접두사 없이 상태 기능이 동작합니다.
`status list`는 저장된 로컬 상태를 나열합니다. 값 없는 `--repo`는 호출자의 저장소를,
`--repo ID`는 해당 저장소를 선택합니다. 상세한 로컬·등록 peer 조회에는
`status board`를 사용하며, `status list`에 기존 상위 명령의 `--host`를 붙여
원격 보드로 대체하지 마세요. `status --address HOST/LABEL`은 수신자 상태 조회로 유지합니다.

### 사용자 TUI: 빠른 전송, 설정, 불확실한 claim 복구

명령 팔레트에서 **HAM: Mailbox (Right Split)**, **HAM: Mailbox (Tab)** 또는
**HAM: Mailbox (quick send)**를 엽니다. 플러그인은 `herdr.agent-messenger`이며
action은 `mailbox-split` / `mailbox-tab` / `mailbox-popup`입니다.
빠른 전송 오버레이는 전송 성공 결과가 오면 닫히고 실패하면 입력창을 유지합니다. 전역
`prefix+m`은 바꾸지 않으므로, 모든 호스트를 갱신한 뒤 원할 때 연결하세요.
기존 메일함은 표시 이름이 아닌 안정적인 pane/terminal ID로 확인합니다.
분할 동작은 등록된 메일함이 현재 탭에 있을 때만 재사용합니다. 다른 탭에 있으면
오른쪽에 새 메일함을 열고 등록을 교체하며, 이전 메일함은 화면 첫 줄에 `NAV ·`가
있을 때만 닫습니다. 탭 동작은 기존처럼 다른 탭에 있는 메일함도 재사용합니다.
`q`는 플러그인 소유 화면만 닫으며 내용이 있는 초안은 폐기 여부를 확인합니다.
최상위의 Esc는 빠른 전송 오버레이만 닫고, 입력창이나 대화상자에서는 빠져나옵니다.
빠른 전송, 메신저, Held Messages, HAM Skill은 Herdr의 임시 확대 오버레이를 사용하고
닫으면 이전 포커스와 확대 상태를 복원합니다. HAM은 너비·높이를 지정하거나 레이아웃을
조회하지 않습니다. 최소 버전은 Herdr 0.8.0이며 기존 `mailbox-popup` ID를 유지합니다.
[배치 선택 이유와 원본 근거](ui-reference.md#herdr-오버레이-배치)를 참고하세요.

Agents, Threads, 읽기 전용 Detail 영역은 화면 크기에 맞춰 배치합니다.
80열 미만이거나 18행 미만이면 포커스가 있는 영역만 표시하며, 80~139열이면
목록을 위아래로 쌓고 Detail을 옆에 배치합니다. 140열 이상이면 세 열로 표시합니다.
32열 미만이거나 10행 미만이면 크기 변경 안내를 표시하되 초안, 커서, 모드와
선택 항목은 보존합니다. NAV에서 Tab·Shift+Tab, ←·→ 또는 h/l로 포커스를 옮기고
↑·↓ 또는 j/k로 행을 고릅니다. PgUp·PgDn·Home·End로 페이지를 넘기거나
처음·끝으로 이동합니다. Detail에서는 방향키로 스크롤합니다. 숫자는 탐색 키가
아닙니다. `s`는 Settings, `?`는 도움말, `e`는 Detail의 진단 정보를 열고
`r`은 새로고침합니다. Detail에서는 Enter가 동작하지 않습니다.

Agents는 호스트별로 묶고 헤더에 stale·오류 상태를 표시합니다. 별칭, 턴 상태,
unread와 native 수신 준비 상태 아래에 흐린 역할·phase·요약 한 줄을 표시합니다.
stale 호스트는 마지막 조회 결과를 유지하고 unread를 `?`로 표시하며 종료됐다고
판단하지 않습니다. Detail은 마지막으로 포커스를 둔 목록의 선택 항목을 자동으로
보여 줍니다. 전체 필드와 상태 갱신·관찰 시각을 현지 시간과 경과 시간으로
구분합니다. Threads에는 메시지 이력, 직접 입력 결과와 대기·실패한 OUTBOX가
포함됩니다. **읽기 전용 조회만으로 ACK·전달·처리·복구가 일어나지 않습니다.**

Settings에서 **수정할 수 있는 행은 일곱 개**입니다.

| 행 | 값과 기본값 |
| --- | --- |
| `mode` | `off`, `manual`, `auto`이며 새 저장소는 `auto`, 모드가 없는 기존 저장소는 `off`로 시작합니다. |
| `main` | `{"host":"canonical","terminal_id":"term_…"}` 또는 `null`입니다. |
| `members` | 위 target의 중복 없는 배열이며 기본값은 빈 배열입니다. |
| `idle_minutes` | 기본값은 3분입니다. |
| `coalesce_seconds` | 기본값은 30초입니다. |
| `wakes_per_hour` | 기본값은 시간당 6회입니다. |
| `max_hop` | 기본값은 10 hop입니다. |

Mode와 네 임곗값은 저장소 전체에 적용합니다. Main·Members 행에는 Mailbox를
연 pane의 호스트·workspace 범위가 표시됩니다(`Main (workspace …)` /
`Members (workspace …)`). 이 범위는 `HERDR_HAM_OPENER_PANE_ID`로 찾으며,
선택한 원격 에이전트를 따라 바뀌지 않습니다. 모든 저장소 보기에서도 기존
저장소 선택 방식을 유지하며, 저장소를 하나로 정할 수 없으면 설명과 함께 편집을 비활성화합니다.

↑·↓로 행을 고르고 Enter로 편집하며 Esc로 닫습니다. `Target: host/alias`는
선택한 Agents 행을 가리킵니다. 해당 에이전트가 활성 상태이며 설정 범위 안에
있을 때만 `m`으로 main을 지정하고 Space로 manual 구성원에 넣거나 뺄 수 있습니다.
서비스도 범위 밖의 대상을 거절합니다. 모든 변경은 기본 선택이 Cancel인 확인창을 거칩니다.

네 임곗값은 각각 0~10000의 정수이며 저장소 전체에 적용하는 하나의 version을
가진 `thresholds` 필드로 저장합니다. home, computed main, version/origin stamp,
충돌과 전파 대기는 모두 **읽기 전용**이며 추가 편집 항목이 아닙니다.
복제하는 저장소 단위 설정은 `(version, canonical origin)`이 큰 값을 채택하며,
version이 같으면 canonical 이름의 사전식 순서가 큰 쪽이 이깁니다.
workspace main·members 변경은 로컬에만 저장합니다. 새 편집에는 관찰된 값보다
새 version을 부여합니다. 충돌 내용은 줄바꿈하여 빠짐없이 표시합니다.

저장은 한 번에 하나만 진행하며 pending 상태를 보여 줍니다. 저장 중에도 텍스트
편집기는 열 수 있지만, 다음 저장 제출이나 main/member 토글은 대기열에 넣지 않고
거절하며 편집 버퍼는 보존합니다. 화면 이동과 닫기는 계속 가능합니다. 결과를
저장소·호스트·workspace 전체 범위와 대조해 즉시 적용하고 오래된 snapshot이
덮어쓰지 못하게 합니다. peer가 오프라인이면 저장소 단위 설정의 원격 전파는
pending으로 남을 수 있지만 workspace 설정은 전파하지 않습니다. 로컬 저장은
모든 peer의 접속을 기다리지 않습니다.

Agents에서 Enter를 누르면 인라인 메일 입력창을 엽니다. Threads의 Enter는 최신
메시지에 답장하며, 호스트·terminal ID로 현재의 stale이 아닌 에이전트를 찾습니다.
stale이거나 찾을 수 없는 수신자는 차단합니다. native 수신기가 없으면 경고하지만
메일은 보낼 수 있습니다. UI 메일은 `sender=user`이며 제목은 없습니다. 답장이나
보존한 초안을 다시 여는 경우를 포함해 새 입력창은 매번 `action`으로 초기화합니다.
Ctrl+G로 해당 입력창의 `action` / `fyi`를 전환하고 선택한 종류를 명시해 전송합니다.
CLI 기본값은 계속 `fyi`입니다. Enter는 별도 확인 없이 즉시 전송하며 빈 본문은 거절합니다.
Shift+Enter는 메일함이 요청한 kitty 키보드 프로토콜로 줄을 바꿉니다.
호환 터미널에서는 herdr를 통해서도 동작합니다. Moshi/mosh는 이 프로토콜을
협상하지 못하므로 Alt+Enter를 사용하세요. Ctrl+J는 줄바꿈 키가 아닙니다.
`HAM_MAILBOX_NO_KITTY=1`로 프로토콜 요청을 끌 수 있습니다. q, ?, 숫자 등 인쇄
가능한 키는 본문에 입력합니다. 방향키·Home·End·Backspace·Delete로 편집하며
상하 방향키로 줄을 옮깁니다. Esc는 수신자·답장별 본문과 커서를 보존하고,
Ctrl+R은 입력창을 유지한 채 새로고침합니다. 입력 중 F1은 도움말을 열며 닫으면
같은 입력창으로 돌아옵니다. 성공하면 제출 후 바뀌지 않은 초안만 지우고 입력창을
닫습니다. 전송 결과를 기다리며 새로 편집한 초안은 보존합니다.

Ctrl+D는 직접 입력으로 전환하거나 되돌립니다. Enter를 누르면 기본 선택이
Cancel인 확인창을 엽니다. Tab 또는 ←·→로 버튼을 고르고 Enter로 실행합니다.
**직접 입력은 수신자의 작업을 방해할 수 있습니다.** `Message from user (HAM):`
헤더와 별도의 accepted/unknown/failed 전송 접수 기록을 사용하며 native 전달
수신 확인이 아닙니다. 불확실한 결과는 다시 보내기 전에 확인하세요.

Threads의 `w`는 로컬에서 보낸 철회 가능한 메일을 철회하고 `t`는 기존 OUTBOX
ID로 재시도합니다. 모두 정확한 대상의 확인을 거칩니다. `delivering`은
**UNCERTAIN**입니다. `a`·`n`은 운영자가 applied·not-applied를 판정하는 기능입니다.
먼저 비어 있지 않은 증거를 입력한 뒤 정확한 메시지·claim ID, 이전 에이전트,
generation과 현재 상태를 확인합니다. terminal에 새 세션이 있어도 이전 claim이
대상입니다. 기본 선택은 Cancel이며 Tab 또는 ←·→로 선택하고 Enter로 실행하며
Esc로 취소합니다. applied는 이전 claim에 적용 사실을 남기고 not-applied는 해당
claim만 해제하며 다시 제출하지 않습니다. 이는 **운영자** 감사 기록이지 native
request/journal 증거, 모델의 이해나 작업 처리 완료가 아닙니다. 적용 여부가
불확실하면 자동 재실행하거나 대체 메시지를 보내지 마세요.

### 클라이언트 수신과 증거의 한계

**OMP:** 선택적으로 설치하는 수신 확장은 Herdr의 root 세션과 지원되는 native
session 파일에서만 동작하며 원장 작업은 Python에 맡깁니다. 지원되는 TUI에서
자동 깨우기를 하려면 편집기가 비어 있고 `idle_minutes` 동안 입력이 없어야 합니다.
Herdr의 `turn_state`가 `idle` 또는 `done`이고 OMP 런타임이 유휴 상태이며 pending 메시지, `willContinue`, retry hold가 없고, 참여 역할과
적격 action 또는 FYI가 있으며 병합·시간당 횟수·hop 제한도 만족해야 합니다. enqueue 직전
같은 tick에서 조건을 다시 확인합니다. 에이전트 발신의 `nextTurn` + `triggerTurn`을
사용하며 steer하지 않습니다. 포커스 자체는 이 깨우기를 막지 않으므로 기존
focused-pane 보류와 구분하세요.

OMP 등록의 `native_session_pending`은 Herdr의 native 세션 보고가 아직 도착하지
않았다는 뜻입니다. 수신기는 정해진 재시도 횟수 안에서 대기 표시를 하고, 등록에
성공하면 자체 상태 표시를 지웁니다. 재시도를 소진하면 사용 불가 상태를 표시합니다.
HAM이 경로를 대신 보고하지 않으며, 이미 보고된 경로와 다르면 계속
`stale_session`으로 거부합니다.

조건을 충족하는 action은 신속히 깨웁니다. queued FYI는 수신자 호스트의 로컬
enqueue 시각으로부터 300초 뒤 깨우기 대상이 되지만 그 순간의 깨우기를 보장하지
않습니다. 일반 이벤트나 5분 간격 안전 확인을 통해 보통 약 5~10분 뒤 묶어서 유휴
깨우기를 합니다. 기존 queued 메일도 발신자의 메시지 생성 시각이 아닌 로컬 전달
시각을 기준으로 합니다.
턴 진행 중 도착한 FYI는 기존 깨우기 조건에 따라 그 턴 직후 전달합니다.
로컬 enqueue 시점에 로컬 조정으로 갱신한 `agents.last_seen`이 15초 이내이고
상태가 `working`일 때만 `delivery.fyi_turn_end`를 기록하여 300초 지연을
생략합니다. 나중에 상태가 바뀌어도 표시하지 않습니다. 참여 여부와 유휴 상태를
비롯한 모든 깨우기 조건은 그대로 적용합니다. 이 표시는 로컬 전달 메타데이터이며
메일 종류나 wire payload를 바꾸지 않습니다.
자연스러운 사용자 turn에서는 off나 manual 비회원도 두 종류를 모두 수신합니다.
action의 깨우기는 아직 300초가 지나지 않은 FYI도 같은 묶음에 포함할 수 있습니다.
묶음은 최대 8개 메시지/8 KiB이며 각 본문은 2 KiB로 제한하고, 잘린 부분에는
정확한 메시지 ID로 읽는 지침을 넣습니다. FYI만 있는 묶음에는 읽고
작업을 계속하되, 작업에 영향을 주는 정보일 때만 답하라는 지침을 넣습니다.
`fs.watch`로 로컬 초인종을 관찰하며 5분마다 코드 전용 안전 확인을 합니다.
일반 FYI는 최소 대기 시간이 지나기 전까지 `inspect.has_action=false`이므로
정확한 시각에 맞춘 타이머나 지연 중 초안·상태의 1초 간격 재확인을 예약하지 않습니다.
대상이 된 뒤에도 기존 깨우기 조건은 모두 적용합니다. ACK는 같은 세션의 활성 분기
journal에 정확한 claim 내용과 해시가 있다는 뜻이지, provider 요청 포함, 모델의
이해, handled를 뜻하지 않습니다.

**Claude Code:** 선택적 수신은 실행 범위가 제한된 **SessionStart**와
**UserPromptSubmit** hook만 사용합니다. Stop hook, idle 깨우기, Claude turn을
시작하는 worker는 없습니다. Herdr 밖에서는 입력을 읽거나 플러그인을 찾거나
원장을 열기 전에 반환합니다. 활성화 환경 변수는 정체성 증거가 아닙니다.
foreground root의 프로세스 조상, native transcript UUID와 현재 generation을
검증해야 합니다. 확인하지 못한 정체성은 메일을 queued로 두고 불명 claim은 보류합니다.

hook의 stdout/종료 코드 0, attachment, `additionalContext` 출력만으로 delivered를
기록하지 않습니다. native 확인에는 두 가지 증거 경로가 있습니다.

- SDK system-blob 경로는 같은 세션의 native request blob에 정확한 묶음이 있고
  새로운 `api-request.tail`과 연결되어야 합니다(`claude:api-request:`).
- 일반 TUI의 typed observer 경로는 raw/rendered native hook 기록이 정확한
  claim 바이트·해시와 일치하고, 인과관계가 확인된 native 요청 증거가 있어야
  합니다(`claude:accepted-journal:`). 이는 **호스트 문맥에 적용됐다는 증거**이지
  정확한 provider 전송 바이트, provider의 증명, 모델 자기보고나 이해의 증거가 아닙니다.

두 경로 모두 `confirmation_source=native`로 기록하며 운영자 판정은 계속
`operator`로 구분합니다. 허가된 저장 기록
`functional-live-b57ae929e8a7/result.json`은 native Claude 2.1.301 실행 파일을
고정한 상태에서 21.69초에 완료됐습니다. 실제 production SessionStart 문맥과
현재 generation의 worker를 통해 `delivered`, `via=hook`, native
`claude:accepted-journal:` ACK가 영구 기록됐습니다. 같은 살아 있는 등록 프로세스에서
manual-worker와 auto-main을 각각 6.02초간 10회 관찰했습니다. 코드 worker는
계속 동작하고 action과 FYI는 queued로 남았으며 추가 primary turn이나 claim은
발생하지 않았습니다.

이 실행은 허가된 무해한 프롬프트 한 번과 primary root 요청 한 번을 사용했습니다.
보조 `quota_check` / `prompt_suggestion` 트래픽은 별도로 구분했으므로 모든
provider 트래픽이 0이었다는 주장이 아닙니다. 메일에만 있던 nonce를 반환한 것은
별도의 기능 응답 확인이지 ACK의 근거가 아닙니다. Herdr는 이미 세션 UUID를 알고
있었습니다. cold-unreported binding CAS는 단위 검증 증거만 있으며 **실제 실행
증거는 아닙니다.** 원래 종료된 사례의 claim은 보류 상태를 유지하며 소급해서
ACK하지 않습니다. 관찰한 호스트 문맥 적용은 정확한 provider 전송 바이트의
증명이나 runtime/provider 전체 성공, 배포 완료를 뜻하지 않습니다.
provider/세션 한도 실패도 별도입니다. 실제 OMP와 물리적인 호스트 간 통합 검증도 진행
중입니다. 이 설명을 전체 단계 PASS나 배포 완료 주장으로 해석하지 마세요.

**실제 Claude 검증의 안전 수칙:** 실제 HOME과 인증을 유지한 채
`XDG_DATA_HOME`만 분리해도 네이티브 자동 갱신은 격리되지 않습니다. 자동 갱신이
전역 `~/.local/bin/claude` 별칭을 폐기할 작업 스토리지 안으로 바꾼 사례가
있습니다. **이름이 `claude`인 전용 심볼릭 링크**를 만들고 미리 검증한 버전별
실행 파일의 절대 경로와 원래 inode에 고정해 실행하세요. 수락된 실행은
2.1.301을 사용했습니다. 숫자 이름 `2.1.301`로 직접 실행하면 Herdr의 클라이언트
감지에 실패했으므로 메타데이터를 꾸미거나 `exec -a`로 숨기지 마세요.
실행 전에 문서화된 `DISABLE_AUTOUPDATER=1`을 해당 프로세스에만 설정하세요.
전역 별칭의 `lstat`, 소유자, 링크 대상과 inode를 기준값으로 기록하고 종료 후
바뀌지 않았는지 확인하세요. 기존 HOME과 인증만 사용하며 인증 정보는 복사하지
마세요. 변경됐다면 증거를 보존하고 보고하며 관련 없는 상태를 복구하지 마세요.
별도로 허가받은 복구도 정확히 소유한 별칭 대상만 비교 후 교환해야 합니다.
관찰된 자동 갱신 부작용은 허가받은 범위에서 원래 2.1.301 실행 경로로 원자적으로
복구했고, 전용 2.1.302 데이터는 보존했으며, 수락된 실행 후 별칭은 바뀌지 않았습니다.
실행 파일과 버전도 기록하세요. SDK와 TUI 2.1.301은 서로 다른 네이티브 증거
형식을 사용하며 stdout만으로는 claim을 ACK하지 않습니다.

**Codex:** 작업 경계에서 스킬로 `inbox read`를 실행하고 관련 작업을 한 뒤
`inbox handle --message UUID`로 명시적으로 처리하세요. 모든 클라이언트와
서브에이전트의 조회는 읽기 전용입니다. queued → handled에는 delivered 시각을
만들지 않으며, 자신에게 온 원문에 답장하면 원문을 원자적으로 handled로 바꿉니다.
Codex 자동 수신/깨우기 adapter는 없으므로 조회를 native delivery로 보고하지 마세요.

### 호스트별 runtime 설치와 안전한 배포 순서

**스킬 설치**는 클라이언트 지침을 복사합니다. 수신 런타임은 대화형 설치기,
`--runtime omp|claude|all` 플래그 또는 아래 명령을 통해 별도로 명시적 동의를
받아 설치합니다. 스킬 설치는 조정 모드나 전역 바인딩을 바꾸지 않습니다.
각 호스트에서 읽기 전용 요약을 확인하고 실제로 쓰는 클라이언트만 선택하세요.

```bash
python3 "$HAM_CLI" runtime status
python3 "$HAM_CLI" runtime install omp
python3 "$HAM_CLI" runtime status omp
python3 "$HAM_CLI" runtime install claude
python3 "$HAM_CLI" runtime status claude
# 나중에 선택한 HAM 소유 runtime만 제거합니다.
python3 "$HAM_CLI" runtime uninstall omp
python3 "$HAM_CLI" runtime uninstall claude
```

클라이언트를 지정한 세 작업 모두 `--destination DIR`을 받지만, 클라이언트를
생략한 요약은 받지 않습니다. OMP의 기본 경로는
`~/.omp/agent/extensions`이며 `ham-mailbox.ts`와 `ham-mailbox.install.json`만
소유합니다. 기존 Herdr 확장은 건드리지 않습니다. Claude는 `CLAUDE_CONFIG_DIR`,
없으면 `~/.claude`를 사용하며 `hooks/ham-mailbox.py`, manifest, `settings.json`의
정확한 HAM SessionStart/UserPromptSubmit 항목만 소유합니다. 설치는 이 항목만
병합하고 제거는 소유한 항목만 삭제하여 관련 없는 hook과 설정을 보존합니다.
소유권·checksum 검사는 관련 없는 파일, 수정된 파일, 관리 대상의 심볼릭 링크를
거절합니다. 충돌을 우회하려고 다른 통합을 지우지 마세요. 선택한 세션만
재시작하거나 reload하세요. 파일이 설치됐다는 상태는 현재 수신 세션의 준비 완료
증거가 아닙니다.

참여할 각 호스트에 다음 순서로 배포합니다.

1. 프로토콜을 지원하는 **플러그인을 먼저** 갱신합니다. 새 peer를 구버전 설치
   endpoint로 연결하지 마세요.
2. 일치하는 스킬과 선택한 수신 runtime을 설치·갱신하고, 참여 정책을 켜기 전에
   해당 수신자의 정체성과 준비 상태를 확인합니다. Codex는 없는 runtime 대신
   명시적 조회·처리 절차를 사용합니다.
3. 사용자 TUI에서 원하는 저장소 모드/main/members를 활성화합니다. 사용자가
   선택한 전역 action 바인딩과 상태/recap 지침 이전은 마지막에 합니다.
   이는 배포 지침이지 실제로 수행했다는 보고가 아닙니다.

저장소 상태 디렉터리 권한은 0700, SQLite DB는 0600이며 WAL을 사용합니다.
최종 상태 메시지의 본문은 90일 뒤, wake/status 로그는 30일 뒤 만료합니다.
worker는 락을 잡고 실행을 시작하면 단조시계의 시작값과 무관하게 즉시 정리합니다.
한 번만 실행하거나 할 일이 없어 바로 종료하는 경우도 같으며, 계속 실행 중일 때는
한 시간마다 다시 정리합니다.
중복 방지 해시, 간결한 수신 확인, 철회 tombstone은 남깁니다. UI는 로컬 초인종을
1초마다, 원격 실시간 상태를 30초마다 독립적으로 확인합니다. 이 확인과 worker,
OMP 안전 확인은 모델을 호출하지 않는 코드 작업입니다. DB 복제본, ASR router,
urgent 채널, 소유권 프레임워크는 없습니다.

### 참여 정책과 기존 프롬프트 전송의 구분

같은 저장소에서 **송신자와 수신자 모두** manual/auto 조정에 참여하면 기존
`send`, `request`, `batch`는 `use_mailbox`로 거절합니다. 대신 `mail send`를
사용하세요. batch는 **모든 대상**의 조회와 정책을 제출 전에 확인합니다.
하나라도 거절되거나 조회에 실패하면 제출은 0건입니다. 이는 실수 방지 정책이지
보안 경계가 아닙니다.

off, 다른 저장소, 그 밖의 비참여 트래픽에는 아래의 기존 prompt·보류·수명 주기
기능이 남습니다. 사용자 직접 입력은 별도의 명시적 경로이며 에이전트의 정책 우회
수단이 아닙니다. 새 세션의 최초 지시는 기존 request가 아니라 원자적 spawn으로
전달하세요.



## 주소 선택과 새 세션 생성

항상 호스트가 포함된 정확한 Agent Labels 주소를 사용하세요. 리터럴 호스트
`local`만 현재 Herdr 서버를 뜻합니다. 나머지 호스트는 모두 설정된 SSH 파일의
명시적인 `Host` 별칭이어야 합니다. 현재 기기 이름과 같더라도 `local`이 아니면
SSH로 처리합니다. 라벨만 사용하면 다른 호스트와 충돌하거나 AI 클라이언트 자체
서브에이전트로 오인될 수 있습니다.

아래 기존 프롬프트 예시는 참여 정책이 허용하는 경우에만 적용됩니다. 기존
에이전트에는 `list`로 확인한 `address`를 그대로 전달합니다. 저장소 또는 설치된
플러그인 루트에서 `HAM_CLI`를 절대 경로로 설정한 뒤 실행하고, 작업 디렉터리를
바꾸더라도 같은 경로를 사용하세요.

```bash
HAM_CLI="$PWD/.agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py"
python3 "$HAM_CLI" list --host local
python3 "$HAM_CLI" list --host macbook-pro
python3 "$HAM_CLI" status --address macbook-pro/purple-koala
python3 "$HAM_CLI" request --address macbook-pro/purple-koala \
  --message '현재 작업을 검토하고 막힌 부분을 알려주세요.' --timeout 120000
```

`list` 결과의 `address`는 `status`, `send`, `read`, `request`의 `--address`에
바로 사용할 수 있고, 배치에서는 `{"address":"host/label","message":"..."}`로
사용할 수 있습니다. 호스트나 라벨이 없거나 모호하면 그 사실을 보고하세요.
라벨을 추측하거나 다른 에이전트로 대체하지 마세요. 누락된 라벨을 찾았다는 이유만으로
새 세션을 만들지 말고, 사용자가 생성을 요청한 경우에만 만드세요.

사용자가 새 에이전트 세션을 열어서 지시를 보내라고 요청하면 한 번의
`spawn --message TEXT` 또는 `spawn --message-file PATH`를 사용하세요. 준비 완료와
최초 제출을 하나의 영구 operation으로 관리하므로 별도의 기존 `request`를 이어서
실행하지 마세요. 직접 `herdr agent start <name>`을 실행하면 HAM의 라벨 선택과
메타데이터 처리를 우회하므로 대안이 아닙니다.

```bash
python3 "$HAM_CLI" spawn --kind omp --cwd /path/to/project --placement tab \
  --message '이 프로젝트의 현재 변경을 검토하고 결과를 알려주세요.' \
  -- --model anthropic/claude-haiku-5-5
```

`--message`와 `--message-file`은 선택 사항이며 동시에 지정할 수 없습니다.
파일은 원격 세션을 만들 때도 호출자 로컬의 UTF-8 파일입니다. 둘 다 생략하면
지시 없이 세션만 시작합니다. 최초 제출 전 target/body와 submitting 상태를
영구 저장하며, unknown이면 이미 제출했을 수 있습니다. operation ID와 남은
배치를 보존하여 확인하고, 다시 보내거나 대체 세션을 만들지 마세요.
입력 접수는 native 메일함 delivery나 완료 응답이 아닙니다.
`spawn`은 사용 중이지 않은 HAM `color-animal` 라벨을 선택하고 색상 메타데이터를
보고합니다. `--label`로 명시한 이름을 사용할 수도 있습니다.

GUI의 코디네이터 지시에는 절대 CLI 경로와 호스트가 포함된 주소가 제공됩니다.
라벨이 없는 수신자 등을 위해 불투명한 `--route` token이 제공되었다면 해당 명령을
그대로 사용하세요. token을 해독하거나 수정하거나 추측한 `herdr` 명령으로 바꾸지
마세요. 기본 출력은 경로 대신 주소를 표시하며, `--verbose`를 사용하면 후속 GUI
라우팅에 필요한 `route`도 확인할 수 있습니다. `route_refreshed: true`이면 반환된
최신 token을 사용하세요. 각 작업은 현재 점유자를 다시 확인하므로 같은 라벨이
재시작 후에도 같은 세션이라고 가정하지 않습니다. 자세한 연속성 규칙은
[SSH 및 라우팅](ssh-and-routing.md)을 참고하세요.

## CLI 전체 참고서

현재 명령에는 `mail`, `inbox`, `repo`, `ledger`, `runtime`, status 작업과 기존
라우팅 명령이 포함됩니다. 모든 명령은 `-h` 또는 `--help`를 지원합니다. 전체
옵션·기본값·단위·범위는 정본 [reference.md](../../.agents/skills/herdr-agent-messenger/reference.md#command-and-flag-reference)를
참고하세요. 아래 표는 남겨 둔 기존 라우팅 명령의 상세 설명입니다.
정책이 허용하는 기존 단일 프롬프트에는 하위 수준의 `send`보다 `request`를
권장합니다. 기존 여덟 라우팅 명령은 기본적으로 간결한 결과를 반환하며
`--verbose`로 전체 진단 정보를 반환합니다. 일반 응답에서는 TUI 테두리,
상태 표시줄, 다시 출력된 프롬프트를 제거합니다.

### 공통 수신자 옵션

기존 수신자 조회/프롬프트 형태의 `status`, `send`, `read`, `request`에 적용됩니다.

| 옵션 | 기본값과 규칙 |
| --- | --- |
| `--host HOST` | 기본값은 `local`입니다. 원격 호스트는 설정된 명시적 SSH 별칭이어야 합니다. |
| `--address HOST/LABEL` | `list` 또는 `spawn`이 반환한 정확한 주소를 지정합니다. 기본값은 없습니다. |
| `--label LABEL` | `--host`에서 현재 사용 중인 정확한 Agent Labels 값을 지정합니다. 기본값은 없습니다. |
| `--route TOKEN` | HAM이 반환한 불투명한 route token을 지정합니다. 기본값은 없습니다. token에 포함된 호스트로 연결합니다. |

`--address`, `--label`, `--route` 중 정확히 하나가 필수이며 함께 지정할 수
없습니다. `--address`를 사용하면 주소의 호스트가 적용됩니다. 기본값이 아닌
`--host`까지 지정했는데 주소의 호스트와 다르면 `conflicting_address`가 발생합니다.

### `list`

```bash
python3 "$HAM_CLI" list --host local
```

| 옵션 | 기본값과 규칙 |
| --- | --- |
| `--host HOST` | 기본값은 `local`입니다. 지정한 호스트의 현재 라벨이 있는 에이전트를 나열합니다. |
| `--verbose` | 기본적으로 꺼져 있습니다. 전체 AgentRecord 호환 정보를 포함합니다. |
| `--legacy` | `--verbose`와 동일하게 동작하는 기존 소비자용 별칭입니다. 기본적으로 꺼져 있습니다. |

기본 결과는 `{"host":"local","agents":[...]}`이며 각 항목은 `address`,
`status`, `workspace`를 포함하는 간결한 정보입니다. 전체 정보가 필요할 때만
`--verbose`를 사용하세요.

### `status`

```bash
python3 "$HAM_CLI" status --address local/yellow-falcon
python3 "$HAM_CLI" status --route ROUTE_TOKEN --verbose
```

공통 수신자 옵션 `--host`, `--address`, `--label`, `--route`를 받습니다.
호스트 전체를 다시 나열하지 않고 선택한 수신자의 상태를 확인합니다.

| 옵션 | 기본값과 규칙 |
| --- | --- |
| `--verbose` | 기본적으로 꺼져 있습니다. 전체 에이전트 정보와 라우팅 메타데이터를 반환합니다. |

기본 결과에는 `address`와 `status`가 있고, 오래된 상태이면 `stale: true`가
추가됩니다.

### `send`

```bash
python3 "$HAM_CLI" send --address local/yellow-falcon \
  --message '현재 상태를 알려주세요.' --no-wait
```

공통 수신자 옵션 `--host`, `--address`, `--label`, `--route`를 받습니다.

| 옵션 | 기본값과 규칙 |
| --- | --- |
| `--message TEXT` | 필수입니다. 공백만 있는 메시지는 허용하지 않습니다. |
| `--wait` | 기본값입니다. Herdr의 완료 신호를 기다립니다. |
| `--no-wait` | 접수 후 완료를 기다리지 않고 반환합니다. `--wait`와 함께 사용할 수 없습니다. |
| `--timeout MS` | 기본값은 `120000`밀리초이며 0보다 큰 정수여야 합니다. `--wait`의 대기 제한입니다. |
| `--defer-timeout MS` | 기본값은 `180000`밀리초, 즉 3분입니다. 포커스 때문에 보류한 메시지를 사용자 결정 대기로 전환할 때까지의 시간이며 `--timeout`과 별개입니다. |
| `--verbose` | 기본적으로 꺼져 있습니다. 전체 전송 결과와 진단 정보를 반환합니다. |

즉시 전송한 기본 결과에는 `sent`, `address`, `waited`가 있고, 경고가 있을 때만
`warnings`가 포함됩니다. `--verbose`에서는 `recipient`, `route_refreshed`,
`route` 등의 전체 정보를 확인할 수 있습니다. 수신자가 이미 작업 중이면
`recipient_already_working` 경고가 있으며, 전체 결과의
`wait_can_track_submitted_turn`이 `false`일 수 있습니다. 기존 작업의 완료 신호일
수 있으므로 안전한 요청 추적에는 `request`를 사용하세요. 즉시 전송한 `send`는
수명 주기 ID나 응답 본문을 제공하지 않습니다. 반면 수신자 pane에 포커스가 있으면
`--wait`와 관계없이 보류된 요청의 `request_id`와 종료 코드 `3`을 즉시 반환합니다.
HAM이 이후 전송을 맡으므로 다시 보내지 마세요. 메시지는 앞뒤 공백을 제거하고
`Message from host/label:` 헤더를 붙여 전달합니다.

### `request`

```bash
python3 "$HAM_CLI" request --address macbook-pro/purple-koala \
  --message '현재 변경을 검토하고 결과를 알려주세요.' \
  --timeout 120000 --output-lines 120 --output-chars 12000
```

공통 수신자 옵션 `--host`, `--address`, `--label`, `--route`를 받습니다.

| 옵션 | 기본값과 규칙 |
| --- | --- |
| `--message TEXT` | 필수입니다. 공백만 있는 메시지는 허용하지 않습니다. |
| `--timeout MS` | 기본값은 `120000`밀리초이며 0보다 큰 정수여야 합니다. 보류 중인 시간을 포함하여 이 명령이 상태를 관찰하는 대기 제한입니다. |
| `--output-lines N` | 기본값은 `120`줄이며 0보다 큰 정수여야 합니다. 반환할 응답의 최대 줄 수입니다. |
| `--output-chars N` | 기본값은 `12000`자이며 0보다 큰 정수여야 합니다. 반환할 응답의 최대 문자 수이며 바이트 수가 아닙니다. |
| `--defer-timeout MS` | 기본값은 `180000`밀리초, 즉 3분입니다. 포커스로 인한 보류를 사용자 결정 대기로 전환하는 제한이며 `--timeout`과 별개입니다. |
| `--verbose` | 기본적으로 꺼져 있습니다. 전체 수명 주기 결과와 정리 전 응답을 반환합니다. |

아래 수명 주기는 참여 정책이 허용하는 기존 프롬프트 전송에만 적용됩니다.
송신자를 확인할 수 있도록 코디네이터의 활성 Herdr 에이전트 pane에서 실행하세요.
전송이 허용되면 수신자를 확인하고 기준 화면을 수집한 뒤 프롬프트를 한 번만
제출합니다. Herdr의 모호한 `--wait`에 의존하지 않고 새 작업 시작과 종료를
관찰하며, 기준 화면 이후 출력을 지정한 한도 안에서 반환합니다. 수신자가 이미
작업 중이었다면 첫 종료는 기존 turn의 경계로만 취급하고 그 다음 작업 시작과
종료를 확인해야 새 요청을 완료로 판정합니다.

### `request-status`

```bash
python3 "$HAM_CLI" request-status --request-id REQUEST_ID --wait 30000
```

| 옵션 | 기본값과 규칙 |
| --- | --- |
| `--request-id ID` | 필수입니다. `request`, 보류된 `send` 또는 배치 결과에서 받은 32자리 소문자 16진수 ID를 그대로 전달합니다. |
| `--wait MS` | 기본값은 `0`이며 한 번만 관찰합니다. 값을 지정하면 `state`, `phase`, `error_code` 중 하나가 바뀌거나, 요청이 종료 상태가 되거나, 지정한 시간이 지나면 반환합니다. 응답 텍스트가 늘어나는 것만으로는 대기를 끝내지 않습니다. `send`와 `batch`의 값 없는 `--wait`와 다릅니다. |
| `--verbose` | 기본적으로 꺼져 있습니다. 변경 여부와 관계없이 전체 저장 결과를 반환합니다. |

저장된 상태를 읽고 종료되지 않은 요청을 관찰하며 중복 요청을 제출하지 않습니다.
보류 제한 전에 포커스가 해제되었다면 저장된 메시지를 전송할 수 있습니다.
기본 결과는 이전 보고와 상태, 단계, 오류 코드 또는 응답 텍스트가 다르면
`changed: true`를 포함합니다. 바뀌지 않았다면 `request_id`, `state`,
`terminal`, `submitted`, `changed: false`만 반환합니다. 같은 ID로 이어서
확인하세요. 관찰 사이에 놓친 작업 시작과 종료를 완전히 복원할 수는 없습니다.
대기를 끝내는 조건과 변경 보고는 별개이므로 응답 텍스트만 늘어났다면 대기를
계속하다가 반환 시점에 `changed: true`로 보고할 수 있습니다.

요청과 보류 대기열은 송신 호스트의 공용 저장소에 보관됩니다.
`HERDR_AGENT_REQUEST_STATE_DIR`로 경로를 지정할 수 있으며, 기본 경로는
`${XDG_STATE_HOME:-$HOME/.local/state}/herdr-agent-messenger/requests`입니다.
새 디렉터리가 없을 때만 기존 `herdr-agent-labels/requests`를 이어서 사용합니다.
`HERDR_PLUGIN_STATE_DIR`는 사용하지 않습니다. 원래 송신 호스트와 같은 저장소에서
조회하세요. 다른 환경에서 `request_not_found`가 발생해도 미전달로 판단하지 마세요.

### `batch`

```bash
python3 "$HAM_CLI" batch \
  --requests-json '[{"address":"local/yellow-falcon","message":"변경된 코드를 검토해주세요."},{"route":"ROUTE_TOKEN","message":"관련 문서를 검토해주세요."}]' \
  --wait --timeout 120000 --max-workers 4
```

| 옵션 | 기본값과 규칙 |
| --- | --- |
| `--requests-json JSON` | 필수입니다. 비어 있지 않은 JSON 배열 또는 `{"requests":[...]}` 객체를 받습니다. `-`를 지정하면 표준 입력에서 읽습니다. |
| `--wait` | 기본값입니다. 각 요청의 수명 주기를 관찰합니다. |
| `--no-wait` | 접수 후 완료를 관찰하지 않고 반환합니다. `--wait`와 함께 사용할 수 없습니다. |
| `--timeout MS` | 기본값은 `120000`밀리초이며 0보다 큰 정수여야 합니다. 전체 배치가 아니라 각 요청의 대기 제한입니다. |
| `--max-workers N` | 기본값은 `4`이며 `1`부터 `8`까지의 정수여야 합니다. 동시에 전송할 요청 수의 상한입니다. |
| `--defer-timeout MS` | 기본값은 `180000`밀리초, 즉 3분입니다. 각 수신자의 포커스 보류를 사용자 결정 대기로 전환하는 제한입니다. |
| `--verbose` | 기본적으로 꺼져 있습니다. 각 항목의 전체 전송·수명 주기 정보를 반환합니다. |

각 항목은 비어 있지 않은 문자열 `message`와 `address` 또는 `route` 중 정확히
하나를 포함해야 합니다. 같은 에이전트에 주소와 token을 각각 사용한 경우도
실제 수신자 identity를 기준으로 중복을 탐지하여 `duplicate_route`로 거부합니다.
CLI는 이미 작성된 개별 지시를 제한된 동시성으로 전달할 뿐, 요청을 의미적으로
분해하거나 메시지를 고쳐 쓰지 않습니다. 결과는 완료 순서가 아니라 입력 순서를
유지합니다.

`--wait`에서 수명 주기에 진입한 항목은 `request_id`와 제한된 `response`를
포함합니다. 보류된 항목에는 `--no-wait`에서도 요청 ID가 있습니다. 사전 검증에
실패했거나 즉시 전송한 `--no-wait` 항목에는 ID가 없을 수 있습니다.
`submitted`와 `deferred` 항목은 다시 보내지 말고 반환된 ID로 `request-status`를
실행하세요. 배치 응답의 필드와 종료 코드 해석은 아래 표를 참고하세요.

### `read`

```bash
python3 "$HAM_CLI" read --address macbook-pro/purple-koala \
  --lines 160 --max-bytes 65536
python3 "$HAM_CLI" read --address macbook-pro/purple-koala \
  --lines 160 --max-bytes 65536 --cursor PREVIOUS_CURSOR
```

공통 수신자 옵션 `--host`, `--address`, `--label`, `--route`를 받습니다.

| 옵션 | 기본값과 규칙 |
| --- | --- |
| `--lines N` | 기본값은 `120`줄이며 0보다 큰 정수여야 합니다. 최근 출력에서 읽을 최대 줄 수입니다. |
| `--max-bytes N` | 기본값은 `65536`바이트이며 0보다 큰 정수여야 합니다. 반환할 UTF-8 출력의 최대 바이트 수입니다. |
| `--cursor TOKEN` | 기본값은 없습니다. 이전 `read`가 반환한 불투명한 `cursor`를 전달하면 안전하게 계산할 수 있는 경우 변경분만 반환합니다. |
| `--watermark TOKEN` | `--cursor`와 같은 옵션의 별칭이며 기본값은 없습니다. |
| `--verbose` | 기본적으로 꺼져 있습니다. 정리 전 출력, cursor 진단 정보와 라우팅 메타데이터를 반환합니다. |

기본 결과에는 `address`, 정리된 `output`, `truncated`, `delta`, `cursor`가
포함됩니다. `--verbose`를 사용하면 `cursor_status`, `route_refreshed`, `route`도
확인할 수 있습니다. 터미널은 추가 전용 로그가 아니라 다시 그려지는 화면일 수
있으므로 줄 번호로 진행 상황을 추측하지 마세요. 안전한 변경분을 계산할 수 없으면
`delta: false`로 현재 화면을 제한된 크기로 반환하며, 전체 결과에서는
`cursor_status: expired`를 확인할 수 있습니다. `truncated: true`이면 일부 출력이
생략되었다고 보고하세요.

### `spawn`

```bash
python3 "$HAM_CLI" spawn --kind codex --cwd /path/to/project --placement workspace
python3 "$HAM_CLI" spawn --kind omp --host macbook-pro \
  --cwd /remote/project --placement split --from-pane REMOTE_PANE --direction down \
  -- --model anthropic/claude-haiku-5-5
```

| 옵션 | 기본값과 규칙 |
| --- | --- |
| `--kind KIND` | 필수입니다. `codex`, `claude`, `omp` 등 대상 Herdr가 지원하는 에이전트 종류를 지정합니다. 공백 없는 비어 있지 않은 token이어야 합니다. |
| `--host HOST` | 기본값은 `local`입니다. 원격 호스트는 명시적인 SSH 별칭이어야 합니다. |
| `--cwd PATH` | 로컬 기본값은 현재 작업 디렉터리입니다. SSH 호스트에서는 필수이며 원격 경로를 지정합니다. |
| `--placement split\|tab\|workspace` | 기본값은 `split`입니다. 분할 pane, 새 tab 또는 새 workspace를 만듭니다. |
| `--from-pane PANE` | 원본 pane을 지정합니다. 로컬 `split`에서는 생략 시 `HERDR_PANE_ID`를 사용합니다. 원격 `split`에서는 필수입니다. `tab`에서는 workspace를 찾는 데 사용할 수 있습니다. |
| `--direction right\|down` | 기본값은 `right`이며 `split`의 방향을 지정합니다. |
| `--workspace WS` | 새 `tab`을 만들 workspace를 지정합니다. 생략 시 원본 pane에서 찾고, 원본 pane도 없으면 Herdr의 기본 대상을 사용합니다. |
| `--workspace-label TEXT` | 새 `tab` 또는 `workspace`의 표시 이름을 지정합니다. 생략 시 명시한 `--label`을 사용하며, 둘 다 없으면 시작 후 자동 HAM 라벨로 이름을 맞춥니다. |
| `--label LABEL` | 기본적으로 사용 중이지 않은 HAM `color-animal` 라벨을 선택합니다. 직접 지정할 때는 비어 있지 않고 `/`를 포함하지 않아야 합니다. |
| `--message TEXT` | 선택 사항입니다. 준비 완료 뒤 최초 프롬프트를 한 번 제출하며 `--message-file`과 함께 사용할 수 없습니다. |
| `--message-file PATH` | 선택 사항입니다. 호출자 로컬 UTF-8 파일에서 최초 프롬프트를 읽습니다. 원격 cwd의 파일이 아닙니다. |
| `--timeout MS` | 기본값은 `90000`밀리초이며 `1`부터 `300000`까지의 정수여야 합니다. 대화형 에이전트의 준비 완료를 기다리는 제한입니다. |
| `--verbose` | 기본적으로 꺼져 있습니다. 전체 세션·라우팅 정보와 메타데이터 보고 결과를 반환합니다. |
| `-- AGENT_ARGS...` | 선택 사항입니다. `--` 뒤의 인수를 대화형 에이전트에 전달합니다. HAM 옵션은 구분자 앞에 두세요. |

기본 성공 결과에는 `address`, `marker`, `created`, `pane_id`,
`workspace_id`가 있고, `tab_id`는 값이 있을 때만 포함됩니다. `created`는 `pane`,
`tab`, `workspace` 중 하나입니다. `label_metadata_reported`와
`placement_label_reported`는 `false`일 때만 표시됩니다. `--verbose`를 사용하면
`label`, `color`, `host`, `kind`, `cwd`, `route`, `agent`와 두 보고 플래그를
모두 확인할 수 있습니다. HAM 팔레트에 없는 라벨은 marker가 비어 있고 팔레트
메타데이터가 없습니다. 보고 플래그가 `false`여도 세션 생성 실패를 뜻하지는 않으므로
실제 주소와 상태를 확인하세요.

생성 후 시작 단계가 실패하면 HAM은 자신이 만든 리소스를 닫으려고 시도하고 오류
메시지에 `cleanup` 결과를 포함합니다. 실패 시 무조건 다시 생성하지 말고 해당
리소스와 세션이 남았는지 먼저 확인하세요.

## 기존 프롬프트의 결과 계약과 종료 코드

이 절은 비참여 등 정책이 허용한 기존 전송에 적용되며 native 메일함 수신 확인이 아닙니다.

### 요청 수명 주기

에이전트가 허용된 CLI `request`, `send`, `batch`로 메시지를 보낼 때 수신자 pane에
포커스가 있으면 HAM이 전송을 보류합니다. 단, 자기 자신에게 보내면 포커스를
확인하기 전에 `recipient_is_sender`로 즉시 실패합니다. 송신자 pane에 포커스가
있어도 자기 자신에게 보낸 메시지를 보류하지 않습니다. 입력창을 읽거나 실제
타이핑을 감지하는 기능은 아닙니다. 제한 시간이 지나기 전에 해당 pane을 떠나면
백그라운드 worker가 메시지를 전달합니다. 원래 CLI의 대기가 끝나도 HAM이
보관한 메시지의 전송을 계속 맡습니다.

`--defer-timeout`의 기본값인 3분이 지나도록 포커스가 유지되면
`awaiting_decision`으로 바뀌고 Herdr 알림을 보냅니다. 명령 팔레트에서
**HAM: Held Messages**를 열고 Up/Down으로 요청을 선택한 뒤 **Push** (`p`) 또는
**Cancel** (`c`)을 선택하세요. `q` 또는 Esc로 창을 닫을 수 있습니다. Push는 해당
pane에 제출하지 않은 글이 있으면 메시지와 합쳐서 제출할 수 있습니다.
Cancel은 미제출 상태에서 요청을 끝내며 `submission_failed`,
`phase: delivery_cancelled`, `error_code: delivery_cancelled`,
`submitted: false`로 기록합니다. **제한 시간이 지난 메시지는 나중에 포커스를
옮겨도 자동으로 전송하지 않습니다.** 반드시 Push 또는 Cancel을 선택해야 합니다.

보류 후 전송하거나 Push로 전송한 요청은 worker가 약 0.5초마다 관찰합니다.
요청이 종료 상태가 되거나 제출 후 30분이 지나면 해당 요청의 관찰을 끝냅니다.
저장된 결과는 `request-status`로 읽으세요. 30분이 지난 뒤에도 이 명령으로
관찰을 이어갈 수 있습니다. worker는 보류 중인 요청, 사용자 결정 대기 요청,
관찰 중인 요청이 모두 없어야 종료합니다.

HAM 오버레이의 직접 전송과 코디네이터 위임은 사용자가 시작한 작업이므로 보류하지
않습니다. 알려진 한계도 있습니다. 글을 입력한 뒤 제출하지 않고 다른 pane으로
이동하면 남아 있는 초안과 수신 메시지가 여전히 합쳐질 수 있습니다.

SSH 수신자는 수신 호스트의 포커스를 확인하지만 보류 대기열, worker,
**HAM: Held Messages**는 모두 **송신 호스트**에 있습니다. 제한 시간이 지나면
양쪽 호스트에 알림을 보내며, 수신 호스트의 알림에는 결정을 내릴 송신 호스트가
표시됩니다. 전송 전에 수신자가 사라지거나 교체되면 새 점유자에게 보내지 않고
미전달 실패로 종료합니다.

기본 수명 주기 payload에는 `request_id`, `state`, `phase`, `terminal`,
`submitted`, `address`가 있습니다. 오류가 있을 때만 사람이 읽는 `error`와
기계가 분기할 `error_code`를 포함하며, `null`인 선택 필드는 생략합니다.
보류 중에는 `defer.reason: recipient_focused`와 `defer.deadline`을 반환합니다.
응답이 있으면 정리된 텍스트를 `response.text`로 반환하며, 완료되지 않았고 텍스트도
없으면 `response`를 생략합니다. `--verbose`에서는 전체 메타데이터와 응답을
반환합니다. 사전 검증 실패는 `{"error":{"code":"...","message":"..."}}`
형태이며 `request_id`가 없습니다. 모든 명령 결과에 요청 ID가 있는 것은 아닙니다.

| `state` | 의미와 후속 조치 |
| --- | --- |
| `submission_failed` | 수신자 확인이나 제출에 실패했거나 보류된 메시지를 취소했습니다. `error_code`를 확인하고 취소 결정을 존중하세요. |
| `deferred` | 수신자 pane에 포커스가 있어 전송을 보류했습니다. 단계는 `deferred_recipient_focused`이며 `submitted: false`인 비종료 상태입니다. HAM이 전송을 맡으므로 다시 보내지 마세요. |
| `awaiting_decision` | 보류 제한 시간이 지났습니다. 단계는 `awaiting_decision`이며 `submitted: false`인 비종료 상태입니다. 송신 호스트에서 Push 또는 Cancel을 선택하세요. |
| `submitted_working` | 요청을 제출했거나 제출했을 수 있으며 마지막으로 확인한 수신자는 작업 중입니다. 완료를 뜻하지 않습니다. `request-status`로 확인하세요. |
| `submitted_settled` | 새 요청의 작업 시작과 종료를 관찰했습니다. 응답 품질과 상관관계를 확인한 뒤 사용하세요. |
| `submitted_unknown` | 제출했거나 제출했을 수 있으나 해당 turn의 완료를 안전하게 확인하지 못했습니다. 재전송하지 말고 저장된 상태와 수신자를 확인하세요. |

`submitted`는 접수가 확인되면 `true`, 미제출 또는 거부가 확인되면 `false`, 접수
여부가 불확실하면 `null`입니다. `terminal`은 더 이상 자동 관찰로 진전시키지 않는
상태인지 나타냅니다. 보통 실패와 완료는 종료 상태이며, 진행 중과 불명 상태는 계속
조회할 수 있습니다. 단, 점유자가 바뀐 `phase: recipient_changed`는
`submitted_unknown`이면서 `terminal: true`입니다. 이때 기존 경로와 이미 수집한
출력만 보존하며 새 에이전트의 출력이나 경로를 이전 요청에 연결하지 않습니다.
종료 상태라는 이유만으로 원래 요청이 미전달이었다고 판단하지 마세요.

주요 관찰 단계는 `waiting_for_preexisting_turn`, `waiting_for_requested_turn`,
`waiting_for_requested_settlement`, `settled`, `wait_timed_out`,
`wait_cancelled`, `refresh_unavailable`, `recipient_changed`입니다.
`phase`는 진단 정보이고, 재전송 판단에는 `state`, `submitted`, `terminal` 및
`error_code`를 함께 사용해야 합니다.

응답 필드는 다음과 같이 해석합니다.

| 필드 | 의미 |
| --- | --- |
| `response.text` | 기본 결과의 정리된 응답 텍스트입니다. 기준 화면 이후에서 제한된 분량을 수집하며, 화면 연속성을 찾지 못하면 현재 화면 일부가 포함될 수 있습니다. 전체 결과에서는 `response.output`을 사용합니다. |
| `response.truncated` | 기본 결과와 전체 결과 모두 출력 한도나 읽기 실패로 응답 텍스트 자체가 잘렸을 때만 `true`입니다. 오래된 이력을 생략했거나 기준 화면과의 연속성을 잃었다는 이유만으로는 설정하지 않습니다. `false`여도 응답의 상관관계나 신뢰할 수 있는 전체 기록을 보장하지 않습니다. |
| `response.correlated` | 새 요청의 작업 시작과 종료를 관찰했고 기준 화면과 최종 읽기도 성공했음을 뜻합니다. 출력이 잘리지 않았거나 내용이 정확하다는 보장은 아닙니다. |
| `response.anchored` | 화면 겹침을 찾지 못했을 때 마지막 `Message from host/label:` 헤더를 찾아 그 위치부터 수집했음을 뜻합니다. 성공한 경우 `true`로 포함되며, 없으면 `false`로 취급하세요. 요청별 고유 ID로 응답을 인증한 것은 아닙니다. |
| `response.error` | 전체 결과에서 응답 화면 읽기에 실패했을 때 포함될 수 있는 설명입니다. 수명 주기의 최상위 `error_code`와 별도로 확인하세요. |

### 배치 결과

배치 결과는 `{"status":"...","results":[...]}`입니다. 기본 항목에는 `address`와
`status`가 있고 필요에 따라 `request_id`, `state`, `error_code`, `error`,
`response`가 포함됩니다. 기본 `response`는 `text`와 `truncated`를 제공합니다.
`--verbose`는 경로와 전체 항목 정보를 반환합니다. 원본 수명 주기의 상관관계나
`anchored` 정보를 확인하려면 해당 ID로 `request-status`를 실행하세요.

| 항목 `status` | 의미와 후속 조치 |
| --- | --- |
| `succeeded` | 해당 요청의 완료를 확인했습니다. 응답을 검토하세요. |
| `submitted` | 접수는 확인했지만 이 실행에서 완료를 확인하지 못했습니다. `--no-wait`도 여기에 해당합니다. 요청 ID가 있으면 `request-status`로 이어서 확인하세요. |
| `deferred` | 포커스 보류 또는 사용자 결정 대기 중입니다. HAM이 전송을 맡고 있으므로 다시 보내지 말고 ID로 상태를 확인하세요. |
| `failed` | 입력·주소 검증이나 제출이 실패했습니다. 해당 항목의 원인과 전달 여부를 확인한 뒤 미전달 항목만 재시도하세요. |
| `timeout` | 접수를 확인하지 못했거나 전송 호출이 시간 초과되었습니다. 미전달의 증거가 아니므로 자동으로 다시 보내지 마세요. |
| `cancelled` | 해당 항목이 취소되었습니다. 재시도 전 전달 여부를 확인하세요. 접수가 확인된 후 대기만 취소되면 `submitted`로 보고됩니다. |

| 전체 `status` | 의미 |
| --- | --- |
| `succeeded` | 모든 항목이 `succeeded`, `submitted` 또는 `deferred`입니다. HAM이 접수하거나 보관했다는 뜻이며 모두 전달되거나 완료되었다는 뜻은 아닙니다. |
| `partial` | 일부 항목만 `succeeded`, `submitted` 또는 `deferred`입니다. 각 항목을 개별적으로 처리하세요. |
| `failed` | `succeeded`, `submitted`, `deferred`인 항목이 없습니다. 불명·시간 초과 항목까지 미전달로 단정하지 마세요. |

### 프로세스 종료 코드

| 종료 코드 | 의미와 후속 조치 |
| --- | --- |
| `0` | 성공입니다. `request`와 `request-status`는 `submitted_settled`이고, 배치는 모든 항목이 `succeeded`입니다. `list`, `status`, `read`, `send`, `spawn`은 정상 payload를 반환했다는 뜻이므로 특히 `send --no-wait`를 완료로 해석하지 마세요. |
| `1` | JSON 오류 envelope, `submission_failed` 또는 배치의 `failed`·`cancelled`·`timeout` 항목이 있습니다. 오류와 각 항목을 읽고 미전달이 확인된 경우에만 원인을 고쳐 재시도하세요. 배치 전체를 다시 보내지 마세요. |
| `2` | argparse의 사용법 오류입니다. 도움말을 보고 인수 형식이나 필수 옵션을 고치세요. 보통 표준 오류에 사용법이 출력되며 JSON 결과를 기대하면 안 됩니다. |
| `3` | HAM이 요청을 계속 맡고 있으므로 다시 보내면 안 됩니다. 요청은 `submitted_working`, `submitted_unknown`, `deferred` 또는 `awaiting_decision`이며, 배치는 실패 항목 없이 `submitted`나 `deferred`가 남은 경우입니다. 반환된 ID로 `request-status`를 실행하고 결정 대기 중이면 송신 호스트에서 Push 또는 Cancel을 선택하세요. ID가 없는 즉시 전송 항목은 `status`와 `read`로 확인하세요. |

시간 초과나 SSH 연결 종료는 접수 후에도 발생할 수 있습니다. 종료 코드 `1`도
무조건 재시도하라는 뜻이 아니며, 종료 코드 `3`은 전송 실패가 아닙니다.

## 오류 코드 참고서

아래 코드는 오류 envelope의 `error.code`, 수명 주기의 `error_code` 또는 배치
항목의 `error`에 나타납니다. 같은 명령을 그대로 반복하는 대신 발생 지점과
`submitted` 값을 확인하세요. 기본 결과는 오류가 없으면 `error_code`를 생략하며,
전체 결과에서는 `null`로 표시합니다.

### 입력과 대상 선택: 원인을 수정한 뒤 미전달 요청만 재시도합니다

| 코드 | 의미와 조치 |
| --- | --- |
| `use_mailbox` | 같은 저장소의 송수신자가 모두 manual/auto 조정에 참여합니다. 기존 프롬프트나 우회 대신 `mail send`를 사용하세요. batch의 정책·조회 사전 검사가 실패했다면 모든 대상의 제출은 0건입니다. |
| `invalid_address`, `conflicting_address`, `missing_address` | `HOST/LABEL` 형식과 수신자 옵션을 고치고 `--host` 충돌을 제거하세요. |
| `invalid_route`, `route_expired`, `route_ambiguous` | token이 잘못되었거나 안전한 점유자를 확인할 수 없습니다. `list`와 `status` 또는 GUI에서 대상을 다시 선택하세요. 진행 중 요청을 새 점유자로 바꾸지 마세요. |
| `host_not_configured` | 해당 호스트가 설정된 명시적 SSH 별칭이 아닙니다. 기존 설정에서 올바른 별칭을 찾으세요. |
| `agent_not_found`, `agent_ambiguous` | 정확한 현재 라벨을 확인할 수 없습니다. 성공한 탐색 결과로 사용자에게 보고하고 추측하지 마세요. 생성 요청이 없으면 `spawn`하지 마세요. |
| `empty_message`, `invalid_message` | 메시지가 비었거나 배치 메시지가 문자열이 아닙니다. 메시지를 고치세요. |
| `invalid_timeout` | 일반 요청·전송·배치는 양의 밀리초 정수여야 하며 `spawn`은 `1`부터 `300000`까지여야 합니다. `request-status --wait`는 `0` 이상의 밀리초 정수여야 합니다. |
| `invalid_defer_timeout` | `request`, `send`, `batch`의 `--defer-timeout`을 양의 밀리초 정수로 고치세요. |
| `invalid_lines`, `invalid_max_bytes`, `invalid_output_lines`, `invalid_output_chars` | 해당 출력 한도를 양의 정수로 고치세요. |
| `invalid_cursor` | cursor가 유효하지 않습니다. token을 임의로 고치지 말고 cursor 없이 새 화면을 읽으세요. |
| `invalid_label` | `spawn --label`을 비어 있지 않고 `/`가 없는 값으로 고치세요. |
| `invalid_batch_json`, `invalid_batch`, `empty_batch` | 올바른 비어 있지 않은 JSON 배열 또는 `requests` 배열을 제공하세요. |
| `invalid_request` | 배치 항목을 객체로 만들고 `address` 또는 `route` 중 정확히 하나를 지정하세요. |
| `duplicate_route` | 같은 실제 수신자를 여러 번 지정했습니다. 중복 지시를 정리한 뒤 거부된 항목만 다시 제출하세요. |
| `invalid_workers` | `--max-workers`를 `1`부터 `8`까지의 정수로 고치세요. |
| `invalid_command` | 지원되지 않는 명령입니다. 현재 CLI 도움말에서 명령을 선택하세요. 일반 CLI에서는 argparse가 먼저 거부합니다. |

### 호스트와 송신자: 접근 권한을 복구하되 제출 여부를 구분합니다

| 코드 | 의미와 조치 |
| --- | --- |
| `host_unavailable` | Herdr 탐색에 실패했습니다. 연결·인증·서버 상태를 확인하세요. 로컬 상세에 샌드박스나 소켓 권한 오류가 있으면 필요한 로컬 Herdr 소켓 접근 권한을 얻어 같은 탐색 명령을 다시 실행하세요. 탐색이 성공하기 전에는 에이전트가 없다고 보고하지 마세요. 이미 제출한 요청의 관찰에서 발생했다면 새 요청을 보내지 말고 `request-status`로 이어서 확인하세요. |
| `sender_probe_failed` | 제출 전에 현재 송신자 확인이 실패했으며 프롬프트는 제출되지 않았습니다. 로컬 소켓·샌드박스 권한 문제라면 필요한 접근 권한을 얻은 뒤 정확히 그 미전달 요청만 다시 실행하세요. 배치에서는 이 코드로 실패한 항목만 재시도하고 전달·제출·불명·시간 초과 항목은 다시 보내지 마세요. |
| `sender_unavailable` | `HERDR_PANE_ID`가 없거나 현재 pane에 에이전트가 없습니다. 활성 Herdr 에이전트 pane에서 실행하세요. 제출 전에 실패합니다. |
| `recipient_is_sender` | 송신자 자신을 수신자로 지정했습니다. 포커스 확인 전에 즉시 실패하므로 송신자 pane에 포커스가 있어도 보류하지 않으며 제출되지 않았습니다. 다른 수신자를 선택하세요. |
| `resolve_failed` | 구체적인 코드가 없는 수신자 확인 실패입니다. 초기 실패면 미제출이며, 제출 후 조회 실패면 기존 요청만 이어서 확인하세요. |

SSH 신뢰 설정을 바꾸거나 host-key 검증을 우회하지 마세요. 설정 파일에 없는
별칭으로 보내면 안 됩니다.

### 제출과 관찰: 불명 상태는 자동 재전송하지 않습니다

| 코드 | 의미와 조치 |
| --- | --- |
| `prompt_failed` | 제출이 거부되었거나 전송 명령이 실패했습니다. 설명과 `submitted`를 확인하고 미전달이 확인된 항목만 원인을 고쳐 재시도하세요. |
| `prompt_acceptance_unknown` | 전송이 중단되었지만 접수되었을 수 있습니다. 수명 주기에서는 `submitted: null`이고 배치에서는 `timeout`으로 보고될 수 있습니다. ID가 있으면 `request-status`로, 없으면 `status`와 `read`로 확인하고 다시 보내지 마세요. |
| `wait_timed_out` | 접수 후 관찰 제한 시간이 지났습니다. 미전달이 아니므로 `request-status`로 이어서 확인하세요. |
| `prompt_timeout`, `timeout` | 하위 전송 또는 배치 호출이 시간 초과되었습니다. 접수 여부를 확인하기 전에는 재전송하지 마세요. |
| `cancelled`, `prompt_cancelled` | 제출 전 취소인지 접수 후 대기 취소인지 `submitted`와 상태로 구분하세요. 접수 후라면 기존 요청을 조회하세요. |
| `delivery_cancelled` | 사용자가 보류된 메시지를 제출 전에 취소했습니다. 종료 상태인 `submission_failed`와 `submitted: false`로 기록합니다. 취소 결정을 존중하고 자동으로 다시 보내지 마세요. |
| `recipient_changed` | 요청 도중 점유자가 바뀌었습니다. 제출 전의 보류 요청은 미전달 실패로 종료하며, 제출 후에는 결과를 불명 상태로 보존합니다. 새 점유자의 출력으로 대체하거나 이전 요청을 다시 보내지 마세요. |
| `read_failed` | 하위 `read`가 실패했습니다. 수신자와 접근 권한을 확인한 뒤 읽기만 다시 시도하세요. 프롬프트를 재전송하지 마세요. |

### 저장소·설치·내부 오류: 복구 전에 실제 상태를 확인합니다

| 코드 | 의미와 조치 |
| --- | --- |
| `invalid_request_id`, `request_not_found` | 받은 요청 ID와 같은 실행 환경의 저장소를 사용하는지 확인하세요. ID나 상태 파일이 없다는 사실은 미전달의 증거가 아닙니다. |
| `request_state_invalid`, `request_state_unavailable` | 저장된 상태가 잘못되었거나 읽기·쓰기에 실패했습니다. 저장소 경로와 권한을 복구하고 수신자 상태를 확인하세요. 제출 후 저장 실패일 수도 있으므로 자동 재전송하지 마세요. |
| `cwd_required`, `sender_pane_unavailable` | 원격 `--cwd` 또는 분할에 필요한 원본 pane이 없습니다. `--cwd`, `--from-pane`, 로컬 `HERDR_PANE_ID`를 확인하세요. 생성 전 검증에서 실패합니다. |
| `placement_failed`, `agent_start_failed`, `label_unavailable` | 배치 위치 생성, 대화형 시작 또는 빈 라벨 선택에 실패했습니다. 오류의 `cleanup` 결과와 남은 pane·tab·workspace·에이전트를 확인한 뒤 원인을 수정하세요. |
| `invalid_sender_result`, `dispatch_failed` | 배치 전송기가 잘못된 결과를 반환했거나 예기치 않게 실패했습니다. 항목의 상세와 수신자를 확인하고 일괄 재전송하지 마세요. |
| `internal_error` | CLI가 예기치 않은 예외를 JSON으로 반환했습니다. 설명과 실제 수신자·리소스 상태를 확인하세요. 명령의 어느 단계에서 실패했는지 모르면 자동 재시도하지 마세요. |
| `cli_not_found` | 스킬 래퍼가 저장소의 CLI 모듈을 찾지 못했습니다. 유지 중인 체크아웃과 스킬 설치 경로를 복구한 뒤 다시 실행하세요. |

## 여러 에이전트 조정

참여 worker에게는 메일함을 사용하세요. 다음 순서는 기존 batch가 정책상 허용되는
경우의 절차이며, 모든 대상 조회·정책 검사가 끝나기 전에는 제출하지 않습니다.

1. 요청받은 각 호스트를 `list`로 조회하고 모든 라벨을 확인하세요.
2. 전체 요청을 구체적이고 서로 겹치지 않는 작업으로 분해하세요.
3. 각 worker에게 필요한 문맥만 전달하세요. 사용자가 직접 방송을 요청한 경우가
   아니라면 원본 요청 전체를 모든 worker에게 복사하지 마세요.
4. 이미 맞춤 작성한 지시를 `address`/`message` 또는 `route`/`message` JSON으로
   만들어 `batch`로 전송하세요. GUI가 제공한 절대 CLI 경로와 token은 스킬이
   전역에 설치되지 않았더라도 그대로 사용할 수 있습니다.
5. 요청한 모든 worker가 응답하거나 명확한 종료 실패가 확인될 때까지 기다리세요.
   `submitted`, `deferred`, `awaiting_decision`은 재전송하지 말고 `request-status`로 이어서 관찰하세요.
6. 각 응답을 읽고 관련 workspace에서 내용을 확인한 뒤 누락되거나 서로 맞지 않는
   부분에 후속 요청을 보내세요.
7. 결과를 종합할 때 사용할 수 없는 호스트, 시간 초과 에이전트, 검증하지 못한 주장,
   남은 위험을 구분해서 보고하세요.

## 출력 수집의 한계

응답은 구조화된 에이전트 API가 아니라 TUI 화면에서 수집합니다. pane이 좁으면
에이전트가 줄을 좁은 폭으로 강제 개행하여 응답에도 그 개행이 남을 수 있습니다.
긴 응답을 받을 새 세션은 `spawn --placement tab` 또는
`spawn --placement workspace`로 넓게 배치하는 편이 좋습니다. 출력이 잘렸거나
`response.correlated`가 `false`이면 완전한 응답이라고 단정하지 마세요.

## 플러그인 없이 HAM 사용

HAM 플러그인을 설치하거나 활성화하지 않고 Herdr와 HAM 스킬만 사용할 수 있습니다.
현재 단독 설치는 소스 기반입니다. 스킬 래퍼가 저장소의 라우터 모듈을 불러오므로
지속적으로 유지할 체크아웃이 필요합니다. 스킬 디렉터리만 복사하는 방식은 독립
실행형 설치가 아닙니다. 플러그인 없이 사용할 때는 설치기의 복사 대신 체크아웃으로
연결되는 심볼릭 링크를 사용하세요. 래퍼는 실제 스크립트의 상위 디렉터리나 Herdr의
설치된 플러그인 등록 정보에서 CLI 모듈을 찾기 때문입니다. 아래 대상 경로에 기존
설치가 있다면 먼저 확인하고 관련 없는 파일을 덮어쓰지 마세요.

```bash
git clone https://github.com/zerodice0/herdr-agent-messenger.git \
  "$HOME/.local/share/ham"
mkdir -p "$HOME/.agents/skills" "$HOME/.claude/skills" "$HOME/.omp/agent/skills"
ln -s "$HOME/.local/share/ham/.agents/skills/herdr-agent-messenger" \
  "$HOME/.agents/skills/herdr-agent-messenger"
ln -s "$HOME/.local/share/ham/.claude/skills/herdr-agent-messenger" \
  "$HOME/.claude/skills/herdr-agent-messenger"
ln -s "$HOME/.local/share/ham/.agents/skills/herdr-agent-messenger" \
  "$HOME/.omp/agent/skills/herdr-agent-messenger"
```

심볼릭 링크는 같은 체크아웃을 계속 가리키므로 다음 명령으로 갱신합니다.

```bash
git -C "$HOME/.local/share/ham" pull --ff-only
```

이 체크아웃을 사용할 때는 다음 절대 CLI 경로를 사용합니다.

```bash
HAM_CLI="$HOME/.local/share/ham/.agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py"
python3 "$HAM_CLI" list --host local
```

플러그인 없는 HAM에서도 기존 라벨을 이용한 요청·대기·읽기·배치 라우팅과
`spawn`을 사용할 수 있습니다. `spawn`은 직접 HAM 라벨과 메타데이터를 처리하지만,
플러그인의 오버레이, 명령 팔레트 설치기, 기존 세션에 대한 자동 라벨링과
agent-detected hook은 제공하지 않습니다. 기존 수신자에게 이름이 없으면 현재
snapshot을 확인해 고유한 이름을 직접 지정할 수 있습니다.

```bash
herdr api snapshot
herdr agent rename <pane-id> yellow-falcon
```

로컬과 선택할 모든 SSH 호스트에 Herdr가 설치되어 실행 중이어야 합니다. 원격
호스트는 `~/.ssh/config`의 명시적인 별칭이어야 합니다.

## 테스트 격리

cwd를 플러그인 체크아웃 루트로 지정하고 로컬 Python·Bun 테스트를 실행합니다.

```bash
python3 -m unittest discover -s tests -t .
bun test tests/omp_mailbox_runtime.test.ts
```

모든 Python 테스트 모듈은 제품 모듈보다 먼저 `tests.ham_test_support as ham_test_support`를 가져옵니다.
이 테스트 전용 보호 장치는 실행자의 기본 원장을 초기화하려는 시도를 디렉터리나
스키마가 생성되기 전에 거부합니다. HOME, XDG 상태 경로, 상속된 원장 경로 재정의와
계정의 실제 홈 경로를 모두 보호하며, 테스트가 이 오류를 잡아도 해당 테스트는 실패합니다.
fixture는 실행하는 모든 코드 경로에 전용 원장 디렉터리나 전용 HOME/XDG 환경을
제공해야 합니다. 전용 `LedgerStore`를 명시하는 것만으로 다른 도우미가 여는 원장까지
격리되지는 않습니다. 제품 명령은 이 보호 장치를 가져오지 않습니다.
