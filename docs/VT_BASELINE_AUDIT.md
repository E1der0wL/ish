# ish-vt 기본 지원 명세와 연결 상태 감사

> 이 문서는 2026-11-16의 변경 전 감사 기록이다. 이후 구현과 검증 결과는 [보완 모듈 리팩토링 결과](VT_IMPLEMENTATION.md)와 [기본 계약 보강 결과](VT_BASELINE_RESULT.md)를 참고한다. 아래의 ABI 1·미연결 표시는 당시 상태이며 현재 지원 판정으로 사용하지 않는다.

기준일: 2026-11-16. 대상은 `codex/hookless-vt` worktree의 현재 작업본이다. 기본 HEAD는 `04a3d4e1ae6c16579d8041eca987b26f98256055`이며, 기존 미커밋 실험 변경을 포함한다. 이 문서는 **목표 명세와 현재 상태의 차이**를 기록한다. 기능을 활성화하거나 구현을 변경하지 않는다.

현재 판정은 **기본 지원 완료 아님**이다. 주요 셸 시나리오는 검증됐지만 표준 항목별 전체 대조 테스트, Windows Terminal의 실제 키보드·IME·마우스·픽셀 검증, 여러 외부 기능 연결이 남아 있다. 이미지 없는 정상 셸 실행의 성공을 이미지·확장 스타일·클립보드까지 포함한 전체 터미널 호환성으로 확대하지 않는다.

## 1. 적용 범위와 판정 방법

### 1.1 두 실행 경로

| 항목 | ish 2.0.0 | ish-vt 실험 |
|---|---|---|
| 진입점 | `ish.stream.__main__:main` | `ish.terminal.__main__:main` |
| 셸 출력 | 원본을 호스트로 전달 | libghostty 상태로 해석한 뒤 PTK로 출력 |
| 기본형 | UI·UI 단축키·셸 hook 없는 전달 경로 | UI 플러그인 없는 전체 화면 VT 경로. 원본 전달은 아님 |
| 화면·스크롤백 | 호스트 | libghostty; 표시·사용자 조작은 ish/PTK |
| 선택한 UI | primary 프롬프트 연동이 필요 | 셸 hook 없이 하단 바와 전역 팝업 |
| 명령줄·continuation·heredoc·프로그램 stdin | native 셸/프로그램 | native 셸/프로그램 |
| 설정·시작 옵션 | `.ishrc.py`, 언어, 진단, 로그, 시작 옵션의 제품 경로 | 실험 CLI 일부만 있음. stream의 완성도를 승계하지 않음 |
| 이번 감사 | 경계·공통 의존성 확인만 | 기본 지원 명세 및 연결·검증 상태 감사 |

근거: [`pyproject.toml`](../pyproject.toml), [`HOOKLESS_VT.md`](HOOKLESS_VT.md), [`terminal/__main__.py`](../src/ish/terminal/__main__.py), [`stream/startup.py`](../src/ish/stream/startup.py). VT의 실패를 stream의 실패로, stream의 385개 회귀 통과를 VT의 합성 검증으로 해석하지 않는다.

### 1.2 고정된 대조 대상

- Python 3.12.14, prompt-toolkit 3.0.53, libghostty-vt **1.0.1**, ish native bridge ABI **1**.
- Windows Terminal **1.24.12741.0**, Ubuntu/WSL. Bash **5.3.9**, zsh **5.9**, tcsh **6.24.13**, Vim **9.1**은 보존된 실제 호스트 검증의 버전이다. 다른 버전까지 자동 승격하지 않는다.
- 로컬 공개 헤더 **35개**, terminal option **48개**, effect callback **19개**, named terminal mode **43개**를 카탈로그로 대조했다.
- 자식 환경은 `TERM=xterm-256color`, `COLORTERM=truecolor`, `TERM_PROGRAM=ish-vt`를 사용한다. Ubuntu에서 `infocmp -1 -x xterm-256color`로 **287개 capability**를 읽었다. 광고와 실제 동작의 전수 정합성은 아직 검증되지 않았다.
- API의 존재, ish의 연결, 자동 테스트, 실제 WT 검증은 각각 독립된 사실이다. API를 사용하지 않았어도 다른 경로가 같은 기능을 구현할 수 있다.

이 숫자는 **위 버전의 카탈로그를 분류한 범위**이다. 모든 ECMA/DEC/xterm 제어코드의 모든 매개변수와 상태 조합을 실험했다는 뜻은 아니다. 세부 검증 증거가 없는 기능군은 통째로 검증 완료 처리하지 않고 아래에 미검증으로 남긴다.

### 1.3 요구 수준과 상태

- **B 기본 호환성:** 셸·일반 TUI와 입력/화면/프로세스 수명에 필요한 릴리스 필수 목표. 구현 난이도 때문에 사후에 제외하지 않는다.
- **P 기본 사용성:** 설정·진단·검색 등 완성된 제품으로 제공하려면 명세와 구현이 필요한 항목. VT 의미 해석과 구분한다.
- **E 선택 확장:** 이미지 프로토콜, 현대 확장 등 명시적으로 지원 범위를 선택할 항목. 미지원이어도 B 통과로 위장하지 않으며 사용자가 요청한 확장은 별도 완료 조건이다.
- **N 범위 밖:** Linux/WSL의 이 실험이 제공하지 않는 플랫폼·서비스. 지원하지 않는 사실과 입력 시 동작을 기록한다.

구현 상태는 **연결 / 부분·조건부 / 미연결 / 미지원 / 확인 필요**로 기록한다. 검증은 **A 자동 회귀 기록**, **W 실제 Windows Terminal 시나리오**, **미검증**으로 따로 기록한다. `미연결`은 엔진에 필요한 API가 있으나 외부 동작까지 잇지 않은 경우이며, 의도적인 비활성 정책은 `미지원(정책)`으로 구분한다.

## 2. 기준 문서와 누락을 찾는 경계

