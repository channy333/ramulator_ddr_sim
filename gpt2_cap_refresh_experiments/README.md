# CAP와 REFsb bank 선택 실험

목표: **CAP 때문에 이미 닫기로 한 row의 precharge를 REFsb의 PREsb와 공유하면 불필요한 닫기/다시 열기를 줄일 수 있는가?**

과거 접근 횟수로 미래 재사용 여부를 예측하는 실험이 아니다. 이전 `gpt2_row_reuse_probe` 대신 이 패키지의 실행 파일을 사용한다. 기존 GPT-2 모델·C 코드·m5 바이너리는 그대로 사용한다.

## 1. 설치

압축을 풀어 아래처럼 배치한다. 모든 명령은 Dev Container 안에서 실행한다.

```text
/workspace/gpt2_cap_refresh_experiments/
/workspace/gpt2_cpu_port/
/workspace/workloads/gpt2/models/
/workspace/src/ramulator/
```

```bash
cd /workspace/gpt2_cap_refresh_experiments
bash setup.sh
python verify_native.py --root /workspace
```

`setup.sh`는 새 C++ 파일 3개와 헤더 1개를 추가하고, `ControllerBase`에 friend 선언 2개 및 CMake 소스 목록을 추가한다. 원래 `ClosedCAP`, `Open`, `DDR5SameBank`, FRFCFS, DDR5 timing 및 controller tick 구현은 수정하지 않는다. 바뀌는 기존 파일은 `/workspace/.cap_refresh_backups/`에 먼저 백업한다. 이미 적용한 동일 내용은 다시 쓰지 않는다.

빌드는 기본 2개 작업만 사용한다. `CMAKE_BIN`, `PYTHON_BIN`, `BUILD_JOBS`로 변경할 수 있다. 기존 Ramulator 빌드가 없는 경우만 configure한다. 기존 gem5가 `/workspace/libramulator.so`를 연결하는 구성이면 gem5 자체를 다시 빌드할 필요는 없다. 실행 중 새 implementation을 찾지 못하면 다른 Ramulator 라이브러리를 로드하고 있는지 확인한다.

대상은 Ramulator upstream `7bead6cb7c4b03e095c6d4769ae3181a8e0bf6da`의 controller API와 이 프로젝트의 gem5 ROI 연동이다. 설치 전 변경한 파일을 원래 소스로 덮어쓰지 않는다. 소스 구조가 다른 경우 설치 스크립트가 중단한다. Python 환경에는 PyYAML이 필요하다.

## 2. 비교 조건

| 이름 | Row 정책 | REFsb bank-ID 선택 | CAP PRE 공유 |
|---|---|---|---|
| `open_rr` | Open | round-robin | 해당 없음 |
| `cap_rr` | CAP(H) | round-robin | 끔 |
| `cap_rr_share` | 동일 CAP(H) | round-robin | 켬 |
| `cap_leastq` | 동일 CAP(H) | 대기 요청 수 최소 | 켬 |
| `cap_closed` | 동일 CAP(H) | 이미 닫힌 bank 수 최대 | 켬 |
| `cap_aware` | 동일 CAP(H) | 닫힌 bank + CAP 닫기 준비 bank 수 최대 | 켬 |

주요 비교:

- `cap_rr → cap_rr_share`: bank 선택을 고정한 상태에서 PRE 공유 효과.
- `cap_rr_share → cap_closed`: 이미 닫힌 bank를 고르는 효과.
- `cap_closed → cap_aware`: **CAP 정보를 추가한 효과.** 이것이 같으면 CAP 정보의 추가 이득을 보인 것이 아니다.
- `cap_leastq → cap_aware`: queue 기반 비교군 대비 효과. AMD 정책의 모든 구현을 재현한 것은 아니다.
- `open_rr → cap_rr`: 이 workload에서 CAP 자체가 주는 손익. CAP가 없는 기존 결과를 CAP 선택 정책의 직접 baseline으로 삼지 않는다.

