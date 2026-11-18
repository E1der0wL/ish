# ish-vt terminfo 추가 대조 결과

> 통합 전 검증 기록이다. 현재 제품과 XTVERSION 이름은 `ish`로 통일됐다. 아래 이름·stream 보존 설명은 당시 결과이며 현재 실행 방법은 [사용 안내](VT_USAGE.md)를 참고한다.

2026-11-17의 추가 검증이다. [이전 완료 기록](VT_BASELINE_RESULT.md)의 188/287 결과와 실제 Windows Terminal 기록은 당시 결과로 보존한다. 이 문서는 **ish-vt**의 libghostty-vt + PTK 경로만 다룬다. ish 2.0.0의 stream 실행·지원 범위는 변경하지 않았다.

## 무엇을 확인했는가

Ubuntu/WSL에서 실제 `infocmp -1 -x xterm-256color`를 다시 읽었다. boolean 13개, number 5개, string 269개로 기존 카탈로그의 **287개 이름과 일치**한다. 기존 `TerminfoContracts`가 조회하는 188개에 새 `RemainingTerminfoContracts`의 string 81개와 metadata 18개를 합치면 누락과 추가 없이 287개가 된다.

검증 대상 이름을 모두 분류하고 조회했다는 뜻이지, **287개 모두를 완전 지원한다는 뜻은 아니다.** 11개는 아래에 명시한 프로파일 차이 또는 기존 제외 범위다. 나머지 276개에도 기본값 검사, native 셀·입력·응답 검사, 호스트 조건부 기능이 섞여 있다. 모든 파라미터 조합, 물리 키, 외관, 타이밍을 전수 인증한 수치로 사용하지 않는다.

새 테스트는 기존 libghostty-vt, 셀 조회 도우미, clipboard broker, ncurses `tiget*`·`tparm`·`tput`을 재사용한다. 기대값은 실제 셀 문자열·독립적인 색상값·wire literal로 작성했다. 제품의 변환 함수로 기대값을 만들지 않았다. 이번 추가 대조에서는 제품 코드를 변경해야 하는 새로운 기본 동작 결함이 확인되지 않았다.

## 잔여 99개와 검사 연결

아래 메서드는 모두 `tests/test_terminal_terminfo.py`의 `RemainingTerminfoContracts`에 있다. 이전 188개는 `tests/test_terminal_contracts.py`의 기존 검사를 유지한다.

