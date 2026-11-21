# 기본 명세와 유지할 자동 회귀 사례

2026-11-18. [기본 지원 명세](history/VT_BASELINE_AUDIT.md), 보완 모듈·기능 정책·선택 확장 계약의 ID에 연결된 필수 회귀를 삭제·이름 변경으로부터 보호한다. 정확한 목록은 아래 검사 코드가 소유한다. 이것은 명세 전체의 합격표가 아니다. 각 행은 아래의 특정 자동 사례만 뜻하며, 물리 입력·픽셀·실제 호스트 증거는 [기능 정책 검증](VT_FEATURE_POLICY.md), [보완 모듈 검증](history/VT_SUPPLEMENTS_RESULT.md), [이전 검증 결과](history/VT_BASELINE_RESULT.md)에서 별도로 판정한다.

정확한 `module.Class.test_method` 목록은 `tools/terminal_contracts.py`의 `REQUIRED_CASES`가 소유한다. 같은 테스트가 여러 명세의 일부를 검증할 수 있지만 실행은 한 번만 한다. 아래 설명은 해당 사례의 범위이며, 같은 ID의 나머지 조합을 검증 완료로 만들지 않는다.

| ID | 보호하는 자동 사례 | 테스트 경계 |
|---|---|---|
| MG01 | 카테고리 도움말·실제 설정 저장·외부 편집기·플러그인 비실행·기능 선택 | `test_management_ui` |
| MG02 | 원본 보존·외부 수정 충돌·symlink·설정 객체 identity·비실행 메타데이터 | `test_management_store` |
| MG03 | 관리 창의 시작 상태 복원·관측 중 입력 순서·편집기 handoff·지연 응답 거부 | `test_management_restore` |
| FP01 | 질의가 있는 기능만 auto, 정책·관찰·사용 분리, identity 없는 선택, resize 후 off 보존, geometry·초기 색 복원 필수 조건, 실제 PTK의 서식 on/off 및 커서 정리. 같은 서식의 연속 문자, 굵기 변경·줄 이동 후의 링크와 밑줄 복원을 수신 화면에서 확인 | `test_terminal_features.FeaturePolicyTests`, `test_terminal_composition.FormattingPolicyTests`, `TerminalCompositionTests` |
| IO01 | 3셸 × 기본형/플러그인의 native 셸·프로그램 입력 | `test_terminal_hookless.HooklessTests`의 6개 조합 |
| IO02 | 호스트 backpressure 중 마지막 출력 보존, 3셸의 1,280행·팝업 4회·resize 8회 뒤 전체 history 비교 | `test_terminal_session.TerminalSessionTests`, `test_terminal_supplements.SustainedOutputTests` |
| IO03 | 종료 후 열린 slave가 있어도 보류된 출력 전달, PTY 시작 실패·즉시 종료 때 자원 회수 | `test_terminal_session.TerminalSessionTests` |
| IO04 | 최초 bar 제외 크기, resize 후 native grid/size 응답, 직렬 크기 조회·실패 복귀·이미지 보존·애니메이션 재개, 연속 resize 중 빈 화면 단독 전송 방지, 키 입력 없이 Vim 본문·상태 줄·커서 복원, 응답하는 TUI의 이전 레이아웃 재전송 방지, 첫 이벤트 루프 차례에 PTY 크기 변경, 응답 대기 중 여러 크기는 마지막 값만 반영하고 준비된 PTY 화면 표시, 가로·세로·대각선 확대에서 새로 노출된 격자만 정리, 분할된 PTY 다시 그리기 병합과 명시적인 동기화 종료 반영, 호스트 픽셀 조회 지연과 무출력 TUI에서의 복구 | `test_terminal_cursor.CursorPTYTests`, `test_terminal_vt.TerminalVTTests`, `test_terminal_geometry`, `test_terminal_composition.TerminalCompositionTests`, `test_terminal_cli.TerminalCLITests` |
| IO05 | 실제 셸의 Ctrl+C/D/Z, jobs/bg/fg와 최종 파일 | 위 `HooklessTests` 6개 조합 |
| IO06 | 최초 상태 관찰, 정상/HUP/TERM/중지·재개, 최종 renderer 실패 cleanup | `test_terminal_cursor.RestorationPTYTests`, `test_terminal_composition.TerminalCompositionTests` |
| IO07 | native 처리 실패를 정상 feed로 보고하지 않음 | `test_terminal_contracts.BaselineContracts` |
| IO08 | native rc/hook 재로딩과 셸 실행 결과 보존 | 위 `HooklessTests` 6개 조합 |
| VT01 | 분할·취소된 제어문 이후 화면과 질의 응답 | `test_terminal_contracts.BaselineContracts` |
| VT02 | 분할 Unicode·wide cell의 문자와 커서 | `test_terminal_vt.TerminalVTTests` |
| VT03 | 앞/뒤/해제된 탭, terminfo 커서 이동의 literal 위치 | `test_terminal_contracts.BaselineContracts`, `TerminfoContracts` |
| VT04 | 좌우 margin, wrap 비활성, pending wrap의 CR 취소 | `test_terminal_contracts.BaselineContracts` |
| VT05 | 문자 삽입·삭제·반복·지우기, terminfo 편집 결과 | `test_terminal_contracts.BaselineContracts`, `TerminfoContracts` |
| VT06 | 영역 scroll·RI·NEL, history 삭제와 화면 보존 | `test_terminal_contracts.BaselineContracts` |
| VT07 | 47/1047/1048 차이, 1049 resize·중복 종료 | `test_terminal_contracts.BaselineContracts` |
| VT08 | 기본색·팔레트 번호·직접 RGB 구별, 굵기와 출력 깊이, RGB가 같은 두 슬롯과 프로그램의 팔레트 재지정·복원 | `test_terminal_vt.TerminalVTTests`, `test_terminal_startup.StartupTests`, `test_terminal_composition.TerminalCompositionTests` |
| VT09 | bold/RGB 셀 스타일, 반복 프레임 간 텍스트 독립성, 셀 스타일과 하이퍼링크 변경 | `test_terminal_vt.TerminalVTTests` |
| VT11 | 관측한 native 커서 기본값, 반복 reset의 불변성 | `test_terminal_cursor.NativeCursorTests`, `test_terminal_composition.TerminalCompositionTests` |
| VT12 | DECSTR 후 입력 mode와 기존 텍스트 | `test_terminal_contracts.BaselineContracts` |
| VT13 | DEC 선그리기와 charset/reset 결과 | `test_terminal_contracts.BaselineContracts` |
| VT14 | synchronized hold 안팎의 색상 경계 | `test_terminal_native_features.NativeFeaturesTests` |
| VT15 | 화면 전환/reset 후 오래된 선택 anchor 거부 | `test_terminal_vt.TerminalVTTests` |
| IN01 | PTK Esc·Alt·방향키, 팝업 종료 후 continuation 입력, native C0·Enter·paste. Bash의 Shift+Enter 제출과 프로그램의 일반·xterm·Kitty 모드별 PTY 수신 바이트, 모드 복귀와 기본 화면·대체 화면의 미예약 페이지 키 | `test_terminal_host.HostProbeTests`, `test_terminal_hookless.HooklessTests`, `test_terminal_vt.TerminalVTTests` |
| IN02 | terminfo 기능키·수정 navigation·keypad, separator의 mode/action, 3셸의 실제 PTK→PTY 최종 입력 파일 | `test_terminal_contracts.TerminfoContracts`, `LiveQueryContracts` |
| IN04 | 실제 입력 reader에서 paste를 하나의 이벤트로 보존 | `test_terminal_host.HostInputTests` |
| IN06 | 호스트 focus가 없는 동안 popup close가 focus-in을 만들지 않음 | `test_terminal_composition.TerminalCompositionTests` |
| IN07 | auto 정책의 마우스가 실제 PTY 파일 소비자에게 한 번 전달, 실제 Vim의 클릭·휠과 primary history 분리 | `test_terminal_mouse_policy.MousePolicyTests`, `test_terminal_cli.TerminalCLITests` |
| IN09 | 기본/키보드 UI에서 host 선택 유지, native 단어·줄 선택 | `test_terminal_mouse_policy.MousePolicyTests`, `test_terminal_selection.SelectionTests` |
| IN10 | alternate가 primary history를 빌리지 않음 | `test_terminal_vt.TerminalVTTests` |
| IN12 | native history 검색·선택, 실제 PTK 검색 단축키의 소비자 | `test_terminal_contracts.SearchContracts`, `test_terminal_composition.TerminalCompositionTests` |
| IN13 | popup 포커스·입력·반환값과 오래된 완료 거부 | `test_terminal_composition.TerminalCompositionTests` |
| HX01 | 실제 PTK→PTY foreground reader의 질의 응답 파일 | `test_terminal_contracts.LiveQueryContracts` |
| HX02 | 식별 응답과 terminfo 기능키·수정 navigation·keypad·저장/탭/wrap/clear의 일부 광고 | `test_terminal_contracts.BaselineContracts`, `TerminfoContracts`. DB 188개 대조이며 전체 287개 인증이 아님 |
| HX03 | 가상 cursor/mode/문자·픽셀 크기 질의 | `test_terminal_contracts.BaselineContracts` |
| HX04 | host palette 변경과 자식 override/reset 구별 | `test_terminal_native_features.NativeFeaturesTests` |
| HX06 | 사용자 변경 정책: host 식별 없이 제목 전달, 종료 시 마지막 제목 유지 | `test_terminal_composition.TerminalCompositionTests`. 과거 감사의 시작 제목 복원 요구는 이 정책으로 대체 |
| HX08 | 기본 쓰기/명시 읽기, 지연·취소·연속 copy/read·종료 drain·응답 writer 실패, 독립 X11 왕복과 실제 foreground 파일 | `test_terminal_clipboard.ClipboardTests`, `test_terminal_clipboard_provider` |
| HX12 | 미지원 제어문·호스트 기능을 과장하지 않는 진단 | `test_terminal_startup.StartupTests` |
| HX13 | native 진단이 입력 본문을 읽거나 수집하지 않음 | `test_terminal_contracts.BaselineContracts` |
| UI01 | 최종 PTK 레이아웃 실패 뒤에도 소유한 host metadata 정리 | `test_terminal_composition.TerminalCompositionTests` |
| UI02 | 기본형은 전체 grid이며 UI·예약 UI 키 없음 | `test_terminal_composition.TerminalCompositionTests` |
| UI03 | 첫 bar 제외 PTY 크기, 작은 창에서도 native 행 유지 | `test_terminal_cursor.CursorPTYTests`, `test_terminal_composition.TerminalCompositionTests` |
| UI04 | wide glyph의 양쪽을 가리는 popup의 닫힘 후 배경 | `test_terminal_composition.TerminalCompositionTests` |
| UI05 | 비동기 플러그인 실패의 진단과 포커스 복원 | `test_terminal_composition.TerminalCompositionTests` |
| UI06 | 조회 부작용 차단, 언어 우선순위, 플러그인 저장, 파일 로그 수준·본문 비수집 | `test_terminal_startup.StartupTests` |
| UI07 | CLI/진단의 정책 설명, history 0바이트 정책 | `test_terminal_startup.StartupTests`, `test_terminal_vt.TerminalVTTests` |
| UI08 | 종료 시 primary 커서·scroll region 복원 | `test_terminal_cursor.RestorationPTYTests`. 내부 history의 호스트 이관을 요구하지 않음 |
| IM04의 synchronized 입력 사례 | hold 중 업로드는 live 저장소로 확인하고, 화면은 해제 전 상태를 유지한 후 새 픽셀 표시 | `test_terminal_image_protocol.ImageProtocolTests`. iTerm 전체 조합의 완료를 뜻하지 않음 |
| IM05의 SIXEL 자동 사례 | 독립 palette/alpha·반복·aspect·HLS 픽셀, fragmented 입력, 취소/reset/disable, query 단일 응답, native history/alternate/erase·커서·겹침 수명, hold 중 live 업로드와 해제 시 표시, 미지원 pending-wrap의 후속 문자 보존 | `test_terminal_sixel.SixelDecoderTests`, `SixelInputTests`, `SixelOverwriteTests`. 공통 placeholder의 글자·ECH 교체, 다색 이미지 조각 좌표, 투명 padding, 스크롤 영역의 고정 배치, 일반 Kitty 보존과 실제 PTY 입력 결과를 검증 |
| IM06의 SIXEL 자동 사례 | sparse/투명·색상 255·wire 한도, page origin·가림/복원, 이미지 제거 후 text repaint, 불완전 alpha의 알려진 배경 처리/모르는 배경 거부. 이미지 밖의 텍스트 변경 시 인코딩 재사용과 클립 경계의 두 칸 문자 처리 | `test_terminal_sixel.SixelEncoderTests`, `SixelPresenterTests`, `test_terminal_graphics.GraphicsRendererTests` |
| OPT01 | 알림 기본 off, OSC 9/777/99 순서, chunk/identity/query, bounded payload, host 제어문자·OSC 9 하위명령 방어, 실제 PTK writer·프레임 한도 | `test_terminal_optional.NotificationTests`, `SemanticTests`, `OptionalCompositionTests`. [선택 확장](VT_OPTIONAL.md)의 제한된 title/body/query 범위이며 OS 알림 풍선 표시의 증명이 아님 |
| OPT02 | native prompt 이동·출력 선택, Unicode·빈 출력·reflow·alternate·history/RIS anchor, 실제 PTK F8/F9·ClipboardBroker·PTY의 최종 파일, 기본형 키/UI 보존 | `test_terminal_optional.SemanticTests`, `OptionalCompositionTests`. OSC 133 마커가 없는 셸의 자동 명령 인식을 뜻하지 않음 |
| IM08의 B 부분 | 이미지 입력 거부 건수와 로그 본문 비수집 | `test_terminal_startup.StartupTests`. 이미지 지원의 추가/완료 판정이 아님 |
| EXT01 | 동일 폴더의 출력/키/클립보드 동시 로딩, 키 등록 충돌·응답 격리·정상 키 보존·실제 PTY 소비자, terminfo 목록 변경 감지 | `test_terminal_supplements`, `test_terminal_keys`, `test_terminal_terminfo` |