`cap_leastq`는 read/write/active buffer의 해당 bank들 요청을 합산한다. prefetch도 들어 있으며 CPU demand만 세는 정책은 아니다. `cap_aware`의 score는 8개 BG 전체를 합산한다. 같은 점수면 닫힌 bank가 더 많은 그룹을, 다시 같으면 회전 cursor 순서로 선택한다. 미래 요청이나 오프라인 trace를 보지 않는다.

## 3. CAP 의미와 구현 범위

H 기본값은 **실험 변수 16**이다. 상용 controller의 고정값이라는 뜻이 아니다. `ClosedCAPExperiment(share_precharge=false)`는 위 upstream `ClosedCAP`의 DDR5 동작을 복제했고 명령 순서 동일성을 검사했다.

- ACT부터 다음 close까지 각 bank의 column access를 센다. READ/WRITE 포함, CAP는 row conflict가 없어도 적용한다.
- count ≥ H이면 timing이 허용할 때 explicit PREpb를 priority queue에 넣는다.
- 그 전에 다음 RD/WR가 선택되고 AP timing이 허용되면 RDA/WRA로 바꿀 수 있다.
- 따라서 **항상 정확히 H번째 접근 직후 닫히는 엄격한 상한은 아니다.** 기존 ClosedCAP의 AP/PRE timing과 controller 우선순위를 유지한다.
- DRAMsim3의 경쟁 row conflict가 있을 때 row-hit 우선권을 제한하는 방식과 의미가 다르다.

`cap_close_ready`는 열린 bank에서 count ≥ H이고 자기 PRE가 이미 대기 중이거나 지금 PRE timing이 준비된 경우다. score에는 이 bank만 더한다. H에 도달했지만 PRE timing이 아직 준비되지 않은 bank는 현재 버전의 CAP 후보로 세지 않는다.

선택한 REFsb를 queue에 넣으면 공유 옵션은 그 REFsb가 담당하는 bank들의 **이 row policy가 만든 PREpb만** 제거한다. 이미 REFsb가 기다리는 bank에는 새 CAP PREpb를 넣지 않는다. 기존 PREsb prerequisite가 닫기를 수행한다. 다른 유지보수 명령, 사용자 명령, 일반 요청은 제거하지 않는다. 물리 timing을 줄이지 않는다.

첫 버전은 **정해진 refresh 슬롯에서 bank를 고르는 실험**이다. CAP 발생 시점까지 refresh를 미루거나 당기지 않는다. 따라서 CAP 후보가 거의 없을 수도 있다. 그 경우 CAP-aware 효과가 없다는 결과도 유효하다. CAP와 refresh 시간을 적극적으로 맞추는 방식은 refresh debt/기한 검증을 추가한 별도 실험으로 다룬다.

## 4. 짧은 확인부터 실행

먼저 gem5 없이 실제 Ramulator C++ controller를 쓰는 짧은 pilot:

```bash
cd /workspace/gpt2_cap_refresh_experiments
python pilot.py --root /workspace --cap 16 \
  --outdir /workspace/results/cap_pilot_H16
```

같은 도착 요청을 재생하며 queue가 차면 재시도한다. `long_row_hits`, `row_conflicts` 두 합성 workload를 6개 조건에서 실행한다. 이것은 **CPU/cache/prefetch를 포함한 GPT-2 성능 측정이 아니다.** 마지막 read 완료 시 종료하므로 마지막 refresh가 남을 수 있다.

이후 tiny 모델로 gem5 설정·ROI·파일 기록이 연결되는지 확인:

```bash
bash run_experiment.sh --workload tiny --policy all --cap 16 \
  --prefetch l1 --outdir /workspace/results/gpt2_cap_tiny_H16
```

tiny는 작아서 ROI 안에 REFsb나 CAP 후보가 없을 수 있다. 정책 성능을 판단하는 용도가 아니다. ROI 끝에서 중단하므로 logits 출력 파일이 없어도 정상이며 기존의 기능 검증을 대체하지 않는다.

실제 GPT-2 첫 block:

```bash
bash run_experiment.sh --workload block1 --policy all --cap 16 \
  --prefetch l1 --outdir /workspace/results/gpt2_cap_block1_H16
```

전체 GPT-2-small 1-token forward:

