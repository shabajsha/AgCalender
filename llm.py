"""The one place that calls Ollama, with the guards that keep the laptop usable.

Used by extractor.py (reading emails) and ranker.py (ordering work). Any refusal or failure raises
LLMUnavailable so the caller can retry later or fall back to a rule.

GPU off (power-saver mode, or Ollama restarted without CUDA): the model would run on the CPU - slow, hot and
6 GB of RAM. The first time that is seen, the model is unloaded, you get one alert, and for GPU_RETRY_AFTER no
further calls are made at all (emails wait; the planner orders work by due date). Then it tries once more.
"Check mail now" in Telegram also tries again straight away (e.g. after `sudo systemctl restart ollama`).
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
import ollama

log = logging.getLogger(__name__)
GPU_FLAG = "llm_cpu_only_since"      # state.meta: when the model was last found running without the GPU
GPU_RETRY_AFTER = timedelta(hours=6)
MIN_GPU_SHARE = 0.3                   # below this share of the model in VRAM it's effectively running on the CPU
MAX_ANSWER_TOKENS = 700               # a JSON answer is ~50-300 tokens; stops a runaway generation


class LLMUnavailable(Exception):
    """Ollama could not be reached, or RAM/GPU state made it unwise to call it. Temporary: retry later."""


class LLMTimeout(Exception):
    """The model didn't answer within timeout_s. Counted against the email, so one that always times out is
    eventually given up on instead of costing minutes of GPU time every run."""


def available_ram_gb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    return float("inf")


def _gpu_share(model):
    """Share of the loaded model that sits in VRAM, or None if it isn't loaded."""
    for m in ollama.ps().models:
        if m.model == model:
            return (m.size_vram or 0) / m.size if m.size else 0.0
    return None


def _state(state):
    if state is not None:
        return state
    from state import State  # here, not at the top: plain LLM users shouldn't need the database
    return State()


def gpu_lost_since(state=None):
    raw = _state(state).get_meta(GPU_FLAG)
    return datetime.fromisoformat(raw) if raw else None


def clear_gpu_flag(state=None):
    """Try the GPU again at the next call (after restarting Ollama, or switching power mode back)."""
    _state(state).set_meta(GPU_FLAG, "")


def _gpu_lost(llm, state, share):
    import alerts  # here, not at the top: alerts -> state/telegram shouldn't load for every LLM user
    try:
        ollama.generate(model=llm["model"], prompt="", keep_alive=0)  # unload now
    except Exception:  # noqa: BLE001 - unloading is best effort
        pass
    state.set_meta(GPU_FLAG, datetime.now(timezone.utc).isoformat())
    log.error("Ollama ran %s without the GPU (%.0f%% in VRAM); unloaded it, no LLM calls for %d h. "
              "Fix: sudo systemctl restart ollama (or leave power-saver mode)", llm["model"], share * 100,
              GPU_RETRY_AFTER.seconds // 3600)
    alerts.alert("gpu", "The NVIDIA GPU isn't available to Ollama (power-saver mode, or Ollama lost CUDA), so the model "
                        f"would run on the CPU. Emails wait and plans use due-date order; I'll try again in "
                        f"{GPU_RETRY_AFTER.seconds // 3600} h. Fix: sudo systemctl restart ollama, then tap 'Check mail now'.",
                 state)


def chat_json(llm, system, user, state=None):
    """Returns the model's raw JSON text. `llm` is the `ollama:` section of config.yaml."""
    require_gpu = llm.get("require_gpu", True)
    if require_gpu:
        state = _state(state)
        lost = gpu_lost_since(state)
        if lost and datetime.now(timezone.utc) - lost < GPU_RETRY_AFTER:
            raise LLMUnavailable(f"GPU unavailable since {lost.astimezone():%H:%M}; waiting before trying again")
    # gemma2:9b spills ~2 GB into system RAM; with the browser open and swap full the kernel
    # OOM-killed Ollama (26 Sep). Better to postpone than to trigger that.
    min_ram = llm.get("min_free_ram_gb", 3)
    if (free := available_ram_gb()) < min_ram:
        raise LLMUnavailable(f"only {free:.1f} GB RAM available (need {min_ram})")
    try:
        if require_gpu and (share := _gpu_share(llm["model"])) is not None and share < MIN_GPU_SHARE:
            _gpu_lost(llm, state, share)  # already loaded on the CPU: don't run an inference to find out
            raise LLMUnavailable("Ollama is running the model without the GPU")
        resp = ollama.Client(timeout=llm.get("timeout_s", 300)).chat(
            model=llm["model"],
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            format="json",
            options={"temperature": 0, "num_ctx": llm.get("num_ctx", 2048), "num_thread": llm.get("num_thread", 4),
                     "num_predict": MAX_ANSWER_TOKENS},
            keep_alive=llm.get("keep_alive", "30s"),
        )
        share = _gpu_share(llm["model"]) if require_gpu else None
    except httpx.TimeoutException as e:
        raise LLMTimeout(f"no answer within {llm.get('timeout_s', 300)} s") from e
    except (ConnectionError, ollama.ResponseError, OSError, httpx.TransportError) as e:
        raise LLMUnavailable(str(e)) from e

    # After an OOM kill (26 Sep) Ollama restarted without detecting the NVIDIA GPU and ran gemma2:9b
    # fully on the CPU: 6 GB of RAM and a hot laptop. Don't keep doing that silently.
    if require_gpu and share is not None:
        if share < MIN_GPU_SHARE:
            _gpu_lost(llm, state, share)
        elif gpu_lost_since(state):
            clear_gpu_flag(state)
            import alerts
            alerts.resolved("gpu", state)
    return resp["message"]["content"]
