<p align="center">
  <img src="assets/ham-logo.png" alt="HAM — Herdr Agent Messenger 마스코트 로고" width="360">
</p>

# HAM — Herdr Agent Messenger

한국어 · [English](README.md)

HAM은 Herdr용 에이전트 라벨링 및 다중 에이전트 메시징 플러그인입니다.
이름 없는 에이전트에 기억하기 쉬운 `color-animal` 이름을 붙이고, 키보드
중심 팝업이나 Codex, Claude Code, omp에서 해당 에이전트로 프롬프트를 전달합니다.
기존 에이전트에 메시지를 보내는 것뿐 아니라 `spawn`으로 새 세션도 시작할 수 있습니다.

## 주요 기능

- `blue-otter` 같은 자동 라벨과 색상 표시를 제공합니다.
- `~/.ssh/config`의 명시적 별칭으로 로컬·SSH 에이전트를 탐색합니다.
- 코디네이터 위임과 다중 수신자 직접 전송을 지원합니다.
- Codex, Claude Code, omp용 스킬을 짧은 `SKILL.md`와 필요할 때 읽는 `reference.md`로 제공합니다.
- 안전한 요청·대기·읽기·배치·세션 생성·원격 배포 도구를 제공합니다.
- 포커스가 있는 pane으로 오는 에이전트 메시지를 보류하고, 기본 3분 후 **HAM: Held Messages**에서 Push 또는 Cancel을 선택하도록 합니다.
- 간결한 CLI 출력, `--verbose` 진단 정보, `request-status --wait`를 통한 후속 관찰을 지원합니다.

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

## 사용

Herdr 에이전트 pane에서 **Send Prompt to Agents**를 열어 수신자를 선택하고
코디네이터 위임 또는 직접 전송 방식을 고릅니다.

Codex에서는 `$ham`을 입력한 뒤 **HAM — Herdr Agent Messenger**를 선택합니다.
Claude Code에서는 `/herdr-agent-messenger`를 명시적으로 호출하거나 자연어로
HAM 작업을 요청할 수 있습니다. omp에서는 스킬을 설치한 뒤 자연어로 요청하거나
`skill://herdr-agent-messenger`를 읽도록 지시합니다.

```text
$ham local/yellow-falcon에게 dxp-ui에서 사용하는 언어 라벨을 물어봐.
응답을 기다린 다음 현재 작업과 기본값을 그 답변에 맞춰줘.
```

현재 기기는 호스트 이름 대신 `local/LABEL`로 지정하고, 원격 기기는 설정된
SSH 별칭을 사용하세요. 라벨만 쓰면 다른 호스트의 에이전트와 충돌하거나
AI 클라이언트 자체 서브에이전트로 오인될 수 있습니다.

새 에이전트 세션을 열고 메시지를 보내라는 요청에는 `spawn`을 사용한 뒤 반환된
`address`로 `request --address HOST/LABEL`을 실행하세요. 직접
`herdr agent start <name>`을 실행하면 HAM의 라벨 선택과 메타데이터 처리를
우회합니다. 기존 라벨이 없다는 이유만으로 새 세션을 만들거나 라벨을 추측하지 마세요.
전체 옵션과 예시는 [스킬 가이드](docs/ko/skill-guide.md)를 참고하세요.

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
[스킬 가이드](docs/ko/skill-guide.md)를 참고하세요.

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

등록된 Herdr 에이전트 pane에서 `mail send --to local/LABEL --body TEXT`로
진행 중인 작업을 방해하지 않고 메시지를 남길 수 있습니다. 받는 쪽은
`inbox read`로 조회하고 `inbox handle --message UUID`로 처리를 기록합니다.
같은 pane을 쓰는 서브에이전트가 조회해도 전달·처리 상태는 바뀌지 않습니다.
`mail show --thread UUID`는 대화와 상태를 보여 주며,
`mail withdraw --message UUID`는 아직 전달 claim이 없는 메시지를 철회합니다.

메일함은 같은 저장소의 원격 `--to CANONICAL_HOST/LABEL`과 등록된 SSH 별칭도
받습니다. `--to main`은 저장소 home 호스트가 접수할 때 한 번만 해석합니다.
`ledger setup --name HOST`로 호스트 정체성을 고정한 뒤 양쪽에서
`ledger peer add --ssh-alias ALIAS`로 연결을 등록합니다. 전송 상태는
`ledger doctor`, `ledger outbox`로 확인합니다. 독립 flock worker는 일시적 오류를
같은 메시지 ID로 재시도합니다. 영구 오류는 다른 메시지를 막지 않으며 명시적으로
다시 시도할 수 있습니다. 메일함 명령은 터미널에 프롬프트를 넣거나 모델을
깨우지 않습니다. 기존 `send`/`request`와 Held Messages도 그대로입니다.
플래그, 저장 위치와 격리 방법은 [스킬 가이드](docs/ko/skill-guide.md#로컬-메일함)를 참고하세요.

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
