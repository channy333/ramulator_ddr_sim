"""Independent NumPy functional checks of the C decoder, not a pretrained GPT-2 accuracy test."""
import argparse
import json
import struct
import subprocess
from pathlib import Path
import numpy as np

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    root=Path(__file__).resolve().parent
    parser.add_argument("--binary",type=Path,default=root/"bin/gpt2_cpu")
    parser.add_argument("--out",type=Path,default=root/"tiny_fixture")
    a=parser.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    rng=np.random.default_rng(20261110)
    v,ctx,d,l,h,f=31,16,8,2,2,20
    eps=1e-5
    def rand(shape,scale=.1):return (rng.standard_normal(shape)*scale).astype(np.float32)
    def norm_params():return (1+rand((d,),.03),rand((d,),.03))
    wte,wpe=rand((v,d)),rand((ctx,d))
    lnf_g,lnf_b=norm_params()
    layers=[]
    for _ in range(l):
        g1,b1=norm_params();g2,b2=norm_params()
        layers.append((g1,b1,rand((3*d,d)),rand((3*d,)),rand((d,d)),rand((d,)),
                       g2,b2,rand((f,d)),rand((f,)),rand((d,f)),rand((d,))))
    checkpoint=a.out/"tiny.bin"
    with checkpoint.open("wb") as out:
        out.write(struct.pack("<8s8If",b"G2CPU001",1,v,ctx,d,l,h,f,0,eps))
        for x in (wte,wpe,lnf_g,lnf_b,*[x for layer in layers for x in layer]):
            out.write(np.asarray(x,dtype="<f4").tobytes())
    def ln(x,g,b):
        mean=x.mean(axis=-1,keepdims=True)
        variance=((x-mean)**2).mean(axis=-1,keepdims=True)
        return (x-mean)/np.sqrt(variance+eps)*g+b
    def reference(ids,blocks):
        x=wte[ids]+wpe[np.arange(len(ids))]
        for block in layers[:blocks]:
            g1,b1,qw,qb,ow,ob,g2,b2,fw,fb,pw,pb=block
            z=ln(x,g1,b1)@qw.T+qb
            q,k,value=np.split(z,3,axis=-1)
            q=q.reshape(len(ids),h,d//h).transpose(1,0,2)
            k=k.reshape(len(ids),h,d//h).transpose(1,0,2)
            value=value.reshape(len(ids),h,d//h).transpose(1,0,2)
            scores=(q@k.transpose(0,2,1))/np.sqrt(np.float32(d//h))
            mask=np.triu(np.ones((len(ids),len(ids)),dtype=bool),1)
            scores=np.where(mask[None],-np.inf,scores)
            weights=np.exp(scores-scores.max(axis=-1,keepdims=True))
            weights/=weights.sum(axis=-1,keepdims=True)
            attn=(weights@value).transpose(1,0,2).reshape(len(ids),d)
            x=x+attn@ow.T+ob
            u=ln(x,g2,b2)@fw.T+fb
            u=.5*u*(1+np.tanh(np.sqrt(2/np.pi)*(u+.044715*u**3)))
            x=x+u@pw.T+pb
        logits=ln(x,lnf_g,lnf_b)[-1]@wte.T
        return x.astype(np.float32),logits.astype(np.float32)
    ids=[1,7,3]
    tokens=a.out/"tokens.txt";tokens.write_text("1 7 3\n")
    report=[]
    for blocks,mode in ((1,"hidden"),(2,"hidden"),(2,"logits")):
        path=a.out/f"actual.{mode}_{blocks}.f32"
        cmd=[str(a.binary.resolve()),"--model",str(checkpoint.resolve()),
             "--tokens",str(tokens.resolve()),"--blocks",str(blocks),
             "--mode",mode,"--output",str(path.resolve())]
        subprocess.run(cmd,check=True)
        hidden,logits=reference(ids,blocks)
        expected=hidden.reshape(-1) if mode=="hidden" else logits
        actual=np.fromfile(path,dtype="<f4")
        np.testing.assert_allclose(actual,expected,atol=3e-6,rtol=3e-5)
        error=float(np.max(np.abs(actual-expected)))
        report.append({"mode":mode,"blocks":blocks,"max_abs_error":error})
        expected.astype("<f4").tofile(a.out/f"reference.{mode}_{blocks}.f32")
    prefix=a.out/"prefix.txt";prefix.write_text("1 7\n")
    prefix_out=a.out/"prefix.f32"
    subprocess.run([str(a.binary.resolve()),"--model",str(checkpoint.resolve()),
                    "--tokens",str(prefix.resolve()),"--mode","hidden","--output",str(prefix_out.resolve())],check=True)
    full=np.fromfile(a.out/"actual.hidden_2.f32",dtype="<f4").reshape(3,d)
    partial=np.fromfile(prefix_out,dtype="<f4").reshape(2,d)
    np.testing.assert_allclose(full[:2],partial,atol=1e-7,rtol=1e-7)
    # Malformed input must fail rather than silently producing a prediction.
    invalid=a.out/"invalid.txt";invalid.write_text(str(v)+"\n")
    rejected=subprocess.run([str(a.binary.resolve()),"--model",str(checkpoint.resolve()),
                             "--tokens",str(invalid.resolve())],capture_output=True)
    assert rejected.returncode!=0
    (a.out/"validation.json").write_text(json.dumps({"functional_checks":report,
        "causal_prefix_check":"pass","invalid_token_rejected":True,
        "trained_checkpoint_validation":False},indent=2)+"\n")
    print("PASS: independent full/partial decoder checks, causal prefix, invalid token rejection")
    print(f"Fixture: {a.out}")

if __name__=="__main__":main()
