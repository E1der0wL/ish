# 가상 터미널 기능과 검증 범위

> 통합 전 구현 기록이다. 현재 제품은 VT 기반 `ish` 하나이며 아래의 별도 실행기·이전 플러그인 API는 현재 사용법이 아니다. 현재 실행·설정 방법은 [사용 안내](../VT_USAGE.md)를 참고한다.

2026-11-15, `experiment/ghostty-ptk`의 `ish-vt` 기준이다. 기본 실행에는 추가 UI나 플러그인 단축키가 없다. 명령줄·continuation·heredoc은 native 셸이 편집하며, 기존 `ish` 진입점과 `refactor/editor-request` 경로는 변경하지 않는다.

## 책임과 재사용

```text
host 입력 → HostInput / HostBroker → 한 PTK Application
                                  ├─ terminal 포커스 → Session 쓰기 큐 → PTY
                                  └─ popup 포커스 → PTK 위젯

PTY 출력 → InputPipeline / ProtocolStream
           ├─ 선택한 native 입력 → libghostty-vt → Frame / Image / Effect
           ├─ 선택한 보완 입력 → 같은 native image store
           └─ clipboard broker → 정규화한 효과 큐
native snapshot + 효과 큐 → PTK layout / TerminalRenderer / Presenter → host
```

| 사실·계약 | 변경 책임 | 재사용한 구현 |
| --- | --- | --- |
| 셸·프로그램 stdin, PTY 수명, 순서 있는 쓰기 | `session.py` | 기존 Session/FDWriter, POSIX controlling PTY; 직접 자식 종료 후 쓰기 중단 |
| 엔진 버전·ABI·검증한 native 기능 | `engine.py` | 기존 bridge·engine을 검사하고 한 입력 구현만 선택하도록 기능 profile 제공 |
| 입력 구현 선택과 미완성 transfer 수명 | `inputs.py` | 기존 ProtocolStream·iTerm shim·clipboard broker를 순서대로 연결; 중복 처리 없음 |
| 화면·history·선택·이미지·프로그램 입력 모드 | `vt.py`/`native/bridge.c`를 통한 libghostty-vt | libghostty-vt 1.0.1 parser·grid·encoder·Kitty image storage |
| 호스트별 정책·예외 | `hosts.py`의 HostAdapter | 기존 버전 검증 범위·출력 선택·XTerm geometry 보정 |
| 외부 기능 관측과 실제 pixel geometry | `host.py`의 HostBroker | PTK의 기존 입력 reader와 키 큐; 별도 stdin reader 없음 |
| UI 포커스·프로그램에 전달한 press/release | `app.py` | 기존 PTK Application과 입력 경로 |
| 선택적 UI 수명·가림 영역 | `plugins.py` | PTK FloatContainer·Window·위젯 |
| 이미지 crop·가림·외부 ID·자원 한도 | `graphics.py` | native RGBA/placement, 표준 zlib/base64 |
| 최종 text/image/effect 출력 순서 | `rendering.py` | PTK 3.0.53 layout·diff·Output·모드 복원 |
| 자식 제어문 경계 | `protocol.py` | bounded byte framing; 일반 VT 의미는 native parser로 전달 |
| iTerm 입력 | `image_protocol.py` | Pillow, native Kitty image store와 placeholder |
| iTerm 이미지 출력 | `graphics.py`/`raster.py` | 기존 clipping·PTK repaint·Pillow PNG |
| 클립보드 권한·요청·완료 | `clipboard.py` | 기존 HostInput 큐와 renderer; host 5522 identity를 별도로 매핑 |
| 선택 gesture·자동 스크롤 예약 | `selection.py` | native SelectionGesture; 선택 범위와 history anchor는 libghostty 소유 |

