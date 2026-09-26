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
| `POST` | `/sessions` | 创建上传会话 |
| `PUT` | `/sessions/{id}/parts/{n}` | 上传/重复提交第 n 块（原始字节为 body） |
| `POST` | `/sessions/{id}/complete` | 校验并合并落盘 |
| `POST` | `/sessions/{id}/abort` | 主动中止（幂等） |
| `GET` | `/sessions/{id}` | 查询状态与上传进度 |
| `GET` | `/objects/{key}` | 查询已落盘对象元信息 |
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
| `QUOTA_EXCEEDED` | 507 | 总容量或对象数配额超限 |
| `OBJECT_ALREADY_EXISTS` | 409 | 对象 key 已存在（创建即拒绝） |

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
| `GATEWAY_MIN_PART_BYTES` | `1` | 单块最小字节数 |

## 本地验证方法

```bash
# 1) 全量测试（并发分块、重复分块、重启续传、摘要不匹配、过期清理、配额等）
uv run pytest -v

# 2) 单测日志：会话标识 / 分块序号 / 判定依据
uv run pytest tests/test_service.py::TestUploadPart::test_part_checksum_mismatch_is_classified \
  -o log_cli=true -o log_cli_level=INFO
# 日志示例：
# session=063b... part=1 action=upload verdict=REJECTED
#   reason=PART_CHECKSUM_MISMATCH expected=fff... actual=ba78... size=3

# 3) 端到端脚本（进程内 ASGI 调用，无需端口）
uv run python scripts/local_verify.py

# 4) 真实 HTTP + curl
GATEWAY_DATA_DIR=/tmp/rogw-demo uv run uvicorn main:app --port 8000
```

## 代码结构

```
gateway/
├── config.py    # 配置（环境变量）
├── errors.py    # ErrorCode 枚举与 GatewayError
├── models.py    # 会话/分块模型与 API schema
├── hashing.py   # SHA-256 与常量时间摘要比对
├── storage.py   # 本地磁盘对象存储（分块暂存/原子合并发布/配额统计）
├── sessions.py  # 会话 JSON 持久化、会话级锁、过期扫描
├── service.py   # 业务规则编排（状态机/幂等/校验/配额）
└── app.py       # FastAPI 装配、错误归类、后台 sweep
tests/           # 服务层单测与 HTTP 集成测试
scripts/local_verify.py  # 端到端本地验证
```
