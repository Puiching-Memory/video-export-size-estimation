# 视频语料扩展验证 · 2026-10-03

已下载并锁定 24 个上游文件（17,058,125,828 字节），另生成一个 90 秒 VFR + AAC 案例。加上原有 9 个开发案例，目前共 34 个案例；新增原件只有 16 个保守来源组，不能把多版本当作独立影片。

- 24/24 个规范输入完成全片视频及音频解码，其中两项使用单独的尾字节修正副本；原件保留，原始错误见 [upstream-anomalies.json](upstream-anomalies.json)。
- 20 个 CTC 序列均解码出 130 帧，尺寸、帧率、位深符合冻结清单。
- 4 个约 12 分钟的 Netflix 分发文件完整解码，两个 SDR 版本含双声道 AAC。
- VFR 衍生 1,800 帧，实际间隔同时含约 1/24 和 1/12 秒，音轨及完整解码通过。
- 5/5 个原生尺寸两秒 MP4 导出通过，覆盖代码、非典型竖屏、4K 烟花、长片音频和 VFR 区段。
- 26 项自动化测试、Ruff 检查及格式检查通过。

这是素材完整性和短导出兼容性验证。未运行新语料上的大小预测误差实验，未校准可靠性区间，未执行完整官方 CTC 或证明 SOTA。当前完整解码耗时是在同一云端并发验证过程中测得，不作单任务编码性能排名。

## 文件索引

| 文件 | 用途 |
| --- | --- |
| [inventory.csv](inventory.csv) | 每个原件的实际参数、分组、许可和输入路径 |
| [validation.json](validation.json) | 全片解码、帧数、摘要、色彩和音轨信息 |
| [derived-vfr.json](derived-vfr.json) | 父片、VFR 制作参数、时间戳和输出摘要 |
| [export-smoke.json](export-smoke.json) | 当前导出适配器五种输入的实际输出证据 |
| [summary.json](summary.json) | 机器可读数量与验证结论 |
| [unit-tests.txt](unit-tests.txt) | 26 项自动化测试记录 |

媒体在 `artifacts/industrial-v1/`，不提交到 Git。完整来源、许可、分组规则、上游异常和复现命令见 [语料说明](../../docs/corpus.md)。

## 逐片实际参数

| ID | 尺寸 | fps | 时长/s | 位深 | 音轨 | 用途 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| artistic-concert | 1920×1080 | 25/1 | 5.200 | 8 | 0 | sdr_8bit |
| artistic-intro | 1920×1080 | 30000/1001 | 4.338 | 8 | 0 | sdr_8bit |
| cosmos-caterpillar-hdr10 | 2048×858 | 24/1 | 5.417 | 10 | 0 | precision_or_hdr_boundary |
| cosmos-hdr-long | 2048×858 | 24000/1001 | 758.758 | 8 | 0 | precision_or_hdr_boundary |
| cosmos-sdr-long | 2048×858 | 24/1 | 730.069 | 8 | 1 | sdr_8bit |
| debugging | 1920×1080 | 30/1 | 4.333 | 8 | 0 | sdr_8bit |
| glass-half | 1920×1080 | 24/1 | 5.417 | 8 | 0 | sdr_8bit |
| meridian-hdr-long | 3840×2160 | 60000/1001 | 718.935 | 8 | 0 | precision_or_hdr_boundary |
| meridian-sdr-long | 3840×2160 | 60000/1001 | 719.019 | 8 | 1 | sdr_8bit |
| mobile-sharing | 1078×2220 | 15/1 | 8.667 | 8 | 0 | sdr_8bit |
| motorcycle | 1920×1080 | 30/1 | 4.333 | 8 | 0 | sdr_8bit |
| mountain-bike | 1920×1080 | 30/1 | 4.333 | 8 | 0 | sdr_8bit |
| neon-4k | 3840×2160 | 30000/1001 | 4.338 | 10 | 0 | precision_or_hdr_boundary |
| noise-ocean-game | 1920×1080 | 60/1 | 2.167 | 8 | 0 | sdr_8bit |
| noise-soccer-game | 1920×1080 | 50/1 | 2.600 | 8 | 0 | sdr_8bit |
| scene-composition | 1920×1080 | 15/1 | 8.667 | 8 | 0 | sdr_8bit |
| shaky-baseball-4k | 3840×2160 | 60000/1001 | 2.169 | 8 | 0 | sdr_8bit |
| shaky-fireworks-4k | 3840×2160 | 30000/1001 | 4.338 | 8 | 0 | sdr_8bit |
| shaky-walk | 1920×1080 | 25/1 | 5.200 | 8 | 0 | sdr_8bit |
| spreadsheet | 1920×1080 | 30/1 | 4.333 | 8 | 0 | sdr_8bit |
| trees-grass | 1920×1080 | 30/1 | 4.333 | 8 | 0 | sdr_8bit |
| vertical-bees | 1080×1920 | 30000/1001 | 4.338 | 8 | 0 | sdr_8bit |
| vertical-carnaby | 1080×1920 | 60000/1001 | 2.169 | 8 | 0 | sdr_8bit |
| walking-street | 1080×1920 | 30/1 | 4.333 | 8 | 0 | sdr_8bit |
