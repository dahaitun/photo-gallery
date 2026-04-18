#!/usr/bin/env python3
"""
📸 缩略图生成服务
图片缩略图 + 视频封面帧提取，带缓存
"""

import hashlib
import logging
import subprocess
from pathlib import Path

from PIL import Image, ExifTags, UnidentifiedImageError

logger = logging.getLogger("thumbnail")

BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 从 config.yaml 读取缩略图大小
import yaml
CONFIG_FILE = BASE_DIR / "config.yaml"
try:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        _cfg = yaml.safe_load(f)
    THUMB_SIZE = _cfg.get("thumbnail_size", 320)
except Exception:
    THUMB_SIZE = 320

SUPPORTED = {ext.lower() for ext in _cfg.get("supported_formats",
    ["jpg", "jpeg", "png", "gif", "webp", "heic", "bmp", "tiff", "tif"])}

VIDEO_FORMATS = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "flv", "wmv", "3gp", "ts"}


def is_image(path: Path) -> bool:
    return path.suffix.lstrip(".").lower() in SUPPORTED

def is_video(path: Path) -> bool:
    return path.suffix.lstrip(".").lower() in VIDEO_FORMATS

def is_media(path: Path) -> bool:
    return is_image(path) or is_video(path)


def _thumb_cache_key(img_path: Path) -> str:
    """生成缓存 key（基于路径+修改时间）"""
    try:
        mtime = img_path.stat().st_mtime
    except OSError:
        mtime = 0
    return f"{img_path}:{mtime}"


def _thumb_cache_path(img_path: Path) -> Path:
    """获取缩略图缓存文件路径"""
    key = _thumb_cache_key(img_path)
    h = hashlib.md5(key.encode()).hexdigest()
    return CACHE_DIR / f"{h}.jpg"


def _video_thumb_cache_path(vid_path: Path) -> Path:
    """获取视频缩略图缓存文件路径"""
    key = f"vthumb:{_thumb_cache_key(vid_path)}"
    h = hashlib.md5(key.encode()).hexdigest()
    return CACHE_DIR / f"{h}.jpg"


def fix_orientation(img: Image.Image) -> Image.Image:
    """根据 EXIF 旋转图片方向"""
    try:
        exif = img._getexif()
        if not exif:
            return img
        orientation_key = next(
            k for k, v in ExifTags.TAGS.items() if v == "Orientation"
        )
        orientation = exif.get(orientation_key)
        rotations = {3: 180, 6: 270, 8: 90}
        if orientation in rotations:
            img = img.rotate(rotations[orientation], expand=True)
    except Exception:
        pass
    return img


def generate_thumbnail(img_path: Path) -> Path:
    """生成图片缩略图并缓存，返回缓存路径"""
    cache = _thumb_cache_path(img_path)
    if cache.exists():
        return cache
    try:
        with Image.open(img_path) as img:
            img = fix_orientation(img)
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            if img.mode in ("RGBA", "P", "LA"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.save(cache, "JPEG", quality=82, optimize=True)
    except (UnidentifiedImageError, Exception) as e:
        # 生成占位图
        placeholder = Image.new("RGB", (THUMB_SIZE, THUMB_SIZE), (40, 40, 50))
        placeholder.save(cache, "JPEG")
        logger.warning(f"生成缩略图失败: {img_path} - {e}")
    return cache


def generate_video_thumbnail(vid_path: Path) -> Path:
    """用 ffmpeg 从视频中提取一帧作为缩略图"""
    cache = _video_thumb_cache_path(vid_path)
    if cache.exists():
        return cache
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-ss", "1",
            "-i", str(vid_path),
            "-vframes", "1",
            "-vf", f"scale={THUMB_SIZE}:{THUMB_SIZE}:force_original_aspect_ratio=decrease,pad={THUMB_SIZE}:{THUMB_SIZE}:(ow-iw)/2:(oh-ih)/2:color=black",
            "-q:v", "4",
            str(cache)
        ], capture_output=True, timeout=10, check=True)
    except Exception as e:
        placeholder = Image.new("RGB", (THUMB_SIZE, THUMB_SIZE), (30, 30, 40))
        placeholder.save(cache, "JPEG")
        logger.warning(f"生成视频缩略图失败: {vid_path} - {e}")
    return cache


def get_thumbnail(img_path: Path) -> Path:
    """获取缩略图（如果不存在则生成）"""
    return generate_thumbnail(img_path)


def get_video_thumbnail(vid_path: Path) -> Path:
    """获取视频缩略图（如果不存在则生成）"""
    return generate_video_thumbnail(vid_path)
