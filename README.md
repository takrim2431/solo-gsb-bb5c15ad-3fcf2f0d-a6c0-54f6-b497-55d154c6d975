# 批次追溯服务（batch-trace）

面向小型食品加工厂的**本地批次追溯服务**：登记原料批次、记录生产投料与产出、
查询批次余量与上下游谱系，在发现疑似污染批次时找出并隔离全部后代产出，
并支持质检复核确认误报后撤销疑似标记、按谱系重算隔离状态。

- 语言/框架：Python 3.12 + FastAPI + SQLAlchemy 2
- 关系数据库：PostgreSQL 16（Docker Compose 一键启动）；无数据库时自动回退本地 SQLite 文件库
- 计量：`NUMERIC(18,4)` 定点数，数量必须为**正数**，同一笔单据内**单位一致**
- 部署：Dockerfile 构建镜像，`docker compose up` 安装依赖、建库建表并启动

---

## 一、快速启动

### 方式 A：Docker Compose（推荐，含 PostgreSQL）

```bash
docker compose up --build -d
```

启动后：

- API 服务地址：`http://localhost:8080`
- 健康检查：<http://localhost:8080/health>
- 交互式接口文档（Swagger UI）：<http://localhost:8080/docs>

API 容器会等待 PostgreSQL 健康后自动**建库建表**（`app/entrypoint.py` 调用 `CREATE TABLE IF NOT EXISTS ...`），
数据持久化在 compose 卷 `trace_pgdata` 中。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据库卷
```

### 方式 B：本地直接运行（自动使用 SQLite）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.entrypoint
```

不设置 `DATABASE_URL` 时会在项目根目录创建 `trace.db`，服务同样监听 `http://0.0.0.0:8080`。

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

---

## 二、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 见下 | SQLAlchemy 数据库连接串。compose 中为 `postgresql+psycopg2://trace:trace@db:5432/trace`；未设置时回退 `sqlite:///./trace.db` |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `db` / `5432` / `trace` / `trace` / `trace` | 未设置 `DATABASE_URL` 时用于拼装 PostgreSQL 连接串 |
| `HOST` | `0.0.0.0` | HTTP 监听地址 |
| `PORT` | `8080` | HTTP 监听端口 |
| `DB_POOL_SIZE` | `5` | PostgreSQL 连接池大小 |

compose 的端口映射：宿主机 `8080` → 容器 `8080`，可在 `docker-compose.yml` 的
`ports` 处修改宿主机端口。

---

## 三、HTTP 接口

所有写操作（`POST`）都必须携带请求头 `Idempotency-Key`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/raw-batches` | 登记一个原料批次及初始数量 |
| POST | `/productions` | 提交一次生产：多批次投入 → 一个产出批次 |
| GET | `/batches/{batch_no}` | 查询批次余量与状态 |
| GET | `/batches/{batch_no}/lineage` | 查询上下游谱系（递归） |
| POST | `/contamination` | 标记疑似污染批次，返回并隔离全部后代 |
| POST | `/reviews` | 质检复核：确认疑似批次为误报，撤销标记并重算子树隔离 |

### 业务规则

1. 数量一律为正数，使用定点数计算；同一生产单内所有批次单位必须一致。
2. 产出量 **不得超过** 投入总量（允许加工损耗，即产出 < 投入）。
3. 批次编号全局唯一、**不得复用**（原料批次与产出批次共用编号空间）。
4. 来源不存在、用量无效（非正数）、单位不一致或余量不足时，**整笔拒绝，不改变任何余量**。
5. 污染规则：标记某批次疑似污染后，其全部直接/间接产出（混料后代）**整体隔离**；
   隔离批次（含疑似批次本身）**不得再投料**，提交生产时整笔拒绝。
6. 质检复核（撤销误报）：操作员提交**批次编号 + 复核说明**撤销该批次的疑似标记，
   并对“该批次 + 其全部后代”重算隔离——只要批次自身或**任一祖先**仍被标记疑似就继续隔离，
   否则解除；混料后代不会因一个来源解除而忽略另一疑似来源。只接受**当前疑似**批次：
   编号不存在返回 `SOURCE_NOT_FOUND`(404)，从未疑似或已撤销返回 `BATCH_NOT_SUSPECT`(409)，
   两种失败都不改变任何状态。批次查询与谱系结果会同步反映隔离变化。
7. 幂等：同一 `Idempotency-Key` + 同一请求体重试，返回首次的状态码与响应体、不重复扣减；
   同一 key + 不同请求体返回 `409 IDEMPOTENCY_CONFLICT`。
8. 并发：生产提交在单事务内对来源批次加行锁（PostgreSQL `SELECT ... FOR UPDATE`；
   SQLite 以 `BEGIN IMMEDIATE` 串行化写事务），复核与污染标记同样对子树行加锁，
   复核、污染标记与生产投料并发的结果**等同某一串行顺序**，并发提交不会超用同一批次余量，
   隔离批次绝不会被投料。

### 错误响应格式

```json
{ "error": { "code": "INSUFFICIENT_BALANCE", "message": "批次 'RM-1' 余量不足：请求 200，剩余 100", "details": { } } }
```

常见错误码：`SOURCE_NOT_FOUND`(404)、`BATCH_NO_REUSED`(409)、`INSUFFICIENT_BALANCE`(409)、
`UNIT_MISMATCH`(422)、`OUTPUT_EXCEEDS_INPUT`(422)、`VALIDATION_ERROR`(422)、
`BATCH_QUARANTINED`(409)、`BATCH_NOT_SUSPECT`(409)、`IDEMPOTENCY_CONFLICT`(409)、
`IDEMPOTENCY_KEY_REQUIRED`(400)。

---

## 四、HTTP 调用样例

### 1. 登记原料批次

```bash
curl -X POST http://localhost:8080/raw-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: raw-rm-a-001' \
  -d '{"batch_no":"RM-A","unit":"kg","quantity":"100","note":"高筋面粉"}'
