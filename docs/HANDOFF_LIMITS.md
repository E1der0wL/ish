# tcsh / BSD csh / dash 입력 경계 검토

이 문서는 **native 셸 관찰 결과**와 향후 필요한 프로토콜을 구분한다.
여기에 기록한 테스트는 tcsh/csh/dash의 PTK continuation 지원을 증명하지 않는다.
이 세 adapter의 continuation capability는 활성화하지 않았고 기존 native 경로를 유지한다.

## 실행 환경과 소스

2026-11-14의 WSL Linux에서 다음 실행 파일을 실제 controlling PTY로 실행했다.

| 대상 | 실제 버전 | 실행 파일 SHA256 |
|---|---|---|
| tcsh | 6.24.13, Debian package `6.24.13-2.1` | `3b660a10244cbf564a5cae251ee829cbbb52ac02ccbc11d48e4e19590cdf5f14` |
| BSD csh | 별도 추출한 Debian `csh_20240808-4_amd64.deb` | `4c5df3c93aa42cc1912ebf962d701a175148639174a114913f27b648ab97c42c` |
| dash | `0.5.12-12ubuntu3` | `c626229526bb58ec2d0f585f3c3ae1412e6f973b4353385042d11c38d8426917` |

`/usr/bin/csh`는 이 환경에서 tcsh의 symlink이므로 BSD csh 검증에 사용하지 않았다.
기존 main 폴더의 별도 BSD csh binary는 읽기 전용으로 실행했다. 배포용 셸 교체나
production 소스 패치는 하지 않았다.

분석에 사용한 원본은 다음과 같다. dash의 Ubuntu 배포 패치 전체와 모든 버전의
차이는 검증 범위에 포함하지 않는다.

tcsh의 `6.24.13-2.1` Debian patch series도 대조했다. 입력 관련 소스의 해당 변경은
`sh.c`의 GCC common-symbol 선언 조정이며 위에서 분석한 reader 분기는 바꾸지 않는다.
BSD csh의 `20240808-4` patch series에서는 `lex.c`/`dol.c`/`func.c`의 해당 입력 분기를
바꾸는 패치가 없음을 확인했다.

