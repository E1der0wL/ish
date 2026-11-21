# Zellij 비교와 VT 보강

이 문서는 VT 기반 `ish` 2.0.0의 비교 기록이다. 이전 stream 제품/실험에는 적용하지 않는다. Zellij는 검증용이며 ish 실행 의존성이 아니다. 기존 작업은 `.cache/vt-parity/baseline-20261117.zip`에 보존했다.

## 테마 색

팔레트 색은 번호와 굵기를, 직접 지정한 RGB는 원래 채널을 유지한다. 자식 프로그램이 OSC 4로 팔레트를 바꾼 동안에는 해당 색을 RGB로 출력한다. Windows Terminal의 실제 응답으로 기본·8비트·24비트 정책과 256개 팔레트 항목을 확인했고, 같은 Nordic 계열 팔레트를 적용한 xterm에서 기본색·ANSI 색·RGB 등 11개 표본의 픽셀이 직접 실행과 일치했다. 추가한 두 표본에서는 차이가 있었다. libghostty의 공개 셀 API는 일반 ANSI 색과 명시적으로 같은 번호를 사용한 0~15번 인덱스 색을 같은 값으로 돌려준다. ish는 이 범위를 일반 ANSI 색으로 출력한다. 예를 들어 `1;38;5;1`을 보냈을 때 이 xterm은 직접 실행에서는 일반 빨강을, ish 경유 시에는 밝은 빨강을 표시했다. 파랑도 같았다.

## Esc와 일반 문자 표시 시간

기존·변경 버전을 같은 WSL 제어 PTY에서 번갈아 두 번씩 실행했다. 아래 범위는 각 실행의 중앙값이다. 일반 문자의 표시 시간은 바깥 PTY에서 키를 보낸 시점부터 자식의 응답 글자가 바깥 화면 관찰기에 나타날 때까지다. 물리 키보드와 모니터의 지연은 측정 범위에서 제외했다.

| 측정 구간 | 변경 전 | 변경 후 |
| --- | --- | --- |
| 일반 문자 한 번, 유휴 상태에서 표시까지 | 19.0~21.5ms | 8.8~11.0ms |
| 일반 문자, 앞 글자 표시 직후 다음 입력부터 표시까지 | 18.8~20.9ms | 15.9~17.8ms |
| 기존 키 방식의 단독 Esc, 자식 프로그램 수신까지 | 51.3~51.4ms | 21.2~21.4ms |

일반 문자가 자식에 도달하는 데 걸린 시간은 전후 모두 약 1ms였다. libghostty가 반환한 행이 직전 프레임의 같은 위치와 일치하면 그 행의 Python 변환 결과를 재사용한다. PTK는 한 프레임 안에서 동일한 셀 서식의 해시와 출력 문자열을 재사용한다. 문자 하나를 바꾼 화면과 모든 줄이 바뀌는 화면의 이득은 다르다. 측정값은 `.cache/keyboard-render-20261118/compare-final/summary.json`과 `.cache/key-render-tuning/frames/interleaved-cpu.json`에 있다.

실제 Windows Terminal/WSL은 이번 세션에서 확장 키를 협상했다. 이 모드의 Esc 신호를 주입한 뒤 PTK 팝업의 닫기 핸들러 진입까지 중앙값은 1.1ms였다(준비 입력 2회, 측정 16회). 검증기는 호스트로 출력된 키 모드에 맞춰 입력 신호를 선택한다. 결과는 `.cache/keyboard-render-20261118/popup-wt.json`에 있다. 같은 호스트에서 Bash·zsh·tcsh 9개 시나리오와 창 크기 변경 36회를 통과했다. 별도 SIXEL 검사에서 120×36 화면을 120×8과 29×36으로 각각 줄였다 복원한 뒤 이미지 배치·글자·커서와 최종 SIXEL 픽셀 데이터를 확인했다.

## 큰 화면의 느린 프레임과 메모리

