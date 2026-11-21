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

좌측에서 카테고리를 클릭하면 오른쪽에 해당 페이지가 열립니다. 포커스가 있는 패널은 설정한 색으로 표시하고, 다른 패널은 같은 내용과 밝기를 유지해 흑백으로 표시합니다. 두 패널 사이의 구분선에 마우스를 올리면 강조색으로 바뀝니다. 구분선을 좌우로 끌어 패널 너비를 조절할 수 있습니다. 긴 페이지에서는 휠로 스크롤하거나 오른쪽의 스크롤바 손잡이를 끌 수 있습니다. 하단 바에 표시된 키를 클릭해도 같은 동작을 실행합니다.

**⚙ 설정**의 본문 하단에는 왼쪽에 상태와 진행 표시가, 오른쪽에 **취소**, **저장** 버튼이 이 순서로 있습니다. 긴 경고는 왼쪽 공간 안에서 줄을 바꿔 표시합니다. 값을 바꾸면 **저장할 변경 있음**이 나타납니다. **Ctrl+S**를 누르거나 **저장**을 클릭하면 변경 사항을 파일에 기록합니다. **취소**는 초안을 마지막으로 저장하거나 불러온 값으로 되돌립니다. 두 동작 모두 창을 열어 둡니다. 닫으려면 좌측 패널에서 Esc를 누르거나 상단 바 오른쪽의 ×를 클릭하세요. 남은 미저장 변경은 버려집니다.

페이지 제목 오른쪽에는 **⚲ 옵션 필터**, **↻ 설정 다시 읽기**, **✎ .ishrc.py 편집**이 있습니다. 본문에서 Tab / Shift+Tab으로 버튼 그룹을 이동하고, 그룹 안에서는 ← / →로 버튼을 고릅니다. 활성 패널의 버튼에 마우스를 올리면 포커스가 이동하고, 버튼에서 벗어나면 원래 위치로 돌아갑니다. 키보드로 고른 버튼은 선택이 유지됩니다. 텍스트·숫자·HEX 입력칸 밖을 클릭하면 유효한 값은 초안에 반영됩니다. 잘못된 값은 편집 시작 시점의 값으로 돌아가며 상태 바에 이유가 표시됩니다. Enter나 저장을 눌렀을 때 값이 잘못되면 입력칸에 그대로 남아 고칠 수 있습니다. Esc는 이번 입력만 취소합니다. 버튼은 클릭하거나 Enter 또는 Space로 실행하고, 본문 빈 곳을 클릭하거나 Esc를 눌러 이전 항목으로 돌아갑니다.

| 위치 | 키 | 동작 |
|---|---|---|
| 좌측 패널 | ↑ / ↓, Tab / Shift+Tab | 현재 그룹의 항목 이동, 다음 / 이전 그룹 이동 |
| 좌측 패널 | Enter / Space / →, Ctrl+← / Ctrl+→ | 본문 열기, 좌측 패널 너비 조절 |
| 설정 본문 | ↑ / ↓, Enter 또는 Space | 항목을 고르고 편집 시작. 목록은 ↑ / ↓로 선택하고 Enter 또는 Space로 적용 |
| 편집 중인 숫자 입력칸 | ↑ / ↓ | Esc 대기 시간은 0.001초, 스크롤백은 1줄 또는 1바이트씩 증가·감소 |
| 설정 본문 | Tab / Shift+Tab | 본문 → 하단 버튼 → 제목 오른쪽 도구줄 → 본문 순서 또는 역순. 본문에서 Tab을 누르면 취소가 선택되고, →로 저장을 선택 |
| 좌측 패널·본문 | Alt+↑ / ↓, Alt+PgUp / PgDn, Alt+Home / End | 포커스된 영역을 한 줄, 한 화면, 처음 또는 끝으로 스크롤 |
| 긴 한 줄 입력 필드 | Alt+← / → | 편집 커서를 유지하고 좌우로 스크롤 |
| 마우스를 가리킨 패널 | 휠, 손잡이 드래그, △ / ▽ 클릭 | 휠 한 번에 세 줄, 화살표 클릭 한 번에 한 줄 이동. 내용이 넘치면 오른쪽에 스크롤바 표시 |
| 설정창 양쪽 패널 | Ctrl+F | 현재 페이지의 옵션 필터 열기. 설정 키·이름 또는 현재 값으로 검색하고 Enter로 적용, Esc로 취소. 페이지마다 필터를 따로 유지 |
| 도움말 창 | Ctrl+F 또는 본문 제목 오른쪽의 찾기 버튼 | 카테고리 이름으로 찾기. Enter로 열고 Esc로 취소 |
| 양쪽 패널 | Esc | 검색·편집에서 돌아가기, 본문에서 좌측으로 이동, 좌측에서 창 닫기 |
| 설정 창 | Ctrl+S | 변경 사항을 파일에 저장하고 계속 편집 |
| 설정 창 | Ctrl+E, Ctrl+R | `.ishrc.py`를 외부 편집기로 열기, 초안을 버리고 다시 읽기 |
| 도움말 창 | Ctrl+D | 도움말 닫기 |

