# 项目背景

更新日期：2026-10-09（Asia/Shanghai）

## 任务 03：公共数据组模块

- 指标注册信息集中在 `app.py` 的 `METRIC_REGISTRY`，并同步写入统一库 `metric_definitions`；看板不得再维护重复指标字典。
- 账号库新增 `data_groups`、`data_group_items`、`data_group_preferences`。系统组包括成交、流量、互动、推广四组；自定义组按账号保存，当前选择按账号和看板保存。
- 公共接口为 `GET /api/data-groups`、`POST /api/data-groups`、`POST /api/data-groups/select`、`POST /api/data-groups/delete`。
- 数据组只改变现有看板指标卡的选择与顺序，不计算业务数据。宝贝、计划和经营看板已接入；系列看板尚无页面和数据接口，暂不显示。
- 计划看板对经营指标返回 `available=false`；宝贝看板对不存在的指标同样不显示替代口径。

## 技术栈与运行方式

- 后端：`app.py`，Python 标准库 `http.server`、`sqlite3`、`csv`。
- 前端：`static/index.html`，原生 HTML/CSS/JavaScript，无构建步骤。
- XLS 解析：调用 `soffice` 转换后读取；CSV 尝试 UTF-8、GB18030、UTF-16。
- 本地启动：`python3 app.py`，默认 `http://127.0.0.1:8765`。
- 容器部署配置：`Dockerfile` 安装 LibreOffice，默认监听 `0.0.0.0:8080`，数据目录为 `/data`；`railway.json` 配置健康检查 `/api/health`。本文件不代表线上已验证。

## 数据架构

- `data/accounts.sqlite3`：账号/店铺、用户、会话、MTD 自定义目标。
- `data/shengyicanmou.sqlite3`：生意参谋上传记录及文件级结果 JSON。
- `data/wujie.sqlite3`：无界上传记录及文件级结果 JSON。
- `data/workbench.sqlite3`：V2.0 文件、批次、原始行、商品、计划、标准事实和指标版本。
- `data/raw/{account_id}/{source_type}/{date}/`：新导入原文件；历史目录保持原位。数据库和原始经营数据不纳入 Git。

## 主要模块

- 初始化与迁移：`init_databases()`。
- 店铺：`list_accounts()`、`create_account()`、`/api/accounts`，前端首页和上传页均可新建/切换店铺。
- 上传：`/api/import`，支持生意参谋商品日报 XLS、无界商品报表 CSV；兼容 UTF-8 BOM、UTF-8、GB18030/GBK 子集、UTF-16，按账号保存并用 SHA-256 识别完全重复文件。旧无界记录仍可查询，但旧计划报表不能作为新版商品事实导入。
- 上传记录：`/api/upload-records` 查询，`/api/imports/delete` 删除账号范围内记录及无引用原文件。
- 看板：`/api/dashboard`，按账号和最多 31 天日期范围读取每个日期最新导入记录。
- 目标：`TARGET_DEFINITIONS`、`/api/targets`，按账号和月份保存自定义 MTD 目标。
- 前端：顶部“首页/经营看板/数据上传”导航；首页店铺卡片切换当前店铺。

## 功能状态

### 已实现但本轮未重新验证

- 账号登录、店铺创建、两类样本上传、统一查询和旧日期范围看板已在临时 `DATA_ROOT` 端到端验证；店铺切换、上传记录删除和 MTD 目标编辑未在本轮浏览器逐项验证。
- 转化率代码口径为“成交买家数 / 访客数 × 100%”，兼容成交买家数量、成交买家数、支付买家数字段。
- 新无界商品报表以 `商品报表` CSV 为当前格式基准，新增解析展现量、点击率、成交笔数、成交人数、购物车数等字段；历史无界记录不迁移。
- 生意参谋与无界数据按同一账号/店铺合并展示。

### 已验证

- `python3 -m unittest discover -s tests -v`：8 项通过；测试使用脱敏合成无界样本，不提交真实经营报表。
- 新无界样本：GB18030、79 列、16 行；花费 1415.07、归因成交 3618.99、ROI 2.56。
- 生意参谋 XLS 样本可解析并持久化，商品 ID 保持字符串。
- 重复文件、同日冲突、账号隔离、非法编码、科学计数法 ID、混合日期、多计划/多商品关系均有回归测试。
- 临时服务验证登录、建店、两类上传、`/api/dashboard` 和统一查询接口。

### 尚未验证或尚未完成

- 冲突批次人工确认/替换接口、用户与店铺授权关系、备份恢复、真实浏览器交互回归。
- 正式周报、官方周期去重访客/成交买家、归因窗口用户选择和完整指标字典管理界面。
- Railway 持久卷、线上导入、线上重启恢复和 LibreOffice 云端实际可用性。

## V2.0 统一数据底座（2026-10-09）

- 新增 `data/workbench.sqlite3`，不替换现有 `shengyicanmou.sqlite3`、`wujie.sqlite3`。
- 统一底座包含 `source_files`、`import_batches`、`import_errors`、`raw_rows`、`products`、`plans`、`daily_product_facts`、`daily_plan_product_facts`、`metric_definitions`。
- 无界新版样本已验证为 GB18030、79 列、16 行、业务日期 2026-10-08；主体 ID 等同商品 ID。
- 无界事实粒度是日期、场景、计划、商品的组合，不能只按计划 ID 建唯一键。
- 新增查询接口：`/api/unified/products`、`plans`、`plan-totals`、`metrics`、`batches`、`linked`；只读取有效批次，冲突批次保留但不参与查询。
- 当前仍未实现用户确认后的批次替换流程；冲突文件先保留为非有效批次。

## 宝贝看板（任务 01）

- 新增 `/api/baby-products` 商品搜索和 `/api/baby-board` 商品经营/推广联合查询，仍只读取统一底座有效批次。
- `daily_product_facts` 增量添加 `page_views`、`cart_people`、`cart_items` 可空字段；不重建数据库，旧批次保持可读。
- 前端新增宝贝看板导航页，包含商品搜索、日/自然周/自然月/自定义日期、经营指标、推广指标、趋势和计划明细；数据组仅保留禁用接入位。

## 计划看板（任务 02）

- 新增`/api/plan-board/options`：按账号和日期范围返回有效事实中的场景名字、计划和商品选项。
- 新增`/api/plan-board`：支持商品 ID、场景名字、计划 ID、计划名字和日期范围组合筛选，返回推广汇总、按日趋势、跨日计划汇总、逐商品计划明细和质量状态。
- 前端新增计划看板导航页，包含紧凑筛选栏、推广指标卡、可切换消耗/点击/归因成交/ROI 趋势、可排序明细表和数据组预留位。
- 计划看板与宝贝看板复用`promotion_metrics()`；店铺切换清空计划页状态，避免展示上一店铺数据。

## Git 现场

- 分支：`main`；任务 00 统一数据底座已在本地提交，任务 01 收尾时用户明确授权创建本地提交。
- 任务 01 修改范围：`app.py`、`static/index.html`、`tests/test_unified_data.py`、`HANDOFF.md`、`PROJECT_CONTEXT.md`、`DATA_RULES.md`。
- 任务 02 修改范围相同，未新增数据库迁移；用户已授权完成收尾检查后创建本地提交，禁止推送或部署。
- 本任务不允许推送或部署；数据库、原始报表和临时验收数据不得纳入 Git。
