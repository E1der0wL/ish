# 과거 실험 기록

이 문서들은 VT 기반 ish로 통합하기 전에 사용한 명령과 당시의 검증 결과를 보존한다. 현재 버전을 실행하거나 검증하려면 [사용 안내](../VT_USAGE.md), [개발·검증 안내](../DEVELOPMENT.md), [자동 회귀 명세](../VT_TEST_CONTRACTS.md)를 참고한다.

| 단계 | 기록 |
|---|---|
| 초기 VT 실험 | [화면 합성과 native 셸 편집](TERMINAL_EXPERIMENT.md) · [기능별 검증 범위](TERMINAL_CAPABILITIES.md) · [이미지·클립보드·호스트 계약](TERMINAL_PROTOCOLS.md) |
| 셸 hook 없는 UI | [기본 화면과 플러그인 시나리오](HOOKLESS_VT.md) |
| 기본 지원 명세 | [변경 전 감사와 명세 ID](VT_BASELINE_AUDIT.md) · [보완 모듈 도입](VT_IMPLEMENTATION.md) · [기본 계약 보강 결과](VT_BASELINE_RESULT.md) |
| 입력·클립보드·terminfo 검증 | [보완 모듈 검증 결과](VT_SUPPLEMENTS_RESULT.md) · [terminfo 후속 대조](VT_TERMINFO_RESULT.md) |

당시 수집한 terminfo 287개 항목은 지금도 자동 회귀의 비교 기준으로 사용한다. 원본 값은 [terminfo 카탈로그](../VT_TERMINFO_CATALOG.md)에 있다.
