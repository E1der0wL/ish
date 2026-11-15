# 직접 전달 실험 검증

이 문서는 `ish.stream`의 관찰 테스트와 실제 호스트 화면 검증을 구별한다.
셸의 프롬프트 문자열과 deadline은 테스트의 관찰·실패 판정에만 사용한다.
production readiness를 화면 문자열이나 무출력 시간으로 추정하지 않는다.

앞선 검증 환경은 WSL2 Ubuntu, tcsh 6.24.13, Bash 5.3.9, zsh 5.9, Vim 9.1, Python 3.12.14,
prompt-toolkit 3.0.53이다. Windows Terminal의 설치 package 버전은
1.24.12741.0이며 실행 파일의 ProductVersion은 1.24.261001001이다.

## ish 2.0.0: ESC·설정·제품 진입점 검증

2026-11-16 검증. 제품 이름은 `ish`, 버전은 `2.0.0`이다.
`ish` console script와 독립 배포 실행기는 `ish.stream`을 실행한다.
`ish2`, `ish-stream`, `ish-legacy` 실행기나 구버전 설정 호환 계층은 제공하지 않는다.
기본 설정 경로는 기존 공통 Config의 `~/ish/.ishrc.py`를 재사용한다.

- ESC/Alt 바이트 판별 기본 대기는 0.05초다. parser가 idle flush에서 정확히
  Escape 하나를 반환한 경우에만 즉시 취소하며, PTK의 별도 1초 대기를 반복하지
  않는다. Alt 조합, 불완전 CSI, 붙여넣기와 플러그인의 다중 키 바인딩은 유지한다.
  실제 PTK Panel에서 5회 측정한 취소 시간은 **51.362–53.258ms**였다.
  물리 키보드부터 화면 갱신까지의 전체 지연 측정은 아니다.
- `.ishrc.py`의 `config`에 시작 설정과 `plugins` 목록을 저장한다. 다음 실행에도
  같은 파일을 읽고 기존 PluginManager가 선택된 플러그인을 로드한다.
  CLI는 해당 설정을 덮어쓰며 `--plugin`은 파일의 목록을 대체한다.
  `--no-rc`와 `--no-plugins`, 잘못된 설정의 시작 전 차단, 파일/CLI 상대 경로의
  기준, symlink 실행 이름 보존을 실제 프로세스와 파일 결과로 확인했다.
- help/version/diagnose는 기존에 있는 Python rc도 실행하지 않는다.
  설정·플러그인·로그 파일 생성 여부를 별도 프로세스와 설치 wheel에서 확인했다.
- 새 진입점이 쓰지 않는 `ish_forward` 및 preload helper 컴파일은 배포에서
  제거했다. README를 현재 실행·설정·플러그인·진단 사용법 중심으로 바꿨다.

보강 후 다른 담당자가 입력 처리, 설정/시작, 패키징, 호스트 검증기를 교차
검토했다. 검토에서 발견한 rc 디렉터리 실행, 상대 셸 경로 처리, 테스트의
PTY EOF 직후 조기 종료 판정 문제를 수정하고 재현 회귀를 추가했다.
마지막 제품 코드 검토에서는 추가 결함을 발견하지 못했다.

| 범위 | 최종 결과 |
| --- | --- |
| `tools/check_stream.py` 전체 회귀 | **360/360**, skip 0, 175.084초 |
| Ruff lint·format | 68개 파일 통과 |
| Bash 5.1.16 adapter·CLI | **40/40**, 25.649초 |
| Bash 5.2.37 adapter·CLI | **40/40**, 25.973초 |
| 설치 wheel | 버전·진입점·한국어 조회·rc 미실행·리소스 14개·provider/builder 3개 통과 |
| CPython 포함 독립 배포 | GCC·CC·Clang·G++ 없는 PATH에서 빌드 및 재배치 실행 통과 |
| 실제 Windows Terminal/WSL | 아래 12개 조합 모두 첫 실행 통과 |

첫 전체 회귀는 번역 테스트가 이전 `ish-stream` 이름을 기대하여 359/360이었다.
제품명에 맞게 독립적인 기대 문자열을 수정한 뒤 전체 360개를 다시 실행했다.
첫 실패 기록도 `.cache/ish2/regression-initial.log`에 보존했다.
같은 디렉터리에 최종 `regression-final.log`, `bash-compat.log`,
`package-smoke-final.log`, `standalone-build-final.log`, `esc-results.json`이 있다.
이 임시 증거 디렉터리 이름은 제품 이름이 아니다.

### 실제 Windows Terminal의 설정·입력 검증

Windows Terminal 1.24.12741.0의 기존 창에 공식 `wt.exe` CLI로 검증 탭을 열었다.
`tests/stream_matrix.py`가 실제 호스트의 응답을 받았으며, CPR을 합성하지 않았다.
각 실행의 `WT_SESSION`, 실제 argv, 설정 경로와 출력은 `host-final/`에 기록했다.

| 셸 | rc 선택 플러그인 | UI 없는 기본형 | 비동기 오류 복구 | 이전 factory 작업 실패 격리 |
| --- | --- | --- | --- | --- |
| tcsh 6.24.13 | 통과 | 통과 | 통과 | 통과 |
| Bash 5.3.9 | 통과 | 통과 | 통과 | 통과 |
| zsh 5.9 | 통과 | 통과 | 통과 | 통과 |

플러그인 3개 조합은 CLI의 `--plugin`·`--plugin-dir` 없이 실제 `.ishrc.py`만으로
선택했다. Alt+B 편집 결과 `팝업 값 one Xtwo`를 파일로 확인했고, ESC 취소 데이터가
셸에 실행되지 않으며 다음 native 명령만 파일을 만드는 것을 확인했다.
ESC 전송부터 새 primary 이벤트까지 tcsh **84.84ms**, Bash **75.08ms**,
zsh **74.54ms**였다. 이는 테스트가 PTY에 보낸 바이트 기준의 측정이다.

기본·플러그인 조합은 native continuation, 공백·Unicode·도구 이름이 들어간
heredoc, Vim 저장 파일, 실제 호스트의 CPR/DSR/scroll-margin 응답을 확인했다.
크기 변경은 창 드래그 대신 `wt.exe split-pane`으로 만들었다.
**120×30 → 82×30** 변화가 호스트와 실행 중인 foreground 프로그램 양쪽에서
확인된 뒤 외부 검증 gate를 해제했다. 제품 readiness를 timeout으로 판단하지 않는다.
검증 후 임시 분할 창과 검증 탭은 종료되어 원래 터미널 탭으로 돌아왔다.

12개 모두 종료 코드 0이며 parent termios가 복원됐다. 별도 증거 검사에서
native 결과 파일 **33개**, 팝업 반환값, 취소 입력 누출 부재를 다시 확인했다.
검증 당시 소스 206개 SHA256이 작업 폴더와 모두 일치했다. 이후에는 검증 기록과
README의 구버전 호환 오해를 없애는 표현만 정리했다. 실제 GUI 키보드 인코딩, 화면 픽셀·SIXEL 이미지 시각 확인은 하지 않았고,
원격 GitHub Actions도 실행하지 않았다.

## 팝업 생성 작업·검증 경로 보강과 교차 검토

이번 단계는 보강 → 다른 담당자의 검토 → 추가 결함 보강 → 재검토 순서로
진행하고, 검토 범위의 미해결 항목이 없어진 뒤 전체 회귀를 실행했다.
셸 연동 스크립트와 wire 계약은 유지했다. 기본형에는 UI나 hook을 추가하지 않았다.

- factory가 공개 PTK task API로 만든 작업도 생성 시점부터 해당 팝업에 묶는다.
  현재 작업의 예외 처리는 유지하며, 닫혔거나 생성에 실패한 팝업의 지연 작업과
  그 후손은 다음 팝업을 해제하지 않는다. 수정 전 두 실패를 재현했다.
- 테스트 실행 파일 선택은 기존 `shell_executable()` 한 곳을 재사용한다.
  Bash/zsh/tcsh adapter와 CLI 모두 실제 wrapper를 실행한 기록으로 검증했다.
  이전 zsh/tcsh adapter가 선택 경로를 무시하는 두 실패를 보존했다.
