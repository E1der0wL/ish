# ish-vt 보완 모듈과 기본 동작 보강

> 통합 전 구현 기록이다. 현재 제품은 VT 기반 `ish` 하나이며 `config.vt`·별도 stream 설정·`ish-vt` 명령은 제공하지 않는다. 현재 평면 설정과 실행 방법은 [사용 안내](VT_USAGE.md)를 참고한다.

> 이 문서는 이전 구현 단계의 기록이다. 이후 기본 계약·종료 복원·ABI 4 변경과 검증은 [기본 계약 보강 결과](VT_BASELINE_RESULT.md)를 참고한다.

2026-11-17, `codex/hookless-vt` 작업본. [변경 전 지원 명세](VT_BASELINE_AUDIT.md)의 ID를 유지한다. 이 문서는 이번에 연결한 기능과 실제 검증 범위를 기록하며, 모든 B/P 항목과 모든 호스트가 인증됐다는 선언이 아니다. `ish` 2.0.0의 stream 경로와 별도인 `ish-vt`에 적용한다.

## 구조

```text
셸 PTY 출력
  → 공통 제어문 프레이밍
  → 프로토콜별 단일 담당자
      native: libghostty
      보완: PluginManager로 선택한 adapter → 검증된 native 입력/PTY 응답
  → libghostty의 단일 화면·커서·history·선택 상태
  → 기존 PTK renderer + 선택한 UI + 호스트 출력 adapter
```

`engine.py`가 검증한 native 기능을 우선한다. `inputs.py`가 담당자를 선택하고 `extensions.py`가 등록 충돌, 세션 고정, 실패·취소·종료를 집행한다. `Application`은 포커스, `Session`은 PTY 수명과 쓰기, libghostty는 화면 상태를 계속 소유한다. 플러그인에 별도 호스트 writer나 두 번째 화면을 제공하지 않는다.

기존 iTerm 이미지 변환기를 같은 registry에 연결했다. 외부 등록은 native OSC parser가 모른다고 확인한 숫자 OSC 명령부터 제공한다. APC/DCS/CSI 전체를 공개하거나 미확인 바이트를 호스트로 전달하는 방식이 아니다. factory·handler는 동기 계약이며 잘못 반환한 coroutine/Task/Future도 취소한다. 모듈 실패 후 같은 입력을 다른 해석기로 재전달하지 않는다. 실패한 route는 닫고 나머지 출력을 계속 처리한다.

업데이트한 libghostty가 동일 계약 검증을 통과하면 `engine.NATIVE_PROTOCOLS`에 이름·kind·prefix를 정확히 등록한다. 이때 보완 factory를 호출하지 않으므로 모듈과 decoder 의존성을 제거할 수 있다. 이미지 출력, 클립보드 권한, UI 포커스와 호스트 표현은 별도 책임으로 남는다. [작성·제거 절차](VT_PROTOCOL_PLUGINS.md).

고정 엔진의 `ghostty_terminal_vt_write_until()`도 검토했다. 같은 native parser의 string/ground 경계를 제공하지만 소비한 바이트는 이미 엔진에 적용된다. 따라서 클립보드 broker가 보류해야 하는 응답이나 iTerm Copy 본문을 현재 경로에서 그대로 먼저 해석시키면 순서와 수집 경계가 달라진다. 이번에는 기존 broker를 유지하고 프레이밍을 보강했다. 이후 이 API로 전체 프레이밍을 대체하려면 native clipboard callback과 보류 계약을 함께 연결해야 한다. 화면을 가진 두 번째 libghostty를 단순 scanner로 추가하지 않았다.

문자열은 완성된 record까지 보류한다. OSC/DCS의 끝에서 ESC만 도착한 순간 native가 만드는 제목 효과·응답은 다음 바이트가 들어올 때까지 늦어질 수 있다. 완성된 입력의 결과·순서 보존과 각 바이트 도착 시점의 완전한 동일성을 구분한다. ESC/CSI의 독립 C0 효과는 즉시 보존하며, adapter가 제어문을 소비하더라도 이전 미완성 UTF-8과 뒤의 바이트가 잘못 합쳐지지 않도록 경계를 유지한다. 한도 초과 record는 버리고 payload 없는 횟수를 진단한다. 이미 실행된 독립 C0 효과를 되돌리지는 않는다.

## 마우스 정책

| 정책 | 일반 셸 | 프로그램이 mouse mode를 요청할 때 | 명시적 클릭 UI |
|---|---|---|---|
| `off` | 호스트 드래그 | 호스트 드래그, 자식에 mouse report 전달 안 함 | 키보드로 조작 |
| `auto` (기본) | 호스트 드래그 | 자식 프로그램이 소비 | 요청한 동안 PTK가 소비 |
| `on` | ish 내부 선택·스크롤 | 자식 프로그램이 소비 | PTK가 소비 |