설정 창에서는 언어·시작 설정, 플러그인과 보완 모듈, 기능별 켜기/끄기/자동, 마우스·클립보드·색 깊이·스크롤백을 관리합니다. 기능 목록은 **사용자 정책**이며 현재 호스트가 지원한다고 확인한 결과가 아닙니다. 지원 질의가 구현된 기능에만 자동을 제공합니다.

예를 들어 **언어·시작 설정**에서 `ui_icons`로 필터를 적용하면 아이콘 스타일만 보입니다. **UI 팔레트**로 이동하면 그 페이지의 옵션이 보이고, **언어·시작 설정**으로 돌아오면 `ui_icons` 필터가 다시 보입니다. 숨겨진 항목의 초안은 유지됩니다. 현재 페이지의 필터를 해제하려면 검색어를 지운 뒤 Enter를 누르세요.

### 도움말·설정창의 색상

`ish -c`에서 **UI 팔레트**를 열고 ↑ / ↓로 역할을 고른 뒤 Enter 또는 Space를 누르세요. 편집 중 ↑ / ↓는 색상 계열을, ← / →는 명도를 바꿉니다. 선택한 명도에는 밑줄이 표시됩니다. 목록 끝의 **기본색**은 본체 터미널의 색을 사용하고 **선택색**은 `HEX #` 뒤에 `1A2B3C`처럼 16진수 여섯 자리를 입력해 지정합니다. Enter로 이번 색을 적용하거나 Esc로 취소할 수 있습니다. 마우스로는 역할과 색상 견본을 클릭하세요. **저장**을 누르면 다음 도움말·설정창에도 적용됩니다.

