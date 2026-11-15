# 프롬프트 전용 UI와 출력 직접 전달 실험

브랜치: `experiment/prompt-stream`. 기존 `experiment/ghostty-ptk`와
`refactor/editor-request` 폴더·브랜치는 변경하지 않는다. `2a6c9d4`는
기존 terminal 실험의 미커밋 구현을 새 브랜치에 보존한 기준 커밋이다.
`ish`와 `ish-vt`는 기존 진입점이며 이번 실험은 `ish-stream`에만 적용한다.

## 실행

WSL에서 새 worktree로 이동한다.

```sh
cd /mnt/c/Users/user/.codex/worktrees/ish-prompt-stream/ish
PYTHONPATH=src .venv/bin/python -m ish.stream --shell tcsh
PYTHONPATH=src .venv/bin/python -m ish.stream --shell tcsh --demo
```

첫 명령은 추가 UI·키 바인딩·셸 hook이 없는 기본형이다. 두 번째 명령은
명시적인 데모 플러그인으로 primary 프롬프트에서 하단 패널을 표시한다.
F2로 패널 안에 팝업을 열고 Enter로 값을 반환하거나 Esc/Ctrl+C/Ctrl+D로
닫는다. 팝업 결과는 셸에 삽입하거나 실행하지 않는다. 임의 위치 overlay와
완성 결과를 셸 버퍼에 적용하는 API는 이번 실험에 포함하지 않는다.

새 `.venv`는 기존의 검증된 Python 3.12.14, PTK 3.0.53, wcwidth 0.8.3을
재사용한 streaming 전용 실행 환경이다. 기존 환경은 수정하지 않았다.
전체 프로젝트의 `uv sync --offline`는 Pillow 13.0.0 캐시 부재로 실패했다.
streaming은 libghostty·Pillow를 import하지 않으므로 이 경로 실행에는 필요하지 않다.
배포 패키지의 console script 선언은 pyproject.toml에 포함되어 있다.

## 책임과 재사용

- 기존 `terminal.Session`이 controlling PTY의 생성·크기·종료·쓰기 큐를
  계속 관리한다. 명시적인 환경 전달과 출력 backpressure만 추가했다.
  기존 ish-vt는 기본 환경 정책을 그대로 사용한다.
- 기존 `FDWriter`가 외부 출력도 순서대로 전송한다. 셸 바이트와 PTK
  프레임은 이 writer 하나에서만 합쳐진다.
- 기본형은 parser를 거치지 않고 셸 출력을 그대로 전달한다. TERM과
  호스트 환경도 상속한다. 이미지·클립보드·질의 응답·스크롤백은 호스트가 처리한다.
- 데모의 `Stream`은 private OSC와 제어문 경계만 관찰한다. 알 수 없는
  이미지 문자열은 크기 제한 없이 점진적으로 전달하며 기능을 번역하지 않는다.
- PTK Application은 실제 키 파서·TextArea·layout·renderer를 사용하지만
  외부 터미널을 직접 읽거나 쓰지 않는다. 예약된 패널의 셀만 프레임으로 반환한다.
  셸 화면을 libghostty로 다시 그리지 않는다.

## UI 계약

셸 연동은 `ESC ] 777;ish;<session nonce>;<serial>;<event> BEL`을
controlling TTY로 보낸다. source의 stdout 리다이렉션에 이 신호를 넣지 않는다.
serial은 세션 안에서 증가하며 오래되거나 중복된 알림은 UI를 다시 켜지 못한다.

| 알림 | 의미 |
|---|---|
| P | primary 장식 표시가 가능한 지점. 일반 stdin이나 PTK 명령 편집 권한이 아니다. |
| H | 현재 장식을 해제한다. postcmd를 command-start/readiness로 해석하지 않는다. |
| B | adapter가 설치한 native F2 binding의 실행 경계. 이것만으로 팝업 입력을 소유하지 않는다. |

F2는 native tcsh로 전달한다. 그 뒤 들어온 입력은 잠시 보류한다. 실제 셸
편집기가 binding을 실행해 B를 보낸 뒤 helper가 private Unix socket에서
같은 nonce/serial로 요청해야 PTK 팝업을 연다. 다른 실행의 H가 먼저 오면
F2 뒤 입력은 native 소비자에게 그대로 전달한다. 따라서 늦게 처리한 P만으로
실행 중 프로그램의 F2를 팝업이 가져가지 않는다.

