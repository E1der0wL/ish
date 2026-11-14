# VT 화면 합성과 native 셸 편집 실험

이 문서는 `libghostty-vt`의 화면 상태를 PTK 전체 화면에 표시하고, 실제 명령줄 편집은 셸에 맡기는 별도 실험의 범위와 자동완성 조사 결과를 기록한다. 조사 기준일은 2026-11-14다. 이전 `refactor/editor-request` 브랜치의 PTK primary/continuation 편집 경로는 별도 구현으로 보존한다.

## 기본형의 계약

입력과 출력은 다음 경로를 사용한다.

```text
외부 터미널 입력
        ↓
하나의 PTK Application
        ↓
       PTY → native 셸 또는 foreground 프로그램

셸/프로그램 출력 → PTY → libghostty-vt 화면 상태
                               ↓
                    PTK 전체 터미널 화면
                               ↓
                          외부 터미널
```

PTK가 외부 입력과 최종 화면 출력을 소유하고, 셸이 명령줄 버퍼와 continuation을 소유한다. 셸의 출력을 외부 터미널에 직접 쓰는 우회 경로는 만들지 않는다. 기본형에는 하단 바, 추가 우측 프롬프트, 팝업 또는 전용 단축키가 없다. 셸과 프로그램이 전체 행·열을 사용한다. 추가 UI는 향후 플러그인 범위이며 현재 플러그인 API를 새로 제공하지 않는다.

기본 전경·배경은 외부 터미널의 기본색을 사용한다. 여기서 투명 배경은 고정 검정색을 칠하지 않고 외부 터미널의 배경 설정에 맡긴다는 의미다. 창 자체의 투명도는 외부 터미널이 결정한다. 셸이나 프로그램이 명시한 배경색은 보존하며, 색상이 검정이라는 이유로 기본색으로 치환하지 않는다.

프로그램이 OSC 10/11로 바꾼 기본 전경·배경은 명시된 색으로 유지하고 OSC 110/111로 초기화하면 외부 기본색으로 돌아간다. ANSI 팔레트 색상은 현재 libghostty-vt가 해석한 RGB를 사용한다. 외부 터미널의 사용자 팔레트까지 완전히 동기화하는 기능은 아직 없으므로 모든 테마 색상이 동일하다고 보장하지 않는다.

첫 대상은 Linux x86_64/WSL의 tcsh다. 새 worktree에는 독립적인 `.venv`와 실제로 빌드한 native library가 준비되어 있다. WSL에서 다음과 같이 실행한다.

```sh
cd /mnt/c/Users/user/.codex/worktrees/ish-ghostty-ptk/ish
PYTHONDONTWRITEBYTECODE=1 .venv/bin/ish-vt --shell /usr/bin/tcsh
```

