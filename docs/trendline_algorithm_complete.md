# 智能直线趋势线算法完整说明

本文逐项说明当前生产代码中的直线趋势线识别流程、公式、阈值、候选保留规则、
去重规则和输出状态。实现以 `services/trendline_analysis_service.py` 为准。

本文描述的是项目内独立实现，不依赖 `algorithm/` 目录。外部导入的
`algorithm/trendline_algorithm_v4_bundle/直线趋势线算法完整说明.md`
仅作为早期设计参考，删除该目录不会影响当前算法。

## 1. 识别目标与基本定义

算法输入一段按时间升序排列的 OHLCV K 线，默认使用最新 150 根，输出两类单侧包络：

- `up`：上涨行情下方的支撑趋势线，斜率必须大于 0；
- `down`：下跌行情上方的压力趋势线，斜率必须小于 0。

算法并不拟合穿过收盘价中心的线性回归线。趋势推进阶段可以远离趋势线，只有回调低点
或反弹高点应当反复靠近单侧包络。

候选线定义为：

```text
y(i) = intercept + slope * (i - fit_start)
```

其中 `i` 是分析窗口内的绝对 K 线序号。代码同时区分：

- `fit_start`、`fit_end`：产生候选线时使用的拟合窗口；
- `first_touch`、`last_touch`：首末有效触点；
- `fit_length = fit_end - fit_start + 1`；
- `structure_length = last_touch - first_touch + 1`。

分级、去重和实际绘图主要使用 `structure_length`，避免一个很长的拟合窗口中只有局部
几根 K 线贴线，却被误判为长期趋势。

## 2. 输入、周期与分析窗口

支持周期：

```text
1D、3D、1W、1M
```

分析窗口 `limit`：

- 默认值：150；
- 最小值：30；
- 最大值：300；
- 聚合后少于 7 根时直接返回空结果。

周末数据由标的设置或请求参数决定。关闭周末数据时，先删除星期六、星期日，再执行周期
聚合。

聚合规则：

- `1D`：不聚合；
- `3D`：从最早数据开始，每连续 3 行合成一根；
- `1W`：按 ISO 年和 ISO 周分组；
- `1M`：按自然年月分组。

每组 OHLCV 合成方式：

```text
Open   = 第一根 Open
High   = 组内最高 High
Low    = 组内最低 Low
Close  = 最后一根 Close
Volume = 组内 Volume 之和
date   = 第一根日期
end_date = 最后一根日期
```

成交量当前只被保留，尚未进入趋势线评分。

## 3. ATR 波动标准化

每根 K 线的真实波幅：

```text
TR(i) = max(
    High(i) - Low(i),
    abs(High(i) - Close(i-1)),
    abs(Low(i) - Close(i-1))
)
```

局部波动尺度使用 TR 的 14 根滚动中位数：

```text
ATR(i) = rolling_median(TR, window=14, min_periods=2)
```

开头缺失值用向后填充。ATR 在同一个 DataFrame 上缓存；所有除法至少使用 `1e-9`
作为分母下限。

此处严格说是“滚动中位真实波幅”，不是 Wilder 平滑 ATR。使用中位数是为了降低单根
异常长 K 线对距离尺度的影响。

## 4. 支撑与压力参考点

实体权重固定为：

```text
body_weight = 0.75
```

上涨支撑参考点：

```text
body_edge_up = min(Open, Close)
wick_mid_up  = (Low + body_edge_up) / 2
anchor_up    = 0.75 * body_edge_up + 0.25 * wick_mid_up
```

等价地，`anchor_up = 0.875 * body_edge_up + 0.125 * Low`。

下跌压力参考点：

```text
body_edge_down = max(Open, Close)
wick_mid_down  = (High + body_edge_down) / 2
anchor_down    = 0.75 * body_edge_down + 0.25 * wick_mid_down
```

等价地，`anchor_down = 0.875 * body_edge_down + 0.125 * High`。

该构造允许趋势线穿过部分影线，同时令实体边缘占主导。上涨和下跌使用完全对称的公式。

## 5. 单个区间的候选线拟合

### 5.1 区间最低长度

拟合区间少于 7 根时直接放弃。

### 5.2 稳健斜率样本

