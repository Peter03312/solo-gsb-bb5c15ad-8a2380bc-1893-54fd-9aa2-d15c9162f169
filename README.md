# 冷链运输温度事件核验服务

基于 **Python + FastAPI + SQLite** 的冷链温度核验服务。客户端按设备提交温度读数，
服务自动核验并维护**超温区间**；支持迟到补报、按事件号更正与撤销，并在每次写操作后
重算受影响区间，返回当前全量区间及本次新增 / 变更 / 失效的区间差异。

此外支持**争议（dispute）**流程：对现有读数以争议号申请更正或撤销，申请时冻结读数
及其修订号；待审争议可批准或驳回，批准时在同事务内核对修订号，读数已被直接操作或
其他争议改变则转为过期、不再改动读数。

## 超温区间判定规则

- 同一设备的**有效**（未撤销）读数按 **(采样时间, 事件号)** 升序排列；
- 温度 **严格大于** 上限（`TEMP_LIMIT`）的读数视为“超限”（等于上限不算超限）；
- **连续 ≥ 2 条**超限读数构成一个超温区间；
  - **起点**：首个超限读数；**终点**：区间之后首个不超限读数；
  - 若超限一直延续到最后一条读数，区间未结束，终点字段为 `null`；
- 区间以**起点读数的事件号**为唯一标识；
- 迟到、更正、撤销读数后会重算受影响区间：
  - 起点不变、内容变化 → 计入 `changed`；
  - 起点改变 → 旧区间计入 `invalidated`，新区间计入 `added`。

## 快速开始

### 方式一：Docker Compose（推荐，一键完成依赖安装与数据库初始化）

```bash
docker compose up --build
```

### 方式二：Docker

```bash
docker build -t coldchain-service .
docker run -d -p 8000:8000 -e TEMP_LIMIT=8.0 -v coldchain-data:/data coldchain-service
```

### 方式三：本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库文件会在服务启动时自动初始化（建表，幂等），无需手工执行任何脚本。

## 配置

| 环境变量     | 默认值                            | 说明                                   |
| ------------ | --------------------------------- | -------------------------------------- |
| `TEMP_LIMIT` | `8.0`                             | 超温上限（°C），严格大于该值视为超限   |
| `DB_PATH`    | 本地 `./coldchain.db`，容器 `/data/coldchain.db` | SQLite 数据库文件路径 |

Compose 中通过 `environment` 修改，例如 `TEMP_LIMIT: "2.0"`。

## 访问地址

| 地址                            | 说明                       |
| ------------------------------- | -------------------------- |
| http://localhost:8000           | 服务信息                   |
| http://localhost:8000/docs      | Swagger 交互式 API 文档    |
| http://localhost:8000/health    | 健康检查                   |
| 端口                            | **8000**（宿主机:容器）    |

## API 一览

| 方法   | 路径                                    | 说明                                       |
| ------ | --------------------------------------- | ------------------------------------------ |
| POST   | `/devices/{device_id}/readings`         | 提交读数（同号同内容幂等，同号异内容 409） |
| PUT    | `/devices/{device_id}/readings/{event_no}` | 按事件号更正采样时间与温度              |
| DELETE | `/devices/{device_id}/readings/{event_no}` | 按事件号撤销读数（重复撤销幂等）        |
| GET    | `/devices/{device_id}/intervals`        | 查询当前全部超温区间                       |
| GET    | `/devices/{device_id}/readings`         | 查询全部读数（含已撤销），按规则排序       |
| POST   | `/devices/{device_id}/disputes`         | 提出争议（同号同内容幂等，异内容 409）     |
| POST   | `/devices/{device_id}/disputes/{dispute_no}/approve` | 批准争议（修订号不匹配则转为过期） |
| POST   | `/devices/{device_id}/disputes/{dispute_no}/reject`  | 驳回争议（不改读数）             |
| GET    | `/devices/{device_id}/disputes/{dispute_no}` | 查询争议单：冻结依据、终态与执行结果 |
| GET    | `/devices/{device_id}/disputes`         | 查询设备全部争议单                         |
| GET    | `/health`                               | 健康检查                                   |

### 请求字段

- `event_no`：设备内唯一事件号（字母 / 数字 / `.` `_` `-`，≤ 64 字符）；
- `sampled_at`：ISO 8601 采样时间（如 `2026-10-01T08:00:00Z`，无时区按 UTC 处理）；
- `temperature`：温度（°C），须为有限数值且在 `[-273.15, 1000]` 内。