```bash
bash run_experiment.sh --workload full --policy all --cap 16 \
  --prefetch l1 --outdir /workspace/results/gpt2_cap_full_H16
```

`--policy all`은 6개 조건을 **순차 실행**한다. 하나만 실행하려면:

```bash
bash run_experiment.sh --workload full --policy cap_aware --cap 16 \
  --prefetch l1 --outdir /workspace/results/gpt2_CAPaware_H16_roi
```

출력 폴더는 매번 `--outdir`로 직접 정한다. 기본적으로 비어 있지 않은 폴더는 덮어쓰지 않는다. `all`이면 그 안에 조건별 하위 폴더를 만든다. 단일 정책이면 지정 폴더에 직접 저장한다. `--dry-run`은 명령만 출력하며 gem5/모델 실행이나 폴더 생성을 하지 않는다.

### 결과 요약 오류로 open_rr에서 멈춘 경우

초기 배포본은 `summarize_roi.py` 호출에 필수 인자 `--tck-ps 416`을 빠뜨려 첫 조건의 gem5 완료 후 중단했다. 수정본에서는 이 인자를 전달한다. C++ 코드는 바뀌지 않았으므로 이 수정에 재빌드는 필요 없다.

기존 `open_rr`의 ROI 결과를 다시 사용하고 나머지 5개 조건을 이어 실행하려면 원래 명령에 `--resume`을 붙인다.

```bash
bash run_experiment.sh --workload tiny --policy all --cap 16 \
  --prefetch l1 --outdir /workspace/results/gpt2_cap_tiny_H16 --resume

bash run_experiment.sh --workload block1 --policy all --cap 16 \
  --prefetch l1 --outdir /workspace/results/gpt2_cap_block1_H16 --resume
```

`--resume`은 기존 조건의 policy/H/CPU/cache/prefetch/binary/workload 인자와 ROI 통계 정합성을 먼저 확인한다. 완료한 조건은 gem5를 생략하고 요약·분석만 다시 수행한다. 아직 없는 조건은 새로 실행한다. 설정이 다르거나 gem5 자체가 완료되지 않은 불완전한 폴더는 덮어쓰지 않고 중단한다. 불완전한 조건 폴더는 별도 보관한 뒤 다시 실행해야 한다.

이미 완료한 단일 조건을 수동으로 요약하려면:

```bash
python summarize_roi.py /workspace/results/gpt2_cap_block1_H16/open_rr --tck-ps 416
python analyze.py /workspace/results/gpt2_cap_block1_H16/open_rr
```

`verify_runner.py --root /workspace`는 실제 실행 shell 및 summary CLI를 mock gem5와 연결하여 최초 실행, 이번 오류에서의 이어 실행, 원본 파일 보존, 설정 불일치 거부, 실패 전파 등을 검사한다. CPU 통계는 합성 값이고 DRAM 통계는 native C++ controller fixture이며 실제 gem5 성능 검증은 아니다. 결과는 배포본의 `runner_validation.json`에 포함한다.

`--prefetch l1|both|l2|none` 지원. 기본은 이전에 관찰한 L1D 효과를 확인하기 위한 `l1`이다. 예전 양쪽 prefetch ON 설정과 맞추려면 `both`로 지정한다. 같은 비교 안에서는 반드시 같은 값을 쓴다. L1I prefetch는 stock 설정을 유지한다.

모델을 다시 export할 필요 없다. root를 다른 곳에 두면 `RAMULATOR_ROOT`, binary 또는 gem5 위치가 다르면 `GPT2_BINARY`, `GEM5_BIN`으로 지정한다.

## 5. 환경과 refresh 조건

| 항목 | 설정 |
|---|---|
| workload | 기존 FP32 C GPT-2-small, D=768/L=12/H=12, batch 1, `Hello` 1 token |
| 측정 | m5 WORKBEGIN~WORKEND forward ROI; 초기화·출력 제외 |
| CPU | gem5 SE X86 TimingSimpleCPU, 1 core, 3 GHz |
| cache | L1I/L1D 32 KiB, L2 256 KiB, stock classic hierarchy |
| DRAM | DDR5_16Gb_x8 / DDR5_4800AN, tCK=416 ps |
| 구성 | 32-bit channel 1개, rank 1개, 8 BG × 4 banks, 8 GiB |
| controller | GenericDDR, FRFCFS, RoBaRaCoCh mapping |
| refresh | REFsb FGR cadence, bank IDs 0~3를 각 round에서 한 번씩 |