每个拟合区间均匀抽取最多 28 个参考点：

```text
sample_count = min(interval_length, 28)
```

对抽样点计算全部两两斜率。上涨只保留正斜率，下跌只保留负斜率；方向正确的斜率少于
3 个时放弃该区间。

从方向正确的斜率分布取 15 个分位数：

```text
q_slope = linspace(0.08, 0.92, 15)
```

分位结果去重后形成斜率网格。

### 5.3 单侧截距网格

对每个斜率计算：

```text
residual(i) = anchor(i) - slope * local_index(i)
```

截距分位数组：

```text
0.03、0.07、0.13、0.20
```

上涨支撑线取上述低分位；下跌压力线对称地取
`97%、93%、87%、80%` 分位。

基础网格最多产生 `15 * 4 = 60` 个“斜率 + 截距”组合。

### 5.4 统一方向距离

定义方向符号：

```text
d = 1   for up
d = -1  for down
```

参考点到线的 ATR 标准化距离：

```text
gap(i) = d * (anchor(i) - line(i)) / ATR(i)
```

`gap >= 0` 表示价格位于趋势线应在的一侧；`gap < 0` 表示穿线。

实体边缘距离：

```text
body_gap(i) = d * (body_edge(i) - line(i)) / ATR(i)
```

### 5.5 低成本初筛

每个网格组合先计算：

```text
coverage  = mean(gap >= -0.22)
proximity = exp(-quantile(abs(gap), 0.20) / 0.65)
severe    = mean(gap < -0.80)
```

实体完整性按第 7 节计算。初筛分：

```text
surrogate =
    0.45 * coverage
  + 0.30 * proximity
  + 0.25 * body_integrity
  - 0.50 * severe
```

拟合窗口取初筛分最高的 5 个组合执行完整评分。最终按拟合区间预期层级选择其中最佳一条：

- 拟合长度不大于 `short_max`：使用短期推进分；
- 拟合长度小于 `long_min`：使用中期趋势分；
- 其余：使用长期结构分。

## 6. 包络完整性

完整评分仍使用第 5.4 节的 `gap`。允许轻微穿线：

```text
tolerated = 0.22 ATR
```

计算：

```text
coverage    = mean(gap >= -0.22)
soft_breach = mean(max(-gap - 0.22, 0))
hard_breach = mean(gap < -0.80)
```

包络完整性：

```text
integrity_base = clip((coverage - 0.72) / 0.27, 0, 1)

integrity =
    integrity_base
  * exp(-2.5 * soft_breach - hard_weight * hard_breach)
```

覆盖率低于或等于 72% 时基础完整性为 0；达到 99% 时基础部分饱和为 1。软穿越深度和
严重穿越比例继续作指数扣分。若最长连续实体穿越不超过 1 根，`hard_weight=1.5`；
否则 `hard_weight=3.0`。因此孤立异常 K 线不会与连续穿越受到同等惩罚。

## 7. 实体穿越约束

实体穿越阈值：

```text
body_gap < -0.10  记为实体穿越
body_gap < -0.35  记为严重实体穿越
```

平均穿入深度从 `0.06 ATR` 开始累计：

```text
penetration = mean(clip(-body_gap - 0.06, 0, 0.80))
breach_ratio = mean(body_gap < -0.10)
severe_ratio = mean(body_gap < -0.35)
max_run = 最长连续实体穿越根数
severe_weight = 2.0 if max_run <= 1 else 4.0
```

实体完整性：

```text
body_integrity = exp(
    -4.0 * penetration
    -severe_weight * severe_ratio
    -0.75 * max(0, max_run - 1)
)
```

单根穿入深度最多按 `0.80 ATR` 计罚。第一根实体穿越不会触发连续性项，孤立严重穿越的
严重比例权重为 2；从连续第 2 根开始同时提高严重比例权重并施加连续性指数扣分。所有
层级在最终筛选时还统一要求：

```text
max_body_breach_run <= 2
```

因此连续 3 根或更多实体被趋势线明显穿过时直接淘汰。影线穿越不触发这条硬过滤。

## 8. 触点检测

### 8.1 局部挑战点

