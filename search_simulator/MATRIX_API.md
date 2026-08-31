# 自定义决策矩阵 Python API

本文说明其他 Python 模块如何构造观察者安全请求、计算精确信念 Cheap-talk 决策矩阵，以及按动作读取已经完成的矩阵单元。接口只接收结构化状态，不调用语言模型，也不接收自然语言发言文本。

当前接口服务于固定七人研究板子：两名狼人、两名村民、一名预言家、一名女巫和一名守卫。它计算第一天白天某名存活玩家发言前的结构化收益矩阵。

## 1. 公共入口

外部模块只从 `search_simulator` 包根导入以下名称：

```python
from search_simulator import (
    DecisionMatrixInputError,
    MatrixInterrupted,
    build_custom_decision_matrix_request,
    calculate_custom_decision_matrix,
    load_custom_decision_matrix_cell,
)
```

不得从 `_decision_matrix.py`、`_role_view.py` 或 `_speech_action.py` 等下划线模块导入内部函数。下划线模块可以重构，包根导出的名称才是外部调用契约。

三个主要函数的职责为：

| 函数 | 用途 | 是否启动计算 |
|---|---|---|
| `build_custom_decision_matrix_request(payload)` | 校验输入并生成规范请求对象，可读取请求摘要和候选动作 | 否 |
| `calculate_custom_decision_matrix(payload, database_path, ...)` | 从缓存、可恢复检查点或新运行得到完整矩阵，返回 JSON-safe 字典 | 是，缓存命中时除外 |
| `load_custom_decision_matrix_cell(payload, database_path, action_key, credibility)` | 按同一规范请求、动作键和可信度读取一个已完成单元格 | 否 |

## 2. 必须输入什么

固定板子已经确定角色数量，但没有确定本次行动者依法知道的私有信息。调用方只需输入不能由规则默认值推导的事实：

| 使用情形 | 必须输入 | 不需要输入 |
|---|---|---|
| 所有角色 | `role_view.actor_id`：当前行动者的零基席位 `0..6` | 完整七人真实角色站位 |
| 所有角色 | `role_view.actor_role`：当前行动者自知角色 | 固定角色多重集合、规则、首日状态、发言顺序和样本参数 |
| 行动者是狼人 | `role_view.known_camps`：自己和另一名狼人席位，两个值均为 `wolf` | 两名狼的具体子角色；当前固定板子只有普通狼人 |
| 行动者是预言家 | `role_view.seer_checks`：昨夜的一条查验，包含目标席位和是否为狼人 | 被查验好人的具体角色 |
| 已发生公开发言或公开状态不同 | 只覆盖 `decision_state` 中确实不同的字段 | 为默认值重复填写占位字段 |

因此，村民、女巫和守卫的最小输入只有两个字段：

```json
{
  "role_view": {
    "actor_id": 3,
    "actor_role": "女巫"
  }
}
```

狼人需要额外提供自己依法知道的狼队站位：

```json
{
  "role_view": {
    "actor_id": 1,
    "actor_role": "狼人",
    "known_camps": [[1, "wolf"], [5, "wolf"]]
  }
}
```

预言家需要额外提供固定首日之前已经取得的查验；第三项 `true` 表示目标是狼人，`false` 表示目标属于好人阵营：

```json
{
  "role_view": {
    "actor_id": 2,
    "actor_role": "预言家",
    "seer_checks": [[2, 5, true]]
  }
}
```

不要输入类似 `seat_roles=["村民", "狼人", ...]` 的完整角色站位。固定板子只确定角色多重集合；算法根据行动者角色、狼队知识、预言家查验和公开证据构造 posterior。把研究者掌握的完整站位传入请求会破坏角色视角信息隔离，因此未知顶层字段和站位字段会被拒绝。

### 2.1 计算前必须同步到模拟器的信息

默认值只是“固定板子、首日、全员存活、没有已记录公开证据、技能资源尚未消耗”的便捷场景。接入真实对局时，外部模块必须在当前玩家发言前取得同一时刻的原子快照，并把所有已经不同于默认值的事实同步进来。

