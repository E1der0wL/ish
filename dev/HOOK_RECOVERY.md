**tcsh 실행 중 훅 변경 — 2026-09-10**

다음 명령을 실행하면 사용자 `precmd`가 ish의 연동용 alias를 덮어써, 프롬프트 완료 마커와 컨텍스트 갱신이 끊겼습니다. 실행 중 `postcmd` 변경은 반대로 다음 ish 프롬프트에서 이전 정의로 덮어쓰고 있었습니다.

```tcsh
alias precmd "set prompt='ish > '"
alias postcmd 'echo running'
```

`_recover()`는 foreach 선입력 수정 때 csh/tcsh의 프로세스 상태 기반 복구를 건너뛰도록 바뀌었습니다. 셸이 잠들어 있고 자식 프로세스가 없다는 조건은 기본 프롬프트 대기뿐 아니라 `foreach` 본문이나 `$<` 입력 대기에서도 성립합니다. 이 상태에서 `source`를 PTY에 보내면 사용자 입력으로 소비될 수 있습니다. 해당 복구를 끄면서 실행 중 훅 교체를 처리할 별도 경로를 마련하지 못했던 것이 이번 회귀의 원인입니다.

현재 구현은 tcsh의 기본 프롬프트 직전 `periodic`에서 훅 정의를 확인합니다. `precmd`·`postcmd`를 교체하거나 삭제했다면 그 결과를 새로운 사용자 훅으로 저장하고 ish의 래퍼를 다시 연결합니다. 두 훅을 한 명령에서 함께 바꾸거나 `source`한 파일에서 바꾸는 경우도 처리합니다. 이 동작은 셸 내부에서 이루어지며 사용자 명령 문자열이나 히스토리에 복구 명령을 추가하지 않습니다.

- [scripts.py](D:/Programs/ish/src/ish/shell/scripts.py)의 `ish_watch_hooks.tcsh`는 사용자 `periodic`을 실행할 시점을 판단하고, `ish_bind_hooks.tcsh`는 새 훅을 저장하고 연동을 다시 연결합니다.
- 기본 프롬프트의 `precmd`에서도 다시 확인하므로 사용자 훅 자체가 다른 훅을 변경한 결과를 반영합니다. 반복해서 확인해도 기존 래퍼를 사용자 훅으로 중복 저장하지 않습니다.
- 내부 확인용 스크립트를 읽을 때는 `postcmd`를 해제하고, 사용자 훅을 실행하는 동안에는 다시 연결합니다. 사용자 훅의 호출 횟수와 종료 상태를 보존합니다.
- [base.py](D:/Programs/ish/src/ish/shell/base.py)의 csh/tcsh 분기에서는 여전히 프로세스 대기 상태만으로 PTY에 복구 명령을 보내지 않습니다. BSD csh는 `precmd`·`postcmd`가 없으므로 이번 tcsh 훅 감시를 설치하지 않습니다.

이 감시는 `periodic`과 `tperiod=0`을 연동용으로 사용합니다. 시작 시 사용자가 설정한 `periodic` 본문과 주기는 별도로 저장하여 유지합니다. 사용자 콜백이 없으면 매 프롬프트마다 시계 조회 프로세스를 실행하지 않습니다. ish 실행 중 `tperiod`의 표시 값은 감시용인 `0`이며, `precmd`·`postcmd`·`periodic` 등 모든 연동 훅을 한꺼번에 제거하는 상황까지 자동 복구하는 장치는 아닙니다. 이 경우에는 ish를 재시작해야 합니다.

tcsh의 [실행 루프](https://github.com/tcsh-org/tcsh/blob/master/sh.c)와 [periodic 구현](https://github.com/tcsh-org/tcsh/blob/master/tc.func.c)을 확인했습니다. `periodic`은 기본 프롬프트 앞에서 실행되며 반복문 입력 대기에는 실행되지 않습니다. 따라서 화면의 프롬프트 문자열이나 유휴 시간으로 입력 상태를 추정할 필요가 없습니다.

검증은 [test_tcsh_history.py](D:/Programs/ish/tests/test_tcsh_history.py)와 [test_foreach_typeahead.py](D:/Programs/ish/tests/test_foreach_typeahead.py)에서 실제 tcsh와 ish CLI에 같은 명령을 보내 비교합니다.

- `precmd` 교체 후 새 프롬프트 표시, `postcmd` 교체·삭제, 두 훅의 동시 변경과 `source`를 통한 변경.
- 명령 종료 코드, 기존 사용자 훅과 `periodic`의 주기·중첩 호출, `!!`와 히스토리 내용.
- 두 훅을 교체한 후 느린 명령 치환이 있는 foreach의 선입력 표시.
- `$<` 입력 대기가 기존 복구 임계 시간보다 길어져도 사용자 입력에 복구 명령이 섞이지 않는지 확인.

재검증:

```bash
PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p 'test_*.py' -v
```

사용자 요청으로 제외한 `tests/test.py`는 실행하거나 수정하지 않았습니다.

WSL에서 Bash·dash·BSD csh·tcsh·zsh를 사용한 전체 **98개 테스트가 통과했습니다**. 테스트용 셸과 rc는 임시 디렉터리에 두었으며 사용자 셸 설정을 변경하지 않았습니다.