| 항목 | 이름 | 새 검사·판정 | 남는 검증 또는 조건 |
|---|---|---|---|
| 숫자 5 | `colors cols it lines pairs` | `test_numeric_metadata_describes_defaults_not_a_fixed_runtime_size`: 실제 값 256/80/8/24/65536, 초기 탭과 resize 후 질의. 색상 검사는 아래와 연결 | `cols/lines`는 시작 기본값이지 고정 창 크기가 아니다. 65536개 color pair 조합의 전수 픽셀 검사는 아님 |
| flag 13 | `OTbs am bce ccc km mc5i mir msgr npc xenl AX XF XT` | `test_boolean_metadata_and_cursor_movement_promises`, `test_background_erase_and_default_reset_follow_boolean_promises`: backspace, delayed wrap, insert/standout 중 커서 이동, UTF-8/Alt, padding 부재, erase 배경 및 default reset. `ccc/XF/XT`는 색상·focus·mouse·OSC 검사와 연결 | `mc5i`는 아래 제외 범위. flag 하나가 모든 xterm 확장을 허용한다는 뜻은 아님 |
| 편집 7 | `E3 dl dl1 ed il il1 rep` | `test_line_edits_repeat_and_history_erase_use_database_sequences`와 background erase 검사: 줄 삽입/삭제, 반복 수, 보이는 화면을 유지한 history 삭제 | 매우 큰 매개변수·wide cell과 모든 조합은 별도 |
| scroll 6 | `csr ind indn nel ri rin` | `test_region_scroll_entries_preserve_rows_outside_the_region`: 영역 밖 첫/끝 행 보존, NEL의 열 복귀 | 실호스트의 동시 resize·장시간 부하를 전수 대체하지 않음 |
| 좌우 margin 4 | `mgc smglp smglr smgrp` | `test_horizontal_margin_parameters_and_cancellation`: 좌/우/양쪽 설정, origin 안에서 wrap/scroll, margin 해제 | `smglp`/`smgrp`에서 생략한 반대쪽은 기본 경계로 설정된다. 두 명령을 이어 보내 기존 반대쪽이 보존된다고 가정하지 않음 |
| 색 5 | `initc oc op setab setaf` | `test_all_256_color_parameters_palette_change_and_reset`: 0–255 전체 인덱스의 fg/bg 매개변수, 팔레트 변경·복구, default 상속 | native RGB 결과를 검사. 호스트 색 깊이·테마·픽셀 검사는 별도 |
| 속성 15 | `blink bold dim invis rev ritm rmso rmul rmxx sgr sgr0 sitm smso smul smxx` | `test_basic_renditions_and_selective_resets_preserve_other_attributes`: 모든 광고 스타일, 선택 reset과 다른 속성 보존, 복합 SGR | 글자 깜박임 주기, dim/italic/strike의 실제 외관은 호스트·폰트 의존 |
| 커서 7 | `Cr Cs Se Ss civis cnorm cvvis` | `test_cursor_visibility_style_and_color_have_native_reset_semantics`: 가시성·blink, 6가지 style, cursor 색과 reset | 색 표현은 기존 호스트 지원/시작값 복원 gate를 유지. native 결과가 실제 WT cursor 색 지원을 뜻하지 않음 |
| ACS 3 | `acsc rmacs smacs` | `test_acs_map_and_ascii_reset_cover_the_advertised_glyphs`: 광고된 map의 모든 문자와 ASCII 복귀 | 폰트별 glyph 외관·폭은 실호스트 검증 필요 |
| 초기화·reset·화면 5 | `is2 rmcup rs1 rs2 smcup` | `test_initialization_reset_and_alternate_screen_use_real_compound_entries`: mode reset, 화면 지우기, primary 화면 복귀 | 이미 연결된 정상/예외/중지 복원 검사를 함께 사용. 호스트 제목 복원은 사용자 정책상 수행하지 않음 |
| mouse 3 | `kmous XM xm` | `test_mouse_focus_and_paste_pairs_have_matching_input_results`: XM mode와 정상 SGR press/release 결과, 비활성화. `xm` 차이는 아래에 별도 기록 | `XM/kmous`는 정상 연결. `xm`는 실험적 응답 설명의 차이이며 정상 mouse wire를 바꾸지 않음 |
| focus 4 | `fd fe kxIN kxOUT` | 같은 검사: 구독 on/off와 정확한 in/out 응답 | 물리 창 전환·호스트 focus 설정은 기존 실호스트 계획과 별도 |
| paste 4 | `BD BE PE PS` | 같은 검사: native bracketed paste on/off, Unicode payload와 정확한 경계 | OS clipboard·IME·모든 대용량 경계의 전수 검사는 아님 |
| clipboard 1 | `Ms` | `test_clipboard_capability_uses_the_existing_policy_broker`: 실제 문자열 매개변수 확장→기존 broker→정규화된 write 1회, 가짜 완료 응답 없음 | clipboard 정책과 호스트의 실제 수락/확인 능력은 별도. 이 검사에서 OS clipboard 결과를 주장하지 않음 |
| query·응답패턴 8 | `u6 u7 u8 u9 RV rv XR xr` | `test_cursor_device_queries_and_explicit_identity_profile_differences`: 실제 cursor/DA/DA2/version 응답, u6/u8의 형식. rv/xr 차이는 아래 | 응답을 묘사하는 `u6/u8/rv/xr`를 셸 출력 명령으로 실행하지 않음 |
| bell 2 | `bel flash` | `test_bell_and_visual_bell_have_distinct_native_effects`: BEL callback, flash 동안 reverse 속성·해제, `$<100/>`는 텍스트가 아닌 ncurses 지연 표기 | 실제 소리와 100ms flash 외관·스케줄링은 호스트 검증 필요 |
| legacy 7 | `mc0 mc4 mc5 meml memu rmm smm` | `test_excluded_legacy_entries_recover_without_claiming_peripheral_support`: 제어문 후 `NEXT` 출력과 정상 parser 상태 | printer·memory lock·8-bit meta 동작 지원으로 보고하지 않음 |

