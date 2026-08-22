import random
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .resource.RESOURCE_PATH import (
    CHEFS_PATH,
    DARKFOOD_PATH,
    DATA_PATH,
    DRINK_PATH,
    FOOD_PATH,
    GANFANREN_PATH,
    HELP_TXT_PATH,
    MEMES_PATH,
)

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _list_images(directory: Path) -> List[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    return [
        f
        for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in _IMG_EXTS and not f.name.startswith(".")
    ]


class ImageManager:
    def __init__(self):
        self.data_dir: Path = DATA_PATH
        self.worlds = ["world1", "world2", "world3", "world4", "common"]
        self.categories = ["food", "drink", "darkfood"]
        self.moods = ["think", "like", "speechless", "scared"]
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for cat in self.categories:
            for w in self.worlds:
                (self.data_dir / cat / w).mkdir(parents=True, exist_ok=True)
        for w in self.worlds:
            for mood in self.moods:
                (self.data_dir / "memes" / w / mood).mkdir(parents=True, exist_ok=True)
        CHEFS_PATH.mkdir(parents=True, exist_ok=True)
        GANFANREN_PATH.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def parse_filename(filename: str) -> Tuple[Optional[str], str]:
        pattern = re.compile(
            r"^(?:【(.*?)】)?(.*?)(?:_\d+)?\.(?:jpg|jpeg|png|gif|webp|bmp)$", re.I
        )
        m = pattern.match(filename)
        if m:
            return m.group(1), m.group(2).strip()
        stem = Path(filename).stem
        return None, stem

    def scan_all_items(self, wv_settings: dict, category: str) -> List[dict]:
        cat_map = {
            "food": ("food", "特产食物"),
            "drink": ("drink", "特产饮品"),
            "dark": ("darkfood", "黑暗料理"),
        }
        folder_name, food_type = cat_map.get(category, ("food", "特产食物"))

        pool: List[dict] = []
        seen = set()

        for w in self.worlds:
            target_dir = self.data_dir / folder_name / w
            for f in _list_images(target_dir):
                chef, food_name = self.parse_filename(f.name)
                key = (food_name, w)
                if key not in seen:
                    seen.add(key)
                    pool.append(
                        {
                            "raw_name": food_name,
                            "food": food_name,
                            "chef": chef or "none",
                            "wv": w,
                            "food_type": food_type,
                            "has_image": True,
                            "path": str(f),
                        }
                    )

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

    def get_chef_image(self, chef_name: str) -> Optional[str]:
        if not chef_name or chef_name == "none":
            return None
        matched: List[str] = []
        for f in _list_images(CHEFS_PATH):
            parsed_chef, parsed_name = self.parse_filename(f.name)
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

    def get_bot_meme(self, world_key: str, mood: str) -> Optional[str]:
        files = _list_images(MEMES_PATH / world_key / mood)
        return str(random.choice(files)) if files else None

    def get_egg_meme(self, char_name: str) -> Optional[str]:
        char_dir = GANFANREN_PATH / char_name
        files = _list_images(char_dir)
        return str(random.choice(files)) if files else None

    def get_ganfanren_data(self) -> Dict[str, dict]:
        pool: Dict[str, dict] = {}
        if not GANFANREN_PATH.exists():
            return pool

        for folder in GANFANREN_PATH.iterdir():
            if not folder.is_dir():
                continue
            name = folder.name
            if name not in pool:
                pool[name] = {"images": [], "words": []}
            for f in folder.iterdir():
                if f.is_file() and f.suffix.lower() in _IMG_EXTS:
                    pool[name]["images"].append(str(f))
                elif f.is_file() and f.name.lower() == "words.txt":
                    for enc in ("utf-8", "gbk"):
                        try:
                            lines = f.read_text(encoding=enc).splitlines()
                            pool[name]["words"].extend(
                                l.strip() for l in lines if l.strip()
                            )
                            break
                        except Exception:
                            continue

        valid_pool = {k: v for k, v in pool.items() if v["images"]}

        try:
            names = list(valid_pool.keys())
            HELP_TXT_PATH.write_text(
                "【系统扫描报告 - ChisaEating】\n"
                "当前已识别到以下干饭人：\n"
                + "\n".join(f"- {n}" for n in names)
                + "\n\n如需在 WebUI 指定卡池，请直接复制下方文本到【指定干饭人卡池】配置框：\n"
                + ";".join(names)
                + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

        return valid_pool
