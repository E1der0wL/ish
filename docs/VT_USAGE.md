# ish-vt 사용 설정

`ish-vt`는 libghostty-vt가 화면을 보관하고 PTK가 그리는 별도 실험 실행기다. `ish` 명령은 기존 출력 직접 전달 방식이며, 두 경로의 지원 범위는 다르다. 아래 옵션은 Linux/WSL의 `ish-vt`에 적용한다. 빌드와 실제 호스트 검증 절차는 [실험 안내](HOOKLESS_VT.md), 목표 범위는 [지원 명세](VT_BASELINE_AUDIT.md), 현재 구현·검증 범위는 [최신 보강 결과](VT_SUPPLEMENTS_RESULT.md)를 참고한다.

## 기본 실행

```sh
uv run --no-sync ish-vt
uv run --no-sync ish-vt --shell bash
uv run --no-sync ish-vt --shell zsh --cwd ~/work
uv run --no-sync ish-vt --shell tcsh --no-plugins
```

새 설정이 없는 기본형은 계정의 로그인 셸을 `-i`로 실행한다. 하단 바·팝업·검색 UI·플러그인 단축키를 추가하지 않는다. 명령줄, 보조 프롬프트, heredoc, 자동완성은 셸이 편집하며 ish는 셸 hook을 설치하지 않는다. 기본 전경·배경은 호스트 터미널 기본색을 따른다.

셸이나 프로그램이 지정한 창 제목도 호스트에 전달한다. ish-vt가 종료되어도 마지막 제목은 유지하며 이전 제목으로 복원하지 않는다. Windows Terminal 등에서 애플리케이션의 제목 변경을 차단한 경우에는 해당 호스트 설정이 우선한다.

현재 native bridge ABI는 **4**이다. 이전에 빌드한 로컬 라이브러리는 거부하므로 변경 뒤에는 `uv run --no-sync python tools/build_ghostty_vt.py --bridge-only`로 다시 빌드한다.

`--login`은 셸에 `-l`만 전달한다. 시작 인수를 직접 고르려면 `--shell-arg=ARG`를 반복한다. 이 목록은 기본 `-i`를 대체하며 `--login`과 함께 쓸 수 없다.

```sh
uv run --no-sync ish-vt --shell bash --login
uv run --no-sync ish-vt --shell bash --shell-arg=--noprofile --shell-arg=--norc --shell-arg=-i
```

셸의 rc 파일과 환경 변수 `HOME`은 변경하지 않는다. `--cwd`는 셸의 시작 디렉터리를, `--home`은 ish 설정 디렉터리를 선택한다.

## 설정 저장

기본 설정 파일은 `~/ish/.ishrc.py`다. `--home DIR`을 지정하면 `DIR/.ishrc.py`를 읽는다. 파일은 Python 코드이며 다음 세션을 시작할 때 한 번 실행된다. 실행 중 설정 파일을 바꾸더라도 현재 세션에는 적용되지 않는다.

`config.lang`, `shell`, `shell_args`, `cwd`, `plugin_dir`, `plugin_lib_dir`, `log_file`, `log_level`, `escape_delay`는 공통 시작 설정이다. `config.vt` 사전은 VT 실행기의 별도 설정이다. 공통 설정을 먼저 읽고 `config.vt`를 적용한 뒤 CLI 옵션이 최종 우선한다.

```python
# ~/ish/.ishrc.py
config.lang = "ko"                  # 없으면 LANG 자동 감지, 그다음 영어
config.shell = "bash"

# VT에만 적용한다. 아래 값은 예시이며 원하는 항목만 선언한다.
config.vt = {
    "plugins": [],                 # 기본형에는 플러그인이 없다
    "mouse": "auto",
    "clipboard": "write",
    "color_depth": "inherit",
    "escape_delay": 0.05,
    "scrollback_lines": 10000,
    "scrollback_bytes": 32 * 1024 * 1024,
}
```

`config.vt`에서는 위 공통 시작 설정도 덮어쓸 수 있다. `config.plugins`는 기존 stream 실행기의 선택이며, VT에서는 자동으로 불러오지 않는다. VT 플러그인은 `config.vt["plugins"]`에 지정한다. stream 실행기는 `config.vt`를 적용하지 않는다. 알 수 없는 VT 설정 키는 VT 실행 전에 오류로 보고한다.

