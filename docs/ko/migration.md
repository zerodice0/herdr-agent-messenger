# 설치 및 마이그레이션

한국어 · [English](../migration.md) · [README](../../README.ko.md)

## 요구 사항

- 참여하는 모든 호스트에 Herdr 0.8.0 이상이 필요합니다.
- Herdr 호스트에는 Python 3.10 이상이 필요합니다.
- 선택 사항인 SSH 배포 명령 `python3 -m ham rollout`은 Python 3.11 이상을 사용합니다.

## Python 패키지 구조

제품 Python 코드는 `ham/` 아래의 `agents/`, `ledger/`, `ui/`, `ops/`,
`integrations/{omp,claude}/`에 있습니다. `ham/paths.py`가 `PLUGIN_ROOT`를
정의하며 루트에는 `ham/__init__.py`와 `herdr-plugin.toml`이 모두 있습니다.
Herdr manifest 액션, 수신기, SSH는 플러그인 루트를 cwd로 지정합니다:
`cd /ABS/PLUGIN_ROOT && python3 -m ham <dispatch> ...`.
dispatch는 `cli`, `labels`, `delivery`, `mailbox`, `messenger`,
`skill-installer`, `worker`, `display`, `rollout`이며, 원장 명령은
`python3 -m ham cli ledger ...`로 실행합니다. 호출자 기준의 `spawn`,
`--message-file`, `--body-file`, `--destination`에는 복사한 스킬이나 번들의
`scripts/herdr_agent_messenger.py` 절대 경로를 권장합니다. 이 실행기는 플러그인
루트를 찾고 호출자 기준을 유지합니다. 발견한 플러그인 루트를 프로세스 cwd로
지정해 `python3 -m ham --cwd <CALLER_DIRECTORY> cli ...`를 실행하고,
고정된 패키지를 불러온 뒤 dispatcher가 호출자 cwd를 복원합니다. 직접 실행하려면
`cd /ABS/PLUGIN_ROOT && python3 -m ham --cwd /ABS/CALLER cli ...`를 사용하세요.
`--cwd` 없이 플러그인 루트에서 모듈을 실행하면 상대 인수도 해당 루트를 기준으로
합니다. 임의의 cwd에서 실행하기 위해 `PYTHONPATH`를 지정하지 마세요.

이번 전환은 순차 업그레이드가 아니라 **다섯 호스트 전체에서 함께 진행하는
일괄 전환**입니다. 프로토콜 **1**과 원장 스키마는 그대로지만, 이전·새 버전이
섞이면 peer 실행 경로가 달라 실패합니다. 호스트 간 전송과 이전 HAM worker·TUI를
멈추고 모든 호스트의 코드를 갱신한 뒤 복사한 스킬도 갱신하세요. 운영자 동의를
받아 각 호스트에서 활성화된 런타임의 `runtime install omp` /
`runtime install claude`를 다시 실행합니다. 수신기를 불러온 OMP 세션, hook을
불러온 Claude 세션, HAM worker·TUI를 모두 다시 시작한 뒤 전송을 재개하세요.
모든 호스트의 업그레이드가 끝나면 실패한 outbox 항목을
`python3 -m ham cli ledger retry --item OUTBOX_ITEM_UUID`로 재시도합니다.
대체 메시지를 새로 보내지 마세요. 아래의 과거 스키마별 안내와 관계없이 이번
패키지 전환에는 런타임 재설치와 세션 재시작이 필요합니다.

## 새로 설치

공개 GitHub 저장소에서 HAM을 설치하고 활성화합니다.

```bash
herdr plugin install zerodice0/herdr-agent-messenger --yes
```

로컬 개발 환경에서는 체크아웃을 직접 연결할 수 있습니다.

```bash
herdr plugin link "$PWD" --enabled
```

