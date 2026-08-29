"""远程图库下载引擎。

对应上游 AstrBot 版 v3.1~v3.5.1 的「全自动远程图库拉取引擎」，改造点：

* 上游用 ``requests`` 同步流式下载 + ``threading.Thread``；GsCore 全异步，
  这里改用 ``httpx.AsyncClient`` 的 ``aiter_bytes``，解压走 ``@to_thread``。
* 上游用 ``threading`` 布尔标志做单例保护（check-then-set 有竞态）；
  这里用 ``asyncio.Lock`` 真正互斥。
* 下载进度按固定间隔写入 core 日志，不再依赖指令查询。
* 下载前并发测速所有加速节点，按实测吞吐排序后再下载；上游为固定顺序，
  遇到失效或拥堵的节点只能干等超时。
"""

import asyncio
import hashlib
import shutil
import time
import zipfile
from pathlib import Path
from typing import Final, List, Optional, Tuple

import httpx
from gsuid_core.logger import logger
from gsuid_core.pool import to_thread

LOG_PREFIX: Final[str] = "[千小妹还在吃]"

# 图库压缩包在 GitHub Release 上的原始地址
ASSET_RAW_URL: Final[str] = (
    "https://github.com/dddada123/astrbot_plugin_chisa_still_eating_photo"
    "/releases/download/v3.0-beta/V3.astrbot_plugin_chisa_still_eating.zip"
)

# 加速前缀，运行时实测选择；空串代表 GitHub 直连
_MIRROR_PREFIXES: Final[Tuple[str, ...]] = (
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://github.moeyy.xyz/",
    "https://ghproxy.net/",
    "https://mirror.ghproxy.com/",
    "",
)

# 各节点完整下载地址；实际顺序由测速结果决定，此处仅作为兜底顺序
ASSET_URLS: Final[Tuple[str, ...]] = tuple(
    f"{prefix}{ASSET_RAW_URL}" for prefix in _MIRROR_PREFIXES
)

# 测速参数：并发拉取头部若干字节，比较实际吞吐
_PROBE_BYTES: Final[int] = 1 << 20
_PROBE_TIMEOUT: Final[float] = 8.0

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


async def _probe_mirror(client: httpx.AsyncClient, url: str) -> Tuple[str, float]:
    """探测单个节点，返回 (地址, 速度 KB/s)；失败返回 -1。

    用 Range 只拉头部 1MB，按实际耗时算吞吐；不支持 Range 的节点
    会返回 200 并开始传全量，读满 1MB 即断开，同样能得到速度。
    """
    host = httpx.URL(url).host
    started = time.monotonic()
    received = 0
    try:
        headers = {"Range": f"bytes=0-{_PROBE_BYTES - 1}"}
        async with client.stream("GET", url, headers=headers, timeout=_PROBE_TIMEOUT) as resp:
            if resp.status_code not in (200, 206):
                logger.debug(f"{LOG_PREFIX} 测速 {host} 返回 HTTP {resp.status_code}")
                return url, -1.0
            async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                received += len(chunk)
                if received >= _PROBE_BYTES:
                    break
    except Exception as exc:
        logger.debug(f"{LOG_PREFIX} 测速 {host} 失败：{type(exc).__name__}")
        return url, -1.0

    elapsed = max(time.monotonic() - started, 1e-3)
    if received <= 0:
        return url, -1.0
    speed = received / 1024 / elapsed
    logger.info(f"{LOG_PREFIX} 测速 {host}：{speed:.0f} KB/s")
    return url, speed


