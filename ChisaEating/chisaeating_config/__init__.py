from pathlib import Path

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .config_default import APPEARANCE_CONFIG_DEFAULT, CONFIG_DEFAULT

CONFIG_PATH = get_res_path() / "ChisaEating" / "config.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

CHISA_CONFIG = StringConfig("ChisaEating", CONFIG_PATH, CONFIG_DEFAULT)
CHISA_APPEARANCE_CONFIG = StringConfig(
    "千小妹外观配置",
    get_res_path() / "ChisaEating" / "show_config.json",
    APPEARANCE_CONFIG_DEFAULT,
)

CHISA_CONFIG.plugin_name = "ChisaEating"
CHISA_APPEARANCE_CONFIG.plugin_name = "ChisaEating"

# One-time world defaults are applied through the public config object.
_DEFAULT_WORLD_CONFIG = {
    "world3_name": "终末地",
    "world3_aliases": ["塔卫二", "帝江号"],
    "world3_selfnames": ["管理员", "咕咕嘎嘎"],
    "world4_name": "崩坏：星穹铁道",
    "world4_aliases": ["星穹铁道", "星铁", "翁法罗斯"],
    "world4_selfnames": ["列车长", "帕姆"],
    "world5_name": "世界5",
    "world5_aliases": [],
    "world5_selfnames": ["向导5"],
}
_MIGRATION_MARKER = "_world_defaults_v2_applied"


def _apply_world_defaults_once() -> None:
    if CHISA_CONFIG.config.get(_MIGRATION_MARKER):
        return
    for key, value in _DEFAULT_WORLD_CONFIG.items():
        config = CHISA_CONFIG.config.get(key)
        if config is not None:
            config.data = value
    marker = CHISA_CONFIG.config.get(_MIGRATION_MARKER)
    if marker is None:
        from gsuid_core.utils.plugins_config.models import GsBoolConfig
        CHISA_CONFIG.config[_MIGRATION_MARKER] = GsBoolConfig("世界默认值迁移标记", "内部标记", True)
    CHISA_CONFIG.write_config()


_apply_world_defaults_once()
# One-time migration of shipped world defaults for existing installations.
_DEFAULT_WORLD_CONFIG = {
    "world3_name": "终末地",
    "world3_aliases": ["塔卫二", "帝江号"],
    "world3_selfnames": ["管理员", "咕咕嘎嘎"],
    "world4_name": "崩坏：星穹铁道",
    "world4_aliases": ["星穹铁道", "星铁", "翁法罗斯"],
    "world4_selfnames": ["列车长", "帕姆"],
    "world5_name": "世界5",
    "world5_aliases": [],
    "world5_selfnames": ["向导5"],
}
_MIGRATION_MARKER = "_world_defaults_v2_applied"


def _apply_world_defaults_once() -> None:
    data = dict(getattr(CHISA_CONFIG, "config", {}) or {})
    if data.get(_MIGRATION_MARKER):
        return
    for key, value in _DEFAULT_WORLD_CONFIG.items():
        data[key] = value
    data[_MIGRATION_MARKER] = True
    CHISA_CONFIG.config = data
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


_apply_world_defaults_once()