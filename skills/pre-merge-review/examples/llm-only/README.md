# LLM-Only Fixtures

이 디렉터리의 fixture들은 **결정론적 검사로는 잡을 수 없는** 위협 시나리오다.
LLM 계층이 구현되면 함께 검증한다.

기본 `run-tests.sh`는 이 디렉터리를 검사하지 않는다.
LLM 호출 어댑터 작성 시 별도 runner를 추가할 예정.

## 현재 등록된 fixture

- **M06-env-exfiltration**: `process.env`를 외부 https endpoint로 POST.
  정규식 시그니처가 없어 결정론 계층은 통과시킨다. LLM의 맥락 판단이 필요.

## 이런 fixture를 두는 이유

"우리 시스템이 무엇을 막을 수 없는가"를 명시적으로 문서화한다.
보안 시스템에서 가장 위험한 것은 "안전하다고 잘못 믿는 것"이므로,
이 디렉터리의 존재 자체가 운영자에게 한계를 상기시킨다.