- 배포 소스 해시에 외부 셸 텍스트·번역·native 소스를 포함하고 캐시·컴파일
  산출물은 제외한다. 독립적인 파일 변경·추가·삭제·이름 변경으로 검증했다.
- 공통 wheel 검사는 소스 밖의 별도 설치 환경에서 진입점, 한국어 조회,
  리소스 13개와 기존 PluginManager의 provider·builder 각 3개를 확인한다.
  이 검사에서 기존 `uv.lock`의 Pillow 누락을 발견해 13.0.0만 추가했다.
  다른 의존성 버전은 변경하지 않았으며 잠금 설치도 통과했다.
- 실제 호스트 실행·재시도·집계는 유지 대상 `tests/stream_matrix.py`로 옮겼다.
  별도 controller 없이 실행하고, 기존 증거 디렉터리 재사용을 거부한다.
  검토에서 발견한 resize 경합은 사용자의 Enter 완료 확인과 최종 커널 크기
  조회로 보완했다. 신호가 아직 오지 않은 실제 PTY 크기 변경도 회귀에 포함했다.

교차 검토 요약은 `.cache/stream-review-loop/review.txt`다. 원문 대화 기록은 아니다.
`factory-before.log`, `selection-before.log`, `package-smoke-before.log`에 최초
실패를 보존했다. 마지막 수정 후 별도 검토에서 추가 결함을 발견하지 못한 범위는
위 변경 경계이며, 가능한 모든 플러그인·호스트의 무결함을 선언하지 않는다.

| 범위 | 결과 | 시간 |
| --- | --- | --- |
| PR 공통 검사기의 전체 회귀 | 341/341, skip 0 | 191.829초 |
| Bash 5.1.16 adapter + 실제 CLI | 40/40 | 51.788초 |
| Bash 5.2.37 adapter + 실제 CLI | 40/40 | 50.930초 |
| Ruff lint·format | 66개 파일 통과 | — |
| 별도 환경의 wheel 설치 검사 | 진입점·번역·리소스·provider/builder 통과 | — |

전체 검사는 Linux의 별도 snapshot과 `uv sync --locked`로 만든 새 환경에서
실행했다. 실행본 190개 파일과 작업 폴더의 byte 일치를 `source-hashes.json`에
기록했다. 결과는 같은 폴더의 `regression-final.log`, `bash-compat.log`,
`locked-environment.log`, `package-smoke.log`에 있다. 유지 대상은 stream 20개와
공통 7개이며, 독립 누락 검사도 27개 모듈을 요구한다. 원격 GitHub Actions는
실행하지 않았다.

새 async/factory 시나리오 6개는 실제 PTY와 PTK를 거치는 사전 검사도 통과했다.
이때 CPR은 pyte로 합성했으므로 **실제 호스트 터미널 검증이 아니다**.
범위를 명시한 결과는 `preflight-01.json`과 `preflight-01/`에 있다.

### 실제 Windows Terminal/WSL 최종 확인

사용자가 공통 실행기를 시작하고 창 크기 변경 후 Enter로 완료를 확인했다.
이후 12개 조합이 별도 controller나 재시도 없이 모두 통과했다.

| 셸 | 샘플 플러그인 | UI 없는 기본형 | 현재 비동기 작업 오류 복구 | 이전 factory 작업 실패 격리 |
| --- | --- | --- | --- | --- |
| tcsh 6.24.13 | 통과 | 통과 | 통과 | 통과 |
| Bash 5.3.9 | 통과 | 통과 | 통과 | 통과 |
| zsh 5.9 | 통과 | 통과 | 통과 | 통과 |

기본·플러그인 6개 실행은 continuation, 공백·Unicode·도구 이름이 들어간
heredoc, Vim의 실제 파일 결과와 호스트 CPR/DSR/scroll-margin 응답을 확인했다.
tcsh 플러그인 실행에서 **316×74 → 199×51** 크기 변경이 foreground 프로그램까지
전달됐고, Enter 이후 확정한 호스트 크기와 최종 foreground 크기가 일치했다.

비동기 오류 3개는 팝업 입력의 native 미실행, primary 복귀 후 명령 실행,
실행 중 진단 출력 부재와 종료 후 오류 진단 1회를 확인했다. factory 작업 3개는
첫 팝업의 작업이 늦게 실패한 뒤에도 둘째 팝업이 `touch factory-leak`를 자신의
값으로 받았으며, 같은 이름의 native 파일이 생성되지 않음을 확인했다.

독립 결과 대조에서 native 파일 **30개**와 팝업 값 파일 **3개**가 기대값과
일치했다. 12개 모두 종료 코드 0, 부모 termios 복원, traceback 부재를 확인했다.
실행 후에도 검증한 소스 190개와 작업 폴더의 해시가 일치하며 추가 제품 코드
수정은 필요하지 않았다.

증거는 `.cache/stream-review-loop/host-final/`의 각 실행 결과와
`host-results.json`에 있다. 이번 결과는 실제 호스트에서의 PTY·파일·프로토콜
검증이며 **스크린샷 픽셀 외관, GUI 키보드 인코딩, SIXEL 이미지의 시각적 표시는
검증하지 않았다.** 원격 GitHub Actions 실행 결과와도 구분한다.

## 출력 종료·비동기 팝업·테스트 누락 방어 (이전 단계)

현재 worktree에서 세 문제를 먼저 재현하고, 기존 구현을 재사용하여 보완했다.
셸별 스크립트·P/H/B 프로토콜·기본형의 UI 없는 동작은 변경하지 않았다.

1. 출력 보류 중 정상 종료에서 10,012바이트 중 4,095바이트만 전달되고 5,917바이트가
   PTY에 남는 기존 문제를 실제 PTY로 재현했다. Session은 자식 종료 관찰과 최종
   출력 완료를 분리하고, 미전송 입력을 즉시 폐기한다. App은 자식 종료 때 팝업을
   해제하되 호스트 출력 보류를 유지한다. 기존 최종 읽기 예산을 재개 간에 이월한다.
2. 비동기 PTK 키 binding 예외가 전역 asyncio 오류 경로로 빠지는 실패를 재현했다.
   공개 task API를 감싸 기존 오류 처리에 연결했다. 닫힌 팝업의 지연·자식 task
   실패는 새 팝업에 영향을 주지 않으며 정상 취소와 시작 전 취소를 보존한다.
3. 검사기·helper·host·startup 테스트가 빠져도 축소된 검사로 통과하는 실패를
   실제 검사기 subprocess에서 재현했다. 유지 대상 stream 19개·공통 7개 모듈을
   모두 요구하고, 독립 기대 목록으로 각 파일의 누락을 검증한다. 새 파일 자동
   수집도 유지한다.

독립 재검토에서 async binding의 취소 `finally`가 만든 후속 task가 Panel 종료
뒤에도 실행되는 문제를 추가로 재현했다. PTK의 공개 종료 메서드에서 생성 종료를
먼저 확정하고, 이후 등록된 task를 실행 전에 취소하도록 보완했다. 보완 후 같은
경계를 다시 검토했고 추가 결함은 발견하지 못했다.

개별 검증은 Session 10개, App 요청·셸 종료 연결 19개, Panel/플러그인 35개,
검사기 8개가 통과했다. 검사기 누락 검증은 26개 subTest를 포함한다. 실제
FDWriter와 4 KiB pipe로 호스트 출력 보류를 일으킨 검증에서 30,036바이트 전량이
정확히 전달됐다. Bash/zsh/tcsh 모두 열린 팝업 중 셸 종료와 비동기 UI 실패 후
native 복귀·파일 결과를 확인했다. 이 개별 검증 수치는 전체 회귀에 중복 합산하지 않는다.

재현·검토 기록은 `.cache/stream-fixes/`에 있다. `session-review.txt`와
`panel-review.txt`는 관찰 결과의 요약이며 raw transcript는 아니다.
`async-before.log`, `async-shutdown-before.log`, `check-missing-before.log`에는
수정 전 실패를, 대응하는 after 로그에는 보완 후 결과를 보존한다.

재검토를 마친 최종 소스의 전체 검증 결과는 다음과 같다.

