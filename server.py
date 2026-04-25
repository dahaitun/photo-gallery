#!/usr/bin/env python3
"""
📸 私人相册服务 v2 — 主程序
基于 SQLite 索引 + 增量扫描的高性能相册服务
"""

import os
import io
import json
import asyncio
import subprocess
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
import aiofiles
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import (
    HTMLResponse, FileResponse, StreamingResponse, JSONResponse
)
from fastapi.staticfiles import StaticFiles
from PIL import Image, ExifTags, UnidentifiedImageError

# ── 导入自定义模块 ────────────────────────────────────────────
import db as DB
import scanner as Scanner
import thumbnail as Thumb

# ── 读取配置 ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
DATA_DIR = BASE_DIR / "data"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

CONFIG_PATH  = BASE_DIR / "config.yaml"
# 兼容旧配置位置
LIBRARY_FILE_OLD = BASE_DIR / "library.json"
LIBRARY_FILE = DATA_DIR / "library.json"
COVER_PREFS_OLD = BASE_DIR / "covers.json"

HOST         = cfg.get("host", "0.0.0.0")
PORT         = cfg.get("port", 8080)
THUMB_SIZE   = cfg.get("thumbnail_size", 320)
SUPPORTED    = {ext.lower() for ext in cfg.get("supported_formats", ["jpg","jpeg","png","gif","webp"])}
VIDEO_FORMATS = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "flv", "wmv", "3gp", "ts"}

# 确保 data 目录存在
DATA_DIR.mkdir(exist_ok=True)
Thumb.CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── 后台缩略图预生成线程池（和请求处理线程池隔离，不阻塞灯箱请求） ──
import concurrent.futures
_prefetch_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="thumb-prefetch")
_prefetch_lock = threading.Lock()  # 防止同一相册重复触发
_prefetching_dirs = set()  # 正在预生成的目录集合

# ── 迁移旧配置文件到 data/ ────────────────────────────────────
def _migrate_data_files():
    """首次运行时把旧配置迁移到 data/ 目录"""
    if LIBRARY_FILE_OLD.exists() and not LIBRARY_FILE.exists():
        import shutil
        shutil.copy2(str(LIBRARY_FILE_OLD), str(LIBRARY_FILE))
        print("   已迁移 library.json → data/library.json")
    # covers.json 迁移到数据库（由扫描器处理）

_migrate_data_files()


