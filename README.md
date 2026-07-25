# 交易分析决策系统

一个本地运行的交易分析工具。当前版本支持行情缓存、K 线查看、MA/EMA 指标配置记忆，以及开发阶段的 SQLite 数据库浏览。

本项目当前优先保证 Windows 原生环境可运行，后续更新应尽可能同时兼容 Windows 和 Linux/WSL；如两者存在差异，优先支持 Windows。

## 功能

- Flask 本地 Web 应用，启动后可自动打开浏览器。
- 输入股票代码查看 2020-01-01 以来 OHLCV 行情。
- 查看行情默认展示本地标的总览，可点击标的进入 K 线详情。
- 默认夜间模式，可在侧边栏底部切换日间/夜间主题。
- 优先读取 SQLite 缓存，缓存不足时调用 Twelve Data API。
- 可一键检查并更新当前标的自 2020-01-01 以来的历史数据。
- 打开行情总览时会自动补齐总览标的日 K，并回刷最近 5 个自然日以修正盘中未完成 K 线。
- 行情总览支持自动更新开关，开启后每 5 分钟刷新一次总览标的最新价格。
- 自研 Canvas K 线图，支持日K、3日K、周K、月K。
- 支持拖拽平移、滚轮缩放、价格轴、时间轴、悬浮 OHLCV。
- 默认显示最近 150 根 K 线，数据不足时按实际数量展示。
- 左侧坐标轴显示价格，右侧坐标轴显示相对当前视图首根开盘价的涨跌幅。
- 支持 MA、EMA 指标线。
- 侧边栏提供智能分析模块，可在最新 150 根 K 线上识别上涨支撑线和下跌压力线。
- 趋势线按短期（约 15 根以内）、中期（约 16～49 根）和长期（约 50 根以上）分层展示。
- 趋势线算法包含多触点确认、首尾桥接过滤和跨层级几何去重。
- 指标按“标的 + K 线视图”保存配置。
- 支持收藏指标，并可快速添加到不同标的或视图。
- 每个标的的每个视图最多同时设置 10 个指标。
- 内置数据库浏览器，支持分页查看表内容。
- 数据库浏览页面支持一键备份 SQLite 数据库。
- 行情总览支持拖拽调整标的显示顺序，并自动保存到数据库。
- 行情总览支持隐藏标的，隐藏不会删除数据库中的历史行情和指标配置。
- 侧边栏提供“退出系统”按钮，可主动停止后端服务。

## 安装

### Windows

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

如果 PowerShell 禁止执行激活脚本，可改用 CMD：

