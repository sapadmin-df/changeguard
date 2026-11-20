# 피드백 루프 — 실행 결과에서 개선점을 도출하는 법 (v0.18+)

changeguard의 판정(정적·LLM)과 CI 성능을 **실행 기록 → 집계 → 개선 후보 → fixture·정책 PR
→ 정책 버전별 재측정** 순으로 반복 개선한다. 이 문서가 루프의 SSOT다.

```
소비자 CI 실행 ──► review-record.json (artifact, 90일)
      │                 │
      │ (opt-in)        ▼
      └──► telemetry issue ◄── 사람 피드백 (라벨 fb:* · 댓글 /cg fp <id>)
                        │
                        ▼
        scripts/cg-insights.py ── 보고서 · 원장(runs/findings/feedback.csv, ledger.sqlite)
                        │
                        ▼
        개선 후보 → fixture 먼저 → changeguard PR → 정책 버전 ↑ → 소비자 bump
                        │
                        └──► --policy-version 으로 전후 비교 (다음 회차의 입력)
```

## 1. 무엇이 기록되나 — 정규화 레코드 `changeguard.review/v1`

모든 실행이 `review-record.json` (artifact `pre-merge-review-<run_id>`, 보존 90일)을 남긴다.
telemetry를 켜지 않아도 남는다. 생성: `skills/pre-merge-review/checks/normalize.py`.

| 필드 | 내용 | 쓰임 |
|---|---|---|
| `run` | repo · event · run_id · PR 번호 · head SHA · run URL | 다른 기록과 조인, 원문 이동 |
| `policy.version` | 정책 버전 | 개선 전후 비교 |
| `verdict` · `verdict_driver` | 판정과 그 주체 (`deterministic`·`llm`·`llm_unavailable`·`none`) | "누가 막았나" |
| `severity_max` · `disagreement` | 정적/LLM 최고 심각도, 불일치 (`llm_stricter`·`det_stricter`·`agree`·`det_only`) | 정적↔LLM 정규화 후보 |
| `signals` | 분석이 필요한 이유 (아래) | telemetry 전송 조건, 필터 |
| `llm` | state · failure_class · model · fallback · usage(토큰) · latency_ms · intent | 가용성·비용·모델 drift |
| `timings` | deprecation · bump_verify · deterministic · adapter · llm · total (ms) | CI 성능 |
| `findings[]` | **rule** · **id** · source · severity · category · path_class (+ full: path·line·description) | 규칙별 오탐·누락 |

**rule** 은 안정적인 규칙 ID다 (`det.pattern.pipe_to_shell`, `llm.network`,
`llm.unavailable.billing` …). 사람용 문구가 바뀌어도 집계가 깨지지 않게 **집계는 항상 rule로**
한다. **id** = `sha1(rule|path|line)[:12]` — 피드백 대상 지목과 run 간 추적의 키.
**path_class** (`code`·`test`·`agent`·`doc`·`workflow`·`lockfile`)는 "테스트 파일에서만
오탐" 같은 문맥 분석의 축이다.

signals: `verdict_block` · `verdict_advisory` · `llm_stricter` (LLM이 정적보다 엄격) ·
`det_fp_candidate` (정적 high+ 인데 LLM이 의도된 변경으로 봄) · `llm_failed` · `llm_refused` ·
`llm_truncated` · `llm_parse_error` · `llm_no_key` · `self_lint`.

v0.17 이하 결과(`review-result.json`)도 `normalize.py`가 문구에서 rule·상태를 추론해
소급 변환한다 — 과거 artifact를 그대로 집계할 수 있다.

## 2. telemetry — 분석 포인트를 issue로 연결 (opt-in, 기본 off)

기본값은 **off**. 소비자가 명시적으로 켤 때만, `signals`가 있는 실행만 보낸다.

```yaml
jobs:
  review:
    uses: sapadmin-df/changeguard/.github/workflows/pre-merge-review.yml@<SHA>
    with:
      policy_sha: <SHA>
      llm_model: ${{ vars.LLM_MODEL }}
      telemetry: full                          # off | minimal | full
      # telemetry_repo: sapadmin-df-desk/changeguard-telemetry  # 기본값 — private 접수함 (DP-1b)
      telemetry_share_private: true            # private 소비자 소유자의 full 동의
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
      CHANGEGUARD_TELEMETRY_TOKEN: ${{ secrets.CHANGEGUARD_TELEMETRY_TOKEN }}
```

