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
            simulation_runs,
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
            table = self._tables["state_signatures"]
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
            table = self._tables["state_signatures"]
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
            table = self._tables["state_signatures"]
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

    def start_run(self, config: dict[str, Any]) -> str:
        run_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                self._tables["simulation_runs"].insert().values(
                    run_id=run_id,
                    created_ns=time.time_ns(),
                    finished_ns=None,
                    status="running",
                    config_json=self._canonical_json(config),
                    summary_json=None,
                )
            )
            self._conn.commit()
        return run_id

    def start_or_resume_run(self, config: dict[str, Any]) -> tuple[str, bool]:
        """复用最近的同配置未完成运行；否则创建新运行。

        参数：
            config: 只包含影响搜索结果的规范运行配置。

        返回：
            ``(run_id, resumed)``；第二项表示是否复用了已有运行。
        """

        table = self._tables["simulation_runs"]
        target = self._canonical_json(config)
        statement = (
            select(table.c.run_id, table.c.config_json)
            .where(table.c.status.in_(("running", "interrupted")))
            .order_by(table.c.created_ns.desc())
        )
        with self._lock:
            # 同时读取 running 与 interrupted：进程崩溃来不及收尾时，
            # 数据库会保留 running；该状态同样属于可恢复运行。
            for row in self._conn.execute(statement).mappings():
                try:
                    candidate = self._canonical_json(json.loads(row["config_json"]))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if candidate != target:
                    continue
                run_id = str(row["run_id"])
                self._conn.execute(
                    update(table)
                    .where(table.c.run_id == run_id)
                    .values(
                        finished_ns=None,
                        status="running",
                        config_json=target,
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
            self._conn.execute(
                update(table)
                .where(table.c.run_id == run_id)
                .values(
                    finished_ns=None,
                    status=status,
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
        with self._lock:
            table = self._tables["simulation_runs"]
            self._conn.execute(
                update(table)
                .where(table.c.run_id == run_id)
                .values(
                    finished_ns=time.time_ns(),
                    status=status,
                    summary_json=self._canonical_json(summary),
                )
            )
            self._conn.commit()

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
            if int(row["persisted_node_count"]) == int(row["state_count"])
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

    def discard_incomplete_position_results(self, run_id: str) -> int:
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
            table = self._tables["state_signatures"]
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