```cmd
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Linux / WSL

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

创建 `.env`：

```env
TWELVEDATA_API_KEY=your_api_key_here
```

## 启动

### Windows

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

CMD：

```cmd
.venv\Scripts\activate.bat
python app.py
```

默认地址：

```text
http://127.0.0.1:5000
```

调试时不想自动打开浏览器：

```powershell
$env:AUTO_OPEN_BROWSER="false"
python app.py
```

CMD：

```cmd
set AUTO_OPEN_BROWSER=false
python app.py
```

### Linux / WSL

```bash
source .venv/bin/activate
python app.py
```

调试时不想自动打开浏览器：

```bash
AUTO_OPEN_BROWSER=false python app.py
```

## 配置

`.env` 可配置：

```env
TWELVEDATA_API_KEY=your_api_key_here
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
AUTO_OPEN_BROWSER=true
AUTO_SHUTDOWN_ON_BROWSER_CLOSE=true
BROWSER_OPEN_COMMAND=
```

## 数据库

SQLite 数据库文件：

```text
data/market_data.sqlite
```

数据库备份文件会保存到：

```text
data/backups/
```

启动时会自动根据 `database/schema.sql` 创建缺失的数据表。

### symbols

标的表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `symbol` | TEXT | 标的代码，唯一 |
| `name` | TEXT | 标的名称 |
| `exchange_name` | TEXT | 交易所名称 |
| `currency` | TEXT | 计价货币 |
| `show_weekend_data` | INTEGER | 是否显示周末 K 线 |
| `show_in_overview` | INTEGER | 是否显示在行情总览 |
| `display_order` | INTEGER | 行情总览显示顺序 |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

### daily_prices

日线行情表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `symbol` | TEXT | 标的代码 |
| `date` | TEXT | 交易日期 |
| `open` | REAL | 开盘价 |
| `high` | REAL | 最高价 |
| `low` | REAL | 最低价 |
| `close` | REAL | 收盘价 |
| `volume` | REAL | 成交量 |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

### trendline_analysis_snapshots

直线趋势线总览快照表。按标的、周期、算法版本和 K 线指纹保存完整识别结果、总览摘要与
最新结构事件，程序重启后可以立即显示已经完成的标的，不需要等待整批重新计算。

约束：`UNIQUE(symbol, date)`。

### api_request_logs

API 请求日志表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `provider` | TEXT | 数据源 |
| `symbol` | TEXT | 标的代码 |
| `status` | TEXT | 请求状态 |
| `error_code` | TEXT | 错误码 |
| `message` | TEXT | 日志消息 |
| `created_at` | TEXT | 创建时间 |

### indicators

全局指标定义表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `code` | TEXT | 指标代码，唯一 |
| `name` | TEXT | 指标名称 |
| `indicator_type` | TEXT | 指标类型，当前支持 `MA`、`EMA` |
| `params_json` | TEXT | 指标参数 JSON |
| `is_favorite` | INTEGER | 是否收藏 |
| `description` | TEXT | 指标说明 |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

默认收藏指标：`EMA8`、`EMA13`、`MA20`。

### symbol_chart_views

标的 K 线视图表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `symbol_id` | INTEGER | 标的 ID |
| `symbol` | TEXT | 标的代码 |
| `view_code` | TEXT | 视图代码，如 `1D`、`3D`、`1W`、`1M` |
| `period_type` | TEXT | 周期类型，如 `day`、`week`、`month` |
| `period_value` | INTEGER | 周期数值 |
| `name` | TEXT | 视图名称 |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

默认视图：`1D`、`3D`、`1W`、`1M`。

### symbol_indicators

标的视图指标配置表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `symbol_id` | INTEGER | 标的 ID |
| `symbol` | TEXT | 标的代码 |
| `chart_view_id` | INTEGER | K 线视图 ID |
| `view_code` | TEXT | 视图代码 |
| `indicator_id` | INTEGER | 指标 ID |
| `color` | TEXT | 指标线颜色 |
| `visible` | INTEGER | 是否显示 |
| `sort_order` | INTEGER | 排序 |
| `created_at` | TEXT | 创建时间 |
| `updated_at` | TEXT | 更新时间 |

约束：`UNIQUE(symbol_id, view_code, indicator_id)`。

## 指标说明

- `MA`：简单移动平均线。
- `EMA`：指数移动平均线。
- 指标基于当前 K 线视图重新计算。
- 数据不足时指标值为空，不绘制对应线段。

## 智能趋势线分析

程序启动后会在后台分析总览内全部标的的最新 150 根日 K。侧边栏打开“智能分析”
会先显示四列 K 线分析总览：标的代码、最新价格、行情更新时间和直线趋势线摘要。计算
期间页面持续显示完成数量、并行进程数和剩余数量；点击任意标的所在行进入其 K 线分析，已有快照会
直接绘制。自动更新开关用于每 5 分钟检查并刷新分析结果。若算法版本、周期、窗口、
周末设置和完整 K 线指纹均未变化，后台直接复用持久化快照，不重新执行趋势线搜索。

批量刷新先在主进程逐个检查数据指纹，只把需要冷计算的标的交给进程池。进程池最多使用
4 个进程，并至少为 Flask 和行情更新保留 1 个逻辑 CPU；任务不足或机器核心数较少时会
自动降低并发数。子进程只读取行情并计算趋势线，快照统一由主进程按完成顺序串行写入
SQLite，最终结果仍按总览标的顺序排列。单个标的计算失败不会中断其余任务。可通过环境
变量 `ANALYSIS_MAX_WORKERS` 把上限调低到 `1`～`4`，设为 `1` 即使用串行冷计算。

直线趋势线列优先显示仍有效或正在被挑战的主线和阶段线，并用简短事件标出今日确认、
新增触点、进入挑战、重回趋势和今日结束。主表显示趋势线在最新 K 线处的点位；评分、
触点数、形成日期、最近触点和 ATR 距离放在悬浮详情中。若直接显示的两条线都属于中期，
按趋势首端到最新显示点位的完整时间跨度分别标为“中长期”和“中短期”。事件由同一次分析中的最近两根 K 线推断，
不会为了总览再单独运行一次“前一天的 150 根 K 线”。

也可以直接搜索标的并选择 K 线周期，再点击“智能识别”。算法输出的不是价格回归中轴，
而是上涨行情的下侧支撑线或下跌行情的上侧压力线。

智能分析每次打开时默认隐藏指标线和指标图例；点击图表工具栏的指标按钮后，本次分析才显示指标图例。该临时显示状态不会覆盖查看行情页面保存的指标可见性。

当前算法的主要步骤：

1. 使用 14 根滚动中位真实波幅（ATR）统一不同资产的价格距离。
2. 以实体边缘为主、影线为辅构造支撑/压力参考点。
3. 在多种长度和起止日期上拟合稳健的单侧分位包络，并补充显著市场枢轴两两连线。
4. 分开评估影线穿越和实体穿越，连续 3 根实体被穿越时直接淘汰候选。
5. 使用平滑后的局部挑战点识别独立触线和假突破后的拒绝。
6. 结合触点跨度与分布，对首尾集中、中间悬空的结构连续减分。
7. 分别使用短期推进分、中期趋势分和长期结构分筛选结果。
8. 将长、中、短期结果统一比较，合并时间高度重叠且几何位置近似相同的重复线。
9. 将解释同一段行情的相似线归为趋势族，保留主线及至多一条具有独立证据的阶段线。
10. 将趋势线画成形成、确认和延伸三段；已结束线延长到反向突破或持续加速位置。

对 150 根日 K，默认分层和主要门槛如下：

| 层级 | 长度 | 默认分数门槛 | 主要结构要求 |
| --- | ---: | ---: | --- |
| 短期 | 7～15 | 64 | 至少2次触线，分布差时最高额外减8% |
| 中期 | 16～49 | 70 | 至少2次触线，分布差时最高额外减14% |
| 长期 | 50～150 | 55 | 至少3次触线，分布差时最高额外减16% |

三次及以上独立触线会得到非线性加分；触点覆盖多个时间区段时继续加分。跨层级去重使用 ATR 归一化后的稳健距离分位数和共享时间比例，不会仅因日期重叠就删除前后错开的加速阶段。重复候选分数相差不超过 10 分时，优先保留有效触点跨度更长的一条。

相似但不完全重合的同方向线还会进入趋势族筛选。主线优先考虑是否尚未结束，再综合评分、触点分布和结构跨度；阶段线必须至少包含 2 个主线不能解释的独立触点，与主线连续分离至少 8 根，且斜率差达到 25%。每个趋势族最多显示 1 条主线和 1 条阶段线。

历史线距今超过 75 根时至少需要 78 分，超过 105 根时至少需要 80 分。状态只分为“趋势中”“挑战中”“已结束”：前两者延伸到最新 K 线，已结束线延伸到反向突破或持续加速位置。反向突破使用连续两根超过 0.30 ATR 或单根超过 0.80 ATR 的收盘确认；顺向远离达到 4.00 ATR、后续至少 3 根且始终未回到 1.50 ATR 内时，视为旧趋势被更快趋势替代。75 分以上使用实线，否则使用虚线。最终总共最多展示 6 条线。

完整的生产算法步骤、全部阈值、数学评价和等价性能优化讨论见
[docs/trendline_algorithm_complete.md](docs/trendline_algorithm_complete.md)；较短的设计概览和
验证案例见 [docs/trendline_algorithm.md](docs/trendline_algorithm.md)。

运行快速单元测试：

```bash
python3 -m unittest tests.test_analysis_overview tests.test_trendline_analysis -v
```

运行模拟行情和数据库真实标的完整验证：

```bash
python3 scripts/validate_trendline_algorithm.py
```

追加审计 SQLite 中的全部标的：

```bash
python3 scripts/validate_trendline_algorithm.py --all-symbols
```
