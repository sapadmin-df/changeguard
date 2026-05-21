# Quickstart — oh-my-bas에 changeguard 적용

`dfocus-sapsol/oh-my-bas`에 changeguard pre-merge-review를 적용하는 구체 절차.
일반 가이드는 `INSTALL.md` 참조.

## 권한 구성 (이 케이스)

| 역할 | 계정/repo | 권한 |
|---|---|---|
| 정책 본부 | `sapadmin-df/changeguard` (public) | sapadmin-df: write / 그 외: read |
| 정책 검토자 | `wschoe2020` | changeguard read (PR 제안 가능, merge 불가) |
| 대상 repo | `dfocus-sapsol/oh-my-bas` | wschoe2020: write |

핵심 격리: **oh-my-bas를 수정할 수 있는 계정(wschoe2020)이 changeguard 정책은
바꿀 수 없다.** 정책 변경에는 sapadmin-df의 승인이 필요. 검사 도구와 검사 대상의
권한이 분리되어, oh-my-bas의 악성 PR이 정책 자체를 무력화할 수 없다.

changeguard가 public이므로 oh-my-bas의 Action은 토큰 없이 정책을 내려받는다.

## Step 0 — changeguard repo 생성 (sapadmin-df 계정)

```bash
# 로컬에서 scaffold 압축 해제
cd ~/Downloads
tar -xzf changeguard-v1.0.tar.gz
cd changeguard

# repo 루트에 내용물이 바로 오도록 push
git init
git add -A                    # .github 포함 전부 — 빼지 않는다
git commit -m "changeguard: pre-merge-review gate v0.9"
git branch -M main
git remote add origin https://github.com/sapadmin-df/changeguard.git
git push -u origin main

# 이 commit의 SHA 확보 — 다음 단계에서 사용
git rev-parse HEAD
```

push 후 GitHub에서 sapadmin-df → changeguard → Settings:
- **Settings → Collaborators**: wschoe2020을 **Read** 권한으로 추가
- **Settings → Rules → Rulesets**: `main` 보호 ruleset `protect-main` — 서명 필수,
  PR + Code Owner 승인, CI status check, force-push/삭제 차단. 규칙 상세는
  changeguard `README.md`의 "브랜치 보호 & 커밋 서명" 참조

## Step 1 — oh-my-bas에 워크플로우 추가 (wschoe2020 계정)

```bash
# oh-my-bas clone (또는 기존 작업 디렉터리)
git clone https://github.com/dfocus-sapsol/oh-my-bas.git
cd oh-my-bas
git checkout -b add-changeguard

mkdir -p .github/workflows
CG_SHA="<Step 0에서 확보한 changeguard SHA>"
curl -fsSL \
  "https://raw.githubusercontent.com/sapadmin-df/changeguard/${CG_SHA}/.github/workflows/pre-merge-review.yml.template" \
  -o .github/workflows/pre-merge-review.yml
```

## Step 2 — FIXME 채우기

```bash
# changeguard를 옆에 clone하여 보조 스크립트 사용
git clone https://github.com/sapadmin-df/changeguard.git ../changeguard
gh auth login
../changeguard/scripts/pin-actions.sh --apply .github/workflows/pre-merge-review.yml
```

그리고 워크플로우 파일에서 직접 채울 것:
- `POLICY_REPO_SHA`: `<Step 0의 changeguard SHA>` (기본값 0000...을 교체)
- `POLICY_REPO`는 이미 `sapadmin-df/changeguard`로 설정됨 — 확인만

## Step 3 — 검증

```bash
# 모든 uses:가 SHA로 고정됐는지, FIXME가 남았는지 확인
../changeguard/scripts/verify-workflow-pins.sh .github/workflows/pre-merge-review.yml
# 기대 출력: "All workflow uses: are pinned to commit SHA. OK."
```

`POLICY_REPO_SHA`가 아직 0000...이면 위 스크립트는 이를 잡지 못하므로(SHA 형식은
맞음) 육안으로 한 번 더 확인한다.

## Step 4 — secrets 등록 (oh-my-bas)

oh-my-bas → Settings → Secrets and variables → Actions:

| Secret | 값 | 필수 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API 키 | ✓ |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook | 선택 |

`SLACK_WEBHOOK_URL` 미등록 시 알림은 워크플로우 로그(stderr)로만 출력된다.
`ANTHROPIC_API_KEY` 미등록 시 LLM 단계를 건너뛰고 결정론 검사만 수행하며,
그 사실이 medium meta finding으로 기록되어 advisory verdict가 된다.

## Step 5 — 첫 실행 + PR

```bash
git add .github/workflows/pre-merge-review.yml
git commit -m "ci: add changeguard pre-merge-review"
git push -u origin add-changeguard
# GitHub에서 PR 생성 → 이 PR 자체가 첫 워크플로우 실행을 트리거
```

첫 실행에서 확인:
- "Sanity check — no floating refs" 통과 (FIXME 모두 해소됐는지)
- "Verify policy SHA matches expectation" 통과 (SHA mismatch 없는지)
- "Run pre-merge review" 출력에 verdict, policy_version, policy_sha 표시

## changeguard 정책 갱신 시 (이후 운영)

changeguard에 새 commit이 생겨도 oh-my-bas는 자동으로 따라가지 않는다.
적용하려면 oh-my-bas에서 워크플로우의 `POLICY_REPO_SHA`를 새 SHA로 바꾸는
PR을 올린다. 이 PR 자체가 pre-merge-review를 거치므로 자기 검증된다.

## 트러블슈팅

자세한 내용은 `INSTALL.md`의 트러블슈팅 섹션 참조. oh-my-bas 특이사항:

- **"Policy SHA mismatch"**: `POLICY_REPO_SHA`가 changeguard의 실제 commit과
  다름. Step 0의 `git rev-parse HEAD` 결과와 워크플로우 값을 재대조.
- **changeguard checkout 실패**: public repo이므로 토큰 문제는 아님. SHA가
  존재하지 않는 commit을 가리키는지 확인 (force push로 사라진 SHA 등).