VT를 Python으로 다시 구현하거나 tmux/Zellij parser를 복제하지 않는다. PTK의 기본 text fragment만으로는 native grapheme 폭, hyperlink, underline metadata, image occlusion을 보존할 수 없어 작은 Window/Renderer 확장 경계를 둔다. 구조화된 Alt 키가 여러 PTK 이벤트로 나뉘어도 popup의 소비가 유지되도록 KeyProcessor의 정확한 handler 경계도 확장한다. prefix 매칭이나 처리 루프는 복제하지 않는다. 이 경계들은 고정한 PTK 버전의 일부 내부 API에 의존하므로 버전 갱신 시 합성 테스트를 반드시 실행한다. 설치된 PTK 소스를 monkeypatch하지 않는다.

PNG/iTerm decode는 Pillow 13.0.0을 재사용하고 native allocator에 제한된 RGBA만 복사한다. SIXEL은 입력·출력 모두 미지원이며 이전 custom decoder/encoder와 libsixel 의존 경로를 제거했다. Euporie 등 전체 프레임워크를 의존성으로 넣거나 별도의 VT 상태를 추가하지 않았다.

## 단계별 상태

| 단계 | 이번 변경 | 남은 범위 |
| --- | --- | --- |
| 1. capability와 응답 소유권 | host query/reply 분리, 늦은 응답 거부, 보수적 fallback | 추가 host별 확인·GUI 행렬 |
| 2. rich snapshot과 renderer 경계 | native 셀 폭·스타일·링크·커서·이미지·효과, 단일 출력 합성 | 외부 폰트/Unicode 폭 차이의 전 조합 |
| 3. text·입력·geometry | 관측한 기본색·256색 palette, 명암 변경 통지, 확장 밑줄·overline·커서 색, 제한된 확장 키, pixel mouse, native 선택·자동 스크롤, resize | 전체 물리 키/media 키, 실시간 palette RGB 동기화 |
| 4. 이미지와 popup 합성 | Kitty 및 확인된 iTerm backend, crop·가림·복구·history·한도 | legacy layer/alpha, 반투명 widget의 임의 pixel 합성; SIXEL 미지원 |
| 5. 부수 효과·수명 | bell/title/cwd, focus, render hold, OSC52/iTerm clipboard, correlated 5522 read/write·사용자 PRIMARY, 정상·TERM/HUP 종료 복원 | 5522 unsolicited MIME paste/OTP, 강제 종료·종료 이후 지연 host 응답 |
| 6. 선택적 UI | 명시적 plugin loading, 바·팝업·입력 격리·취소·오류 정리 | 셸 편집 버퍼와 연동하는 오른쪽 프롬프트/자동완성 |
| 7. 실제 검증 | PTY/PTK 회귀와 Kitty·xterm·WezTerm GUI의 픽셀·파일·클립보드 결과 | OS·host 확대, 성능 비교, detach/attach·멀티 클라이언트 |

이는 모든 터미널 escape sequence 또는 tmux/Zellij의 모든 기능을 구현했다는 의미가 아니다. 아래 표의 미지원 기능을 capability로 광고하지 않는다.

## 입출력 capability

