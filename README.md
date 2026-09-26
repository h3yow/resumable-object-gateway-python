# Resumable Object Gateway（本地磁盘模拟对象存储）

一套**完全本地可验证**的分块上传与对象落盘服务：用本地磁盘目录模拟对象存储，
不依赖任何外部服务或真实凭据。支持上传会话创建、分块写入、完成合并、主动中止、
并发写入、中断恢复（进程重启后续传）、过期清理、单块/整体摘要一致性校验与
容量/数量配额，且所有入口（Python API、HTTP、进程重启）对同一情形给出
**一致且可区分**的错误结论。

---

## 1. 快速开始

```bash
# 安装（含测试依赖）
uv sync --group dev

# 运行测试
uv run pytest -q

# 启动本地服务（默认数据目录 ./.local-data，默认 127.0.0.1:8000）
uv run python main.py
```

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

---

## 2. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/sessions` | 创建上传会话 |
| `GET` | `/sessions/{sid}` | 查询会话进度（已收/缺失分块、字节数、过期时间） |
| `PUT` | `/sessions/{sid}/parts/{n}` | 写入第 `n` 个分块（序号从 1 开始） |
| `POST` | `/sessions/{sid}/complete` | 校验并合并落盘 |
| `POST` | `/sessions/{sid}/abort` | 主动中止会话 |
| `POST` | `/admin/sweep` | 立即触发一次过期清理（可注入 `now`，便于验证） |
| `GET` | `/objects/{key}` | 查看对象元数据 |
| `GET` | `/objects/{key}/content` | 下载对象内容 |

分块摘要通过请求头 `X-Part-Digest: <sha256 hex>` 提供；整体摘要在创建会话
（`whole_digest`）或完成请求（`whole_digest`，优先级更高）中提供，均为
**64 位小写十六进制 sha256**。

### 端到端示例

```bash
SID=$(curl -s -X POST http://127.0.0.1:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"object_key":"demo/a.txt","part_count":2,"total_size":11}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

printf 'hello ' | curl -s -X PUT --data-binary @- \
  http://127.0.0.1:8000/sessions/$SID/parts/1
printf 'world' | curl -s -X PUT --data-binary @- \
  http://127.0.0.1:8000/sessions/$SID/parts/2

curl -s http://127.0.0.1:8000/sessions/$SID            # 查看进度
curl -s -X POST http://127.0.0.1:8000/sessions/$SID/complete \
  -H 'Content-Type: application/json' -d '{}'
curl -s http://127.0.0.1:8000/objects/demo/a.txt/content
```

---

## 3. 会话状态机

```
                 POST /sessions
                       │
                       ▼
                 ┌──────────┐   全部分块到齐 + 摘要/配额校验通过
   PUT parts ──▶ │ PENDING  │ ───────────────────────────────▶ ┌──────────┐
                 │ (可写)   │                                  │ COMPLETE │ 终态
                 └──────────┘                                  └──────────┘
                    │       │                                        ▲
     POST /abort    │       │ now > expires_at（后台或 /admin/sweep） │ 对象原子落盘
                    ▼       ▼                                        │ 后才迁移
              ┌─────────┐ ┌─────────┐                                │
              │ ABORTED │ │ EXPIRED │ ──────────── 终态，拒绝写 ─────┘
              └─────────┘ └─────────┘
```

* **PENDING**：唯一可写入状态。每次成功写入新分块会把 `updated_at` 刷新为
  当前时间，并把 `expires_at = updated_at + session_ttl_seconds`
  （**滑动过期**）。
* **COMPLETE**：对象数据已原子发布到对象区，状态迁移与对象发布在同一把
  全局锁内完成；之后再写分块/完成/中止均返回 `session_state_conflict`。
* **ABORTED / EXPIRED**：临时分块目录已被清理。对 ABORTED 的写操作返回
  `session_state_conflict`（409）；对 EXPIRED 返回可区分的
  `session_expired`（410）。
* 校验失败（分块缺失、摘要不匹配、配额超限）**不会**迁移状态：会话仍停留在
  PENDING（整体摘要失败时仍可修正后重试完成），且不会产生对外可见的对象。

---

## 4. 分块、摘要与配额规则

### 4.1 分块规则

* 序号 `n` 为整数，范围 `1 .. part_count`；越界返回
  `part_index_out_of_range`（400）。
* 单块字节数不得超过 `max_part_bytes`，否则返回 `quota_exceeded`（507）。
* 累计已收字节不得超过创建时声明的 `total_size`，否则返回
  `quota_exceeded`。`complete` 时还会复查“已收字节总数 == total_size”。
* **重复提交同一分块是幂等的**：已存在且内容一致（比对 sha256）时直接返回
  既有接收信息，不重复计字节、不改变 `received_at`；同序号但内容/声明摘要
  不一致时返回 `part_digest_mismatch`（422），绝不覆盖已存数据。
