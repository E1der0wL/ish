# 셸 문법과 실행 정책

`src/ish/shell/adapters.py`에서 셸 분류와 연동 설정을 관리합니다.

- `SYNTAXES`: 문법별 인용 함수, 변수 대입 형식, 종료 코드 조회·복원 표현식, 스크립트 로딩 명령, alias 파서, 구문 강조 lexer.
- `ADAPTERS`: 셸 이름별 문법 계열(`family`), 실행 인수, 연동 스크립트, 실행 정책과 개별 예외.
- `SHELL_CATEGORIES`: 위 두 딕셔너리에서 모듈 로딩 시 생성하는 문법별 셸 목록. 셸을 추가할 때 별도로 편집하지 않습니다.
- `SHELL_ALIASES` / `RESOLVED_SHELL_ALIASES`: `dash`, `bsd-csh` 및 tcsh를 가리키는 csh 심볼릭 링크의 정규화.

현재 `posix`에는 bash·zsh·sh, `csh`에는 csh·tcsh가 속합니다. 여기서 `posix`는 ish가 사용하는 공통 문법을 뜻하며 zsh 전체가 POSIX 표준을 준수한다는 의미는 아닙니다.

zsh는 `-i`로 실행해 시스템·사용자 시작 설정을 읽습니다. 전역 설정을 생략하는 `-d`는 사용하지 않으며, 기본 ZLE는 연동 스크립트에서 끕니다. Bash의 `PROMPT_COMMAND`는 5.1 이상에서 배열을 사용하고, 이전 버전에서는 기존 문자열 훅과 종료 상태를 보존하는 함수 하나를 등록합니다. 버전별 처리는 `scripts.py`의 Bash 템플릿에 모았습니다. EL8의 Bash 4.4.20·zsh 5.5.1 재현 결과는 [EL8_COMPATIBILITY.md](D:/Programs/ish/dev/EL8_COMPATIBILITY.md)에 있습니다.

`ShellBehavior`는 문법과 별도로 선택합니다. 현재 지원하는 모든 셸은 보조 입력을 셸에 맡기고 여러 줄을 한 번에 보냅니다. C shell 계열은 보조 프롬프트와 같은 줄에 남은 선입력 표시도 유지합니다. 기본 입력 편집·자동완성은 prompt-toolkit이 담당합니다. 같은 문법을 쓰는 셸이라도 필요한 정책을 개별적으로 설정할 수 있습니다. 새 정책에서도 셸에 넘긴 입력을 `tcflush()`로 지우거나 복사본으로 재생하지 않아야 합니다.

`preserve_output_line`은 `OutputLine`으로 일반 텍스트·SGR 색상 코드만 프롬프트 접두부에 보존합니다. 화면 전환·커서 이동·삭제·단독 CR 등으로 화면을 조작한 명령의 출력은 접두부로 복사하지 않습니다. 원본 PTY 출력은 그대로 전달합니다. `Sequencer.output_callback`은 프롬프트 콜백보다 앞선 출력부터 순서대로 알려주므로, 같은 읽기에 섞인 프롬프트 뒤의 출력이 접두부로 들어가지 않습니다. [TUI_RETURN.md](D:/Programs/ish/dev/TUI_RETURN.md)에 재현과 검증을 기록했습니다.

`idle_recovery` 설정과 프로세스 유휴 상태를 조회하는 `_recover` 루프는 제거했습니다. 기본 프롬프트를 확인한 시점에만 context 동기화·복구를 수행합니다. `ShellSyntax.preserve_status()`는 자동 재연결 명령 앞뒤로 셸의 종료 상태를 보존합니다. 다른 문법을 추가할 때 `restore_status` 표현식도 지정해야 합니다. 모든 마커·훅이 사라지면 원시 입력을 유지하며 사용자가 셸 프롬프트에서 `ish_recover`로 재연결할 수 있습니다. 자세한 경계와 제약은 [STREAMING_REVIEW.md](D:/Programs/ish/dev/STREAMING_REVIEW.md)의 개선 결과에 정리했습니다.

