# 셸 hook 없는 VT 실험

`codex/hookless-vt`는 `experiment/prompt-stream`의 `04a3d4e`에서 분기한 별도 worktree다. 기존 브랜치와 폴더는 수정하지 않는다. `ish` 명령은 기존 출력 직접 전달 경로이며, 이번 실험은 `ish-vt`에서만 실행한다. tmux나 Zellij를 실행하지 않는다.

2026-11-17의 보완 모듈·마우스·설정 보강과 최신 검증 범위는 [리팩토링 결과](VT_IMPLEMENTATION.md)에 기록한다. 아래 2026-11-16 결과는 최초 hookless 실험의 기록이다.

## 실행

Linux/WSL에서 저장소 루트를 기준으로 실행한다. 셸과 Vim은 시스템에 설치되어 있어야 한다. Windows Terminal에서는 WSL 프로필에서 실행한다.

```sh
uv sync --frozen --group dev
uv run --no-sync python tools/build_ghostty_vt.py

# 기본형: 전체 셸 화면, 추가 UI와 UI 전용 예약 키 없음
uv run --no-sync ish-vt --shell tcsh
uv run --no-sync ish-vt --shell bash
uv run --no-sync ish-vt --shell zsh

# 명시적인 외부 플러그인: 아래 한 줄과 F2 팝업
uv run --no-sync ish-vt --shell tcsh \
  --plugin-dir examples/terminal_plugins \
  --plugin terminal_bar --plugin terminal_popup
```

`--cwd DIR`로 시작 디렉터리를 고른다. 기본 시작 인수는 `-i`이며 `--login` 또는 `--shell-arg`로 변경할 수 있다. 공통 `.ishrc.py`의 시작 설정을 재사용하고 VT 전용 선택은 `config.vt`에 둔다. 언어·플러그인·마우스·스크롤백·로그 옵션은 [VT 사용 설정](VT_USAGE.md)을 참고한다. 네이티브 Windows와 macOS 실행은 이번 검증 범위에 없다.

플러그인은 기존 PluginManager가 읽는다. `--plugin NAME`은 중복 없이 명시한 이름을 선택하며, 기본 스크립트·라이브러리 경로는 공통 PluginManager 설정을 따른다. `--plugin-dir DIR`, `--library-dir DIR`로 경로를 지정할 수 있다. `module:setup` 실험 로더는 제거했다. 플러그인은 `PLUGIN_META`, `TERMINAL_API = 1`, `setup(api)`를 제공한다. 변경은 새 세션에 적용된다.

- `terminal_bar`만 선택하면 한 줄만 예약하며 F2를 가로채지 않는다.
- `terminal_popup`을 선택하면 F2는 전역 팝업 키다. 셸 primary·continuation·heredoc, 실행 중 프로그램 및 Vim에서도 ish가 처리한다.
- 팝업에서 Enter는 팝업의 결과를 확정하고 Escape는 닫는다. 결과를 셸에 입력하거나 명령으로 자동 실행하지 않는다.
- 프로그램은 팝업 중에도 계속 실행된다. 이전에 PTY에 보낸 입력은 회수하지 않는다. 팝업 이후의 입력만 팝업이 소비한다.

## 재사용과 책임

기존 `src/ish/terminal`의 PTY session, libghostty-vt 1.0.1 bridge, 입력 인코더, 화면·스크롤백·선택, PTK 합성 및 호스트 연결을 재사용한다. 셸의 rc 파일을 변경하지 않고 PROMPT_COMMAND, precmd, postcmd, ZLE 또는 프롬프트 마커를 설치하지 않는다. 명령줄·보조 프롬프트 편집과 자동완성은 셸이 계속 담당한다.

libghostty-vt가 셸 화면을 보관하고 PTK가 플러그인 표면을 합성한다. PTK가 할당한 셸 영역 크기를 VT와 실제 PTY에 함께 전달한다. 전체 화면 지우기와 alternate screen은 이 영역 안에서 처리된다. 플러그인은 호스트 writer나 셸 실행 dispatcher를 소유하지 않는다.

## 검증

```sh
uv run --no-sync python tools/check_terminal.py
uv run --no-sync python tools/check_stream.py
```

terminal 검사기는 필수 도구·native library·모듈 누락, 빈 테스트 및 skip을 실패로 처리한다. 실제 PTY/PTK 테스트에서는 최종 파일과 입력 소비자, 독립적인 화면 관찰을 확인한다. 실제 Windows Terminal 검증은 별도의 `tests/terminal_matrix.py`가 호스트 응답을 왕복 전달하여 수행한다. 프로그램이 만든 진단 상태는 테스트 증거이며 production 입력 권한을 판정하는 데 사용하지 않는다.

