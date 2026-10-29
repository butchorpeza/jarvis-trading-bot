import json
import time


def log(entry: dict, path="logs/research_log.jsonl"):
    """Append-only. Never edit/delete: counts every variant tested so winners are not flukes."""
    with open(path, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%F %T"), **entry}) + "\n")
    with open(path) as f:
        return sum(1 for _ in f)
