# 循环泵累计故障率核算台（Flask + SQLite）

面向维护班组的网页工具：上传每泵一行的台账 CSV，按累计运行小时计算
**故障累计发生率（Aalen–Johansen 竞争风险累积发生率，CIF）**，支持任意
小时查询、单泵记录更正；台账与汇总在同一 SQLite 事务内生效。

## 1. 计算口径（与需求逐条对应）

每条记录：泵号、入册累计运行小时、出册累计运行小时、结束方式
（`failure` 故障 / `preventive` 预防更换 / `censored` 观察结束）。

对每个不同的运行小时时点 t，严格按以下顺序处理：

1. **先计入新入册的泵**（延迟进入风险集）；
2. 用**当时在册数 n(t)** 计算两类事件的风险率：
   - 故障风险率 `h_f(t) = d_f(t) / n(t)`
   - 预防更换风险率 `h_p(t) = d_p(t) / n(t)`
3. **移除所有出册泵**（故障、预防更换、观察结束都退出风险集）。

累计故障率按**此前未发生两类事件的概率 S(t⁻) 加权累加**（预防更换为
竞争事件，观察结束只退出风险集、不带事件概率）：

```
S(t)   = Π_{u≤t} (1 − d_f(u)/n(u) − d_p(u)/n(u))
F_f(t) = Σ_{u≤t} S(u⁻) · d_f(u)/n(u)      # 累计故障发生率
F_p(t) = Σ_{u≤t} S(u⁻) · d_p(u)/n(u)      # 累计预防更换率
恒等式：S(t) + F_f(t) + F_p(t) = 1
```

任意小时查询取不晚于该小时的最近时点值（阶梯函数），并返回该时点的
逐时点计数（入册、在册 n、故障 d_f、预防 d_p、观察结束 d_c 等）。

## 2. 数据与事务保证

SQLite 表（见 `app/db.py`）：

| 表 | 内容 |
|---|---|
| `ledger` | 泵台账，泵号主键，含入/出册小时、结束方式、更新时间 |
| `life_table` | 汇总基准：每个时点一行，逐时点计数、风险率、S、F_f、F_p |
| `cif_base` | 改判过的时点的基准风险率与 S(t⁻)，供从基准精确重算修正量 |
| `cif_delta` | 当前后缀修正量（时序号 → ΔF_f、ΔF_p），用于整表展示 |
| `cif_tree` | 同一批修正量的 Fenwick（树状数组），供任意小时查询按 O(log M) 前缀求和 |
| `meta` | 泵总数、时点数、汇总更新时间、最近数据来源、schema 版本 |

- 上传 CSV = 整体替换台账；单泵更正 = upsert 一行。两条写路径都在
  **一个事务**内完成「写台账 → 更新汇总 → 写辅助修正量」，任何一步失败即
  `ROLLBACK`，**保留原有台账与汇总**（`app/services.py`）。
- 单泵更正只需提交该泵数据，服务端自行从库内加载数据重算，
  **无须读取/回传其他泵明细**。
- **纯改判快路径**：入册、出册小时都不变，只在本泵的 `故障 ↔ 预防更换`
  之间改判时，服务端只读取这一台泵的台账行，再读取该出册时点及其前一个
  时点的汇总行；风险集 n(t) 与两类事件合计不变，所以 `S(t)` 不变，只需把
  `S(t⁻)·(当前风险率 − 基准风险率)` 作为**常数后缀修正**记入 Fenwick 树
  （写入 O(log M) 行）。修正量始终相对最近一次全量重算的基准值重算，
  **改回原判据时精确复原**，不会累积浮点漂移。查询取「基准值 + 前缀和」，
  读取 1 条汇总行与 O(log M) 条辅助记录；首页明细按 `cif_delta` 顺序叠加，
  结果与全量重算逐位一致（浮点误差内）。
- **全量重算去重**：整表重传或其他字段更正会先清空 `cif_delta`/`cif_tree`，
  再以这次要保存的台账重建基准汇总；已计入的改判不会叠加到新基准上。
