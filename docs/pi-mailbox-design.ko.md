# pi 메일함 수신기와 긴급 메시지(amend/cancel) 설계

## 1. 목표와 역할 분담

HAM은 중앙 관제를 담당하고, 실제 큐 처리는 각 pi 세션 안에서 동작합니다.

| 구성 요소 | 책임 |
|---|---|
| HAM ledger | 주소 해석, 메시지 원본 보관, 상태 전이(queued → delivering → delivered → handled), 역할과 권한 판정, hop과 시간당 횟수 제한, 호스트 간 전송, 보드 표시 |
| pi extension(별도 패키지 [pi-mailbox](https://github.com/zerodice0/pi-mailbox)) | 새 메일 신호 감시, 메일함 표시(상태 표시줄과 overlay), idle 판정, 처리 시점 결정, 세션 주입, 세션 기록을 근거로 한 ACK 보고 |

HAM은 pi에 메시지를 밀어넣지 않습니다. 즉 HAM worker가 pi 터미널에 프롬프트를 입력하거나 pi의 턴을 시작하지 않습니다. 기존 구현에서도 worker가 직접 깨우는 대상은 Claude와 Codex뿐이며, 이 설계는 그 범위를 그대로 유지합니다. HAM이 pi에 보내는 것은 bell 파일 변경이라는 "새 메일 있음" 신호뿐이고, 이 신호는 턴을 시작하지 않습니다.

## 2. 큐의 원본과 처리 흐름

큐의 원본은 HAM ledger에 있습니다. pi extension은 ledger의 상태를 보여 주는 캐시만 가지며, 메일을 자기 파일로 옮기지 않습니다.

1. **표시:** `ledger runtime inspect`로 대기 중인 메일 목록을 읽습니다. 읽기는 claim이나 ACK가 아닙니다.
2. **claim:** 세션에 주입하기 직전에 `ledger runtime claim`을 호출합니다. HAM은 이 단계에서 역할, hop, 시간당 횟수, coalesce 조건을 판정하므로 pi 쪽에서 우회할 수 없습니다.
3. **주입:** `pi.sendMessage()`로 `ham_mailbox` custom message를 주입합니다.
4. **ACK:** 세션 기록(`custom_message` entry)에서 marker와 sha256이 일치하는 항목을 찾은 뒤 `ledger runtime applied`를 보고합니다.
5. **release:** 주입하지 못했다면 `not_applied`로 되돌리고, 결과가 불확실하면 `unknown`으로 표시합니다.

## 3. 일반 메일 처리 규칙

일반 메일은 idle 상태가 되면 자동으로 처리합니다. 다음 조건을 모두 만족해야 합니다.

- pi가 idle 상태이고(`ctx.isIdle()`), 대기 중인 pi 내부 메시지가 없습니다(`!ctx.hasPendingMessages()`).
- editor가 비어 있고, 마지막 입력 이후 설정된 입력 정지 시간(`idle_minutes`)이 지났습니다.
- 확장 UI 대화상자가 열려 있지 않습니다(`ui_prompt_start`/`ui_prompt_end`로 추적).
- herdr에 보고된 turn state가 `idle` 또는 `done`입니다.
- HAM의 wake 조건(역할, coalesce, 시간당 횟수, hop)을 통과합니다.

한 번에 한 묶음(최대 8개, 8 KiB)만 처리합니다. 다음 묶음은 그 턴이 끝난 뒤(`agent_settled`) 다시 판정합니다. 사용자가 프롬프트를 제출할 때(`before_agent_start`) 대기 중인 메일을 함께 붙이는 기존 OMP 동작도 유지합니다.

## 4. 긴급 메시지

긴급 메시지는 새 kind가 아니라, 송신자가 자신이 보낸 원본 메일을 지정하는 두 가지 명령입니다. 두 명령 모두 `kind=action`이고, `reply_to`에 원본 메일 ID를 담으며, 새 필드 `urgency`에 `amend` 또는 `cancel`을 기록합니다.

| 명령 | 용도 | pi에서의 처리 방식 |
|---|---|---|
| `mail amend --message ID --body TEXT` | 원본 메일에 빠진 내용이나 보강할 내용을 전달합니다. | steer |
| `mail cancel --message ID [--body REASON]` | 잘못 보낸 원본 메일을 취소합니다. | abort |

### 4.1 권한

- **송신 가능 주체:** 사용자(Mailbox TUI)와 main 역할의 에이전트만 보낼 수 있습니다.
- **대상 제한:** 자신이 보낸 메일만 amend하거나 cancel할 수 있습니다.
  - 에이전트는 원본을 보낸 터미널과 같은 터미널, 같은 repo에서만 보낼 수 있습니다.
  - 사용자는 이 호스트에서 사용자가 보낸 메일만 대상으로 지정할 수 있습니다.
- **수신자:** amend와 cancel은 원본 메일의 수신자에게만 전달됩니다. 주소를 다시 해석하지 않고 원본에 기록된 경로를 사용합니다.
- **검증 위치:** 권한은 HAM이 송신 시점에 검증합니다.

### 4.2 cancel의 상태별 동작

| 원본 상태 | 동작 |
|---|---|
| 대기 중(queued, 같은 호스트) | 기존 withdraw로 회수합니다. cancel 메시지를 보내지 않고 턴도 시작하지 않습니다. |
| 대기 중(다른 호스트, 회수 요청 중) | 회수 요청과 cancel 메시지를 함께 보냅니다. 호스트별 FIFO 순서 때문에 회수 요청이 먼저 처리됩니다. 원본이 실제로 회수되었다면 수신 호스트는 claim 단계에서 cancel 메시지를 자동으로 `handled` 처리하고 에이전트에게 전달하지 않습니다. |
| 전달 중이거나 전달됨, 처리 완료 | cancel 메시지를 보냅니다. |
| 이미 회수됨, 전달 불가 | 취소할 대상이 없으므로 오류를 반환합니다. |

수신한 pi는 다음과 같이 처리합니다.

- **에이전트가 지금 원본 thread를 처리하고 있을 때:** editor에 초안이 있어도 즉시 `ctx.abort()`로 작업을 중단합니다. 초안은 그대로 둡니다. 중단이 끝나면(`agent_settled`) cancel 메시지를 주입하고 턴을 시작합니다.
- **에이전트가 다른 작업을 하고 있을 때:** 중단하지 않습니다. idle 상태가 되면 일반 메일보다 먼저 처리합니다.
- **idle 상태일 때:** 초안이 있어도 바로 주입하고 턴을 시작합니다.

cancel 메시지가 시작한 턴에서 에이전트는 다음 지침을 받습니다.

- 원본 메일에 따른 작업을 더 진행하지 않습니다.
- 이미 한 변경을 스스로 되돌리지 않습니다.
- 지금까지 진행한 내용(변경한 파일, 실행한 명령, 부수 효과)을 `mail send --reply-to CANCEL_ID --kind action`으로 송신자에게 보고합니다. 송신자는 이 보고를 보고 원복 여부를 결정합니다.

### 4.3 amend의 상태별 동작

| 원본 상태 | 동작 |
|---|---|
| 대기 중 | amend도 같은 수신자의 큐에 들어갑니다. 긴급 claim은 대기 중인 원본을 함께 claim하므로 원본과 보강 내용이 한 묶음으로 전달됩니다. |
| 그 밖의 상태 | amend 메시지를 보냅니다. |

수신한 pi는 editor가 비어 있을 때만 처리합니다.

- **에이전트가 지금 원본 thread를 처리하고 있을 때:** `deliverAs: "steer"`로 주입합니다. 메시지는 현재 tool 실행이 끝난 뒤, 다음 LLM 호출 전에 반영됩니다.
- **에이전트가 다른 작업을 하고 있을 때:** idle 상태가 되면 일반 메일보다 먼저 처리합니다.
- **idle 상태일 때:** 주입하고 턴을 시작합니다.

### 4.4 HAM 쪽 긴급 claim의 판정

`ledger runtime claim`에 `urgent`(처리할 urgency 목록)를 주면 HAM은 긴급 메시지만 claim합니다.

- **건너뛰는 조건:** turn state, coalesce, 시간당 횟수를 건너뜁니다. 송신자가 명시적으로 보낸 긴급 요청이고, 작업 중에 전달하는 것이 목적이기 때문입니다.
- **유지하는 조건:** 수신기 등록 여부와 hop 제한은 그대로 검사합니다.
- **기록:** 긴급 claim은 `via='urgent'`로 기록하며 `wake_log`에 포함하지 않습니다.

"지금 처리 중인 thread"인지는 pi extension이 판정합니다. extension은 자신이 주입한 묶음의 thread ID를 기록해 두고, 턴이 끝나 idle 상태가 되면 이 기록을 지웁니다. amend와 cancel은 원본과 같은 thread에 속하므로 thread ID로 비교할 수 있습니다.

## 5. 프로토콜과 저장소 변경

- **schema 13:** `messages.urgency` 열을 추가합니다(NULL, `amend`, `cancel`).
- **wire payload:** `urgency`는 값이 있을 때만 payload에 포함합니다. 따라서 일반 메시지의 형태와 payload hash는 바뀌지 않습니다. 이전 버전 호스트는 긴급 메시지만 `invalid_message`로 거부하며, 해당 outbox 항목은 failed 상태로 보입니다. 기존 원칙대로 모든 호스트를 함께 업그레이드해야 합니다.
- **native bundle:** 긴급 메시지의 header에 `[action · cancel of ID]` 또는 `[action · amend of ID]`를 표시하고, 처리 지침을 한 줄 추가합니다.

## 6. pi 세션 식별과 설치

- **세션 식별:** pi 세션은 herdr의 pi integration(`herdr integration install pi`)이 보고한 세션 파일 경로로 식별합니다. 보고가 없으면 등록은 `native_session_pending`으로 대기합니다.
- **설치 위치:** 수신기는 HAM이 아니라 별도 pi 패키지인 pi-mailbox가 제공하며 `pi install`로 설치합니다. pi-mailbox는 HAM이 없을 때 로컬 backend로 동작하고, Herdr 안에서 HAM을 찾으면 이 문서의 HAM backend를 씁니다. HAM의 `runtime install`은 pi를 다루지 않습니다.
- **동작 조건:** HAM 백엔드는 TUI 모드, Herdr 환경, 서브에이전트가 아닌 경우(`PI_SUBAGENT_CHILD` 미설정)에만 동작합니다.

## 7. 다른 전달 경로 차단

legacy `send`, `request`, `batch`는 대상 터미널에 프롬프트를 직접 입력합니다. 수신기가 등록된 pi(`client=pi`, `extension_present=1`)에게는 repo와 관계없이 이 경로를 `use_mailbox`로 거부합니다. 사용자가 Mailbox TUI에서 직접 입력하는 DIRECT 경로는 사용자의 의도이므로 유지합니다.

이 거부는 기존 legacy 사전 검사(`CollaborationPolicy`) 안에서 동작합니다. 따라서 송신자가 HAM 조정에 참여하는 에이전트일 때(송신자 repo의 mode가 `off`가 아니고 역할이 main 또는 worker일 때)만 적용됩니다. 조정에 참여하지 않는 송신자의 legacy 경로까지 막으려면 사전 검사 구조를 바꿔야 하므로 이번 범위에서 제외했습니다.

## 8. pi extension UI

- **상태 표시줄:** `HAM ✉ N` 형식으로 대기 중인 메일 수를 보여 주고, 긴급 메시지가 있으면 `!N`을 덧붙입니다. 처리 중인 claim이 있으면 `…`를 표시합니다.
- **overlay:** `/ham-mailbox` 명령이나 `ctrl+alt+m` 단축키로 엽니다. 대기 중인 메일의 송신자, kind, urgency, 본문 첫 부분을 보여 주는 읽기 전용 목록이며, overlay를 열어도 claim이나 ACK가 발생하지 않습니다.
- **알림:** cancel 때문에 작업을 중단하면 `ctx.ui.notify`로 알립니다.

## 9. 범위에서 제외한 항목

- **세션 이름 동기화:** OMP 수신기에 있는 세션 이름 동기화는 pi 수신기에 포함하지 않습니다.
- **원격 원본의 즉시 판정:** 원격 원본에 대한 cancel은 회수 요청 결과를 동기적으로 알 수 없습니다. 그래서 4.2에서 설명한 수신 측 자동 처리에 의존합니다.

## 10. 구현 위치

| 영역 | 파일 |
|---|---|
| schema 13, `urgency` 열 | `ham/ledger/store.py` |
| payload 검증, `Mailbox.urgent()`, 권한 판정 | `ham/ledger/mail.py` |
| pi 등록, 긴급 claim, `inspect` 확장, 회수된 원본에 대한 cancel 자동 처리, bundle 표시 | `ham/ledger/runtime.py` |
| pi 세션 보고 대기 | `ham/ledger/status.py` |
| `mail amend`, `mail cancel` | `ham/ledger/cli.py`, `ham/cli.py`, `ham/agents/output.py` |
| legacy 경로 거부 | `ham/agents/collaboration_policy.py` |
| Mailbox TUI(Threads `u`/`c`, 긴급 표시) | `ham/ui/mailbox.py` |
| pi extension | 별도 저장소 `zerodice0/pi-mailbox` |
| 스킬 문서 | `.agents/skills/herdr-agent-messenger/SKILL.md`, `reference.md` |

## 11. 검증 계획

- **Python 단위 테스트:** urgency payload 검증, amend/cancel 권한(자기 메일, main 역할, 사용자), cancel의 withdraw 우선 처리, 긴급 claim(원본 동반 claim, 건너뛰는 조건), 회수된 원본에 대한 cancel 자동 처리, pi 등록과 ACK, legacy 경로 거부를 검증합니다.
- **pi extension 테스트(bun, pi-mailbox 저장소):** idle 자동 처리, 초안이 있을 때의 지연, cancel의 abort와 초안 예외, amend의 steer, 원본 thread가 아닐 때의 지연, ACK와 release를 가짜 pi API로 검증합니다.
- **전체 회귀 테스트:** `python3 -m unittest discover -s tests -t .`를 실행합니다.