| 기준 | 이번 명세에 반영한 범위 | 이것만으로 충분하지 않은 이유 |
|---|---|---|
| [ECMA-48 5판](https://ecma-international.org/publications-and-standards/standards/ecma-48/) | C0/C1, 제어문 프레이밍, 문자·커서·지우기·모드 기능군 | 모든 장치가 전체 기능을 구현하는 규격이 아님. 모든 ECMA 기능을 B로 선언하지 않음 |
| [xterm 제어코드, patch 411](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html) | DEC/xterm 화면·입력·SGR·OSC·질의·모드·reset·확장 | xterm 전용 기능, 프린터·레거시 문자셋·창 관리 등은 별도 분류 필요 |
| [terminfo](https://invisible-island.net/ncurses/man/terminfo.5.html) 및 실제 Ubuntu 데이터 | `TERM`으로 프로그램에 약속하는 출력·키·초기화·응답 capability | 애플리케이션이 terminfo 밖의 코드를 직접 보낼 수도 있음 |
| [Unicode UAX #29](https://www.unicode.org/reports/tr29/) / [UAX #11](https://www.unicode.org/reports/tr11/) | 결합 문자·grapheme·동아시아 폭·emoji와 variation | 참조 문서는 Unicode 18.0. 실제 엔진·호스트·폰트의 버전/폭 정책 일치는 별도 미검증 |
| [libghostty terminal](https://lib.ghostty.org/vt/group__terminal.html), [rendering](https://lib.ghostty.org/vt/guide-rendering.html), [effects](https://lib.ghostty.org/vt/guide-effects.html) | 내부 상태와 외부 입력/출력 연결의 경계 | 엔진 지원만으로 PTK 표현·호스트 효과가 완성되지 않음 |
| [Kitty keyboard](https://sw.kovidgoyal.net/kitty/keyboard-protocol/), [graphics](https://sw.kovidgoyal.net/kitty/graphics-protocol/), [libghostty images](https://lib.ghostty.org/vt/guide-images.html) | 확장 키, 이미지 저장·배치·조회·전송 | host/input/output 양쪽의 교집합을 검증해야 함 |
| [Microsoft Console VT](https://learn.microsoft.com/en-us/windows/console/console-virtual-terminal-sequences) | Windows 계열 제어코드 참고 | Console 문서를 특정 Windows Terminal 버전의 지원 증거로 대체하지 않음 |

PTY 수명, 흐름 제어, 실제 키보드·IME, UI 포커스, 종료 복원은 escape 명세만으로 포괄되지 않는다. 아래 별도 요구사항에 포함한다. [VTTEST](https://invisible-island.net/vttest/)도 재사용할 검증 도구지만 현대 입력·클립보드·이미지·팝업 전체의 인증 도구로 취급하지 않는다.

## 3. 기본 지원 명세와 현재 구현

아래의 테스트는 이번에 재실행한 결과가 아니라 보존된 회귀 로그와 테스트 소스에 근거한다. A가 있어도 해당 행의 모든 하위 기능과 모든 호스트에서 통과했다는 뜻은 아니다. `test_terminal_*.py`는 모두 `tests/` 아래 경로다.

### 3.1 프로세스와 전송

| ID / 수준 | 요구 동작 | 현재 구현·증거 | 남은 항목 |
|---|---|---|---|
| IO01 B | controlling PTY에서 셸을 실행하고 실행 중 프로그램 stdin을 보존 | `session.py:61`, `pty.fork/execvpe`; session 회귀 A, native stdin W | 네이티브 Windows/ConPTY는 N. macOS 미검증 |
| IO02 B | 부분 write/EAGAIN·backpressure에서 입력 순서와 출력 전달 보존 | `runtime/fdio.py`, `session.py:89`; session 회귀 A | 실제 WT의 대용량·느린 출력·동시 popup 스트레스 미검증 |
| IO03 B | child exit 관찰과 마지막 출력 전달·호스트 drain을 구분 | `session.py:135`; retained slave/backpressure A | post-exit 읽기 256KiB 상한 정책 존재. 백그라운드 프로세스의 무한 출력까지 보존하는 서비스는 아님 |
| IO04 B | 화면 영역 resize를 VT와 PTY에 동일하게 전달 | `app.py:605`, `session.py:181`; 프로그램 SIGWINCH/Vim 크기 W | 초소형·최대 크기, 연속 resize와 paste/이미지/scrollback 조합 미검증 |
| IO05 B | Ctrl+C/D/Z 및 job control을 native 셸에 전달 | 문자 제어 입력 경로; Ctrl+C/D A·W | child Ctrl+Z/jobs/fg/bg 전용 WT 행렬 없음. ish 자체 TSTP/CONT와 구분 |
| IO06 B | 정상·HUP·TERM·예외 종료 때 소유 자원과 터미널 상태 복원 | `__main__.py:19`, `app.py:777`, `session.py:205`; 정상 termios W, HUP/TERM A | 임의 시작 keyboard/mouse/focus/paste/cursor 상태 전체 snapshot/복원 없음. 실제 WT 비정상·suspend/resume 미검증 |
| IO07 B | 오류·메모리 상한에서 조용한 부분 적용을 감지 | Python/native 반환 코드 검사 있음 | `VT_PROCESSING_ERROR` 조회 없음. native void feed 내부 semantic 오류 감지 미연결 |
| IO08 B | shell rc/hook·종료 상태를 변경하지 않음 | hookless 시나리오 A·W, 별도 native shell | 사용자 rc 모든 조합에 대한 보장은 아님. 임시 HOME 시나리오와 개인 실제 환경 구분 |

### 3.2 문자·화면·모드

| ID / 수준 | 요구 동작 | 현재 구현·증거 | 남은 항목 |
|---|---|---|---|
| VT01 B | UTF-8 및 제어문이 임의 바이트 경계로 나뉘어도 동일한 결과 | `protocol.py`, native parser; split UTF-8/OSC 등 A | 모든 ESC/CSI/OSC/DCS/APC 종료·CAN/SUB·잘못된 입력 조합 전수 미검증 |
| VT02 B | 한글·결합 문자·wide cell·emoji의 내용과 셀 위치 보존 | native grapheme/width → `Frame.cells`; vt/composition A; Unicode 파일 결과 W | ZWJ/VS/ambiguous width, 비라틴 결합·폰트 fallback, 줄끝/resize/팝업의 실제 픽셀 미검증 |
| VT03 B | CR/LF/BS/HT, 탭 정지 설정·해제, 커서 상대/절대 이동 | 엔진 위임; 일부 cursor A | CHT/CBT/HTS/TBC 포함 명세별 완전 coverage 없음 |
| VT04 B | 자동 줄바꿈·pending wrap·reverse wrap·origin·상하좌우 margins | 엔진 상태, 입력/이미지 context에 일부 노출 | DECAWM/DECOM/DECRST와 넓은 문자·리사이즈 조합, 좌우 margin 전체 미검증 |
| VT05 B | 문자/줄 삽입·삭제·반복·지우기 및 기본 배경색 유지 | 엔진 → 셀 렌더링; 일부 erase A | ICH/DCH/IL/DL/ECH/REP/ED/EL/BCE의 하위 조합 미검증. selective erase는 VT16에서 별도 분류 |
| VT06 B | 스크롤 영역·SU/SD/IND/RI, history 정책 일관성 | native viewport/history; scrollback A | host 스크롤과 같은 것으로 간주 금지. WT 실제 wheel/선택과 조합 미검증 |
| VT07 B | main/alternate 47·1047·1048·1049, 커서 저장/복원 | native alternate; 1049 A, Vim W | 모든 변형의 중첩·중복·resize·reset 동작은 미검증 |
| VT08 B | ANSI 16/256/RGB, 기본색과 명시한 검정 구별 | native 색 존재 플래그·host palette 연결; A | WT 실제 색/투명 배경 픽셀 비교 없음. `true_color=false`만으로 256색 한정 판정 금지 |
| VT09 B | bold/dim/italic/underline/reverse/hidden/strike/blink | native style flags → PTK; 일부 A | host 실제 표현·동적 blink 미검증. 엔진 속성 보유와 호스트가 그리는 능력 구분 |
| VT10 E | 복합 밑줄·밑줄 색·overline·OSC8 링크 보존 | `rendering.py:258`; host gate로 조건부 연결; A | WT unknown 프로필은 해당 기능 비활성. 클릭·픽셀 미검증 |
| VT11 B | 커서 위치·가시성·bar/block/underline·깜박임 | native frame → PTK; 일부 A | hollow block은 일반 BLOCK으로 축소. 호스트 기본 style/blink 상속 미연결, WT 외관 미검증 |
| VT12 B | RIS/DECSTR와 mode 변경 후 일관된 상태 | native parser + RIS 시 adapter 취소; A 일부 | native 상태와 host 외부 상태/입력 캡처/이미지의 통합 reset 전수 미검증 |
| VT13 B | DEC 선그리기와 실제 terminfo의 ACS 계약 | 엔진에 파싱 위임, curses/Vim 간접 사용 | SO/SI/SCS/ACS·fallback 문자셋 전용 WT 검증 없음 |
| VT14 B | synchronized update를 완결된 화면으로 표시 | `RENDER_HOLD`, PTK single writer; native_features A | 1초 강제 해제 정책. 장기 hold+resize+팝업+종료 실제 WT 조합 미검증 |
| VT15 B | reset·화면 전환 중 선택과 입력 관련 상태를 정리 | `app.py:629`; 일부 회귀 A | 모든 named mode의 상태 전이·저장/복원 coverage 미완성 |
| VT16 E | selective erase/rectangle ops/checksum/legacy VT 확장 | 엔진 위임 또는 비활성 | 개별 지원 판정 미검증. checksum은 정책상 비활성. B 완료로 포함시키지 않음 |

### 3.3 입력과 사용자 조작

| ID / 수준 | 요구 동작 | 현재 구현·증거 | 남은 항목 |
|---|---|---|---|
| IN01 B | 문자·C0·Enter/Tab/Backspace·Escape·Alt 입력 보존 | PTK → native encoder 또는 raw fallback; A | 물리 Alt/ESC, 키 배열·AltGr, ESC 지연 실제 WT 미검증. VT timeout은 50ms 고정 |
| IN02 B | cursor/application keypad/수정키/F-key의 자식 mode 반영 | bridge named key F1–F12와 navigation, Shift/Alt/Ctrl; A | host decoder가 keypad Enter/방향/Home/End 등을 일반 키로 축소해 물리 keypad 구별을 잃음. F13–63 전수 재인코딩 미검증. raw fallback은 일부 입력을 보존하지만 mode-aware 재인코딩을 보장하지 않음 |
| IN03 E | Kitty keyboard press/repeat/release·대체 keycode·text 보존 | host structured key + native encoder 부분 연결; A | 대체 keycode·associated text는 decode해도 app이 native 전달에 사용하지 않음. Caps/Num lock도 최종 modifier에서 제외. Super/Hyper/Meta와 일부 functional key 미지원, consumed/composing 정보 제한. WT enhanced keyboard 비활성 |
| IN04 B | bracketed paste/다중 줄/대량 paste를 현재 포커스에 한 번 전달 | `encode_paste`, PTK paste; A·W | 물리 Ctrl+Shift+V, OS clipboard, 경계 크기·붙여넣기 중 resize 미검증 |
| IN05 B | IME의 확정된 Unicode 텍스트 전달 | 문자열 전달 경로 있음 | 실제 한글 조합·취소·후보창·커서 위치·팝업 전환 미검증. 조합 UI는 호스트가 담당 |
| IN06 B | 창 focus와 popup focus를 구분해 자식에 전달 | `app.py:326`, focus API; A | 실제 WT↔다른 창 focus, popup close/child exit race 미검증 |
| IN07 B | 자식이 mouse mode를 켠 경우 좌표·버튼·수정키 전달 | native encoder, SGR/X10 등 host decoder; mouse/composition A | 실제 WT mouse 미검증. outer UTF8 1005/highlight/추가 side button decoder 미구현이며 자식 encoder 지원과 별개 |
| IN08 E | pixel mouse 1016을 관측한 픽셀 좌표로 전달 | 별도 host parser+native encoder 있음 | geometry/profile 조건부. WT 최종 상태에서는 비활성 |
| IN09 B | 자식 mouse mode가 없을 때 로컬 scrollback/선택 | gesture·tracked anchor·formatter; A | WT drag, double/triple click, 사각 선택, autoscroll 미검증 |
| IN10 B | main history와 alternate 프로그램의 wheel/Page 키 구분 | `app.py:152,486`; A | main Shift+PageUp/Down은 ish가 소비. 기본형의 '예약 키 없음'은 UI 전용 키 없음으로 한정해야 함 |
| IN11 E | 수평 스크롤·중간 클릭 PRIMARY paste | 자식 mouse 수평 wheel 전달. PRIMARY는 로컬 선택 경로·확인된 host5522·idle transaction·살아 있는 동일 포커스 조건 | 로컬 수평 wheel 무시. resize/focus 변경 시 PRIMARY 요청 취소. WT5522 false라 middle paste 불가. OS paste와 별개이며 child 요청용 --clipboard read-write 정책과도 구분 |
| IN12 P | 내부 scrollback 검색·선택 복사 | 선택 복사 구현, libghostty search API 존재 | 검색 API 10개 모두 미연결; host 검색이 내부 전체 history를 검색한다고 보장 불가 |
| IN13 B | popup key는 popup만 소비, 닫힘 후 native 입력 복귀 | 플러그인 focus/task 관리; A·W | 이전 PTY 입력 회수·실행 프로그램 정지는 보장하지 않는 계약 |

### 3.4 호스트 효과·질의 응답·지원 광고

| ID / 수준 | 요구 동작 | 현재 구현·증거 | 남은 항목 |
|---|---|---|---|
| HX01 B | DA/DSR/CPR/DECRQM/DECRQSS 응답을 올바른 PTY에 전달 | WRITE_PTY 연결; 일부 literal 기대값 A | 전체 query/parameter별 결과와 mode 전이·popup 중 응답 순서 미검증 |
| HX02 B | `TERM`·terminfo·기능 질의·실제 표현의 정합성 | child xterm-256color, 고정 VT220/color DA | `TERMINFO_NAME` 미설정; TN/XTGETTCAP 미검증. 287 capability 중 누락·과대 광고 감사 필요 |
| HX03 B | 크기 조회는 셸 영역의 rows/cols 및 확인된 pixel geometry | SIZE callback/resize 경로 연결 | pixel 크기 미확인일 때 질의·mode2048의 일관성 미검증 |
| HX04 B | 색상 조회·팔레트 변경·default reset 동작 | native/host appearance; A | WT theme 변경·색상 응답·적용 픽셀 왕복 미검증 |
| HX05 B | BEL은 호스트 설정에 따라 표시/소리 | callback → PTK bell | 여러 BEL은 프레임별 coalesce 가능; 물리 WT 동작 미검증 |
| HX06 B | 프로그램 제목 변경과 종료 복원 | callback → title_control gate | WT 현재 gate off. title-report(readback)는 별도 정책 disabled |
| HX07 E | 현재 디렉터리 보고와 종료 복원 | PWD effect → OSC7, launch cwd 복원 | OSC9/1337 bare path→URI 정규화가 코드에 없음. 실제 오류 재현은 미실행 |
| HX08 B | 명시적 clipboard write/read 정책, 오류·취소·늦은 응답 처리 | Python broker가 OSC52/5522/1337 Copy 처리; A | 기본 write/read opt-in. 읽기·확인된 왕복은 host5522 의존, WT off. 실제 Windows clipboard 미검증 |
| HX09 E | desktop notification·progress·program status | 엔진 callback 있음 | OSC9/777, OSC9;4, OSC7501 미연결 |
| HX10 E | 프로그램 요청으로 호스트 창 크기 변경 | RESIZE_WINDOW API 있음 | CSI8t 미연결. 호스트 창 변경을 PTY에 전달하는 IO04와 다른 기능 |
| HX11 E | OSC22 마우스 포인터 모양 | 엔진 MOUSE_SHAPE 데이터 있음 | 데이터 조회/host 적용 미연결 |
| HX12 B | 미지원 요청의 처리 범위와 지원 광고를 일치시킴 | 엔진 및 adapter의 거부 경로 일부 있음 | unknown 통계/callback 없음. 모르는 escape를 호스트로 그대로 전달하는 제품 기능 없음 |
| HX13 P | native 오류·로그를 진단에서 확인 | 버전/ABI 검사와 Python 오류·로그 경로 | SYS_LOG 및 VT_PROCESSING_ERROR 미연결. 현재 파일 로그가 native 로그까지 포함하지 않음 |

### 3.5 화면 합성·제품 사용성

| ID / 수준 | 요구 동작 | 현재 구현·증거 | 남은 항목 |
|---|---|---|---|
| UI01 B | 셸 화면·UI·이미지·효과는 한 writer에서 순서대로 출력 | `TerminalRenderer/Transaction`, A | 모든 exit/error/host reply와 고부하 동시성 WT 검증 필요 |
| UI02 B | 기본형에 바·팝업·UI 전용 단축키·셸 hook 없음 | 명시 plugin 선택, 기본형 A·W | 전체 화면 VT·내부 history·마우스 처리라는 차이는 남음 |
| UI03 B | 바가 차지하는 행을 셸 크기에서 제외 | PTK layout → VT/PTY size; A·W | 바 동적 높이·최소 화면·다중 팝업 실제 WT 미검증 |
| UI04 B | popup 닫힘/이동/중첩 후 최신 배경·커서 복원 | 셀 합성, opaque rect, wide-tail 회귀 A; popup workflow W | 픽셀 복원 미검증. 임의 alpha/비직사각형 합성은 계약 밖 |
| UI05 B | 비동기 plugin 실패가 포커스·입력·자원 상태를 깨지 않음 | Plugins task scope/close, A | 모든 actual WT 비동기 실패 조합 미검증 |
| UI06 P | 설정·언어·로그·버전·환경 진단을 제품 수준으로 제공 | 공통 i18n/log/PluginManager 일부 재사용 | `.ishrc.py`, --lang/--version/--diagnose/--login/--shell-arg/전체 로그 설정 미이식. 영어 CLI 문구 잔존 |
| UI07 P | history 한도·이미지 한도·ESC 지연·clipboard 정책을 설명 | 고정 상수와 clipboard CLI 있음 | 대부분의 한도/동작을 사용자가 설정하는 CLI/rc API 없음 |
| UI08 P | 종료 후 화면/history 정책을 사용자가 예측 가능 | PTK full_screen/alternate에서 실행하고 종료 시 호스트의 기존 primary 화면으로 복귀 | 내부 scrollback을 호스트 scrollback으로 이관하는 구현 없음 |
| UI09 P | 접근성·screen reader·폰트/배율/IME 호환성 | 호스트/PTK 기능 일부에 의존 | 전용 WT 접근성 및 실제 입력 검증 없음 |
| UI10 E | 셸 후보+plugin 후보 dropdown, 명령줄 우측 prompt | 셸 native completion은 W | 후보/편집 버퍼 취득·적용 API 없음. VT 상태만으로 추정하지 않음 |

### 3.6 이미지 확장과 정책

| ID / 수준 | 요구 동작 | 현재 구현·증거 | 남은 항목 |
|---|---|---|---|
| IM01 E | Kitty 직접 PNG/RGB/RGBA 입력·저장·배치·삭제 | native Kitty + Pillow PNG, A | 실제 WT graphics는 disabled. 표시 성공 증거 없음 |
| IM02 E | Kitty animation·crop·layer·scrollback·resize | native tick + Presenter, A | 실제 WT·팝업·동영상 픽셀 검증 없음 |
| IM03 E | file/temp/shared-memory 전송 | native API 존재 | 모두 기본 disabled 정책. 직접 byte 전송과 구분 |
| IM04 E | iTerm inline/multipart 정적 raster | `image_protocol.py` 보완 모듈, A | animated/file download 및 일부 margin/origin/pending-wrap 상황 거부 |
| IM05 E | SIXEL 입력 | route=`unsupported`, 거부 회귀 A | 미지원. 변환 모듈 구현·활성화 없음 |
| IM06 E | 호스트 이미지 출력 | Kitty 또는 특정 WezTerm iTerm raster backend | SIXEL 출력 backend 없음. WT가 SIXEL을 지원해도 ish-vt가 자동 지원하지 않음 |
| IM07 E | popup 가림·이동·삭제·종료 후 복원 | Presenter + opaque rectangles, A | iTerm raster는 below-background 거부, below-text는 대상 셀이 공백임을 입증할 때만 허용. 임의 host 그래픽 pass-through 복원 보장 없음 |
| IM08 B/E | 미지원/한도 초과/geometry 불명일 때 상태를 설명 | 오류·한도·feature gates, 텍스트 유지 경로 | 사용자 표시/설정·재활성화 정책 정리 필요. 이미지 거부를 이미지 성공으로 세지 않음 |

Windows Terminal은 SIXEL 지원을 도입했다. 이것은 호스트 기능이며 ish-vt의 SIXEL 입력/출력 구현을 대신하지 않는다. [Microsoft 1.22 발표](https://devblogs.microsoft.com/commandline/windows-terminal-preview-1-22-release/). `ish` stream은 원본 전달 경로이므로 이 VT 결손과 별개다.

## 4. libghostty 1.0.1 공개 API 대조

### 4.1 Effect callback 19개 전수

Native 직접 등록은 8개다. Clipboard 2개와 RIS의 앞단 처리를 고려해야 하므로, 콜백 미등록 수를 기능 미지원 수로 읽으면 안 된다. 근거는 `native/bridge.c:223`, `inputs.py:118`, `clipboard.py:271`, `vt.py:469`, `app.py:711`이다.

| 번호 / option | 현재 처리 | 판정 |
|---|---|---|
| 2 WRITE_PTY | replies buffer → Python → Session | 연결, 질의별 결과 미검증 부분 있음 |
| 3 BELL | effect → PTK bell | 연결, coalesce 정책 |
| 4 ENQUIRY | 미등록 | ENQ answerback 미연결, 레거시 E |
| 5 XTVERSION | 미등록 | ish 식별 연결 없음. native 기본 응답은 미검증이며 무응답으로 단정하지 않음 |
| 6 TITLE_CHANGED | profile gate → set_title | 조건부 연결, WT off |
| 7 SIZE | rows/cols/cell size | 연결, pixel 불명 상태 검증 필요 |
| 8 COLOR_SCHEME | 관측된 scheme만 응답 | 조건부 연결 |
| 9 DEVICE_ATTRIBUTES | VT220/color 고정 프로필 | 연결, 지원 광고 대조 필요 |
| 26 PWD_CHANGED | OSC7 재출력 | 연결, bare path 정규화 검증 필요 |
| 27 CLIPBOARD_WRITE | Python broker 선점 | 대체 구현, native 미등록을 결손으로 세지 않음 |
| 30 DESKTOP_NOTIFICATION | 미등록 | 미연결 |
| 31 PROGRESS_REPORT | 미등록 | 미연결 |
| 36 UNKNOWN_SEQUENCE | 미등록, MAX_BYTES=0 | unknown OSC/APC 보고 미연결; 범용 fallback API 아님 |
| 39 CLIPBOARD_READ | Python broker 선점 | 조건부 대체 구현 |
| 42 RENDER_HOLD | frame/image hold | 연결, 1초 상한 |
| 43 SEMANTIC_PROMPT | 미등록 | OSC133 이벤트 활용 미연결; hookless 바/팝업에는 필수 아님 |
| 44 RESET | InputPipeline RIS 취소 + native parser | reset 자체 지원; host까지 일관성 검증 필요 |
| 47 PROGRAM_STATUS | 미등록 | OSC7501 미지원 |
| 48 RESIZE_WINDOW | 미등록 | CSI8t 호스트 창 변경 미연결 |

`UNKNOWN_SEQUENCE`는 현재 OSC/APC의 종료된 미지원 문자열 일부를 제공한다. CSI/DCS 전체를 포괄하지 않으며 취소·truncation·엔진에서 비활성화한 알려진 기능 등은 별도다. 이 콜백을 등록하면 모든 미확인 escape를 완전하게 보존할 수 있다는 의미가 아니다.

### 4.2 나머지 terminal option 29개 전수

위 19개와 합하여 48개이다. invalid/max sentinel은 제외했다. 로컬 [`terminal.h`](../.cache/ghostty-vt/install/include/ghostty/vt/terminal.h)의 2034줄 이후를 기준으로 한다. 이 로컬 1.0.1 헤더가 감사 기준이며 온라인 최신 문서와 구분한다.

| 번호 / option | 현재 상태 |
|---|---|
| 1 USERDATA | bridge state 연결 |
| 10 TITLE | 초기 setter 미사용. OSC 제목 처리 자체는 있음 |
| 11 PWD | 초기 setter 미사용. OSC 디렉터리 처리 자체는 있음 |
| 12 COLOR_FOREGROUND | host/default/native override 연결 |
| 13 COLOR_BACKGROUND | 연결, 기본 배경과 명시 색 구별 |
| 14 COLOR_CURSOR | native 값 연결, host 표현은 gate |
| 15 COLOR_PALETTE | host palette/native override 연결 |
| 16 KITTY_IMAGE_STORAGE_LIMIT | 최초 0, 검증된 host 조건에서 설정 |
| 17 KITTY_IMAGE_MEDIUM_FILE | 기본 disabled 유지 |
| 18 KITTY_IMAGE_MEDIUM_TEMP_FILE | 기본 disabled 유지 |
| 19 KITTY_IMAGE_MEDIUM_SHARED_MEM | 기본 disabled 유지 |
| 20 APC_MAX_BYTES | native 기본값, 바깥 framing 한도도 적용 |
| 21 APC_MAX_BYTES_KITTY | native 기본값, 한도 간 일관성 검증 필요 |
| 22 SELECTION | native gesture/anchor 연결 |
| 23 DEFAULT_CURSOR_STYLE | 미설정, native block 기본; host 기본 상속 없음 |
| 24 DEFAULT_CURSOR_BLINK | 미설정, native nonblinking 기본 |
| 25 GLYPH_PROTOCOL | 기본 disabled. 새 glyph 등록 확장 미지원 |
| 28 SCROLLBACK_MAX_BYTES | 32MiB 기본 연결 |
| 29 SCROLLBACK_MAX_LINES | 10,000줄 기본 연결 |
| 32 CONTINUATION_MAX_BYTES | wire snapshot export 추적 비활성. 셸 PS2나 일반 분할 파싱과 무관 |
| 33 TITLE_REPORT | 기본 disabled, CSI21t readback 미지원 |
| 34 MODE_DEFAULT | native 기본값 사용 |
| 35 MODE | hold 강제 해제 등 연결 |
| 37 UNKNOWN_MAX_BYTES | 0, 보고 비활성 |
| 38 TERMINFO_NAME | 미설정, XTGETTCAP TN 정합성 결손 후보 |
| 40 CLIPBOARD_WRITE_MAX_BYTES | native override 없음, Python broker 한도 사용 |
| 41 RESIZE_PULL_SCROLLBACK | native 기본 true. 현재 Linux PTY 범위의 결손으로 세지 않음 |
| 45 XT_CHECKSUM_REPORT | 기본 disabled |
| 46 XT_CHECKSUM_EXTENSION | 기본 0 = DEC checksum 계산 방식. 보고 자체는 옵션 45가 꺼져 있어 현재 사용하지 않음 |

추가 system option 4개: `DECODE_PNG`는 Pillow로 연결, `LOG`는 미연결, `USERDATA`는 callback에 필요 없어 미사용, `RANDOM_SECURE` override는 OS 기본 사용이므로 결손이 아니다.

### 4.3 공개 헤더 35개 전체의 역할

| 헤더 | 현재 처리 및 미사용의 의미 |
|---|---|
| terminal.h | 생성/feed/resize/get/set/viewport/grid-ref 사용. `vt_write_until` 대신 ProtocolStream 사용 |
| screen.h, render.h, style.h | 셀/grapheme/style/색상/커서/image iterator 사용. 독립 helper 미사용 자체는 결손 아님 |
| grid_ref.h, grid_ref_tracked.h, point.h | 링크·선택 anchor 사용. render-state가 일부 getter를 대체 |
| selection.h, formatter.h | gesture/selection/text 사용. convenience 함수 전부를 호출할 필요 없음 |
| key.h, key/event.h, key/encoder.h | encoder 사용. frontend key/modifier 범위는 제한 |
| mouse.h, mouse/event.h, mouse/encoder.h | mode-aware encoder 사용. host 입력 범위 별도 |
| paste.h, focus.h | plain UTF-8 explicit paste와 focus 연결. 모든 MIME paste는 아님 |
| modes.h | mode 조회/변경 사용. standalone report encoder 미사용은 native 응답이 대체 |
| color.h, color_scheme.h, size_report.h, device.h | terminal callbacks와 state 사용. standalone encoder 미사용은 결손 아님 |
| kitty_graphics.h | pixels/generation/tick 사용. placement는 render API로 접근 |
| osc.h, sgr.h, unicode.h | standalone API 미사용. terminal parser와 resolved grid가 대신 처리 |
| search.h | 10개 검색 API 미사용. 내부 scrollback 검색 미연결 |
| snapshot.h | serialization/replay 미연결. 저장·재접속 서비스 미구현 |
| allocator.h, build_info.h, version.h, types.h, io.h | 할당/version/build 검사·writer 사용. 진단 helper 미사용은 사용자 기능 결손과 구분 |
| sys.h | PNG 연결, native log 미연결 |
| wasm.h | WASM 실행 범위 밖 |

추가 미연결 데이터는 `VT_PROCESSING_ERROR`, `MOUSE_SHAPE`, render의 password-input 표시 및 wide-tail cursor 정보다. 마지막 두 항목은 각각 선택적 표시와 정확한 커서 표현 검증 대상으로 남긴다. compression/dirty-row/overscan/증분 snapshot API 미사용은 성능 최적화 후보이며 기본 표시 기능 결손으로 세지 않는다.

### 4.4 Named mode 43개의 대조 범위

| 묶음 | 코드 | 감사 판정 |
|---|---|---|
| ANSI 4개 | 2 keyboard action, 4 insert, 12 send/receive, 20 newline | native/encoder 위임. 네 모드의 사용자 결과 전수 미검증 |
| DEC cursor·layout 10개 | 1,3,4,5,6,7,8,12,25,40 | 일부 cursor/reverse/wrap A. 80/132 column·repeat·기본값·reset 전체 미검증 |
| DEC margin·key·screen 13개 | 45,47,66,67,69,1035,1036,1039,1045,1047,1048,1049,2004 | 1049/paste/일부 key A·W. 키패드·margin·reverse wrap·나머지 변형은 전수 미검증 |
| DEC mouse·focus 10개 | 9,1000,1002,1003,1004,1005,1006,1007,1015,1016 | native encoder/gesture 연결. host decoder 범위 제한 및 WT 물리 이벤트 미검증 |
| DEC 현대 확장 6개 | 2026,2027,2031,2033,2048,5522 | 2026/2031 일부 A; 2033 visibility 미연결 여부 확인 필요; 5522 unsolicited paste는 broker가 permanently-reset으로 응답 |

이 분류는 mode define의 존재를 지원으로 승격하지 않는다. 특히 ANSI keyboard disable/local echo, DEC autorepeat, visibility는 외부 입력·호스트 연결까지 확인해야 한다.

## 5. terminfo 광고와 전수 검증이 필요한 항목

실제 Ubuntu `xterm-256color` 287개 capability를 읽었다. [전체 카탈로그와 그룹별 판정](VT_TERMINFO_CATALOG.md)에 이름·종류·원래 값을 보존했다. **모든 실제 entry**를 향후 검증 목록으로 사용해야 한다. 아래 주요 차이는 확정된 실패 재현과 정적 결손 후보를 구분한다.

| 묶음 | 대표 entry | 현재 확인된 간극 |
|---|---|---|
| 화면·편집·색상 | cup/csr/ich/dch/il/dl/ech/rep/setaf/setab/sgr/initc | native 일반 경로 존재. entry별 전수 테스트 없음 |
| 초기화·reset·화면 전환 | is2/rs1/rs2/smcup/rmcup/smkx/rmkx | 여러 제어를 한 entry에서 조합. title push/pop·keypad·mode reset까지 묶어서 검증 필요 |
| 스타일 | blink/invis/dim/smxx/rmxx/sitm/ritm | host 실제 표현 검증 없음. unsupported host 정책을 명시해야 함 |
| 기본·수정·F 키 | kf1…kf63, kDC/kUP/... 확장 | bridge named 범위는 F1–12. fallback과 child mode 재인코딩은 별도이며 전수 미검증 |
| 키패드 | ka1/ka2/ka3/kb1/kb2/kb3/kc1/kc2/kc3/kp* | mode/NumLock/물리키 식별 정합성 미검증 |
| clipboard·cursor·focus·paste | Ms/Cr/Cs/Se/Ss/fe/fd/BE/BD/PS/PE | cursor color와 일부 host 기능은 WT gate off; 실제 device 결과 미검증 |
| 질의와 식별 | u6/u7/u8/u9/RV/rv/XR/xr | 고정 DA, XTVERSION frontend 미연결, TN 미설정. xterm regex와 ish 식별 정책 정리 필요 |
| 레거시 peripheral | mc0/mc4/mc5/mc5i, meml/memu, smm/rmm | 프린터·memory lock·8-bit meta 등을 기본으로 약속하지 않음. 선택한 TERM과의 차이를 명시/검증해야 함 |
| 플래그·수치 | am/bce/ccc/km/mir/msgr/xenl/AX/XT/cols/lines/colors/pairs | 문자열뿐 아니라 flag/number의 약속도 검증 대상. cols/lines의 정적 기본값은 실제 PTY 크기와 구분 |

무조건 xterm을 완전히 복제하는 것이 명세의 목표는 아니다. 그러나 `TERM`을 그렇게 설정한 이상 프로그램이 사용하는 광고를 조용히 무시하는 상태는 B 완료 조건에 맞지 않는다. 호환되는 entry와 runtime capability를 정합적으로 선택하는 작업이 필요하다.

## 6. Windows Terminal에서 확인된 상태

`host-confirmed`의 6개 최종 진단은 동일하게 `host_adapter=unknown`이다. 정책상 꺼진 값과 호스트 자체가 기능을 지원하지 않는다는 주장은 구분한다.

| 값 | 6개 실행의 최종 상태 | 의미 |
|---|---|---|
| graphics_enabled / image_protocol | false / null | 이미지 성공 증거 없음 |
| cell_width / cell_height | 0 / 0 | resize 후 pixel geometry 미확인 |
| graphics_reason | cell-size-unavailable-after-resize | geometry 경로의 한계도 있음. 이것만 고쳐도 SIXEL 입력/출력 결손이 해결되는 것은 아님 |
| kitty_graphics | false | Kitty 출력 미활성 |
| hyperlinks / extended_styles / overline | false | WT의 실제 지원 여부와 별개로 ish 표현 경로가 꺼짐 |
| enhanced_keyboard / pixel_mouse | false | 해당 확장 입력 검증 불가 |
| clipboard_protocol | false | OSC5522 확인된 왕복/PRIMARY paste 제한 |
| title_control / title_stack / cursor_color_control | false | 제목·커서색 적용/복원 경로 제한 |
| scheme_notifications | false | 동적 theme 알림 연결 제한 |
| synchronized_output | true | 해당 capability 관측됨 |
| true_color | false | PTK 환경 감지 fallback이 있으므로 256색으로 단정하지 않음 |

현재 HostAdapter는 Kitty, 특정 WezTerm 버전, XTerm 등의 경로를 가진다. Windows Terminal 전용 정책은 없으며, unknown 호스트도 일부 ACK/geometry 기반 기능을 사용할 수 있다. 호스트 이름 추가만으로 검증 없이 모든 flag를 켜는 것은 해결책이 아니다.

## 7. 보존된 검증 증거와 신뢰 범위

- [`terminal-final.log`](../.cache/hookless-vt/terminal-final.log): 324개 회귀 통과 및 lint/format 통과 기록. 실제 native library를 사용하는 테스트와 PTY/PTK 회귀가 포함된다.
- [`stream-final.log`](../.cache/hookless-vt/stream-final.log): 별도 stream 385개 회귀 통과 기록.
- [`host-confirmed/summary.json`](../.cache/hookless-vt/host-confirmed/summary.json): 셸 3개 × 기본형/플러그인형 6개 통과.
- `host-final`은 120×36→82×36→120×36, `host-confirmed`는 120×36→120×24→120×36. 총 12 case 실행에서 24회 변경. native 프로그램 SIGWINCH와 Vim 행/열 값으로 전달을 확인했다.
- [`source-manifest.json`](../.cache/hookless-vt/host-confirmed/source-manifest.json)의 36개 SHA256은 감사 시점 현재 파일과 모두 일치했다. **모든 테스트 파일과 실제 .so 바이너리까지 묶인 manifest는 아니다.**
- 이 manifest는 `host-confirmed`에만 있으며 `host-final`에는 같은 소스 manifest가 없다. 두 실행의 소스 동일성을 같은 수준으로 증명하지 않는다.
- W 입력은 제어기가 outer PTY에 쓴 바이트이며 응답은 실제 WT 것이다. 각 result의 `pixel_verification`/`gui_keyboard_verification`은 **not performed**다.
- UI의 한글·emoji가 파일에 정확히 저장된 것과, host 픽셀·폰트·IME가 정확히 보였다는 것은 다른 증거다.
- 현재 worktree에는 `.cache/terminal-test/evidence`가 없다. Kitty/Xvfb·WezTerm·xterm GUI 테스트 소스가 있다는 이유로 이 작업본에서 실행했다고 간주하지 않는다.

`.cache`의 헤더·검증 기록은 Git에서 제외된 로컬 자료다. 이 문서를 커밋하더라도 GitHub 독자가 근거 링크를 모두 열 수 있는 것은 아니다. 공개 검증 때는 버전·manifest와 결과를 별도 artifact로 보존해야 한다.

### 7.1 검증 공백

| 영역 | 기존 증거 | 미검증 |
|---|---|---|
| 셸 workflow | primary/native completion/continuation/loop/heredoc/paste/rc reload/raw stdin/popup/Vim/Ctrl+C/D/정상 종료 A·W | 모든 사용자 rc·모든 셸 버전·job control 전체 |
| 텍스트·화면 | 독립 literal 기대값을 쓰는 native/compose A | 표준 item별 complete coverage, 실제 pixel |
| 질의·host feature | 여러 mock wire 및 native reply A, 실제 probe relay W | 각 query·mode·late reply의 실제 호스트 전수 |
| 입력 | scripted key/paste A·W | 물리 keyboard/IME/AltGr/ESC latency/mouse/focus |
| 선택·scrollback | native gesture/formatter·PTK A | 실제 WT wheel/drag/클립보드 결과 |
| 이미지 | native/adapter/Presenter A | WT 이미지·팝업 가림·resize·동영상 실제 표시 |
| 종료 | 정상 termios W, 일부 HUP/TERM A | 실제 WT 강제 종료/suspend-resume/모든 초기 mode 복원 |

### 7.2 누락 방어와 CI

`tools/check_terminal.py`는 18개 필수 terminal 모듈과 2개 공유 PluginManager 모듈, 추가 자동 발견 테스트를 실행한다. 환경/모듈/import/빈 테스트/skip의 실패 판정이 있고 `test_terminal_checks.py`의 독립 목록으로 필수 파일 삭제를 방어한다. 하지만 **기능 ID별 테스트가 반드시 있다는 검사까지 있는 것은 아니다.**

GUI 6개 모듈은 `tools/check_terminal_gui.py`의 별도 opt-in 경로다. `.github/workflows/stream.yml`은 stream 검사와 stream wheel 검사만 연결한다. **VT 회귀·native build·GUI·WT host matrix는 현재 PR CI에 연결되지 않았다.**

hookless checker/scenarios/host/matrix와 일부 새 테스트 및 문서는 아직 untracked다. ignore 예외는 있지만, 원격 checkout에 포함되어 재현 가능하다는 보장은 커밋 전에는 없다. 이번 문서 작업은 커밋하지 않는다.

## 8. 고정 한도와 의도적인 동작 차이

| 소유 경계 | 현재 값/정책 | 영향 |
|---|---|---|
| VT frame | 262,144 cells; 버퍼 32MiB | 임의 크기 화면/프레임을 무제한 지원하지 않음 |
| scrollback | 10,000 lines / 32MiB | 호스트의 전체 history와 다름 |
| native image | 16MiB 기본 storage | 넘는 이미지의 거부/정리 검증 필요 |
| protocol framing | 문자열 32MiB, 일반 sequence 4096 bytes | 한도 넘는 입력과 중간 취소 동작이 계약 |
| graphics presenter | image 16MiB, cache 32MiB, placement 1024, fragment 4096, occluder 128 | popup 가림으로 fragment가 늘어날 수 있음 |
| graphics recovery | host failure 3회 후 제한 | 무한 retry하지 않음; 회복 정책을 사용자에게 설명해야 함 |
| clipboard broker | transaction 64MiB, text 1MiB, metadata 4096bytes, MIME 256개, 요청 30초 | host permission 및 5522 조건 별도. timeout은 실패·취소 경계 |
| host decoder | 제어 문자열 64KiB, 좌표/크기 65,535, 셀 pixel 4096 | 큰 응답·좌표를 무제한 수용하지 않음 |
| host title | 조회한 원래 제목 1024자 | 종료 복원을 위한 원래 제목 수용 한도 |
| PTY input/write | 4MiB queue | overflow는 transport 오류 경로, 무제한 입력 저장 아님 |
| PTY output | 배치 256KiB; 종료 후 final pass 같은 상한 | 지속 background output의 수명 정책과 구분 |
| host effect queue | 96MiB | arbitrary host side-effect를 무제한 누적하지 않음 |
| UI | popup 16개, bar 최대 8행 | 불투명 직사각형 popup 계약 |
| timing | ESC 50ms, probe 750ms, render hold 1초 | probe 실패/hold 해제 상한이지 셸 readiness 추론이 아님 |
| 입력·렌더 주기 | wheel 3행, selection autoscroll 50ms, 연속 click 500ms, 최소 redraw 간격 1/60초 | 조작 감도·지연 정책이며 모든 화면에서 60fps 보장은 아님 |
| shutdown | HUP 후 최대 2초, 남은 group KILL | 강제 정리 정책. 원격 세션/daemon 영속성 서비스 아님 |

근거: `vt.py`, `protocol.py`, `graphics.py`, `clipboard.py`, `host.py`, `mouse.py`, `selection.py`, `session.py`, `app.py`, `plugins.py` 선언부. 이 값들은 대부분 현재 VT 설정 API에서 조절되지 않는다.

## 9. Windows Terminal/WSL 검증 계획

이 절은 **계획**이며 이번 감사에서 실행한 테스트가 아니다. 새로운 capability를 켜기 전에 해당 결과가 필요하다.

### 단계 0. 재현 가능한 기준 고정

- 기존 stream worktree를 보존하고 `ish`/`ish-vt` 명령·로그·결과 디렉터리를 구분한다.
- WT/WSL/Ubuntu/kernel·셸·Vim·Python·PTK·libghostty·native bridge의 버전과 SHA256, 소스 tree hash, 실행 argv/env, font·font size·배율·theme·viewport를 기록한다. 개인 rc를 변경하지 않는다.
- 실제 `infocmp -1 -x xterm-256color`의 287개 entry를 독립 요구 목록으로 고정한다. 새 버전에서는 diff를 검토하며 누락을 자동 성공 처리하지 않는다.
- 가상화된 child의 광고, native engine 지원, frontend 표현, host 능력을 각기 수집한다. host 응답을 가짜로 주입한 회귀와 실제 host 응답을 구분한다.

### 단계 1. 기본 VT 의미와 실제 입력 소비자

- VT01–VT16, HX01–HX04를 명세별 literal 기대값에 연결한다. 가능한 부분은 VTTEST를 재사용하되 자동 결과와 수동 화면 판정을 구분한다.
- 같은 byte stream을 한 번에/매 바이트/여러 경계로 나누어 feed한다. reset·취소·잘린 문자열·잘못된 UTF-8·초과 입력 이후 정상 입력이 회복되는지 확인한다.
- basic/alternate, margins/origin/wrap, wide/combining, reset·SGR·cursor·query의 pairwise 조합을 검사한다. 현재 구현을 호출해서 기대값을 생성하지 않는다.
- key/paste/mouse/host reply는 별도 raw 수신 fixture에 기록하고, popup/native 최종 소비자를 파일 바이트로 확인한다.

### 단계 2. 실제 WT 텍스트·키보드·마우스

- 기존 6조합(Bash/zsh/tcsh × 기본형/플러그인형) 시나리오를 보존한다. bar only/popup only/both도 추가한다.
- WT를 직접 실행한 기준 화면과 ish-vt 화면을 동일 fixture로 비교한다. 글자·셀 위치뿐 아니라 넓은 글자 양쪽 가림, 마지막 열, transparency/default black, underline/color/cursor를 screenshot/픽셀과 육안으로 검사한다.
- 물리 Escape·Alt/AltGr·Ctrl 조합·F1–F12/수정 F-key·숫자 키패드·한글 IME 확정/취소를 검사한다. scripted PTY 입력으로 대체 통과 처리하지 않는다.
- 실제 wheel/drag/double/triple click/사각 선택/중간 클릭, focus 전환, Windows clipboard 읽기·쓰기·거부·지연을 확인한다. clipboard 테스트는 전용 임시 내용과 복원 절차를 사용한다.
- font·배율·theme 변경, 너비/높이 resize, 최소 크기, child mouse mode와 로컬 선택의 전환을 확인한다.

### 단계 3. 호스트 효과·광고의 정합성

- OSC8, title/cursor color, OSC4/10/11/12, BEL, cwd URI, focus/size/색상 질의를 실제 WT에서 확인한다. WT가 직접 지원하는데 ish가 gate하는 항목과 호스트 미지원 항목을 분리한다.
- unknown adapter의 flag를 이름만 보고 일괄 활성화하지 않는다. feature probe/정책과 실제 표현·복원이 모두 확인된 뒤 capability를 선언한다.
- `TERM`/terminfo와 DA/DECRQM/XTGETTCAP/XTVERSION 응답을 대조한다. 지원하지 않는 기능을 지원한다고 알려 프로그램이 잘못된 경로를 고르게 하지 않는다.
- replies의 지연·분할·중복·popup 중 도착·종료 후 도착을 검사한다. 답변이 사용자 키로 해석되거나 두 번 전달되지 않아야 한다.

### 단계 4. 이미지 모듈 별도 gate

- 현재 WT 이미지 결과는 미지원/비활성이다. 먼저 direct WT에서 같은 SIXEL fixture의 표시를 확인하고, ish-vt에서 실패하는 입력/출력/geometry 경계를 분리한다.
- IM01–IM08은 채택할 프로토콜별로 별도 합격 판정을 둔다. SIXEL을 채택한다면 입력 디코더와 host 인코더, 투명 배경·palette·cursor·scroll 의미를 모두 검사한다. 모듈 설치만으로 합격하지 않는다.
- popup 표시/이동/중첩/닫힘, 일반 출력·전체 지우기·scroll·resize·alternate·종료와 이미지를 조합한다. animation은 정지 화면과 별도 검증한다.
- 원본 또는 변환 결과 중 한 번만 표시되는지, 한도 초과·실패 후 텍스트와 입력이 유지되는지 확인한다.

### 단계 5. 수명·실패·장시간·회귀

- normal exit, Ctrl+C/D, child Ctrl+Z/fg/bg, ish HUP/TERM/TSTP/CONT, child crash, plugin async exception, host 닫힘, writer failure를 구분한다.
- 시작 전후 termios뿐 아니라 alternate screen, cursor shape/visibility/color, title/cwd, keyboard/mouse/paste/focus/sync modes, 이미지 자원과 남은 프로세스를 비교한다. SIGKILL/host 강제 소멸처럼 프로그램이 정리 코드를 실행할 수 없는 상황은 보장 범위를 명시한다.
- 대량 output/paste, 긴 history, 반복 resize/popup, 느린 host에서 입력·출력 유실 및 메모리 한도를 확인한다.
- 기존 `check_terminal.py`, 별도 GUI 검사, 실제 WT 검사, `check_stream.py`의 결과를 나누어 보관한다. stream 경로의 보존도 별도 gate로 유지한다.

### 합격 기준과 누락 방어

1. 모든 B 항목에 명세 근거·소유 계층·구현·독립 테스트·실제 호스트 결과가 있어야 한다. 지원하지 않는 host 조건은 명시된 대체 동작과 함께 기록한다.
2. **미검증을 pass로 집계하지 않는다.** A/W/pixel/physical input 결과를 각각 표시한다. 실패한 필수 항목을 사후 E/N으로 바꾸지 않는다.
3. 필수 모듈 목록 외에 명세 ID별 테스트 연결표도 필요하다. 검사 파일 삭제와 개별 기능 테스트 삭제를 별도로 탐지한다.
4. native/API/PTK 업그레이드 때 새 옵션·콜백·mode·render field·terminfo entry를 분류하지 않으면 검토가 완료되지 않은 것으로 취급한다.
5. supported capability는 엔진·연결·호스트의 실제 경로가 완성됐을 때만 선언한다. 테스트 개수만으로 완료를 판정하지 않는다.

## 10. 우선 처리할 결손과 명시적 제외

### 기본 완료를 막는 우선 항목

1. 표준/terminfo 항목과 테스트의 연결표 부재, VT PR 검사 미연결, 실제 WT 입력·픽셀 증거 부재.
2. native 처리 오류·로그 관측 미연결 및 지원 광고/식별의 정합성 미검증.
3. WT에서 제목·커서색·확장 스타일·링크·읽기 clipboard 등이 꺼지는 정책과 실제 호스트 지원의 간극.
4. key/mouse 입력 범위, ESC/Alt/IME, 검색·선택·clipboard의 사용자 결과 검증.
5. 기본 cursor 상속, hollow cursor 축소, reset/종료/자체 suspend-resume의 상태 복원 범위.
6. 실험 CLI와 제품 수준 설정·번역·진단·옵션의 차이. 제품 stream의 기능을 VT에도 있다고 보고하지 않기.

### 선택 확장 및 범위 밖

- SIXEL, Kitty 파일/공유메모리, iTerm 애니메이션/파일 다운로드, 현대 glyph/status/notification, shell completion API는 현재 상태를 각각 미지원/미연결로 유지한다. 필요한 확장만 별도 gate로 채택한다.
- 임의 unknown escape의 완전 passthrough·완전한 UI 복원, 모든 호스트의 모든 확장을 무수정 재현하는 것은 이 VT 명세의 보장이 아니다.
- 네이티브 Windows/ConPTY, macOS, daemon attach/detach·세션 저장, WASM, Tektronix/ReGIS/프린터 등 peripheral 서비스를 현재 기본 지원으로 선언하지 않는다.
- legacy/control 확장을 입력받을 때 파서가 정상 상태로 돌아오고 이후 텍스트가 계속 표시되는 성질은 B의 오류 복구 검증에 포함한다.

이 문서는 알려진 API 연결과 검증의 빈칸을 숨기지 않는 기본 명세 초안이다. API 48개를 분류했다는 사실은 터미널 동작 전체가 증명됐다는 뜻이 아니다. 현재 완료로 확인된 것은 보존된 셸 시나리오 및 해당 자동 회귀의 범위이며, 나머지는 위 ID별 계획으로 닫아야 한다.