설정 파일의 상대 경로는 `.ishrc.py`가 있는 디렉터리를 기준으로 한다. CLI 상대 경로는 ish-vt를 실행한 디렉터리를 기준으로 한다. 셸 실행 파일은 심볼릭 링크 이름을 유지한다.

- `--no-rc`: ish 설정 파일을 실행하지 않는다. 셸 rc는 원래대로 읽는다.
- `--no-plugins`: 저장된 플러그인을 이번 세션에서 모두 끈다.
- `--plugin NAME`: 이번 세션의 선택 목록을 지정한다. 반복할 수 있으며 저장된 목록에 덧붙이지 않고 대체한다.

## 플러그인 선택

저장소에 제공된 샘플은 명시적으로 선택할 때만 실행된다.

```sh
uv run --no-sync ish-vt --shell tcsh \
  --plugin-dir examples/terminal_plugins \
  --plugin terminal_bar --plugin terminal_popup

# 스크롤백 검색만 선택: Ctrl+F로 열기
uv run --no-sync ish-vt \
  --plugin-dir examples/terminal_plugins --plugin terminal_search
```

`terminal_bar`는 아래 한 줄만 사용한다. `terminal_popup`은 F2를 전역 팝업 키로 예약한다. `terminal_search`는 Ctrl+F를 예약하며 Enter로 이전 결과, Ctrl+P로 다음 결과, Esc로 닫는다. 해당 키는 셸과 Vim 같은 프로그램에서도 ish가 소비하므로 필요한 플러그인만 선택한다. 선택하지 않은 단축키는 native 입력 경로에 남는다.

팝업을 열어도 실행 중 프로그램은 계속 실행된다. 이미 PTY에 전달한 입력을 회수하지 않으며 팝업 결과를 셸 명령으로 실행하지 않는다. 새 플러그인의 로딩·메타데이터·의존성은 기존 PluginManager가 담당한다.

기본 플러그인 스크립트 경로는 `~/ish/plugin/script`, 의존성 경로는 `~/ish/plugin/lib`다. `--home`을 지정하면 이 경로도 함께 바뀐다. `--plugin-dir`과 `--plugin-lib-dir`로 각각 다른 경로를 지정할 수 있다. 기존 VT의 `--library-dir`도 같은 의존성 경로 옵션이다.

```python
# 예: 샘플을 영구 선택하려면 실제 저장소의 절대 경로로 바꾼다.
config.vt = {
    "plugin_dir": "/absolute/path/to/ish/examples/terminal_plugins",
    "plugins": ["terminal_bar", "terminal_popup"],
}
```

## 입력·스크롤백·클립보드

출력·키·클립보드 보완 모듈도 위의 **같은 플러그인 경로와 선택 목록**으로 관리한다. 기능별 경로는 추가하지 않는다. 외부 Python 모듈 파일을 변경하고 새 세션을 시작하면 적용되며 native 재컴파일은 필요하지 않다. API와 입력 제한은 [보완 모듈 작성](VT_PROTOCOL_PLUGINS.md)을 참고한다.

Windows/WSL 또는 전달된 X11 클립보드에 연결하려면 `clipboard_system` 패키지를 기존 플러그인 경로에 넣고 선택한다. 저장소에서 먼저 사용하려면 다음과 같이 실행할 수 있다. 이 모듈은 UI나 키 바인딩을 추가하지 않는다.

```sh
uv run --no-sync ish-vt --plugin-dir examples/terminal_plugins \
  --plugin clipboard_system --clipboard read-write
```

```python
# ~/ish/plugin/script/clipboard_system 에 패키지를 놓은 경우
config.vt = {
    "plugins": ["clipboard_system"],
    "clipboard": "read-write",  # 프로그램 읽기도 원할 때 명시
}
```

정책을 생략하면 기본 `write`를 유지하므로 프로그램의 직접 읽기는 허용하지 않는다. 본체 터미널의 평소 복사·붙여넣기와 별개다. 읽기 요청을 사용하는 프로그램 또는 그 프로그램의 clipboard provider 설정이 필요하다. 원격 실행은 전달된 X11 연결 등 실제 클립보드 연결이 있어야 하며 단순 SSH 접속만으로 로컬 클립보드가 생기지는 않는다.

