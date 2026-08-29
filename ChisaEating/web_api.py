"""Plugin-owned WebUI session authentication and management routes."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from gsuid_core.data_store import get_res_path
from gsuid_core.webconsole.app_app import app

from .chisaeating_config import CHISA_CONFIG
from .utils.downloader import STATE
from .utils.image_manager import _IMG_EXTS

_WEBUI_ROOT = Path(__file__).parent / "webui"
_DATA_ROOT = get_res_path() / "ChisaEating"
_SESSION_TTL = 24 * 60 * 60
_SESSIONS: dict[str, float] = {}


def _config(key: str, default: str = "") -> str:
    return str(CHISA_CONFIG.get_config(key).data or default).strip()


def _session(token: str | None) -> str:
    now = time.time()
    for key, expires in list(_SESSIONS.items()):
        if expires <= now:
            del _SESSIONS[key]
    if not token or token not in _SESSIONS:
        raise HTTPException(status_code=401, detail="请先登录 ChisaEating WebUI")
    return token


def _safe_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise HTTPException(status_code=400, detail="非法资源路径")
    return path


def _resource_file(relative: str) -> Path:
    root = _DATA_ROOT.resolve()
    path = (root / _safe_path(relative)).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=400, detail="非法资源路径")
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="资源不存在")
    return path


def _webui_user(token: str | None = Cookie(default=None, alias="chisaeating_session")) -> str:
    return _session(token)


@app.get("/app/app/chisaeating", include_in_schema=False)
@app.get("/app/app/chisaeating/", include_in_schema=False)
async def legacy_webui_route() -> RedirectResponse:
    return RedirectResponse("/app/chisaeating/", status_code=307)


@app.get("/app/chisaeating/", include_in_schema=False)
async def webui_index() -> FileResponse:
    return FileResponse(_WEBUI_ROOT / "index.html")


@app.get("/app/chisaeating/{asset:path}", include_in_schema=False)
async def webui_asset(asset: str) -> FileResponse:
    root = _WEBUI_ROOT.resolve()
    path = (root / _safe_path(asset)).resolve()
    if path != root and root not in path.parents or not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="页面资源不存在")
    return FileResponse(path)


@app.post("/api/chisaeating/login")
async def login(username: str, password: str) -> dict[str, Any]:
    configured_password = _config("webui_password")
    if not configured_password:
        raise HTTPException(status_code=503, detail="请先在 GsCore 插件配置中设置 WebUI 密码")
    if not hmac.compare_digest(username, _config("webui_username", "admin")) or not hmac.compare_digest(password, configured_password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + _SESSION_TTL
    response = JSONResponse({"status": 0, "data": {"username": _config("webui_username", "admin")}})
    response.set_cookie("chisaeating_session", token, max_age=_SESSION_TTL, httponly=True, samesite="strict", secure=False)
    return response


@app.post("/api/chisaeating/logout")
async def logout(token: str | None = Cookie(default=None, alias="chisaeating_session")) -> dict[str, Any]:
    if token:
        _SESSIONS.pop(token, None)
    return {"status": 0, "data": {}}


@app.get("/api/chisaeating/session")
async def session(_: str = Depends(_webui_user)) -> dict[str, Any]:
    return {"status": 0, "data": {"authenticated": True, "username": _config("webui_username", "admin")}}


@app.get("/api/chisaeating/status")
async def status(_: str = Depends(_webui_user)) -> dict[str, Any]:
    return {"status": 0, "data": {"downloading": STATE.is_downloading, "stage": STATE.stage, "downloaded_mb": STATE.downloaded_mb, "total_mb": STATE.total_mb, "percent": STATE.percent}}


@app.get("/api/chisaeating/catalog")
async def catalog(_: str = Depends(_webui_user)) -> dict[str, Any]:
    result: dict[str, list[str]] = {}
    for category in ("food", "drink", "darkfood", "chefs", "ganfanren"):
        directory = _DATA_ROOT / category
        result[category] = [path.relative_to(_DATA_ROOT).as_posix() for path in sorted(directory.rglob("*")) if path.is_file() and not path.is_symlink() and path.suffix.lower() in _IMG_EXTS] if directory.is_dir() else []
    return {"status": 0, "data": result}


@app.get("/api/chisaeating/images")
async def images(category: str = "food", world: str = "world1", _: str = Depends(_webui_user)) -> dict[str, Any]:
    if category not in {"food", "drink", "darkfood", "chefs", "memes", "ganfanren"} or world not in {"world1", "world2", "world3", "world4", "world5", "common"}:
        raise HTTPException(status_code=400, detail="非法资源分类")
    directory = _DATA_ROOT / category / world
    if category in {"chefs", "ganfanren"}:
        directory = _DATA_ROOT / category
    return {"status": 0, "data": [p.relative_to(_DATA_ROOT).as_posix() for p in sorted(directory.rglob("*")) if p.is_file() and not p.is_symlink() and p.suffix.lower() in _IMG_EXTS] if directory.is_dir() else []}


@app.get("/api/chisaeating/image/{resource:path}")
async def image(resource: str, _: str = Depends(_webui_user)) -> FileResponse:
    path = _resource_file(resource)
    if path.suffix.lower() not in _IMG_EXTS:
        raise HTTPException(status_code=400, detail="非法图片类型")
    return FileResponse(path)


@app.get("/api/chisaeating/skin")
async def skin(_: str = Depends(_webui_user)) -> dict[str, Any]:
    return {"status": 0, "data": {"skins": ["maple_dew", "chisa_red_black", "chisa_red_white", "yy_xuanling"], "default": "maple_dew"}}


@app.get("/api/chisaeating/download/progress")
async def download_progress(_: str = Depends(_webui_user)) -> dict[str, Any]:
    return {"status": 0, "data": {"downloading": STATE.is_downloading, "stage": STATE.stage, "percent": STATE.percent}}
