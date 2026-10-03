# 核心验收范围与证据契约

冻结日期：2026-10-03。本文定义本轮可重复验收的范围和成功条件，结果应另存于 `results/`。列出某项条件不表示它已经通过，也不表示项目已经达到行业 SOTA。

## 两种导出语义分别验收

默认仍为一次连续 `libx264` CRF 导出。输入的主视频、允许的常量滤镜、第一条音轨和当前 MP4 封装构成其参考产物。带上下文的试编码属于该模式的预测测量；它改变了参考帧、GOP 或 lookahead 的历史，不能拼成等价的连续产物。

新增模式必须由调用方显式选择，在结果中声明 `semantics=independent_segments`。它将一个冻结帧计划中的每段分别以 x264 编码，使用闭合 GOP 和独立初始 IDR，再把实际 AVC 样本原样写入固定格式的 fragmented MP4。已经完成的试编码段就是最终产物的一部分。该模式改变了码流、大小、质量和随机访问行为，需要独立缓存键和独立参考导出。

两个模式都以实际**整个输出文件**的字节数为预测目标。单独视频 payload、片段普通 MP4 字节之和、HLS 文件包或连续导出文件的大小不得替代分段 fMP4 的真值；这些诊断指标若报告，需在字段和图表中注明目标。

| 范围 | 分段模式本轮合约 | 范围之外的响应 |
| --- | --- | --- |
| 视频 | Linux/FFmpeg/libx264；8-bit SDR；输出 yuv420p | 10-bit、PQ、HLG 等明确拒绝，不能静默降精度后算通过 |
| 导出配置 | CRF、preset、线程数、常量滤镜与工具链版本完整记录 | 未注册新配置不能继承预测校准 |
| 音频 | 保留第一条音轨并全片编码为 AAC-LC 一次；可显式 drop_audio | 延迟起始、超过双声道、采样率不在当前写入器范围或特殊时间线明确拒绝 |
| 音频起点 | 音视频源起点须在一音频采样以内对齐；AAC priming 单独表示 | 不支持的 movie offset 不得通过删音轨或清零时间戳掩盖 |
| 输入时序 | 严格递增的逐帧呈现时间戳；受支持的每 sample 一 picture 源编码 | 包/帧数不一致、缺失时间戳或无法保证帧映射时拒绝 |
| VFR | 必须显式 `fps=...`，先计数整个转换后输出序列，再按整数帧划分 | 无显式转换的 VFR 拒绝；不能把 avg_frame_rate 当作 CFR 证明 |
| 封装 | 单个 fMP4；每个 box 小于 4 GiB；固定宽度时间与索引字段 | 需要额外 box 格式、编解码器或播放器语义时另开范围验收 |

当前音频适配器接受 mono/stereo、正采样率且低于 65536 Hz；写入器还会检查 AAC-LC、时间基、priming/padding 与 presentation start。原音频短于或长于视频的尾部均应保留，不能用 `-shortest` 假装同步。

## 时间线与复用必须有逐产物证据

`SegmentedMedia.plan()` 记录 `source_hash`、`toolchain_key`、`plan_hash`、`source_frames`、`source_geometry`、`output_rate`、`total_frames`、`segment_frames` 和逐段 `start_frame/frame_count`。非转换输入还记录原始整数 PTS 边界。分段时长先换算为整数帧，不能用浮点秒的边界判断来决定重复或丢弃一帧。

CFR 首批必须包含整数帧率和 `30000/1001` 等非整数帧率、非零源起点、B-frame 重排，以及一个或数个 frame 的短尾段。对无损诊断配置，逐段解码帧拼接须与经过声明滤镜的完整源序列逐帧一致。对有损配置，最终 fMP4 解码帧须与其已编码段的解码帧逐帧一致；不能拿有损输出直接要求等于原始像素。

VFR 的 fps 滤镜需保留整片滤镜相位。当前适配策略会从源开头解码相应片段，记录 `fps_conversion_access=decode_from_start`；这可能昂贵，必须算入预算。其他输入记录 `timestamp_seek`。VFR 转换的正确性与该策略的性能分别验收，不能把某次转换正确解释为它已经足够快。

每段记录 `video_frames`、`payload_bytes`、`duration_ticks`、`first_pts`、`first_dts`、`first_keyframe`、`extradata_hex`、`time_base`、源摘要、工具链和完整配置。复用前检查缓存文件的大小与摘要；同一候选的 SPS/PPS、宽高和时间基不一致时拒绝。写入器还须核验实际 IDR NAL，不能只相信 MP4 的 keyframe flag。

