# 实际播放起点检查（独立 v2.1 maintenance）

这是开发中的真实同步回归，不是 native holdout 精度评测。原 FPS 研究的 772 个对比、计数证据及源码摘要在修改 src 之前已经封存；维护代码与历史 f39 accuracy 版本分开记录。

真实可触发输入是 [rawvideo-delayed-video.mkv](rawvideo-delayed-video.mkv)：Matroska 的原始 bgr24 视频头部已声明像素格式，所以 Media 无需看到第一帧就能通过 8-bit admission；video/audio stream.start_pts 都报 −21（time base 1/1000）。实际首 decoded video 是 5125/1000 秒，audio 是 −21/1000 秒，真实差 5.146 秒。因为默认 probe 尚未读到延迟视频，avg_frame_rate 为 0，研究明确指定 fps12，按原契约转换。

用冻结历史 adapter 实际完成 12 帧导出：[legacy-lost-delay.mp4](legacy-lost-delay.mp4)，21,123 bytes，精确容器字节相等且完整 decode 通过；首 decoded video/audio 都变成 0，丢掉原 A/V delay。新 adapter 在任何 segment encode 或 complete-cache 复用之前，按实际首 presentation frame 的 PTS 拒绝输入。

[reproducer.py](reproducer.py) 可独立重放；[evidence.json](evidence.json) 保存命令、原始 SHA、冻结 adapter SHA、源 metadata、各 stream 首 decoded frames、旧导出与完整解码证据、新拒绝原因。重放使用冻结 adapter 类以及当前 Media 的 source-stat 防护，容器 writer 没有改变；这是结构/行为回归，不是对历史预测指标的重新评分。

先前两次假设也如实保留：5.125 秒 H.264 Bunny 版本在默认 probe 下 pixel format 未知，被旧 admission 先拒绝；1.125 秒 H.264 版本会被 probe 修正 stream.start_pts，同样已被旧校验拒绝。不能把它们当成实际已触发的错误文件。

维护实现对实际选定 video/audio stream 分别用共享 deadline 执行 ffprobe `%+#8` packet-count 起点解码。正常 AAC MP4 的负 priming packet 被 decoder 的 Skip Samples 处理后首 PTS 为 0；检查不会再次加 skip_samples 或 initial_padding。无法取得有效 decoded PTS 时明确拒绝；较大 decoder startup delay、不可靠时间戳和损坏源不被伪称已对齐。

版本键提升为 `independent-cfr-x264-v2-source-starts`；root 在 complete-cache key 同时加入 segment_pipeline 与 segment_plan。首 decoded presentation start 进入 segment_plan 的可审查 metadata。所有 native 与长片 accuracy 数字仍属于原冻结 f39 源码，不能据本维护补丁推断性能/精度改善。

验证：adapter 的 18 项真实 FFmpeg 检查通过；bgr24 回归最终版本单独再次通过；原有 6 项 engine segmented integration 通过。正常 AAC priming、完整 AAC tail/PCM、无音频、共同非零 origin、cache-plan 复用前检查和 missing-PTS 拒绝均覆盖。新增 pipeline-version 完整缓存隔离检查另保存于 tests/test_engine_segmented.py，由 root 汇总整个 suite。
