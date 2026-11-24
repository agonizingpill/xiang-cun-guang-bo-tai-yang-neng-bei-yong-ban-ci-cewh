# 乡村广播太阳能备用班次

岭声乡村广播的节目组和电源值守共用一张备播卡，在版权离线时段安排本地校时与蓄电余量。

## 现场工作与联系人

模拟客户为岭声乡村广播合作社。广播时刻表、蓄电日志、用量周期预测、备用机优先级；节目版权离线时段只能接本地时钟。

- 节目时段表 `airslots`，字段为 `title`（节目）、`hour`（起播小时）、`license`（节目源许可）、`offline`（离线时段）。模型约束位于 `backend/domain/airslots_model.py`，规则位于 `airslots_service.py`。
- 蓄电班记与测量 `battery_logs`，字段为 `meter`（电表编号）、`hour`（两日内整点）、`load_wh`（用量 Wh）、`clear`（天空晴）、`verified`（抄表审核）。模型约束位于 `backend/domain/battery_logs_model.py`，规则位于 `battery_logs_service.py`。
- 备用分钟核算 `reserves`，字段为 `airslot`（节目时段）、`plan_wh`（计划备用 Wh）、`fallback`（备用源优先级）、`mae`（时间验证MAE Wh）、`token`（计算号）。模型约束位于 `backend/domain/reserves_model.py`，规则位于 `reserves_service.py`。
- 班组备播卡 `switchcards`，字段为 `reserve`（备用评估）、`source`（实际时钟源）、`approved_wh`（锁定备用 Wh）、`state`（状态）。模型约束位于 `backend/domain/switchcards_model.py`，规则位于 `switchcards_service.py`。

团队为模拟的6 人广播合作社值守组，负责两个电源档位的备播。

值守员用节目表和电池抄数安排断电时段。周期负荷难从表格读出，漏掉一小时会扭曲拟合，离线版权段容易误选台外时钟。

节目时刻表和已签电量日志进入估计，接班人拿到有限额与时钟源的备用卡。

### 一天的业务操作

1. 07:00 确认 AIR-17 晚间村讯版权离线。
2. 08:00 班组登记 BAT-11 整点 88.4 Wh。
3. 09:00 核对 BAT-04 两日班记。
4. 11:20 用 24 小时 Fourier 基回测末 6 小时。
5. 15:00 签发 RV-24 备用电量评估。
6. 17:45 台外时钟请求被拦后改用本地时钟。
7. 22:10 持 WR-77 完成闭台交接。

## 启动

```sh
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```

打开 `http://127.0.0.1:18317`。FastAPI OpenAPI 在 `http://127.0.0.1:19317/docs`。
Compose 运行 Nginx、Python FastAPI、独立 Redis 消费进程、SQL 数据库和 Redis，SQL 与 Redis 使用命名卷。
本机开发时，在 Python 3.11 环境安装 `backend/requirements.txt` 后从 backend 目录运行 `uvicorn core.app:app`。须提供 `DATABASE_URL`、`REDIS_URL`、`JWT_SECRET`，`SEED_DEMO` 控制演示初始化。

| 演示账号 | 权限 |
|---|---|
| operator@demo.test | 提交领域记录和计算请求 |
| reviewer@demo.test | 提交记录并执行有业务约束的复核操作 |
| viewer@demo.test | 浏览业务台账 |

三个本地演示账号均使用 `DemoC0de!2026`。角色在 `backend/core/roles.py` 和 `frontend/src/store.js` 中声明。计算状态在 worker、queueing、API 和同一前端 store 中使用。

## 页面与接口

| 业务任务 | FastAPI 路由 | 页面 |
|---|---|---|
| 节目时段表 | `/airslots` /airslots/mark-offline | `frontend/src/pages/airslots.js` |
| 蓄电班记与测量 | `/battery_logs` /battery_logs/log-charge，/battery_logs/approve-meter | `frontend/src/pages/battery_logs.js` |
| 备用分钟核算 | `/reserves` /reserves/forecast-wh，/reserves/save-reserve | `frontend/src/pages/reserves.js` |
| 计算中断接手 | `/compute/{token}` /compute/{token}/takeover | `frontend/src/pages/reserves.js` |
| 班组备播卡 | `/switchcards` /switchcards/issue-switch，/switchcards/handover | `frontend/src/pages/switchcards.js` |

## 中断续跑与幂等

备用电量算到一半原机器断线时，凭原计算号即可接手，不需要重开评估。

- **沿用原计算号与审签读数**：提交时输入（节目时段、整点评签读数、晴天标记）已冻结进任务，接手只递增 `attempt`（时代号），`token` 与输入快照不变。
- **原机器结果不再采用**：worker 回写带时代栅栏（Redis `WATCH/MULTI` 比对 `status`+`attempt`）。接手一旦发生，`attempt` 自增，原机器随后交回的结果因时代号过期被静默丢弃，无法覆盖新任务。
- **断线重试不新增评估**：`forecast-wh` 按“提交人 + 输入指纹”做幂等键，同一输入重试直接返回原计算号（响应带 `reused: true`），不重复入队、不重复写审计。
- **已备案返回原单**：`save-reserve` 先按计算号查 `reserves`，命中即返回原记录（`reused: true`），不插入、不重复留痕；并发撞唯一约束时回查已落库原单。
- **崩溃自动续跑**：worker 启动时把 `processing` 中与当前时代号一致的遗留任务递增时代号重新入队，过期条目直接剔除。
- **页面可查可接手**：备用核算页可按计算号查询（含 `attempt`、`takeovers`、当前状态），中断后一键接手，算完即可备案。`running` 任务在 120 秒租期内受保护，超时（疑似僵死）才允许接手。
- **归属不变**：`submitter` 始终是原提交人，接手只更新本轮执行者 `owner`；提交人与复核员可查看/接手，只读访客无权。

SQL 实体与约束在 `backend/domain` 下维护。前端使用原生 ES modules。核心算子为 Fourier 基 + OLS。OLS 在训练时段拟合一、二阶谐波和晴天标记。备用量为截零后的点预测加末 6 小时 MAE。

按模拟广播班记推演。卡片供人工值守查阅，应用只处理纸本条件的数字记录。

[MIT](LICENSE)
