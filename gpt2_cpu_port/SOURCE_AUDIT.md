# 업로드한 GPT-2 소스의 CPU 이식 점검

다음은 이번에 받은 `gpt2.c`와 관련 스크립트를 기준으로 확인한 사항이다.

| 위치 | 관찰 | CPU 기준 구현에서 처리 |
| --- | --- | --- |
| `gpt2.c:1` | 주석 시작 `/*`가 빠진 형태로 파일이 시작함 | 별도 정상 C 소스로 작성 |
| `gpt2.c:643` / `759` | `aim_linear_*`가 전달된 행렬로 실제 결과를 계산/반환하지 않음. 1-float in/out과 PIM 호출 횟수를 사용 | 실제 QKV/attention projection/FFN/LM head의 FP32 선형 연산 구현 |
| `gpt2.c:1094` 등 | PIM 호출 뒤 `NewMatrix`로 결과 영역을 별도로 만듦. 첨부 코드에서 수치 결과를 채우는 CPU 경로가 보이지 않음 | 각 선형 연산이 실제 output tensor를 작성 |
| `gpt2.c:1002` | 0으로 초기화한 `buf`의 +8 위치에 prompt를 복사하고 `tokenize(buf,...)`를 호출. 문자열 시작이 NUL로 남음 | HF tokenizer의 명시적 token ID 입력 |
| `gpt2.c:135` / `136` / `976` | `VOCAB_SIZE=5e4`와 `WTE_ROWS=50257`이 혼재하고 50,000행을 읽는 경로 | checkpoint header의 vocab size를 일관되게 사용 |
| `gpt2.c:537` | LayerNorm variance를 `cols-1`로 나눔 | GPT-2의 population variance를 사용 |
| `gpt2.c:879` / `887` | 약 7.25 GB arena와 별도 1 GB BPE 영역을 할당 | 실제 모델 tensors와 필요한 activations만 할당 |
| `gpt2.c:890` / `959` | `/root/.data/GPT-2` 절대 경로와 fread 결과 검사 부족 | 명시적 `--model`, `--tokens`, 형식/길이 검사 |
| `download_model.sh` | 활성 다운로드는 774M, 코드의 아키텍처는 124M | 기본 export 모델을 `openai-community/gpt2`로 지정하고 metadata 저장 |
| `Makefile`, `build.sh`, `run.sh` | LLVM-Tracer, 구형 gem5-Aladdin, DRAMsim3, Aladdin cfg에 의존 | 현재 gem5 25.1 / Ramulator의 일반 CPU binary 경로 사용 |

기존 지연 루프와 PIM 호출은 PIM timing/operation 모델의 구성요소로 취급한다.
이번 포트는 이를 일반 CPU 명령어로 실제 계산하는 새로운 기준 구현이다. 이전 실험의 PIM cycle 수와 동일한 CPU cycle 수를 만들도록 설계하지 않았다.
출력 값을 확인하지 않는 timing workload와 실제 학습 모델의 수치적 inference를 구분한다.

원래 raw TensorFlow 데이터 파일을 현재 FP32 checkpoint로 이름만 바꾸어 사용하면 안 된다.
가중치 tensor의 의미/순서/transpose를 exporter에서 명시했고, 실제 pretrained checkpoint 검증은 사용자 환경에서 별도로 수행해야 한다.
