from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from itertools import islice
from pathlib import Path
from typing import Any
from typing import Iterable

from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import Float
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import Text
from sqlalchemy import and_
from sqlalchemy import create_engine
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import literal
from sqlalchemy import select
from sqlalchemy import tuple_
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine

from ._config import PERSISTENCE_BATCH_SIZE


def _build_schema() -> tuple[MetaData, dict[str, Table]]:
    metadata = MetaData()
    state_signatures = Table(
        "state_signatures",
        metadata,
        Column("namespace", Text, nullable=False),
        Column("position_signature", Text, nullable=False),
        Column("signature", Text, nullable=False),
        Column("last_seen_ns", BigInteger, nullable=False),
        Column("hit_count", Integer, nullable=False),
        PrimaryKeyConstraint("namespace", "position_signature", "signature"),
        sqlite_with_rowid=False,
    )
    Index(
        "idx_state_signatures_position",
        state_signatures.c.position_signature,
        state_signatures.c.namespace,
        state_signatures.c.last_seen_ns,
    )

    # lru 是磁盘热缓存层。旧版 state_signatures 保留用于兼容迁移，
    # 新写入统一进入职责明确的 lru 表。
    lru = Table(
        "lru",
        metadata,
        Column("namespace", Text, nullable=False),
        Column("position_signature", Text, nullable=False),
        Column("signature", Text, nullable=False),
        Column("last_seen_ns", BigInteger, nullable=False),
        Column("hit_count", Integer, nullable=False),
        PrimaryKeyConstraint("namespace", "position_signature", "signature"),
        sqlite_with_rowid=False,
    )
    Index(
        "idx_lru_position",
        lru.c.position_signature,
        lru.c.namespace,
        lru.c.last_seen_ns,
    )

    simulation_runs = Table(
        "simulation_runs",
        metadata,
        Column("run_id", String(32), primary_key=True),
        Column("created_ns", BigInteger, nullable=False),
        Column("finished_ns", BigInteger),
        Column("status", Text, nullable=False),
        Column("config_json", Text, nullable=False),
        Column("summary_json", Text),
    )
    memory = Table(
        "memory",
        metadata,
        Column("run_id", String(32), primary_key=True),
        Column("config_key", Text, nullable=False),
        Column("created_ns", BigInteger, nullable=False),
        Column("updated_ns", BigInteger, nullable=False),
        Column("status", Text, nullable=False),
        Column("next_position_index", Integer),
        Column("config_json", Text, nullable=False),
        Column("summary_json", Text),
        ForeignKeyConstraint(
            ["run_id"],
            ["simulation_runs.run_id"],
            ondelete="CASCADE",
        ),
    )
    Index("idx_memory_config_status", memory.c.config_key, memory.c.status)
    solution = Table(
        "solution",
        metadata,
        Column("run_id", String(32), nullable=False),
        Column("config_key", Text, nullable=False),
        Column("position_index", Integer, nullable=False),
        Column("position_signature", Text, nullable=False),
        Column("state_count", Integer, nullable=False),
        Column("edge_count", Integer, nullable=False),
        Column("result_json", Text, nullable=False),
        Column("created_ns", BigInteger, nullable=False),
        PrimaryKeyConstraint("run_id", "position_signature"),
        ForeignKeyConstraint(
            ["run_id"],
            ["simulation_runs.run_id"],
            ondelete="CASCADE",
        ),
    )
    Index(
        "idx_solution_config_position",
        solution.c.config_key,
        solution.c.created_ns,
        solution.c.position_index,
    )
    position_results = Table(
        "position_results",
        metadata,
        Column("run_id", String(32), nullable=False),
        Column("position_index", Integer, nullable=False),
        Column("position_signature", Text, nullable=False),
        Column("roles_json", Text, nullable=False),
        Column("state_count", Integer, nullable=False),
        Column("edge_count", Integer, nullable=False),
        Column("terminal_count", Integer, nullable=False),
        Column("good_paths", Text, nullable=False),
        Column("wolf_paths", Text, nullable=False),
        Column("wide_lower", Float, nullable=False),
        Column("wide_upper", Float, nullable=False),
        Column("narrow_lower", Float, nullable=False),
        Column("narrow_upper", Float, nullable=False),
        Column("camp", Text, nullable=False),
        Column("runtime_seconds", Float, nullable=False),
        PrimaryKeyConstraint("run_id", "position_signature"),
        ForeignKeyConstraint(
            ["run_id"],
            ["simulation_runs.run_id"],
            ondelete="CASCADE",
        ),
        sqlite_with_rowid=False,
    )
    Index(
        "idx_position_results_page",
        position_results.c.run_id,
        position_results.c.position_index,
    )

    graph_nodes = Table(
        "graph_nodes",
        metadata,
        Column("run_id", String(32), nullable=False),
        Column("position_signature", Text, nullable=False),
        Column("node_id", Integer, nullable=False),
        Column("state_signature", Text, nullable=False),
        Column("phase", Text, nullable=False),
        Column("day_count", Integer, nullable=False),
        Column("night_count", Integer, nullable=False),
        Column("is_terminal", Integer, nullable=False),
        Column("result", Text, nullable=False),
        Column("good_paths", Text, nullable=False),
        Column("wolf_paths", Text, nullable=False),
        Column("wide_lower", Float, nullable=False),
        Column("wide_upper", Float, nullable=False),
        Column("narrow_lower", Float, nullable=False),
        Column("narrow_upper", Float, nullable=False),
        Column("state_json", Text, nullable=False),
        PrimaryKeyConstraint("run_id", "position_signature", "node_id"),
        ForeignKeyConstraint(
            ["run_id", "position_signature"],
            ["position_results.run_id", "position_results.position_signature"],
            ondelete="CASCADE",
        ),
        sqlite_with_rowid=False,
    )
    Index(
        "idx_graph_nodes_signature",
        graph_nodes.c.run_id,
        graph_nodes.c.position_signature,
        graph_nodes.c.state_signature,
    )

    graph_edges = Table(
        "graph_edges",
        metadata,
        Column("run_id", String(32), nullable=False),
        Column("position_signature", Text, nullable=False),
        Column("parent_id", Integer, nullable=False),
        Column("child_id", Integer, nullable=False),
        Column("action_key", Text, nullable=False),
        Column("action_label", Text, nullable=False),
        Column("action_variant_count", Integer, nullable=False),
        Column("multiplicity", Text, nullable=False),
        Column("reasons_json", Text, nullable=False),
        Column("wide_lower", Float, nullable=False),
        Column("wide_upper", Float, nullable=False),
        Column("narrow_lower", Float, nullable=False),
        Column("narrow_upper", Float, nullable=False),
        PrimaryKeyConstraint(
            "run_id",
            "position_signature",
            "parent_id",
            "child_id",
            "action_key",
        ),
        ForeignKeyConstraint(
            ["run_id", "position_signature"],
            ["position_results.run_id", "position_results.position_signature"],
            ondelete="CASCADE",
        ),
        sqlite_with_rowid=False,
    )
    Index(
        "idx_graph_edges_parent",
        graph_edges.c.run_id,
        graph_edges.c.position_signature,
        graph_edges.c.parent_id,
    )
    return metadata, {
        table.name: table
        for table in (
            state_signatures,
            lru,
            simulation_runs,
            memory,
            solution,
            position_results,
            graph_nodes,
            graph_edges,
        )
    }