```text
区间长度 < 36：gap window = 3
区间长度 >= 36：gap window = 5
minimum_distance = max(2, interval_length // 18)
minimum_prominence = 0.12 ATR
min_periods = 1
```

算法先对候选线的 `gap` 序列做居中滚动平均，然后用 `find_peaks(-smooth_gap)` 找到
靠近支撑/压力包络的局部挑战点。上涨线寻找回调低点，下跌线寻找反弹高点。

首尾区域宽度仍为：

```text
edge_width = max(4, interval_length // 15)
```

固定市场枢轴无法知道哪根边界 K 线最接近某条具体直线，因此每条候选还在首尾区域补充
一个线相对距离最小的挑战点。

### 8.2 有效接触

局部挑战点满足以下条件才是有效接触：

```text
smooth_gap <= 0.75 ATR
```

也就是说，价格不必精确碰到趋势线；在趋势线正确一侧、距离不超过 0.75 ATR 的结构性
回调或反弹也可算作挑战。

### 8.3 触点后的拒绝质量

观察窗口：

```text
horizon = min(24, max(7, interval_length // 6))
```

触点后少于 2 根数据时，该触点可计入挑战次数，但不计入拒绝质量。

对可评价触点：

```text
rebound = max(future_smooth_gap) - smooth_gap_at_touch
stayed_intact = min(future_smooth_gap) > -0.85

q_forward    = clip(rebound / 1.50, 0, 1)
q_prominence = clip(prominence / 1.20, 0, 1)
q = 0.70 * q_forward + 0.30 * q_prominence
```

若后续未保持完整，`q *= 0.20`。如果触点是浅假突破且随后保持完整：

```text
-0.45 <= smooth_gap_at_touch < 0
```

则 `q` 增加 0.12，最高仍为 1。最终 `rejection` 是所有可评价触点 `q` 的均值；没有可
评价触点时为 0。

## 9. 触点数量、跨度与分布

触点数量基础分：

| 触点数 | 0 | 1 | 2 | 3 | 4 | 5 及以上 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 基础分 | 0.00 | 0.12 | 0.43 | 0.73 | 0.88 | 1.00 |

首末触点跨度占整个拟合区间的比例：

```text
event_span =
    (last_touch_local - first_touch_local)
    / max(1, interval_length - 1)
```

两个以上触点时，将触点位置归一化到首末触点之间的 `[0, 1]`，计算：

```text
max_touch_gap = 最大相邻触点归一化间距
```

并将触点划入 4 个等宽时间箱。位置恰好为 1 的末触点归入第 4 箱。分布分：

```text
gap_score = clip((0.78 - max_touch_gap) / 0.38, 0, 1)
bin_score = clip((occupied_bins - 1) / 3, 0, 1)

touch_distribution =
    0.65 * gap_score
  + 0.35 * bin_score
```

`interior_touches` 统计落在整个拟合区间 20%～80% 位置内的触点数量，目前只作为解释
指标返回，不直接作为硬门槛。

跨度修正和分布修正：

```text
span_factor =
    0.62 + 0.38 * min(1, event_span / 0.60)

distribution_factor =
    0.72 + 0.28 * touch_distribution

touch_score =
    count_score * span_factor * distribution_factor
```

触点贴近度只在候选触点处计算：

```text
pivot_distance = median(abs(smooth_gap[touch_indices]))
proximity = exp(-pivot_distance / 0.65)
```

没有触点时 `proximity = 0`。

## 10. 方向推进指标

设 `Close` 为拟合区间收盘价。

方向效率：

```text
signed_net = d * (Close_last - Close_first)
path = sum(abs(diff(Close))) + 1e-9
efficiency = clip(signed_net / path, 0, 1)
```

斜率强度：

```text
move_atr = d * slope * (interval_length - 1) / median(ATR)
slope_strength = clip(move_atr / 3.0, 0, 1)
```

方向漂移 t 值：

```text
signed_returns = d * diff(Close)

drift_t =
    mean(signed_returns)
    / (std(signed_returns, ddof=1) + 1e-9)
    * sqrt(interval_length - 1)
```

它衡量方向一致性，但不是经过多重搜索校正后的统计显著性或 p 值。

## 11. 三类评分公式

