"""init"""
from pathlib import Path

from gsuid_core.sv import Plugins
from gsuid_core.webconsole.plugin_page import register_plugin_page

# 导入并装配 FastAPI 路由
from . import chisaeating_api  # noqa: F401
from . import chisaeating_config  # noqa: F401,E402
from . import chisaeating_help  # noqa: F401,E402
from . import chisaeating_main  # noqa: F401,E402
from . import chisaeating_assets  # noqa: F401,E402

Plugins(
    name="ChisaEating",
    force_prefix=[],
    allow_empty_prefix=True,
)

# 注册 WebConsole 插件前端页
_web_manager_dir = Path(__file__).parent / "web" / "manager"
if _web_manager_dir.exists():
    register_plugin_page(
        title="千小妹干饭管理",
        static_dir=_web_manager_dir,
        page_id="manager",
        plugin="ChisaEating",
        description="管理跨次元美食图库、干饭人与DLC商城",
    )