复用成功的验收指标是同一目标配置的每段仅编码一次、最终样本内容未被重新有损编码，且冷启动从预测到发布的总费用降低。暖缓存直接命中完整产物属于缓存收益，单独统计。

## 完整文件大小的可加恒等式

固定写入器的 `ByteAccounting` 提供：

```text
total_bytes = init_bytes + video_payload_bytes
              + audio_payload_bytes + fragment_bytes
container_bytes = init_bytes + fragment_bytes
fragment_bytes = video_fragments * (32 + 64 * track_count)
                 + 16 * (video_samples + audio_samples)
```

`track_count` 为 1 或 2。配置音频时，每个 fragment 保留音频 traf，即使该 fragment 暂无音频 sample，所以交织位置不改变这个固定开销公式。64-bit duration 字段和固定布局使初始化长度不随最后算出的时长或压缩 payload 长度改变。上述公式仅属于本写入器，不能套给任意 FFmpeg MP4。

第一个视频段确定合法视频 sample entry、全片 AAC 已编码、帧计划不变时，`planned_accounting()` 能精确给出音频和封装贡献；尚未观测的视频 payload 必须仍标为未知。它不能把已知开销的精确性升级为全文件预测的精确性。

每个完整产物的 `file_bytes` 必须等于恒等式，允许差值 **0 字节**。独立解析器/FFprobe 要验证各段 sample 数、顺序、时间线、音轨与 box；测试还须全片解码并比较视频 frame hashes 和 AAC PCM。AAC 测试以解码内容、采样数与 priming edit 为依据，单个工具报告的 format/stream duration 可能包含其展示的 priming 时长，不能仅据该浮点数判断音频丢失。

## 预算、拒答和完整产物发布

三个请求轴分别记录：

| 请求轴 | 验收对象 | 达标证据 |
| --- | --- | --- |
| compute | 墙钟、线程、新探针上限、累计请求视频编码量 | 共用 Deadline、完整实测成本及被终止工作；不是预计耗时 |
| reliability | `abs(predicted-actual)/actual` 的误差与请求 coverage | 同一冻结 family 的有效区间，或实际完整产物的 exact 证据 |
| size | 整个输出文件的 max_bytes 与允许 CRF 范围 | 完整产物实际字节核验；概率性上界仅作调度规划 |

推断、片段/音频编码、容器写入、源身份核验和所有子进程应服从同一个预测截止时间。取消时终止子进程组，不发布残缺产物。操作系统调度和清理有尾延迟，不能把普通 Deadline 宣称为硬实时保证；需报告超时请求的实际退出延迟。

`max_encode_fraction` 的分子为 `attempted_encode_seconds`，即请求的视频编码秒数；连续 context 探针计入完整 padded span，分段计入实际请求段时长，完整导出计入完整视频时长。它不等于 CPU 时间、已成功产出的媒体秒数或全部解码工作。分母是该模式的完整输出视频时长。缓存命中不新增视频编码量，已请求但被取消的编码仍计费；VFR 从开头解码的额外工作由墙钟与资源指标体现。

音频独立记录 `attempted_audio_seconds` 和 `audio_preparation_seconds`。音频不进入视频 encode_fraction 的分子，但所有音频处理、检查和缓存读取都受同一 wall budget 约束；attempted_audio_seconds 应包含已启动后被取消的音频请求，不能只累计成功返回的音频任务。连续导出或探针中的音频与独立全片音频分别注明，不能把“视频比例受限”解释为“所有媒体处理均受该比例限制”。结果中的 `encode_fraction_basis` 应明示这一区别。

预算不够、没有可靠性校准、有效区间仍太宽、候选在允许范围内尚未验证可行，都可以返回已有点估计及明确未满足项。`uncertainty.kind=uncalibrated`、缺失区间或缺失 artifact 不得返回“请求全部满足”。区分“未找到可行候选”与“证明所有允许候选不可行”；CRF 搜索不假设严格单调，也不宣称全局最优感知质量。

最终发布需具备完整 artifact、源身份核验、实际字节、SHA-256 和已核验的硬尺寸条件。目的路径存在时保留原文件；临时文件复制、摘要检查和原子创建目的路径的费用单独报告。只有预测而没有完整产物时，不能声称已经导出或满足硬尺寸上限。

