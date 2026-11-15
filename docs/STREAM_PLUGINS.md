# ish Python 플러그인

ish는 기존 ish의 `PluginManager`를 재사용해 선택한 외부 Python 파일을
불러온다. 하단 바·팝업 내용과 셸별 시작 정책을 플러그인으로 분리했으며,
명령줄·보조 프롬프트 편집은 계속 셸이 담당한다. 별도 플러그인 관리자를
새로 만들거나 Python 명령 dispatcher를 복원한 것은 아니다.

플러그인은 UI 내용과 셸 연동을 등록한다. 본체는 PTY, 단일 터미널 writer,
입력 큐, 팝업 포커스, 요청 identity와 acknowledgement 검증, 종료 복원을
소유한다. 플러그인이 직접 외부 터미널에 출력하는 API는 제공하지 않는다.

## 실행 방법

Linux/WSL의 대화형 터미널에서, ish가 설치된 Python 환경의 `ish`를 사용한다.
저장소에서 실행할 때는 같은 환경에서 `PYTHONPATH=src python -m ish.stream`으로
대체할 수 있다.

```sh
# 기본형: 추가 UI, UI 단축키, 셸 hook 없이 출력과 입력을 직접 전달한다.
ish --no-rc --shell tcsh

# 저장소 루트에서, 아직 없는 사용자 디렉터리에 셸 provider와 UI 예제를 모은다.
cp -R src/ish/stream/bundled "$HOME/ish-plugins"
cp -R examples/stream_plugins/stream_bar "$HOME/ish-plugins/"
cp -R examples/stream_plugins/stream_popup "$HOME/ish-plugins/"

# 샘플 하단 바·팝업과 tcsh 연동을 명시적으로 선택한다.
ish --shell tcsh --plugin-dir "$HOME/ish-plugins" \
  --plugin stream_bar --plugin stream_popup --plugin stream_tcsh

# 하단 바만 사용한다. 팝업용 F2 예약은 설치하지 않는다.
ish --shell tcsh --plugin-dir "$HOME/ish-plugins" \
  --plugin stream_bar --plugin stream_tcsh

# 팝업만 사용한다.
ish --shell zsh --plugin-dir "$HOME/ish-plugins" \
  --plugin stream_popup --plugin stream_zsh
```

`--plugin NAME`은 여러 번 지정할 수 있다. 파일 설정의 플러그인 목록을 대체한다.
선택한 순서를 유지하며 같은 이름은
한 번만 활성화한다. 등록된 의존 플러그인은 같은 source 디렉터리에서 불러오지만,
선택하지 않은 모듈의 stream `setup()`을 자동 실행하지는 않는다.

| 옵션 | 의미 |
| --- | --- |
| `--plugin NAME` | 이번 실행에 사용할 Python 플러그인 목록을 지정한다. 파일에도 선택이 없으면 기본 선택은 없다. |
| `--no-plugins` | 다른 설정은 적용하고 플러그인 선택만 무시한다. `--plugin`과 함께 쓸 수 없다. |
| `--plugin-dir DIR` | 기본 제공 셸 플러그인 디렉터리를 **전체 대체**한다. 누락된 이름을 기본 디렉터리에서 보충하지 않는다. |
| `--plugin-lib-dir DIR` | 플러그인 Python 패키지를 확인·설치할 별도 디렉터리다. 생략하면 ish 설정의 plugin library 경로를 쓴다. |
| `--integration-dir DIR` | 셸 플러그인이 사용하는 수정 가능한 `.sh`·`.zsh`·`.csh` 템플릿 디렉터리를 대체한다. |

제품에 `--demo` 옵션은 없다. UI 예제는 `examples/stream_plugins/`에 있으며,
기본 UI로 활성화하지 않는다. `--home`으로 변경한 설정 경로는 `.ishrc.py`와
기본 plugin library 경로에도 적용한다. 언어·로그·시작 인수와 읽기 전용 진단은
[사용 안내](../README.md)를 참고한다.

### 선택 저장과 설치본에서 복사

위 예제처럼 파일을 준비했다면 `~/ish/.ishrc.py`에 다음을 적어 선택을 저장한다.

```python
config.shell = "tcsh"
config.plugin_dir = "~/ish-plugins"
config.plugins = ["stream_tcsh", "stream_bar", "stream_popup"]
```