키보드 전용 팝업은 auto 캡처를 켜지 않는다. tracking과 1016 좌표 형식은 별도로 관리한다. 모드 전환·popup·reset·resize에서 이전 gesture와 native 누름 상태를 정리하며, 캡처를 끈 뒤 도착한 보고를 일반 입력으로 실행하지 않는다. `off`는 플러그인과 프로그램 요청보다 우선한다.

호스트 드래그는 현재 보이는 셀을 선택한다. libghostty 내부에만 있는 history, 팝업 뒤 글자까지 호스트가 선택하는 기능은 아니다. 선택형 `terminal_history` 플러그인은 기본 화면에서 Shift+PageUp/Down으로 내부 history를 이동한다.

## 기본 명세와 이번 변경

| 명세 ID | 변경·재사용한 기능 | 검증 또는 남는 경계 |
|---|---|---|
| IO01–04, IO08 | 기존 controlling PTY·단일 쓰기 큐·최종 출력 drain·resize·native rc 보존 | 실제 셸과 파일 결과 검사 재사용. 무한 백그라운드 출력 보존 서비스는 아님 |
| IO05–06 | native Ctrl+C/D/Z 및 종료 복원 경로 재사용 | 3셸×2UI의 Ctrl+Z→jobs→fg→동일 PID/입력 파일, 3셸 HUP/TERM/명시 PTK 예외의 termios·모드 해제·셸 회수 검사. 셸 job control과 ish 자체 suspend는 별개. SIGKILL의 정리 보장은 없음 |
| IO07, HX13 | `VT_PROCESSING_ERROR` 확인, native log 수준별 횟수 집계 | 오류 상태에서 세션 종료. native 메시지/입출력 payload는 기록하지 않음. OOM 실제 유발 대신 실패 반환 주입 회귀 |
| VT01 | 분할·취소·재시작 때 native 의미를 유지하는 프레이밍 | 소유하지 않은 DCS payload를 새 OSC로 실행하지 않도록 검사. 모든 무작위 입력 조합 증명은 아님 |
| VT03–07, VT12–13 | 엔진의 탭·origin·wrap·삽입/삭제·scroll region·ACS·reset 재사용 | 독립 literal 셀·응답 기대값 추가. 전체 DEC 매개변수 조합 검증은 아님 |
| VT02, VT08–11, VT14–15 | 기존 native 셀·색상·커서·hold·선택 수명 연결 유지. 색깊이 auto/1/4/8/24 선택 추가 | 한글/Unicode 파일과 셀은 검사. WT에서 명시 24bit 출력의 RGB 채널값 보존 확인. auto의 미확인 호스트는 PTK 기본 256색. 폰트·emoji·투명 배경·blink 등의 실제 픽셀 대조는 별도 |
| VT11 | host DECRQSS의 구체적인 커서 코드 1–6을 native 기본값에 연결 | 최초 관측 전에 모양 변경 금지, 자식 시작 전에만 기본값/reset 적용, 종료 후 관측값 복원. WT 6개 세션에서 실제 코드 6 응답 및 마지막 코드 6 출력 확인. 0/무응답은 추측하지 않음. primary 진입 전 모든 상태 보존이나 hollow block 표현은 보장하지 않음 |
| IN01–02 | ESC 지연 설정, F13–F25 및 keypad 식별을 native encoder로 보존 | 호스트가 구별해서 보낸 입력에 적용. 호스트가 같은 바이트로 보내는 키는 분리 불가 |
| IN04–06, IN13 | 기존 paste·focus·popup 소비자 분리 | 실제 입력 파일과 PTK 포커스 회귀. 모든 물리 키 배열·IME 조합·접근성은 미검증 |
| IN07, IN09–10 | off/auto/on, 클릭 UI opt-in, host drag 선택 | 일반 선택과 자식 mouse request를 분리. 1005/highlight/모든 side button 지원 추가는 아님 |
| IN12 | native search feed/tick/선택 연결, Ctrl+F 샘플 플러그인 | history·Unicode·resize·새 검색 취소 및 PTK 검색 키 경로 검사. 기본형에는 검색 UI/키 추가 없음 |
| HX01–03 | `TERMINFO_NAME=xterm-256color`, XTVERSION=`ish-vt 2.0.0` | literal 질의 응답과 크기 경로 검사. terminfo 287항목의 모든 조합 인증은 아님 |
| HX04–06, HX08 | 기존 색상·bell·제목·clipboard 정책 재사용 | 호스트의 확인된 capability를 넘겨 활성화하지 않음. WT 이미지·title·5522 등의 기존 gate가 남음 |
| HX07 E | OSC9/1337의 절대 cwd를 OSC7 URI로 정규화 | 상대 경로·제어문 포함 경로는 전달하지 않음 |
| HX12 | 외부 adapter 담당 namespace와 native 소유 경계 명시 | 알 수 없는 코드가 전부 지원되거나 전부 진단되는 것은 아님 |
| UI01–05 | 기존 renderer·bar geometry·popup 합성·PluginManager 재사용 | 등록한 bind/task 실패 격리. 임의 widget renderer/mouse callback까지 격리한다는 계약은 아님 |
| UI06–07 | `.ishrc.py`, 언어, CLI, version/diagnose, 로그, history·ESC·clipboard 설정 | stream 공통 구현 재사용. 이미지/프레이밍의 모든 내부 한도를 사용자 조절값으로 노출하지 않음 |
| UI08 | full-screen 종료·history 정책 문서화 | 종료 후 호스트의 기존 primary 화면으로 복귀. 내부 history 이관 없음 |
| UI09 | 호스트/PTK의 접근성 경계 유지 | 실제 screen reader·IME·모든 폰트/배율은 미검증. 제품 완성으로 판정하지 않음 |
| IM08 | 기존 이미지 오류/host gate 유지 | 이미지 디코딩 성공과 호스트 표시 성공을 구분. WT의 SIXEL을 새로 연결하지 않음 |

