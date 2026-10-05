# 冷链运输温度事件核验服务

基于 **Python + FastAPI + SQLite** 的冷链温度核验服务。客户端按设备提交温度读数，
服务自动核验并维护**超温区间**；支持迟到补报、按事件号更正与撤销，并在每次写操作后
重算受影响区间，返回当前全量区间及本次新增 / 变更 / 失效的区间差异。

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

迟到补报示例：若已有 `E2(08:10, 9.0)`、`E3(08:20, 9.5)` 构成起点为 `E2` 的区间，
补交一条更早的超限读数 `E1(08:00, 8.5)` 后，响应中 `changes.invalidated` 含旧区间
`E2`、`changes.added` 含新区间 `E1`（起点改变 → 旧失效、新新增）。

## 错误码

| 状态码 | 场景                                                                 |
| ------ | -------------------------------------------------------------------- |
| 200    | 成功（含幂等重放、重复撤销）                                         |
| 404    | 更正 / 撤销的事件号不存在                                            |
| 409    | 同事件号提交不同内容；更正已撤销的读数                               |
| 422    | 无效时间格式、无效温度（非数值 / 无穷 / 超范围）、非法事件号         |

错误响应体均含 `detail` 字段说明原因。

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 项目结构

```
app/
  main.py       # FastAPI 路由、事务内重算区间、错误处理
  intervals.py  # 区间计算与差异比对（纯函数）
  models.py     # 请求/响应模型与输入校验
  db.py         # SQLite 连接与建表
  config.py     # 环境变量配置
  util.py       # 时间规范化（UTC、固定宽度，保证可排序）
tests/test_api.py
Dockerfile / docker-compose.yml / requirements*.txt
```
