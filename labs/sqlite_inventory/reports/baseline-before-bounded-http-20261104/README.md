# 酒店预订并发实验室

这是位于 **E:\Projects\hotel-reservation-lab** 的独立后端项目，使用**明确标注的合成数据**模拟酒店预订。没有修改电商项目，没有安装软件、创建账户或推送代码。

已实现真实 SQLite 文件数据库上的日期房态、原子预订、防超卖、客户范围幂等、取消回补、库存流水对账和本机 HTTP 接口。无需第三方依赖。**尚未使用 PostgreSQL；当前验证不能视为 PostgreSQL 或生产环境承载验证。**

## 从这里开始

- 查看数据：[合成数据目录](data/synthetic-v1)、[生成清单](data/synthetic-v1/manifest.json)。
- 理解字段和分布：[数据字典与构造说明](docs/01-data.md)。
- 理解并发实现与面试讲解：[事务与并发设计](docs/02-design.md)。
- 复现实验、查看真实结果：[场景及验证边界](docs/03-verification.md)、[原始报告](reports)。

## 启动

在 PowerShell 中运行，所有项目输出和临时目录均由启动脚本放在当前 E 盘项目内。使用已核验的现有 Python 3.10.4 基础解释器，仅导入标准库；不会使用或修改电商项目的虚拟环境。

~~~powershell
Set-Location -LiteralPath 'E:\Projects\hotel-reservation-lab'
# 数据库已生成，无需重新 seed。
.\scripts\run.ps1 hotel.api --db .runtime/hotel.db --port 8091
~~~

打开 http://127.0.0.1:8091/health 可查看状态；服务仅监听本机。Ctrl+C 停止。本项目是后端实验，没有管理网页或 Swagger。

如解释器路径不同，先设置当前进程的 `$env:HOTEL_PYTHON` 为已安装的 Python 3.10+ 路径；脚本不会安装 Python。运行脚本会把**当前 PowerShell 进程**的工作目录和 TEMP/TMP 等切到项目目录，不修改全局系统设置。

另开 PowerShell 测试一笔：

~~~powershell
Set-Location -LiteralPath 'E:\Projects\hotel-reservation-lab'
$booking = @{
    customer_id='C00001'; room_type_id='H1-R2'
    check_in='2027-02-20'; check_out='2027-02-23'
    quantity=1; idempotency_key='manual-demo-001'
} | ConvertTo-Json
$order = Invoke-RestMethod 'http://127.0.0.1:8091/reservations' -Method Post -ContentType 'application/json' -Body $booking
$order
# 同请求重试返回同一订单；改变数量且保留键会返回 409。
Invoke-RestMethod 'http://127.0.0.1:8091/reservations' -Method Post -ContentType 'application/json' -Body $booking
Invoke-RestMethod ("http://127.0.0.1:8091/reservations/" + $order.id + "/cancel") -Method Post -ContentType 'application/json' -Body '{"customer_id":"C00001"}'
~~~

API 使用客户 ID 演示业务归属检查，**没有真实身份认证**，不能部署为公网酒店服务。手工操作会改变运行库；已交付 CSV 是初始快照。读取房态后可能被别人抢走，最终以预订事务结果为准。

## 复现

~~~powershell
Set-Location -LiteralPath 'E:\Projects\hotel-reservation-lab'
.\scripts\run.ps1 scripts.verify
.\scripts\run.ps1 scripts.load
.\scripts\run.ps1 scripts.reproduce
~~~

功能测试使用独立临时库；压测每次创建独立库和带时间戳报告，均不操作初始演示库。复现脚本重新生成完整数据，核对七张 CSV 的 SHA-256 和清单。

重新生成演示数据必须指定新路径，工具拒绝覆盖已有文件：

~~~powershell
.\scripts\run.ps1 scripts.seed --db .runtime/hotel-v2.db --output data/synthetic-v2 --seed 20261104 --customers 2000 --attempts 1200 --days 60
~~~

日期范围固定为 2027-01-01 起 60 天，方便未来重复实验；不采用实际当天日期。价格单位为人民币分。项目不接真实支付、个人信息、短信或外部酒店系统。

## 代码阅读顺序

`hotel/schema.sql` → `hotel/service.py` → `hotel/api.py` → `scripts/seed.py` → `tests/test_booking.py` → `scripts/load.py`。

