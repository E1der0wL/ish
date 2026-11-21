# ish-vt terminfo 광고 전체 카탈로그

> 제품 통합 전 카탈로그를 시험 근거로 보존한다. 현재 제품 이름은 `ish`이며 실행·설정 방법은 [사용 안내](VT_USAGE.md)를 참고한다.

기준일: 2026-11-16. [기본 지원 명세](history/VT_BASELINE_AUDIT.md)의 HX02 대조 자료다. Ubuntu/WSL에서 읽은 `xterm-256color`를 그대로 보존한다. 런타임이나 terminfo 데이터베이스를 변경하지 않았다.

**후속 결과(2026-11-17):** [추가 대조 결과](history/VT_TERMINFO_RESULT.md)에 기존 188개와 잔여 99개의 검사 연결, 명시적인 차이·제외 11개, 실제 호스트에서 별도로 확인할 조건을 정리했다. 아래의 그룹별 상태 문장은 최초 감사 시점의 기록이며 최신 완료 판정으로 읽지 않는다. 원래 값 전체는 후속 대조에서도 동일했다.

- 명령: `infocmp -1 -x xterm-256color`
- 원본 경로: `/usr/share/terminfo/x/xterm-256color`
- 합계: **287개 = boolean 13개 + number 5개 + string 269개**.
- 아래 22개 그룹에 각 이름을 정확히 한 번 배정했다. 이름의 누락·중복이 없는 것을 문서 생성 시 확인했다.
- **카탈로그의 완전성과 동작 검증의 완전성은 다르다.** 현재는 모든 entry를 독립 테스트와 연결한 표가 없다. 해당 그룹에 A/W 회귀가 있더라도 각 entry 전체를 통과로 처리하지 않는다.
- 값은 이 환경의 스냅샷이다. 다른 ncurses/terminfo 버전은 달라질 수 있다. 주입하는 child `TERM`, DB 광고, native 처리, PTK 표현, host 능력을 각각 대조해야 한다.

## 그룹별 연결 상태와 남은 검증

### 플래그 — 13개

관련 명세: HX02. 광고 13개. 일부 native 동작과 대응하나 flag별 사용자 결과는 미검증. mc5i 등 peripheral 광고는 범위 결정 필요.

`OTbs`, `am`, `bce`, `ccc`, `km`, `mc5i`, `mir`, `msgr`, `npc`, `xenl`, `AX`, `XF`, `XT`

### 수치 — 5개

관련 명세: HX02, HX03. 색 수·초기 크기·탭 간격·color-pair 광고. 실제 PTY 크기·truecolor·host 능력과의 정합성은 미검증.

`colors`, `cols`, `it`, `lines`, `pairs`

### 커서 이동·탭·저장 — 19개

관련 명세: VT03. native 일반 경로에 위임. 각 entry의 모든 매개변수·경계·저장/복원 결과 미검증.

`cbt`, `cr`, `cub`, `cub1`, `cud`, `cud1`, `cuf`, `cuf1`, `cup`, `cuu`, `cuu1`, `home`, `hpa`, `ht`, `hts`, `rc`, `sc`, `tbc`, `vpa`

### 삽입·삭제·지우기·반복 — 16개

관련 명세: VT05, VT12. native 위임, 일부 회귀 있음. entry별 검증표 없음. E3는 내부 scrollback 삭제까지 확인 필요.

`clear`, `dch`, `dch1`, `dl`, `dl1`, `ech`, `ed`, `el`, `el1`, `ich`, `il`, `il1`, `rep`, `E3`, `rmir`, `smir`

### 스크롤 — 6개

관련 명세: VT06. native 위임. 영역·history·wide cell·resize 조합은 미검증.

`csr`, `ind`, `indn`, `nel`, `ri`, `rin`

### 줄바꿈·좌우 margin — 6개

관련 명세: VT04. native 위임. 개별 entry 및 상태 조합 미검증.

`mgc`, `rmam`, `smam`, `smglp`, `smglr`, `smgrp`

### 색상 — 5개

관련 명세: VT08, HX04. 연결. default/palette/query와 WT 실제 색 표현의 entry별 검증 미완성.

`initc`, `oc`, `op`, `setab`, `setaf`

### 글자 속성 — 15개

관련 명세: VT09, VT13. native→PTK 일반 속성 연결. host별 외관 미검증; sgr/sgr0의 ACS reset도 포함.

