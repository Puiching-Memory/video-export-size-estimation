# video-export-size-estimation

面向视频导出的预算式文件大小预估与体积控制引擎。一个请求同时调节计算预算、可靠性要求和文件大小上限；返回实际达到的结果、证据及未满足的约束。

目前是可在 Linux 云端运行和验证的研究实现。支持 **单个视频、libx264 CRF、MP4、主视频流与第一条音轨**，尚未证明达到 SOTA 或完成工业验收。

## 三个控制轴

| 轴 | 请求参数 | 行为 |
| --- | --- | --- |
| 计算预算 | `wall_seconds`、`max_probes`、`threads`、`max_encode_fraction` | 共用截止时间；停止后保留已完成的估计。限制新增试编码次数和请求视频编码秒数；另记录音频和预览成本。 |
| 可靠性 | `relative_error`、`coverage` | 只有经过独立来源校准的区间满足要求，或已获得完整导出，才认为达标。没有校准数据时明确返回 `uncalibrated`。 |
| 体积约束 | `max_bytes`、`max_crf`、`max_candidates` | 在允许的 CRF 范围内搜索；硬上限由完整文件的实际字节数确认。 |

三个轴可以同时设置，但并非任意组合都有可行解。结果中的 `requested` 保存要求，`estimate` 保存实际证据，`unmet` 说明尚未满足的条件。`constraints_unmet` 不表示已经证明所有设置都不可行。

`max_encode_fraction` 是**累计请求的视频编码时长 / 输出视频计划时长**，包含被中断的请求和整个暖编码窗口；缓存命中不计入。它不是完整编码耗时比例。音频计入 `attempted_audio_seconds`，全片预览计入 `content_discovery.elapsed_seconds`，所有处理共用墙钟预算。`threads` 同时限制解码、编码与滤镜线程，不等于操作系统 CPU 配额。

## 快速运行

需要 Python 3.11+、Linux、可执行的 `ffmpeg` / `ffprobe`，以及 FFmpeg 的 libx264 和 AAC 编码器。

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
vsize examples/request.json --cache .vsize-cache --events
```

先将 [examples/request.json](examples/request.json) 的 `source` 改为本机视频路径：

```json
{
  "encode": {"source": "input.mp4", "crf": 23, "preset": "medium"},
  "compute": {"wall_seconds": 8, "max_probes": 6, "threads": 2},
  "reliability": {"relative_error": 0.1, "coverage": 0.95},
  "size": {"max_bytes": 20000000, "max_crf": 32, "max_candidates": 4},
  "sample_seconds": 1,
  "sampling_plan": "content",
  "seed": 20261002
}
```

不要求体积上限时可省略 `size`。仅允许少量预估工作时，在 `compute` 中增加 `"max_encode_fraction": 0.2`。程序不会自行放宽请求的误差、大小或 CRF 限制。

`--events` 输出逐行 JSON，先发送 `estimate` 事件，再发送最终 `result`。返回码：`0` 表示请求达标，`2` 表示预算内未达标（仍可能有可用点估计），`1` 表示执行或输入错误。

加入 `--prepare` 可先复制、完整校验并封印 Linux memfd 输入快照；默认每个快照最多 2 GiB。输入准备成本在暖请求预算外，并单列 `prepared_asset.ingest_seconds`；CLI 的 `end_to_end_seconds` 包含准备与发布。Python 可复用一个仍打开的 `PreparedAsset`，原路径后来改变不会改变上传版本。

加入 `--output export.mp4` 可保存已验证的完整导出。保存时重新验证字节数和摘要，不覆盖已有文件；该复制操作在分析预算之外。统计区间达标但没有完整文件时，不能保存导出。

Python API：

```python
from pathlib import Path
from vsize import ComputeBudget, EncodeSpec, Reliability, Request, SizeConstraint
from vsize.engine import Engine

