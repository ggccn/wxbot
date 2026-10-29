"""统一的路径解析：人格与记忆默认放在 ~/wxbot_home，可用环境变量 WXBOT_HOME 覆盖。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HOME_DIR = Path(os.environ.get("WXBOT_HOME") or (Path.home() / "wxbot_home"))
DATA_DIR = BASE_DIR / "data"
NOTES_DIR = HOME_DIR / "notes"
ARCHIVE_DIR = NOTES_DIR / "_archive"
PERSONA_FILE = HOME_DIR / "PERSONA.md"
MEMES_FILE = HOME_DIR / "memes.md"
GAMES_FILE = HOME_DIR / "games.md"


def ensure_dirs():
    for d in (HOME_DIR, NOTES_DIR, ARCHIVE_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)
