import sqlite3
from pathlib import Path
from collections import OrderedDict


class _SQLiteLRUSignatureStore:
    """使用 SQLite 持久化 + LRU 热缓存做状态签名去重。"""

    def __init__(
        self,
        db_path: Path,
        *,
        lru_capacity: int,
        commit_interval: int,
    ) -> None:
        self.db_path = db_path
        self.lru_capacity = max(1, lru_capacity)
        self.commit_interval = max(1, commit_interval)
        self._pending_writes = 0

        self._lru_cache: dict[str, OrderedDict[str, None]] = {
            "visited": OrderedDict(),
            "ending": OrderedDict(),
        }
        self._stats: dict[str, int] = {
            "lru_hits": 0,
            "sqlite_hits": 0,
            "inserted": 0,
        }

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS state_signatures (
                namespace TEXT NOT NULL,
                signature TEXT NOT NULL,
                PRIMARY KEY (namespace, signature)
            )
            """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_state_signatures_namespace
            ON state_signatures(namespace)
            """)
        self._conn.commit()

    def reset(self) -> None:
        """清空所有缓存和数据库记录。"""
        self._conn.execute("DELETE FROM state_signatures")
        self._conn.commit()
        self._pending_writes = 0
        for cache in self._lru_cache.values():
            cache.clear()
        for key in self._stats:
            self._stats[key] = 0

    def contains(self, namespace: str, signature: str) -> bool:
        """检查指定命名空间下的签名是否已存在。"""
        cache = self._lru_cache[namespace]
        if signature in cache:
            cache.move_to_end(signature)
            self._stats["lru_hits"] += 1
            return True

        row = self._conn.execute(
            """
            SELECT 1
            FROM state_signatures
            WHERE namespace = ? AND signature = ?
            LIMIT 1
            """,
            (namespace, signature),
        ).fetchone()
        if row is None:
            return False

        self._stats["sqlite_hits"] += 1
        self._remember(namespace, signature)
        return True

    def add(self, namespace: str, signature: str) -> bool:
        """写入签名并返回是否首次出现。"""
        cache = self._lru_cache[namespace]
        if signature in cache:
            cache.move_to_end(signature)
            self._stats["lru_hits"] += 1
            return False

        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO state_signatures(namespace, signature)
            VALUES (?, ?)
            """,
            (namespace, signature),
        )
        inserted = cursor.rowcount > 0
        if inserted:
            self._pending_writes += 1
            self._stats["inserted"] += 1

        self._remember(namespace, signature)
        if self._pending_writes >= self.commit_interval:
            self.flush()
        return inserted

    def flush(self) -> None:
        """将所有待写入的签名提交到 SQLite 数据库。"""
        if self._pending_writes > 0:
            self._conn.commit()
            self._pending_writes = 0

    def close(self) -> None:
        """关闭数据库连接，并确保所有待写入的签名已提交。"""
        try:
            self.flush()
        finally:
            self._conn.close()

    def stats_snapshot(self) -> dict[str, int]:
        """返回当前缓存和数据库的统计信息。"""
        return {
            **self._stats,
            "visited_lru_size": len(self._lru_cache["visited"]),
            "ending_lru_size": len(self._lru_cache["ending"]),
            "lru_capacity": self.lru_capacity,
        }

    def _remember(self, namespace: str, signature: str) -> None:
        """将签名添加到 LRU 缓存，并在超过容量时移除最旧的条目。"""
        cache = self._lru_cache[namespace]
        cache[signature] = None
        cache.move_to_end(signature)
        while len(cache) > self.lru_capacity:
            cache.popitem(last=False)
