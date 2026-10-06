# Resumable Object Gateway（本地磁盘模拟）

一套**不依赖外部服务、不需要真实凭据**的分块上传与对象落盘服务。
使用本地磁盘目录模拟对象存储，使并发写入、中断恢复与一致性校验在
服务层与 HTTP 层得到一致、可解释的结论。

- 语言/运行时：Python 3.14、FastAPI（ASGI）
- 包管理：[uv](https://docs.astral.sh/uv/)
- 持久化：纯本地文件（JSON 元数据 + 分块文件 + 原子 rename 发布）

## 快速开始

```bash
uv sync                       # 安装依赖
uv run pytest                 # 全部单测/集成测试（46 个用例）
uv run python scripts/local_verify.py   # 端到端本地验证脚本

# 启动真实 HTTP 服务
GATEWAY_DATA_DIR=.local-data uv run uvicorn main:app --port 8000
# 或
uv run python main.py
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/sessions` | 创建上传会话（可用 `X-Tenant-Id` 头指定调用方） |
| `PUT` | `/sessions/{id}/parts/{n}` | 上传/重复提交第 n 块（原始字节为 body） |
| `POST` | `/sessions/{id}/complete` | 校验并合并落盘 |
| `POST` | `/sessions/{id}/abort` | 主动中止（幂等） |
| `GET` | `/sessions/{id}` | 查询状态与上传进度 |
| `GET` | `/objects/{key}` | 查询已落盘对象元信息 |
| `GET` | `/tenants/{id}/usage` | 治理查询：调用方配额、占用、剩余与名下对象 |
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
   - 总容量/对象数配额（预检）超限 → `QUOTA_EXCEEDED`（507）。
   - 合并后重算整体 SHA-256，与声明不符 →
     `OVERALL_CHECKSUM_MISMATCH`（422）。
5. **原子落盘**：分块先在会话目录合并为临时文件并 fsync；随后在
   staging 目录组装 `data` + `meta.json`，最后通过**整目录 rename**
   发布。对象对外要么完整可见、要么不存在，不存在残缺对象。

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
| `QUOTA_EXCEEDED` | 507 | 默认调用方（老用法）总容量或对象数配额超限 |
| `OBJECT_ALREADY_EXISTS` | 409 | 对象 key 已存在（创建即拒绝） |
| `OBJECT_NOT_FOUND` | 404 | 查询的对象不存在（与会话不存在可区分） |
| `TENANT_NOT_FOUND` | 404 | 指定了未配置的调用方 |
| `TENANT_QUOTA_EXCEEDED` | 507 | 命名调用方自身额度不足（不影响其他调用方） |
| `PART_SIZE_OUT_OF_RANGE` | 413 | 单块尺寸越出该调用方配置的上下限 |

错误响应体统一为：

```json
{"error_code": "PART_CHECKSUM_MISMATCH",
 "message": "part 1 checksum mismatch: expected=... actual=...",
 "session_id": "...."}
```

## 并发与恢复语义

- **同会话串行化**：进程内为每个 `session_id` 维护一把可重入锁，
  同一会话的分块写入、完成、中止、过期清理互斥；不同会话互不阻塞。
- **文件原子性**：分块与元数据写入均为 `临时文件 + fsync + rename`，
  并发写同一块时“全量覆盖”，配合锁与摘要比对保证不会产生半截文件。
- **崩溃恢复**：会话状态存于 `<data>/sessions/<id>/meta.json`，
  分块存于同目录。进程重启后新实例直接扫描磁盘恢复，未完成会话可
  继续上传；已完成/已中止会话终态保持（见 `TestRestart`）。
- **滑动过期 + 双检清理**：sweep 扫描到候选会话后在会话锁内重新读取
  并二次检查状态，避免清理与完成操作竞争。

## 调用方治理（多调用方配额）

服务支持按**调用方（tenant）**隔离的额度治理。创建会话时通过请求头
`X-Tenant-Id: <name>` 指定调用方；不带该头的请求走**默认调用方**
（见“兼容范围”）。

### 配置

命名调用方通过环境变量 `GATEWAY_TENANTS`（JSON 对象）配置：

```bash
export GATEWAY_TENANTS='{
  "team-a": {"max_bytes": 104857600, "max_objects": 100,
             "min_part_bytes": 4096, "max_part_bytes": 8388608},
  "team-b": {"max_bytes": 52428800,  "max_objects": 50}
}'
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `max_bytes` | 是 | 容量额度（字节，预留 + 已落盘合计不得越过） |
| `max_objects` | 是 | 对象数量额度（预留 + 已落盘合计不得越过） |
| `min_part_bytes` | 否（默认 1） | 单块最小字节数（最后一块不受此限） |
| `max_part_bytes` | 否（默认不限） | 单块最大字节数 |

未配置的调用方一律在创建会话时被拒（`TENANT_NOT_FOUND`，404）。

### 占用与释放语义

- **占住**：命名调用方的额度在会话期间被占住——创建会话占 1 个对象位；
  每写入一个分块，按实际字节数占容量（写入前原子“检查 + 占住”，
  占不住即以 `TENANT_QUOTA_EXCEEDED` 拒绝，不会先收数据再报错）。
- **结转**：会话完成时，该会话的预留**原样**转为实际占用
  （对象数 +1、容量 += 对象实际大小）。
- **归还**：主动中止、过期清理，以及一切未能成功完成的会话，其占住的
  对象位与容量**原样归还**；分块落盘失败也会当场退回本次占用的字节。
- **无残留**：任何失败路径都不会留下对外可见的半个对象——对象只在
  全部校验通过后整目录 rename 发布。

### 并发与重启一致性

- 账本内所有“检查 + 记账”都在同一把锁内原子完成；同一会话的操作另由
  会话锁串行化。因此无论多少完成请求同时到达，任何调用方的最终落盘
  都不会越过自己的额度；重复提交、完成与清理并发也不会重复扣减或漏还
  （释放/结转按会话幂等）。
- 账本不单独落盘：**对象 meta（含 `tenant_id`）+ 会话 meta 是唯一事实
  来源**，进程启动时全量扫描重建（对账）。重启后各调用方占用与重启前
  一致——既不丢占用、也不多算；未完成的会话可继续续传。

### 用量查询

`GET /tenants/{id}/usage` 返回该调用方的配额、`committed`（已落盘）、
`reserved`（会话预留）、`remaining`（剩余）以及名下对象清单。
查询不存在的对象返回 `OBJECT_NOT_FOUND`，查询不存在的会话返回
`SESSION_NOT_FOUND`，两者是可区分的 404。

### 兼容范围（老用法如何对应）

不带 `X-Tenant-Id` 的既有接入方式**完全不变**：它映射到内置默认调用方，
其额度就是原来的全局限额（`GATEWAY_MAX_TOTAL_OBJECT_BYTES` /
`GATEWAY_MAX_OBJECT_COUNT` / `GATEWAY_MIN_PART_BYTES`），超额仍报
`QUOTA_EXCEEDED`（507），且只在完成时按已落盘对象检查——语义与改造前
一致，只是“检查 + 记账”改为持锁原子完成，并发完成不会越过限额。
命名调用方的额度与默认调用方互不占用：任一方用满只拒绝该方。

## 磁盘布局

```
<data_dir>/
├── sessions/<session_id>/
│   ├── meta.json            # 会话状态（原子写）
│   ├── part_1, part_2, ...  # 暂存分块（完成/中止/过期后删除）
│   └── .merge-*             # 合并临时文件（失败即删除）
└── objects/
    ├── <sha256(object_key)>/
    │   ├── data             # 对象内容（整目录 rename 原子发布）
    │   └── meta.json        # key/size/checksum/created_at
    └── .staging/<tmp>/      # 发布前暂存
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
| `GATEWAY_MAX_TOTAL_OBJECT_BYTES` | `1073741824` | 对象总容量配额 |
| `GATEWAY_MAX_OBJECT_COUNT` | `10000` | 对象总数量配额 |
| `GATEWAY_MIN_PART_BYTES` | `1` | 单块最小字节数（默认调用方） |
| `GATEWAY_TENANTS` | `{}` | 命名调用方配额表（JSON，见“调用方治理”） |

## 本地验证方法

```bash
# 1) 全量测试（并发分块、重复分块、重启续传、摘要不匹配、过期清理、配额、
#    调用方隔离/预留归还/重启对账/用量查询/单块尺寸约束等）
uv run pytest

# 2) 单测日志：逐条用例结果 + 会话标识 / 调用方 / 分块序号 / 判定依据
#    （pytest.ini 已默认开启 -v 与 log_cli）
uv run pytest tests/test_tenants.py   # 只看治理能力相关用例
# 日志示例：
# INFO gateway.service: session=ebf7... tenant=alpha part=1 action=upload
#   verdict=ACCEPTED size=4 ...
# INFO gateway.service: session=ebf7... action=complete verdict=COMPLETED ...

# 3) 端到端脚本（进程内 ASGI 调用，无需端口；含调用方治理演示）
uv run python scripts/local_verify.py

# 4) 真实 HTTP + curl
GATEWAY_DATA_DIR=/tmp/rogw-demo \
GATEWAY_TENANTS='{"team-a":{"max_bytes":1048576,"max_objects":10,"min_part_bytes":4}}' \
  uv run uvicorn main:app --port 8000

curl -X POST localhost:8000/sessions -H 'X-Tenant-Id: team-a' \
  -H 'Content-Type: application/json' -d '{"object_key":"a.bin","total_parts":1}'
curl localhost:8000/tenants/team-a/usage | jq
```

## 代码结构

```
gateway/
├── config.py    # 配置（环境变量，含 GATEWAY_TENANTS）
├── errors.py    # ErrorCode 枚举与 GatewayError
├── models.py    # 会话/分块模型与 API schema（含用量查询响应）
├── hashing.py   # SHA-256 与常量时间摘要比对
├── storage.py   # 本地磁盘对象存储（分块暂存/原子合并发布/对象元信息）
├── sessions.py  # 会话 JSON 持久化、会话级锁、过期扫描
├── tenants.py   # 调用方配额解析与用量账本（预留/结转/归还/重启对账）
├── service.py   # 业务规则编排（状态机/幂等/校验/配额/治理）
└── app.py       # FastAPI 装配、错误归类、后台 sweep
tests/           # 服务层单测、HTTP 集成测试与调用方治理测试
scripts/local_verify.py  # 端到端本地验证
```
