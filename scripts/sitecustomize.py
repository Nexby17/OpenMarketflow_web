"""F-131b sitecustomize: preload typing_extensions at interpreter start.

Why: robot/py4 is prepended to sys.path by every robot script (legacy SDK dir) and contains
an ACL-broken typing_extensions.py (read denied, cannot be deleted/renamed even by the owner).
Any modern dependency that imports typing_extensions (pydantic, grpcio>=1.6x) then dies with
PermissionError before the robot even starts.

Preloading the good copy here (site-packages) puts it into sys.modules before any script runs,
so the broken py4 file is never opened. Harmless no-op for processes that do not need it.
"""

try:
    import typing_extensions  # noqa: F401
except Exception:  # never break interpreter startup
    pass
