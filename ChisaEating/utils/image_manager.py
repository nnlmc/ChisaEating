import re
import random
from pathlib import Path

from gsuid_core.data_store import get_res_path

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _list_images(directory: Path) -> list:
    if not directory.exists():
        return []
    return [f for f in directory.iterdir() if f.suffix.lower() in _IMG_EXTS and not f.name.startswith(".")]


def _read_words(path: Path) -> list:
    """读取干饭人台词文件。

    图库里的 words.txt 编码不统一（utf-8 / 带 BOM / gbk），用 replace 兜住
    非法字节即可，台词是展示用文本，个别乱码字符不影响功能。
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    if "�" in text:
        text = raw.decode("gbk", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


class ImageManager:
    """图库访问层。

    所有资源统一存放在 ``get_res_path()/ChisaEating``，由「更新千小妹图库」
    指令从远程拉取部署，插件仓库不再内置任何图片。
    """

    def __init__(self, plugin_dir: Path):
        self.plugin_dir = plugin_dir
        self.user_data_dir: Path = get_res_path() / "ChisaEating"

        self._worlds = ["world1", "world2", "world3", "world4", "world5", "common"]
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
            for item in self._scan_dir(self.user_data_dir, folder_name, w, food_type):
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
        """干饭人表情包，资源来自远程图库的 ganfanren/<角色名>/。"""
        files = _list_images(self.user_data_dir / "ganfanren" / char_name)
        return str(random.choice(files)) if files else None

    def get_ganfanren_data(self) -> dict:
        pool: dict = {}
        user_dir = self.user_data_dir / "ganfanren"
        if not user_dir.exists():
            return pool
        for folder in user_dir.iterdir():
            if not folder.is_dir():
                continue
            name = folder.name
            if name not in pool:
                pool[name] = {"images": [], "words": []}
            for f in folder.iterdir():
                if f.suffix.lower() in _IMG_EXTS:
                    pool[name]["images"].append(str(f))
                elif f.name.lower() == "words.txt":
                    pool[name]["words"].extend(_read_words(f))
        return {k: v for k, v in pool.items() if v["images"]}
