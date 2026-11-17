# ish 작업 규칙

## 정식 작업 위치

- 정식 개발 저장소는 `D:/Developments/ish`이며 WSL 경로는 `/mnt/d/Developments/ish`다. 독립적인 `.git`을 사용하고 현재 작업 브랜치는 `codex/hookless-vt`다. 이후 코드 변경과 검증은 이 위치에서 진행한다.
- 이전 `C:/Users/user/.codex/worktrees/ish-hookless-vt/ish`는 백업과 과거 검증 기록으로 보존한다. 기존 main·stream·다른 실험 worktree는 수정하지 않는다. 아래 과거 worktree 설명보다 이 작업 위치 규칙이 우선한다.
- 이동 자체로 실행 진입점이나 릴리스 상태가 바뀌지는 않는다. `ish-vt`는 VT 경로, `ish`는 기존 stream 경로를 유지한다. 정식 작업 위치와 검증 범위는 [개발 안내](docs/DEVELOPMENT.md)를 따른다.

## VT 구현 범위

- 호스트 기능 정책은 `terminal.features`가 소유한다. 기능마다 on/off를 제공하고 실제 지원 질의를 구현한 항목만 auto를 허용한다. 사용자 정책·관찰한 지원 응답·실제 선택 profile·이미 변경한 상태를 구분한다. on으로 관찰 결과를 위조하지 않고 크기·복원 필수 조건을 유지한다. 호스트 이름은 일반 기능 허용 근거로 사용하지 않으며 검증된 개별 차이 보정만 hosts 경계에 둔다.
- `codex/hookless-vt`에서 기존 `ish-vt`를 재사용한다. `experiment/prompt-stream`, `experiment/ghostty-ptk`, `refactor/editor-request`, 기존 main의 브랜치·폴더·환경을 변경하지 않는다.
- 이번 사용자 요청이 아래 stream 전용 정책보다 우선한다. `ish`의 기존 stream 진입점은 보존하고, 별도 `ish-vt`에서 libghostty-vt + PTK 화면 합성을 검증한다. tmux나 Zellij를 실행 의존성으로 추가하지 않는다.
- 기본형에는 추가 UI·UI 전용 단축키·셸 hook이 없다. 명시적으로 선택한 외부 Python 플러그인만 하단 행과 전역 팝업 단축키를 등록한다. 내부 history의 Shift+PageUp/Down과 UI 단축키는 구분한다. 제품 `--demo` 옵션은 추가하지 않는다.
- 마우스 정책은 off/auto/on이며 기본은 auto다. off는 프로그램·플러그인 요청보다 우선하여 호스트 선택을 유지한다. auto는 native mouse mode 또는 명시적인 클릭 UI 요청에서만 capture한다. keyboard-only popup은 capture를 요청하지 않는다. tracking과 1016 좌표 형식을 구분하고 모드 전환·resize·종료 시 단일 소유자에서 정리한다.
- 프로토콜 보완 모듈은 기존 PluginManager와 ProtocolRegistry를 사용한다. 세션 첫 입력 전에 담당자를 고정하고 selector 충돌·native 선점을 거부한다. 실패한 입력을 다른 해석기로 재전달하지 않는다. 공개 등록은 검증된 미지원 OSC numeric namespace부터 제공하며 APC/DCS/CSI 전체 지원으로 확대 해석하지 않는다.
- 외부 출력·키·클립보드 보완 모듈은 모두 기존 PluginManager의 단일 plugin_dir와 config.vt.plugins로 관리한다. 기능별 검색 경로나 별도 로더를 추가하지 않는다. plugin_lib_dir는 기존 공통 의존성 경로이며 모듈 검색 경로가 아니다. Python 모듈 변경은 새 세션부터 적용하고 실행 중 담당자를 교체하지 않는다.
- 출력 OSC 등록과 호스트 키 등록은 별도 계약이다. 키 확장은 완성된 미소유 시퀀스를 기존 공통 키로 변환하는 제한된 등록만 제공한다. 본체가 제어문 분리·응답 예약·협상·포커스·PTY 쓰기를 소유하며 기존 키·응답·붙여넣기 영역 선점과 등록 충돌을 거부한다. 외부 모듈에 writer나 임의 원문 입력 재전달 API를 제공하지 않는다.
- 지원하는 문자·키·붙여넣기의 내용·순서·출처를 보존한다. 터미널 응답과 구별할 수 없는 신호 및 미지원 제어 시퀀스를 사용자 키로 추측하지 않으며, 시간 경과나 화면 문자열로 모호성을 해소하지 않는다. 단축키 미등록과 입력 형식 미지원을 구분하고 제한되는 형식을 진단·문서·독립 회귀에 명시한다.
- 프로그램 클립보드 접근의 기본값은 write이며 read-write는 명시적인 설정으로 켠다. ClipboardBroker가 허용 정책·요청 identity·취소·응답을 소유하고 보완 provider는 실제 OS/X11 전송과 자원 수명을 담당한다. 명시한 대상이 실패하면 다른 데스크톱이나 내부 캐시로 대체하지 않는다. 호스트 프로토콜 지원과 로컬 provider 가용성을 같은 capability로 위장하지 않으며 입력·클립보드 내용은 일반 로그에 남기지 않는다.
- libghostty가 화면·커서·history를 계속 소유한다. 보완 모듈은 완전한 native 입력과 순서가 있는 PTY 응답만 반환하며 호스트 writer를 갖지 않는다. 새 native 지원은 동일 계약 테스트를 통과한 후 보완 모듈을 대체한다. clipboard 정책·호스트 표현 등 ish의 책임은 유지한다.
- 공통 .ishrc.py의 config.vt 사전만 VT 플러그인·마우스·history 정책을 선택한다. config.plugins는 stream 전용으로 유지한다. 조회 옵션은 rc·plugin·native engine을 실행하거나 파일을 생성하지 않는다. native 로그 원문은 저장하지 않고 severity·프로토콜 실패 횟수만 진단한다.
- 셸·프로그램 출력은 libghostty-vt의 화면 상태에만 반영하고 하나의 PTK renderer가 셸 화면과 플러그인 UI를 합성한다. 화면·스크롤백·선택 상태는 libghostty-vt, 포커스는 Application, PTY 수명과 쓰기 큐는 Session이 소유한다.
- ish-vt가 변경한 터미널 상태는 관찰한 시작값으로 복원한다. 제목은 명시적인 예외로, 자식의 제목 변경을 기존 writer로 전달하고 종료·중지 때 원래 제목으로 되돌리지 않는다. 제목 조회나 title stack을 변경 허용 조건으로 요구하지 않는다.
- SIGKILL·호스트 소멸 등 예외적인 강제 종료에서의 완전 복원은 추가 구현 목표에서 제외한다. 기존 정상 종료·처리 가능한 오류·중지 시 정리는 유지한다. 본체 터미널이 먼저 소비한 단축키는 ish의 입력 지원 결함으로 보고하지 않는다.
- 셸 rc, PROMPT_COMMAND, precmd, ZLE 및 프롬프트 문자열을 ish가 변경하거나 상태 감지에 사용하지 않는다. 명령줄·continuation·heredoc은 native 셸, 실행 중 프로그램의 stdin은 기존 PTY가 담당한다.
- 전용 팝업 키는 셸·프로그램 어디에서든 ish가 소비한다. 팝업 이후 입력만 팝업에 전달한다. 이전에 PTY에 보낸 입력의 회수나 실행 중 프로세스 정지를 보장하지 않는다. 팝업 결과를 자동으로 셸 명령으로 실행하지 않는다.
- 기존 PluginManager가 Python 로딩·의존성을 담당한다. VT 표면 등록 API와 공통 설정·번역·로그를 재사용하며 별도 Python 모듈 로더를 만들지 않는다.
- `tools/check_terminal.py`로 필수 회귀의 환경·모듈 누락·빈 테스트·skip을 실패로 처리한다. 실제 Windows Terminal/WSL 검증은 자동 PTY 회귀 및 선택적 Kitty GUI 테스트와 구분한다. 이번 범위는 Bash/zsh/tcsh, 기본형/플러그인, heredoc, Vim, resize이며 검증하지 않은 호스트·이미지 기능의 완전 지원을 선언하지 않는다.