REFsb 하나는 **모든 BG에서 같은 bank ID인 8개 bank**를 대상으로 한다. bank 하나만 고르는 것이 아니다. 각 rank에 별도 round mask/cursor가 있다. 다음 슬롯 시각은 `ceil(event × nREFI / (2 × banks_per_BG))`; 현재 nREFI=9375 CK이므로 1171/1172 CK 간격, 평균 487.5 ns다. nRFCsb=312 CK를 그대로 쓴다. 해당 슬롯에 queue에 넣고 실제 발행은 기존 timing/우선순위를 따른다.

자동 온도 조절, retention bit 오류, on-die ECC, mode-register 전환, 새 refresh postponement/pull-in 정책, DDR5 power model을 구현한 것이 아니다. round mask는 커버리지를 관리하지만 이 코드만으로 모든 JEDEC 기한에 대한 형식 검증을 했다고 주장하지 않는다. `refsb_max_delay_ck`가 크면 발행 밀림부터 살펴본다.

Micron DDR5 Core Data Sheet Rev. E (11/2024), p.182 및 pp.185–186의 REFsb 순회/평균 간격 설명을 기존 FGR manager와 동일하게 적용한다:
https://www.avnet.com/wcm/connect/dacdfea7-999f-4ee0-b514-6f9e0bf68c6d/ddr5-sdram-core.pdf?MOD=AJPERES

## 6. 결과에서 볼 파일

| 파일 | 용도 |
|---|---|
| `comparison.csv` | 조건별 시간·ACT·bank precharge·CAP 기회 비교 |
| `cap_summary.json` | 한 조건의 기계 판독용 요약 |
| `summary.txt` | 기존 gem5/Ramulator ROI 통계 요약 |
| `cap_metrics.ch0.csv` | 실제 명령 발행을 관찰한 bank 단위 close 및 REFsb 지연 |
| `command_counts.csv` | RD/RDA/WR/WRA/ACT/PRE/REF 명령 수 |
| `ramulator_stats.<tick>.yaml` | ROI 끝의 controller/row policy/refresh manager 통계 |
| `stats.txt`, `roi.json` | gem5 지표와 ROI 시각 |
| `experiment.json`, `command.sh`, `terminal.log` | 설정·재현 명령·실행 기록 |

분석을 다시 실행하려면 `python analyze.py /workspace/results/gpt2_cap_full_H16`.

우선 아래 순서로 판단한다.

1. `cap_candidate_slot_pct`: 미방문 ID 중 CAP-ready bank가 하나라도 있던 슬롯 비율. 거의 0이면 현재 방식의 기회가 부족하다.
2. `selected_cap_ready_banks`: 실제 선택한 그룹의 CAP-ready bank 합계. REFsb 하나에 최대 8개다.
3. `cap_pre_cancelled`, `cap_pre_shared_bank_cycles`: 공유 동작이 실행되었는지 확인. 뒤 지표는 **억제한 bank-cycle**이며 절감한 PRE 명령 수가 아니다.
4. `ACT`, `effective_bank_precharges`, `cap_reopen_then_refresh_close`: 실제 동작 감소 여부. 마지막 지표는 CAP close → ACT → REFsb용 PREsb close가 관측된 횟수이며, 그 전체가 반드시 피할 수 있었다는 뜻은 아니다.
5. `roi_ms`, DRAM latency, row-hit rate: 전체 효과와 비용. `time_change_vs_cap_rr_pct`는 음수가 빠른 것이다.

PREsb 1개는 열린 bank 1~8개를 닫을 수 있고 이미 모두 닫혔으면 PREsb 자체가 필요 없다. 따라서 PRE 명령 수와 `effective_bank_precharges`를 구분한다. RDA/WRA의 implicit close도 effective count에 포함한다. **이 수치만으로 에너지 절감률을 계산하지 않는다.**

