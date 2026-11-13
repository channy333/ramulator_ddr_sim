"""Host-side export of trained GPT-2 weights and a tokenized prompt.

Run with host Python/PyTorch, outside the gem5 timing simulation.
Supports standard GPT-2 with gelu_new, tied embeddings, ordinary causal attention.
The produced format is independent of the uploaded legacy TensorFlow .ckpt.
"""
import argparse
import json
import struct
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model",default="openai-community/gpt2")
    p.add_argument("--revision",help="Hugging Face revision/commit, recorded in metadata")
    p.add_argument("--prompt",default="Hello")
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--reference-blocks",type=int,default=1)
    a=p.parse_args()
    import numpy as np
    import torch
    import transformers
    from transformers import AutoTokenizer, GPT2LMHeadModel

    torch.set_num_threads(1)
    load_args={"revision":a.revision} if a.revision else {}
    model=GPT2LMHeadModel.from_pretrained(a.model,attn_implementation="eager",**load_args).float().cpu().eval()
    tokenizer=AutoTokenizer.from_pretrained(a.model,**load_args)
    c=model.config
    if (c.activation_function!="gelu_new" or not c.tie_word_embeddings or
        not getattr(c,"scale_attn_weights",True) or
        getattr(c,"scale_attn_by_inverse_layer_idx",False) or
        getattr(c,"reorder_and_upcast_attn",False) or getattr(c,"add_cross_attention",False)):
        raise SystemExit("Unsupported GPT-2 variant; this exporter expects standard GPT-2 attention/GELU.")
    if not (1<=a.reference_blocks<=c.n_layer):
        raise SystemExit("Invalid --reference-blocks")
    inner=c.n_inner or 4*c.n_embd
    for block in model.transformer.h:
        if (tuple(block.attn.c_attn.weight.shape)!=(c.n_embd,3*c.n_embd) or
            tuple(block.mlp.c_fc.weight.shape)!=(c.n_embd,inner)):
            raise SystemExit("Unsupported linear-weight layout; expected GPT-2 Conv1D [in,out].")
    if not torch.equal(model.lm_head.weight,model.transformer.wte.weight):
        raise SystemExit("LM head and token embedding are not tied numerically.")
    ids=tokenizer.encode(a.prompt,add_special_tokens=False)
    if not ids or len(ids)>c.n_positions:
        raise SystemExit("Prompt token count is outside model context")
    a.out.mkdir(parents=True,exist_ok=True)
    params=model.state_dict()
    def array(name,transpose=False):
        t=params[name]
        if transpose:t=t.t()
        return t.detach().cpu().contiguous().numpy().astype("<f4",copy=False)
    checkpoint=a.out/"gpt2_cpu.bin"
    temporary=checkpoint.with_suffix(".bin.tmp")
    with temporary.open("wb") as f:
        f.write(struct.pack("<8s8If",b"G2CPU001",1,c.vocab_size,c.n_positions,
                            c.n_embd,c.n_layer,c.n_head,inner,0,c.layer_norm_epsilon))
        for name in ("transformer.wte.weight","transformer.wpe.weight",
                     "transformer.ln_f.weight","transformer.ln_f.bias"):
            f.write(array(name).tobytes())
        for i in range(c.n_layer):
            base=f"transformer.h.{i}."
            for suffix,transpose in (
                ("ln_1.weight",False),("ln_1.bias",False),
                ("attn.c_attn.weight",True),("attn.c_attn.bias",False),
                ("attn.c_proj.weight",True),("attn.c_proj.bias",False),
                ("ln_2.weight",False),("ln_2.bias",False),
                ("mlp.c_fc.weight",True),("mlp.c_fc.bias",False),
                ("mlp.c_proj.weight",True),("mlp.c_proj.bias",False),
            ):
                f.write(array(base+suffix,transpose).tobytes())
    temporary.replace(checkpoint)
    (a.out/"tokens.txt").write_text(" ".join(map(str,ids))+"\n")
    captured={}
    def hook(_module,_inputs,output):
        value=output[0] if isinstance(output,(tuple,list)) else output
        captured["hidden"]=value[0].detach().cpu().numpy().astype("<f4",copy=True)
    handle=model.transformer.h[a.reference_blocks-1].register_forward_hook(hook)
    with torch.inference_mode():
        result=model(input_ids=torch.tensor([ids],dtype=torch.long),use_cache=False)
    handle.remove()
    logits=result.logits[0,-1].detach().cpu().numpy().astype("<f4",copy=False)
    logits.tofile(a.out/"reference.logits.f32")
    captured["hidden"].tofile(a.out/f"reference.hidden_{a.reference_blocks}.f32")
    predicted=int(logits.argmax())
    metadata={"model":a.model,"requested_revision":a.revision,
              "resolved_commit":getattr(c,"_commit_hash",None),
              "torch_version":torch.__version__,"transformers_version":transformers.__version__,
              "prompt":a.prompt,"token_ids":ids,"next_token_id":predicted,
              "next_token_text":tokenizer.decode([predicted]),"config":c.to_dict(),
              "reference_blocks":a.reference_blocks,"checkpoint_bytes":checkpoint.stat().st_size,
              "linear_weight_layout":"output-major [out,in]"}
    (a.out/"metadata.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2,default=str)+"\n")
    print(f"Exported {checkpoint}; token_ids={ids}; reference next_token_id={predicted}")
    print("Host inference/export complete; no gem5 performance was measured here.")

if __name__=="__main__":main()