2026-11-20에 같은 Windows Terminal 1.25.3131.0 창과 WSL2 Ubuntu 26.04에서 변경 전·후·후·전 순서로 각각 새 ish를 실행했다. 160×50 기본형에서 프레임마다 7줄을 스크롤하거나 49줄을 다시 쓰거나 240×150픽셀 이미지를 교체했다. 한 이미지의 원본 색은 36,000개이며 호스트 출력은 협상된 SIXEL이다. 각 프레임은 자식의 PTY 쓰기 시작부터 해당 화면의 ish 렌더러가 호스트 출력 flush를 마칠 때까지 측정했다. 다음 프레임은 직전 flush 확인 즉시 보냈다. 이 구간에는 모니터 주사 시간과 호스트 내부의 최종 픽셀 표시 시간은 포함되지 않는다.

각 실행의 첫 사이클을 제외하고 조건별 360프레임을 합산했다. 단위는 ms이며, 가장 느린 1% 평균은 가장 오래 걸린 4프레임의 평균이다.

| 160×50 작업 | p99 전→후 | 가장 느린 1% 평균 전→후 | 최대 전→후 |
| --- | ---: | ---: | ---: |
| 빠른 스크롤 | 251→119 | 275→126 | 300→136 |
| 전체 텍스트 갱신 | 223→94 | 230→102 | 237→110 |
| 고색상 이미지 교체 | 551→492 | 601→506 | 691→520 |

스크롤로 이동한 행은 바로 앞 화면의 native 행 데이터와 바이트가 일치할 때 재사용한다. PTK는 같은 창 크기의 좌표와 한 프레임 안의 동일한 단일 폭 글자를 재사용한다. 이미지 출력은 기존 프로토콜 분리기에서 일반 데이터 구간을 한 번에 넘기고, 기존 SIXEL 인코더가 같은 값의 반복을 묶는다. 1,000프레임의 별도 이미지 비교에서 전후 출력이 바이트 단위로 일치했다. 고색상 입력을 256색으로 변환하는 Pillow 연산은 변경 후에도 주요 비용이었다. 배포용 libghostty, Esc 및 일반 키 처리에는 변경이 없다.

서로 다른 RGB 스타일 262,144개를 연속 표시하는 4,096프레임 실험에서는 ish의 RSS가 기존 37.1→152.9MiB에서 변경 후 37.1→40.7MiB로 줄었다. PTK의 파생 스타일 조회표 크기에 화면 면적 기준 상한을 두었다. 제거한 항목은 필요할 때 다시 계산하며, 같은 입력의 출력 해시는 전후 일치했다.

지속 부하에서는 같은 세 가지 작업을 한 ish 프로세스에서 30분 52초간 11,520프레임 반복했다. 이 측정은 프레임마다 flush를 확인하고 최소 120ms 주기로 다음 프레임을 보냈다. 앞·중간·끝 3등분의 p99는 스크롤 119→93→96ms, 전체 갱신 118→89→87ms, 이미지 504→406→380ms였다. 전체 기간의 최악 프레임은 각각 231ms, 235ms, 587ms였다. 같은 PC의 다른 작업량도 변하므로 구간별 차이를 실행 시간 자체의 효과로 해석할 수는 없다.

첫 프레임 RSS는 47.26MiB였다. 26,880줄을 스크롤하면서 native history가 10,000줄에 도달했고, 11분 50초에 오래된 줄이 처음 정리됐다. 당시 68.09MiB였으며 이후 최댓값은 70.25MiB, 종료 직전은 70.14MiB였다. 이 구간을 프레임 수 기준 네 부분으로 나눈 RSS 중앙값은 68.39, 68.92, 69.88, 70.00MiB였다. 이 소폭 증가의 원인과 더 긴 실행에서의 상한은 확인하지 못했다. RSS는 ish 프로세스만 측정했다. 전체 시간 추이와 워밍업을 제외한 전후 각 작업 1,080프레임 비교는 `.cache/frame-live-20261120/report/comparison.json`에 있다.

별도 160×50 창에서 한 줄의 짧은 출력을 60회 보낸 뒤 20분간 입출력을 멈췄다. 첫 재개 프레임은 23.8ms였고 유휴 전 50프레임의 중앙값은 25.1ms, 재개 이후 59프레임의 중앙값은 19.6ms였다. 유휴 중 30초마다 측정한 41개 RSS 표본은 모두 46.78MiB였다. 이는 한 번의 유휴 복귀 관찰이다.

