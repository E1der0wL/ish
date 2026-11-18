# ish 2.0.0

ish는 **libghostty-vt + prompt_toolkit(PTK)** 기반 터미널입니다. 명령줄·보조 프롬프트·heredoc은 원래 셸이 편집하고, ish는 화면과 입력 전달을 담당합니다. 필요할 때 Python 플러그인으로 하단 바·팝업·검색·프로토콜 보완 기능을 추가합니다.

**기본형은 추가 UI와 셸 hook이 없습니다.** Bash·zsh·tcsh를 Linux/WSL에서 검증합니다. 본체 터미널의 모든 기능을 원문 통과시키는 프로그램은 아니며 지원 범위는 아래에 설명합니다.

## 시작하기

설치된 실행 파일이 있다면:

```sh
ish                         # 계정의 기본 셸
ish --shell bash
ish --shell zsh --cwd ~/work
ish --shell tcsh --no-plugins
ish -h                      # 카테고리별 도움말 TUI
ish -c                      # 설정 TUI
```

`python -m ish`도 같은 실행 경로입니다. 현재 제품 명령은 `ish` 하나입니다. 이전 stream 실행기·`ish-vt` 별도 명령·구 설정 호환 기능은 제공하지 않습니다.

소스에서 실행하려면 Linux x86_64/WSL에서 다음을 실행하세요. Python 3.12.14 이상·3.13 미만과 uv, native 빌드용 C 컴파일러·curl·tar·xz가 필요합니다.

```sh
uv sync --frozen --group dev
uv run --no-sync python tools/build_ghostty_vt.py  # 최초 native 빌드
uv run --no-sync ish
```

정식 개발 폴더는 `D:\Developments\ish`이며, WSL에서는 `/mnt/d/Developments/ish`입니다. 개발 환경과 배포 관련 내용은 [개발 안내](docs/DEVELOPMENT.md)를 참고하세요.

## 도움말과 설정 TUI

`ish -h` 또는 `ish --help`는 시작하기·키·설정 파일·플러그인·호스트 기능·입력/화면·진단·지원 범위·CLI 옵션을 카테고리로 보여줍니다. 터미널이 아닌 곳으로 출력하면 같은 문서를 일반 텍스트로 제공합니다.

```sh
ish --help --lang ko
ish --help | less
ish --config
ish --config --home ~/my-ish
```

- 화살표: 카테고리·항목 이동
- Enter/오른쪽: 선택한 카테고리 내용으로 이동
- Tab / Shift+Tab: 입력 항목 사이 이동
- Space: 선택·해제
- PageUp / PageDown: 문서 스크롤
- Esc / Ctrl+C / Ctrl+D: 닫기·취소
- 설정에서 Ctrl+S: 저장 후 닫기
- 설정에서 Ctrl+E: `.ishrc.py`를 외부 편집기로 열기
- 설정에서 Ctrl+R: 창의 변경을 버리고 파일 다시 읽기

설정 창에서는 언어·시작 설정, 플러그인과 보완 모듈, 기능별 켜기/끄기/자동, 마우스·클립보드·색 깊이·스크롤백을 관리합니다. 기능 목록은 **사용자 정책**이며 현재 호스트가 지원한다고 확인한 결과가 아닙니다. 지원 질의가 구현된 기능에만 자동을 제공합니다.

플러그인 목록은 메타데이터만 읽습니다. 목록을 보거나 선택을 저장할 때 Python 플러그인을 실행하거나 의존성을 설치하지 않습니다. 선택한 플러그인은 다음 세션에서 로드합니다.

도움말·버전·진단은 설정 Python 코드도 실행하지 않습니다. 설정 창은 현재 값을 보여주기 위해 `.ishrc.py`를 평가합니다. `--config`와 함께 쓸 수 있는 옵션은 `--home`, `--lang`, `--no-rc`입니다.

## 설정 파일

기본 경로는 **`~/ish/.ishrc.py`**이며 `--home DIR`이면 `DIR/.ishrc.py`입니다. 설정은 평면 `config` 객체 하나에 지정합니다.