**22개 계열 × 9단계로 총 198색**을 제공합니다. 설정 파일에는 `색상이름-단계` 형식의 토큰을 씁니다. 예를 들어 `blue-100`은 밝은 파랑, `blue-900`은 어두운 파랑입니다. 단계는 100부터 900까지 100 단위이며 각 값은 [Tailwind CSS 3.4.17 색상표](https://github.com/tailwindlabs/tailwindcss/blob/v3.4.17/src/public/colors.js)를 따릅니다.

| 계열 | 설정에 쓰는 이름 |
|---|---|
| 빨강·주황·노랑 | `red`, `orange`, `amber`, `yellow` |
| 초록·청록 | `lime`, `green`, `emerald`, `teal`, `cyan` |
| 파랑·보라·분홍 | `sky`, `blue`, `indigo`, `violet`, `purple`, `fuchsia`, `pink`, `rose` |
| 회색 | `slate`, `gray`, `zinc`, `neutral`, `stone` |

| 역할 | 설정 키 | 기본값 |
|---|---|---|
| 배경, 본문 글자 | `background`, `text` | `default` (본체 터미널 기본색) |
| 강조 | `accent` | `lime-300` |
| 보조 글자 | `muted` | `slate-400` |
| 구분선 | `edge` | `slate-600` |
| 선택 배경 | `selection` | `slate-800` |
| 오류 | `error` | `rose-400` |

이 팔레트는 도움말·설정창에 적용됩니다. 셸이나 Vim 화면의 색 깊이는 `config.color_depth`에서 따로 설정합니다.

**언어·시작 설정 → 아이콘 스타일**에서 **유니코드** 또는 **Nerd Font**를 선택하면 화면에서 바로 비교할 수 있습니다. 기본 유니코드 모드는 일반 터미널 글꼴을 사용합니다. Nerd Font를 선택하려면 본체 터미널의 글꼴도 Nerd Font로 설정하세요. **저장**하면 다음 설정창과 도움말에도 적용됩니다. 파일에서는 `config.ui_icons = 'unicode'` 또는 `config.ui_icons = 'nerdfont'`로 지정합니다.

값을 입력하는 칸을 클릭하면 편집이 시작되고 제목 왼쪽에 연필이 나타납니다. Esc 대기 시간, 스크롤백 줄 수와 바이트 수처럼 숫자를 입력하는 칸은 오른쪽에 위아래 화살표가 표시됩니다.

플러그인 목록은 메타데이터만 읽습니다. 목록을 보거나 선택을 저장할 때 Python 플러그인을 실행하거나 의존성을 설치하지 않습니다. 선택한 플러그인은 다음 세션에서 로드합니다.

설정 창은 현재 값을 보여주기 위해 `.ishrc.py`를 평가합니다. 대화형 도움말은 파일에 직접 적힌 `config.ui_palette` 딕셔너리와 `config.ui_icons` 문자열을 읽습니다. Python 코드로 계산하는 항목에는 각각 기본값을 적용합니다. 도움말·버전·진단은 rc 코드를 실행하지 않습니다. `--config`와 함께 쓸 수 있는 옵션은 `--home`, `--lang`, `--no-rc`입니다.

## 설정 파일

기본 경로는 **`~/ish/.ishrc.py`**이며 `--home DIR`이면 `DIR/.ishrc.py`입니다. 설정은 평면 `config` 객체 하나에 지정합니다.

```python
config.lang = "ko"                 # None: LANG 자동 감지 → 영어
config.shell = "bash"
config.plugins = []                # 기본형은 아무 플러그인도 없음
config.mouse = "auto"              # off / auto / on
config.clipboard = "write"         # off / write / read-write
config.color_depth = "inherit"     # 본체 터미널에 맞춤. 직접 지정할 때는 1 / 4 / 8 / 24
config.ui_palette = {"accent": "blue-300", "selection": "slate-700"}
config.features = {"hyperlinks": "on", "enhanced_keyboard": "auto"}
```

언어 우선순위는 명시한 설정(CLI가 파일보다 우선), `LANG`, 영어입니다. `LC_ALL`·`LC_MESSAGES`·`LANGUAGE`는 ish 언어를 바꾸지 않습니다. 셸 출력은 번역하지 않습니다.

설정 TUI는 기존 코드·주석을 보존하고 파일 끝의 관리 블록에 변경한 필드를 저장합니다. 기존 파일은 같은 디렉터리에 백업하고, 파일이 다른 곳에서 변경되었다면 덮어쓰지 않고 다시 읽도록 안내합니다. 취소는 저장하지 않습니다. 설정 변경은 **다음 세션부터** 적용됩니다.

외부 편집기는 `VISUAL`, `EDITOR`, `vi` 순서로 선택하며 셸 명령 문자열로 실행하지 않습니다. 창의 초안이 있으면 먼저 저장하거나 Ctrl+R로 버린 뒤 파일을 편집하세요. 수동 편집과 TUI 저장을 번갈아 할 때에는 관리 블록을 파일 끝에 유지하거나 통째로 제거하세요.

## 플러그인과 보완 모듈

소스 경로는 `~/ish/plugin/script`, 의존성 경로는 `~/ish/plugin/lib`입니다. **UI·이미지/프로토콜·키·클립보드 보완 모듈은 같은 PluginManager와 선택 목록을 사용합니다.**

플러그인은 `NAME/NAME.py` 또는 `NAME/__init__.py`와 리터럴 `PLUGIN_META` 메타데이터를 가진 패키지입니다. 플러그인 경로를 편집한 뒤 Enter를 누르면 새 폴더의 목록을 바로 확인할 수 있습니다. 현재 폴더에 파일을 추가했다면 변경분을 먼저 저장하고 **↻ 설정 다시 읽기** 또는 Ctrl+R을 누르세요. 설정 파일과 플러그인·모듈 목록을 함께 다시 읽습니다. 선택 해제는 파일을 지우지 않고 다음 실행에서 로드하지 않게 합니다.

설정창의 **플러그인** 페이지에서 소스·의존성 경로와 일반 플러그인을 관리하고, **모듈** 페이지에서 터미널 보완 모듈을 선택합니다. 패키지의 `PLUGIN_META["category"]`에 `"module"`을 쓰면 모듈 페이지에, `"both"`를 쓰면 양쪽에 표시됩니다. 기본값은 `"plugin"`입니다. 양쪽에 표시되는 패키지의 체크 상태와 실행 순서는 공유됩니다. 누락된 선택은 플러그인 페이지에서 해제할 수 있습니다.

저장소 샘플 사용:

```sh
ish --plugin-dir examples/terminal_plugins --plugin terminal_bar --plugin terminal_popup
ish --plugin-dir examples/terminal_plugins --plugin terminal_search
ish --plugin-dir examples/terminal_plugins --plugin clipboard_system --clipboard read-write
```

- `terminal_bar`: 하단 한 줄
- `terminal_popup`: F2 팝업
- `terminal_search`: Ctrl+F 스크롤백 검색
- `terminal_history`: 기본 화면에서 Shift+PgUp·Shift+PgDn으로 이전 출력 탐색. 선택하지 않으면 두 키는 프로그램에 전달됩니다.
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
| `--color-depth inherit/1/4/8/24` | 기본값 `inherit`는 본체 터미널에 맞춤. 색 깊이를 확인할 수 없으면 기존 PTK 기본값 사용 |
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
