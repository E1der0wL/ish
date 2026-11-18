# ish-vt 기본 계약 보강과 검증

> 통합 전 검증 기록이다. 현재 제품은 VT 기반 `ish` 하나이며 아래의 stream 검사기·`ish-vt` 명령은 제거됐다. 현재 실행·검증 방법은 [사용 안내](VT_USAGE.md)와 [개발 안내](DEVELOPMENT.md)를 참고한다.

후속 입력·클립보드·보완 모듈 및 terminfo 전수 분류는 [최신 보강 결과](VT_SUPPLEMENTS_RESULT.md)를 참고한다. 아래 수치와 미검증 판정은 이 문서 작성 당시의 기록이다.

2026-11-17, `codex/hookless-vt`. [기본 지원 명세](VT_BASELINE_AUDIT.md)의 B/P 항목을 대상으로 한다. 선택 확장(E), SIXEL, native Windows/ConPTY는 이번 구현 범위가 아니다. 기존 `ish` 2.0.0 stream 경로와 다른 실행 경로이며 기본형에는 UI·UI 단축키·셸 hook을 추가하지 않았다.

## 구현한 경계

### 종료와 중지 복원 — IO06, VT11/12/15

PTK의 첫 화면 변경 전에 호스트의 변경 대상 모드·커서·SGR을 관찰한다. 실제 변경한 값 중 구체적인 시작값이 확인된 항목만 복원한다. 미응답을 OFF 또는 기본값으로 해석하지 않는다. 셸 입력 readiness는 이 조회와 관계없다.

원점 모드는 대체 화면에서 나가기 전에 복원하여 원래 화면의 커서 위치를 다시 이동시키지 않는다. PTK 정리 후 마우스·focus·paste·wrap 등 확인한 상태를 복원하며, 반복 reset은 복원된 상태를 다시 덮지 않는다. SIGTSTP는 PTK handoff로 화면과 시작 termios를 복원한 뒤 프로세스를 중지한다. SIGCONT 후 동일 native 셸과 PTY를 재사용하고 새 크기로 다시 그린다. 이미 종료 중이면 중지하지 않는다.

첫 PTY 생성도 첫 레이아웃 계산 이후로 옮겼다. 플러그인이 하단 행을 사용하는 경우 자식의 최초 크기부터 해당 행을 제외한다. [복원 순서·소유권·한계](VT_RESTORATION.md).

### 기본 키·질의·terminfo — IN01/02, HX01/02

실제 Ubuntu `xterm-256color`에서 capability **188개**를 독립 기대값으로 대조했다. 기능키 63개, navigation 75개, keypad 18개, 이동 13개, 편집 8개, 탭·커서 저장·wrap 9개, keypad 전환 2개다. 전체 287개를 모두 검증했다는 뜻은 아니다.

libghostty의 legacy 수정키+F3 출력이 광고한 terminfo와 다른 결함을 수정했다. 키 의미의 책임은 `identity.py`, 현재 keyboard flags의 책임은 native 엔진에 둔다. flags를 복제하지 않고 조회하므로 협상된 키보드 프로토콜의 동작은 바꾸지 않는다. 이를 위해 private bridge ABI는 **4**가 되었고 libghostty 자체는 **1.0.1**을 그대로 사용한다.

호스트의 일반 CSI 기능키도 기존 decoder를 재사용한다. 협상되지 않은 Kitty 확장 이벤트를 키로 추측하지 않는다. `CSI 1;2 R`처럼 Shift+F3와 커서 위치 응답이 같은 바이트인 경우에는 지연된 응답을 사용자 입력으로 만들지 않도록 응답을 우선한다. 이 모호한 호스트 입력의 완전한 키 지원은 선언하지 않는다.

추가 대조에서 native legacy keypad separator(`kpCMA`)의 빈 인코딩을 재현했다. bridge는 native 결과가 비어 있고 해당 legacy press/repeat인 경우만 현재 native keypad/NumLock 정책을 읽어 보완한다. 별도 모드 상태를 만들지 않으며, native가 결과를 제공하면 보완은 사용하지 않는다. modifier 8조합, repeat/release, enhanced·alternate·reset 경계를 확인했다. 호스트가 키패드와 일반 키를 같은 바이트로 보낸 경우 그 출처를 복구한다는 뜻은 아니다.