평균 read latency는 queue 대기를 포함한 controller 지표이며 CPU demand만의 지표가 아니다. 서로 실행시간이 다르면 REFsb 개수도 달라지므로 raw REF 개수만으로 refresh 강도를 비교하지 않는다.

ROI reset은 통계만 초기화한다. cache·열린 row·CAP lifetime count·실제 clock·refresh round/phase·대기 요청은 유지한다. observer의 CAP→재개방 진단 이력만 지워 pre-ROI 동작을 ROI 이득으로 세지 않는다. ROI에 발행된 refresh의 지연은 enqueue가 pre-ROI여도 실제 due→issue 시간을 포함한다. 초기화 동안 정책이 다르게 작동하므로 ROI 시작 cache/row/phase도 달라질 수 있다는 점은 비교 해석에 포함한다.

PRE 공유로 취소한 요청은 controller의 accepted-maintenance 수에 남고 served 수에는 들어가지 않는다. `cap_pre_cancelled`와 pending/pre-ROI 요청을 함께 보고 accepted와 served 차이를 해석해야 한다.

## 7. 다음 실험과 판단

먼저 H=16, prefetch=L1D만 ON으로 확인한다. `cap_aware`가 `cap_closed`와 같으면 CAP 추가 정보의 효과가 없는 것이다. CAP 후보가 실제 존재하고 ACT/bank-close/time에 일관된 차이가 생길 때 H=4·64 및 `--prefetch none`을 추가한다. 이 패키지에는 반복 실행 결과나 GPT-2 성능 이득이 미리 들어 있지 않다.

H=16 합성 pilot 두 종류에서는 CAP-ready 선택이 0이었고 `cap_closed`와 `cap_aware` 결과가 같았다. 이는 메커니즘 실패가 아니라 그 트래픽/시점에서 후보가 없었던 결과다. 별도의 H=2 경계 시점 검증에서는 CAP-ready ID를 선택하고 CAP PRE를 REFsb와 공유함을 확인했다. 실제 GPT-2에서도 기회가 없다면 큰 sweep을 계속하기 전에 refresh 시점 조정 또는 CAP 정의 변경의 필요성을 검토한다.

## 8. 검증 범위 및 수정할 코드

`validation.json`은 **실제 컴파일한 Ramulator C++ controller** 검증 결과다. RR의 기존 manager와의 명령 순서 일치, CAP copy의 H=1/4/16 일치 및 AP 경로, 2-rank round/cadence, traffic 중 round coverage, queue/closed/CAP 선택, 자기 PRE만 취소, ROI state 보존, effective close 계산 및 analyzer 정합성 등을 검사한다. analyzer의 gem5 metadata fixture는 합성 값이며 실제 gem5 실행이 아니다. 기존 AllBank/DDR5SameBank 회귀 테스트도 실행했다. 제작 환경에서 gem5/GPT-2 통합 실행은 하지 않았으므로 위 tiny 실행으로 사용자 환경의 연결을 확인한다.

| 설치 후 파일 | 수정할 내용 |
|---|---|
| `src/ramulator/controller/refresh/impl/ddr5_cap_refresh.cpp` | `score()` / `better()`의 다음 bank 선택 기준 |
| `src/ramulator/controller/rowpolicy/impl/closed_cap_experiment.cpp` | CAP count, 닫기 준비 판단, PRE 공유 |
| `src/ramulator/controller/plugin/impl/cap_experiment_stats.cpp` | 관측 지표 |
| `gpt2_cap_refresh_experiments/policies.py` | 비교군 조합 |
| `gpt2_cap_refresh_experiments/gem5_cap_se.py` | CPU/cache/DRAM 실험 설정 |
| `gpt2_cap_refresh_experiments/run_experiment.sh` | 실행 조합·입출력 경로 |

설치 후 직접 수정한 C++를 유지하려면 package `src/` 쪽에도 반영한 뒤 setup을 실행하거나 `cmake --build /workspace/build -j2`만 실행한다. setup은 package의 소스를 설치 위치에 다시 적용한다.