### 11.1 长期结构分

方向门控：

```text
direction_gate =
    1 / (1 + exp(-1.7 * (drift_t - 1.0)))
```

原始结构分：

```text
raw_long =
    0.17 * integrity
  + 0.18 * body_integrity
  + 0.11 * proximity
  + 0.22 * touch_score
  + 0.14 * rejection
  + 0.10 * touch_distribution
  + 0.03 * efficiency
  + 0.05 * slope_strength
```

长度置信度：

```text
length_confidence_long =
    0.86 + 0.14 * (1 - exp(-(fit_length - 7) / 20))
```

最终长期结构分：

```text
long_base =
    100 * raw_long
    * length_confidence_long
    * (0.68 + 0.32 * direction_gate)

long_score = long_base
```

### 11.2 中期趋势分

方向显著性平滑项：

```text
significance_medium =
    1 / (1 + exp(-1.35 * (drift_t - 0.65)))
```

长度置信度：

```text
length_confidence_medium =
    0.90 + 0.10
    * (1 - exp(-max(0, fit_length - 12) / 18))
```

中期基础分：

```text
medium_base = 100 * length_confidence_medium * (
    0.17 * integrity
  + 0.18 * body_integrity
  + 0.12 * proximity
  + 0.20 * touch_score
  + 0.12 * rejection
  + 0.09 * touch_distribution
  + 0.06 * efficiency
  + 0.04 * slope_strength
  + 0.02 * significance_medium
)

medium_score = medium_base
```

### 11.3 短期推进分

方向显著性平滑项：

```text
significance_short =
    1 / (1 + exp(-1.4 * (drift_t - 0.70)))
```

短期推进分：

```text
short_base = 100 * (
    0.16 * integrity
  + 0.16 * body_integrity
  + 0.14 * proximity
  + 0.14 * efficiency
  + 0.12 * slope_strength
  + 0.08 * significance_short
  + 0.12 * touch_score
  + 0.04 * rejection
  + 0.02 * event_span
  + 0.02 * touch_distribution
)

short_score = short_base
```

三个分数都落在近似 0～100 范围，但并不是概率。不同层级的分数含义和门槛不同。

## 12. 长、中、短期边界

对分析窗口长度 `N`：

```text
short_max = max(10, round(0.10 * N))
long_min  = max(short_max + 1, round(N / 3))
```

分级依据是 `structure_length`：

```text
short:  structure_length <= short_max
medium: short_max < structure_length < long_min
long:   structure_length >= long_min
```

最终筛选还要求短期结构至少 7 根。`N=150` 时：

| 层级 | 有效触点跨度 |
| --- | ---: |
| 短期 | 7～15 |
| 中期 | 16～49 |
| 长期 | 50～150 |

## 13. 全局候选区间搜索

### 13.1 粗搜索长度

候选长度集合去重、取整并限制到 `[7, N]`：

```text
7
10
short_max
round(0.15 * N)
round(0.21 * N)
round(0.30 * N)
round(0.42 * N)
long_min + 9
round(0.57 * N)
round(0.77 * N)
N
```

`N=150` 时为：

```text
7、10、15、22、32、45、59、63、86、116、150
```

每种长度分别搜索上涨和下跌。拟合终点步长：

```text
length <= 10：stride = 1
10 < length < long_min：stride = 2
length >= long_min：stride = 3
```

### 13.2 时间均衡保留

所有粗候选先按其 `structure_length` 对应层级的分数降序排列：

- 短期用 `short_score`；
- 中期用中期选择分；
- 长期用 `long_score`。

全局直接保留前 36 条。

随后把整个图表按 `ceil(N / 5)` 分成 5 个时间区。按以下键分组：

```text
方向、层级、拟合起点时间区、拟合终点时间区
```

每组再保留前 2 条。全局候选和分组候选合并后，以
`(fit_start, fit_end, direction)` 去重。

### 13.3 常规局部精搜

从合并候选中按层级分取前 42 条作为种子。起点和终点分别尝试偏移：

```text
-6、-3、0、3、6
```

即每个种子最多尝试 `5 * 5 = 25` 个邻域窗口。窗口裁剪到数据边界，长度至少为 7，已经
计算过的 `(start, end, direction)` 不重复计算。