## 범위와 목표

- 이 브랜치는 experiment/prompt-stream에서 native 셸 출력 직접 전달과 primary 프롬프트 전용 PTK UI를 실험한다. 기존 experiment/ghostty-ptk 폴더와 브랜치를 수정하지 않는다.
- 제품명은 ish, 버전은 2.0.0이며 ish 진입점은 native stream 경로를 실행한다. 이전 버전 호환 계층·설정 변환·legacy 실행기는 추가하지 않는다. 기본형은 UI/단축키/셸 hook 없이 직접 전달하고, 명시적으로 선택한 Python 플러그인에서만 primary 장식과 제한된 PTK 팝업을 켠다. 제품 --demo 옵션은 없으며 샘플 하단 바·팝업은 examples/stream_plugins에 둔다. 테스트의 demo 편의 인자는 명시적인 플러그인 선택으로 변환하고 제품 경로에 추가하지 않는다. continuation과 프로그램 stdin은 항상 셸에 맡긴다.
- ish의 셸 동작과 셸별 정책은 수정 가능한 integrations 외부 텍스트에 둔다. 기본형은 연동 파일을 읽지 않으며, UI 준비 중 파일 누락·계약 불일치는 셸 실행 전에 UI 없는 경로로 전환한다. 변경은 새 세션에 적용한다. Bash 팝업의 F2는 지원하는 Readline 키맵에서 전용으로 예약하며 continuation/read-e에서는 팝업 없이 소비한다. 외부 프로그램 입력은 그대로 전달하고 하단 바만 선택한 세션의 F2도 변경하지 않는다.
- 명령줄과 continuation 편집은 셸이 담당하고, PTK는 화면 합성과 팝업 포커스를 담당한다.
- 기본형은 전체 셸 화면만 표시한다. 하단 바·우측 프롬프트·팝업과 해당 UI 전용 단축키를 기본으로 추가하지 않고 향후 플러그인으로 제공한다. 기본 전경·배경색은 외부 터미널 기본색을 유지하며 프로그램이 명시한 색상과 구별한다.
- Python 플러그인 로딩·메타데이터·의존성 검사는 기존 PluginManager가 소유한다. stream은 표시 내용과 셸 builder 등록 API만 제공하며 별도의 Python 로더를 만들지 않는다. 플러그인 변경은 새 세션에 적용한다. 본체가 native 입력·단일 writer·요청 identity·포커스 동기화를 계속 소유한다.
- 언어·설정 경로·로그·진단은 기존 공통 구현을 재사용한다. help/version/diagnose 조회는 플러그인이나 셸을 실행하지 않고 설정·로그 파일을 생성하지 않는다. ish는 기존 공통 설정 경로인 ~/ish/.ishrc.py 또는 --home으로 지정한 디렉터리의 .ishrc.py를 읽는다. config 객체에 다음 세션 설정과 plugins 목록을 선언하며 실제 플러그인 로딩은 PluginManager가 수행한다. CLI는 해당 설정을 덮어쓰고 --no-rc/--no-plugins로 비활성화할 수 있다. help/version/diagnose 조회에서는 rc 코드도 실행하지 않는다. 선택적인 파일 로그는 진단만 기록하며 native 입력·출력 bytes를 수집하지 않는다. 콘솔 로그는 셸 시작 전 또는 터미널 복원 후에만 출력한다.
- 시작 옵션은 native 셸에 전달한다. --login은 -l 단독이며 --shell-arg와 상호 배제한다. 명시적인 --shell-arg 목록은 기본 -i를 대체한다. UI provider가 지원하지 않는 시작 인수는 셸 실행 전에 UI 없이 원래 argv/env로 시작하며 검증되지 않은 연동을 켜지 않는다.
- 기존 ish-vt의 스크롤백·선택 범위는 libghostty-vt가 소유한다. 새 ish의 화면·스크롤백·마우스는 호스트가 처리한다. 화면 문자열로 입력 소비자를 추측하지 않는다.
- 기존 refactor/editor-request 브랜치와 폴더를 보존한다. ish-vt 실험 코드는 보존하되 ish 2.0.0의 실행·지원 경로와 혼동하지 않는다.
- 첫 검증 대상은 Linux/WSL의 tcsh, heredoc, Vim과 resize다. OS 이식과 전 셸 자동완성 지원은 검증 없이 선언하지 않는다.

