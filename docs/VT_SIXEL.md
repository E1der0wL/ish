# SIXEL 보완 모듈 지원 범위

ish는 SIXEL을 libghostty의 native 지원이라고 표시하지 않는다. 고정된 입력
보완 모듈이 완성된 DCS를 RGBA로 변환하고, 기존 Kitty 입력을 통해 native
이미지 저장소에 넣는다. 화면·스크롤백·커서·이미지 수명은 libghostty가
계속 소유한다. 별도 화면 격자나 호스트 raw passthrough는 없다.

## 입력

`terminal.sixel.Decoder`는 P1 픽셀 비율, P2 배경, RGB/HLS 색 정의,
256개 팔레트, 반복, graphics carriage return/next line, raster attributes를
해석한다. 투명 입력 `P2=1`은 별도 alpha 마스크를 사용하므로 팔레트 255도
정상 색으로 사용할 수 있다. 입력 전체를 검증한 뒤 native 저장을 시작한다.
CAN/SUB·분할 전송·RIS는 공통 ProtocolStream이 처리하며, 미완성 데이터는
화면 문자열로 흘러나가지 않는다. 잘못된 완성 이미지 이후 정상 입력은
계속 처리한다.

`SixelImages`는 기존 iTerm 입력과 같은 native Unicode placeholder 배치를
재사용한다. 이미지가 차지한 셀의 원래 문자와 이전 이미지 셀은 투명 픽셀
부분까지 교체한다. 투명 이미지 아래 기존 글자와 이미지의 보존은 지원하지
않는다. 새 글자를 쓰거나 셀을 지우면 libghostty가 해당 placeholder를 제거하고,
그 칸의 이미지 조각도 사라진다. native 이미지 원본 RGBA를 수정하지 않으며
별도 문자 격자·덮어쓰기 추적·VT 파서를 만들지 않는다. 각 셀에 원본 행·열을
명시해 중간 셀 삭제 후에도 남은 이미지 조각의 좌표를 보존한다.
alternate screen·history·ED2·RIS와 resize는 native 셀 수명을 따른다.
일반 Kitty placement와 레이어 순서는 native 의미를 유지한다. 팝업 가림은
이미지 셀을 교체하지 않는 일시적인 표시 마스크이므로 닫으면 이미지가 복원된다.
DECSDM(80)은 화면 원점에서 스크롤 없는 출력과 커서 기준 스크롤 출력을
구별한다. 8452는 출력 후 오른쪽 커서를 선택한다. private palette(1070)는
항상 켜짐으로 질의에 답하며 공유 팔레트 애니메이션은 지원하지 않는다.
XTSMGRAPHICS는 읽기 질의만 제공한다. DA1의 SIXEL 표시는 native 저장과
실제 호스트 출력이 활성화됐을 때만 기존 native callback에 추가된다.

다음 동작과 제한은 별도로 구분한다.

- ECH·EL 등 셀 지우기는 해당 이미지 셀도 지운다. 공백 문자 출력도 그 칸을
  교체하며, ED2는 화면의 이미지 셀을 제거한다. Zellij의 SIXEL ECH 보존과
  다르지만 이를 흉내 내기 위한 별도 예외 처리나 복원 상태는 두지 않는다.
- 투명한 부분과 셀 정렬을 위한 padding도 이미지 셀의 일부다. 새 이미지가
  덮은 셀의 이전 내용은 남지 않는다. 삽입·삭제·reflow도 native 문자 셀의
  동작을 따르며, 이미지가 항상 고정된 픽셀 사각형을 유지한다고 보장하지 않는다.
- ish 내부 선택·복사는 이미지 placeholder와 그 좌표 문자를 공백으로 반환한다.
  일반 글자와 결합문자는 보존한다. native 검색은 엔진의 원래 문자열을 사용하므로
  이미지 좌표 결합문자가 검색 대상에 포함될 수 있다. 이를 숨기는 별도 검색
  인덱스는 두지 않는다. 본체 터미널의 직접 복사는 이미 그려진 공백을 사용한다.
