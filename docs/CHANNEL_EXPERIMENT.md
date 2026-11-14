# tcsh 구문 채널과 stdin 분리 실험

이 실험은 **셸 소스를 수정하지 않고** 구문 입력을 FIFO로, 프로그램의 일반 stdin을
controlling PTY로 분리할 수 있는지 확인한다. 기존 tcsh adapter를 바꾸거나 PTK
continuation을 활성화한 구현은 아니다. Bash/zsh의 공통 semantic 경로와
tcsh/BSD csh/sh의 production 경로는 그대로다.

검증 대상은 WSL Linux의 tcsh 6.24.13이다. 실행 파일과 소스의 정확한 버전·해시는
[HANDOFF_LIMITS.md](HANDOFF_LIMITS.md)에 있다. 다른 tcsh 버전, BSD csh, dash에
같은 결과가 성립한다고 가정하지 않는다.

## 실험 구조와 권한의 범위

```text
테스트 controller ── 승인된 구문 바이트 ──> syntax FIFO ──> tcsh source
         │                                      ↑
         └── request/reply socket ──> test-only read gate

테스트 controller ── native 입력 바이트 ──> controlling PTY ──> 일반 stdin
```

하나의 interactive tcsh에서 `source syntax.fifo`를 실행한다. `source`는 동일한
셸의 구문 입력 원천을 바꾸며 일반 stdin은 PTY에 남긴다. 외부 프로그램을 별도
셸에서 실행하는 구조가 아니므로 해당 source에서 변경한 셸 상태를 유지할 수 있다.

- [tcsh_channel.py](../tests/tcsh_channel.py)는 stock tcsh의 실제 PTY와 FIFO를
  만든다. `start_source()`의 FIFO open 성공은 transport 관찰이며 입력 권한이 아니다.
  `write_syntax()`와 `write_stdin()`은 controller가 이미 선택한 목적지에 바이트를 쓴다.
  이 API가 입력 내용이나 셸 상태를 보고 목적지를 추측하지 않는다.
- [channel_gate.c](../tests/channel_gate.c)는 별도 임시 빌드한 preload 실험이다.
  원래 셸 프로세스가 등록 FIFO를 읽기 전에 정지하고 private socket에서 응답을
  기다린다. production preload, adapter 또는 배포용 셸에는 설치하지 않는다.
- [test_tcsh_channel_gate.py](../tests/test_tcsh_channel_gate.py)는 실제 PTK
  `PromptSession`의 key processor와 편집 반환값을 사용한다. 입력은 pipe input,
  화면 출력은 `DummyOutput`이다. **실제 ish CLI/session/dispatcher 연결 검증은 아니다.**

구문과 native stdin을 분리한 것은 입력 출처를 정한 것이다. 어느 시점의 키를 어느
채널에 보낼지 자동으로 결정하거나, raw 입력을 회수하는 문제가 함께 해결된 것은 아니다.
테스트 controller는 목적지를 명시적으로 선택한다.

## read gate의 실제 wire 계약

```text
Q <session token> <FIFO inode> <read serial> <capacity>\n
A <session token> <FIFO inode> <read serial> <length>\n
N\n    # 잘못된 응답; 같은 read는 계속 정지
G\n    # 해당 read의 응답 수락
```

이 이름들은 기존 C/R 이벤트와 구별한 실험 전용 값이다.

1. shim은 원래 PID에서만 동작하며 실제 FD의 device/inode를 등록 경로와 대조한다.
   fork 자식은 PID 검사에서 제외하고 exec 자식에는 이 실험의 preload 환경을
   전달하지 않는다. 숫자 FD만 같다는 이유로 요청을 만들지 않는다.
2. Q의 serial은 실제 read마다 증가한다. 현재 token/inode/serial과 길이 범위가
   일치하는 A만 수락한다. controller는 FIFO에 해당 chunk를 먼저 넣고 A를 보낸다.
3. G는 응답 수락 ACK다. 명령 실행 완료, 구문 완료 또는 실제 read 완료를 뜻하지
   않는다. 길이가 양수이면 그 뒤 제한된 크기로 원래 read를 호출한다.
