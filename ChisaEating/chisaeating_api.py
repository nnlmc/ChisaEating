from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from gsuid_core.data_store import get_res_path
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app

router = APIRouter(prefix="/api/chisaeating", tags=["ChisaEating"])

# 皮肤镜像与下载白名单配置
SKIN_MIRROR_NODES = (
    "gh-proxy.com",
    "hk.gh-proxy.com",
    "gh.dpik.top",
    "edgeone.gh-proxy.com",
)
SKIN_VAR_WHITELIST = {
    "--hover-tint", "--bg", "--panel", "--card", "--text", "--muted",
    "--primary", "--primary-hover", "--primary-contrast", "--line", "--shadow",
    "--surface", "--surface-dark", "--input-bg", "--overlay",
}
COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{6}|rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*(0|1|0?\.\d+)\s*\))$")
SHADOW_RE = re.compile(
    r"^(?:-?(?:0|\d{1,3}(?:\.\d+)?px)\s+){2,4}(?:#[0-9a-fA-F]{6}|rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*(?:0|1|0?\.\d+)\s*\))(?:\s+inset)?$"
)
OFFICIAL_SKIN_SOURCE = "dddada123/astrbot_plugin_chisa_still_eating_photo"
BUILTIN_SKIN_IDS = {"maple_dew", "yy_xuanling", "chisa_red_black", "chisa_red_white"}
SKIN_ASSET_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SKIN_ASSETS = {
    "03.jpg": "skin/03.jpg",
    "04.jpg": "skin/04.jpg",
}
BUILTIN_SKIN_ASSETS = {
    "maple_dew": "03.jpg",
    "yy_xuanling": "04.jpg",
}
SKIN_ASSET_CHUNK_BYTES = 192 * 1024

_dlc_best_node: Optional[str] = None