| 범위 | 결과 | 시간 |
| --- | --- | --- |
| PR 공통 검사기의 전체 회귀 | 326/326, skip 0 | 200.443초 |
| Bash 5.1.16 adapter + 실제 CLI | 40/40 | 56.879초 |
| Bash 5.2.37 adapter + 실제 CLI | 40/40 | 55.041초 |
| Ruff lint·format | 62개 파일 통과 | — |

로그는 `.cache/stream-fixes/regression-final.log`와 `bash-compat.log`다.
실행본 187개 파일이 현재 소스와 byte 단위로 일치함을 `source-hashes.json`에
기록했다. 실제 Windows Terminal 검증은 기본형·플러그인 6개와 비동기 팝업 실패
복구 3개를 아래와 같이 완료했다. 이전 단계의 호스트 결과를 이번 변경 결과로
계산하지 않는다. 원격 GitHub Actions job은 실행하지 않았다.

### 실제 Windows Terminal/WSL 최종 결과

| 셸 | 플러그인 모드 | UI 없는 기본형 | 비동기 팝업 오류 후 복구 |
| --- | --- | --- | --- |
| tcsh 6.24.13 | 통과 | 통과 | 통과 |
| Bash 5.3.9 | 통과 | 통과 | 통과 |
| zsh 5.9 | 통과 | 통과 | 재검사 통과 |

기본·플러그인 6개 실행은 continuation·Unicode heredoc·Vim의 파일 내용,
실제 호스트 CPR/DSR/scroll-margin 응답, 정상 종료와 부모 termios 복원을
확인했다. tcsh 실행의 호스트와 foreground 크기 기록 31개가 일치하며,
사용자 조작이 **188×63 → 155×40**으로 전달됐다. 나머지 실행은 155×40이다.

비동기 오류 3개는 오류 발생 이후 primary 복귀, native 명령의 `recovered`
파일, 팝업에 입력한 명령의 미실행, 실행 중 traceback·진단 출력 부재,
종료 후 진단 1회, 종료 코드 0·부모 termios 복원을 확인했다. 전역 asyncio
오류 출력 대신 기존 UI 해제·로그 경로를 사용했다.

기록은 `.cache/stream-fixes/host-results.json`에 각 최종 실행 경로를 연결했다.
`host-final/`의 8개 성공과 `host-zsh-retry/zsh-async-error/`의 성공을 합친
**9개 조합**이며, 최초 zsh 실패를 성공으로 덮어쓰지 않았다.

두 검증기 문제도 별도로 보존한다. 처음에는 사용자 실행 전에 외부 단계
제어기의 대기시간이 만료돼 primary 단계에서 진행하지 못했다. 시작 전 대기와
검증 실행 시간을 구분하도록 제어기를 보완하고, 실행 중인 터미널을 재시작하지
않고 이어갔다. 복구 전 상태는 `host-before-resume/`에 있다. 마지막 zsh 오류
검사에서는 `P` 복귀 이벤트가 있었지만 검증기가 프롬프트 문자의 재출력을
요구하여 실패했다. zsh는 기존 프롬프트 셀을 재사용할 수 있으므로, 오류 로그
이후의 다음 `P`와 후속 명령의 실제 파일 결과를 확인하도록 검증기를 수정했다.
제품 코드는 바꾸지 않았고 이 한 조합만 별도 폴더에서 다시 검증했다.

이번 실제 호스트 검증은 파일 결과·PTY·호스트 응답 검증이다. **스크린샷 픽셀
외관·GUI 키보드 인코딩·SIXEL 이미지의 시각적 표시는 검증하지 않았다.** 출력
보류와 종료가 겹치는 유실 회귀는 앞서 명시한 실제 PTY·FDWriter·막힌 pipe
검증으로 확인했으며, 이를 호스트 화면의 픽셀 검사로 확대하지 않는다.

## 요청 계약·공통 정의·PR 검사 정리 (이전 단계)

이번 변경은 요청 identity와 상태 전이를 `handoff.Request`에 모으고,
`protocol.Event/Reply` 및 loader의 기본 경로를 재사용한다. wire bytes와 셸별
스크립트는 유지했다. 기본형은 UI 없이, 명령·continuation·프로그램 stdin은
기존 native PTY 경로로 동작한다.

구현 뒤 독립 검토와 필요한 보완을 반복했다.

1. 실제 Unix socket·PTK 하네스에서 두 번째 증가한 `B`가 첫 요청 번호를
   덮어쓰는 기존 문제를 재현했다. 첫 번호만 확정하고 정상 helper만 한 번
   포커스를 얻도록 수정했다.
2. 팝업 닫힘과 `done` 큐 등록을 분리했다. 다음 `P`에서만 완료하고, 예상 밖
   `P`·연결 종료는 취소로 남긴다. 완료 후 도착한 `drain` 실패가 완료를
   취소로 되돌리지 않는지도 실제 응답을 전송한 뒤 `drain` 완료를 지연·실패시켜
   확인했다.
3. 검사기의 실제 선택 셸 경로·버전 출력을 보완했다. 저장소 밖에서 실행할 때
   동명 상대 경로 셸을 잘못 선택하는 실패를 재현하고, 호출자 cwd에서 먼저
   경로를 확정하도록 수정했다. Windows/LF 포맷 차이도 정리했다.
4. 재검토 후 PR과 동일한 로컬 명령에서 최초 309개 전체 회귀가 skip 없이
   통과했다. 이후 실제 호스트 검증 중 테스트 하네스 자체의 resize 신호
   재진입 오류가 발견되어 성공으로 계산하지 않고 보완 대상으로 분리했다.

초기 호스트 실행에서는 연속 `SIGWINCH` 중 `HostRun.record()`의 동일 임시 파일
쓰기가 재진입하여 `FileNotFoundError`가 발생했다. foreground 크기 전달은 실제
파일에서 확인됐으나 이 실행은 실패다. `.cache/stream-contracts/host-final/`에
결과·출력·로그를 그대로 보존한다. 이는 제품의 asyncio 신호 처리 경로와 별도인
동기 검증 하네스 문제다.

동일 실패를 이전 하네스로 재현한 뒤, 신호 handler에서는 변경 여부만 표시하고
실제 크기 전달·파일 기록은 정상 실행 루프에서 수행하도록 보완했다. 연속 신호
회귀 2개와 tcsh/Bash/zsh의 실제 PTY resize 3개가 통과했다. 이 polling은 검증용
크기 기록에만 사용하며 제품의 입력 readiness 판단에는 추가하지 않았다.

보완과 재검토 후 최종 소스에서 다시 실행한 결과는 다음과 같다.

| 범위 | 결과 | 시간 |
| --- | --- | --- |
| PR 공통 검사기의 전체 회귀 | 311/311, skip 0 | 128.945초 |
| Bash 5.1.16 adapter + 실제 CLI | 40/40 | 34.655초 |
| Bash 5.2.37 adapter + 실제 CLI | 40/40 | 35.148초 |
| Ruff lint·format | 62개 파일 통과 | — |

최종 로그는 `.cache/stream-contracts/regression-final.log`와
`bash-compat.log`다. 구버전 Bash 80회는 311개와 별도 실행이다.
실행본 187개 파일이 작업 폴더와 byte 단위로 일치함을 `source-hashes.json`에
기록했다.

### 실제 Windows Terminal/WSL 재검증

수정된 하네스로 최종 소스를 실행해 tcsh 6.24.13, Bash 5.3.9, zsh 5.9의
플러그인 모드와 UI 없는 기본형 **6개 조합 모두 통과**했다. 각 실행에서
continuation·Unicode heredoc·Vim의 최종 파일 내용, 실제 호스트의 CPR/DSR/
scroll-margin 응답, 종료 코드 0과 부모 termios 복원을 확인했다.

세 플러그인 실행의 로그에 `Popup request stage=complete`가 기록되어 정상
팝업 반환과 primary 복귀가 연결됨을 확인했다. native 데이터는 진단 로그에
포함되지 않았고, 기본형 3개 실행은 지정한 settings 디렉터리도 만들지 않았다.

