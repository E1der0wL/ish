# 이미지·클립보드·호스트 상태 계약

2026-11-15의 Linux/WSL `ish-vt` 구현이다. 기본형은 셸의 전체 화면을 표시하며 하단 바·우측 프롬프트·팝업·UI 전용 단축키를 추가하지 않는다. 선택적인 UI만 `--plugin module:setup` 경로를 사용한다.

## 입력과 출력의 경계

### 교체 가능한 책임 경계

| 책임 | 소유 모듈 | 교체·제거 조건 |
| --- | --- | --- |
| 검증한 엔진 버전·bridge ABI·native 입력 기능 | `engine.py`, `native/bridge.c` | 실제 header/runtime/빌드 기능과 공통 계약 테스트가 일치한 새 버전으로 갱신 |
| 입력 프로토콜별 구현 선택 | `inputs.py` | 시작 시 한 구현만 선택. native 지원 검증 후 보완 경로와 lazy import 제거 |
| iTerm 이미지 보완 | `image_protocol.py` | native가 pixels·cursor/style·erase·history·multipart 취소 계약을 통과하면 삭제 |
| 호스트별 정책·검증 범위·geometry 보정 | `hosts.py` | 동일한 보장을 제공하는 실제 capability 응답과 호스트 회귀로 해당 예외를 대체 |
| 호스트 응답·probe 세대·복원 수명 | `host.py` | 호스트별 규칙을 실행하되 검증한 관측 사실과 정책을 섞지 않음 |
| 호스트 이미지 출력 | `graphics.py`의 `Presenter` | 같은 clipping·layer·popup 가림·오류·종료 정리를 보장하는 구현으로 교체 |
| clipboard 권한·비동기 요청/완료 | `clipboard.py` | 엔진 callback 추가만으로 제거하지 않음. identity·권한·취소·실제 완료 의미까지 보장해야 대체 가능 |

화면·이미지·선택 상태는 계속 libghostty만 소유한다. 보완 모듈은 미완성 transfer를, presenter는 출력용 파생 캐시와 자신이 만든 외부 resource ID를 소유한다. 두 번째 VT 상태를 만들지 않는다. 기존 parser·이미지 decoder·clipper·renderer·clipboard broker를 재사용한다.

입력 구현은 `native / adapter / unsupported`로 고정하며, 호스트 출력 지원과는 별개다. 현재는 Kitty=native, iTerm=adapter, SIXEL=unsupported다. native iTerm을 선택하는 경로에서는 iTerm 보완 모듈을 import하거나 생성하지 않는다. 이는 미래 교체 경로의 단위 검증이며 현재 엔진의 iTerm native 지원을 뜻하지 않는다. 부분 처리한 입력을 다른 구현에 재전송하는 fallback은 없다. 호스트 출력이 중단되면 진행 중인 이미지 보완 transfer를 취소하고 기존 입력 구현 선택은 유지한다.

libghostty가 이미지 **입력**을 지원하게 되어도 외부 터미널용 **출력**과 PTK popup 합성이 저절로 생기지는 않는다. 입력 shim과 출력 presenter를 독립적으로 제거할 수 있게 분리했다. Pillow도 native PNG decode callback과 raster 출력에서 사용하므로 iTerm shim 삭제만으로 의존성이 없어지지는 않는다.

`engine.py`의 `ENGINE_VERSION`과 `BRIDGE_ABI`가 빌드 도구와 런타임 검사의 단일 원본이다. 현재는 libghostty-vt 1.0.1만 검증했다. 버전이 더 높거나 심볼이 있다는 이유만으로 활성화하지 않는다. metadata가 없는 이전 bridge, 다른 ABI, header/runtime 불일치, 필수 빌드 기능 누락은 state 생성 전에 재빌드 안내와 함께 거절한다. 빌드 도구는 임시 산출물 검사에 성공한 뒤에만 기존 bridge를 교체한다.

