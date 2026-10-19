# 설치 및 마이그레이션

한국어 · [English](../migration.md) · [README](../../README.ko.md)

## 새로 설치

공개 GitHub 저장소에서 HAM을 설치하고 활성화합니다.

```bash
herdr plugin install zerodice0/herdr-agent-messenger --yes
```

로컬 개발 환경에서는 체크아웃을 직접 연결할 수 있습니다.

```bash
herdr plugin link "$PWD" --enabled
```

기존 이름 없는 에이전트는 **Assign Agent Label** 액션으로 라벨을 지정할 수
있습니다. 설치 후 감지되는 에이전트에는 자동으로 라벨이 붙습니다.

## Workspace 역할 마이그레이션(스키마 v7)

이전 원장을 쓰기 가능한 방식으로 열면 기존 데이터를 보존하며 스키마 v7로
업그레이드합니다. 배포할 때는 읽기 명령을 사용하기 전에 각 호스트에서
`ledger reconcile`을 실행하세요. v6 원장을 읽기 전용으로 열면
`schema_upgrade_required` 오류와 함께 `ledger reconcile` 실행 안내가 나옵니다.
읽기만으로 원장을 마이그레이션하거나 수정하지는 않습니다.

main과 worker 구성원은 이제 `(repo_id, 정규 호스트, Herdr workspace_id)`별로
정하므로 worktree workspace도 자체 main을 가질 수 있습니다. 모드·home·깨우기
임곗값은 계속 저장소 전체에 적용합니다. 메일 이력의 범위와 주소를 명시한
workspace 간 메일도 저장소 단위로 유지합니다.

각 호스트는 기존 저장소 단위의 `main`·`members` 대상 중 활성 로컬 에이전트와
일치하는 대상을 현재 범위로 옮기고 members를 범위별로 묶습니다. 다른 호스트의
대상은 해당 호스트의 원장 사본에서 옮깁니다. 비활성 또는 알 수 없는 로컬 대상은
범위를 안전하게 정할 수 없어 제외하며,
`ledger_meta.workspace_settings_migration_dropped`에
`{repo_id,field,target}` 형태의 JSON 항목을 기록합니다. 필요한 대상이 있다면
Settings에서 활성 에이전트를 다시 선택하세요. 업그레이드는 저장소 단위의
`main`·`members`·`computed_main` 행과 기존 구성원·조건부 설정 보조 테이블을
제거합니다. 자동 main은 저장하거나 전파하지 않고 조회 시 계산합니다.
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
다른 workspace를 선택하지 않고 명확한 오류로 실패합니다. 기존의 미해석 심볼릭
메시지는 기록된 송신자의 로컬 범위를 찾을 수 있으면 그 범위를 사용하며,
그렇지 않으면 `main_unresolved`로 실패합니다. Mailbox Settings는 저장소 전체의
Mode·Thresholds와 시작 pane의 workspace Main·Members를 구분해 표시합니다.
호스트 코드를 갱신할 때 복사한 스킬도 갱신해 새 주소 해석·역할 규칙을 적용하세요.

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