실호스트 4회 원자료는 `.cache/frame-live-20261120/burst-comparison.json`, 유휴 복귀는 같은 경로의 `idle-wake/comparison.json`, 텍스트·이미지의 독립 출력 비교는 `.cache/text-soak-20261120/summary-final.json`과 `.cache/graphics-soak-20261120/comparison.json`, RGB 메모리 비교는 `.cache/rgb-cache-soak-20261120/`에 있다. 측정 코드는 테스트용이며 기본 세션에는 계측을 추가하지 않는다.

### 텍스트 서식과 새 셀 생성

ish는 연속해서 출력하는 글자에 같은 링크·밑줄·윗줄이 적용되어 있으면 기존 서식을 재사용한다. PTK가 글자 속성을 설정하거나 초기화할 때, 임의의 제어문을 직접 출력할 때, 프레임이 끝날 때는 기억한 서식을 비운다. 다음 글자에 필요한 서식을 다시 전송한다. 바뀐 행을 해석할 때는 좌표, 문자 폭, 색, 링크 등 기존 10개 필드를 가진 불변 `NamedTuple`로 셀을 생성한다. 내부 `vt.Cell`을 직접 가져오는 Python 코드에서는 필드 이름으로 값을 읽을 수 있으며, dataclass 전용 `asdict`와 `replace`는 이 타입에 적용할 수 없다.

160×50 Windows Terminal/WSL 기본형에서 이전 코드, 서식만 변경, 셀만 변경, 두 변경 적용을 같은 입력으로 비교했다. 각 빌드를 두 번 실행했고 두 번째는 역순으로 진행했다. 각 작업·빌드에서 예열을 제외한 320프레임을 측정했다. 아래 시간은 자식의 출력 시작부터 ish의 호스트 출력 flush까지의 중앙값이다.

| 작업 | 이전 | 서식만 변경 | 셀만 변경 | 두 변경 적용 | 프레임당 출력 바이트 중앙값 이전 → 이후 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 7줄씩 스크롤 | 35.9ms | 31.0ms | 35.7ms | 32.0ms | 187,153 → 9,478 |
| 기존 내용을 한 줄씩 옮겨 49줄 쓰기 | 33.8ms | 28.6ms | 38.1ms | 29.2ms | 187,590 → 9,524 |
| 고유한 내용으로 49줄 쓰기 | 56.3ms | 48.6ms | 51.8ms | 46.2ms | 178,525 → 9,475 |

고유한 내용 49줄에서 Python 행 해석·셀 구성 구간의 중앙값은 이전 22.5ms, 셀만 변경 14.8ms, 두 변경 적용 13.9ms였다. 두 변경 적용 전후 가장 느린 1%인 4프레임의 평균은 스크롤 65.3→62.3ms, 행 이동 71.0→63.0ms, 고유 내용 142.9→154.2ms였다. 비교 원자료는 `.cache/text-render-optimization/benchmark/summary.json`이다.

전체 회귀를 마친 뒤 구간별 계측을 빼고 같은 Windows Terminal 창에서 재측정했다. 이전 빌드와 두 변경을 적용한 빌드를 각각 여섯 번 실행하며 빌드 순서와 작업 순서를 번갈아 바꿨다. 예열 이후 작업·빌드별 720프레임이며 가장 느린 1% 평균은 오래 걸린 8프레임의 평균이다. 아래는 이전 → 이후 값이다.

| 작업 | 중앙값 | p99 | 가장 느린 1% 평균 |
| --- | ---: | ---: | ---: |
| 7줄씩 스크롤 | 31.7 → 24.6ms | 46.8 → 42.6ms | 51.0 → 63.7ms |
| 기존 내용을 한 줄씩 옮겨 49줄 쓰기 | 29.8 → 23.9ms | 40.6 → 36.8ms | 48.0 → 56.3ms |
| 고유한 내용으로 49줄 쓰기 | 47.2 → 34.6ms | 75.3 → 54.3ms | 108.8 → 61.2ms |

세 작업에서 중앙값과 p99는 줄었다. 스크롤과 행 이동에서는 가장 느린 8프레임의 평균이 늘었으며, 변경 후 가장 오래 걸린 프레임은 각각 122.4ms와 107.6ms였다. 이때 렌더링 스레드의 CPU 시간은 각각 119.9ms와 103.1ms였다. 어떤 함수가 해당 지연을 만들었는지는 이 재측정으로 특정할 수 없다. 원자료는 `.cache/text-render-optimization/latency/summary.json`에 있다. 별도 검증에서는 한글·결합문자·링크·서식·팝업·화면 전환과 resize에 대해, 이전 버전과 변경 버전의 출력을 각각 수신 터미널로 해석한 142개 상태의 셀과 커서가 일치했다.