```

```json
{"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":100.0,
 "is_suspect":false,"is_quarantined":false,"note":"高筋面粉"}
```

### 2. 提交一次混料生产（多批次投入 → 一个产出）

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{
        "output_batch_no": "MIX-1",
        "unit": "kg",
        "output_quantity": 90,
        "inputs": [
          {"batch_no": "RM-A", "quantity": 40},
          {"batch_no": "RM-B", "quantity": 55}
        ]
      }'
```

### 3. 安全重试（同一 key + 同一内容）

```bash
# 与上一条完全相同的请求：返回原结果，余量只扣一次
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{ ...同上... }'

# 同一 key 换不同内容：
# 409 {"error":{"code":"IDEMPOTENCY_CONFLICT", ...}}
```

### 4. 查询批次余量

```bash
curl http://localhost:8080/batches/RM-A
# {"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":60.0, ...}
```

### 5. 查询上下游谱系

```bash
curl http://localhost:8080/batches/RM-A/lineage
```

```json
{
  "batch_no": "RM-A",
  "upstream": [],
  "downstream": [
    {"batch_no": "MIX-1", "depth": 1, "remaining_qty": 40.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false},
    {"batch_no": "FG-1",  "depth": 2, "remaining_qty": 50.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false}
  ],
  "edges": [
    {"from": "RM-A", "to": "MIX-1"},
    {"from": "MIX-1", "to": "FG-1"}
  ]
}
```

`depth` 表示与查询批次相隔的生产层数；`edges` 给出当前谱系子图中的“原料 → 产出”有向边。

### 6. 标记疑似污染并找出全部后代

```bash
curl -X POST http://localhost:8080/contamination \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cont-20260930-01' \
  -d '{"batch_no":"RM-A","note":"抽检农残超标"}'
```

```json
{
  "source": "RM-A",
  "is_suspect": true,
  "affected_batch_nos": ["MIX-1", "FG-1"],
  "newly_quarantined": ["MIX-1", "FG-1"]
}
```

`MIX-1`（混料时混入 RM-A）与 `FG-1`（间接后代）被**整体隔离**；与 RM-A 无关的批次不受影响。
此后再用隔离批次投料会被拒绝：

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-fg2-001' \
  -d '{"output_batch_no":"FG-2","unit":"kg","output_quantity":5,
       "inputs":[{"batch_no":"MIX-1","quantity":5}]}'
# 409 {"error":{"code":"BATCH_QUARANTINED","message":"批次 'MIX-1' 已隔离，隔离批次不得再投料"}}
```

### 7. 质检复核：撤销误报并重算隔离

```bash
curl -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: review-20260930-01' \
  -d '{"batch_no":"RM-A","note":"复检合格，农残未超标，判定误报"}'
