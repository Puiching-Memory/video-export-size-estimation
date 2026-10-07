# 显式 FPS 随机 seek 研究（开发数据，未修改冻结 v2）

保留全片时间戳相位，并按实际源 PTS 找到目标首帧的前驱，是可审查的替代候选。在 6 个 Bunny 输入/衍生配置、12 与 30000/1001 两种输出 FPS、144 个首段/中段/随机边界/长空档/短尾窗口上，packet guard 与 noaccurate 变体都通过每帧 SHA-256、整数 PTS、duration 和 time base 完全相同检查。所有视频来源是同一个已经查看过的 Bunny；这不是独立工业 holdout，不作新精度或 SOTA 声明。

冻结的 v2 仍使用 decode_from_start，本报告没有修复或更改它。后续实现必须另外提交、另外测试、明确版本边界。本研究没有读取 native holdout label，也没有据其误差调算法。

| 候选/对照 | 完全一致 / 窗口数 | 用途 |
| --- | ---: | --- |
| `fixed_guard` | 126/144 | 提前固定 0.5 秒，保留全局 PTS |
| `local_reset` | 72/144 | 输入 seek 后局部归零，再补整数输出 offset |
| `packet_guard` | 144/144 | 根据 packet PTS 选择真实前驱，再保留全局 PTS |
| `packet_guard_noaccurate` | 144/144 | 同前驱方案，另保留 seek keyframe 的前导帧 |
| `packet_guard_without_origin` | 11/52 | 省略 format origin 归一化（仅非零起点配置） |
| `prefix_frames` | 144/144 | 完整从头 fps 后按 frame index 裁切 |

共 772 项命令级对比；每种主要方法 144 个窗口，另有 52 个非零起点归一化对照。每项只做一次 timing，研究串行、FFmpeg 线程 1；共享四核环境还有其他研究任务。墙钟是描述性成本，不能当生产 SLO 或统计显著结论。

时间戳方法：普通 MP4/Matroska 输入使用 `-ss <前驱时间> -copyts -start_at_zero -i source`，保持与完整默认输入相同的全局归一化时间轴；先 `fps=F`，再 `trim=start_pts=K0:end_pts=K1`，最终 segment 才允许重置局部 PTS。窗口的第一输出 tick 是完整序列首 tick 加上段的帧索引，不无条件等于段帧索引本身。

FFmpeg 7.1.5 的 fps 将实际解码帧的 PTS 换算为整数 `q_i=round_near_inf(P_i × TB × F)`；对每个输出 tick `k`，选显示顺序里最后一个 `q_i ≤ k` 的帧。必须保留这个前驱，并读到必要后继或真正视频 EOF。固定 0.5 秒提前量会在本研究 2.2–2.7 秒 VFR 空档中遗漏前驱。先归零再补整数 offset 会改变格点选帧，相同 SHA 仅在部分恰好对齐窗口成立。

本实验先按 PTS 排序索引，定位最后一个量化 PTS ≤ 目标首 tick 的 packet，再提前两个 packet；CLI seek 时间向下舍入至微秒，避免略过选定前驱。这个 guard 是经过本组样例验证的候选，不是任意编解码器的通用证明。正确性契约应建立在 decoder 实际 frame PTS、完整随机访问参考、单调时间轴、每 packet 一帧与可 seek 输入上；不满足就回退从头解码。B 帧按显示 PTS 比较，不能直接按 DTS 选帧。

源码依据：[vf_fps.c](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavfilter/vf_fps.c)、[ffmpeg_demux.c](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/fftools/ffmpeg_demux.c)、[ffmpeg_opt.c](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/fftools/ffmpeg_opt.c)。普通容器的 origin 偏移先量化到输入 time base，再加到整数 PTS；不要用浮点秒减法声称逐 tick 相等。MPEG-TS/AVFMT_TS_DISCONT 的 effective start、时间戳断点修复与 copyts 行为不同，本研究没有支持它。

逐帧完全一致之后才比较成本：144 个配对窗口的总实际解码量从 **38,811 帧降到 5,755 帧**；配对解码量降低倍数中位数 **3.30**，墙钟倍数中位数 **1.50**。现有约 60 秒 Bunny 的 40 个非首段窗口，配对解码倍数中位数 **8.47**，墙钟倍数中位数 **2.54**。随机 seek 仍依赖 GOP 距离，本组最多解码 241 帧；不能承诺每个段只解码目标帧数。

全片 planning 也有独立候选：扫描 packet 时间戳后，实际解码首个 FPS 输出得到 `k0`；再 seek 到末尾几帧，保持全局时间轴并读到真实视频 EOF，得到最后输出 tick `H-1`。在连续 fps 输出格点的契约下，`N=max(0,H-k0)`。12/12 配置的总计数、首帧和完整尾后缀 SHA/PTS 全部与完整全片真值相同；实际头尾解码中位数 33 帧，最大 97 帧。候选没有输入 `-t`、前置 trim 或输出 frames 限制来制造假 EOF。

