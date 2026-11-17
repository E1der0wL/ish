# ish 2.0.0

이 worktree의 `codex/hookless-vt` 실험은 별도 **`ish-vt`** 명령에서 셸 hook 없는 libghostty-vt + PTK 화면을 제공합니다. 기본형에는 추가 UI가 없고, 선택한 플러그인만 상시 하단 바와 전역 팝업을 켭니다. 실행법·범위·검증은 [hook 없는 VT 실험](docs/HOOKLESS_VT.md)을 참고하세요. 아래 `ish` 명령의 stream 동작은 유지합니다.

**ish는 기존 셸의 편집과 터미널 출력을 유지하면서, 프롬프트 대기 중에만 선택한 Python 플러그인의 UI를 표시합니다.**

- 명령줄, 보조 프롬프트, heredoc, 자동완성은 Bash·zsh·tcsh 등 실행한 셸이 처리합니다.
- 셸과 프로그램의 출력은 호스트 터미널에 직접 전달합니다. 화면, 스크롤백, 마우스, 이미지 지원은 호스트가 담당합니다.
- 플러그인을 선택하지 않은 기본형에는 하단 바, 팝업, 예약 단축키, 셸 hook이 없습니다. 기본 전경·배경색도 호스트 설정을 따릅니다.
- 플러그인을 선택하면 primary 프롬프트에서 하단 바와 F2 팝업을 사용할 수 있습니다. 프로그램 실행 중이나 보조 프롬프트에서는 UI를 숨깁니다.

현재 실행 대상은 **Linux와 Windows Terminal/WSL**입니다. 네이티브 Windows·macOS 지원을 선언하지 않습니다. 셸별 UI 검증 범위는 Bash 5.1.16·5.2.37·5.3.9, zsh 5.9, tcsh 6.24.13이며, 자세한 결과와 제한은 [검증 기록](docs/STREAM_VALIDATION.md)에 있습니다.

## 설치하고 실행하기

### 소스에서 실행

Linux/WSL에 uv와 사용하려는 셸을 설치한 뒤 저장소 루트에서 실행합니다. 프로젝트는 `.python-version`과 `uv.lock`의 Python·의존성을 사용합니다. Windows Python과 WSL Python의 가상환경은 공유하지 않습니다.

```sh
uv sync --locked
uv run --no-sync ish
uv run --no-sync ish --shell bash
uv run --no-sync ish --shell zsh --cwd "$HOME"
uv run --no-sync ish --shell tcsh
```

셸을 생략하면 계정의 로그인 셸, `SHELL`, `sh` 순으로 선택합니다. 셸 실행 파일은 별도로 설치되어 있어야 합니다. Windows Terminal에서는 WSL 프로필을 열고 위 Linux 명령을 실행합니다.

### 런타임을 포함한 배포본

이 브랜치에서 빌드한 배포 디렉터리에서는 `./ish`를 실행합니다. 호스트 Python은 필요하지 않습니다. 실행 파일 하나만 복사하지 말고 `bin/`을 포함한 배포 디렉터리 전체를 유지하세요.

```sh
cd /path/to/release
./ish --shell tcsh
./ish --help
```

직접 빌드하려면 x86_64 Linux/WSL에 uv를 준비합니다. 빌더는 고정된 CPython 런타임과 잠금 파일의 Python 패키지를 묶습니다.

```sh
sh tools/build.sh --output-dir "$HOME/ish-release"
```

빌드 출력은 대소문자를 구분하는 Linux 파일시스템에 두세요. 빌더가 출력하는 실제 경로에 `ish`, 런타임, 문서와 예제가 함께 들어갑니다. 배포본에는 셸 실행 파일이 포함되지 않습니다. 잠금 의존성·런타임·라이선스 아카이브를 미리 준비하지 않았다면 빌드 중 네트워크가 필요합니다. 사용할 호스트의 glibc와 호환되는 환경에서 빌드하고, 배포 디렉터리에 포함된 라이선스를 함께 보존하세요. 빌드 옵션은 `sh tools/build.sh --help`로 확인할 수 있습니다.

## 설정을 저장하기: `.ishrc.py`

ish의 설정 파일은 기본적으로 **`~/ish/.ishrc.py`**입니다. `--home DIR`을 사용하면 `DIR/.ishrc.py`를 읽습니다. 설정은 아래 `config` API를 사용합니다. 이전 버전의 `prompt` 객체용 설정 API나 자동 변환은 제공하지 않습니다.

필요한 경우 디렉터리와 파일을 직접 만듭니다. 파일이 없어도 기본형으로 실행할 수 있습니다.

```sh
mkdir -p "$HOME/ish"
```

`~/ish/.ishrc.py` 예:

```python
config.lang = None  # LANG 자동 감지. 한국어로 고정하려면 "ko"
config.shell = "bash"
config.log_level = "WARNING"
config.escape_delay = 0.05

# 아래 이름들을 선택하려면 먼저 다음 절의 플러그인 파일을 준비하세요.
# config.plugin_dir = "plugins"
# config.plugins = ["stream_bash", "stream_bar", "stream_popup"]
```

**파일에 플러그인 선택을 적어 두면 다음 실행에도 같은 선택이 적용됩니다.** 파일을 저장하는 것이 선택 저장이며, ish가 설정 파일을 덮어쓰거나 별도의 선택 DB를 만드는 방식은 아닙니다. 변경은 새 세션부터 적용됩니다. 실제 로딩·메타데이터·의존성 검사는 기존 `PluginManager`가 담당합니다.

| 설정 | 의미 |
| --- | --- |
| `config.lang` | ish 안내 언어. 예: `"ko"`, `"en"`. 기본 `None`은 `LANG` 자동 감지입니다. 셸·프로그램의 언어는 바꾸지 않습니다. |
| `config.shell` | 셸 이름 또는 실행 파일 경로. |
| `config.shell_args` | 셸 시작 인수 목록. 기본 `['-i']`를 대체합니다. |
| `config.cwd` | 셸 시작 디렉터리. |
| `config.plugins` | 순서가 있는 플러그인 이름 목록. 기본은 빈 목록입니다. |
| `config.plugin_dir` | 플러그인 원본 디렉터리. 기본 제공 디렉터리를 전체 대체합니다. |
| `config.plugin_lib_dir` | 플러그인 Python 패키지 설치 경로. 기본은 `<home>/plugin/lib`입니다. |
| `config.integration_dir` | 셸 연동 텍스트 파일의 대체 디렉터리. |
| `config.log_file` | 선택적인 파일 로그 경로. 기본 `None`은 파일 로그를 끕니다. |
| `config.log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. 기본 `WARNING`. |
| `config.escape_delay` | ESC와 Alt/제어 시퀀스를 구별하기 위한 대기 시간(초). 기본 `0.05`, 허용 범위 `0.005`–`2.0`. |

설정 파일의 상대 경로는 설정 파일이 있는 home 디렉터리를 기준으로 해석합니다. CLI의 상대 경로는 ish를 실행한 현재 디렉터리 기준입니다. CLI에 지정한 값은 해당 파일 설정을 덮어씁니다. 특히 `--plugin` 목록은 파일의 선택 목록을 **대체**하며 합치지 않습니다.

```sh
ish --no-rc                       # 설정 파일 전체를 읽지 않는 기본형
ish --no-plugins                  # 다른 설정은 적용하고 플러그인 선택만 해제
ish --home "$HOME/ish-work"      # 별도 설정 프로필
ish --shell tcsh                  # 이번 실행의 셸만 변경
```

`--no-plugins`와 `--plugin`은 함께 쓸 수 없습니다. `.ishrc.py`는 시작 시 실행하는 Python 파일입니다. 문법 오류나 잘못된 설정은 셸을 시작하기 전에 오류로 알리며, 일부 값만 적용한 세션을 시작하지 않습니다.

## 언어 설정과 언어팩

언어 선택 순서는 **명시한 설정 → `LANG` 자동 감지 → 영어 기본값**입니다. 명시한 값 중에서는 CLI의 `--lang`이 `.ishrc.py`의 `config.lang`보다 우선합니다. `config.lang = None`이거나 설정하지 않으면 `LANG=ko_KR.UTF-8` 등을 읽습니다. 다른 locale 환경변수(`LC_ALL`, `LC_MESSAGES`, `LANGUAGE`)는 ish 안내 언어를 덮어쓰지 않습니다. `LANG`이 없거나 비어 있거나 `C`·`POSIX` 또는 잘못된 값이면 영어를 사용합니다. OS에 해당 locale이 설치되어 있을 필요는 없습니다.

```sh
LANG=ko_KR.UTF-8 ish --no-rc       # LANG으로 한국어 자동 선택
ish --lang en                     # 이번 실행은 영어로 고정
LANG=ko_KR.UTF-8 ish --help        # 설정 코드를 실행하지 않고 한국어 도움말
```

영어·한국어는 기본 제공됩니다. `<home>/lang/<언어 코드>.json`에 UTF-8 JSON 객체를 저장하면 해당 문구를 바꿀 수 있습니다. 기본 home은 `~/ish`이며, 예를 들어 `~/ish/lang/ko.json`에 다음을 저장합니다.

```json
{
    "stream_description": "내 셸을 그대로 사용하는 ish"
}
```

영어 기본 문구, 사용자 `en.json`, 기본 제공 번역, 선택한 사용자 언어팩 순서로 덮어씁니다. 번역이 없는 키는 이전 단계의 문구를 유지합니다. 번역의 `{error}` 같은 자리표시자는 원문의 이름을 유지해야 합니다. 잘못된 파일이나 형식은 진단하고 사용할 수 있는 기본 문구로 대체합니다. 언어팩과 설정 변경은 새 세션부터 적용되며 파일을 자동으로 만들거나 덮어쓰지 않습니다.

언어팩은 ish가 작성한 도움말·오류·진단·로그와 제공하는 샘플 UI에 적용됩니다. 셸 출력, 외부 프로그램·라이브러리의 원래 오류 내용, 명령어·경로·프로토콜 필드는 그대로 보존합니다. 직접 작성한 플러그인의 문구도 공통 `ish.lang.i18n`을 사용해야 언어팩을 따릅니다.

조회 명령과 설정 파일을 읽기 전의 인수·TTY 오류는 rc를 실행하지 않으므로 `--lang` → `LANG` → 영어 순서로 표시합니다.

## 하단 바와 팝업 켜기

플러그인은 **표시할 UI**와 **셸 연동 provider**를 함께 선택합니다. 기본 제공 provider는 `stream_bash`, `stream_zsh`, `stream_tcsh`이고, 샘플 UI는 `examples/stream_plugins/stream_bar`와 `stream_popup`입니다. 플러그인 이름과 내부 `ish.stream` API 이름은 유지합니다.

저장소 루트에서 다음 명령으로 별도 사용자 디렉터리를 준비할 수 있습니다. 대상 `~/ish/plugins`가 아직 없을 때의 예입니다.

```sh
mkdir -p "$HOME/ish"
cp -R src/ish/stream/bundled "$HOME/ish/plugins"
cp -R examples/stream_plugins/stream_bar "$HOME/ish/plugins/"
cp -R examples/stream_plugins/stream_popup "$HOME/ish/plugins/"
```

배포본에서 provider 원본 경로를 찾는 방법과 직접 작성하는 방법은 [플러그인 안내](docs/STREAM_PLUGINS.md)를 참고하세요. `--plugin-dir`은 전체 대체 위치이므로 사용자 UI와 사용할 셸 provider를 같은 디렉터리에 둡니다.

이제 `~/ish/.ishrc.py`에 선택을 저장합니다.

```python
config.shell = "bash"
config.plugin_dir = "plugins"
config.plugins = ["stream_bash", "stream_bar", "stream_popup"]
```

`ish`를 다시 실행하면 primary 프롬프트 아래에 샘플 바가 표시됩니다. **F2**로 팝업을 열고, Enter로 값을 반환하거나 Esc·Ctrl+C·Ctrl+D로 닫습니다. 샘플 팝업의 결과는 셸 명령으로 실행하거나 명령줄에 삽입하지 않습니다.

- zsh는 `config.shell = "zsh"`와 `stream_zsh`, tcsh는 `"tcsh"`와 `stream_tcsh`를 함께 선택합니다.
- 바만 원하면 목록에서 `stream_popup`을 뺍니다. 이 경우 F2를 예약하지 않습니다.
- 팝업만 원하면 `stream_bar`를 뺍니다. 팝업용 F2는 계속 예약합니다.
- 셸 provider만 선택하고 UI를 선택하지 않으면 셸 hook도 설치하지 않습니다.

한 번만 선택하려면 파일을 바꾸지 않고 CLI를 사용할 수 있습니다.

```sh
ish --shell tcsh --plugin-dir "$HOME/ish/plugins" \
  --plugin stream_tcsh --plugin stream_bar --plugin stream_popup
