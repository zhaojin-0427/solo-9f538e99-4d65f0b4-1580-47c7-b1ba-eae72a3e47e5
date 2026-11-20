# 考古地层对接与浮选取样 API（FastAPI + SQLite）

管理同一考古遗址最多 40 个层位的探方编号、层位编号与可能沉积年代段（整年闭区间，
可含空档），登记跨探方层位对接与“下层 → 上层”叠压关系，并自动完成：

- **分组**：直接或间接对接的层位视为同一次沉积，合并为连通组，全组共用一个年份；
- **成环检测**：合组后的叠压关系（含同组自环）不得成环；
- **年代约束**：下层沉积年严格早于上层；同组年份必须落在各成员年代段的交集内；
- **可行年代段求解**：对每个组给出可与其他全部层位约束同时成立的所有年份
  （保留空档的闭区间列表）；
- **撤销重算**：删除对接/叠压后按剩余关系重新分组、重新求解。

同一站点内还管理浮选取样：按遗址登记土袋（袋号、出土层位、原始体积毫升），
登记由一袋或多袋组成的浮选批（各袋取用毫升数 + 籽粒总数），跟踪余量，并按
**当前地层组**统计总粒数、用土毫升数与每升粒数。

## 快速开始（docker compose）

```bash
docker compose up --build
# API:      http://localhost:8000
# Swagger:  http://localhost:8000/docs   （可直接在页面上录入数据）
# ReDoc:    http://localhost:8000/redoc
```

SQLite 数据文件保存在命名卷 `archaeo-data`（容器内 `/data/archaeo.db`），
`docker compose down` 不会删除；如需清空：`docker compose down -v`。

## 本地裸跑

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库默认在 ./data/archaeo.db，可用环境变量 DB_PATH 覆盖
```

## 年代表示约定

- 年份均为整数；公元后用正整数，公元前用负整数（如 `-221` 表示公元前 221 年）。
- 每个层位给一个或多个闭区间 `[year_from, year_to]`，多段之间允许空档
  （如 `[-5000,-4000]` 与 `[-3000,-2000]`，中间一千年无沉积可能）。
- 同组年代域 = 组内各层位区间的交集；求解结果 `feasible_years` 同样是带空档的区间列表。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sites` | 建遗址 |
| GET | `/sites` / `/sites/{id}` | 遗址列表/详情 |
| POST | `/sites/{id}/strata` | 录入层位（探方、层位号、年代段；每遗址上限 40） |
| GET | `/sites/{id}/strata` / `/strata/{id}` | 层位查询 |
| POST | `/sites/{id}/correlations` | 登记两探方层位对接 |
| GET | `/sites/{id}/correlations` | 对接列表 |
| DELETE | `/correlations/{id}` | 撤销对接，按剩余对接重算 |
| POST | `/sites/{id}/superpositions` | 登记叠压（下层 → 上层） |
| GET | `/sites/{id}/superpositions` | 叠压列表 |
| DELETE | `/superpositions/{id}` | 撤销叠压并重算 |
| GET | `/sites/{id}/solution` | 整站分组、组间边、各组全部可行年代段 |
| GET | `/strata/{id}/correlation-view` | 按原始层位查同组成员与可行年代段 |
| POST | `/sites/{id}/bags` | 登记土袋（袋号、出土层位、原始体积毫升） |
| GET | `/sites/{id}/bags` / `/bags/{id}` | 土袋列表/详情（原始体积、累计取用、余量） |
| POST | `/sites/{id}/flotations` | 登记浮选批（批号、各袋取用毫升、籽粒总数） |
| GET | `/sites/{id}/flotations` / `/flotations/{id}` | 浮选批查询 |
| DELETE | `/flotations/{id}` | 撤销浮选批，取用量退回土袋 |
| GET | `/sites/{id}/flotation-report` | 按地层组统计粒数、用土量与每升粒数 |

### 录入示例

```bash
SID=1
# 层位：探方 T1 第 3 层，两个可能年代段（中间有空档）
curl -X POST localhost:8000/sites/$SID/strata -H 'Content-Type: application/json' -d '{
  "trench": "T1", "layer_no": "L3",
  "intervals": [{"year_from": -5000, "year_to": -4000},
                {"year_from": -3000, "year_to": -2000}]
}'
# 叠压：下层 1 -> 上层 2
curl -X POST localhost:8000/sites/$SID/superpositions \
  -H 'Content-Type: application/json' -d '{"lower_id": 1, "upper_id": 2}'
# 跨探方对接：层位 1 与层位 3 属同一次沉积
curl -X POST localhost:8000/sites/$SID/correlations \
  -H 'Content-Type: application/json' -d '{"stratum_a_id": 1, "stratum_b_id": 3}'
```

## 浮选取样、土袋余量与密度统计

### 概念

- **土袋**：在遗址下登记，袋号在遗址内唯一，标明出土层位（层位 id，或
  探方 + 层位号）与原始体积（毫升）。
- **浮选批**：一个批号可由一袋或多袋土组成；每袋只登记本批实际取用的毫升数
  （可小于原始体积），另给整批筛出的籽粒总数。
- 同一袋土可以分多次取用（分属不同批）；同一批也可以合并若干袋土。
- **地层组**：不是原始层位，而是对接关系形成的连通组。撤销/新增对接后，
  下一次查询立即按新分组判定；分组与浮选记录总取自同一个已提交状态
  （同一把写锁、同一 SQLite 连接、同一版本号）。