## 비교 기준

비교 버전은 공식 Zellij 0.45.0이다. 다운로드 SHA-256은 `65101a384562224493e3f29812c9ecb00365df47dec6032f486b9556d81712e9`이다. 동일한 controlling PTY 크기·입력·자식 프로그램을 사용하고, 화면·응답·파일 결과를 독립 기대값과 대조한다. Zellij의 모든 차이가 정답은 아니다.

1. 문자 삽입·RGB 스타일·한글·결합문자·이모지, alternate 화면 복귀, DSR, application cursor, Alt, bracketed paste, resize, termios 복원을 비교한다.
2. Vim, Neovim, less, htop, fzf, Python의 실제 입력과 저장 결과를 검사한다.
3. 실제 Windows Terminal/WSL의 화면·입력·호스트 응답을 별도로 검사한다. PTY의 Unicode bytes 통과는 물리적인 한글 IME 검증을 대신하지 않는다.
4. 동일 출력량과 echo 입력으로 처리 시간·CPU·RSS를 기록한다. 단일 환경의 비교 측정이며 보편적인 성능 보장이나 장기 메모리 무결성 증명은 아니다.
5. SIXEL/Kitty/iTerm 입력과 실제 호스트의 이미지 출력, 애니메이션, 팝업 가림·복원을 확인한다.

## 구현 경계

- 공식 libghostty 1.0.1 원본과 private bridge ABI 8을 사용한다. 엔진 소스 패치는 없다. SIXEL·iTerm은 공통 Unicode placeholder 경로를 사용하며 화면·history·선택·이미지 원본은 native 엔진이 소유한다. 아래 ABI 5·6·7 기록은 이전 구현의 증거로 구분한다.
- SIXEL 보완 코덱과 출력 presenter를 추가했다. 호스트는 DA1 속성 4로 지원을 확인한다. XTSMGRAPHICS에 답하지 않는 Windows Terminal에서도 실제 응답이 있는 DA1을 사용할 수 있다. 셀 크기와 DECRQM 80 초기값이 없으면 켜기를 요청해도 출력하지 않는다.
- iTerm을 명시적으로 켜면 우선하고, 이어 Kitty, SIXEL 순서로 출력 담당자 하나만 선택한다. 설정·조회 결과·활성화 결과를 구별한다.
- 알림과 OSC 133 탐색은 선택 기능이다. 기본형은 UI·단축키·hook을 추가하지 않는다. 자세한 의미와 제한은 [선택 확장](VT_OPTIONAL.md)에 둔다.
- 변환 모듈은 [SIXEL 지원 범위](VT_SIXEL.md)의 계약을 소유한다. native 지원으로 교체할 때 동일한 입력·화면 수명 검사를 통과해야 한다.

## 확인한 차이

분해된 결합문자 `e` + U+0301 입력은 ish에서 `é`로 표시되지만 Zellij 0.45.0에서는 악센트가 사라졌다. 최종 비교기는 전체 문자열의 NFC 기대값을 검사하고, Zellij 결과를 `different`와 종료 코드 1로 기록한다. 이 차이에 맞춰 ish를 바꾸지 않았다.

현재 SIXEL 문자 덮어쓰기와 ECH 등 셀 지우기는 native placeholder 셀의 교체·삭제를 따른다. 투명 부분 아래의 원래 문자·이미지는 보존하지 않는다. Zellij의 ECH 보존이나 과거 ish 패치 버전과 다른 동작이며, 이를 맞추기 위한 별도 상태나 예외 처리는 추가하지 않는다. 일반 Kitty 이미지와 레이어는 native 의미를 유지한다.

SIXEL은 일반적인 부분 알파를 표현하지 못한다. 원래 셀이 빈 칸이고 배경색을 알 때만 배경 위 알파를 계산할 수 있다. 최종 SIXEL 출력은 별도로 256색으로 줄이므로 사진·그라데이션에 눈에 띄는 색 손실이 생길 수 있다. 글꼴 픽셀이 필요한 반투명 이미지와 글자 중첩은 지원한다고 선언하지 않는다.

