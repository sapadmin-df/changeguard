# 대상 Repo 설치 가이드

이 가이드는 **대상 repo**(보호하려는 application repo)에 changeguard의
pre-merge-review 워크플로우를 설치하는 절차다.

정책 본부: `sapadmin-df/changeguard` (public)

## 사전 조건

- 대상 repo의 GitHub Actions가 활성화되어 있을 것
- 대상 repo에 secrets 등록 권한 보유:
  - `ANTHROPIC_API_KEY` (필수)
  - `SLACK_WEBHOOK_URL` (선택)
- changeguard의 최신 안정 commit SHA 확보
- changeguard는 public이므로 정책 접근 토큰 불필요

## 설치 단계

### 1. 워크플로우 템플릿 복사

```bash
# changeguard에서 템플릿 가져오기 — SHA 고정으로 fetch
POLICY_SHA="<changeguard의 commit SHA>"
curl -fsSL \
  "https://raw.githubusercontent.com/sapadmin-df/changeguard/${POLICY_SHA}/.github/workflows/pre-merge-review.yml.template" \
  -o .github/workflows/pre-merge-review.yml
```

### 2. FIXME 채우기

워크플로우 파일을 열면 `<FIXME-...>` 플레이스홀더가 있다. 두 가지 방법:

**자동:** changeguard의 보조 스크립트 사용

```bash
# changeguard clone 후
gh auth login  # gh CLI 인증
./changeguard/scripts/pin-actions.sh --apply .github/workflows/pre-merge-review.yml
```

**수동:** GitHub Marketplace에서 각 action의 최신 release tag를 찾고, 해당 tag의 commit SHA 조회

```bash
# 예: actions/checkout v4
gh api /repos/actions/checkout/git/refs/tags/v4.1.7 --jq .object.sha
```

또한 다음 두 변수도 채워야 한다:

- `POLICY_REPO`: 정책 repo의 `owner/name`
- `POLICY_REPO_SHA`: 정책 repo의 40자 commit SHA

### 3. 검증

```bash
bash changeguard/scripts/verify-workflow-pins.sh \
  .github/workflows/pre-merge-review.yml
# 출력: "All workflow uses: are pinned to commit SHA. OK."
```

검증 실패 시 어떤 라인의 어떤 ref가 문제인지 표시된다. exit 1이면 절대
commit하지 말 것.

### 4. Secrets 등록

대상 repo의 Settings → Secrets and variables → Actions에서:

| Secret | 용도 | 필수 |
|---|---|---|
| `ANTHROPIC_API_KEY` | LLM 분석 | ✓ |
| `SLACK_WEBHOOK_URL` | verdict 알림 | 선택 (없으면 stdout만) |
| `POLICY_REPO_READ_TOKEN` | private 정책 repo 접근 | private만 |

`POLICY_REPO_READ_TOKEN`은 **machine user**의 PAT 사용 권장 — 개인 토큰
사용 시 그 사람이 떠나면 워크플로우가 마비된다.

### 5. 첫 push

워크플로우가 main으로 push되면 Actions 탭에서 첫 실행이 시작된다. 첫 실행에서
확인할 점:

- Step "Sanity check"이 통과 (FIXME 모두 해소됐는지)
- Step "Verify policy SHA"가 통과 (SHA mismatch 없는지)
- Step "Run pre-merge review"의 출력에 `verdict`, `policy_version`, `policy_sha` 표시

## 정책 repo 갱신 절차

정책 repo에 새 commit이 생기면 *자동으로* 받아들이지 않는다. 명시적 절차:

1. 정책 repo의 변경 사항 검토 (CHANGELOG, fixture 회귀 결과)
2. 새 SHA로 대상 repo 워크플로우 PR 생성:
   ```yaml
   POLICY_REPO_SHA: "<새 commit SHA>"
   ```
3. 이 PR 자체에 대해 pre-merge-review가 실행됨 (자기 검증)
4. 정책 변경의 영향을 review한 뒤 merge

이 절차는 "정책이 변경됐다는 사실을 무비판적으로 신뢰하지 않는다"는 약속의
운영적 구현이다.

## 트러블슈팅

### "Policy SHA mismatch"
정책 repo의 ref가 환경변수의 SHA와 다르다. 가능한 원인:
- branch가 SHA로 강제 reset됨 → 정책 repo 관리자에게 문의
- `POLICY_REPO_SHA` 환경변수에 오타 → 워크플로우 파일 확인

### "ANTHROPIC_API_KEY 미설정" meta finding
LLM 단계를 건너뛰고 결정론만 사용 중. 운영 환경에서는 secret을 등록할 것.
임시로 결정론만 운영하려면 의도된 동작.

### Slack 알림이 안 옴
- `SLACK_WEBHOOK_URL` 등록 확인
- pass verdict는 알림 안 함 (noise 방지) — block/advisory에서만 발송
- webhook URL이 유효한지 `curl -X POST ... <url>` 으로 직접 검증

### PR comment가 없음
- `github-script` step 실패 — Actions 로그의 해당 step 확인
- permissions에 `pull-requests: read`만 있고 `write`가 없으면 comment 불가
  → permissions를 `pull-requests: write`로 조정 (보안 검토 후)

## 운영 후 정기 체크리스트

- **분기마다**: 정책 repo의 SHA 갱신 검토 (새 보안 패턴 반영)
- **분기마다**: action SHA 갱신 (각 action의 새 릴리스 적용)
- **월마다**: Slack 알림 false positive 비율 확인 — 5% 이상이면 정책 조정
- **이상 시**: drift 알림 발생 시 즉시 mock vs production 비교 분석