개별 B 항목의 기존 회귀를 위 기능군 설명으로 대체하지 않는다. [변경 전 명세의 전체 목록](VT_BASELINE_AUDIT.md)과 [terminfo 카탈로그](VT_TERMINFO_CATALOG.md)는 누락 점검 기준으로 보존한다.

## 제품 사용

기존 `~/ish/.ishrc.py`의 `config.vt`에 VT 정책과 플러그인 선택을 저장한다. 공통 설정 → VT 설정 → CLI 순서로 적용한다. `config.plugins`는 기존 stream 선택으로 남기며 VT에서 자동 로드하지 않는다. 플러그인 선택이 없는 기본형은 바·팝업·검색 UI·UI 전용 단축키·셸 hook을 설치하지 않는다.

언어는 명시한 설정(CLI 우선), `LANG`, 영어 순이다. help/version/diagnose는 rc·플러그인·native 라이브러리를 실행하거나 파일을 생성하지 않는다. 파일 로그는 opt-in이며 native 본문 대신 심각도 및 프로토콜 실패 횟수를 기록한다. 진단 콘솔 출력은 터미널을 복원한 뒤 수행한다. [실행·설정 예제](VT_USAGE.md).

libghostty-vt는 고정된 1.0.1, private bridge는 ABI **3**다. 이전 ABI를 잘못 로드하면 실행 전에 재빌드를 요구한다. `python tools/build_ghostty_vt.py --bridge-only`로 기존 고정 엔진을 재사용해 bridge를 빌드할 수 있다.

## 검증 절차와 결과

`tools/check_terminal.py`는 필수 테스트 모듈 목록과 독립 기대 목록을 함께 검사한다. 누락·빈 테스트·skip은 실패다. `.github/workflows/terminal.yml`은 동일 명령을 사용한다. 실제 GitHub 실행과 로컬 실행은 구분한다.

실제 Windows Terminal 검증은 자동 PTY 검사와 구분하여 `.cache/vt-modules/`에 원본 결과를 보존한다. 임시 홈과 전용 터미널 창을 사용하며 개인 rc·호스트 설정을 수정하지 않는다.

환경은 Windows Terminal 1.24.12741.0, Ubuntu/WSL, Python 3.12.14, PTK 3.0.53, libghostty-vt 1.0.1/ABI 3, Bash 5.3.9, zsh 5.9, tcsh 6.24.13, Vim 9.1이다.