| 설정 | 역할 |
| --- | --- |
| `source_command`, `source_args` | 기본 로딩 명령을 덮어쓰거나, 위치 인수를 받지 못하는 셸에 FIFO 경로를 변수로 전달 |
| `refresh_script`, `capture_refresh_status` | 명시적 상태 갱신 스크립트와 source 전에 종료 코드를 저장할지 선택 |
| `unhooked_prompt` | tcsh처럼 제어 문자가 caret 표기로 출력될 때 사용할 추가 프롬프트 마커 |
| `builtins_command`, `builtins` | 내장 명령 조회 명령 또는 정적 목록 |

`ShellAdapter.source()`, `refresh_command()`, `configure_sequencer()`가 실제 명령 생성과 시퀀서 등록을 담당합니다. `base.py`는 이 메서드와 실행 정책을 사용하며 셸 이름으로 분기하지 않습니다. alias 파서와 UI의 자동완성 인용·구문 강조·내장 명령 조회도 같은 설정을 참조합니다.

## 셸 추가

기존 셸과 연동 방식이 같은 셸은 `dataclasses.replace`로 복사한 설정을 추가할 수 있습니다. 예를 들어 tcsh와 호환되는 셸은 다음과 같습니다. 새 파일명 상수는 `constants.py`에 선언하고 import합니다.

```python
from dataclasses import replace

# ADAPTERS 선언 뒤, SHELL_CATEGORIES 생성 전에 추가
ADAPTERS["new-shell"] = replace(
    ADAPTERS["tcsh"],
    name="new-shell",
    args=("-i",),
    script=NEW_SHELL_INTEGRATION_SCRIPT,
)
```

기존 등록 항목처럼 `ADAPTERS` 딕셔너리 안에 `ShellAdapter(...)`로 직접 선언해도 됩니다.

문법 자체가 다르면 `SYNTAXES`에 `ShellSyntax(...)`를 추가하고 새 adapter의 `family`에 그 키를 지정합니다. 분류만 추가하면 실제 셸 지원이 완성되지는 않습니다. 해당 셸의 스크립트를 `scripts.py`의 `make_scripts()`에 구현하고 실행 인수·훅·종료 코드·선입력 동작을 실제 셸로 검증해야 합니다. 지원하지 않는 셸은 임의의 계열로 추정하지 않고 오류를 냅니다.

## 신호와 파일 이름

`src/ish/shell/constants.py`의 `PROMPT_ID_PREFIX`, `BEFORE_PROMPT`, `AFTER_PROMPT`, `CARET_BEFORE_PROMPT` 등의 상수를 송수신 양쪽에서 사용합니다. 실제 실행에서는 `SessionSignals.create()`로 만든 세션 식별자를 `scope()`로 삽입하고, 같은 객체를 `make_scripts()`와 `configure_sequencer()`에 전달합니다. 셸 템플릿에 필요한 printf 이스케이프도 이 바이트 상수에서 생성합니다. 빈 식별자는 기존 상수 기반 테스트·호환 API용이며 실제 `InteractiveShell`은 무작위 식별자를 사용합니다.

`CSH_UPDATE_SCRIPT`, `POSIX_UPDATE_SCRIPT`를 비롯한 연동 스크립트 이름과 전달 바이너리·FIFO 이름도 이 파일에서 관리합니다. 기존 `shellIntegraion.*` 파일명은 호환성을 위해 그대로 유지했습니다. `integration.py`에서 기존 프롬프트 상수를 import하던 코드도 계속 동작합니다.

실제 런타임의 스크립트와 전달 바이너리는 각 실행의 전용 임시 디렉터리에 설치하고 종료 시 정리합니다. 전역 XDG 경로에 다른 세션의 토큰이 덮어써지지 않습니다. `install_scripts()`와 `build_binary()`를 인수 없이 호출하는 기존 API는 유지합니다.

회귀 테스트는 `tests/test_shell_adapters.py` 및 기존 실제 셸 테스트에 있습니다. 테스트 탐색 시 기존 `tests/test.py`를 제외하는 `python -m unittest discover -s tests -p 'test_*.py' -v`를 사용합니다.
