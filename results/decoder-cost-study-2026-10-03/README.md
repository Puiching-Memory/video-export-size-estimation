# 源解码成本诊断（仅开发集）

实际检查 12 个配对案例；首段均在源 EOF 前停止：True；额外 frames 限制省下的解码帧数范围为 0–1。
12/12 文件 SHA、包数及包时序、逐帧解码结果相同。所有 timing 重放与捕获产物相同：True；debug 与 info 产物相同：True。

研究使用现有 motion / grain 开发片和以前已查看的 Bunny；未读取 holdout 误差，也未改动 frozen src。
所有比较保持 codec、输入、滤镜、CRF、preset、GOP 和线程一致；frames_limit 仅额外添加 `-frames:v segment.frame_count`。
时间是直接命令重放的墙钟，不含源准备与输出检查；研究为串行单线程，但正式评测在并行运行，因此不宣称严格延迟优势。
debug + benchmark_all 仅用于源输入帧计数，另存诊断日志，不混入主要墙钟比较。

| 案例 | 源总帧 | 输出帧 | 原命令实际解码帧 | frames限制解码帧 | 文件/逐帧/包相同 | 墙钟比 limit/original |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| motion-segment0 | 576 | 48 | 50 | 49 | 是 | 0.879 |
| motion-segment6 | 576 | 48 | 98 | 97 | 是 | 1.003 |
| motion-segment11 | 576 | 48 | 96 | 96 | 是 | 0.922 |
| grain-segment0 | 576 | 48 | 50 | 49 | 是 | 0.994 |
| grain-segment6 | 576 | 48 | 112 | 111 | 是 | 1.036 |
| grain-segment11 | 576 | 48 | 123 | 123 | 是 | 0.887 |
| bunny-cfr-segment0 | 1440 | 48 | 50 | 49 | 是 | 0.993 |
| bunny-cfr-segment15 | 1440 | 48 | 217 | 216 | 是 | 1.278 |
| bunny-cfr-segment29 | 1440 | 48 | 295 | 295 | 是 | 1.179 |
| bunny-fps24-segment0 | 1440 | 48 | 51 | 50 | 是 | 1.043 |
| bunny-fps24-segment15 | 1440 | 48 | 771 | 770 | 是 | 1.118 |
| bunny-fps24-segment30 | 1440 | 2 | 1440 | 1440 | 是 | 0.927 |

输出等价同时检查文件 SHA-256、video packet 数量及 PTS/DTS/duration/size/flags 摘要、逐帧解码 MD5 和逐帧时间戳记录。
[measurements.json](measurements.json) 保存捕获的完整原命令、重放命令、来源指纹、每次计时、包数、帧哈希与 debug 源解码计数；[metadata.json](metadata.json) 保存前后源码 SHA 和 FFmpeg 版本。
诊断 stderr/framemd5 随报告保存；实际小片产物位于 metadata 的 artifact_root，不随源码包分发。

## 已确认的成本和适用边界

- 主要结论：Existing trim already ends source decoding; explicit frame limit saves at most one decoded frame in these cases.
- 正常 CFR 的额外输入帧位于所需输出之前，来自 GOP 寻址与 trim 的前导边界；单凭无 frames 参数不能认定输出后的整片尾部被解码。
- 显式 fps 转换为保留全片滤镜相位和按帧选择的语义，当前使用 decode_from_start。该前缀成本随段位置增大，frames 限制无法消除；上表 Bunny fps24 中/尾段展示了该区别。
- 明确 frames 限制在本次版本、输入和滤镜上的等价结论取决于上表实际验证，不能推广为任意编解码器、任意滤镜或 FFmpeg 版本的保证。
- 短任务重复、并行正式评测、输入缓存预热和进程启动均影响墙钟。limit/original 从约 0.879 到 1.278；不据此作统计显著或生产 SLO 声明。
- 下一步应分离测量源 seek/preroll、显式 fps 的前缀处理、每段进程启动、ffprobe/摘要/缓存写入、最终 mux 的成本。更激进寻址必须验证整数边界与 fps 相位一致，不能直接把输入 -ss 挪到段起点就宣称等价。

所有源码文件的前后摘要一致，研究中没有改动 frozen src。原 Bunny MP4 的 avg_frame_rate 与 r_frame_rate 不同，所以它只以明确 fps=24 转换进入研究；CFR 路径使用已查看的 bunny-full.mkv。
