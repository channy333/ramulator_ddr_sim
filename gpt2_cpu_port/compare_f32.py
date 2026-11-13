"""Compare raw little-endian FP32 inference outputs."""
import argparse
import numpy as np

p=argparse.ArgumentParser(description=__doc__)
p.add_argument("reference")
p.add_argument("actual")
p.add_argument("--atol",type=float,default=3e-4)
p.add_argument("--rtol",type=float,default=3e-4)
a=p.parse_args()
x=np.fromfile(a.reference,dtype="<f4")
y=np.fromfile(a.actual,dtype="<f4")
if x.size==0 or x.shape!=y.shape or not np.isfinite(x).all() or not np.isfinite(y).all():
    raise SystemExit("FAIL: empty, mismatched, or non-finite output")
error=float(np.max(np.abs(x-y)))
print(f"elements={x.size} max_abs_error={error:.9g} argmax_reference={x.argmax()} argmax_actual={y.argmax()}")
if not np.allclose(x,y,atol=a.atol,rtol=a.rtol):
    raise SystemExit("FAIL: numerical mismatch")
print("PASS")