사용자의 실제 창 크기 변경은 tcsh 플러그인 실행에서 **202×62 → 188×63**으로
전달됐다. 호스트와 foreground 프로그램의 기록 17개가 일치했다. 크기 조작
종료를 확인한 뒤 나머지 5개를 188×63으로 실행했다. Bash/zsh의 크기 변경
동작 자체는 위의 실제 PTY 회귀에서 별도로 확인했다.

기록은 `.cache/stream-contracts/host-repeat/`의 `summary.json`, 각 `result.json`,
`output.bin`, `session.log`에 있다. 앞선 하네스 실패 기록도 삭제하지 않았다.
테스트 입력은 실제 outer PTY를 통해 전달했다. **스크린샷 픽셀 외관·GUI 키보드
인코딩·SIXEL 이미지의 시각적 표시는 검증하지 않았다.** native 이미지 프로그램의
호스트 응답과 파일 검증을 이미지 표시 성공으로 확대하지 않는다. 이전 단계의
호스트 결과는 이번 변경 결과에 합산하지 않는다.

### PR 자동 검사

PR workflow는 `.github/workflows/stream.yml`, 공통 실행기는
`tools/check_stream.py`다. 환경·필수 테스트 누락, 빈 모듈, import 오류와 skip을
실패로 처리한다. actionlint 1.7.12 검사를 통과했으며, Ubuntu의 tcsh 6.21.00과
6.24.10 바이너리를 격리해 각각 기존 adapter·native completion 41개를 확인했다.
시스템 셸을 설치·교체하지 않았으며 기록은 `.cache/pr-check/`에 있다.
원격 GitHub Actions job은 아직 실행하지 않았다. workflow 추가와 로컬 동일 명령
검증을 원격 CI 통과로 보고하지 않는다.

## 시작 옵션·번역·로그 연결 후 검증 (이전 단계)

기존 Config/I18N/logger와 환경 진단을 재사용한 코드에서 아래 검증을 수행했다.
이 절 아래의 이전 단계 수치는 중복 합산하지 않는다. 제품 `--demo`는 제거했고,
테스트는 예제 UI와 셸 provider를 명시적인 `--plugin` 목록으로 선택한다.

| 범위 | 결과 | 시간 |
| --- | --- | --- |
| 전체 `test_stream_*.py` | 211/211 | 131.566초 |
| 공통 번역·로그·진단 | 35/35 | 아래 추가 묶음에 포함 |
| PluginManager·Session·tcsh 자동완성 관찰 | 46/46 | 추가 81개 합계 10.400초 |
| Bash 5.1.16 adapter + 실제 CLI | 40/40 | 41.111초 |
| Bash 5.2.37 adapter + 실제 CLI | 40/40 | 40.138초 |

기본 회귀는 **292개**, 구버전 Bash는 별도로 **80회**다. 코드·예제·테스트 45개
파일의 Ruff 검사와 포맷 검사, `git diff --check`도 통과했다. 실행본 169개 파일이
작업 폴더와 byte 단위로 일치함을 `.cache/stream-startup/source-hashes.json`에
기록했다. 회귀·린트 로그는 같은 디렉터리에, 구버전 Bash 기록은
`.cache/startup/bash-compat.log`에 있다.

새 공개 CLI 검증은 다음 관찰 가능한 결과를 확인한다.

- `--help/--version/--diagnose`: TTY 없이 실행하고, 선택된 셸·플러그인을 실행하지
  않으며 설정·로그·플러그인 library 파일을 만들지 않는다.
- 한국어 리소스·사용자 JSON 우선순위, 언어 전환, 잘못된 언어 경로와 포맷,
  번역 파일 오류에 대한 로그 수준과 1회 출력, 기존 `ish` 정보 조회 보존.
- 실제 PTY에서 tcsh/Bash/zsh 로그인 파일 실행·native HOME·종료 코드,
  사용자 지정 인수와 UI 미지원 조합의 native fallback, 상대 셸 경로 + 별도 cwd.
  심볼릭 링크의 선택된 이름을 보존한다.
- DEBUG lifecycle/event/focus의 파일 즉시 기록, 실행 중 콘솔 비침범,
  종료 뒤 콘솔 출력, WARNING 필터, UTF-8 append, UI 예외 뒤 native 파일 결과.
- 일반 파일을 가리키는 symlink는 허용하고 터미널·장치·FIFO 로그 대상은
  셸 시작 전에 거부한다. 파일 쓰기 실패도 live stderr traceback을 만들지 않는다.
- 기본형은 추가 UI와 설정 파일을 만들지 않는다. `--home`는 셸의 HOME을 바꾸지
  않는다. 명령·continuation·heredoc·프로그램 stdin은 기존 native 경로다.

로컬 wheel을 빌드하여 한국어 JSON, 셸 provider 3개, 셸 연동 텍스트 9개의 포함을
확인했다. 예제 UI와 제거한 shim은 제품 wheel에 없으며, wheel을 푼 설치 경로에서
한국어 도움말이 동작하고 settings 파일을 만들지 않았다.
`.cache/stream-startup/package-check.json`에 결과를 보존했다. 이는 리소스 패키징
검증이며 새로운 standalone 배포 바이너리를 발행한 것은 아니다.

### 실제 Windows Terminal/WSL

최종 소스로 셸 3종의 플러그인 모드와 기본형 **6개 조합 모두 통과**했다.
한국어 옵션과 별도 `--home`를 적용했고 플러그인 모드는 DEBUG 파일 로그도 켰다.

| 셸 | 플러그인 모드 | UI 없는 기본형 | 종료 0·부모 TTY 복원 |
| --- | --- | --- | --- |
| tcsh 6.24.13 | 통과 | 통과 | 모두 통과 |
| Bash 5.3.9 | 통과 | 통과 | 모두 통과 |
| zsh 5.9 | 통과 | 통과 | 모두 통과 |

검증 단계는 primary, 팝업(플러그인 모드), native continuation, Unicode heredoc,
Vim 저장 파일, native 이미지 프로그램의 실제 host CPR/DSR/scroll-margin 응답,
foreground 크기, 마지막 명령 파일이다. 로그에서 lifecycle·popup focus를 확인했고
native 파일 본문은 로그에 없었다. 기본형은 지정한 settings 경로도 만들지 않았다.
최종 실행은 **160×52**로 고정했다. 결과는 `.cache/stream-startup/host-final/`의
`summary.json`, 각 `result.json`, `output.bin`, `session.log`에 보존했다.

입력은 테스트 하네스가 실제 outer PTY로 전달했다. **스크린샷 픽셀 외관·GUI 키보드
인코딩·SIXEL 이미지의 시각적 표시 여부는 이번에 검증하지 않았다.** 호스트 응답과
파일 결과를 해당 시각적 검증으로 확대하지 않는다. Bash/zsh resize는 실제 PTY
회귀에서, 사용자 조작에 의한 호스트 resize는 아래 tcsh 실행에서 확인했다.

#### 초기 관찰과 보존한 실패

Windows Terminal/WSL에서 tcsh 플러그인 경로와 기본형은 native 파일 결과·Vim·
host CPR/DSR/scroll-margin 응답·정상 종료·termios 복원을 통과했다.
tcsh 플러그인 경로에서 실제 foreground 크기 **152×45 → 156×47** 전달도 확인했다.
이어진 Bash 팝업 단계에 사용자의 창 크기 변경이 계속 도착하여 장식·팝업 요청이
취소됐고, 팝업 표시만 기다리던 하네스가 실패했다. `B → resize → P → resize`
순서를 파일 로그에서 확인했다. 이 실행은 성공으로 계산하지 않으며
`.cache/stream-startup/host-matrix/`에 원본 결과와 로그를 보존했다.

언어 설정은 셸·외부 프로그램 출력이나 모든 기술 로그를 번역하지 않는다.
로그인·사용자 지정 인수의 UI 지원은 확장하지 않았고, 지원하지 않는 조합은
UI 없이 실행한다. 임의 플러그인의 직접 출력이나 별도 logger는 공통 로그 정책의
보호 범위가 아니며, 로그 rotation은 구현하지 않았다.

## 외부 Python 플러그인 분리 후 검증 (이전 단계)