- origin mode 또는 수평 margin mode의 입력은 거부한다. 수직 margin 조합의
  전체 호환성은 검증하지 않았다.
- display mode 80에서 커서가 마지막 열의 pending-wrap 상태라면 이미지를
  거부한다. 공개 API로 이 상태를 손실 없이 복원할 수 없어 다음 문자를
  잘못된 열에 덮어쓰는 대신 원래 텍스트 동작을 보존한다.
- 이미지를 전송 중간에 점진적으로 보여주지 않는다. 완성된 DCS만 반영한다.
- 팔레트 공유, 과거 이미지 색을 바꾸는 팔레트 애니메이션은 제공하지 않는다.
- 역사적 장치별 기본 배경 영역·커서 차이를 모두 재현한다고 보장하지 않는다.

libghostty 소스는 수정하지 않는다. 공식 archive checksum과 추출된 원본 파일을
대조하고, 과거 패치 캐시와 분리한 순정 소스에서 빌드한다. 엔진·컴파일러·빌드
옵션·실제 라이브러리 해시를 기록하며 이전 패치 산출물은 새 bridge에서 거부한다.
기존 패치 파일·전용 등록 API·패치 revision은 제거했다. 보완 모듈은 공개
Kitty 입력을 생성할 뿐이며, Python에서 문자 쓰기를 해석하지 않는다.

## 호스트 출력

HostBroker가 DA1의 SIXEL 기능 응답과 mode 80 시작 상태를 확인해 출력 정책을
집행한다. 실제 Windows Terminal은 DA1의 기능 4 및 mode 80 질의에 응답했으며,
XTSMGRAPHICS에는 응답하지 않았다. XTSMGRAPHICS 무응답을 지원 근거로 쓰지 않는다.
80을 켜고 시작값으로 복원하는 책임도 HostBroker에 있다.

`RasterPresenter`는 기존 iTerm 합성 경로를 재사용한다. native image의 crop·scale·
layer·popup 가림을 반영한 하나의 RGBA 화면을 SIXEL로 재인코딩한다. mode 80에서
원점이 고정되므로 앞쪽 빈 공간을 투명 픽셀로 채운다. SIXEL 출력은 ECH로 지워지지 않는 호스트도 있으므로 ish가 소유한 alternate
screen 전체를 ED2로 지운 뒤 PTK 문자·팝업 전체 재그리기, 새 이미지 순서로
복원한다. 이미지 장면이 바뀔 때 전체 repaint 비용이 들며, 기존 iTerm 경로의
부분 ECH 동작은 유지한다. native 원본 이미지는 변경하지 않는다.

SIXEL은 이진 투명도만 표현한다. 부분 alpha는 해당 native 셀이 장식 없는 빈
셀이고 명시한 RGB 배경 또는 실제 조회한 호스트 배경이 있을 때만 평탄화한다.
글자·역상·알 수 없는 배경 위의 부분 alpha는 정확하게 합성할 수 없어 거부한다.
팝업의 투명 구멍은 평탄화하지 않는다. 아래쪽 text layer 역시 기존 RasterPresenter의
검증 정책을 따른다. 배경 위 alpha 계산과 최종 색 표현은 별개다. 256색으로
줄이므로 사진·그라데이션에는 눈에 띄는 색 손실이나 띠가 생길 수 있다.
RGB 백분율 단위의 반올림 오차도 추가된다. Kitty와 iTerm 출력의 기존 alpha
경로는 유지한다.

## 한도와 성능

- 입력: 16 MiB DCS 본문, 최대 4,194,304 pixels, 한 변 16,384 pixels,
  명령 1,048,576개, 256개 색, 숫자 인자 최대 65,535.
- 출력: 최대 4,194,304 pixels와 presenter의 기존 wire/cache budget.
- 한 변 제한과 전체 픽셀 제한은 동시에 적용한다. 16,384² 이미지를 허용한다는
  뜻이 아니다. 한도 초과는 이미지 실패로 기록하고 다음 입력으로 진행한다.
