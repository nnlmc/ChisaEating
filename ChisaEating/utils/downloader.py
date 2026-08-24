"""远程图库下载引擎。

对应上游 AstrBot 版 v3.1~v3.5.1 的「全自动远程图库拉取引擎」，改造点：

* 上游用 ``requests`` 同步流式下载 + ``threading.Thread``；GsCore 全异步，
  这里改用 ``httpx.AsyncClient`` 的 ``aiter_bytes``，解压走 ``@to_thread``。
* 上游用 ``threading`` 布尔标志做单例保护（check-then-set 有竞态）；
  这里用 ``asyncio.Lock`` 真正互斥。
* 下载进度按固定间隔写入 core 日志，不再依赖指令查询。
"""

import asyncio
import hashlib
import shutil
import zipfile
from pathlib import Path
from typing import Final, List, Optional, Tuple

import httpx
from gsuid_core.logger import logger
from gsuid_core.pool import to_thread

LOG_PREFIX: Final[str] = "[千小妹还在吃]"

# 图库压缩包镜像节点，按顺序尝试；上游首个节点 mirror.ghproxy.com 已失效但保留以便回退
ASSET_URLS: Final[Tuple[str, ...]] = (
    "https://ghproxy.net/https://github.com/dddada123/astrbot_plugin_chisa_still_eating_photo/releases/download/v3.0-beta/V3.astrbot_plugin_chisa_still_eating.zip",
    "https://github.com/dddada123/astrbot_plugin_chisa_still_eating_photo/releases/download/v3.0-beta/V3.astrbot_plugin_chisa_still_eating.zip",
    "https://mirror.ghproxy.com/https://github.com/dddada123/astrbot_plugin_chisa_still_eating_photo/releases/download/v3.0-beta/V3.astrbot_plugin_chisa_still_eating.zip",
)

# 压缩包 SHA-256，与上游 main.py 的 TARGET_HASH 一致
ASSET_SHA256: Final[str] = "239dda1a6de8ad4227f166eabe19db83c9ce4a15806e14fdbd7ecbbf98da30ae"

# 压缩包实际大小 168252425 字节，用于进度百分比
ASSET_TOTAL_BYTES: Final[int] = 168252425

_CHUNK_SIZE: Final[int] = 1 << 16
_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)

# 每下载这么多字节写一条进度日志
_LOG_STEP_BYTES: Final[int] = 10 * 1024 * 1024

# 压缩包顶层应包含的目录，用于在解压结果中定位真实根目录
_ROOT_MARKERS: Final[Tuple[str, ...]] = ("food", "drink", "chefs")


class DownloadState:
    """下载状态，供指令层读取进度。"""

    def __init__(self) -> None:
        self.is_downloading: bool = False
        self.downloaded_bytes: int = 0
        self.stage: str = ""

    @property
    def downloaded_mb(self) -> float:
        return self.downloaded_bytes / 1048576

    @property
    def total_mb(self) -> float:
        return ASSET_TOTAL_BYTES / 1048576

    @property
    def percent(self) -> float:
        if ASSET_TOTAL_BYTES <= 0:
            return 0.0
        return min(100.0, self.downloaded_bytes * 100 / ASSET_TOTAL_BYTES)


STATE: Final[DownloadState] = DownloadState()
_LOCK: Final[asyncio.Lock] = asyncio.Lock()


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            block = fp.read(1 << 20)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@to_thread
def _verify_archive(path: Path) -> Tuple[bool, str]:
    """校验压缩包 SHA-256，返回 (是否通过, 实际哈希)。"""
    actual = _sha256_of(path)
    return actual == ASSET_SHA256, actual


@to_thread
def _extract_archive(zip_path: Path, target_dir: Path) -> int:
    """解压并部署到 target_dir，返回部署的顶层条目数。

    先解到临时目录，再按 food/drink/chefs 定位真实根，最后覆盖式搬运。
    """
    extract_tmp = target_dir / "extract_tmp"
    if extract_tmp.exists():
        shutil.rmtree(extract_tmp, ignore_errors=True)
    extract_tmp.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(extract_tmp)

    # 压缩包可能带一层包裹目录，向下找到含 food/drink/chefs 的那一层
    src_dir = extract_tmp
    for candidate in [extract_tmp, *(p for p in extract_tmp.rglob("*") if p.is_dir())]:
        names = {child.name for child in candidate.iterdir() if child.is_dir()}
        if any(marker in names for marker in _ROOT_MARKERS):
            src_dir = candidate
            break

    moved = 0
    for item in src_dir.iterdir():
        dest = target_dir / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
        moved += 1

    shutil.rmtree(extract_tmp, ignore_errors=True)
    return moved