| 소스 | 원본 아카이브 SHA256 |
|---|---|
| [tcsh 6.24.13](https://deb.debian.org/debian/pool/main/t/tcsh/tcsh_6.24.13.orig.tar.gz) | `9258812157f037c8ec25c7b5e0167fb9efbd5a3fa1b87cf74fa6d757ad0e6a31` |
| [BSD csh 20240808](https://deb.debian.org/debian/pool/main/c/csh/csh_20240808.orig.tar.xz) | `df916baa73c264516177c6667cc0a061f6eb9743f862b625f17067d74a3f4d1c` |
| [dash 0.5.12](https://deb.debian.org/debian/pool/main/d/dash/dash_0.5.12.orig.tar.gz) | `6a474ac46e8b0b32916c4c60df694c82058d3297d8b385b74508030ca4a8f28a` |

## tcsh 입력 경계 지도

아래 조건은 소스 내부에서 확인할 수 있는 문맥이다. 부모 ish가 read syscall이나
프롬프트 문자열만 보고 같은 문맥을 확보했다는 뜻은 아니다.

| 경로 | 관련 함수 | 구문 입력과 일반 stdin을 구별하는 근거 |
|---|---|---|
| primary 명령 | `sh.c:process`, `sh.lex.c:lex/readc/bgetc` | interactive `process`의 lexer이며, 실제 TTY refill은 `intty`, `SHIN`, `fseekp/feobp`와 현재 입력 원천을 함께 확인해야 한다. |
| backslash continuation | `sh.lex.c:word/getC1/readc/bgetc` | lexer가 같은 명령의 다음 문자를 요청한다. 실제 syscall 전에는 이미 저장된 문자·alias·eval 입력을 먼저 반환할 수 있다. |
| interactive foreach / while | `sh.func.c:doforeach/dowhile/preread/search/getword` | loop preread가 구문을 수집한다. `search`는 TTY 버퍼가 소진된 경우에만 prompt2를 출력한다. 이미 저장된 loop 본문 재독해는 새 사용자 요청이 아니다. |
| false if / switch 탐색 | `sh.func.c:doif/doswitch/search/getword` | 분기/종료 위치를 찾기 위한 구문 읽기다. source 파일 또는 loop 버퍼에서 같은 함수가 실행될 수 있다. |
| true if 본문 | `sh.func.c:doif`, `sh.c:process` | `if (1) then` 뒤에 primary prompt가 다시 출력되는 것을 관찰했다. 표시용 prompt 종류는 enclosing conditional이나 ish dispatch 정책을 완전히 표현하지 않는다. |
| heredoc | `sh.dol.c:heredoc`, `sh.lex.c:readc/bgetc` | `inheredoc` 상태에서 구문 데이터 줄을 읽는다. 인용되지 않은 본문의 확장은 별도의 실행/일반 stdin을 발생시킬 수 있으므로 읽기 진입 문맥을 유지해야 한다. |
| `$<` | `sh.dol.c:Dgetdol` | `OLDSTD`에서 `force_read(..., 1)`로 일반 stdin을 소비한다. lexer의 `bgetc` TTY refill과 다른 경로다. FD 번호만으로는 이 의미가 보장되지 않는다. |
| 명령 치환 | `sh.glob.c:dobackp`, `sh.sem.c:execute`, 자식 실행 경로 | 치환 프로그램이 먼저 일반 stdin을 읽고, 원래 foreach가 나중에 구문 수집으로 돌아갈 수 있다. |
| source | `sh.c:dosource/srcunit/st_save/process/st_restore` | `SHIN`·`intty`·입력 버퍼를 교체한다. 파일의 구문은 사용자 편집 요청이 아니며, 그 파일의 `$<`는 계속 일반 stdin을 읽을 수 있다. |
| eval | `sh.func.c:doeval`, `sh.lex.c:readc` | `evalvec/evalp` 문자열이 구문 원천이다. eval 내부 `$<`는 여전히 일반 stdin이다. |
| 외부 foreground 프로그램 | `sh.sem.c:execute`, 자식의 stdin | 프로그램 소유 입력이다. PTK 명령 편집 요청으로 바꾸면 안 된다. |

소스의 핵심 위치는 [tcsh lexer](https://sources.debian.org/src/tcsh/6.24.13-2.1/sh.lex.c/),
[구문 실행 함수](https://sources.debian.org/src/tcsh/6.24.13-2.1/sh.func.c/),
[변수 확장과 heredoc](https://sources.debian.org/src/tcsh/6.24.13-2.1/sh.dol.c/),
[process/source 수명](https://sources.debian.org/src/tcsh/6.24.13-2.1/sh.c/)이다.

## 하나의 제출 안에서 raw → syntax가 필요한 재현

다음 파일을 둔다.

```sh
# read.sh
printf '__ISH_OBS_RAW__\n' >&2
IFS= read -r answer
printf '%s\n' "$answer" > raw-input
printf '%s\n' "$answer"
```

tcsh와 BSD csh에서 다음 순서로 입력한다.

```csh
foreach value (`/bin/sh read.sh`)
first second
echo $value >> loop-output
end
```

첫 줄 제출 뒤 `read.sh`가 일반 stdin의 `first second`를 소비한다. 그 프로그램이
끝나고 나서야 foreach 본문 입력을 받는다. 최종 `raw-input`은 `first second\n`,
`loop-output`은 `first\nsecond\n`이다. tcsh의 `foreach value ( $< )`도 같은 전환을
보였다. 여기서 둘째 줄을 PTK의 명령 편집이나 도구 dispatch로 보내면 소비자가 달라진다.

현재 공통 계약의 `BARRIER → C → reply → BARRIER` 반복과
`R → RECLAIM → P`만으로 이 전체 흐름을 활성화해서는 안 된다. 필요한 추가 보장은
**native 실행 도중의 reclaim 완료 후 새 syntax request로 전환하는 `R → RECLAIM → C`**다.
직접 `R → C`를 허용하는 것으로 해결되지 않는다. 같은 제출 안의 각 요청과 회수 거래에
서로 다른 identity가 필요하다.

## 동기화와 identity에 대한 답

1. **구문 요청 증명:** 현재 helper는 증명하지 못한다. 후보는 `bgetc`의 실제 TTY
   refill 직전이며, alias/eval/file/이미 버퍼에 있는 입력을 배제하는 셸 문맥이 필요하다.
   primary와 본문을 같은 편집 UI로 다루더라도 dispatch 허용 여부는 별도로 제공해야 한다.
2. **일반 stdin 구별:** `$<`의 `Dgetdol/OLDSTD`와 외부 자식의 stdin을 별도 native
   경로로 유지해야 한다. process group, FD 번호, read 호출, postcmd만으로 결정하지 않는다.
3. **편집 중 읽기 중지:** 해당 구문 reader가 명시적으로 멈춰 있는 상태에서 요청을
   발행하고, 정확히 일치하는 응답을 받을 때만 한 줄을 소비해야 한다. prompt2 출력에는
   이 동기화나 요청 identity가 없다. 출력 마커 관찰만으로는 권한을 부여하지 않는다.
4. **이미 들어간 입력:** 부모의 새 입력을 먼저 보류하고 pending PTY writer를 정리한
   뒤 커널의 unread suffix를 회수해야 한다. 셸 내부 버퍼의 loop/source 구문과 아직
   소비하지 않은 typeahead는 구분해야 한다. 현재 helper는 셸의 private buffer를
   반환하거나 그 출처를 증명할 API가 없다. 기존 tcsh read-ahead 방어는 유지한다.
5. **wire identity:** 후보 프로토콜에는 session nonce, 실제 submission, 그 안의 단조
   증가 request, reclaim 거래 identity가 필요하다. 현재 tcsh postcmd/prompt2는 이
   correlation을 제공하지 않는다. 로컬 모델의 ID를 wire 보장으로 보고하지 않는다.
6. **취소·지연·중복:** 중지된 reader가 취소 시 해당 identity를 폐기하고, 새 요청은
   다른 identity를 사용해야 한다. 지연된 ACK는 현재 reader를 풀지 않아야 한다.
   이 교환은 tcsh/csh/dash에 아직 구현·실증되지 않았다.
7. **raw → primary:** 기존 tcsh primary helper의 입력 회수 동작은 유지한다. 그
   보장을 임의의 syntax 진입점에도 있다고 가정할 수 없다. native 관찰 테스트에서는
   완료된 명령의 마커 뒤로 늦게 보낸 텍스트가 다음 parser에서 명령으로 실행됐다.
8. **같은 제출에서 교대:** 위 foreach 치환과 `$<` 재현이 요구한다. 새 syntax 요청
   전에 raw reader와 writer의 중지·회수 완료를 증명하는 공통 계약 보완이 필요하다.

## 기존 helper/shim의 범위와 필요한 native 협력

현재 `guard.py`의 preload는 원래 셸 프로세스와 원래 TTY에 한정해 FIONREAD를
조정하여 tcsh editor의 read-ahead를 막는다. 입력을 읽거나 새로운 의미를 만들지
않으며 parser 문맥, request identity, continuation의 차단/재개 기능을 제공하지 않는다.
`read()`를 가로채도 그것만으로 구문과 일반 stdin의 의미를 얻을 수 없다.

확장 종류는 구분해야 한다.

- **관찰:** 호출 종류, 실제 입력 원천, 버퍼 소진 여부를 별도 테스트 빌드에서 기록할
  수 있다. 이것만으로 PTK를 활성화하지 않는다.
- **차단/재개:** 셸 reader가 새 입력을 소비하기 전에 멈추고, 부모와 회수·응답을
  교환해야 한다. native 입력으로 돌아갈 때도 별도 release가 필요하다.
- **셸 문맥 협력:** 구문 reader 진입, 일반 stdin 실행, 내부 버퍼 재독해, 취소 unwind를
  셸이 알려줘야 한다. 설치된 tcsh의 dynamic exports에는 이 parser API가 없다.

최소 source 협력 후보는 tcsh `bgetc`의 TTY refill, `Dgetdol`의 native stdin 진입,
`process/execute`의 실행 및 primary 복귀, `pintr`/error unwind의 요청 폐기다. 해당
후보는 설계이며 구현 완료 보장이 아니다. 내부 buffer를 외부로 반환할지, 처음부터
승인된 입력 이상의 read-ahead를 금지할지도 프로토콜의 일부로 검증해야 한다.
production 소스를 패치하거나 셸 binary를 교체하는 작업은 이번 변경에 포함하지 않는다.

## BSD csh와 dash

**BSD csh:** `func.c:search`는 추가 구문에 `? `를 출력할 수 있지만,
`dol.c:heredoc`은 prompt를 출력하지 않고 `readc(1)`을 반복한다. 실제 heredoc 파일은
공백·Unicode·`ish_exit`·도구 이름을 그대로 보존했고, 완료된 전체 출력에는 `? `가
없었다. prompt를 통신 시그널로 바꾸는 것만으로 모든 입력 요청을 얻을 수 없다.
또한 tcsh와 같은 foreground stdin → foreach syntax 교대가 관찰됐다.
[BSD csh heredoc 소스](https://sources.debian.org/src/csh/20240808-4/dol.c/).

**dash:** `parser.c:parsecmd/setprompt`는 명령과 추가 구문 prompt를 구분하지만,
`input.c:preadfd/preadbuffer`에는 별도 parser buffer가 있다. `read` builtin은
`miscbltin.c:readcmd`에서 stdin을 직접 읽고, `main.c:cmdloop`는 parse 결과를
`evaltree`로 넘긴다. PS2에 signal을 넣는 것만으로 실행 입력 허용과 read-ahead
회수까지 보장되는 것은 아니다. 기존 helper에 검증된 execution grant가 없으므로
이번에는 native continuation을 유지한다. 이는 모든 가능한 native 확장이 불가능하다는
증명이 아니라 현재 확보한 지원 근거의 한계다.
[dash parser](https://sources.debian.org/src/dash/0.5.12-12/src/parser.c/),
[dash input](https://sources.debian.org/src/dash/0.5.12-12/src/input.c/).

## 검증한 범위와 다음 최소 범위

`tests/test_shell_handoff_boundaries.py`의 12개 테스트가 통과했다. 테스트-only
문자열과 deadline은 관찰/실패 판정에만 사용한다. production readiness에 사용하지 않는다.
기존 `test_tcsh_guard`, `test_tcsh_secondary`, `test_tcsh_interrupt`의 12개 테스트도
통과했다. preload의 격리 실험과 실제 ish CLI의 native continuation·취소 검증을
포함하며, 기존 native 경로 유지에 대한 회귀 검증이다.

- tcsh: blank·구문 오류·backslash, foreach/while 중첩, false if/switch와 true if,
  heredoc, `$<`, 명령 치환, source/eval, 외부 stdin, Ctrl+C, heredoc Ctrl+D.
- tcsh/BSD csh: 명령 치환 raw → foreach syntax 교대와 source의 파일 구문/native stdin 분리.
- tcsh/BSD csh/dash: 실제 파일의 공백·Unicode·도구 이름 보존, 외부 프로그램 stdin,
  짧은 명령 완료 후 늦은 입력의 다음 parser 소비.
- typeahead/여러 줄 전송: loop·heredoc 본문을 한 번에 보낸 뒤 최종 파일을 확인했다.
  native 셸 테스트의 newline batch이며 PTK bracketed paste ownership 검증은 아니다.

```sh
ISH_TEST_CSH=/path/to/bsd-csh PYTHONPATH=src:tests \
  python -B -m unittest test_shell_handoff_boundaries -v
```

tcsh/csh/dash의 새 wire 요청·응답, 지연·중복·취소 ACK, native 내부 buffer 회수,
새 PTK continuation 및 그 안의 Python 도구 dispatch 금지는 **아직 실증하지 않았다**.
native heredoc 파일에 도구 이름이 남았다는 사실은 PTK dispatcher 검증을 대신하지 않는다.

현재 바로 활성화할 수 있는 최소 범위는 기존에 검증한 primary 편집뿐이다.
셸 문법을 Python에서 추측하지 않고 특정 continuation만 고르려면 해당 셸이 실제로
요청하는 시점의 문맥을 먼저 제공해야 한다. 이후 별도 [채널 분리 실험](CHANNEL_EXPERIMENT.md)에서
수정하지 않은 tcsh의 source FIFO 읽기를 테스트용 shim으로 차단하고, 요청 identity와
제한된 조건의 PTY 회수를 검증했다. 이는 TTY parser 문맥을 알아낸 구현이 아니라
구문 원천 자체를 분리한 실험이며, interactive 실행 시점·hook·history·취소 의미가
달라졌다. 기존 live 경로는 유지한다. source 수명과 오류·취소 복귀를 포함한 검증 전에는
6~7단계의 live PTK continuation과 모든 지원 셸 목표를 완료로 표시하지 않는다.