### 오류·지원 범위 진단 — IO07, HX12/13, UI06/07, IM08

기존 번역 카탈로그로 VT·host·app의 사용자 대상 오류를 연결했다. 한도 초과 `BufferError`도 터미널 복원 후 공통 세션 오류 로그로 보고한다. 기존 이미지 경로의 거부·출력 실패는 누적 횟수만 남기고 입력 본문과 native 로그 원문을 기록하지 않는다. 기본형에는 알림 UI를 추가하지 않았으며 지원 조건 설명은 INFO 로그와 `--diagnose`에서 제공한다. 이미지 지원을 추가하거나 기존 gate를 해제한 변경은 아니다.

## 검증 방법

| 경계 | 독립 기대 결과와 실제 소비자 |
|---|---|
| 화면·제어문 | tabs, wrap, origin/좌우 margins, insert/delete/erase, alternate 47/1047/1048/1049, resize, reset, history 삭제, mode/size 질의의 literal 셀·응답 |
| 키·질의 전달 | Bash/zsh/tcsh × 기본형/플러그인의 실제 PTK→PTY foreground reader가 저장한 파일 |
| 셸 보존 | primary/continuation, heredoc의 공백·Unicode·도구 이름, 여러 줄 paste, native Tab/stdin, rc 재로딩, Ctrl+C/D/Z, jobs/bg/fg, Vim |
| 동시 동작 | 1,200줄 출력 중 팝업 유지·닫힌 뒤 최신 배경, 큰 paste의 최종 길이와 SHA256. 최종 marker만으로 출력 중간 모든 줄의 보존을 주장하지 않음 |
| 복원 | 비기본 모드·커서·SGR을 설정한 실제 Windows Terminal에서 전후 DECRQM/DECRQSS/CPR 응답 비교. 정상/HUP/TERM/PTK 예외/중지·재개를 별도 검사 |
| 초기 크기 | 자식 첫 실행 줄에서 실제 PTY 크기를 파일로 기록. 기본형과 3행 bar 구분 |
| 누락 방어 | `tools/check_terminal.py`와 독립 필수 목록을 유지. skip·빈 모듈·누락은 실패 |

명세 **48개 ID에 연결된 개별 테스트 74개**와 검사기 자체의 핵심 사례 5개도 실제 수집 suite에서 정확히 한 번 존재해야 한다. 삭제·이름 변경·중복·`load_tests` 은폐를 실행 전에 거부한다. 기존 공통 runner에 선택적 수집 검증을 연결했으며 stream의 기본 호출은 그대로다. [사례 연결표와 보장 범위](VT_TEST_CONTRACTS.md).

실제 호스트 검증은 `tests/terminal_matrix.py`와 `tests/terminal_restore.py`를 사용한다. 마우스·검색 조작은 `tests/terminal_interaction.py`가 별도로 요구한다. 자동 입력과 실제 호스트 응답 검증을 물리 키보드·IME·픽셀 검증으로 보고하지 않는다.

검사 기록은 `.cache/vt-baseline/`에 보존한다. 중간 실패도 삭제하거나 성공 결과로 덮어쓰지 않는다.

### 최종 검증 결과

Windows Terminal 1.24.12741.0 / WSL 2.5.9.0 / Ubuntu 26.04 LTS / kernel 6.6.87.2-microsoft-standard-WSL2에서 실행했다. Python 3.12.14, PTK 3.0.53, libghostty-vt 1.0.1 / bridge ABI 4, Bash 5.3.9, zsh 5.9, tcsh 6.24.13, Vim 9.1이다.

