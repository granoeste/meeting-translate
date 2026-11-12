import json
import sys
from datetime import datetime
from pathlib import Path


class Recorder:
    """Append every event to a .jsonl as it happens; export .txt/.srt on close."""

    def __init__(self, out_dir="transcripts"):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.base = self.dir / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.jsonl = self.base.with_suffix(".jsonl")
        self._f = open(self.jsonl, "a", encoding="utf-8")

    def write(self, ev):
        self._f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._f.flush()  # survive a crash

    def close(self):
        self._f.close()
        return export(self.jsonl)


def _load(path):
    with open(path, encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    return sorted(events, key=lambda e: e["start"])


def _ts(sec, sep):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


def export(jsonl_path):
    jsonl_path = Path(jsonl_path)
    events = _load(jsonl_path)
    txt, srt = jsonl_path.with_suffix(".txt"), jsonl_path.with_suffix(".srt")

    with open(txt, "w", encoding="utf-8") as f:
        for e in events:
            f.write(f"[{_ts(e['start'], '.')[:-4]}] {e['speaker']}\n")
            f.write(f"  {e['text']}\n  -> {e['translation']}\n\n")

    with open(srt, "w", encoding="utf-8") as f:
        for i, e in enumerate(events, 1):
            end = max(e["end"], e["start"] + 1.0)
            f.write(f"{i}\n{_ts(e['start'], ',')} --> {_ts(end, ',')}\n")
            f.write(f"[{e['speaker']}] {e['translation']}\n{e['text']}\n\n")
    return txt, srt


if __name__ == "__main__":
    # regenerate .txt/.srt from a .jsonl, e.g. after a crash
    for p in sys.argv[1:]:
        print(*export(p))