기존 `PluginManager`를 재사용하고 UI와 셸 builder를 외부화한 최종 코드에서
다음 회귀가 통과했다. 아래 이후 절의 과거 테스트 수와 중복하여 더하지 않는다.

| 범위 | 결과 | 시간 |
| --- | --- | --- |
| 전체 `test_stream_*.py` | 185/185 | 121.810초 |
| PluginManager 로딩·의존성 설치 | 24/24 | 아래 추가 묶음에 포함 |
| terminal Session | 4/4 | 아래 추가 묶음에 포함 |
| tcsh native 자동완성 관찰 | 18/18 | 추가 46개 합계 10.530초 |
| Bash 5.1.16 adapter + 실제 CLI | 40/40 | 25.161초 |
| Bash 5.2.37 adapter + 실제 CLI | 40/40 | 27.378초 |

기본 회귀는 총 231개이며 구버전 Bash는 별도로 80회 실행했다. 구버전 바이너리는
기존 캐시에서 재사용했고 배포용 셸을 설치하거나 수정하지 않았다. 현재 기본
바이너리 버전은 Bash 5.3.9, zsh 5.9, tcsh 6.24.13, Vim 9.1로 다시 확인했다.

새 검증은 외부 Python 파일 변경의 다음 세션 적용, 선택하지 않은 모듈 미실행,
하단 바만 사용 시 사용자 F2 보존, 셸 provider만 선택 시 hook 미설치, 실패한
builder의 argv/env 변경 격리, 실패 전 등록 자원 정리, 역순 cleanup, 팝업 오류 시
입력 폐기·셸 복귀, UI 오류 시 미전달 native Escape 보존을 포함한다. 실제 PTK
패널에서는 새 view, 오래된 accept 거부, 임의 완료 키 뒤 원본 입력 보존 및 사용자
render/key 예외도 확인했다.

원본 PluginManager를 별도 복사한 재현 테스트 4개에서 6개 실패를 확인한 뒤,
원본 RECORD 파일 검증과 활성 전이 의존성 검사로 보강했다. 네트워크 없는 fixture는
pip의 stage 생성만 대체하며 실제 교체·복구·rollback과 플러그인이 읽는 파일 결과를
검증한다. 검사 범위의 한계는 [플러그인 문서](STREAM_PLUGINS.md)에 명시했다.

실제 Windows Terminal/WSL에서도 외부 복사한 플러그인과 기본형을 각각 실행했다.

| 셸 | 외부 플러그인 | 기본형 | 정상 종료·부모 TTY 복원 |
| --- | --- | --- | --- |
| tcsh 6.24.13 | 통과 | 통과 | 모두 통과 |
| Bash 5.3.9 | 통과 | 통과 | 모두 통과 |
| zsh 5.9 | 통과 | 통과 | 모두 통과 |

공통 단계는 primary, 팝업(플러그인 경로), native continuation, heredoc,
Vim의 실제 저장 파일, native 이미지 프로그램과 호스트 CPR/DSR/scroll-margin
응답, foreground 크기 조회, 최종 명령 파일이다. 사용자가 검증 프로그램을
Windows Terminal에서 시작하고 창 크기를 변경했다. tcsh 플러그인 경로에서는
실제 foreground 크기가 120×30 → 121×30 → 121×31로 바뀌었고 이후 기본형에서도
크기 변경이 관찰됐다. Bash/zsh의 resize 동작은 별도 실제 PTY 회귀에서 검증했다.

computer-use 도구가 터미널 실행을 차단했고 사용자가 연 창도 접근 목록에 나타나지
않아 이번에는 스크린샷/픽셀과 GUI 키 인코딩을 자동 검증하지 못했다. 입력은 실제
호스트에 연결된 outer PTY로 보냈고 호스트 응답을 그대로 반환했다. 따라서 이 결과는
실제 호스트 프로토콜·셸 파일 결과 검증이며 화면 이미지를 직접 확인한 결과와 구분한다.

증거는 `.cache/plugin-split/`의 `regression-final.log`, `additional.log`,
`bash-compat.log`, `loader-baseline.log`, `loader-fixed.log`와
`host-matrix/*/{result,environment}.json`, `output.bin`, `home/`에 보관했다.
첫 통합 실행의 세 실패는 테스트 플러그인이 parent 작업 디렉터리를 child의
`--cwd`로 잘못 가정한 fixture 문제였으며 명시적인 경로로 수정했다. 전체 회귀의
첫 실행에서는 미지원 셸 안내 문구 한 건이 달라 기존 문구를 복원하고 전체를 재실행했다.
호스트 6개 검증 종료 시 보조 controller가 summary 파일의 쓰기 중간을 읽은 오류가
있었으나, 최종 6개 개별 결과·summary·종료 코드·TTY 복원은 모두 통과였다.

## 실제 PTY 경로

`tests/test_stream_cli.py`는 public CLI를 controlling PTY에 실행한다. 임시
HOME의 tcsh 설정을 사용하며, 외부 입력은 PTY master를 통해 전달한다.
`pyte`는 테스트 화면 관찰과 테스트 호스트의 CPR 응답에만 사용한다.
이 결과를 Windows Terminal의 실제 렌더링 검증으로 보고하지 않는다.

| 검증 | 독립적인 기대값 |
|---|---|
| 기본형 | 플러그인 하단 바와 alternate screen 진입이 없다. |
| 원문 출력 | truecolor, hyperlink, SIXEL, OSC52와 128 KiB unknown OSC가 byte-for-byte 전달된다. |
| foreach·heredoc·빠른 multiline 입력 | loop 파일은 `one\ntwo\n`, heredoc 파일은 선행 공백·Unicode·`ish_exit`·`python`을 그대로 포함한다. |
| 실행 중 입력 | 외부 프로그램이 F2·Ctrl+C·Ctrl+D를 포함한 실제 바이트를 파일에 기록한다. |
| Enter 직후 입력 | 제출과 같은 input batch에 들어간 F2가 native 프로그램의 stdin에 도착한다. |
| 큰 입력과 역압 | native reader가 명시 file gate를 기다리는 동안 6 MiB를 제공하고, gate 이후 실제 파일 크기와 SHA-256이 모두 일치한다. |
| `$<`·Ctrl+C | tcsh의 입력 소비 결과와 취소 후 새 명령의 실제 파일 결과가 일치한다. |
| Ctrl+D | 비어 있는 native primary에서 셸이 정상 종료한다. |
| 팝업 포커스 | 팝업에 입력한 `touch popup-leak`는 파일을 만들지 않고, 닫은 뒤 셸 명령은 정상 실행된다. |
| 팝업 burst | F2·팝업 텍스트·Enter를 한 번에 전달해도 팝업 텍스트가 셸 명령으로 실행되지 않는다. |
| 팝업 종료 뒤 선입력 | 같은 batch에서 Enter 뒤에 들어온 셸 명령은 native 복귀 후 정확히 한 번 실행된다. |
| 사용자 editor 명령 | `bindkey -c`로 실행한 프로그램도 F2와 뒤따르는 stdin을 그대로 받는다. |
| resize | foreground 프로그램이 outer PTY의 전체 크기를 실제 ioctl 값으로 확인한다. |
| 반복 Enter | 빈 제출은 프롬프트 한 줄만 진행하며 이전 프롬프트를 불필요하게 밀지 않는다. |
| 짧은 출력·팝업 복귀 | 명령 결과와 이미 표시된 이력이 추가 5행 스크롤로 사라지지 않는다. |

예시 실행:

```sh
PYTHONPATH=src:tests python -m unittest -v test_stream_cli
```

`tests/stream_fixture.py`의 프로그램들은 ish 구현을 import하지 않는다. 파일 결과와
고정 출력 bytes가 기대값이며, ish 내부 검증 함수를 기대값 생성에 사용하지 않는다.

CLI 회귀 항목 15개를 확인했다. 반복 Enter, 짧은 출력 보존, 팝업 복귀의 화면
회귀 3개와 기존 입력·파일 결과 검증을 모두 통과했다. 이 suite의 `pyte`는
테스트 전용 의존성이며 기본
stream 실행에 필요한 의존성과 구별한다.