- **공유 지점 (v0.19, DP-1b)**: 원본 기록은 운영자의 **private 접수함**
  `sapadmin-df-desk/changeguard-telemetry` 에 받고, 공개는 집계 결과로 한다 (REQUIREMENTS G6·Gap 9).
  접수함은 초대된 계정만 본다 — desk org 기본 권한 none, 사람 계정 triage(피드백 라벨), 봇 read.
  공개 repo를 `telemetry_repo` 로 지정하는 것도 여전히 가능하다 (아래 공개 대상 규칙 적용).
- **토큰**: issue 작성·댓글**만** 가능한 전용 봇 계정의 토큰. 접수함이면 봇을 desk org 멤버 +
  repo **read** 로 두고 fine-grained PAT(그 repo 하나, Issues 쓰기)를 쓴다 — org 밖 협력자는
  classic PAT만 쓸 수 있어 범위가 넓어진다. 공개 repo면 아무 권한 없는 계정. **triage 이상은 주지
  않는다** — 라벨 조작·남의 issue 종결이 가능해 소비자가 피드백 원장의 공격 지점이 된다
  (REQUIREMENTS REQ-C.3-5, DP-5b).
  issue는 라벨 없이 제목 접두사 `[cg-telemetry]`로 식별하고, 같은 키의 기존 issue는 그 계정이 연 것만 이어 쓴다.
  소비자 repo의 `GITHUB_TOKEN`은 다른 repo에 쓸 수 없다.
- **이슈 단위**: PR 실행은 PR마다 한 이슈, push 실행은 커밋마다 한 이슈. 같은 PR의 다음
  실행은 같은 이슈에 댓글로 레코드를 덧붙인다 (재실행·후속 커밋 추적).
- **열람자 보호 (v0.19)**: 소비자가 private이면 telemetry repo가 public이든 private 접수함이든
  `minimal`로 낮춘다 (파일 경로·finding 문구·LLM 서술 제외) — private이라도 열람자는 소비자 repo와
  다르다. minimal에도 repo 이름과 run 링크는 남는다.
- **소유자 동의**: private 소비자의 소유자가 `telemetry_share_private: true` 를 주면 full로
  보낸다. 동의는 소비자 workflow 파일에 남으므로 기록된다. 대상이 private 접수함이면 high/critical
  상세까지, 공개 repo면 medium 이하 문구까지.
- **high/critical 상세 비공개**: 공개 telemetry repo에는 high/critical finding의 경로·라인·문구와
  LLM 서술을 싣지 않고 `redacted: "high+"` 와 규칙·id만 남긴다 (아직 고치지 않은 취약점을 공개
  지점에 모으지 않음). 원본은 소비자 run의 artifact에 있다.
- **diff 유래 문구**: 표에서는 code span 안에만(@멘션·링크 무력화), 레코드 문구는 300자 제한,
  JSON 블록은 내용 속 백틱보다 긴 fence로 감싼다.
- **미리보기 (v0.18.2)**: telemetry가 켜져 있으면 공개 issue에 실릴 본문을 소비자 run의 step
  summary에 그대로 남긴다. **토큰 없이 켜면 보내지 않고 미리보기만** 남으므로, 켜기 전에 무엇이
  공개되는지 자기 run에서 먼저 확인할 수 있다 (대상 공개 여부를 모르면 공개로 간주해 보수적으로 표시).
- 경계의 SSOT는 [REQUIREMENTS.md](REQUIREMENTS.md) REQ-C.3, 결정 분기점은 §D.
- 실패해도 게이트에는 영향이 없다 (경고만).

