# 선택 확장: 알림과 명령 출력 탐색

이 문서는 정식 VT 기반 `ish` 2.0.0의 선택 확장을 설명한다. 기본형에는 하단 바·팝업·전용 단축키·셸 hook을 추가하지 않는다. 명령줄, continuation, heredoc과 프로그램 stdin은 계속 native 셸과 PTY가 담당한다.

## 데스크톱 알림

기본값은 `off`다. 호스트로 알림을 보내려면 다음과 같이 명시한다.

```sh
ish --feature desktop_notifications=on
```

설정 파일에서는 다른 기능 설정과 함께 선택할 수 있다.

```python
config.features = {"desktop_notifications": "on"}
```

`on`은 알림 전송을 허용하는 정책이다. 호스트가 알림을 실제로 표시한다는 검증 결과가 아니다. 실제 표시 여부는 본체 터미널의 OSC 9 지원, OS 알림 설정과 사용자 정책에 달려 있다. 이 기능에는 호스트 지원 질의를 구현하지 않았으므로 `auto`를 제공하지 않는다. 터미널 이름에 따른 허용 목록도 사용하지 않는다.

| 입력 | ish 처리 범위 | 소유자 |
| --- | --- | --- |
| OSC 9 | 단순 알림 본문 | libghostty desktop notification callback |
| OSC 777 `notify` | 제목과 본문 | 같은 native callback |
| OSC 99 | 제목·본문의 분할 전송, UTF-8/base64, 식별자가 있는 지원 질의 | `notifications.Notifications` 보완 모듈 |

OSC 99 지원 질의는 요청의 검증된 `i`를 그대로 돌려주며 `p=title,body:o=always`만 광고한다. 이는 ish 입력 처리 범위의 응답이며, 본체 터미널의 알림 표시를 조회한 결과가 아니다. `off`에서는 알림과 질의 응답 모두 보내지 않는다. 활성화 보고, 닫힘 보고, 기존 알림 교체·닫기, 아이콘·버튼·소리·긴급도·만료·포커스 제어는 구현하거나 광고하지 않는다. 같은 ID로 완료된 알림을 다시 보내면 기존 알림의 교체를 보장하지 않으며 새 단순 알림으로 취급한다. 전체 OSC 99 구현이라고 부르지 않는다.

원본 escape code는 호스트로 전달하지 않는다. 제목·본문에서 C0/C1 제어문자를 제거하고 줄바꿈·탭은 공백으로 바꾼 후 단순 OSC 9로 직렬화한다. `ish: ` 접두사는 `4;...`·`9;...` 같은 본문이 호스트의 진행 상태·작업 디렉터리 하위 명령으로 해석되는 것을 막는다. HostBroker가 전송 정책을 집행하고 기존 renderer/writer 하나가 출력한다. native OSC 9/777과 보완 OSC 99가 한 PTY read에 섞여도 처리 순서를 보존한다.

한도는 다음과 같다. 알림 내용은 로그에 기록하지 않는다.

- 제목·본문 합계는 알림당 UTF-8 16 KiB다. 초과한 알림을 조각내서 표시하지 않는다.
- OSC 99 한 조각은 일반 입력 2,048 bytes, base64 입력 4,096 bytes다. 미완료 알림은 최대 16개, 알림당 최대 64조각이다.
- 메타데이터는 1,024 bytes, 응답에 사용하는 ID는 128 bytes 이하로 제한한다. ID는 영문·숫자·`_+.-`만 허용한다.
- 미완료 알림 저장 한도를 넘거나 해석할 수 없는 메타데이터를 받으면 해당 보완 모듈은 취소·reset까지 새 알림 조합을 중지한다. 오래된 조각을 버리고 뒷부분만 알림으로 표시하지 않는다.
- native 콜백은 한 번의 event drain당 최대 64개를 보관한다. Application은 렌더 프레임당 최대 64개를 기존 출력 큐에 넣고 나머지는 버린 수만 진단에 남긴다. 이 두 제한은 각각 native buffer와 화면 출력 큐의 별도 한도다.