### 13.4 最近趋势增强搜索

额外搜索拟合终点位于最新 10 根内的短中期结构：

```text
recent_ends = max(6, N - 10) ... N - 1
recent_max = max(short_max, min(30, N))
```

近期长度集合：

```text
7、10、short_max、20、24、recent_max
```

去重并过滤无效起点后，对上涨和下跌分别拟合。

近期粗候选按通用长期结构分 `score` 排序，取前 16 条继续精搜：

```text
start_offset = -6、-4、-2、0、2、4、6
end_offset   = -2、-1、0、1、2
```

精搜终点强制不早于 `N - 10`，窗口长度要求：

```text
7 <= fit_length <= recent_max
```

最终候选池再次按有效结构层级对应分数降序排列。

## 14. 最终分层筛选

### 14.1 历史时效惩罚

定义：

```text
age = N - 1 - last_touch
```

门槛附加分：

| age | 附加门槛 |
| ---: | ---: |
| 0～45 | 0 |
| 46～75 | 2 |
| 76～105 | 6 |
| 106 以上 | 8 |

另有显示硬门槛：

```text
age > 75  时 tier_score >= 78
age > 105 时 tier_score >= 80
```

### 14.2 当前仍有效的结构

从最后有效触点到最新 K 线重新计算 `gap`。当前仍有效需同时满足：

```text
min(post_touch_gap) >= -0.50
latest_gap <= 1.25
```

该结果按 DataFrame 长度缓存在候选对象上。

### 14.3 长期结构

默认分数门槛：

```text
long_threshold = 55
```

全部条件：

```text
structure_length >= long_min
long_score >= 55 + freshness_extra
通过历史显示硬门槛
touches >= 3
event_span >= 0.38
max_touch_gap <= 0.80
drift_t >= 0.55
max_body_breach_run <= 2
```

### 14.4 中期趋势

默认门槛：

```text
medium_threshold = 70
```

“近期中期反转”定义：

```text
structure_length <= 30
拟合终点距最新 K 线不超过 12 根
```

近期中期反转的选择分取 `max(medium_score, short_score)`。当前仍有效的中期线在输出筛选
时同样取两者最大值。近期反转或当前仍有效时基础门槛为 66，否则为 70。

全部条件：

```text
short_max < structure_length < long_min
medium_output_score >= base_threshold + freshness_extra
通过历史显示硬门槛
touches >= 2
event_span >= 0.28
drift_t >= 0.65
max_body_breach_run <= 2
```

三个及以上触点的中期线还必须满足：

```text
max_touch_gap <= 0.80
touch_distribution >= 0.15
```

只有 2 个触点时还需：

```text
medium_output_score >= base_threshold + freshness_extra + 4
```

### 14.5 短期推进

默认门槛：

```text
short_threshold = 64
```

全部条件：

```text
7 <= structure_length <= short_max
拟合终点距最新 K 线为 0～9 根
short_score >= 64
touches >= 2
event_span >= 0.28
drift_t >= 0.85
max_body_breach_run <= 2
```

有效跨度少于 10 根时要求：

```text
short_score >= 71
```

只有 2 个触点时要求：

```text
short_score >= 67
```

短期结构即使后来已被突破，也可作为已经完成的近期历史线保留，但绘图不会延长到突破
后的区域。

## 15. 几何去重

### 15.1 同方向前提

上涨线与下跌线永不互相去重。两条同方向线的共享区间按首末有效触点计算：

```text
overlap_ratio =
    overlap_length / min(structure_length_1, structure_length_2)
```

若共享长度不超过 1 根，直接判为不同。

“边界接近”定义为：

```text
abs(first_touch_1 - first_touch_2) <= 15
abs(last_touch_1  - last_touch_2)  <= 15
```

若 `overlap_ratio < 0.40` 且边界也不接近，则不再比较。

### 15.2 ATR 几何距离

在共享区间均匀抽样最多 7 个位置，计算两条线距离除以当地 ATR。统计：

```text
median_distance
distance_70
distance_80
```

斜率累计分歧：

```text
slope_divergence =
    abs(slope_1 - slope_2)
    * max(1, overlap_length - 1)
    / median(sampled_ATR)
```

