"""What this machine can run.

Answers "which model should I pull" with a measurement instead of a guess.
The alternative is the user downloading nine gigabytes to discover it does not
fit, which is a slow way to learn something a query could have told them.

Detection is best-effort by design. No GPU is a valid answer, not an error -
CPU inference works, it is just slow enough that the recommendation changes.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

__all__ = ["GpuInfo", "HardwareProfile", "detect_hardware"]


@dataclass(frozen=True, slots=True)
class GpuInfo:
    name: str
    total_mib: int
    used_mib: int = 0

    @property
    def total_gb(self) -> float:
        return round(self.total_mib / 1024, 1)

    @property
    def free_gb(self) -> float:
        return round((self.total_mib - self.used_mib) / 1024, 1)


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    gpus: tuple[GpuInfo, ...]
    system_ram_gb: float
    detail: str = ""

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpus)

    @property
    def vram_gb(self) -> float:
        """VRAM of the largest single card.

        Not the sum. Ollama runs one model on one device unless told
        otherwise, so two 8GB cards do not host a model that needs twelve.
        """
        return max((g.total_gb for g in self.gpus), default=0.0)

    @property
    def usable_vram_gb(self) -> float:
        """What is left for weights.

        The KV cache, the compositor and whatever else is already resident all
        want VRAM, and a model sized to the nameplate figure will not load.
        """
        return round(max(self.vram_gb - 1.5, 0.0), 1)

    def summary(self) -> str:
        if not self.gpus:
            return f"no GPU detected, {self.system_ram_gb:.0f} GB RAM (CPU inference)"
        gpu = self.gpus[0]
        extra = f" (+{len(self.gpus) - 1} more)" if len(self.gpus) > 1 else ""
        return f"{gpu.name}, {gpu.total_gb} GB VRAM{extra}, {self.system_ram_gb:.0f} GB RAM"


def _nvidia_smi() -> tuple[GpuInfo, ...]:
    """Shell out rather than import a CUDA binding.

    ``nvidia-smi`` ships with the driver, so it is present wherever an NVIDIA
    GPU is usable. ``pynvml`` would be tidier and is one more dependency that
    fails to build on the machines least likely to have a GPU anyway.
    """
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return ()
    try:
        output = subprocess.run(  # noqa: S603 - fixed argv, resolved via which
            [
                binary,
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if output.returncode != 0:
        return ()

    gpus: list[GpuInfo] = []
    for line in output.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            gpus.append(GpuInfo(name=parts[0], total_mib=int(parts[1]), used_mib=int(parts[2])))
        except ValueError:
            continue
    return tuple(gpus)


def _system_ram_gb() -> float:
    import os

    # getattr rather than os.sysconf directly: the attribute does not exist on
    # Windows, so a type checker running there flags the call even though it is
    # guarded.
    sysconf = getattr(os, "sysconf", None)
    if sysconf is not None:
        try:
            return round(int(sysconf("SC_PHYS_PAGES")) * int(sysconf("SC_PAGE_SIZE")) / 1024**3, 1)
        except (ValueError, OSError):
            pass

    # Windows has no sysconf; ask the kernel directly rather than adding psutil
    # for one number.
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined,unused-ignore]
        return round(int(status.ullTotalPhys) / 1024**3, 1)
    except (AttributeError, OSError):
        return 0.0


def detect_hardware() -> HardwareProfile:
    """Look at the machine. Never raises."""
    gpus = _nvidia_smi()
    ram = _system_ram_gb()
    detail = "nvidia-smi" if gpus else "no NVIDIA GPU visible"
    return HardwareProfile(gpus=gpus, system_ram_gb=ram, detail=detail)
