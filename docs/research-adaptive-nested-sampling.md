# 按已付观测自适应的两阶段采样与最终新批次

本方向是有限总体的两阶段差值估计，而不是新的 overlap confidence sequence。
它允许刚付费得到的 `a_F` 改变第二阶段分配、预测和条件概率，也允许过去的 M
改变下一阶段的 F 概率。全 a census 和外部校准不再是无偏性的必要条件。
是否节省真实 CPU、收窄误差或达到 hard finalsize，仍没有媒体证据；本 family
只有 SOURCE 代码、本文与一次 invented exact rational fixture。已有 fixed-unit
cheap gate 的成功不等于 generic continuous-medium M oracle 已证明。

## 当期 F 可以改变 b，但必须在当期 M 的 coins 之前

固定一个真正连续目标：未知单位字节 `Y_i` 是固定有限总体。单位可为固定的
16/32 coded-frame pieces，不必整 GOP；单位计数字段应不重叠，而其真实参考、
解码、编码、未来输入闭包可能重叠。把已付并核实的完整历史记作 `H`，已知 exact
单位总字节为 `K`，剩余单位为 `U`。历史中包括所有费用、成功/失败、缓存键及
已得到的 cheap/exact 数值。SOURCE 几何、目标 identity 和单位定义先固定。

1. 在当前 F coins 前，从 H 构造任意有限 `mu_i(H)`，并能计算完整 `sum_U mu`。
   从 H 与已核实 SOURCE 费用产生一个有限可行 F 库，其边际 `p_i(H)>0` 对 U
   的每个单位成立。F tickets 可使用过去已付的 a/M，但不能偷看当前未付值。
2. 抽 F，取得并核实所有 `a_F`，包括合法缓存复用。在这一完整 transcript 上
   任意构造 `b_i(H,F,a_F)`，包括 F 内均值、非线性回归、过去 M 拟合的 slope，
   以及整个 F 的联动项。不要求知道 U 的 a 总量，也不要求 b 是一个抽样前固定
   的全总体 potential。当前 M 的 Y 不可进入这次 b 或其概率。
3. 在 F transcript 条件下抽 `M subset F`。其精确条件 inclusion probability
   `r_i=P(i in M | H,F,a_F)>0` 必须覆盖 F 的每个单位。r 可以由 a_F 与所有
   过去 M 的数值决定；无须等概率或与 F 独立。
4. 返回有符号估计，不 clamp、不只发表漂亮结果：

`Z = K + sum_U mu + sum_F (b_i-mu_i)/p_i + sum_M (Y_i-b_i)/(p_i*r_i)`。

先对当前 M 条件期望，b 精确消去：

`E[Z | H,F,a_F] = K + sum_U mu + sum_F (Y_i-mu_i)/p_i`。

再对 F 的 coins 取期望，得到 `E[Z|H]=K+sum_U Y_i=T`。即使 cheap a 只是一种
差预测，这一等式仍成立。这里的校正是 **F 的加权预测均值**，不是未知 U 的
full-a 均值。因此当期 b 可以依赖整个 F，而旧固定-potential 公式不能这么改。
接口应先 commit 完整 a_F transcript，再创建 b/r/tickets，最后支付并打开 M。
事后把 r 写成一个观测到的经验概率，或读到 Y 后重抽/换分配，都不成立。

`p_i*r_i` 是实际路径上的条件分解，通常不是无条件 `q_i=P(i in M|H)`。
对本次变化的 b 使用 q 不能条件消去 b；本 fixture 用精确 q 显示偏差。若 b
对每个单位预先固定，则旧公式配 q 是另一种合法估计，不能与本公式混搭。
任何拟合到本次 M 的 b 必须另做校正或独立分样；本实现只拒绝这种数据流。

## 可执行的 whole-budget 库