| 信息来源 | 需要同步的事实 | payload 字段 | 何时必须显式提供 | 在模拟器中的用途 |
|---|---|---|---|---|
| 当前行动请求 | 发言者席位 | `role_view.actor_id` | 始终 | 确定矩阵收益视角、候选目标和当前轮次 |
| 当前行动者私有状态 | 发言者自身角色 | `role_view.actor_role` | 始终 | 确定阵营效用、合法声明和角色视角 |
| 当前行动者私有状态 | 狼队友席位 | `role_view.known_camps` | 行动者是狼人时 | 精确条件化到行动者依法知道的狼队阵营站位 |
| 当前行动者私有状态 | 昨夜查验目标与阵营结果 | `role_view.seer_checks` | 行动者是预言家时 | 形成预言家私有硬约束，并限制真实预言家的合法查验声明 |
| 公开对局状态 | 每个席位是否存活 | `decision_state.alive` | 与全员存活默认值不同时 | 决定发言、投票、夜间技能的合法行动者和目标 |
| 公开对局状态 | 当前发言顺序、位置和行动者 | `speech_order`、`speech_index`、`actor_id` | 顺序不是默认席位顺序或当前席位无法由默认值推出时 | 从正确的微阶段继续模拟，避免重复或跳过发言 |
| 公开对局状态 | 已发生的结构化发言证据 | `public_events` | 需要让已发生声明影响当前信念时 | 在三个可信度档位下更新隐藏站位权重；不接收原始发言文本 |
| 公开对局状态 | 已公开身份声明 | `public_role_claims` | 无法由 `public_events` 推导或有规则公开身份时 | 保持后续公开状态一致；普通自称不会自动变成硬知识 |
| 规则引擎受保护状态 | 上一次守卫目标 | `last_guard_target` | 守卫已经执行过守护且该规则记忆影响下一夜时 | 只用于检查下一次守护是否合法，不作为其他玩家的信念证据 |
| 规则引擎受保护状态 | 女巫解药、毒药是否仍可用 | `witch_save_available`、`witch_poison_available` | 任一药物已经消耗时 | 决定未来夜间合法动作；不把药物状态升级成无关玩家的角色知识 |
| 规则进度 | 阶段、日夜计数和终局状态 | `phase`、`day_count`、`night_count`、`winner` | 与当前支持的默认首日发言状态不同时 | 防止从错误阶段、错误轮次或已终局状态启动模拟；当前 API 会拒绝超出支持边界的值 |
| 实验设置 | 每格样本数与基准种子 | `samples_per_cell`、`base_seed` | 只在不使用 `100` 和 `7` 时 | 确定估计精度、可复现随机流和缓存请求身份 |

同步信息有两条隔离通道：

1. `decision_state` 是规则继续运行需要的状态。其中公开事件可以影响当前行动者的信念；守卫目标和药物可用性等受保护规则状态只能用于合法性与结算。
2. `role_view` 是当前行动者真正可用于决策的私有硬知识。自身角色、狼队信息和自身查验只能从该玩家的服务端可见性视图生成。

以下信息即使服务端掌握，也不得同步到自定义矩阵请求：

- 按席位排列的完整真实角色表；
- 当前行动者依法不知道的其他玩家具体角色或阵营；
- 其他玩家未公开的查验、守护、用药意图或推理过程；
- 语言模型 prompt、原始自然语言发言、模型输出或模型生成的行动概率；
- 研究者主观标注的“疑似狼人”作为硬知识。

这些未知量由精确信念层枚举或由前向终局模拟的显式策略与随机机制处理。调用方不得先按上帝视角挑选一个隐藏世界再请求矩阵，否则结果不再代表该行动者的信息集。

### 2.2 推荐同步时机与一致性检查