## 증거

- 최초 기준: `.cache/comparison-baseline-7/summary.json` — 변경 전 ish 및 Zellij의 core 계약 통과.
- 확장 비교: `.cache/comparison-current-all-1/summary.json` — 두 제품의 core·실제 6개 앱·출력/echo 측정 통과.
- 강화한 결합문자 비교: `.cache/comparison-unicode-confirmed/summary.json` — ish 통과, Zellij 악센트 손실. 앞선 느슨한 기대값과 구별한다.
- 최종 8개 실행: `.cache/comparison-final/summary.json`. 200,000줄·23.4 MB의 반복 출력은 `.cache/comparison-stress-1/summary.json`에서 양쪽 통과.
- 실제 호스트 질의: `.cache/vt-parity/host-probe.json` — SIXEL DA1, mode 80, 10×20 cell 응답. XTSMGRAPHICS 응답 없음.
- 실제 Windows Terminal 이미지: `.cache/vt-parity/presentation-2/result.json` — SIXEL·Kitty animation·iTerm PNG, 팝업과 이미지 영역의 실제 교차, 종료 후 mode 80 `2→2`와 termios 복원 통과.
- 실제 픽셀: 같은 폴더의 `user-popup.png`, `visual-observation.json`. 사용자가 투명 영역의 원래 글자·팝업 가림·Esc 뒤 빨간 사각형 복원을 확인했다. animation의 세부 픽셀 전체나 모든 알파/레이어 조합을 사람이 확인했다는 뜻은 아니다.
- 실제 키보드: `.cache/vt-parity/keyboard-1/home/`의 popup/native 파일이 각각 `한글 입력 확인`과 같은 문구+개행이다. 첫 종합 검사는 추가 선행 `d`가 들어와 키 단계에서 실패한 기록을 보존했다. `.cache/vt-parity/keyboard-keys-2/result.json`에서 키 단계만 다시 실행해 `1b781b0d`(Alt+x, Esc, Enter)가 정확히 일치했다. 선행 글자의 발생 원인은 확정하지 않았다.
- 실제 이미지 가로 resize: `.cache/presentation-resize/geometry-2/case/result.json`에서 SIXEL·Kitty animation·iTerm PNG 각각 팝업 표시/닫힘 상태로 `120×36→82×36→120×36`, 총 6회 통과. native 이미지 ID·RGBA hash·placement, 새 호스트 픽셀 크기, 자식 SIGWINCH와 최종 입력 파일을 검증했다. 이번 resize 기록은 실제 픽셀 촬영을 대신하지 않는다. 첫 `geometry-1`의 관찰 파일 교체 경합 실패를 보존했고, 검증기가 단일 snapshot을 읽도록 보강했다.
- 실제 이미지 세로 resize: `.cache/presentation-resize/geometry-height-3/case/result.json`에서 같은 세 장면이 `120×36↔120×24`, 총 6회 통과. 이미지가 축소 화면 안에 있는 조건에서 원본·배치·입력·모드 80 및 termios 복원을 확인했다. 화면 밖으로 잘리는 모든 배치를 검증한 것은 아니다.
- 최종 실제 Windows Terminal 셸 검사: `.cache/management-native/parity-order-final-20261118/summary.json` — Bash·zsh·tcsh 기본형/플러그인 6개와 반복 출력 3개, 총 9개·실제 resize 36회 통과. 각 반복 출력에서 1,280행·팝업 4회·resize 8회와 최종 history를 확인했다. `input-wire-observations.jsonl`에는 응답 도중 scripted 입력 삽입이 없었고, 팝업 Esc 14.04ms 뒤 완전한 크기 응답이 들어온 실제 경합도 정상 처리됐음을 기록했다. 테스트 전용 입력 기록이며 제품 로그에는 입력 본문을 추가하지 않았다.
- 최종 전체 회귀: `.cache/vt-parity/full-final-3.json` 및 `.log` — **731개 통과**, skip 없음, Ruff/서식 128개 파일 통과. 검사 전후 제품 소스 해시가 같다. 최초 703개 실행의 옛 SIXEL 진단·mode 80 기대값 실패, 그 이전 집중 검사의 ABI 기대값 보정, 검사기 서식 중단, 728개가 통과했지만 실행 중 ESC 수정으로 소스가 바뀐 중간 결과도 그대로 보존했다. 원격 GitHub CI를 실행했다는 뜻은 아니다.
- 배포본: `/home/user/.cache/ish-build/vt-parity-20261118`을 새로 빌드하고 `.cache/vt-parity/package-smoke.json`에서 설치된 ish 2.0.0·동봉 native bridge·Bash/zsh/tcsh 기본형/플러그인 6개 조합을 확인했다. 실행 중 호스트 Python·컴파일러를 호출하지 않았으며 읽기 전용 help/version/diagnose도 검증했다.

