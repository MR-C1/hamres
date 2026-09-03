"""Shared helpers: config loading, paths, logging."""
import json
import logging
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "queue"
OUTPUT = ROOT / "output"
REVIEW = OUTPUT / "review"
APPROVED = OUTPUT / "approved"
CACHE = ROOT / "cache"
ASSETS = ROOT / "assets"
LOGS = ROOT / "logs"
STATE_FILE = ROOT / "state.json"


def load_config():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    """Persistent ledger: which scripts were used/uploaded."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"rendered": [], "uploaded": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def setup_logging(name):
    LOGS.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOGS / f"{name}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(name)


def validate_script(script):
    """Make sure a queue JSON has everything the renderer needs."""
    required = ["id", "title", "hook", "scenes"]
    missing = [k for k in required if k not in script or not script[k]]
    if missing:
        raise ValueError(f"script missing keys: {missing}")
    for i, scene in enumerate(script["scenes"]):
        if "narration" not in scene or "visual_keywords" not in scene:
            raise ValueError(f"scene {i} needs 'narration' and 'visual_keywords'")
    return script
