# ish의 기능별 호스트 정책

이 정책은 `ish`의 외부 터미널 출력·입력 연결에 적용한다. 셸 명령줄 편집과 플러그인 선택은 별도의 책임이다. 기본형에는 추가 UI와 셸 hook이 없다.

## 설정

`~/ish/.ishrc.py`에서 다음 세션의 선택을 저장한다.

```python
config.features = {
    "hyperlinks": "on",
    "enhanced_keyboard": "auto",
    "pixel_mouse": "off",
}
config.color_depth = "inherit"
config.clipboard = "write"
```

CLI는 지정한 기능만 덮어쓴다. 다른 rc 선택은 유지하며 같은 이름이 반복되면 마지막 값이 적용된다.

```sh
ish --feature hyperlinks=off --feature enhanced_keyboard=auto
```

- `on`: 해당 프로토콜 사용을 요청한다. 실제 지원 응답이 있었다고 기록하지 않는다.
- `off`: 해당 선택 기능의 사용을 중지한다. 이미 변경한 상태의 정리는 생략하지 않는다.
- `auto`: 아래에 적힌 지원 질의의 긍정 응답이 있어야 사용한다. 무응답은 미확인이지, 미지원 확정이 아니다.

지원 질의를 구현하지 않은 항목의 `auto`, 알 수 없는 이름·값은 셸 실행 전에 오류로 거부한다. 설정은 한 세션 동안 고정한다.

| 기능 이름 | 허용 값 / 기본값 | 실제 질의 또는 추가 조건 |
|---|---|---|
| `hyperlinks` | on/off, **on** | OSC 8. 보편적인 지원 질의 없음 |
| `extended_styles` | on/off, **on** | 확장 밑줄 종류·색. 지원 질의 없음 |
| `overline` | on/off, **on** | SGR 53. 지원 질의 없음 |
| `cursor_color` | on/off, **on** | 커서 색 출력. 초기 색·동적 상태를 복원할 수 있어야 함. 현재 색 조회를 지원 질의로 간주하지 않음 |
| `iterm_graphics` | on/off, **off** | iTerm 이미지 출력. 정확한 셀 크기 필요, 지원 질의 없음 |
| `kitty_graphics` | on/off/auto, **auto** | 식별자가 일치하는 Kitty graphics `a=q`의 OK 응답. 정확한 셀 크기 필요 |
| `sixel_graphics` | on/off/auto, **auto** | DA1 속성 4. 정확한 셀 크기와 복원 가능한 DECRQM 80 시작값 필요 |
| `desktop_notifications` | on/off, **off** | 정규화한 알림을 OSC 9로 출력. 지원 질의 없음. 실제 OS 표시를 보장하지 않음 |
| `synchronized_output` | on/off/auto, **auto** | DECRQM 2026의 변경 가능한 모드 응답 |
| `enhanced_keyboard` | on/off/auto, **auto** | `CSI ? u` 응답으로 확장 키 프로토콜 존재 확인 |
| `pixel_mouse` | on/off/auto, **auto** | DECRQM 1016의 변경 가능한 모드 응답과 정확한 셀 크기 |
| `clipboard_protocol` | on/off/auto, **auto** | DECRQM 5522. **호스트 OSC 5522 전송** 선택만 뜻함 |
| `scheme_notifications` | on/off/auto, **auto** | DECRQM 2031. 복원할 초기 모드와 초기 997 테마 응답도 필요 |

`on`은 지원 응답과 무관하게 기능 사용을 요청하는 방법이다. 이미지의 크기 정보나 커서·테마의 복원 정보까지 만들어 내지는 않는다. 여러 이미지 출력을 함께 요청하면 iTerm, Kitty, SIXEL 순으로 하나만 선택한다. 실제 출력 방식은 `image_protocol` 하나가 결정한다. SIXEL은 모드 80의 시작값을 확인하지 못하면 강제 켜기에서도 사용하지 않는다. 셀 크기가 유실되면 이미지·픽셀 마우스를 중지하고, 유효한 크기가 다시 생겨도 `off`는 유지한다. 종전 WezTerm 이름에 따른 iTerm 자동 선택은 제거했으므로 필요한 세션에서는 `iterm_graphics=on`을 명시한다.

이미지 질의 OK는 현재 사용 중인 기본 전송 경로의 근거이며 이미지 프로토콜의 모든 확장을 지원한다는 뜻이 아니다. 키보드 질의는 프로토콜 존재와 현재 플래그 응답을 확인한다. 기존 disambiguation/event-types 요청과 push/pop을 유지하며, 모든 확장 플래그를 따르지 않는 부분 구현까지 검증했다고 주장하지 않는다.

## 다른 정책과의 구분