## 2026-11-18 순정 엔진과 공통 placeholder 전환

- 공식 libghostty 1.0.1 소스를 수정하지 않고 ABI 7 연결 코드를 빌드했다.
  `.cache/placeholder-images/native-provenance.json`에서 공식 archive와 소스 파일의
  일치, 라이브러리 출처·alias, 패치 전용 native 심볼이 없음을 확인했다.
- 기존 iTerm 배치 함수를 SIXEL에도 사용한다. 투명 부분을 포함한 셀 교체,
  ECH 삭제, native 레이어·스크롤·reflow를 따르며 별도 복원 상태는 없다.
  각 셀의 원본 행·열을 명시해 부분 삭제 뒤 다색 이미지가 틀어지지 않도록 했고,
  고정 배치의 행 이동은 스크롤 영역에 영향을 주지 않는다.
- 집중 이미지 회귀 **114개**와 최종 전체 회귀 **758개**가 통과했다. skip 없이
  Ruff·format 128개 파일도 통과했다. `.cache/placeholder-images/full-1.json`과
  `.log`에 기록했으며 실행 전후 코드·테스트·빌드 도구 해시는
  `f7f5e3a415fb19b216bad660cd3b59301d85330113ec40f150ef3bd5696fb16b`로 같다.
- 실제 Windows Terminal/WSL 셸 검증은
  `.cache/management-native/placeholder-20261118-1/summary.json`이다.
  Bash·zsh·tcsh 기본형/플러그인과 연속 출력 **9개**, 실제 resize **36회**가
  통과했다. continuation·heredoc·Vim·입력 파일과 종료 복원을 포함한다.
- 실제 이미지 덮어쓰기 검증은 `.cache/placeholder-images/overwrite-1/case/result.json`이다.
  native 원본 RGBA 불변, 제거된 셀의 placement/crop, 최종 SIXEL 출력의 글자 칸을
  독립적으로 확인했다. 팝업 복원·resize **2회**·입력 파일·mode 80 및 termios 복원이 통과했다.
- 기존 SIXEL 투명 입력·Kitty 애니메이션·iTerm PNG 검증은
  `.cache/presentation-resize/placeholder-20261118-1/case/result.json`이며,
  팝업 가림/복원과 resize **6회**가 통과했다.
- 이미지 내부 좌표 문자가 선택·복사에 새지 않도록 기존 grapheme 분리를 재사용했다.
  한글·결합문자·이모지·개행을 보존하는 실제 native 선택 검사도 전체 회귀에 포함한다.
- 신규 배포본 `/home/user/.cache/ish-build/placeholder-20261118`의 빌드와
  Bash·zsh·tcsh 기본형/플러그인 **6개** smoke 검사가 통과했다.
  `.cache/placeholder-images/package-check.json`, `package-smoke.json`,
  `bundle-native-check.json`에 기록했다. 설치 Python의 `-I` 실행으로 소스 경로와
  분리한 뒤 실제 SIXEL 셀 덮어쓰기·같은 문자 반복·원본 RGBA/세대 불변을 확인했다.
  동봉 엔진의 순정 출처·해시와 패치 전용 심볼이 없음도 확인했다.
- 실제 화면 재검사에서 팝업이 닫힌 뒤 빨간 사각형 안에 `A한`이 보이고, 글자가 차지한
  세 칸만 비워진 채 나머지 빨간 영역이 이어지는 것을 사용자가 확인했다. 관찰과 첨부 화면은
  `.cache/placeholder-images/visual-2/visual-observation.json`과 `restored-screen.png`에 보존했다.
  같은 실행에서 복원 후 SIXEL 출력 해시가 기대값과 일치했고, 검사 종료 시 mode 80과
  호스트·외부 PTY의 termios가 모두 시작값으로 돌아왔다.

