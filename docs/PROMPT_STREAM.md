# 프롬프트 전용 UI와 출력 직접 전달 실험

브랜치: `experiment/prompt-stream`. 기존 `experiment/ghostty-ptk`와
`refactor/editor-request` 폴더·브랜치는 변경하지 않는다. `2a6c9d4`는
기존 terminal 실험의 미커밋 구현을 새 브랜치에 보존한 기준 커밋이다.
`ish`와 `ish-vt`는 기존 진입점이며 이번 실험은 `ish-stream`에만 적용한다.

## 실행과 시작 옵션

Linux/WSL의 대화형 터미널에서 ish가 설치된 Python 환경의 `ish-stream`을 실행한다.
저장소에서 실행할 때는 저장소 루트에서 `PYTHONPATH=src python -m ish.stream`으로
대체할 수 있다. 실행 환경은 프로젝트의 Python·의존성 요구사항을 충족해야 한다.

```sh
# 추가 UI·키 바인딩·셸 hook 없는 기본형
ish-stream
ish-stream --shell tcsh --cwd "$HOME"

# 대화형 세션을 시작하지 않는 조회
ish-stream --lang ko --help
ish-stream --version
ish-stream --shell bash --diagnose

# native 로그인 셸 / 명시적인 native 시작 인수
ish-stream --shell tcsh --login
ish-stream --shell bash --shell-arg=--noprofile --shell-arg=--norc --shell-arg=-i

# 선택적인 UTF-8 파일 로그
ish-stream --shell bash --home "$HOME/ish-stream" --log-file --log-level INFO
```