### 统一响应结构（写操作与区间查询）

```json
{
  "device_id": "truck-001",
  "limit": 8.0,
  "intervals": [ /* 当前全部有效超温区间 */ ],
  "changes": {
    "added":       [ /* 本次新增区间 */ ],
    "changed":     [ /* 起点不变但内容变化的区间（变更后版本） */ ],
    "invalidated": [ /* 本次失效区间（变更前版本） */ ]
  }
}
```

区间对象：`start_event_no` / `start_sampled_at` / `start_temperature` /
`end_event_no` / `end_sampled_at` / `end_temperature`（未结束为 `null`）/
`reading_count`（连续超限条数）/ `max_temperature`（区间峰值）。

## 争议（dispute）流程

对读数的更正 / 撤销除了直接调用 PUT / DELETE，也可以走争议审批流程：

### 提出争议

`POST /devices/{device_id}/disputes`，请求体：

```json
{
  "dispute_no": "D1",
  "event_no": "E2",
  "action": "correct",
  "sampled_at": "2026-10-01T08:10:00Z",
  "temperature": 5.0,
  "reason": "探头校准偏差，复测为 5.0°C"
}
```

- `dispute_no`：设备内唯一争议号（字符规则同事件号）；
- `action`：`correct`（更正，必须携带 `sampled_at` 与 `temperature`）或
  `revoke`（撤销，不得携带目标字段）；
- `reason`：非空原因（去空白后不能为空），全程保留；
- 申请时**冻结**目标读数的快照与**修订号**（`frozen` 字段）；
- 目标读数不存在 → 404，已撤销 → 409；
- 同争议号同内容重试 → 200 返回原申请（即使目标读数此后已变化）；
  同号异内容 → 409。新建成功返回 201。

### 批准 / 驳回

`POST .../disputes/{dispute_no}/approve` 或 `.../reject`：

- **驳回**：不改动读数，争议进入 `rejected`；
- **批准**：在同一事务内核对冻结修订号——
  - 读数已被直接操作（PUT / DELETE）或其他已批准争议改变（修订号不一致，
    **即使现值恰好与冻结值相同**）→ 争议转为 `expired`，记录 `expire_reason`，
    **不再改动读数**；
  - 修订号仍匹配 → 按现有规则执行更正或撤销（读数修订号随之递增），
    争议进入 `approved`，并记录决定当次的区间差异 `changes`；
- 争议只能进入一个终态：同一决定重试返回**首次结果**（含原 `decided_at`
  与 `changes`），反向决定 → 409；未知争议号 → 404。

### 争议单响应结构

```json
{
  "device_id": "truck-001",
  "dispute_no": "D1",
  "event_no": "E2",
  "action": "correct",
  "target": {"sampled_at": "2026-10-01T08:10:00.000000Z", "temperature": 5.0},
  "reason": "探头校准偏差，复测为 5.0°C",
  "status": "approved",
  "frozen": {"sampled_at": "2026-10-01T08:10:00.000000Z", "temperature": 9.5,
             "revoked": false, "revision": 0},
  "decision": "approve",
  "decided_at": "2026-10-02T01:23:45.000000Z",
  "expire_reason": null,
  "changes": {"added": [], "changed": [], "invalidated": [ /* 区间对象 */ ]},
  "created_at": "2026-10-02T01:20:00.000000Z",
  "updated_at": "2026-10-02T01:23:45.000000Z"
}
```

- `status`：`pending`（待审）→ `approved` / `rejected` / `expired`（终态）；
- `frozen`：申请时冻结的读数快照与修订号（批准时的核对依据）；
- `changes`：决定当次的区间差异；待审为 `null`，驳回 / 过期为空差异；
- `target`：更正目标（`action=revoke` 时为 `null`）。

## 调用示例

```bash
BASE=http://localhost:8000/devices/truck-001

# 1. 提交读数：首条超限，尚不构成区间
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E1","sampled_at":"2026-10-01T08:00:00Z","temperature":9.0}'

# 2. 第二条连续超限 → 形成区间（未结束，终点为 null），changes.added 出现 E1 区间
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E2","sampled_at":"2026-10-01T08:10:00Z","temperature":9.5}'

# 3. 首次不超限 → 区间闭合，终点为 E3，计入 changes.changed
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":6.0}'

# 4. 幂等重放：与 E3 内容完全一致 → 200 且 changes 全空
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":6.0}'

# 5. 同号异内容 → 409 Conflict
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":7.0}'

# 6. 更正：把 E2 改为不超限 → 原区间失效（changes.invalidated）
curl -X PUT $BASE/readings/E2 -H 'Content-Type: application/json' \
  -d '{"sampled_at":"2026-10-01T08:10:00Z","temperature":5.0}'

# 7. 撤销读数
curl -X DELETE $BASE/readings/E1

# 8. 查询当前区间与全部读数
curl $BASE/intervals
curl $BASE/readings
```

