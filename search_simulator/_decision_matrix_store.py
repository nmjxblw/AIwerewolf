"""精确信念 Cheap-talk 决策矩阵的 SQLAlchemy Core 持久化。

本模块是矩阵唯一数据库写入口。所有 DDL、查询、upsert 和完整性校验均
通过 SQLAlchemy Core 构造，计算 worker 不得导入本模块。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Column
from sqlalchemy import Float
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine


def _now() -> str:
    """生成可审计的 UTC 时间戳。"""

    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DecisionMatrixStore:
    """在现有 SQLite 文件中管理三张独立的决策矩阵表。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{self.database_path.resolve().as_posix()}"
        self.engine: Engine = create_engine(url, future=True)
        self.metadata = MetaData()
        self.runs = Table(
            "decision_matrix_runs",
            self.metadata,
            # matrix_id 是一次可恢复运行的不可变标识。
            Column("matrix_id", String(64), primary_key=True),
            Column("request_digest", String(128), nullable=False, unique=True),
            Column("request_json", Text, nullable=False),
            Column("target_samples", Integer, nullable=False),
            Column("expected_cell_count", Integer, nullable=False),
            Column("status", String(20), nullable=False),
            Column("error_summary", Text, nullable=True),
            Column("created_at", String(64), nullable=False),
            Column("updated_at", String(64), nullable=False),
        )
        self.rows = Table(
            "decision_matrix_rows",
            self.metadata,
            Column("matrix_id", String(64), nullable=False),
            Column("action_key", String(512), nullable=False),
            Column("action_family", String(64), nullable=False),
            Column("action_json", Text, nullable=False),
            Column("credibility", Float, nullable=False),
            Column("sample_count", Integer, nullable=False),
            Column("reward_sum", Float, nullable=False),
            Column("reward_sum_sq", Float, nullable=False),
            Column("delta_sum", Float, nullable=False),
            Column("delta_sum_sq", Float, nullable=False),
            Column("scenario_counts_json", Text, nullable=False),
            Column("updated_at", String(64), nullable=False),
            UniqueConstraint("matrix_id", "action_key", "credibility", name="uq_decision_matrix_row"),
        )
        self.batches = Table(
            "decision_matrix_batches",
            self.metadata,
            Column("batch_pk", String(128), primary_key=True),
            Column("matrix_id", String(64), nullable=False),
            Column("credibility", Float, nullable=False),
            Column("batch_id", String(128), nullable=False),
            Column("sample_start", Integer, nullable=False),
            Column("sample_end", Integer, nullable=False),
            Column("aggregate_json", Text, nullable=False),
            Column("status", String(20), nullable=False),
            Column("committed_at", String(64), nullable=True),
            UniqueConstraint("matrix_id", "credibility", "batch_id", name="uq_decision_matrix_batch"),
        )
        self.metadata.create_all(self.engine)
        self._validate_schema()

    def _validate_schema(self) -> None:
        """用 Inspector 确认三张决策矩阵表已经存在。"""

        names = set(inspect(self.engine).get_table_names())
        expected = {self.runs.name, self.rows.name, self.batches.name}
        missing = expected - names
        if missing:
            raise RuntimeError(f"决策矩阵表缺失: {sorted(missing)}")

    def find_run(self, request_digest: str) -> dict[str, Any] | None:
        """按规范请求摘要读取最新运行头。"""

        statement = select(self.runs).where(self.runs.c.request_digest == str(request_digest))
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().first()
        return dict(row) if row is not None else None

    def start_run(
        self,
        *,
        request_digest: str,
        request_json: dict[str, Any],
        target_samples: int,
        expected_cell_count: int,
        force_new: bool = False,
    ) -> tuple[str, bool, bool]:
        """创建、恢复或复用运行。

        返回 ``(matrix_id, resumed, already_complete)``。强制重算会派生新的
        request_digest，不覆盖历史矩阵。
        """

        digest = str(request_digest)
        existing = self.find_run(digest)
        if existing is not None and existing["status"] == "failed" and not force_new:
            # 失败记录保留作审计；新的计算必须使用新的运行身份。
            force_new = True
        if existing is not None and not force_new:
            matrix_id = str(existing["matrix_id"])
            if existing["status"] == "complete":
                return matrix_id, False, True
            with self.engine.begin() as connection:
                connection.execute(
                    update(self.runs)
                    .where(self.runs.c.matrix_id == matrix_id)
                    .values(status="running", updated_at=_now(), error_summary=None)
                )
            return matrix_id, True, False
        matrix_id = uuid.uuid4().hex
        if force_new:
            digest = f"{digest}:force:{matrix_id}"
        now = _now()
        values = {
            "matrix_id": matrix_id,
            "request_digest": digest,
            "request_json": _json(request_json),
            "target_samples": int(target_samples),
            "expected_cell_count": int(expected_cell_count),
            "status": "running",
            "error_summary": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as connection:
            connection.execute(self.runs.insert().values(**values))
        return matrix_id, False, False

    def initialize_rows(
        self, *, matrix_id: str, actions: tuple[dict[str, Any], ...], credibility_levels: tuple[float, ...]
    ) -> None:
        """为所有二级动作和可信度档位建立零值行，重复调用幂等。"""

        now = _now()
        values = []
        for action in actions:
            for credibility in credibility_levels:
                values.append(
                    {
                        "matrix_id": matrix_id,
                        "action_key": str(action["action_key"]),
                        "action_family": str(action["action_family"]),
                        "action_json": _json(action["action_json"]),
                        "credibility": float(credibility),
                        "sample_count": 0,
                        "reward_sum": 0.0,
                        "reward_sum_sq": 0.0,
                        "delta_sum": 0.0,
                        "delta_sum_sq": 0.0,
                        "scenario_counts_json": _json({}),
                        "updated_at": now,
                    }
                )
        if not values:
            return
        insertion = sqlite_insert(self.rows).values(values)
        statement = insertion.on_conflict_do_nothing(
            index_elements=[self.rows.c.matrix_id, self.rows.c.action_key, self.rows.c.credibility]
        )
        with self.engine.begin() as connection:
            connection.execute(statement)

    def committed_batches(self, *, matrix_id: str) -> set[str]:
        """返回已经提交的稳定 batch_id 集合。"""

        statement = select(self.batches.c.batch_id).where(
            self.batches.c.matrix_id == str(matrix_id),
            self.batches.c.status == "committed",
        )
        with self.engine.connect() as connection:
            return {str(value) for value in connection.execute(statement).scalars()}

    def commit_batch(self, *, matrix_id: str, batch: dict[str, Any]) -> bool:
        """在单事务中幂等提交批次并累加矩阵充分统计量。"""

        credibility = float(batch["credibility"])
        batch_id = str(batch["batch_id"])
        batch_pk = f"{matrix_id}:{batch_id}"
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(self.batches.c.status).where(self.batches.c.batch_pk == batch_pk)
            ).scalar_one_or_none()
            if existing == "committed":
                return False
            batch_values = {
                "batch_pk": batch_pk,
                "matrix_id": matrix_id,
                "credibility": credibility,
                "batch_id": batch_id,
                "sample_start": int(batch["sample_start"]),
                "sample_end": int(batch["sample_end"]),
                "aggregate_json": _json(batch),
                "status": "committed",
                "committed_at": _now(),
            }
            if existing is None:
                connection.execute(self.batches.insert().values(**batch_values))
            else:
                connection.execute(
                    update(self.batches).where(self.batches.c.batch_pk == batch_pk).values(**batch_values)
                )
            for incoming in batch.get("rows", ()):
                action_key = str(incoming["action_key"])
                current = (
                    connection.execute(
                        select(self.rows).where(
                            self.rows.c.matrix_id == matrix_id,
                            self.rows.c.action_key == action_key,
                            self.rows.c.credibility == credibility,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if current is None:
                    raise RuntimeError(f"矩阵行不存在，拒绝写入: {action_key}")
                old_scenarios = json.loads(current["scenario_counts_json"] or "{}")
                for name, count in (incoming.get("scenario_counts") or {}).items():
                    old_scenarios[str(name)] = int(old_scenarios.get(str(name), 0)) + int(count)
                connection.execute(
                    update(self.rows)
                    .where(
                        self.rows.c.matrix_id == matrix_id,
                        self.rows.c.action_key == action_key,
                        self.rows.c.credibility == credibility,
                    )
                    .values(
                        sample_count=int(current["sample_count"]) + int(incoming["sample_count"]),
                        reward_sum=float(current["reward_sum"]) + float(incoming["reward_sum"]),
                        reward_sum_sq=float(current["reward_sum_sq"]) + float(incoming["reward_sum_sq"]),
                        delta_sum=float(current["delta_sum"]) + float(incoming["delta_sum"]),
                        delta_sum_sq=float(current["delta_sum_sq"]) + float(incoming["delta_sum_sq"]),
                        scenario_counts_json=_json(old_scenarios),
                        updated_at=_now(),
                    )
                )
            connection.execute(
                update(self.runs).where(self.runs.c.matrix_id == matrix_id).values(updated_at=_now(), status="running")
            )
        return True

    def mark_status(self, *, matrix_id: str, status: str, error_summary: str | None = None) -> None:
        """更新运行终态或可恢复状态。"""

        if status not in {"running", "complete", "interrupted", "failed"}:
            raise ValueError(f"未知矩阵运行状态: {status}")
        with self.engine.begin() as connection:
            connection.execute(
                update(self.runs)
                .where(self.runs.c.matrix_id == str(matrix_id))
                .values(status=status, error_summary=error_summary, updated_at=_now())
            )

    def validate_complete(
        self,
        *,
        matrix_id: str,
        target_samples: int,
        expected_cells: int,
    ) -> bool:
        """校验所有矩阵行样本数和情景计数后再标记 complete。"""

        statement = select(self.rows).where(self.rows.c.matrix_id == str(matrix_id))
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        if len(rows) != int(expected_cells):
            return False
        for row in rows:
            if int(row["sample_count"]) != int(target_samples):
                return False
            scenario_counts = json.loads(row["scenario_counts_json"] or "{}")
            if sum(int(value) for value in scenario_counts.values()) != int(target_samples):
                return False
        self.mark_status(matrix_id=matrix_id, status="complete")
        return True

    def load_complete(self, *, request_digest: str) -> dict[str, Any] | None:
        """读取 complete 矩阵及其所有行。"""

        run = self.find_complete_run(request_digest)
        if run is None or run["status"] != "complete":
            return None
        statement = (
            select(self.rows)
            .where(self.rows.c.matrix_id == str(run["matrix_id"]))
            .order_by(
                self.rows.c.action_key,
                self.rows.c.credibility,
            )
        )
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement).mappings()]
        for row in rows:
            row["action_json"] = json.loads(row["action_json"])
            row["scenario_counts"] = json.loads(row.pop("scenario_counts_json") or "{}")
        return {"run": run, "rows": rows}

    def find_complete_run(self, request_digest: str) -> dict[str, Any] | None:
        """按基础请求摘要查找最新完整运行，包含强制重算后缀记录。"""

        digest = str(request_digest)
        statement = (
            select(self.runs)
            .where(
                self.runs.c.status == "complete",
                or_(
                    self.runs.c.request_digest == digest,
                    self.runs.c.request_digest.like(f"{digest}:force:%"),
                ),
            )
            .order_by(self.runs.c.updated_at.desc())
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().first()
        return dict(row) if row is not None else None

    def load_row(
        self,
        *,
        matrix_id: str,
        action_key: str,
        credibility: float,
    ) -> dict[str, Any] | None:
        """按矩阵、规范动作键和可信度读取一个已聚合单元格。"""

        statement = select(self.rows).where(
            self.rows.c.matrix_id == str(matrix_id),
            self.rows.c.action_key == str(action_key),
            self.rows.c.credibility == float(credibility),
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().first()
        if row is None:
            return None
        result = dict(row)
        result["action_json"] = json.loads(result["action_json"])
        result["scenario_counts"] = json.loads(result.pop("scenario_counts_json") or "{}")
        return result

    def close(self) -> None:
        """释放数据库连接池。"""

        self.engine.dispose()
