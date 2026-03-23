"""
Shim: delegates to scripts/Spec_check.py so both root and scripts paths work.
"""
import os
import subprocess
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_main = os.path.join(_script_dir, "scripts", "Spec_check.py")

if not os.path.exists(_scripts_main):
    print("[ERROR] scripts/Spec_check.py not found. Fix: run from repo root or copy scripts/ into your project.", file=sys.stderr)
    sys.exit(1)

sys.exit(subprocess.call([sys.executable, _scripts_main] + sys.argv[1:]))