큰 입력 검증은 별도 producer thread가 부분 write를 보존하며 입력을 제공한다.
2 MiB 이상 보낸 뒤 실제 `EAGAIN`을 관찰한 시점에만 테스트가 `read-gate` 파일을
만들어 native reader를 재개한다. 무출력 시간이나 sleep 종료를 readiness로
사용하지 않는다. 총 6,291,456 bytes의 크기와 양쪽 SHA-256을 비교했으며,
모든 입력 크기·프로그램의 응답 시간을 보장하는 결과로 확대하지 않는다.

## 실제 Windows Terminal/WSL 경로

`tests/stream_host.py`를 Windows Terminal 안의 WSL에서 실행한다. 이 harness는
셸 출력을 실제 호스트에 전달하고 호스트가 생성한 응답을 ish의 outer PTY로
돌려보낸다. test input은 outer PTY에 scripted bytes로 주입한다. 따라서 이 경로는
실제 호스트까지의 출력·응답 경로와 native 파일 결과를 확인한다. 픽셀 외관과
Windows GUI 키보드 이벤트 인코딩은 별도 관찰 없이는 검증한 것으로 보고하지 않는다.

```sh
PYTHONPATH=src:tests python tests/stream_host.py .cache/stream-test/wt-demo --demo
```

기본형은 `--demo`를 생략한다. 테스트가 사용하는 HOME과 결과 파일은 evidence
directory 아래에 격리되며 사용자의 셸 설정이나 Windows Terminal 설정을 변경하지
않는다. 실제 호스트에 표시하는 이미지 fixture에는 clipboard 변경 명령이 없다.

`stage.json`이 현재 관찰할 화면을 알린다. `continue-N` 파일을 만들면 다음 단계로
진행한다. 스크린샷을 볼 수 없는 환경에서 gate를 해제했다면 외관 검증은 미완료로
기록해야 한다. 단계는 다음과 같다.

1. native primary의 하단 바 또는 기본형의 UI 없는 화면
2. PTK popup (`--demo`에서만)
3. native continuation과 UI 숨김
4. Vim 화면과 UI 숨김, 종료 후 실제 파일 내용
5. native SIXEL 프로그램의 red/green/blue frame과 UI 숨김
6. native 크기 관찰 프로그램에서 실제 호스트 resize를 확인하고 primary 복귀

`--demo`가 없으면 popup 단계가 빠지므로 번호는 하나씩 줄어든다. resize 단계에서는
`--require-resize`를 주면 실제 호스트 크기를 바꾼 뒤 gate를 진행해야 한다.
`host-size.json`에 실제 SIGWINCH 경로로
관찰한 크기가 기록된다. 화면 스크린샷의 성공 여부는 별도로 기록해야 한다.

결과 파일:

- `environment.json`: WT_SESSION, WSL 배포판, Python, 입력 방법
- `stage.json`: 현재 화면 관찰 단계
- `host-size.json`: 실제 outer terminal 크기 이력
- `output.bin`: ish가 호스트로 전달한 바이트 기록
- `result.json`: 실제 native 파일 결과, 정상 종료 상태, resize 관찰 여부
- `home/host-replies.json`: 실제 native 프로그램에 돌아온 CPR·DSR·DECRQSS 응답

`parent_termios_restored`는 harness 자체가 호스트 termios를 복원했다는 관찰이다.
이를 ish가 변경 가능한 모든 터미널 모드를 검증한 결과로 확대하지 않는다.

## 초기 tcsh Windows Terminal 관찰 결과

`.cache/host-plain`의 기본형과 `.cache/host-demo-2`의 PTK UI 활성 실행 모두 실제
Windows Terminal/WSL에서 foreach, heredoc, Vim의 파일 결과와 정상 종료 상태 0을
확인했다. 데모는 native popup 요청으로 PTK를 열고 Unicode 텍스트를 입력한 뒤
native primary로 복귀했다. 캡처된 출력에서 하단 바·팝업·Unicode 필드가 호스트로
전달된 사실과 private 제어 마커가 노출되지 않은 사실을 확인했다.

두 실행에서 native 프로그램의 CPR은 `[15, 1]`, DSR은 정상 응답, DECRQSS의
스크롤 영역은 `[1, 30]`이었다. 프로그램에 전달된 PTY 크기는 호스트와 같은
`120 × 30`이었다. 이는 UI를 숨긴 실행 경로에서 전체 스크롤 영역과 크기가
복구된 사실을 뒷받침하며, 이미지의 픽셀 외관까지 증명하지 않는다.

제공된 화면 도구가 이 Windows Terminal 창을 노출하지 않아 다음 항목은
**미검증**이다. 프로세스 실행 성공이나 VT 응답으로 대체 판정하지 않는다.

- 하단 바·팝업·Vim·SIXEL의 실제 픽셀 외관
- 실제 GUI 키 입력 인코딩
- 실제 Windows Terminal 창을 드래그해 크기를 바꾸는 경로

두 호스트 실행 결과의 `resized`는 `false`다. 별도의 실제 PTY resize 회귀 테스트 통과와
호스트 GUI resize의 미검증 상태를 구별한다.

첫 데모 실행 `.cache/host-demo`는 harness가 popup Escape 종료와 다음 명령을
동기화하지 않아 continuation 대기에서 실패했다. native prompt 복귀를 확인하도록
harness를 고친 뒤 `.cache/host-demo-2`에서 위 검증을 완료했다. 최초 실패 기록은
덮어쓰지 않았다.

## Enter 이후 과도한 화면 이동 수정

매 primary마다 5행을 강제로 스크롤하던 예약 방식을 변경했다. 이전 패널을
비운 뒤 되감기 없는 출력이 이어졌을 때는 호스트의 IND/CUU로 부족한 행만
확보한다. 첫 표시나 배치를 알 수 없는 출력 뒤에는 기존의 안전한 전체 확보를
사용한다. 매 표시/해제의 PTY 크기 변경도 제거했다. 빈 Enter에서 발생하던
tcsh의 `Unmatched '"'.`·`Faulty alias 'precmd' removed.`는 popup 판정 변수의
비교 인용을 고쳐 해결했다. history 캡처와 실제 popup handoff는 유지했다.

회귀 테스트는 CLI 15개, 셸 22개, 프로토콜 17개, panel 12개, request 10개,
Session 4개, 스크롤 4개를 통과했다. 스크롤 테스트는 24행 화면의 시작 위치
1·8·19·20·22·24행, 세 열 위치에서 각각 0·0·0·1·3·5행만 스크롤하는지,
기존 내용과 scrollback 및 열 위치가 보존되는지 확인한다. 위쪽으로 이동한
출력과 reverse-wrap Backspace는 재사용을 취소한다. 실제 libghostty 호스트의
`?45h`·`?1045h`에서도 하단 문자 160개가 패널 밖으로 이동해 보존됨을 확인했다.
데모 자체에는 CPR 질의나 입력 응답 필터를 추가하지 않았다.

`.cache/host-scroll-fixed`는 실제 Windows Terminal/WSL에서 빈 Enter 32회를
실행한 결과다. 120×30 화면에서 커서는 1행부터 25행까지 한 행씩 진행하고,
이후에는 native 영역의 마지막 행인 25행을 유지했다. 팝업 전후 위치는 모두
`[25, 17]`이며, native 파일 결과와 종료 상태 0을 확인했다. 위치 질의는
독립적인 테스트 harness가 수행했다.

`.cache/host-scroll-suite`에서는 같은 수정으로 foreach·heredoc·팝업·Vim의
파일 결과 및 호스트 CPR/DSR/스크롤 영역 응답을 다시 확인했다. 실행 중에는
전체 120×30 PTY와 스크롤 영역 `[1, 30]`이 유지됐다. 이 검증도 픽셀 외관이나
GUI 키 입력·창 드래그 resize까지 확인한 것으로 확대하지 않는다.

첫 호스트 실행 `.cache/host-scroll-fix`는 WSL의 `/mnt/c` 파일 읽기가 PTK
import 중 지연되어 시작 제한 시간을 넘겼다. 이를 production timeout 변경으로
우회하지 않고 동일 소스와 의존성을 Linux 임시 디렉터리에 복사해 검증했다.
원본과 테스트 복사본의 App SHA-256 일치는 `.cache/stream-verify/manifest.json`에
기록했다. 성공 결과와 최초 실패 기록을 구별해 보존했다.

