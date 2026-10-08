# 电商数据工作台数据设计

## 1. 设计目标

服务两个目标：

1. 生意参谋、无界报表长期可靠存储。
2. 支持每日和每周数据看板。

设计原则：原始数据只读、导入批次可追溯、重复上传不静默覆盖、指标计算有版本、跨日指标按指标类型选择正确聚合方式。

## 2. 最小存储分层

```text
原始文件层：保存用户上传的原始 XLS/CSV 和文件指纹
导入批次层：记录来源、店铺、业务日期、粒度、状态和版本
原始行层：保留解析后的每一行及原始字段快照
标准事实层：按日、店铺、商品、计划保存可聚合数值
指标定义层：保存字段映射、公式、聚合方式和版本
看板查询层：按日/周实时从事实层聚合，或保存带版本的快照
```

## 3. 推荐最小数据库

第一版可使用 SQLite，原始大表和后续分析量增大后再评估 DuckDB。SQLite 负责元数据、批次、规则和事实表，原始文件放在本地受控目录，不把二进制文件塞进数据库。

建议目录：

```text
data/
├── workbench.sqlite3
└── raw/
    └── {store_id}/{source_type}/{business_date}/{batch_id}/original.ext
```

## 4. 核心表

### 4.0 accounts

账号是店铺报表的归属主键。生意参谋库和无界库分别保存数据，但两者通过同一个 `account_id` 组成同一账号/店铺的综合看板。

| 字段 | 说明 |
|---|---|
| id | 稳定账号主键 |
| account_name | 账号展示名称，唯一 |
| store_name | 对应店铺名称 |
| created_at | 创建时间 |

上传、看板查询和上传记录均应使用 `account_id`，不能仅依赖可变的店铺名称。历史数据迁移时按原 `store_name` 自动创建同名账号并回填关联。

### 4.1 stores

| 字段 | 说明 |
|---|---|
| id | 内部稳定主键 |
| store_name | 店铺名称 |
| platform | 淘宝/天猫等 |
| platform_store_id | 平台店铺 ID，若可取得 |
| status | active/inactive |
| created_at/updated_at | 时间 |

### 4.2 source_files

| 字段 | 说明 |
|---|---|
| id | 文件主键 |
| store_id | 归属店铺 |
| source_type | 生意参谋/无界计划/无界商品/无界内容 |
| original_name | 原始文件名 |
| storage_path | 原始文件路径 |
| sha256 | 文件指纹，识别完全重复上传 |
| file_size | 文件大小 |
| mime_type | 文件类型 |
| created_at | 保存时间 |

### 4.3 import_batches

| 字段 | 说明 |
|---|---|
| id | 批次主键 |
| store_id | 店铺 |
| source_file_id | 原始文件 |
| source_type | 来源 |
| business_date_start | 文件覆盖开始日期 |
| business_date_end | 文件覆盖结束日期 |
| report_grain | daily/weekly/unknown |
| schema_version | 识别的表结构版本 |
| calculation_version | 使用的指标规则版本 |
| status | pending/succeeded/partial/failed |
| row_count | 解析行数 |
| error_count | 错误数 |
| imported_at | 导入时间 |

批次不能被静默删除或覆盖。重新上传应形成新批次，并通过唯一业务键和状态标识哪个版本生效。

### 4.4 import_errors

记录批次、行号、字段、原始值、错误类型、错误消息和处理状态。

### 4.5 products

保存店铺内商品身份：`store_id`、`product_id`、商品名称、主商品 ID、状态、首次/最近出现日期。商品 ID 是历史关联主键，商品标题不能作为唯一键。

### 4.6 plans

保存无界计划身份：`store_id`、`plan_id`、计划名称、场景 ID、场景名称、状态、首次/最近出现日期。

### 4.7 raw_rows

建议保存每个导入行的原始字段 JSON，同时保留定位字段：

| 字段 | 说明 |
|---|---|
| id | 主键 |
| batch_id | 导入批次 |
| row_number | 原始行号 |
| business_date | 业务日期 |
| entity_type | product/plan/other |
| entity_id | 商品 ID 或计划 ID |
| raw_payload | 原始字段 JSON |
| row_hash | 行指纹 |

### 4.8 daily_product_facts

生意参谋商品日报的标准事实表，至少保存：

`store_id`、`business_date`、`product_id`、`batch_id`、`visitors`、`paid_buyers`、`paid_units`、`gmv`、`successful_refund_amount`、原始商品支付转化率、质量状态。

唯一性建议：`store_id + business_date + product_id + effective_batch_id`。同一业务键允许多个导入版本，但查询默认只使用当前有效版本。

### 4.9 daily_plan_facts

无界计划日报至少保存：

`store_id`、`business_date`、`plan_id`、`batch_id`、`spend`、`clicks`、`direct_deal_amount`、`indirect_deal_amount`、`total_deal_amount`、原始投入产出比、场景信息、质量状态。

### 4.10 metric_definitions

保存 `metric_code`、展示名、来源字段、公式、分子、分母、聚合方法、单位、版本、生效时间和确认状态。

## 5. 已确认指标口径

### 商品/店铺/系列

```text
GMV = SUM(支付金额)
GSV = SUM(支付金额) - SUM(成功退款金额)
退款率 = SUM(成功退款金额) / SUM(支付金额)
商品层面转化率 = 生意参谋商品支付转化率
店铺/系列转化率 = SUM(支付买家数) / SUM(商品访客数)
客单价 = SUM(支付金额) / SUM(支付件数)
推广费比 = SUM(花费) / GMV
```

### 投产比

```text
计划层面：保留无界原始投入产出比
店铺/系列层面：SUM(总成交金额) / SUM(花费)
```

不能把计划级投产比相加或简单平均。

## 6. 日报与周报聚合规则

### 可加总指标

在确认记录去重和版本有效性后，可按日求和：

- GMV
- 成功退款金额
- 支付件数
- 推广花费
- 推广点击量
- 直接成交金额
- 间接成交金额
- 总成交金额

### 必须重新计算的比率

- 退款率：周成功退款金额 / 周 GMV。
- 店铺/系列转化率：周支付买家数 / 周访客数。
- 客单价：周 GMV / 周支付件数。
- 店铺/系列投产比：周总成交金额 / 周花费。
- 推广费比：周花费 / 周 GMV。
- 点击单价：周花费 / 周点击量。

### 不能简单跨日累加的周期去重指标

当前样本中的“商品访客数”和“支付买家数”是日统计数字。若没有用户级明细或平台提供的周期去重字段，不能声称周访客数/周支付买家数可以通过日数相加得到去重结果。

周报必须先确定以下方案之一：

1. 使用官方周报中的周期去重字段。
2. 使用可去重的用户级/访客级明细。
3. 明确把结果标记为“日累计口径”，不称为周期去重访客/买家。

在方案确认前，周转化率不得用日访客和日支付买家简单累加后冒充官方周期口径。

## 7. 重复上传与版本建议

- 文件完全相同：用 SHA-256 识别，提示“已存在”，默认不重复导入。
- 文件不同但业务键相同：新建批次，进入冲突状态，不静默覆盖。
- 用户确认替换：旧批次保留为历史版本，新批次成为有效版本。
- 部分日期重叠：按日期和实体键生成冲突清单，允许用户选择有效批次。
- 查询结果必须带 `effective_batch_id` 和 `calculation_version`。

## 8. 原始数据不可变要求

原始文件和 `raw_rows` 只允许新增，不允许页面编辑覆盖。字段映射、清洗和指标结果都应通过新版本生成，便于回溯和重算。
