추가 수정: [tcsh 실행 중 훅 변경 복구](D:/Programs/ish/dev/HOOK_RECOVERY.md)와 [csh/tcsh foreach 선입력·다중 줄 표시·히스토리 오염](D:/Programs/ish/dev/FOREACH_TYPEAHEAD.md). tcsh의 편집 모드는 `postcmd`에서 복원하며, 기본 프롬프트 경계에서 변경된 사용자 훅을 보존하고 연동을 다시 연결합니다. 일반 셸과의 비교를 포함한 98개 테스트가 통과했습니다. 아래 내용은 P2 수정 당시 기록입니다.

**P2 수정 결과 — 2026-09-10**

원래 리뷰의 P2(8–19번)를 현재 코드와 대조했습니다. 이미 수정된 부분은 유지했고, 19번은 사용자 요청에 따라 제외했습니다.

| 원래 항목 | 처리 결과 |
|---|---|
| 8. 입출력 FD | `_display()`의 stdout 대상은 이미 수정되어 유지했습니다. 남아 있던 `Prompt.input_fd/output_fd` 전달을 셸과 Python worker 모두에 반영했습니다. UI용 스트림을 닫아도 호출자가 넘긴 FD는 닫지 않습니다. |
| 9. hook 계약 | `run(hook)`로 호출하며, `pre → 명령 실행 → post → 실패 시 fallback` 순서를 적용했습니다. hook은 인수 없는 import 가능한 함수입니다. 실패 로그에 원래 예외를 남깁니다. |
| 10. 셸 지원 | 인수 튜플 오타는 이미 수정되어 있었습니다. 나머지는 셸별 어댑터와 통합 스크립트로 정리했습니다. Bash·zsh·BSD csh·tcsh·Ubuntu sh(dash)를 실제 PTY와 CLI에서 검증했습니다. |
| 11. alias | Bash/zsh의 인용된 alias 출력을 평가 없이 파싱합니다. 공백, 작은따옴표, 줄바꿈, `=`가 있는 값을 보존합니다. csh/tcsh는 이름과 본문의 탭 구분을 사용합니다. |
| 12. 종료 코드·상태 동기화 | Bash의 기존 문자열/배열 `PROMPT_COMMAND`보다 앞에서 사용자 명령의 종료 코드를 저장합니다. 기존 훅의 순서·내용을 유지하고 첫 훅에도 원래 상태를 전달합니다. FIFO와 PTY에 같은 prompt ID를 보내며 해당 상태가 도착한 후 post/fallback을 실행합니다. |
| 13. 메모리 제한 | history, 미완성 줄, 마지막 출력에 보관 바이트 예산을 적용했습니다. Sequencer의 프롬프트·미완성 제어열·명령 echo 마스킹에도 상한을 적용했습니다. 출력 큐는 상한과 PTY 읽기 일시 중단/재개로 느린 소비자에 대응합니다. |
| 14. resize | 원래 prompt_toolkit callback을 호출하고 종료 시 복원하는 처리가 이미 반영되어 있어 추가 수정하지 않았습니다. |
| 15. 자동완성 | 인용·이스케이프·명령 구분자·명령 치환의 문맥을 읽습니다. 공백과 특수문자가 있는 경로를 셸에 맞게 인용하며, 대소문자를 무시할 때 입력과 후보를 모두 정규화합니다. |
| 16. 내부 도구 인수 | 첫 토큰으로 도구를 선택하고 인용 규칙으로 나머지 인수를 전달합니다. `tool "two words" '' a\ b`의 인수 경계를 보존합니다. |
| 17. 라이브러리 버전·설치 | `SpecifierSet`으로 `~=`, 복합 범위, 와일드카드 등을 검사합니다. 잘못된 조건을 통과시키지 않습니다. pip에는 argv 리스트와 `check=True`를 전달하고, import 이름과 배포 패키지 이름의 버전 조회를 구분합니다. 실제 pip 설치는 하지 않았습니다. |
| 18. 언어 파일 | `base_path` 오타는 이미 수정되어 유지했습니다. JSON 실패를 로그에 남기고, 내장 기본값 → 영어 파일 → 선택한 언어 파일 순서로 병합합니다. 언어 전환 시 이전 번역이 남지 않습니다. |
| 19. 테스트 탐색의 파일 생성 | **요청에 따라 건너뛰었습니다. `tests/test.py`는 수정하지 않았습니다.** 검증에서는 `test_*.py` 패턴으로 이 생성 도구를 제외합니다. |

