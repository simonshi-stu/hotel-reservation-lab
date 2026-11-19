# 14日只读房态与连续入住试算

启动原演示 `python -B -m hotel_demo.server --port 8019`，访问 `/availability`，或从主页“14日只读房态”进入。这是本地/云端开发演示界面，没有部署生产网站。

## 语义

- `GET /api/availability?mode=protected_upgrade&start=2026-12-07` 返回连续14日的单一SQLite快照。
- 每房型显示剩余、生效保护和配置保护。只有保护升级策略生效；保护线约束低房型升级，原生高档需求仍可消耗保护量。不是已锁房数量。
- 范围外是“无库存数据”，返回 available=false、空 types，不用0冒充已知库存。
- 快照时间、策略和有效日期范围明确展示。修改条件立即清除旧值；过时响应不会覆盖新条件。
- 试算使用内存分配器检查同房全程、床型、人数/儿童年龄、加床/婴儿床库存、升级授权及预算。读事务内读取当前库存，在独立内存对象试算，不写订单或锁定库存。
- 页面及API不返回客人记录、占用者ID、订单流水；成功试算可以返回拟分配房号。原主页保留其既有订单演示功能，不属于只读页面。
- 多个拒绝码代表候选检查障碍，不是唯一因果证明。新快照仍可能变化，成功试算不代表预订成功。

参数：mode/start；试算必须同时提供 check_in/check_out/room_type/bed/adults。可选 child_ages（逗号分隔0至17岁）、allow_upgrade/allow_extra_bed（true或false）、budget（分，默认100000）。未知、重复、空参数或无效格式返回400；不完整试算不默默忽略。

## 已执行验证

- Python 3.12：完整119项测试通过，无跳过；含6项新增路由/快照测试。
- 写入一笔合成预订后比较试算前后SQLite完整SQL导出，完全一致；响应没有占用者标识。
- 实际新建 week.json 合成库：14日窗口中10日有数据、4日明确未知。2成人、标准房/大床、12月7日至9日的快照试算结果见 `availability-evidence-20261119.json`。
- 新界面真实JS的4项DOM桩检查通过：未知日、过时响应、拒绝说明、失败清旧值。
- 原界面的22项DOM桩逻辑检查通过；新增JS语法检查通过。
- **真实浏览器视觉验收未完成**：云环境启动已安装Chromium时报 `socket() failed: Operation not permitted`；未调整安全设置。保留可在支持环境复跑的 `scripts/test_availability_browser.js`，不能把DOM桩测试称为浏览器验收。

```bash
python -B -m unittest discover -s tests -v
node scripts/test_availability_client.js
node scripts/test_demo_client.js
# 可选，需已安装 Playwright 与可启动 Chromium：
node scripts/test_availability_browser.js
```

本阶段独立基于main，不依赖PR #4回放CLI，不修改PR #3的712笔房型实验；没有融合两个分配内核。