4. 승인 한 번의 단위는 **read chunk**다. physical line, 논리 명령 또는 primary
   경계가 아니다. 긴 Unicode 줄이 여러 요청으로 나뉘어도 새 편집기 여러 개로
   취급하지 않고 이미 승인된 문자열의 나머지를 이어 보낸다.
5. 길이 0은 이번 source reader에 EOF를 반환한다. PTY의 VEOF와 다른 동작이며,
   셸 세션이나 프로그램의 stdin을 닫았다는 뜻도 아니다.
6. FIFO에 이미 넣은 chunk를 취소하려면 그 FIFO의 수명을 끝내야 한다. serial을
   바꾸는 것만으로 이전 바이트가 제거되지는 않는다. 새 channel identity와 기존
   입력 폐기/회수는 별도의 문제다.

현재 inode 검사는 **등록 FIFO에서 읽고 있음**을 확인한다. 등록된 파일을 동일
셸이 일반 stdin으로 다시 열거나 redirect하는 경우까지 구문 reader라고 증명하지는
않는다. 이번 실험은 해당 FIFO를 wrapper의 구문 전용 원천으로 사용하는 범위다.
배포 설계에서는 open/close/dup/reopen과 채널 수명, nested source를 추적하거나
그 재사용을 제한하는 별도 계약이 필요하다. read interception 일반에 parser 의미가
생긴 것으로 해석하지 않는다.

현재 shim은 등록 경로와 일치하지 않는 FD를 원래 read로 통과시킨다. 따라서 이전
source가 살아 있는 동안 등록 경로를 삭제·교체하면 기존 reader가 gate를 우회할 수
있다. 취소 테스트는 원래 PTY에서 복구 명령이 실행된 파일 결과로 source unwind를
확인한 **뒤에만** 이전 FIFO를 폐기한다. production에서는 이 순서와 revoked FD의
차단을 별도 수명 계약으로 집행해야 한다. 간단한 테스트용 숫자 decoder도 production
프로토콜의 모든 잘못된 frame 검증을 대신하지 않는다.

## 소스가 설명하는 동작 차이

아래 함수는 검증한 tcsh 6.24.13 소스의 문맥이며, shim에서 읽을 수 있는 공개
parser API라는 뜻은 아니다.

| 소스 경계 | FIFO/source에서 달라지는 의미 |
| --- | --- |
| `sh.c:srcunit/st_save/st_restore` | SHIN, intty, input arena, loop/eval 상태를 저장·교체·복원한다. FIFO는 isatty가 false다. 정상 EOF는 source에서 복귀하지만 `process(0)` 오류는 상위 error handler까지 unwind한다. |
| `sh.func.c:doforeach/dowhile/preread` | interactive loop의 본문 선행 수집은 intty 조건에 있다. FIFO source는 그 단계를 건너뛰며 아직 end를 입력하지 않았어도 첫 iteration의 본문을 실행할 수 있다. |
| `sh.lex.c:settell/bgetc/bseek` | FIFO는 seek할 수 없어 내부 input arena에서 loop 구문을 보존·재독해한다. 내부 버퍼 재독해에는 syscall과 새 Q가 없을 수 있다. |
| `sh.c:process` | precmd와 interactive history 저장은 intty/catch 조건을 사용한다. postcmd는 source 안의 실행에서도 호출될 수 있으므로 전체 stream의 완료 신호가 아니다. |
| `sh.func.c:doeval` | evalvec가 구문 원천이다. eval 내부 구문을 FIFO에서 다시 받아서는 안 된다. |
| `sh.dol.c:Dgetdol`, 자식 실행 | 일반 stdin 소비는 구문 원천과 별개다. `$<`, 외부 프로그램, Python `input()`을 PTK 편집 요청으로 바꾸지 않는다. |

