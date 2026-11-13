# GPT-2 CPU reference port for gem5 + Ramulator DDR5

## 목적과 검증 범위

이 패키지는 업로드한 PIM용 `gpt2.c`의 GPT-2 연산 구조를 일반 CPU에서 실제로 계산하도록 재구성한 기준 구현이다.
Aladdin/PIM 호출, 지연을 만들기 위한 반복문, 기존 TensorFlow raw checkpoint/BPE loader는 사용하지 않는다.
기존 파일과 빌드 스크립트는 덮어쓰지 않는다. 이 결과는 CPU DDR5 workload이며 GDDR6-AiM PIM 성능의 재현 결과가 아니다.

지원 연산: FP32 embedding + positional embedding, pre-layernorm, QKV projection, causal multi-head self-attention, attention output projection, GELU-new FFN, residual, final layernorm, tied LM head.
batch=1의 prefill forward를 지원한다. KV cache와 autoregressive generation loop는 포함하지 않는다.
`logits` 모드는 전체 decoder와 최종 입력 토큰의 vocabulary logits를 계산한다.
`hidden` 모드는 선택한 수의 block을 거친 hidden states를 최종 layernorm 전에 반환한다. Block 수를 줄인 실행은 부분 연산 실험이다.

검증한 것:

- GCC 정적 x86-64 빌드
- 작은 2-block 모델의 전체 logits 및 1/2-block hidden state를 독립 NumPy 구현과 비교
- Causal mask: 뒤쪽 토큰을 추가해도 이전 토큰들의 hidden state가 바뀌지 않음
- 잘못된 token ID 거절

아직 이 제작 환경에서 검증하지 않은 것:

- 실제 pretrained GPT-2-small 가중치와 Hugging Face 출력의 일치
- gem5 통합 실행 및 optional m5 ROI 경로

실제 가중치가 첨부되지 않았고 이 환경에는 PyTorch/Transformers가 설치되지 않아, 위 두 검증은 아래 절차로 사용자 환경에서 진행해야 한다.
작은 모델 검증 결과는 `validation/tiny_validation.json`에 있다.

## 1. 배치와 CPU 빌드

이 폴더를 현재 Ramulator 루트의 `/workspace/gpt2_cpu_port`에 둔다.
모든 경로는 기존 dev container 기준이다. 기존 gem5 wrapper 및 빌드는 그대로 사용한다.

```bash
cd /workspace/gpt2_cpu_port
bash build.sh
python verify_tiny.py
```

두 번째 명령으로 약 7 KiB의 synthetic checkpoint와 검증용 token 파일을 `tiny_fixture/` 아래에 생성한다.
`verify_tiny.py`에는 NumPy만 필요하다. 기존 Ramulator 가상환경에 없다면 그 환경에 `python -m pip install numpy`로 추가한다.

## 2. 먼저 tiny decoder를 gem5에서 실행

```bash
cd /workspace
tools/gem5/build/X86/gem5.opt \
    --outdir=/workspace/results/gpt2_tiny \
    gpt2_cpu_port/gem5_gpt2_se.py \
    --binary /workspace/gpt2_cpu_port/bin/gpt2_cpu \
    --cpu timing --cache l1l2 --record-commands -- \
    --model /workspace/gpt2_cpu_port/tiny_fixture/tiny.bin \
    --tokens /workspace/gpt2_cpu_port/tiny_fixture/tokens.txt \
    --output /workspace/results/gpt2_tiny/logits.f32
```

```bash
python /workspace/gpt2_cpu_port/compare_f32.py \
    /workspace/gpt2_cpu_port/tiny_fixture/reference.logits_2.f32 \
    /workspace/results/gpt2_tiny/logits.f32
```

이 tiny model은 구조·연결 검증용 합성 가중치이다. GPT-2-small의 실제 정확도/성능 결과로 사용하지 않는다.

## 3. 실제 GPT-2-small 가중치를 host Python에서 export

현재 업로드한 `download_model.sh`는 활성화된 다운로드가 774M이고, 코드의 설정은 124M이다.
기존 `.ckpt`에 TensorFlow metadata/index가 없으면 파일명만으로 tensor 순서를 보장할 수 없다.
이번 패키지는 명시적인 형식으로 다시 export하여 loader와 수치 결과를 함께 검증하는 경로를 제공한다.

가중치 준비/토크나이징/정답 계산은 host Python에서 한다. 이 단계는 gem5 성능 측정이 아니다.
기존 Ramulator 환경과 분리해서 다음처럼 Python 환경을 만들 수 있다.