**검증 결과**

WSL Ubuntu / Python 3.14.4에서 **65개 테스트가 모두 통과했습니다(건너뛴 테스트 없음)**. 기존 ANSI·P1 테스트도 포함됩니다.

- Bash의 문자열/배열 `PROMPT_COMMAND`, alias, 원래 프롬프트, `false`/`true` 상태와 hook 순서.
- zsh의 기존 `precmd` 및 실패하는 hook 배열, tcsh의 기존 `precmd`와 원래 `$status` 보존.
- Bash·zsh·BSD csh·tcsh·dash에서 별도 stdin/stdout, 공백·작은따옴표·느낌표가 있는 통합 스크립트 경로, 상태·환경 갱신.
- 다섯 셸의 실제 `python -m ish.main <shell>` UI 시작, 64KiB 이상 환경변수, 명령 출력, `ish_exit`와 FIFO 정리.
- 긴 줄·미완성 제어열·Unicode 분할, 쓰기 큐 상한, 느린 출력 장치에서 worker의 마지막 데이터 보존.
- 자동완성 인용, 내부 도구 인수, FD 소유권, 언어 fallback, 버전 조건과 pip argv 생성.

zsh 5.9-8ubuntu3, BSD csh 20240808-4, tcsh 6.24.13-2.1은 Ubuntu 패키지를 임시 디렉터리에 추출해 검증했습니다. 시스템에 설치하거나 사용자 기본 셸·rc 파일을 변경하지 않았습니다. zsh 테스트는 추출된 패키지의 모듈 경로를 격리된 `.zshrc`에 지정했습니다.

설치된 셸로 다시 검증하려면:

```bash
cd /mnt/d/Programs/ish
source ~/.venvs/ish/bin/activate
PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p 'test_*.py' -v
```

설치되지 않은 셸의 테스트는 skip됩니다. 별도 바이너리를 쓰려면 `ISH_TEST_ZSH`, `ISH_TEST_CSH`, `ISH_TEST_TCSH` 환경변수에 절대 경로를 지정할 수 있습니다.

**동작 범위와 보관 정책**

