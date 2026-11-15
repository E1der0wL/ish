# tcsh 자동완성 후보 수집 실험

## 결론

**셸 소스를 수정하거나 화면 문자열을 파싱하지 않고, 협력하는 동적 자동완성
생성기의 원본 출력은 수집했다. tcsh가 최종적으로 선택·표시하는 모든 후보를
외부에 반환하는 범용 경로는 확보하지 못했다.**

이 둘은 다르다. 실험 생성기가 `alpha`, `bravo`를 출력할 때, `pick al<Tab>`의
기록에는 두 항목이 모두 남지만 실제 실행된 명령의 인수는 `alpha` 하나다.
접두어 비교와 인용 처리는 여전히 tcsh 안에서 진행된다. 기본 파일 완성은
그 생성기를 전혀 호출하지 않고 정상 동작했다.

따라서 이 결과로 tcsh용 통합 드롭다운 capability를 활성화하지 않는다.
Application, 입력 라우터, Session, 셸 연동 스크립트와 기본형 동작은 변경하지
않았다. 추가한 것은 격리된 관찰 테스트, 이 문서, 새 파일의 ignore 예외뿐이다.

## 환경과 관찰 방법

- WSL2 Ubuntu, Linux `6.6.87.2-microsoft-standard-WSL2`, x86_64.
- 설치된 `/usr/bin/tcsh`: `6.24.13 (Astron) 2024-06-12`, 패키지 `6.24.13-2.1`.
- 바이너리 SHA-256:
  `3b660a10244cbf564a5cae251ee829cbbb52ac02ccbc11d48e4e19590cdf5f14`.
- 소스 검토: 공식 `TCSH6_24_13` 태그의 커밋
  `b6fc0fbda4acdea881920f9481658702c8f3cb86`.
  배포판 패치 전체와의 차이까지 대조한 것은 아니다.
- Python 3.12.14. 기존 `test_stream_shells.Shell`의 controlling PTY를 재사용했다.

테스트마다 별도 HOME, `.tcshrc`, 작업 디렉터리를 사용한다. 실제 Tab,
`list-choices`, 커서 이동, Enter, Ctrl+C를 PTY로 입력한다. 완료 결과는 실행된
프로그램의 `argv`, 실제 stdin 바이트, hook/status 파일로 판정한다.
PTY 출력은 배출하고 증거로 보관할 뿐 후보나 준비 상태를 읽어내지 않는다.
시작 대기도 화면 대신 테스트 전용 `precmd`의 파일 기록을 사용한다. 이 파일은
관찰용 증거이며 ish의 입력 소유권을 부여하는 프로토콜이 아니다.

지연 테스트는 생성기가 진입 사실을 기록한 뒤 명시적인 파일 gate를 기다리게
한다. 제한 시간은 실패 검출에만 사용한다. 테스트 종료 시에는 해당 controlling
session에 속하는 foreground process group과 셸을 정리한다.

## 수집해 본 경로

| 경로 | 실제로 얻은 것 | 얻지 못한 것 |
| --- | --- | --- |
| `complete pick` | 등록된 규칙 문자열 | 현재 입력에 대해 계산된 후보 목록 |
| 정적 규칙 `(alpha bravo)` | 규칙에 적힌 원천 항목 | tcsh의 문맥 판정·필터·삽입 결과를 반환하는 API |
| 변수 기반 규칙 `$choices` | 셸 변수가 가진 원천 배열 | 모든 원천을 통합한 최종 후보 export |
| backtick 동적 생성기 | 해당 생성기의 stdout bytes와 `COMMAND_LINE` | 필터 이후 후보, 안전한 교체 범위·삽입 문자열 |
| 기본 파일 완성 | 실행 결과 파일에서 최종 `argv` 확인 | 생성기 hook을 통한 파일 후보 수집 |
| `list-choices` | 호출된 생성기의 원본 bytes | 표시된 후보를 구조화하여 반환하는 별도 채널 |