이후 `ish`를 실행하면 기존 `PluginManager`가 이 이름들을 로딩한다. 별도의
`plugin.load()` API나 선택 DB를 만들지 않는다. 파일을 바꾸면 다음 세션부터
적용되고, 실행 중 플러그인을 교체하지 않는다. `--no-rc`는 파일 전체를 무시한다.
`--no-plugins`는 언어·로그 등 다른 설정을 유지하면서 UI와 셸 연동을 해제한다.
설정 파일에 쓴 상대 경로는 설정 home을 기준으로 한다.

배포본에는 `src/` 디렉터리 대신 설치된 Python 패키지에 provider 원본이 들어
있다. 배포 디렉터리에서 다음처럼 경로를 확인하고 복사할 수 있다. 대상 사용자
디렉터리가 아직 없을 때 실행한다.

```sh
provider_dir=$(./bin/python/bin/python3 -I -c 'from ish.stream.extensions import DIRECTORY; print(DIRECTORY)')
cp -R "$provider_dir" "$HOME/ish-plugins"
cp -R examples/stream_plugins/stream_bar "$HOME/ish-plugins/"
cp -R examples/stream_plugins/stream_popup "$HOME/ish-plugins/"
```

wheel 설치 환경에서는 같은 Python의 `python -I -c ...`로 provider 경로를
확인한다. 샘플 UI는 이 저장소의 `examples/stream_plugins/`에서 가져온다.
`ish --diagnose`도 기본 provider 경로를 표시하지만 `.ishrc.py`를 실행하지
않으므로 파일 설정을 적용한 결과는 표시하지 않는다.

UI를 등록하는 플러그인과 현재 셸을 지원하는 플러그인이 함께 있어야 장식을
사용할 수 있다. 셸 플러그인만 선택하고 UI를 등록하지 않으면 셸 hook도 설치하지
않는다. 하단 바만 사용하면 프롬프트 표시/숨김 연동은 설치하지만 팝업용 키와
socket은 설치하지 않는다. F2는 기존 셸 설정대로 전달된다.

샘플 `stream_bar`와 별도의 하단 바 플러그인을 함께 선택하면 두 하단 바가 중복
등록되어 준비 단계에서 거부된다. 자신의 UI로 바꾸려면 샘플 대신 자신의 이름을
선택한다. 팝업도 한 provider만 등록할 수 있다.

## 외부 파일을 수정해서 사용하기

위에서 만든 사용자 디렉터리에 새 하단 바 플러그인을 추가한다. UI 예제를 쓰지
않을 경우에는 셸 provider 디렉터리만 복사해도 된다.

```sh
mkdir -p "$HOME/ish-plugins/my_bar"
```

`$HOME/ish-plugins/my_bar/my_bar.py`를 다음 내용으로 만든다.

```python
PLUGIN_META = {
    "version": "1.0.0",
    "description": "My prompt-time bar",
    "requirements": [],
    "dependencies": [],
}
STREAM_API = 1
BAR_TEXT = " My ish session "


def setup(api):
    api.set_bar([("fg:ansicyan", BAR_TEXT)])
```

복사해 둔 tcsh 플러그인과 함께 선택한다.

```sh
ish --shell tcsh \
  --plugin-dir "$HOME/ish-plugins" \
  --plugin my_bar --plugin stream_tcsh
```

`NAME/NAME.py` 또는 `NAME/__init__.py`가 entry가 된다. 둘 다 있으면 `NAME.py`를
사용한다. `PLUGIN_META`는 기존 관리자와 같은 literal 사전이며, `requirements`는
다른 ish 플러그인, `dependencies`는 Python 패키지 요구사항이다. 자세한 기존
metadata는 import 전에 읽으므로 함수 호출이나 변수 참조로 구성하지 않는다.
`requirements`에 이름과 버전 조건을 쓰면 같은 source 디렉터리의 의존 플러그인을
검사한다. 플러그인 원본 자체를 다운로드하지는 않는다. `dependencies`에는
`requests>=2.31` 같은 Python 배포 요구사항을 쓴다. 배포명과 import 이름이
다르면 `PyYAML>=6|yaml`처럼 지정할 수 있다. 필요한 Python 패키지는 선택한
library 디렉터리에 pip로 설치하므로 패키지 접근 경로나 오프라인 wheel이 필요하다.
구버전 Prompt 설정 API를 위한 호환 계층은 제공하지 않는다.

