#!/usr/bin/env python3
"""
📸 数据库层 — SQLite 索引 + 属性管理
负责所有数据库操作：表创建、CRUD、高级查询
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

# ── 数据库路径 ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "metadata.db"


def get_connection() -> sqlite3.Connection:
    """获取数据库连接（WAL 模式，支持并发读写）"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── 表创建 ──────────────────────────────────────────────────────
_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS media_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    library_root  TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    filename      TEXT NOT NULL,
    dir_path      TEXT NOT NULL,
    type          TEXT NOT NULL,
    size          INTEGER,
    mtime         REAL,
    width         INTEGER,
    height        INTEGER,
    duration      REAL,
    format        TEXT,
    thumbnail_generated INTEGER DEFAULT 0,
    created_at    REAL DEFAULT (strftime('%s','now')),
    updated_at    REAL DEFAULT (strftime('%s','now')),
    UNIQUE(library_root, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_media_dir ON media_files(library_root, dir_path);
CREATE INDEX IF NOT EXISTS idx_media_type ON media_files(type);
CREATE INDEX IF NOT EXISTS idx_media_mtime ON media_files(mtime);
CREATE INDEX IF NOT EXISTS idx_media_library ON media_files(library_root);
CREATE INDEX IF NOT EXISTS idx_media_name ON media_files(filename);

CREATE TABLE IF NOT EXISTS directories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    library_root  TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    dir_name      TEXT NOT NULL,
    parent_path   TEXT DEFAULT '',
    depth         INTEGER DEFAULT 0,
    media_count   INTEGER DEFAULT 0,
    total_media   INTEGER DEFAULT 0,
    img_count     INTEGER DEFAULT 0,
    vid_count     INTEGER DEFAULT 0,
    total_img     INTEGER DEFAULT 0,
    total_vid     INTEGER DEFAULT 0,
    cover_path    TEXT,
    cover_type    TEXT DEFAULT 'image',
    mtime         REAL,
    created_at    REAL DEFAULT (strftime('%s','now')),
    updated_at    REAL DEFAULT (strftime('%s','now')),
    UNIQUE(library_root, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_dirs_parent ON directories(library_root, parent_path);
CREATE INDEX IF NOT EXISTS idx_dirs_root ON directories(library_root);

CREATE TABLE IF NOT EXISTS metadata (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    library_root  TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    target_type   TEXT NOT NULL,
    title         TEXT,
    description   TEXT,
    tags          TEXT DEFAULT '[]',
    favorite      INTEGER DEFAULT 0,
    rating        INTEGER DEFAULT 0,
    date_taken    TEXT,
    location      TEXT,
    created_at    REAL DEFAULT (strftime('%s','now')),
    updated_at    REAL DEFAULT (strftime('%s','now')),
    UNIQUE(library_root, rel_path, target_type)
);

CREATE INDEX IF NOT EXISTS idx_meta_favorite ON metadata(library_root, favorite);
CREATE INDEX IF NOT EXISTS idx_meta_rating ON metadata(library_root, rating);
CREATE INDEX IF NOT EXISTS idx_meta_target ON metadata(library_root, rel_path, target_type);

CREATE TABLE IF NOT EXISTS scan_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_scan     REAL,
    status        TEXT DEFAULT 'idle',
    total_files   INTEGER DEFAULT 0,
    total_dirs    INTEGER DEFAULT 0,
    progress      REAL DEFAULT 0
);
"""


def init_db():
    """初始化数据库（创建表 + 初始化 scan_state）"""
    conn = get_connection()
    try:
        conn.executescript(_CREATE_TABLES)
        # 确保单例 scan_state 行存在
        conn.execute("INSERT OR IGNORE INTO scan_state (id) VALUES (1)")
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 文件索引 CRUD
# ══════════════════════════════════════════════════════════════

def upsert_file(library_root: str, rel_path: str, filename: str, dir_path: str,
                type: str, size: int = None, mtime: float = None,
                width: int = None, height: int = None, duration: float = None,
                format: str = None, thumbnail_generated: int = 0):
    """插入或更新文件索引"""
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO media_files (library_root, rel_path, filename, dir_path, type, size, mtime,
                                     width, height, duration, format, thumbnail_generated, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(library_root, rel_path) DO UPDATE SET
                filename = excluded.filename,
                dir_path = excluded.dir_path,
                type = excluded.type,
                size = excluded.size,
                mtime = excluded.mtime,
                width = excluded.width,
                height = excluded.height,
                duration = excluded.duration,
                format = excluded.format,
                thumbnail_generated = excluded.thumbnail_generated,
                updated_at = excluded.updated_at
        """, (library_root, rel_path, filename, dir_path, type, size, mtime,
              width, height, duration, format, thumbnail_generated, time.time()))
        conn.commit()
    finally:
        conn.close()