满足以下任一规则即视为重复。

常规共线：

```text
overlap_ratio >= 0.40
median_distance <= 0.55 ATR
distance_80 <= 0.90 ATR
slope_divergence <= 1.25 ATR
```

高度嵌套：

```text
overlap_ratio >= 0.85
median_distance <= 1.05 ATR
distance_70 <= 1.50 ATR
slope_divergence <= 2.00 ATR
```

边界接近：

```text
边界接近为真
median_distance <= 0.90 ATR
distance_80 <= 1.40 ATR
slope_divergence <= 3.00 ATR
```

### 15.3 层内抑制

每层先按本层分数降序执行非极大值抑制：

```text
长期临时候选最多 6
中期临时候选最多 8
短期临时候选最多 4
```

### 15.4 跨层级统一聚类

层内筛选后，将所有长、中、短期候选建立重复关系图，并用并查集求连通分量。

每个重复簇先取最高层级分 `best_score`，再保留：

```text
tier_score >= best_score - 10
```

的近优候选作为代表竞争者。最终依次优先：

1. `structure_length` 更长；
2. `last_touch` 更新；
3. `tier_score` 更高。

代表线按：

```text
tier_score - freshness_extra
```

优先排序，结构跨度作为第二排序键。最终上限：

```text
长期最多 3
中期最多 4
短期最多 2
总计最多 6
```

## 16. 层级父子关系

长期线和中期线可作为父结构。中期、短期候选寻找同方向且层级不同的父线，要求父线覆盖
子线至少 80%：

```text
intersection / child.structure_length >= 0.80
parent.structure_length > child.structure_length
```

有多个父结构时，选择跨度最小者，即最接近的上一级结构。父子关系仅用于解释和展示，
不改变分数。

## 17. 状态判定与绘图范围

从最后触点到最新 K 线的最差距离小于 `-0.50 ATR` 时：

```text
status = broken
active = false
```

否则：

```text
latest_gap <= 0.50  -> challenging
0.50 < latest_gap <= 1.25 -> valid
latest_gap > 1.25 -> historical
```

`challenging` 和 `valid` 的 `active = true`；`historical` 的 `active = false`。

绘图起点永远是首个有效触点，结构段终点永远是最后有效触点：

```text
start_index = first_touch
end_index   = last_touch
```

仅 `active` 结构把 `projection_end_index` 延长到最新 K 线；其他状态只画到
`last_touch`。因此失效线不会被画到突破位置。

前端样式：

- 长期：点状线；
- 中期：虚线；
- 短期：实线；
- 同层级多线使用不同颜色；
- 当前有效线从最后触点到最新 K 线的投影段使用更弱的点状样式。

## 18. 输出解释字段

后端除坐标外还返回：

- `score`：长期结构公式的通用分；
- `tier_score`：该线最终所属层级采用的分数；
- `integrity`、`body_integrity`；
- `body_breach_ratio`、`severe_body_breach_ratio`、`max_body_breach_run`；
- `touches`、`touch_score`、`rejection`、`proximity`；
- `event_span`、`touch_distribution`、`max_touch_gap`、`interior_touches`；
- `efficiency`、`slope_strength`、`drift_t`；
- `fit_start_index`、`fit_end_index`；
- `active`、`status`、`age`、`current_gap`、`parent_id`。

排查图形时应优先看 `tier_score`，因为中期和短期并不以通用 `score` 作为最终排序依据。

## 19. 算法合理性与数学评价

### 19.1 合理之处

1. **目标函数与交易语义一致。** 支撑和压力本质上是单侧边界，使用分位包络比中心回归
   更符合“回踩支撑、反弹受压”的定义。
2. **价格尺度基本不变。** 大部分距离用 ATR 标准化，对价格单位变化和不同资产的名义
   价格较稳健。
3. **方向严格对称。** 上涨和下跌共享同一套符号化公式，减少人为不一致。
4. **斜率估计具有稳健性。** 两两斜率分位搜索接近 Theil-Sen 思路，不会由一个异常点
   唯一决定斜率。
5. **证据不是“两点成线”。** 第三个及更多独立触点获得明显非线性奖励，同时评价触点
   分布和触线后的拒绝。