* 零字节块是合法边界，空内容 sha256 是确定值。

### 4.2 摘要规则（sha256, hex）

* 分块携带 `X-Part-Digest` 时，落盘前实时计算并比对；不匹配则删除临时文件、
  返回 `part_digest_mismatch`，该块不算已接收，可用正确内容重传。
* 完成时按序号流式拼接并计算整体摘要；与 `complete` 请求或创建时声明的
  `whole_digest` 不一致则返回 `whole_digest_mismatch`（422），
  删除合并临时文件，对象索引不变。
* 未声明整体摘要时，完成接口返回实际计算出的摘要，由调用方自行核对。
* 成功对象的数据按内容摘要寻址存储（`objects/<sha前2位>/<sha>`），
  下载内容与提交摘要必然一致。

### 4.3 配额（超限统一为 `quota_exceeded`, 507，消息区分原因）

| 约束 | 判定时机 |
| --- | --- |
| 单会话声明总量 `total_size ≤ max_session_bytes` | 创建会话时 |
| 单块大小 `≤ max_part_bytes` | 写入分块时 |
| 累计已收 `≤ total_size` | 写入分块时 |
| 已收总量 `== total_size` | 完成时 |
| 全局已用容量 + 本对象 `≤ max_total_bytes` | 合并前后各复查一次 |
| 全局对象数 + 1 `≤ max_object_count` | 合并前后各复查一次 |
| 对象键不得重复 | 完成时（`object_exists`, 409） |

配额计数与对象索引一起原子更新，并在进程重启时依据对象索引重建，
因此重启后配额约束依然成立。

### 4.4 错误码总表

| code | HTTP | 触发情形 |
| --- | --- | --- |
| `invalid_request` | 400 | 摘要格式非法、对象键含路径穿越等 |
| `part_index_out_of_range` | 400 | 分块序号 `<1` 或 `>part_count` |
| `session_not_found` | 404 | 会话 id 不存在 |
| `object_not_found` | 404 | 对象键不存在 |
| `session_state_conflict` | 409 | 对 COMPLETE/ABORTED 会话写入或重复完成/中止；对象键重复 |
| `parts_missing` | 409 | 完成时仍有分块缺失（消息列出缺失序号） |
| `whole_digest_mismatch` / `part_digest_mismatch` | 422 | 摘要不匹配 |
| `session_expired` | 410 | 会话超过无活动 TTL |
| `quota_exceeded` | 507 | 各类容量/数量超限 |

错误响应体统一为：

```json
{"error": {"code": "parts_missing", "message": "分块缺失，无法完成: [2, 3]"}}
```

---

## 5. 并发、原子性与恢复语义

### 5.1 并发模型（单进程多线程）

* **全局 RLock**：保护所有元数据 JSON（会话 meta、对象索引、配额）的
  读-改-写，临界区只做内存操作与原子 rename，耗时极短。
* **每会话一把锁**：同一会话的分块写入、完成、中止彼此串行；
  不同会话互不阻塞。锁序固定为“会话锁 → 全局锁”，避免死锁。
* **分块文件原子发布**：先写 `parts/NNNNN.tmp-<uuid>`，`fsync` 后
  `os.replace` 为正式文件，再更新元数据；任何中途失败都只留下临时文件
  （会被清理），不会污染正式状态。
* 同一分块被多线程并发提交时，只有一个发布会生效，其余按幂等返回；
`tests/test_idempotency_concurrency.py` 用 24 块 × 16 线程 × 每块重复
  两次验证最终拼接摘要与原始内容一致。

### 5.2 中断恢复（进程重启续传）

* 会话、分块、对象索引、配额全部持久化在 `data_dir` 下。
* 重启（`init_layout`）时：
  1. 依据对象索引重建配额计数；
  2. 扫描会话目录，对每个会话**以分块数据文件为准**剔除孤儿元数据记录
     （模拟“元数据写成功但数据文件丢失”的崩溃残留）；
  3. PENDING 会话可继续上传，COMPLETE/ABORTED/EXPIRED 会话保持终态拒绝写入。

### 5.3 过期清理

* 后台守护线程（可关闭）按 `sweeper_interval_seconds` 周期调用清理；
  也可通过 `POST /admin/sweep`（或测试中注入 `now`）立即触发。
* 清理规则：仅针对 PENDING 且 `now > expires_at` 的会话，置为 EXPIRED、
  删除分块文件。清理获取会话锁后会二次复查，避免与正在进行的写入竞态。

### 5.4 “失败无残缺对象”保证

合并先写对象区的隐藏临时文件 `.merge-<sid>-<uuid>`，摘要与配额全部通过后，
在全局锁内：复查配额与对象键 → `os.replace` 发布内容寻址文件 →
更新对象索引与配额 → 会话置 COMPLETE（元数据原子写）。任一步失败都会
删除临时文件且不更新索引，因此外部永远观察不到残缺对象。

