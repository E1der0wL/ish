# ish-vt 프로토콜 보완 플러그인

이 API는 별도 `ish-vt` 실험 경로에만 적용한다. `ish` 2.0.0의 stream 경로를 바꾸지 않으며, 기본형에는 플러그인·UI·예약 키를 추가하지 않는다.

## 단일 관리 경로

출력·키·클립보드 보완 모듈은 모두 기존 `PluginManager`의 한 스크립트 경로를 사용한다. 기본은 `~/ish/plugin/script`이며 `config.vt["plugin_dir"]` 또는 `--plugin-dir`로 경로 하나를 바꿀 수 있다. 종류별 디렉터리 설정이나 별도 로더는 없다. `plugin_lib_dir`는 Python 의존성 설치 경로이며 또 다른 모듈 검색 경로가 아니다. 선택 목록도 `config.vt["plugins"]` 하나다.

아래 계약 안에서 외부 Python 파일을 추가·수정·제거할 때 native 재컴파일은 필요하지 않다. 변경은 다음 세션부터 적용한다. 새 입력 구조·협상·공통 이벤트가 필요하면 본체 API 보강이 필요할 수 있으며, C bridge 또는 libghostty를 바꿀 때는 해당 native 라이브러리를 다시 빌드한다. 지원 버전은 `TERMINAL_API = 1`이다.

## 출력 프로토콜 지원 범위

기존 `PluginManager`로 명시적으로 선택한 Python 플러그인이 `setup(api)`에서 보완 모듈을 등록한다. 외부 등록은 **고정된 libghostty가 모르는 숫자 OSC 명령**으로 제한한다. 예를 들어 `b"7400;"`는 명령 번호 전체를 소유한다. `b"74"`, `b"7400;part="`, 선행 0이 있는 번호 및 APC/DCS/CSI 등록은 허용하지 않는다.

libghostty의 독립 OSC parser로 소유 여부를 확인한다. 제목·색상·링크·클립보드 등 이미 알려진 명령은 내용이 불완전하거나 기능이 꺼져 있어도 보완 모듈로 넘기지 않는다. 같은 이름이나 겹치는 selector도 거부한다. `UNKNOWN_SEQUENCE` callback을 범용 fallback으로 사용하지 않는다.

이 범위는 보편적인 터미널 확장 API가 아니다. 새 DCS/APC 해석기를 공개하려면 해당 프로토콜의 native 소유 여부와 입력 경계를 먼저 입증해야 한다. SIXEL 지원을 추가하지 않았다.

## 최소 예시

기존 플러그인 폴더 규칙에 따라 `example_codec/__init__.py`에 작성한다. 아래 명령 번호는 설명용이며 표준 할당을 주장하지 않는다.

```python
from ish.terminal.extensions import ProtocolResult

PLUGIN_META = {"version": "1.0.0"}
TERMINAL_API = 1


class Codec:
    def handle(self, control):
        # control.kind/body/raw는 완성된 한 제어문의 변경 불가능한 값이다.
        # 이 예시는 payload를 화면/호스트에 그대로 전달하지 않는다.
        return ProtocolResult(native=b"[extension received]")

    def cancel(self):
        # multipart 등 임시 전송 상태가 있다면 비운다.
        pass

    def close(self):
        # 보유 자원을 정리한다. 세션당 한 번 호출한다.
        pass


def setup(api):
    api.register_protocol(
        "example-codec", kind="osc", prefixes=(b"7400;",), factory=Codec
    )
```

`handle`, `cancel`, `close`는 동기 함수다. PTY 입력이 시작되면 등록은 고정된다. 한 명령이 여러 번 전송되면 각각 별개 명령으로 처리한다. 여기서 금지하는 중복은 여러 해석기의 동일 명령 처리와 실패 후 재전달이며, 프로토콜 자체에 없는 요청 ID를 만들어 정상적인 반복 명령을 제거하지 않는다.