- Bash·zsh·tcsh는 프롬프트 훅에서 상태를 수집합니다. BSD csh에는 해당 훅이 없으므로 기본 프롬프트 도착 후 상태 갱신 스크립트를 별도로 실행합니다. dash에도 같은 방식과 POSIX `.` 호출을 사용합니다. csh가 tcsh를 가리키는 심볼릭 링크이면 tcsh 어댑터를 선택합니다. C shell 통합 경로에 줄바꿈이 있으면 명확한 오류를 냅니다.
- zsh는 기존 훅의 실패 시 나머지 사용자 훅을 중단하면서 ish의 상태 전송은 마칩니다. 참고: [zsh hook 함수 규칙](https://zsh.sourceforge.io/Doc/Release/Functions.html#Hook-Functions).
- ScrollBack 기본 10MiB는 history/미완성 줄 5MiB와 마지막 출력 5MiB로 나눕니다. 오래된 바이트를 버리며 `history_truncated`, `last_output_truncated`로 알립니다. 이는 저장 payload 예산이며 Python 객체 오버헤드나 전체 프로세스 RSS 상한은 아닙니다. UTF-8 경계가 잘리면 대체 문자로 표시합니다.
- 프롬프트는 최대 64KiB를 보관하고 초과하면 `[prompt truncated]`를 표시합니다. 긴 미완성 제어열은 끝까지 소비하면서 본문을 버립니다. 쓰기 큐 기본 상한은 4MiB이고, 2MiB부터 PTY 읽기를 멈춰 1MiB 이하에서 재개합니다. 과도한 typeahead는 1MiB에서 오류로 보고하며, 복구용 입력 기록은 최근 64KiB만 보관합니다.
- 내부 도구는 리터럴 인수를 받습니다. 파이프·리다이렉션·변수/명령 치환이 필요한 입력은 셸에 전달합니다. 자동완성은 확장을 실행하지 않으며, 해석할 수 없는 동적 단어나 단어 중간의 커서는 임의로 바꾸지 않습니다.
- Python hook과 내부 도구의 import/pickle 조건은 아래 P1 수정 결과와 같습니다. 복잡한 사용자 프롬프트 플러그인 및 모든 셸 문법 조합을 검증한 것은 아닙니다.

아래 P1 결과와 원래 리뷰는 당시 기록으로 보존했습니다. 기존 리뷰의 구조·성능 제안 전체를 이번 작업 범위에 포함하지는 않았습니다.

---

**P1 수정 결과 — 2026-09-09**

현재 코드와 대조해 이미 반영된 수정은 유지하고, 남은 P1을 수정했습니다. 아래 원래 리뷰는 수정 전 기록이며 파일 줄 번호도 당시 기준입니다.

| 항목 | 처리 결과 |
|---|---|
| P1-1 부분 쓰기·무한 재시도 | `FDWriter` 큐와 쓰기 준비 콜백으로 변경했습니다. 부분 쓰기, EINTR/EAGAIN, 영구 오류, 리다이렉션 파일 출력을 검증했습니다. C forwarding도 poll 후 재시도하고 터미널 설정을 복구합니다. |
| P1-2 초기화 이벤트 루프 차단 | reader를 유지하고 비동기 handshake·timeout·자식 종료·I/O 실패를 함께 기다립니다. 큰 환경변수가 FIFO를 채워도 초기화가 진행됩니다. |
| P1-3 FIFO 프레임 유실 | 완성된 여러 프레임을 소비하고 미완성 꼬리를 보존합니다. 새 ISH2 형식은 base64 본문과 NUL 구분 환경변수를 사용해 값 안의 제어문자·줄바꿈을 보존합니다. 기존 프레임도 읽습니다. |
| P1-4 시작 실패 시 자원 누수 | 첫 자원 취득부터 ExitStack으로 관리합니다. 부분 초기화 실패·spawn 실패·init 실패·취소 시 FD/FIFO/터미널/FD 플래그를 정리하고 자식을 회수합니다. 프롬프트 작업은 셸 초기화 후 시작합니다. |
| P1-5 Tab | `select_first`, `current_completion`은 이미 수정되어 있어 소스 수정 없이 비동기 후보 생성과 선택 적용 테스트로 확인했습니다. |
| P1-6 플러그인 | `sep`, `sys.modules`, `registry.has`는 이미 수정되어 있어 유지했습니다. 남은 예외 로그의 `self.version`, 모듈 해제 경로, 재로드를 막는 로딩 상태 정리를 수정했습니다. |
| P1-7 Python worker | 모듈 수준 spawn worker와 Unix FD 전달(send_handle/recv_handle)로 변경했습니다. PTY 입력·출력, 제어 터미널, 큰 출력의 마지막 데이터, 종료 코드, 취소, 시작 실패를 검증했습니다. |

검증: WSL Python 3.14.4에서 기존 ANSI 테스트를 포함한 **36개 테스트 통과**. 실제 PTY에서 64KiB 이상 환경변수와 제어문자를 포함한 Bash 초기화, 출력 명령, `ish_exit`, 임시 FIFO 정리가 통과했습니다. C helper는 `-Wall -Wextra -Werror`로 컴파일해 가득 찬 FIFO 테스트를 수행했습니다.

```bash
cd /mnt/d/Programs/ish
source ~/.venvs/ish/bin/activate
PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p 'test_*.py' -v
```

이 테스트 패턴은 기존 생성 도구 `tests/test.py`를 제외합니다. 해당 도구의 import 부작용은 P2 기록으로 남아 있습니다.

Python 도구는 import 가능한 모듈의 함수와 pickle 가능한 인수를 사용해야 합니다. 지역 함수와 lambda는 자원을 열기 전에 설명을 포함한 TypeError로 거절합니다. 전역 multiprocessing 시작 방식은 변경하지 않습니다. 새 FIFO 송신 형식은 Linux의 `base64 -w0`, `env -0` 명령을 사용합니다. 쉘 실행 시 통합 스크립트가 다시 생성됩니다.

자원·출력 경로를 정리하면서 stdout 대상과 원래 resize callback 보존도 함께 반영했습니다. 나머지 P2 항목 전체를 해결한 것은 아니며, zsh/csh/tcsh의 실제 실행은 이번 검증 대상에 포함하지 않았습니다.

---

프로젝트 전체 리뷰 — 2026-09-09

현재 Bash에서 기본 프롬프트 표시, 명령 실행, 정상 종료는 가능하지만, I/O 오류·여러 메시지 수신·초기화 실패·Tab·플러그인·Python 도구 실행에서 기능 결함이 남아 있습니다. P1은 우선 수정할 기능 장애, P2는 특정 조건에서 발생하는 오류 또는 안정성 문제입니다. 이번 리뷰에서는 애플리케이션 소스를 변경하지 않았습니다.

**검증 범위와 한계**

- 소스 Python 파일 22개, 테스트와 생성되는 셸/C 코드를 검토했습니다. 소스 문법 검사는 통과했습니다.
- 설치된 WSL Ubuntu / Python 3.14.4 / prompt_toolkit 3.0.53에서 독립 재현을 수행했습니다.
- 기존 테스트 13개는 모두 통과했습니다. 모두 ShellANSI 관련 테스트이며 전체 셸 동작을 보증하지 않습니다.
- 별도 PTY, 임시 HOME/XDG 디렉터리, 최소 환경변수, 명시적인 터미널 크기로 실행한 Bash에서 `printf 'REVIEW_%s\n' OK`의 `REVIEW_OK` 출력과 `ish_exit`의 종료 코드 0을 확인했습니다.
- 처음에는 상속 환경과 준비 대기를 충분히 통제하지 않은 PTY 검사에서 초기화 대기가 발생했습니다. 이 결과만으로 추가 결함을 확정하지 않았습니다. 환경과 터미널 크기를 고정하고 프롬프트 준비를 확인한 검사는 통과했습니다.
- zsh/csh/tcsh 실제 바이너리는 설치되어 있지 않아 해당 셸의 종단 간 동작은 검증하지 않았습니다. 사용자 설정을 수정하거나 실제 pip 설치를 실행하지 않았습니다.

**우선 수정할 결함**

1. **[P1] 쓰기 실패가 이벤트 루프를 점유하고, 부분 쓰기는 데이터를 잃습니다.** [base.py:396](D:/Programs/ish/src/ish/shell/base.py:396)

   `_write()`는 `os.write()`의 반환 바이트 수를 무시합니다. 모의 장치가 6바이트 중 2바이트를 받아도 한 번 호출하고 성공처럼 반환했습니다. `EBADF`도 즉시 반복하며 재시도 횟수나 대기가 없습니다. 닫힌 FD에서는 루프가 끝나지 않아 다른 작업이 실행되지 않습니다. `_send()`와 C forwarding 코드도 부분 쓰기 처리가 필요합니다. 미전송 바이트를 큐에 남기고 `add_writer()` 등으로 쓰기 가능 시 재개하며, `EAGAIN`/`EINTR`/영구 오류를 구분해야 합니다.

2. **[P1] 초기화 대기가 asyncio 전체를 막습니다.** [base.py:531](D:/Programs/ish/src/ish/shell/base.py:531)

   `_init()` 내부의 `select.select()`는 동기 호출이고 루프에 제어를 돌려주는 `await`가 없습니다. 30ms 초기화 대기 동안 5ms 타이머가 실행되지 않는 것을 재현했습니다. `_ish_update()`의 환경·alias 출력이 FIFO 용량을 넘으면, FIFO를 읽어야 하는 콜백도 실행되지 않아 셸이 프롬프트에 도달하지 못할 수 있습니다. 기존 reader를 유지하고 Future/Event와 비동기 timeout으로 초기화 완료를 기다리는 방식이 적합합니다.

3. **[P1] FIFO 메시지가 합쳐져 도착하면 뒤의 메시지가 사라집니다.** [base.py:409](D:/Programs/ish/src/ish/shell/base.py:409)

   첫 EOT까지만 처리한 뒤 `shell_pipe_buffer.clear()`로 나머지까지 버립니다. exitcode 1과 2 프레임을 한 번에 보내자 1만 전달되고 잔여 버퍼도 비었습니다. 완성된 프레임을 반복 소비하고 미완성 꼬리는 보존해야 합니다. 환경변수 값에 줄바꿈이나 RS/EOT가 포함될 때도 현재 구분 방식이 깨지므로, 프레이밍과 값 직렬화를 명시적으로 정해야 합니다.

4. **[P1] 초기화 실패 시 자원이 정리되지 않습니다.** [base.py:667](D:/Programs/ish/src/ish/shell/base.py:667)

   자원 생성과 `_spawn()`/`_init()`이 정리용 `try/finally` 밖에 있습니다. `_spawn()` 실패를 주입하자 FD 5개와 FIFO 파일 2개가 남았습니다. 바깥 `run()`은 termios만 복구합니다. 자원을 얻은 즉시 정리 작업을 등록하고 전체 초기화를 `AsyncExitStack` 또는 하나의 수명 관리 블록으로 감싸야 합니다. stdin의 `O_NONBLOCK` 원래 플래그 복구와 종료한 자식 프로세스의 `wait()`도 포함해야 합니다.

5. **[P1] Tab 자동완성이 두 경로에서 모두 예외를 냅니다.** [prompt.py:532](D:/Programs/ish/src/ish/ui/prompt.py:532)

   자동완성 상태가 없으면 `elect_first` 때문에 TypeError, 있으면 `current_compltion` 때문에 AttributeError가 발생했습니다. 각각 `select_first`, `current_completion`으로 수정해야 합니다. 비동기로 생성되는 completion state를 즉시 다시 읽어도 아직 없을 수 있으므로, 후보 생성 시작과 후보 선택을 구분하고 실제 키 입력 테스트를 추가해야 합니다.

6. **[P1] 플러그인을 추가하면 로드 경로가 막힙니다.** [manager.py:319](D:/Programs/ish/src/ish/plugin/manager.py:319)

   임시 플러그인의 `_resolve()`는 `registry.hsa`, `_load_plugin()`은 `sys.module`, `_parse_requirement()`는 `if set in req` 때문에 각각 실패했습니다. `has`, `sys.modules`, `sep`가 의도된 이름입니다. 또한 `load()`의 예외 보고 경로는 정의되지 않은 `self.version`을 참조하므로 원래 오류도 가릴 수 있습니다. 의존성 없는 최소 플러그인부터 실제 등록·조회·해제까지 통합 검증해야 합니다.

7. **[P1] 현재 Python 3.14에서 Python 도구 프로세스를 시작할 수 없습니다.** [pytool.py:106](D:/Programs/ish/src/ish/app/pytool.py:106)

   `ProcessHandler.run()` 안의 지역 함수 `wrapper`가 multiprocessing의 대상입니다. WSL에서 직접 호출하자 `PicklingError: Can't pickle local object ...wrapper`가 발생했습니다. worker 진입점을 모듈 수준으로 옮기고, 호출할 함수와 PTY FD를 자식에게 전달하는 방식을 명시해야 합니다. 지역 함수를 옮기는 것만으로 FD 상속 문제가 모두 해결되지는 않습니다. `fork` 강제 선택도 `asyncio.to_thread()`와의 조합을 검토한 뒤 결정해야 합니다.

**추가 기능·안정성 문제**

8. **[P2] 출력이 stdout 대신 stdin으로 전달됩니다.** [base.py:430](D:/Programs/ish/src/ish/shell/base.py:430)

   `_display()`가 `_write(self.stdin_fd, data)`를 호출합니다. stdin=10/stdout=11인 모의 세션에서 실제 대상은 10이었습니다. 일반 터미널에서는 두 FD가 같은 장치를 가리켜 숨겨지지만, 별도 입출력 FD나 리다이렉션에서는 잘못 동작합니다. Prompt의 `input_fd`/`output_fd` 역시 InteractiveShell 생성에 전달되지 않아 UI와 셸의 입출력 대상이 달라질 수 있습니다.

9. **[P2] pre/post/fallback hook의 호출 계약이 맞지 않습니다.** [prompt.py:630](D:/Programs/ish/src/ish/ui/prompt.py:630)

   `ProcessHandler.run(func, ...)`에 `run(fd, hook)`를 전달합니다. 첫 인수가 함수가 아닌 정수 9인 것을 재현했습니다. multiprocessing 문제를 해결해도 hook 실행이 실패합니다. [base.py:655](D:/Programs/ish/src/ish/shell/base.py:655)에서는 명령 전에 post hook, 명령 후에 pre hook을 호출하므로 이름의 의미와 순서도 반대입니다. 함수 시그니처와 hook 실행 시점을 함께 정리해야 합니다.

10. **[P2] Bash 외 셸 지원이 실행 인수와 통합 스크립트에서 깨집니다.** [base.py:215](D:/Programs/ish/src/ish/shell/base.py:215)

    zsh/csh/tcsh의 `('-i')`는 튜플이 아니라 문자열이므로 실제 인수 목록이 `['-', 'i']`가 됩니다. `('-i',)`가 필요합니다. Ubuntu `/bin/sh`에는 `source`가 없어 실제 호출이 종료 코드 127로 실패했습니다. `.sh` 통합 코드도 `[[ ... ]]`, `PROMPT_COMMAND` 등 Bash 동작에 의존합니다. 셸별 실행 인수·source 명령·hook·지원 기능을 어댑터로 묶고, 검증 전에는 Bash만 지원한다고 명시하는 편이 정확합니다. 통합 스크립트와 FIFO 경로의 shell quoting도 필요합니다.

11. **[P2] Bash alias가 실제 이름으로 파싱되지 않습니다.** [prompt.py:159](D:/Programs/ish/src/ish/ui/prompt.py:159)

    `alias ll='ls -l'`을 공백으로 분리하면 키가 `alias`가 됩니다. 두 alias를 입력하자 `{'alias': "ll='ls -l'"}`만 남았습니다. Bash 출력 형식을 전용 파서로 처리하거나 이름과 값을 별도 필드로 직렬화해야 합니다. 단순 공백 분리는 따옴표와 공백이 있는 값도 보존하지 못합니다.

12. **[P2] 기존 PROMPT_COMMAND가 있으면 종료 코드가 왜곡됩니다.** [integration.py:128](D:/Programs/ish/src/ish/shell/integration.py:128)

    기존 hook 뒤에 `_ish_precmd`를 붙이므로 그 안에서 읽는 `$?`는 사용자 명령의 상태가 아닐 수 있습니다. 기존 hook이 `:`일 때 `false`를 실행해도 보고된 상태가 0인 것을 재현했습니다. hook 체인의 시작에서 사용자 명령의 상태를 보존해야 합니다. 배열형 PROMPT_COMMAND도 별도로 처리해야 하며, FIFO 상태 갱신과 PTY 프롬프트 도착 사이의 순서도 명령 식별자 등으로 맞출 필요가 있습니다.

13. **[P2] 출력량 제한이 실제 메모리 사용량을 제한하지 않습니다.** [base.py:98](D:/Programs/ish/src/ish/shell/base.py:98), [sequencer.py:122](D:/Programs/ish/src/ish/shell/sequencer.py:122)

    deque의 maxlen 자동 퇴출은 `_current_bytes`에서 차감되지 않습니다. 실제 4바이트인데 10바이트로 집계되는 사례를 재현했습니다. `_partial_line`, `_last_output`은 max_bytes와 무관하게 커지며, 단일 긴 줄도 상한을 초과합니다. Sequencer의 미완성 프롬프트 버퍼 역시 100,000바이트를 보관했습니다. 이전에 추가한 ShellANSI의 65,536자 제한은 그보다 앞선 수신 버퍼를 제한하지 못합니다. 전체 보관 바이트 예산과 잘린 출력 정책이 필요합니다. `_between()`의 매 바이트 전체 복사도 긴 입력에서 누적 비용이 커집니다.

14. **[P2] 창 크기 변경 시 prompt_toolkit의 화면 재계산을 건너뜁니다.** [base.py:688](D:/Programs/ish/src/ish/shell/base.py:688)

    `app._on_resize`를 PTY 크기 전달만 하는 lambda로 교체합니다. 설치된 원래 메서드는 renderer erase, 커서 위치 재요청, redraw를 수행합니다. 원래 처리를 유지하면서 PTY 크기를 갱신해야 합니다. 이 항목은 코드·라이브러리 구현을 대조한 결과이며 실제 창 드래그의 시각적 검증은 하지 않았습니다.

15. **[P2] 자동완성이 입력의 셸 의미를 보존하지 않습니다.** [completer.py:27](D:/Programs/ish/src/ish/ui/completer.py:27)

    파일 `hello world`의 완성 결과가 이스케이프 없는 `lo world `라서 `cat hel`이 `cat hello world`로 바뀝니다. 단일 경로가 두 인수가 됩니다. 따옴표 상태에 맞춰 escape 또는 quote해야 합니다. `ignore_case=True`도 후보만 소문자로 만들기 때문에 `L` 입력에 `ls`가 제안되지 않았습니다. 입력도 같은 기준으로 정규화하고 `&&`, 명령 치환, 공백 경로 등 문맥 테스트가 필요합니다.

16. **[P2] 인수가 있는 내부 도구가 셸로 넘어갑니다.** [prompt.py:768](D:/Programs/ish/src/ish/ui/prompt.py:768)

    전체 입력 문자열을 internal_tools의 키와 비교합니다. `tool`을 등록해도 `tool argument`는 도구로 분기하지 않고 셸 명령으로 반환됐습니다. 첫 토큰으로 도구를 선택하고 나머지는 인수로 파싱해야 합니다. 이때 `split()` 대신 셸 인용 규칙을 반영하는 파서가 필요합니다.

17. **[P2] 라이브러리 설치 명령과 버전 검사가 잘못 해석될 수 있습니다.** [manager.py:153](D:/Programs/ish/src/ish/plugin/manager.py:153), [manager.py:214](D:/Programs/ish/src/ish/plugin/manager.py:214)

    `~=1.2` 조건에 2.0을 허용하는 것을 재현했습니다. `packaging.specifiers.SpecifierSet` 등 이미 사용 중인 packaging의 표준 기능을 사용하는 편이 정확합니다. 설치 명령을 `' '.join(cmd), shell=True`로 실행하므로 `demo>=1.2`의 `>`가 셸 리다이렉션으로 해석됩니다. argv 리스트 그대로 `subprocess.run(cmd, check=True)`에 전달해야 합니다. 검사에서는 명령 생성만 확인했고 실제 pip 설치는 실행하지 않았습니다.

18. **[P2] 언어 파일 로딩이 항상 실패합니다.** [i18n.py:54](D:/Programs/ish/src/ish/lang/i18n.py:54)

    `self.bash_path`가 정의되지 않아 AttributeError가 발생합니다. `load_messages()`가 이를 숨겨 기본 메시지만 보입니다. `base_path`로 수정하고 JSON 로딩 실패를 진단 가능하게 해야 합니다. 언어 파일을 일부 키만 제공하는 용도로 지원한다면 기본 메시지와 병합하는 정책도 필요합니다.

19. **[P2] 테스트 탐색이 작업 디렉터리에 파일을 생성합니다.** [tests/test.py:45](D:/Programs/ish/tests/test.py:45)

    모듈 최상위에서 `main()`을 호출합니다. 임시 디렉터리에서 `unittest discover`를 실행하자 테스트 통과와 함께 `stdlib.py`가 생성됐습니다. 기존 파일이 있다면 덮어쓸 수 있습니다. 생성 도구를 dev로 이동하고 `if __name__ == '__main__':`로 감싸야 합니다. 생성 경로도 명시적인 인수로 받는 편이 좋습니다.

**구조·성능 개선**

- [main.py:3](D:/Programs/ish/src/ish/main.py:3)의 stdlib 일괄 import 필요성을 재검토하세요. 이 WSL에서 단발 측정한 import 비용은 약 0.821초, 최대 RSS 증가분은 약 4,488KiB였습니다. 시스템·캐시 상태에 따른 값이며 일반화한 벤치마크는 아닙니다. 필요한 모듈만 지연 import하고 [stdlib.py:2](D:/Programs/ish/src/ish/stdlib.py:2)의 전역 warning 억제를 제거하는 편이 진단과 시작 비용에 유리합니다.
- [prompt.py:615](D:/Programs/ish/src/ish/ui/prompt.py:615)는 매 프롬프트마다 PATH 전체를 스캔합니다. PATH와 디렉터리 변경 여부를 기준으로 캐시하고, 이전 비동기 스캔 결과가 최신 cwd를 덮지 않도록 세대 번호 또는 취소를 적용하세요. 이것이 regex import 여부보다 우선 검토할 저사양 비용입니다.
- `ShellANSI`와 `Sequencer`의 책임을 분명히 하세요. 전자는 완성된 프롬프트의 서식·메타데이터 변환, 후자는 PTY 바이트 스트림과 메시지 경계 처리입니다. 토큰 경계 규칙은 공유할 수 있지만 스트리밍 상태와 화면 출력 정책까지 섞지 않는 것이 좋습니다. [sequencer.py:88](D:/Programs/ish/src/ish/shell/sequencer.py:88)의 decoder는 초기화되지 않아 항상 예외 fallback으로 진행합니다. 원시 바이트 통과가 목적이면 해당 decode 경로를 제거하고, 텍스트 디코딩이 필요하면 증분 decoder를 올바르게 초기화해야 합니다.
- [ShellContext](D:/Programs/ish/src/ish/shell/context.py:14)의 모든 미정의 속성에 None을 반환하는 동작은 메서드 오타까지 숨깁니다. 상태 조회는 `get()` 같은 명시적 API로 제공하고 미정의 속성에는 AttributeError를 사용하는 편이 디버깅에 유리합니다.
- [패키지 초기화](D:/Programs/ish/src/ish/__init__.py:4)에서 `__version__`은 값 없이 annotation만 선언되어 있고 `__dir__`에는 `sotred` 오타가 있습니다. 버전 값과 패키지 메타데이터를 한 곳에서 관리하세요.
- README, pyproject.toml, 지원 Python/셸 범위, 의존성 버전 범위, 실행 entry point가 필요합니다. 현재는 PYTHONPATH와 수동 설치에 의존합니다. 복사한 PromptSession layout과 ShellANSI 내부 파서 override는 prompt_toolkit 업그레이드 회귀 테스트 대상으로 명시하세요.
- 임시 FIFO는 공유 `/tmp`의 개별 경로 대신 권한 0700의 전용 임시 디렉터리 아래에 만들고 FIFO 권한을 명시하세요. 생성·정리와 경로 quoting을 한 컴포넌트에서 관리할 수 있습니다.
- C helper는 시작할 때마다 같은 경로에 컴파일합니다. 변경된 소스/플랫폼에 대해서만 빌드하고 임시 결과를 원자적으로 교체하면 시작 비용과 동시 세션 충돌을 줄일 수 있습니다.

**권장 작업 순서**

1. Tab, 플러그인 오타, hook/도구 호출 계약을 회귀 테스트와 함께 수정합니다.
2. 부분 쓰기, 비동기 초기화, FIFO 프레이밍, 시작 실패 정리를 먼저 안정화합니다.
3. Python worker 전달 방식, 셸별 어댑터, alias/종료 코드 동기화를 구현합니다.
4. 긴 출력·개행 없는 출력·Unicode 분할·Ctrl+C/Ctrl+D·창 크기 변경·초기화 실패를 PTY 통합 테스트에 추가합니다.
5. 측정 후 PATH 캐시, stdlib 지연 로딩, C helper 캐시를 적용하고 배포 구성을 추가합니다.