| 옵션 | 동작 |
| --- | --- |
| `--shell SHELL` | 실행 파일 이름 또는 경로를 선택한다. 생략하면 계정의 passwd 셸, `SHELL`, `sh` 순으로 선택한다. |
| `--cwd DIR` | 셸의 시작 디렉터리다. 생략하면 현재 디렉터리다. |
| `--login` | 기본 `-i` 대신 `-l` 하나를 전달한다. `--shell-arg`와 함께 쓸 수 없다. |
| `--shell-arg ARG` | 반복한 값을 순서대로 전달하며 기본 `-i`를 대체한다. `-`로 시작하는 값은 `--shell-arg=-f`처럼 쓴다. |
| `--home DIR` | ish 설정·언어 파일·기본 플러그인 library·기본 로그 경로의 기준이다. 셸의 `HOME`은 바꾸지 않는다. |
| `--lang CODE`, `-l CODE` | 기존 언어 로더를 사용한다. 영어 기본 문구, 제공되는 한국어 번역, 사용자 언어 파일을 사용한다. `-l`은 언어 옵션이며 로그인은 `--login`이다. |
| `--help`, `--version`, `--diagnose` | 플러그인이나 셸을 실행하지 않으며 설정·로그 파일을 만들지 않는 조회다. 대화형 TTY도 필요하지 않다. |
| `--log-file [PATH]` | 선택하면 UTF-8 append 파일 로그를 켠다. 경로를 생략하면 `config.LOG_FILE`, 기본적으로 `~/ish/.ish.log`를 사용한다. 옵션 자체를 생략하면 파일 로그는 없다. 일반 파일만 허용하고 터미널 장치·FIFO는 거부한다. |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` 중 선택한다. 기본값은 `WARNING`이다. |

로그는 진단용이다. 본체는 native 입력·출력 bytes를 기록하지 않는다. 파일 로그는
발생 시 기록하고, 콘솔 진단은 셸 시작 전 또는 터미널 복원 후에만 출력하여 실행 중
화면에 끼어들지 않는다. 진단 조회는 선택한 경로와 환경을 보여주지만 플러그인의 실행
가능성이나 셸 연동 지원 여부를 검증하지 않는다.

언어 선택은 ish의 도움말·진단·사용자 안내에 적용한다. 셸과 외부 프로그램의
출력 언어는 바꾸지 않으며, Python argparse의 일부 상세 오류·기술 로그·외부
플러그인의 자체 메시지는 영어로 남을 수 있다. 플러그인은 공통 `get_logger()`를
사용해야 이 로그 정책을 따른다. 플러그인의 직접 `print()`나 별도 logger까지
가로채지는 않는다. 로그 회전·보관 기간 정책은 이번 범위에 포함하지 않는다.

`ish-stream`은 기존 ish의 `.ishrc.py`를 실행하지 않는다. 기본형은 명시적인
플러그인 선택이 없으며 기존 셸의 rc 파일과 native 입력 경로를 사용한다.
로그인이나 사용자 지정 시작 인수가 선택된 UI provider의 지원 범위를 벗어나면,
원래 인수를 보존하여 **UI 없이 native 셸을 한 번만 시작**한다.

## 요청 상태와 검증 명령

팝업 요청의 identity와 상태 전이는 `stream/handoff.py`의 `Request` 한 곳에서
관리한다. 첫 `B`만 요청 번호를 확정하고, 후속 `B`로 같은 요청의 번호를 바꾸지
않는다. 정상 경로는 `waiting → popup → returning → resuming → complete`다.
`returning`은 팝업 포커스를 놓은 시점, `resuming`은 `done` 응답을 전송 큐에 넣은
시점이며, 다음 `P`로 native primary 복귀가 확인되어야 `complete`가 된다.
전송 큐 등록이나 `drain()` 반환만으로 셸 복귀를 판단하지 않는다.

거절·연결 종료·철회는 `cancelled`다. 완료와 취소는 서로 되돌아가지 않는다.
취소 결과가 확정되어도 보류한 native 입력은 셸 복귀 경계까지 기다릴 수 있다.
사용자가 Esc/Ctrl+C로 팝업 값을 취소하는 것은 별도 UI 결과이므로 정상
`done → P → complete` 경로를 사용할 수 있다. 새 wire handshake는 추가하지 않았다.

Python 이벤트·응답 값은 `protocol.Event/Reply`를 재사용하며, 외부 셸 스크립트의
wire 표현과 독립 테스트의 literal 기대값은 유지한다. 셸별 정책값을 공통 모듈에
모으지 않으며 진단의 기본 경로는 실제 리소스 로더에서 읽는다.

PR 자동 검사와 로컬 검사는 같은 명령을 사용한다.

```sh
uv sync --locked --no-default-groups --group dev --python "$(cat .python-version)"
uv run --no-sync python tools/check_stream.py
uv run --no-sync python tools/check_stream_package.py
```

Linux/WSL에서 Bash·zsh·tcsh·Vim이 필요하다. `--lint-only` 또는 `--tests-only`로
검사 단계를 나눌 수 있다. 필수 환경·모듈 누락, 빈 테스트, import 오류, skip은
실패로 처리한다. 새 `test_stream_*.py`는 자동 발견하고, 기존 공통 모듈 회귀도
함께 실행한다. 현재 유지 대상 stream 20개·공통 7개 모듈은 모두 필수다.
유지 대상 모듈을 추가할 때는 필수 목록과 독립 누락 검사도 함께 갱신한다.
검사 범위는 실행기 한 곳에 정의하며 `.gitattributes`는 Python의
LF 줄바꿈을 유지한다.

`ISH_TEST_BASH`, `ISH_TEST_ZSH`, `ISH_TEST_TCSH`로 선택한 실행 파일은 버전 보고,
adapter와 CLI 검사에 같은 선택기를 통해 적용한다. 상대 경로는 검사 시작 위치를
기준으로 고정한다. 패키지 검사는 잠금 의존성과 wheel을 별도 환경에 설치하고,
소스 경로 밖에서 진입점·한국어 조회·연동 리소스·PluginManager를 확인한다.
배포의 `source_sha256`도 셸 스크립트와 번역 리소스를 포함한다.

실제 호스트 검증은 Linux/WSL 터미널 안에서 다음 공통 실행기를 사용한다.

```sh
uv run --no-sync python tests/stream_matrix.py .cache/host-new-run --require-resize
```

Bash/zsh/tcsh의 기본형·샘플 플러그인·비동기 오류 복구·이전 factory 작업 실패
12개 조합을 순서대로 검사한다. 첫 `STREAM_NATIVE_READY`에서 창 크기를 바꾸고,
드래그를 끝낸 뒤 Enter를 누르면 이후 단계는 자동 진행한다. `--shell`과
`--scenario`로 범위를 제한할 수 있고, 재시도에는 반드시 새 증거 폴더를 사용한다.
이 실행기는 실제 파일·PTY·호스트 응답을 검사하며 픽셀·GUI 키보드 검사는 아니다.

`.github/workflows/stream.yml`은 PR에서 Ubuntu 22.04·24.04로 실행하고 읽기 권한만
사용한다. 기존 release workflow는 유지한다. 실제 Windows Terminal 검증은 이
CI 결과와 별개이며, CI 성공을 픽셀·GUI 입력 검증으로 해석하지 않는다.
runner와 설치 방식은 [GitHub 공식 문서](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)와
[uv 공식 문서](https://docs.astral.sh/uv/guides/integration/github/)를 참고했다.

## 샘플 UI 선택

제품 `--demo` 옵션은 제거했다. 하단 바·팝업 예제는 `examples/stream_plugins/`에
있고, 설치 패키지의 기본 제공 플러그인은 셸 provider다. 저장소 루트에서 아래처럼
셸 provider와 UI 예제를 같은 사용자 디렉터리에 모은 뒤 필요한 이름만 선택한다.
아래 복사 명령은 대상 디렉터리가 아직 없을 때 실행하는 예다.

```sh
cp -R src/ish/stream/bundled "$HOME/ish-stream-plugins"
cp -R examples/stream_plugins/stream_bar "$HOME/ish-stream-plugins/"
cp -R examples/stream_plugins/stream_popup "$HOME/ish-stream-plugins/"
ish-stream --shell tcsh --plugin-dir "$HOME/ish-stream-plugins" \
  --plugin stream_bar --plugin stream_popup --plugin stream_tcsh