플러그인 표시 이름은 **HAM**이며 ID는 `herdr.agent-messenger`입니다.
기존 이름 없는 에이전트는 **Assign Agent Label** 액션으로 라벨을 지정할 수
있습니다. 설치 후 감지되는 이름 없는 에이전트에는 `blue-otter` 같은
`color-animal` 라벨과 그에 맞는 색상 표시를 자동으로 붙입니다.
사용자가 직접 붙인 에이전트 이름은 변경하지 않습니다.

### 수신 의존성

- **플러그인:** 원장, 메일함, UI와 전송을 담당합니다.
- **스킬:** 에이전트 지침과 CLI 실행기를 제공하며 수신기는 아닙니다.
- **OMP 어댑터:** 네이티브 수신과 조건을 충족하는 action·FYI의 깨우기를 제공합니다.
- **Claude 훅:** 턴 경계에서 네이티브 수신을 제공하며 원장 워커가 유휴 깨우기를 더합니다.
- **Codex:** 어댑터가 필요 없으며 원장 워커가 유휴 세션을 깨우고 스킬은 작업 경계에서 직접 조회합니다.

원장 워커는 스키마, 프로토콜, 통합 코드 변경 없이 유휴 상태의 Claude Code·Codex 세션을
깨웁니다. 조건은 OMP와 같고 유휴 시간은 영속 상태 로그로 측정하며 새로 설치할 runtime은
없습니다(Claude는 등록된 hook이 여전히 필요합니다). 자세한 내용은
[스킬 가이드](skill-guide.md)의 「Claude Code·Codex 유휴 깨우기」를 참고하세요.