| 검사 | 결과 | `.cache/vt-baseline/`의 기록 |
|---|---|---|
| 전체 VT 회귀·린트·포맷 | **446개 통과**, skip 없음 | `terminal-success.log` |
| 전체 stream 회귀·린트·포맷 | **387개 통과**, skip 없음 | `stream-complete.log` |
| 실제 WT 셸 × 기본형/플러그인 | **6개 조합 통과**, native 프로그램과 Vim의 실제 viewport resize 12회 | `host-matrix-complete/summary.json`, `resize-actions.json` |
| 실제 WT 시작값 복원 | **15개 통과**: 3셸 × 정상/HUP/TERM/예외/중지·재개 | `host-state-complete/summary.json` 및 각 `result.json` |
| 실제 마우스 조작 | auto+native off의 호스트 드래그·복사, auto+native on의 native 클릭, off+native on의 호스트 드래그·복사 **3개 통과** | `manual-input-retry/auto-native-off`, `manual-input-retry/auto-native-on`, `manual-input-copykey/off-native-on` |
| 실제 검색 팝업 조작 | 열림·닫힘, 검색 UI 입력이 native 프로그램에 새지 않음, 정상 종료 확인 | `manual-input-copykey/optional-search/result.json` |
| 실제 WT 제목 | ish 종료 후에도 마지막 제목 유지 | `host-title/result.json`, `host-title/window-title.json` |
| Vim 마우스 실패 재현 후 반복 | 보강한 동일 검사 **20회 연속 통과**, 클릭 `4,5`·휠 `16` 파일 확인 | `vim-mouse-repro-fixed/0..19` |
| 최종 입력 파일 동일성 | 소스·테스트·native 파일 **159개 SHA256 일치** | `source-success-before.json`, `source-success-after.json` |

종합 기록은 `summary-final.json`, 환경은 `environment-final.json`이다. `terminal-complete.log`의 최초 전체 회귀 실패와 검증기·수동 입력의 실패 기록도 남겼다. 수동 검사는 서로 다른 실행에서 성공한 각 사례를 합쳤으며, 실패한 실행 전체를 성공으로 바꾸지 않았다. 검색 수동 기록은 **실제 검색어 입력·결과 일치의 증명은 아니다**. 검색 결과·선택의 의미는 별도의 native/실제 PTK 자동 회귀가 검증한다. 제목은 Windows의 실제 창 제목 메타데이터로 확인했으며 픽셀 비교로 표현하지 않는다.

실제 WT 검증 후 제품 코드는 바꾸지 않았다. 마지막 변경은 Vim 테스트의 호스트 캡처 대기와 필수 사례 등록이다. 기존 stream worktree는 변경하지 않았다. GitHub workflow 원격 실행과 선택적 Kitty GUI 검사는 이번 결과에 포함하지 않는다. 아직 커밋하지 않았다.

### 재검토에서 재현하고 수정한 문제

- DECOM을 primary 복귀 뒤 적용하면 저장된 커서가 홈으로 이동했다. alternate에서 나가기 전에 적용하도록 고쳤다.
- Windows Terminal에서 mouse tracking OFF 명령이 다른 tracking ON까지 끄는 경우가 있었다. 관찰한 OFF 상태를 먼저, ON 상태를 마지막에 복원한다.
- legacy press 뒤 자식이 확장 keyboard mode를 켜면 팝업에서 존재하지 않았던 release를 만들 수 있었다. 수신 시 호스트의 release 계약과 전달 시 native mode를 각각 확인한다.
- 플러그인 bar가 있을 때 첫 native PTY 크기가 전체 화면 크기일 수 있었다. 첫 레이아웃 이후 PTY를 생성한다.
- 복원 검증기의 추가 PTY에 raw 진입 전 focus 보고가 전달되어 `^[[I`가 에코되고 좌표가 4칸 이동했다. 제품 변경 없이 실제 PTY의 ECHO/ICANON이 꺼진 동안에만 테스트용 호스트 입력을 중계하도록 고쳤다. 수정 후 실제 호스트 15개 복원 사례가 통과했다.
- 수동 검증의 복사 키를 Ctrl+Shift+C로 고정한 안내가 사용자 설정(Ctrl+C)과 달랐다. 중단 기록은 보존하고, 안내를 호스트에 설정된 복사 기능으로 정정했다. 이 실패를 마우스 지원 성공으로 집계하지 않는다.
- 전체 회귀의 Vim 클릭 검사가 간헐적으로 `4,5` 대신 `1,1`을 기록했다. 원래 검사 그대로 반복해, 실패 때는 native 준비 파일만 존재하고 호스트 캡처 요청은 아직 출력되지 않았음을 확인했다. 실제 `1003h/1006h` 출력과 활성 캡처를 확인한 뒤 클릭하도록 검증기를 고쳤다. 제품의 미요청 마우스 차단이나 기대 좌표·timeout을 완화하지 않았다.