```

```json
{
  "reviewed": {
    "batch_no": "RM-A",
    "is_suspect": false,
    "is_quarantined": false,
    "review_note": "复检合格，农残未超标，判定误报"
  },
  "affected_descendant_nos": ["FG-1", "MIX-1"],
  "released_batch_nos": ["FG-1", "MIX-1", "RM-A"]
}
```

- `reviewed`：本次复核的批次及其重算后的疑似/隔离状态、复核说明；
- `affected_descendant_nos`：涉及的全部后代（按层数排序）；
- `released_batch_nos`：本次**实际解除隔离**的批次；为空表示复核批次仍被其他疑似祖先连带隔离。

只接受当前疑似批次，失败不改状态：

```bash
# 编号不存在
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-x' \
  -d '{"batch_no":"GHOST","note":"复核"}'
# 404 {"error":{"code":"SOURCE_NOT_FOUND", ...}}

# 已撤销（或从未疑似）：明确报错且状态不变
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-again' \
  -d '{"batch_no":"RM-A","note":"再次复核"}'
# 409 {"error":{"code":"BATCH_NOT_SUSPECT", ...}}
```

混料场景：`MIX-1` 由 `RM-A` 与 `RM-B` 混料，两者都疑似时只解除 `RM-A`，
`MIX-1` 与下游 `FG-1` 仍因 `RM-B` 保持隔离（本次 `released_batch_nos` 仅含 `RM-A`）：

```bash
curl -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-a' \
  -d '{"batch_no":"RM-A","note":"RM-A 误报"}'
# {"reviewed":{"batch_no":"RM-A","is_suspect":false,"is_quarantined":false,...},
#  "affected_descendant_nos":["FG-1","MIX-1"],
#  "released_batch_nos":["RM-A"]}
```

复核后 `GET /batches/{batch_no}` 与 `GET /batches/{batch_no}/lineage`
中的 `is_suspect` / `is_quarantined` 同步反映变化；解除隔离的批次可再次投料。
同键同内容重试安全返回首次结果（即使批次此刻已非疑似也不会报 `BATCH_NOT_SUSPECT`），
同键异内容返回 `409 IDEMPOTENCY_CONFLICT`。

### 8. 整笔拒绝示例（余量不足时不产生副作用）

```bash
# RM-A 当前余量 60，却请求消耗 200
curl -i -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-bad-001' \
  -d '{"output_batch_no":"MIX-X","unit":"kg","output_quantity":200,
       "inputs":[{"batch_no":"RM-A","quantity":200}]}'
# HTTP/1.1 409 ... INSUFFICIENT_BALANCE；MIX-X 未创建，RM-A 余量仍为 60
```

---

## 五、项目结构

```
app/
  config.py       # 环境变量配置
  db.py           # 引擎/会话、SQLite 写锁与 PG 行锁支撑、建表
  models.py       # batches / productions / production_inputs / contamination_reviews / idempotency_keys
  schemas.py      # Pydantic 请求响应模型（正数、定点数校验）
  errors.py       # 业务错误码
  service.py      # 核心逻辑：登记、生产事务、幂等、递归谱系、污染传播、复核重算
  routes.py       # HTTP 路由
  main.py         # FastAPI 应用与异常处理
  entrypoint.py   # 容器入口：等待数据库 → 建表 → 启动
tests/            # 端到端 + 并发测试（35 个用例）
Dockerfile
docker-compose.yml
```

## 六、并发与一致性说明

- 每次生产提交是一个数据库事务：先锁定全部来源批次，再依次校验单位、隔离状态、
  余量与产出总量，全部通过后才扣减并写入产出；任一校验失败立即回滚。
- PostgreSQL 依赖 `SELECT ... FOR UPDATE` 行锁串行化同一批次的扣减；
  SQLite 写事务以 `BEGIN IMMEDIATE` 开始并设置 `busy_timeout`，写操作全局串行。
- 复核撤销在单事务内锁定“复核批次 + 全部后代”后再重算隔离，与污染标记、生产投料
  并发时三方互斥于同一批行锁，结果等同某一串行顺序；隔离状态依据递归谱系实时重算，
  混料批次的另一疑似来源在子树之外也会被上溯识别，不会被错误解除。
- 幂等记录与业务数据在同一事务内落库，唯一约束 `(method, path, key)` 兜底并发首请求竞争。