`ProtocolResult(native=..., reply=...)`는 native 입력을 먼저 처리하고 그 결과 발생한 엔진 응답 뒤에 `reply`를 기존 Session writer로 넣는다. `native`는 libghostty에만 들어가며 외부 터미널로 직접 출력하지 않는다. 제목·bell 등 외부 효과도 기존 libghostty→공통 effect 경로를 사용한다. 모듈은 화면·커서·스크롤백을 따로 복제하지 않는다. 일반 셸 출력이나 프로그램 stdin을 Python 명령으로 실행하는 API도 제공하지 않는다.

결과의 타입·크기·제어문 완결성을 쓰기 전에 검사한다. 현재 native 변환 결과는 최대 32 MiB, reply는 최대 1 MiB이며 공통 전송 큐의 한도도 적용된다. handler 오류 또는 잘못된 결과가 발생하면 그 route의 임시 상태를 취소하고 닫는다. 이후 해당 route의 입력도 소비하며 native나 다른 해석기로 재시도하지 않는다. 나머지 출력은 계속 처리하고 제한된 진단을 `Terminal.protocol_errors`로 제공한다. 실제 native/PTY writer 오류는 세션 오류로 전파하며 일부 출력을 재전달하지 않는다.

CAN/SUB·RIS·EOF는 임시 전송을 취소한다. 일반 resize가 모듈 소유의 두 번째 화면 상태를 만들지는 않는다. 크기·커서·호스트 권한에 의존하는 확장은 이 최소 API만으로 지원을 선언할 수 없다. 한도·취소·중간 상태·응답 순서를 갖춘 별도 계약과 검증이 필요하다.

## native 지원으로 바뀔 때

새 libghostty가 해당 프로토콜을 처리한다는 사실을 버전 번호나 심볼 존재만으로 판단하지 않는다. 같은 입력·분할·취소·화면·커서·history·응답 순서 검증을 통과한 뒤 engine 소유 `NATIVE_PROTOCOLS`에 `(이름, kind, prefixes)` 전체를 등록한다. `EngineInfo.native_protocols`의 해당 항목과 정확히 일치할 때만 native로 선택하며 보완 factory를 호출하지 않는다. 이미지 capability 이름만 재사용하거나 다른 OSC 번호를 붙여서 native 승격을 주장할 수 없다. 변환 모듈과 그 의존성을 제거할 수 있다.

일반 플러그인 파일의 최상위 import는 PluginManager가 실행한다. 제거 예정인 무거운 decoder의 import는 factory 내부에 두어야 native 선택에서 해당 의존성을 읽지 않는다. 클립보드 권한·비동기 거래, 호스트 출력 presenter, UI 포커스는 별개의 ish 책임이므로 엔진 지원만으로 제거하지 않는다.

Python 플러그인은 기존 PluginManager와 동일한 사용자 권한으로 실행하는 신뢰 코드다. 이 API의 writer 제한은 설계 계약이며 Python 실행을 격리하는 보안 sandbox가 아니다.

## 호스트 키 매핑

`api.register_keys(name, mappings)`는 완성된 추가 시퀀스를 기존 `StructuredKey`로 변환한다. OSC 출력 등록과 다른 입력 경계이며 임의 callback·원문 PTY 쓰기·새 프로토콜 협상을 제공하지 않는다.

```python
from ish.terminal.host import StructuredKey

PLUGIN_META = {"version": "1.0.0"}
TERMINAL_API = 1

def setup(api):
    api.register_keys("extra_keys", {"\x1b[9001~": StructuredKey("f13", 57376)})
```

`9001~`는 예시로 예약한 시퀀스이며 자동으로 물리 키에 연결되는 표준 키 코드가 아니다. 호스트가 이 신호를 보내도록 별도 설정했을 때만 매핑이 작동한다. `terminal_keys` 샘플은 이 매핑만 등록하며 UI나 단축키를 추가하지 않는다.

