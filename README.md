# 酒店分房实验台

一个可在本机运行的酒店分房与 SQLite 事务演示。使用合成数据比较「严格房型」「允许升级」「保护库存升级」三种策略，并展示预订、取消、幂等重试、故障回滚和旧轮次结果核实。

**仅用于学习和实验，不连接真实酒店库存，不处理真实客人信息或支付。**

## 快速启动

需要 Python 3.11+，无需安装第三方 Python 包或数据库服务。在项目根目录运行：

```bash
python -m hotel_demo.server --port 8019
```

在同一台电脑的现代浏览器打开 http://127.0.0.1:8019/ ，按 Ctrl+C 停止服务。不要直接打开 HTML 文件。

页面包含 8 间模拟客房、10 晚库存、儿童与床型限制、婴儿床和加床资源。三种策略使用独立库存。可先点击「填入带婴儿示例」并预订，再尝试原请求键重放、取消或故障注入，观察事务回执与库存变化。

默认数据写入 `.hotel-demo-state/`，重启后保留。「新开一轮」创建新库存并归档旧轮次，不删除旧数据库。可用 `--state-dir <专用新目录>` 指定存储位置，勿指向生产数据。结果未确认时，应保留原请求键核实，避免另建重复订单。

## 测试

Python 测试包括分房规则、SQLite 持久化、HTTP 接口和跨轮次恢复核实：

```bash
python -m unittest discover -s tests -v
python scripts/sqlite_demo.py
python scripts/audit_invariants.py
python scripts/audit_sqlite.py
python scripts/audit_sqlite_crashes.py
python scripts/audit_sqlite_state_machine.py
```

可选的前端逻辑测试需要 Node.js 18+，启动演示不需要 Node：

```bash
node --check hotel_demo/static/app.js
node scripts/test_demo_client.js
```

前端测试使用模拟 DOM 和 HTTP 响应，不等同于真实浏览器验收。另附 `scripts/test_recovery_browser.py`，需要单独安装 Playwright 和 Chromium，并通过 `CHROMIUM_PATH` 指定 Chromium 可执行文件；它不属于默认测试，未完成跨浏览器验收。

批量比较三种分房策略：

```bash
python -m hotel_sim --input data/week.json --output results/week.json
```

部分审计脚本将报告写入 `results/`；生成结果和数据库不纳入源码仓库。

## HTTP 并发实验

新增独立临时服务的分级并发脚本：

```bash
python scripts/benchmark_http.py
```

报告吞吐、p95/p99、传输错误及预订/取消后的库存核对。[方法与实测结果](docs/HTTP_BENCHMARK.md)。这是合成短时本机实验，不是生产容量或抗攻击证明。

## 项目结构

- `hotel_demo/`：本机 HTTP 服务、恢复查询和原生 HTML/CSS/JavaScript 界面。
- `hotel_sim/`：分房引擎、SQLite 事务与命令行工具。
- `data/`：可复现的合成场景，字段口径见 [data/README.md](data/README.md)。
- `tests/`：Python 自动化测试。
- `scripts/`：数据生成、独立审计和前端检查。

## 使用边界

- 整段入住分配同一间实体房；入住日计入、离店日不计入。价格、儿童政策与需求量均为模拟参数。
- Web 操作在单个服务进程内串行执行。同一数据目录不要同时运行多个服务或 CLI 写入。
- SQLite 并发与故障测试不能证明 PostgreSQL、真实酒店流量或生产性能。
- 默认只监听本机。没有登录、权限管理、TLS、支付或渠道库存同步；Python 标准库 HTTP 服务不适合公网生产部署。不要开放到公共网络，也不要输入真实客人数据。
- 旧轮次查询只读取原记录，不自动补单或修库。未找到记录不能证明请求从未执行；缺失或损坏数据库仍须保留未确认状态。