`blink`, `bold`, `dim`, `invis`, `rev`, `ritm`, `rmso`, `rmul`, `sgr`, `sgr0`, `sitm`, `smso`, `smul`, `rmxx`, `smxx`

### 커서 외관 — 7개

관련 명세: VT11. 모양/가시성 일부 연결. 커서색은 host gate, WT off. 실제 외관·기본값 복원 미검증.

`civis`, `cnorm`, `cvvis`, `Cr`, `Cs`, `Se`, `Ss`

### 선그리기 문자셋 — 3개

관련 명세: VT13. native 위임. 실제 glyph·encoding·reset 조합 전용 검증 없음.

`acsc`, `rmacs`, `smacs`

### 초기화·reset·화면 전환 — 5개

관련 명세: VT07, VT12, HX06. native 일반 경로 연결. 복합 sequence의 title stack/mode/palette 효과까지 entry별 미검증.

`is2`, `rs1`, `rs2`, `smcup`, `rmcup`

### cursor/application keypad 전환 — 2개

관련 명세: IN02. native mode-aware encoder 존재. frontend의 물리 keypad 식별 손실 때문에 전체 지원 아님.

`rmkx`, `smkx`

### F-key 광고 — 63개

관련 명세: IN02. kf1–kf63은 수정 F-key 조합을 포함한다. named encoder F1–F12 및 raw fallback 존재; 광고 63개 전수 왕복 미검증.

`kf1`, `kf10`, `kf11`, `kf12`, `kf13`, `kf14`, `kf15`, `kf16`, `kf17`, `kf18`, `kf19`, `kf2`, `kf20`, `kf21`, `kf22`, `kf23`, `kf24`, `kf25`, `kf26`, `kf27`, `kf28`, `kf29`, `kf3`, `kf30`, `kf31`, `kf32`, `kf33`, `kf34`, `kf35`, `kf36`, `kf37`, `kf38`, `kf39`, `kf4`, `kf40`, `kf41`, `kf42`, `kf43`, `kf44`, `kf45`, `kf46`, `kf47`, `kf48`, `kf49`, `kf5`, `kf50`, `kf51`, `kf52`, `kf53`, `kf54`, `kf55`, `kf56`, `kf57`, `kf58`, `kf59`, `kf6`, `kf60`, `kf61`, `kf62`, `kf63`, `kf7`, `kf8`, `kf9`

### 키패드 입력 — 18개

관련 명세: IN02. 물리 keypad 일부를 일반 키로 축소하는 결손. raw fallback으로 일부 보존되더라도 application-keypad 의미 전체 보장 없음.

`ka1`, `ka3`, `kb2`, `kc1`, `kc3`, `kent`, `ka2`, `kb1`, `kb3`, `kc2`, `kp5`, `kpADD`, `kpCMA`, `kpDIV`, `kpDOT`, `kpMUL`, `kpSUB`, `kpZRO`

### 일반·수정 navigation 입력 — 75개

관련 명세: IN01, IN02. Shift/Alt/Ctrl·navigation 일부 연결. 실제 물리 키와 child mode/광고 byte 조합의 전수 검증 없음.

`kDC`, `kEND`, `kHOM`, `kIC`, `kLFT`, `kNXT`, `kPRV`, `kRIT`, `kbeg`, `kbs`, `kcbt`, `kcub1`, `kcud1`, `kcuf1`, `kcuu1`, `kdch1`, `kend`, `khome`, `kich1`, `kind`, `knp`, `kpp`, `kri`, `kDC3`, `kDC4`, `kDC5`, `kDC6`, `kDC7`, `kDN`, `kDN3`, `kDN4`, `kDN5`, `kDN6`, `kDN7`, `kEND3`, `kEND4`, `kEND5`, `kEND6`, `kEND7`, `kHOM3`, `kHOM4`, `kHOM5`, `kHOM6`, `kHOM7`, `kIC3`, `kIC4`, `kIC5`, `kIC6`, `kIC7`, `kLFT3`, `kLFT4`, `kLFT5`, `kLFT6`, `kLFT7`, `kNXT3`, `kNXT4`, `kNXT5`, `kNXT6`, `kNXT7`, `kPRV3`, `kPRV4`, `kPRV5`, `kPRV6`, `kPRV7`, `kRIT3`, `kRIT4`, `kRIT5`, `kRIT6`, `kRIT7`, `kUP`, `kUP3`, `kUP4`, `kUP5`, `kUP6`, `kUP7`