```

Bash나 zsh에서는 각각 `--shell bash --plugin stream_bash`,
`--shell zsh --plugin stream_zsh`로 셸과 provider를 함께 바꾼다.
primary 프롬프트에서만 하단 패널을 표시하며 F2로 패널 안에 팝업을 연다.
Enter로 값을 반환하거나 Esc/Ctrl+C/Ctrl+D로 닫는다. 팝업 결과는 셸에 삽입하거나
실행하지 않는다. 임의 위치 overlay와 완성 결과를 native 셸 버퍼에 적용하는 API는 없다.

## 외부 연동 스크립트 수정

하단 바·팝업 내용과 셸별 Python 준비 코드는 이제 외부 Python 플러그인이다.
기존 `PluginManager`로 명시적으로 선택한 모듈만 로드한다.
`--plugin NAME`으로 개별 선택하고 `--plugin-dir`로 수정한 원본 디렉터리를
지정한다. 선택하지 않으면 로더·UI·셸 hook이 모두 비활성이다.
사용법과 API, 의존성 설치 보강은 [Python 플러그인](STREAM_PLUGINS.md)을 따른다.

셸별 hook·widget·키 정책은 `src/ish/stream/integrations/`의 텍스트 파일에 있다.
셸 Python 플러그인은 시작 경로와 런타임 값의 인용을 준비한다. wheel에도 원문 파일로
포함되며 별도로 컴파일하지 않는다. 기본형은 이 디렉터리를 읽지 않는다.

설치본을 직접 수정하는 대신 디렉터리를 복사하여 선택할 수 있다.

```sh
cp -R src/ish/stream/integrations /원하는/경로/ish-integrations
ish-stream --shell bash --plugin-dir "$HOME/ish-stream-plugins" \
  --plugin stream_bar --plugin stream_popup --plugin stream_bash \
  --integration-dir /원하는/경로/ish-integrations