| 기능 | 현재 동작·보장 | 제한 |
| --- | --- | --- |
| 기본색·palette | 시작 시 OSC10/11/12·OSC4 응답을 native 기본값에 적용; 기본 배경은 host를 상속하며 명시적 검정과 구별 | 미관측 palette 항목은 native 기본값 사용; host 색상으로 확인했다고 간주하지 않음 |
| 테마 변경 | 실제 mode2031/명암 응답을 확인한 host에서 변경 통지 전달; child OSC override 보존 | 명암 통지에는 RGB가 없으므로 새 RGB·palette는 미확인으로 처리; 전체 palette의 실시간 동기화는 미지원 |
| Unicode와 폭 | native grapheme/셀 폭을 PTK에 전달, popup이 넓은 글자의 절반을 가리면 남은 절반을 비움 | host 폰트별 폭과 emoji shaping이 반드시 같다는 보장 없음 |
| 링크·밑줄·글자·커서 | 확인된 Kitty에서 OSC8·확장 밑줄, Kitty/WezTerm에서 overline; cursor shape/blink와 복원 가능한 cursor 색 전달 | 미확인 host에 colon SGR을 보내지 않음; hollow cursor는 block; Kitty dynamic cursor는 별도 응답 확인 필요 |
| DSR/DA 등 자식 query | 가상 VT가 자기 상태를 응답 | host 응답을 자식 입력으로 그대로 전달하지 않음 |
| 외부 협상 | Kitty graphics identity, cell/area size, XTVERSION, DA1, DECRQM, 기본색·palette·명암·제목 질의 | 미응답은 기능 비활성화; deadline은 협상 종료용이며 셸 readiness 판단 아님 |
| 지연·중복 query 응답 | host parser에서 소비; 끝난 probe를 다시 활성화하지 않음 | identity가 없는 geometry query는 초기 한 차례만 사용 |
| resize | PTY grid와 native pixel geometry 갱신 | 이후 커널 pixel 정보가 없으면 이미지/pixel mouse 비활성화 |
| 키·paste | 기존 native encoder와 bracketed paste, 초기 협상 중 입력 큐 보존 | 키 인코딩은 프로그램 모드에 따름 |
| 확장 키 | 확인된 Kitty에서 Unicode, Ctrl/Alt/Shift, F1–F12, navigation/keypad navigation; non-text press/repeat/release | 전체 physical key, Super/Hyper/media 키, 모든 key identity를 보장하지 않음 |
| 마우스 | legacy/SGR 셀 경로, 확인된 host에서 원본 1016 pixel 위치 보존 | 모르는 geometry로 픽셀을 추정하지 않음 |
| 로컬 선택 | native 단어·행·Alt 사각 선택, 범위 밖 드래그 자동 스크롤; 선택은 CLIPBOARD와 PRIMARY로 복사 | 프로그램이 mouse를 소유하면 기존 native 전달 우선; Shift로 로컬 선택 |
| 중간 클릭 | 확인된 host5522로 실제 시스템 PRIMARY를 읽고 native bracketed paste로 전달 | 미지원 host에서 내부 복사본으로 대체하지 않음; host 자체 Shift+middle 동작은 미검증 |
| 포커스 변경 | 전달한 키·마우스를 이전 소비자에게 release, popup은 자체 입력만 소비 | 임의 플러그인의 직접 stdout/OS 접근을 sandbox하는 API는 아님 |
| synchronized output | native가 표시 hold 시작 시 committed snapshot을 보존; host 출력도 2026 transaction 사용 | 1초 표시 watchdog은 미완성 hold 해제용; 입력 권한과 무관 |
| bell/title/cwd·종료 | 단일 renderer로 전달; 제목·커서·명암 모드와 PTK 입력 모드 정리, TERM/HUP 정상 종료, 자식 회수 | title은 stack 또는 실제 초기 응답이 있는 경우만 변경; cwd는 실제 시작 디렉터리로 복원; SIGKILL 복원 불가 |

호스트 식별 환경 변수(`KITTY_*`, `TMUX`, `STY`, WezTerm/Windows Terminal 식별자 등)를 자식에게 그대로 상속하지 않는다. 가상 터미널의 기능과 바깥 프로그램의 전용 socket/path를 혼동하지 않기 위해서다.

## 이미지 계약

Kitty graphics는 기존 libghostty-vt parser를 사용한다. **iTerm OSC1337 File/MultipartFile**은 입력 어댑터에서 decode한 뒤 같은 native image store로 정규화한다. SIXEL은 libghostty-vt 1.0.1에서 지원하지 않으므로 기능을 광고하거나 별도로 번역하지 않는다. native가 보유한 이미지와 외부 출력 이미지 ID는 구별하며, 외부에서 이 renderer가 만든 ID만 삭제한다. 환경 변수만 보고 이미지 지원을 켜지 않는다. 호스트가 응답한 프로토콜·버전·feature와 실제 양수 셀 pixel geometry를 확인한다.

