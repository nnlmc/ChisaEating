import asyncio
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
from gsuid_core.logger import logger

from .resource.RESOURCE_PATH import DATA_PATH, DRINK_PATH, FOOD_PATH

ASSET_URLS = [
    (
        "GitHub 直连",
        "https://github.com/dddada123/astrbot_plugin_chisa_still_eating_photo/releases/download/v3.0-beta/V3.astrbot_plugin_chisa_still_eating.zip",
    ),
    (
        "ghproxy.net 镜像",
        "https://ghproxy.net/https://github.com/dddada123/astrbot_plugin_chisa_still_eating_photo/releases/download/v3.0-beta/V3.astrbot_plugin_chisa_still_eating.zip",
    ),
    (
        "mirror.ghproxy.com 镜像",
        "https://mirror.ghproxy.com/https://github.com/dddada123/astrbot_plugin_chisa_still_eating_photo/releases/download/v3.0-beta/V3.astrbot_plugin_chisa_still_eating.zip",
    ),
]

_download_lock = asyncio.Lock()
_is_downloading = False


def is_resource_downloading() -> bool:
    return _is_downloading


def check_needs_download() -> bool:
    if not FOOD_PATH.exists() or not any(FOOD_PATH.iterdir()):
        return True
    if not DRINK_PATH.exists() or not any(DRINK_PATH.iterdir()):
        return True
    return False


def clean_legacy_bundled_resources(plugin_dir: Path) -> None:
    legacy_dirs = [
        plugin_dir / "bundled_food_data",
        plugin_dir / "Still_eating_meme",
    ]
    for d in legacy_dirs:
        if d.exists() and d.is_dir():
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


async def test_mirror_speed() -> List[Tuple[str, str, float]]:
    results: List[Tuple[str, str, float]] = []
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        for name, url in ASSET_URLS:
            try:
                t0 = time.monotonic()
                resp = await client.head(url)
                cost = (time.monotonic() - t0) * 1000
                if resp.status_code < 400:
                    results.append((name, url, cost))
                else:
                    results.append((name, url, 99999.0))
            except Exception:
                results.append((name, url, 99999.0))
    results.sort(key=lambda x: x[2])
    return results


async def download_and_extract_assets(
    progress_callback: Optional[callable] = None,
) -> Tuple[bool, str]:
    global _is_downloading
    if _download_lock.locked():
        return False, "资源正在下载中，请勿重复操作！"

    async with _download_lock:
        _is_downloading = True
        try:
            if progress_callback:
                await progress_callback("📡 正在检测下载节点连接速度与直连延迟...")

            speed_results = await test_mirror_speed()
            speed_log = []
            valid_sources = []
            for name, url, cost in speed_results:
                if cost < 90000:
                    speed_log.append(f"• {name}: {cost:.1f}ms (可用)")
                    valid_sources.append((name, url))
                else:
                    speed_log.append(f"• {name}: 超时/不可用")

            speed_report = "\n".join(speed_log)
            logger.info(f"[ChisaEating] 下载源测速完成:\n{speed_report}")

            if not valid_sources:
                return False, f"所有下载源测速均失败，无法拉取资源！\n{speed_report}"

            best_name, best_url = valid_sources[0]
            if progress_callback:
                await progress_callback(
                    f"⚡ 测速完成，选用最优节点【{best_name}】\n{speed_report}\n\n正在下载基础图库资源包 (约 160MB)..."
                )

            zip_path = DATA_PATH / "assets_temp.zip"
            DATA_PATH.mkdir(parents=True, exist_ok=True)

            success = False
            for name, url in valid_sources:
                try:
                    logger.info(f"[ChisaEating] 开始从 {name} ({url}) 下载...")
                    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
                        async with client.stream("GET", url) as resp:
                            if resp.status_code >= 400:
                                continue
                            with open(zip_path, "wb") as f:
                                async for chunk in resp.aiter_bytes(chunk_size=65536):
                                    f.write(chunk)
                    if zip_path.exists() and zip_path.stat().st_size > 1024 * 1024:
                        success = True
                        break
                except Exception as e:
                    logger.warning(f"[ChisaEating] 下载失败 ({name}): {e}")
                    if zip_path.exists():
                        zip_path.unlink(missing_ok=True)

            if not success:
                return False, "图库资源包下载失败，所有可用节点均断开或下载不完整！"

            if progress_callback:
                await progress_callback("📦 图库下载完成，正在解压部署到数据目录...")

            extract_tmp = DATA_PATH / "extract_tmp"
            if extract_tmp.exists():
                shutil.rmtree(extract_tmp, ignore_errors=True)

            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(extract_tmp)

            src_dir = extract_tmp
            for root, dirs, _ in os.walk(extract_tmp):
                if "food" in dirs or "drink" in dirs or "chefs" in dirs:
                    src_dir = Path(root)
                    break

            for item in src_dir.iterdir():
                dest = DATA_PATH / item.name
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)

            zip_path.unlink(missing_ok=True)
            shutil.rmtree(extract_tmp, ignore_errors=True)

            logger.info("[ChisaEating] 基础图库解压部署完成！")
            return True, "🎉 基础图库资源包下载并解压部署完成！"

        except Exception as e:
            logger.exception(f"[ChisaEating] 下载解压过程发生异常: {e}")
            return False, f"资源更新失败: {e}"
        finally:
            _is_downloading = False