- 在 LLM 发言请求创建之前，从同一个已提交的游戏状态生成矩阵输入；本 API 本身仍不调用 LLM。
- `decision_state.actor_id`、`role_view.actor_id` 与 `speech_order[speech_index]` 必须是同一席位。
- `public_events` 只能包含该快照之前已经公开的事件，不得包含当前发言或未来行动。
- 生成快照后若出现死亡、公开声明、药物消耗、守卫目标变化或轮次推进，应构造新 payload；不要复用旧矩阵。
- 对同一原子快照重复调用会使用相同请求摘要和缓存结果；执行进程数、批次大小与回调不改变请求身份。
- HTTP 或消息队列接入时，由服务端游戏引擎构造这两个通道；不要让客户端直接声明私有知识。

## 3. 输入总览

`payload` 必须是可映射为 JSON object 的字典，顶层结构如下：

```json
{
  "role_view": {
    "actor_id": 0,
    "actor_role": "村民"
  }
}
```

这就是普通好人角色在默认首日、全员存活、尚无公开发言证据时的最小合法输入。固定板子、规则、发言顺序、药物状态、样本数和基准种子均由接口补齐。调用方只有在状态确实不同或需要可复现实验变体时才覆盖默认值。

允许的顶层字段只有：

| 字段 | 类型 | 必填 | 默认值 | 是否进入请求身份 |
|---|---|---:|---|---:|
| `config` | object | 否 | 固定七人板子与固定规则 | 是 |
| `decision_state` | object | 否 | 当前行动者的首日默认发言状态 | 是 |
| `role_view` | object | 是 | — | 是 |
| `samples_per_cell` | integer | 否 | `100`；必须大于零 | 是 |
| `base_seed` | integer | 否 | `7`；必须不小于零 | 是 |

未知字段会被拒绝，避免调用方误以为某个参数已经生效。修改样本数或基准种子会产生新的请求摘要，不会静默复用旧矩阵。

## 4. `config` 参数