| 검사 | 결과와 근거 |
|---|---|
| VT 전체 회귀 | **400개 통과**, Ruff·포맷 검사 통과. `terminal-confirmed.log`에 기록. 필수 23개 VT 모듈과 공유 PluginManager 2개 모듈, skip 없음 |
| stream 전체 회귀 | 385개 통과, `stream-final.log`. 공유 시작 경로·번역의 후속 관련 검사도 통과 |
| 설치 wheel | `package-confirmed.log`: 저장소 밖 설치, 한국어 도움말/진단, 조회 부작용 차단, provider 리소스 확인 통과 |
| 실제 WT 최종 행렬 | `host-final/summary.json`: Bash/zsh/tcsh × 기본형/플러그인형 6개 통과. continuation, heredoc, 여러 줄 paste, native stdin/Tab, rc 재로딩, 팝업, Ctrl+C/D, Ctrl+Z/jobs/fg, Vim, 종료 복원 |
| 실제 resize | `host-final/resize-actions.json`: 6개 조합 각각 raw 프로그램과 Vim에서 2회, 총 12회. 120×36 → 120×24 → 120×36. child SIGWINCH와 Vim 크기/파일 결과 확인 |
| 실제 커서 응답 | `host-final/observations.json`: 6개 모두 실제 코드 6 관측, 종료 후 코드 6 출력, outer/host termios 복원. 화면 픽셀 확인과 별개 |
| 실제 RGB 출력 | `host-color24-final/result.json`: 명시 `--color-depth 24`에서 전경 2종·배경 1종의 원래 RGB 채널값이 outer 출력에 보존. 다른 host gate는 그대로 유지 |
| native 차등 검토 | 3,952개 분할 trace/23,314개 feed 경계 및 추가 6,000개 deterministic 입력 비교. 명시한 string 종료 시점 차이 외 추가 차이 없음. 발견한 UTF-8/빈 feed 문제는 literal 회귀로 유지 |
| 실제 물리 마우스·검색 키 | **미완료**. `manual-final/`은 첫 확인 입력을 받지 못해 1,800초 제한 시간으로 실패 기록. 실제 drag/clipboard 결과를 자동 mouse bytes 검사로 대신하지 않음. computer-use 목록에 Windows Terminal 창이 없어 직접 조작 불가 |
| PR CI / 선택 GUI | workflow는 추가했으나 GitHub에서 실행하지 않음. 별도 Kitty GUI·픽셀·IME·screen reader 검증은 이번 결과에 포함하지 않음 |

최종 전체 회귀 전의 실패 기록도 보존한다. `terminal-final.log`의 3개 실패는 필수 모듈 수의 오래된 기대값과 로컬 마우스 두 검사의 과거 always-on 전제였다. 두 검사는 `--mouse on`을 명시하도록 분리했고, 기본 fixture와 auto/off 검사를 유지했다. 제품 코드를 과거 정책으로 되돌리지 않았다. 변경 도중 실행해 신구 Python 클래스가 섞인 앞선 `terminal-regression.log`는 최종 회귀 증거로 사용하지 않는다.

현재 코드/lock/검증기/native 라이브러리 157개 파일의 SHA256은 `source-final.json`에 보존한다. 개인 설정과 클립보드 원문은 manifest에 포함하지 않는다. 원래 `experiment/prompt-stream` worktree는 clean 상태를 유지했다. 이번 변경은 아직 커밋하지 않았다.

```sh
python tools/check_terminal.py
python tools/check_stream.py
python tests/terminal_matrix.py .cache/vt-host-new --resize-each
python tests/terminal_interaction.py .cache/vt-mouse-new --with-search
```

마지막 검사는 실제 드래그·복사·클릭·검색 키 입력을 요구한다. 클립보드 원문을 저장하지 않고 해당 검사의 고유 문구 포함 여부만 기록한다. 소스 manifest, 도구 버전, 테스트 결과, 실제 host reply와 최종 소비 파일을 따로 보존한다.

## 계속 남는 책임과 제한

- libghostty 지원 업데이트를 검증하고 native 계약/ABI를 갱신하는 작업은 ish가 맡는다. 보완 모듈을 쉽게 제거할 수 있다는 뜻이지 자동 업그레이드나 유지보수 면제를 뜻하지 않는다.
- PTK 표현·팝업 가림·호스트 출력·키 변환·PTY 수명·클립보드 정책은 ish 책임이다. 외부 Python 플러그인은 같은 사용자 권한의 신뢰 코드이며 sandbox가 아니다.
- 공개 보완 등록은 OSC부터 제공한다. DCS/APC/CSI 확장은 별도 소유권·취소·한도 계약을 입증해야 한다. SIXEL은 미지원이다.
- 호스트가 지원하지 않거나 실제 capability를 확인하지 못한 이미지·확장 글자·클립보드 기능은 계속 제한한다. libghostty가 이해한다는 이유만으로 WT가 표시한다고 선언하지 않는다.
- 임의 초기 호스트 모드의 완전한 snapshot 복원, ish 자체의 외부 TSTP/CONT, 모든 물리 IME/접근성/픽셀 조합은 아직 보장하지 않는다. 정상 종료·HUP·TERM·검사한 예외 복원과 구분한다.
- 화면만 보고 셸 편집 버퍼를 추정하지 않는다. 자동완성 후보 취득·적용 및 우측 명령 프롬프트는 이번 보완 모듈 API로 제공하지 않는다.