원문: [source/process](https://sources.debian.org/src/tcsh/6.24.13-2.1/sh.c/),
[loop/eval](https://sources.debian.org/src/tcsh/6.24.13-2.1/sh.func.c/),
[input buffer](https://sources.debian.org/src/tcsh/6.24.13-2.1/sh.lex.c/),
[변수 입력](https://sources.debian.org/src/tcsh/6.24.13-2.1/sh.dol.c/).

따라서 source stream은 기존 interactive loop의 표시만 교체한 구현과 같지 않다.
변수·환경·alias·cwd를 유지할 수 있다는 사실과 preread/history/hook 의미까지
같다는 주장은 구별해야 한다. 향후 wrapper가 protocol emit 명령을 추가한다면
사용자의 `$status`도 그 emit 실행 전에 저장·복원하는 경로를 따로 검증해야 한다.
stock source의 상태 검증은 아직 작성하지 않은 wrapper의 보존을 증명하지 않는다.

## 현재 상위 계약과 연결할 때 필요한 구분

[handoff.py](../src/ish/shell/handoff.py)의 C는 실제 interactive syntax 요청이며
submission과 request를 모두 가진다. 이번 Q는 특정 source reader의 refill 요청이다.
Q의 serial을 submission ID로 바꾸거나 모든 refill을 PRIMARY로 보고해서는 안 된다.

| 사실 | 기존 계약을 재사용할 수 있는 부분 | 아직 필요한 보장 |
| --- | --- | --- |
| 구문 reader가 응답 전에 정지함 | 요청 수명·중복 거부·PTK 응답을 한 번만 소비하는 원칙 | 실제 submission과 read chunk의 관계, 채널 수명, 취소 unwind |
| FIFO 구문과 PTY stdin이 분리됨 | syntax 응답과 native 전송의 목적지를 분리 | Enter 직후 typeahead의 자동 목적지 선택 |
| 일반 stdin 실행 뒤 새 FIFO read에 도달함 | raw 전송을 중지하는 RECLAIM 상태 | writer drain, unread PTY 입력 회수, 그 완료와 새 syntax 요청의 correlation |
| source가 종료·오류·취소됨 | 확인된 primary의 context/input-return barrier | 외부 PTY parser가 재개되기 전에 실제로 멈춰 있다는 증명 |

하나의 source stream에서 다음 흐름이 필요하다.

```text
구문 요청 → PTK 응답 → native stdin 사용 → 새 구문 reader 정지
          → 부모 raw 전송 중지 및 회수 완료 → 다음 PTK 편집
```

이는 기존의 제출별 C 반복 후 마지막 R이라는 계약으로 모두 표현되지 않는다.
공통 확장 후보는 **RAW → RECLAIM 완료 → C**다. source 파일명이나 tcsh 함수명은
공통 계층에 올릴 필요가 없지만, 회수 거래와 다음 요청을 연결하는 의미는 공통
계약으로 정의해야 한다. 이번 실험에서는 production Handoff를 변경하지 않는다.

FIFO에 syntax를 전달한 직후 native 입력을 허용하는 정책을 택할 수는 있다.
그 허용은 분리된 stdin에 대한 권한이지 명령 실행 시작 관찰이 아니다. 실제
refill 요청이 올 때마다 남은 stdin을 회수해야 하는지, 같은 실행의 다음 `$<`를
위해 남겨야 하는지도 정책과 검증 대상이다. FIFO 분리 자체가 그 선택을 제공하지 않는다.

또한 refill은 top-level과 continuation을 구분하지 못한다. true if 본문, heredoc,
loop 본문, 명령 시작이 같은 channel을 사용한다. 이 범위의 편집 응답은 모두
`commands=False`와 동등하게 취급해야 한다. Python 도구나 ish 내부 명령의 dispatch는
독립적으로 확인된 UI 동작 또는 primary 권한을 통해서만 허용할 수 있다.

## 회수와 종료 경계의 남은 문제

source가 활성인 동안 늦은 PTY 바이트는 FIFO parser로 직접 들어가지 않는다.
하지만 그 바이트가 사라진 것은 아니다. 다음 `$<`가 소비하거나 source EOF 후
원래 PTY parser가 명령으로 실행할 수 있다. **분리와 회수는 다른 보장이다.**

안전한 복귀에는 다음을 실제로 연결해야 한다.

1. 원래 source reader 또는 복귀할 PTY parser를 정지시킨다.
2. 부모의 새 native 입력 전달을 중지하고 이미 시작한 PTY 쓰기를 정리한다.
3. 아직 소비되지 않은 native 입력의 출처를 보존하며 회수한다.
4. 같은 복귀 identity의 완료를 확인한 뒤 다음 편집기를 연다.

FIFO EOF, FD close 또는 source 뒤에 붙인 `echo done`만으로 이 네 단계를
대신하지 않는다. 구문 오류와 Ctrl+C는 뒤쪽 wrapper 명령을 건너뛸 수 있다.
명시적인 `exit`, `exec`, onintr, background reader, job control까지 고려한 복귀·종료
협력은 아직 없다. 이번 실험에서 완료되지 않은 보장으로 남긴다.

소스 수정 없이 확장할 후보도 있다. 정상 EOF는 gate가 read=0을 반환하기 전에
회수 완료를 기다리도록 할 수 있다. 오류·취소 경로의 `st_restore`는 현재 SHIN을
닫은 뒤 원래 SHIN과 intty를 복원하므로, 정확히 등록한 source FD의 close에 gate를
두는 방안도 검토할 수 있다. 이는 **소스 흐름에 근거한 후보**이며 구현·검증 결과가
아니다. 해당 FD의 수명 추적과 exec/error cleanup 등 우회 경로까지 검증해야 한다.
소스 패치가 유일한 해법이라고 단정하거나 close 관찰만으로 primary를 허가하지 않는다.

## 검증 결과

2026-11-14에 stock shell 비교 15개와 native gate/PTK 실험 9개가 통과했다.
후자는 native gate와 실제 PTK 편집 사례를 합친 수이며 서로 다른 9개씩이 아니다.
테스트 파일은 다음 세 층을 구분한다.

| 층 | 테스트 | 무엇을 확인하는가 |
| --- | --- | --- |
| stock shell 비교 | [test_tcsh_channel.py](../tests/test_tcsh_channel.py) | 동일 persistent tcsh의 interactive 입력과 FIFO source의 실제 소비자·파일·상태 차이 |
| native gate | [test_tcsh_channel_gate.py](../tests/test_tcsh_channel_gate.py) | 실제 reader 차단, wire 거부, chunk 전달, FIFO/PTY 분리와 남은 누출 |
| 실제 PTK 편집 | 같은 gate 테스트의 PTK 사례 | 실제 편집 키·paste·EOF의 반환값을 source에 전달한 결과. ish CLI ownership/dispatch는 범위 밖 |

확인한 실제 결과는 다음과 같다.

| 시나리오 | 관찰한 소비자와 파일 결과 |
| --- | --- |
| `$<`와 외부 foreground 프로그램 | syntax는 FIFO, 데이터는 PTY에서 읽었다. 공백과 Unicode가 결과 파일에 그대로 남았다. |
| 명령 치환 → foreach 본문 | 치환 프로그램이 PTY의 `first second`를 읽은 뒤 FIFO 본문을 실행했다. raw 파일은 `first second\n`, loop 파일은 `first\nsecond\n`이었다. |
| PTK 응답 → `$<` → 다음 PTK 응답 | 같은 foreach에서 PTY 데이터와 PTK 본문을 명시적으로 나눠 전달했고 결과를 확인했다. 자동 ownership 전환을 검증한 사례는 아니다. |
| nested while/foreach | FIFO는 end 제출 전에 첫 `A\n` 파일을 만들었다. interactive baseline은 end를 받은 후 실행했다. 최종 `A\nB\n` 결과는 같았다. |
| heredoc 및 PTK multiline paste | 공백·Unicode·`ish_exit`·`probe`가 파일 데이터로 남았다. 700개 한글 문자로 된 2,100-byte 줄은 여러 gate chunk로 나누어 정확히 전달했다. ish dispatcher는 이 실험에 참여하지 않는다. |
| source/eval 내부 stdin | 내부 구문 파일·eval 문자열을 다시 편집하지 않고 그 안의 `$<`가 PTY 데이터만 읽었다. |
| 셸 상태 | 동일 PID에서 변수·환경·alias·cwd가 유지됐고 `/bin/false` 다음 `$status`는 1이었다. source 복귀 뒤에도 상태가 유지됐다. |
| hook/history | 같은 4개 명령에서 interactive는 precmd/postcmd 각각 4회, source는 1회/5회였다. source 내부 명령은 interactive history에 없고 source 호출이 기록됐다. |
| 분할 write 및 CRLF | 1 byte씩 나눈 Unicode/backslash 구문도 정확했다. 반면 `echo value > crlf\r\n`은 FIFO에서 `crlf\r` 파일을, PTY에서 `crlf` 파일을 만들었다. newline 변환 정책이 같지 않다. |
| Python stdin | 실제 Python `input()`이 PTY의 데이터를 읽었고 stdin은 TTY였다. 자식에 이 실험의 LD_PRELOAD가 남지 않았다. |
| 잘못된 wire 응답 | foreign token, 잘못된 inode, 미래/오래된 serial, 다음 요청에 재사용한 이전 응답을 명시적인 N으로 거부했다. 바르게 승인한 두 명령은 파일에 한 번씩만 기록됐다. |
| 늦은 PTY 입력 | FIFO parser로 새지 않았지만 다음 `$<`가 그대로 소비했다. source EOF 후에는 원래 PTY parser가 그 바이트를 실제 명령으로 실행했다. |
| 제한된 조건의 회수 → PTK | Q로 구문 reader가 정지하고 테스트 controller가 유일한 writer이며 다른 reader/job이 없는 상태에서 unread PTY suffix를 회수했다. `echo 회수 > reclaimed`를 실제 PTK 편집기에 넣어 반환받고 FIFO로 전송해 정확한 `회수\n` 파일을 확인했다. |
| source 오류 | 닫히지 않은 quote 오류 뒤의 completion 명령은 실행되지 않았고 원래 PTY parser로 복귀했다. |
| EOF | native cat의 PTY Ctrl+D와 FIFO/source EOF는 서로 다른 소비자에 작용했다. PTK 빈 Ctrl+D를 source EOF로 바꾼 뒤에도 셸의 PTY stdin은 살아 있었다. |
| stock source 취소 | Ctrl+C와 FIFO close를 함께 사용하면 복귀한 뒤 새 FIFO source를 시작할 수 있었고 이전 변수도 유지됐다. |
| gate 승인 전 취소 | PTK Ctrl+C 뒤 VINTR/control close/FIFO close만으로 끝나지 않아 새 Q를 실제로 받았다. 새 요청에서 취소된 ACK를 N으로 거부하고 명시적인 길이 0 응답으로 source를 끝냈다. 복귀 파일과 새 FIFO source의 결과 파일은 정확했고, 취소한 명령 파일은 없었다. 새 source의 요청에서도 이전 ACK는 N으로 거부됐다. |

회수 실험은 child가 알려준 실제 PTY slave 경로를 사용하며 `/proc`을 조회하지
않는다. 이때 raw mode로 unread suffix를 읽은 뒤 원래 termios를 복원한다. 위의
제한된 조건은 테스트가 미리 확보한 사실이다. production의 동시에 진행 중인 writer,
다른 stdin reader, 원래 helper/session의 회수 거래까지 검증한 것은 아니다.

**실패와 한계:** 초기 stock FIFO 대기 실험에서는 Ctrl+C만 보낸 후 기대한 복귀가
deadline 안에 일어나지 않았다. 통과한 회귀 사례는 FIFO close도 수행한다. 따라서
기존 native interrupt 교환을 그대로 재사용할 수 있다고 보지 않는다. EOF 후 늦은
PTY 바이트가 명령으로 실행되는 사례와 end 전 실행·hook/history·CRLF 차이는 실패를
숨긴 skip이 아니라, 해당 차이를 기대값으로 고정해 통과한 재현 테스트다.
gate에서도 control 연결 종료만으로 source가 끝나지 않는다는 사실을 별도로
관찰했다. 재시도 Q와 명시적인 EOF 응답을 포함한 경로만 통과한 것으로 보고한다.

**미검증:** 실제 ish CLI의 자동 typeahead 분배 및 dispatch, production raw reclaim,
G 이후 부분 전송 중 취소와 source 취소 뒤 지연 응답의 모든 경로, arbitrary
hook/status wrapper, channel 재개방·FD 수명, background reader/job control,
다른 셸·버전은 검증하지 않았다. 취소 실험은 G를 보내기 전 staged chunk에 한정한다.

테스트의 prompt 문자열과 deadline은 관찰 및 실패 판정용이다. production readiness
판정 규칙으로 사용하지 않는다. 새 shim은 임시 디렉터리에 빌드하며 stock 셸 소스와
실행 파일을 교체하지 않는다.

```sh
PYTHONPATH=src:tests python -B -m unittest \
  test_tcsh_channel test_tcsh_channel_gate -v
```

### 기존 live 경로의 회귀 확인

기존 tcsh guard/secondary/interrupt와 Bash/zsh 실제 PTK 모듈을 함께 실행했다.
33개 테스트의 최초 실행(500.446초)에서 guard 6개, interrupt 3개, Bash PTK 9개,
zsh PTK 12개와 tcsh 긴 continuation/Unicode 사례는 통과했다. 실패 4건은
tcsh completed-paste 1건 및 custom prompt의 3개 subtest가 시작 제한 30초를
넘긴 경우였다. 한 실패 대기 중 ish는 tcsh 자식을 만들기 전 WSL의
`p9_client_rpc` I/O 대기 상태였다. 이는 환경 지연의 관찰이며 모든 실패의 원인을
확정하는 근거로 쓰지 않는다.

파일과 입력 처리 제한은 그대로 두고 별도 runner에서 시작 제한만 90초로 늘렸다.
completed-paste는 12.812초에 통과했다. custom prompt 재실행(127.705초)에서
최초 미검증이던 marked-ansi/cursor/empty를 포함한 5개 설정은 통과했으나,
array 설정은 마지막 `echo "$#prompt2" > prompt-values` 제출 뒤 6초 내 결과 파일이
생기지 않아 실패했다. array는 최초 실행에서는 통과했다. 이 실패는 시작 단계가
아니며 원인은 미확정이다. 따라서 **기존 회귀 전체를 완전 통과로 보고하지 않는다.**

이번 변경은 실험 테스트·shim·문서와 ignore 예외에 한정한다. `src`의 live 구현,
기존 tcsh 보호 처리와 Bash/zsh capability는 변경하지 않았다. 위 미해결 결과를
이 실험의 지원 활성화나 기존 경로 수정의 근거로 사용하지 않는다.

## 다음 단계의 선택

이 방식은 source 패치를 필수로 전제하지 않는 유효한 native 협력 후보다. 다만
기존 interactive semantics를 그대로 유지하는 구현으로 채택하려면 위 의미 차이와
회수 경계를 먼저 해결해야 한다. 현재 실험만으로 tcsh capability를 켜지 않는다.

별도 선택지는 PTK가 전체 multiline block을 편집하고 사용자가 명시적으로 실행하는
모드다. 이 경우 block 내부는 항상 literal shell syntax로 취급하고 shell에게 실행을
맡길 수 있다. 이는 사용자가 논의한 PTK 자체 UI와 잘 맞지만, Enter마다 interactive
shell이 본문을 요구하는 기존 동작과 같은 계약은 아니다. 어느 정도의 의미 변경을
허용할지 먼저 정해야 한다.

`shell -n`을 완전한 block 판별 API로 사용하지도 않는다. 앞선 실측에서 tcsh
`-f -n`은 end 없는 foreach와 종료자 없는 heredoc에도 0을 반환했다. 별도 셸의
parse 결과가 현재 세션의 alias·상태를 반영하는 것도 아니다. 명시적 실행 모드와
자동 완성도 판별 기능은 분리해서 설계해야 한다.

다음 최소 실험은 source 수명과 취소 경로를 통제한 상태에서 **native 입력 회수 후
새 syntax request를 여는 실제 session 경로**를 검증하는 것이다. 그 후 Enter 직후
typeahead, PTK dispatch 금지, delayed/cancelled request, source 종료·오류 복귀를
실제 ish CLI로 확인해야 한다. 이 작업 전에는 6~7단계나 모든 셸의 PTK continuation
목표를 완료로 표시하지 않는다.
