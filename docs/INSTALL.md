# changeguard 설치 가이드

> **v0.16+ 자동 온보딩** — 한 명령으로 끝납니다. v0.15 이하의 수동 단계는
> `docs/UPGRADING.md` 참조.

## 사전 조건

- [`gh` CLI](https://cli.github.com) 설치 + `gh auth login` 인증
- 대상 repo에 대한 write 권한 (collaborator 또는 admin)
- (선택) Anthropic API 키 — 없어도 결정론 검사는 동작
- (선택) Slack incoming webhook URL

## Step 1 — 자동 온보딩 (한 명령)

대상 repo 디렉터리 안에서:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/sapadmin-df/changeguard/main/scripts/bootstrap-consumer.sh)
```

(또는 사전에 `--dry-run` 으로 변경 내용 확인 후 실행)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/sapadmin-df/changeguard/main/scripts/bootstrap-consumer.sh) --dry-run
```

스크립트가 수행하는 것:

1. `gh` 인증·git repo 여부 사전 체크
2. 대상 repo의 default branch 자동 감지 (`main` / `master` / `develop` 모두 지원)
3. changeguard `main` 의 최신 SHA fetch
4. 두 워크플로우 파일을 **5-10줄 reusable 호출 형태**로 생성
5. `verify-workflow-pins.sh` 로 SHA 고정 사후 검증
6. secrets 등록 안내 명령 출력 (값은 직접 입력)
7. PR 자동 생성

생성되는 워크플로우 파일 예시:

```yaml
# .github/workflows/pre-merge-review.yml (5-10줄)
name: pre-merge-review
on:
  push: { branches: [main] }
  pull_request: {}
jobs:
  review:
    uses: sapadmin-df/changeguard/.github/workflows/pre-merge-review.yml@<SHA>
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
```

## Step 2 — secrets 등록 (사용자 직접)

bootstrap 스크립트가 출력한 명령을 실행 — *값은 prompt에서 직접 입력*하므로
shell history에 노출되지 않습니다:

```bash
gh secret set ANTHROPIC_API_KEY --repo <owner/repo>
gh secret set SLACK_WEBHOOK_URL --repo <owner/repo>   # 선택
```

API key 발급 위치:
- **Anthropic**: https://console.anthropic.com/settings/keys
- **Slack webhook**: https://api.slack.com/messaging/webhooks

미등록 시 동작:
- `ANTHROPIC_API_KEY` 없음 → LLM 분석 생략, 결정론 검사만 (advisory verdict)
- `SLACK_WEBHOOK_URL` 없음 → stdout에만 출력, 채널 알림 없음

## 첫 push 후 확인

bootstrap이 만든 PR을 review·merge하면, 그 이후의 push/PR마다 게이트가 자동
실행됩니다.

- Actions 탭에서 `pre-merge-review` 잡이 보이는지
- block verdict면 Slack에 ALERT 도착하는지 (webhook 등록 시)
- PR 본문 상단에 verdict 요약 한 줄 prepend 되는지
- PR Files changed 탭의 라인 옆에 inline annotation 표시되는지

## 정책 자동 갱신 (watcher)

`policy-bump-watcher.yml` (옵션, bootstrap에서 함께 생성)이 매주 월요일 09:00 UTC
에 changeguard upstream `main` 최신 SHA를 폴링해 갱신 PR을 자동 생성합니다.
사람이 review·merge만 하면 됩니다 (자동 merge 안 함 — 정책 변경은 *명시적*).

수동 트리거:
```bash
gh workflow run policy-bump-watcher.yml --repo <owner/repo>
```

## 트러블슈팅

### "github.workflow_sha 가 비어있음"
reusable workflow를 `@<SHA>` 가 아닌 `@main`/`@master` 등 mutable ref로 호출.
supply chain 원칙 위반이라 즉시 실패. 호출자 측 `uses: ...@<40-hex-SHA>` 로 수정.

### "Policy SHA mismatch"
정책 repo의 ref가 호출 SHA와 다름. 가능한 원인:
- 정책 repo가 force-push로 SHA가 사라짐 → 정책 repo 관리자에게 문의
- 소비자 워크플로우의 `uses: ...@<SHA>` 오타

### "ANTHROPIC_API_KEY 미설정" meta finding
LLM 단계 생략 중. 정책상 결정론만 운영하려는 의도면 무시 가능. 운영 환경에서는
secret 등록 권장.

### Slack 알림이 안 옴
- `SLACK_WEBHOOK_URL` secret 등록 여부 확인
- `pass` verdict는 알림 안 보냄 (noise 방지 — block/advisory만)
- webhook URL 유효성: `curl -X POST -H 'Content-Type: application/json' -d '{"text":"test"}' <URL>`

### PR comment가 없음
- `pull-requests: write` 권한 필요 — reusable workflow가 이미 자동 설정. 호출자
  측 workflow에서 추가 조정 불필요.

## 정책 변경 절차 (정책 본부 운영자용)

소비자 측 가이드는 위로 충분. 정책 본부 운영자라면 `README.md` 의 "정책 변경
절차" + `docs/UPGRADING.md` 참조.