```python
config.lang = "ko"                 # None: LANG 자동 감지 → 영어
config.shell = "bash"
config.plugins = []                # 기본형은 아무 플러그인도 없음
config.mouse = "auto"              # off / auto / on
config.clipboard = "write"         # off / write / read-write
config.color_depth = "inherit"     # inherit / 1 / 4 / 8 / 24
config.features = {"hyperlinks": "on", "enhanced_keyboard": "auto"}
```

언어 우선순위는 명시한 설정(CLI가 파일보다 우선), `LANG`, 영어입니다. `LC_ALL`·`LC_MESSAGES`·`LANGUAGE`는 ish 언어를 바꾸지 않습니다. 셸 출력은 번역하지 않습니다.

설정 TUI는 기존 코드·주석을 보존하고 파일 끝의 관리 블록에 변경한 필드를 저장합니다. 기존 파일은 같은 디렉터리에 백업하고, 파일이 다른 곳에서 변경되었다면 덮어쓰지 않고 다시 읽도록 안내합니다. 취소는 저장하지 않습니다. 설정 변경은 **다음 세션부터** 적용됩니다.

외부 편집기는 `VISUAL`, `EDITOR`, `vi` 순서로 선택하며 셸 명령 문자열로 실행하지 않습니다. 창의 초안이 있으면 먼저 저장하거나 Ctrl+R로 버린 뒤 파일을 편집하세요. 수동 편집과 TUI 저장을 번갈아 할 때에는 관리 블록을 파일 끝에 유지하거나 통째로 제거하세요.

## 플러그인과 보완 모듈

소스 경로는 `~/ish/plugin/script`, 의존성 경로는 `~/ish/plugin/lib`입니다. **UI·이미지/프로토콜·키·클립보드 보완 모듈은 같은 PluginManager와 선택 목록을 사용합니다.**

플러그인은 `NAME/NAME.py` 또는 `NAME/__init__.py`와 리터럴 `PLUGIN_META` 메타데이터를 가진 패키지입니다. 신뢰하는 패키지를 소스 경로에 넣고 설정 창에서 목록을 새로 읽어 선택하세요. 선택 해제는 파일을 지우지 않고 다음 실행에서 로드하지 않게 합니다.

저장소 샘플 사용:

```sh
ish --plugin-dir examples/terminal_plugins --plugin terminal_bar --plugin terminal_popup
ish --plugin-dir examples/terminal_plugins --plugin terminal_search
ish --plugin-dir examples/terminal_plugins --plugin clipboard_system --clipboard read-write
```

- `terminal_bar`: 하단 한 줄
- `terminal_popup`: F2 팝업
- `terminal_search`: Ctrl+F 스크롤백 검색
- `clipboard_system`: Windows/WSL 또는 X11 클립보드 연결, 추가 UI 없음

선택한 플러그인의 전용 키는 Vim 등 실행 중 프로그램에서도 소비될 수 있습니다. `--no-plugins`는 이번 세션에서 모두 끄고, 반복한 `--plugin NAME`은 이번 세션의 목록을 지정합니다. 플러그인은 사용자의 권한으로 실행되는 Python 코드이며, 실제 로드 때 선언한 의존성을 설치할 수 있습니다.

[보완 모듈 API](docs/VT_PROTOCOL_PLUGINS.md), [샘플 코드](examples/terminal_plugins)를 참고하세요.

## 주요 옵션