### mouse — 3개

관련 명세: IN07. SGR/X10 등 일부 outer decoder와 native encoder 연결. 실제 WT 입력·모드 전이·좌표 전수 미검증.

`kmous`, `XM`, `xm`

### focus — 4개

관련 명세: IN06. 연결. 실제 WT↔다른 창·popup 전환의 entry별 왕복 미검증.

`fd`, `fe`, `kxIN`, `kxOUT`

### bracketed paste — 4개

관련 명세: IN04. 연결 및 scripted paste 증거 있음. 실제 OS paste·경계 크기·mode 전이 조합 미검증.

`BD`, `BE`, `PE`, `PS`

### clipboard — 1개

관련 명세: HX08. Python broker 대체 구현. 쓰기 연결, 읽기는 host5522 등 조건부. 실제 Windows clipboard 결과 미검증.

`Ms`

### 질의·응답 패턴 — 8개

관련 명세: HX01, HX02. WRITE_PTY 연결, 고정 DA. 일부 기대값은 XTerm 식별 패턴이므로 ish 식별과 정합성 미검증. XTVERSION callback 미연결.

`u6`, `u7`, `u8`, `u9`, `RV`, `rv`, `XR`, `xr`

### bell·visual bell — 2개

관련 명세: HX05, VT09. BEL 연결 및 reverse-video native 위임. 실제 WT 소리/visual flash·타이밍 미검증.

`bel`, `flash`

### 레거시 peripheral·meta — 7개

관련 명세: HX02, VT16. 프린터·memory lock·8-bit meta의 지원은 선언하지 않음. 실제 처리 및 광고 조정 필요; 파서 복구는 기본 요구.

`mc0`, `mc4`, `mc5`, `meml`, `memu`, `smm`, `rmm`

## 원래 값 전체

아래 `\E`, `^G`, `\007` 등은 infocmp의 **텍스트 표기**다. 실제 escape byte가 아니며 그대로 터미널에 보내는 테스트 fixture로 사용하지 않는다. 매개변수 확장과 독립 기대값을 별도로 준비해야 한다. `rv`, `xr`처럼 응답을 표현하는 패턴도 출력 명령으로 취급하지 않는다.

