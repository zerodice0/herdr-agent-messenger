# 안전한 SSH 원격 배포

한국어 · [English](../rollout.md) · [README](../../README.ko.md)

`python3 -m ham rollout`은 사용자가 명시적으로 선택한 원격 Herdr 호스트에 HAM을
설치·마이그레이션·갱신합니다. 전체 40자 commit SHA와 Agent Messenger가 사용하는
SSH 설정 및 선택적 `ssh-hosts` 허용 목록에 등록된 명시적 별칭만 받습니다.
배포 대상을 자동으로 탐색하거나 추가하지 않습니다.

## 미리 보기

cwd를 플러그인 루트로 지정하고 SSH 연결 없이 실행할 정확한 명령을 먼저 확인합니다.

미리 보기에는 조건부 마이그레이션 단계도 포함됩니다. 구 플러그인의 비활성화는
새 플러그인을 활성화하기 전에 수행하고, 구 플러그인의 제거와 제거 여부 확인은
새 플러그인이 검증을 통과한 뒤에 수행합니다. 이 단계들은 사전 확인에서 구
플러그인이 발견된 경우에만 실행됩니다.

```bash
python3 -m ham rollout \
  --host macbook-pro \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --dry-run \
  --format json
```

## 적용

`--confirm`은 지정한 호스트의 설치·갱신과 Herdr 서버 reload를 명시적으로
승인합니다.

```bash
python3 -m ham rollout \
  --host macbook-pro \
  --host winmini \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --confirm \
  --format json
```

## 검증 프로필

기본 `smoke` 프로필은 다음을 확인합니다.

- 활성 상태
- 정확한 GitHub 소스와 확인된 commit
- manifest 버전
- `herdr config check`
- 서버 설정 reload
- `agent-skill-guide`, `label-current`, `message-agents` 액션

`--profile full`을 추가하면 대상 commit이 추적하는 모든 파일의 SHA-256을
비교하고 전체 unittest를 실행합니다. 전체 테스트에서는 bytecode 생성을
비활성화합니다.

Herdr는 현재 설치 과정에서 플러그인을 즉시 활성화합니다. 따라서 도우미는 새
설치를 바로 비활성화하고 검증한 뒤에만 다시 활성화해 서버를 reload합니다.
설치와 비활성화 사이의 짧은 구간을 줄일 수는 있지만 완전히 없앨 수는 없습니다.

## 마이그레이션과 rollback

변경 전에 현재 `herdr.agent-messenger` 또는 구 `herdr.agent-labels` 설치의
정확한 GitHub commit과 활성 상태를 기록합니다. 새 ID가 검증을 통과한 뒤에만 구
ID를 제거합니다.

설치·마이그레이션·설치 후 검증이 실패하면 기록한 ID와 상태로 best-effort
rollback을 수행합니다. 이전에 HAM이 없었다면 실패한 설치를 제거합니다. 각
호스트 결과에는 rollback 성공 여부가 포함됩니다.

호스트별 작업은 독립적이므로 한 호스트의 실패가 다음 대상을 중단하지 않습니다.
종료 코드는 다음과 같습니다.

- `0`: 모든 호스트 통과
- `1`: 하나 이상의 호스트 배포 또는 검증 실패
- `2`: 승인 누락 또는 잘못된 사전 입력

도우미는 Herdr 플러그인 등록만 갱신하며 별도로 복사한 스킬이나 설치된 런타임은
갱신하지 않습니다. [패키지 전환](migration.md#python-패키지-구조)은 다섯 호스트에서
함께 진행해야 합니다. 전송을 재개하기 전에 스킬 갱신, 활성 런타임 재설치,
수신기·hook을 불러온 세션과 HAM worker·TUI의 재시작을 마치세요.

스키마 v10에서는 enqueue를 다시 시작하기 전에 원장 코드와 CLI·복사한 스킬을
함께 갱신하세요. 이전 CLI 코드는 턴 종료 후 FYI 표시를 누락합니다. 기존 v9 전달은
표시하지 않습니다. OMP 확장은 바뀌지 않으므로 이 정책 때문에 확장을 다시 시작할
필요는 없습니다. 프로세스 교체와 DB 백업·복원 절차는
[스키마 v10 마이그레이션](migration.md#턴-종료-후-fyi스키마-v10)을 따르세요.