class _SQLiteLRUSignatureStore:
    """SQLAlchemy Core 封装的站位感知 SQLite 持久层与 LRU 热缓存。"""

    def __init__(
        self,
        db_path: Path,
        *,
        lru_capacity: int,
        commit_interval: int,
    ) -> None:
        self.db_path = Path(db_path)
        self.lru_capacity = max(1, int(lru_capacity))
        self.commit_interval = max(1, int(commit_interval))
        self.disk_signature_capacity = max(self.lru_capacity, self.lru_capacity * 8)
        self._pending_writes = 0
        self._closed = False
        self._lock = threading.RLock()
        self._lru: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._stats = {
            "lru_hits": 0,
            "sqlite_hits": 0,
            "inserted": 0,
            "position_results": 0,
            "graph_nodes": 0,
            "graph_edges": 0,
        }
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._metadata, self._tables = _build_schema()
        self._engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(self.db_path)),
            connect_args={"timeout": 30.0, "check_same_thread": False},
        )
        self._metadata.create_all(self._engine)
        self._validate_schema(self._engine)
        self._conn: Connection = self._engine.connect()
        self._migrate_legacy_signatures()
        self._migrate_legacy_runs()

    def _validate_schema(self, engine: Engine) -> None:
        schema = inspect(engine)
        for table_name, table in self._tables.items():
            actual = {
                str(column["name"])
                for column in schema.get_columns(table_name)
            }
            expected = {column.name for column in table.columns}
            missing = expected - actual
            if missing:
                names = ", ".join(sorted(missing))
                raise RuntimeError(
                    f"SQLite 缓存表 {table_name} 与当前 schema 不兼容，"
                    f"缺少列：{names}。请移走旧缓存后重新运行。"
                )

    def _migrate_legacy_signatures(self) -> None:
        """将旧版 state_signatures 一次性迁移到 lru 表。

        迁移只复制状态签名和命中元数据，不触碰图数据及运行结果；
        使用 Core select/insert 批量执行，避免把旧缓存读入长期内存。
        """

        legacy = self._tables["state_signatures"]
        target = self._tables["lru"]
        with self._lock:
            target_count = int(
                self._conn.execute(select(func.count()).select_from(target)).scalar_one()
            )
            if target_count:
                return
            rows = self._conn.execute(select(legacy)).mappings()
            while True:
                batch = [dict(row) for row in islice(rows, PERSISTENCE_BATCH_SIZE)]
                if not batch:
                    break
                insertion = sqlite_insert(target).on_conflict_do_nothing(
                    index_elements=[
                        target.c.namespace,
                        target.c.position_signature,
                        target.c.signature,
                    ]
                )
                self._conn.execute(insertion, batch)
            self._conn.commit()

    def _migrate_legacy_runs(self) -> None:
        """为旧版 simulation_runs 补建 memory 运行记忆索引。"""

        run_table = self._tables["simulation_runs"]
        memory_table = self._tables["memory"]
        with self._lock:
            existing = set(
                self._conn.execute(select(memory_table.c.run_id)).scalars()
            )
            rows = self._conn.execute(select(run_table)).mappings()
            pending: list[dict[str, Any]] = []
            for row in rows:
                run_id = str(row["run_id"])
                if run_id in existing:
                    continue
                try:
                    config = json.loads(row["config_json"])
                    config_key = self._solution_config_key(config)
                except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                    config_key = "{}"
                summary_json = row["summary_json"]
                next_position = None
                if summary_json:
                    try:
                        summary = json.loads(summary_json)
                        next_position = summary.get("next_position_index")
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
                pending.append(
                    {
                        "run_id": run_id,
                        "config_key": config_key,
                        "created_ns": int(row["created_ns"]),
                        "updated_ns": int(row["finished_ns"] or row["created_ns"]),
                        "status": str(row["status"]),
                        "next_position_index": next_position,
                        "config_json": str(row["config_json"]),
                        "summary_json": summary_json,
                    }
                )
                if len(pending) >= PERSISTENCE_BATCH_SIZE:
                    self._conn.execute(memory_table.insert(), pending)
                    pending.clear()
            if pending:
                self._conn.execute(memory_table.insert(), pending)
            self._conn.commit()

    def schema_columns(self, table_name: str) -> set[str]:
        """使用 Inspector API 返回表列，不暴露连接或 SQL。"""

        if table_name not in self._tables:
            raise KeyError(table_name)
        return {
            str(column["name"])
            for column in inspect(self._engine).get_columns(table_name)
        }

    def _remember(self, key: tuple[str, str, str]) -> None:
        self._lru[key] = None
        self._lru.move_to_end(key)
        while len(self._lru) > self.lru_capacity:
            self._lru.popitem(last=False)

    def contains(self, namespace: str, position_signature: str, signature: str) -> bool:
        key = (namespace, position_signature, signature)
        with self._lock:
            if key in self._lru:
                self._lru.move_to_end(key)
                self._stats["lru_hits"] += 1
                return True
            table = self._tables["lru"]
            statement = (
                select(literal(True))
                .select_from(table)
                .where(
                    table.c.namespace == namespace,
                    table.c.position_signature == position_signature,
                    table.c.signature == signature,
                )
            )
            if self._conn.execute(statement).first() is None:
                return False
            self._stats["sqlite_hits"] += 1
            self._remember(key)
            return True

    def add(self, namespace: str, position_signature: str, signature: str) -> bool:
        key = (namespace, position_signature, signature)
        now = time.time_ns()
        with self._lock:
            if key in self._lru:
                self._lru.move_to_end(key)
                self._stats["lru_hits"] += 1
                return False
            table = self._tables["lru"]
            insertion = sqlite_insert(table).values(
                namespace=namespace,
                position_signature=position_signature,
                signature=signature,
                last_seen_ns=now,
                hit_count=1,
            )
            statement = insertion.on_conflict_do_update(
                index_elements=[
                    table.c.namespace,
                    table.c.position_signature,
                    table.c.signature,
                ],
                set_={
                    "last_seen_ns": insertion.excluded.last_seen_ns,
                    "hit_count": table.c.hit_count + 1,
                },
            ).returning(table.c.hit_count)
            hit_count = int(self._conn.execute(statement).scalar_one())
            inserted = hit_count == 1
            self._stats["inserted" if inserted else "sqlite_hits"] += 1
            self._remember(key)
            self._pending_writes += 1
            if self._pending_writes >= self.commit_interval:
                self.flush()
            return inserted

    def add_many(
        self,
        namespace: str,
        position_signature: str,
        signatures: Iterable[str],
    ) -> int:
        signature_iterator = (str(item) for item in signatures)
        total_inserted = 0
        with self._lock:
            table = self._tables["lru"]
            while True:
                batch = list(islice(signature_iterator, PERSISTENCE_BATCH_SIZE))
                if not batch:
                    break
                unique_signatures = list(dict.fromkeys(batch))
                existing = set(
                    self._conn.execute(
                        select(table.c.signature).where(
                            table.c.namespace == namespace,
                            table.c.position_signature == position_signature,
                            table.c.signature.in_(unique_signatures),
                        )
                    ).scalars()
                )
                new_signatures = [
                    signature
                    for signature in unique_signatures
                    if signature not in existing
                ]
                if new_signatures:
                    statement = sqlite_insert(table).on_conflict_do_nothing(
                        index_elements=[
                            table.c.namespace,
                            table.c.position_signature,
                            table.c.signature,
                        ]
                    )
                    now = time.time_ns()
                    self._conn.execute(
                        statement,
                        [
                            {
                                "namespace": namespace,
                                "position_signature": position_signature,
                                "signature": signature,
                                "last_seen_ns": now,
                                "hit_count": 1,
                            }
                            for signature in new_signatures
                        ],
                    )
                inserted = len(new_signatures)
                total_inserted += inserted
                self._stats["inserted"] += inserted
                self._stats["sqlite_hits"] += len(unique_signatures) - inserted
                for signature in unique_signatures:
                    self._remember((namespace, position_signature, signature))
                self._pending_writes += len(unique_signatures)
                if self._pending_writes >= self.commit_interval:
                    self.flush()
            return total_inserted

    @staticmethod
    def _canonical_json(value: dict[str, Any]) -> str:
        """生成稳定 JSON，确保配置键顺序不影响断点运行匹配。"""

        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _solution_config_key(cls, config: dict[str, Any]) -> str:
        """生成解复用键；lambda 只影响观测，不影响未来转移。"""

        identity = {
            key: value
            for key, value in config.items()
            if key not in {"lambda_risk", "force_recompute"}
        }
        return cls._canonical_json(identity)

    def _config_for_run(self, run_id: str) -> tuple[str, str]:
        """返回运行的规范配置 JSON 和解复用键。"""

        table = self._tables["simulation_runs"]
        row = self._conn.execute(
            select(table.c.config_json).where(table.c.run_id == run_id)
        ).first()
        if row is None:
            raise KeyError(f"未知运行批次：{run_id}")
        config_json = str(row[0])
        try:
            config = json.loads(config_json)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"运行配置损坏：{run_id}") from exc
        if not isinstance(config, dict):
            raise RuntimeError(f"运行配置不是对象：{run_id}")
        return config_json, self._solution_config_key(config)

    def start_run(self, config: dict[str, Any]) -> str:
        run_id = uuid.uuid4().hex
        now = time.time_ns()
        config_json = self._canonical_json(config)
        config_key = self._solution_config_key(config)
        with self._lock:
            self._conn.execute(
                self._tables["simulation_runs"].insert().values(
                    run_id=run_id,
                    created_ns=now,
                    finished_ns=None,
                    status="running",
                    config_json=config_json,
                    summary_json=None,
                )
            )
            self._conn.execute(
                self._tables["memory"].insert().values(
                    run_id=run_id,
                    config_key=config_key,
                    created_ns=now,
                    updated_ns=now,
                    status="running",
                    next_position_index=None,
                    config_json=config_json,
                    summary_json=None,
                )
            )
            self._conn.commit()
        return run_id

    def start_or_resume_run(
        self,
        config: dict[str, Any],
        *,
        force_new: bool = False,
    ) -> tuple[str, bool]:
        """复用最近的同配置未完成运行；否则创建新运行。

        参数：
            config: 只包含影响搜索结果的规范运行配置。
            force_new: 为 True 时忽略未完成批次，创建新的运行记录。

        返回：
            ``(run_id, resumed)``；第二项表示是否复用了已有运行。
        """

        if force_new:
            return self.start_run(config), False
        table = self._tables["simulation_runs"]
        memory_table = self._tables["memory"]
        target = self._canonical_json(config)
        target_key = self._solution_config_key(config)
        statement = (
            select(table.c.run_id, table.c.config_json)
            .where(table.c.status.in_(("running", "interrupted", "failed")))
            .order_by(table.c.created_ns.desc())
        )
        with self._lock:
            # failed 也允许在用户再次点击后恢复：站位内快照可能已经完整落盘，
            # 失败终态只描述上一次启动，不代表检查点不可继续。
            for row in self._conn.execute(statement).mappings():
                try:
                    candidate_payload = json.loads(row["config_json"])
                    candidate = self._canonical_json(candidate_payload)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if candidate != target and self._solution_config_key(candidate_payload) != target_key:
                    continue
                run_id = str(row["run_id"])
                now = time.time_ns()
                self._conn.execute(
                    update(table)
                    .where(table.c.run_id == run_id)
                    .values(
                        finished_ns=None,
                        status="running",
                        config_json=target,
                    )
                )
                self._conn.execute(
                    update(memory_table)
                    .where(memory_table.c.run_id == run_id)
                    .values(
                        updated_ns=now,
                        status="running",
                        config_key=target_key,
                        config_json=target,
                        next_position_index=None,
                    )
                )
                self._conn.commit()
                return run_id, True
        return self.start_run(config), False

    def checkpoint_run(
        self,
        run_id: str,
        summary: dict[str, Any],
        *,
        status: str = "running",
    ) -> None:
        """提交站位边界检查点，不把运行标记为已结束。

        参数：
            run_id: 当前批次的稳定标识。
            summary: 仅由已完整持久化站位构成的运行摘要。
            status: 检查点状态，正常迭代时固定为 ``running``。
        """

        with self._lock:
            table = self._tables["simulation_runs"]
            memory_table = self._tables["memory"]
            now = time.time_ns()
            self._conn.execute(
                update(table)
                .where(table.c.run_id == run_id)
                .values(
                    finished_ns=None,
                    status=status,
                    summary_json=self._canonical_json(summary),
                )
            )
            next_position = summary.get("next_position_index")
            config_json, config_key = self._config_for_run(run_id)
            self._conn.execute(
                update(memory_table)
                .where(memory_table.c.run_id == run_id)
                .values(
                    updated_ns=now,
                    status=status,
                    config_key=config_key,
                    config_json=config_json,
                    next_position_index=(
                        int(next_position) if next_position is not None else None
                    ),
                    summary_json=self._canonical_json(summary),
                )
            )
            self._conn.commit()

    def finish_run(
        self,
        run_id: str,
        summary: dict[str, Any],
        *,
        status: str,
    ) -> None:
        """写入唯一运行终态，并拒绝把不完整 DAG 登记为 solution。"""

        if status == "complete":
            positions = list(summary.get("positions", []))
            position_count = int(summary.get("position_count", len(positions)))
            total_position_count = int(summary.get("total_position_count", 0))
            invalid_position = any(
                int(item.get("state_count", 0)) <= 0
                for item in positions
            )
            if (
                total_position_count <= 0
                or position_count != total_position_count
                or len(positions) != total_position_count
                or invalid_position
            ):
                raise ValueError(
                    "拒绝登记不完整 solution："
                    f"positions={position_count}/{total_position_count}, "
                    f"summaries={len(positions)}, invalid={invalid_position}"
                )
        with self._lock:
            table = self._tables["simulation_runs"]
            memory_table = self._tables["memory"]
            now = time.time_ns()
            self._conn.execute(
                update(table)
                .where(table.c.run_id == run_id)
                .values(
                    finished_ns=now,
                    status=status,
                    summary_json=self._canonical_json(summary),
                )
            )
            config_json, config_key = self._config_for_run(run_id)
            self._conn.execute(
                update(memory_table)
                .where(memory_table.c.run_id == run_id)
                .values(
                    updated_ns=now,
                    status=status,
                    config_key=config_key,
                    config_json=config_json,
                    next_position_index=(
                        int(summary["next_position_index"])
                        if summary.get("next_position_index") is not None
                        else None
                    ),
                    summary_json=self._canonical_json(summary),
                )
            )
            if status == "complete":
                self._record_solution_rows(
                    run_id=run_id,
                    config_key=config_key,
                    summaries=summary.get("positions", []),
                    created_ns=now,
                )
            self._conn.commit()

    def _record_solution_rows(
        self,
        *,
        run_id: str,
        config_key: str,
        summaries: Iterable[dict[str, Any]],
        created_ns: int,
    ) -> None:
        """登记完整站位解索引，不复制节点和边数据。"""

        table = self._tables["solution"]
        rows = []
        for item in summaries:
            rows.append(
                {
                    "run_id": run_id,
                    "config_key": config_key,
                    "position_index": int(item["position_index"]),
                    "position_signature": str(item["position_signature"]),
                    "state_count": int(item["state_count"]),
                    "edge_count": int(item["edge_count"]),
                    "result_json": self._canonical_json(item),
                    "created_ns": created_ns,
                }
            )
        if not rows:
            return
        insertion = sqlite_insert(table)
        self._conn.execute(
            insertion.on_conflict_do_update(
                index_elements=[table.c.run_id, table.c.position_signature],
                set_={
                    "config_key": insertion.excluded.config_key,
                    "position_index": insertion.excluded.position_index,
                    "state_count": insertion.excluded.state_count,
                    "edge_count": insertion.excluded.edge_count,
                    "result_json": insertion.excluded.result_json,
                    "created_ns": insertion.excluded.created_ns,
                },
            ),
            rows,
        )

    def load_solution(
        self,
        config: dict[str, Any],
        *,
        expected_position_signatures: set[str],
    ) -> dict[str, Any] | None:
        """查询并校验可复用完整解，命中时只返回摘要索引。"""

        table = self._tables["solution"]
        run_table = self._tables["simulation_runs"]
        config_key = self._solution_config_key(config)
        statement = (
            select(table.c.run_id, table.c.position_signature, table.c.created_ns)
            .select_from(table.join(run_table, table.c.run_id == run_table.c.run_id))
            .where(table.c.config_key == config_key)
            .where(run_table.c.status == "complete")
            .order_by(table.c.created_ns.desc(), table.c.position_index)
        )
        with self._lock:
            rows = self._conn.execute(statement).mappings().all()
        candidate_ids: list[str] = []
        for row in rows:
            run_id = str(row["run_id"])
            if run_id not in candidate_ids:
                candidate_ids.append(run_id)
        expected = {str(item) for item in expected_position_signatures}
        for run_id in candidate_ids:
            summaries = self.list_completed_position_results(run_id)
            by_signature = {
                str(item["position_signature"]): item for item in summaries
            }
            if expected and not expected.issubset(by_signature):
                continue
            selected = [by_signature[item] for item in expected if item in by_signature]
            selected.sort(key=lambda item: int(item["position_index"]))
            if len(selected) != len(expected):
                continue
            source_config_json, _ = self._config_for_run(run_id)
            try:
                source_config = json.loads(source_config_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                source_config = {}
            source_lambda = source_config.get("lambda_risk")
            for item in selected:
                item["restored_from_solution"] = True
                if source_lambda is not None:
                    # UI 据此判断是否需要按当前滑块动态回传 interval。
                    item["interval_lambda"] = float(source_lambda)
                item.pop("restored_from_checkpoint", None)
            return {
                "run_id": run_id,
                "config_key": config_key,
                "source_lambda": (
                    float(source_lambda) if source_lambda is not None else None
                ),
                "positions": selected,
            }
        return None

    def list_memory_runs(
        self,
        *,
        config: dict[str, Any] | None = None,
        statuses: tuple[str, ...] = ("running", "interrupted", "failed"),
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """查询断点记忆摘要，供恢复面板和 CLI 检查使用。"""

        table = self._tables["memory"]
        conditions = [table.c.status.in_(tuple(statuses))]
        if config is not None:
            conditions.append(table.c.config_key == self._solution_config_key(config))
        statement = (
            select(table)
            .where(and_(*conditions))
            .order_by(table.c.updated_ns.desc())
            .limit(max(1, int(limit)))
        )
        with self._lock:
            rows = self._conn.execute(statement).mappings().all()
        return [
            {
                "run_id": str(row["run_id"]),
                "config_key": str(row["config_key"]),
                "created_ns": int(row["created_ns"]),
                "updated_ns": int(row["updated_ns"]),
                "status": str(row["status"]),
                "next_position_index": row["next_position_index"],
                "config": json.loads(row["config_json"]),
                "summary": (
                    json.loads(row["summary_json"])
                    if row["summary_json"]
                    else None
                ),
            }
            for row in rows
        ]

    def get_memory_run(self, run_id: str) -> dict[str, Any] | None:
        """按稳定运行 ID 查询单条断点记忆，不依赖分页上限。"""

        table = self._tables["memory"]
        statement = select(table).where(table.c.run_id == str(run_id))
        with self._lock:
            row = self._conn.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        return {
            "run_id": str(row["run_id"]),
            "config_key": str(row["config_key"]),
            "created_ns": int(row["created_ns"]),
            "updated_ns": int(row["updated_ns"]),
            "status": str(row["status"]),
            "next_position_index": row["next_position_index"],
            "config": json.loads(row["config_json"]),
            "summary": (
                json.loads(row["summary_json"])
                if row["summary_json"]
                else None
            ),
        }

    def list_solution_runs(
        self,
        *,
        config: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """查询 solution 索引中的历史完整运行，不读取完整 DAG。"""

        table = self._tables["solution"]
        run_table = self._tables["simulation_runs"]
        conditions = [run_table.c.status == "complete"]
        if config is not None:
            conditions.append(table.c.config_key == self._solution_config_key(config))
        statement = (
            select(
                table.c.run_id,
                table.c.config_key,
                func.max(table.c.created_ns).label("created_ns"),
                func.count().label("position_count"),
            )
            .select_from(table.join(run_table, table.c.run_id == run_table.c.run_id))
            .where(and_(*conditions))
            .group_by(table.c.run_id, table.c.config_key)
            .order_by(func.max(table.c.created_ns).desc())
            .limit(max(1, int(limit)))
        )
        with self._lock:
            rows = self._conn.execute(statement).mappings().all()
        return [
            {
                "run_id": str(row["run_id"]),
                "config_key": str(row["config_key"]),
                "created_ns": int(row["created_ns"]),
                "position_count": int(row["position_count"]),
            }
            for row in rows
        ]

    @staticmethod
    def _node_row(
        run_id: str,
        position_signature: str,
        node: dict[str, Any],
    ) -> dict[str, Any]:
        state_payload = node.get("state")
        if state_payload is None:
            state_payload = {
                "format": "game_state_compact_v1",
                "state_compact": node.get("state_compact"),
                "state_observation": node.get("state_observation", ((), "", None, 0, "")),
            }
        return {
            "run_id": run_id,
            "position_signature": position_signature,
            "node_id": int(node["node_id"]),
            "state_signature": str(node["state_signature"]),
            "phase": str(node["phase"]),
            "day_count": int(node["day_count"]),
            "night_count": int(node["night_count"]),
            "is_terminal": int(bool(node["is_terminal"])),
            "result": str(node["result"]),
            "good_paths": str(node["good_paths"]),
            "wolf_paths": str(node["wolf_paths"]),
            "wide_lower": float(node["wide_interval"][0]),
            "wide_upper": float(node["wide_interval"][1]),
            "narrow_lower": float(node["narrow_interval"][0]),
            "narrow_upper": float(node["narrow_interval"][1]),
            "state_json": json.dumps(
                state_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _edge_row(
        run_id: str,
        position_signature: str,
        edge: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "position_signature": position_signature,
            "parent_id": int(edge["parent_id"]),
            "child_id": int(edge["child_id"]),
            "action_key": str(edge["action_key"]),
            "action_label": str(edge["action_label"]),
            "action_variant_count": int(edge.get("action_variant_count", 1)),
            "multiplicity": str(edge["multiplicity"]),
            "reasons_json": json.dumps(
                edge.get("reasons", []),
                ensure_ascii=False,
            ),
            "wide_lower": float(edge["wide_interval"][0]),
            "wide_upper": float(edge["wide_interval"][1]),
            "narrow_lower": float(edge["narrow_interval"][0]),
            "narrow_upper": float(edge["narrow_interval"][1]),
        }

    def begin_position_result(self, run_id: str, result: dict[str, Any]) -> None:
        wide = result["wide_interval"]
        narrow = result["narrow_interval"]
        position_signature = str(result["position_signature"])
        position_table = self._tables["position_results"]
        node_table = self._tables["graph_nodes"]
        edge_table = self._tables["graph_edges"]
        position_values = {
            "run_id": run_id,
            "position_index": int(result["position_index"]),
            "position_signature": position_signature,
            "roles_json": json.dumps(result["roles"], ensure_ascii=False),
            "state_count": int(result["state_count"]),
            "edge_count": int(result["edge_count"]),
            "terminal_count": int(result["terminal_count"]),
            "good_paths": str(result["good_paths"]),
            "wolf_paths": str(result["wolf_paths"]),
            "wide_lower": float(wide[0]),
            "wide_upper": float(wide[1]),
            "narrow_lower": float(narrow[0]),
            "narrow_upper": float(narrow[1]),
            "camp": str(result["camp"]),
            "runtime_seconds": float(result["runtime_seconds"]),
        }
        insertion = sqlite_insert(position_table).values(**position_values)
        position_upsert = insertion.on_conflict_do_update(
            index_elements=[
                position_table.c.run_id,
                position_table.c.position_signature,
            ],
            set_={
                key: getattr(insertion.excluded, key)
                for key in position_values
                if key not in {"run_id", "position_signature"}
            },
        )
        with self._lock:
            self._conn.execute(position_upsert)
            node_constraints = (
                node_table.c.run_id == run_id,
                node_table.c.position_signature == position_signature,
            )
            edge_constraints = (
                edge_table.c.run_id == run_id,
                edge_table.c.position_signature == position_signature,
            )
            self._conn.execute(delete(edge_table).where(*edge_constraints))
            self._conn.execute(delete(node_table).where(*node_constraints))
            self._conn.commit()

    def begin_position_staging(
        self,
        run_id: str,
        result: dict[str, Any],
    ) -> None:
        """创建站位暂存行并清空旧残片，允许搜索过程中流式写边。"""

        provisional = {
            **result,
            # -1 明确表示“暂存中”；0 个持久化节点不能与它相等并被误判完成。
            "state_count": -1,
            "edge_count": -1,
            "terminal_count": 0,
            "good_paths": 0,
            "wolf_paths": 0,
            "wide_interval": [-1.0, 1.0],
            "narrow_interval": [-1.0, 1.0],
            "camp": "未决",
            "runtime_seconds": 0.0,
        }
        self.begin_position_result(run_id, provisional)

    def update_position_result_summary(
        self,
        run_id: str,
        result: dict[str, Any],
    ) -> None:
        """更新暂存站位摘要，但保留已经流式写入的节点和边。"""

        wide = result["wide_interval"]
        narrow = result["narrow_interval"]
        position_signature = str(result["position_signature"])
        table = self._tables["position_results"]
        values = {
            "position_index": int(result["position_index"]),
            "roles_json": json.dumps(result["roles"], ensure_ascii=False),
            "state_count": int(result["state_count"]),
            "edge_count": int(result["edge_count"]),
            "terminal_count": int(result["terminal_count"]),
            "good_paths": str(result["good_paths"]),
            "wolf_paths": str(result["wolf_paths"]),
            "wide_lower": float(wide[0]),
            "wide_upper": float(wide[1]),
            "narrow_lower": float(narrow[0]),
            "narrow_upper": float(narrow[1]),
            "camp": str(result["camp"]),
            "runtime_seconds": float(result["runtime_seconds"]),
        }
        with self._lock:
            self._conn.execute(
                update(table)
                .where(
                    table.c.run_id == run_id,
                    table.c.position_signature == position_signature,
                )
                .values(**values)
            )
            self._conn.commit()

    def sync_position_edge_intervals(
        self,
        run_id: str,
        position_signature: str,
    ) -> None:
        """用子节点最终 interval 一次性同步暂存边，避免逐边 Python 更新。"""

        edge_table = self._tables["graph_edges"]
        child = self._tables["graph_nodes"].alias("child_interval")

        def child_value(column: Any) -> Any:
            return (
                select(column)
                .where(
                    child.c.run_id == edge_table.c.run_id,
                    child.c.position_signature == edge_table.c.position_signature,
                    child.c.node_id == edge_table.c.child_id,
                )
                .scalar_subquery()
            )

        with self._lock:
            self._conn.execute(
                update(edge_table)
                .where(
                    edge_table.c.run_id == run_id,
                    edge_table.c.position_signature == position_signature,
                )
                .values(
                    wide_lower=child_value(child.c.wide_lower),
                    wide_upper=child_value(child.c.wide_upper),
                    narrow_lower=child_value(child.c.narrow_lower),
                    narrow_upper=child_value(child.c.narrow_upper),
                )
            )
            self._conn.commit()

    def append_position_nodes(
        self,
        run_id: str,
        position_signature: str,
        nodes: list[dict[str, Any]],
    ) -> None:
        if not nodes:
            return
        self.add_many(
            "state",
            position_signature,
            (str(node["state_signature"]) for node in nodes),
        )
        rows = [self._node_row(run_id, position_signature, node) for node in nodes]
        with self._lock:
            self._conn.execute(self._tables["graph_nodes"].insert(), rows)
            self._conn.commit()
            self._stats["graph_nodes"] += len(rows)

    def append_position_edges(
        self,
        run_id: str,
        position_signature: str,
        edges: list[dict[str, Any]],
    ) -> None:
        if not edges:
            return
        rows = [self._edge_row(run_id, position_signature, edge) for edge in edges]
        with self._lock:
            self._conn.execute(self._tables["graph_edges"].insert(), rows)
            self._conn.commit()
            self._stats["graph_edges"] += len(rows)

    def finish_position_result(self) -> None:
        with self._lock:
            self._stats["position_results"] += 1

    def abort_position_result(self, run_id: str, position_signature: str) -> None:
        with self._lock:
            for table_name in ("graph_edges", "graph_nodes", "position_results"):
                table = self._tables[table_name]
                self._conn.execute(
                    delete(table).where(
                        table.c.run_id == run_id,
                        table.c.position_signature == position_signature,
                    )
                )
            self._conn.commit()

    def save_position_result(self, run_id: str, result: dict[str, Any]) -> None:
        position_signature = str(result["position_signature"])
        self.begin_position_result(run_id, result)
        for start in range(0, len(result["nodes"]), PERSISTENCE_BATCH_SIZE):
            self.append_position_nodes(
                run_id,
                position_signature,
                result["nodes"][start : start + PERSISTENCE_BATCH_SIZE],
            )
        for start in range(0, len(result["edges"]), PERSISTENCE_BATCH_SIZE):
            self.append_position_edges(
                run_id,
                position_signature,
                result["edges"][start : start + PERSISTENCE_BATCH_SIZE],
            )
        self.finish_position_result()

    def list_position_results(
        self,
        run_id: str,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        table = self._tables["position_results"]
        statement = (
            select(table)
            .where(table.c.run_id == run_id)
            .order_by(table.c.position_index)
            .limit(max(1, limit))
            .offset(max(0, offset))
        )
        with self._lock:
            rows = self._conn.execute(statement).mappings().all()
        return [
            {
                "position_index": row["position_index"],
                "position_signature": row["position_signature"],
                "roles": json.loads(row["roles_json"]),
                "state_count": row["state_count"],
                "edge_count": row["edge_count"],
                "terminal_count": row["terminal_count"],
                "good_paths": int(row["good_paths"]),
                "wolf_paths": int(row["wolf_paths"]),
                "wide_interval": [row["wide_lower"], row["wide_upper"]],
                "narrow_interval": [row["narrow_lower"], row["narrow_upper"]],
                "camp": row["camp"],
                "runtime_seconds": row["runtime_seconds"],
            }
            for row in rows
        ]

    def list_completed_position_results(self, run_id: str) -> list[dict[str, Any]]:
        """仅返回节点数和边数都与摘要一致的完整站位。

        参数：
            run_id: 待读取批次标识。

        返回：
            可安全跳过重算的完整站位摘要，按站位编号升序排列。
        """

        position_table = self._tables["position_results"]
        node_table = self._tables["graph_nodes"]
        edge_table = self._tables["graph_edges"]
        node_count = (
            select(func.count())
            .select_from(node_table)
            .where(
                and_(
                    node_table.c.run_id == position_table.c.run_id,
                    node_table.c.position_signature
                    == position_table.c.position_signature,
                )
            )
            .correlate(position_table)
            .scalar_subquery()
        )
        edge_count = (
            select(func.count())
            .select_from(edge_table)
            .where(
                and_(
                    edge_table.c.run_id == position_table.c.run_id,
                    edge_table.c.position_signature
                    == position_table.c.position_signature,
                )
            )
            .correlate(position_table)
            .scalar_subquery()
        )
        statement = (
            select(
                position_table,
                node_count.label("persisted_node_count"),
                edge_count.label("persisted_edge_count"),
            )
            .where(position_table.c.run_id == run_id)
            .order_by(position_table.c.position_index)
        )
        with self._lock:
            rows = self._conn.execute(statement).mappings().all()
        complete_rows = [
            row
            for row in rows
            if int(row["state_count"]) > 0
            and int(row["persisted_node_count"]) == int(row["state_count"])
            and int(row["persisted_edge_count"]) == int(row["edge_count"])
        ]
        # position_results 会先于节点/边批次写入，因此单看摘要行无法
        # 判断站位是否完整；只有两个实际计数都吻合才能作为检查点。
        return [
            {
                "position_index": row["position_index"],
                "position_signature": row["position_signature"],
                "roles": json.loads(row["roles_json"]),
                "state_count": row["state_count"],
                "edge_count": row["edge_count"],
                "terminal_count": row["terminal_count"],
                "processed_states": row["state_count"],
                "good_paths": int(row["good_paths"]),
                "wolf_paths": int(row["wolf_paths"]),
                "wide_interval": [row["wide_lower"], row["wide_upper"]],
                "narrow_interval": [row["narrow_lower"], row["narrow_upper"]],
                "camp": row["camp"],
                "runtime_seconds": row["runtime_seconds"],
                "restored_from_checkpoint": True,
            }
            for row in complete_rows
        ]

    def discard_incomplete_position_results(
        self,
        run_id: str,
        *,
        preserve_signatures: set[str] | None = None,
    ) -> int:
        """删除半写入站位；完整站位保持不变。

        参数：
            run_id: 待清理批次标识。

        返回：
            被清理的不完整站位数量。
        """

        position_table = self._tables["position_results"]
        with self._lock:
            all_signatures = set(
                self._conn.execute(
                    select(position_table.c.position_signature).where(
                        position_table.c.run_id == run_id
                    )
                ).scalars()
            )
        complete_signatures = {
            item["position_signature"]
            for item in self.list_completed_position_results(run_id)
        }
        incomplete = all_signatures - complete_signatures
        if preserve_signatures:
            incomplete -= {str(item) for item in preserve_signatures}
        # 删除顺序由 abort_position_result 统一维护，搜索层不接触表结构。
        for position_signature_value in incomplete:
            self.abort_position_result(run_id, position_signature_value)
        return len(incomplete)

    def get_position_graph(
        self,
        run_id: str,
        position_signature: str,
    ) -> dict[str, list[dict[str, Any]]]:
        node_table = self._tables["graph_nodes"]
        edge_table = self._tables["graph_edges"]
        node_statement = (
            select(node_table)
            .where(
                node_table.c.run_id == run_id,
                node_table.c.position_signature == position_signature,
            )
            .order_by(node_table.c.node_id)
        )
        edge_statement = (
            select(edge_table)
            .where(
                edge_table.c.run_id == run_id,
                edge_table.c.position_signature == position_signature,
            )
            .order_by(edge_table.c.parent_id, edge_table.c.child_id)
        )
        with self._lock:
            nodes = self._conn.execute(node_statement).mappings().all()
            edges = self._conn.execute(edge_statement).mappings().all()
        graph_nodes: list[dict[str, Any]] = []
        for row in nodes:
            state_payload = json.loads(row["state_json"])
            node = {
                "node_id": row["node_id"],
                "state_signature": row["state_signature"],
                "phase": row["phase"],
                "day_count": row["day_count"],
                "night_count": row["night_count"],
                "is_terminal": bool(row["is_terminal"]),
                "result": row["result"],
                "good_paths": int(row["good_paths"]),
                "wolf_paths": int(row["wolf_paths"]),
                "wide_interval": [row["wide_lower"], row["wide_upper"]],
                "narrow_interval": [row["narrow_lower"], row["narrow_upper"]],
            }
            if (
                isinstance(state_payload, dict)
                and state_payload.get("format") == "game_state_compact_v1"
            ):
                node["state_compact"] = state_payload.get("state_compact", [])
                node["state_observation"] = state_payload.get(
                    "state_observation",
                    [[], "", None, 0, ""],
                )
            elif isinstance(state_payload, dict):
                node["state"] = state_payload
            else:
                node["state_compact"] = state_payload
            graph_nodes.append(node)
        return {
            "nodes": graph_nodes,
            "edges": [
                {
                    "parent_id": row["parent_id"],
                    "child_id": row["child_id"],
                    "action_key": row["action_key"],
                    "action_label": row["action_label"],
                    "action_variant_count": int(row["action_variant_count"]),
                    "multiplicity": int(row["multiplicity"]),
                    "reasons": json.loads(row["reasons_json"]),
                    "wide_interval": [row["wide_lower"], row["wide_upper"]],
                    "narrow_interval": [row["narrow_lower"], row["narrow_upper"]],
                }
                for row in edges
            ],
        }

    def flush(self) -> None:
        with self._lock:
            if self._pending_writes <= 0:
                return
            table = self._tables["lru"]
            count = int(
                self._conn.execute(select(func.count()).select_from(table)).scalar_one()
            )
            overflow = count - self.disk_signature_capacity
            if overflow > 0:
                victim_statement = (
                    select(
                        table.c.namespace,
                        table.c.position_signature,
                        table.c.signature,
                    )
                    .order_by(table.c.last_seen_ns)
                    .limit(overflow)
                )
                victims = list(self._conn.execute(victim_statement).tuples())
                if victims:
                    key_columns = tuple_(
                        table.c.namespace,
                        table.c.position_signature,
                        table.c.signature,
                    )
                    self._conn.execute(delete(table).where(key_columns.in_(victims)))
            self._conn.commit()
            self._pending_writes = 0

    def stats_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                **self._stats,
                "lru_size": len(self._lru),
                "lru_capacity": self.lru_capacity,
                "disk_signature_capacity": self.disk_signature_capacity,
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.flush()
            self._conn.close()
            self._engine.dispose()
            self._closed = True
