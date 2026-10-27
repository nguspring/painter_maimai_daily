"""把插件目录里的持久数据一次性迁入 SDK 授予的数据目录。"""

from contextlib import closing
from pathlib import Path
from typing import Optional

import logging
import os
import shutil
import sqlite3
import tempfile


logger = logging.getLogger("plugin.painter_maimai_daily.migration")
_MIGRATION_MARKER = ".migrated_from_plugin_data_v1"
_DATA_FILES = ("session_settings.json", "quota.sqlite3")


def _copy_data_file(source: Path, destination: Path) -> None:
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        if source.suffix == ".sqlite3":
            # SQLite backup 包含已提交但还在 WAL 中的数据，不能只复制主数据库文件。
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
                with closing(sqlite3.connect(temporary_path)) as copied:
                    original.backup(copied)
        else:
            shutil.copyfile(source, temporary_path)
        with temporary_path.open("r+b") as copied_file:
            copied_file.flush()
            os.fsync(copied_file.fileno())
        # 同目录硬链接原子发布完整文件；即使另一实例先创建了目标，也不覆盖它。
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            return
        logger.info("已迁移绘卷数据：%s -> %s（旧文件保留）", source, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def migrate_plugin_data(source_dir: Path, data_dir: Path) -> None:
    """只迁移已知文件，目标已有数据优先；失败可重试，成功后不重复导入旧副本。"""
    if not data_dir.is_absolute():
        raise ValueError("SDK 数据目录必须是绝对路径")
    source_dir = source_dir.resolve()
    data_dir = data_dir.resolve()
    if source_dir == data_dir:
        return
    marker = data_dir / _MIGRATION_MARKER
    if marker.exists():
        if marker.read_bytes() != b"1\n":
            raise ValueError(f"绘卷数据迁移标记损坏：{marker}")
        return
    sources = [source_dir / name for name in _DATA_FILES if (source_dir / name).exists()]
    if not sources:
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    for source in sources:
        destination = data_dir / source.name
        if not destination.exists():
            _copy_data_file(source, destination)
    # 最后原子发布标记；中断不会留下半写标记，并发迁移也不能覆盖已有标记。
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=data_dir, prefix=f"{_MIGRATION_MARKER}.", suffix=".tmp", delete=False,
        ) as completed:
            temporary_path = Path(completed.name)
            completed.write(b"1\n")
            completed.flush()
            os.fsync(completed.fileno())
        try:
            os.link(temporary_path, marker)
        except FileExistsError:
            if marker.read_bytes() != b"1\n":
                raise ValueError(f"绘卷数据迁移标记损坏：{marker}")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