```

팝업 안에서는 PTK 키 바인딩을 사용합니다. 예를 들어 Alt+B는 단어 왼쪽으로 이동하며, 사용자 팝업에서 `("escape", "p")` 바인딩으로 Alt+P를 등록할 수 있습니다. 단독 Esc는 Alt 입력과 같은 접두 바이트를 사용하므로 짧은 판별 대기가 필요합니다. 느린 원격 연결에서 Alt가 분리되어 도착하면 `config.escape_delay`를 늘릴 수 있습니다. 팝업 밖의 명령 편집은 셸의 키 바인딩을 사용합니다. UI 없는 기본형에는 ish의 ESC 판별 대기가 없습니다.

## 시작 옵션·언어·진단·로그

```sh
ish --lang ko --help
ish --version
ish --shell bash --diagnose
ish --shell bash --login
ish --shell bash --shell-arg=--noprofile --shell-arg=--norc --shell-arg=-i
ish --log-file --log-level INFO
ish --log-file "$HOME/ish/session.log" --log-level DEBUG
```

- `--help`, `--version`, `--diagnose`는 **설정 Python 코드·플러그인·셸을 실행하지 않고**, 설정·로그 파일도 만들지 않습니다. 진단은 CLI와 기본값을 보여주므로 `.ishrc.py` 적용 결과 확인용은 아닙니다. 조회 언어는 `--lang` → `LANG` → 영어 순서로 선택합니다.
- `--login`은 기본 `-i` 대신 `-l` 하나를 셸에 전달합니다. `--shell-arg`와 함께 쓸 수 없습니다. 명시한 `--shell-arg` 목록도 기본 `-i`를 대체합니다.
- 현재 UI provider는 일반 non-login interactive 시작을 지원합니다. 로그인이나 지원하지 않는 시작 인수를 선택하면 원래 인수로 UI 없이 셸을 시작합니다.
- `--home`은 ish 설정 경로만 바꾸고 셸의 사용자 `HOME`을 바꾸지 않습니다. `-l CODE`는 언어 옵션이며 로그인 옵션은 `--login`입니다.
- `--log-file`의 경로를 생략하면 `<home>/.ish.log`를 사용합니다. 파일 로그는 명시적으로 켠 경우만 생성합니다. native 입력·출력 내용은 수집하지 않습니다. 콘솔 진단은 셸 시작 전이나 터미널 복원 후에 표시합니다.

## 알아둘 범위

현재 UI는 **하단 5행의 예약 영역**을 사용합니다. 임의 위치 팝업 합성, native 프롬프트 옆의 우측 프롬프트 API, 셸 자동완성 후보와 플러그인 후보를 합치는 드롭다운은 아직 제공하지 않습니다. 셸의 기존 자동완성은 그대로 사용합니다.

Bash 팝업을 선택하면 지원하는 Readline 키맵의 F2가 전용 키가 됩니다. continuation과 `read -e`에서는 팝업 없이 소비하고, 외부 프로그램의 F2 입력은 전달합니다. 모든 사용자 키맵·hook 조합을 지원한다는 의미는 아닙니다.

플러그인·연동 파일 준비 실패는 셸 시작 전에 UI 없는 경로로 전환합니다. 실행 중 UI 오류는 장식을 해제하고 native 세션을 유지하지만, 이미 설치된 셸 hook과 키 바인딩은 세션 종료까지 남을 수 있습니다. 완전히 기본형으로 돌아가려면 종료 후 `ish --no-plugins`로 새 세션을 시작하세요.

이미지·클립보드·색상·폰트 등은 Windows Terminal, Kitty 등 **호스트 터미널이 지원하는 기능 범위**를 따릅니다. ish가 지원하지 않는 이미지 프로토콜을 호스트에 추가해 주는 것은 아닙니다. ish에는 libghostty 에뮬레이션 설정이 없습니다.

## 개발·검증

```sh
uv sync --locked --no-default-groups --group dev --python "$(cat .python-version)"
uv run --no-sync python tools/check_stream.py
uv run --no-sync python tools/check_stream_package.py
uv run --no-sync python tests/stream_matrix.py .cache/host-new-run --require-resize
```

회귀 검사는 Linux/WSL의 Bash·zsh·tcsh·Vim을 사용합니다. 마지막 명령은 실제 호스트 터미널에서 실행하며, resize 안내에서 창 크기를 바꾸고 조작을 마친 뒤 Enter를 누릅니다. 매번 새 결과 폴더를 사용하세요. 자동 검사와 실제 호스트 검증의 차이는 [검증 기록](docs/STREAM_VALIDATION.md)에 구분합니다.

- [Python 플러그인 API와 실패 처리](docs/STREAM_PLUGINS.md)
- [직접 전달 구조와 셸 연동 계약](docs/PROMPT_STREAM.md)
- [별도 ish-vt 실험](docs/TERMINAL_EXPERIMENT.md): libghostty-vt 기반 에뮬레이션과 별도 플러그인 API
- [ish-vt 설정과 클립보드·마우스 정책](docs/VT_USAGE.md), [기능별 켜기·끄기·지원 질의](docs/VT_FEATURE_POLICY.md), [단일 경로의 출력·키·클립보드 보완 모듈](docs/VT_PROTOCOL_PLUGINS.md), [기본 검증 범위](docs/VT_BASELINE_RESULT.md), [보완 모듈 검증](docs/VT_SUPPLEMENTS_RESULT.md), [terminfo 대조](docs/VT_TERMINFO_RESULT.md), [종료·중지 복원](docs/VT_RESTORATION.md)

이전 실험명 `ish-stream`이 **ish 2.0.0**입니다. 배포 패키지와 Python 모듈 이름 `ish`·`ish.stream`, 개발 검사 파일의 `stream` 이름은 유지합니다. 이전 버전용 실행 명령이나 설정 호환 기능은 제공하지 않습니다.