## 原始输入与 prepared asset 的费用边界

原始文件路径请求首次读取和 SHA-256 计算属于预测预算。不能用文件大小、mtime、inode 的组合代替内容身份，也不能把全片扫描或哈希时间藏到计时之外。原始路径会在会话中做 stat guard，并在结果发布边界再次核对完整摘要。

`PreparedAsset.from_path()` 是显式一次性接收步骤：复制完整捕获字节、同时计算 SHA-256、由 Linux memfd 的 WRITE/GROW/SHRINK/SEAL seals 固化快照。后续预测读取该快照并继承文件描述符；关闭 asset 后新读者应失败。原始文件之后变化不应改变快照，也不应改变其摘要。捕获期间的普通源变化应拒绝；这里只保证实际捕获字节的身份，不声称对恶意并发写入提供瞬时原文件快照证明。

prepared asset 的 `size_bytes` 与 `ingest_seconds` 独立报告。默认接收上限为 2 GiB，可显式配置；过大、非普通文件、Linux seal 不可用或捕获失败时拒绝，不退回 stat 身份缓存。其内存/page-cache、生命周期和同时活跃 asset 数量计入资源指标。

两种成本必须同时公布：

```text
cold_end_to_end = ingestion_if_requested + prediction_elapsed + publication_elapsed
warm_asset_prediction = prediction_elapsed on an already ingested, live snapshot
```

段计划、音频缓存与完整产物缓存是否命中分别记录，例如 `prepare_cache_hit`、段 `cache_hit` 和完整产物 `cache_hit`。prepared asset 可以避免重复读原始输入求摘要；缓存检查、packet inspection 与实际解码仍有费用。身份固化不等于免费预处理、持久分布式资产库或容量自动治理。

## 统计域及当前证据缺口

无分布假设、无真实先验范围时，小预算不可能对任意视频提供窄且高覆盖的区间。以 N=180 段为例，全部低负载与包含一个未知高熵 burst 的视频在只观测普通段时无法区分；随机观察 6 段漏掉 burst 的概率为 `174/180=96.7%`。增大抽样量、发现它的机会和复用所得产物可以解决工程问题，但不能用观察到的低方差消除这个反例。

连续模式的上下文探针改进应通过真实参考降低经验偏差；分段模式则直接把目标改成可加的实际样本。两者都不能凭抽样标准误差认定可靠性目标已满足。

轨迹级 conformal 每个来源组仅有一个分数，取该组全部预注册 variant、CRF 与采样前缀的最大绝对 log error。family 必须冻结完整配置、候选网格、采样时长、probe policy 及 padding、seed 策略、工具链、线程和模型。该最大分数包络覆盖 family 内的自适应停止和候选选择；新增配置必须重校准。

注册的前缀上限必须是 family 指纹中的有限 `maximum_prefix`。每个校准源以冻结计划的 `source_blocks` 确定完整范围 `minimum_prefix..min(maximum_prefix,source_blocks)`，不能把观测后的停止点当作源总段数。运行时必须传递实际 `sample_count`；免费缓存让观测超过原注册上限时，也不能继续套用包络。输入 FPS 和源段数可以在声明域内变化，只要源派生规则冻结且校准组与新来源交换；同一 family 不代表对每个 FPS/长度子域另有条件覆盖。

覆盖是声明域中交换来源组的边际保证。唯一 group_id 和 provenance 不能证明独立或交换性，必须说明实际分组依据与域。开发/训练、校准、最终测试按原始来源分组，同片裁剪、尺寸、CRF、编辑变体不得分流；`test_reserved` 不能参与校准。测试数据一旦用于调参，需要重新标记角色并准备新的最终评测，不能继续叫盲测。

95% 时，`ceil((G+1)*0.95)` 不得超过 G，至少需要 **19 个独立校准来源组**才有有限区间，此时取最大组分数。现有 **7 个校准来源组**不足；保持用户的 95% 要求并返回未校准，不能拆段伪造样本数或悄悄降低 coverage。数学测试中的人工分数只能验证公式和契约。

相对误差认证针对真实大小作分母。区间 `[L,U]` 内最优连续点为 `2LU/(L+U)`，最坏误差为 `(U-L)/(U+L)`；整数版本应检查相邻点和两个端点。原模型点与这个认证点分别记录，不能在校准后暗改模型标签。区间仍太宽时拒绝达标。

## 冻结的实际评测矩阵