def _get_base_dir() -> Path:
    base = get_res_path() / "ChisaEating"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f".tmp.{os.getpid()}.{id(data)}")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _extract_dlc_zip_safe(zip_path: str, target_dir: str) -> None:
    import zipfile
    from pathlib import PurePosixPath

    with zipfile.ZipFile(zip_path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > 10000:
            raise ValueError("ZIP contains too many files")
        if sum(info.file_size for info in infos) > 2 * 1024 * 1024 * 1024:
            raise ValueError("ZIP expands beyond 2 GiB")
        target_root = os.path.abspath(target_dir)
        for info in infos:
            normalized = info.filename.replace("\\", "/")
            parts = PurePosixPath(normalized).parts
            mode = (info.external_attr >> 16) & 0o170000
            if not parts or normalized.startswith("/") or ".." in parts or ":" in parts[0] or mode == 0o120000:
                raise ValueError("Unsafe path in ZIP")
            target = os.path.abspath(os.path.join(target_root, *parts))
            if os.path.commonpath((target_root, target)) != target_root:
                raise ValueError("Unsafe path in ZIP")
        archive.extractall(target_root)


async def _fetch_allowed_https_bytes(url: str, max_bytes: int, trust_env: bool = False, timeout_seconds: int = 25) -> Optional[bytes]:
    allowed_hosts = {
        "github.com", "raw.githubusercontent.com", "cdn.jsdelivr.net", "api.github.com",
        "objects.githubusercontent.com", "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com", *SKIN_MIRROR_NODES,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    current = str(url or "").strip()
    async with aiohttp.ClientSession(trust_env=trust_env) as session:
        for _ in range(5):
            parsed = urlsplit(current)
            host = str(parsed.hostname or "").lower()
            if parsed.scheme != "https" or host not in allowed_hosts or parsed.username or parsed.password:
                raise ValueError(f"Blocked download URL: {current}")
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("Invalid download URL port") from exc
            if port not in (None, 443):
                raise ValueError(f"Blocked download port: {port}")
            async with session.get(
                current,
                timeout=timeout,
                headers={"Accept": "application/octet-stream"},
                allow_redirects=False,
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if resp.status != 200:
                    return None
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            raise ValueError(f"Remote response exceeds {max_bytes} bytes")
                    except ValueError as exc:
                        if "exceeds" in str(exc):
                            raise
                chunks = []
                total = 0
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"Remote response exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ValueError("Too many download redirects")


async def _download_allowed_https_file(
    url: str, target_path: str, max_bytes: int, trust_env: bool = False, timeout_seconds: int = 300
) -> Tuple[str, int]:
    allowed_hosts = {
        "github.com", "raw.githubusercontent.com", "cdn.jsdelivr.net", "api.github.com",
        "objects.githubusercontent.com", "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com", *SKIN_MIRROR_NODES,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    current = str(url or "").strip()
    try:
        async with aiohttp.ClientSession(trust_env=trust_env) as session:
            for _ in range(5):
                parsed = urlsplit(current)
                host = str(parsed.hostname or "").lower()
                if parsed.scheme != "https" or host not in allowed_hosts or parsed.username or parsed.password:
                    raise ValueError(f"Blocked download URL: {current}")
                try:
                    port = parsed.port
                except ValueError as exc:
                    raise ValueError("Invalid download URL port") from exc
                if port not in (None, 443):
                    raise ValueError(f"Blocked download port: {port}")
                async with session.get(current, timeout=timeout, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if not location:
                            raise RuntimeError("Download redirect has no Location")
                        current = urljoin(current, location)
                        continue
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    content_length = resp.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise ValueError(f"Remote file exceeds {max_bytes} bytes")
                        except ValueError as exc:
                            if "exceeds" in str(exc):
                                raise
                    digest = hashlib.sha256()
                    total = 0
                    with open(target_path, "wb") as stream:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError(f"Remote file exceeds {max_bytes} bytes")
                            stream.write(chunk)
                            digest.update(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    return digest.hexdigest(), total
        raise ValueError("Too many download redirects")
    except Exception:
        if os.path.exists(target_path):
            os.remove(target_path)
        raise


async def _get_optimal_dlc_node() -> str:
    global _dlc_best_node
    if _dlc_best_node is not None:
        return _dlc_best_node

    logger.info("[Chisa DLC] 🌐 开始智能并发测速以寻找最优下载节点...")
    test_url = "https://raw.githubusercontent.com/dddada123/astrbot_plugin_chisa_still_eating_photo/main/index/catalog.json"
    nodes = list(SKIN_MIRROR_NODES)

    async def test_node(node: str) -> Tuple[str, float]:
        start = time.time()
        url = f"https://{node}/{test_url}" if node else test_url
        try:
            content = await _fetch_allowed_https_bytes(url, max_bytes=8 * 1024 * 1024, trust_env=False, timeout_seconds=10)
            if content:
                return node, int((time.time() - start) * 1000)
        except Exception:
            pass
        return node, float("inf")

    tasks = [asyncio.create_task(test_node(n)) for n in nodes]
    done, pending = await asyncio.wait(tasks, timeout=10, return_when=asyncio.ALL_COMPLETED)

    best_node = None
    best_lat = float("inf")

    for task in done:
        try:
            node, lat = task.result()
            if lat != float("inf"):
                logger.info(f"[Chisa DLC] 👉 [{node or 'direct'}] 响应延迟: {lat}ms")
                if lat < best_lat:
                    best_lat = lat
                    best_node = node
            else:
                logger.info(f"[Chisa DLC] 👉 [{node or 'direct'}] 状态: 🔴 超时/连通失败")
        except Exception:
            pass

    if best_node is None and pending:
        logger.info("[Chisa DLC] ⏳ 10秒内未捕获到极速节点，自动进入最高 30 秒深度探测模式...")
        done2, pending2 = await asyncio.wait(pending, timeout=20, return_when=asyncio.ALL_COMPLETED)
        for task in done2:
            try:
                node, lat = task.result()
                if lat != float("inf"):
                    logger.info(f"[Chisa DLC] 👉 [{node or 'direct'}] 响应延迟: {lat}ms")
                    if lat < best_lat:
                        best_lat = lat
                        best_node = node
                else:
                    logger.info(f"[Chisa DLC] 👉 [{node or 'direct'}] 状态: 🔴 超时/连通失败")
            except Exception:
                pass

    if best_node is not None:
        logger.info(f"[Chisa DLC] 👑 测速决议：最优节点锁定为 [{best_node or 'direct'}] ({best_lat}ms)。")
        _dlc_best_node = best_node
        return best_node

    logger.warning("[Chisa DLC] ❌ 国内加速镜像均不可用，回退到支持系统代理的 direct。")
    _dlc_best_node = "direct"
    return "direct"


def _get_store_dir(store_type: str, repo_id: Optional[str] = None) -> Path:
    plugin_dir = _get_base_dir()
    if store_type == "custom":
        if not repo_id:
            return plugin_dir / "Webui-PIC" / "Workshop" / "_empty_" / "index"
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(repo_id))
        return plugin_dir / "Webui-PIC" / "Workshop" / safe_id / "index"
    return plugin_dir / "Webui-PIC" / "Shop" / "index"


def _get_banner_dir() -> Path:
    return _get_base_dir() / "Webui-PIC" / "banner"


def _get_cover_dir(store_type: str, repo_id: Optional[str] = None) -> Path:
    pic_root = _get_base_dir() / "Webui-PIC"
    if store_type == "custom" and repo_id:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(repo_id))
        return pic_root / "Workshop" / "cover" / safe_id
    return pic_root / "Shop" / "cover"


def _skins_dir() -> Path:
    d = _get_base_dir() / "Webui-PIC" / "skins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_skin_source(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("/")
    if not text:
        return None
    url_match = re.fullmatch(
        r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?",
        text,
        re.IGNORECASE,
    )
    pair_match = re.fullmatch(
        r"([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?",
        text,
        re.IGNORECASE,
    )
    match = url_match or pair_match
    if not match:
        return None
    owner, repo_name = match.group(1), match.group(2)
    if owner in (".", "..") or repo_name in (".", ".."):
        return None
    return f"{owner.lower()}/{repo_name.lower()}"


def _skin_custom_sources() -> List[str]:
    sources: List[str] = []
    src_path = _skins_dir() / "_sources.json"
    if not src_path.exists():
        return sources
    try:
        with open(src_path, "r", encoding="utf-8-sig") as f:
            raw_sources = json.load(f)
    except Exception:
        return sources
    if not isinstance(raw_sources, list):
        return sources
    for raw_source in raw_sources:
        source = _normalize_skin_source(raw_source)
        if source and source != OFFICIAL_SKIN_SOURCE and source not in sources:
            sources.append(source)
    return sources


def _skin_source_allowed(source: str) -> bool:
    return source == OFFICIAL_SKIN_SOURCE or source in _skin_custom_sources()


def _skin_pref_path() -> Path:
    return _skins_dir() / "_skin_pref.json"


def _skin_store_dir(source: Optional[str]) -> Path:
    norm_source = _normalize_skin_source(source) or OFFICIAL_SKIN_SOURCE
    legacy_dir = None
    if norm_source == OFFICIAL_SKIN_SOURCE:
        folder = "OfficialWS"
    else:
        owner, repo_name = norm_source.split("/", 1)
        legacy_folder = f"{owner}_{repo_name}"
        folder = f"{legacy_folder}_{hashlib.sha256(norm_source.encode('utf-8')).hexdigest()[:10]}"
        legacy_dir = _skins_dir() / legacy_folder
    store_dir = _skins_dir() / folder
    if legacy_dir and not store_dir.exists() and legacy_dir.is_dir():
        try:
            shutil.copytree(str(legacy_dir), str(store_dir))
        except OSError as exc:
            logger.warning(f"[Chisa Skin] 迁移旧来源缓存失败: {exc}")
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


def _skin_config_path(skin_id: str, source: Optional[str] = None) -> Path:
    norm_source = _normalize_skin_source(source) if source else None
    if norm_source:
        return _skin_store_dir(norm_source) / "skin" / f"{skin_id}.json"
    return _skins_dir() / f"{skin_id}.json"


def _skin_asset_cache_dir(skin_id: str, source: Optional[str]) -> Path:
    return _skin_store_dir(source) / "skin"


def _safe_skin_asset_rel(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    rel = value.strip()
    if ".." in rel or "\\" in rel:
        return None
    if not re.fullmatch(r"skin/[A-Za-z0-9][A-Za-z0-9_.-]{0,110}", rel):
        return None
    if Path(rel).suffix.lower() not in SKIN_ASSET_EXTENSIONS:
        return None
    return rel


def _skin_is_glass(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    type_value = str(data.get("type", data.get("skin_type", data.get("theme_type", ""))) or "").strip().lower()
    glass_value = data.get("glass")
    glass_flag = glass_value is True or str(glass_value).strip().lower() in ("1", "true", "yes", "glass", "frosted", "毛玻璃")
    return type_value in ("glass", "frosted", "frosted-glass", "transparent", "毛玻璃") or glass_flag


def _validate_skin_json(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(data, dict):
        return None, "皮肤配置必须是 JSON 对象"
    if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
        return None, "schema_version 必须为整数 1"
    sid = str(data.get("id", "")).strip()
    if not sid or len(sid) > 40 or not re.fullmatch(r"[a-z0-9_]+", sid):
        return None, "id 必须为 1-40 位小写字母/数字/下划线"
    vars_in = data.get("vars")
    if not isinstance(vars_in, dict) or not vars_in:
        return None, "vars 不能为空"
    clean_vars = {}
    for k, v in vars_in.items():
        if k not in SKIN_VAR_WHITELIST:
            continue
        value = v.strip() if isinstance(v, str) else ""
        valid_value = COLOR_RE.match(value) or (k == "--shadow" and SHADOW_RE.match(value))
        if not value or not valid_value:
            return None, f"变量 {k} 的颜色格式不合法"
        clean_vars[k] = value
    if "--text" not in clean_vars or "--bg" not in clean_vars:
        return None, "vars 至少需要包含 --text 与 --bg"
    is_glass = _skin_is_glass(data)
    desc = data.get("desc")
    cleaned = {
        "schema_version": 1,
        "id": sid,
        "name": str(data.get("name", sid))[:40],
        "author": str(data.get("author", ""))[:40],
        "type": "glass" if is_glass else "solid",
        "desc": desc.strip()[:200] if isinstance(desc, str) else "",
        "vars": clean_vars,
        "glass": is_glass,
        "_skin_type_version": 1,
        "is_custom": True,
    }
    q = data.get("quotes")
    if isinstance(q, list):
        cleaned["quotes"] = [str(x)[:80] for x in q[:6] if str(x).strip()]
    if "assets" in data and not isinstance(data.get("assets"), dict):
        return None, "assets 必须是 JSON 对象"
    assets = data.get("assets") or {}
    bg = assets.get("bg") or data.get("bg") or data.get("background")
    if isinstance(bg, str) and bg.strip():
        rel = _safe_skin_asset_rel(bg)
        if not rel:
            return None, "assets.bg 必须是 skin/ 下的安全图片路径"
        cleaned["assets"] = {"bg": rel}
    return cleaned, None


def _load_cached_skin(skin_id: str, expected_source: Optional[str] = None) -> Optional[Dict[str, Any]]:
    expected = _normalize_skin_source(expected_source) if expected_source else None
    sources = [expected] if expected else [OFFICIAL_SKIN_SOURCE] + _skin_custom_sources()
    paths = [(source, _skin_config_path(skin_id, source)) for source in sources if source]
    paths.append((None, _skin_config_path(skin_id)))
    for path_source, path in paths:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            cleaned, err = _validate_skin_json(data)
            if err or not cleaned or cleaned.get("id") != skin_id:
                continue
            source = _normalize_skin_source(data.get("_source")) or path_source or OFFICIAL_SKIN_SOURCE
            if expected and source != expected:
                continue
            cleaned["_source"] = source
            cleaned["_official"] = source == OFFICIAL_SKIN_SOURCE
            target_path = _skin_config_path(skin_id, source)
            if path != target_path or data != cleaned:
                _atomic_write_json(target_path, cleaned)
                if path != target_path and path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass
            return cleaned
        except Exception:
            continue
    return None


async def _skin_fetch_raw(repo: Optional[str], rel_path: str, preferred_node: Optional[str] = None) -> Optional[bytes]:
    norm_repo = _normalize_skin_source(repo)
    if not norm_repo:
        return None
    source_bases = [
        f"https://raw.githubusercontent.com/{norm_repo}/main",
        f"https://raw.githubusercontent.com/{norm_repo}/refs/heads/main",
        f"https://cdn.jsdelivr.net/gh/{norm_repo}@main",
    ]
    node = str(preferred_node or "").strip().lower()
    if node == "smart":
        node = str(await _get_optimal_dlc_node() or "direct").strip().lower()
    if node not in ("direct", "") and node not in SKIN_MIRROR_NODES:
        node = "direct"

    async def _do_fetch(url: str, trust_env: bool) -> Optional[bytes]:
        try:
            return await _fetch_allowed_https_bytes(url, max_bytes=96 * 1024 * 1024, trust_env=trust_env, timeout_seconds=25)
        except Exception as exc:
            logger.warning(f"[Chisa Skin] fetch blocked/failed: {exc}")
            return None

    quoted_rel = quote(str(rel_path).lstrip("/"), safe="/._-")
    for base in source_bases:
        if "jsdelivr" in base:
            content = await _do_fetch(f"{base}/{quoted_rel}", trust_env=True)
            if content is None and node not in ("direct", ""):
                content = await _do_fetch(f"https://{node}/{base}/{quoted_rel}", trust_env=False)
            if content:
                return content
            continue

        url = f"{base}/{quoted_rel}"
        if node and node != "direct":
            url = f"https://{node}/{url}"
        content = await _do_fetch(url, trust_env=(node in ("direct", "")))
        if content is None and node and node != "direct":
            content = await _do_fetch(f"{base}/{quoted_rel}", trust_env=True)
        if content:
            return content
    return None


async def _get_or_fetch_skin_config(
    skin_id: str, source: Optional[str], force: bool = False, preferred_node: Optional[str] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    norm_source = _normalize_skin_source(source) or OFFICIAL_SKIN_SOURCE
    if not force:
        cached = _load_cached_skin(skin_id, norm_source)
        if cached:
            return cached, None
    raw_bytes = await _skin_fetch_raw(norm_source, f"skin/{skin_id}.json", preferred_node)
    if not raw_bytes:
        return None, "皮肤配置拉取失败"
    try:
        data = json.loads(raw_bytes.decode("utf-8-sig"))
    except Exception:
        return None, "皮肤 JSON 解析失败"
    cleaned, err = _validate_skin_json(data)
    if err or not cleaned or cleaned.get("id") != skin_id:
        return None, err or "皮肤配置校验失败"
    cleaned["_source"] = norm_source
    cleaned["_official"] = norm_source == OFFICIAL_SKIN_SOURCE
    _atomic_write_json(_skin_config_path(skin_id, norm_source), cleaned)
    return cleaned, None


# =========================================================================
# 端点实现
# =========================================================================

@router.post("/frontend_log")
async def page_frontend_log(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        msg = payload.get("msg", "")
        if msg:
            logger.info(f"[Chisa Skin Front] {msg}")
        return JSONResponse({"status": "success"})
    except Exception:
        return JSONResponse({"status": "error"}, status_code=500)


@router.get("/test_reflection")
async def page_test_reflection() -> JSONResponse:
    return JSONResponse({"status": "ok", "message": "GsCore reflection active"})


@router.get("/list_images")
async def page_list_images() -> JSONResponse:
    """获取本地图库清单"""
    try:
        data_dir = _get_base_dir()
        result: Dict[str, Any] = {
            "food": {"common": [], "world1": [], "world2": [], "world3": [], "world4": [], "world5": []},
            "drink": {"common": [], "world1": [], "world2": [], "world3": [], "world4": [], "world5": []},
            "darkfood": {"common": [], "world1": [], "world2": [], "world3": [], "world4": [], "world5": []},
            "chefs": [],
            "memes": {"common": [], "world1": [], "world2": [], "world3": [], "world4": [], "world5": []},
            "ganfanren": {},
        }

        for cat in ["food", "drink", "darkfood"]:
            for world in result[cat].keys():
                target_path = data_dir / cat / world
                if target_path.exists():
                    for file in target_path.iterdir():
                        if not file.name.startswith(".") and file.is_file():
                            result[cat][world].append(file.name)

        chef_path = data_dir / "chefs"
        if chef_path.exists():
            for file in chef_path.iterdir():
                if not file.name.startswith(".") and file.is_file():
                    result["chefs"].append(file.name)

        meme_path = data_dir / "memes"
        if meme_path.exists():
            for w in result["memes"].keys():
                w_dir = meme_path / w
                if w_dir.exists():
                    for mood in w_dir.iterdir():
                        if mood.is_dir():
                            for file in mood.iterdir():
                                if not file.name.startswith(".") and file.is_file():
                                    result["memes"][w].append(f"{mood.name}/{file.name}")

        gf_path = data_dir / "ganfanren"
        if gf_path.exists():
            for char_dir in gf_path.iterdir():
                if char_dir.is_dir():
                    result["ganfanren"][char_dir.name] = {"words": "", "images": []}
                    words_file = char_dir / "words.txt"
                    if words_file.exists():
                        try:
                            result["ganfanren"][char_dir.name]["words"] = words_file.read_text(encoding="utf-8")
                        except Exception:
                            pass
                    for file in char_dir.iterdir():
                        if not file.name.startswith(".") and file.name not in ("words.txt", "lines.txt") and file.is_file():
                            result["ganfanren"][char_dir.name]["images"].append(file.name)

        return JSONResponse({"status": "ok", "data": result})
    except Exception as e:
        logger.error(f"[ChisaEating] list_images error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/image-data")
async def page_image_data(path: str = Query(default="")) -> JSONResponse:
    """发送真实图片 Base64 数据"""
    try:
        if not path:
            return JSONResponse({"status": "error", "message": "No path provided"}, status_code=400)

        base_dir = _get_base_dir().resolve()
        full_path = (base_dir / path).resolve()

        if not str(full_path).startswith(str(base_dir)):
            return JSONResponse({"status": "error", "message": "Access denied"}, status_code=403)

        if not full_path.exists() or not full_path.is_file():
            if path == "Webui-PIC/Chisa.gif":
                fallback_path = (Path(__file__).parent / "web" / "manager" / "Chisa.gif").resolve()
                if fallback_path.exists():
                    full_path = fallback_path
                else:
                    return JSONResponse({"status": "error", "message": "File not found"}, status_code=404)
            else:
                return JSONResponse({"status": "error", "message": "File not found"}, status_code=404)

        file_size = full_path.stat().st_size
        if file_size > 8 * 1024 * 1024:
            return JSONResponse({"status": "error", "message": "Image too large for preview"}, status_code=413)

        media_type = mimetypes.guess_type(str(full_path))[0] or "image/png"
        raw_bytes = full_path.read_bytes()
        data_url = f"data:{media_type};base64,{base64.b64encode(raw_bytes).decode('ascii')}"

        return JSONResponse({"status": "ok", "data_url": data_url})
    except Exception as e:
        logger.error(f"[ChisaEating] page_image_data error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/upload_image")
async def page_upload_image(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        category = payload.get("category")
        world = payload.get("world", "")
        if ".." in world or "/" in world or "\\" in world:
            world = ""
        mode = payload.get("mode", "batch")
        single_chef = payload.get("single_chef", "").strip()
        single_dish = payload.get("single_dish", "").strip()
        if ".." in single_chef or "/" in single_chef or "\\" in single_chef:
            single_chef = ""
        if ".." in single_dish or "/" in single_dish or "\\" in single_dish:
            single_dish = ""
        mood = payload.get("mood", "think")
        files = payload.get("files", [])

        base_dir = _get_base_dir().resolve()

        if category in ["food", "drink", "darkfood"]:
            if not world:
                world = "common"
            target_dir = base_dir / category / world
        elif category == "chefs":
            target_dir = base_dir / "chefs"
        elif category == "memes":
            if not world:
                world = "common"
            target_dir = base_dir / "memes" / world / mood
        elif category == "ganfanren":
            char_name = payload.get("char_name", "")
            if ".." in char_name or "/" in char_name or "\\" in char_name:
                char_name = ""
            target_dir = base_dir / "ganfanren" / char_name
        else:
            return JSONResponse({"status": "error", "message": "Invalid category"}, status_code=400)

        target_dir = target_dir.resolve()
        if not str(target_dir).startswith(str(base_dir)):
            return JSONResponse({"status": "error", "message": "跨目录上传拒绝"}, status_code=403)
        target_dir.mkdir(parents=True, exist_ok=True)

        for f in files:
            fname = f.get("filename", "")
            if ".." in fname or "/" in fname or "\\" in fname:
                continue
            b64 = f.get("data", "")
            if not fname or not b64:
                continue

            ext = Path(fname).suffix
            if mode == "single" and category in ["food", "drink", "darkfood", "chefs"]:
                if category == "chefs":
                    base_name = f"{single_dish}"
                else:
                    base_name = f"【{single_chef}】{single_dish}" if single_chef else single_dish

                final_name = base_name + ext
                counter = 1
                while (target_dir / final_name).exists():
                    final_name = f"{base_name}_{counter}{ext}"
                    counter += 1
                fname = final_name

            if b64.startswith("data:"):
                b64 = b64.split(",")[1]
            img_path = (target_dir / fname).resolve()
            if not str(img_path).startswith(str(target_dir)):
                continue
            img_path.write_bytes(base64.b64decode(b64))

        return JSONResponse({"status": "ok"})
    except Exception as e:
        logger.error(f"[ChisaEating] upload_image error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/delete_image")
async def page_delete_image(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        paths = payload.get("paths", [])
        if "path" in payload and payload["path"] not in paths:
            paths.append(payload["path"])

        if not paths:
            return JSONResponse({"status": "error", "message": "No path provided"}, status_code=400)

        base_dir = _get_base_dir().resolve()
        deleted = 0
        for path_str in paths:
            if ".." in path_str:
                continue
            full_path = (base_dir / path_str).resolve()
            if not str(full_path).startswith(str(base_dir)):
                continue
            if full_path.exists() and full_path.is_file():
                full_path.unlink()
                deleted += 1

        if deleted > 0:
            return JSONResponse({"status": "ok", "message": f"成功删除 {deleted} 张图片"})
        return JSONResponse({"status": "error", "message": "File not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/rename_image")
async def page_rename_image(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        old_path = payload.get("old_path", "").strip()
        new_name = payload.get("new_name", "").strip()
        new_ext = payload.get("new_ext", "").strip()

        if not old_path or not new_name:
            return JSONResponse({"status": "error", "message": "参数缺失"}, status_code=400)

        if new_ext and not new_ext.startswith("."):
            new_ext = "." + new_ext

        base_dir = _get_base_dir().resolve()
        full_old_path = (base_dir / old_path).resolve()
        if not str(full_old_path).startswith(str(base_dir)) or not full_old_path.exists():
            return JSONResponse({"status": "error", "message": "原文件不存在或无权限"}, status_code=404)

        parent_dir = full_old_path.parent
        final_name = new_name + new_ext
        full_new_path = (parent_dir / final_name).resolve()
        if not str(full_new_path).startswith(str(base_dir)):
            return JSONResponse({"status": "error", "message": "非法的新文件名"}, status_code=400)

        if full_new_path != full_old_path:
            counter = 1
            base_new_name = new_name
            while full_new_path.exists():
                final_name = f"{base_new_name}_{counter}{new_ext}"
                full_new_path = (parent_dir / final_name).resolve()
                counter += 1
            full_old_path.rename(full_new_path)

        return JSONResponse({"status": "ok", "message": "重命名成功"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/add_ganfanren")
async def page_add_ganfanren(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        name = payload.get("name", "").strip()
        words = payload.get("words", "").strip()
        images = payload.get("images", [])

        if not name or ".." in name or "/" in name or "\\" in name:
            return JSONResponse({"status": "error", "message": "干饭人名字为空或包含非法字符"}, status_code=400)

        base_dir = _get_base_dir().resolve()
        gf_dir = (base_dir / "ganfanren" / name).resolve()
        if not str(gf_dir).startswith(str(base_dir / "ganfanren")):
            return JSONResponse({"status": "error", "message": "非法越权访问"}, status_code=403)
        gf_dir.mkdir(parents=True, exist_ok=True)

        words_path = gf_dir / "words.txt"
        words_path.write_text(words, encoding="utf-8")

        for img in images:
            fname = img.get("filename", "")
            if ".." in fname or "/" in fname or "\\" in fname:
                continue
            b64 = img.get("data", "")
            if fname and b64:
                if b64.startswith("data:"):
                    b64 = b64.split(",")[1]
                img_path = (gf_dir / fname).resolve()
                if not str(img_path).startswith(str(gf_dir)):
                    continue
                img_path.write_bytes(base64.b64decode(b64))

        return JSONResponse({"status": "ok", "message": f"成功招募干饭人 {name}！"})
    except Exception as e:
        logger.error(f"[ChisaEating] add_ganfanren error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/update_ganfanren")
async def page_update_ganfanren(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        name = payload.get("name", "").strip()
        words = payload.get("words", "").strip()
        if not name or ".." in name or "/" in name or "\\" in name:
            return JSONResponse({"status": "error", "message": "非法名称"}, status_code=400)

        base_dir = _get_base_dir().resolve()
        gf_dir = (base_dir / "ganfanren" / name).resolve()
        if not str(gf_dir).startswith(str(base_dir / "ganfanren")):
            return JSONResponse({"status": "error", "message": "非法越权访问"}, status_code=403)

        if gf_dir.exists():
            (gf_dir / "words.txt").write_text(words, encoding="utf-8")
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "error", "message": "Not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/delete_ganfanren")
async def page_delete_ganfanren(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        name = payload.get("name", "").strip()
        if not name or ".." in name or "/" in name or "\\" in name:
            return JSONResponse({"status": "error", "message": "非法名称"}, status_code=400)

        base_dir = _get_base_dir().resolve()
        gf_dir = (base_dir / "ganfanren" / name).resolve()
        if not str(gf_dir).startswith(str(base_dir / "ganfanren")):
            return JSONResponse({"status": "error", "message": "非法越权访问"}, status_code=403)

        if gf_dir.exists() and gf_dir.is_dir():
            shutil.rmtree(gf_dir)
            return JSONResponse({"status": "ok", "message": f"已成功删除干饭人 {name}"})
        return JSONResponse({"status": "error", "message": "该干饭人不存在"}, status_code=404)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/dlc_catalog")
async def page_dlc_catalog(store_type: str = "official", repo_id: str = "") -> JSONResponse:
    try:
        if store_type == "custom" and not repo_id.strip():
            return JSONResponse({"status": "missing"})

        index_dir = _get_store_dir(store_type, repo_id)
        catalog_path = index_dir / "catalog.json"
        if not catalog_path.exists():
            return JSONResponse({"status": "missing"})

        with open(catalog_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)

        meta_path = index_dir / "metadata.json"
        meta_data = {}
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8-sig") as mf:
                    meta_data = json.load(mf)
            except Exception:
                meta_data = {}

        return JSONResponse({"status": "success", "data": data, "metadata": meta_data})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/fetch_dlc_catalog")
async def page_fetch_dlc_catalog(request: Request) -> JSONResponse:
    global _dlc_best_node
    try:
        payload = await request.json()
        node = str(payload.get("node", "smart") or "smart").strip().lower()
        custom_url = payload.get("custom_url", "").strip()
        store_type = payload.get("store_type", "official")
        repo_id = payload.get("repo_id", "")

        if store_type == "custom" and not custom_url:
            return JSONResponse({"status": "error", "message": "请先输入第三方仓库地址"}, status_code=400)

        def extract_owner_repo(url: str) -> Tuple[Optional[str], Optional[str]]:
            m = re.search(r"github\.com[/:]([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/|$)", url)
            if m:
                return m.group(1), m.group(2)
            return None, None

        owner, repo_name = "dddada123", "astrbot_plugin_chisa_still_eating_photo"
        if store_type == "custom" and custom_url:
            c_owner, c_repo = extract_owner_repo(custom_url)
            if not c_owner or not c_repo:
                return JSONResponse(
                    {"status": "error", "message": "无法识别仓库地址，请填写形如 https://github.com/作者名/仓库名 的完整地址"},
                    status_code=400,
                )
            owner, repo_name = c_owner, c_repo

        source_bases = [
            f"https://raw.githubusercontent.com/{owner}/{repo_name}/main",
            f"https://raw.githubusercontent.com/{owner}/{repo_name}/refs/heads/main",
            f"https://cdn.jsdelivr.net/gh/{owner}/{repo_name}@main",
        ]

        if node == "smart":
            node = str(await _get_optimal_dlc_node() or "direct").strip().lower()
        if node not in ("direct", "") and node not in SKIN_MIRROR_NODES:
            return JSONResponse({"status": "error", "message": "Invalid download node"}, status_code=400)

        async def _do_fetch(url: str, use_node: str) -> Optional[bytes]:
            try:
                return await _fetch_allowed_https_bytes(
                    url,
                    max_bytes=8 * 1024 * 1024,
                    trust_env=(use_node in ("direct", "")),
                    timeout_seconds=20,
                )
            except Exception as exc:
                logger.warning(f"[Chisa DLC] blocked/failed catalog fetch: {exc}")
                return None

        async def _fetch_via(base: str, path: str) -> Optional[bytes]:
            if "jsdelivr" in base:
                content = await _do_fetch(f"{base}/{path}", "direct")
                if content is None and node != "direct":
                    content = await _do_fetch(f"https://{node}/{base}/{path}", node)
                return content
            url = f"{base}/{path}"
            if node and node != "direct":
                url = f"https://{node}/{url}"
            content = await _do_fetch(url, node)
            if content is None and node != "direct":
                content = await _do_fetch(f"{base}/{path}", "direct")
            return content

        async def fetch_repo_file(path: str) -> Tuple[Optional[bytes], str]:
            for idx, base in enumerate(source_bases):
                content = await _fetch_via(base, path)
                if content:
                    return content, f"source{idx+1}({base.split('/')[2]})"
            return None, "ALL_SOURCES_FAILED"

        (catalog_content, cat_src), (meta_content, meta_src) = await asyncio.gather(
            fetch_repo_file("index/catalog.json"),
            fetch_repo_file("Chisa_DLC_Metadata.json"),
        )

        logger.info(
            f"[Chisa DLC] 📥 拉取结果 via [{node or 'direct'}]: "
            f"catalog={'OK' if catalog_content else 'FAIL'} ({cat_src}) "
            f"metadata={'OK' if meta_content else 'FAIL'} ({meta_src})"
        )

        if not catalog_content:
            if payload.get("node", "smart") == "smart":
                _dlc_best_node = None
            return JSONResponse({"status": "error", "message": "Failed to fetch catalog.json"}, status_code=500)

        try:
            catalog_data = json.loads(catalog_content.decode("utf-8-sig"))
        except Exception as exc:
            return JSONResponse({"status": "error", "message": f"Invalid catalog JSON: {exc}"}, status_code=400)

        if not isinstance(catalog_data, list) or len(catalog_data) > 5000 or any(not isinstance(item, dict) for item in catalog_data):
            return JSONResponse({"status": "error", "message": "Catalog must be an array of at most 5000 objects"}, status_code=400)

        index_dir = _get_store_dir(store_type, repo_id)
        index_dir.mkdir(parents=True, exist_ok=True)
        catalog_path = index_dir / "catalog.json"
        _atomic_write_json(catalog_path, catalog_data)

        meta_path = index_dir / "metadata.json"
        meta_data: Dict[str, Any] = {}
        if meta_content:
            try:
                meta_data = json.loads(meta_content.decode("utf-8-sig"))
                if not isinstance(meta_data, dict):
                    meta_data = {}
            except Exception as parse_err:
                logger.warning(f"[Chisa DLC] ⚠️ Chisa_DLC_Metadata.json 解析失败: {parse_err}")

        if not meta_data:
            existing: Dict[str, Any] = {}
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8-sig") as mf:
                        existing = json.load(mf)
                except Exception:
                    existing = {}
            if existing and not existing.get("is_placeholder"):
                meta_data = existing
            else:
                if store_type == "custom":
                    meta_data = {
                        "store_name": f"{owner} 的创意工坊",
                        "author": owner,
                        "description": f"来自 {repo_name} 仓库的第三方内容",
                        "is_placeholder": True,
                    }
                else:
                    meta_data = {
                        "store_name": "千小妹官方云仓",
                        "author": "千小妹",
                        "description": "官方精选推荐内容",
                        "is_official": True,
                        "is_placeholder": True,
                    }

        with open(meta_path, "w", encoding="utf-8") as mf:
            json.dump(meta_data, mf, ensure_ascii=False)

        banner_dir = _get_banner_dir()
        banner_dir.mkdir(parents=True, exist_ok=True)
        if store_type == "official":
            banner_local_path = banner_dir / "shop_banner.jpg"
        else:
            safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(repo_id))
            banner_local_path = banner_dir / f"workshop_{safe_id}.jpg"

        banner_content = None
        banner_url = meta_data.get("banner_url", "")
        if banner_url and banner_url.startswith("https://raw.githubusercontent.com/"):
            b_url = banner_url
            if node and node != "direct":
                b_url = f"https://{node}/{banner_url}"
            banner_content = await _do_fetch(b_url, node)
            if banner_content is None and node and node != "direct":
                banner_content = await _do_fetch(banner_url, "direct")

        if not banner_content:
            candidates = [
                "assets/banner.png", "assets/banner.jpg", "assets/banner.gif", "assets/banner.webp",
                "banner.png", "banner.jpg", "banner.gif", "banner.webp",
            ]
            for cand in candidates:
                banner_content, _ = await fetch_repo_file(cand)
                if banner_content:
                    break

        if banner_content:
            banner_local_path.write_bytes(banner_content)

        if store_type == "custom" and repo_id:
            try:
                last_repo_path = _get_banner_dir().parent / "Workshop" / "_last_repo.json"
                last_repo_path.parent.mkdir(parents=True, exist_ok=True)
                with open(last_repo_path, "w", encoding="utf-8") as lf:
                    json.dump(
                        {"url": custom_url, "repo_id": repo_id, "store_name": meta_data.get("store_name", "")},
                        lf,
                        ensure_ascii=False,
                    )
            except Exception as persist_err:
                logger.warning(f"[Chisa DLC] ⚠️ 写入 last_repo 失败: {persist_err}")

        return JSONResponse({"status": "success", "metadata": meta_data})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/store_banner")
async def page_store_banner(store_type: str = "official", repo_id: str = "") -> JSONResponse:
    try:
        banner_dir = _get_banner_dir()
        if store_type == "official":
            path = banner_dir / "shop_banner.jpg"
        else:
            safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(repo_id))
            path = banner_dir / f"workshop_{safe_id}.jpg"

        if path.exists() and path.is_file():
            media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
            raw_bytes = path.read_bytes()
            data_url = f"data:{media_type};base64,{base64.b64encode(raw_bytes).decode('ascii')}"
            return JSONResponse({"status": "success", "data_url": data_url})
        return JSONResponse({"status": "missing"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/last_custom_repo")
async def page_get_last_custom_repo() -> JSONResponse:
    try:
        last_repo_path = _get_banner_dir().parent / "Workshop" / "_last_repo.json"
        if last_repo_path.exists():
            try:
                with open(last_repo_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if data and data.get("repo_id"):
                    return JSONResponse({"status": "success", "data": data})
            except Exception:
                pass
        return JSONResponse({"status": "missing"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/workshop_bookmarks")
async def page_get_workshop_bookmarks() -> JSONResponse:
    try:
        path = _get_banner_dir().parent / "Workshop" / "_bookmarks.json"
        bookmarks = []
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    bookmarks = json.load(f)
            except Exception:
                bookmarks = []
        if not isinstance(bookmarks, list):
            bookmarks = []
        return JSONResponse({"status": "success", "data": bookmarks})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/save_workshop_bookmarks")
async def page_save_workshop_bookmarks(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        bookmarks = payload.get("bookmarks", [])
        if not isinstance(bookmarks, list):
            return JSONResponse({"status": "error", "message": "Invalid bookmarks"}, status_code=400)
        cleaned = []
        for b in bookmarks[:20]:
            if isinstance(b, dict):
                url = str(b.get("url", "")).strip()[:300]
                name = str(b.get("name", ""))[:80]
                if url:
                    cleaned.append({"url": url, "name": name})
            elif isinstance(b, str) and b.strip():
                cleaned.append({"url": b.strip()[:300], "name": ""})
        path = _get_banner_dir().parent / "Workshop" / "_bookmarks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
        return JSONResponse({"status": "success", "data": cleaned})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/dlc_metadata")
async def page_dlc_metadata(store_type: str = "official", repo_id: str = "") -> JSONResponse:
    try:
        if store_type == "custom" and not repo_id.strip():
            return JSONResponse({"status": "missing"})
        index_dir = _get_store_dir(store_type, repo_id)
        meta_path = index_dir / "metadata.json"
        if not meta_path.exists():
            return JSONResponse({"status": "missing"})
        try:
            with open(meta_path, "r", encoding="utf-8-sig") as f:
                meta_data = json.load(f)
        except Exception:
            return JSONResponse({"status": "missing"})
        return JSONResponse({"status": "success", "data": meta_data})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/dlc_cover")
async def page_dlc_cover(file: str = "", store_type: str = "official", repo_id: str = "") -> JSONResponse:
    try:
        if not file or ".." in file or "/" in file or "\\" in file:
            return JSONResponse({"status": "error"}, status_code=400)

        cover_dir = _get_cover_dir(store_type, repo_id)
        full_path = cover_dir / file

        if not full_path.exists() or not full_path.is_file():
            return JSONResponse({"status": "missing"})

        media_type = mimetypes.guess_type(str(full_path))[0] or "image/jpeg"
        raw_bytes = full_path.read_bytes()
        data_url = f"data:{media_type};base64,{base64.b64encode(raw_bytes).decode('ascii')}"

        return JSONResponse({"status": "success", "data_url": data_url})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/fetch_single_cover")
async def page_fetch_single_cover(request: Request) -> JSONResponse:
    global _dlc_best_node
    try:
        payload = await request.json()
        filename = payload.get("file", "").strip()
        node = str(payload.get("node", "smart") or "smart").strip().lower()
        custom_url = payload.get("custom_url", "").strip()
        store_type = payload.get("store_type", "official")
        repo_id = payload.get("repo_id", "")

        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return JSONResponse({"status": "error"}, status_code=400)

        raw_base = "https://raw.githubusercontent.com/dddada123/astrbot_plugin_chisa_still_eating_photo/main"
        if custom_url:
            m_cover = re.search(r"github\.com[/:]([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/|$)", custom_url)
            if m_cover:
                raw_base = f"https://raw.githubusercontent.com/{m_cover.group(1)}/{m_cover.group(2)}/main"
            elif store_type == "custom":
                return JSONResponse({"status": "error", "message": "Invalid custom repository"}, status_code=400)

        original_url = f"{raw_base}/covers/{filename}"

        if node == "smart":
            node = str(await _get_optimal_dlc_node() or "direct").strip().lower()
        if node not in ("direct", "") and node not in SKIN_MIRROR_NODES:
            return JSONResponse({"status": "error", "message": "Invalid download node"}, status_code=400)

        url = original_url
        if node and node != "direct":
            url = f"https://{node}/{original_url}"

        try:
            content = await _fetch_allowed_https_bytes(
                url, max_bytes=16 * 1024 * 1024, trust_env=(node in ("direct", "")), timeout_seconds=15
            )
            if not content:
                raise RuntimeError("Empty cover response")
        except Exception as e:
            if payload.get("node", "smart") == "smart":
                _dlc_best_node = None
                return JSONResponse({"status": "error", "message": "ALL_NODES_FAILED"}, status_code=500)
            return JSONResponse({"status": "error"}, status_code=500)

        cover_dir = _get_cover_dir(store_type, repo_id)
        cover_dir.mkdir(parents=True, exist_ok=True)
        full_path = cover_dir / filename
        full_path.write_bytes(content)

        return JSONResponse({"status": "success"})
    except Exception as e:
        return JSONResponse({"status": "error"}, status_code=500)


@router.get("/get_dlc_downloaded")
async def page_get_dlc_downloaded(store_type: str = "official", repo_id: str = "") -> JSONResponse:
    try:
        base_dir = _get_base_dir()
        if store_type == "custom" and repo_id.strip():
            safe_rid = "".join(c if c.isalnum() or c in "-_" else "_" for c in repo_id.strip())
            json_path = base_dir / "Webui-PIC" / "Workshop" / safe_rid / "index" / "downloaded.json"
        else:
            json_path = base_dir / "Webui-PIC" / "Shop" / "index" / "downloaded.json"

        downloaded = []
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8-sig") as f:
                downloaded = json.load(f)

        return JSONResponse({"status": "success", "data": downloaded})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/get_download_progress")
async def page_get_download_progress(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        dlc_id = payload.get("id", "").strip()

        if not dlc_id or ".." in dlc_id or "/" in dlc_id or "\\" in dlc_id:
            return JSONResponse({"status": "error"}, status_code=400)

        temp_zip_path = _get_base_dir() / f"temp_{dlc_id}.zip"
        if temp_zip_path.exists():
            size = temp_zip_path.stat().st_size
            return JSONResponse({"status": "success", "size": size})
        return JSONResponse({"status": "success", "size": 0})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/download_dlc")
async def page_download_dlc(request: Request) -> JSONResponse:
    global _dlc_best_node
    try:
        payload = await request.json()
        dlc_id = payload.get("id", "").strip()
        expected_sha256 = payload.get("sha256", "").strip().lower()
        node = str(payload.get("node", "smart") or "smart").strip().lower()
        store_type = payload.get("store_type", "official")
        custom_url = payload.get("custom_url", "").strip()

        if not dlc_id or ".." in dlc_id or "/" in dlc_id or "\\" in dlc_id:
            return JSONResponse({"status": "error", "message": "Invalid DLC ID"}, status_code=400)
        if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return JSONResponse({"status": "error", "message": "Invalid SHA-256"}, status_code=400)
        if store_type == "custom" and not custom_url:
            return JSONResponse({"status": "error", "message": "Missing custom repository"}, status_code=400)

        if store_type == "custom" and custom_url:
            m_repo = re.search(r"github\.com[/:]([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/|$)", custom_url)
            if not m_repo:
                return JSONResponse({"status": "error", "message": "无法识别第三方仓库地址"}, status_code=400)
            release_base = f"https://github.com/{m_repo.group(1)}/{m_repo.group(2)}/releases/download/Chisa_Dlc_Store"
        else:
            release_base = "https://github.com/dddada123/astrbot_plugin_chisa_still_eating_photo/releases/download/Chisa_Dlc_Store"
        original_url = f"{release_base}/{dlc_id}.zip"

        if node == "smart":
            node = str(await _get_optimal_dlc_node() or "direct").strip().lower()
        if node not in ("direct", "") and node not in SKIN_MIRROR_NODES:
            return JSONResponse({"status": "error", "message": "Invalid download node"}, status_code=400)

        url = original_url
        if node and node != "direct":
            url = f"https://{node}/{original_url}"

        temp_zip_path = _get_base_dir() / f"temp_{dlc_id}.zip"
        temp_zip_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"[Chisa DLC] 开始下载 DLC 包: {url}")
        try:
            actual_sha256, _ = await _download_allowed_https_file(
                url,
                str(temp_zip_path),
                max_bytes=512 * 1024 * 1024,
                trust_env=(node in ("direct", "")),
                timeout_seconds=300,
            )
        except Exception as e:
            if payload.get("node", "smart") == "smart":
                _dlc_best_node = None
                return JSONResponse({"status": "error", "message": "ALL_NODES_FAILED"}, status_code=500)
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

        if expected_sha256 and actual_sha256 != expected_sha256:
            if temp_zip_path.exists():
                temp_zip_path.unlink()
            return JSONResponse(
                {"status": "error", "message": f"Hash mismatch! Expected {expected_sha256[:8]}, got {actual_sha256[:8]}"},
                status_code=500,
            )

        target_extract_dir = str(_get_base_dir())
        try:
            await asyncio.to_thread(_extract_dlc_zip_safe, str(temp_zip_path), target_extract_dir)

            if temp_zip_path.exists():
                temp_zip_path.unlink()

            logger.info(f"[Chisa DLC] 🎉 DLC {dlc_id} 部署完成！")

            repo_id_param = payload.get("repo_id", "").strip()
            if store_type == "custom" and repo_id_param:
                safe_rid = "".join(c if c.isalnum() or c in "-_" else "_" for c in repo_id_param)
                json_path = _get_base_dir() / "Webui-PIC" / "Workshop" / safe_rid / "index" / "downloaded.json"
            else:
                json_path = _get_base_dir() / "Webui-PIC" / "Shop" / "index" / "downloaded.json"

            json_path.parent.mkdir(parents=True, exist_ok=True)
            downloaded = []
            if json_path.exists():
                try:
                    with open(json_path, "r", encoding="utf-8-sig") as f:
                        downloaded = json.load(f)
                except Exception:
                    pass
            if dlc_id not in downloaded:
                downloaded.append(dlc_id)
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(downloaded, f, ensure_ascii=False)

            return JSONResponse({"status": "success"})
        except Exception as e:
            if temp_zip_path.exists():
                temp_zip_path.unlink()
            return JSONResponse({"status": "error", "message": f"Extraction failed: {str(e)}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# =========================================================================
# 皮肤相关端点
# =========================================================================

@router.get("/skin_index")
async def page_skin_index(
    source: str = "", force: bool = False, node: str = "smart"
) -> JSONResponse:
    norm_source = _normalize_skin_source(source) if source else OFFICIAL_SKIN_SOURCE
    if not norm_source or not _skin_source_allowed(norm_source):
        return JSONResponse({"status": "error", "message": "Invalid skin source"}, status_code=400)

    index_path = _skin_store_dir(norm_source) / "skin" / "index.json"
    if not force and index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return JSONResponse({"status": "success", "data": data})
        except Exception:
            pass

    content = await _skin_fetch_raw(norm_source, "skin/index.json", preferred_node=node)
    if not content:
        return JSONResponse({"status": "error", "message": "皮肤索引拉取失败"}, status_code=502)

    try:
        data = json.loads(content.decode("utf-8-sig"))
        _atomic_write_json(index_path, data)
        return JSONResponse({"status": "success", "data": data})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.post("/skin_get")
async def page_skin_get(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        skin_id = str(payload.get("skin_id", "")).strip()
        source = payload.get("source")
        force = bool(payload.get("force", False))
        node = payload.get("node", "smart")

        if not skin_id or not re.fullmatch(r"[a-z0-9_]{1,40}", skin_id):
            return JSONResponse({"status": "error", "message": "Invalid skin ID"}, status_code=400)

        cfg, err = await _get_or_fetch_skin_config(skin_id, source, force=force, preferred_node=node)
        if err or not cfg:
            return JSONResponse({"status": "error", "message": err or "Failed"}, status_code=502)
        return JSONResponse({"status": "success", "data": cfg})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/skin_local")
async def page_skin_local(source: str = "") -> JSONResponse:
    try:
        norm_source = _normalize_skin_source(source) if source else None
        skins: List[Dict[str, Any]] = []
        seen_ids = set()

        sources = [norm_source] if norm_source else [OFFICIAL_SKIN_SOURCE] + _skin_custom_sources()
        for s in sources:
            s_dir = _skin_store_dir(s) / "skin"
            if not s_dir.exists():
                continue
            for f in s_dir.glob("*.json"):
                if f.name == "index.json":
                    continue
                try:
                    with open(f, "r", encoding="utf-8-sig") as jf:
                        d = json.load(jf)
                    sid = d.get("id")
                    if sid and sid not in seen_ids:
                        seen_ids.add(sid)
                        d["_source"] = s
                        skins.append(d)
                except Exception:
                    continue

        return JSONResponse({"status": "success", "data": skins})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/skin_delete")
async def page_skin_delete(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        skin_id = str(payload.get("skin_id", "")).strip()
        source = payload.get("source")
        if not skin_id:
            return JSONResponse({"status": "error", "message": "Missing skin ID"}, status_code=400)

        norm_source = _normalize_skin_source(source) or OFFICIAL_SKIN_SOURCE
        cfg_file = _skin_config_path(skin_id, norm_source)
        deleted = False
        if cfg_file.exists():
            cfg_file.unlink()
            deleted = True

        return JSONResponse({"status": "success", "deleted": deleted})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/skin_sources")
async def page_skin_sources() -> JSONResponse:
    try:
        sources = [OFFICIAL_SKIN_SOURCE] + _skin_custom_sources()
        return JSONResponse({"status": "success", "data": sources})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/save_skin_sources")
async def page_save_skin_sources(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        raw_sources = payload.get("sources", [])
        if not isinstance(raw_sources, list):
            return JSONResponse({"status": "error", "message": "Invalid sources format"}, status_code=400)

        custom: List[str] = []
        for s in raw_sources:
            norm = _normalize_skin_source(s)
            if norm and norm != OFFICIAL_SKIN_SOURCE and norm not in custom:
                custom.append(norm)

        src_path = _skins_dir() / "_sources.json"
        _atomic_write_json(src_path, custom)
        return JSONResponse({"status": "success", "data": [OFFICIAL_SKIN_SOURCE] + custom})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/skin_pref")
async def page_get_skin_pref() -> JSONResponse:
    try:
        pref_path = _skin_pref_path()
        if not pref_path.exists():
            return JSONResponse({"status": "missing"})
        try:
            with open(pref_path, "r", encoding="utf-8-sig") as f:
                pref = json.load(f)
            return JSONResponse({"status": "success", "data": pref})
        except Exception:
            return JSONResponse({"status": "missing"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.post("/save_skin_pref")
async def page_save_skin_pref(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        skin_id = str(payload.get("skin_id", "")).strip()
        if not re.fullmatch(r"[a-z0-9_]{1,40}", skin_id):
            return JSONResponse({"status": "error", "message": "Invalid skin id"}, status_code=400)
        bg_blur = max(0, min(100, int(payload.get("bg_blur", 0))))
        source = _normalize_skin_source(payload.get("source", "")) or OFFICIAL_SKIN_SOURCE

        pref = {"skin_id": skin_id, "source": source, "bg_blur": bg_blur}
        _atomic_write_json(_skin_pref_path(), pref)
        return JSONResponse({"status": "ok", "data": {"saved": True, "preference": pref}})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


def _skin_asset_descriptor(local_path: Path, response_skin_id: Optional[str], source: str, cached: bool) -> Dict[str, Any]:
    size = local_path.stat().st_size
    chunk_size = SKIN_ASSET_CHUNK_BYTES
    return {
        "id": response_skin_id,
        "skin_id": response_skin_id,
        "source": source,
        "file": local_path.name,
        "mime": mimetypes.guess_type(str(local_path))[0] or "application/octet-stream",
        "size": size,
        "chunk_size": chunk_size,
        "chunk_count": (size + chunk_size - 1) // chunk_size,
        "delivery": "chunked",
        "cached": bool(cached),
    }


@router.get("/skin_asset")
async def page_skin_asset(
    file: str = "", skin_id: str = "", force: bool = False, source: str = "", node: str = ""
) -> JSONResponse:
    try:
        payload_file = str(file).strip()
        skin_id = str(skin_id).strip()
        force = bool(force)
        preferred_node = str(node).strip()

        if not skin_id or skin_id in BUILTIN_SKIN_ASSETS:
            expected_file = BUILTIN_SKIN_ASSETS.get(skin_id, payload_file)
            if expected_file not in SKIN_ASSETS:
                return JSONResponse({"status": "error", "message": "Unknown built-in skin asset"}, status_code=404)
            if payload_file and payload_file != expected_file:
                return JSONResponse({"status": "error", "message": "Built-in skin asset mismatch"}, status_code=400)
            payload_file = expected_file
            src = OFFICIAL_SKIN_SOURCE
            rel_path = SKIN_ASSETS[payload_file]
            local_path = _skins_dir() / payload_file
            response_skin_id = skin_id or None
        else:
            if not re.fullmatch(r"[a-z0-9_]{1,40}", skin_id):
                return JSONResponse({"status": "error", "message": "Invalid skin id"}, status_code=400)
            src = _normalize_skin_source(source) or OFFICIAL_SKIN_SOURCE
            config, err = await _get_or_fetch_skin_config(skin_id, src, False, preferred_node)
            if err or not config:
                return JSONResponse({"status": "error", "message": err or "Failed"}, status_code=400)
            rel_path = _safe_skin_asset_rel((config.get("assets") or {}).get("bg"))
            if not rel_path:
                return JSONResponse({"status": "error", "message": "Skin has no valid background asset"}, status_code=404)

            asset_name = Path(rel_path).name
            cache_dir = _skin_asset_cache_dir(skin_id, src)
            cache_dir.mkdir(parents=True, exist_ok=True)
            local_path = cache_dir / asset_name
            response_skin_id = skin_id

        if not force and local_path.exists() and local_path.stat().st_size > 0:
            return JSONResponse({"status": "success", "data": _skin_asset_descriptor(local_path, response_skin_id, src, True)})

        content = await _skin_fetch_raw(src, rel_path, preferred_node)
        if not content:
            return JSONResponse({"status": "error", "message": "Skin asset download failed"}, status_code=502)

        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)

        return JSONResponse({"status": "success", "data": _skin_asset_descriptor(local_path, response_skin_id, src, False)})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/skin_asset_chunk")
async def page_skin_asset_chunk(
    file: str = "", skin_id: str = "", index: int = -1, source: str = ""
) -> JSONResponse:
    try:
        payload_file = str(file).strip()
        skin_id = str(skin_id).strip()
        chunk_index = int(index)

        if not skin_id or skin_id in BUILTIN_SKIN_ASSETS:
            expected_file = BUILTIN_SKIN_ASSETS.get(skin_id, payload_file)
            if expected_file not in SKIN_ASSETS:
                return JSONResponse({"status": "error", "message": "Unknown built-in skin asset"}, status_code=404)
            src = OFFICIAL_SKIN_SOURCE
            local_path = _skins_dir() / expected_file
            response_skin_id = skin_id or None
        else:
            if not re.fullmatch(r"[a-z0-9_]{1,40}", skin_id):
                return JSONResponse({"status": "error", "message": "Invalid skin id"}, status_code=400)
            src = _normalize_skin_source(source) or OFFICIAL_SKIN_SOURCE
            config = _load_cached_skin(skin_id, src)
            if not config:
                return JSONResponse({"status": "error", "message": "Skin config is not cached"}, status_code=409)
            rel_path = _safe_skin_asset_rel((config.get("assets") or {}).get("bg"))
            if not rel_path:
                return JSONResponse({"status": "error", "message": "Skin has no valid background asset"}, status_code=404)
            asset_name = Path(rel_path).name
            local_path = _skin_asset_cache_dir(skin_id, src) / asset_name
            response_skin_id = skin_id

        if not local_path.is_file() or local_path.stat().st_size <= 0:
            return JSONResponse({"status": "error", "message": "Skin asset is not cached"}, status_code=409)

        size = local_path.stat().st_size
        chunk_size = SKIN_ASSET_CHUNK_BYTES
        chunk_count = (size + chunk_size - 1) // chunk_size
        if chunk_index < 0 or chunk_index >= chunk_count:
            return JSONResponse({"status": "error", "message": "Index out of range"}, status_code=416)

        with open(local_path, "rb") as stream:
            stream.seek(chunk_index * chunk_size)
            raw_chunk = stream.read(chunk_size)

        return JSONResponse({
            "status": "success",
            "data": {
                "id": response_skin_id,
                "skin_id": response_skin_id,
                "source": src,
                "index": chunk_index,
                "chunk_count": chunk_count,
                "chunk": base64.b64encode(raw_chunk).decode("ascii"),
            },
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# 将路由注册到框架 FastAPI app
app.include_router(router)
logger.info("[ChisaEating] FastAPI 路由已成功装配至 /api/chisaeating")