```

선택한 디렉터리는 전체 연동 리소스의 대체 위치다. 빠진 파일을 설치본에서
섞어 가져오지 않는다. 변경은 **새 세션부터** 적용되며 실행 중인 셸에 다시
source하지 않는다. 파일 첫 줄의 `# ish-stream-api: 1`과 `@NAME@` 런타임
치환 규칙은 [연동 파일 설명](../src/ish/stream/integrations/README.md)을 따른다.
삽입된 경로 안의 토큰처럼 보이는 문자는 다시 치환하지 않는다.

파일 누락·읽기 실패·계약 버전 불일치·알 수 없는 치환 항목은 셸을 실행하기
전에 발견하여 안내 후 **UI 없는 기본형으로 한 번만 시작**한다. 연동이 없는
셸 이름도 같은 경로를 사용한다. UI 플러그인을 선택하지 않으면 잘못된 연동 경로도 읽지
않는다. 사용자가 편집한 스크립트가 이미 실행된 뒤의 임의 문법/동작 오류를
자동으로 되돌리는 기능은 아니다. 이때는 `--plugin` 없이 연동을 건너뛸 수 있다.

기존 셸의 hook과 정책 변경에는 본체 재빌드가 필요 없다. 기존 공통 통신 계약을
구현하는 새 셸은 외부 Python provider로 등록할 수 있다. 공통 통신 계약 자체를
바꾸는 변경은 본체와 플러그인의 API 버전 및 검증을 함께 갱신해야 한다.

## 책임과 재사용

- 기존 `terminal.Session`이 controlling PTY의 생성·크기·종료·쓰기 큐를
  계속 관리한다. 명시적인 환경 전달과 출력 backpressure를 재사용한다.
  기존 ish-vt는 기본 환경 정책을 그대로 사용한다.
- Session의 선택적 `child_exited` 알림은 실제 자식 종료 관찰이며, 기존 `exited`
  알림은 마지막 PTY 출력 전달까지 마친 완료다. 자식이 끝나면 미전송 입력을
  폐기하고 App은 입력과 팝업을 해제한다. 호스트 출력 보류는 유지하며, 재개되면
  남은 출력을 전달한 뒤 완료한다. 기존 최종 읽기 예산 256 KiB를 보류·재개에
  걸쳐 유지하고 EOF 또는 현재 읽을 데이터가 없는 경계에서도 마친다. 이는
  백그라운드 프로세스의 모든 미래 출력이나 slave EOF를 무기한 기다리는 계약이 아니다.
- 기존 `FDWriter`가 외부 출력도 순서대로 전송한다. 셸 바이트와 PTK
  프레임은 이 writer 하나에서만 합쳐진다.
- 기본형은 parser를 거치지 않고 셸 출력을 그대로 전달한다. TERM과
  호스트 환경도 상속한다. 이미지·클립보드·질의 응답·스크롤백은 호스트가 처리한다.
- UI 경로의 `Stream`은 private OSC와 제어문 경계만 관찰한다. 알 수 없는
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

F2는 native 셸로 전달한다. 그 뒤 들어온 입력은 잠시 보류한다. 실제 셸
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

## 셸별 연동

기본형은 셸 adapter를 설치하지 않는다. Linux/WSL의 controlling PTY에서
`-i`로 실행할 수 있는 셸을 폭넓게 사용할 수 있는 구조지만, 모든 셸·OS의
호환성을 검증한 것은 아니다. 선택한 UI에는 다음 셸별 협력이 필요하다.

| 셸 | primary/해제 경계 | 팝업 경계 | 검증 버전 |
|---|---|---|---|
| tcsh | 기존 precmd/postcmd 알림 | native bindkey 명령 + helper | 6.24.13 |
| Bash | PS1 끝의 상태 알림, PS2/PS0 산술 확장 및 PROMPT_COMMAND hook | native bind-x + helper | 5.1.16 · 5.2.37 · 5.3.9 |
| zsh | ZLE line-init/line-finish와 CONTEXT | native ZLE widget + helper | 5.9 |

