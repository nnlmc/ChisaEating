import sys
from pathlib import Path
from gsuid_core.data_store import get_res_path

DATA_PATH = get_res_path() / "ChisaEating"
sys.path.append(str(DATA_PATH))

FOOD_PATH = DATA_PATH / "food"
DRINK_PATH = DATA_PATH / "drink"
DARKFOOD_PATH = DATA_PATH / "darkfood"
MEMES_PATH = DATA_PATH / "memes"
CHEFS_PATH = DATA_PATH / "chefs"
GANFANREN_PATH = DATA_PATH / "ganfanren"

HISTORY_PATH = DATA_PATH / "group_history.json"
HELP_TXT_PATH = DATA_PATH / "👉当前可用干饭人一览.txt"

for p in (FOOD_PATH, DRINK_PATH, DARKFOOD_PATH, MEMES_PATH, CHEFS_PATH, GANFANREN_PATH):
    p.mkdir(parents=True, exist_ok=True)