### 争议流程示例

```bash
BASE=http://localhost:8000/devices/truck-001

# 1. 准备两条超限读数（形成区间）
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E1","sampled_at":"2026-10-01T08:00:00Z","temperature":9.0}'
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E2","sampled_at":"2026-10-01T08:10:00Z","temperature":9.5}'

# 2. 对 E2 提出更正争议 → 201，status=pending，frozen 记录快照与修订号
curl -X POST $BASE/disputes -H 'Content-Type: application/json' \
  -d '{"dispute_no":"D1","event_no":"E2","action":"correct",
       "sampled_at":"2026-10-01T08:10:00Z","temperature":5.0,
       "reason":"探头校准偏差，复测为 5.0°C"}'

# 3. 同号同内容重试 → 200 返回原申请；同号异内容 → 409
# 4. 批准：修订号匹配 → 执行更正，status=approved，changes 记录区间失效
curl -X POST $BASE/disputes/D1/approve

# 5. 重复批准 → 200 返回首次结果；再驳回 → 409（终态不可逆）
curl -X POST $BASE/disputes/D1/approve
curl -X POST $BASE/disputes/D1/reject

# 6. 过期场景：先提争议，再直接更正同一读数，批准时修订号已变化
curl -X POST $BASE/disputes -H 'Content-Type: application/json' \
  -d '{"dispute_no":"D2","event_no":"E1","action":"revoke","reason":"重复上报"}'
curl -X PUT $BASE/readings/E1 -H 'Content-Type: application/json' \
  -d '{"sampled_at":"2026-10-01T08:00:00Z","temperature":9.1}'
curl -X POST $BASE/disputes/D2/approve
# → status=expired，expire_reason 说明修订号变化，读数保持直接更正后的值

# 7. 撤销类争议与驳回
curl -X POST $BASE/disputes -H 'Content-Type: application/json' \
  -d '{"dispute_no":"D3","event_no":"E1","action":"revoke","reason":"误报"}'
curl -X POST $BASE/disputes/D3/reject   # → status=rejected，读数不变

# 8. 查询争议单（冻结依据 / 终态 / 执行结果）与设备全部争议
curl $BASE/disputes/D1
curl $BASE/disputes
```

迟到补报示例：若已有 `E2(08:10, 9.0)`、`E3(08:20, 9.5)` 构成起点为 `E2` 的区间，
补交一条更早的超限读数 `E1(08:00, 8.5)` 后，响应中 `changes.invalidated` 含旧区间
`E2`、`changes.added` 含新区间 `E1`（起点改变 → 旧失效、新新增）。

## 错误码

| 状态码 | 场景                                                                 |
| ------ | -------------------------------------------------------------------- |
| 200    | 成功（含幂等重放、重复撤销、争议同决定重试）                         |
| 201    | 争议新建成功                                                         |
| 404    | 更正 / 撤销的事件号不存在；争议目标读数不存在；争议号不存在          |
| 409    | 同事件号提交不同内容；更正已撤销的读数；对已撤销读数提争议；同争议号异内容；对已终态争议做反向决定 |
| 422    | 无效时间格式、无效温度（非数值 / 无穷 / 超范围）、非法事件号 / 争议号、原因为空、更正缺目标字段、撤销携带目标字段 |

错误响应体均含 `detail` 字段说明原因。

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 项目结构

```
app/
  main.py       # FastAPI 路由、事务内重算区间、争议审批（修订号核对）、错误处理
  intervals.py  # 区间计算与差异比对（纯函数）
  models.py     # 请求/响应模型与输入校验（含争议申请校验）
  db.py         # SQLite 连接、建表与老库迁移（readings.revision、disputes 表）
  config.py     # 环境变量配置
  util.py       # 时间规范化（UTC、固定宽度，保证可排序）
tests/test_api.py       # 读数与区间端到端测试
tests/test_disputes.py  # 争议流程端到端测试
Dockerfile / docker-compose.yml / requirements*.txt
```
