# ish 설정 안내

현재 제품은 libghostty-vt + PTK 기반 **ish** 하나다. 설치된 `ish`, 소스의 `python -m ish`가 같은 경로이며 예전 stream 실행기와 `ish-vt` 별칭은 제공하지 않는다. 지원 범위·제약은 [README](../README.md), 구현 계약은 [AGENTS.md](../AGENTS.md)를 참고한다.

## TUI로 익히기

```sh
ish -h                       # 카테고리별 도움말
ish --help --lang ko
ish --help > ish-help.txt     # 같은 문서를 일반 텍스트로 출력
ish -c                       # 설정 창
ish --config --home ~/my-ish
```

도움말은 시작하기, 키, 설정 파일, 플러그인·보완 모듈, 호스트 기능, 입력·화면, 진단·로그, 지원 범위, 실제 CLI 옵션으로 나뉜다. 본문 제목 오른쪽의 찾기 버튼 또는 Ctrl+F로 카테고리 이름을 검색한다. Enter로 검색한 페이지를 열고, Esc로 검색 전 페이지로 돌아간다.

화살표로 카테고리를 고르고 Enter 또는 →로 본문을 연다. 긴 문서는 마우스 휠이나 Alt+PgUp / Alt+PgDn으로 스크롤한다. Esc는 본문에서 좌측 패널로 돌아가고, 좌측 패널에서 한 번 더 누르면 창이 닫힌다. 상단 바의 ×, 오른쪽 아래 닫기 버튼 또는 Ctrl+D로도 도움말을 닫을 수 있다.

설정 창은 다음을 제공한다.

| 카테고리 | 설정 |
|---|---|
| 언어·시작 설정 | 언어, 셸, 시작 디렉터리, ESC/Alt 대기, 로그 파일·수준 |
| UI 팔레트 | 도움말·설정 창의 색상 역할과 명도 |
| 플러그인 | 플러그인 선택, 두 페이지가 공유하는 코드·의존성 경로 설정 |
| 모듈 | 프로토콜 등 터미널 기능을 보완하는 모듈 선택 |
| 호스트 기능 | 기능별 on/off, 실제 지원 질의가 있는 항목에만 auto |
| 입력·화면 | 마우스, 프로그램 클립보드 권한, 색 깊이, 스크롤백 한도 |

본문 하단 왼쪽에는 작업 진행·결과, 미저장 변경, 적용한 필터, 오류가 표시된다. 긴 경고는 왼쪽 영역에서 필요한 만큼 줄을 바꾼다. 오른쪽 버튼은 **취소**, **저장** 순서다. 본문에서 Tab을 누르면 취소가 선택되고, →를 누르면 저장으로 이동한다. Ctrl+S는 저장 버튼과 같은 동작으로 변경 사항을 파일에 기록한다. 취소는 초안을 마지막으로 저장하거나 불러온 값으로 되돌린다. 두 동작 모두 창을 열어 둔다.

페이지 제목 오른쪽의 옵션 필터·다시 읽기·파일 편집 도구는 클릭해서 실행한다. 키보드로는 본문에서 Shift+Tab으로 도구줄로 이동한 뒤 ← / →로 고르고 Enter나 Space로 실행한다. Ctrl+E는 `.ishrc.py`를 외부 편집기로 열고, Ctrl+R은 초안을 버리고 파일을 다시 읽는다. 외부 편집기는 VISUAL·EDITOR·vi 순서로 선택한다. 이미 창에서 바꾼 값이 있으면 저장이나 취소 후 편집한다.

`--config`에는 `--home`, `--lang`, `--no-rc`만 함께 지정할 수 있다. 그 밖의 CLI 세션 옵션은 실제 세션을 시작할 때 사용한다.

## 설정 파일과 저장

설정 파일은 `~/ish/.ishrc.py`다. `--home DIR`이면 `DIR/.ishrc.py`다. 상대 경로는 파일의 디렉터리가 기준이며 CLI 상대 경로는 실행한 디렉터리가 기준이다. 셸의 HOME과 rc 파일은 바꾸지 않는다.

```python
config.lang = "ko"
config.shell = "bash"
config.shell_args = ["-i"]
config.plugins = []
config.mouse = "auto"
config.clipboard = "write"
config.color_depth = "inherit"
config.escape_delay = 0.02
config.scrollback_lines = 10000
config.scrollback_bytes = 32 * 1024 * 1024
config.features = {
    "hyperlinks": "on",
    "enhanced_keyboard": "auto",
}
```

