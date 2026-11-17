# 입력·클립보드 보완 모듈과 검증

2026-11-17, `codex/hookless-vt`. 별도 `ish-vt` 경로의 후속 보강이다. 기존 `ish` 2.0.0 stream 경로와 다른 작업이며, 기본형은 UI·셸 hook·UI 전용 키 없이 유지한다. 이전 [기본 계약 결과](VT_BASELINE_RESULT.md)는 당시 기록으로 보존한다.

## 적용한 계약

출력·키·클립보드 보완 모듈은 기존 PluginManager의 **한 폴더와 한 선택 목록**을 사용한다. 기본 폴더는 `~/ish/plugin/script`, 선택은 `.ishrc.py`의 `config.vt["plugins"]`다. 의존성 설치용 기존 `plugin_lib_dir`와 모듈 검색 경로를 혼동하지 않는다. 기능별 로더나 검색 경로를 추가하지 않았다.

외부 Python 모듈은 새 세션에서 적용한다. 현재 확장 API로 표현되는 변경에는 native 재컴파일이 필요 없다. 입력 framing·응답 예약·협상·포커스·PTY writer 및 화면 상태의 책임은 본체에 남긴다. 모듈에 임의 raw 입력 재전달이나 외부 터미널 writer를 제공하지 않는다. 새 native 기능은 동일 계약 검증 후 보완 모듈을 대체하며, clipboard 정책과 host 연결은 엔진 지원만으로 사라지지 않는다.

기존 설계 규칙 1–8은 유지했다. AGENTS.md에는 단일 플러그인 경로, 입력과 출력 확장의 구별, 미지원 제어문을 키로 추측하지 않는 정책, clipboard 읽기 권한과 수명의 책임을 명시했다. 제목을 유지하는 정책, 예외적 강제 종료의 완전 복원 제외, SIXEL·선택 확장 제외도 유지한다.

## 클립보드

기본 정책은 **write**다. 프로그램의 읽기는 `config.vt["clipboard"] = "read-write"` 또는 `--clipboard read-write`로 켠다. 새 `clipboard_system` 샘플을 기존 플러그인 목록에서 명시적으로 선택한다. UI나 단축키를 추가하지 않는다.

`D:\WorkSpace\ish\hub`의 X11 선택 worker를 재사용했다. 로컬 WSL은 Windows `powershell.exe`·`clip.exe`, 전달된 DISPLAY는 libX11 경로로 연결한다. SSH 세션에서는 원격 Windows/Wayland 데스크톱으로 우회하지 않는다. 선택 대상이 실패해도 다른 대상이나 내부 캐시로 바꾸지 않는다.

ClipboardBroker는 정책·identity·취소·응답 순서·대기열을, provider는 실제 OS 연결과 worker 수명을 소유한다. 일반 CLIPBOARD의 UTF-8 텍스트 1 MiB, OSC 52와 OSC 5522 `text/plain` 단일 표현을 지원한다. PRIMARY·임의 MIME·이미지·alias·실제 MIME 목록 요청은 이 provider의 지원 범위가 아니다. 후자는 `ENOSYS`로 응답한다. OSC 52 연속 요청은 최대 64개·합계 4 MiB로 제한한다.

독립 검토에서 복사 직후 읽기가 앞선 비동기 복사를 추월하는 문제와, 여러 복사 중 첫 완료에서 종료 대기가 풀리는 문제를 발견했다. 순서 있는 대기열 및 전체 대기열 완료 경계로 수정했다. 지연·취소된 요청은 다음 요청을 완료하지 못하며, 응답 writer 오류를 provider 실패로 숨기지 않고 Application 오류 경계에 전달한다. 정상 로그는 내용 없이 실패 횟수만 기록한다.

## 키 입력

외부 `register_keys`는 완성된 미소유 숫자 CSI `~`를 기존 공통 키로 변환하는 제한된 매핑이다. native/PTK 키·응답·마우스·붙여넣기·협상 영역 선점과 중복 등록을 거부한다. `terminal_keys` 예시는 호스트가 별도로 보내도록 설정한 시퀀스이며, 새 물리 키 지원이나 모든 프로토콜 해석을 주장하지 않는다.

알 수 없는 제어문을 PTK가 숫자·문자 입력으로 분해하여 프로그램 stdin에 보내지 않도록 했다. Full-screen PTK는 CPR을 요청하지 않으며, 늦은 CPR은 **키 매칭 전 개별 이벤트에서** 소비한다. 초기 수정에서 다중 키 바인딩의 정상 prefix까지 버리는 문제가 재현되어 함께 보완했다. 미완성 CSI 뒤 Ctrl+C/D·Enter·편집 키는 잘못된 prefix와 구별하여 보존한다. 입력 framing은 붙여넣기를 키 매핑하지 않고 기존 native paste 경로로 전달하며, libghostty의 기존 제어문 정리 정책을 유지한다.