업그레이드 순서는 ① 새 엔진의 공개 API와 의미 확인 ② native 입력 기능 profile 및 필요한 bridge 연결 변경 ③ 보완 경로와 동일한 독립 wire/pixel/파일 기대값으로 검증 ④ 실제 호스트에서 이미지·popup·resize·종료 회귀 ⑤ 통과한 보완 경로와 전용 의존성만 삭제다. 지원 선언을 먼저 늘리지 않는다.

`--dump-state`는 `engine`, `input_routes`, `host_adapter`, 기존 `host`, `graphics_enabled`/`graphics_reason`/`graphics_error`, `clipboard_policy`를 따로 기록한다. UI는 이 정보를 표시하지 않는다. native 입력 가능·호스트 출력 가능·사용자 권한·현재 활성 여부를 한 boolean으로 합치지 않는다.

실험용 Python 내부 API에서 `Terminal.images` 객체 노출은 `Terminal.image_errors` 읽기 전용 tuple로 대체했다. 기존 `Terminal.clipboard` 연결은 입력 시작 전까지 유지하며, 처리 중 broker 교체는 거절한다. 기존 `ish` 진입점과 셸 입력·hook 순서는 변경하지 않았다.

`ProtocolStream`은 PTY 출력의 완성된 CSI/OSC/DCS/APC 경계만 나눈다. 일반 텍스트와 미처리 제어문은 기존 native parser로 순서대로 전달한다. VT 화면을 Python에 복제하거나 셸 문법을 해석하지 않는다. UTF-8 continuation byte를 raw C1 제어문으로 착각하지 않으며, 잘린 문자열·취소·용량 초과 데이터가 화면 텍스트로 새어 나오지 않도록 처리한다.

iTerm 이미지 입력은 Pillow로 decode하고 native Kitty image store에 넣는다. image ID는 이미 존재하는 native ID와 충돌하지 않도록 선택한다. 점유 셀은 native virtual placeholder를 사용하므로 기존 text overwrite·부분 erase·scrollback·reflow를 재사용한다. cursor/style은 native 상태에서 읽고 native 명령으로 복구한다.

화면·이미지·선택 범위의 원본은 libghostty-vt다. 출력 backend는 native snapshot에서 파생한 bounded cache만 소유한다. popup 사각형을 제외한 최종 가시 영역은 기존 합성 경로로 계산한다. 이미지·클립보드·title·cursor 등 외부 출력은 검증하고 정규화한 뒤 같은 renderer queue를 사용한다. 자식의 escape code를 외부 터미널에 그대로 통과시키는 경로는 없다.

외부 입력은 하나의 PTK reader가 소유한다. 호스트의 capability·색상·clipboard 응답과 focus 알림을 키 입력과 구별하고, 순서가 필요한 이벤트는 같은 입력 queue에서 처리한다. 잘린 응답은 재조립하고 오래되거나 중복된 응답은 소비하되 프로그램 stdin으로 전달하지 않는다. 시작 probe의 deadline은 capability를 보수적으로 확정하는 용도이며 셸 readiness 판단에 쓰지 않는다.

## 이미지 지원 조건

| 입력 | 구현한 범위 | 제한 |
| --- | --- | --- |
| Kitty | 기존 libghostty image parser와 native 저장소 | 출력 backend의 layer/alpha 제약은 별도 |
| iTerm File | `inline=1`, Pillow가 읽는 정적 raster, px/cell/% 크기·비율·cursor 정책 | 파일 다운로드는 실행하지 않음; animated iTerm, origin/horizontal margins 삽입 제외 |
| iTerm multipart | MultipartFile → FilePart → FileEnd, 분할 입력·취소·크기 제한 | 부분 전송을 화면에 먼저 표시하지 않음 |
| SIXEL | 미지원 | 불투명·투명 입력 모두 제외; 자식에게 지원을 광고하지 않음 |

| 외부 출력 | 활성화 근거 | 표현 제약 |
| --- | --- | --- |
| Kitty | graphics query 성공 + 실제 셀 크기 | 기존 ID별 cache·z-order·alpha 유지 |
| WezTerm iTerm | 확인한 `20240203-110809-5046fc22` 버전 + 실제 셀 크기 | 이미지 cell 삽입·지우기 의미에 맞춘 repaint 필요; 임의 below-text layer와 부분 alpha는 제한 |