engine = Engine(Path(".vsize-cache"))
result = engine.run(Request(
    encode=EncodeSpec(Path("input.mp4")),
    compute=ComputeBudget(wall_seconds=8, max_probes=6),
    reliability=Reliability(relative_error=0.1, coverage=0.95),
    size=SizeConstraint(max_bytes=20_000_000, max_crf=32),
    sample_seconds=2,
))
print(result["status"], result["estimate"], result["unmet"])
```

## 实现与验证

默认 `sampling_plan="temporal"` 直接从元数据建时间计划，尽快开始探针。显式 `"content"` 会先运行可缓存的 4 fps、96×54 全片预览，按纹理与时间变化选代表片段；预览包含源解码成本，可能耗尽短预算。其余片段随机抽样，修正固定代表模型的残差。代表数量根据新增探针和视频编码量预算调整。

默认 `encode.export_mode="continuous"` 以连续完整 MP4 为目标。`probe_mode="auto"` 在整个请求开始时固定探针策略：预算能容纳全部代表片段的暖编码时，用 preset/FPS 对齐的 lookahead 和前文；否则使用普通短片段。中央包计量分离初始化 SEI，视频单轨的 MP4 表开销从探针直接解析后外推。GOP 补偿及表外推都仍是经验模型。

显式设置 `encode.export_mode="segmented"` 和 `segment_seconds` 可导出独立段的 fMP4。该模式冻结整数帧计划，每个探针就是最终复用的实际段；音频全片 AAC 编码一次，容器开销精确可加。输出与连续 x264 不同，必须评估体积和画质代价。分段目前只接受 8-bit SDR、CFR 或显式 fps 转换、起点对齐的 mono/stereo AAC。明确的范围见 [核心验收契约](docs/core-acceptance.md)。

[分段请求示例](examples/request-segmented.json) 使用 4 秒段和 20% 请求视频编码预算；这是可调整的导出选择，不保证任意输入都能在所给预算内获得估计或满足可靠性要求。

短片段的编码上下文与完整视频不同，因此抽样方差不能直接充当最终大小的置信度。当前仓库**没有经过真实独立数据校准的模型包**；严格可靠性默认只能通过完整导出兑现。schema 2 校准每个独立来源的全部注册 CRF/变体/采样前缀最大误差，前缀上限和模型完整绑定。95% 有限区间至少需要 19 个独立来源组；现有 7 个不够。旧 schema 1 校准包不用于新策略。不能据接口测试宣称业务覆盖率已经验证。

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

测试包含真实 FFmpeg 编解码、音频保留、硬体积上限搜索、缓存失效与损坏、截止时间、文件发布，以及估计与校准的数学性质。GitHub Actions 运行相同检查。

- [核心验收契约与当前证据边界](docs/core-acceptance.md)
- [冻结 v2 开发评测与分段质量代价](results/core-validation-v2-2026-10-03/README.md)
- [原分辨率独立短片评测：完整参考及同目标基线](results/industrial-core-2026-10-03/holdout/README.md)
- [连续编码启动开销与状态偏差诊断](results/startup-study-2026-10-03/README.md)
- [编码器级上下文停止实验：中央包保持一致并减少未来编码](results/stream-context-study-2026-10-03/README.md)
- [FPS 随机跳转实验：逐帧相位与真实 EOF 验证](results/fps-seek-study-2026-10-03/README.md)
- [构建、输入身份与同步维护修复](results/core-maintenance-2026-10-03/README.md)
- [长片完整参考与预算记录](results/industrial-long-2026-10-03/README.md)
- [实际预算、基线、复用与质量诊断](results/core-validation-2026-10-03/README.md)
- [全局内容发现与未观测突发反例](results/discovery-study-2026-10-03/README.md)
- [架构与接口边界](docs/architecture.md)
- [评测协议和复现方式](docs/evaluation.md)
- [新增工业相关视频语料：AOM CTC 子集、长片与 VFR](docs/corpus.md)
- [新增语料的完整解码与导出验证记录](results/corpus-2026-10-03/README.md)
- [云端实测结果与已知缺口](results/development-2026-10-02/README.md)
- [文献综述](survey.tex)

尚未支持多轨时间线、任意滤镜图、HDR 保真、硬件编码器或多租户资源调度。常量 `scale`、`crop`、`fps`、`transpose`、`hflip`、`vflip`、`eq` 可通过 `encode.video_filter` 使用。CRF 上限是编码参数边界，不能替代感知质量验收。
