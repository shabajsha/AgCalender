"""Benchmarks Ollama models on made-up emails with known answers (accuracy, speed, GPU fit, CPU temp).

    python eval_extractor.py                       # model from config.yaml
    python eval_extractor.py gemma3:4b qwen2.5:3b  # compare several

Uses the real prompt and validation from extractor.py. Writes nothing.
"""
import subprocess
import sys
import threading
import time
from datetime import datetime
from glob import glob
from pathlib import Path
from zoneinfo import ZoneInfo

import ollama

from config import load_config
from extractor import UnreadableAnswer, extract_items

TZ = ZoneInfo("Asia/Kolkata")
SAT = datetime(2026, 9, 26, 10, 9, tzinfo=TZ)
WED = datetime(2026, 9, 30, 18, 0, tzinfo=TZ)

# (received, subject, body, expected items as (is_deadline, "YYYY-MM-DD HH:MM" or "YYYY-MM-DD" for all-day))
CASES = [
    (SAT, "[DSA] Assignment 2 released",
     "Dear students,\nAssignment 2 is out on Moodle. Submit by next Friday 11:59 PM.\n"
     "Also, the quiz scheduled for tomorrow at 10 AM in H105 stands.\n-TA",
     [(True, "2026-10-02 23:59"), (False, "2026-09-27 10:00")]),
    (SAT, "Course update", "Viva slots: this Monday 2-5 PM in the lab.", [(False, "2026-09-28 14:00")]),
    (WED, "Course update", "Midsem for OS will be held on Oct 12 at 9:30 AM. Lab report due Friday.",
     [(False, "2026-10-12 09:30"), (True, "2026-10-02 23:59")]),
    (WED, "Course update", "Today's 3 PM class is cancelled and rescheduled to next Tuesday same time.",
     [(False, "2026-10-06 15:00")]),
    (WED, "Course update", "The 11 AM lab on Thursday is moved to Saturday, same time, same venue.",
     [(False, "2026-10-03 11:00")]),
    (WED, "Registrations are LIVE for Megathon X!",
     "Register now for the biggest hackathon! Meeting new people guaranteed. Deadline to register: soon!", []),
    (WED, "Club meeting minutes",
     "Thanks to everyone who attended last week's meeting. Minutes are attached. See you around!", []),
    (WED, "End Semester Examination schedule",
     "Dear all,\nThe End Semester Examination for CS1.301 Algorithms will be held on 21/11/2026 "
     "from 2:00 PM to 5:00 PM in the Himalaya exam hall.\nRegards,\nExam Cell",
     [(False, "2026-11-21 14:00")]),
    (WED, "Project proposal", "Reminder: the project proposal submission deadline is 5th October. Submit on Moodle.",
     [(True, "2026-10-05 23:59")]),
    (WED, "SE Project milestones",
     "Phase 1 report is due on October 8 at 5 PM, and the final presentation is on October 15 at 10 AM in SH1.",
     [(True, "2026-10-08 17:00"), (False, "2026-10-15 10:00")]),
    (WED, "Quiz 2 marks", "The quiz held yesterday has been graded. Marks are on Moodle.", []),
    (SAT, "Thesis discussion",
     "Hi, can we have a meeting tomorrow at 4:30 pm in my office to discuss your thesis draft?\n- Prof. Rao",
     [(False, "2026-09-27 16:30")]),
    (SAT, "Interhouse Sports Updates",
     "Hello everyone! Registrations for inter-house badminton close on Tuesday. Fill the form linked below. "
     "Trials will follow. Go team!",
     [(True, "2026-09-29 23:59")]),
]


def _key(item):
    when = item["due"] or item["start"]
    stamp = when.strftime("%Y-%m-%d %H:%M") if isinstance(when, datetime) else when.isoformat()
    return (item["type"] == "deadline", stamp)


def _package_temp_sensor():
    for hw in glob("/sys/class/hwmon/hwmon*"):
        if Path(hw, "name").read_text().strip() == "coretemp":
            return Path(hw, "temp1_input")
    return None


class TempMonitor:
    """Samples the CPU package temperature in the background; .peak holds the max seen."""

    def __init__(self):
        self.sensor, self.peak, self._stop = _package_temp_sensor(), 0.0, threading.Event()

    def read(self):
        return int(self.sensor.read_text()) / 1000 if self.sensor else 0.0

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                self.peak = max(self.peak, self.read())
                time.sleep(0.5)
        threading.Thread(target=loop, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


def cool_down(monitor, below=65, max_wait=90):
    start = time.time()
    while monitor.read() > below and time.time() - start < max_wait:
        time.sleep(2)


def gpu_split(model):
    for line in subprocess.run(["ollama", "ps"], capture_output=True, text=True).stdout.splitlines():
        if line.startswith(model):
            return " ".join(line.split()[4:6])  # "29%/71% CPU/GPU" or "100% GPU"
    return "?"


def evaluate(model, llm_base):
    llm = {**llm_base, "model": model, "keep_alive": "2m"}
    tp = fp = fn = 0
    failures, times, split = [], [], "?"
    probe = TempMonitor()
    cool_down(probe)
    with TempMonitor() as temps:
        for i, (received, subject, body, expected) in enumerate(CASES):
            msg = {"id": "eval", "subject": subject, "received": received, "sender": "someone@iiit.ac.in", "body": body}
            t = time.time()
            try:
                got = [_key(it) for it in extract_items(msg, llm, "Asia/Kolkata", 0.6, received)]
            except UnreadableAnswer:
                got = []  # invalid JSON from the model counts as "found nothing"
            times.append(time.time() - t)
            if i == 0:
                split = gpu_split(model)
            want = set(expected)
            hit = want & set(got)
            tp, fp, fn = tp + len(hit), fp + len(set(got) - want), fn + len(want - hit)
            if set(got) != want:
                failures.append(f"    {subject!r}: want {sorted(want)}, got {sorted(set(got))}")
    ollama.generate(model=model, prompt="", keep_alive=0)  # unload
    warm = times[1:]
    return {"model": model, "tp": tp, "fp": fp, "fn": fn, "load_s": times[0], "avg_s": sum(warm) / len(warm),
            "split": split, "peak_c": temps.peak, "failures": failures}


def main():
    cfg = load_config()
    models = sys.argv[1:] or [cfg["ollama"]["model"]]
    expected_total = sum(len(c[3]) for c in CASES)
    results = []
    for model in models:
        print(f"running {model} on {len(CASES)} emails ...", flush=True)
        results.append(evaluate(model, cfg["ollama"]))

    print(f"\n{'model':<14} {'correct':>9} {'wrong extra':>12} {'1st call':>9} {'per email':>10} {'peak CPU':>9}  where it ran")
    for r in results:
        print(f"{r['model']:<14} {r['tp']:>4}/{expected_total:<4} {r['fp']:>12} {r['load_s']:>8.1f}s {r['avg_s']:>9.1f}s "
              f"{r['peak_c']:>7.0f}°C  {r['split']}")
    for r in results:
        if r["failures"]:
            print(f"\n{r['model']} mistakes:")
            print("\n".join(r["failures"]))


if __name__ == "__main__":
    main()