`GraphicsPresenter`는 문자열로 된 출력 계획만 만든다. 실제 쓰기는 PTK renderer가 한 번의 transaction으로 수행한다. text repaint 뒤 placement를 복원하며, 변하지 않은 픽셀은 다시 upload하지 않는다. source RGBA는 유지하고 표시용 crop/scale만 캐시한다. scaling은 명시적인 nearest-neighbor이며, 큰 확대 이미지는 보이는 부분만 rasterize한다.

팝업은 불투명 사각형으로 취급한다. native 이미지가 text 위 layer에 있어도 popup 사각형을 빼고 남은 pixel fragment만 그린다. popup 이동·닫기·resize·history 이동 뒤 최신 native image로 복원한다. 이는 PTK의 문자열을 이미지로 다시 그리는 방식이 아니며, 임의의 반투명 위젯·그림자·다른 폰트까지 합성하는 GPU compositor는 아니다.

native animation clock과 다음 deadline을 연결했다. frame 교체는 같은 generation·배치 경로를 사용한다. 애니메이션의 모든 blend/disposal 조합에 대한 실제 GUI 행렬은 아직 없다.

자원 정책은 모듈 선언부에 둔다. 현재 native image storage 16MiB, 표시 이미지당 16MiB, 표시 캐시 32MiB, placement 1,024개, 가림 fragment 4,096개다. 자원 한도 초과는 해당 이미지 장면을 지우고 텍스트/입력을 계속 유지하며 장면이 바뀌면 재평가한다. 잘못된 native 구조는 같은 오류로 숨기지 않는다. host image 오류는 동일 content에서 반복 전송하지 않고 새 generation/geometry에서만 재시도하며, 누적 3회면 해당 세션의 이미지 표시를 중단한다. 진단은 `--dump-state`의 `graphics_reason`/`graphics_error`에 기록한다.

iTerm 출력은 이미지 ID별 삭제 기능이 없으므로 별도 `RasterPresenter`가 기존 clipping 계산과 PTK repaint를 재사용한다. 바뀐 장면의 이전 점유 셀을 지우고 텍스트를 다시 그린 후 현재 이미지를 출력한다. 인코딩 결과는 캐시하지만 텍스트 렌더 후 pixel 재전송은 필요하다. Kitty의 전송 비용과 같다고 보장하지 않는다.

추가 이미지 backend는 실제 확인한 WezTerm 20240203-110809-5046fc22의 iTerm 프로토콜로 제한한다. xterm 407은 이미지 없는 text/color host다. 다른 버전/호스트를 같은 구현으로 가정하지 않는다. macOS iTerm2 자체를 실행한 결과는 아니다. 자세한 입력·출력 제한과 재실행 환경은 [프로토콜 문서](TERMINAL_PROTOCOLS.md)에 기록한다. 원본 escape를 그대로 외부 터미널에 쓰는 우회 경로는 추가하지 않았다.

## 클립보드 경계

기존 로컬 선택 복사는 유지한다. 자식 프로그램에는 기본적으로 clipboard 쓰기를 허용하고, 읽기는 `--clipboard read-write`로 명시적으로 켠다. `--clipboard off`는 자식의 읽기·쓰기를 막으며 사용자가 직접 선택해 복사하는 동작은 유지한다. 외부 host의 권한 정책도 그대로 적용한다.

사용자의 중간 클릭은 자식 접근 정책과 별도인 사용자 paste 요청이다. 실제 시스템 PRIMARY의 유효한 UTF-8 응답(최대 1MiB)이 완료된 뒤에만 전달한다. 새 키·팝업·포커스 변경·resize·종료 시 요청을 취소하고 늦은 응답은 소비한다. 같은 소비자로 되돌아와도 취소된 응답을 재사용하지 않는다.

원래 프로토콜을 구별할 수 없는 native 동기 callback 대신, 입력 경계에서 OSC52·iTerm CopyToClipboard·Kitty5522를 구별하는 broker를 추가했다. OSC52/iTerm 쓰기에는 완료 ACK를 꾸며 보내지 않는다. Kitty5522의 DONE은 실제 호스트의 해당 요청 완료 뒤에만 전달한다. host 요청 ID와 child ID를 구별하고 오래된·중복·취소된 응답은 소비한다. 응답도 키·포커스와 같은 PTK 입력 큐에서 순서대로 처리한다.

