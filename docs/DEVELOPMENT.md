# ish 개발 저장소

정식 작업 위치는 Windows의 `D:\Developments\ish`, WSL의 `/mnt/d/Developments/ish`다. 현재 개발 브랜치는 `codex/hookless-vt`이며, 저장소 내부에 독립된 `.git`과 전체 조상 커밋 이력을 보관한다. 기존 GitHub 저장소를 `origin`으로 사용한다.

`17c74ec`는 기능별 호스트 설정과 그에 필요한 기존 VT 보강·테스트·문서를 함께 저장한 기준 커밋이다. 이전 C 드라이브 worktree와 다른 실험 폴더는 백업으로 남아 있으며 새 저장소가 그 Git metadata나 가상환경을 참조하지 않는다. 이전 폴더를 새 개발 위치와 번갈아 수정하지 않는다.

## 실행과 검증

Linux x86_64/WSL에서 저장소 루트를 기준으로 실행한다. Python 환경은 이 위치의 `.venv`로 새로 생성한다. `uv`와 Python 3.12.14 이상·3.13 미만이 필요하며, native 빌드에는 C 컴파일러·curl·tar·xz, 전체 검사에는 Bash·zsh·tcsh·Vim·ncurses 도구·Xvfb·xclip·util-linux·libX11이 필요하다. 현재 WSL 환경에는 이 도구들이 준비되어 있다.

```sh
# 자신이 내려받은 저장소 폴더에서 실행한다.
uv sync --frozen --group dev

# 새로 복제했거나 native 빌드가 없을 때
uv run --no-sync python tools/build_ghostty_vt.py

# 추가 UI 없는 기본형
uv run --no-sync ish --shell tcsh
uv run --no-sync ish --shell bash
uv run --no-sync ish --shell zsh

# 유지할 자동 검사
uv run --no-sync python tools/check_terminal.py
```

이전 환경에서 검증한 Linux native 라이브러리는 `.cache/ghostty-vt/install/lib`에 상대 symlink와 함께 보존했다. 새로 빌드할 때는 기존 checksum 고정 빌드 도구를 사용한다. 가상환경과 native 바이너리는 Git에 커밋하지 않는다.

이 개발 PC에서는 Windows Terminal에서도 아래 명령으로 시작할 수 있다. 다른 위치에 내려받았다면 `--cd` 뒤의 경로를 자신의 저장소 폴더로 바꾼다.

```powershell
wsl.exe -d Ubuntu --cd /mnt/d/Developments/ish --exec .venv/bin/ish --shell tcsh
```

## Windows Terminal에서 검증하기

Windows Terminal 안에서 WSL을 열고 저장소 루트에서 실행한다. 결과 디렉터리는 검사마다 새 경로를 지정한다. 첫 번째 명령에서는 안내에 따라 창 크기를 바꾼다.

```sh
uv run --no-sync python tests/terminal_matrix.py .cache/host-new-run --require-resize
uv run --no-sync python tests/management_host.py .cache/management-host-new
```

자동 테스트가 보장하는 범위는 [회귀 명세](VT_TEST_CONTRACTS.md)에, 호스트에서 관찰한 응답과 렌더링 결과는 [기능 정책](VT_FEATURE_POLICY.md)과 [비교 기록](VT_PARITY.md)에 있다. 이전 실행의 로그는 당시 개발 환경의 `.cache`에 보관한다.

## 관리 범위

`ish`는 libghostty-vt와 PTK를 사용하는 단일 제품 경로다. 이전 stream 실행기와 별도 `ish-vt` 명령은 제거했으며 호환 실행기를 제공하지 않는다. `ish --help`와 `ish --config`에서 사용법과 설정을 확인한다. GitHub push·기본 브랜치 변경·릴리스 태그 발행은 별도의 배포 작업이다. 현재 검증 대상은 Windows Terminal/WSL의 Bash·zsh·tcsh이며, 이 위치를 정식 개발 저장소로 사용하는 것이 모든 OS·터미널 기능의 검증 완료를 뜻하지는 않는다.
