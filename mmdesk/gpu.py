"""Make the pip-installed CUDA DLLs discoverable on Windows.

ctranslate2 loads cublas64_12.dll lazily at the first encode, and Windows resolves
that DLL's own dependencies through PATH — not through os.add_dll_directory alone.
Both are therefore set here, before faster_whisper is imported. Import this module
first, or call `enable_cuda_dlls()` before constructing a GPU model.
"""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path

MARKER = "_MMDESK_CUDA_DLLS"


def cuda_bin_dirs() -> list[Path]:
    dirs: list[Path] = []
    roots: list[str] = []
    try:
        roots += site.getsitepackages()
    except Exception:  # noqa: BLE001 - some embedded interpreters have no site.py
        pass
    try:
        roots.append(site.getusersitepackages())
    except Exception:  # noqa: BLE001
        pass
    for base in roots:
        nv = Path(base) / "nvidia"
        if nv.is_dir():
            dirs += [d for d in nv.glob("*/bin") if d.is_dir()]
    return dirs


def enable_cuda_dlls() -> list[Path]:
    if os.environ.get(MARKER):
        return cuda_bin_dirs()
    dirs = cuda_bin_dirs()
    if not dirs:
        return []
    # PATH first: this is what resolves cublasLt -> cublas dependencies.
    os.environ["PATH"] = os.pathsep.join(str(d) for d in dirs) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        for d in dirs:
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass
    os.environ[MARKER] = "1"
    return dirs


def cuda_available() -> bool:
    try:
        from ctranslate2 import get_cuda_device_count
    except Exception:  # noqa: BLE001
        return False
    try:
        return get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    print("python:", sys.version.split()[0])
    print("bin dirs:", [str(d) for d in enable_cuda_dlls()])
    print("cuda devices:", cuda_available())