## Bash·zsh 추가와 최종 회귀

최종 source에서 총 143개 테스트를 통과했다. 새 테스트는 `.gitignore` 예외로
추적 가능하게 추가했다. Ruff check와 format 검사도 통과했다.

| 경계 | 테스트 수 | 관찰 결과 |
|---|---:|---|
| 기존 tcsh CLI·adapter, panel, request, protocol, scroll, Session | 88 | 기존 입력 소비자와 화면 재사용 보존 |
| Bash adapter 실제 PTY | 16 | 사용자 hook/종료 상태/PS0, native 버퍼, emacs/vi, bind-x stdin, 고급 Readline |
| zsh adapter 실제 PTY | 11 | 사용자 hook/종료 상태, ZDOTDIR, ZLE context·중첩/동적 widget, ERR_RETURN |
| Bash·zsh public CLI + 실제 PTK | 28 | 각 14개: 기본형, 팝업, 빠른 선입력, continuation/heredoc, paste, builtin/외부 stdin, 취소, Vim 파일, PTY resize |

명령 예:

```sh
PYTHONPATH=src:tests python -m unittest -v test_stream_bash test_stream_zsh test_stream_posix
```

`.cache/stream-matrix/result.json`과 `matrix.log`는 최종 28개 결과 및 adapter·App·
protocol 해시를 기록한다. `.cache/stream-shells/core-final.log`에 기존 경계의
88개 결과, `bash-result.json`과 `zsh-result.json`에 adapter 검증 결과를 기록했다.
후자의 결과 JSON은 관찰한 실행 결과 기록이며 별도 stdout 전체 로그는 아니다.

Bash 일반 accept-line을 bind-x hide macro로 감쌌던 중간 구현에서는 줄바꿈 없는
출력 손실과 빠른 builtin read 입력 회귀가 발생했다. 최종 구현은 native Enter를
유지하고 PROMPT_COMMAND의 선행 H hook으로 빈 제출 경계를 처리한다. zsh에서는
정상 ZLE의 ED0/입력 모드 변경마다 예약 공간이 무효화되어 5행 스크롤이 반복됐다.
명시적 ED0/ED2 이후 빈 영역 재사용과 화면을 바꾸지 않는 입력 모드 통과로 고쳤다.
화면 지우기 관찰은 UI eligibility나 입력 권한을 생성하지 않는다.

## 최종 Windows Terminal/WSL 결과

최종 소스를 Linux 임시 폴더에 복사하여 실제 Windows Terminal에서 다음을
실행했다. 원본과 실행 소스의 SHA-256 일치는 `.cache/stream-shells/source-hashes.json`
및 matrix 결과와 대조했다. 모든 아래 경로는 추가 관찰 도구가 그린 화면이 아니라
Windows Terminal의 실제 출력·응답 경로다. 입력은 outer PTY에 scripted bytes로 넣었다.

| 실행 기록 (`.cache/stream-shells/` 아래) | 결과 | 실제 호스트 resize |
|---|---|---|
| `host-tcsh-final` | demo 팝업·continuation·heredoc·Vim 파일·native 복귀·종료 0 | 120×30 → 120×22 |
| `host-bash-v3` | 동일 시나리오 및 종료 0 | 120×30 → 120×22 |
| `host-zsh-final` | 동일 시나리오 및 종료 0 | 120×30 → 120×22 |
| `host-bash-plain` | UI 없는 기본형의 continuation·heredoc·Vim·native 파일·종료 0 | 이 실행에서는 변경하지 않음 |
| `host-zsh-plain` | UI 없는 기본형의 동일 시나리오 및 종료 0 | 이 실행에서는 변경하지 않음 |

resize는 검증 전용 Windows Terminal 창에 `wt -w <name> split-pane`으로 임시
pane을 만든 실제 크기 변경이다. native foreground 프로그램이 ioctl로 변경된
크기를 기록했고, 같은 크기가 host SIGWINCH 기록에 도착했다. 창을 마우스로
드래그하는 GUI 입력 경로를 검증한 것으로 확대하지 않는다. 별도의 사용자 창이나
터미널 설정을 변경하지 않았다.

각 실행의 native 프로그램은 CPR `[15, 1]`, DSR 정상, 스크롤 영역 `[1, 30]`,
전체 PTY `120×30`을 확인했다. 데모는 이후 resize와 primary 복귀까지 확인했다.
화면 도구에서 Windows Terminal 창을 볼 수 없어 **픽셀 외관, GUI 키보드 입력,
SIXEL 이미지 외관은 미검증**이다. host 결과의 해당 필드도 `not performed`다.

호스트 harness의 초기 실패도 보존했다. `tcsh-demo`는 Vim의 질의 응답이 도착하기
전에 너무 빨리 편집·종료하여 늦은 응답이 다음 셸 줄로 들어간 실행이다. 테스트가
Vim의 실제 `TermResponseAll cursorblink` 수신 파일을 기다리도록 고쳐 native
질의 응답 완료 후 진행한다. production에 응답 필터나 timeout 판정을 추가하지 않았다.
`host-bash-final`은 popup 필드 갱신의 BAR를 종료 확인으로 오인하여 Escape의 키
구분이 끝나기 전에 다음 명령을 입력했다. 호스트 harness는 Enter로 명확히 닫고,
Escape 취소는 실제 PTY/PTK matrix에서 별도 검증한다. `host-bash-v2`는 native
PS1 재표시를 명령 완료로 오인했다. 이후 테스트 명령의 실제 완료 파일을 확인하여
`host-bash-v3`에서 통과했다. 이 실패를 셸 readiness 추정으로 우회하지 않았다.

## 외부 스크립트와 Bash 5.1·5.2 호환

셸 연동을 `src/ish/stream/integrations/`의 UTF-8 텍스트로 분리했다.
`--integration-dir`로 복사한 디렉터리를 선택할 수 있고 변경은 다음 세션에
적용된다. Bash, zsh, tcsh의 시작 경로와 값 인용은 Python adapter가 준비한다.
기본형은 연동 리소스를 읽지 않는다. 데모 준비 중 파일·계약 오류가 발견되면
사용자 셸을 시작하기 전에 UI 없는 경로를 선택한다. 실행 중 스크립트 오류를
자동 rollback하는 기능은 아니다.

공통 P/H/B 및 nonce/serial, native callback과 socket helper의 일치 확인은
유지했다. App에 추가한 것은 선택적 리소스 준비와 실행 전 fallback이다.
App에 셸 이름별 입력 분기를 추가하거나 Session·InputRouter의 입력 계약을
바꾸지 않았다. 기본형의 단축키와 tcsh/zsh의 기존 F2 정책도 유지했다.

Bash의 F2는 데모가 설치된 지원 Readline 키맵에서 예약한다. primary만 팝업을
열고 continuation·heredoc·`read -e`에서는 소비한다. 외부 프로그램은 F2를
그대로 받는다. 이 정책으로 5.3의 부모 문맥 command substitution 없이
PS1/PS2/PS0 산술 확장과 PROMPT_COMMAND를 이용할 수 있다. 일반 Enter는
native 동작을 유지한다. 사용자 hook의 순서·종료 상태·바인딩 본문은 실제
셸 테스트로 검증한다.

### 구버전 실행 환경

GNU 배포본 Bash 5.1.16과 5.2.37을 WSL 임시 경로에 별도로 빌드했다.
시스템 셸을 교체하거나 소스를 패치하지 않았다. GCC 15의 기본 C23 모드와
기존 C 선언의 충돌을 피하기 위해 GNU17 모드 및
`-Wno-error=implicit-function-declaration`, `--without-bash-malloc`을 사용했다.
원본 archive·실행 파일·configure/build 로그는
`.cache/bash-compat/bash-runtimes.tar`에 보존했다.