## 설계 규칙

- 제품 문구는 기존 `ish.lang.i18n`과 공통 Message 카탈로그에서 정의한다. 언어는 명시한 설정(CLI가 rc를 덮어씀), `LANG` 자동 감지, 영어 기본값 순서로 선택한다. `LC_ALL`·`LC_MESSAGES`·`LANGUAGE`는 ish 언어를 덮어쓰지 않는다. ish가 작성한 오류·로그·제공 샘플 UI도 번역하며, 셸 출력·외부 예외 세부 내용·wire 값·프로그램 식별자는 보존한다. 조회 명령은 rc를 실행하지 않고 `--lang`과 `LANG`만 적용한다.

1. 계약의 의미와 허용되는 상태 전환은 한 곳에서 정의한다. adapter는 같은 계약을 구현한다.
2. 정책을 선택하는 곳과 집행하는 곳을 분리한다. adapter가 기능과 정책을 선택하고 session이 전송·보류·회수를 집행한다.
3. 같은 사실의 변경 책임은 하나로 둔다. 기존 편집 요청 상태는 기존 session이 관리한다. UI 포커스는 Application, PTY 수명과 쓰기 큐는 Session이 소유한다. ish-vt 화면 상태는 libghostty-vt가, ish 화면은 호스트가 소유한다. 서로 다른 사실을 하나의 ID나 플래그로 합치지 않는다.
4. 셸·OS의 세부사항은 해당 경계에서 해석한다. 공통 편집기나 입력 라우터에 셸 이름별 분기를 추가하지 않는다.
5. 같은 이름은 같은 동작과 보장을 뜻한다. 읽기 관찰, 편집 요청, 입력 권한 부여, 완료를 구분한다.
6. 공개 계약은 독립적인 기대값과 사용자 결과로 검증한다. 실제 PTY·PTK 경로에서 최종 입력 소비자와 파일 결과를 확인한다. 내부 상태 모델만으로 live 지원을 선언하지 않는다.
7. 고정된 계약값과 정책값은 소유 모듈의 선언부에 모은다. 세션 토큰·요청 ID 등 런타임 값은 생성 책임이 있는 곳에서 만든다. 셸별 상수를 공통 상수 모듈에 몰아넣지 않는다.
8. 이름은 문맥에서 드러난 정보를 반복하지 않고 핵심 대상과 동작만 표현한다. 불명확한 약어를 피하고, submit·write·ack·complete의 의미를 구분한다. Python의 snake_case, PascalCase, UPPER_SNAKE_CASE를 따른다.