새로 설치하거나 업그레이드할 때는 [클라이언트 스킬](skill-guide.md#설치와-클라이언트별-호출)을
설치·갱신하고 **선택한 모든 호스트**에서 cwd를 플러그인 루트로 지정해 읽기 전용 요약을 확인합니다.

```bash
python3 -m ham cli runtime status
# 사용자가 동의한 경우에만, 실제로 쓰는 클라이언트(omp 또는 claude)에 설치합니다.
python3 -m ham cli runtime install omp
```

플러그인과 스킬 설치만으로 네이티브 수신 준비가 완료됐다고 판단하지 마세요.
누락된 런타임은 명시적 동의를 받아 설치합니다. 대화형 설치기의 기본 선택은
**Not now(나중에)**이며, `python3 -m ham skill-installer install all --runtime omp|claude|all`은
명시적 동의입니다. 관련 없는 파일이나 수정된 파일을 덮어쓰지 마세요.
다시 시작해야 할 세션을 알리고 사용자가 선택한 세션만 다시 시작합니다.
이후 `extension_present=1`을 확인하거나 **설치됨, 활성화 대기**로 보고합니다.
플러그인 로드는 클라이언트 설정을 쓰지 않으며 새 저장소의 기본 `auto` 모드도
유지됩니다. 자세한 내용은 [수신 설정 가이드](skill-guide.md#수신-의존성)를 참고하세요.

## 자동 main 유지(스키마 v12)

스키마 v12는 로컬 전용 `auto_main_incumbents` 테이블을 추가합니다.
키는 `(repo_id, host, anchor_workspace_id)`이며 `terminal_id`와
`recorded_at`을 저장합니다. v10과 v11 모두에서 기존 사용자 설정과 메일을
보존하며 업그레이드할 수 있고, v10에는 v11의 추가 항목도 적용합니다.
마이그레이션 자체는 main을 선출하지 않습니다. 원장을 백업한 뒤 코드와
복사한 스킬을 갱신하고 `ledger reconcile`을 실행한 다음, 해당 호스트의
HAM 프로세스를 함께 다시 시작하세요. 읽기 전용 열기는 마이그레이션하지
않으며, 이전 코드는 v12를 열 수 없습니다. 다운그레이드에는 백업이 필요합니다.

auto의 우선순위는 활성 명시적 지정, 유효한 기존 자동 main, 기존의 단일 후보
규칙 순서입니다. 기존 자동 main은 같은 범위 안에서 활성 상태여야 하며
기준 workspace에 있어야 합니다. 레지스트리를 갱신할 때 활성 명시적 지정과
유효한 기존 자동 main이 없으면 단일 후보를 기록하고, 무효해진 기존 기록은
같은 트랜잭션에서 삭제합니다. 조회는 선출 결과를 기록하지 않습니다.
기록은 terminal을 기준으로 하므로 같은 terminal에서 세션을 다시 시작해도
유지됩니다. 이후 메인 체크아웃에 다른 세션이 열려도 main을 유지하며, 기존
main이 나가면 다음 단일 후보가 이어받습니다. 최초 스냅샷부터 후보가 둘이면
main을 정하지 않고, 연결된 workspace의 세션도 main을 빼앗지 못합니다.

이 기록은 사용자 설정이나 명시적 지정이 아닙니다. manual·off에서는 무시하며,
명시적 지정을 해제하면 기존 자동 main 또는 자동 선출 결과가 적용됩니다.
`workspace_settings` 행은 다시 쓰지 않습니다. `repo show`에는 읽기 전용
`main_source`(`pinned`, `auto`, 또는 null)를 추가합니다. 에이전트 상태 내보내기와
프로토콜 1은 바뀌지 않습니다. 기존 자동 main 기록은 복제하지 않으며,
peer는 원래 호스트가 계산한 역할을 받아 봅니다.

## 발신자 정리(스키마 v11)

스키마 v11은 로컬 `mail_dismissals`와 nullable `messages.sender_context`를
추가하며 v10에서의 업그레이드를 명시적으로 허용합니다. 기존 식별자·설정·본문·
전달 상태·payload 해시·수신 확인은 다시 쓰지 않습니다. 감사 테이블은 본문을 참조하는 외래 키 없이 발신자·역할 그룹 식별 정보와
실패 당시 정보를 보존합니다. 본문이 보존 기한에 따라 삭제되어도 최초 정리 기록은
남으며, 권한이 있는 재시도 때문에 본문을 복원하지 않습니다. `final_at`과 전달의
`updated_at`, 원래 보존 기한도 바꾸지 않습니다.

새 에이전트 메일은 검증된 발신자 식별 정보와 발신 당시 anchor를 로컬에
보존합니다. 이 정보는 불변 payload 해시나 원격 envelope에 포함하지 않습니다.
연결된 workspace와 발신 에이전트가 모두 사라져도 원래 anchor의 main 권한은
남습니다. 기존 v10 메일의 context는 null이며 과거 그룹을 임의로 만들지 않습니다.
이 메일에서 main의 대리 권한을 허용하려면 남아 있는 발신자·workspace 기록으로
그룹을 증명해야 합니다. 연결된 workspace나 anchor의 과거 증거가 없다면 거부합니다.
원래 terminal은 발신 저장소의 소유권 증거를 검증할 수 있을 때 정리할 수 있습니다.
같은 저장소라는 이유만으로 main을 선택하지 않습니다.

사용자가 배포를 승인하면 호스트 DB를 백업하고 같은 원장을 쓰는 이전 HAM
프로세스를 먼저 멈춥니다. 호스트 코드와 복사한 스킬을 함께 갱신한 뒤
`python3 -m ham cli ledger reconcile`로 쓰기 마이그레이션을 수행하고,
HAM worker·TUI와 HAM 코드를 불러오는 프로세스를 다시 시작합니다.
읽기 전용 열기는 마이그레이션하지 않습니다. 이전 코드는 v11을 열 수 없으며
다운그레이드에는 업그레이드 전 DB를 복원해야 합니다. 버전 표시만 고치면 안 됩니다.
이 기능 자체는 OMP·Claude 런타임 재설치를 요구하거나 수신 계약을 바꾸지 않습니다.
실제 배포와 메일 정리는 여전히 사용자의 명시적 승인이 필요합니다.

이 기록은 **발신 원장의 확인 의무 정리**이며 수신자의 작업 처리 기록으로 복제하지
않습니다. 프로토콜 1, 전달 상태, outbox 종류, 원격 수신 확인은 그대로입니다.
새 수신 확인·네이티브 ACK·깨우기·handled 시각을 만들지 않고 수신 호스트의 이력도
바꾸지 않습니다. [`mail dismiss` 계약](../../.agents/skills/herdr-agent-messenger/reference.md#sender-dismissal)을 참고하세요.

## 턴 종료 후 FYI(스키마 v10)

스키마 v10은 로컬 `deliveries.fyi_turn_end INTEGER NOT NULL DEFAULT 0`을
추가합니다. 기존 v9 전달은 수신 당시 작업 중이었는지 증명할 수 없으므로
보수적으로 표시하지 않습니다.

조건을 충족하는 action은 참여 중인 유휴 OMP를 신속히 깨웁니다. queued FYI는
수신자 호스트의 로컬 enqueue 시각으로부터 600초 뒤 깨우기 대상이 되며 같은 안전
조건을 적용합니다. 600초는 예약 시각이 아닌 최소 대기 시간입니다. 일반 이벤트나
5분 간격 안전 확인에서 깨우므로 보통 약 10~15분이 걸립니다. 이 지연 경로는 스키마나
설정을 추가하지 않으며, 기존 queued FYI도 마이그레이션 없이 로컬 전달 시각을 씁니다.

기본 임곗값은 이제 `coalesce_seconds` 60초, `wakes_per_hour` 시간당 4회입니다
(`idle_minutes` 3분과 `max_hop` 10은 그대로입니다). 저장된 `thresholds`가 없는
저장소에만 적용하며, 이미 임곗값을 저장한 저장소는 저장된 값을 유지합니다.
설정이나 스키마를 바꿀 필요는 없습니다.

최종 수신자의 로컬 enqueue 시점에 로컬 조정으로 확인한 상태가 `working`이고
`agents.last_seen`이 15초 이내일 때 턴 종료 후 전달 표시를 기록합니다.
로컬 조정은 보통 5초마다 실행합니다. 표시된 FYI는 600초를 기다리지 않고 기존
깨우기 조건에 따라 그 턴 직후 전달합니다. 유휴 상태이거나 상태를 알 수 없거나
오래된 경우에는 표시하지 않지만, 시간이 지나 조건을 충족하면 지연 경로로
깨울 수 있습니다. 원격 wire payload에는 표시를 넣지 않으며 최종 수신자의
원장이 로컬에서 판단합니다.

원장을 백업하고 이전 HAM worker·Mailbox 프로세스를 멈추세요. 호스트의 원장
코드와 CLI·복사한 스킬을 함께 갱신한 뒤 업그레이드한 `ledger reconcile`을
실행하고 새 HAM 프로세스를 시작하세요. 이전 enqueue 코드는 표시를 누락할 수
있고 이전 코드는 v10 저장소를 열 수 없습니다. 읽기 전용 열기는 마이그레이션하지 않습니다. 다운그레이드하려면
업그레이드 전 DB를 복원해야 합니다. OMP 확장과 Claude hook은 바뀌지 않으므로
이 정책 때문에 확장을 다시 시작하거나 런타임을 다시 설치할 필요는 없습니다.

TUI의 새 입력창은 매번 `action`으로 시작합니다. Ctrl+G로 `action` / `fyi`를
전환하며 새 입력창을 열 때 초기화합니다. CLI의 `--kind` 기본값은 계속 `fyi`입니다.


## Worktree 역할 그룹(스키마 v9)

현재 역할 규칙은 이 절을 따릅니다. 아래의 스키마 v7 절은 과거 기록입니다.
역할, main·members 선택, Settings 변경, `--to main`은 모두
`(repo_id, 정규 호스트, anchor_workspace_id)`를 기준으로 합니다.
모드·home·깨우기 임곗값은 저장소 전체에 적용합니다.

기존 스키마에 추가되는 `workspaces` 테이블에는 Herdr 스냅샷에서 현재 열려 있는
workspace의 `host`, `workspace_id`, `repo_key`, `is_linked_worktree`, `label`을
저장합니다. 에이전트가 없는 workspace도 포함합니다. 같은 저장소·호스트의 연결된
workspace는 Herdr `worktree.repo_key`가 같고 열려 있는 비연결 workspace를
기준 workspace(anchor)로 삼습니다. 후보가 여럿이면 결정적인 순서로 하나를
선택합니다. 메타데이터나 열린 기준 workspace가 없으면 자체 범위를 유지합니다.
일반 workspace를 잘못 묶지 않도록 git common directory로 대체하지 않습니다.

기존 `workspace_settings` 행은 이동·복사·삭제하지 않습니다. 그룹에 속한 동안에는
기준 workspace의 명시적 main·members만 적용하며, 연결된 workspace의 설정은
변경 없이 보존합니다. 그룹에서 분리되면 기존 설정이 다시 적용됩니다. 유효한 명시적
main은 그룹 안의 어느 workspace에 있어도 됩니다. 명시적 main이 없으면 유효한
기존 자동 main을 유지하고(스키마 v12), 그런 기록도 없으면 기준 workspace에
활성 에이전트가 있을 때만 선출합니다. 그룹의 활성 에이전트가 하나이면 그
에이전트를, 여럿이면 메인 체크아웃의 활성 에이전트가 정확히 하나일 때 선택합니다.
열린 기준 workspace에 에이전트가 없으면 연결된 에이전트가 하나뿐이어도
**자동 main이 없습니다**. manual worker는 기준 workspace에서 선택한
그룹 구성원 중 main을 제외한 에이전트이며, auto worker는 그룹의 나머지 에이전트입니다.
기준 workspace를 열거나 닫으면 그룹이 바뀌고 영향을 받는 역할을 갱신합니다.
`repo show`는 기준 workspace를 `workspace_id`로, 원래 workspace를
`requested_workspace_id`로 반환합니다.

`--to main`은 송신자의 그룹에서 main을 찾아 구체적인 대상을 저장하며, 다른 그룹이나
home으로 대체하지 않습니다. `[HOST/]main@REPO`는 연결된 workspace 수가 아니라
서로 다른 역할 그룹을 셉니다. main이 없으면 `main_unresolved`, main이 있는 그룹이
여럿이면 `main_ambiguous`로 실패합니다. `HOST/LABEL` 주소는 바뀌지 않습니다.

### 저장소 업그레이드와 혼합 버전 배포

1. 업그레이드 전에 각 호스트의 원장 DB를 백업합니다. 이전 HAM 플러그인·worker·Mailbox
   프로세스와 HAM 코드를 불러온 수신 세션을 멈추고, 해당 호스트의 코드와 복사한 스킬을
   함께 갱신합니다. 이전 프로세스와 새 프로세스가 업그레이드된 원장을 함께 쓰면 안 됩니다.
2. 각 호스트의 갱신된 HAM 체크아웃에서
   `python3 -m ham cli ledger reconcile`을
   실행합니다. 이 쓰기 단계에서 이전 저장소를 현재 스키마인 v12로 업그레이드하며
   v9의 workspace 메타데이터도 포함합니다. 읽기 전용 명령은 마이그레이션하지 않습니다.
3. 업그레이드한 코드로 해당 호스트의 HAM 프로세스를 다시 시작합니다. OMP 확장과
   Claude hook 계약은 바뀌지 않으므로 새 어댑터·hook 설정은 필요하지 않습니다.
   참여 호스트의 업그레이드와 reconcile이 끝나면 그룹을 기준으로 하는 주소 사용을 재개합니다.

프로토콜은 **1**을 유지합니다. 상태 내보내기의 역할 범위 메타데이터는 선택 사항이므로
이전 peer도 원래 호스트가 계산한 역할을 표시할 수 있습니다. 다만 이전 peer의 로컬
에이전트에는 업그레이드 전까지 기존 역할 규칙이 적용됩니다. 이전 역할 설정 payload는
계속 적용하지 않고 성공으로 응답하며, 저장소 전체 설정만 복제합니다. 스키마 v8의
메일 호환성 요구 사항도 유지됩니다. wire 호환성이 DB 공유 호환성을 뜻하지는 않습니다.
이전 코드는 스키마 v9 DB를 열 수 없습니다. 다운그레이드하려면 스키마 버전 표시를
수정하는 대신 업그레이드 전에 백업한 DB를 복원해야 합니다.

## Workspace 역할 마이그레이션(스키마 v7)
> 과거 v6→v7 마이그레이션 기록입니다. workspace별 독립 역할 규칙은
> [스키마 v9의 worktree 역할 그룹](#worktree-역할-그룹스키마-v9)으로 대체됐습니다.
> 현재 배포에는 위의 v9 절차를 사용하세요.


스키마 v7 도입 당시에는 이전 원장을 쓰기 가능한 방식으로 열면 v7로
업그레이드했습니다. 당시 배포할 때는 읽기 명령을 사용하기 전에 각 호스트에서
`ledger reconcile`을 실행하세요. v6 원장을 읽기 전용으로 열면
`schema_upgrade_required` 오류와 함께 `ledger reconcile` 실행 안내가 나옵니다.
읽기만으로 원장을 마이그레이션하거나 수정하지는 않습니다.

스키마 v7은 main과 worker 구성원을 `(repo_id, 정규 호스트, Herdr workspace_id)`별로
정해 worktree workspace에도 독립 main을 허용했습니다. 스키마 v9는 이 선출 범위를
역할 그룹으로 대체합니다. 모드·home·깨우기 임곗값과 메일 이력은 저장소 전체 범위를
유지했습니다.

각 호스트는 기존 저장소 단위의 `main`·`members` 대상 중 활성 로컬 에이전트와
일치하는 대상을 현재 범위로 옮기고 members를 범위별로 묶습니다. 다른 호스트의
대상은 해당 호스트의 원장 사본에서 옮깁니다. 비활성 또는 알 수 없는 로컬 대상은
범위를 안전하게 정할 수 없어 제외하며,
`ledger_meta.workspace_settings_migration_dropped`에
`{repo_id,field,target}` 형태의 JSON 항목을 기록합니다. 필요한 대상이 있다면
Settings에서 활성 에이전트를 다시 선택하세요. 업그레이드는 저장소 단위의
`main`·`members`·`computed_main` 행과 기존 구성원·조건부 설정 보조 테이블을
제거합니다. 단일 후보는 조회 시 계산하며, v12부터는 레지스트리 갱신 시 기존
자동 main을 별도 로컬 테이블에 기록합니다. `computed_main` 설정으로는 저장하거나 전파하지 않습니다.
이후 명시적 대상이 비활성화되거나 범위 밖으로 이동해도 지정은 저장된 채로
남지만, 해당 상태에서는 역할 계산에 사용하지 않습니다.

workspace 역할 설정은 해당 호스트에만 저장하며 복제하지 않습니다. peer는
상태 내보내기를 통해 각 에이전트의 호스트가 계산한 역할을 받아 봅니다.
프로토콜은 1을 유지합니다. 이전 peer의 `main`·`members`·`computed_main`
설정은 적용하지 않고 성공으로 응답하므로 이전 outbox가 영구 실패하지 않습니다.
home·mode·thresholds 복제는 바뀌지 않습니다. 이전 버전의 호스트는 여전히
기존 방식으로 역할을 계산하므로 각 호스트를 업그레이드해야 해당 호스트에도
workspace별 역할을 적용할 수 있습니다.

`--to main`은 보낼 때 송신 호스트에서 송신자의 현재 workspace main을 찾아
구체적인 terminal 대상을 저장합니다. main이 없으면 저장소 home을 기다리거나
다른 workspace를 선택하지 않고 명확한 오류로 실패합니다. 원격 송신 메시지와
home 경유 메시지를 포함한 이전 버전의 미해석 심볼릭 메시지는
`legacy_symbolic_main` 오류로 거부하며 모든 호스트를 업그레이드하라고 안내합니다.
호스트 간에 `--to main`을 사용하기 전에 모든 호스트를 업그레이드해야 합니다.
Mailbox Settings는 저장소 전체의 Mode·Thresholds와 시작 pane의 workspace
Main·Members를 구분해 표시합니다. 호스트 코드를 갱신할 때 복사한 스킬도
갱신해 새 주소 해석·역할 규칙을 적용하세요.

다음은 스키마 v7 도입 당시의 배포 순서입니다.

1. 모든 호스트의 HAM 코드와 복사한 스킬을 갱신합니다. 이전 Mailbox pane을
   새로 열고 수신 런타임과 worker를 업그레이드한 코드로 다시 시작합니다.
2. **모든 호스트**의 HAM 체크아웃에서 업그레이드한 스킬의 명령을 실행합니다.
   `python3 .agents/skills/herdr-agent-messenger/scripts/herdr_agent_messenger.py ledger reconcile`
   이 쓰기 단계를 명시적으로 수행해 읽기 전용 상태 조회나 peer 내보내기 전에
   해당 호스트의 원장을 업그레이드합니다.
3. 모든 호스트의 업그레이드와 reconcile이 끝난 뒤에만 호스트 간 `--to main`
   사용을 재개합니다. 주소를 명시하는 `HOST/LABEL` 방식은 바뀌지 않습니다.

## 0.7.x에서 마이그레이션

HAM 0.8.0에서는 플러그인 ID가 `herdr.agent-labels`에서
`herdr.agent-messenger`로 변경됐습니다. 저장소 이름은 `herdr-agent-labels`에서
`herdr-agent-messenger`로 변경됐으며, 설치 소스는 이제
`zerodice0/herdr-agent-messenger`입니다. 기존 GitHub URL은 새 주소로
리디렉션되므로 기존 GitHub 설치도 계속 업데이트됩니다. 저장소 이름 변경과
별개로 아래의 구 플러그인 ID 마이그레이션은 여전히 필요합니다.

기존 GitHub 설치는 구 ID를 비활성화하고 새 버전을 설치·검증한 다음에만 구
등록을 제거합니다.

```bash
herdr plugin disable herdr.agent-labels
herdr plugin install zerodice0/herdr-agent-messenger --yes
herdr plugin list --plugin herdr.agent-messenger --json
herdr plugin action invoke agent-skill-guide --plugin herdr.agent-messenger
herdr plugin uninstall herdr.agent-labels
```

로컬 개발 링크는 체크아웃을 갱신하고 Herdr 설정을 다시 불러옵니다. 로컬 링크는
경로 기반이므로 unlink/relink 없이 0.8.0 manifest의 새 ID를 인식합니다.

```bash
git pull --ff-only
herdr config check
herdr server reload-config
herdr plugin list --plugin herdr.agent-messenger --json
herdr plugin action invoke agent-skill-guide --plugin herdr.agent-messenger
```

## 호환 동작

마이그레이션 중에는 다음 fallback을 유지합니다.

- 새 설정 경로에 파일이 없으면 기존 `ssh-hosts` 허용 목록을 읽습니다.
- 새 상태가 없으면 기존 탐색 캐시와 요청 상태 디렉터리를 사용합니다.
- 구 `HERDR_AGENT_LABELS_*` 환경 변수도 동작하지만 새 설정에는
  `HERDR_AGENT_MESSENGER_*`를 사용해야 합니다.
- 0.8.0 스킬 래퍼는 `herdr.agent-messenger`를 먼저 찾고 구 플러그인 ID로
  fallback합니다.

구 플러그인을 제거하기 전에 **HAM Skill**을 열어 복사된 프로젝트·시스템 스킬을
갱신하세요. 갱신하지 않은 0.7.x 스킬은 구 ID만 인식합니다.

## 최종 상태 검증

```bash
herdr config check
herdr server reload-config
herdr plugin list --plugin herdr.agent-messenger --json
herdr plugin action list --plugin herdr.agent-messenger
```

최종 액션 목록에는 `agent-skill-guide`, `label-current`, `message-agents`가
있어야 하며 `herdr.agent-messenger`만 활성화돼야 합니다.