읽기는 identity가 있는 host5522로만 전달한다. OSC52 읽기도 내부적으로 이 경로로 번역한다. identity가 없는 host OSC52 read를 재사용하여 지연 응답이 다른 요청에 붙는 위험을 만들지 않는다. 미지원 호스트에서는 빈 OSC52 응답 또는 5522 미지원/권한 오류로 처리한다. 직접 5522 read/write와 실제 `kitten clipboard`는 지원하지만 unsolicited MIME paste/OTP 이벤트 모드는 지원하지 않으므로 해당 DECRQM 모드는 permanently-reset으로 답한다.

직접 자식이 종료될 때 진행 중인 clipboard 응답이 있으면 PTK reader를 유지해 완료·오류·실패 deadline까지 처리한다. 이때 종료된 셸의 PTY에는 새 키나 응답을 쓰지 않는다. 강제 종료나 deadline보다 더 늦은 외부 응답이 이후 부모 프로세스에 전혀 도착하지 않는다는 보장은 제공하지 않는다. 외부 프로토콜에 취소 완료 ACK가 없기 때문이다.

## 플러그인 계약

`ish-vt --plugin my_ui:setup`을 명시한 경우에만 Python 모듈의 `setup(plugins)`를 호출한다. 기본값은 빈 목록이다. 기존 `ish` 플러그인 API와 동일한 인터페이스라는 뜻은 아니다.

```python
from prompt_toolkit.widgets import TextArea

def setup(plugins):
    popup = None

    @plugins.bind("f2")
    def toggle(event):
        nonlocal popup
        if popup is not None and not popup.result.done():
            plugins.close_popup(popup)
        else:
            popup = plugins.show_popup(TextArea(text="메모"), width=30, height=6)
```

- `bind(key)`는 명시적으로 예약한 단일 키만 등록한다. multi-key prefix로 native 입력을 임의 보류하지 않는다.
- `show_popup(content, width=..., height=..., left=..., top=...)`는 PTK container와 결과 Future를 가진 popup handle을 반환한다. 입력은 popup에만 전달한다.
- `close_popup(handle, result)`는 이전 포커스를 복구한다. 부모 종료 시 자식을 취소하며 오래된/중복 완료는 적용하지 않는다.
- `set_bar(content, rows=...)`는 실제 PTY 행 수를 줄인다. `set_bar()`로 제거하면 복원한다. 작은 창에서도 native 한 행을 남긴다.
- `create_task(coroutine)`의 수명은 플러그인 UI에 속한다. 종료 시 취소한다. 등록한 sync/async 단축키와 task 예외는 기록하고 임시 popup을 닫는다.
- `setup`은 정리 callable을 반환할 수 있다. 정리 실패가 Session/host 모드 복원을 막지 않는다. setup 실패는 정상적인 terminal 복원 후 시작 실패로 처리한다.

Python 플러그인은 신뢰하는 로컬 코드다. 위젯 자체 렌더링 오류나 임의 OS 접근까지 격리하는 별도 프로세스 sandbox는 아니다. 실제 오른쪽 프롬프트·completion은 셸 편집 버퍼를 입증하는 별도 계약이 필요하며 화면 문자를 역파싱해서 적용하지 않는다.

## 검증

교체 경계 리팩토링 후 일반 회귀 **263개가 한 번의 전체 실행에서 통과했다(150.117초)**. native bridge는 `-Wall -Wextra -Werror`로 재빌드했다. 버전·ABI·필수 기능 거절, 빌드 실패 시 이전 산출물 보존, native 대체 시 보완 모듈 미생성, 입력 중복 방지, 이미지/clipboard 소유권, 호스트 정책 분리, presenter 전환 정리를 추가 검증했다.

