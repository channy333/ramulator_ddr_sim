# GPT-2 cache / prefetch 비교 실행

기존 Ramulator 소스, GPT-2 C 바이너리, 가중치와 DDR5SameBank manager를 재사용한다.
새 폴더 안의 Python 설정 파일과 shell runner를 사용하므로 기존 실행 파일을 덮어쓰지 않는다.
캐시 구성 변경은 gem5 Python 설정이며 이 패키지 자체를 위해 gem5나 Ramulator를 재빌드할 필요는 없다.

## 배치

`gpt2_cache_experiments.zip`을 `/workspace`에 둔 뒤:

```bash
cd /workspace
unzip gpt2_cache_experiments.zip
bash gpt2_cache_experiments/run_gpt2_cache.sh --help
```

## 우선 실행할 prefetch OFF 비교

기존 L1/L2 캐시를 유지하고 L1D/L2 prefetcher만 끈다. L1I 설정은 유지한다.
`pair`는 REFno와 REFsb 두 조건을 순서대로 실행한다.

```bash
bash /workspace/gpt2_cache_experiments/run_gpt2_cache.sh pair \
    --cache l1l2 \
    --l1d-prefetch off --l2-prefetch off \
    --outdir /workspace/results/gpt2_noPF_compare
```

결과는 `gpt2_noPF_compare/off/`, `gpt2_noPF_compare/refsb/`에 저장한다.
상위 `comparison.txt`와 `comparison.json`에 REFsb-REFno 시간 차이와 REFno 대비 증가율을 기록한다.
같은 조건을 prefetch ON으로 새 설정에서 다시 확보하려면:

```bash
bash /workspace/gpt2_cache_experiments/run_gpt2_cache.sh pair \
    --cache l1l2 \
    --l1d-prefetch on --l2-prefetch on \
    --outdir /workspace/results/gpt2_PF_compare
```

한 조건만 실행하려면 `pair` 대신 `off`, `refsb`, `allbank`를 사용한다.
`both`는 REFab/REFno, `all`은 REFab/REFno/REFsb다.

## 캐시 자체의 구성

| 옵션 | 구성 |
| --- | --- |
| `--cache l1l2` | L1I/L1D 각 32 KiB + L2 256 KiB |
| `--cache l1` | L1I/L1D 각 32 KiB, L2 없음 |
| `--cache none` | L1/L2 없음, gem5 NoCache 사용 |

`l1`과 `l1l2`는 stock gem5 hierarchy를 사용한다. 해당 구조의 walker 연결도 함께 적용되므로 캐시 수준을 바꾼 실험에는 전체 hierarchy 변화가 포함된다.
L1 없이 L2만 사용하는 연결은 이 패키지에서 제공하지 않는다.
이 옵션은 시뮬레이션 시작 전 연결 구조 선택이며 실행 중 cache power gating/flush 모델이 아니다.

```bash
bash /workspace/gpt2_cache_experiments/run_gpt2_cache.sh off \
    --cache l1 --outdir /workspace/results/gpt2_REFno_l1only

bash /workspace/gpt2_cache_experiments/run_gpt2_cache.sh off \
    --cache none --outdir /workspace/results/gpt2_REFno_nocache
```

NoCache는 명령어·데이터 접근을 메모리로 보내므로 전체 GPT-2 실행은 매우 느려질 수 있다.
먼저 tiny fixture로 확인하려면 기존 tiny 모델/토큰 경로를 지정한다.

```bash
MODEL=/workspace/gpt2_cpu_port/tiny_fixture/tiny.bin \
TOKENS=/workspace/gpt2_cpu_port/tiny_fixture/tokens.txt \
bash /workspace/gpt2_cache_experiments/run_gpt2_cache.sh off \
    --cache none --outdir /workspace/results/tiny_nocache
```

## 개별 prefetch 스위치

`--l1d-prefetch on|off`, `--l2-prefetch on|off`를 독립적으로 선택한다.
기본은 둘 다 on이다. L1I prefetcher는 stock 설정을 유지한다.
존재하지 않는 캐시의 prefetch 설정은 적용 대상이 없다.
기존 on/on 결과와 off/off 결과에서 차이가 확인되면 on/off와 off/on을 각각 REFno/REFsb와 조합해 위치를 좁힌다.

## 기록과 적용 확인

