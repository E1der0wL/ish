# ish

Linux 셸과 PTY로 통신하면서 prompt-toolkit의 편집·자동완성을 제공하는 명령어 UI입니다.
Bash, zsh, BSD csh, tcsh, dash를 지원합니다. Windows에서는 WSL에서 실행합니다.

## 개발 환경

Python **3.12.14**와 uv를 사용합니다. `.venv`는 WSL에서 생성하는 Linux 가상환경입니다.
Windows Python과 같은 가상환경을 공유하지 마세요.

```sh
cd /mnt/d/Programs/ish
export PATH="$HOME/.local/bin:$PATH"
uv sync --locked
uv run ish tcsh
```

이 환경의 uv 실행 파일은 `~/.local/bin/uv`에 설치되어 있습니다.
새 WSL 터미널에서 `uv`를 찾지 못하면 위의 PATH 설정을 먼저 실행합니다.
uv는 `.python-version`에 지정한 Python을 선택하고 `uv.lock`의 의존성을 설치합니다.
개발용 Ruff와 테스트의 psutil도 기본 동기화에 포함됩니다.
`uv run`을 사용하면 가상환경을 따로 활성화할 필요가 없습니다.
CLI 시작 비용을 줄이기 위해 uv 동기화 시 의존성 바이트코드를 사전 컴파일합니다.
WSL의 `/mnt/d`에 가상환경을 두면 파일 접근 때문에 첫 실행이 수 초 걸릴 수 있습니다.

실행 호스트에는 사용할 셸과 GNU coreutils(`base64 -w0`, `env -0`)가 필요합니다.
소스 실행에는 GCC도 필요합니다. Nuitka 배포본은 미리 빌드한 보조 프로그램을 포함합니다.
셸 통합 스크립트, 보조 프로그램, FIFO는 `config.CACHE_DIR/session-...`에 생성하며,
이 경로에서 실행이 허용되어야 합니다. 기본 위치는 `~/ish/.cache`입니다.

## Linux distribution

Build with Python 3.12.14 on the oldest Linux/glibc release you intend to support:

```sh
uv sync --locked --group build
uv run --locked --group build python tools/build_nuitka.py --mode standalone
uv run --locked --group build python tools/build_nuitka.py --mode onefile
```

The builder prints the release directory under `dist/nuitka/`. Test the executable
in a real terminal, or run the automated PTY checks against it:

```sh
uv run --locked python tools/smoke_distribution.py /absolute/path/to/ish
```

Onefile extracts into a private directory under
`~/ish/.cache/nuitka/<build-id>/launch-...` instead of `/tmp`, and cleans up on exit.
Its files stay available for the full process lifetime. Shell sessions use separate
`config.CACHE_DIR/session-...` directories and remove only their own files on exit.
See [distribution details](docs/DISTRIBUTION.md) for cache lifetime, plugin dependency
installation, target compatibility, and the scope of automated verification.
The [local validation report](docs/DISTRIBUTION_VALIDATION.md) records the final
artifact, checksums, actual test results, and the RHEL 8 compatibility limitation.

## 코드 검사

```sh
uv run ruff check .
uv run ruff format --check .
uv run python -B -m unittest discover -s tests -p 'test_*.py' -v
```

`tests/test.py`는 import 시 파일을 생성하는 기존 도구이므로 검사·테스트에서 제외합니다.
PTY 테스트는 초기 import·보조 프로그램 빌드를 위해 최초 프롬프트에 최대 30초를
허용합니다. 명령 응답과 TUI 복귀에는 각 테스트의 기존 제한 시간을 적용합니다.
설치되지 않은 셸의 테스트는 건너뛸 수 있습니다. Ubuntu의 `csh`가 tcsh 심볼릭 링크이면
BSD csh 검증에는 실제 `bsd-csh` 바이너리를 `ISH_TEST_CSH`로 지정해야 합니다.