---

## 6. 磁盘布局

```
<data_dir>/
├── sessions/
│   └── <sid>/
│       ├── meta.json              # 会话元数据（原子写）
│       └── parts/00001            # 已收分块（原子 rename 发布）
├── objects/
│   └── <sha前2位>/<sha256>        # 内容寻址的对象数据
└── meta/
    ├── objects.json               # 对象键 -> 元数据索引（原子写）
    └── quota.json                 # 对象数/总字节计数（原子写，可由索引重建）
```

---

## 7. 可配置项

可构造 `Settings(...)` 或用 `ROG_` 前缀环境变量配置（`Settings.from_env()`）：

| 配置 | 环境变量 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `data_dir` | `ROG_DATA_DIR` | `./.local-data` | 本地磁盘根目录 |
| `session_ttl_seconds` | `ROG_SESSION_TTL_SECONDS` | `3600` | 会话无活动过期秒数（滑动） |
| `max_session_bytes` | `ROG_MAX_SESSION_BYTES` | `100 MiB` | 单会话总大小上限 |
| `max_part_bytes` | `ROG_MAX_PART_BYTES` | `16 MiB` | 单块大小上限 |
| `max_total_bytes` | `ROG_MAX_TOTAL_BYTES` | `1 GiB` | 全局对象总容量上限 |
| `max_object_count` | `ROG_MAX_OBJECT_COUNT` | `10000` | 全局对象数量上限 |
| `enable_sweeper` | `ROG_ENABLE_SWEEPER` | `true` | 是否启动后台清理线程 |
| `sweeper_interval_seconds` | `ROG_SWEEPER_INTERVAL_SECONDS` | `30.0` | 清理扫描间隔 |

示例：`ROG_SESSION_TTL_SECONDS=2 ROG_DATA_DIR=/tmp/rog-demo uv run python main.py`

---

## 8. 本地验证方法

```bash
uv run pytest -q                       # 全部用例
uv run pytest -k concurrent -q         # 仅并发/幂等场景
uv run pytest tests/test_recovery.py -q   # 重启续传/崩溃残留
uv run pytest tests/test_expiry.py -q     # 过期会话（含后台线程）
uv run pytest tests/test_checksum.py -q   # 摘要不匹配/无残缺对象
uv run pytest tests/test_quota.py -q      # 容量与数量配额
uv run pytest tests/test_http_api.py -q   # HTTP 入口错误码归类
```

测试覆盖矩阵：

| 场景 | 用例文件 |
| --- | --- |
| 生命周期/状态机/越界/缺失/重复键/键安全 | `test_session_lifecycle.py` |
| 并发分块、重复分块幂等、并发去重 | `test_idempotency_concurrency.py` |
| 单块/整体摘要不匹配、失败不留残对象 | `test_checksum.py` |
| 单块/会话/全局容量/数量配额、配额跨重启 | `test_quota.py` |
| 重启续传、终态持久化、孤儿记录剔除 | `test_recovery.py` |
| 手动/后台过期清理、滑动 TTL、过期持久化 | `test_expiry.py` |
| HTTP 全流程、错误码归类、参数校验 | `test_http_api.py` / `test_validation.py` |

### 日志可解释性

网关日志（logger 名 `resumable_object_gateway`）在每个关键判定点输出
**会话标识 `sid`、分块序号 `part` 与判定依据**，例如：

```
INFO  接收分块: sid=4636.. part=2 size=6 digest=75f7.. 累计块=2
INFO  重复分块幂等返回: sid=4636.. part=1 size=3 依据=摘要一致(afae..)
WARNING 拒绝完成(整体摘要不匹配): sid=.. 期望=ab12.. 实际=cd34..
WARNING 拒绝分块(序号越界): sid=.. part=4 上限=3
INFO  过期清理: sid=.. key=.. 依据=now(1700)>expires_at(1599)
```

`pytest` 默认会在失败时打印捕获到的日志；也可用
`uv run pytest --log-cli-level=INFO` 实时查看每个用例的判定依据。

---

## 9. 代码结构

```
app/
├── config.py         # Settings 配置与环境变量加载
├── errors.py         # 可区分业务错误（code + HTTP 状态）
├── models.py         # Pydantic 模型与会话状态枚举
├── logging_setup.py  # 统一日志
├── storage.py        # 磁盘后端：会话/分块/对象/配额/清理（核心）
├── service.py        # 高层应用服务（编排、重启恢复、对象键校验）
├── sweeper.py        # 过期清理后台线程
└── http_api.py       # FastAPI 路由与统一错误响应
main.py               # 启动入口
tests/                # 8 个测试文件，38 个用例
```