issue를 고른 이유: 실행 링크·사람 피드백(라벨·댓글)·검색이 한곳에서 되고 별도 인프라가
없다. 대량 집계는 issue가 아니라 레코드 블록(` ```json changeguard-record `)을
`cg-insights.py`가 뽑아 원장으로 만든다. telemetry 없이도 artifact로 같은 집계가 된다.

## 3. 사람 피드백 — 판정이 맞았나

telemetry issue에서:

| 방법 | 범위 | 예 |
|---|---|---|
| 라벨 `fb:true-positive` · `fb:false-positive` · `fb:noise` · `fb:missed` · `fb:llm-too-strict` · `fb:llm-too-lenient` | 실행 전체 | 테스트 파일 오탐으로 막혔으면 `fb:false-positive` |
| 댓글 `/cg fp <finding_id> 메모` (`tp`·`fp`·`noise`·`missed`) | finding 하나 | `/cg fp 3f9a1c2b7d10 부정 테스트의 문자열` |

finding id는 PR 코멘트의 각 finding 옆(`rule · id`)에 표시된다. 피드백은 `rule`로 집계된다.
댓글 약어(`tp`·`fp`)는 라벨 이름(`true-positive`·`false-positive`)으로 통일해 집계한다.

- **신뢰**: 라벨은 triage 권한자만 붙일 수 있어 그대로 신뢰한다 (그래서 봇에 triage를 주지 않는다).
  `/cg` 댓글은 repo 권한자(OWNER·MEMBER·COLLABORATOR) 것만 센다. 단 **레코드를 올리는 계정**
  (issue 작성자·`--trust`)의 댓글은 권한자로 보여도 세지 않는다 — 접수함에서는 봇도 COLLABORATOR다
  (REQ-C.3-7, `rejected.uploader_feedback`).
- **Git Bash 주의**: `gh issue comment -b "/cg tp …"` 는 MSYS 경로 변환으로 `C:/Program Files/Git/cg …`
  로 올라간다 (실측). `MSYS_NO_PATHCONV=1` 을 붙이거나 웹에서 쓴다.

## 4. 집계 — `scripts/cg-insights.py`

```bash
# 소비자 artifact 소급 분석 (gh 인증 필요) + job·step 런타임까지
python3 scripts/cg-insights.py --artifacts dfocus-sapsol/oh-my-bas --job-timing --ledger out/

# telemetry issue + 피드백
python3 scripts/cg-insights.py --issues sapadmin-df-desk/changeguard-telemetry --trust changeguard-telemetry-bot --ledger out/
# --trust: 이 작성 계정의 레코드만. /cg 댓글 피드백은 repo 권한자 것만, 작성 계정 댓글은 제외
# v0.18 시험 기록(공개): --issues sapadmin-df/changeguard

# 정책 버전 하나만 (개선 전후 비교)
python3 scripts/cg-insights.py --artifacts <repo> --policy-version 0.18.0
```

보고서: 판정 개요 · LLM 가용성·비용 · 단계별 런타임(p50/p90/max) · diff 크기별 런타임 ·
**정책 버전별 비교** · 규칙별 통계 · 정적 0인데 LLM medium+ 범주 · 개선 후보.

### 원장 (`--ledger DIR`)

| 파일 | 행 단위 | 열기 |
|---|---|---|
| `runs.csv` | 실행 1건 | 엑셀·스프레드시트 (UTF-8 BOM — 한글 그대로) |
| `findings.csv` | finding 1건 | 〃 |
| `feedback.csv` | 피드백 1건 | 〃 |
| `ledger.sqlite` | 위 3개 테이블 | `--sql "…"` 또는 sqlite3·DB Browser |

`runs` 주요 열: run_id · repo · event · pr_number · created_at · policy_version · verdict ·
verdict_driver · disagreement · signals · det_max · llm_max · gating_call_llm · llm_state ·
llm_failure_class · llm_model · llm_fallback · llm_intent · input_tokens · output_tokens ·
cost_usd_est · diff_files · diff_bytes · deprecation_ms · bump_verify_ms · deterministic_ms ·
adapter_ms · llm_ms · total_ms · ci_queue_ms · ci_job_ms · ci_steps · run_url · telemetry_issue

### 바로 쓰는 질의

```sql
-- 누가 막았나: 정적 규칙별 차단과 경로 분류 (테스트·주석 오탐 찾기)
select rule, path_class, count(*) n, sum(verdict='block') blocks
from findings where source='deterministic' group by 1,2 order by n desc;

-- LLM이 정적보다 엄격했던 실행
select run_id, verdict, llm_max, signals, run_url from runs where disagreement='llm_stricter';

-- LLM 가용성: 실패 원인별
select llm_failure_class, count(*) from runs where llm_state in ('failed','no_key') group by 1;

-- CI 성능: LLM 호출 여부별 job 시간
select gating_call_llm, count(*) n, avg(ci_job_ms)/1000 job_s, max(ci_job_ms)/1000 max_s from runs group by 1;

-- 성격별 단계 런타임 (v0.18+)
select avg(deterministic_ms), avg(llm_ms), avg(total_ms) from runs where total_ms is not null;

-- 비용: 정책 버전별
select policy_version, sum(cost_usd_est), sum(input_tokens), sum(output_tokens) from runs group by 1;

-- step별 시간 (job-timing)
select json_extract(ci_steps,'$."Run pre-merge review"')/1000.0 review_s, ci_job_ms/1000.0 job_s
from runs where ci_steps is not null order by job_s desc limit 10;

-- 피드백: 규칙별 사람 판정
select f.rule, fb.kind, count(*) from feedback fb join findings f on f.finding_id = fb.target group by 1,2;
```

## 5. 개선 후보 → 반영 (재귀 루프 1회차)

후보 판단 규칙은 `cg-insights.py`의 `CANDIDATE_*`·`PERF_*` 상수로 고정되어 있다 (같은 입력 →
같은 후보).

| 후보 | 조건 | 다음 행동 |
|---|---|---|
| `relax-or-contextualize` | 정적 규칙이 verdict를 좌우한 run ≥ 3, 그중 LLM '의도된 변경' ≥ 80% | path_class·문맥 확인 → 문맥 규칙(테스트 경로·주석 등) 검토 |
| `human-fp` | 규칙별 사람 fp 피드백 ≥ 2 | 해당 finding 원문 확인 → fixture |
| `promote-to-static` | 정적 0인 run에서 같은 LLM 범주 medium+ ≥ 2 | 반복되는 *구체적 패턴*이 있으면 정적 규칙화 (예: v0.18 pipe-to-shell 변형) |
| `ops` | LLM 미가용 billing·quota·auth·model_not_found | 운영 조치 (크레딧·키·모델) |
| `perf` | LLM p90 ≥ 60s · 정적 p90 ≥ 20s · diff 상한 초과 | 분할·제외·effort 조정 |

반영 절차 (정책 repo):
1. 후보의 원문(run_url·path·line)을 확인해 **판정이 실제로 틀렸는지** 사람이 결정한다.
   후보는 결론이 아니다.
2. **fixture를 먼저** 추가한다 (`examples/` — 오탐이면 benign/edge, 누락이면 malicious).
3. 규칙·프롬프트를 고치고 fixture·단위 테스트를 통과시킨다.
4. 정책 PR → VERSION ↑ → README 변경 이력.
5. 소비자 bump 후 `--policy-version` 으로 전후를 비교한다 → 다음 회차 입력.

정적 결과를 LLM 판단으로 낮추는 규칙은 만들지 않는다 (설계 원칙). 루프가 바꾸는 것은
**정적 규칙 자체의 문맥 인식**과 **프롬프트의 기준**이다.

## 6. 1회차 기록 (2026-11-18, oh-my-bas, v0.17.1, 751 run)

| 관찰 | 결정 | 반영 |
|---|---|---|
| 비정상 종료 5건 — 비 UTF-8 diff에서 `ci-caller` 크래시, 검토 없이 master 반영 | 버그 | v0.18 수정 |
| `curl … \| VAR=x sh -` 를 정적이 놓쳐 LLM만 medium — code-server(`\| sh`)는 정적 critical | 정규화 (정적 규칙화) | v0.18 M20 |
| LLM 미가용 42건 전부 billing(36)·quota(6) | 운영 | failure_class 기록 → ops 후보 |
| 결과 파일에 run 키·rule·토큰·런타임 없음, artifact 30일 | 기록 형식 | v0.18 정규화 레코드, 90일 |
| `det.injection.keyword`(테스트 3건)·`det.pattern.pipe_to_shell`(테스트·주석) 오탐 | 문맥 규칙 후보 | 결정 대기 (테스트 경로 하향·주석 제외) |
| `llm.diff_truncated` 22회 → advisory 17회 (대형 merge) | 성능·소음 후보 | 결정 대기 |
| LLM이 정적보다 엄격 17건 — 대부분 정당(TLS 검증 끔, 신원 헤더 제거, 공인 DNAT 등) | 유지 | — |