def upsert_file_batch(items: list):
    """批量插入或更新文件索引"""
    conn = get_connection()
    try:
        now = time.time()
        conn.executemany("""
            INSERT INTO media_files (library_root, rel_path, filename, dir_path, type, size, mtime,
                                     width, height, duration, format, thumbnail_generated, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(library_root, rel_path) DO UPDATE SET
                filename = excluded.filename,
                dir_path = excluded.dir_path,
                type = excluded.type,
                size = excluded.size,
                mtime = excluded.mtime,
                width = COALESCE(excluded.width, media_files.width),
                height = COALESCE(excluded.height, media_files.height),
                duration = COALESCE(excluded.duration, media_files.duration),
                format = excluded.format,
                thumbnail_generated = excluded.thumbnail_generated,
                updated_at = excluded.updated_at
        """, [
            (it['library_root'], it['rel_path'], it['filename'], it['dir_path'],
             it['type'], it.get('size'), it.get('mtime'),
             it.get('width'), it.get('height'), it.get('duration'),
             it.get('format'), it.get('thumbnail_generated', 0), now)
            for it in items
        ])
        conn.commit()
    finally:
        conn.close()


def remove_missing_files(library_root: str, existing_paths: set):
    """删除数据库中存在但磁盘上不存在的文件记录"""
    conn = get_connection()
    try:
        # 查询该库的所有文件
        rows = conn.execute(
            "SELECT id, rel_path FROM media_files WHERE library_root = ?",
            (library_root,)
        ).fetchall()
        to_delete = [row['id'] for row in rows if row['rel_path'] not in existing_paths]
        if to_delete:
            # 分批删除（避免 SQL 过长）
            batch_size = 500
            for i in range(0, len(to_delete), batch_size):
                batch = to_delete[i:i + batch_size]
                placeholders = ','.join('?' * len(batch))
                conn.execute(f"DELETE FROM media_files WHERE id IN ({placeholders})", batch)
            conn.commit()
        return len(to_delete)
    finally:
        conn.close()


