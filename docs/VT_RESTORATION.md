# ish 종료·중지 복원 계약

2026-11-17, `codex/hookless-vt`. 이 문서는 `ish`의 호스트 수명 처리만 다룬다. 셸의 rc/hook과 명령줄 편집은 셸이 계속 소유한다.

## 목표와 소유자

ish가 변경한 상태 중 시작값을 확인한 것은 그 값으로 복원한다. **제목은 사용자가 선택한 예외**로, 셸·프로그램이 마지막으로 지정한 제목을 종료 후에도 유지한다. 모든 호스트 상태를 조회할 수 있다는 뜻은 아니다. `HostBroker`가 최초 관찰값과 변경된 모드를 관리하고, `TerminalRenderer`가 기존 PTK 출력과 reset 경로를 통해 적용한다. 화면과 history는 계속 libghostty가 소유한다. `RestoringOutput`은 기존 PTK 출력의 좁은 위임 계층이며 별도 stdout writer가 아니다.

## 시작 순서

1. PTK 입력의 raw context에서 실제 시작 `termios`를 저장한다.
2. PTK 생성자·초기 reset의 커서 쓰기와 첫 화면 렌더링을 억제한다. 원래 primary 화면을 변경하지 않은 채 같은 입력 reader에서 시작값을 질의한다.
3. 커서 모양(DECRQSS), SGR(DECRQSS), 변경 대상 private mode(DECRQM)를 한 번 관찰한다. 기존 색상·호스트 capability 조회를 재사용한다. 제목은 조회·저장하지 않는다.
4. 기존 bounded probe가 끝난 뒤 alternate 화면에 진입하고, inherited mouse capture와 origin을 정리한 후 PTK 화면을 표시한다. 이 deadline은 호스트 capability가 불명인 경우를 끝내는 수단이며 셸 입력 readiness를 판단하지 않는다.

관찰 대상 mode는 `1, 6, 7, 12, 25, 1000, 1002, 1003, 1004, 1005, 1006, 1015, 1016, 2004, 2026`이다. 실제 변경한 mode만 복원한다. 최초 요청 구간의 첫 응답만 인정하고 중복·지연 응답은 관찰값을 교체하지 않는다. `1(set)`과 `2(reset)`만 원래값으로 적용한다. 무응답, `0(unknown)`, `3/4(permanent)`를 임의의 false나 기본값으로 바꾸지 않는다.

## 종료 순서

- 기존 이미지 자원·화면 로컬 keyboard stack·focus/pixel capture·커서 색의 cleanup을 재사용한다.
- 제목 변경은 기존 native effect와 PTK 단일 writer로 전달하며, 종료·예외·중지 시 제목을 되돌리지 않는다. 호스트의 제목 변경 허용 설정은 그대로 적용한다. 별도 title capability 추정이나 title stack을 사용하지 않는다.
- origin mode인 DECOM(6)은 **alternate 화면을 떠나기 전에** 복원한다. 이 명령은 커서를 홈으로 이동시키므로 primary 커서 복원 뒤 실행하면 원래 위치가 사라진다.
- PTK가 alternate 화면·paste·mouse·cursor를 정리한 뒤 관찰한 나머지 모드, 커서 모양, SGR을 복원한다.
- 모드는 관찰한 off 값을 먼저 적용하고 on 값을 나중에 적용한다. Windows Terminal에서는 서로 다른 mouse tracking 모드도 같은 활성 상태를 사용하므로 `1000h` 뒤 `1002l/1003l`을 보내면 원래 활성 모드를 다시 끌 수 있다. 실제 호스트에서 이 실패를 재현했고, 1000/1002/1003 각각의 literal 출력·native 질의 회귀를 유지한다.
- 이후 반복되는 reset은 이미 복원한 hidden/blinking cursor를 덮어쓰지 않는다. 원래 `termios`는 PTK context 종료 때 복원된다.
- 정상 종료, HUP, TERM, PTK 예외는 같은 출력 수명 경로를 사용한다. 시작 전에 실패한 경우 변경하지 않은 화면을 불필요하게 정리하지 않는다.