`mouse=off/auto/on`은 마우스를 **누가 소비할지** 결정하는 기존 정책이다. 여기서 `auto`는 프로그램·플러그인의 마우스 요청에 따라 캡처한다는 뜻이며, 호스트 기능 지원의 자동 판정이 아니다. `features.pixel_mouse=on`도 `mouse=off`를 무시하거나 캡처를 새로 켜지 않는다.

`clipboard=off/write/read-write`는 프로그램의 접근 권한이다. 기본 `write`는 그대로 유지한다. `clipboard_protocol=on`이나 지원 응답으로 읽기 권한이 생기지 않는다. 이 기능을 꺼도 별도로 허용한 OSC 52 복사나 선택한 시스템 provider까지 끄는 것은 아니다. 전체 프로그램 접근을 막으려면 `clipboard=off`를 사용한다.

색 깊이는 별도 `color_depth=inherit/1/4/8/24`가 단독으로 소유한다. 종전의 `auto`는 지원 질의가 아니라 PTK 환경 상속이었으므로 `inherit`로 이름을 바꿨다. 이전 `color_depth="auto"` 값은 오류로 거부하며 자동 변환하지 않는다. 기본 `inherit`는 `NO_COLOR`, `PROMPT_TOOLKIT_COLOR_DEPTH`, PTK의 TERM 기본값을 따른다. 터미널 이름으로 RGB를 승격하지 않는다. 명시한 `24` 등이 환경값보다 우선한다.

## 구현 경계와 진단

`terminal.features`가 기능 이름·선택값·기본값·자동 선택 의미를 소유한다. `Settings`와 CLI는 이를 재사용한다. `HostBroker`가 실제 응답과 수명, 초기 상태와 크기를 소유하고 하나의 계산 경로에서 실행 profile을 만든다. renderer는 그 결과를 집행한다. libghostty의 OSC/SGR 해석이나 PluginManager를 새로 만들지 않는다.

호스트 이름은 일반 기능을 허용하지 않는다. 기존 XTerm 407의 실제 픽셀 여백 보정, Kitty의 동적 커서 색 구별 같은 검증된 차이만 `hosts.py`에 남긴다. 알려지지 않은 터미널도 동일한 질의에 응답하면 `auto` 기능을 사용할 수 있다.

런타임 진단은 기능마다 다음을 구분한다.

1. `policy`: 사용자가 요청한 값 또는 기본값.
2. `support`: 지원 질의 없음(`unqueried`), 무응답(`unknown`), 긍정(`supported`), 부정(`unsupported`).
3. `active`: 필요한 조건을 만족해 현재 사용하도록 선택했는지.
4. `reason`: 비활성화 또는 선택 이유. 화면 크기 유실·원래 상태 미확인 등을 구별한다.

`active`는 해당 내용이 지금 화면에 있다는 뜻이 아니다. 예를 들어 픽셀 마우스 기능을 사용할 수 있어도 실제 캡처가 꺼져 있을 수 있다. 실제 입력 모드 활성·변경 기록은 기존 소유자가 별도로 관리한다. 최초 기능 조회가 끝난 뒤 지연·중복 지원 응답으로 기능을 다시 켜지 않는다.

창 크기 변경 때 커널이 픽셀 크기를 제공하지 않으면 별도의 크기 조회만 반복한다. 최초 CSI 16t·14t 양쪽 응답이 확인된 세션에서 한 쌍만 진행하며, 연속 resize는 최신 크기로 합친다. 이전 쌍의 응답을 모두 소비하기 전 다음 쌍을 보내지 않고, 셀 크기 × 행·열과 전체 픽셀 크기가 정확히 일치해야 적용한다. 그동안 PTY 출력 읽기에 역압을 걸고 native 이미지 원본을 보존한다. 입력은 기존 PTY로 전달된다. 검증 실패나 제한 시간 초과 시 이미지를 끄고 텍스트 출력을 재개하며, 해당 세션에서 다시 질의하지 않는다.

이 크기 프로토콜에는 wire 요청 ID가 없다. 로컬 세대 번호는 타이머와 resize 순서만 구별하며, 호스트가 질의 순서대로 한 번씩 응답한다는 전제가 있다. 뒤늦게 복제된 옛 응답이 새 쌍의 값과 우연히 일치하는 경우까지 구별한다고 보장하지 않는다. 제한 시간은 측정 실패 판정에만 사용하며 셸 readiness를 추정하지 않는다.

`--diagnose`는 터미널을 조회하지 않고 rc도 실행하지 않는다. 기본값·CLI 요청만 보여 주며 실제 관찰은 수행하지 않았다고 표시한다. 실제 세션 결과는 `--log-level INFO`의 기능별 진단에서 확인한다. 입력·출력·클립보드 내용은 일반 파일 로그에 기록하지 않는다.

## 근거와 검증 범위