| 옵션 / config.vt 키 | 기본값 | 의미 |
|---|---|---|
| `--mouse` / `mouse` | `auto` | `off`: 마우스 보고를 요청하지 않음. `auto`: 프로그램 또는 플러그인이 요청할 때 캡처. `on`: 로컬 선택까지 항상 활성화 |
| `--clipboard` / `clipboard` | `write` | `off`: 자식 프로그램의 클립보드 접근 차단. `write`: 쓰기 허용. `read-write`: 읽기도 허용. 호스트 지원·권한 제한은 그대로 적용 |
| `--color-depth` / `color_depth` | `inherit` | `1`: 단색, `4`: 16색, `8`: 256색, `24`: RGB. `inherit`는 PTK 환경 설정과 PTK 기본값을 상속 |
| `--feature 이름=값` / `features` | 기능별 | `on/off`, 실제 지원 질의를 구현한 항목만 `auto`. [전체 목록과 의미](VT_FEATURE_POLICY.md) |
| `--escape-delay` / `escape_delay` | `0.05`초 | ESC 단독 입력과 Alt 조합을 구분하는 대기 시간. 허용 범위 0.005~2.0초 |
| `--scrollback-lines` / `scrollback_lines` | `10000` | native 스크롤백의 대략적인 줄 수 한도 |
| `--scrollback-bytes` / `scrollback_bytes` | `33554432` | native 스크롤백의 대략적인 바이트 한도. `0`이면 스크롤백을 비활성화 |

스크롤백은 libghostty의 페이지 단위로 보관되므로 정확한 줄 수·바이트 수와 다를 수 있다. 두 한도 중 먼저 도달한 쪽이 적용된다. CLI 한도는 0부터 `9223372036854775807`까지의 정수이며 bool·음수·그보다 큰 값은 거부한다. 이 값은 전체 프로세스의 메모리 상한이 아니다.

ESC 대기를 줄이면 느리게 들어오는 Alt 시퀀스가 분리될 수 있다. 호스트별 특수키·IME·이미지 기능 지원은 이 옵션만으로 추가되지 않는다. `--mouse off`에서는 플러그인도 키보드로 조작해야 한다. `auto` 상태에서 native 프로그램이 마우스를 요청하면 그 프로그램이 입력을 소비한다.

색 깊이 `inherit`에서는 `NO_COLOR`와 `PROMPT_TOOLKIT_COLOR_DEPTH`를 PTK의 기존 규칙대로 읽는다. 별도 환경 선택이 없으면 `TERM=xterm-256color`의 PTK 기본값은 **256색**이다. 터미널 이름으로 RGB를 추측해 켜지 않는다. 종전 `auto`라는 이름은 지원 질의와 혼동하지 않도록 `inherit`로 바뀌었다. Windows Terminal/WSL에서 RGB를 그대로 출력하려면 다음처럼 명시한다.

```sh
uv run --no-sync ish-vt --color-depth 24
```

`config.vt["color_depth"] = "24"`로 저장할 수도 있다. 설정값은 숫자가 아니라 `"inherit"`, `"1"`, `"4"`, `"8"`, `"24"` 중 하나인 **문자열**이다. 명시한 색 깊이는 PTK 환경 설정보다 우선한다. 이 옵션은 색상 출력만 바꾸며 이미지·링크·클립보드·키보드 등 다른 호스트 기능을 활성화하지 않는다. 호스트가 선택한 색 깊이를 실제로 표현하는지는 별개의 조건이다.

커서 기본 모양·깜박임은 시작 시 호스트의 DECRQSS ` q` 응답이 구체적인 값 1~6일 때만 가져온다. 질의 전에는 PTK가 커서 모양을 바꾸지 않으며, 관측한 값을 native 기본값에 연결하고 자식 프로그램 실행 전에 한 번 적용한다. 이후 프로그램이 지정한 커서는 덮어쓰지 않는다. 종료할 때에는 PTK의 자체 reset 뒤에 관측한 세션 초기 값을 복원한다.