`config` 객체 자체를 재할당하지 않는다. 이전 `config.vt` 구조를 변환하거나 대신 읽지 않는다. `config` 필드는 Python 속성으로 선언하며, 알 수 없는 필드·잘못된 값은 셸 실행 전에 오류로 보고한다.

설정 TUI는 기존 코드와 주석을 유지하고 아래 형식의 관리 블록을 파일 끝에 둔다. 수동 코드 뒤에서 이 값이 최종 적용된다.

```python
# >>> ish settings
config.lang = 'ko'
config.mouse = 'off'
# <<< ish settings
```

기존 파일은 같은 디렉터리에 `.ishrc-backup-...` 파일로 백업한다. 새 파일을 완성하고 검증한 뒤 원자적으로 교체하며, 외부 변경이나 심볼릭 링크 대상 변경을 발견하면 저장을 거부한다. 다른 편집기와의 완전한 파일시스템 트랜잭션을 보장하는 것은 아니다. 손상된 관리 블록은 임의로 덮어쓰지 않는다. 파일을 직접 정리해 다시 읽거나 관리 블록을 통째로 제거할 수 있다.

저장할 때 rc를 다시 실행하거나 플러그인을 로드하지 않는다. 명시적으로 저장한 변경은 다음 세션에 적용한다. 취소는 설정 파일을 쓰지 않는다.

## 값과 우선순위

CLI가 저장 설정보다 우선한다. `--no-rc`는 이번 실행에서 ish 설정 파일을 건너뛴다. 셸의 rc는 원래대로 읽는다. `--login`은 셸에 `-l`만 전달하며, 반복한 `--shell-arg=ARG`는 기본 `-i`를 대체하고 `--login`과 함께 쓸 수 없다.

| 필드 | 기본값 / 허용값 |
|---|---|
| `lang` | None: LANG 자동 감지 후 영어. 명시적 언어가 최우선 |
| `shell` | 계정 로그인 셸, 없으면 SHELL 또는 sh |
| `shell_args` | `["-i"]` |
| `cwd` | 시작한 디렉터리 |
| `plugins` | `[]` |
| `plugin_dir` | `~/ish/plugin/script` |
| `plugin_lib_dir` | `~/ish/plugin/lib` |
| `mouse` | `auto`; `off`, `auto`, `on` |
| `clipboard` | `write`; `off`, `write`, `read-write` |
| `color_depth` | 기본 `inherit`는 본체 터미널의 색 깊이를 따르며 알 수 없으면 PTK 기본값을 사용. 직접 지정하려면 `1`, `4`, `8`, `24` 문자열 |
| `escape_delay` | 0.02초, 0.005~2.0. 느린 연결에서 Alt·방향키가 갈라지면 늘리세요. |
| `scrollback_lines` | 10000, 0 이상 정수 |
| `scrollback_bytes` | 33554432, 0이면 기록 비활성화 |
| `log_file` | None: 파일 로그 사용 안 함 |
| `log_level` | `WARNING`; DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `features` | 기능별 기본 정책, 아래 설명 |

한도는 native 페이지 단위의 대략적인 값이며 프로세스 전체 메모리 상한이 아니다. 스크롤백 CLI 정수 범위는 0~9223372036854775807이다.

## 언어

언어 우선순위는 명시한 설정(CLI 우선), LANG, 영어다. LC_ALL·LC_MESSAGES·LANGUAGE는 ish 언어를 덮어쓰지 않는다. 영어 기본 문구는 코드에 있고 영어 파일 없이 동작한다. 한국어는 제공 언어팩을 사용한다. 사용자 언어팩은 설정 디렉터리의 `lang/LANG.json`에 둔다.

도움말·버전·진단은 rc 코드를 실행하지 않아 --lang·LANG·영어 순서를 사용한다. 설정 창은 rc를 읽으므로 저장 언어를 표시한다. 셸 출력·외부 예외 세부 정보·wire 값·프로그램 식별자는 번역하지 않는다.

## 플러그인·보완 모듈

UI·키·출력·클립보드 보완 모듈은 같은 `config.plugins`와 `plugin_dir`를 사용한다. 별도 보완 모듈 경로나 로더는 없다. 메타데이터 조회는 코드 실행·의존성 설치 없이 수행하며, 발견된 메타데이터가 실행 가능성을 보증하는 것은 아니다.

설정 창에서 플러그인 경로 편집을 마치면 해당 폴더의 플러그인·모듈 목록이 갱신된다. 원래 폴더에 패키지를 추가한 경우 변경분을 저장한 뒤 ↻ 또는 Ctrl+R로 설정 파일과 목록을 함께 다시 읽는다. 선택 해제는 파일 삭제가 아니다. `.py` 수정과 선택 변경은 다음 세션에 적용하고 실행 중 교체하지 않는다.