| 옵션 | 의미 |
|---|---|
| `--shell SHELL`, `--cwd DIR` | 셸·시작 디렉터리 |
| `--login` | 셸에 `-l` 전달 |
| `--shell-arg=ARG` | 기본 `-i`를 대체하는 인수, 반복 가능, `--login`과 동시 사용 불가 |
| `--home DIR`, `--no-rc` | ish 설정 위치·이번 실행에서 설정 파일 무시 |
| `--lang LANG` | ish 언어 |
| `--mouse off/auto/on` | 호스트 드래그 유지 / 프로그램 요청에 따라 캡처 / 내부 선택도 사용 |
| `--clipboard off/write/read-write` | 프로그램의 직접 클립보드 접근 권한 |
| `--feature NAME=on/off/auto` | 기능별 정책, 자동은 지원 질의를 구현한 기능만 |
| `--color-depth inherit/1/4/8/24` | 출력 색 깊이, 기본 PTK·환경 설정 상속 |
| `--escape-delay SECONDS` | Esc 단독 바이트의 후속 입력 대기, 기본 0.02초. 원격 연결에서 Alt·방향키 입력이 갈라지면 이 값을 늘리세요. |
| `--scrollback-lines N`, `--scrollback-bytes N` | native 기록 한도, 바이트 0이면 비활성화 |
| `--log-file [PATH]`, `--log-level LEVEL` | 선택적 파일 로그·수준 |
| `--version`, `--diagnose` | 버전·읽기 전용 환경 진단 |

CLI가 파일 설정보다 우선합니다. 상세 사용법은 `ish --help`와 [설정 안내](docs/VT_USAGE.md)에 있습니다.

## 지원 범위

- Linux/WSL의 Bash·zsh·tcsh, continuation·heredoc·Vim·resize를 검증합니다.
- SIXEL·Kitty·iTerm 이미지 입력을 지원합니다. libghostty 소스는 수정하지 않으며 SIXEL과 iTerm은 같은 이미지용 특수 문자 방식을 씁니다. 이미지 칸에 글자를 쓰거나 칸을 지우면 해당 이미지 부분도 사라지고, 투명 부분 아래의 기존 글자·이미지는 보존하지 않습니다. SIXEL 출력은 호스트의 DA1 지원 응답·셀 크기·표시 모드 복원 가능성을 확인해 자동 선택합니다. 이미지·링크·확장 키는 실제 호스트 기능에 따라 달라지며, 알파 합성 등 남은 제한은 [SIXEL 지원 범위](docs/VT_SIXEL.md)에 정리합니다.
- 데스크톱 알림은 `--feature desktop_notifications=on`으로 선택합니다. 기본값은 꺼짐입니다. 명령 출력 탐색·복사는 `terminal_commands` 샘플 플러그인으로 선택하며, 셸이 이미 보내는 OSC 133 정보를 사용합니다. ish가 셸 hook을 설치하지 않습니다. [선택 확장 사용법](docs/VT_OPTIONAL.md)을 참고하세요.
- 마우스 off에서는 호스트 드래그·복사를 사용합니다. 호스트는 현재 보이는 화면만 알며 숨겨진 native history나 팝업 뒤의 내용을 복사하지 못합니다.
- 프로그램의 직접 클립보드 접근과 호스트의 평소 복사·붙여넣기는 별개입니다. 원격 연결에서는 실제 클립보드 전송 경로가 필요합니다.
- 일부 오래된 키 형식은 터미널 응답과 구분할 수 없어 키로 추측하지 않습니다. 호스트가 먼저 소비한 키는 ish로 오지 않습니다.
- 정상 종료는 관찰한 시작 상태를 복원하지만 마지막 제목은 유지합니다. 강제 종료·호스트 소멸 때의 완전 복원은 보장하지 않습니다.

## 개발·검증

```sh
uv run --no-sync python tools/check_terminal.py
# 실제 호스트 터미널 안에서 별도로 실행
uv run --no-sync python tests/terminal_matrix.py .cache/host-new-run --require-resize
uv run --no-sync python tests/management_host.py .cache/management-host-new
```

[AGENTS.md](AGENTS.md)의 계약·소유권·명명 규칙을 따릅니다. 자동 PTY 테스트, 실제 호스트 응답, 화면 외관·물리 입력 검증은 구분합니다. 상세 계약은 [검증 명세](docs/VT_TEST_CONTRACTS.md), [기능 정책](docs/VT_FEATURE_POLICY.md), [복원](docs/VT_RESTORATION.md)에 정리합니다.
