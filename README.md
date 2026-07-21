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