def get_files_by_dir(library_root: str, dir_path: str) -> list:
    """获取某目录下直接的媒体文件"""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM media_files
            WHERE library_root = ? AND dir_path = ?
            ORDER BY filename COLLATE NOCASE
        """, (library_root, dir_path)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_files_recursive(library_root: str = None, filter_type: str = "all",
                       sort: str = "name", sort_dir: str = "asc",
                       offset: int = 0, limit: int = 200) -> tuple:
    """
    递归获取所有匹配的媒体文件（分页）。
    返回 (items_list, total_count)
    """
    conn = get_connection()
    try:
        where_parts = []
        params = []

        if library_root:
            where_parts.append("library_root = ?")
            params.append(library_root)

        if filter_type in ("image", "video"):
            where_parts.append("type = ?")
            params.append(filter_type)

        # 属性筛选（收藏）
        # 通过 LEFT JOIN metadata 实现

        where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""

        # 排序
        sort_map = {
            "name": "filename COLLATE NOCASE",
            "date": "mtime",
            "size": "size",
            "rating": "COALESCE(meta.rating, 0)",
        }
        order_sql = sort_map.get(sort, "filename COLLATE NOCASE")
        if sort_dir == "desc":
            order_sql += " DESC"
        else:
            order_sql += " ASC"

        # 总数
        total = conn.execute(f"SELECT COUNT(*) FROM media_files {where_sql}", params).fetchone()[0]

        # 分页查询
        rows = conn.execute(
            f"SELECT f.*, m.favorite, m.rating, m.tags as meta_tags, m.title as meta_title "
            f"FROM media_files f "
            f"LEFT JOIN metadata m ON m.library_root = f.library_root "
            f"  AND m.rel_path = f.rel_path AND m.target_type = 'file' "
            f"{where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()

        return [dict(r) for r in rows], total
    finally:
        conn.close()


def get_file(library_root: str, rel_path: str) -> Optional[dict]:
    """获取单个文件记录"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM media_files WHERE library_root = ? AND rel_path = ?",
            (library_root, rel_path)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_file_count(library_root: str = None) -> int:
    """获取文件总数"""
    conn = get_connection()
    try:
        if library_root:
            row = conn.execute(
                "SELECT COUNT(*) FROM media_files WHERE library_root = ?",
                (library_root,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()
        return row[0]
    finally:
        conn.close()


def mark_thumbnail_generated(library_root: str, rel_path: str, width: int = None, height: int = None):
    """标记文件缩略图已生成"""
    conn = get_connection()
    try:
        sql = "UPDATE media_files SET thumbnail_generated = 1, updated_at = ?"
        params = [time.time()]
        if width and height:
            sql += ", width = ?, height = ?"
            params.extend([width, height])
        sql += " WHERE library_root = ? AND rel_path = ?"
        params.extend([library_root, rel_path])
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def get_ungenerated_thumbnails(library_root: str = None, limit: int = 100) -> list:
    """获取未生成缩略图的文件列表"""
    conn = get_connection()
    try:
        if library_root:
            rows = conn.execute(
                "SELECT * FROM media_files WHERE library_root = ? AND thumbnail_generated = 0 LIMIT ?",
                (library_root, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM media_files WHERE thumbnail_generated = 0 LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 目录索引 CRUD
# ══════════════════════════════════════════════════════════════

def upsert_directory(library_root: str, rel_path: str, dir_name: str,
                     parent_path: str = "", depth: int = 0,
                     media_count: int = 0, total_media: int = 0,
                     img_count: int = 0, vid_count: int = 0,
                     total_img: int = 0, total_vid: int = 0,
                     mtime: float = None):
    """插入或更新目录索引"""
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO directories (library_root, rel_path, dir_name, parent_path, depth,
                                    media_count, total_media, img_count, vid_count,
                                    total_img, total_vid, mtime, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(library_root, rel_path) DO UPDATE SET
                dir_name = excluded.dir_name,
                parent_path = excluded.parent_path,
                depth = excluded.depth,
                media_count = excluded.media_count,
                total_media = excluded.total_media,
                img_count = excluded.img_count,
                vid_count = excluded.vid_count,
                total_img = excluded.total_img,
                total_vid = excluded.total_vid,
                mtime = excluded.mtime,
                updated_at = excluded.updated_at
        """, (library_root, rel_path, dir_name, parent_path, depth,
              media_count, total_media, img_count, vid_count,
              total_img, total_vid, mtime, time.time()))
        conn.commit()
    finally:
        conn.close()


def upsert_directory_batch(items: list):
    """批量插入或更新目录索引"""
    conn = get_connection()
    try:
        now = time.time()
        conn.executemany("""
            INSERT INTO directories (library_root, rel_path, dir_name, parent_path, depth,
                                    media_count, total_media, img_count, vid_count,
                                    total_img, total_vid, mtime, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(library_root, rel_path) DO UPDATE SET
                dir_name = excluded.dir_name,
                parent_path = excluded.parent_path,
                depth = excluded.depth,
                media_count = excluded.media_count,
                total_media = excluded.total_media,
                img_count = excluded.img_count,
                vid_count = excluded.vid_count,
                total_img = excluded.total_img,
                total_vid = excluded.total_vid,
                mtime = excluded.mtime,
                updated_at = excluded.updated_at
        """, [
            (it['library_root'], it['rel_path'], it['dir_name'],
             it.get('parent_path', ''), it.get('depth', 0),
             it.get('media_count', 0), it.get('total_media', 0),
             it.get('img_count', 0), it.get('vid_count', 0),
             it.get('total_img', 0), it.get('total_vid', 0),
             it.get('mtime'), now)
            for it in items
        ])
        conn.commit()
    finally:
        conn.close()


def get_child_dirs(library_root: str, parent_path: str) -> list:
    """获取某目录下的子目录"""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT d.*, m.favorite as meta_favorite, m.title as meta_title, m.tags as meta_tags
            FROM directories d
            LEFT JOIN metadata m ON m.library_root = d.library_root
                AND m.rel_path = d.rel_path AND m.target_type = 'directory'
            WHERE d.library_root = ? AND d.parent_path = ?
            ORDER BY d.dir_name COLLATE NOCASE
        """, (library_root, parent_path)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_directory(library_root: str, rel_path: str) -> Optional[dict]:
    """获取单个目录记录"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM directories WHERE library_root = ? AND rel_path = ?",
            (library_root, rel_path)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_root_dirs(library_root: str) -> list:
    """获取根目录下的一级子目录"""
    return get_child_dirs(library_root, "")


def get_directory_tree(library_root: str) -> list:
    """获取完整的目录树结构"""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT rel_path, dir_name, parent_path, depth, media_count, total_media,
                   img_count, vid_count, total_img, total_vid
            FROM directories
            WHERE library_root = ?
            ORDER BY rel_path COLLATE NOCASE
        """, (library_root,)).fetchall()
        all_dirs = [dict(r) for r in rows]

        # 构建树形结构
        dir_map = {}
        roots = []
        for d in all_dirs:
            d['children'] = []
            dir_map[d['rel_path']] = d

        for d in all_dirs:
            parent = d.get('parent_path', '')
            if parent in dir_map:
                dir_map[parent]['children'].append(d)
            elif d['rel_path'] == '':
                continue  # 跳过根目录自身
            else:
                roots.append(d)

        # 按名称排序
        def sort_tree(nodes):
            nodes.sort(key=lambda n: n['dir_name'].lower())
            for n in nodes:
                sort_tree(n['children'])

        sort_tree(roots)
        return roots
    finally:
        conn.close()


def remove_missing_dirs(library_root: str, existing_paths: set):
    """删除数据库中存在但磁盘上不存在的目录记录"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, rel_path FROM directories WHERE library_root = ?",
            (library_root,)
        ).fetchall()
        to_delete = [row['id'] for row in rows
                     if row['rel_path'] != '' and row['rel_path'] not in existing_paths]
        if to_delete:
            batch_size = 500
            for i in range(0, len(to_delete), batch_size):
                batch = to_delete[i:i + batch_size]
                placeholders = ','.join('?' * len(batch))
                conn.execute(f"DELETE FROM directories WHERE id IN ({placeholders})", batch)
            conn.commit()
        return len(to_delete)
    finally:
        conn.close()


def update_dir_counts(library_root: str, rel_path: str,
                      media_count: int = None, total_media: int = None,
                      img_count: int = None, vid_count: int = None,
                      total_img: int = None, total_vid: int = None):
    """更新目录的媒体计数"""
    conn = get_connection()
    try:
        sets = []
        params = []
        for field, val in [('media_count', media_count), ('total_media', total_media),
                           ('img_count', img_count), ('vid_count', vid_count),
                           ('total_img', total_img), ('total_vid', total_vid)]:
            if val is not None:
                sets.append(f"{field} = ?")
                params.append(val)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(time.time())
        params.extend([library_root, rel_path])
        conn.execute(
            f"UPDATE directories SET {', '.join(sets)} WHERE library_root = ? AND rel_path = ?",
            params
        )
        conn.commit()
    finally:
        conn.close()


def set_directory_cover(library_root: str, rel_path: str, cover_path: str, cover_type: str = "image"):
    """设置目录封面"""
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE directories SET cover_path = ?, cover_type = ?, updated_at = ?
            WHERE library_root = ? AND rel_path = ?
        """, (cover_path, cover_type, time.time(), library_root, rel_path))
        conn.commit()
    finally:
        conn.close()


def reset_directory_cover(library_root: str, rel_path: str):
    """重置目录封面为自动选择"""
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE directories SET cover_path = NULL, cover_type = 'image', updated_at = ?
            WHERE library_root = ? AND rel_path = ?
        """, (time.time(), library_root, rel_path))
        conn.commit()
    finally:
        conn.close()


def get_cover_images_for_dir(library_root: str, rel_path: str, limit: int = 100) -> list:
    """递归获取目录及其子目录中所有图片/视频，用于封面选择"""
    conn = get_connection()
    try:
        if rel_path:
            pattern = rel_path + "/%"
            rows = conn.execute("""
                SELECT f.* FROM media_files f
                WHERE f.library_root = ? AND (f.dir_path = ? OR f.dir_path LIKE ?)
                AND f.type IN ('image', 'video')
                ORDER BY f.filename COLLATE NOCASE
                LIMIT ?
            """, (library_root, rel_path, pattern, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT f.* FROM media_files f
                WHERE f.library_root = ? AND f.type IN ('image', 'video')
                ORDER BY f.filename COLLATE NOCASE
                LIMIT ?
            """, (library_root, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_dir_preview_images(library_root: str, rel_path: str, limit: int = 4) -> list:
    """获取目录下前几张图片/视频作为预览"""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM media_files
            WHERE library_root = ? AND dir_path = ? AND type IN ('image', 'video')
            ORDER BY filename COLLATE NOCASE
            LIMIT ?
        """, (library_root, rel_path, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 属性管理 CRUD
# ══════════════════════════════════════════════════════════════

def set_metadata(library_root: str, rel_path: str, target_type: str, **fields) -> bool:
    """设置属性（单个文件或目录）"""
    allowed = {'title', 'description', 'tags', 'favorite', 'rating', 'date_taken', 'location'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False

    # tags 需要序列化为 JSON
    if 'tags' in updates and isinstance(updates['tags'], list):
        updates['tags'] = json.dumps(updates['tags'], ensure_ascii=False)

    conn = get_connection()
    try:
        now = time.time()
        cols = ', '.join(updates.keys())
        placeholders = ', '.join('?' * len(updates))
        update_set = ', '.join(f'{k} = excluded.{k}' for k in updates.keys())

        params = [library_root, rel_path, target_type] + list(updates.values()) + [now]
        conn.execute(f"""
            INSERT INTO metadata (library_root, rel_path, target_type, {cols}, updated_at)
            VALUES (?, ?, ?, {placeholders}, ?)
            ON CONFLICT(library_root, rel_path, target_type) DO UPDATE SET
                {update_set}, updated_at = excluded.updated_at
        """, params)
        conn.commit()
        return True
    finally:
        conn.close()


def get_metadata(library_root: str, rel_path: str, target_type: str) -> Optional[dict]:
    """获取属性"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM metadata WHERE library_root = ? AND rel_path = ? AND target_type = ?",
            (library_root, rel_path, target_type)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        # 反序列化 tags
        if result.get('tags'):
            try:
                result['tags'] = json.loads(result['tags'])
            except (json.JSONDecodeError, TypeError):
                result['tags'] = []
        else:
            result['tags'] = []
        return result
    finally:
        conn.close()


def batch_set_metadata(items: list) -> int:
    """批量设置属性
    items: [{"library_root", "rel_path", "target_type", ...fields}, ...]
    """
    count = 0
    for item in items:
        lr = item.pop('library_root', None)
        rp = item.pop('rel_path', None)
        tt = item.pop('target_type', None)
        if lr and rp and tt:
            if set_metadata(lr, rp, tt, **item):
                count += 1
    return count


def batch_add_tags(library_root: str, rel_paths: list, target_type: str, tags: list) -> int:
    """批量添加标签（不删除已有标签）"""
    count = 0
    conn = get_connection()
    try:
        for rp in rel_paths:
            # 获取当前 tags
            row = conn.execute(
                "SELECT tags FROM metadata WHERE library_root = ? AND rel_path = ? AND target_type = ?",
                (library_root, rp, target_type)
            ).fetchone()
            existing = []
            if row and row['tags']:
                try:
                    existing = json.loads(row['tags'])
                except (json.JSONDecodeError, TypeError):
                    existing = []
            # 合并新标签（去重）
            merged = list(set(existing) | set(tags))
            tags_json = json.dumps(merged, ensure_ascii=False)
            now = time.time()
            conn.execute("""
                INSERT INTO metadata (library_root, rel_path, target_type, tags, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(library_root, rel_path, target_type) DO UPDATE SET
                    tags = excluded.tags, updated_at = excluded.updated_at
            """, (library_root, rp, target_type, tags_json, now))
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count


def batch_remove_tags(library_root: str, rel_paths: list, target_type: str, tags: list) -> int:
    """批量删除标签"""
    count = 0
    conn = get_connection()
    try:
        for rp in rel_paths:
            row = conn.execute(
                "SELECT tags FROM metadata WHERE library_root = ? AND rel_path = ? AND target_type = ?",
                (library_root, rp, target_type)
            ).fetchone()
            if not row or not row['tags']:
                continue
            try:
                existing = json.loads(row['tags'])
            except (json.JSONDecodeError, TypeError):
                existing = []
            merged = [t for t in existing if t not in tags]
            tags_json = json.dumps(merged, ensure_ascii=False)
            conn.execute("""
                UPDATE metadata SET tags = ?, updated_at = ?
                WHERE library_root = ? AND rel_path = ? AND target_type = ?
            """, (tags_json, time.time(), library_root, rp, target_type))
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count


def batch_set_favorite(library_root: str, rel_paths: list, target_type: str, favorite: int) -> int:
    """批量收藏/取消收藏"""
    conn = get_connection()
    try:
        now = time.time()
        conn.executemany("""
            INSERT INTO metadata (library_root, rel_path, target_type, favorite, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(library_root, rel_path, target_type) DO UPDATE SET
                favorite = excluded.favorite, updated_at = excluded.updated_at
        """, [(library_root, rp, target_type, favorite, now) for rp in rel_paths])
        conn.commit()
        return len(rel_paths)
    finally:
        conn.close()


def batch_set_rating(library_root: str, rel_paths: list, target_type: str, rating: int) -> int:
    """批量设置评分"""
    conn = get_connection()
    try:
        now = time.time()
        conn.executemany("""
            INSERT INTO metadata (library_root, rel_path, target_type, rating, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(library_root, rel_path, target_type) DO UPDATE SET
                rating = excluded.rating, updated_at = excluded.updated_at
        """, [(library_root, rp, target_type, rating, now) for rp in rel_paths])
        conn.commit()
        return len(rel_paths)
    finally:
        conn.close()


def delete_metadata(library_root: str, rel_path: str, target_type: str):
    """删除属性"""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM metadata WHERE library_root = ? AND rel_path = ? AND target_type = ?",
            (library_root, rel_path, target_type)
        )
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 高级查询
# ══════════════════════════════════════════════════════════════

def get_favorites(library_root: str = None, filter_type: str = "all",
                  sort: str = "name", sort_dir: str = "asc",
                  offset: int = 0, limit: int = 200) -> tuple:
    """获取收藏的媒体文件（分页）"""
    conn = get_connection()
    try:
        where_parts = ["m.favorite = 1"]
        params = []

        if library_root:
            where_parts.append("f.library_root = ?")
            params.append(library_root)

        if filter_type in ("image", "video"):
            where_parts.append("f.type = ?")
            params.append(filter_type)

        where_sql = "WHERE " + " AND ".join(where_parts)

        sort_map = {
            "name": "f.filename COLLATE NOCASE",
            "date": "f.mtime",
            "size": "f.size",
        }
        order_sql = sort_map.get(sort, "f.filename COLLATE NOCASE")
        if sort_dir == "desc":
            order_sql += " DESC"

        total = conn.execute(f"""
            SELECT COUNT(*) FROM media_files f
            INNER JOIN metadata m ON m.library_root = f.library_root
                AND m.rel_path = f.rel_path AND m.target_type = 'file'
            {where_sql}
        """, params).fetchone()[0]

        rows = conn.execute(f"""
            SELECT f.*, m.favorite, m.rating, m.tags as meta_tags, m.title as meta_title
            FROM media_files f
            INNER JOIN metadata m ON m.library_root = f.library_root
                AND m.rel_path = f.rel_path AND m.target_type = 'file'
            {where_sql}
            ORDER BY {order_sql} LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()

        return [dict(r) for r in rows], total
    finally:
        conn.close()


def search_by_tags(library_root: str = None, tags: list = None, filter_type: str = "all",
                   sort: str = "name", sort_dir: str = "asc",
                   offset: int = 0, limit: int = 200,
                   target_type: str = "file") -> tuple:
    """按标签搜索媒体文件或目录（分页）
    target_type: "file" 搜索文件, "directory" 搜索目录, "all" 搜索全部
    """
    if not tags:
        if target_type == "directory":
            return [], 0
        return get_files_recursive(library_root, filter_type, sort, sort_dir, offset, limit)

    conn = get_connection()
    try:
        # 标签匹配条件
        tag_conditions = []
        tag_params = []
        for tag in tags:
            tag_conditions.append("m.tags LIKE ?")
            tag_params.append(f'%"{tag}"%')
        tag_where = f"({' OR '.join(tag_conditions)})"

        # 搜索目录级标签
        if target_type == "directory":
            where_parts = [tag_where]
            params = list(tag_params)
            if library_root:
                where_parts.append("m.library_root = ?")
                params.append(library_root)
            where_sql = "WHERE " + " AND ".join(where_parts)

            total = conn.execute(f"""
                SELECT COUNT(*) FROM metadata m
                {where_sql} AND m.target_type = 'directory'
            """, params).fetchone()[0]

            rows = conn.execute(f"""
                SELECT m.*, d.dir_name, d.img_count, d.vid_count, d.total_img, d.total_vid,
                       d.cover_path, d.cover_type, d.rel_path as dir_rel_path
                FROM metadata m
                LEFT JOIN directories d ON d.library_root = m.library_root AND d.rel_path = m.rel_path
                {where_sql} AND m.target_type = 'directory'
                ORDER BY d.dir_name COLLATE NOCASE
                LIMIT ? OFFSET ?
            """, params + [limit, offset]).fetchall()
            return [dict(r) for r in rows], total

        # 搜索文件级标签（默认）
        where_parts = []
        params = []
        if library_root:
            where_parts.append("f.library_root = ?")
            params.append(library_root)
        if filter_type in ("image", "video"):
            where_parts.append("f.type = ?")
            params.append(filter_type)
        where_parts.append(tag_where)
        params.extend(tag_params)

        where_sql = "WHERE " + " AND ".join(where_parts)

        sort_map = {
            "name": "f.filename COLLATE NOCASE",
            "date": "f.mtime",
            "size": "f.size",
        }
        order_sql = sort_map.get(sort, "f.filename COLLATE NOCASE")
        if sort_dir == "desc":
            order_sql += " DESC"

        total = conn.execute(f"""
            SELECT COUNT(*) FROM media_files f
            INNER JOIN metadata m ON m.library_root = f.library_root
                AND m.rel_path = f.rel_path AND m.target_type = 'file'
            {where_sql}
        """, params).fetchone()[0]

        rows = conn.execute(f"""
            SELECT f.*, m.favorite, m.rating, m.tags as meta_tags, m.title as meta_title
            FROM media_files f
            INNER JOIN metadata m ON m.library_root = f.library_root
                AND m.rel_path = f.rel_path AND m.target_type = 'file'
            {where_sql}
            ORDER BY {order_sql} LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()

        return [dict(r) for r in rows], total
    finally:
        conn.close()


def get_all_tags(library_root: str = None) -> list:
    """获取所有已使用的标签（去重）"""
    conn = get_connection()
    try:
        if library_root:
            rows = conn.execute(
                "SELECT tags FROM metadata WHERE library_root = ? AND tags != '[]'",
                (library_root,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT tags FROM metadata WHERE tags != '[]'"
            ).fetchall()
        all_tags = set()
        for row in rows:
            try:
                tags = json.loads(row['tags'])
                if isinstance(tags, list):
                    all_tags.update(tags)
            except (json.JSONDecodeError, TypeError):
                pass
        return sorted(all_tags)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 扫描状态
# ══════════════════════════════════════════════════════════════

def get_scan_state() -> dict:
    """获取扫描状态"""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM scan_state WHERE id = 1").fetchone()
        return dict(row) if row else {"status": "idle", "total_files": 0, "total_dirs": 0, "progress": 0}
    finally:
        conn.close()


def update_scan_state(status: str = None, total_files: int = None,
                      total_dirs: int = None, progress: float = None):
    """更新扫描状态"""
    conn = get_connection()
    try:
        sets = []
        params = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if total_files is not None:
            sets.append("total_files = ?")
            params.append(total_files)
        if total_dirs is not None:
            sets.append("total_dirs = ?")
            params.append(total_dirs)
        if progress is not None:
            sets.append("progress = ?")
            params.append(progress)
        if sets:
            conn.execute(f"UPDATE scan_state SET {', '.join(sets)} WHERE id = 1", params)
            conn.commit()
    finally:
        conn.close()


def mark_scan_complete(total_files: int, total_dirs: int):
    """标记扫描完成"""
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE scan_state SET
                status = 'idle',
                last_scan = ?,
                total_files = ?,
                total_dirs = ?,
                progress = 1.0
            WHERE id = 1
        """, (time.time(), total_files, total_dirs))
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════

def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def fmt_duration(seconds: float) -> str:
    """格式化视频时长"""
    if not seconds or seconds <= 0:
        return "0:00"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{str(m).zfill(2)}:{str(s).zfill(2)}"
    return f"{m}:{str(s).zfill(2)}"