```text
#	Reconstructed via infocmp from file: /usr/share/terminfo/x/xterm-256color
xterm-256color|xterm with 256 colors,
	OTbs,
	am,
	bce,
	ccc,
	km,
	mc5i,
	mir,
	msgr,
	npc,
	xenl,
	AX,
	XF,
	XT,
	colors#0x100,
	cols#80,
	it#8,
	lines#24,
	pairs#0x10000,
	acsc=``aaffggiijjkkllmmnnooppqqrrssttuuvvwwxxyyzz{{||}}~~,
	bel=^G,
	blink=\E[5m,
	bold=\E[1m,
	cbt=\E[Z,
	civis=\E[?25l,
	clear=\E[H\E[2J,
	cnorm=\E[?12l\E[?25h,
	cr=\r,
	csr=\E[%i%p1%d;%p2%dr,
	cub=\E[%p1%dD,
	cub1=^H,
	cud=\E[%p1%dB,
	cud1=\n,
	cuf=\E[%p1%dC,
	cuf1=\E[C,
	cup=\E[%i%p1%d;%p2%dH,
	cuu=\E[%p1%dA,
	cuu1=\E[A,
	cvvis=\E[?12;25h,
	dch=\E[%p1%dP,
	dch1=\E[P,
	dim=\E[2m,
	dl=\E[%p1%dM,
	dl1=\E[M,
	ech=\E[%p1%dX,
	ed=\E[J,
	el=\E[K,
	el1=\E[1K,
	flash=\E[?5h$<100/>\E[?5l,
	home=\E[H,
	hpa=\E[%i%p1%dG,
	ht=^I,
	hts=\EH,
	ich=\E[%p1%d@,
	il=\E[%p1%dL,
	il1=\E[L,
	ind=\n,
	indn=\E[%p1%dS,
	initc=\E]4;%p1%d;rgb:%p2%{255}%*%{1000}%/%2.2X/%p3%{255}%*%{1000}%/%2.2X/%p4%{255}%*%{1000}%/%2.2X\E\\,
	invis=\E[8m,
	is2=\E[!p\E[?3;4l\E[4l\E>,
	kDC=\E[3;2~,
	kEND=\E[1;2F,
	kHOM=\E[1;2H,
	kIC=\E[2;2~,
	kLFT=\E[1;2D,
	kNXT=\E[6;2~,
	kPRV=\E[5;2~,
	kRIT=\E[1;2C,
	ka1=\EOw,
	ka3=\EOy,
	kb2=\EOu,
	kbeg=\EOE,
	kbs=^?,
	kc1=\EOq,
	kc3=\EOs,
	kcbt=\E[Z,
	kcub1=\EOD,
	kcud1=\EOB,
	kcuf1=\EOC,
	kcuu1=\EOA,
	kdch1=\E[3~,
	kend=\EOF,
	kent=\EOM,
	kf1=\EOP,
	kf10=\E[21~,
	kf11=\E[23~,
	kf12=\E[24~,
	kf13=\E[1;2P,
	kf14=\E[1;2Q,
	kf15=\E[1;2R,
	kf16=\E[1;2S,
	kf17=\E[15;2~,
	kf18=\E[17;2~,
	kf19=\E[18;2~,
	kf2=\EOQ,
	kf20=\E[19;2~,
	kf21=\E[20;2~,
	kf22=\E[21;2~,
	kf23=\E[23;2~,
	kf24=\E[24;2~,
	kf25=\E[1;5P,
	kf26=\E[1;5Q,
	kf27=\E[1;5R,
	kf28=\E[1;5S,
	kf29=\E[15;5~,
	kf3=\EOR,
	kf30=\E[17;5~,
	kf31=\E[18;5~,
	kf32=\E[19;5~,
	kf33=\E[20;5~,
	kf34=\E[21;5~,
	kf35=\E[23;5~,
	kf36=\E[24;5~,
	kf37=\E[1;6P,
	kf38=\E[1;6Q,
	kf39=\E[1;6R,
	kf4=\EOS,
	kf40=\E[1;6S,
	kf41=\E[15;6~,
	kf42=\E[17;6~,
	kf43=\E[18;6~,
	kf44=\E[19;6~,
	kf45=\E[20;6~,
	kf46=\E[21;6~,
	kf47=\E[23;6~,
	kf48=\E[24;6~,
	kf49=\E[1;3P,
	kf5=\E[15~,
	kf50=\E[1;3Q,
	kf51=\E[1;3R,
	kf52=\E[1;3S,
	kf53=\E[15;3~,
	kf54=\E[17;3~,
	kf55=\E[18;3~,
	kf56=\E[19;3~,
	kf57=\E[20;3~,
	kf58=\E[21;3~,
	kf59=\E[23;3~,
	kf6=\E[17~,
	kf60=\E[24;3~,
	kf61=\E[1;4P,
	kf62=\E[1;4Q,
	kf63=\E[1;4R,
	kf7=\E[18~,
	kf8=\E[19~,
	kf9=\E[20~,
	khome=\EOH,
	kich1=\E[2~,
	kind=\E[1;2B,
	kmous=\E[<,
	knp=\E[6~,
	kpp=\E[5~,
	kri=\E[1;2A,
	mc0=\E[i,
	mc4=\E[4i,
	mc5=\E[5i,
	meml=\El,
	memu=\Em,
	mgc=\E[?69l,
	nel=\EE,
	oc=\E]104\007,
	op=\E[39;49m,
	rc=\E8,
	rep=%p1%c\E[%p2%{1}%-%db,
	rev=\E[7m,
	ri=\EM,
	rin=\E[%p1%dT,
	ritm=\E[23m,
	rmacs=\E(B,
	rmam=\E[?7l,
	rmcup=\E[?1049l\E[23;0;0t,
	rmir=\E[4l,
	rmkx=\E[?1l\E>,
	rmm=\E[?1034l,
	rmso=\E[27m,
	rmul=\E[24m,
	rs1=\Ec\E]104\007,
	rs2=\E[!p\E[?3;4l\E[4l\E>,
	sc=\E7,
	setab=\E[%?%p1%{8}%<%t4%p1%d%e%p1%{16}%<%t10%p1%{8}%-%d%e48;5;%p1%d%;m,
	setaf=\E[%?%p1%{8}%<%t3%p1%d%e%p1%{16}%<%t9%p1%{8}%-%d%e38;5;%p1%d%;m,
	sgr=%?%p9%t\E(0%e\E(B%;\E[0%?%p6%t;1%;%?%p5%t;2%;%?%p2%t;4%;%?%p1%p3%|%t;7%;%?%p4%t;5%;%?%p7%t;8%;m,
	sgr0=\E(B\E[m,
	sitm=\E[3m,
	smacs=\E(0,
	smam=\E[?7h,
	smcup=\E[?1049h\E[22;0;0t,
	smglp=\E[?69h\E[%i%p1%ds,
	smglr=\E[?69h\E[%i%p1%d;%p2%ds,
	smgrp=\E[?69h\E[%i;%p1%ds,
	smir=\E[4h,
	smkx=\E[?1h\E=,
	smm=\E[?1034h,
	smso=\E[7m,
	smul=\E[4m,
	tbc=\E[3g,
	u6=\E[%i%d;%dR,
	u7=\E[6n,
	u8=\E[?%[;0123456789]c,
	u9=\E[c,
	vpa=\E[%i%p1%dd,
	BD=\E[?2004l,
	BE=\E[?2004h,
	Cr=\E]112\007,
	Cs=\E]12;%p1%s\007,
	E3=\E[3J,
	Ms=\E]52;%p1%s;%p2%s\007,
	PE=\E[201~,
	PS=\E[200~,
	RV=\E[>c,
	Se=\E[2 q,
	Ss=\E[%p1%d q,
	XM=\E[?1006;1000%?%p1%{1}%=%th%el%;,
	XR=\E[>0q,
	fd=\E[?1004l,
	fe=\E[?1004h,
	kDC3=\E[3;3~,
	kDC4=\E[3;4~,
	kDC5=\E[3;5~,
	kDC6=\E[3;6~,
	kDC7=\E[3;7~,
	kDN=\E[1;2B,
	kDN3=\E[1;3B,
	kDN4=\E[1;4B,
	kDN5=\E[1;5B,
	kDN6=\E[1;6B,
	kDN7=\E[1;7B,
	kEND3=\E[1;3F,
	kEND4=\E[1;4F,
	kEND5=\E[1;5F,
	kEND6=\E[1;6F,
	kEND7=\E[1;7F,
	kHOM3=\E[1;3H,
	kHOM4=\E[1;4H,
	kHOM5=\E[1;5H,
	kHOM6=\E[1;6H,
	kHOM7=\E[1;7H,
	kIC3=\E[2;3~,
	kIC4=\E[2;4~,
	kIC5=\E[2;5~,
	kIC6=\E[2;6~,
	kIC7=\E[2;7~,
	kLFT3=\E[1;3D,
	kLFT4=\E[1;4D,
	kLFT5=\E[1;5D,
	kLFT6=\E[1;6D,
	kLFT7=\E[1;7D,
	kNXT3=\E[6;3~,
	kNXT4=\E[6;4~,
	kNXT5=\E[6;5~,
	kNXT6=\E[6;6~,
	kNXT7=\E[6;7~,
	kPRV3=\E[5;3~,
	kPRV4=\E[5;4~,
	kPRV5=\E[5;5~,
	kPRV6=\E[5;6~,
	kPRV7=\E[5;7~,
	kRIT3=\E[1;3C,
	kRIT4=\E[1;4C,
	kRIT5=\E[1;5C,
	kRIT6=\E[1;6C,
	kRIT7=\E[1;7C,
	kUP=\E[1;2A,
	kUP3=\E[1;3A,
	kUP4=\E[1;4A,
	kUP5=\E[1;5A,
	kUP6=\E[1;6A,
	kUP7=\E[1;7A,
	ka2=\EOx,
	kb1=\EOt,
	kb3=\EOv,
	kc2=\EOr,
	kp5=\EOE,
	kpADD=\EOk,
	kpCMA=\EOl,
	kpDIV=\EOo,
	kpDOT=\EOn,
	kpMUL=\EOj,
	kpSUB=\EOm,
	kpZRO=\EOp,
	kxIN=\E[I,
	kxOUT=\E[O,
	rmxx=\E[29m,
	rv=\E\\[>41;[1-6][0-9][0-9];0c,
	smxx=\E[9m,
	xm=\E[<%i%p3%d;%p1%d;%p2%d;%?%p4%tM%em%;,
	xr=\EP>\\|XTerm\\(([1-9][0-9]+)\\)\E\\\\,
```
