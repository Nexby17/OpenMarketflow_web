"""F-131b: launcher for DataProvider (:5060).

Why: DataProvider/main.py bootstrap does sys.path.insert(0, robot/py4) — legacy SDK dir,
which now contains an ACL-broken old typing_extensions.py (read denied). It shadows the
fresh typing_extensions and crashes pydantic_core on `from typing_extensions import Sentinel`.

Fix without touching code/ACL: preload typing_extensions from site-packages BEFORE main.py
runs its bootstrap, so the broken py4 copy is never imported (module is cached in sys.modules).
"""
import os
import runpy
import sys

DP = r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\DataProvider"

sys.path.insert(0, DP)
import typing_extensions  # noqa: E402,F401  (preload: shadows broken robot/py4 copy)

os.chdir(DP)
sys.argv = ["main.py"]
runpy.run_path(os.path.join(DP, "main.py"), run_name="__main__")
