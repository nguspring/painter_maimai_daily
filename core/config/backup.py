"""配置文件备份与版本管理

SDK 2.15 自带配置重建；本模块在重建前把原始 config.toml 拷到 old/。
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

import logging
import re
import shutil

logger = logging.getLogger("plugin.painter_maimai_daily.config_backup")


def backup_config_if_version_changed(
    plugin_dir: str,
    config_file_name: str,
    expected_version: str,
    current_version: Optional[str],
    *,
    force: bool = False,
) -> Optional[Path]:
    """备份失败必须中止升级，不能继续覆盖唯一的旧配置。"""
    if not force and current_version == expected_version:
        return None
    config_path = Path(plugin_dir) / config_file_name
    if not config_path.exists():
        return None

    old_dir = config_path.parent / "old"
    old_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    version_tag = re.sub(r"[^A-Za-z0-9._-]", "_", current_version or "unknown")
    backup_name = f"{config_file_name}.backup_{version_tag}_{timestamp}.toml"
    backup_path = old_dir / backup_name
    shutil.copy2(config_path, backup_path)
    logger.info("配置升级 %r -> %r，原始配置已备份到 %s", current_version, expected_version, backup_path)
    return backup_path