등록은 기존 native/PTK 키 및 응답·마우스·붙여넣기·확장 키 프로토콜의 예약 영역과 겹칠 수 없다. 추가 영역은 완성된 숫자 CSI `~` 형식으로 제한한다. 키 이름·코드점·수정키는 기존 지원 표현과 일치해야 하며 press 이벤트만 등록한다. 등록 최대 16개, 전체 256키, 시퀀스 32자 한도다. 등록된 사전과 값은 복사되며 첫 호스트 입력 뒤에는 변경할 수 없다.

일반 문자·기존 키·붙여넣기는 기존 입력 경로를 유지한다. 미지원 제어 시퀀스를 일반 문자로 분해하거나 셸에 재전달하지 않는다. `CSI 1;2R`처럼 커서 위치 응답과 같은 legacy Shift+F3 신호는 사용자 키로 추측하지 않는다. 확장 키 프로토콜에서 구분 가능한 키는 기존 협상 경로를 사용한다. 팝업 포커스와 release 처리는 본체가 소유한다.

## 시스템 클립보드 연결

`api.register_clipboard(name, factory)`로 일반 CLIPBOARD 텍스트를 읽고 쓰는 provider 하나를 선택한다. factory는 `async read() -> str`, `async write(text) -> None`, `close()`, `async aclose()`를 제공하는 객체를 반환한다. 세션 시작 전에 한 번 생성하며 읽기·쓰기는 실제 요청에서만 수행한다.

기본 접근 정책은 `write`다. `.ishrc.py`의 `config.vt["clipboard"] = "read-write"` 또는 `--clipboard read-write`로 프로그램 읽기를 켠다. provider는 정책·PTY writer를 갖지 않으며 Broker가 요청 순서·취소·지연 완료·크기 제한을 관리한다. 잘못된 provider, 중복 선택, 세션 시작 후 등록은 거부한다. 운영 로그에는 내용 대신 실패 횟수만 남긴다.

`clipboard_system` 샘플은 hub의 별도 X11 worker를 재사용한다. `TARGET`의 기본 `auto`는 로컬 WSL에서 Windows, 전달된 `DISPLAY`가 있는 환경에서 X11을 선택한다. SSH 환경에서는 원격 Windows/Wayland 데스크톱으로 우회하지 않는다. `windows` 또는 `x11`로 명시할 수도 있으며, 선택한 대상 실패 후 다른 대상으로 바꾸지 않는다. X11에는 연결 가능한 `DISPLAY`·인증과 시스템 libX11이 필요하다. Windows는 WSL의 Windows 실행 파일 상호운용성과 `powershell.exe`·`clip.exe`가 필요하다.

연결 범위는 OSC 52 일반 클립보드와 OSC 5522의 `text/plain` 단일 표현이다. PRIMARY·다른 선택 영역·임의 MIME/이미지·alias 지원을 선언하지 않는다. UTF-8 텍스트는 1 MiB 한도이며 provider 전송도 같은 한도를 적용한다. 연결을 선택하지 않은 경우 기존 호스트 프로토콜 경로를 유지한다. 외부 읽기 실패를 내부 레지스터 값으로 대체하지 않는다. X11 선택 소유 프로세스는 교체·종료 시 정리된다.

실제 MIME 목록을 조회하는 OSC 5522 `.` 요청도 이 텍스트 provider에서는 `ENOSYS`로 거부한다. `read/write` 계약만으로 OS의 모든 저장 형식을 알 수 없기 때문이다. 연속된 로컬 OSC 52 복사·읽기는 순서대로 처리하며, 대기열은 64개 요청·합계 4 MiB 한도다. 한도 초과나 전송 실패는 성공으로 보고하지 않는다. 종료 시 대기열 전체의 완료를 기다리고, 취소된 요청의 늦은 결과는 다음 요청이나 PTY에 전달하지 않는다.