| GNU 원본 | SHA-256 |
|---|---|
| `bash-5.1.16.tar.gz` | `5bac17218d3911834520dad13cd1f85ab944e1c09ae1aba55906be1f8192f558` |
| `bash-5.2.37.tar.gz` | `9599b22ecd1d5787ad7d3b7bf0c59f312b3396d1e281175dd1f8a4014da621ff` |

### 자동 회귀 결과

공개 CLI와 실제 controlling PTY·PTK 팝업 경로에서 Bash 5.1.16, 5.2.37,
5.3.9는 각각 18개, zsh 5.9는 14개, 총 **68개**를 통과했다.
`.cache/compat-matrix/result.json`, `*-final.log`, `final-source.sha256`에
실행 버전과 소스 해시를 기록했다. tcsh 6.24.13과 zsh 5.9의 외부 스크립트
adapter 검증은 각각 23개와 12개를 통과했다. 외부 파일을 수정한 뒤 동일한
Python 프로세스의 다음 세션에서 실제 파일 결과가 바뀌는지도 확인했다.

Bash adapter 실제 PTY 테스트는 최종 동일 소스·테스트로 각 버전 22개씩,
총 66개를 통과했다. 로그는 `.cache/bash-compat/evidence/`의
`bash51-adapter-final-results.log`, `bash52-adapter-final-results.log`,
`bash53-adapter-final-results.log`다. 사용자 hook/종료 상태, emacs/vi,
prompt 확장·completion·사용자 widget의 stdin, 중첩 `read -e`, 예약 F2,
문자 인용·Unicode·실제 NUL 키 및 다음 세션의 외부 파일 변경을 확인했다.

공통 CLI·tcsh/zsh adapter·panel·request·protocol·scroll·Session과 당시 loader
테스트를 합친 106개가 통과했다. 이후 loader 경계 2개를 추가한 최종
`test_stream_scripts` 7개도 통과했다. 이 수치는 중복 실행을 합친 총수가 아니다.
기본형은 연동 디렉터리가 실제로 빠진 설치 복사본에서도 동작했다.
누락·잘못된 헤더·알 수 없는 치환 항목·adapter 없는 셸 이름에서 startup이
한 번만 실행되고 native 프로그램의 입력 bytes와 출력이 보존됨을 확인했다.
삽입 경로의 `@TOKEN@` 재치환도 차단했다. 로그는
`.cache/stream-compat/core-final.log`, `scripts-final.log`에 있다.

wheel을 실제 빌드한 뒤 9개 셸 리소스와 README가 원문 bytes 그대로 들어가고
컴파일된 `.pyc`가 없는지 확인했다. 결과는
`.cache/stream-compat/wheel-check.json`에 있다. 변경한 Python 경계의
Ruff check·format 검사와 `git diff --check`도 통과했다.

중간 실패를 통해 Bash 5.1/5.2의 `bind -X` 구분자와 바인딩 본문 직렬화가
5.3과 다르다는 것을 확인했다. Readline 직렬화만 해석하고 사용자 셸 구문은
파싱하지 않도록 수정했다. Bash 5.2가 prefix 키의 shadow entry를 끝의
`\000`로 출력하는 차이도 처리했다. 실제 NUL 키의 `\C-@` 및 literal
backslash 키는 구별하여 보존한다. stdin을 읽는 사용자 바인딩 실행 전에 H가
나오고 F2 뒤 입력이 최종 native 파일에 도착하는지 확인했다.

빠른 builtin read 테스트의 CR 기대값은 native tty 상태에 의존했다.
기본형과 데모 모두 같은 현상을 보였으므로 제품의 입력 변환을 추가하지 않았다.
일반 Enter는 실제 실행 알림 뒤 확인하고, CR 보존은 명시적인 `stty -icrnl`
상태에서 파일 bytes를 확인한다. 관찰 기록과 중간 실패 로그를
`.cache/compat-matrix/native-cr-observation.json` 및 같은 폴더에 보존했다.

adapter 테스트에서도 cached PS1의 오래된 P를 새 primary로 인정하는 관찰
결함과 파일 open/truncate 직후 write 전에 내용을 읽는 경합을 수정했다.
제품의 readiness나 시간 제한을 변경하지 않았다. 최초 병렬 실행에서 나온
history-search의 F2/Ctrl+C 후 8초 정지·PTY EIO는 최초 로그에 child 종료
상태가 없어 **원인을 확정하지 못했다**. 최종 전체 테스트와 native/adapter
각각의 12회 대조에서는 재현되지 않았다. 재현 시 종료 signal/status와 tty
상태를 남기는 추가 probe도 보존했다. 이 초기 실패를 완전히 해결했다고
선언하지 않으며, 최종 성공 기록과 구분한다. 이전 관찰 predicate로도 별도
native/adapter 각 6회가 통과했으므로 predicate 수정만으로 최초 실패가
해결됐다고 단정할 수도 없다. 추가 분석은
`.cache/compat-matrix/initial-failure-review.json`에 기록했다.
사용자 `bind -x` 내부의 중첩 `read -e`가 반환한 입력을 일반 명령으로 다시
평가하는 현상은 native Bash와 adapter 양쪽에서 관찰됐다. 파일값과 복귀는
일치하지만 ish가 이 native 동작을 고치거나 안전한 중첩 편집을 보장하지 않는다.

### 실제 Windows Terminal/WSL

최종 소스를 별도 Linux 폴더에서 실행했고 원본과 실행본의 해시를
`.cache/stream-compat/host-source-hashes.json`으로 대조했다. 아래 결과는
Windows Terminal의 실제 출력·응답과 native 파일을 검사한 것이다.

| 기록 (`.cache/stream-compat/` 아래) | 실행 | 결과 |
|---|---|---|
| `host-bash51-final`, `host-bash52-final` | 복사한 외부 디렉터리를 지정한 데모 | 팝업·continuation·Unicode heredoc·Vim·native 복귀·종료 0·termios 복원 |
| `host-bash51-plain`, `host-bash52-plain` | 존재하지 않는 연동 경로를 지정한 기본형 | 추가 UI 없이 동일 native 파일·종료 0·termios 복원 |
| `host-compat-tcsh`, `host-compat-zsh` | 외부화한 기존 데모 | 기존 팝업과 native 동작·종료 0·termios 복원 |

데모 네 실행 모두 검증 전용 창의 임시 pane으로 실제 **120×30 → 120×22**
resize를 만들고 foreground 프로그램의 ioctl 결과와 host SIGWINCH 기록이
일치함을 확인했다. 기본형 두 실행에서는 resize를 변경하지 않았다.
`host-bash51`, `host-bash52`는 마지막 바인딩 보완 전의 중간 성공 기록이며
최종 Bash 검증과 구분하여 보존했다.

입력은 outer PTY에 scripted bytes로 전달했다. **픽셀 외관·GUI 키보드 입력·
마우스 드래그 resize·SIXEL 표시 외관은 검증하지 않았다.** 이전 결과와 같이
bytes 전달 및 호스트 응답의 성공을 해당 시각적 검증으로 확대하지 않는다.

## 검증 범위의 한계

이후 Ctrl+C 정지·종료를 다시 조사하여 native Bash/Readline에서도 생존 정지와
SIGSEGV를 재현했다. 테스트 전용 소스 수정의 대조 결과와 실사용 미적용 범위는
[Ctrl+C 조사](INTERRUPT_INVESTIGATION.md)에 기록했다. 위의 최초 실패 기록은
당시 관찰이며, 이번 재현이 과거 프로세스의 미수집 종료 상태를 소급 확정하지는 않는다.

- byte-for-byte SIXEL 전달 성공과 호스트의 실제 이미지 표시 성공은 별개다.
- 프롬프트 대기 중인 것처럼 보이는 화면만으로 popup 입력 소유권을 증명하지 않는다.
- Enter 직후 선입력 테스트 하나로 임의로 지연된 P/H 이벤트와 모든 사용자 key binding을
  검증했다고 주장하지 않는다. native 협력 프로토콜을 바꿀 때 해당 경합 테스트를 추가한다.
- 확인한 tcsh/Bash/zsh 버전과 문서화한 startup·편집기 구성 밖의 셸/플러그인,
  다른 Windows Terminal 버전은 자동으로 지원 범위에 포함하지 않는다.
