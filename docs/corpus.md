# 工业相关视频测试语料 industrial-v1

这次扩展增加 **24 个上游视频文件 + 1 个 90 秒变帧率衍生案例**，保留原来的 9 个开发诊断案例。新增上游文件约 17.06 GB；加上两个格式修正副本和衍生视频，本地约需 20 GB，建议预留 23 GB。

对齐的依据是 [AOM 公共 CTC 测试序列](https://media.xiph.org/video/aomctc/test_set/)和 [Netflix Open Content](https://opencontent.netflix.com/)。这是 **CTC 素材子集 + 导出工作流补充**，没有执行完整官方 CTC 的编码配置、质量测量和码率比较，也不是工业认证。CTC 用于编码器比较，不能单独证明本项目的文件大小预测准确率。

本次实际执行的逐片参数、全片解码、VFR 时间戳、短导出及自动化测试见 [云端验证记录](../results/corpus-2026-10-03/README.md)。

## 已落地的内容

| 类别 | 文件 / 场景 | 覆盖 |
| --- | --- | --- |
| AOM a1_4k | Neon | 原生 3840×2160、29.97 fps、10-bit SDR |
| AOM a2_2k | Motorcycle、MountainBike、TreesAndGrass、Vertical Carnaby、Vertical Bees、WalkingInStreet | 实拍运动、细密纹理、城市、自然；1080p 横屏/竖屏；29.97/30/59.94 fps |
| AOM b1_syn | Glass Half | 1080p24 动画 |
| AOM b2_scc | Debugging、Spreadsheet、SceneComposition、MobileDeviceScreenSharing | 真实代码、表格、合成软件、手机共享；15/30 fps；含 1078×2220 非典型尺寸 |
| AOM e_nonpristine | Noise Ocean、Noise Soccer、Shaky Walk、Artistic Concert、Artistic Intro、Shaky Baseball、Shaky Fireworks | 游戏、水面效果、演唱会、图形、抖动、烟花；1080p/4K；25–60 fps |
| AOM hdr2_2k | Cosmos Caterpillar | 2048×858p24、10-bit、PQ；发布方明确给出 BT.2100 / limited range 解释 |
| Netflix 长片 | Cosmos Laundromat SDR / HDR-labelled preview、Meridian SDR / HDR-labelled preview | 每个版本约 12 分钟；2K 动画、4K59.94 实拍、多镜头；两个 SDR 版本带 AAC 双声道 |
| 自建工作流衍生 | Cosmos VFR 90s | 保持时间戳，24→12→24 fps；90 秒、1,800 帧、AAC 双声道 |

20 个 CTC 序列均为 130 帧，时长约 2.17–8.67 秒。它们保留上游分辨率、帧率、像素和位深。四个 Netflix 文件保留上游 MP4 字节，是已经有损压缩的分发版本，不能标为无损母版。相同影片的多个版本不代表多个独立来源。

`Noise Ocean` 是 Hellblade 游戏画面，`Noise Soccer` 是 PES 2017；没有将它们当作真实海洋摄影或体育转播。`Shaky Baseball` 是球场场景，不能仅凭文件名声称覆盖高速棒球动作。

## 实测发现的来源问题

1. **文件名和画面方向不一致。** `WalkingInStreet_1920x1080_30fps.y4m` 的实际分辨率是 **1080×1920**，清单使用读取到的参数。
2. **两份上游 Y4M 尾部多一个换行。** `Noise Soccer` 和 `Shaky Baseball` 与官方 MD5 完全一致，但在 130 个完整帧后多出 `0a`，FFmpeg `-xerror` 严格解码报错。保留原件；核对每个帧头、帧长度和尾部后，另存 `.canonical.y4m`，只移除该字节，前后 SHA-256 都被锁定。运行清单指向副本。没有删帧、改像素或掩盖原文件错误。
3. **HDR 文件名不等于 HDR10。** Netflix 两个带 `HDR_P3PQ` 文件名的 MP4 实际是 **H.264 8-bit、无音轨、无嵌入色彩原色/传递函数标签**。按发布方语义保留为边界输入，不能用于 HDR10 合规验证。另行加入具备明确 10-bit PQ 说明的 Cosmos Caterpillar。

当前引擎输出为 x264 8-bit SDR，没有 HDR 保真保证。因此 Neon 10-bit、Cosmos Caterpillar 及两个 HDR-labelled MP4 不进入 `sdr_8bit` 清单。Y4M 的色彩解释来自发布方说明，不能假定 FFmpeg 自动读出了全部色彩元数据。

## 固定来源分组与保留集

在运行预测准确率实验之前固定划分：

| 集合 | 上游文件 | 保守来源组 | 用途 |
| --- | ---: | ---: | --- |
| 原开发集 | 9 | 诊断案例 | 已用于开发，不能作为最终盲测 |
| calibration_reserved | 13 | 7 | 后续独立校准；目前没有误差标签或校准模型 |
| test_reserved | 11 | 9 | 冻结实现后再做留出评估 |

同一影片的 SDR、HDR、CTC 片段、VFR 衍生共享 `group_id` 和 split。同一作者的相关采集也保守地放到同组；这只是防泄漏分组，不是对统计独立性的证明。VFR 衍生归入 Cosmos 的校准组，**不增加独立来源数**。新素材目前仅做字节、格式、解码、时间戳与兼容性检查，没有计算本项目的预测误差或校准区间。

16 个来源组仍不足以支撑广泛工业泛化结论，特别是高覆盖率的有限样本校准。CTC 短片和长片应分别报告结果，不能用大量短片的均值掩盖长片成本。

## 下载和复现

需要 Python 3.11+、FFmpeg / ffprobe；VFR 衍生还需要 libx264 / AAC。数据脚本使用 Python 标准库。全部路径默认相对仓库定位，不依赖启动目录。

```sh
python benchmarks/corpus.py list
python benchmarks/corpus.py fetch
python benchmarks/corpus.py verify
python benchmarks/derive_corpus.py
PYTHONPATH=src python benchmarks/smoke_corpus.py
```

默认存储在 `artifacts/industrial-v1/`，用 `--root /path/to/media` 可以改位置。文件不会提交进 Git。下载使用临时文件、断点续传和大小检查，发布方 MD5（20 个 CTC）以及仓库锁定 SHA-256（全部 24 个）通过后才成为正式文件。S3 ETag 只用于 HTTP 续传，不冒充发布方校验和。

`sources.lock.json` 中 Netflix SHA-256 是本次通过 HTTPS 下载后记录的本地指纹，发布方没有在选定下载处提供同样的 SHA-256 声明。后续下载必须匹配该锁。`--freeze-lock` 只用于明确创建全新快照，不能覆盖既有锁。

`verify` 完整解码主视频和所有音轨，统计实际帧数，核对尺寸、帧率、位深和摘要。错误返回非零；逐片成功记录保存在 `validation/`，最终生成：

- `validation.json`：实际流参数、输入摘要、全片解码结果及工具版本。
- `manifest-calibration_reserved-sdr_8bit.json`、`manifest-test_reserved-sdr_8bit.json`：当前 8-bit SDR 导出基准可用的文件。
- 对应 `-all.json`：包括精度/HDR 边界输入；当前 `benchmarks/run.py` 会拒绝用它混做 8-bit SDR 评测。
- `manifest-derived-calibration_reserved.json`：独立列出的 VFR 工作流案例，沿用父片来源组。

VFR 衍生固定取 Cosmos SDR 的 60–150 秒，中间 30 秒每隔一帧取一帧，保留原时间戳，再编码为 x264 CRF18 / AAC128k。实际核对帧间隔同时含约 1/24 和 1/12 秒，而不是只相信容器帧率字段。生成器记录父片 SHA-256、完整 FFmpeg 参数、版本、输出指纹及变更说明；不同 FFmpeg 构建的输出字节不保证相同。

`smoke_corpus.py` 使用当前 `Media.encode` 在五种代表输入上实际导出两秒 MP4，检查原生尺寸、音轨、时长和解码。包含 VFR 中间的 12 fps 区段；这只证明短导出链路可用，不证明保留了全部 VFR 时间结构或长片预测准确率。

旧 `benchmarks/run.py` 仍是开发诊断基准：固定去音频、参考编码有 120 秒期限，并未改造成整套工业验收器。运行新长片的正式评测前需要按硬件冻结参考编码预算，并另测保留音频的完整导出。此次没有把其旧结果重新标成新语料结果。

## 许可与归属

可追溯入口为 [catalog.json](../benchmarks/corpora/industrial-v1/catalog.json)、[sources.lock.json](../benchmarks/corpora/industrial-v1/sources.lock.json) 和 [notices/](../benchmarks/corpora/industrial-v1/notices/)。清单逐片保存来源 URL、作者原始声明文件及声明 SHA-256；应随分发保留对应声明、作者归属和变更说明。

- 默认原件采用 CC BY 3.0、CC BY 4.0 或 CC BY-SA 4.0，依据各文件对应说明。BY-SA 衍生仍需遵守相应许可义务；媒体不自动继承仓库代码许可证。
- Netflix 长片使用当前 Open Content 下载位置随附的 CC BY 4.0 声明。AOM 中另一些旧 Netflix 序列的说明是 BY-NC-ND，不把当前站点的许可推定到那些旧文件上。
- UVG 的公开数据许可包含 NC，其他部分常用测试片也有研究/标准化用途限制；本次默认包未纳入这些文件。授权选型与技术覆盖同样需要可追溯。

## 尚缺的工业覆盖

当前仍缺长于一小时的真实录屏/直播、真实手机 VFR 采集、复杂多音轨和字幕、HDR10+/Dolby Vision/HLG、相机原始 10/12-bit、交错视频、硬件编码与真实编辑时间线。VFR 衍生覆盖已知时间戳结构，不能替代真实设备异常。严重损坏文件、网络中断和下载摘要错误由故障测试补充，不混入成功解码集稀释指标。

下一轮算法评测应按内容类型、短/长、分辨率、帧率和输入来源分别报告三轴曲线，包含失败率、实际成本和按来源分组的不确定性，再用真实业务分布补足公开素材的偏差。