### 规则

1. **容量**：任一批登记后，每袋的累计取用量（跨全部批）不得超过原始体积，
   否则 `409`；被拒绝的登记不产生任何写入。
2. **幂等重发**：同一遗址内批号唯一。原样重发（同批号、同各袋取用量、同籽粒
   总数，明细顺序无关）返回已登记的批，`idempotent: true`，**不重复计量**、
   不产生新版本；同批号但内容不同则 `409` 并返回已登记的批。
3. **余量**：登记成功当次即返回所用各袋的 `used_ml` / `remaining_ml`。
4. **撤销**：删除浮选批后，各袋取用量退回，统计与余量按剩余记录重算；
   批号可再次使用。
5. **失败安全**：登记失败时余量与统计保持原样；所有数据落库，重启后仍可查询。

### 统计口径（`GET /sites/{id}/flotation-report`）

按当前地层组逐个给出：

| 字段 | 含义 |
| --- | --- |
| `stratum_ids` / `strata` | 该组包含的层位及探方/层号 |
| `bag_ids` / `bag_count` | 出土层位属于该组的土袋 |
| `volume_ml` | 该组土袋的原始体积合计 |
| `total_seed_count` | 计入本组的籽粒总数 |
| `used_ml` | 本组土袋的**全部**取用量（含跨组批次里的用量） |
| `density_used_ml` | 纳入密度统计的用土量（仅整批同组的批次） |
| `seeds_per_liter` | `total_seed_count / (density_used_ml / 1000)`；分母为 0 时为 `null` |
| `batches` | 整批土均来自本组的批次 |
| `mixed_batches` | 涉及本组但整批跨多个组的批次（仅列批号与涉及的组） |

**一批土来自多个地层组时**：整批都不纳入密度统计（不给任何组贡献籽粒数，
其用土量也不进 `density_used_ml` 的分母），但各袋的取用量照常计入所在组的
`used_ml`（耗用）。这类批次在报告的顶层 `mixed_batches` 中列出批号及涉及的
组，并同时出现在相关各组的 `mixed_batches` 里。组被对接合并后，原来的跨组批
可能变成整组批，下一次查询即按新分组重新判定。

### 录入示例

```bash
SID=1
# 土袋：袋号 B-01，出土层位 T1/L3，原始体积 1000 毫升
curl -X POST localhost:8000/sites/$SID/bags -H 'Content-Type: application/json' -d '{
  "bag_no": "B-01", "trench": "T1", "layer_no": "L3", "volume_ml": 1000
}'
# 浮选批：两袋土合成一批，各取 300 / 200 毫升，籽粒总数 57
curl -X POST localhost:8000/sites/$SID/flotations -H 'Content-Type: application/json' -d '{
  "batch_no": "F-2024-01", "seed_count": 57,
  "items": [{"bag_no": "B-01", "used_ml": 300},
            {"bag_no": "B-02", "used_ml": 200}]
}'
# 按地层组统计
curl localhost:8000/sites/$SID/flotation-report
```

## 无解时的行为

写入会在**同一事务**内先重算再提交。若新记录导致无解（`EMPTY_DOMAIN` /
`GROUP_CYCLE` / `ORDER_CONFLICT`），事务回滚，接口返回 `409`，响应体包含：

- `contradiction`：一组本身即矛盾的**原始记录见证**（层位年代段、对接、叠压，
  含可读原因与记录 id，便于定位）；
- `previous_solution`：回滚后库内已有对接与年代结论（保持可行、保持可用）。

## 一致性与并发

- 所有写操作经进程内同一把锁串行化，按**提交顺序**核算；每次成功写入使全局
  `version` 加 1，查询结果带 `version`，可据此确认读到的是哪一次已完成提交。
- 分组与年代结论**不物化落库**，每次查询及重启都由原始记录现场求解；
  原始记录由 SQLite 事务保证原子性，因此任何查询看到的分组/年代都来自同一次
  完整提交，不存在半写入的中间态。
- SQLite 启用 WAL 与外键约束，连接带 30s busy timeout。

## 求解算法（`app/solver.py`）

1. 并查集按对接关系求连通组；组域为成员年代域交集（支持空档的区间运算）。
2. 叠压边提升为组间边；Kahn 拓扑排序判环，DFS 三色标记提取具体环与见证。
3. DAG 上两次贪心：
   - 最早解 E：拓扑序每组取域内 `> max(E[前驱])` 的最小年份；
   - 最晚解 L：逆拓扑序每组取域内 `< min(L[后继])` 的最大年份。
   任意可行解 F 都满足 E ≤ F ≤ L，故组 g 的全部可同时成立年份恰为
   其年代域 ∩ [E[g], L[g]]（仍可为含空档的多段区间）。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖区间工具、缺口域求解、成环/空域/次序冲突及矛盾见证、40 层上限、
无解拒绝与旧结论保留、撤销重算、并发写入串行化、重启后结论恢复；
浮选部分覆盖分批取用余量、容量上限与并发超量拒绝、原样重发幂等、
同批号改内容冲突、失败不改余量、撤销批次、跨组批次剔除密度统计、
对接分组变化后统计即时更新、重启后统计与幂等判定恢复。
