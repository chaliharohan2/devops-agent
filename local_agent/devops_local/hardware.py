"""Hardware detection and local model selection.

Default developer machine: **20 GB RAM, CPU only, 512 GB disk**. The model has
to share RAM with the IDE, a browser and the Docker test environment it builds,
so the RAM budget for the model is deliberately conservative (see
docs/research/LOCAL_MODEL_SELECTION.md for the full analysis).
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True)
class ModelOption:
    key: str
    display: str
    source: str  # what `ollama pull` fetches
    params: str
    quant: str
    file_gb: float
    ram_gb: float  # resident estimate incl. KV cache at the default context
    context_tokens: int
    license: str
    min_total_ram_gb: float
    notes: str


# Ordered best-first. Sources are GGUF repos on Hugging Face pulled through Ollama
# (`ollama pull hf.co/<repo>:<quant>`), so they do not depend on Ollama library tags.
CATALOG: list[ModelOption] = [
    ModelOption(
        key="ornith-1.5-9b",
        display="Ornith 1.5 9B (Q4_K_M)",
        source="hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M",
        params="9B dense", quant="Q4_K_M", file_gb=5.6, ram_gb=7.5, context_tokens=16384, license="MIT",
        min_total_ram_gb=16,
        notes="Default. Agentic-coding model (vendor SWE-bench Verified 70.6), OpenAI-style tool calls, 262K native context.",
    ),
    ModelOption(
        key="gemma-4-12b",
        display="Gemma 4 12B Unified (Q4_K_M)",
        source="hf.co/unsloth/gemma-4-12b-it-GGUF:Q4_K_M",
        params="12B dense", quant="Q4_K_M", file_gb=7.3, ram_gb=9.5, context_tokens=16384, license="Apache-2.0",
        min_total_ram_gb=20,
        notes="Alternative with native function calling (Tau2 69%); slower on CPU than the 9B.",
    ),
    ModelOption(
        key="gemma-4-e4b",
        display="Gemma 4 E4B (Q4_K_M)",
        source="hf.co/unsloth/gemma-4-E4B-it-GGUF:Q4_K_M",
        params="~4B effective", quant="Q4_K_M", file_gb=3.0, ram_gb=4.5, context_tokens=16384, license="Apache-2.0",
        min_total_ram_gb=10,
        notes="Low-memory fallback; weaker tool calling, so the agent leans on deterministic routing.",
    ),
]


@dataclass
class Hardware:
    os: str
    arch: str
    cpu_cores: int
    cpu_threads: int
    cpu_flags: list[str]
    total_ram_gb: float
    available_ram_gb: float
    free_disk_gb: float
    gpu: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _cpu_flags() -> list[str]:
    flags: set[str] = set()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("flags"):
                flags.update(line.split(":", 1)[1].split())
                break
    except OSError:
        pass
    if platform.machine().lower() in {"arm64", "aarch64"}:
        flags.add("neon")
    return sorted(f for f in flags if f in {"avx", "avx2", "avx512f", "avx512_vnni", "avx_vnni", "f16c", "fma", "neon", "amx_tile"})


def _gpu() -> str | None:
    if shutil.which("nvidia-smi"):
        return "nvidia"
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "apple-silicon"
    return None


def detect(path: Path | None = None) -> Hardware:
    vm = psutil.virtual_memory()
    disk = shutil.disk_usage(str(path or Path.home()))
    return Hardware(
        os=f"{platform.system()} {platform.release()}",
        arch=platform.machine(),
        cpu_cores=psutil.cpu_count(logical=False) or os.cpu_count() or 1,
        cpu_threads=psutil.cpu_count(logical=True) or os.cpu_count() or 1,
        cpu_flags=_cpu_flags(),
        total_ram_gb=round(vm.total / 1024**3, 1),
        available_ram_gb=round(vm.available / 1024**3, 1),
        free_disk_gb=round(disk.free / 1024**3, 1),
        gpu=_gpu(),
    )


def recommend(hw: Hardware) -> tuple[ModelOption, list[str]]:
    """Pick the best catalog model that leaves room for IDE + browser + Docker."""
    notes: list[str] = []
    reserve = 9.0  # OS + IDE + browser (~5 GB) + a small Docker test env (~4 GB)
    budget = hw.total_ram_gb - reserve
    notes.append(f"{hw.total_ram_gb} GB RAM total; reserving {reserve:.0f} GB for OS/IDE/browser/Docker -> "
                 f"{budget:.1f} GB model budget")
    if "avx2" not in hw.cpu_flags and "neon" not in hw.cpu_flags:
        notes.append("CPU lacks AVX2: generation will be slow; prefer the smallest model.")
    if hw.free_disk_gb < 20:
        notes.append(f"Only {hw.free_disk_gb} GB disk free; models + Docker images need ~20 GB.")
    # Prefer the first model in catalog order (default = Ornith 9B) that fits.
    for m in CATALOG:
        if m.ram_gb <= budget and hw.total_ram_gb >= m.min_total_ram_gb:
            if m.key == "gemma-4-12b":
                continue  # alternative, opt-in only
            return m, notes
    notes.append("Very little RAM: using the smallest model.")
    return CATALOG[-1], notes


def by_key(key: str) -> ModelOption:
    for m in CATALOG:
        if m.key == key:
            return m
    raise KeyError(f"unknown model {key!r}; choose from {[m.key for m in CATALOG]}")
