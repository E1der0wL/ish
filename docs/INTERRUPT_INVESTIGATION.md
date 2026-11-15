# Bash Ctrl+C 정지·PTY 종료 조사

## 현재 결론

Ctrl+R 검색 중 Ctrl+C와 후속 입력이 빠르게 이어질 때 나타나는 정지와 실제
SIGSEGV를 재현했다. ish 연동 없는 native Bash, UI 없는 ish-stream,
데모에서 모두 관찰했다. 사용자 F2 바인딩이 없거나 F2를 아예 보내지 않는
조건에서도 native Bash 5.3.9의 SIGSEGV를 확인했다.

원인은 Bash에 포함된 Readline의 지연된 신호 처리와 검색 상태의 수명에 있다.
ish의 입력 라우터나 popup handshake를 수정할 근거는 찾지 못했다.
원인 대조용 수정은 **별도 Bash 테스트 빌드에만 적용**했다. 현재 설치된 Bash와
ish의 production 코드는 그대로이므로 **실사용 문제가 해결됐다고 선언하지 않는다.**

과거 최초 EIO 로그에는 자식 종료 상태가 없었다. 이번에는 같은 입력 경계에서
EIO를 낳는 SIGSEGV를 실제로 확보했지만, 과거 프로세스의 종료 신호까지
소급해서 확정할 수는 없다.

## 확인한 두 경로

### 살아 있는 셸의 정지

실제 syscall trace에서 다음 순서를 확인했다.

1. Readline의 pselect6가 입력 준비를 반환한다.
2. Ctrl+C의 SIGINT(SI_KERNEL)가 도착한다. TTY가 입력 큐를 비운다.
3. Readline이 이전 준비 결과를 바탕으로 read(0, 1)에 진입한다.
4. 셸은 살아 있지만 빈 입력을 기다리며 지연된 신호 처리를 마치지 못한다.

waitid(WNOWAIT)에 종료 이벤트가 없고 foreground process group도 유지된다.
별도로 SIGINT cleanup과 재전달까지 했지만 Bash의 application signal hook이
아직 실행되지 않아 다음 pselect/read로 돌아가는 형태도 관찰했다.
두 번째 Ctrl+C로 복구되는 사례는 진단 증거로만 사용했다. 제품에서 Ctrl+C를
중복 전송하거나 임의 문자를 추가하는 우회는 도입하지 않았다.

### 검색 상태 정리 후의 비정상 종료

Ctrl+R 뒤 Ctrl+C와 후속 명령을 한 입력 배치로 보내는 조건에서 cleanup
이전의 waitid가 SIGSEGV 11을 보고했다. UI 없는 CLI와 데모는 이 자식 상태를
종료 코드 139로 반환했다. 테스트 cleanup이 보낸 종료 신호와 구분했다.

동일 Ubuntu 소스의 심볼 포함 빌드에서 core를 확보했다.
realloc → xrealloc → _rl_isearch_dispatch → rl_search_history가 실행 중이었고,
전역 _rl_iscxt는 NULL이지만 호출자는 정리된 검색 context를 계속 참조했다.
후속 입력의 첫 문자도 해당 context에 남았다. 이는 Python 예외가 아니라
Readline 검색 상태를 정리한 뒤 다시 사용하는 native 오류다.

관련 함수는 lib/readline/input.c의 rl_getc, lib/readline/signals.c의
_rl_handle_signal, lib/readline/isearch.c, bashline.c의 bash_event_hook이다.
사용한 소스는 설치 패키지와 일치하는 Ubuntu bash 5.3-2ubuntu1
(GNU 5.3의 공식 패치 001–009 및 배포판 패치)다.

## 진단 수정과 대조

원본과 수정본은 같은 configure 옵션으로 별도 빌드했다. 시스템 설치나
기본 실행 경로를 바꾸지 않았다.

| 대조 | 결과 |
|---|---|
| 원본, 실제 pselect 반환을 1ms 늦춰 경합을 드러낸 native 정지 검사 | 6/6 정지 |
| v1: input.c의 일부 지점에서 application hook 호출 | 정지 검사 6/6 통과, 데모의 다른 SIGSEGV는 남음 |
| v2: deferred signal 처리 종료 지점에서 application hook 호출 | 정지 검사 6/6 실제 파일 결과까지 통과 |
| v2: native/basic/demo의 빠른 후속 입력 | 각 2회, 총 6회 실제 파일 결과까지 통과 |

v2는 신호 정리로 무효화된 검색 작업에 돌아가기 전에 Bash가 중단 처리를
마칠 수 있게 하고, pselect 직후 이미 도착한 신호가 있으면 read 전에 반복한다.
다만 **검사 직후 새 신호가 도착하는 창까지 원자적으로 막는 수정은 아니다.**
일반 Readline 사용자·signal trap·중첩 reader·모든 Bash 버전에 대한 완전한
해결이나 upstream 검토를 대체하지 않는다. v1은 실패한 중간 수정으로 보존했다.