本实现先用 SOURCE 上界筛 F 的有限库，再使用一张条件 M 库。原型选择 `|F|=2`，
M 为 F 的 singleton。F 合法的条件是：付完 F 的全部 fresh cheap 和所有 cache
key/hash lookup 后，F 中 **每一个**候选 i 的完整 exact closure 都能在本阶段
剩余预算内执行。于是 r 任意变化都不会产生 overbudget 分支。M 的 tickets 用
`epsilon/|F| + (1-epsilon)*w_i/sum_F w`，epsilon=1/4；权重由已观测 cheap
预测差与完整 exact CPU 上界的比值决定，只是分配 heuristic，绝不是 byte bound。
F tickets 则只看历史已付的预测差。代码通过 exact Fraction 构造有限概率；部署
可统一分母为整数 tickets 后使用理想 `randbelow`，不能把普通浮点近似当作 exact
law。没有 cryptographic RNG 或 native executor 的实测证据。
CPU ticket normalizer 必须为正，这是本配方的分母约束，不是证明真实 CPU 的下界。
History 要求 immutable tuples、唯一合法单位 ID 和 exact finite Fraction，避免
重复 key 被 dict 静默覆盖；单位必须属于冻结 SOURCE population。

费用向量包含 whole-query CPU 上界、完整输入帧以及 I/O；实际还需独立处理串行
峰值内存、wall deadline 和持久化上界。本例所有费、闭包和数值都是 invented，
CPU ticks 不是真实 CPU 秒。exact_closure 原型包含从 frame0 到单位的重复前缀，
每次全部重计，不能将 overlap 中央帧的并集当成本。SOURCE/已知 pilot 固定账
在开始即入 ledger；缓存依然支付 lookup/hash，fresh F 支付完整本地 cheap 输入，
每次 M 支付整个原连续目标 replay/参考/未来输入/flush 身份核验闭包。
函数不把失败后的成功单位拼成部分估计；原生失败需要 whole transaction poison
并保留费用。source 准备、先前 pilot、decode、imports、startup、hash、receipt 和
cleanup 的真实上界也必须入 native preflight，不是这些 toy 向量所能证明。

这个限制比“要求最贵全总体 M 每一轮都 fit”更细：只要求已经选到的 F 中每个
M 分支 fit，但每个原未知单位必须属于某个合法 F。不同 cost classes 可各维护
库，预算与 support 底线在开 coin 之前核验。finite library 的大小与 p/r 精确
计算成本也必须收费。大总体用固定 SOURCE 类、有限 panel grid 或已证明概率
的生成器；不能拿本例的穷举复杂度声称 scalable。positive support 不等于可靠
尾部；很小 p*r 会产生很大的逆权重，应给用户注册概率 floor 并在无法 fit 时拒绝。

对于任意有限合法 joint action 库 L，在其支持内存在所有单位正 inclusion 的
概率律，当且仅当各 action 的 exact-observed 集合的并集覆盖 U。必要性：并集
之外的单位在任何可行路径中都不会被观察；充分性：对一个有限 cover 的每个
action 给严格正权重。对于本条件两阶段架构，先称 F 合法当且仅当在付完 F 后
可行 M 的并集覆盖 F，再要求合法 F 的并集覆盖 U。这就是本实现的 preflight。
若某个单位在 B 内没有任何真实 continuous-M closure，就不能同时保证 hard B
与 all-unit positive exact coverage。a 值、概率调整、adaptive stop 都无法修复。
要求最小 inclusion floor 后，还需对应概率 LP 可行性，单纯并集覆盖不够。

实际 grain 的四个粗 GOP 闭包可能在 20%/50% reference cap 下无法满足覆盖。
本 family 不改那四个单位，不读取其 actual a/Y；切小 scoring pieces 也不会自动
缩短 continuous-medium 的参考闭包。若最终 reserve 只能选择 full-medium exact
census，则总费用是探索加 full encode，可能更贵；这是准确度/可扩展性取舍，
没有速度胜利承诺。已有 veryfast 全量成本和 superfast 的观察不能变成 cheap
的统一低成本、低残差或 hard upper 假设。