| 开发输入 | FPS | 完整帧数 | head+tail 解码帧 | 扫描+完整 fps null count (秒) | 扫描+head/tail (秒) |
| --- | ---: | ---: | ---: | ---: | ---: |
| bunny-existing-cfr | 12 | 721 | 97 | 1.362 | 0.327 |
| bunny-existing-cfr | 30000/1001 | 1801 | 97 | 1.277 | 0.343 |
| bunny-existing-vfr | 12 | 721 | 35 | 0.803 | 0.302 |
| bunny-existing-vfr | 30000/1001 | 1801 | 35 | 0.931 | 0.312 |
| bunny-derived-cfr2997 | 12 | 233 | 21 | 0.613 | 0.247 |
| bunny-derived-cfr2997 | 30000/1001 | 581 | 18 | 0.634 | 0.260 |
| bunny-derived-vfr-gap | 12 | 232 | 32 | 0.432 | 0.271 |
| bunny-derived-vfr-gap | 30000/1001 | 581 | 34 | 0.446 | 0.285 |
| bunny-derived-vfr-offset | 12 | 232 | 32 | 0.436 | 0.270 |
| bunny-derived-vfr-offset | 30000/1001 | 581 | 35 | 0.423 | 0.271 |
| bunny-derived-vfr-delayed-video | 12 | 232 | 32 | 0.432 | 0.260 |
| bunny-derived-vfr-delayed-video | 30000/1001 | 581 | 32 | 0.432 | 0.278 |

成本对照是完整 `fps → null` 计数，避免把对所有像素做 SHA 的真值成本算成现有 prepare 耗时。candidate 还包含少量头尾像素 SHA，因此不是无验证的乐观成本。输入 SHA/不可变资产准备不计入这里；全 packet 扫描仍需完整读取索引，某些裸流还需读全源字节。计时是单次、不同阶段，不是严格配对性能试验。

仅有严格 CFR PTS 和每 packet 一帧仍不够推出真实 EOF。本组 `bunny-full.mkv` 的 1,440 个 PTS 在 24 FPS 格点上（误差 ≤ 1 tick），但实际最后一帧 duration 是 **125 ms**。完整 fps12 输出 **721 帧**，按 `1440/24 × 12` 算只得 720；29.97 时会算 1798 而实际 1801。packet duration/format.duration 不能无条件代替 decoder/filter EOF。只有额外明确最后显示帧 duration 与真实 EOF 的格式契约（例如固定帧周期、已完整验证的原始帧流），才可用真实起点与 EOF 整数时间轴直接推导。普通编码文件更稳妥的研究方案是实际首帧 + 真 EOF 尾解码；若尾部无可验证输出、随机访问损坏或缺少 PTS，需要回退。

完整计划的正确性还要求 FPS 输出在 `[k0,H)` 连续、没有输入时间戳断点和未覆盖的时间滤镜状态。Open GOP、损坏/丢失帧、解码器补 PTS、interlaced/multi-picture packets、TS 断点和 fps 前的时间滤镜没有被证明。空间 scale 可以逐帧应用，本实验衍生视频使用了 256×144 scale；已有 Bunny 保持 640×360。

额外发现的 A/V metadata 风险：`sources/bunny-derived-vfr-delayed-video.mkv` 的实际首 video packet PTS 是 **5125 × 1/1000 = 5.125 秒**，音频起点约 −0.021 秒；但 ffprobe 的两个 stream.start_pts 都是 −21，format.start_time 也是 −0.021。只比较 stream.start_pts 会把真实的约 5.146 秒 delay 误判成 aligned。但此特定 H.264 文件在 Media 默认 probe 下 pix_fmt 未知，旧 v2 会先由 8-bit admission 拒绝；把 delay 改成 1.125 秒则默认 probe 会修正 video.start_pts，旧版本也会拒绝，不能把两者称为已触发的错误导出。随后使用头部可知 bgr24 格式的 rawvideo Matroska + 显式 fps12，实际确认冻结旧 adapter 可以通过门禁并导出丢失 delay 的文件；独立 maintenance 记录见 [maintenance/README.md](maintenance/README.md)。

复现：

```bash
.venv/bin/python benchmarks/fps_seek_study.py --output results/fps-seek-study-new
.venv/bin/python benchmarks/fps_seek_study.py --stage planning --output results/fps-seek-study-new
.venv/bin/python benchmarks/fps_seek_study.py --stage planning-cost --output results/fps-seek-study-new
```

证据：[metadata.json](metadata.json)、[results.jsonl](results.jsonl)、[summary.json](summary.json)、[paired-costs.json](paired-costs.json)、[planning/results.json](planning/results.json)、[planning/full-count-cost/results.json](planning/full-count-cost/results.json)、[audit.json](audit.json)。每个完整真值与窗口的 framehash、原命令及 stderr 都保存；原始和两次扩展的 runner 快照对应各自 metadata SHA。所有 frozen src 前后摘要相同。