수정 내용은 **다음 ish 세션부터** 적용된다. 실행 중인 플러그인이나
셸 hook을 실시간 교체하는 기능은 없다. entry 파일은 원본 bytes를 읽으므로
동일한 크기·mtime의 수정에서도 오래된 entry bytecode를 재사용하지 않는다.
entry가 import하는 보조 모듈은 일반 Python import 규칙을 따른다.

## stream 등록 API

외부 모듈은 정수 `STREAM_API = 1`과 `setup(api)`를 제공해야 한다. 다음 등록은
setup 중에만 허용된다. setup이 반환한 뒤에는 등록 구성을 변경할 수 없다.

| API | 계약 |
| --- | --- |
| `api.set_bar(content)` | PTK formatted text 또는 이를 반환하는 함수를 등록한다. 한 개만 허용한다. 함수는 렌더링할 때 호출되며 별도 주기 갱신을 예약하지 않는다. |
| `api.set_popup(factory)` | 팝업을 열 때마다 새 `PopupView`를 만드는 함수를 등록한다. 한 개만 허용한다. |
| `api.add_shell(matches, build)` | 셸 선택 함수와 시작 환경 builder를 등록한다. 현재 셸에 정확히 한 provider가 맞아야 한다. |
| `api.get(name)` | 기존 PluginManager registry에서 이미 불러온 모듈을 얻는다. 임의 파일을 새로 실행하는 함수가 아니다. |

팝업 factory는 본체의 `accept(result)` callback을 인수로 받고
`ish.stream.ui.PopupView(content, focus)`를 반환한다. `content`는 PTK container,
`focus`는 초기 포커스를 받을 위젯이다. `accept(text)` 또는 `accept(None)`으로
팝업을 닫을 수 있다. 결과를 셸 명령으로 실행하거나 native 명령 버퍼에 삽입하지
않는다. 간단한 TextArea 예제는
[popup 예제](../examples/stream_plugins/stream_popup/stream_popup.py)를
참고한다. 본체가 실제 native callback·요청 일치·복귀 acknowledgement를 확인한
경계에서만 팝업 입력을 전달한다.

팝업 위젯은 PTK의 키 바인딩을 사용한다. `KeyBindings.add("escape", "p")`는
Alt+P를 표현한다. Esc와 Alt는 같은 ESC 바이트 접두를 쓰므로 단독 Esc 판별에
짧은 대기가 필요하다. ish는 `config.escape_delay`로 판별 시간을 선택하며,
기본값은 0.05초다. ESC를 eager binding으로 등록하면 Alt 시퀀스가 끊길 수
있으므로 같은 팝업에서 Alt 키를 쓰려면 접두 바인딩을 고려한다. 이 설정은
셸 readiness 판단용 timeout이 아니며 UI 없는 기본형에는 적용하지 않는다.

`setup(api)`는 정리 함수 또는 `None`을 반환한다. 정리 함수는 종료 시 등록의
역순으로 호출되며 awaitable 반환도 지원한다. 위젯·키 입력·터미널 출력의 수명은
본체가 관리한다. 플러그인이 별도로 만든 작업이나 리소스는 반환한 정리 함수에서
해제한다. API 정의는 [extensions.py](../src/ish/stream/extensions.py),
[PopupView](../src/ish/stream/ui.py)에 있다.

셸 플러그인의 `matches(argv)`는 실행 인자 tuple을 받아 지원 여부를 반환한다.
`build(argv, env, directory, nonce, popup_socket, *, script_dir=None)`는 임시 시작
파일을 준비하고 `(child_argv, child_env)`를 반환한다. 셸별 정책·quoting·hook은
해당 플러그인과 외부 셸 템플릿에 둔다. 기본 구현은
[tcsh](../src/ish/stream/bundled/stream_tcsh/stream_tcsh.py),
[Bash](../src/ish/stream/bundled/stream_bash/stream_bash.py),
[zsh](../src/ish/stream/bundled/stream_zsh/stream_zsh.py)를 참고한다.