# ── 相册库管理 ────────────────────────────────────────────────
def load_library() -> list:
    if LIBRARY_FILE.exists():
        try:
            with open(LIBRARY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    default = str(cfg["photo_root"])
    save_library([{"name": "默认相册", "path": default, "enabled": True}])
    return [{"name": "默认相册", "path": default, "enabled": True}]

def save_library(libs: list):
    with open(LIBRARY_FILE, "w", encoding="utf-8") as f:
        json.dump(libs, f, ensure_ascii=False, indent=2)

def get_all_photo_roots() -> list:
    libs = load_library()
    enabled = [lib for lib in libs if lib.get("enabled")]
    if not enabled and libs:
        libs[0]["enabled"] = True
        save_library(libs)
        enabled = [libs[0]]
    if not enabled:
        return [Path(cfg["photo_root"]).expanduser()]
    return [Path(lib["path"]).expanduser() for lib in enabled]

# 向后兼容
def migrate_library_format():
    if not LIBRARY_FILE.exists():
        return
    try:
        with open(LIBRARY_FILE, "r", encoding="utf-8") as f:
            libs = json.load(f)
        migrated = False
        for lib in libs:
            if "active" in lib and "enabled" not in lib:
                lib["enabled"] = lib.pop("active")
                migrated = True
        if migrated:
            save_library(libs)
    except Exception:
        pass

migrate_library_format()

PHOTO_ROOTS = get_all_photo_roots()
PHOTO_ROOT = PHOTO_ROOTS[0] if PHOTO_ROOTS else Path(cfg["photo_root"]).expanduser()

# ── 封面偏好（兼容旧 covers.json + 数据库） ─────────────────
COVER_PREFERENCES = DATA_DIR / "covers.json"
# 如果旧的 covers.json 存在，迁移过来
if COVER_PREFS_OLD.exists() and not COVER_PREFERENCES.exists():
    import shutil
    shutil.copy2(str(COVER_PREFS_OLD), str(COVER_PREFERENCES))

def load_cover_preferences() -> dict:
    if COVER_PREFERENCES.exists():
        try:
            with open(COVER_PREFERENCES, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cover_preferences(prefs: dict):
    with open(COVER_PREFERENCES, "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False, indent=2)

# ── 初始化数据库 ──────────────────────────────────────────────
DB.init_db()
print("   数据库已初始化")

# ── 工具函数 ──────────────────────────────────────────────────
def is_image(path: Path) -> bool:
    return path.suffix.lstrip(".").lower() in SUPPORTED

def is_video(path: Path) -> bool:
    return path.suffix.lstrip(".").lower() in VIDEO_FORMATS

def safe_rel(path: Path, root: Path = None) -> str:
    if root is None:
        root = PHOTO_ROOT
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name

def resolve_media_path(path: str, root: str = None) -> Path:
    if root:
        base = Path(root).expanduser()
    else:
        base = PHOTO_ROOT
    img_path = base / path
    img_path = img_path.resolve()
    return img_path, base


# ── FastAPI 应用 ──────────────────────────────────────────────
app = FastAPI(title="私人相册", version="2.0.0")
# 确保 static 目录存在（Starlette 要求挂载时目录必须存在）
_static_dir = BASE_DIR / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# 全局变量
_scanner: Optional[Scanner.LibraryScanner] = None


# ══════════════════════════════════════════════════════════════
# 页面路由
# ══════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = BASE_DIR / "templates" / "index.html"
    async with aiofiles.open(html_file, "r", encoding="utf-8") as f:
        return HTMLResponse(content=await f.read())


# ══════════════════════════════════════════════════════════════
# 相册浏览 API（数据库驱动）
# ══════════════════════════════════════════════════════════════

@app.get("/api/albums")
async def list_albums(path: str = ""):
    """
    列出指定路径下的子相册（文件夹）和图片
    从数据库查询，合并所有已启用相册库的内容
    """
    roots = get_all_photo_roots()
    cover_prefs = load_cover_preferences()

    all_albums = []
    all_images = []

    for root in roots:
        library_root = str(root.resolve())
        target = root / path if path else root
        target = target.resolve()

        if not str(target).startswith(str(root.resolve())):
            continue
        if not target.exists() or not target.is_dir():
            continue

        # 获取子目录
        if path:
            child_dirs = DB.get_child_dirs(library_root, path)
        else:
            child_dirs = DB.get_all_root_dirs(library_root)

        # 检查是否有子文件夹
        has_subdirs = len(child_dirs) > 0

        # 如果是根目录（path=""）且该库下没有子文件夹，把整个库作为相册卡片展示
        if not path and not has_subdirs:
            # 获取根目录的直接文件
            dir_info = DB.get_directory(library_root, "")
            img_count = (dir_info or {}).get('img_count', 0)
            vid_count = (dir_info or {}).get('vid_count', 0)

            # 获取预览图
            preview_files = DB.get_dir_preview_images(library_root, "", 4)
            preview_images = [f['rel_path'] for f in preview_files]

            if img_count + vid_count > 0:
                cover = None
                cover_type = "image"
                if preview_images:
                    cover = preview_images[0]
                    f_info = preview_files[0] if preview_files else None
                    cover_type = f_info['type'] if f_info else "image"

                # 检查自定义封面
                dir_db = DB.get_directory(library_root, "")
                if dir_db and dir_db.get('cover_path'):
                    cover = dir_db['cover_path']
                    cover_type = dir_db.get('cover_type', 'image')

                preview_types = [f['type'] for f in preview_files]

                all_albums.append({
                    "name": root.name,
                    "path": "",
                    "img_count": img_count,
                    "vid_count": vid_count,
                    "cover": cover,
                    "cover_type": cover_type,
                    "preview_images": preview_images[:4],
                    "preview_types": preview_types[:4],
                    "is_dir_only": False,
                    "library_root": library_root,
                    "is_virtual_root": True,
                    "meta_favorite": dir_db.get('meta_favorite', 0) if dir_db else 0,
                    "meta_title": dir_db.get('meta_title') if dir_db else None,
                    "meta_tags": dir_db.get('meta_tags') if dir_db else None,
                })
            continue

        # 渲染子目录为相册卡片
        for d in child_dirs:
            drel = d['rel_path']
            img_count = d.get('img_count', 0)
            vid_count = d.get('vid_count', 0)

            # 获取预览图
            preview_files = DB.get_dir_preview_images(library_root, drel, 4)
            preview_images = [f['rel_path'] for f in preview_files]

            # 纯子目录相册（没有直接媒体文件）：递归获取子目录中的图片做 mosaic 封面
            is_dir_only = img_count == 0 and vid_count == 0
            if is_dir_only and not preview_images:
                recursive_previews = DB.get_dir_recursive_preview_images(library_root, drel, 4)
                preview_images = [f['rel_path'] for f in recursive_previews]
                # 附带 type 信息用于前端判断是图片还是视频缩略图
                preview_types = [f['type'] for f in recursive_previews]
            else:
                preview_types = [f['type'] for f in preview_files]

            # 封面选择：自定义 > 数据库 > 预览图第一张
            cover = None
            cover_type = "image"
            if d.get('cover_path'):
                cover = d['cover_path']
                cover_type = d.get('cover_type', 'image')
            elif preview_images:
                cover = preview_images[0]
                cover_type = preview_types[0] if preview_types else "image"

            all_albums.append({
                "name": d['dir_name'],
                "path": drel,
                "img_count": img_count,
                "vid_count": vid_count,
                "cover": cover,
                "cover_type": cover_type,
                "preview_images": preview_images[:4],
                "preview_types": preview_types[:4],
                "is_dir_only": is_dir_only,
                "library_root": library_root,
                "meta_favorite": d.get('meta_favorite', 0),
                "meta_title": d.get('meta_title'),
                "meta_tags": d.get('meta_tags'),
            })

        # 获取当前目录的直接媒体文件
        files = DB.get_files_by_dir(library_root, path if path else "")
        for f in files:
            all_images.append({
                "name": f['filename'],
                "path": f['rel_path'],
                "size": f['size'],
                "mtime": f['mtime'],
                "type": f['type'],
                "library_root": library_root,
                "meta_favorite": f.get('favorite', 0),
                "meta_rating": f.get('rating', 0),
                "meta_title": f.get('meta_title'),
                "meta_tags": f.get('meta_tags'),
            })

    # 面包屑
    breadcrumbs = []
    if path:
        parts = Path(path).parts
        accumulated = ""
        for part in parts:
            accumulated = str(Path(accumulated) / part) if accumulated else part
            breadcrumbs.append({"name": part, "path": accumulated})

    return JSONResponse({
        "current_path": path,
        "breadcrumbs": breadcrumbs,
        "albums": all_albums,
        "images": all_images,
        "total_albums": len(all_albums),
        "total_images": sum(1 for i in all_images if i["type"] == "image"),
        "total_videos": sum(1 for i in all_images if i["type"] == "video"),
    })


# ══════════════════════════════════════════════════════════════
# 全局搜索 API（数据库驱动 + 分页）
# ══════════════════════════════════════════════════════════════

@app.get("/api/search")
async def search_media(
    filter_type: str = Query("all"),
    sort: str = Query("name"),
    sort_dir: str = Query("asc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
):
    """全局搜索（数据库查询，支持分页）"""
    items, total = DB.get_files_recursive(
        filter_type=filter_type,
        sort=sort,
        sort_dir=sort_dir,
        offset=offset,
        limit=limit,
    )

    images = []
    for f in items:
        images.append({
            "name": f['filename'],
            "path": f['rel_path'],
            "size": f['size'],
            "mtime": f['mtime'],
            "type": f['type'],
            "library_root": f['library_root'],
            "meta_favorite": f.get('favorite', 0),
            "meta_rating": f.get('rating', 0),
            "meta_title": f.get('meta_title'),
            "meta_tags": f.get('meta_tags'),
        })

    return JSONResponse({
        "images": images,
        "total": total,
        "total_images": sum(1 for i in images if i["type"] == "image"),
        "total_videos": sum(1 for i in images if i["type"] == "video"),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
    })


# ══════════════════════════════════════════════════════════════
# 收藏 & 标签搜索 API
# ══════════════════════════════════════════════════════════════

@app.get("/api/favorites")
async def get_favorites(
    filter_type: str = Query("all"),
    sort: str = Query("name"),
    sort_dir: str = Query("asc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
):
    """获取收藏的媒体"""
    items, total = DB.get_favorites(
        filter_type=filter_type, sort=sort, sort_dir=sort_dir,
        offset=offset, limit=limit,
    )
    images = [{
        "name": f['filename'], "path": f['rel_path'], "size": f['size'],
        "mtime": f['mtime'], "type": f['type'], "library_root": f['library_root'],
        "meta_favorite": f.get('favorite', 0), "meta_rating": f.get('rating', 0),
        "meta_title": f.get('meta_title'), "meta_tags": f.get('meta_tags'),
    } for f in items]
    return JSONResponse({
        "images": images, "total": total,
        "total_images": sum(1 for i in images if i["type"] == "image"),
        "total_videos": sum(1 for i in images if i["type"] == "video"),
        "offset": offset, "limit": limit,
        "has_more": offset + limit < total,
    })


@app.get("/api/tag-search")
async def search_by_tags_api(
    tags: str = Query(""),
    filter_type: str = Query("all"),
    sort: str = Query("name"),
    sort_dir: str = Query("asc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    target_type: str = Query("all"),
):
    """按标签搜索（文件/目录/全部）"""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if not tag_list:
        return JSONResponse({"images": [], "albums": [], "total": 0, "offset": offset, "limit": limit, "has_more": False})

    results_images = []
    results_albums = []

    # 搜索文件级标签
    if target_type in ("all", "file"):
        items, total_files = DB.search_by_tags(
            tags=tag_list, filter_type=filter_type,
            sort=sort, sort_dir=sort_dir,
            offset=offset, limit=limit,
            target_type="file",
        )
        for f in items:
            results_images.append({
                "name": f['filename'], "path": f['rel_path'], "size": f['size'],
                "mtime": f['mtime'], "type": f['type'], "library_root": f['library_root'],
                "meta_favorite": f.get('favorite', 0), "meta_rating": f.get('rating', 0),
                "meta_title": f.get('meta_title'), "meta_tags": f.get('meta_tags'),
            })

    # 搜索目录级标签
    if target_type in ("all", "directory"):
        items, total_dirs = DB.search_by_tags(
            tags=tag_list, filter_type=filter_type,
            sort=sort, sort_dir=sort_dir,
            offset=0, limit=500,
            target_type="directory",
        )
        cover_prefs = load_cover_preferences()
        for d in items:
            lib_root = d.get('library_root', '')
            drel = d.get('dir_rel_path') or d.get('rel_path', '')
            # 获取目录名称（可能为空，如根目录）
            dir_name = d.get('dir_name') or (drel.split('/')[-1] if drel else lib_root.split('/')[-1] if lib_root else '根目录')
            # 获取预览图
            preview_files = DB.get_dir_preview_images(lib_root, drel, 4) if drel else []
            preview_images = [f['rel_path'] for f in preview_files]
            cover = None
            cover_type = "image"
            if d.get('cover_path'):
                cover = d['cover_path']
                cover_type = d.get('cover_type', 'image')
            elif preview_images:
                cover = preview_images[0]
                f_info = preview_files[0] if preview_files else None
                cover_type = f_info['type'] if f_info else "image"
            # 解析标签
            meta_tags = d.get('tags')
            if isinstance(meta_tags, str):
                try:
                    import json as _json
                    meta_tags = _json.loads(meta_tags)
                except:
                    meta_tags = []
            elif not isinstance(meta_tags, list):
                meta_tags = []
            results_albums.append({
                "name": dir_name,
                "path": drel,
                "img_count": d.get('img_count') or 0,
                "vid_count": d.get('vid_count') or 0,
                "cover": cover,
                "cover_type": cover_type,
                "preview_images": preview_images[:4],
                "library_root": lib_root,
                "meta_favorite": d.get('favorite', 0),
                "meta_tags": meta_tags,
            })

    total = len(results_images) + len(results_albums)
    return JSONResponse({
        "images": results_images,
        "albums": results_albums,
        "total": total,
        "total_images": sum(1 for i in results_images if i["type"] == "image"),
        "total_videos": sum(1 for i in results_images if i["type"] == "video"),
        "total_albums": len(results_albums),
        "offset": offset, "limit": limit,
        "has_more": offset + limit < total if target_type == "file" else False,
    })


@app.get("/api/all-tags")
async def get_all_tags():
    """获取所有已使用的标签"""
    tags = DB.get_all_tags()
    return JSONResponse({"tags": tags})


# ══════════════════════════════════════════════════════════════
# 媒体文件 API
# ══════════════════════════════════════════════════════════════

@app.get("/api/thumbnail")
async def get_thumbnail(path: str = Query(...), root: str = Query(default=None)):
    """生成并返回缩略图"""
    img_path, base = resolve_media_path(path, root)
    if not str(img_path).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not img_path.exists() or not img_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    loop = asyncio.get_event_loop()
    cache_path = await loop.run_in_executor(None, Thumb.generate_thumbnail, img_path)

    # 更新数据库中的缩略图状态
    try:
        DB.mark_thumbnail_generated(str(base), path)
    except Exception:
        pass

    return FileResponse(cache_path, media_type="image/jpeg")


@app.get("/api/photo")
async def get_photo(path: str = Query(...), root: str = Query(default=None)):
    """返回原图"""
    img_path, base = resolve_media_path(path, root)
    if not str(img_path).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not img_path.exists() or not img_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    suffix = img_path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp",
        ".tiff": "image/tiff", ".tif": "image/tiff",
    }
    media_type = media_types.get(suffix, "image/jpeg")
    return FileResponse(img_path, media_type=media_type)


@app.get("/api/video")
async def get_video(path: str = Query(...), request: "Request" = None, root: str = Query(default=None)):
    """流式传输视频，支持 Range 请求"""
    from fastapi import Request
    vid_path, base = resolve_media_path(path, root)
    if not str(vid_path).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not vid_path.exists() or not vid_path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")

    suffix = vid_path.suffix.lower()
    video_mime = {
        ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".m4v": "video/mp4",
        ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv",
        ".3gp": "video/3gpp", ".ts": "video/mp2t",
    }
    media_type = video_mime.get(suffix, "video/mp4")
    return FileResponse(vid_path, media_type=media_type, headers={"Accept-Ranges": "bytes"})


@app.get("/api/video-thumbnail")
async def get_video_thumbnail(path: str = Query(...), root: str = Query(default=None)):
    """生成并返回视频缩略图"""
    vid_path, base = resolve_media_path(path, root)
    if not str(vid_path).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not vid_path.exists() or not vid_path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")

    loop = asyncio.get_event_loop()
    cache_path = await loop.run_in_executor(None, Thumb.generate_video_thumbnail, vid_path)
    return FileResponse(cache_path, media_type="image/jpeg")


def _prefetch_thumbnails_for_dir(lib_root_str: str, dir_path: str):
    """在后台线程中预生成指定目录下所有缺失的缩略图（低优先级，不阻塞请求）"""
    root = Path(lib_root_str)
    if not root.exists():
        return

    conn = DB.get_connection()
    try:
        # 获取当前目录及其子目录的所有媒体文件
        if dir_path:
            pattern = dir_path + "/%"
            rows = conn.execute("""
                SELECT rel_path, type FROM media_files
                WHERE library_root = ? AND (dir_path = ? OR dir_path LIKE ?)
                AND type IN ('image', 'video')
            """, (lib_root_str, dir_path, pattern)).fetchall()
        else:
            rows = conn.execute("""
                SELECT rel_path, type FROM media_files
                WHERE library_root = ? AND type IN ('image', 'video')
            """, (lib_root_str,)).fetchall()
    finally:
        conn.close()

    generated = 0
    for row in rows:
        file_path = root / row['rel_path']
        if not file_path.exists():
            continue
        try:
            if row['type'] == 'video':
                cache = Thumb._video_thumb_cache_path(file_path)
                if not cache.exists():
                    Thumb.generate_video_thumbnail(file_path)
                    generated += 1
            else:
                cache = Thumb._thumb_cache_path(file_path)
                if not cache.exists():
                    Thumb.generate_thumbnail(file_path)
                    generated += 1
        except Exception:
            pass

    if generated > 0:
        logging.getLogger("prefetch").debug(f"预生成 {dir_path or '/'}：{generated} 张缩略图")

    # 完成后从集合中移除
    with _prefetch_lock:
        _prefetching_dirs.discard((lib_root_str, dir_path))


@app.get("/api/prefetch")
async def prefetch_thumbnails(path: str = Query(default=""), root: str = Query(default=None)):
    """打开相册时触发后台缩略图预生成（fire-and-forget，不阻塞请求）"""
    if not root:
        return JSONResponse({"ok": True, "prefetching": False})

    dir_key = (root, path)
    with _prefetch_lock:
        if dir_key in _prefetching_dirs:
            return JSONResponse({"ok": True, "prefetching": True, "reason": "already_running"})

    _prefetching_dirs.add(dir_key)
    _prefetch_executor.submit(_prefetch_thumbnails_for_dir, root, path)
    return JSONResponse({"ok": True, "prefetching": True})


# ══════════════════════════════════════════════════════════════
# 媒体信息 API
# ══════════════════════════════════════════════════════════════

@app.get("/api/media-info")
async def get_media_info(path: str = Query(...), root: str = Query(default=None)):
    """获取图片或视频的元数据信息"""
    media_path, base = resolve_media_path(path, root)
    if not str(media_path).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    stat = media_path.stat()
    info = {
        "name": media_path.name,
        "size": stat.st_size,
        "size_display": _format_size(stat.st_size),
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "format": media_path.suffix.lstrip(".").upper(),
        "type": "image" if is_image(media_path) else "video",
    }

    # 获取数据库中已有信息（分辨率、时长）
    lib_root = str(base.resolve())
    db_file = DB.get_file(lib_root, path)
    if db_file:
        if db_file.get('width') and db_file.get('height'):
            info["width"] = db_file['width']
            info["height"] = db_file['height']
            info["resolution"] = f"{db_file['width']} × {db_file['height']}"
        if db_file.get('duration'):
            info["duration_seconds"] = round(db_file['duration'], 1)
            info["duration_display"] = _fmt_duration(db_file['duration'])

    # 如果数据库中没有分辨率信息，实时读取
    if "resolution" not in info and is_image(media_path):
        try:
            with Image.open(media_path) as img:
                info["width"] = img.width
                info["height"] = img.height
                info["resolution"] = f"{img.width} × {img.height}"
        except Exception:
            info["resolution"] = "未知"
    elif "resolution" not in info and is_video(media_path):
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(media_path)],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                probe = json.loads(result.stdout)
                for stream in probe.get("streams", []):
                    if stream.get("codec_type") == "video":
                        w = stream.get("width", "?")
                        h = stream.get("height", "?")
                        info["width"] = w
                        info["height"] = h
                        info["resolution"] = f"{w} × {h}"
                        break
                duration = float(probe.get("format", {}).get("duration", 0))
                info["duration_seconds"] = round(duration, 1)
                info["duration_display"] = _fmt_duration(duration)
                # 保存到数据库
                if info.get("width") and info.get("height"):
                    DB.upsert_file(lib_root, path, media_path.name,
                                  str(Path(path).parent), "video",
                                  stat.st_size, stat.st_mtime,
                                  info["width"], info["height"], duration,
                                  media_path.suffix.lstrip(".").lower())
        except Exception:
            info["resolution"] = "未知"
            info["duration_display"] = "未知"

    # 获取属性（如果有的话）
    meta = DB.get_metadata(lib_root, path, "file")
    if meta:
        info["meta_title"] = meta.get('title')
        info["meta_description"] = meta.get('description')
        info["meta_tags"] = meta.get('tags', [])
        info["meta_favorite"] = meta.get('favorite', 0)
        info["meta_rating"] = meta.get('rating', 0)
        info["meta_date_taken"] = meta.get('date_taken')
        info["meta_location"] = meta.get('location')

    return JSONResponse(info)


# ══════════════════════════════════════════════════════════════
# 属性管理 API
# ══════════════════════════════════════════════════════════════

@app.post("/api/metadata/set")
async def set_metadata_api(
    library_root: str = Query(...),
    rel_path: str = Query(...),
    target_type: str = Query("file"),
    title: str = Query(default=None),
    description: str = Query(default=None),
    tags: str = Query(default=None),       # JSON 数组字符串
    favorite: int = Query(default=None),
    rating: int = Query(default=None),
    date_taken: str = Query(default=None),
    location: str = Query(default=None),
):
    """设置属性"""
    fields = {}
    if title is not None:
        fields['title'] = title
    if description is not None:
        fields['description'] = description
    if tags is not None:
        try:
            fields['tags'] = json.loads(tags)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="tags 必须是 JSON 数组")
    if favorite is not None:
        fields['favorite'] = favorite
    if rating is not None:
        fields['rating'] = max(0, min(5, rating))
    if date_taken is not None:
        fields['date_taken'] = date_taken
    if location is not None:
        fields['location'] = location

    DB.set_metadata(library_root, rel_path, target_type, **fields)
    return JSONResponse({"ok": True})


@app.get("/api/metadata/get")
async def get_metadata_api(
    library_root: str = Query(...),
    rel_path: str = Query(...),
    target_type: str = Query("file"),
):
    """获取属性"""
    meta = DB.get_metadata(library_root, rel_path, target_type)
    if meta:
        return JSONResponse(meta)
    return JSONResponse({
        "title": None, "description": None, "tags": [],
        "favorite": 0, "rating": 0, "date_taken": None, "location": None,
    })


@app.post("/api/metadata/batch")
async def batch_metadata_api(request_body: dict = None):
    """批量设置属性
    Body: {
        "library_root": "...",
        "rel_paths": ["path1", "path2"],
        "target_type": "file",
        "action": "set_favorite" | "add_tags" | "remove_tags" | "set_rating" | "set",
        ...
    }
    """
    import json as _json
    # FastAPI 会把 body 解析为 dict
    body = request_body
    if not body:
        raise HTTPException(status_code=400, detail="请求体不能为空")

    library_root = body.get("library_root", "")
    rel_paths = body.get("rel_paths", [])
    target_type = body.get("target_type", "file")
    action = body.get("action", "set")

    if not rel_paths:
        raise HTTPException(status_code=400, detail="rel_paths 不能为空")

    if action == "set_favorite":
        favorite = 1 if body.get("favorite", True) else 0
        count = DB.batch_set_favorite(library_root, rel_paths, target_type, favorite)
        return JSONResponse({"ok": True, "count": count})

    elif action == "add_tags":
        tags = body.get("tags", [])
        count = DB.batch_add_tags(library_root, rel_paths, target_type, tags)
        return JSONResponse({"ok": True, "count": count})

    elif action == "remove_tags":
        tags = body.get("tags", [])
        count = DB.batch_remove_tags(library_root, rel_paths, target_type, tags)
        return JSONResponse({"ok": True, "count": count})

    elif action == "set_rating":
        rating = max(0, min(5, body.get("rating", 0)))
        count = DB.batch_set_rating(library_root, rel_paths, target_type, rating)
        return JSONResponse({"ok": True, "count": count})

    else:
        raise HTTPException(status_code=400, detail=f"不支持的操作: {action}")


# ══════════════════════════════════════════════════════════════
# 封面管理 API
# ══════════════════════════════════════════════════════════════

@app.post("/api/set-cover")
async def set_album_cover(album_path: str = Query(...), image_path: str = Query(...), root: str = Query(default=None)):
    """设置某个相册的封面图"""
    lib_root = str(Path(root).expanduser().resolve()) if root else str(PHOTO_ROOT.resolve())
    DB.set_directory_cover(lib_root, album_path, image_path,
                           "video" if is_video(Path(root or str(PHOTO_ROOT)) / image_path) else "image")
    # 同时保存到旧的 covers.json（兼容）
    prefs = load_cover_preferences()
    prefs[album_path] = image_path
    save_cover_preferences(prefs)
    return JSONResponse({"ok": True, "album": album_path, "cover": image_path})


@app.post("/api/reset-cover")
async def reset_album_cover(album_path: str = Query(...)):
    """重置相册封面为自动选择"""
    prefs = load_cover_preferences()
    prefs.pop(album_path, None)
    save_cover_preferences(prefs)
    # 重置所有库中该路径的封面
    for root in get_all_photo_roots():
        DB.reset_directory_cover(str(root.resolve()), album_path)
    return JSONResponse({"ok": True, "album": album_path})


@app.get("/api/cover-images")
async def get_cover_images(album_path: str = Query(...)):
    """递归获取相册及其子目录中所有图片，用于封面选择器"""
    roots = get_all_photo_roots()
    all_images = []

    for root in roots:
        lib_root = str(root.resolve())
        files = DB.get_cover_images_for_dir(lib_root, album_path, 100)
        for f in files:
            all_images.append({
                "name": f['filename'],
                "path": f['rel_path'],
                "rel_path": f['rel_path'],
                "library_root": lib_root,
                "type": f['type'],
            })

    return JSONResponse({"images": all_images, "total": len(all_images)})


# ══════════════════════════════════════════════════════════════
# 目录树 API（数据库驱动）
# ══════════════════════════════════════════════════════════════

@app.get("/api/tree")
async def get_tree(path: str = ""):
    """递归返回目录树结构（从数据库查询）"""
    result = []
    roots = get_all_photo_roots()

    for root in roots:
        lib_root = str(root.resolve())
        tree = DB.get_directory_tree(lib_root)

        # 如果指定了 path，过滤到对应子树
        if path:
            def find_subtree(nodes, target):
                for node in nodes:
                    if node['rel_path'] == target:
                        return node
                    found = find_subtree(node.get('children', []), target)
                    if found:
                        return found
                return None
            sub = find_subtree(tree, path)
            if sub:
                result.extend(sub.get('children', []))
        else:
            result.extend(tree)

    return JSONResponse({"tree": result})


# ══════════════════════════════════════════════════════════════
# 扫描状态 API
# ══════════════════════════════════════════════════════════════

@app.get("/api/scan-status")
async def get_scan_status():
    """获取扫描状态"""
    state = DB.get_scan_state()
    return JSONResponse(state)


@app.post("/api/scan/trigger")
async def trigger_scan():
    """手动触发扫描"""
    global _scanner
    if _scanner and _scanner.is_scanning():
        return JSONResponse({"ok": False, "message": "正在扫描中"})
    if _scanner:
        _scanner.start_background_scan()
    return JSONResponse({"ok": True, "message": "扫描已触发"})


# ══════════════════════════════════════════════════════════════
# 应用信息 API
# ══════════════════════════════════════════════════════════════

@app.get("/api/info")
async def app_info():
    libs = load_library()
    scan_state = DB.get_scan_state()
    file_count = DB.get_file_count()
    return {
        "photo_root": str(PHOTO_ROOT),
        "photo_roots": [str(r) for r in get_all_photo_roots()],
        "accessible": PHOTO_ROOT.exists(),
        "thumbnail_size": THUMB_SIZE,
        "libraries": libs,
        "scan_state": scan_state,
        "indexed_files": file_count,
        "version": "2.0.0",
    }


# ══════════════════════════════════════════════════════════════
# 相册库管理 API（保持兼容）
# ══════════════════════════════════════════════════════════════

@app.get("/api/libraries")
async def get_libraries():
    libs = load_library()
    return JSONResponse({"libraries": libs})


@app.post("/api/libraries/add")
async def add_library(name: str = Query(...), path: str = Query(...)):
    global PHOTO_ROOTS, PHOTO_ROOT
    libs = load_library()
    new_path = str(Path(path).expanduser())
    if any(l["path"] == new_path for l in libs):
        raise HTTPException(status_code=400, detail="该路径已存在")
    libs.append({"name": name, "path": new_path, "enabled": True})
    save_library(libs)
    PHOTO_ROOTS = get_all_photo_roots()
    PHOTO_ROOT = PHOTO_ROOTS[0] if PHOTO_ROOTS else Path(cfg["photo_root"]).expanduser()
    # 触发新库扫描
    if _scanner:
        _scanner.start_background_scan()
    return JSONResponse({"ok": True, "library": {"name": name, "path": new_path}})


@app.post("/api/libraries/toggle")
async def toggle_library(path: str = Query(...)):
    global PHOTO_ROOTS, PHOTO_ROOT
    libs = load_library()
    target_path = str(Path(path).expanduser())
    found = False
    for lib in libs:
        if lib["path"] == target_path:
            lib["enabled"] = not lib.get("enabled", True)
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="相册库不存在")
    enabled_count = sum(1 for l in libs if l.get("enabled"))
    if enabled_count == 0:
        for lib in libs:
            if lib["path"] == target_path:
                lib["enabled"] = True
                break
        raise HTTPException(status_code=400, detail="至少保留一个启用的相册库")
    save_library(libs)
    PHOTO_ROOTS = get_all_photo_roots()
    PHOTO_ROOT = PHOTO_ROOTS[0] if PHOTO_ROOTS else Path(cfg["photo_root"]).expanduser()
    return JSONResponse({"ok": True, "enabled": enabled_count})


@app.post("/api/libraries/remove")
async def remove_library(path: str = Query(...)):
    global PHOTO_ROOTS, PHOTO_ROOT
    libs = load_library()
    target_path = str(Path(path).expanduser())
    if len(libs) <= 1:
        raise HTTPException(status_code=400, detail="至少保留一个相册库")
    new_libs = [l for l in libs if l["path"] != target_path]
    if len(new_libs) == len(libs):
        raise HTTPException(status_code=404, detail="相册库不存在")
    save_library(new_libs)
    PHOTO_ROOTS = get_all_photo_roots()
    PHOTO_ROOT = PHOTO_ROOTS[0] if PHOTO_ROOTS else Path(cfg["photo_root"]).expanduser()
    return JSONResponse({"ok": True})


@app.post("/api/refresh")
async def refresh_libraries():
    """刷新相册库 + 重新扫描"""
    global PHOTO_ROOTS, PHOTO_ROOT, _scanner
    PHOTO_ROOTS = get_all_photo_roots()
    PHOTO_ROOT = PHOTO_ROOTS[0] if PHOTO_ROOTS else Path(cfg["photo_root"]).expanduser()
    # 触发后台扫描
    if _scanner:
        _scanner.start_background_scan()
    return JSONResponse({
        "ok": True,
        "roots": [str(r) for r in PHOTO_ROOTS],
        "count": len(PHOTO_ROOTS),
    })


# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════

def _format_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"

def _fmt_duration(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return "0:00"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{str(m).zfill(2)}:{str(s).zfill(2)}"
    return f"{m}:{str(s).zfill(2)}"


# ══════════════════════════════════════════════════════════════
# 启动
# ══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    global _scanner
    # 创建并启动后台扫描器
    _scanner = Scanner.LibraryScanner(
        get_libraries_fn=get_all_photo_roots,
        db_module=DB,
    )
    _scanner.start_background_scan()

@app.on_event("shutdown")
async def shutdown_event():
    global _scanner
    if _scanner:
        _scanner.stop_scan()


if __name__ == "__main__":
    roots = get_all_photo_roots()
    print(f"📸 私人相册 v2 启动中...")
    print(f"   已启用 {len(roots)} 个相册库：")
    for r in roots:
        print(f"     - {r}")
    print(f"   访问地址：http://localhost:{PORT}")
    print(f"   局域网访问：http://<本机IP>:{PORT}")
    for r in roots:
        if not r.exists():
            print(f"\n⚠️  警告：照片目录不存在 → {r}")
    uvicorn.run(app, host=HOST, port=PORT, reload=False)