WezTerm backend에서는 text가 없는 영역임을 native 셀로 확인한 placeholder layer만 after-text로 표시한다. 기존 글자/밑줄을 덮어 의미가 달라지는 장면은 capability 오류로 구분하며, 구조 오류와 섞어 숨기지 않는다. 진단은 `--dump-state PATH`의 `image_errors`/`graphics_error`에 남기고 기본형에 경고 popup을 추가하지 않는다.

**SIXEL은 입력과 출력 모두 지원 범위에서 제거했다.** 이전 custom decoder/encoder·backend와 libsixel 의존성을 제거했으며, SIXEL negotiation도 하지 않는다. 일부 불투명 입력만 지원하는 예외도 남기지 않는다. 이미지와 native 셀의 삭제·수명을 정확히 연동하지 못한 경로를 별도 shadow VT로 보완하지 않는다. xterm 407은 text·색상·resize 호스트로 사용하고 이미지 backend로 선택하지 않는다. 오래된 SIXEL 관련 응답을 키로 누출하지 않도록 소비하는 처리는 지원 활성화와 무관하다.

## 기본색·팔레트·명암 통지

초기 probe에서 OSC10/11/12의 전경·배경·커서 RGB와 OSC4의 256개 팔레트 항목을 조회한다. 실제 받은 항목만 `HostAppearance`에 기록하며, 없는 응답은 미관측으로 남긴다. 자식의 색상 조회와 native 팔레트 해석에는 이 관측값을 적용한다. 외부에 그릴 기본 전경·배경은 계속 기본색 SGR을 사용하므로 창의 배경과 투명도 설정을 상속한다. 관측한 배경 RGB를 모든 기본 셀에 명시적으로 칠하지 않는다.

프로그램이 OSC10/11/12 또는 OSC4로 설정한 색상은 host 기본값과 구별한다. 호스트의 명암 통지가 프로그램의 명시적 색상 override를 덮지 않는다. 기본색 reset은 다시 상속 상태로 돌아간다.

2031 mode 지원과 초기 명암 응답을 확인한 경우에만 변경 통지를 사용한다. 실제 `CSI ? 997 ; ... n` 알림을 순서대로 native 상태에 전달하며, 원래 켜져 있던 mode는 종료 시 끄지 않는다. **이 알림은 dark/light 사실만 담으며 새 RGB나 팔레트 전체를 제공하지 않는다.** 알림 후 현재 RGB·팔레트 관측은 미확인으로 바꾸고 초기 snapshot은 복원 자료로 별도 보존한다. identity가 없는 색상 query를 반복해 지연 응답을 새 테마 값으로 오인하는 방식은 사용하지 않는다. 따라서 실행 중 테마 변경 후 모든 ANSI 색상이 외부 팔레트와 계속 일치한다고 보장하지 않는다.

실제 Kitty GUI에서는 사용자 지정 기본색·팔레트 query 응답, 화면의 ANSI 픽셀, dark→light 알림과 자식 전경 override 보존을 확인했다. 모든 호스트·테마 변경 방식에 대한 검증은 아니다.

## 스타일·커서·종료 복원