Windows Terminal에서는 다음 명령으로 동일한 여섯 조합을 실행한다. 각 조합의 일반 입력 프로그램과 Vim 단계에서 실제 창 또는 pane 크기를 바꾼 뒤 Enter를 누른다. 테스트 입력은 자동 전달되며 개인 rc 파일을 대신하는 임시 HOME을 사용한다.

```sh
uv run --no-sync python tests/terminal_matrix.py .cache/hookless-vt/host-check --resize-each
```

2026-11-16 검증 환경은 Windows Terminal 1.24.12741.0, Ubuntu/WSL, Python 3.12.14, prompt_toolkit 3.0.53, libghostty-vt 1.0.1, Bash 5.3.9, zsh 5.9, tcsh 6.24.13, Vim 9.1이다.

- 실제 Windows Terminal에서 셸 세 개 × 기본형/플러그인형 여섯 조합이 통과했다. 입력은 테스트 제어기가 outer PTY에 전달하고, 터미널 질의에는 실제 호스트 응답만 사용했다.
- 각 조합에서 primary·continuation·heredoc, 원래 자동완성, 여러 줄 붙여넣기, rc 재로딩, 실행 중 stdin, 팝업, Vim, Ctrl+C/Ctrl+D, 정상 종료를 검사했다. 본문·편집·팝업 결과는 최종 파일 바이트로 확인했다.
- 첫 실행에서는 테스트 전용 pane 추가·종료로 실제 viewport를 120×36 → 82×36 → 120×36으로 바꿨다. 최종 재실행에서는 높이를 120×36 → 120×24 → 120×36으로 바꿨다. 여섯 조합을 각각 두 번, 크기 변경은 총 24회 검증했다. 일반 프로그램의 SIGWINCH 기록과 실행 중 Vim의 `&lines`/`&columns`로 전달을 확인했고, 플러그인형은 하단 바 한 행을 제외한 크기를 받았다.
- 여섯 조합 모두 PTY와 호스트의 termios 설정이 복원됐다. 화면 픽셀의 육안 비교, 물리 키보드·IME 입력 및 선택적 Kitty/Xvfb 테스트는 이번 실제 호스트 검사에 포함하지 않았다.

검증 중 zsh 관련 테스트의 관찰 경합 두 개도 고쳤다. VT 검증기는 성공 조건을 확인한 뒤 재평가하면서 순간적인 화면 변경을 실패로 처리했고, stream 검증기는 사용자 hook의 마지막 파일 기록 전에 비교했다. 제품의 렌더러·셸 연동 변경 없이 관찰 완료 조건을 고쳤다. 독립 회귀와 VT zsh 전체 시나리오 8회, stream 해당 검사 10회 반복이 통과했다. 최초 실패 자료도 지우지 않았다.

실제 호스트 결과는 `.cache/hookless-vt/host-confirmed/summary.json`과 조합별 `result.json`, `resize-native-program.json`, `resize-vim.json`에 있다. 실행 소스 해시는 `source-manifest.json`, 초기 너비 변경 결과는 `host-final`에 보존했다. GitHub CI 및 다른 호스트에서의 실행 여부와는 별개의 로컬 검증이다.

최종 `tools/check_terminal.py`는 324개, 기존 `tools/check_stream.py`는 385개 테스트가 모두 통과했다. 두 검사기의 Ruff·포맷 검사도 통과했으며 로그는 `.cache/hookless-vt/terminal-final.log`, `stream-final.log`에 보존했다. 기존 `experiment/prompt-stream` worktree는 변경 없이 clean 상태다.

## 남는 범위

이 경로는 호스트에 원본 바이트를 그대로 전달하지 않는다. 호스트별 이미지·클립보드·색상·키 프로토콜 연결은 기존 VT 구현의 지원 범위를 따른다. SIXEL 입력은 지원하지 않는다. 이번 실험은 이미지 기능을 새로 확장하거나 모든 호스트 escape code의 완전 지원을 선언하지 않는다.

화면 상태만으로 셸의 실제 편집 버퍼나 자동완성 후보를 알아내지 않는다. 셸 후보와 플러그인 후보를 결합하는 자동완성 및 공통 우측 프롬프트는 이번 범위가 아니다. 하단 바·팝업에만 셸 연동이 불필요하다는 것을 검증한다.