현재 세 provider는 non-login interactive 시작만 지원한다. `--login`이나 별도의
`--shell-arg`가 그 범위를 벗어나면, UI를 준비하는 단계에서 거부하고 원래 인수를
그대로 보존한 native 경로로 시작한다. 일반 실행에 필요한 셸 인수의 의미는 셸이
판단하며, Python에서 입력 구문을 분석하거나 다른 시작 방식으로 대체하지 않는다.

프롬프트 알림은 화면 문자열을 읽어 추정하는 기능이 아니다. 실제 셸 hook이
공통 마커를 보내고 본체가 세션·순서·팝업 요청을 검증한다. 셸 템플릿의
`# ish-stream-api: 1`과 placeholder, acknowledgement 순서는
[연동 계약](../src/ish/stream/integrations/README.md)을 유지해야 한다.

## 현재 UI 범위와 실패 처리

- primary 프롬프트 대기 중에만 장식을 표시한다. continuation·heredoc·프로그램
  stdin은 native 셸/프로그램이 처리한다.
- 장식은 현재 하단 **5행 예약 영역** 안에 표시된다. 팝업도 그 영역을 사용한다.
  화면 임의 위치의 범용 overlay나 native 프롬프트 옆 우측 prompt API는 없다.
- 창이 너무 작거나 안전한 표시 경계를 확인하지 못하면 장식을 숨긴다. resize
  직후에는 새 primary 알림을 확인할 때까지 장식을 다시 표시하지 않는다.
- native 명령 버퍼·커서·자동완성 후보를 읽거나 수정하는 API는 없다. Python 도구
  실행이나 텍스트 기반 내부 명령 dispatch도 제공하지 않는다.
- 셸 시작 전 plugin 로딩·등록·연동 준비가 실패하면 정리 후 UI 없이 셸을 시작한다.
  실제 셸이 실행한 사용자 시작 스크립트의 임의 변경까지 되돌리는 것은 아니다.
- 실행 중 지원되는 UI callback에서 예외가 나면 팝업을 닫고 장식을 해제한다.
  PTK의 비동기 키 binding과 `Application.create_background_task()`로 등록한
  작업도 같은 오류 경계를 사용한다. 정상 task 취소는 UI 오류가 아니며, 닫힌
  팝업에서 늦게 실패한 작업과 그 자식 작업은 새 팝업을 해제하지 않는다.
  factory는 해당 PTK Application 문맥에서 실행되고, 그 안에서 등록한 작업도
  포커스를 얻기 전부터 해당 팝업에 속한다. factory 생성 자체가 실패해도 남은
  작업이나 뒤늦게 생성한 자식 작업이 다음 팝업을 해제하지 않는다.
  패널 종료가 시작된 뒤 등록되는 후속 PTK 작업은 실행하지 않고 취소·정리한다.
  플러그인이 별도로 만든 asyncio task 전체를 가로채는 기능은 아니다.
  팝업 입력을 명령으로 보내지 않으며 native 세션은 유지한다. **이미 설치한 셸
  hook·키 바인딩은 그 셸 세션이 끝날 때까지 남는다.** 이 상태를 hook 없는 기본형과
  동일하다고 부르지 않는다. 완전히 기본형으로 돌아가려면 종료 후 플러그인 없이
  새로 실행한다.

## 불완전한 Python 패키지 설치 검증

기존 검사에서는 package metadata와 최상위 import가 남아 있으면 내부 `.py`,
data 파일, native 파일이 사라져도 사용 가능하다고 판정했다. extras 없는 일반
요구사항에서는 전이 의존성 누락도 놓쳤다. 원본 구현을 별도 복사해 재현했으며,
그 결과 필요한 복구가 실행되지 않거나 불완전한 새 설치가 승인되는 경우를 확인했다.

수정본은 활성 `Requires-Dist` 트리를 검사하고, 원본 RECORD CSV에 적힌 library
파일의 존재·크기·hash를 확인한다. CPython 3.12의 `Distribution.files`는 누락 파일을
목록에서 제외하므로 그 목록을 완전성 검사에 사용하지 않는다. 관리형 설치에서는
같은 `.dist-info`의 `METADATA`·`WHEEL`·`RECORD` 항목도 필수로 확인한다.

