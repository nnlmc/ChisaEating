"""图库资源管理指令。

对应上游 AstrBot 版的「更新千小妹图库」「千小妹图库下载进度」，
差异：上游靠 WebUI 勾选 + 启动自动下载，这里改为**指令触发**，且限主人权限。
"""

from pathlib import Path
from typing import Final

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ..utils.downloader import (
    ASSET_TOTAL_BYTES,
    STATE,
    download_assets,
    has_food_assets,
)
from ..utils.image_manager import ImageManager

# pm=0 仅限主人：下载会占用 160MB 流量与磁盘，且会覆盖已部署的图库
sv_assets = SV("千小妹图库管理", pm=0, area="ALL")

_plugin_dir: Final[Path] = Path(__file__).parent.parent.parent
_image_mgr: Final[ImageManager] = ImageManager(_plugin_dir)


@sv_assets.on_fullmatch(
    ("更新千小妹图库", "下载千小妹图库", "千小妹图库更新"),
    block=True,
)
async def on_update_assets(bot: Bot, ev: Event) -> None:
    """拉取并部署远程图库（仅主人）。"""
    target_dir = _image_mgr.user_data_dir

    if STATE.is_downloading:
        await bot.send(
            f"【千小妹提示】图库正在下载中，请勿重复触发\n"
            f"当前进度 {STATE.downloaded_mb:.2f} MB / {STATE.total_mb:.2f} MB"
            f"（{STATE.percent:.1f}%）"
        )
        return

    logger.info(f"[千小妹还在吃] 主人 {ev.user_id} 触发图库下载")
    await bot.send(
        f"【千小妹提示】开始拉取基础图库（约 {ASSET_TOTAL_BYTES / 1048576:.0f}MB）\n"
        f"下载进度会实时输出到后台日志，完成后会在此提醒\n"
        f"也可发送「千小妹图库下载进度」查询"
    )

    success, message = await download_assets(target_dir)
    if success:
        await bot.send(f"【千小妹提示】{message}\n现在可以发送「吃什么」开始点菜啦")
        return
    await bot.send(
        f"【千小妹提示】图库下载失败：{message}\n"
        f"可稍后重试，或参考 README 手动下载解压到\n{target_dir}"
    )


@sv_assets.on_fullmatch(
    ("千小妹图库下载进度", "图库下载进度", "千小妹下载进度"),
    block=True,
)
async def on_download_progress(bot: Bot, ev: Event) -> None:
    """查询下载进度（仅主人）。"""
    if not STATE.is_downloading:
        if has_food_assets(_image_mgr.user_data_dir):
            await bot.send("【千小妹提示】当前没有下载任务，图库已就绪")
            return
        await bot.send(
            "【千小妹提示】当前没有下载任务，且图库为空\n"
            "发送「更新千小妹图库」开始拉取"
        )
        return

    stage_text = {
        "downloading": "正在下载",
        "verifying": "正在校验 SHA-256",
        "extracting": "正在解压部署",
    }.get(STATE.stage, "处理中")

    await bot.send(
        f"【千小妹下载进度】{stage_text}\n"
        f"{STATE.downloaded_mb:.2f} MB / {STATE.total_mb:.2f} MB"
        f"（{STATE.percent:.1f}%）"
    )