- placeholder 좌표는 기존 공통 diacritic 표의 297칸 범위 안이어야 한다. 실제로 출력하는
  행·열 각각에 이 제한을 적용하며, 셀 정렬 padding까지 포함해 16 MiB RGBA
  메모리 한도를 검사한다. 화면 밖으로 잘려 출력하지 않는 열에는 좌표 제한을
  추가하지 않는다. 제한 초과는 업로드·커서 이동 전에 거부한다.
- 인코딩한 장면은 image generation·geometry·가림·배경·관련 native cells가
  바뀌지 않으면 재사용한다. 정적 화면에서 매 프레임 재인코딩하지 않는다.
- native 문자 쓰기 경로를 수정하지 않는다. placeholder 전송·이미지 변환·호스트
  재그리기의 비용은 남으며, 이전 패치 버전의 성능 측정을 현재 성능으로 사용하지 않는다.

WSL 로컬 solid-RGB fixture, 각각 3회 중앙값(2026-11-17):

| 크기 | 인코딩 | 디코딩 | SIXEL wire |
|---|---:|---:|---:|
| 320×180 | 58 ms | 2.3 ms | 301 bytes |
| 1200×600 | 306 ms | 7.0 ms | 1,032 bytes |

이는 프로토콜 codec 측정이며 PTY·PTK·호스트 렌더링 전체 지연이 아니다.
고색상 사진과 동영상은 다른 비용을 가진다. 고 FPS 동영상이나 Zellij와 동일한
처리량은 보장하지 않는다. 추가 native runtime dependency는 없고 기존 Pillow를
양자화·픽셀 처리에 사용한다. libsixel은 비교 oracle로만 사용할 수 있다.

## 검증과 향후 제거

`tests/test_terminal_sixel.py`는 literal 입력·독립 wire parser로 픽셀, alpha,
palette 255, ratio, HLS, repeat, 예산, 가림/복원, 배경 평탄화, native history,
alternate/ED2/RIS, byte 단위 fragmentation, 취소, 오류 후 복귀, mode query,
커서/스크롤/저장 커서, 이미지 순서를 검증한다. 입력 분류/기존 iTerm/기존 graphics
회귀도 유지한다. synchronized output의 입력 검증은 live native image 존재를
조회하며, 렌더링용 frozen image snapshot과 혼동하지 않는다. SIXEL과 iTerm을
2026 hold 안에서 보낸 뒤 release 시 표시하는 회귀를 함께 유지한다.
seeded 임의 데이터 5,000개에서 정상 결과 또는 ValueError로
종료함을 추가 관찰했으며 이것이 모든 입력의 증명은 아니다.

Zellij 비교는 `.cache/comparison-images-2/zellij-images`의 실제 PTY 출력과
Zellij 0.45.0 `grid.rs`의 문자 쓰기/erase 구현을 함께 확인했다. 실제 Windows
Terminal 픽셀 합성 검증 결과는 별도의 현재 검증 보고서에 기록한다. 자동 wire
검증만으로 실제 호스트 픽셀 통과를 선언하지 않는다.

검증된 libghostty 버전이 SIXEL 입력과 같은 수명 계약을 제공하면
`select_routes()`를 native로 선택하고 `SixelImages`와 `Decoder`를 제거할 수 있다.
호스트 SIXEL `Encoder`, popup 합성, 권한/지원 질의, 시작 상태 복원은 출력 측
책임이므로 native 입력 지원만으로 제거하지 않는다. 공개 plugin 등록 범위는
변경하지 않았고 임의 DCS/CSI 선점 API를 추가하지 않았다.

근거: [Xterm control sequences](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html),
[Windows Terminal SIXEL parser](https://github.com/microsoft/terminal/blob/main/src/terminal/adapter/SixelParser.cpp),
[libghostty image API](https://lib.ghostty.org/vt/guide-images.html).
