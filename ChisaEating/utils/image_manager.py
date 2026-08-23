import re
import random
from pathlib import Path

from gsuid_core.data_store import get_res_path

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _list_images(directory: Path) -> list:
    if not directory.exists():
        return []
    return [f for f in directory.iterdir() if f.suffix.lower() in _IMG_EXTS and not f.name.startswith(".")]


class ImageManager:
    def __init__(self, plugin_dir: Path):
        self.plugin_dir = plugin_dir
        self.user_data_dir: Path = get_res_path() / "ChisaEating"
        self.bundled_data_dir: Path = plugin_dir / "bundled_food_data"
        self.egg_dir: Path = plugin_dir / "Still_eating_meme"

        self._worlds = ["world1", "world2", "world3", "world4", "common"]
        self._categories = ["food", "drink", "darkfood"]
        self._moods = ["think", "like", "speechless", "scared"]
        self._ensure_dirs()

    def _ensure_dirs(self):
        for cat in self._categories:
            for w in self._worlds:
                (self.user_data_dir / cat / w).mkdir(parents=True, exist_ok=True)
        for w in self._worlds:
            for mood in self._moods:
                (self.user_data_dir / "memes" / w / mood).mkdir(parents=True, exist_ok=True)
        (self.user_data_dir / "chefs").mkdir(parents=True, exist_ok=True)
        (self.user_data_dir / "ganfanren").mkdir(parents=True, exist_ok=True)
        for char in ["千咲", "派蒙", "达妮娅"]:
            (self.egg_dir / char).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _parse_filename(filename: str):
        pattern = re.compile(
            r"^(?:【(.*?)】)?(.*?)(?:_\d+)?\.(?:jpg|jpeg|png|gif|webp|bmp)$", re.I
        )
        m = pattern.match(filename)
        if m:
            return m.group(1), m.group(2).strip()
        stem = Path(filename).stem
        return None, stem

    def _scan_dir(self, base: Path, folder: str, world: str, food_type: str) -> list:
        target = base / folder / world
        items = []
        for f in _list_images(target):
            chef, food_name = self._parse_filename(f.name)
            items.append(
                {
                    "raw_name": food_name,
                    "food": food_name,
                    "chef": chef or "none",
                    "wv": world,
                    "food_type": food_type,
                    "has_image": True,
                    "path": str(f),
                }
            )
        return items

    def scan_all_items(self, wv_settings: dict, category: str) -> list:
        cat_map = {
            "food": ("food", "特产食物"),
            "drink": ("drink", "特产饮品"),
            "dark": ("darkfood", "黑暗料理"),
        }
        folder_name, food_type = cat_map.get(category, ("food", "特产食物"))

        pool = []
        seen: set = set()

        for w in self._worlds:
            for base in (self.bundled_data_dir, self.user_data_dir):
                for item in self._scan_dir(base, folder_name, w, food_type):
                    key = (item["raw_name"], item["wv"])
                    if key not in seen:
                        seen.add(key)
                        pool.append(item)

        text_key_map = {
            "food": "文字食物",
            "drink": "文字饮品",
            "dark": "文字黑暗料理",
        }
        t_key = text_key_map.get(category, "文字食物")
        for w_key, conf in wv_settings.items():
            for text_item in conf.get(t_key, []):
                if text_item and not any(
                    p["food"] == text_item and p["wv"] == w_key for p in pool
                ):
                    pool.append(
                        {
                            "raw_name": text_item,
                            "food": text_item,
                            "chef": "none",
                            "wv": w_key,
                            "food_type": food_type,
                            "has_image": False,
                            "path": None,
                        }
                    )
        return pool

    def get_chef_image(self, chef_name: str):
        if not chef_name or chef_name == "none":
            return None
        chef_dir = self.user_data_dir / "chefs"
        matched = []
        for f in _list_images(chef_dir):
            parsed_chef, parsed_name = self._parse_filename(f.name)
            if (
                parsed_name == chef_name
                or parsed_chef == chef_name
                or f.name.startswith(chef_name)
            ):
                matched.append(str(f))
        if matched:
            gifs = [p for p in matched if p.lower().endswith(".gif")]
            return random.choice(gifs) if gifs else random.choice(matched)
        return None

    def get_bot_meme(self, world_key: str, mood: str):
        files = _list_images(self.user_data_dir / "memes" / world_key / mood)
        return str(random.choice(files)) if files else None

    def get_egg_meme(self, char_name: str):
        files = _list_images(self.egg_dir / char_name)
        return str(random.choice(files)) if files else None

    def get_ganfanren_data(self) -> dict:
        pool: dict = {}
        user_dir = self.user_data_dir / "ganfanren"
        for scan_dir in (self.egg_dir, user_dir):
            if not scan_dir.exists():
                continue
            for folder in scan_dir.iterdir():
                if not folder.is_dir():
                    continue
                name = folder.name
                if name not in pool:
                    pool[name] = {"images": [], "words": []}
                for f in folder.iterdir():
                    if f.suffix.lower() in _IMG_EXTS:
                        pool[name]["images"].append(str(f))
                    elif f.name.lower() == "words.txt":
                        for enc in ("utf-8", "gbk"):
                            try:
                                lines = f.read_text(encoding=enc).splitlines()
                                pool[name]["words"].extend(
                                    l.strip() for l in lines if l.strip()
                                )
                                break
                            except (UnicodeDecodeError, Exception):
                                continue
        return {k: v for k, v in pool.items() if v["images"]}