## 复用历史，停止后取新批次

下一阶段将已经支付的 exact 单位移入 K，保留已付 cheap 缓存。可根据这些数值
重新拟合 mu、选择 F tickets、选择 b 与 M tickets；每个阶段的条件证明都成立。
`D_t=Z_t-T` 是对 pre-stage history 的 martingale difference。固定 n 的平均无偏；
有界 predictable 权重的 stopped **centered sum** 也均值零。但 `Z_tau`、随机
样本数的平均、按费用成功后才发表的均值通常有偏。current Y 触发停止时，最终
批次已被结果选择。fixture 同时给出这三个量，绝不把 numerator 定理误写为
random-denominator 无偏定理。

一个可实施的修正是预留最终预算。在剩余预算中探索/更新历史；根据旧历史决定
“结束探索、现在出估计”，然后重新开一个完整 final F/M coin transaction，输出
这个新批次的 Z，而不按其 Y 决定是否发表。对于每个 final 前的历史，均值都为 T，
所以 overall output 无偏。若探索令未知单位全部 exact 已知，则直接 exact K。
最终必须在每个合法路径发生并完整成功；选择性失败/拒绝的 conditional mean
不受此证明保护。本 toy 枚举一次探索加一次 final，final p/b/r 依第一次已付
观测而变化，并逐个检查每个 prior-history 的 final conditional expectation。
探索预算必须永不吞掉预留 final 的全 support closure 费用。一个安全但昂贵的
fallback reserve 是 complete original target encode；便宜 reserve 需真实闭包证明。

这只是 point 的 selection-bias 修正，既不提供随时有效 confidence set，也不保证
hard finalsize radius。若需要统计 certificate，应另注册合法 caps 与 e-process，
并为其 own evidence/error event 付费；没有 deterministic tight byte caps 时，
现有 hidden-unit obstruction 依旧存在。不能用 sampled residual/cheap agreement
替代 cap，不能把无偏性改称95% reliability。若用户需要 hard finalsize 而 budget
不够，输出 unsatisfied/refuse；完整连续 exact 结果是可收费的 fallback。

## ONE registered pure evidence 和接入点

`benchmarks/native_adaptive_nested_sampling.py register` 把 canonical code/本文和
完整 SOURCE snapshots 固化在独立 family。旧 docs 仅绑定 SOURCE hash，不读旧
results、score、完整 a/Y 或 media。随后 `run` 只有一个 immutable attempt，
外部 supervisor 等待 Python-only worker 并保存 whole wait4 user/system CPU、
wall、RSS、stdout/stderr hash。worker 费用涵盖其 startup/imports/绑定/枚举/序列化
与退出；supervisor 的最终写入/退出与 registration/planning/Ruff 费用标 UNKNOWN
而非免费，不声称完整 native runtime certificate。

一次 rational tree 验证：当期 a_F 影响 b 和 conditional r；过去 M/cache 影响
下一阶段 mu/p；每个历史分支条件无偏；所有路径 obey whole-vector budget；错误
无条件 q、当期 Y fitted slope、current-Y stop、预算成功条件化均展示偏差；无可行
全支持闭包/闭包少计/cache变值拒绝。没有重复已有 nested variance 大枚举或
overlap CS coverage 测试。family≤256KiB，包括 canonical、snapshots 和 final32KiB；
持久化前 workspace 保留134217728字节。

接入需要三个已冻结的原生接口：先 cheap broker 原子提交整 F 的 scalar/cache
identity/whole fees；再 sampling controller 固化 b 与 conditional M tickets；最后
exact broker 返回本来 continuous medium 的 Y 与全 reference/pixel/clock identity，
成功后更新 K/cache/费用。第一项有局部 fixed-unit gate 基础；后两项仍是 SOURCE
接口，generic exact M 和真实 whole-cost 上界未证。raw local medium clip 不可代替
Y。本代码没有 decoder、encoder、compiler 或媒体入口，也不宣称 SOTA。