## 입력 계약

- 기존 편집 요청 경로는 유효한 요청 identity와 필요한 동기화 완료가 있어야 한다.
- 새 terminal 경로에서는 한 PTK Application이 외부 입력과 렌더링을 소유한다. 셸 입력은 terminal 포커스일 때 PTY로 전달하고, 팝업 입력은 팝업에서만 소비한다.
- 기존 ish-vt는 셸 출력을 VT 상태에만 반영한다. 새 ish는 하나의 coordinator/writer에서 셸 바이트와 제한된 PTK panel 출력을 순서대로 전달한다. 플러그인은 외부 터미널 writer를 소유하지 않는다.
- prompt 마커는 UI 표시/숨김 알림이며 구문 입력 권한이나 실행 readiness가 아니다. postcmd를 입력 권한으로 해석하지 않는다. 제어문 중간에 UI를 삽입하지 않으며 미지원 상태에서는 장식을 해제한다.
- stream 팝업 요청의 identity와 상태 전이는 handoff.Request가 소유한다. 첫 B만 identity를 확정하며, 팝업 닫힘·done 큐 등록·P 복귀 완료를 구분한다. 완료와 취소는 되돌릴 수 없는 별도 결과이며, 취소된 요청도 native 복귀 경계까지 입력 보류가 필요할 수 있다. 팝업 값의 취소와 handoff 취소를 합치지 않는다.
- Python 공통 wire 값과 의미는 stream.protocol의 Event/Reply에서 정의한다. 외부 셸 스크립트의 wire 구현과 독립 테스트의 literal 기대값은 각 경계에 유지한다. 기본 리소스 경로는 실제 로더가 소유하고 진단은 그 값을 읽는다.
- 셸 버퍼·커서를 입증하지 못한 상태에서는 화면 문자열을 역파싱하여 자동완성 후보를 적용하지 않는다.
- 명령 편집과 ish 명령 dispatch를 분리한다. continuation·heredoc 본문을 내부 명령이나 Python 도구로 실행하지 않는다.
- 프로그램 stdin과 Python input()은 native 입력 경로를 유지한다.
- 사용자 hook, 종료 상태, 입력 순서와 출처를 보존한다. timeout이나 화면 문자열은 production readiness 판단에 사용하지 않는다.
- 배포용 셸 소스 패치나 검증되지 않은 capability 활성화는 하지 않는다.

## 검증과 변경 범위

- 변경 경계에 맞는 테스트와 린트를 실행한다. 기존 API와 hook 순서를 바꿀 때는 호환 영향과 근거를 명시한다.
- stream의 PR 검사와 로컬 검사는 tools/check_stream.py를 공유한다. 필수 환경·테스트 모듈 누락, 빈 테스트, skip을 성공으로 간주하지 않는다. GitHub 실행 여부와 실제 호스트 터미널 검증 여부는 따로 보고한다.
- 새 stream 회귀 모듈을 유지 대상으로 추가할 때 검사기의 REQUIRED_STREAM과 독립적인 누락 검사 기대 목록에도 등록한다. 새 파일 자동 수집만으로 삭제·누락 방어가 된 것으로 간주하지 않는다.
- tests 디렉터리의 기존 로컬 테스트는 대부분 Git 추적 밖에 있다. 새 회귀 테스트는 명시적인 ignore 예외로 추적 가능하게 유지한다.
- 새 테스트는 실제 편집 입력, 반환값, 파일 결과 등 관찰 가능한 계약을 확인한다. 구현의 검증 함수를 그대로 호출해 기대값을 만들지 않는다.
- 기존 main 작업 폴더의 파일과 환경은 변경하지 않고 이 worktree에서 작업한다.
