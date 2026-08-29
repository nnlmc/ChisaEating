import json
import random
from collections import deque
from pathlib import Path
from threading import Lock

from gsuid_core.data_store import get_res_path


class FoodDataManager:
    def __init__(self):
        self.data_path: Path = get_res_path() / "ChisaEating"
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.history_path: Path = self.data_path / "group_history.json"
        self.history_limit: int = 30
        self.group_history: dict = {}
        self._lock = Lock()
        self._load_history_cache()

    def _load_history_cache(self):
        if self.history_path.exists():
            try:
                data = json.loads(self.history_path.read_text(encoding="utf-8"))
                for gid, lst in data.items():
                    self.group_history[gid] = deque(lst, maxlen=self.history_limit)
            except (OSError, ValueError):
                self.group_history = {}

    def _save_history_cache(self):
        export = {gid: list(deq) for gid, deq in self.group_history.items()}
        temp_path = self.history_path.with_suffix(".json.tmp")
        try:
            temp_path.write_text(
                json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp_path.replace(self.history_path)
        except OSError:
            if temp_path.exists():
                temp_path.unlink()

    def filter_and_pick(
        self, group_id: str, full_pool: list, active_wv: str, config: dict
    ):
        if not full_pool:
            return None

        mode_loyal = config.get("mode_loyal", False)
        mode_roller = config.get("mode_roller", False)
        mode_normie = config.get("mode_normie", False)

        filtered_pool = []
        for item in full_pool:
            wv = item["wv"]
            if mode_normie:
                if wv == "common":
                    filtered_pool.append(item)
                continue
            if mode_roller and wv == "common":
                continue
            if mode_loyal and wv != "common" and wv != active_wv:
                continue
            filtered_pool.append(item)

        # Strict modes must report an empty pool instead of silently violating
        # the user's selected mode by falling back to all worlds.
        if not filtered_pool:
            return None

        current_limit = max(0, int(config.get("history_limit", 30)))
        weights = {
            "common": max(0, int(config.get("weight_3d", 70))),
            "world1": max(0, int(config.get("weight_world1", 20))),
            "world2": max(0, int(config.get("weight_world2", 5))),
            "world3": max(0, int(config.get("weight_world3", 5))),
            "world4": max(0, int(config.get("weight_world4", 0))),
            "world5": max(0, int(config.get("weight_world5", 0))),
        }
        weighted_pool = [item for item in filtered_pool if weights.get(item["wv"], 0) > 0]
        if weighted_pool:
            filtered_pool = weighted_pool

        with self._lock:
            if current_limit != self.history_limit:
                self.history_limit = current_limit
                for gid in list(self.group_history.keys()):
                    self.group_history[gid] = deque(
                        list(self.group_history[gid]), maxlen=current_limit
                    )

            if current_limit == 0:
                return random.choice(filtered_pool)

            if group_id not in self.group_history:
                self.group_history[group_id] = deque(maxlen=current_limit)
            history = self.group_history[group_id]
            fresh = [i for i in filtered_pool if i["raw_name"] not in history]
            candidates = fresh if fresh else filtered_pool
            picked = random.choices(
                candidates,
                weights=[weights.get(item["wv"], 1) for item in candidates],
                k=1,
            )[0]
            history.append(picked["raw_name"])
            self._save_history_cache()
            return picked