## 이전 구현: 2026-11-18 native 패치 덮어쓰기 검증

이 절은 제거된 패치 구현의 과거 기록이다. 아래 픽셀·성능·배포 결과를 순정 엔진과 placeholder 구현의 검증으로 사용하지 않는다.

- 집중 회귀 125개, 최종 전체 회귀 **751개** 통과. skip 없이 Ruff·서식 128개 파일도 통과했다. `.cache/sixel-overwrite/full-1.json`과 `.log`에 보존했고, 실행 전후 제품·테스트·빌드 도구·native 패치 해시는 `acdcf0ff81ecb8862608ff3015cd0977a3f6c46d85b5eb34d1aca4b921fee314`로 같다.
- 같은 글자 재출력, 공백·REP, UTF-8 분할, 한글·결합문자·variation selector 폭 변경, wrap·origin mode의 쓰기 위치, 겹친 SIXEL, 일반 Kitty와 ID 재사용, history·alternate·RIS·synchronized output 및 실제 PTY 입력 파일을 확인했다. ECH 보존은 별도 계약이다.
- 실제 Windows Terminal/WSL 새 덮어쓰기 검사: `.cache/sixel-overwrite/host-2/case/result.json`. 160×80 이미지의 3셀×1행 삭제를 native RGBA와 호스트로 보낸 SIXEL에서 각각 검증했다. 팝업 닫힘과 실제 resize 2회 후에도 삭제 상태·native 자산 ID·입력 파일이 유지됐고, mode 80 `2→2`와 termios가 복원됐다.
- 실제 픽셀: `.cache/sixel-overwrite/visual-1/visual-observation.json`. 사용자가 팝업 닫힘 뒤 “A한이 보이고 글자 칸만 비워져 있음”을 확인했다. 같은 폴더의 자동 검사도 정상 종료·모드 복원까지 통과했다. 사용자 관찰과 자동 wire/asset 검증을 구분하며, 새 화면 캡처나 모든 색상 조합의 픽셀 검증을 의미하지 않는다.
- 기존 이미지 실제 호스트 회귀: `.cache/presentation-resize/overwrite-regression-20261118/case/result.json`. SIXEL 투명 이미지·Kitty animation·iTerm PNG와 팝업, 실제 resize 6회 통과.
- 실제 셸 회귀: `.cache/management-native/sixel-overwrite-20261118/summary.json`. Bash·zsh·tcsh 기본형/플러그인 6개와 반복 출력 3개, 실제 resize 36회 통과. continuation·heredoc·Vim·파일 결과와 종료 복원을 포함한다. 이번 검사는 물리 키보드·IME 재검증을 포함하지 않는다.
- 설치용 배포본 `/home/user/.cache/ish-build/sixel-overwrite-20261118`을 빌드했다. `.cache/sixel-overwrite/package-smoke.json`의 Bash·zsh·tcsh 기본형/플러그인 6개와 읽기 전용 CLI 조회가 통과했다. `.cache/sixel-overwrite/package-image.json`에서는 소스 작업 폴더와 분리한 `-I` Python으로 동봉 ABI 6을 로드하고, 동일 문자 재출력의 native 셀 보존·이미지 alpha 삭제를 다시 확인했다. 원격 GitHub CI를 실행하거나 릴리스를 게시한 결과는 아니다.
- 최초 호스트 시도 `.cache/sixel-overwrite/host-1`은 Windows 쪽 결과 파일 읽기가 WSL의 원자 교체를 잠가 검증 기록 단계에서 실패했다. `.next`의 실패 결과와 원래 중간 결과를 함께 보존했다. 제품 코드 변경 없이 읽기 경합을 없앤 새 실행이 통과했다.
- 최종 독립 재검토에서 추가 차단 결함을 발견하지 못했다. libghostty 원본의 정확한 해시, 적용 패치, 실제 native 바이너리 출처를 검증하며 배포 자료에도 포함한다. upstream 갱신 때 패치를 재검토해야 한다. SIXEL history가 남을 때의 문자별 처리 비용은 [SIXEL 지원 범위](VT_SIXEL.md)에 기록했다. 부분 알파의 호스트 의존성과 256색 인코딩 제한은 이번 변경 범위에 포함하지 않았다.