```python
config.plugin_dir = "/absolute/path/to/ish/examples/terminal_plugins"
config.plugins = ["terminal_bar", "terminal_popup"]
```

`--plugin NAME`은 이번 세션의 선택 목록을 대체한다. `--no-plugins`는 저장된 선택을 이번 세션에서 끈다. 설정창의 **확장 → 플러그인**에서 하단 바·팝업 같은 일반 플러그인을, **확장 → 모듈**에서 키·클립보드 같은 보완 모듈을 선택한다. 실제 로딩과 의존성 검사는 같은 PluginManager가 담당한다. [분류별 예제 목록](../README.md#플러그인과-보완-모듈) · [플러그인 개발](PLUGINS.md) · [보완 모듈 API](VT_PROTOCOL_PLUGINS.md)

## 기능별 정책

모든 기능에 켜짐·꺼짐이 있으며, 지원 질의를 구현한 기능만 자동을 제공한다. 설정 화면에 나오는 정책과 실제 지원 조회 결과는 구별된다. 설정 화면은 호스트 질의를 보내지 않는다.

| 기능 | 기본 정책 | 자동을 위한 질의 |
|---|---|---|
| hyperlinks, extended_styles, overline, cursor_color | on | 없음 |
| iterm_graphics | off | 없음 |
| kitty_graphics | auto | Kitty graphics query |
| synchronized_output | auto | DECRQM 2026 |
| enhanced_keyboard | auto | CSI ? u |
| pixel_mouse | auto | DECRQM 1016 |
| clipboard_protocol | auto | DECRQM 5522 |
| scheme_notifications | auto | DECRQM 2031 |

on은 실제 지원 확인과 다르며 이미지 크기·커서 색 복원 등 필수 조건을 생략하지 않는다. 자동은 긍정 응답이 있어야 켠다. `clipboard_protocol`은 전송 프로토콜을 고르며 클립보드 읽기 권한을 부여하지 않는다.

## 입력·클립보드·복원

마우스 off는 호스트 드래그를 유지한다. auto는 프로그램이나 명시적인 클릭 UI 요청에 따라 입력을 받고, on은 내부 선택도 켠다. 호스트는 현재 표시한 내용만 알며 숨겨진 native history·팝업 뒤의 내용을 선택하지 못한다.

기본형은 Shift+PgUp·Shift+PgDn도 안쪽 프로그램에 전달한다. 본체의 일반 드래그 선택을 유지하면서 이 두 키로 이전 출력을 보려면 `terminal_history` 플러그인을 선택한다. 이 플러그인은 기본 화면에서만 페이지를 이동하며 Vim의 대체 화면과 팝업에서는 키를 예약하지 않는다.

키의 동작은 안쪽 프로그램이 결정한다. 일반 셸에서 Shift+Enter는 Enter와 같은 바이트(CR)로 전달된다. 프로그램이 Kitty 키보드 프로토콜 또는 xterm modifyOtherKeys를 요청하면 해당 형식으로 전달하므로 수정 키를 구별할 수 있다.

프로그램의 직접 클립보드 권한은 기본 write다. 읽기를 원할 때만 read-write를 선택한다. 이것은 평소 본체 터미널의 복사·붙여넣기와 별개다. `clipboard_system` 보완 모듈은 Windows/WSL이나 X11에 연결하며, 원격에서는 실제 전송 경로가 있어야 한다.

정상 종료·중지 시 관찰하고 변경한 터미널 상태를 복원한다. 프로그램이 바꾼 제목은 마지막 값을 유지한다. SIGKILL·호스트 소멸 같은 예외 종료의 완전 복원은 목표가 아니다. 자세한 범위는 [복원 계약](VT_RESTORATION.md)을 참고한다.

## 진단·로그

```sh
ish --version
ish --diagnose
ish --log-file ./ish.log --log-level INFO
```

조회 명령은 rc·플러그인·셸·native 라이브러리를 실행하지 않고 설정·로그 파일을 만들지 않는다. 진단은 기본값과 CLI 선택을 보고하며 실제 세션 지원을 확인했다고 표시하지 않는다.

파일 로그는 진단만 기록하고 native 입출력·클립보드 내용·native 로그 본문을 기록하지 않는다. 콘솔 로그는 활성 터미널 화면 밖에서 출력한다. 테스트 하네스의 숨긴 `--dump-state`는 화면 문자를 포함하는 명시적 진단이며 일반 로그와 다르다.
