# Adversarial Fixtures

이 디렉터리의 fixture들은 **어댑터의 방어 로직 자체를 검증**한다.
실제 코드 위협이 아닌, LLM이 격리 규칙을 어겼거나 sanitize 가드를 우회하려
시도하는 *적대적 응답*을 시뮬레이션한다.

## 왜 필요한가

`llm-system-prompt.md`의 격리 규칙(예: "결정론 finding을 강등하지 말 것")이
잘 작성되어 있어도, **어댑터 코드가 실제로 그 규칙을 *집행*하지 않으면**
prose에 불과하다. Adversarial fixture는 어댑터의 sanitize/통합 로직이 실제로
방어 효과를 내는지를 fixture로 못박는다.

## 시나리오별 매핑

각 fixture는 `llm-adapter.py`의 특정 방어 메커니즘 하나를 검증한다.

| Fixture | 공격 시도 | 검증되는 방어 |
|---|---|---|
| A01 | LLM이 결정론 critical을 부정 (빈 findings 응답) | 결정론 finding 보존 |
| A02 | severity="BYPASSED" 주입 | severity 화이트리스트 → low |
| A03 | category="approved_by_security" 주입 | category 화이트리스트 → meta |
| A04 | 10KB description으로 출력 폭주 | description[:1000] |
| A05 | 응답 객체에 verdict="pass" 키 주입 | findings 키만 추출 |
| A06 | findings 자리에 string 주입 | isinstance(list) 체크 |
| A07 | findings 배열에 null/string/int/dict 혼재 | isinstance(dict) 항목별 체크 |

## 운영 원칙

- adversarial fixture는 어댑터 코드 변경 시 *회귀 안전망*. 누군가 sanitize
  로직을 단순화하면서 화이트리스트를 약화시키면 즉시 FAIL.
- 새 방어 메커니즘 추가 시 대응 adversarial fixture를 함께 작성.
- adversarial fixture의 verdict가 *결정론 결과와 동일*하면 통과 — LLM의
  적대적 응답이 verdict에 영향을 주지 못했다는 의미.