```json
{
  "roles": ["狼人", "狼人", "村民", "村民", "预言家", "女巫", "守卫"],
  "max_days": 8,
  "rules_spec": "seven-player-microphase-rules"
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---:|---|
| `roles` | array[string] | 否 | 默认固定角色多重集合；覆盖时仍必须与该集合完全相同 |
| `max_days` | integer | 否 | 默认 `8`，必须大于零 |
| `rules_spec` | string | 否 | 默认且只允许 `seven-player-microphase-rules` |

`roles` 的数组顺序没有站位语义。接口会对角色多重集合稳定排序；调用方不得借此传入或编码完整隐藏站位。

整个 `config` 可以省略。省略时等价于上面的完整对象；提供 `config` 时，其中三个字段也都可以分别省略并使用表中默认值。

## 5. `decision_state` 参数

`decision_state` 包含公开状态、当前发言顺序及规则继续运行所需的受保护资源状态，但不包含任何玩家的隐藏角色：

```json
{
  "alive": [true, true, true, true, true, true, true],
  "phase": "day_speech",
  "day_count": 0,
  "night_count": 1,
  "speech_order": [0, 1, 2, 3, 4, 5, 6],
  "speech_index": 2,
  "actor_id": 2,
  "public_role_claims": [],
  "public_events": [
    ["speech", 0, "silence", null, null, null, null],
    ["speech", 1, "accusation", null, null, null, null]
  ],
  "last_guard_target": null,
  "witch_save_available": true,
  "witch_poison_available": true,
  "winner": null
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---:|---|
| `alive` | array[boolean] | 否 | 默认全员存活；长度必须为 `7`；当前行动者必须存活 |
| `phase` | string | 否 | 默认且只允许 `day_speech` |
| `day_count` | integer | 否 | 默认且只允许 `0`，表示第一天 |
| `night_count` | integer | 否 | 默认且只允许 `1` |
| `speech_order` | array[integer] | 否 | 默认 `[0,1,2,3,4,5,6]`；必须无重复并恰好覆盖全部存活席位 |
| `speech_index` | integer | 否 | 默认由当前行动者在 `speech_order` 中的位置推导 |
| `actor_id` | integer | 否 | 默认取 `role_view.actor_id`；零基席位，范围为 `0..6` |
| `public_role_claims` | array[[seat, role]] | 否 | 默认从 `public_events` 中的预言家声明推导 |
| `public_events` | array[array] | 否 | 默认空；只接受已经发生的结构化发言事件，不接受自然语言文本 |
| `last_guard_target` | integer/null | 否 | 上夜守卫目标；存在时必须为合法席位 |
| `witch_save_available` | boolean | 否 | 默认 `true` |
| `witch_poison_available` | boolean | 否 | 默认 `true` |
| `winner` | null | 否 | 发言前请求必须尚未终局 |

一条公开发言事件固定使用七项结构：

```text
["speech", speaker_id, family, claim_role, claim_target, claim_result, tactic]
```

`family` 必须是 `baseline`、`accusation`、`support`、`vote_intent`、`seer_claim` 或 `silence`。身份声明使用 `claim_role="预言家"`；完整查验声明的 `claim_result` 只能是 `good` 或 `wolf`。事件只能来自此前已经经过的发言位置，同一发言者至多一条，并按 `speech_order` 保持顺序。

整个 `decision_state` 可以省略。省略时接口使用全员存活、按席位顺序发言、当前行动者位于其自然顺序位置、无公开证据、守卫上夜目标未知、女巫双药可用且尚未终局的状态。提供 `public_events` 时只需列出希望纳入信念更新的已发生结构事件；事件必须来自当前行动者之前的席位并保持发言顺序，不要求为没有记录证据的发言补占位事件。

## 6. `role_view` 参数

`role_view` 是当前行动者依法知道的硬知识。它是调用边界中最重要的信息隔离对象：

```json
{
  "actor_id": 2,
  "actor_role": "预言家",
  "known_roles": [[2, "预言家"]],
  "known_camps": [[2, "good"]],
  "seer_checks": [[2, 5, true]],
  "view_spec": "role-view-hard-knowledge"
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---:|---|
| `actor_id` | integer | 是 | 必须与 `decision_state.actor_id` 一致 |
| `actor_role` | string | 是 | 当前行动者真实自知角色 |
| `known_roles` | array[[seat, role]] | 否 | 默认 `[[actor_id, actor_role]]`；当前固定板子中只能包含行动者自己 |
| `known_camps` | array[[seat, camp]] | 视角色而定 | 好人默认自身为 `good`；狼人必须显式给出自己和狼队友的席位 |
| `seer_checks` | array[[observer, target, is_wolf]] | 视角色而定 | 固定首日的预言家必须提供恰好一条；其他角色不得提供 |
| `view_spec` | string | 否 | 默认且只允许 `role-view-hard-knowledge` |

角色视角的具体规则：

- 村民、女巫和守卫只能提供自己的角色与自身好人阵营；
- 预言家必须通过 `seer_checks` 提供昨夜的一条私有查验，只能表示目标为好人或狼人，不能提供目标的具体好人角色；
- 狼人的 `known_camps` 必须包含自己和依法知道的全部狼队席位，值均为 `wolf`；
- 公开声明、死亡、怀疑、posterior 排名和研究者知道的真实站位不得写入硬知识；
- 完整隐藏站位、其他玩家的真实角色列表以及自然语言分析均不属于 API 输入。

`actor_id` 和 `actor_role` 是唯一始终必填的业务字段。`known_roles`、好人自己的 `known_camps`、非预言家的空 `seer_checks` 和 `view_spec` 都由接口补齐。狼人队友席位和预言家昨夜的实际查验无法从固定板子推出，必须显式提交；接口不会猜测私有信息。

该 Python API 是进程内可信接口。若未来经 HTTP 暴露，`role_view` 必须由服务端对局可见性层构造，不能直接信任浏览器或远程客户端提交的私有知识。

## 7. 计算执行参数

执行参数作为 `calculate_custom_decision_matrix()` 的关键字参数传入，不放进 `payload`：

| 参数 | 类型 | 默认值 | 说明 | 是否进入请求身份 |
|---|---|---|---|---:|
| `database_path` | `str | Path` | `search_simulator_cache.sqlite3` | 与完整分支树结果共用的 SQLite 文件 | 否 |
| `workers` | integer | `2` | 隔离计算子进程数，最小为 `1` | 否 |
| `batch_size` | integer | `10` | 一个任务覆盖的连续样本索引数 | 否 |
| `force_recompute` | boolean | `false` | 创建新运行，不覆盖历史完整结果 | 否 |
| `memory_reserve_gib` | number | `8.0` | 物理内存绝对安全保留量 | 否 |
| `memory_reserve_ratio` | number | `0.15` | 物理内存比例安全保留量，范围 `[0,1]` | 否 |
| `progress_callback` | callable/null | `null` | 批次事务成功提交后的只读进度回调 | 否 |
| `stop_event` | event/null | `null` | 具有 `is_set()` 的停止事件；只在批次边界生效 | 否 |

`workers` 和 `batch_size` 只影响执行方式，不改变逐值结果。生产默认使用两个计算子进程；不要从多个 Web worker 无限制并发调用同一个 SQLite 文件。

## 8. 完整调用示例

```python
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from search_simulator import calculate_custom_decision_matrix


DATABASE_PATH = Path(r"D:\Codes\AIwerewolf\search_simulator_cache.sqlite3")


def _payload() -> dict:
    return {
        "role_view": {
            "actor_id": 0,
            "actor_role": "村民",
        },
    }


def main() -> None:
    result = calculate_custom_decision_matrix(
        _payload(),
        DATABASE_PATH,
        workers=2,
        batch_size=10,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
```

Windows 使用多进程时必须保留 `if __name__ == "__main__"` 入口保护。若调用发生在 FastAPI、任务队列或其他常驻服务中，应把此同步 CPU 任务放入单独的有界作业协调器，不要直接阻塞异步事件循环。

## 9. 返回内容

`calculate_custom_decision_matrix()` 返回 JSON-safe 字典：

```json
{
  "matrix_id": "...",
  "request_digest": "...",
  "status": "complete",
  "request": {},
  "action_rows": [
    {
      "action_key": "...",
      "action": {
        "family": "accusation",
        "claim_role": null,
        "claim_target": null,
        "claim_result": null,
        "target_id": 3,
        "intensity": 0.7,
        "tactic": null,
        "structure_spec": "structured-cheap-talk-actions"
      },
      "by_credibility": {
        "0.0": {
          "mean": 0.1,
          "standard_error": 0.09,
          "baseline_delta": 0.0,
          "baseline_delta_standard_error": 0.0,
          "sample_count": 100,
          "scenario_counts": {}
        },
        "0.5": {},
        "0.8": {}
      }
    }
  ],
  "notice": "结果只代表规则与既定行为模型下的模拟收益，不代表真实玩家概率。"
}
```

| 字段 | 含义 |
|---|---|
| `matrix_id` | 本次持久化运行标识 |
| `request_digest` | 规范输入摘要；相同观察者安全请求得到相同基础摘要 |
| `status` | 完整返回固定为 `complete` |
| `request` | 已规范化的观察者安全请求，不含完整隐藏站位 |
| `action_rows` | 每个二级具体动作一行 |
| `action_key` | 与展示文本无关的稳定动作键，可用于单元格查询 |
| `action` | 结构化动作字段，不含自然语言文本 |
| `by_credibility` | `0.0 / 0.5 / 0.8` 三档结果，不能合并为单一分数 |
| `mean` | 行动者阵营终局效用样本均值 |
| `standard_error` | Monte Carlo 标准误 |
| `baseline_delta` | 使用共同随机数得到的相对 baseline 配对差 |
| `baseline_delta_standard_error` | 配对差的 Monte Carlo 标准误 |
| `sample_count` | 本单元已提交样本数 |
| `scenario_counts` | 六类互斥反应情景计数 |
| `notice` | 模型条件解释边界 |

返回值不包含抽样轨迹、抽样世界完整角色站位、语言模型选择、prompt 或自然语言发言。

## 10. 进度回调

每次批次事务提交后，`progress_callback` 接收一个新字典。调用方不得修改该字典来影响计算：

```python
def on_progress(event: dict) -> None:
    if event["status"] == "running":
        print(event["committed_batches"], event["total_batches"])
```

事件至少包含：

| 字段 | 含义 |
|---|---|
| `kind` | 固定为 `matrix_progress` |
| `status` | `running` 或 `complete` |
| `matrix_id` | 当前运行标识 |
| `committed_batches` | 已成功写入数据库的批次数 |
| `total_batches` | 规范请求总批次数 |
| `cache_hit` | 是否直接复用完整结果 |

运行事件还可能包含 `credibility`、`sample_start`、`sample_end` 和 `resumed`。这些字段只用于观察，不进入请求摘要。

## 11. 单元格查询

计算完成后，可以使用原始 `payload`、返回的 `action_key` 和一个可信度档位读取单元格：

```python
from search_simulator import load_custom_decision_matrix_cell

cell = load_custom_decision_matrix_cell(
    _payload(),
    DATABASE_PATH,
    action_key=result["action_rows"][0]["action_key"],
    credibility=0.5,
)
```

命中时返回：

```json
{
  "action_key": "...",
  "action": {},
  "credibility": 0.5,
  "mean": 0.1,
  "standard_error": 0.09,
  "baseline_delta": 0.0,
  "baseline_delta_standard_error": 0.0,
  "sample_count": 100,
  "scenario_counts": {}
}
```

完整矩阵尚未完成或不存在时返回 `None`。未知动作键、非法可信度或输入不一致会抛出 `DecisionMatrixInputError`，不会启动隐式计算。

## 12. 缓存、中断与失败

- 相同 `payload` 再次计算时，优先返回最新完整矩阵；
- `force_recompute=True` 创建新运行并保留历史结果；
- 内存安全保护或调用方设置 `stop_event` 时抛出 `MatrixInterrupted`，已提交批次保留；
- 使用相同 `payload` 再次调用会从缺失批次按原样本索引和种子恢复；
- 输入校验失败抛出 `DecisionMatrixInputError`，不会创建运行；
- 计算进程、持久化或完整性错误保持原始异常，不返回部分动作排名；
- 调用方不应通过修改数据库或更换失败样本种子来“补齐”结果。

推荐异常处理：

```python
from search_simulator import DecisionMatrixInputError, MatrixInterrupted

try:
    result = calculate_custom_decision_matrix(_payload(), DATABASE_PATH)
except DecisionMatrixInputError as exc:
    # 输入错误：修正 payload 后重新提交
    raise
except MatrixInterrupted:
    # 可恢复中断：稍后以完全相同的 payload 再次调用
    raise
```

不要捕获所有异常并返回空矩阵；`failed` 必须对调用方可见。

## 13. 信息隔离检查清单

调用前至少确认：

- `config.roles` 是无序角色多重集合，不是按席位排列的真实身份；
- `decision_state` 只含公开信息和顺序字段；
- `role_view.known_roles` 没有写入其他玩家未公开的真实角色；
- 好人没有通过 `known_camps` 直接获得未知玩家阵营；
- 预言家查验只记录 `is_wolf`，没有记录好人目标的具体角色；
- 狼人只携带规则允许的狼队阵营知识；
- `public_events` 是结构化事件，不含 prompt、发言原文或语言模型分析；
- 不在外部日志中打印上帝视角站位后再把它与矩阵请求关联。

## 14. 当前边界

- 当前只支持固定七人研究板子和第一天白天发言前状态；
- 当前是同步 Python API，不是 HTTP API；
- 当前调用返回完整矩阵或显式异常，不返回部分完成排名；
- 当前不接受调用方自定义候选集合，候选由模块根据状态和角色视角完整生成；
- 当前不接受自定义可信度档位、功利等级策略、策略温度、随机数派生方案或规则规范；
- 若未来需要 HTTP 服务，应在此 Python API 外增加鉴权、服务端角色视角构造、单一有界作业队列和状态查询端点，不把浏览器请求直接传给计算器。