현재 규칙의 “배포용 셸 소스 패치나 검증되지 않은 capability 활성화는 하지
않는다”에 따라 이 패치를 제품이나 설치된 셸에 자동 적용하지 않았다.
실사용 해결에는 Bash/Readline의 검토된 수정과 해당 버전의 재검증이 필요하다.
단순한 외부 셸 스크립트로 Readline 내부의 신호 처리 순서를 보장할 수는 없다.

## 이번 worktree의 변경

- tests/stream_state.py가 PTY EIO/EOF와 자식의 실제 종료 상태를 구분한다.
  waitid(WNOWAIT) 관찰은 기존 fixture의 reap 책임을 빼앗지 않는다.
- Bash/CLI fixture는 실패 시 cleanup 전 상태, foreground group, termios,
  최근 입출력을 남긴다. Bash 초기 생성 실패의 자원 정리도 보강했다.
- 실제 PTY 테스트로 정상 종료, SIGSEGV, 살아 있는 reader, slave만 닫힌
  살아 있는 프로세스 및 이미 reap한 상태를 구분했다.
- 호스트 harness는 파일 생성 직후의 오래된 prompt를 완료로 오인하지 않도록,
  실제 후속 완료 출력 다음의 prompt를 기다린다.

테스트 시간 제한을 늘리거나 production에서 시간·화면 문자열로 readiness를
추측하는 처리는 추가하지 않았다. tcsh/zsh, 기본형, P/H/B와 popup socket 계약,
사용자 signal trap 및 일반 stdin 경로는 변경하지 않았다.

## 회귀와 실제 터미널

최종 테스트 진단을 포함한 stream 회귀 163개와 Session 4개, 총 167개가 통과했다.
Bash 5.1.16·5.2.37은 각각 adapter/public CLI 40개, v2 진단 빌드도 같은
40개를 통과했다. 버전별 반복 실행은 고유 테스트 수와 구분한다.
Ruff check·format 검사도 통과했다. 실제 Windows Terminal/WSL에서는 다음을
관찰했다. 입력은 outer PTY에 넣은 scripted bytes이며 호스트가 직접 렌더링하고
질의에 응답했다.

| 실행 | 관찰 |
|---|---|
| 설치된 Bash 5.3.9 데모 | Ctrl+R → Ctrl+C+후속 입력에서 CLI 종료 139, termios 복원 |
| v2 별도 테스트 Bash 데모 | 같은 경계 5회 연속 파일 결과 성공, 팝업·continuation·heredoc·Vim·정상 종료 |
| v2 별도 테스트 Bash 기본형 | 같은 경계 5회 연속 파일 결과 성공, 추가 UI 없이 native 동작·정상 종료 |
| tcsh 6.24.13 데모 | 명령 편집 Ctrl+C 취소, 후속 파일·팝업·foreach·heredoc·Vim·정상 종료 |
| zsh 5.9 데모 | 명령 편집 Ctrl+C 취소, 후속 파일·팝업·continuation·heredoc·Vim·정상 종료 |

이 실행에서는 host resize를 새로 발생시키지 않았다. 픽셀 외관과 GUI 키보드
입력을 검증한 것으로 확대하지 않는다. 최초 호스트 probe의 Ctrl+R 시작 실패는
완료 파일 생성과 최종 prompt 사이의 테스트 관찰 경합이었다. 이를 보강한
host-original-observed에서 실제 종료를 확인했으며 두 기록을 모두 보존했다.

## 재현 증거

- .cache/interrupt-bash/manifest.json: 소스 경계, native 상태, 빌드 해시와 한계.
- .cache/interrupt-bash/readline-diagnostic-v2.patch: 테스트 빌드에만 적용한
  최소 진단 수정. [추적 가능한 사본](bash-readline-diagnostic.patch)도 둔다.
  v1은 같은 cache 디렉터리에 보존한다.
- .cache/interrupt-bash/test-binaries.tar: 심볼 포함 원본/v1/v2 테스트 바이너리.
- .cache/interrupt-matrix/probe.py: native/basic/demo 입력·파일·종료 대조.
- .cache/interrupt-matrix/evidence/debug-core/: backtrace와 검색 상태 관찰.
- .cache/interrupt/matrix-evidence.tar.gz: cleanup 전 상태와 전체 syscall trace,
  원본 및 중간 실패를 포함한 재현 증거.
- .cache/interrupt/: 최종 회귀 로그, 실제 Windows Terminal 기록과 소스 해시.

이 자료는 원인 분석 및 향후 dependency 수정 검증용이다. 검증된 배포용
Bash 패키지나 ish의 자동 패치 설치 기능이 아니다.
