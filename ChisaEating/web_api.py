"""ChisaEating WebUI and authenticated management API."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from gsuid_core.data_store import get_res_path
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_admin, require_auth

from .utils.downloader import STATE
from .utils.image_manager import _IMG_EXTS

_WEBUI_ROOT = Path(__file__).parent / "webui"
_DATA_ROOT = get_res_path() / "ChisaEating"


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise HTTPException(status_code=400, detail="非法资源路径")
    return path


def _resource_file(relative: str) -> Path:
    path = (_DATA_ROOT / _safe_relative_path(relative)).resolve()
    root = _DATA_ROOT.resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=400, detail="非法资源路径")
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="资源不存在")
    return path


@app.get("/app/chisaeating/", include_in_schema=False)
async def chisaeating_webui() -> FileResponse:
    return FileResponse(_WEBUI_ROOT / "index.html")


@app.get("/app/chisaeating/{asset:path}", include_in_schema=False)
async def chisaeating_asset(asset: str) -> FileResponse:
    path = (_WEBUI_ROOT / _safe_relative_path(asset)).resolve()
    root = _WEBUI_ROOT.resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=404, detail="资源不存在")
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(path)


@app.get("/api/chisaeating/catalog")
async def chisaeating_catalog(_: Any = Depends(require_auth)) -> dict[str, Any]:
    result: dict[str, list[str]] = {}
    for category in ("food", "drink", "darkfood", "chefs", "ganfanren"):
        directory = _DATA_ROOT / category
        result[category] = [
            path.relative_to(_DATA_ROOT).as_posix()
            for path in sorted(directory.rglob("*"))
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in _IMG_EXTS
        ] if directory.is_dir() else []
    return {"status": 0, "data": result}


@app.get("/api/chisaeating/status")
async def chisaeating_status(_: Any = Depends(require_auth)) -> dict[str, Any]:
    return {
        "status": 0,
        "data": {
            "downloading": STATE.is_downloading,
            "stage": STATE.stage,
            "downloaded_mb": STATE.downloaded_mb,
            "total_mb": STATE.total_mb,
            "percent": STATE.percent,
        },
    }


@app.get("/api/chisaeating/images")
async def chisaeating_images(
    category: str = "food",
    world: str = "world1",
    _: Any = Depends(require_auth),
) -> dict[str, Any]:
    if category not in {"food", "drink", "darkfood", "chefs", "memes", "ganfanren"}:
        raise HTTPException(status_code=400, detail="非法资源分类")
    if world not in {"world1", "world2", "world3", "world4", "world5", "common"}:
        raise HTTPException(status_code=400, detail="非法世界")
    directory = _DATA_ROOT / category / world
    if category in {"chefs", "ganfanren"}:
        directory = _DATA_ROOT / category
    if not directory.is_dir():
        return {"status": 0, "data": []}
    files = [
        path.relative_to(_DATA_ROOT).as_posix()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in _IMG_EXTS
    ]
    return {"status": 0, "data": files}


@app.get("/api/chisaeating/image/{resource:path}", include_in_schema=False)
async def chisaeating_image(resource: str, _: Any = Depends(require_auth)) -> FileResponse:
    path = _resource_file(resource)
    if path.suffix.lower() not in _IMG_EXTS:
        raise HTTPException(status_code=400, detail="非法图片类型")
    return FileResponse(path)


@app.post("/api/chisaeating/upload")
async def chisaeating_upload(
    relative_dir: str,
    file: UploadFile = File(...),
    _: Any = Depends(require_auth),
) -> dict[str, Any]:
    directory = (_DATA_ROOT / _safe_relative_path(relative_dir)).resolve()
    root = _DATA_ROOT.resolve()
    if root not in directory.parents and directory != root:
        raise HTTPException(status_code=400, detail="非法资源路径")
    if Path(file.filename or "").name != file.filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _IMG_EXTS:
        raise HTTPException(status_code=400, detail="不支持的图片类型")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / Path(file.filename or "").name
    temp = target.with_name(f".{target.name}.upload")
    size = 0
    try:
        with temp.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 10 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="图片超过 10MB 限制")
                output.write(chunk)
        temp.replace(target)
    finally:
        await file.close()
        if temp.exists():
            temp.unlink()
    return {"status": 0, "data": {"path": target.relative_to(_DATA_ROOT).as_posix()}}


@app.post("/api/chisaeating/rename")
async def chisaeating_rename(
    resource: str,
    name: str,
    _: Any = Depends(require_auth),
) -> dict[str, Any]:
    source = _resource_file(resource)
    target_name = Path(name).name
    if target_name != name or Path(target_name).suffix.lower() not in _IMG_EXTS:
        raise HTTPException(status_code=400, detail="非法目标文件名")
    target = source.with_name(target_name)
    if target.exists():
        raise HTTPException(status_code=409, detail="目标文件已存在")
    source.rename(target)
    return {"status": 0, "data": {"path": target.relative_to(_DATA_ROOT).as_posix()}}


@app.delete("/api/chisaeating/image/{resource:path}")
async def chisaeating_delete(
    resource: str,
    _: Any = Depends(require_admin),
) -> dict[str, Any]:
    path = _resource_file(resource)
    path.unlink()
    return {"status": 0, "data": {"deleted": resource}}