## OSC 133 기반 탐색과 복사

`terminal_commands`는 명시적으로 선택하는 예제 플러그인이다. 저장소의 예제를 직접 사용하려면 플러그인 디렉터리를 지정한다.

```sh
ish --plugin-dir /path/to/ish/bundles/plugins --plugin terminal_commands
```

| 키 | 동작 |
| --- | --- |
| F6 | 이전 native prompt marker로 스크롤 |
| F7 | 다음 native prompt marker로 스크롤 |
| F8 | 마지막으로 표시된 명령 출력 범위 선택 |
| F9 | 같은 범위를 선택하고 기존 ClipboardBroker로 복사 |

플러그인을 선택하지 않으면 이 키들을 예약하지 않는다. 선택한 동안에는 키를 플러그인에서 소비한다. 마커가 없거나 유효한 출력 범위가 없으면 탐색·선택·복사는 아무 결과를 만들지 않는다. 복사는 기존 클립보드 정책과 provider를 따르며 호스트 클립보드 접근 제한을 우회하지 않는다.

이 기능은 셸/프로그램이 보내는 OSC 133 의미 마커를 사용한다. ish는 이를 위해 `PROMPT_COMMAND`, `precmd`, ZLE 등을 설치하거나 셸 rc 파일을 수정하지 않는다. 이미 마커를 보내는 환경이나 명시적으로 마커를 출력하는 프로그램에서만 사용할 수 있다. 화면의 `$`, `%`, 명령 문자열을 읽어 마커를 추정하지 않는다. 마커는 입력 권한이나 셸 readiness 판단에도 사용하지 않는다.

프롬프트 이동은 `ghostty_terminal_scroll_viewport`의 `DELTA_PROMPT`, 출력 선택은 `ghostty_terminal_select_output`을 사용한다. 마지막 출력 위치는 native tracked grid ref 하나만 보관한다. 별도의 Python 명령 목록·출력 범위·history 모델은 만들지 않는다. native ref가 resize/reflow를 따라가고, 사라진 history와 RIS reset에서는 선택하지 않는다. primary의 anchor를 alternate screen 내용에 적용하지 않는다.

F8/F9의 대상은 **명령 출력**이다. 프롬프트와 입력 명령 자체를 포함한 명령 블록 전체나 이미지를 복사하는 기능은 아니다. 완료 전 출력도 현재 남아 있는 native 범위에서 선택할 수 있다. 복사는 native formatter의 Unicode·줄바꿈 규칙을 사용한다. 임의의 화면 덮어쓰기 이후 의미 범위의 해석은 libghostty에 맡긴다.

## 검증 범위

`tests/test_terminal_optional.py`에서 독립된 wire 기대값, 실제 libghostty, 실제 PTK 입력·renderer, 실제 controlling PTY와 파일 결과를 검증한다. 알림 혼합 순서·bounded payload·제어문자/ID 방어·기본 off·query correlation·기존 writer 경유·프레임 제한, native 출력 선택·한글·빈 출력·reflow·alternate·history 삭제·RIS·실제 F8/F9와 클립보드 provider, 일반 입력의 최종 파일 결과를 포함한다.

이 자동 검사는 OS 알림 풍선이 실제로 표시되었다는 증거가 아니다. Windows Terminal/WSL의 실제 화면 결과는 전체 호스트 검증 기록과 함께 별도로 보고한다.

참고: [Kitty OSC 99 명세](https://sw.kovidgoyal.net/kitty/desktop-notifications/), [Windows Terminal shell integration](https://learn.microsoft.com/en-us/windows/terminal/tutorials/shell-integration), [Windows Terminal OSC 9;9 작업 디렉터리](https://learn.microsoft.com/en-us/windows/terminal/tutorials/new-tab-same-directory). native API의 기준은 빌드에 사용한 libghostty-vt 1.0.1 공개 헤더다.