## 남는 범위

- 조회할 수 없는 임의 시작 상태, 임의 중첩 alternate 화면, 초기 호스트 cwd, SIGKILL·출력 연결 소멸까지 완전 복원한다고 보장하지 않는다. 사용자가 바꾼 창 크기는 되돌리지 않는다.
- 제목은 사용자 요청에 따라 변경을 허용하고 시작 제목 복원에서는 제외한다. 확인되지 않은 클립보드 읽기 프로토콜은 활성화하지 않는다. 나머지 상태도 호스트가 제공하는 보장 없이 복원을 추측하지 않는다.
- 호스트에서 구별되지 않는 키와 물리 IME·접근성·모든 폰트/배율은 별도 검증 범위다. 기본 마우스 경로는 협상한 SGR을 사용하며 legacy1005/highlight 등 선택되지 않은 outer 형식을 새로 추가하지 않았다.
- 모든 terminfo 항목과 모든 제어문 상태 조합을 인증한 것은 아니다. 명세별 사례, 실제 소비 파일, 실제 호스트 응답의 범위를 각각 보고한다.
- 선택 이미지/스타일/알림/키보드 확장·드롭다운·우측 프롬프트 및 SIXEL은 이번 단계의 추가 구현에서 제외한다.

### 제목은 변경하고 복원하지 않는다

설치된 Windows Terminal **1.24.12741.0**의 같은 버전 공식 소스를 확인했다. OSC 제목 설정은 구현되어 있지만, 시작 제목 조회 `CSI 21 t`와 저장/복원 `CSI 22/23 t`는 처리하지 않는다. 사용자가 제목을 복원하지 않는 정책을 명시했으므로 제목 변경에는 시작 제목 조회를 요구하지 않는다. 종료 뒤에는 마지막으로 설정한 제목이 남는다. 다른 프로그램이나 호스트가 이후 제목을 변경하는 것까지 막는다는 뜻은 아니다. `--suppressApplicationTitle`로 연 기존 matrix는 가시적 제목 변경 증거로 사용하지 않는다.

근거: Microsoft의 [WindowManipulation 구현](https://github.com/microsoft/terminal/blob/v1.24.12741.0/src/terminal/adapter/adaptDispatch.cpp#L3223), [지원 값 목록](https://github.com/microsoft/terminal/blob/v1.24.12741.0/src/terminal/adapter/DispatchTypes.hpp#L575), [제목 설정 API](https://github.com/microsoft/terminal/blob/v1.24.12741.0/src/cascadia/TerminalCore/TerminalApi.cpp#L80).

### terminfo의 나머지 99개

이 수치는 미구현 개수가 아니다. 현재 직접 DB 이름을 조회해 기대 결과와 대조하는 188개 외의 항목이다. 기존 literal·native·PTY 회귀와 겹치는 기능, 정적 정보, 호스트 조건부 기능, 명세의 기존 제외 범위가 섞여 있다.

| 분류 | 개수 |
|---|---:|
| 플래그·수치 | 18 |
| 편집·스크롤·margin | 17 |
| 색·글자 속성·커서 외관 | 27 |
| 문자셋·reset·화면 전환 | 8 |
| mouse·focus·paste | 11 |
| clipboard | 1 |
| 질의·응답 패턴 | 8 |
| bell | 2 |
| peripheral·legacy meta | 7 |

정적 플래그도 `bce/ccc/am`처럼 행동 보장을 포함하므로 단순히 검증 불필요로 처리하지 않는다. `rv/xr`의 XTerm 식별 패턴과 ish의 실제 DA2/XTVERSION은 다르다. 이는 공유 terminfo의 광고 정합성 제한으로 남기며, 테스트를 맞추려고 ish를 XTerm으로 식별하지 않는다. 모든 DB 항목·매개변수·물리 입력·외관을 전수 인증했다는 의미로 188개 결과를 확대하지 않는다.

재실행:

```sh
python tools/check_terminal.py
python tools/check_stream.py
# 실제 Windows Terminal/WSL에서 새 결과 디렉터리 사용
python tests/terminal_matrix.py .cache/vt-new-matrix --resize-each
python tests/terminal_restore.py .cache/vt-new-restore
python tests/terminal_interaction.py .cache/vt-new-input --with-search
```
