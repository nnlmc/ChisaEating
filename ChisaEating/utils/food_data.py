import json
import random
from collections import deque
from pathlib import Path

from gsuid_core.data_store import get_res_path


class FoodDataManager:
    def __init__(self):
        self.data_path: Path = get_res_path() / "ChisaEating"
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.history_path: Path = self.data_path / "group_history.json"
        self.history_limit: int = 30
        self.group_history: dict = {}
        self._load_history_cache()

    def _load_history_cache(self):
        if self.history_path.exists():
            try:
                data = json.loads(self.history_path.read_text(encoding="utf-8"))
                for gid, lst in data.items():
                    self.group_history[gid] = deque(lst, maxlen=self.history_limit)
            except Exception:
                self.group_history = {}

    def _save_history_cache(self):
        try:
            export = {gid: list(deq) for gid, deq in self.group_history.items()}
            self.history_path.write_text(
                json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

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

        if not filtered_pool:
            filtered_pool = full_pool

        current_limit = config.get("history_limit", 30)
        if current_limit != self.history_limit:
            self.history_limit = current_limit
            for gid in list(self.group_history.keys()):
                self.group_history[gid] = deque(
                    list(self.group_history[gid]), maxlen=self.history_limit
                )

        if group_id not in self.group_history:
            self.group_history[group_id] = deque(maxlen=max(self.history_limit, 1))
        history = self.group_history[group_id]

        fresh = [i for i in filtered_pool if i["raw_name"] not in history]
        picked = random.choice(fresh if fresh else filtered_pool)

        if self.history_limit > 0:
            history.append(picked["raw_name"])
            self._save_history_cache()

        return picked
