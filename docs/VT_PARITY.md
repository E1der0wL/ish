# Zellij 비교와 VT 보강

이 문서는 VT 기반 `ish` 2.0.0의 비교 기록이다. 이전 stream 제품/실험에는 적용하지 않는다. Zellij는 검증용이며 ish 실행 의존성이 아니다. 기존 작업은 `.cache/vt-parity/baseline-20261117.zip`에 보존했다.

## 비교 기준

비교 버전은 공식 Zellij 0.45.0이다. 다운로드 SHA-256은 `65101a384562224493e3f29812c9ecb00365df47dec6032f486b9556d81712e9`이다. 동일한 controlling PTY 크기·입력·자식 프로그램을 사용하고, 화면·응답·파일 결과를 독립 기대값과 대조한다. Zellij의 모든 차이가 정답은 아니다.

1. 문자 삽입·RGB 스타일·한글·결합문자·이모지, alternate 화면 복귀, DSR, application cursor, Alt, bracketed paste, resize, termios 복원을 비교한다.
2. Vim, Neovim, less, htop, fzf, Python의 실제 입력과 저장 결과를 검사한다.
3. 실제 Windows Terminal/WSL의 화면·입력·호스트 응답을 별도로 검사한다. PTY의 Unicode bytes 통과는 물리적인 한글 IME 검증을 대신하지 않는다.
4. 동일 출력량과 echo 입력으로 처리 시간·CPU·RSS를 기록한다. 단일 환경의 비교 측정이며 보편적인 성능 보장이나 장기 메모리 무결성 증명은 아니다.
5. SIXEL/Kitty/iTerm 입력과 실제 호스트의 이미지 출력, 애니메이션, 팝업 가림·복원을 확인한다.

## 구현 경계

- 공식 libghostty 1.0.1 원본과 private bridge ABI 7을 사용한다. 엔진 소스 패치는 없다. SIXEL·iTerm은 공통 Unicode placeholder 경로를 사용하며 화면·history·선택·이미지 원본은 native 엔진이 소유한다. 아래 ABI 5·6 기록은 이전 구현의 증거로 구분한다.
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
