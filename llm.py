"""The one place that calls Ollama, with the guards that keep the laptop usable.

Used by extractor.py (reading emails) and ranker.py (ordering work). Any refusal or failure raises
LLMUnavailable so the caller can retry later or fall back to a rule.
"""
import logging

import httpx
import ollama

log = logging.getLogger(__name__)
_cpu_only = False  # set when Ollama is found running without the GPU; stops further calls this run


class LLMUnavailable(Exception):
    """Ollama could not be reached, or RAM/GPU state made it unwise to call it."""


def available_ram_gb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    return float("inf")


def _on_gpu(model):
    return any(m.model == model and (m.size_vram or 0) > 0 for m in ollama.ps().models)


def chat_json(llm, system, user):
    """Returns the model's raw JSON text. `llm` is the `ollama:` section of config.yaml."""
    global _cpu_only
    if _cpu_only:
        raise LLMUnavailable("Ollama is running without the GPU")
    # gemma2:9b spills ~2 GB into system RAM; with the browser open and swap full the kernel
    # OOM-killed Ollama (26 Sep). Better to postpone than to trigger that.
    min_ram = llm.get("min_free_ram_gb", 3)
    if (free := available_ram_gb()) < min_ram:
        raise LLMUnavailable(f"only {free:.1f} GB RAM available (need {min_ram})")
    try:
        resp = ollama.Client(timeout=llm.get("timeout_s", 300)).chat(
            model=llm["model"],
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            format="json",
            options={"temperature": 0, "num_ctx": llm.get("num_ctx", 2048), "num_thread": llm.get("num_thread", 4)},
            keep_alive=llm.get("keep_alive", "30s"),
        )
    except (ConnectionError, ollama.ResponseError, OSError, httpx.TransportError) as e:
        raise LLMUnavailable(str(e)) from e

    # After an OOM kill (26 Sep) Ollama restarted without detecting the NVIDIA GPU and ran gemma2:9b
    # fully on the CPU: 6 GB of RAM and a hot laptop. Don't keep doing that silently.
    if llm.get("require_gpu", True) and not _on_gpu(llm["model"]):
        _cpu_only = True
        ollama.generate(model=llm["model"], prompt="", keep_alive=0)  # unload now
        log.error("Ollama ran %s without the GPU; unloaded it and paused LLM calls for this run. "
                  "Fix: sudo systemctl restart ollama", llm["model"])
        import alerts  # here, not at the top: alerts -> state/telegram shouldn't load for every LLM user
        alerts.alert("gpu", "Ollama lost the NVIDIA GPU and would run the model on the CPU (hot, 6 GB RAM), so "
                            "emails are waiting unread. Fix: sudo systemctl restart ollama")
    return resp["message"]["content"]