## ish 자체의 중지·재개

자식 프로그램의 `Ctrl+Z → jobs → fg`와 별개다. ish 프로세스에 전달된 SIGTSTP는 PTK의 `in_terminal()`을 사용해 화면과 입력을 먼저 반환한다. 이때 단순한 cooked 기본값이 아니라 시작 `termios`를 적용한다. 그 뒤 자기 프로세스를 SIGSTOP으로 중지하므로 orphan process group의 SIGTSTP 무시에도 의존하지 않는다.

SIGCONT 후 같은 PTK Application과 native 셸을 유지하며 화면 크기를 다시 반영하고 그린다. 중지 전에는 PTY 출력 읽기를 잠시 중지하고 재개 후 다시 연결한다. 셸이나 프로그램의 stdin을 다른 소비자로 보내지 않는다. 이미 요청된 종료가 handoff 대기 중 도착하면 이후 SIGSTOP을 보내지 않는다.

## 검증

기존 회귀 모듈을 확장했으며 새 필수 테스트 모듈은 만들지 않았다.

- `test_terminal_cursor`: literal host 응답과 실제 CLI/PTK/PTY를 사용해 최초 질의 이전에 상태를 변경하지 않는지, non-default 모드를 정상/HUP/TERM 종료 후 복원하는지 확인한다. 실제 SIGTSTP/SIGCONT 전후의 termios·모드·동일 셸 PID·입력 파일 결과와 중지 중 resize도 확인한다.
- 같은 모듈의 origin 회귀는 실제 CLI 출력 전체를 outer VT에 적용하고, 독립된 기대 좌표 `(3, 3)`과 시작 scroll region을 확인한다. DECOM 복원 순서 문제를 이 검사가 재현한다.
- `test_terminal_host`: 기존 keyboard stack의 enter/pop이 같은 alternate 화면에서 일어나는지, 초기 wire 요청과 종료 순서가 맞는지 확인한다.
- `test_terminal_composition`: 반복 reset의 무효성, handoff 대기 중 종료 요청이 온 경우 SIGSTOP을 보내지 않는지 확인한다. 호스트 식별 없이도 제목 변경을 전달하고 정상 reset·마지막 렌더 예외 후 제목은 유지하면서 다른 metadata는 복원하는지 확인한다.

복원 구현의 targeted 검사에서 cursor+host 83개, 후속 cursor+composition+mouse-policy 52개가 통과했다. 최종 전체 회귀 및 실제 Windows Terminal 결과는 통합 검증 기록을 기준으로 한다. scripted host 응답을 실제 호스트 검증으로 표현하지 않는다.

## 보장하지 않는 범위

- 초기값에 응답하지 않는 호스트에서는 기존의 검증된 종료 cleanup을 사용한다. 이는 정확한 시작값 복원과 구분한다.
- nested alternate screen은 일반적인 화면 스택이 아니다. 다른 full-screen 프로그램이 이미 사용 중인 화면의 임의 중첩 보존을 보장하지 않는다.
- 터미널 창 크기, 사용자가 실행 중 바꾼 테마·폰트, 읽을 수 없는 기존 OSC7 cwd 등 모든 호스트 설정을 일괄 snapshot하지 않는다. 기존 OSC7의 launch-directory 복귀는 실제 호스트 cwd 관찰값의 복원과 구분한다.
- SIGKILL, 호스트 연결 소멸, 출력 자체의 실패에서는 종료 코드를 실행하거나 호스트에 복원 명령을 전달할 수 없을 수 있다.
- 선택 확장이나 SIXEL을 새로 활성화하지 않는다. 기본형에 UI·UI 단축키·셸 hook을 추가하지 않는다.
