# Resumable Object Gateway（本地磁盘模拟）

一套**不依赖外部服务、不需要真实凭据**的分块上传与对象落盘服务。
使用本地磁盘目录模拟对象存储，使并发写入、中断恢复与一致性校验在
服务层与 HTTP 层得到一致、可解释的结论。

在分块上传之上提供**多调用方（租户）治理**：每个调用方拥有独立的
容量/对象数量额度与单块尺寸上下限，额度在会话创建时占住、完成时转正、
中止/过期时归还，重启后占用与磁盘对账一致。不显式携带调用方标识的
老用法自动归属默认调用方，行为与此前完全一致。

- 语言/运行时：Python 3.14、FastAPI（ASGI）
- 包管理：[uv](https://docs.astral.sh/uv/)
- 持久化：纯本地文件（JSON 元数据 + 分块文件 + 原子 rename 发布）

## 快速开始

```bash
uv sync                       # 安装依赖
uv run pytest                 # 全部单测/集成测试（71 个用例）
uv run python scripts/local_verify.py   # 端到端本地验证脚本

# 启动真实 HTTP 服务
GATEWAY_DATA_DIR=.local-data uv run uvicorn main:app --port 8000
# 或
uv run python main.py
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/sessions` | 创建上传会话（可选 `caller_id` 或 `X-Caller-Id` 头） |
| `PUT` | `/sessions/{id}/parts/{n}` | 上传/重复提交第 n 块（原始字节为 body） |
| `POST` | `/sessions/{id}/complete` | 校验并合并落盘 |
| `POST` | `/sessions/{id}/abort` | 主动中止（幂等） |
| `GET` | `/sessions/{id}` | 查询状态与上传进度 |
| `GET` | `/objects/{key}` | 查询已落盘对象元信息 |
| `GET` | `/tenants/{caller}/usage` | 治理查询：占用/剩余额度与名下对象清单 |
| `POST` | `/internal/sweep` | 立即触发一次过期会话清理 |

分块摘要通过请求头 `X-Part-Checksum-Sha256`（64 位十六进制 SHA-256）提交；
整体摘要可在创建会话或完成时以 `overall_checksum_sha256` 声明。

### 典型流程

```bash
SID=$(curl -s -X POST localhost:8000/sessions -H 'Content-Type: application/json' \
  -d '{"object_key":"a/b.bin","total_parts":2,
       "overall_checksum_sha256":"<整体sha256>"}' | jq -r .session_id)

curl -X PUT localhost:8000/sessions/$SID/parts/1 --data-binary @p1 \
  -H "X-Part-Checksum-Sha256: <p1sha256>"
curl -X PUT localhost:8000/sessions/$SID/parts/2 --data-binary @p2 \
  -H "X-Part-Checksum-Sha256: <p2sha256>"

curl -X POST localhost:8000/sessions/$SID/complete -H 'Content-Type: application/json' -d '{}'
```

## 会话状态机

```
                 POST /sessions
                      │
                      ▼
              ┌───────────────┐
   PUT part   │  INITIATED    │   POST complete（全部校验通过）
 ┌───────────▶│  (可写)        │──────────────────────────────▶ COMPLETED（终态）
 │            └──────┬────────┘                                     对象已落盘
 │                   │ POST abort                          任何写操作 → 409
 │                   ▼
 │             ABORTED（终态，幂等）             超过 TTL 且仍为 INITIATED
 │             暂存分块被删除                    sweep 扫描/惰性判定
 │             任何写操作 → 409                         │
 │                                                      ▼
 └────────────────────────────────────────────── EXPIRED（终态）
        重启进程后从 meta.json 恢复                暂存分块被删除
                                                   任何写操作 → 410
```

要点：

- `COMPLETED` / `ABORTED` / `EXPIRED` 均为**终态**，不可再写入，
  且三者对应**不同错误码**，调用方可明确区分。
- `abort` 幂等：对已中止会话重复中止仍返回成功。
- 过期采用**滑动 TTL**：每次成功的分块活动都会顺延 `expires_at`。
  过期判定有两个入口：后台周期扫描（默认 300s）与写操作时的惰性判定。
  `POST /internal/sweep` 可在测试/运维中立即触发。
- sweep 只清理 `INITIATED` 会话；终态会话的 `meta.json` 保留为可解释记录。

## 分块与校验规则

1. **分块序号**：从 1 开始的整数，上界为创建时声明的 `total_parts`
   （未声明则为配置上限 `max_parts_per_upload`），越界 →
   `PART_NUMBER_OUT_OF_RANGE`（HTTP 416）。
2. **单块幂等**：重复提交同一分块时，摘要与大小与首次完全一致 →
   幂等成功，响应 `duplicated=true`；内容不同 → `PART_CHECKSUM_MISMATCH`
   （HTTP 422），首次内容不会被覆盖。
3. **单块摘要**：提供 `X-Part-Checksum-Sha256` 时服务端重算 SHA-256
   进行常量时间比对，不一致立即拒绝且不写盘。
4. **完成前置检查**（任一项失败均不会产生对象）：
   - 分块必须恰好为 `1..total_parts` 且无缺失：
     缺块 → `PART_MISSING`（409）；存在超出总数的块 →
     `PARTS_NOT_CONTIGUOUS`（409）。
   - 总字节数超单对象上限 → `OBJECT_SIZE_EXCEEDED`（413）。
   - 调用方容量/对象数量额度不足 → `QUOTA_EXCEEDED`（507）。
   - 合并后重算整体 SHA-256，与声明不符 →
     `OVERALL_CHECKSUM_MISMATCH`（422）。
5. **原子落盘**：分块先在会话目录合并为临时文件并 fsync；随后在
   staging 目录组装 `data` + `meta.json`，最后通过**整目录 rename**
   发布。对象对外要么完整可见、要么不存在，不存在残缺对象。

## 调用方（租户）治理

### 调用方标识与兼容范围

- 创建会话时可携带 `caller_id`（请求体字段或 `X-Caller-Id` 头）标识调用方；
  **不携带时自动归属默认调用方 `default`**——原来的老用法不需要任何改动。
- 默认调用方的额度就是原来的全局限制：`max_total_object_bytes` →
  容量额度、`max_object_count` → 对象数量额度、`min_part_bytes` →
  单块下限。也就是说，老部署的限额语义被原样保留为"默认调用方的额度"。
- 具名调用方通过 `Settings.caller_policies` 或环境变量配置
  （见"可配置项"）。未配置的调用方一律拒绝：`CALLER_NOT_FOUND`（404）。
- 每个调用方的额度**完全独立**：某一方占满只会收到自己的
  `QUOTA_EXCEEDED`，其他调用方与默认调用方不受影响。

### 额度的占用与释放语义

```
initiate                complete                 abort / sweep(过期)
   │ 占住 reserve          │ 转正 convert+finalize      │ 归还 release
   ▼                      ▼                          ▼
reserved += 估计值    reserved 清零               reserved 清零
(对象数恒为 1；      committed += 实际值          committed 不变
 字节按 part_size    （对象真实大小/1 个）
 ×total_parts 估计，
 未声明则为 0）
```

- **占住（reserve）**：会话创建成功即为该会话占住额度。占住即计入
  "已用"，占不住（越界）则会话创建失败并回滚，不产生任何残留。
  同一 session 重复占住幂等。
- **转正（convert/finalize）**：完成时先按对象**实际大小**校验差额
  （实际 − 占住估计），越界则 `QUOTA_EXCEEDED`，对象不落盘、占住保留、
  会话仍可修正后重试；校验通过并原子发布后，占住转为实际占用。
- **归还（release）**：主动中止、过期清理，以及一切未成功完成的会话，
  占住的额度**原样归还**；归还按 session_id 幂等，重复中止/重复清理/
  并发清理都不会多还或漏还。中止与清理同时删除暂存分块，
  不会留下半个对外可见的对象。
- 额度不足统一抛出 `QUOTA_EXCEEDED`（HTTP 507），与缺块、摘要不符、
  尺寸越界等其它失败原因明确区分。

### 并发与重启一致性保证

- **并发完成不越界**：每个调用方有一把额度锁，完成时的
  "校验 → 原子发布 → 落账" 全程在锁内串行；同一时刻再多完成请求
  打进来，任何一方的最终落盘都不会越过自己的额度
  （见 `TestConcurrentComplete`）。
- **同操作不重复扣**：同一 session 的完成被并发重复提交时，会话锁保证
  只有一个生效，额度只转正一次；其余得到 `SESSION_COMPLETED`。
- **完成/中止竞争不漏还**：会话锁串行化完成与中止，二者必居其一，
  额度要么转正要么归还，不会重复扣减也不会漏还。
- **重启对账（reconcile）**：账本持久化于
  `<data>/tenants/<sha256(caller)>/usage.json`（原子写）。进程重启后：
  - committed（实际占用）以磁盘上已发布对象的 `meta.json` 为权威**重算**，
    不会重复计、也不会丢；
  - reserved（占住）仅保留仍为 `INITIATED` 的会话对应的记录，
    其余（会话已终态或已消失）一律回收；
  - 未传完的会话可继续上传并完成，占用随之正确转正。

### 单块尺寸上下限

每个调用方策略可配 `min_part_bytes` / `max_part_bytes`。
写入分块时尺寸越界**立即以尺寸原因拒绝**（`PART_SIZE_OUT_OF_RANGE`，
HTTP 400），不写盘、不留半截分块；最后一块不受下限约束
（`total_parts` 已声明时）。`max_part_bytes=None` 表示不设上限。

### 治理查询

`GET /tenants/{caller}/usage` 返回：

```json
{
  "caller_id": "alice",
  "used_bytes": 5, "used_objects": 1,
  "reserved_bytes": 0, "reserved_objects": 0,
  "limit_bytes": 64, "limit_objects": 2,
  "remaining_bytes": 59, "remaining_objects": 1,
  "min_part_bytes": 2, "max_part_bytes": 32,
  "objects": [{"object_key": "a1", "size": 5, "checksum_sha256": "..."}]
}
```

- `used_*`：已落盘的实际占用；`reserved_*`：存活会话占住的额度；
  `remaining_*` = 上限 − used − reserved。
- 查询不存在的对象 → `OBJECT_NOT_FOUND`（404）；
  查询不存在的会话 → `SESSION_NOT_FOUND`（404）；
  查询未配置的调用方 → `CALLER_NOT_FOUND`（404）。
  三者错误码不同，可明确区分。

### 错误码与 HTTP 状态

| error_code | HTTP | 触发条件 |
| --- | --- | --- |
| `SESSION_NOT_FOUND` | 404 | 会话不存在 |
| `SESSION_NOT_INITIATED` | 400 | 创建参数非法（空 key、块数越界、摘要格式错） |
| `SESSION_COMPLETED` | 409 | 对已完成会话写/中止 |
| `SESSION_ABORTED` | 409 | 对已中止会话写入 |
| `SESSION_EXPIRED` | 410 | 对已过期会话写入 |
| `PART_NUMBER_OUT_OF_RANGE` | 416 | 块序号越界 / total_parts 越界 |
| `PART_CHECKSUM_MISMATCH` | 422 | 单块摘要不符 / 同块重复提交内容不同 |
| `PART_MISSING` | 409 | 完成时存在缺失分块 |
| `PARTS_NOT_CONTIGUOUS` | 409 | 上传了超出 total_parts 的块 |
| `OVERALL_CHECKSUM_MISMATCH` | 422 | 整体摘要不符 / 摘要格式非法 |
| `OBJECT_SIZE_EXCEEDED` | 413 | 单对象字节超限 |
| `QUOTA_EXCEEDED` | 507 | 调用方容量或对象数量额度不足（创建占住或完成转正时） |
| `OBJECT_ALREADY_EXISTS` | 409 | 对象 key 已存在（创建即拒绝） |
| `OBJECT_NOT_FOUND` | 404 | 对象不存在（与会话不存在可区分） |
| `PART_SIZE_OUT_OF_RANGE` | 400 | 单块尺寸超出调用方上下限 |
| `CALLER_NOT_FOUND` | 404 | 调用方未配置 |

错误响应体统一为：

```json
{"error_code": "PART_CHECKSUM_MISMATCH",
 "message": "part 1 checksum mismatch: expected=... actual=...",
 "session_id": "...."}
```

## 并发与恢复语义

- **同会话串行化**：进程内为每个 `session_id` 维护一把可重入锁，
  同一会话的分块写入、完成、中止、过期清理互斥；不同会话互不阻塞。
- **同调用方额度串行化**：每个 `caller_id` 一把额度锁，完成的
  "校验→发布→落账"与占住/归还均在锁内（锁顺序：会话锁 → 额度锁），
  并发完成不会击穿任何一方的额度。
- **文件原子性**：分块与元数据写入均为 `临时文件 + fsync + rename`，
  并发写同一块时“全量覆盖”，配合锁与摘要比对保证不会产生半截文件。
- **崩溃恢复**：会话状态存于 `<data>/sessions/<id>/meta.json`，
  分块存于同目录。进程重启后新实例直接扫描磁盘恢复，未完成会话可
  继续上传；已完成/已中止会话终态保持（见 `TestRestart`）。
- **滑动过期 + 双检清理**：sweep 扫描到候选会话后在会话锁内重新读取
  并二次检查状态，避免清理与完成操作竞争。

## 磁盘布局

```
<data_dir>/
├── sessions/<session_id>/
│   ├── meta.json            # 会话状态（原子写，含 caller_id）
│   ├── part_1, part_2, ...  # 暂存分块（完成/中止/过期后删除）
│   └── .merge-*             # 合并临时文件（失败即删除）
├── objects/
│   ├── <sha256(object_key)>/
│   │   ├── data             # 对象内容（整目录 rename 原子发布）
│   │   └── meta.json        # key/size/checksum/caller_id/created_at
│   └── .staging/<tmp>/      # 发布前暂存
└── tenants/<sha256(caller_id)>/
    └── usage.json           # 调用方额度账本（占住/占用，原子写）
```

## 可配置项

均通过环境变量覆盖（前缀 `GATEWAY_`）：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `GATEWAY_DATA_DIR` | `.local-data` | 本地数据根目录 |
| `GATEWAY_SESSION_TTL_SECONDS` | `3600` | 会话无活动过期时间（滑动 TTL） |
| `GATEWAY_SWEEP_INTERVAL_SECONDS` | `300` | 后台过期扫描间隔 |
| `GATEWAY_MAX_PARTS_PER_UPLOAD` | `1000` | 单会话最大分块数 |
| `GATEWAY_MAX_OBJECT_BYTES` | `67108864` | 单对象最大字节数 |
| `GATEWAY_MAX_TOTAL_OBJECT_BYTES` | `1073741824` | 默认调用方容量额度（原全局容量配额） |
| `GATEWAY_MAX_OBJECT_COUNT` | `10000` | 默认调用方对象数量额度（原全局数量配额） |
| `GATEWAY_MIN_PART_BYTES` | `1` | 默认调用方单块最小字节数 |
| `GATEWAY_CALLERS` | 空 | 具名调用方名单，逗号分隔（如 `alice,bob`） |
| `GATEWAY_CALLER_<NAME>_MAX_BYTES` | 同默认调用方 | 该调用方容量额度 |
| `GATEWAY_CALLER_<NAME>_MAX_OBJECTS` | 同默认调用方 | 该调用方对象数量额度 |
| `GATEWAY_CALLER_<NAME>_MIN_PART_BYTES` | 同默认调用方 | 该调用方单块最小字节数 |
| `GATEWAY_CALLER_<NAME>_MAX_PART_BYTES` | 不限 | 该调用方单块最大字节数 |

`<NAME>` 为调用方名大写、非字母数字字符转下划线（如 `team-a` →
`GATEWAY_CALLER_TEAM_A_MAX_BYTES`）。

## 本地验证方法

```bash
# 1) 全量测试（并发分块、重复分块、重启续传、摘要不匹配、过期清理、
#    调用方额度/隔离/对账/尺寸上下限等；71 个用例逐条列出结果）
uv run pytest -v

# 2) 单测日志：会话标识 / 调用方 / 分块序号 / 判定依据
uv run pytest tests/test_service.py::TestUploadPart::test_part_checksum_mismatch_is_classified \
  -o log_cli=true -o log_cli_level=INFO
# 日志示例：
# session=063b... part=1 action=upload verdict=REJECTED
#   reason=PART_CHECKSUM_MISMATCH expected=fff... actual=ba78... size=3

# 3) 治理能力专项用例（并发完成额度边界、中止/过期归还、重启对账、
#    调用方隔离、用量查询、两类"查不到"、单块尺寸上下限）
uv run pytest tests/test_tenancy.py -v -o log_cli=true -o log_cli_level=INFO
# 日志示例：
# caller=alice session=... action=reserve verdict=RESERVED bytes=300 objects=1
# caller=alice session=... action=convert verdict=REJECTED reason=QUOTA_EXCEEDED ...
# caller=alice action=reconcile verdict=OK committed_bytes=100 ...

# 4) 端到端脚本（进程内 ASGI 调用，无需端口；含治理演示）
uv run python scripts/local_verify.py

# 5) 真实 HTTP + curl
GATEWAY_DATA_DIR=/tmp/rogw-demo \
GATEWAY_CALLERS=alice \
GATEWAY_CALLER_ALICE_MAX_BYTES=1048576 \
GATEWAY_CALLER_ALICE_MAX_OBJECTS=10 \
  uv run uvicorn main:app --port 8000

curl -s -X POST localhost:8000/sessions -H 'Content-Type: application/json' \
  -d '{"object_key":"a.bin","total_parts":1,"caller_id":"alice"}'
curl -s localhost:8000/tenants/alice/usage | jq
```

## 代码结构

```
gateway/
├── config.py    # 配置（环境变量 + 调用方策略 CallerPolicy）
├── errors.py    # ErrorCode 枚举与 GatewayError
├── models.py    # 会话/分块模型与 API schema（含用量查询视图）
├── hashing.py   # SHA-256 与常量时间摘要比对
├── storage.py   # 本地磁盘对象存储（分块暂存/原子合并发布/对象归属）
├── sessions.py  # 会话 JSON 持久化、会话级锁、过期扫描
├── tenancy.py   # 调用方额度账本（占住/转正/归还/持久化/重启对账）
├── service.py   # 业务规则编排（状态机/幂等/校验/额度）
└── app.py       # FastAPI 装配、错误归类、后台 sweep
tests/           # 服务层单测、HTTP 集成测试与调用方治理单测
scripts/local_verify.py  # 端到端本地验证（含治理能力演示）
```