모두 같은 P/H/B, nonce/serial, 팝업 socket 계약을 사용한다. Application에
셸 이름별 입력 분기를 추가하지 않았다. 기존 InputRouter 계약은 변경하지 않았고,
Session에는 위의 선택적 자식 종료 알림을 추가했다. 화면 처리에 추가한 공통 동작은 ED0/ED2의 명시적 화면 지우기 뒤 빈
하단 영역을 재사용하는 것과 화면을 바꾸지 않는 cursor/keypad 입력 모드의
통과뿐이다. 이는 화면 배치 근거이며 입력 readiness 이벤트가 아니다.

Bash UI 연동은 5.1 이상의 기능, `promptvars`, native Readline을 사용한다.
5.3의 `${ command; }`에는 의존하지 않는다. 사용자 PS1 확장 뒤에 P를 표시하고
PS2/PS0에서는 산술 확장으로 상태·serial을 갱신하며 H를 표시한다.
PROMPT_COMMAND 앞에는 H hook, 뒤에는 다음 primary 준비 hook을 넣는다.
PROMPT_COMMAND의 선언 자체는 바뀌지만 scalar/배열 형태,
기존 요소 순서와 `$?`를 보존하며 DEBUG trap을 교체하지 않는다. readonly,
nameref, 연관 배열 PROMPT_COMMAND나 필수 hook 제거는 UI 비활성화로 처리한다.
사용자 bind-x는 H/원래 명령/P로 감싸고, completion·검색·quoted-insert 등은
native 동작 전에 UI를 해제한 뒤 다음 확인된 primary까지 숨긴다. 일반 Enter는
native accept-line을 유지하여 줄바꿈 없는 출력과 PS0를 지우지 않는다.
emacs/vi 팝업은 검증했지만 임의 submap, prefix-only Escape와 추가 Readline
함수 전체의 호환은 미검증이다.

Bash의 F2는 팝업을 선택한 세션의 지원 Readline 키맵에서 전용으로 예약한다.
primary에서만 팝업을 열고 continuation·heredoc·`read -e`에서는 키를 소비하며
팝업을 열지 않는다. 해당 문맥의 기존 F2 사용자 바인딩은 복원하지 않는다.
외부 프로그램에는 원래 F2 bytes를 전달한다. 부모가 F2를 바로 가로채지 않고
native callback과 helper를 기다리는 기존 동기화는 유지한다. tcsh/zsh의 기존
F2 정책과 기본형의 사용자 키 바인딩은 변경하지 않는다.

zsh는 사용자 ZLE init/finish를 이어 호출하고 preexec/precmd, PS1/PS2,
TRAPINT를 교체하지 않는다. 사용자·completion widget과 추가 키를 읽는
native widget의 실행 중에는 UI를 해제한다. 중첩 widget, 동적 widget 등록,
사용자 hook 진입 상태와 ERR_RETURN를 검증했다. `zle -F` 비동기 callback,
특수 `zle-*` hook의 임의 stdin, 직접 `.builtin`에 연결한 사용자 키는
미검증이다. 모든 사용자 Readline/ZLE 플러그인의 호환을 선언하지 않는다.

## 표시 범위와 의도적인 제약

- UI provider는 표에 기재한 셸의 non-login interactive 실행을 대상으로 한다. 로그인 셸,
  `-f`/`-c` 등 별도 startup 옵션에서는 UI 없이 원래 인수로 실행한다. 기존 ish의
  Bash/zsh semantic submission 경로는 변경하지 않았다.