실제 Kitty·xterm·WezTerm GUI **11개 시나리오를 검증했다**. 전체 순차 실행(162.641초)은 10개 통과, 선택 테스트의 마지막 진단 snapshot 검사 1개 실패였다. 이때 실제 붙여넣기 파일과 OS clipboard 값은 이미 일치했으며, `_finish()` 뒤 전달한 입력보다 비동기 `_after_render` 진단 파일이 늦게 갱신되는 테스트 경합이었다. 고정 지연 대신 완료된 snapshot을 기다리도록 fixture를 수정한 뒤 해당 GUI 1개가 통과했다(24.312초). 이 재실행 사이에 production 코드는 변경하지 않았다. terminal 관련 49개 Python 파일의 Ruff check·format check, `git diff --check`, CLI `--help`도 통과했다.

기본 회귀는 실제 PTY, PTK pipe input, native libghostty-vt, 독립적인 wire/파일 기대값을 사용한다. PTK 합성은 실제 Vt100Output과 독립 pyte 화면으로도 확인한다. 이미지 픽셀 결과는 pyte로 검증할 수 없어 별도 실제 GUI 검사를 둔다.

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest \
  test_terminal_engine test_terminal_inputs \
  test_terminal_protocol test_terminal_image_protocol test_terminal_clipboard \
  test_terminal_vt test_terminal_native_features test_terminal_graphics_vt \
  test_terminal_graphics test_terminal_host test_terminal_mouse \
  test_terminal_selection test_terminal_session test_terminal_composition \
  test_terminal_cli test_terminal_basics -v