## 숨기지 않고 유지하는 프로파일 차이 11개

### 식별 응답 패턴 2개: rv, xr

실제 DA2 응답은 `CSI >1;0;0c`, XTVERSION은 `DCS >|ish-vt 2.0.0 ST`다. 설치된 xterm entry의 `rv`는 XTerm의 특정 숫자 형식을, `xr`는 `XTerm(...)` 문자열을 기대하므로 일치하지 않는다. 질의가 미구현되어 응답이 없는 상황과 구별한다.

이번에는 `TERM=xterm-256color` 호환 프로파일을 유지한다. 테스트를 통과시키려고 실제 제품 식별을 XTerm으로 바꾸지 않았고, 사용자 시스템 DB도 수정하지 않았다. 이 차이를 광고 수준에서 제거하려면 별도 이름의 terminfo를 배포하고 로컬·원격 실행 환경에서 그 DB의 존재를 보장해야 한다. 그 작업은 이번 범위에 추가하지 않았다.

### 실험적 mouse 응답 설명 1개: xm

이 환경의 `xm`을 ncurses로 확장하면 `ESC[<0;5;3;M`처럼 최종 문자 앞에 추가 `;`가 생긴다. native가 실제 프로그램에 전달하는 정상 SGR report는 `ESC[<0;5;3M`이다. 실험적 metadata에 맞추려고 올바른 mouse wire를 변경하지 않는다. `XM`의 mode 설정과 `kmous`의 prefix는 실제 native 결과와 일치한다.

ncurses는 `xm`을 실험적인 응답 설명으로 구분하고, 알려진 interpreter가 이 정보를 사용하지 않는다고 설명한다. [`user_caps(5)`의 mouse 설명](https://invisible-mirror.net/ncurses/man/user_caps.5.html).

### 기존 제외 범위 8개: mc5i와 legacy 7개

공유 xterm entry에는 printer flag `mc5i`, printer `mc0/mc4/mc5`, memory lock `meml/memu`, 8-bit meta `smm/rmm`가 존재한다. ish-vt의 기존 기본 명세에서는 이 기능을 지원으로 선언하지 않는다. 새 검사는 입력 이후 파서·텍스트가 정상적으로 진행되는지를 확인하며, 해당 장치 기능의 구현 완료로 집계하지 않는다. 이 항목을 광고에서 제거하는 것 역시 private terminfo 배포 문제와 연결된다.

## 결과를 해석하는 기준

- **이름 분류·조회:** 287/287. 기존 188개와 새 99개에 누락·중복 분류가 없다.
- **새 회귀:** 17개. 기존 terminfo 회귀 9개와 합쳐 26개가 통과했다. 여러 개별 capability·매개변수는 각 사례의 subtest/반복에서 대조한다. `test_advertised_inventory_has_no_unreviewed_entries`는 이 카탈로그의 독립 원본 값과 현재 DB를 비교해, DB 추가·삭제·값 변경을 검토 없이 통과시키지 않는다.
- **관련 회귀:** `test_terminal_terminfo`, `test_terminal_contracts`, `test_terminal_vt`, `test_terminal_native_features`의 77개 통과. 기록은 `.cache/vt-terminfo/focused-20261117.log`에 새로 저장했다. Ruff 검사와 포맷 검사도 통과했다.
- **명시적 차이·제외:** 11개. 통과 개수에 포함된 감사 검사가 이 기능들의 지원을 뜻하지 않는다.
- **실제 터미널 검증:** 본 문서의 native/DB 검사는 기존 실제 PTK·PTY·Windows Terminal 결과를 대체하지 않는다. 이번 전체 회귀·호스트 검증의 결과는 별도 완료 기록을 따른다.

수치와 응답 패턴도 terminal description의 일부이며, 단순 문자열 목록이나 구현해야 할 명령 목록과 동일하지 않다. [ncurses `terminfo(5)`](https://invisible-island.net/ncurses/man/terminfo.5.html), [XTerm control sequences](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html)를 구분 기준으로 사용했다.

재실행:

```sh
PYTHONPATH=src:tests python -m unittest test_terminal_contracts.TerminfoContracts test_terminal_terminfo -v
```
