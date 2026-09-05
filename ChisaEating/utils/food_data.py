import json
import random
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from gsuid_core.data_store import get_res_path
from gsuid_core.logger import logger


class FoodDataManager:
    def __init__(self, history_limit: int = 30):
        self.history_limit = history_limit
        self.group_history: Dict[str, deque] = {}
        self._lock = Lock()
        self.cache_file = get_res_path() / "ChisaEating" / "history_cache.json"
        self._load_history_cache()

    def _load_history_cache(self):
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for gid, hist in data.items():
                        self.group_history[gid] = deque(hist, maxlen=self.history_limit)
            except Exception as e:
                logger.error(f"[ChisaEating] 读取历史记录失败: {e}")

    def _save_history_cache(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                data = {gid: list(hist) for gid, hist in self.group_history.items()}
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[ChisaEating] 保存历史记录失败: {e}")

    def filter_and_pick(
        self,
        group_id: str,
        pool: List[Dict[str, Any]],
        active_wv: str,
        config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not pool:
            return None

        mode_loyal = config.get("mode_loyal", False)
        mode_roller = config.get("mode_roller", False)
        mode_normie = config.get("mode_normie", False)

        filtered_pool = []
        for item in pool:
            wv = item.get("wv", "common")
            if mode_normie and wv != "common":
                continue
            if mode_roller and wv == "common":
                continue
            if mode_loyal and wv != "common" and wv != active_wv:
                continue
            filtered_pool.append(item)

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
                item_weights = [max(1, weights.get(item["wv"], 1)) for item in filtered_pool]
                return random.choices(filtered_pool, weights=item_weights, k=1)[0]

            if group_id not in self.group_history:
                self.group_history[group_id] = deque(maxlen=current_limit)
            history = self.group_history[group_id]
            fresh = [i for i in filtered_pool if i["raw_name"] not in history]
            candidates = fresh if fresh else filtered_pool
            item_weights = [max(1, weights.get(item["wv"], 1)) for item in candidates]
            picked = random.choices(candidates, weights=item_weights, k=1)[0]
            history.append(picked["raw_name"])
            self._save_history_cache()
            return picked