```sh
ISH_TEST_CSH=/usr/bin/bsd-csh uv run python -B -m unittest discover -s tests -p 'test_*.py' -v
```

## 구조

- `src/ish/shell`: 셸 어댑터, 통합 스크립트, FIFO 프로토콜, PTY 입출력과 프롬프트 경계.
- `src/ish/ui`: prompt-toolkit 편집기, ANSI 프롬프트 표현, 자동완성.
- `src/ish/parser`: CLI 옵션, 셸 출력·명령 인수·자동완성 문맥 파싱.
- `src/ish/app`: 별도 프로세스에서 실행하는 Python 도구와 PTY 연결.
- `src/ish/plugin`: 플러그인 등록·의존성 확인·로딩.
- `src/ish/lang`: 번역 파일과 기본 메시지.

기본 프롬프트에서는 ish가 편집을 담당하고, 명령 실행 중과 보조 프롬프트에서는
셸이 입력을 읽습니다. 프롬프트 신호와 상태 FIFO가 모두 동기화된 뒤 UI로 돌아옵니다.
셸 훅과 프롬프트 마커를 모두 제거한 경우 원시 셸 프롬프트에서 `ish_recover`로 복구합니다.

## Prompt extension API

Use `set_tool`, `set_key`, and `set_float` in `.ishrc.py` to register, replace,
or remove extensions. These replace the former `add_*` and `delete_*` methods;
update existing rc files and plugins to use the new names.

```python
from prompt_toolkit.widgets import Label

prompt.set_tool("say", function=print)
prompt.set_tool("say", function=None)
prompt.set_tool("report", plugin_name="reports", function_name="run")


def insert_example(event):
    """Insert example text at the current cursor position."""
    event.current_buffer.insert_text("example")


prompt.set_key("c-x", handler=insert_example, eager=True)
prompt.set_key("c-x", handler=None)
prompt.set_key(insert_example)  # Remove all bindings using this handler.

panel = prompt.set_float(Label("Status"), top=0, right=0)
panel = prompt.set_float(Label("Updated"), target_float=panel, top=0, right=0)
prompt.set_float(target_float=panel)
prompt.set_float()  # Clear all custom floats.
```

Setting a tool replaces the callable for its command name. Tool functions run in
a spawned worker and must be pickleable; use an importable function such as `print`.
Setting a key replaces every binding for that exact sequence, including conditional
variants, and accepts the options supported by `KeyBindings.add`. A positional
handler removes all bindings using that function. Removing an absent entry is a no-op.

`set_float` returns a new `Float` handle. To replace a float, pass its current handle
as `target_float` and retain the returned handle. Replacement preserves list order
and uses default values for omitted `Float` options. Omitting content removes the
target, or clears all custom floats when the target is also omitted. Invalid tool
sources, key options, or float replacements raise errors before changing existing entries.

## Ruff 정책

요청한 E/W/F/I/B/C4/UP 규칙과 타입 표기 관련 예외를 사용합니다.
문서화 누락을 확인하기 위해 D100–D107을 추가했습니다. 전체 pydocstyle 규칙을 강제하지는 않습니다.
E722 예외는 선택적 import를 사용하는 `stdlib.py`에만 적용하며, 일반 코드에서는
`KeyboardInterrupt`나 `SystemExit`까지 삼키는 bare except를 피합니다.
S 규칙을 선택하지 않았으므로 테스트의 S101 예외는 두지 않습니다.

Ruff는 Git에서 무시한 테스트도 검사합니다. 기존 `.gitignore`의 `tests/`, `dev/` 규칙은
유지하므로 새 테스트·개발 문서를 버전 관리할 때는 포함 여부를 확인해야 합니다.

설정 참고: [uv의 Python 버전 선택](https://docs.astral.sh/uv/concepts/python-versions/),
[uv 바이트코드 사전 컴파일](https://docs.astral.sh/uv/reference/settings/#compile-bytecode),
[Ruff 설정](https://docs.astral.sh/ruff/configuration/).
