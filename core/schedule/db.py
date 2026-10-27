"""内置日程 SQLite 存储。

WAL 模式 + 线程本地连接；所有方法都是同步的，调用方必须放在
``asyncio.to_thread`` 中执行，避免阻塞插件事件循环。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import logging
import sqlite3
import threading

logger = logging.getLogger("plugin.painter_maimai_daily.schedule.db")


class ScheduleDB:
    """线程安全的日程库封装；实例被关闭后拒绝继续读写。"""

    def __init__(self, db_path: Path | str) -> None:
        """保存数据库路径，连接在首次访问时按线程惰性创建。"""
        self.db_path = str(db_path)
        self._thread_local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        # SQLite 连接不能与关闭操作并发使用；统一串行化所有访问。
        self._operation_lock = threading.RLock()
        self._closed = False

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程独享的连接；每个连接都登记以便统一关闭。"""
        if self._closed:
            raise RuntimeError("日程数据库已关闭")
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=NORMAL")
            self._thread_local.connection = connection
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """事务上下文；异常时回滚并保留原始异常。"""
        with self._operation_lock:
            connection = self._get_conn()
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def ensure_schema(self) -> None:
        """幂等建表；服务启动和热更新都可安全重复调用。"""
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_items (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_date TEXT NOT NULL,
                    start_min     INTEGER NOT NULL,
                    end_min       INTEGER NOT NULL,
                    activity_type TEXT NOT NULL DEFAULT 'other',
                    description   TEXT NOT NULL DEFAULT '',
                    mood          TEXT NOT NULL DEFAULT 'neutral',
                    source        TEXT NOT NULL DEFAULT 'template',
                    created_at    TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedule_items_date ON schedule_items (schedule_date)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS state (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get_state(self, key: str) -> str | None:
        """读取键值状态；不存在时返回 ``None``。"""
        with self._operation_lock:
            row = self._get_conn().execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row is not None else None

    def set_state(self, key: str, value: str) -> None:
        """写入或覆盖状态值。"""
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?,?,?)",
                (key, value, now),
            )

    def replace_schedule_items(self, schedule_date: str, items: list[dict[str, Any]]) -> None:
        """同一天全量替换；生成失败时调用方根本不会进入这里。"""
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as connection:
            connection.execute("DELETE FROM schedule_items WHERE schedule_date=?", (schedule_date,))
            for item in items:
                connection.execute(
                    """
                    INSERT INTO schedule_items
                    (schedule_date, start_min, end_min, activity_type, description, mood, source, created_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        schedule_date,
                        int(item["start_min"]),
                        int(item["end_min"]),
                        str(item.get("activity_type", "other")),
                        str(item.get("description", "")),
                        str(item.get("mood", "neutral")),
                        str(item.get("source", "template")),
                        now,
                    ),
                )

    def list_schedule_items(self, schedule_date: str) -> list[dict[str, Any]]:
        """按日期读取并按开始时间升序返回。"""
        with self._operation_lock:
            rows = self._get_conn().execute(
                "SELECT * FROM schedule_items WHERE schedule_date=? ORDER BY start_min ASC",
                (schedule_date,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_schedule_items_by_range(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """按闭区间读取多天日程，用于历史摘要。"""
        with self._operation_lock:
            rows = self._get_conn().execute(
                """
                SELECT * FROM schedule_items
                WHERE schedule_date BETWEEN ? AND ?
                ORDER BY schedule_date ASC, start_min ASC
                """,
                (start_date, end_date),
            ).fetchall()
            return [dict(row) for row in rows]

    def cleanup_old_schedule_items(self, cutoff_date: str) -> int:
        """删除早于 ``cutoff_date`` 的记录；日期由调用方按日程时区计算。"""
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM schedule_items WHERE schedule_date < ?", (cutoff_date,)
            )
            deleted = cursor.rowcount
        logger.info("日程清理完成：删除 %d 条（%s 之前）", deleted, cutoff_date)
        return deleted

    def close_all(self) -> None:
        """关闭所有线程连接；等待进行中的事务结束后再关闭，避免并发崩溃。"""
        with self._operation_lock:
            with self._connections_lock:
                connections, self._connections = self._connections, []
            self._closed = True
            for connection in connections:
                try:
                    connection.close()
                except sqlite3.Error:
                    logger.exception("关闭日程数据库连接失败")
            self._thread_local.connection = None