```bash
python3 -m venv /workspace/gpt2_export_venv
/workspace/gpt2_export_venv/bin/python -m pip install numpy transformers safetensors
/workspace/gpt2_export_venv/bin/python -m pip install torch --index-url https://download.pytorch.org/whl/cpu

/workspace/gpt2_export_venv/bin/python /workspace/gpt2_cpu_port/export_hf_gpt2.py \
    --model openai-community/gpt2 \
    --prompt "Hello" \
    --reference-blocks 1 \
    --out /workspace/workloads/gpt2/models
```

처음에는 모델 다운로드가 필요하다. GPT-2-small export 파일은 약 498 MB다.
인터넷 다운로드 대신 Hugging Face 형식의 로컬 모델 디렉터리를 `--model`에 지정할 수도 있다.
재현 실험에서는 `--revision <commit>`을 지정한다. 요청한 revision, 실제 resolved commit, 라이브러리 버전, prompt/token IDs는 `metadata.json`에 기록한다.

출력:

- `gpt2_cpu.bin`: 모델 tensor를 저장한 little-endian FP32 checkpoint
- `tokens.txt`: 토크나이징을 완료한 입력 token IDs
- `reference.logits.f32`: Hugging Face의 마지막 입력 토큰 logits
- `reference.hidden_1.f32`: 첫 block 후 hidden states, final LN 이전
- `metadata.json`: 모델·입력·환경·예측 정보

기존 `vocab.bpe`의 불완전한 C tokenizer를 사용하지 않는다. Python tokenizer 결과를 C에 token IDs로 전달한다.

## 4. 실제 가중치로 native 수치 검증

```bash
cd /workspace/gpt2_cpu_port
bin/gpt2_cpu \
    --model /workspace/workloads/gpt2/models/gpt2_cpu.bin \
    --tokens /workspace/workloads/gpt2/models/tokens.txt \
    --output /workspace/workloads/gpt2/models/native.logits.f32

python compare_f32.py \
    /workspace/workloads/gpt2/models/reference.logits.f32 \
    /workspace/workloads/gpt2/models/native.logits.f32
```

`PASS`와 next-token ID를 확인한다. 차이가 허용 오차를 넘으면 performance 실험으로 넘어가기 전에 tensor layout/연산/라이브러리 차이를 확인한다.
이 C 구현은 비교하기 쉬운 scalar reference이다. 최적화된 PyTorch/BLAS와 같은 instruction stream이나 performance를 재현하지 않는다.

## 5. 학습 가중치의 첫 Transformer block부터 gem5 실행 확인

전체 GPT-2-small의 scalar CPU 실행은 gem5에서 오래 걸릴 수 있다.
처음에는 같은 D=768, heads=12, FF=3072의 학습된 첫 block을 짧은 입력으로 실행한다.
전체 checkpoint를 읽지만 forward는 첫 block만 수행한다. 따라서 이 실행은 GPT-2 전체 inference 성능이 아니다.
아래의 기본 실행 시간에는 checkpoint 로딩도 포함된다. Forward 성능 비교에는 6절의 ROI를 적용한다.

```bash
cd /workspace
tools/gem5/build/X86/gem5.opt \
    --outdir=/workspace/results/gpt2_block1 \
    gpt2_cpu_port/gem5_gpt2_se.py \
    --binary /workspace/gpt2_cpu_port/bin/gpt2_cpu \
    --cpu timing --cache l1l2 -- \
    --model /workspace/workloads/gpt2/models/gpt2_cpu.bin \
    --tokens /workspace/workloads/gpt2/models/tokens.txt \
    --mode hidden --blocks 1 \
    --output /workspace/results/gpt2_block1/hidden.f32
```

```bash
python /workspace/gpt2_cpu_port/compare_f32.py \
    /workspace/workloads/gpt2/models/reference.hidden_1.f32 \
    /workspace/results/gpt2_block1/hidden.f32
```

`--record-commands`는 필요할 때 설정 파일 인자에 추가한다. 모델 로딩을 포함한 전체 trace는 커질 수 있다.
전체 forward는 workload 인자의 `--mode hidden --blocks 1`을 제거해 기본 `logits` 모드로 실행한다.

## 6. Optional: forward-only ROI

모델 로딩을 포함한 전체 실행 통계를 inference 지연으로 해석하지 않는다.
다음은 선택 기능이며, tiny fixture로 m5 이벤트와 결과 통계를 먼저 확인한다.

```bash
cd /workspace/tools/gem5/util/m5
scons build/x86/out/libm5.a

cd /workspace/gpt2_cpu_port
GEM5_ROOT=/workspace/tools/gem5 WITH_M5=1 bash build.sh
```

`gpt2_cpu_m5`는 `--roi`를 전달했을 때만 special instruction을 실행한다.
일반 컴퓨터에서 native 실행할 때는 `--roi`를 사용하지 않는다.