def _cleanup(zip_path: Path, target_dir: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    extract_tmp = target_dir / "extract_tmp"
    if extract_tmp.exists():
        shutil.rmtree(extract_tmp, ignore_errors=True)


async def _fetch_to(url: str, zip_path: Path) -> Tuple[bool, str]:
    """从单个节点流式下载到 zip_path，边下边写日志进度。"""
    STATE.downloaded_bytes = 0
    next_log = _LOG_STEP_BYTES

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"

            with zip_path.open("wb") as fp:
                async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                    fp.write(chunk)
                    STATE.downloaded_bytes += len(chunk)
                    if STATE.downloaded_bytes >= next_log:
                        next_log += _LOG_STEP_BYTES
                        logger.info(
                            f"{LOG_PREFIX} 图库下载中 "
                            f"{STATE.downloaded_mb:.2f} MB / {STATE.total_mb:.2f} MB "
                            f"({STATE.percent:.1f}%)"
                        )
    return True, ""


async def download_assets(target_dir: Path) -> Tuple[bool, str]:
    """下载并部署图库资源，返回 (是否成功, 结果说明)。

    并发调用时后来者直接返回失败提示，不会重复下载。
    """
    if _LOCK.locked():
        return False, "图库正在下载中，请勿重复触发"

    async with _LOCK:
        STATE.is_downloading = True
        STATE.downloaded_bytes = 0
        STATE.stage = "downloading"
        target_dir.mkdir(parents=True, exist_ok=True)
        zip_path = target_dir / "assets_temp.zip"
        failures: List[str] = []

        logger.info(f"{LOG_PREFIX} 开始拉取基础图库（约 {ASSET_TOTAL_BYTES / 1048576:.2f} MB）")

        for index, url in enumerate(ASSET_URLS, 1):
            host = httpx.URL(url).host
            logger.info(f"{LOG_PREFIX} 尝试节点 {index}/{len(ASSET_URLS)}：{host}")

            ok, reason = await _fetch_to(url, zip_path)
            if not ok:
                logger.warning(f"{LOG_PREFIX} 节点 {host} 下载失败：{reason}")
                failures.append(f"{host} {reason}")
                if zip_path.exists():
                    zip_path.unlink()
                continue

            logger.info(
                f"{LOG_PREFIX} 下载完成 {STATE.downloaded_mb:.2f} MB，开始 SHA-256 校验"
            )
            STATE.stage = "verifying"
            passed, actual = await _verify_archive(zip_path)
            if not passed:
                logger.warning(
                    f"{LOG_PREFIX} 安全告警：资源包哈希不匹配，已删除并尝试下一节点\n"
                    f"预期 {ASSET_SHA256}\n实际 {actual}"
                )
                failures.append(f"{host} 哈希不匹配")
                zip_path.unlink()
                continue

            logger.info(f"{LOG_PREFIX} 校验通过，开始解压部署")
            STATE.stage = "extracting"
            moved = await _extract_archive(zip_path, target_dir)
            _cleanup(zip_path, target_dir)

            STATE.is_downloading = False
            STATE.stage = ""
            logger.info(f"{LOG_PREFIX} 图库部署完成，共 {moved} 个顶层目录")
            return True, f"图库已部署完成，共 {moved} 个资源目录"

        _cleanup(zip_path, target_dir)
        STATE.is_downloading = False
        STATE.stage = ""
        detail = "；".join(failures) if failures else "未知原因"
        logger.error(f"{LOG_PREFIX} 所有下载节点均失败：{detail}")
        return False, f"所有下载节点均失败（{detail}）"


def has_food_assets(target_dir: Path) -> bool:
    """判断图库是否已就绪：food 目录下存在任意图片。"""
    food_dir = target_dir / "food"
    if not food_dir.is_dir():
        return False
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    for path in food_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in exts:
            return True
    return False