async def _rank_mirrors() -> Tuple[str, ...]:
    """并发测速所有节点，按速度从快到慢排序。

    全部失败时退回原始顺序，让后续下载逐个重试——测速用的超时较短，
    可能误判慢节点为不可用，而真正下载允许更长的连接时间。
    """
    logger.info(f"{LOG_PREFIX} 开始测速 {len(ASSET_URLS)} 个下载节点")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await asyncio.gather(
            *(_probe_mirror(client, url) for url in ASSET_URLS),
            return_exceptions=False,
        )

    alive = [(url, speed) for url, speed in results if speed > 0]
    if not alive:
        logger.warning(f"{LOG_PREFIX} 所有节点测速失败，按默认顺序尝试下载")
        return ASSET_URLS

    alive.sort(key=lambda item: item[1], reverse=True)
    fastest_host = httpx.URL(alive[0][0]).host
    logger.info(
        f"{LOG_PREFIX} 测速完成，{len(alive)}/{len(ASSET_URLS)} 个节点可用，"
        f"最快 {fastest_host}（{alive[0][1]:.0f} KB/s）"
    )

    # 可用节点在前，其余按原顺序附在后面作为兜底
    ranked = [url for url, _ in alive]
    ranked.extend(url for url in ASSET_URLS if url not in set(ranked))
    return tuple(ranked)


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
def _extract_archive(zip_path: Path, target_dir: Path) -> Tuple[int, str]:
    """安全解压到临时目录，并原子替换资源目录。"""
    extract_tmp = target_dir / ".extract_tmp"
    deploy_tmp = target_dir / ".deploy_tmp"
    for path in (extract_tmp, deploy_tmp):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    extract_tmp.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as archive:
        root = extract_tmp.resolve()
        for member in archive.infolist():
            destination = (extract_tmp / member.filename).resolve()
            if destination != root and root not in destination.parents:
                raise ValueError(f"压缩包包含非法路径：{member.filename}")
        archive.extractall(extract_tmp)

    src_dir = extract_tmp
    for candidate in [extract_tmp, *(p for p in extract_tmp.rglob("*") if p.is_dir())]:
        names = {child.name for child in candidate.iterdir() if child.is_dir()}
        if any(marker in names for marker in _ROOT_MARKERS):
            src_dir = candidate
            break

    deploy_tmp.mkdir(parents=True, exist_ok=True)
    moved = 0
    for item in src_dir.iterdir():
        destination = deploy_tmp / item.name
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)
        moved += 1

    managed_names = {item.name for item in deploy_tmp.iterdir()}
    for item in target_dir.iterdir():
        if item.name not in {".extract_tmp", ".deploy_tmp", "assets_temp.zip"} and item.name in managed_names:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    for item in deploy_tmp.iterdir():
        item.replace(target_dir / item.name)
    shutil.rmtree(extract_tmp, ignore_errors=True)
    shutil.rmtree(deploy_tmp, ignore_errors=True)
    return moved, "ok"


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
    """下载并部署图库资源，返回 (是否成功, 结果说明)。"""
    if _LOCK.locked():
        return False, "图库正在下载中，请勿重复触发"

    async with _LOCK:
        STATE.is_downloading = True
        STATE.downloaded_bytes = 0
        STATE.stage = "downloading"
        target_dir.mkdir(parents=True, exist_ok=True)
        zip_path = target_dir / "assets_temp.zip"
        failures: List[str] = []
        try:
            logger.info(f"{LOG_PREFIX} 开始拉取基础图库（约 {ASSET_TOTAL_BYTES / 1048576:.2f} MB）")
            STATE.stage = "probing"
            ranked_urls = await _rank_mirrors()
            STATE.stage = "downloading"

            for index, url in enumerate(ranked_urls, 1):
                host = httpx.URL(url).host
                logger.info(f"{LOG_PREFIX} 尝试节点 {index}/{len(ranked_urls)}：{host}")
                STATE.downloaded_bytes = 0
                ok, reason = await _fetch_to(url, zip_path)
                if not ok:
                    logger.warning(f"{LOG_PREFIX} 节点 {host} 下载失败：{reason}")
                    failures.append(f"{host} {reason}")
                    if zip_path.exists():
                        zip_path.unlink()
                    continue

                STATE.stage = "verifying"
                passed, actual = await _verify_archive(zip_path)
                if not passed:
                    logger.warning(
                        f"{LOG_PREFIX} 安全告警：资源包哈希不匹配，预期 {ASSET_SHA256}，实际 {actual}"
                    )
                    failures.append(f"{host} 哈希不匹配")
                    zip_path.unlink()
                    continue

                logger.info(f"{LOG_PREFIX} 校验通过，开始解压部署")
                STATE.stage = "extracting"
                moved, _ = await _extract_archive(zip_path, target_dir)
                _cleanup(zip_path, target_dir)
                logger.info(f"{LOG_PREFIX} 图库部署完成，共 {moved} 个顶层目录")
                return True, f"图库已部署完成，共 {moved} 个资源目录"

            _cleanup(zip_path, target_dir)
            detail = "；".join(failures) if failures else "未知原因"
            logger.error(f"{LOG_PREFIX} 所有下载节点均失败：{detail}")
            return False, f"所有下载节点均失败（{detail}）"
        except Exception as exc:
            _cleanup(zip_path, target_dir)
            logger.exception(f"{LOG_PREFIX} 图库下载部署异常")
            return False, f"图库下载部署异常：{type(exc).__name__}"
        finally:
            STATE.is_downloading = False
            STATE.stage = ""



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