새 checkout에서는 Python 3.12.14 이상(3.13 미만), C 컴파일러, tcsh와 테스트용 Vim이 필요하다. 다음 명령은 의존성과 native library를 해당 checkout에 설치한다. 빌드 도구는 SHA256으로 고정한 공식 libghostty-vt 1.0.1과 Zig 0.17.1을 내려받아 ReleaseFast, baseline CPU, SIMD 비활성화 설정으로 빌드한다. 시스템 셸이나 전역 Python 환경을 교체하지 않는다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python tools/build_ghostty_vt.py --cache-dir /tmp/ish-ghostty-ptk-build
```

`--cache-dir`는 다운로드·빌드 작업 위치다. 결과는 checkout의 `.cache/ghostty-vt/install/lib`에 설치한다. WSL에서는 Linux 파일시스템의 캐시를 권장한다. `ISH_GHOSTTY_LIB`를 지정할 경우 raw libghostty가 아니라 이 실험의 C bridge인 `libish_vt.so` 경로를 전달한다. 기본값은 checkout에 빌드한 bridge다. `.venv`와 `.cache`는 Git에 넣지 않는다. 공식 라이브러리의 역할은 [libghostty-vt 문서](https://lib.ghostty.org/vt/index.html)를 참고한다.

기본색과 viewport 상태를 보존하기 위해 C bridge의 화면 레코드가 확장되었고, 스크롤·마우스·선택 API도 추가되었다. 이전 실험에서 빌드한 `.so`를 사용하는 checkout은 위 build 명령을 다시 실행해야 한다. 현재 작업 폴더의 라이브러리는 변경된 코드에 맞춰 재빌드했다.

| 조작 | 기본형의 의미 |
| --- | --- |
| 일반 문자·Enter·방향키·붙여넣기 | 현재 출력 화면으로 돌아온 뒤 PTY로 전달한다. 명령 편집과 제출은 셸이 처리한다. |
| Tab | native 셸에 전달한다. 셸의 기존 키 설정과 completion 동작을 사용한다. |
| F2 / F10 / Esc | 다른 키와 마찬가지로 셸이나 foreground 프로그램에 전달한다. ish UI 단축키로 예약하지 않는다. |
| 휠 | 일반 화면에서는 이전/다음 출력 3줄을 탐색한다. live 화면에서 프로그램이 마우스를 요청했다면 그 프로그램으로 보낸다. |
| Shift+PageUp / Shift+PageDown | primary 화면의 출력을 화면 높이에서 한 줄을 뺀 만큼 탐색한다. alternate 화면에서는 프로그램에 원래 키를 전달한다. |
| 왼쪽 드래그 | 프로그램이 마우스를 요청하지 않았거나 과거 출력을 보고 있다면 텍스트를 선택한다. 놓으면 내부 선택 텍스트를 저장하고 외부 클립보드 복사를 요청한다. |
| 가운데 버튼 | 로컬 선택 경로에서는 ish에서 마지막으로 선택한 텍스트를 native paste로 보낸다. 프로그램 마우스 모드에서는 프로그램에 전달한다. |
| Ctrl+C / Ctrl+D | PTY로 전달한다. 취소·EOF의 의미는 셸, 프로그램, PTY 설정이 결정한다. |
| 종료 | 셸의 `exit` 또는 셸이 허용하는 Ctrl+D로 종료한다. |

키 시퀀스 구분에 쓰는 PTK timeout은 셸 readiness 판단과 무관하다. 이전 커밋 `1d0e605`의 바·팝업 실험은 Git 이력에 남아 있으며 기본 실행에서는 제거했다.

## 스크롤백과 마우스

출력 기록과 viewport는 libghostty-vt가 관리한다. Python에서 화면 문자열을 다시 저장해 history를 만들지 않는다. 기본 보관 한도는 10,000줄과 32MiB이며 native page 단위의 근사 한도다. ish 실행 전에 외부 터미널에 쌓인 출력은 이 history에 포함되지 않는다.

과거 출력을 보는 동안 새 출력이 와도 보고 있던 위치를 유지한다. 키 입력·붙여넣기를 하면 live 화면으로 복귀한다. 커서는 과거 화면에서는 숨기고, live 화면에서 다시 표시한다. resize와 reflow, history 제거에 따른 위치 보정도 VT에 맡긴다. alternate screen은 primary history와 분리한다. 프로그램이 마우스를 요청하지 않은 alternate 화면에서 1007 모드가 켜져 있으면 휠을 위/아래 방향키로 보낸다.

마우스는 외부 터미널의 SGR·X10·urxvt 이벤트를 물리 셀 좌표로 읽는다. PTK의 문자열 index로 바꾸지 않으므로 한글 뒤의 클릭 위치도 달라지지 않는다. 프로그램의 1000·1002·1003 등 모드와 보고 형식은 libghostty encoder가 적용한다. 실제로 전달한 press는 같은 소비자에게 release하고, history로 이동할 때는 마지막 live 좌표에서 먼저 release한다. 과거 화면의 클릭·드래그는 실행 중 프로그램으로 보내지 않는다.

로컬 드래그 선택의 anchor는 native tracked grid reference로 관리하며 출력·reflow 후에도 같은 내용에 연결한다. 선택 텍스트도 VT formatter에서 얻어 줄바꿈·Unicode 의미를 보존한다. 선택 영역 표시는 셀 강조만 사용하며 바나 팝업을 추가하지 않는다. 더블 클릭 단어 선택·트리플 클릭 줄 선택·선택 중 자동 스크롤은 아직 별도 구현 범위다.

드래그를 놓으면 PTK 출력 경로로 OSC 52 클립보드 쓰기를 요청한다(UTF-8 1MiB 이하). 실제 시스템 클립보드 반영은 외부 터미널의 OSC 52 지원·허용 설정에 따른다. 가운데 버튼은 OS 클립보드를 읽지 않고 ish의 마지막 선택 텍스트를 붙여넣는다. 외부 앱에서 복사한 내용은 외부 터미널의 일반 붙여넣기 키를 사용한다. 외부 터미널의 Shift+드래그 선택도 사용할 수 있다. Windows Terminal의 이 동작은 [공식 선택 문서](https://learn.microsoft.com/en-us/windows/terminal/selection)에 설명되어 있다. Shift 마우스 이벤트가 ish에 전달되는 경우에도 프로그램 전달 대신 로컬 선택·탐색으로 처리한다.

현재 외부 입력은 셀 좌표다. 프로그램이 1016 pixel mouse 모드를 요구하면 가짜 픽셀 좌표를 만들어 전달하지 않으며, 이 모드는 지원 완료 범위가 아니다. 상위 터미널에서 가져온 실제 pixel geometry를 연결하는 것은 별도 작업이다.

이 실험은 primary/continuation을 판정하여 편집기를 교체하지 않는다. tcsh의 보조 프롬프트도 native 편집 경로에서 처리한다. 그러므로 이는 모든 셸의 continuation을 PTK 편집기로 대체했다는 결과가 아니다. `$<`, 외부 프로그램의 stdin, Python 프로그램의 `input()`도 같은 native PTY 경로에 남는다. 새 source FIFO 실행 경로나 Python 도구 dispatch는 도입하지 않는다.

## native Tab과 PTK 자동완성은 다른 기능이다

native Tab 지원은 Tab을 셸로 보내고 셸이 그린 결과를 정상 표시하는 기능이다. 셸이 후보를 찾고, 선택 규칙과 quoting을 적용하며, 명령줄을 수정한다. PTK는 후보의 의미나 편집 버퍼를 알 필요가 없다.

PTK 자동완성 dropdown은 후보를 구조적으로 받아 표시하고, 사용자의 선택을 현재 셸 편집 요청에 안전하게 적용하는 기능이다. VT 화면의 문자와 화면 커서는 논리적인 명령줄 버퍼·편집 커서·후보 목록과 같은 계약이 아니다. 화면 문자열을 역파싱하거나, 현재 줄 길이를 추정해 Backspace와 문자열을 보내는 방식으로 이 차이를 메우지 않는다.

기본형에는 PTK dropdown이 없다. native Tab의 성공을 PTK dropdown 자동완성 구현으로 보고하지 않는다. 아래 Warp와 셸별 API 조사는 후속 플러그인 설계의 참고 자료다.

## Warp 공식 자료에서 확인한 범위

Warp는 자체 입력 편집기가 셸의 편집 기능을 대체한다고 설명한다. 따라서 Warp의 dropdown을, 수정하지 않은 native 셸 편집기 위에 팝업만 얹은 사례로 해석할 수 없다. 이는 공개된 동작 설명에 대한 판단이며 Warp 내부 전체 프로토콜을 분석한 결과는 아니다. [Warp 입력 편집기와 Vim 키](https://docs.warp.dev/terminal/editor/vim/)

Warp는 내장 completion과 셸 자체 completion을 별도로 설정한다. `native_shell_completions_enabled` 설명에 따르면 셸 completion은 Tab 또는 지정 키로 요청하며, 매 문자 입력마다 생성하지 않는다. 자체 편집기 소유와 셸을 후보 공급자로 사용하는 것은 양립한다. [Warp 입력 설정](https://docs.warp.dev/terminal/settings/all-settings/#input)

공식 지원 목록의 macOS/Linux 셸은 Bash, zsh, fish, PowerShell이다. tcsh는 이 목록에 없다. 따라서 Warp의 구현 가능성을 tcsh의 동일 API 존재를 입증하는 근거로 사용하지 않는다. [Warp 지원 셸](https://docs.warp.dev/getting-started/supported-shells/)

## 셸별 completion capability 설계

다음 표는 공식 API 조사에 따른 후속 설계 후보다. 이 표 자체는 ish에서의 구현 또는 live 검증 완료를 뜻하지 않는다. 첫 terminal 실험에서는 셸별 completion bridge를 활성화하지 않는다.

| 셸 | 확인한 협력 수단 | 후속 PTK dropdown의 후보 범위 | 첫 실험 정책 |
| --- | --- | --- | --- |
| Bash | `bind -x`에서 `READLINE_LINE`, `READLINE_POINT`로 편집 내용과 위치를 읽고 수정할 수 있다. | Readline callback이 요청을 유지하는 동안 후보를 선택하고 셸이 버퍼를 적용하는 bridge를 검증할 수 있다. 기존 programmable completion 결과를 모두 얻는 일은 별도 과제다. | native 키 전달 |
| zsh | 사용자 ZLE widget에서 `BUFFER`, `CURSOR`를 다룰 수 있고 completion widget 체계가 있다. | ZLE 요청 안에서 후보 조회·선택·적용을 연결할 수 있다. 기존 widget, keymap, completion 설정과의 공존 검증이 필요하다. | native 키 전달 |
| tcsh | `complete`의 외부 명령 후보 공급, `COMMAND_LINE`, `complete-word` 계열, `list-choices`, `bindkey`가 있다. | 특정 명령의 opt-in 후보 공급자와 native 적용을 조합하는 실험이 가능하다. 임의의 전체 버퍼·커서 조회/교체 API는 이번 조사에서 확립하지 못했다. | native Tab 우선 |
| BSD csh | 이번 조사에서는 tcsh와 동등한 구조적 completion bridge API를 확인하지 않았다. | 실제 버전과 편집 기능을 별도로 조사해야 한다. tcsh API를 있다고 가정하지 않는다. | native 키 전달; completion 결과는 셸 설정에 따름 |
| sh/dash | 이번 조사에서는 위와 같은 셸 편집 버퍼 bridge API를 확인하지 않았다. | 빌드·편집 라이브러리별 별도 조사 대상이다. | native 키 전달; Tab completion 자체를 보장하지 않음 |

Bash의 버퍼·위치 API는 [GNU Bash 매뉴얼](https://www.gnu.org/software/bash/manual/bash.html), zsh의 widget 및 버퍼 API는 [Zsh Line Editor 매뉴얼](https://zsh.sourceforge.io/Doc/Release/Zsh-Line-Editor.html)에 근거한다. API가 존재한다는 사실은 비동기 응답, 취소 또는 모든 사용자 completion과의 호환성까지 보장하지 않는다.

### tcsh에서 확인한 것과 아직 확인하지 못한 것

tcsh 6.24.13의 `complete`는 외부 명령의 출력을 후보로 사용할 수 있고, 그 명령에 `COMMAND_LINE` 환경 변수를 제공한다. `bindkey -c`는 명령 실행, `bindkey -s`는 키 매크로 연결 수단이다. 이것만으로 임의의 편집 버퍼와 커서를 원자적으로 교체하는 API가 생기는 것은 아니다. [tcsh 6.24.13 공식 매뉴얼](https://raw.githubusercontent.com/tcsh-org/tcsh/TCSH6_24_13/tcsh.man.in)

같은 버전 소스의 `ed.inputl.c`는 completion에 `Cursor - InputBuf` 길이를 넘긴다. `tw.parse.c`의 `tenematch()`는 그 위치까지 분석한 문자열과 현재 명령 구간을 `tw_complete()`로 보내고, `tw.comp.c`는 받은 문자열을 `COMMAND_LINE`에 설정한다. 따라서 이를 raw 전체 버퍼와 독립적인 커서 offset의 공개 계약으로 사용해서는 안 된다. 커서 뒤쪽 내용과 quoting 변환을 포함하는 실제 전달값은 별도 PTY 관찰이 필요하다. [tcsh 6.24.13 소스](https://github.com/tcsh-org/tcsh/tree/TCSH6_24_13)

소스 수정 없이 시도할 수 있는 최소 후보는 특정 명령에만 등록하는 `complete` 공급자다.

1. tcsh가 native completion을 시작하고 외부 공급자를 호출한다.
2. 공급자가 전용 IPC로 요청 identity와 후보를 보내고 선택을 기다린다.
3. PTK가 popup에서 선택을 받는다.
4. 공급자가 선택된 후보를 반환한다.
5. tcsh의 completion 코드가 원래 문맥에서 삽입과 quoting을 처리한다.

이는 설계 제안이며 아직 구현하거나 실행 검증하지 않았다. 모든 사용자 `complete` 정의를 자동으로 가져오는 범용 bridge도 아니다. 후보 공백·Unicode·따옴표, 중간 커서와 뒤쪽 문자열, 취소 시 무수정, native undo, 기존 keymap과의 공존을 실제로 검증해야 한다. 후보를 helper 표준 출력으로 되돌릴 때의 구분·escaping 규칙도 확인해야 한다.

### 기능을 활성화하기 위한 공통 조건

후속 adapter는 적어도 다음 보장을 구별해야 한다. 아직 코드에 추가된 capability 이름은 아니다.

- 입력 키를 native 경로로 전달하는 기능.
- 셸 편집 요청이 활성 상태임을 증명하고 후보를 제공하는 기능.
- 그 요청에 대한 선택을 셸이 적용하는 기능.
- 필요한 경우 전체 버퍼와 논리 커서를 조회·수정하는 기능.

셸이 후보 적용까지 담당하는 tcsh 공급자 방식에는 전체 버퍼 교체 API를 억지로 요구하지 않는다. 대신 공급자 대기 중 편집 문맥이 유지되는지 증명해야 한다. 각 선택은 세션과 실제 요청에 연결하고, 취소·지연·중복 응답은 현재 요청을 수정하지 못하게 한다. 프로그램 stdin이나 heredoc 데이터 입력에서 completion popup을 임의로 시작하지 않는다.

## 실제 구현과 검증 결과

실제 libghostty-vt 1.0.1 공유 라이브러리를 빌드하고 C bridge와 Python ctypes binding으로 연결했다. 다른 VT 구현으로 대체하지 않았다. PTY 출력은 libghostty-vt의 화면 상태로만 들어가며, PTK가 terminal pane 하나를 전체 화면에 렌더링한다. 기존 `ish` 진입점과 semantic submission 경로는 수정하지 않았다.

WSL의 실제 controlling PTY에서 tcsh를 실행했다. CLI 검증은 별도의 pyte 화면으로 PTK가 출력한 셸·Vim 화면과 셀 색상을 확인하고, 입력의 최종 소비자가 만든 파일을 독립적인 기대값과 비교한다. `--dump-state PATH`는 테스트를 위한 명시적 진단 옵션이며 화면 텍스트를 기록한다. 이 값이나 prompt 문자열을 production 입력 권한 판단에 사용하지 않는다.

스크롤백·마우스 추가 후 VT 22개, 실제 CLI 16개, 외부 마우스 디코더 8개로 총 46개 테스트가 통과했다. 입력 capture 보완 뒤에는 영향받는 실제 마우스·선택 경로만 재확인했다. session 3개는 이전 실험에서 통과한 변경 없는 검증이다. Ruff 검사와 C bridge의 `-Wall -Wextra -Werror` 빌드도 통과했다.

| 파일 | 검증 범위 |
| --- | --- |
| `tests/test_terminal_vt.py` | UTF-8·커서·색상·DSR·키·paste·resize, history 한도·viewport·출력 anchor·alternate 분리, native mouse 인코딩, Unicode 선택·reflow·stale anchor 거부 |
| `tests/test_terminal_session.py` | 실제 PTY 입력과 마지막 출력, 다른 slave FD가 열려 있어도 직접 자식 종료 감지, 쓰기 큐 초과의 오류 전달 |
| `tests/test_terminal_cli.py` | 실제 tcsh 보조 입력·heredoc·Tab·stdin·paste·기본색·F2/F10·Vim·resize·Ctrl+C/D, 화면 history·anchor·입력 복귀, mouse wire와 capture, 선택·복사 요청, Vim 클릭·휠 |
| `tests/test_terminal_mouse.py` | 독립적인 SGR·X10·urxvt wire 기대값, 버튼·이동·modifier·휠, 물리 셀 좌표, 잘못된 데이터 거부 |

실제 사용자 경로에서 확인한 결과는 다음과 같다.

- backslash continuation과 중첩 `while`/`foreach`를 native tcsh가 수집했다. 종료 구분자 전에는 파일이 실행 결과로 생성되지 않았고, 완료 후 정확히 `A`, `B`가 기록되었다.
- heredoc의 공백·한글·emoji와 이전 ish 명령처럼 보이는 이름이 데이터로 보존되었다. 빠른 Enter 입력은 실제 CR 바이트로 검증했다.
- `$<`의 명령과 stdin을 빠르게 연속 전달해 공백·한글이 정확히 소비되는 것을 확인했다. 별도 Python 프로그램의 `sys.stdin.readline()`에도 실제 입력이 전달되었다.
- tcsh native Tab이 파일 이름을 완성했고, 완성된 경로를 읽은 파일 결과가 일치했다. PTK 자동완성 dropdown을 구현한 결과는 아니다.
- F2/F10은 실제 foreground 프로그램의 stdin으로 전달된다. 기본형에 팝업이나 종료 단축키로 남기지 않는다.
- 실제 외부 화면 셀에서 기본 배경 → 명시적 검정 배경 → 기본 배경 복원을 확인했다. 색상 초기화·지우기·SGR 역상과 OSC 기본색 변경도 구별한다. DECSCNM 전체 역상과 OSC 기본색 조합은 수정 전 실패를 재현한 뒤, 기본색만 교환하고 셀 명시색을 보존하도록 수정했다.
- 셸의 한 줄 bracketed paste가 Enter 전에 실행되지 않았다. Vim에 여러 줄·한글을 붙여넣어 정확히 저장했다.
- 초기 24행 × 100열, 변경 후 33행 × 110열을 Vim과 셸에 그대로 전달했다. 하단 UI를 위한 한 줄을 빼지 않는다.
- 실행 중 프로그램의 Ctrl+C, `cat`과 셸의 Ctrl+D, 종료 후 외부 터미널 속성 복원을 확인했다.
- 실제 출력 80줄을 휠·페이지 키로 탐색하고 추가 출력 12줄 뒤에도 이전 화면 위치가 유지됨을 확인했다. resize 후에도 정확한 stdin 파일과 PTY 크기를 확인했다.
- 1000/1002/1003 SGR 및 legacy 모드에서 실제 프로그램이 받은 모든 mouse bytes를 독립 기대값과 비교했다. Shift 변경, 창 밖 release, history 진입 전 release 및 후속 중복 제거도 검증했다.
- 실제 Vim에서 클릭한 줄·열과 휠에 따른 화면 위치를 파일로 확인했고 alternate 화면에 primary history가 섞이지 않음을 확인했다.
- 드래그한 `COPY_ME`의 선택 강조, PTK가 내보낸 OSC52 요청 payload, 가운데 버튼으로 native stdin이 받은 파일을 확인했다. 외부 호스트 앱이 실제 OS 클립보드에 수락하는지는 테스트 대상 밖이다.

이전 바·팝업 구현의 검증 기록은 `1d0e605`에 보존한다. 현재 테스트는 기본형의 전체 화면과 native 입력 계약을 확인한다.

테스트 재실행 명령은 다음과 같다.

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest \
  test_terminal_vt test_terminal_mouse test_terminal_session test_terminal_cli -v
```