- [Kitty 키보드 프로토콜의 질의와 push/pop](https://sw.kovidgoyal.net/kitty/keyboard-protocol/).
- [Kitty graphics 지원 질의](https://sw.kovidgoyal.net/kitty/graphics-protocol/).
- [OSC 5522 지원 질의와 권한 구분](https://sw.kovidgoyal.net/kitty/clipboard/).
- [OSC 8 명세의 지원 감지 한계](https://gist.github.com/egmontkob/eb114294efbcd5adb1944c9f3cb5feda).

자동 회귀는 독립적인 wire 응답, 실제 PTK 출력, controlling PTY의 최종 소비자·파일 결과를 검증한다. Windows Terminal/WSL에서는 실제 호스트 응답과 창 크기 변경을 별도로 확인한다. 자동 wire 검증을 실제 클릭·폰트·픽셀 검증으로 보고하지 않는다.

## Windows Terminal/WSL 결과

다음 숫자는 단일 `ish`로 통합하기 전의 기능 정책 검증 기록이며 삭제된 stream 검사기를 현재 실행하라는 안내가 아니다. 당시 전체 자동 회귀는 `tools/check_terminal.py` **540개**, `tools/check_stream.py` **387개**가 모두 통과했다. skip은 0이며 두 검사기의 Ruff·format 검사도 통과했다. 최종 원문은 `.cache/vt-feature-policy/terminal-verified.log`, `stream-final.log`에 보존했다. 필수 모듈 등록과 독립적인 누락 검사, FP01 계약의 실제 PTK/정책 회귀도 추가했다. 독립 재검토에서 남은 확정 결함을 찾지 못했으며, 이 결론은 아래의 검증 범위에 한정한다.

Windows Terminal 1.24.12741.0, libghostty-vt 1.0.1 / bridge ABI 4, Bash 5.3.9·zsh 5.9·tcsh 6.24.13·Vim 9.1에서 확인했다. 실제 호스트가 질의에 응답하며 테스트가 가짜 지원 응답을 주입하지 않는다.

| 검사 | 결과와 증거 |
|---|---|
| 세 셸 × UI 없음/선택 플러그인 | 6개 통과. continuation·heredoc·paste·native stdin·사용자 rc 재로딩·job control·팝업·Vim·resize. `.cache/vt-feature-policy/host-retry/summary.json` |
| 세 셸 반복 출력 | 3개 통과. 셸마다 Unicode 1,280행의 전체 history, 팝업 4회·resize 8회 확인. 위 일반 검사와 합쳐 실제 viewport 변경 36회 |
| 세 셸 × 형식 on/off | 6개 통과. 링크·확장 밑줄·밑줄 RGB·overline·커서 색의 host-directed wire, 정확한 stdin 파일, 기본 clipboard write와 UI 없음 확인. `features-final/summary.json` |
| 정상·처리 가능한 오류·중지/재개 복원 | 15개 통과. 실제 초기 mode·cursor·SGR·termios 복원. `restore-final/summary.json` |
| 명시적인 24-bit 색 | 1개 통과. 독립적인 RGB 입력과 실제 host-directed SGR 일치. `color-final/result.json` |

위 짧은 경로는 모두 `.cache/vt-feature-policy/` 아래에 있다. 이 호스트에서는 2026 동기화 출력은 `supported`, 확장 키·Kitty graphics는 응답이 없어 `unknown`, 1016·5522·2031은 부정 응답으로 `unsupported`였다. 이 구분을 유지하며 이름 `unknown`인 호스트에서도 명시한 링크·스타일과 복원 가능한 커서 색 출력은 사용할 수 있었다. 물리적인 링크 클릭, 글꼴별 밑줄 모양, 다른 호스트의 이미지 표시나 키보드 부분 구현까지 검증한 결과는 아니다. 실제 Windows 클립보드 왕복의 별도 미검증 상태도 이번 전송 정책 검사로 바꾸지 않는다.

첫 실행의 실패도 보존했다. pyte가 colon SGR을 글자로 표시하거나 넓은 문자의 머리만 덮어쓴 중간 화면에서 빈 tail을 읽다가 실패했다. 시험용 관찰기에서만 pyte의 표현을 보강하고 원본 출력은 보존했다. 605,085바이트의 실제 실패 출력을 재생해 Vim 결과까지 확인하고 독립적인 분할 입력·열 위치 회귀를 추가했다. 로그의 표시 문구를 raw 연속 문자열로 요구하던 테스트도 독립 화면 확인과 실제 입력 소비 검증으로 바꿨다. 최초 `host-final/` 및 `terminal-final.log`의 실패를 삭제하지 않고 새 실행으로 전체 결과를 확인했다.

재실행 동안 제품·번역·샘플·native 파일 41개의 해시는 변하지 않았다(`product-before.json`, `product-after.json`). UI와 셸 동작을 시험 관찰기의 수정으로 대체하지 않았다. GitHub Actions 실행과 다른 터미널 GUI의 검증은 이번 로컬 결과에 포함하지 않는다.