동적 실험은 같은 fixture 생성기를 직접 호출하는 규칙과 `tap`을 경유하는 규칙을
비교했다. 즉 **해당 규칙을 협력 helper 호출로 바꿔야 하는 제한적 방식**이다.
사용자의 임의 자동완성 설정 전체를 수정 없이 가로채는 구현이 아니다.

`tap`은 생성기의 stdout/stderr를 각각 버퍼링하고 기록한 후 tcsh에 돌려준다.
검증한 유한·비대화형 생성기에서는 완성 결과가 같았다. 하지만 실시간 출력
시점과 stdout/stderr의 교차 순서는 보존하지 않는다. signal 종료도 같은 signal을
재발생시키지 않고 숫자 종료 코드로 변환한다. 따라서 임의 생성기에 투명한
production wrapper라고 주장할 수 없다.

`COMMAND_LINE`도 전체 편집 상태가 아니다. 실측에서 다음 차이가 있었다.

- `pick al tail`의 커서를 `al` 뒤로 옮겨 완성하면 `pick al`만 전달됐다.
  실행 결과에는 기존 `tail`이 유지됐다.
- `/bin/true; pick al`에서는 앞 명령이 빠진 ` pick al`이 전달됐다.
- `pick "al`에서 완성하면 기록에는 따옴표가 없는 `pick al`이 남았다.
  tcsh는 이 사례에서 닫는 따옴표를 자동 삽입했다.

이 값으로 원래 버퍼·커서·교체 구간을 복원해 후보를 적용하면 안 된다.

## 실제 PTY 검증 결과

새 테스트 **18개 통과**. 마지막 실행은 7.801초였다. 아래 결과는 새 PTK UI가
아닌 native tcsh 편집·실행 결과의 관찰이다.

| 검증 | 실제 결과 |
| --- | --- |
| 기본 파일 완성 | `uniq<Tab>`이 `unique-file` 인수로 실행됨. 생성기 호출 없음 |
| 정적 규칙과 규칙 출력 | 정의 출력에서 규칙을 확인. `br<Tab>`은 `bravo`로 실행됨 |
| 동적 직접 호출 / tap 대조 | 원본 `alpha`, `bravo`를 기록하면서 `al<Tab>`은 `alpha`로 실행됨 |
| 커서 중간 완성 | 커서 뒤 `tail`이 보존됨. 생성기 context에는 뒤쪽 내용 없음 |
| 명령 구분자 | 앞 명령이 context에서 빠짐. 뒤 명령의 완성은 정상 |
| 열린 따옴표 | 직접 호출과 tap 모두 `alpha` 한 인수로 실행. 원래 인용 문법은 context에 없음 |
| 공백·한글 파일명 | `space name`, `한글 파일`이 각각 한 인수로 보존됨 |
| 동적 stdout의 공백·한글 | `space name`은 tcsh에서 단어로 분리됨. `spa` 완성 결과는 `space`; 한글 후보도 확인 |
| `list-choices` | 생성기 원본 출력은 기록됨. 미완성 `al`은 그대로 실행되며 최종 배열 export는 생기지 않음 |
| 변수 배열 후보 | `space name`, `한글 항목`을 각각 한 인수로 완성. 동적 생성기 호출 없음 |
| `fignore` | `source.c`, `source.o` 중 `.o` 제외 설정에 따라 `source.c`로 완성됨 |
| 빠른 typeahead | 한 PTY write의 Tab 뒤 `TAIL`이 중복·유실 없이 후속 인수로 실행됨 |
| 명시적으로 지연시킨 생성기 | 대기 중 보낸 `TAIL`이 gate 해제 후 native 순서로 실행됨 |
| 생성기 대기 중 Ctrl+C | 부분 명령은 실행되지 않고 이후 명령·다음 완성은 정상 동작 |
| `$<` | Tab과 한글이 실행 중 stdin 데이터로 전달됨. 완성 생성기 호출 없음 |
| 외부 프로그램 stdin | Tab과 한글의 실제 입력 bytes가 보존됨. 완성 생성기 호출 없음 |
| hook·종료 상태 | fixture의 `precmd`·`postcmd` 기록과 명령 종료 상태가 직접 호출 / tap에서 동일 |
| 기존 tcsh adapter 대조 | 기존 연동을 켜고 꺼도 같은 동적 완성 결과와 원본 기록을 얻음 |