## 검사기의 역할

1. 기존 `REQUIRED_TERMINAL`은 모듈 파일의 존재를 확인한다. 빈 모듈·import 실패·skip도 기존 strict runner가 실패로 처리한다.
2. `REQUIRED_CASES`는 **실제 실행할 unittest suite** 안에 각 사례가 정확히 한 번 있는지 확인한다. 함수가 파일에 남아 있어도 `load_tests`가 수집에서 빼면 실패한다. 다른 테스트가 남아 있는 파일에서 하나만 삭제하거나 이름을 바꿔도 실패한다.
3. `test_terminal_checks.py`의 독립 literal 목록은 선언에서 ID·필수 사례를 지워 보호 범위가 조용히 줄어드는 것을 막는다. 이름을 의도적으로 바꿀 때는 선언과 독립 기대값을 함께 검토해야 한다.
4. 별도 `REQUIRED_CHECKS`는 이 방어를 검증하는 검사 자체도 보호한다. 제품 명세에 연결한 사례와 검사기 테스트를 구분한다.
5. 검사 실패는 사례의 실행 전에 명세 ID와 누락/중복된 전체 테스트 이름을 보고한다. import 실패가 원인이면 원래 수집 traceback도 보존한다. 수집하거나 실행하는 별도 두 번째 테스트 runner는 만들지 않는다. 단일 `tools/check_terminal.py` 검사기가 수집·실행을 소유한다.
6. `REQUIRED_HELPERS`는 Zellij 비교·이미지·실제 presentation·키보드/IME runner가 사용하는 helper 6개를 lint 입력으로 고정한다. 독립 literal 경로 목록과 각 파일 삭제 검사가 glob 자동 발견에 기대지 않고 누락을 잡는다. 별도의 Zellij 다운로드·비교 실행·실제 Windows Terminal 조작을 매번 unit 회귀 안에서 실행한다는 뜻은 아니다.

테스트 본문의 assertion을 제거하거나 같은 이름을 무의미한 검사로 바꾸는 것은 이 검사기의 보장 범위가 아니다. 결과의 독립성과 테스트 의미는 코드 검토가 계속 확인한다. 개별 `subTest` 조합 삭제도 별도 테스트 ID 삭제와 같게 감지하지 않는다.

## 여전히 별도인 항목

- IN05의 실제 IME, UI09의 접근성, 물리 마우스·검색 키·클립보드·픽셀 결과는 자동 선언에 새로 통과 표시하지 않는다.
- HX05 BEL의 실제 호스트 효과 및 HX06 제목의 실제 호스트 결과는 자동 사례와 따로 확인한다.
- terminfo 287개의 [이전 후속 대조](history/VT_TERMINFO_RESULT.md)는 당시 범위의 기록이다. 이번 IM05/IM06·OPT01/OPT02 자동 사례 추가가 전 상태 조합·전 호스트 표현이나 전체 선택 확장의 지원을 뜻하지 않는다. 기존 명시적 차이와 이번 native placement·알림 범위 제한은 별도로 남는다.
- 실제 Windows Terminal 검사와 GitHub workflow 실행 여부는 로컬 회귀와 따로 기록한다.
