# 직접 전달 실험 검증

이 문서는 `ish.stream`의 관찰 테스트와 실제 호스트 화면 검증을 구별한다.
셸의 프롬프트 문자열과 deadline은 테스트의 관찰·실패 판정에만 사용한다.
production readiness를 화면 문자열이나 무출력 시간으로 추정하지 않는다.

검증 환경은 WSL2 Ubuntu, tcsh 6.24.13, Vim 9.1, Python 3.12.14,
prompt-toolkit 3.0.53이다. Windows Terminal의 설치 package 버전은
1.24.12741.0이며 실행 파일의 ProductVersion은 1.24.261001001이다.

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

예시 실행:

```sh
PYTHONPATH=src:tests python -m unittest -v test_stream_cli
```

`tests/stream_fixture.py`의 프로그램들은 ish 구현을 import하지 않는다. 파일 결과와
고정 출력 bytes가 기대값이며, ish 내부 검증 함수를 기대값 생성에 사용하지 않는다.

CLI 회귀 항목은 12개를 확인했다. 마지막 popup 입력 보존 변경 뒤에는 관련 3개
항목을 다시 실행해 통과했고, 입력 역압 연결 뒤에는 6 MiB 검증 1개를 추가해
통과했다. 이 suite의 `pyte`는 테스트 전용 의존성이며 기본
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
6. 실제 호스트 창 resize 후 primary 복귀

`--demo`가 없으면 popup 단계가 빠지므로 번호는 하나씩 줄어든다. resize 단계에서는
실제 창 크기를 바꾼 뒤 gate를 진행한다. `host-size.json`에 실제 SIGWINCH 경로로
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

## 현재 Windows Terminal 관찰 결과

`.cache/host-plain`의 기본형과 `.cache/host-demo-2`의 PTK UI 활성 실행 모두 실제
Windows Terminal/WSL에서 foreach, heredoc, Vim의 파일 결과와 정상 종료 상태 0을
확인했다. 데모는 native popup 요청으로 PTK를 열고 Unicode 텍스트를 입력한 뒤
native primary로 복귀했다. 캡처된 출력에서 하단 바·팝업·Unicode 필드가 호스트로
전달된 사실과 private 제어 마커가 노출되지 않은 사실을 확인했다.

두 실행에서 native 프로그램의 CPR은 `[15, 1]`, DSR은 정상 응답, DECRQSS의
스크롤 영역은 `[1, 30]`이었다. 프로그램에 전달된 PTY 크기는 호스트와 같은
`120 × 30`이었다. 이는 UI를 숨긴 실행 경로에서 전체 스크롤 영역과 크기가
복구된 사실을 뒷받침하며, 이미지의 픽셀 외관까지 증명하지 않는다.

현재 제공된 화면 도구가 이 Windows Terminal 창을 노출하지 않아 다음 항목은
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

## 검증 범위의 한계

- byte-for-byte SIXEL 전달 성공과 호스트의 실제 이미지 표시 성공은 별개다.
- 프롬프트 대기 중인 것처럼 보이는 화면만으로 popup 입력 소유권을 증명하지 않는다.
- Enter 직후 선입력 테스트 하나로 임의로 지연된 P/H 이벤트와 모든 사용자 key binding을
  검증했다고 주장하지 않는다. native 협력 프로토콜을 바꿀 때 해당 경합 테스트를 추가한다.
- 테스트에서 확인한 tcsh 외의 셸이나 다른 Windows Terminal 버전은 자동으로 지원 범위에
  포함하지 않는다.