새 `.venv`의 editable 설치, `ish-vt --help`, native library 로드도 확인했다. C bridge는 `-Wall -Wextra -Werror` 검사를 통과했다. 화면 판독과 테스트 deadline은 관찰·실패 판정에만 사용한다.

## 남은 범위

- 빌드·실행 검증은 Linux x86_64/WSL에 한정된다. native Windows·macOS 지원이나 성능 동등성은 검증하지 않았다. 현재 native 빌드는 SIMD를 비활성화한다.
- 하단 바·오른쪽 프롬프트·팝업, PTK 자동완성 dropdown, 기존 ish 설정·플러그인 API 이식은 후속 플러그인 범위다. 기본형에는 추가 UI가 없다.
- native history·사용자 keymap 전체, continuation/heredoc 도중 Ctrl+C/D, 모든 background job·disown 종료 조합은 아직 별도 검증하지 않았다. 세션 종료는 직접 자식과 현재 foreground process group을 정리하며 전체 프로세스 트리 관리를 보장하지 않는다.
- 기본 화면·색상·커서·alternate screen·내부 scrollback·셀 단위 마우스는 연결했다. 이미지, 하이퍼링크, 모든 확장 키보드 프로토콜은 지원 완료 범위가 아니다. 복잡한 emoji 조합의 폭과 외부 터미널별 Unicode 차이도 추가 검증이 필요하다. 클립보드 및 pixel mouse 범위는 위 절에 구분했다.
- PTK는 외부 대체 화면을 사용하며 실행 중 출력은 내부 scrollback에서 탐색한다. 자식의 `TERM`·`COLORTERM`·`TERM_PROGRAM`은 가상 터미널에 맞게 설정하며, 커서 위치·표시는 전달하지만 커서 모양은 아직 전달하지 않는다. 기본 UI를 없앤 결과가 모든 터미널 동작의 완전한 동등성을 뜻하지는 않는다.
- 첫 실험에서 입증한 것은 native tcsh 편집과 PTK 화면 합성의 공존이다. 모든 셸 자동완성, 모든 VT 기능, 기존 PTK 명령 편집 API와의 동등 호환성 또는 제품 완성도를 선언하지 않는다.