```

GUI 검사는 실제 **Kitty 0.50.0, xterm 407, WezTerm 20240203-110809-5046fc22**를 별도 Xvfb에서 실행한다. 공식 screenshot API 또는 X11 화면 캡처로 최종 pixel을 읽고 실제 프로그램이 기록한 파일과 비교한다. 클립보드는 해당 X display의 OS clipboard bytes와 직접 비교한다. 물리 모니터, 사용자의 실제 터미널 설정, Windows Terminal/macOS GUI를 검증했다는 뜻은 아니다.

```sh
.venv/bin/python tools/check_terminal_gui.py
```

사전 준비: Linux/WSL, `Xvfb`, `xauth`, `xclip`, tcsh, Vim, Pillow와 테스트용 Kitty·xterm·WezTerm 실행 파일이 필요하다. 해당 실행 파일과 없는 X11 library는 `.cache/terminal-test` 아래에만 배치했다. 테스트는 자동으로 시스템 패키지를 설치하지 않는다. private Xauthority로 보호한 임시 Xvfb와 테스트 전용 socket을 만들고 종료 후 정리한다. libsixel은 더 이상 실행·검증 의존성이 아니다.

검증 시나리오:

- native tcsh foreach continuation, 공백·Unicode·도구 이름 heredoc, Vim 저장, native stdin.
- RGBA 이미지와 PTK popup의 부분 가림, popup 뒤 PNG 갱신, 이동·닫기 후 최신 이미지 복원.
- 실제 OS 창 resize, 이미지의 history 진입·다시 보기·live 복귀, popup 입력 격리.
- 플러그인 없는 기본형의 전체 화면·기본 배경, F2/Ctrl+C/Ctrl+D의 native 바이트 전달.
- 실제 PTK 입력 경로의 1016 pixel 좌표, popup 전환 시 press/release 보존.
- resource limit, 늦은 host 응답, 중복 popup 완료, 플러그인 예외와 종료 복원.
- 구조화된 Alt 키 도중 popup 열기·닫기, 소비한 fallback과 별개 입력이 같은 prefix buffer에 있어도 입력 유실·중복·역전 없음.
- iTerm 실제 입력·출력, popup 배경뿐 아니라 겹친 흰 글리프 보존, native erase 후 외부 이미지 제거. SIXEL P2=0/1은 이미지·payload 노출 없이 native 미지원 처리.
- 실제 OS clipboard의 80,000-byte Unicode text·120,295-byte PNG, OSC52/iTerm write, 5522 MIME read/write, 공식 `kitten clipboard` 명령 왕복과 SHA256 비교.
- 자식 종료 직전 비동기 clipboard write의 실제 완료까지 응답을 회수하고 부모 TTY에 clipboard ACK가 남지 않는 정상 종료 경로.
- iTerm clipboard capture 중 CAN/SUB/불완전 제어문 취소 후 EndCopy까지 데이터 폐기, 화면·클립보드에 부분 payload가 노출되지 않음.
- 실제 Kitty의 custom 기본색·palette 응답과 픽셀, overline 추가 픽셀, 커서 색 적용·복원, `set-colors`로 발생한 실제 2031 명암 변경과 child override 보존. 실제 xterm palette 관측과 resize.
- 실제 Kitty XTEST 더블 클릭의 단어 선택·CLIPBOARD/PRIMARY 복사, 외부 프로그램이 변경한 Unicode PRIMARY를 실제 중간 클릭으로 native stdin에 한 번 전달. 행·사각형 선택과 범위 밖 자동 스크롤은 native/controller 회귀로 검증한다.
- 정상 종료·SIGHUP·SIGTERM 뒤 실제 Kitty의 제목·커서 색·cwd와 query 가능한 모드·termios 복원, 실제 PTY 셸 회수. 마지막 PTK 렌더 실패 시에도 metadata 정리.
- 실제 PTK 출력에서 Kitty→iTerm 전환 시 이전 이미지 정리 순서, 비지원 출력으로 전환 시 전송 중단, overline만 있는 공백을 below-text 이미지로 덮지 않고 글자 변경 뒤 재평가하는 동작.

보완 모듈의 제거 조건과 업그레이드 절차는 [교체 가능한 책임 경계](TERMINAL_PROTOCOLS.md#교체-가능한-책임-경계)에 정리했다. native iTerm 분기를 검증하는 fake sink 테스트는 교체 방식의 검증이며 live native iTerm 지원 선언이 아니다. 이번 리팩토링으로 SIXEL이나 Windows Terminal 이미지 출력 지원을 추가하지 않았다.

GUI 결과는 `.cache/terminal-test/evidence/`의 `result.json`, `base-result.json`, `xterm-result.json`, `wezterm-result.json`, `clipboard-result.json`, `clipboard-exit.json`, `appearance-result.json`, `selection-result.json`, `test_*_restores_presentation.json`과 PNG에 기록한다. 캐시 파일은 Git에 넣지 않는다. 테스트 deadline과 화면 판독은 실패 판정에만 쓰며 production 셸 readiness로 사용하지 않는다.

이번 리팩토링 실행 로그는 `.cache/terminal-test/refactor-unit.log`, `refactor-gui.log`, `refactor-selection-gui.log`에 보관했다. GUI 로그에는 위에서 설명한 첫 실패도 남겨 두었다.

Linux x86_64/WSL 밖의 OS, 실제 비지원 host GUI, 모든 이미지 프로토콜, 네트워크/SSH 중첩 및 tmux/Zellij 중첩, 성능 동등성은 아직 검증하지 않았다. 전체 visible grid snapshot과 이미지 재표본화 비용이 있으므로 기존 터미널과 성능 차이가 없다고 선언하지 않는다. detach/attach·세션 서버·멀티 클라이언트도 별도 제품 기능이다.

## 참고한 원본

- [libghostty-vt 이미지 guide](https://lib.ghostty.org/vt/guide-images.html), [terminal 구축 guide](https://lib.ghostty.org/vt/first-terminal.html)
- [Kitty graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/)
- [재사용한 PTK 3.0.53 renderer](https://github.com/prompt-toolkit/python-prompt-toolkit/blob/3.0.53/src/prompt_toolkit/renderer.py)

문서보다 설치한 버전의 header와 실제 동작을 우선하여 검증했다. 외부 구현을 통째로 교체하는 대신 engine·UI·이미지 decoder의 기존 책임을 재사용하고, 이 사이에 필요한 계약만 추가했다.