legacy Shift+F3와 CPR처럼 같은 바이트로 표현되는 신호는 사용자 키로 추측하지 않는다. 구분 가능한 확장 키는 기존 협상을 사용한다. 본체 터미널이 먼저 소비한 키는 ish가 받을 수 없다. 운영 진단은 미지원 제어문 건수만 남긴다.

## 검증 기록

환경: Windows Terminal 1.24.12741.0 / WSL Ubuntu 26.04 / Python 3.12.14 / PTK 3.0.53 / libghostty-vt 1.0.1, private ABI 4. Bash 5.3.9, zsh 5.9, tcsh 6.24.13, Vim 9.1. 이 보강은 native 엔진과 bridge ABI를 추가 변경하지 않았다.

| 경계 | 결과와 의미 |
|---|---|
| VT 전체 회귀 | 503개 및 lint 통과, skip 없음 |
| stream 회귀 | 387개 및 lint 통과, skip 없음 |
| 실제 Windows Terminal/WSL | 3셸 × 기본/플러그인 6개 + 스트레스 3개 통과. heredoc, 프로그램 stdin, Ctrl+C/D/Z, Vim, 팝업, 실제 resize 36회 |
| 출력 보존 | 셸마다 Unicode 1,280행 전체 history를 독립 기대값과 비교, 팝업 4회·실제 resize 8회 뒤 누락·중복 없음 |
| 실제 호스트 종료·복원 | 3셸 × 정상/HUP/TERM/PTK 오류/중지·재개 15개 통과. 기존 처리 가능한 종료를 보존한 검사이며 SIGKILL 완전 복원 요구를 추가하지 않음 |
| 시스템 clipboard | 격리된 실제 Xvfb↔libX11↔xclip 왕복, 한글·빈 텍스트·INCR 전송, 실제 controlling PTY의 foreground 파일 검사 통과. 실제 Windows clipboard는 시작 클립보드에 텍스트 외 형식이 있어 내용 조회·변경 전에 중단. 임시 일반 텍스트 복사 확인을 요청했으며 왕복 검사는 미실행 |
| 단일 플러그인 경로 | 저장된 rc에서 출력·키·clipboard 세 모듈을 함께 불러오고 화면 결과·PTY 응답·키 파일·종료를 확인 |
| terminfo | 287개 inventory 전수 분류와 잔여 99개 대조. 11개 명시적 차이·제외를 유지. [상세 결과](VT_TERMINFO_RESULT.md) |

실제 호스트 검사는 스크립트로 outer PTY에 입력하고 **호스트가 직접 보낸 응답** 및 자식의 파일 결과·크기·termios를 확인한다. 물리 키보드/IME·픽셀 외관 검증을 수행했다는 뜻은 아니다. 이전 실제 마우스 결과도 이 실행의 새 증거로 합치지 않는다.

최초 전체 VT 실행에서는 503개 중 검사기 테스트 두 개의 고정된 기대 개수가 새 모듈 수를 반영하지 못했다. 후속 실행에서 남아 있던 파일 개수 상수도 제거하고, 독립 목록의 길이로 고쳤다. 검사기 자체 17개와 최종 전체 503개를 통과했고 두 실패 로그를 보존했다. 추가로 X11 테스트 환경이 일반 Linux에서도 무조건 unshare를 요구하던 부분을 좁혔다. 쓰기 가능한 socket 디렉터리에서는 직접 Xvfb, WSLg처럼 불가능한 경우만 private mount를 사용하며 공유 경로의 권한을 바꾸지 않는다. 두 경로의 실제 X11 검사를 각각 통과했다.

새 회귀 모듈은 명시적으로 추적 가능하게 하고 strict 검사기와 독립 누락 목록에 등록했다. 보호 대상은 **49개 계약 ID / 95개 테스트**다. GitHub workflow의 X11·terminfo 테스트 의존성도 추가했으나 **GitHub Actions 원격 실행은 하지 않았다**.

로컬 증거는 `.cache/vt-supplements/terminal-first.log`, `terminal-second.log`, `terminal-final.log`, `stream-final.log`, `host-final/summary.json`, `host-final/resize-actions.json`, `restore-final/summary.json`에 보존한다. 실제 호스트 검증 전후 제품·번역·샘플·native 파일 99개의 SHA-256도 일치했다. cache는 배포 문서나 소스 패키지에 포함하지 않는다.

## 남는 범위

- ETX → SSH 로비 → 개발 호스트의 실제 원격 연결은 여기서 재현하지 않았다. X11 연결 원리를 재사용하고 격리 서버에서 검증한 결과와 구별한다.
- 실제 Windows clipboard 검사의 최종 결과, 물리 IME 조합·호스트별 모든 키 형식·픽셀 외관은 각각 별도 판정한다.
- host가 이미지나 표현을 지원하지 않는 경우, 모호한 legacy 키, 현재 API 밖의 새 협상·화면 기능은 보완 Python 파일만으로 자동 해결되지 않는다. 기존 [모듈 계약](VT_PROTOCOL_PLUGINS.md)의 범위를 따른다.

사용 설정과 설치 예시는 [VT_USAGE.md](VT_USAGE.md)에 정리했다.
