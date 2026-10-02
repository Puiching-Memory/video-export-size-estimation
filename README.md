# video-export-size-estimation

面向视频导出的预算式文件大小预估与体积控制引擎。一个请求同时调节计算预算、可靠性要求和文件大小上限；返回实际达到的结果、证据及未满足的约束。

目前是可在 Linux 云端运行和验证的研究实现。支持 **单个视频、libx264 CRF、MP4、主视频流与第一条音轨**，尚未证明达到 SOTA 或完成工业验收。

## 三个控制轴

| 轴 | 请求参数 | 行为 |
| --- | --- | --- |
| 计算预算 | `wall_seconds`、`max_probes`、`threads`、`max_encode_fraction` | 共用截止时间；停止后保留已完成的估计。可以限制新增试编码次数和累计请求编码的素材秒数。 |
| 可靠性 | `relative_error`、`coverage` | 只有经过独立来源校准的区间满足要求，或已获得完整导出，才认为达标。没有校准数据时明确返回 `uncalibrated`。 |
| 体积约束 | `max_bytes`、`max_crf`、`max_candidates` | 在允许的 CRF 范围内搜索；硬上限由完整文件的实际字节数确认。 |

三个轴可以同时设置，但并非任意组合都有可行解。结果中的 `requested` 保存要求，`estimate` 保存实际证据，`unmet` 说明尚未满足的条件。`constraints_unmet` 不表示已经证明所有设置都不可行。

`max_encode_fraction` 是**累计请求编码的素材时长 / 输入时长**，包含被中断的编码请求，缓存命中不计入。它不是 CPU 使用率或完整编码耗时比例。`threads` 控制视频编码线程数，不是整个进程的 CPU 配额。

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
  "sample_seconds": 2,
  "seed": 20261002
}
```

不要求体积上限时可省略 `size`。仅允许少量预估工作时，在 `compute` 中增加 `"max_encode_fraction": 0.2`。程序不会自行放宽请求的误差、大小或 CRF 限制。

`--events` 输出逐行 JSON，先发送 `estimate` 事件，再发送最终 `result`。返回码：`0` 表示请求达标，`2` 表示预算内未达标（仍可能有可用点估计），`1` 表示执行或输入错误。

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

引擎提取全片低分辨率视觉特征，选取内容代表片段，再用随机抽样修正剩余部分的估计。所有片段都使用实际导出参数编码。时间和编码量预算允许时，可以升级为可复用的完整导出。

短片段的编码上下文与完整视频不同，因此抽样方差不能直接充当最终大小的置信度。当前仓库**没有经过真实独立数据校准的模型包**；严格可靠性默认只能通过完整导出兑现。校准接口和有限样本计算已经实现，但不能据此宣称已经验证了 95% 覆盖率。

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

测试包含真实 FFmpeg 编解码、音频保留、硬体积上限搜索、缓存失效与损坏、截止时间、文件发布，以及估计与校准的数学性质。GitHub Actions 运行相同检查。

- [架构与接口边界](docs/architecture.md)
- [评测协议和复现方式](docs/evaluation.md)
- [云端实测结果与已知缺口](results/development-2026-10-02/README.md)
- [文献综述](survey.tex)

尚未支持多轨时间线、任意滤镜图、HDR 保真、硬件编码器或多租户资源调度。常量 `scale`、`crop`、`fps`、`transpose`、`hflip`、`vflip`、`eq` 可通过 `encode.video_filter` 使用。CRF 上限是编码参数边界，不能替代感知质量验收。