검증 실패 후에는 기존 staging·파일 소유권 기반 교체·검증 실패 시 rollback을
사용한다. 플러그인이 실행되기 전에 실제 data 파일이 복구되는 결과, 잘못된 설치
후 원래 파일과 다른 패키지가 보존되는 결과를 네트워크 없는 fixture로 검증했다.
테스트에서 pip subprocess는 결정적인 stage producer로 대체했고, 파일 교체와
검증·rollback은 실제 구현을 실행했다.

이 검사는 배포 원본의 진위나 모든 실행 가능성을 증명하지 않는다.

- RECORD에서 payload 항목 자체를 지우고 manifest를 재작성한 경우, 별도 원본
  manifest 없이 누락된 항목 전체를 알아낼 수 없다.
- native 파일의 bytes가 맞아도 외부 동적 라이브러리나 ABI 호환까지 보장하지 않는다.
- 외부 legacy/egg-info·editable 설치에 RECORD가 없으면 기존 metadata/import
  검증 범위를 유지한다. ish가 관리하는 library 설치에서 RECORD가 없으면 거부한다.
- library root 밖의 console-script 등은 hash하지 않는다. 내부 payload 경로가
  symlink로 외부를 가리키는 경우는 거부한다. library root 자체의 symlink는 지원한다.
- 재생성 가능한 bytecode는 필수 payload로 취급하지 않는다. 검사 후 파일이 외부에서
  바뀌는 상황까지 잠그는 기능은 아니다.
- Python 배포 metadata 없는 import-only 이름은 패키지 설치가 정상이라는 근거로
  사용하지 않는다. `dependencies`에는 실제 Python 배포 요구사항을 적는다.

## 검증과 직접 확인

아래 수치는 **플러그인 분리 단계의 이전 검증 기록**이다. 이후 시작 옵션·언어·로그
연결과 샘플 정리의 최신 결과는 [STREAM_VALIDATION.md](STREAM_VALIDATION.md)에
구분하여 기록한다. 이전의 제품 `--demo` 명령은 현재의 명시적 플러그인 선택과
같은 CLI 문법이 아니다.

플러그인 분리 당시 Linux/WSL 기본 회귀는 stream **185개**, plugin loader **24개**, Session
**4개**, tcsh 자동완성 관찰 **18개**, 합계 **231개**가 통과했다. 별도로 Bash
5.1.16과 5.2.37에서 각각 **40개**를 실행했다. 새 테스트가 포함된 전체 합계이므로
새 테스트 수를 다시 더하지 않는다. 세부 범위는
[STREAM_VALIDATION.md](STREAM_VALIDATION.md)를 참고한다.

당시 실제 Windows Terminal/WSL에서는 tcsh·Bash·zsh 각각 플러그인 경로와 기본형,
총 6개를 확인했고 종료 후 TTY 복원도 6개 모두 확인했다. 호스트 응답과 실제 파일
결과를 사용했으며 tcsh 플러그인 경로에서 120×30→121×31 resize도 확인했다.
GUI capture 도구가 차단되어 픽셀 스크린샷 검증은 수행하지 못했다. 이를 화면
이미지를 확인한 결과로 해석하지 않는다. 해당 증거는
`.cache/plugin-split/host-matrix/summary.json` 및 같은 디렉터리에 보관한다.

직접 확인할 때는 위 기본형과 샘플을 명시적으로 선택한 명령을 각각 실행한다. primary에서 F2로
팝업을 열고 Enter/Esc/Ctrl+C로 닫은 뒤 native 입력이 이어지는지 확인한다.
창 크기를 변경하고 새 primary로 돌아와 바가 다시 표시되는지 확인한다.
tcsh에서는 아래 입력을 한 줄씩 제출해 보조 프롬프트와 파일 결과를 확인할 수 있다.

```tcsh
foreach item (one two)
echo $item >> /tmp/ish-loop-result
end
cat << EOF > /tmp/ish-heredoc-result
space name
한글 본문
EOF
cat /tmp/ish-loop-result /tmp/ish-heredoc-result
vim
```

Vim에서는 실제 파일을 편집·저장한 뒤 종료한다. 기본형에서는 F2를 포함한 키가
기존 셸/프로그램 동작대로 전달되고 추가 영역이 생기지 않아야 한다. 종료 후
원래 터미널의 입력·echo·창 크기 처리가 복원되는지도 확인한다.