6. **实体和影线分层处理。** 这比把 High/Low 或 Close 当作唯一真值更贴近 K 线结构。
7. **有效跨度与拟合跨度分离。** 这是避免首尾桥接和错误长期线的重要设计。
8. **去重基于几何关系。** ATR 距离分位数、共享时间和斜率分歧比仅比较起止日期可靠。

### 19.2 数学上是否“足够美”

算法的局部构件较美：方向对称、ATR 无量纲化、单侧分位包络和触点边际收益递减都具有
清晰结构。整体则更接近一个可解释的工程评分系统，而不是由单一概率模型或单一最优化
目标自然推导出的算法。

不够简洁的地方主要有：

- 完整性、实体完整性、贴近度和触点分彼此相关，存在重复计权；
- 效率、斜率强度和 `drift_t` 也共享方向推进信息；
- 多个硬门槛会在边界附近产生不连续结果；
- 55、64、70 等总分门槛没有概率含义，跨层级不能直接比较；
- 参数数量较多，存在针对有限案例反复校准后过拟合的风险；
- 在大量区间中挑最高分会产生“赢家偏差”，当前分数未做多重搜索校正。

因此，当前算法适合作为视觉分析和决策辅助工具，但不应把分数解释为趋势成立概率，也
不应未经独立回测直接转化为交易信号。

### 19.3 参数可解释性

可解释性较强的参数：

- 0.10、0.35、0.50、0.75、0.80、1.25 等 ATR 距离阈值；
- 触点数、首末跨度、最大空白比例；
- 连续实体穿越根数；
- 15/50 根长短边界以及 45/75/105 根时效区间；
- 每层最大展示数量。

可解释性较弱的参数：

- 三套评分权重；
- sigmoid 的中心和陡峭度；
- 长度置信度的指数衰减常数；
- 初筛分权重和候选保留数；
- 去重的多个距离分位数组合。

这些参数都有工程含义，但目前不是从标注概率、损失函数或统计置信区间估计得到的。

### 19.4 未来算法优化角度

在当前视觉结果已经稳定的前提下，下一阶段最值得做的不是继续按个例微调，而是建立独立
评估体系：

1. 固定训练标的和调参日期，另设从未参与调参的标的、年份和市场作为留出集。
2. 用滚动截断方式做 walk-forward 验证，严格避免未来信息。
3. 定义可度量标签，例如未来保持时间、突破概率、最大不利偏离和再次触线概率。
4. 对分层分数做 logistic 或 isotonic 概率校准，使“80 分”具有稳定解释。
5. 用块自助法或保留波动聚集的随机过程估计完整搜索后的假阳性率，校正赢家偏差。
6. 用最小描述长度、贝叶斯模型选择或全局集合优化，同时决定应展示几条线；重复线惩罚
   可直接进入目标，而不只依赖事后去重。
7. 引入变化点和分段线性模型，显式表示匀速、加速和趋势切换。
8. 为斜率和截距提供自助法置信区间，展示“趋势带”而不只是一条精确直线。
9. 按资产类别、周期和波动状态校准参数；当前固定的 ATR 阈值未必适合所有市场。
10. 后续可独立验证成交量、缺口、多周期一致性和流动性，不应未经样本外验证直接加权。

## 20. 性能优化实现与评估

优化前一次当前环境的只读剖析结果（SPX、150 根日 K）：

```text
总耗时                 21.21 秒
fit_interval 调用       2,068 次
完整 _score_line        10,020 次
_event_metrics          10,020 次
_body_metrics          130,260 次
```

计时会受机器和后台负载影响，但调用次数揭示了稳定的结构性热点。

当前已实现以下性能优化：

- 每个分析窗口一次性预计算 OHLC 数组、ATR、上下参考点和实体边缘；
- 将分位包络候选的初筛改为 NumPy 批量矩阵计算；
- 按算法版本、请求设置和完整 K 线窗口指纹缓存最近 64 个 API 分析结果。

同一组 SPX 数据完整 API 冷计算已从最初逐候选实现的约 `20` 秒级下降到数秒级；缓存
命中通常只需要毫秒到几十毫秒。新版评分和候选空间已按需求发生变化，因此不再要求与旧
版本输出逐字节一致，而是使用模拟、真实目标和全标的结构审计验证。