- **一致性快照**：查询在 WAL 读事务（`BEGIN`…`ROLLBACK`）内读取汇总与
  辅助数据，只会看到同一已提交状态；写路径使用 `BEGIN IMMEDIATE`。
- **旧库直接升级**：启动时按 `PRAGMA user_version` 就地补 `life_table.seq`、
  辅助表与 `time_point_count`，已有正常台账无须重传 CSV。
- 查询接口（`/query`、`/api/query`、`/api/lifetable`）只读汇总与辅助表，
  不执行任何写操作。

## 3. 目录结构

```
app/
  __init__.py     Flask 路由（上传、台账、更正/改判、查询、JSON API）
  lifetable.py    生命表/CIF 纯计算与校验
  csvio.py        CSV 解析（中英文表头、三种结束方式别名、逐行报错）
  db.py           SQLite 连接、建表与就地 schema 升级
  services.py     事务服务层（替换台账、单泵 upsert / 增量改判、只读查询）
  templates/      4 个 Jinja 页面
  static/style.css
tests/test_app.py 34 个 unittest 用例（数学、边界、CSV、事务回滚、增量与
                  全量一致性、同小时多台/改回、旧库升级、读写量对数增长、
                  查询快照、网页、只读性）
sample/pumps_sample.csv
Dockerfile, docker-compose.yml, requirements.txt, wsgi.py
```

## 4. 用 Docker Compose 启动

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

- 数据库在命名卷 `pump_data` 内（容器路径 `/app/instance/pumps.db`），
  重建容器数据不丢；健康检查 `GET /health`。
- 生产环境请通过环境变量 `FLASK_SECRET_KEY` 改密钥。

停止：`docker compose down`（保留数据卷）；连数据一起清除：
`docker compose down -v`。

## 5. 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python wsgi.py                      # 或 gunicorn wsgi:app
```

## 6. 网页试用流程

1. 首页「上传 CSV」选择 `sample/pumps_sample.csv`（或同结构文件，
   表头支持 `泵号/入册累计运行小时/出册累计运行小时/结束方式`，
   结束方式填 `故障/预防更换/观察结束`，也接受英文 failure/preventive/censored；
   编码支持 UTF-8 与 GBK）。
2. 首页查看逐时点计算明细表。
3. 「故障率查询」输入任意小时（如 1500、2000），得到累计故障率、
   累计预防率、无事件概率及该结果使用的逐时点计数。
4. 「台账」页可新增泵或对单台泵做更正；对「故障 / 预防更换」的泵还可点
   **「改为预防更换 / 改为故障」**一键改判（入/出册小时不变），保存后汇总
   立即按原口径更新，首页明细、查询页和 JSON API 同步反映。
   改判不叠加：再次点同一按钮即改回；整表重传后按新台账重算。

JSON 接口：

```bash
curl 'http://localhost:8000/api/query?hours=2000'
curl 'http://localhost:8000/api/lifetable'
```

## 7. 自测

```bash
python -I -m unittest discover -s tests -v     # 34 个用例应全部 OK
```

## 8. 示例数据的手算基准（sample CSV，6 台泵）

| t | 入册 | n(t) | d_f | d_p | d_c | S(t) | F_f(t) | F_p(t) |
|---|---|---|---|---|---|---|---|---|
| 0 | 4 | 4 | 0 | 0 | 0 | 1.0000 | 0.0000 | 0.0000 |
| 500 | 1 | 5 | 0 | 0 | 0 | 1.0000 | 0.0000 | 0.0000 |
| 1000 | 1 | 6 | 1 | 1 | 0 | 0.6667 | 0.1667 | 0.1667 |
| 2000 | 0 | 4 | 1 | 0 | 1 | 0.5000 | 0.3333 | 0.1667 |
| 3000 | 0 | 2 | 0 | 1 | 1 | 0.2500 | 0.3333 | 0.4167 |

末时点 `S + F_f + F_p = 0.25 + 0.3333 + 0.4167 = 1`。