- Overline은 확인한 Kitty와 WezTerm adapter에서 출력한다. xterm에는 활성화하지 않는다. 확장 밑줄·하이퍼링크 출력은 기존 Kitty capability 범위를 유지한다.
- 커서 색상은 복원 가능한 초기 RGB 또는 Kitty dynamic cursor 상태를 실제로 확인한 경우에만 외부 OSC12로 적용한다. Kitty에서 OSC12만 받은 경우에는 dynamic과 고정 검정을 구별할 수 없으므로 커서 변경 권한을 부여하지 않는다.
- 자식 커서 색상 reset과 종료 시에는 관측한 RGB 또는 dynamic 상태로 복원한다. 실제 Kitty GUI에서 자식 커서 색상 적용과 초기 색상 복귀를 확인했다.
- Title은 확인한 Kitty/xterm의 push/pop을 사용한다. stack을 사용할 수 없는 호스트는 초기 title 응답을 실제로 받은 경우에만 변경·복원한다. 확인한 WezTerm 버전은 title stack이 없으며 초기 응답도 없으면 자식 title을 외부에 적용하지 않는다.
- OSC7에는 기존 호스트 cwd를 읽는 portable query가 없다. 자식이 cwd metadata를 바꾼 경우 종료 시 **ish를 시작한 프로세스의 실제 cwd**를 file URI로 보고한다. `--cwd`로 지정한 자식 cwd와 구별하며, 이전 호스트 OSC7 값을 복구했다고 주장하지 않는다.
- 정상 종료와 처리된 SIGTERM/SIGHUP는 같은 정리 경로에서 termios·대체 화면·입력 mode·지원되는 title/cursor/cwd 표시를 복구한다. 실제 Kitty의 세 종료 경로를 검증했다. SIGKILL, 호스트 연결 소실, 전체 background process tree 정리는 보장 범위가 아니다.

## 선택과 시스템 PRIMARY

선택 범위·단어/줄 경계·사각형 추출은 native selection API가 소유한다. 단일 드래그, 더블 클릭 단어, 트리플 클릭 줄, Alt를 누르고 시작하는 사각형 선택, 가장자리 밖 드래그의 자동 스크롤을 연결했다. Application은 pointer capture와 timer만 관리하며 별도 선택 UI를 추가하지 않는다.

선택을 놓으면 UTF-8 text를 OSC52의 clipboard와 PRIMARY 대상으로 복사한다. 실제 시스템 반영은 호스트의 허용 설정에 따른다. 가운데 버튼의 로컬 붙여넣기는 확인된 5522 host protocol로 **시스템 PRIMARY**를 읽는다. 자식의 clipboard 접근 정책과 별개인 명시적 사용자 동작이다. 지원하지 않는 호스트·busy·읽기 실패에는 이전 로컬 선택 텍스트를 대신 붙이지 않는다. 응답 전에 입력·포커스·세션이 바뀌면 요청을 취소하고 오래된 결과를 새 소비자에게 전달하지 않는다.

프로그램이 마우스를 요청한 live 화면에서는 기존 native mouse 경로를 유지한다. 로컬 선택 정책과 Shift override는 셸 상태가 아닌 VT mode와 viewport로 결정한다. 실제 Kitty/Xvfb에서 자식 정책 `off` 상태의 더블 클릭 복사와 외부 프로그램이 바꾼 PRIMARY의 가운데 버튼 붙여넣기를 OS clipboard bytes와 native stdin 파일로 확인했다. 다른 선택 gesture의 native·controller 회귀와 실제 GUI 검증 범위는 구분한다.

## 자식 클립보드

```sh
# 기본값: 자식은 쓰기만, UI는 기본형 그대로
.venv/bin/ish-vt --shell tcsh

# 명시적으로 자식의 읽기도 허용; 호스트 권한 설정은 별도로 적용
.venv/bin/ish-vt --shell tcsh --clipboard read-write

# 자식 접근 차단; 사용자의 직접 선택 복사와 PRIMARY 붙여넣기는 별도 정책
.venv/bin/ish-vt --shell tcsh --clipboard off
```