### 20.1 已实现：消除重复数据准备

旧实现会在很多小区间反复执行 DataFrame 切片、列转 NumPy、参考点构造和实体边缘构造。
当前实现在一次请求开始时预计算并复用：

```text
Open、High、Low、Close
ATR
anchor_up、anchor_down
body_edge_up、body_edge_down
```

后续只对 NumPy 数组做视图切片，不改变数学结果。

### 20.2 已实现：批量向量化候选组合

单区间的60个基础分位组合已改为二维矩阵，一次计算全部候选的：

- line；
- gap 和 body_gap；
- coverage、20% 距离分位数、严重穿越比例；
- 实体穿入深度、穿越比例和严重穿越比例。

最长连续穿越采用候选维度批量、时间维度顺序扫描的游程统计。实现保持原 NumPy 分位数
算法、浮点精度和元组排序规则。

### 20.3 已实现：缓存不变分析

完整 API 结果按以下信息缓存：

```text
算法版本、标的、period、limit、周末设置、窗口位置、总数据量、完整窗口指纹
```

窗口指纹覆盖每根 K 线的全部字段，因此历史价格被修订时不会误用旧结果。缓存使用线程锁
和最近使用顺序，最多保留 64 项；返回结果使用防御性复制，调用方不能修改缓存内容。

当前没有实现区间级增量缓存。它会增加失效管理和内存占用，而冷计算已经降至约 4.5 秒，
现阶段收益不足。

### 20.4 暂缓：多核并行

以下任务天然独立：

- `up` 与 `down` 两个方向；
- 不同粗搜索长度；
- 不同 `(start, end, direction)` 区间；
- 最终完整评分中的少量候选。

候选搜索由粗搜、依赖粗搜排名的局部精搜和依赖近期种子的二次精搜组成，多阶段之间存在
顺序依赖。为大量仅含 7～150 根的小任务增加进程通信、数组共享和稳定排序，预期收益
有限。

项目主要运行在 Windows Python/Flask 环境中，进程池还要承担 `spawn` 启动成本和服务
生命周期管理。当前单标的冷计算约 4.5 秒、缓存命中约 0.019 秒，暂不值得引入这些复杂
性。未来批量分析多个标的需要加速时，应优先在标的之间并行；这种任务粒度更大，也更
容易保持算法内部顺序。

### 20.5 暂缓：JIT 或编译热点

实体指标粗筛已经批量向量化，标量实体统计不再是主要热点。剩余成本分散在完整评分、
NumPy 分位数和 SciPy 局部峰值检测中；为这些小任务增加 JIT 依赖、首次编译延迟和跨
平台部署成本暂不划算。

对于单标的、150 根这种小矩阵，GPU 数据传输和内核启动成本通常大于收益。只有同时批量
分析数百或数千个标的时，GPU 批处理才值得评估。

### 20.6 体验优化不等于计算优化

后台任务、进度提示、取消和先显示 K 线再叠加趋势线可以改善等待体验，但不会缩短实际
计算时间。当前合理顺序是：

1. 保留已实现的 NumPy 预计算、批量粗筛和数据快照缓存；
2. 用真实运行数据继续观察冷计算是否影响主要工作流；
3. 批量分析多标的需要加速时，在标的层使用进程池；
4. 只有单标的冷计算仍构成明确瓶颈时，再独立验证等价的触点检测实现。

进一步减少完整评分候选可能漏掉独立结构。减少窗口数量或降低局部精搜密度会改变候选
空间，应作为独立算法版本重新跑全量验证。

## 21. 测试与验证

运行单元测试：

```bash
python3 -m unittest tests.test_trendline_analysis -v
```

运行目标标的和模拟行情验证：

```bash
python3 scripts/validate_trendline_algorithm.py
```

追加审计数据库内全部标的：

```bash
python3 scripts/validate_trendline_algorithm.py --all-symbols
```

任何评分、搜索密度或阈值调整都应同时保存算法版本、标的数据截止日期和验证结果，避免
只对当前肉眼案例变好、对未来样本反而退化。