기존 동작 보존 확인은 별도로 실행했다.

- `test_stream_*.py`: **163개 통과**, 156.955초.
- `test_terminal_session.py`: **4개 통과**, 0.396초.
- 새 두 Python 파일 Ruff 검사 및 포맷 검사 통과.
- 실험 전후 `src/ish/stream` 파일 20개의 해시 동일. 설치된 tcsh 바이너리도
  해시 동일. 종료 후 fixture 작업 디렉터리를 사용하는 잔여 프로세스 없음.

기존 테스트에는 Bash/zsh/tcsh와 UI 없는 기본형 경로가 포함된다. 이 회귀 통과가
새 tcsh 드롭다운이나 실제 Windows Terminal 화면 합성을 검증했다는 뜻은 아니다.

초기 fixture 실패도 구분했다. 동적 규칙의 `/` 구분자 안에 `./tap`을 넣어 생긴
규칙 구문 오류를 수정했고, tcsh의 자동 닫는 따옴표와 구분자 뒤 context 선행
공백을 실제 native 대조 결과에 맞췄다. 운영 셸 결함을 수정한 것은 아니다.

## 소스에서 확인한 경계

공식 소스의 다음 경계가 실측과 일치한다.

1. [`docomplete` / `tw_pr`](https://github.com/tcsh-org/tcsh/blob/b6fc0fbda4acdea881920f9481658702c8f3cb86/tw.comp.c#L55)는
   등록 규칙을 출력한다. 현재 버퍼의 최종 후보를 질의하는 명령이 아니다.
   규칙 출력은 공백도 정규화하므로 모든 사용자 규칙의 무손실 재등록 수단으로
   가정할 수 없다.
2. [편집기에서 `tenematch` 호출](https://github.com/tcsh-org/tcsh/blob/b6fc0fbda4acdea881920f9481658702c8f3cb86/ed.inputl.c#L411)은
   내부 `InputBuf`와 `Cursor - InputBuf`를 사용한다.
   [`tenematch`](https://github.com/tcsh-org/tcsh/blob/b6fc0fbda4acdea881920f9481658702c8f3cb86/tw.parse.c#L155)에서
   커서까지의 입력과 현재 명령 구간이 처리된다.
3. [`tw_complete`의 `COMMAND_LINE` 설정](https://github.com/tcsh-org/tcsh/blob/b6fc0fbda4acdea881920f9481658702c8f3cb86/tw.comp.c#L634)과
   [`tw_result`](https://github.com/tcsh-org/tcsh/blob/b6fc0fbda4acdea881920f9481658702c8f3cb86/tw.comp.c#L354)가
   원천 선택·동적 생성기 실행에 관여한다. 동적 출력은 내부 word-list 원천이 된다.
4. [`tw_collect_items`](https://github.com/tcsh-org/tcsh/blob/b6fc0fbda4acdea881920f9481658702c8f3cb86/tw.parse.c#L1091)와
   [`t_search`](https://github.com/tcsh-org/tcsh/blob/b6fc0fbda4acdea881920f9481658702c8f3cb86/tw.parse.c#L1509)가
   매칭·수집과 표시/완성 동작을 수행한다. 생성기 출력 이후에도 내부 처리가 남는다.
5. [`tw_list_items`](https://github.com/tcsh-org/tcsh/blob/b6fc0fbda4acdea881920f9481658702c8f3cb86/tw.parse.c#L1402)는
   표시 경로이고, 완성 삽입은 `insert_meta` 등을 거친다. 목록 표시와 단어 완성은
   `fignore`, 정확한 일치, 접미사 처리까지 같은 계약이라고 가정할 수 없다.

설치된 바이너리의 동적 심벌도 확인했다. `tenematch`, `t_search`, `tw_*`,
`InputBuf`, `Cursor`, `InsertStr`, `DeleteBack`은 외부에 노출된 동적 심벌에서
찾지 못했다. libc `read`/`write`를 가로채는 기존 방식은 후보의 의미와 편집
문맥을 얻는 hook이 아니다. 위 내부 함수에 단순한 이름 기반 `LD_PRELOAD`
interposition을 적용할 경로도 이번 바이너리에서는 확보하지 못했다.

## 적용 가능한 범위와 남은 일

셸 수정 없이 적용 가능한 최소 범위는 **명시적으로 참여하는 동적 후보 제공자의
원본 결과 수집**이다. 이것만으로 기본 파일·명령·변수·사용자 정의 완성을 모두
합쳐 tcsh와 동일한 드롭다운을 제공할 수는 없다. 정적 규칙을 해석하거나 Python에서
tcsh의 필터를 재구현하는 방안은 이번에 도입하지 않았다.

최종 후보 드롭다운에는 서로 다른 세 보장이 필요하다.

1. tcsh가 현재 요청에 대해 계산한 최종 후보와 적용 방법을 얻는 것.
2. 실제 버퍼·커서와 요청 identity를 연결하고 취소·변경된 요청을 버리는 것.
3. 선택한 후보를 native 편집기에 안전하게 적용하는 것.

이번 관찰 helper는 어느 것도 범용으로 제공하지 않는다. 파일 기록의 UUID는
증거 파일 충돌 방지용이지 셸 편집 요청의 wire identity가 아니다.

추가 native 협력이나 버전별 바이너리 계측은 별도 검토 대상이다. **소스 수정
없이는 원리적으로 불가능하다고 증명한 결과는 아니다.** 이번에 확인한 공개
인터페이스와 helper 확장 범위에서 범용 export를 찾지 못했다는 결론이다.
배포용 셸 패치, 바이너리 계측, 별도 후보 엔진, popup focus 전환은 구현하지 않았다.

새 실험에서 미검증인 범위는 임의 사용자 생성기의 비정상 종료·대화형 입력·출력
스트리밍, Ctrl+D, 모든 인용/Unicode 조합, `recexact`·`listmax` 등의 모든 설정,
명령/사용자/호스트 원천 전체, 비동기 요청의 지연·중복 응답 적용, PTK 메뉴에서
선택한 항목의 native 삽입, 실제 호스트 화면 렌더링이다. 기존 회귀 테스트의
통과로 이 범위를 지원한다고 선언하지 않는다.

## 재실행과 증거

의존성이 설치된 Linux/WSL Python 환경에서 저장소 루트를 기준으로 실행한다.
기존 셸 연동 테스트와 동일한 의존성이 필요하다.

```sh
PYTHONPATH=src:tests python -m unittest -v test_tcsh_completion
PYTHONPATH=src:tests python -m unittest discover -s tests -p 'test_stream_*.py'
PYTHONPATH=src:tests python -m unittest discover -s tests -p 'test_terminal_session.py'
```

선택적으로 `ISH_COMPLETION_EVIDENCE`를 지정하면 테스트별 argv/context/tap/stdin
기록, fixture 설정, 분석하지 않은 PTY 출력 bytes를 해당 디렉터리에 복사한다.
이번 실행 로그·환경·소스 사본·증거 묶음은 Git에서 제외된
`.cache/tcsh-completion/`에 보관한다. 테스트 코드는
`tests/test_tcsh_completion.py`, `tests/tcsh_completion.py`이며 새 ignore 예외로
추적 가능하게 유지한다.