팝업이 열려 있는 동안 helper는 socket 응답을 기다리며 명령 stdin을 읽지
않는다. 부모는 PTY output read를 중단해 kernel backpressure를 적용한다.
팝업이 닫혀 ACK가 전송되어도 다음 P까지 새 셸 입력은 보류한다. 취소·중복·
오래된 요청과 연결 종료는 새 팝업을 해제하지 못한다. 이 소켓은 팝업 동기화만
담당한다. 명령·continuation·heredoc·프로그램 stdin을 보내는 통로는 PTY 하나다.
FIFO나 Python command dispatcher를 추가하지 않았다.

## 표시 범위와 의도적인 제약

- 데모는 현재 tcsh 6.24.13의 non-login interactive 실행만 검증했다.
  Bash/zsh의 기존 ish 경로를 수정하지 않았으며 새 데모 지원을 선언하지 않는다.
- primary에서만 하단 5줄을 확보한다. 호스트에서 정상 스크롤하여 빈 줄을
  만든 뒤 그 영역만 그리므로 기존 출력·이미지 위에 팝업을 덧칠하지 않는다.
  이 때문에 프롬프트 진입 때 화면이 스크롤될 수 있다. 기본형에는 이 동작이 없다.
- continuation·heredoc·프로그램 실행 중에는 장식을 숨긴다. 편집은 계속
  셸에 맡긴다. UI 해제 시 PTY 크기를 호스트 전체 크기로 복원한다.
- resize는 현재 UI를 해제하고 다음 확인된 primary에서 다시 표시한다.
  최소 20열·8행보다 작은 화면에서는 UI를 켜지 않는다.
- 전체 지우기·화면 버퍼/마진 변경·좌표 지정 등 패널을 침범할 수 있는
  제어문에서는 보수적으로 장식을 해제한다. 일반 background 출력은
  native 영역으로 흐르며 팝업 동안에는 출력이 대기할 수 있다.
- F2는 데모에 예약된다. 각 primary에서 binding을 재설치한다. 기본형은
  기존 F2 동작을 바꾸지 않는다. 셸의 사용자 hook과 종료 상태는 검증된 범위에서 보존한다.
- tcsh 사용자 rc를 읽기 전에 HOME/home을 복원하지만, 먼저 실행되는
  `/etc/csh.cshrc`는 임시 HOME을 볼 수 있다. 실행 중 precmd 자체를 교체하면
  새 P가 없어져 장식이 비활성화된다.
- stdin을 직접 읽거나 exec하는 사용자 prompt hook은 미검증이다. F2
  handoff 중 입력을 요구하는 postcmd는 대기할 수 있어 이 구성은 지원 범위에
  포함하지 않는다. handoff 대기는 Ctrl+C로 취소할 수 있다.
- 팝업은 호스트의 alternate screen·마우스 모드를 켜지 않는다. 자체 cursor
  save/restore 슬롯을 잠시 사용하므로 prompt를 가로질러 해당 슬롯을 유지하는
  사용자 제어문과의 호환은 보장하지 않는다.
- raw passthrough도 호스트가 지원하지 않는 프로토콜을 지원하게 하지는 않는다.
  SIXEL의 원문 전달과 실제 이미지 외관 검증은 구분한다.

## 한도와 실패 처리

입력·출력 writer는 기존 FDWriter의 4 MiB queue와 2 MiB backpressure 기준을
재사용한다. 셸이 입력을 읽지 않으면 호스트 input read를, 호스트가 출력을
받지 못하면 PTY output read를 중단했다가 queue가 줄어들면 재개한다.
PTY read는 64 KiB씩 수행한다. 팝업 요청 대기 입력이 64 KiB에
도달하면 외부 입력 read를 멈춰 무제한 버퍼를 만들지 않는다. private 제어
요청은 1 KiB, private OSC 후보는 256 bytes, 짧은 escape 후보는 128 bytes로
제한한다. 이 제한을 이미지 payload나 일반 셸 출력 크기 제한으로 사용하지 않는다.
50 ms의 Escape 구분 지연은 F2와 단독 Escape의 키 인코딩 모호성만 해결하며
셸 readiness를 추정하지 않는다. 테스트 deadline은 실패 판정에만 사용한다.

실제 검증 결과와 호스트 화면을 확인하지 못한 범위는
[STREAM_VALIDATION.md](STREAM_VALIDATION.md)에 기록한다.