```bash
cd /workspace
tools/gem5/build/X86/gem5.opt \
    --outdir=/workspace/results/gpt2_tiny_roi \
    gpt2_cpu_port/gem5_gpt2_se.py \
    --binary /workspace/gpt2_cpu_port/bin/gpt2_cpu_m5 \
    --cpu timing --cache l1l2 --expect-roi -- \
    --model /workspace/gpt2_cpu_port/tiny_fixture/tiny.bin \
    --tokens /workspace/gpt2_cpu_port/tiny_fixture/tokens.txt --roi
```

ROI 동작:

1. 모델 로딩, token 읽기, workspace 할당을 수행한다.
2. `m5_work_begin`에서 CPU/cache/Ramulator 통계를 reset한다. Cache를 flush하는 것은 아니다.
3. Embedding부터 선택한 block 또는 full logits까지 forward를 수행한다. 반복마다 결과 1개를 volatile 변수로 소비한다.
4. `m5_work_end`에서 gem5 시뮬레이션을 종료한다. 이후 C의 checksum 출력·결과 파일 쓰기·free는 실행되지 않는다.
5. 따라서 출력 일치 검증은 먼저 ROI 없이 수행한다. ROI 모드는 성능 측정 단계다.

`roi.json`의 tick 범위, `stats.txt`, `ramulator_stats.yaml`과 command counter를 확인한다.
`CmdTraceRecorder`는 reset 시 기존 내용을 지우지 않으므로 전체 실행 기록을 보유한다. ROI별 명령 개수에는 reset된 counter/statistics를 사용한다.
ROI 진입 시점에 이전 cache prefetch 등이 남아 있을 수 있다. 연구 실험에서는 warm-up/cold-cache/진입 시 in-flight 처리 정책을 고정해야 한다.

## 7. 현재 MLP 실행 분석

이번 `config(1).ini`는 `mlp_smoke 1`, TimingSimpleCPU, L1I/D 32 KiB, L2 256 KiB, DDR5 AllBank 구성이다.

| 지표 | 값 |
| --- | ---: |
| gem5 시뮬레이션 시간 | 0.557677431 ms |
| CPU instructions | 1,090,566 |
| CPU IPC | 0.651198 |
| DRAM read / write | 3,893 / 13 |
| REFab / REFsb | 142 / 0 |
| 평균 controller read latency | 88.1567 cycles = 36.673 ns |
| DRAM row hit | 90.4506% |
| L1D overall hit | 99.6265% |
| L1I overall hit | 99.9461% |

명령 카운트와 trace의 횟수가 일치한다. `floor(1,340,571 / 9,375) = 142`로 refresh 생성 횟수도 맞는다.
PREab가 114회이고 REFab가 142회인 것은 매 refresh마다 새 precharge가 필요한 것은 아니기 때문이다.
DRAM read 중 2,783회(약 71.49%)는 L1D/L2 prefetcher 요청이다. Current default cache hierarchy에 stride prefetchers가 있다.
단순히 L2 demand miss 수를 DRAM read 수와 동일시하면 안 된다.
현재 통계는 모델 초기화/프로그램 종료를 포함하며 inference-only ROI가 없다.
이전 Hello/NoCache 수치와 workload와 cache가 모두 다르므로 성능 개선율을 계산하지 않는다.

## 8. Checkpoint format

Header: Python struct `<8s8If`, 44 bytes.
Magic=`G2CPU001`; version=1; vocab, context, dim, layers, heads, inner, flags=0; FP32 layernorm epsilon.
Payload는 little-endian FP32이며 다음 순서다.

1. WTE `[vocab,dim]`, WPE `[context,dim]`, final LN gamma/beta `[dim]`
2. Layer 번호 0부터 순서대로: LN1 gamma/beta, QKV weight `[3*dim,dim]`/bias, attention output weight `[dim,dim]`/bias, LN2 gamma/beta, FF input weight `[inner,dim]`/bias, FF output weight `[dim,inner]`/bias

Conv1D weights는 exporter가 input-major에서 output-major로 transpose한다. 헤더나 tensor 순서를 다른 형식과 혼용하지 않는다.

## 공식 참고 자료

- GPT-2-small config: https://huggingface.co/openai-community/gpt2/blob/main/config.json
- Hugging Face GPT-2: https://huggingface.co/docs/transformers/model_doc/gpt2
- gem5 M5ops: https://www.gem5.org/documentation/general_docs/m5ops/
- Ramulator gem5 wrapper: https://github.com/CMU-SAFARI/ramulator2/tree/7bead6cb7c4b03e095c6d4769ae3181a8e0bf6da/resources/gem5_wrappers

제작일: 2026-11-10. 상세 소스 감사는 SOURCE_AUDIT.md를 참고한다.
