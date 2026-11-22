# ish 플러그인 개발

하단 바 하나를 만드는 예제로 시작한다. 셸이 명령을 편집하는 동안 ish가 화면 아래 한 줄을 플러그인에 할당한다.

## 하단 바 작성

기본 플러그인 폴더 아래에 `~/ish/plugin/script/my_bar/my_bar.py`를 만든다. 패키지 이름과 파일 이름을 맞추거나 같은 디렉터리의 `__init__.py`를 진입점으로 쓸 수 있다.

```python
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl

PLUGIN_META = {"version": "1.0.0", "category": "plugin"}
TERMINAL_API = 1


def setup(api):
    api.set_bar(Window(FormattedTextControl("my_bar")), rows=1)
```

설정창은 `PLUGIN_META`의 리터럴 값을 읽어 패키지를 분류한다. ish는 선택한 패키지를 세션 시작 때 불러와 `TERMINAL_API`를 확인하고 `setup(api)`를 호출한다. 세션 종료 때 자원을 정리해야 하면 `setup`에서 정리 함수를 반환한다.

## 실행하고 선택 저장하기

1. `ish --plugin my_bar`를 실행한다. 셸 아래 한 줄에 `my_bar`가 보이면 등록과 렌더링을 함께 확인한 것이다.
2. 다음 실행에도 사용하려면 `ish -c`를 열고 **확장 → 플러그인**에서 `my_bar`를 선택한 뒤 **저장**을 누른다. 다른 폴더를 쓴다면 이 페이지에서 플러그인 경로를 패키지들의 상위 디렉터리로 지정한다.
3. 새 `ish` 세션에서 저장한 하단 바를 확인한다. `ish --no-plugins`로 실행하면 기본 셸 화면을 확인할 수 있다.

코드를 편집한 뒤에는 새 세션에서 다시 실행한다. 이미 열린 설정창에서 새 패키지를 찾으려면 현재 변경을 저장한 뒤 Ctrl+R로 목록을 다시 읽는다. 파일에서 패키지를 선택하는 방법은 [설정 안내](VT_USAGE.md)에 있다.

## 설정창에 옵션 공개하기

플러그인과 보완 모듈은 기존 진입 파일의 리터럴 `PLUGIN_META["settings"]`에 옵션을 선언한다. 옵션 키는 Python 식별자이며, 각 항목에는 `title`, `description`, `default`가 필요하다. 제목과 설명에는 영어 문자열 또는 `en`을 포함하는 언어별 사전을 쓴다. 기본값으로 한 줄 문자열·정수·불리언을 쓸 수 있다. 선택 목록을 제공하려면 `choices`에 기본값을 포함한 같은 타입의 값들을 넣는다.

```python
PLUGIN_META = {
    "version": "1.0.0",
    "settings": {
        "label": {
            "title": {"en": "Bar text", "ko": "하단 바 문구"},
            "description": {"en": "Text shown below the shell.", "ko": "셸 아래에 표시할 문구입니다."},
            "default": "ish",
        }
    },
}
```

설정창은 설치 폴더를 읽어 **설치된 확장 → my_bar**에 이 항목을 표시한다. 사용자가 저장한 값은 `~/ish/.ishrc.py`의 `config.plugin_settings`에 패키지별로 기록된다. 예를 들어 `config.plugin_settings = {"my_bar": {"label": "작업 중"}}`으로 직접 설정해도 된다. 패키지의 `setup(api)`에서는 `api.settings_for("my_bar")["label"]`로 검증된 값이나 선언한 기본값을 읽는다. 패키지 활성화는 별도로 선택하며, 저장한 값은 다음 세션에 적용된다.

## 팝업과 단축키

팝업은 PTK 컨테이너로 작성한다. `api.bind("f2")`로 여는 키를 등록하고, `api.show_popup(...)`이 반환한 팝업을 `api.close_popup(...)`에 넘겨 닫는다. 팝업 안의 Enter·Esc 처리는 팝업 위젯에 둔다. [F2 팝업 예제](../examples/terminal_plugins/terminal_popup/terminal_popup.py)

등록한 단축키는 Vim 같은 프로그램을 사용하는 중에도 실행될 수 있다. 대체 화면과 팝업에서는 키를 원래 프로그램이나 위젯에 맡기려면 `api.bind(..., primary_only=True)`를 쓴다. 터미널의 기본 화면에 포커스가 있는지를 기준으로 한다. [스크롤백 단축키 예제](../examples/terminal_plugins/terminal_history/__init__.py)

## 보완 모듈 개발

`PLUGIN_META`의 `category`를 `"module"`로 지정하면 **확장 → 모듈**에 표시된다. `"both"`는 양쪽 페이지에 표시되고, 생략하면 **플러그인**에 표시된다. 세 경우 모두 플러그인 경로와 `config.plugins` 선택 목록을 공유한다.

출력 프로토콜·호스트 키 매핑·클립보드 제공자는 같은 `setup(api)`에서 각 기능의 등록 메서드를 호출한다. 메서드별 입력과 수명, 실제 예제는 [보완 모듈 API](VT_PROTOCOL_PLUGINS.md)와 [설정창 분류별 예제 목록](../README.md#플러그인과-보완-모듈)에 있다.