응답이 없거나 `0`(불명확한 기본값)이면 추측하지 않고 기존 block·비깜박임 기본 정책을 유지한다. 시작값 관찰은 첫 화면 변경과 alternate 화면 진입 전에 수행한다. 조회할 수 없는 상태까지 포함한 **호스트 전체 snapshot은 아니다**. hollow block은 PTK 표현 범위에 따라 일반 block으로 축소한다. 실제 호스트 응답 가용성과 외관 확인은 문자·응답 회귀와 별도로 기록한다. 자세한 순서와 한계는 [복원 계약](VT_RESTORATION.md)을 따른다.

`off` 또는 마우스를 요청하지 않은 `auto`에서는 호스트의 평소 드래그·복사 기능을 사용한다. 이때 호스트는 화면에 표시된 내용만 알며, libghostty 내부의 숨겨진 history나 팝업 뒤 내용을 선택하지 못한다. `on`에서는 ish의 내부 선택·휠 스크롤을 사용한다. 내부 history는 Shift+PageUp/Down으로도 이동한다. 종료하면 호스트의 기존 primary 화면으로 돌아가며 내부 history를 호스트 scrollback에 옮기지 않는다.

## 언어·진단·로그

언어 선택 순서는 `--lang`, 설정에 명시한 언어(`config.vt["lang"]`이 공통 `config.lang`보다 우선), `LANG`, 영어다. `LC_ALL`, `LC_MESSAGES`, `LANGUAGE`는 ish 문구의 언어를 바꾸지 않는다. 셸과 실행 프로그램의 출력은 번역하지 않는다. 공통 언어팩은 `~/ish/lang` 또는 `--home DIR` 아래의 `lang`에 둔다.

```sh
uv run --no-sync ish-vt --help --lang ko
uv run --no-sync ish-vt --version
uv run --no-sync ish-vt --diagnose
uv run --no-sync ish-vt --log-file ./ish-vt.log --log-level INFO
```

`--help`, `--version`, `--diagnose`는 rc 코드·플러그인·셸·native 라이브러리를 실행하지 않고 설정·로그 파일을 생성하지 않는다. 따라서 진단에 표시되는 값은 기본값과 CLI 옵션이며, 저장한 rc를 실행해 확인한 값이 아니다. 조회 언어도 `--lang`, `LANG`, 영어 순서다. native 라이브러리 경로가 존재한다는 보고는 호환성이나 호스트 기능 지원을 증명하지 않는다.

파일 로그는 `--log-file PATH` 또는 설정으로 명시할 때만 기록한다. 경로 없이 `--log-file`만 주면 ish 설정 디렉터리의 `.ish.log`를 사용한다. 수준은 `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`이며 기본값은 `WARNING`이다. `INFO`는 세션 시작·종료와 native 진단의 수준별 건수를 포함한다. 로그는 native 입력·출력 바이트와 native 로그 본문을 수집하지 않는다. 콘솔 진단은 터미널 복원 뒤 출력한다. 외부 플러그인은 직접 작성하는 로그에 대해 책임진다.

입력 framing 한도를 초과해 버린 제어문은 `WARNING`으로 건수만 보고한다. 프로토콜 보완 모듈의 실패도 식별자·건수만 보고하며 해당 제어문의 본문을 로그에 쓰지 않는다.

호스트 입력에서 지원하지 않는 제어문을 소비한 횟수는 `INFO`, 외부 클립보드 연결 실패 횟수는 `WARNING`으로 보고한다. 일반 문자·지원 키·붙여넣기와 구분하며, 응답과 같은 신호를 보내는 일부 legacy 수정 기능키는 입력으로 추측하지 않는다. 알려진 입력 제한과 추가 키 매핑 방법은 [보완 모듈 작성](VT_PROTOCOL_PLUGINS.md)에 정리한다.

테스트 하네스의 숨긴 `--dump-state`는 화면 문자를 포함하는 별도 opt-in 진단이다. 일반 파일 로그와 다르며 평상시에는 지정하지 않는다.

## 지원 범위

현재 검증 대상은 Linux/WSL의 Bash·zsh·tcsh다. 셸 hook을 사용하지 않는다고 해서 모든 OS, 모든 호스트 터미널, 모든 escape code를 검증한 것은 아니다. SIXEL 입력은 지원하지 않는다. 기본형의 이미지·클립보드·글자 표현도 libghostty 연결과 실제 호스트의 기능에 따라 달라진다. `ish`의 출력 직접 전달 경로와 혼동하지 않는다.