所有评测输出应保存请求、冻结 commit/代码摘要、源/来源组/split、完整配置、FFmpeg 版本、CPU 配额、内存限制、任务顺序、计时边界、预测日志、参考摘要及失败原因。先保存预测，再读取对应参考字节评分。下表是评测要求；尚无对应结果文件的单元格一律记为**待评测**。

| 维度 | 固定的比较/案例 | 必须报告的实际指标 |
| --- | --- | --- |
| continuous 预测 | 均匀片段、官方采样基线、上下文探针、直接完整编码；同一配置真值 | 全部请求分母、点估计率、median/P90/worst APE、目标命中率、实际成本 |
| independent_segments 预测 | 均匀抽样实际段、随机残差校正、渐进完成全部段；同一分段真值 | 相同误差指标；已复用段比例、重复编码量、距 exact 剩余实际成本 |
| 计算轴 | 1/3/10/30 秒请求；长片另加 120 秒档；固定 preset/threads 后分别测量 | 实际墙钟、相对完整导出成本、请求编码秒数、预算退出延迟、拒答率 |
| 可靠性轴 | ε=5%/10%/20%，coverage=90%/95%/99%；无校准包亦保留 | 请求达标率、证据种类、区间宽度、group 覆盖及联合轨迹覆盖；无证据不填覆盖率 |
| 体积轴 | 无上限，以及同配置参考大小的 90%/70%/50%；CRF 上限与候选数固定 | 完整产物超限率、找到可行候选的比例、尝试数、成本、失败原因 |
| 内容 | 旧开发合成诊断、CTC 短片、真实录屏/游戏/动画、1080p/4K、真实长片 | 按来源组和内容类分开报告；短片不能替代长片指标 |
| 时间/音频 | 非整数 CFR、短尾、B-frame、非零起点、VFR 显式 fps、mono/stereo、长短音轨 | 帧/样本数、presentation/DTS 连续性、音频 PCM、priming 和音画对齐 |
| 质量代价 | 相同源/滤镜/CRF下 continuous 与 segmented；再做等体积比较 | 实际体积变化、PSNR/XPSNR/VMAF 中可用指标、段边界质量变化；CRF 不代替质量 |
| 身份/缓存 | raw 冷启动、prepared asset、段计划/段/audio/完整 artifact 命中、缓存损坏 | ingestion 与预测分别计时，端到端总成本、哈希/身份检查、重新生成量 |
| 失败恢复 | 源修改、关闭 asset、超时/取消、坏 plan/片段、已有目标、不兼容参数集 | 无错误产物发布、原文件保留、临时文件/子进程清理、明确错误结果 |
| 资源/播放器 | 实测峰值 RSS、快照与 artifact 总空间；FFprobe/FFmpeg 解码 | 并发资源上限与峰值、完整解码率；其他播放器待独立兼容性验收 |

原先只测视频的采样基线应保留在 video-only 子矩阵，不能把缺少音频的结果直接排到包含 AAC 的完整文件任务中。正式来源覆盖率以来源组为分母；开发诊断的每段、每次 seed 或每个 CRF 不能充当额外业务样本。

经验精度目标是：在声明输入域中，测试低预算下 10% APE 的命中率及成本是否优于相同预算基线。这一项目前是**待评测目标**，不是数学保证或已实现 SLO。可确认的 exact 产物要求大小误差 0、封装恒等式差 0、没有漏/重复视频帧和音频内容丢失；这些还需对应本轮实测文件支撑。

## 复现入口与结论边界

基础数学与集成检查使用：

```sh
.venv/bin/python -m unittest discover -s tests -v
```

重点文件为 `test_trajectory.py`、`test_fmp4.py`、`test_segmented_media.py`、`test_assets.py` 和完整控制器集成测试。素材复现见 [corpus.md](corpus.md)，已完成的上下文偏差开发研究见 `results/context-study-2026-10-03/README.md`，统计推导与条件见 [research-statistical-design.md](research-statistical-design.md)。新的完整预测/尺寸/质量结果应提供自己的固定命令和输出目录，不能借用旧结果替代新范围验收。

本轮可以验收具体输入域、配置与资源内的控制器、字节核算、复用和失败语义。行业 SOTA、任意硬件或编辑器支持、真实业务覆盖率与生产运行 SLO 需要更强基线、独立数据和实际部署证据；缺少这些时保持范围明确，同时继续推进可验证的编码与调度改进。