- `terminal.log`, `command.sh`: 출력과 실행 명령
- `config.ini`, `stats.txt`, `ramulator_stats.yaml`, `command_counts.csv`, `roi.json`: 기존 통계
- `run_check.txt`: 실제 refresh manager, cache 유무, prefetch 연결, L1D/L2 demand miss 수·평균/총지연·prefetch 발행/유효 횟수
- 기존 `/workspace/gpt2_cpu_port/summarize_roi.py`가 있으면 `summary.txt`도 생성

`run_check.txt`는 요청한 캐시·prefetch 설정과 실제 `config.ini`가 다르면 실패 처리한다.
각 실험은 별도 gem5 프로세스로 실행한다. 결과가 있는 출력 폴더는 덮어쓰지 않는다.
`--dry-run`은 명령만 출력한다. `--record-commands`는 큰 trace를 만들 수 있으므로 필요할 때 사용한다.

Prefetch OFF에서는 DRAM 요청 수와 cache miss 수가 바뀔 수 있다.
같은 코드·입력·ROI를 유지하고 각 prefetch 상태 안에서 REFsb-REFno 차이를 비교한다.
전체 DRAM 평균에는 prefetch 요청도 포함되므로 CPU demand miss 지연과 구분한다.
ROI reset은 통계를 초기화하며 cache 내용과 in-flight 요청, refresh phase를 초기화하지 않는다.

## REFsb 순서/주기 문서와 코드

확인한 공개 upstream: CMU-SAFARI/ramulator2 `7bead6cb7c4b03e095c6d4769ae3181a8e0bf6da`.

Micron DDR5 Core Data Sheet Rev. E (11/2024):

- p.182, SAME BANK REFRESH Command: bank 순서는 자유롭지만 모든 bank ID를 처리한 뒤 다음 round로 진행한다. 모든 bank를 처리하기 전 같은 ID를 다시 지정하면 같은 refresh row를 반복한다.
- p.185, Table 182: 0~85°C에서 REFsb 평균 간격은 tREFI/(2*n). 현재 n=4이므로 0.4875 us.
- p.186, Refresh Operation Scheduling Flexibility: 평균 tREFI 및 명시된 제한 안에서 refresh 연기/집중 발행을 허용한다. 정확한 등간격 발행을 강제하는 규칙은 아니다.

https://www.avnet.com/wcm/connect/dacdfea7-999f-4ee0-b514-6f9e0bf68c6d/ddr5-sdram-core.pdf?MOD=AJPERES

기존 Ramulator 구현:

- `src/ramulator/dram/commands/REFsb.h`: `BankTarget::SameBank`; 대상 bank가 열려 있으면 PREsb를 prerequisite로 요구한다.
- `src/ramulator/dram/commands/PREsb.h`: 각 BG의 같은 bank ID를 precharge한다.
- `python/ramulator/dram/ddr5.py`: nRFCsb, nREFSBRD, nRP, nRRD, nFAW 등 timing을 정의한다. 기본 automatic refresh는 normal REFab이며 REFsb를 자동 스케줄하지 않는다고 명시한다.

지난번 추가한 구현:

- `src/ramulator/controller/refresh/impl/ddr5_same_bank.cpp`: rank별 bank ID 0,1,2,3을 순회한다. due tick은 ceil(event*nREFI/(2*banks_per_BG))로 잡아 fractional interval drift를 막는다.
- 주기마다 `priority_send()`로 요청을 넣는다. 실제 PREsb/REFsb 명령 발행은 기존 controller의 prerequisite·timing·우선순위 처리를 따른다.
- 순회 방식은 규격이 허용하는 순서 중 하나다. 온도별 정책 변경이나 명시적 refresh debt/pull-in/postponement 최적화를 추가한 구현은 아니다.
- 기존 REFsb 명령 함수 자체는 bank-ID round의 방문 이력이나 retention row counter를 구현하지 않는다. 이 baseline은 manager의 round-robin과 발행 주기로 coverage를 유지한다.

## 검증 범위

gem5 v25.1.0.1 공개 Python hierarchy의 실제 메서드 본문과 모의 포트로 cache 연결·prefetch 스위치를 검사했다.
Shell 구문, 인자 전달, 36개 단일 조건 조합, pair/both/all 비교, 출력 보호, 실제 설정 불일치 감지, 실패 전파를 mock gem5로 검사했다.
이 제작 환경에서 실제 gem5 바이너리를 실행한 것은 아니므로 첫 실행은 사용자 컨테이너에서 확인해야 한다.
실험 성능 수치는 이 패키지에 포함하지 않는다.
