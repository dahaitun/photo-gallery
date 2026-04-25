#!/usr/bin/env python3
"""
📸 增量文件扫描器
启动时后台运行，增量扫描所有已启用相册库，更新数据库索引。
"""

import os
import json
import time
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("scanner")


class LibraryScanner:
    """增量扫描器：对比磁盘文件和数据库索引，增量更新"""

    def __init__(self, get_libraries_fn, db_module):
        """
        Args:
            get_libraries_fn: 返回已启用相册库列表的函数
            db_module: db 模块的引用
        """
        self.get_libraries = get_libraries_fn
        self.db = db_module
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start_background_scan(self):
        """启动后台扫描线程"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_scan, daemon=True, name="scanner")
        self._thread.start()
        logger.info("后台扫描线程已启动")

    def stop_scan(self):
        """停止扫描"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def wait_scan_done(self, timeout: float = 300):
        """等待扫描完成"""
        if self._thread:
            self._thread.join(timeout=timeout)

    def is_scanning(self) -> bool:
        """是否正在扫描"""
        state = self.db.get_scan_state()
        return state.get('status') == 'scanning'

    def _run_scan(self):
        """扫描主循环"""
        try:
            self.db.update_scan_state(status='scanning', progress=0)
            library_roots = self.get_libraries()

            total_files = 0
            total_dirs = 0

            for i, lib_path in enumerate(library_roots):
                if self._stop_event.is_set():
                    logger.info("扫描被用户停止")
                    break

                # library_roots 可能返回 Path 对象或 dict 列表
                if isinstance(lib_path, dict):
                    lib_path = Path(lib_path['path']).expanduser().resolve()
                elif isinstance(lib_path, str):
                    lib_path = Path(lib_path).expanduser().resolve()
                elif isinstance(lib_path, Path):
                    lib_path = lib_path.resolve()

                if not lib_path.exists() or not lib_path.is_dir():
                    logger.warning(f"相册库路径不存在: {lib_path}")
                    continue

                lib_root = str(lib_path)
                progress = (i / len(library_roots)) * 0.8  # 80% 用于文件扫描

                logger.info(f"扫描相册库: {lib_path}")
                f_count, d_count = self._scan_library(lib_root, progress)
                total_files += f_count
                total_dirs += d_count

            # 更新所有目录的递归计数（total_media 等）
            if not self._stop_event.is_set():
                self.db.update_scan_state(progress=0.9, total_files=total_files)
                library_roots = self.get_libraries()
                for lib_path in library_roots:
                    if isinstance(lib_path, dict):
                        lib_path = Path(lib_path['path']).expanduser().resolve()
                    elif isinstance(lib_path, str):
                        lib_path = Path(lib_path).expanduser().resolve()
                    elif isinstance(lib_path, Path):
                        lib_path = lib_path.resolve()
                    if lib_path.exists():
                        self._update_recursive_counts(str(lib_path))

            # 预生成缩略图（已移除：改为打开相册时按需预生成，不再扫描时全量执行）

            self.db.mark_scan_complete(total_files, total_dirs)
            logger.info(f"扫描完成: {total_files} 个文件, {total_dirs} 个目录")

        except Exception as e:
            logger.error(f"扫描出错: {e}", exc_info=True)
            self.db.update_scan_state(status='idle', progress=0)

    def _scan_library(self, library_root: str, base_progress: float) -> tuple:
        """扫描单个相册库"""
        root = Path(library_root)
        file_batch = []
        dir_batch = []
        existing_paths = set()
        file_count = 0
        dir_count = 0

        # 获取数据库中已有的文件 mtime 映射
        conn = self.db.get_connection()
        try:
            rows = conn.execute(
                "SELECT rel_path, mtime FROM media_files WHERE library_root = ?",
                (library_root,)
            ).fetchall()
            db_files = {row['rel_path']: row['mtime'] for row in rows}
        finally:
            conn.close()

        # 遍历文件系统
        batch_size = 500
        max_depth = 25

        def scan_dir(dir_path: Path, depth: int):
            nonlocal file_count, dir_count
            if self._stop_event.is_set():
                return
            if depth > max_depth:
                return

            try:
                entries = list(dir_path.iterdir())
            except PermissionError:
                return
            except Exception as e:
                logger.warning(f"无法读取目录 {dir_path}: {e}")
                return

            dir_media_count = 0
            dir_img_count = 0
            dir_vid_count = 0

            for entry in sorted(entries, key=lambda p: p.name.lower()):
                if self._stop_event.is_set():
                    return

                name = entry.name
                if name.startswith('.'):
                    continue

                try:
                    rel = str(entry.relative_to(root))
                except ValueError:
                    continue

                if entry.is_dir():
                    dir_count += 1
                    dir_batch.append({
                        'library_root': library_root,
                        'rel_path': rel,
                        'dir_name': name,
                        'parent_path': str(Path(rel).parent) if rel != name else '',
                        'depth': depth,
                        'mtime': entry.stat().st_mtime,
                    })
                    existing_paths.add(rel)
                    scan_dir(entry, depth + 1)

                elif entry.is_file():
                    ext = entry.suffix.lstrip('.').lower()
                    media_type = None

                    if ext in {'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'bmp', 'tiff', 'tif'}:
                        media_type = 'image'
                    elif ext in {'mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', 'flv', 'wmv', '3gp', 'ts'}:
                        media_type = 'video'

                    if media_type:
                        existing_paths.add(rel)
                        file_count += 1

                        if media_type == 'image':
                            dir_img_count += 1
                        else:
                            dir_vid_count += 1
                        dir_media_count += 1

                        # 增量判断：对比 mtime
                        try:
                            st = entry.stat()
                            mtime = st.st_mtime
                        except OSError:
                            continue

                        db_mtime = db_files.get(rel)
                        if db_mtime is not None and abs(db_mtime - mtime) < 1:
                            # 未修改，跳过
                            continue

                        dir_path_str = str(Path(rel).parent)

                        file_batch.append({
                            'library_root': library_root,
                            'rel_path': rel,
                            'filename': name,
                            'dir_path': dir_path_str,
                            'type': media_type,
                            'size': st.st_size,
                            'mtime': mtime,
                            'format': ext,
                        })

                        # 批量写入
                        if len(file_batch) >= batch_size:
                            self.db.upsert_file_batch(file_batch)
                            file_batch.clear()
                            prog = base_progress + 0.8 * (file_count / max(file_count + dir_count, 1))
                            self.db.update_scan_state(
                                total_files=file_count,
                                total_dirs=dir_count,
                                progress=min(prog, 0.9)
                            )

            # 更新当前目录的直接计数
            if dir_media_count > 0 or dir_count > 0:
                # 找到 dir_batch 中对应的目录并更新计数
                for d in reversed(dir_batch):
                    if d['library_root'] == library_root and d['rel_path'] == str(dir_path.relative_to(root)) if str(dir_path).startswith(str(root)) else '':
                        d['media_count'] = dir_media_count
                        d['img_count'] = dir_img_count
                        d['vid_count'] = dir_vid_count
                        break

        # 从根目录开始扫描
        scan_dir(root, 0)

        # 写入剩余批次
        if file_batch:
            self.db.upsert_file_batch(file_batch)
        if dir_batch:
            self.db.upsert_directory_batch(dir_batch)

        # 清理不存在的文件
        removed = self.db.remove_missing_files(library_root, existing_paths)
        if removed > 0:
            logger.info(f"清理了 {removed} 个已删除的文件记录: {library_root}")

        # 清理不存在的目录（修复：切换/删除目录后空文件夹遗留问题）
        removed_dirs = self.db.remove_missing_dirs(library_root, existing_paths)
        if removed_dirs > 0:
            logger.info(f"清理了 {removed_dirs} 个已删除的目录记录: {library_root}")

        return file_count, dir_count

    def _collect_dir_paths(self, root: Path) -> set:
        """收集磁盘上所有目录的相对路径"""
        paths = set()
        def _walk(d: Path, depth: int):
            if depth > 25:
                return
            try:
                for entry in d.iterdir():
                    if entry.name.startswith('.'):
                        continue
                    if entry.is_dir():
                        try:
                            rel = str(entry.relative_to(root))
                            paths.add(rel)
                            _walk(entry, depth + 1)
                        except ValueError:
                            pass
            except PermissionError:
                pass
        _walk(root, 0)
        return paths

    def _update_recursive_counts(self, library_root: str):
        """更新所有目录的递归计数（total_media 等）"""
        conn = self.db.get_connection()
        try:
            # 获取所有目录
            dirs = conn.execute(
                "SELECT rel_path FROM directories WHERE library_root = ? ORDER BY rel_path",
                (library_root,)
            ).fetchall()

            # 获取所有文件按目录分组
            files = conn.execute(
                "SELECT dir_path, type FROM media_files WHERE library_root = ?",
                (library_root,)
            ).fetchall()

            # 统计每个目录的直接文件数
            direct_count = {}  # dir_path -> {image: n, video: n}
            for f in files:
                dp = f['dir_path']
                if dp not in direct_count:
                    direct_count[dp] = {'image': 0, 'video': 0, 'total': 0}
                direct_count[dp][f['type']] += 1
                direct_count[dp]['total'] += 1

            # 构建目录树结构
            dir_map = {d['rel_path']: {'children': [], 'depth': d['rel_path'].count('/')}
                       for d in dirs}

            for d in dirs:
                parent = str(Path(d['rel_path']).parent)
                if parent != d['rel_path'] and parent in dir_map:
                    dir_map[parent]['children'].append(d['rel_path'])

            # 递归计算 total
            def calc_total(rel_path: str) -> dict:
                dc = direct_count.get(rel_path, {'image': 0, 'video': 0, 'total': 0})
                children_total = {'image': 0, 'video': 0, 'total': 0}
                for child in dir_map.get(rel_path, {}).get('children', []):
                    ct = calc_total(child)
                    children_total['image'] += ct['image']
                    children_total['video'] += ct['video']
                    children_total['total'] += ct['total']

                total_img = dc['image'] + children_total['image']
                total_vid = dc['video'] + children_total['video']
                total_media = dc['total'] + children_total['total']

                conn.execute("""
                    UPDATE directories SET
                        media_count = ?, img_count = ?, vid_count = ?,
                        total_media = ?, total_img = ?, total_vid = ?
                    WHERE library_root = ? AND rel_path = ?
                """, (dc['total'], dc['image'], dc['video'],
                      total_media, total_img, total_vid,
                      library_root, rel_path))

                return {'image': total_img, 'video': total_vid, 'total': total_media}

            # 从最深层开始计算
            for d in sorted(dirs, key=lambda x: x['rel_path'].count('/'), reverse=True):
                calc_total(d['rel_path'])

            conn.commit()
        finally:
            conn.close()