- primary에서만 하단 5줄을 확보한다. 처음 표시하거나 화면 배치를 보존할 수
  없는 제어문 뒤에는 호스트를 5행 스크롤하여 빈 영역을 만든다. 이후 비운
  하단 영역과 선형 출력이 유지되면 현재 커서에서 5행 내려갔다 올라온다.
  호스트가 화면에 맞지 않는 행만 스크롤하므로 빈 Enter·짧은 출력·팝업
  복귀마다 5행을 다시 밀지 않는다. 커서 위치 질의나 응답 대기는 추가하지 않는다.
  화면 내용이나 이미지 위치를 역파싱하지 않으며 기본형에는 이 동작이 없다.
- continuation·heredoc·프로그램 실행 중에는 장식을 숨긴다. 편집은 계속
  셸에 맡긴다. PTY 크기는 항상 호스트 전체 크기를 유지하고, UI 표시 중에만
  호스트의 스크롤 영역을 줄인다. 표시/해제마다 SIGWINCH를 발생시키지 않는다.
- resize는 현재 UI를 해제하고 다음 확인된 primary에서 다시 표시한다.
  최소 20열·8행보다 작은 화면에서는 UI를 켜지 않는다.
- 전체 지우기·화면 버퍼/마진 변경·좌표 지정 등 패널을 침범할 수 있는
  제어문에서는 보수적으로 장식을 해제한다. 일반 background 출력은
  native 영역으로 흐르며 팝업 동안에는 출력이 대기할 수 있다.
- F2는 팝업을 선택한 경우에만 예약된다. 각 primary에서 binding을 재설치한다.
  기본형과 하단 바만 선택한 경우에는 기존 F2 동작을 바꾸지 않는다. 셸의 사용자
  hook과 종료 상태는 검증된 범위에서 보존한다.
- tcsh 사용자 rc를 읽기 전에 HOME/home을 복원하지만, 먼저 실행되는
  `/etc/csh.cshrc`는 임시 HOME을 볼 수 있다. 실행 중 precmd 자체를 교체하면
  새 P가 없어져 장식이 비활성화된다.
- zsh는 private ZDOTDIR로 시작하여 사용자 .zshenv/.zshrc와 사용자 변경
  ZDOTDIR를 복원한다. 시스템 zshenv/zshrc는 임시 ZDOTDIR를 볼 수 있다.
  시스템 startup의 ZDOTDIR 변경이나 RCS 해제, startup 중 exec는 미지원이다.
  사용자 .zshenv에서 RCS를 끄면 private .zshrc의 설치·복원까지 건너뛸 수
  있으므로 해당 구성도 UI 지원 범위에서 제외한다.
- tcsh에서 stdin을 직접 읽거나 exec하는 사용자 prompt hook은 미검증이다. F2
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

Bash의 Ctrl+R 검색 도중 Ctrl+C와 후속 입력이 빠르게 이어지면 native Readline의
신호 처리 경합으로 정지하거나 종료될 수 있다. ish 없는 Bash 5.3.9에서도
재현했으며 기본형/플러그인 UI 모두 영향을 받을 수 있다. 현재 설치된 셸에는 수정이
적용되지 않았다. 원인과 별도 테스트 빌드 대조는
[Ctrl+C 조사](INTERRUPT_INVESTIGATION.md)를 참고한다.

## 이전 실험 기록 읽기

이전 검증 문서·로그에 나오는 제품 `--demo`는 당시의 편의 옵션이다. 현재는 위의
명시적 플러그인 선택으로 대체했다. `tests/stream_host.py --demo`와 테스트 fixture의
`demo=True`는 테스트 내부 편의 이름이며 제품 CLI 옵션이 아니다. 테스트도 실제로는
샘플과 셸 provider를 합성한 디렉터리에서 `--plugin` 목록을 전달한다.

초기 streaming 전용 `.venv`는 Python 3.12.14, PTK 3.0.53, wcwidth 0.8.3을 재사용했고,
당시 전체 프로젝트의 `uv sync --offline`는 Pillow 13.0.0 캐시 부재로 실패했다.
이 내용은 초기 환경 구축 기록이며 현재 설치 절차의 성공·실패를 뜻하지 않는다.
streaming 경로는 libghostty·Pillow를 import하지 않는다.