## 이전 전체 경로 성능 관찰

순정 placeholder 전환 전 구현에서 다른 검사를 종료한 뒤 측정한 값이다. `.cache/comparison-final-performance-20261118/summary.json`에 양쪽 입력·파일 결과와 샘플을 보존했다. 현재 전환 구현의 성능 측정으로 사용하지 않는다.

| 측정 | ish | Zellij 0.45.0 |
|---|---:|---:|
| 20,000행·2.26 MB 출력 관찰 완료 | 264 ms | 320 ms |
| echo 입력 중앙값 | 24.7 ms | 17.1 ms |
| 프로세스 RSS 합계 최대 관찰값 | 65.8 MiB | 110.3 MiB |
| 샘플링한 프로세스 CPU 합계 | 1.46 s | 1.06 s |

단일 WSL 로컬 실행이며 일반적인 우열·지연 상한·장시간 안정성 보장이 아니다. RSS 합계는 공유 페이지를 중복 계산하며, CPU는 짧게 실행된 자식을 놓칠 수 있다. 관찰 지연에는 제어기·pyte 비용도 포함된다. 이미지 변환의 비용은 별도 [SIXEL 문서](VT_SIXEL.md)에 기록한다.

## 재검토에서 보강한 오류

- 이전 regular placement 구현에서는 SIXEL의 전송 순서를 별도 z 값으로 보강했다. 현재는 그 상태를 제거하고 native placeholder 셀 교체와 레이어 순서를 따른다.
- host ECH만으로 이미지 삭제를 가정하지 않고 SIXEL canvas 변경은 ED2와 전체 PTK repaint로 처리한다. 성능 비용을 숨기지 않는다.
- 화면 고정 SIXEL이 pending wrap을 지워 다음 글자를 덮던 경우는 이미지를 안전하게 거부해 입력을 보존한다.
- synchronized output 중 새 이미지 검증은 live native 저장소를 조회한다. 렌더링은 기존 held snapshot을 유지한다.
- 이미지가 수신 중 비활성화되면 그 프레이밍 record만 취소한다. 재활성화 뒤 오래된 image tail이 새 이미지로 등장하지 않으며 제목·클립보드·질의는 유지한다.
- 취소된 Kitty APC가 C1 ST·새 ESC·ESC 뒤 CAN/SUB로 끝나도 native 재생 경로를 통해 되살아나지 않도록 종료 순서를 통일했다. 다음 정상 이미지는 계속 수용한다.
- Windows Terminal/WSL이 커널 픽셀 크기를 0으로 보고할 때 resize 후 이미지가 영구 비활성화되던 경로에 직렬 크기 재조회를 연결했다. 대기 중 native 원본을 보존하고 기존 Session의 출력 역압을 재사용한다. 실패 시 텍스트 출력은 재개한다. 응답 identity의 한계는 [기능 정책](VT_FEATURE_POLICY.md)에 명시한다.
- 크기 복구 후 자식 출력이 없어도 멈췄던 애니메이션 타이머를 다시 시작한다. 시작 전에 출력이 보류된 PTY는 읽기 callback을 등록하지 않아 바쁜 반복을 방지한다.
- 빈 인자를 포함한 native 모드 명령을 SIXEL 모듈이 잘못 가로채 세션을 닫던 결함을 수정했고, 과도한 SIXEL 파라미터 목록은 분리·할당 전에 한도를 확인한다.
- 실제 tcsh 반복 검사에서 팝업 Esc와 호스트 크기 응답이 인접할 때 응답 일부가 명령줄에 나타났다. 연속된 ESC/C1 시작 문자를 PTK에 함께 넘기지 않도록 프레이밍을 수정하고, Alt+방향키·모든 분할 위치·실제 PTK 팝업과 PTY 입력 파일을 검증했다. 최초 실패는 `.cache/management-native/parity-geometry-final-20261118/tcsh-stress/result.json`에 보존했다.
- 알림은 native/보완 경로 순서를 보존하며, OSC 9 숫자 하위 명령으로 오해할 수 없도록 고정된 프로그램 접두사를 사용한다.