- OSC52와 iTerm CopyToClipboard/EndCopy는 text write로 정규화한다. 성공 완료 ACK가 없는 프로토콜에 가짜 ACK를 만들지 않는다.
- Kitty5522는 text와 binary MIME, chunked write, read, alias를 bounded transaction으로 중재한다. child ID와 별도의 세션별 host ID를 사용한다.
- DONE은 실제 host completion 이후에만 전달한다. 권한 거부·busy·오류·지연·중복 응답도 해당 transaction과 상관관계를 검사한다.
- child write chunk에 ID가 있으면 현재 요청의 ID와 일치해야 한다. ID가 없는 표준 continuation은 wire 순서로 현재 요청에 속하므로, 잘못된 송신자가 새 요청 뒤에 보내는 ID 없는 과거 chunk의 세대까지 구별할 수는 없다.
- 호스트에 읽기를 요청할 때는 5522 identity를 사용한다. OSC52 child read의 결과는 원래 OSC52 형식으로 돌려준다.
- 프로그램의 읽기는 기본 거부한다. 사용자가 직접 복사한 선택은 자식 정책과 별개이며 busy 중에는 최신 한 건만 보류해 완료 후 전달한다.
- 하나의 호스트 transaction만 진행한다. clipboard 응답 자체는 일반 shell stdin이나 popup text 입력이 아니다. 사용자 PRIMARY read의 검증된 text 결과만 현재 native paste 경로로 전달한다.
- 5522 unsolicited MIME paste/OTP 이벤트 모드는 구현하지 않았고 지원한다고 광고하지 않는다. 직접 명령형 5522 read/write 및 실제 kitten CLI와 구분한다.

text/capture 한도는 1MiB, MIME transaction 한도는 64MiB다. outgoing write는 MIME별로 정규화한 base64 stream을 사용한다. timeout은 clipboard 작업 실패 판정에만 쓰며 셸이나 프로그램 입력 준비 판단에는 쓰지 않는다. 정상 자식 종료 시 진행 중인 host transaction의 완료·오류·deadline까지 reader를 유지한다. 외부 terminal이 응답하지 않거나 강제로 종료한 뒤에도 미래 응답이 절대 없다는 보장은 할 수 없다.

## 의존성과 재실행

- 기존 `.venv`, libghostty-vt 1.0.1, Pillow 13.0.0을 재사용한다. libsixel은 필요하지 않으며 SIXEL용 custom 코드와 의존성을 제거했다.
- bridge 변경 후 `.venv/bin/python tools/build_ghostty_vt.py --bridge-only`로 재빌드한다.
- 실제 GUI 테스트는 `.venv/bin/python tools/check_terminal_gui.py`로 실행한다. Kitty·xterm·WezTerm의 캐시된 실행 파일과 Xvfb/xauth/xclip 및 테스트용 X11 도구가 필요하다. 테스트가 시스템 패키지나 사용자 설정을 바꾸지는 않는다.

GUI 검증은 임시 Xauthority로 인증한 별도 Xvfb를 사용한다. 실제 X11 클립보드도 이 display에 한정하며 사용자의 데스크톱 클립보드를 읽거나 변경하지 않는다. 최종 pixel과 독립적인 파일/OS clipboard bytes를 비교한다. macOS iTerm2, 모든 호스트 버전, 모든 PNG animation/Unicode/색상 조합의 검증을 뜻하지 않는다.

최신 전체 재실행 결과와 증거 파일 위치는 [기능·검증 문서](TERMINAL_CAPABILITIES.md#검증)에 기록한다. 이미지·popup 합성, 셸/프로그램 stdin, text·PNG clipboard·공식 kitten CLI, 기본색·팔레트·커서·명암 통지, 시스템 PRIMARY, 종료 복원은 각 테스트의 실제 관찰 범위로 구분한다.

## 원본 규격과 재사용

- [iTerm 이미지와 multipart 규격](https://iterm2.com/documentation-images.html)
- [Kitty image 및 placeholder](https://sw.kovidgoyal.net/kitty/graphics-protocol/)
- [Kitty clipboard 5522](https://sw.kovidgoyal.net/kitty/clipboard/)
- [Kitty 색상 제어와 dynamic cursor](https://sw.kovidgoyal.net/kitty/color-stack/)
- [2031 명암·팔레트 변경 통지](https://github.com/contour-terminal/contour/blob/master/docs/vt-extensions/color-palette-update-notifications.md)
- [xterm 색상·title 제어](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html)
- [WezTerm escape sequence](https://wezterm.org/escape-sequences.html)

프로토콜마다 입력·출력의 의미를 구별한다. 예를 들어 iTerm 입력을 Kitty 출력으로 표시하는 기능과 macOS iTerm2 자체에서 전체 프로그램을 실행한 검증은 서로 다른 범위다.
