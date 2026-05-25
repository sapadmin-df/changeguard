# Upgrading changeguard

## v0.15 → v0.16 (Reusable workflow 전환)

v0.16부터 changeguard는 **template 복사** 방식에서 **reusable workflow** 방식으로
전환되었습니다. 핵심 효과:

- 소비자 워크플로우 YAML이 **100+ 줄 → 5-10줄**로 축소
- action SHA pinning, branch 감지, 권한 조정 등 *수동 단계가 사라짐*
- 한 명령 (`bootstrap-consumer.sh`) 으로 끝나는 자동 온보딩

### v0.15와의 호환성

기존 v0.15 이하 소비자 워크플로우(`.template` 복사본)는 **즉시 깨지지 않습니다.**
다만 changeguard 측의 `.template` 파일이 제거되었으므로:

- 새로 onboarding하는 repo는 **반드시** 새 방식을 사용해야 합니다
- 기존 소비자(예: `oh-my-bas`)는 *가능한 빠른 시점*에 마이그레이션 권장

### 마이그레이션 — 두 가지 길

#### A. 자동 (권장)

기존 워크플로우 파일을 *그대로 두고* bootstrap 스크립트를 실행하면 새 형태로
**덮어쓰기 확인**을 묻습니다. yes 하면 한 번에 마이그레이션 완료:

```bash
cd <대상 repo>
bash <(curl -fsSL https://raw.githubusercontent.com/sapadmin-df/changeguard/main/scripts/bootstrap-consumer.sh)
```

`--dry-run` 으로 사전 diff 확인 가능.

#### B. 수동

기존 `.github/workflows/pre-merge-review.yml` 전체를 다음으로 교체:

```yaml
name: pre-merge-review
on:
  push:
    branches: [<your-default-branch>]    # main / master / develop
  pull_request: {}
jobs:
  review:
    uses: sapadmin-df/changeguard/.github/workflows/pre-merge-review.yml@<v0.16-SHA>
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
```

`policy-bump-watcher.yml` 도 동일 패턴:

```yaml
name: policy-bump-watcher
on:
  schedule: [{ cron: '0 9 * * 1' }]
  workflow_dispatch: {}
jobs:
  bump:
    uses: sapadmin-df/changeguard/.github/workflows/policy-bump-watcher.yml@<v0.16-SHA>
    secrets:
      SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
```

`<v0.16-SHA>` 는 https://github.com/sapadmin-df/changeguard/commits/main 에서
최신 commit SHA로 교체.

### 변경 사항 요약

| 항목 | v0.15 이하 | v0.16+ |
|---|---|---|
| 분배 방식 | `.template` 파일 복사 | reusable workflow 호출 |
| 소비자 워크플로우 분량 | 100+ 줄 | 5-10줄 |
| 정책 SHA 위치 | `env.POLICY_REPO_SHA` | `uses: ...@<SHA>` |
| action SHA 손으로 pin | 4-6개 placeholder | 정책 본부 측이 관리 |
| default branch 조정 | 손으로 main↔master | bootstrap이 자동 감지 |
| permissions 조정 | 손으로 read→write | reusable이 자동 |
| secrets 전달 | env: 명시 | `secrets:` 명시적 pass-through |
| 온보딩 시간 | 30분~2시간 | 1-2분 |

### Watcher 호환성

`policy-bump-watcher.yml` 의 SHA 감지는 v0.15/v0.16 둘 다 호환:
- v0.16+ : `uses: .../pre-merge-review.yml@<SHA>` 의 SHA를 추출
- v0.15- : `POLICY_REPO_SHA: "<SHA>"` env 변수를 fallback으로 추출

따라서 watcher가 만드는 자동 bump PR이 두 방식 모두에서 정상 동작합니다.

## v0.14 → v0.15

자동 검증 + finding 메시지 self-lint 추가. 소비자 액션 불필요 (정책 코드만 변경).
다음 SHA bump 시 자동 활성.

## v0.13 → v0.14

LLM 분석 narrative 채널 추가. 소비자 액션 불필요.

## 이전 버전

README의 v0.10~v0.13 섹션 참조.
