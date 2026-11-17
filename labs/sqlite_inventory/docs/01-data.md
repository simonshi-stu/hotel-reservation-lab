# 合成数据、字段与关联

**所有酒店、客户、订单和联系信息均为人工生成的模拟记录。** 名称带 SIMULATED，邮件使用 example.invalid；不存在真实住客、手机号、身份证或支付数据。CSV 使用 UTF-8 BOM，Excel 可直接打开；空字符串表示 SQL NULL。金额均为人民币分，日期 YYYY-MM-DD，时间为 UTC。

## 规模和构造

固定种子 20261104，Python 标准库 random.Random；日期从 2027-01-01 到 2027-03-01 共 60 晚，最后可退房日为 2027-03-02。

| 表 | 行数 | 含义 |
|---|---:|---|
| hotels | 3 | 上海、成都、杭州各一家模拟酒店 |
| room_types | 12 | 每店 Standard/Twin/Family/Suite 四类 |
| customers | 2,000 | 可复现的模拟客户 |
| nightly_inventory | 720 | 12 类房型 × 60 个营业日 |
| reservations | 1,192 | 成功创建的订单，含 205 笔已取消 |
| reservation_nights | 2,212 | 每笔订单的逐晚数量和价格快照 |
| inventory_events | 2,593 | 2,212 条扣房事件 + 381 条取消回补事件 |

生成器顺序发起 1,200 个合成预订请求，1,192 成功，8 个因部分日期无房被拒绝；成功订单中 205 笔取消，最终有效订单 987 笔。这是**数据构造过程**，不是并发吞吐量实验。拒单不创建订单行，因此订单表不包含那 8 个请求；数量详见 manifest.json。

| 因素 | 构造规则 |
|---|---|
| 每日容量 | 各店四类分别为 30、20、12、6 间；库存按天独立 |
| 基础价格 | 上海四类分别 350、420、580、880 元，成都加 20 元、杭州加 40 元 |
| 周末价格 | 周五、周六、周日为基础价格 120%，其余 100%，按整数分计算 |
| 客户城市 | 北京、上海、深圳、成都、武汉等概率抽样 |
| 客户分群 | business/leisure/family，抽样权重 35%/45%/20% |
| 热点房型 | H1-R1 权重 11，其余 11 类各权重 1，因此约 50% 请求集中至 H1-R1 |
| 入住日期 | 从前 54 天等概率抽取，为最长 5 晚预留余量 |
| 住宿晚数 | 1/2/3/5 晚，抽样权重 45%/30%/20%/5% |
| 每单房量 | 1/2/3 间，抽样权重 80%/17%/3% |
| 取消 | 每个成功订单有 18% 抽样机会取消；实际比例受随机样本影响 |

这是控制变量数据，**不代表现实酒店需求预测**。客户分群与购房行为未建模相关性；容量不含维修房、节假日和团房配额；固定创建时间按请求序号生成，取消时间与创建时间相同，旨在字节级复现而非模拟真实支付时序。订单 ID 在合成生成时由 seed 和序号哈希构造，正常 API 下单使用 UUID。manifest 保存每张 CSV 的 SHA-256，可检测数据变化；同一 Python/参数下，scripts.reproduce 会重建并逐文件核验。

## 字段字典

### hotels.csv

| 字段 | 含义 |
|---|---|
| id | 酒店主键 H1-H3 |
| name | 带 SIMULATED 标记的酒店名 |
| city | 城市英文标签 |

### room_types.csv

| 字段 | 含义 |
|---|---|
| id | 房型主键，如 H1-R1 |
| hotel_id | 外键 → hotels.id |
| name | 带 SIMULATED 标记的房型名 |
| base_price_cents | 基础每间每晚价格，分，正整数 |
| max_guests | 每间最大住客数，用作房型资料；本版不受理住客人数参数 |

### customers.csv

| 字段 | 含义 |
|---|---|
| id | 客户主键 C00001-C02000 |
| name | SIMULATED Guest + 序号 |
| city | 模拟客源城市 |
| segment | business / leisure / family |
| email | 唯一的 example.invalid 模拟邮箱 |

### nightly_inventory.csv

| 字段 | 含义 |
|---|---|
| room_type_id | 外键 → room_types.id；联合主键之一 |
| stay_date | 营业日；联合主键之一 |
| capacity | 该夜初始可售容量，非负 |
| available | 当前剩余量，数据库强制 0 ≤ available ≤ capacity |
| price_cents | 该日期、该房型的每间每晚价格 |

### reservations.csv

| 字段 | 含义 |
|---|---|
| id | 订单主键 |
| customer_id | 外键 → customers.id |
| room_type_id | 外键 → room_types.id |
| check_in | 入住日，包含 |
| check_out | 退房日，不包含 |
| quantity | 每晚预订间数；同一订单所有夜晚相同；1-10 |
| total_cents | 所有占用夜晚的价格之和 × quantity |
| status | CONFIRMED / CANCELLED |
| idempotency_key | 同一客户范围内唯一的请求键 |
| created_at | UTC 创建时间 |
| cancelled_at | UTC 取消时间；未取消为空 |

### reservation_nights.csv

| 字段 | 含义 |
|---|---|
| reservation_id | 外键 → reservations.id；联合主键之一 |
| room_type_id | 与 stay_date 联合外键 → nightly_inventory |
| stay_date | 实际占用夜；联合主键之一 |
| quantity | 该夜占用间数 |
| unit_price_cents | 下单时该夜每间价格快照 |

订单取消后保留逐晚记录，以便历史追溯；统计当前占用时必须连接 reservations 并筛选 CONFIRMED。

### inventory_events.csv

| 字段 | 含义 |
|---|---|
| id | 自增流水主键 |
| reservation_id | 外键 → reservations.id |
| room_type_id | 与 stay_date 联合外键 → nightly_inventory |
| stay_date | 库存发生变化的营业日 |
| kind | BOOK 扣房 / CANCEL 回补 |
| delta | BOOK 为 -quantity，CANCEL 为 +quantity |
| created_at | UTC 事件时间 |

(reservation_id, stay_date, kind) 唯一，限制同单同晚重复记录同类动作。事件表在应用中只追加，不开放修改接口；SQLite 文件所有者仍可直接改库，因此不能声称防篡改审计。

## 关联与示例

~~~mermaid
erDiagram
    hotels ||--|{ room_types : contains
    room_types ||--|{ nightly_inventory : dates
    customers ||--o{ reservations : books
    room_types ||--o{ reservations : selected
    reservations ||--|{ reservation_nights : occupies
    nightly_inventory ||--o{ reservation_nights : holds
    reservations ||--|{ inventory_events : audits
~~~

H1-R1 入住 1 月 1 日、退房 1 月 4 日、订 2 间：只占用 1、2、3 日，每天 available 扣 2；第 4 日不占。若第 2 日仅剩 1 间，整笔拒绝，1、3 日也不扣减。

查看和校验：打开 data/synthetic-v1 下 CSV；运行 scripts.reproduce 重建并核对哈希。正常服务只改运行库，CSV 不会自动更新。

