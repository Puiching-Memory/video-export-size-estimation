# 研究发布状态与阶段同步

2026-10-04 已独立复核的研究 checkpoint 是
`d8b0833712483989b5756666cfcdeb78bd1662cc`。GitHub 分支
`research/budgeted-export-engine` 与本地该提交相同；[草稿 PR #1](https://github.com/Puiching-Memory/video-export-size-estimation/pull/1)
保留复审入口，尚未合并 main。[该提交的 PR CI](https://github.com/Puiching-Memory/video-export-size-estimation/actions/runs/37192393149)
已完成且成功。本页记录这个已核验的历史快照；此后最新 HEAD 和待发布标记由下面的只读命令报告。

## 已发布的六个研究提交

| 提交 | 范围 |
| --- | --- |
| `04ead54` | 三轴预算式视频导出引擎、可复现诊断 |
| `b1e4827` | 预留视频语料、AOM 来源及长片流程 |
| `d300d1f` | 预算会话与可精确复用的独立分段导出 |
| `c8134d5` | 已加载编码器构建验证、输入变动及相位错位拒绝 |
| `0c4343c` | 冻结的原生编码器与长视频研究记录 |
| `d8b0833` | 自适应两阶段抽样源码、注册证据及独立数值重建 |

五个既有提交与新增自适应 checkpoint 的原始 SHA 均已在 GitHub 重建并核验，没有重写历史。
自适应增量包含 18 个文件，注册哈希、源码封存字节和已提交 README 的三条新增入口均已核验。
既有产品测试记录为 144 项通过、约 139 秒；发布源码快照的 51 个 Python 文件与新增两份源码
均通过 Ruff check/format。以上不表示工作区全部研究文件、媒体、中间产物或未提交配置已经上传。
大量后续 native 阶段仍只在工作区中，必须继续按可复审的小批次同步。

## 后续 native 阶段的证据边界

这些阶段尚未纳入上面的已发布研究 checkpoint；下表是阶段事实摘要，不是新的性能评测。

| 机制 | 已有事实 | 失败或尚未实际验证 |
| --- | --- | --- |
| Sparse v3 | 真实 8 次 encode、8 次 probe、8 次完整非空像素 decode；记录完整 CPU 为 15.610587 秒 | 不能从这次查询复现推导新来源精度、通用成本上界或速度优势 |
| VQ | 编译链接与 ABI 检查成功；09:39 的 keeper 检查仍为 S、FDSize 64 | 尚无真实 B8 目标的媒体验证；链接或 ABI 成功不能代替目标码流复现 |
| MultiRF | 原 SHA 目标重建；完整费用记录为 6.228602 秒 | 新目标 owner 在 09:39 检查时已为 zombie、FDSize 0，ELF 已丢失；新版三 RF 媒体路径尚未实测 |
| Cosmos SOURCE4 / AUX-v3 | SOURCE4 处理计数 2400，执行成功；AUX-v3 startup 门通过，worker 全费 CPU 0.216676 秒包含失败 cleanup | SOURCE4 的 owner 后来为 zombie，17 个 FD 已丢失；AUX-v3 在 lost-input 阶段失败，编码器未启动，没有取得 a/Y |

completion/final-freeze 文件也可能封存 FAIL、拒绝或未完成验证。仅看见封存标记，不能把它计为科学成功。
应保留失败与已付费用，先解决持有者生命周期、输入身份和真实目标验证，再讨论后续机制或性能。
这些成功记录不表示当前 ELF 可用或 READY。EOF 型持有进程无法保证跨 agent 完成后的存活；
统一的 signal keeper 与有界持久 custody 仍待完成并实际核验，不能从一个仍存活的 keeper 推广为整体已解决。

## 每阶段同步流程

阶段完成封存后，立即运行：

```sh
python benchmarks/research_publication_status.py --max-files 20
python benchmarks/research_publication_status.py --remote --require-synced --max-files 20
```

第一条仅比较本地缓存的 origin ref，不能证明实时远端相同。第二条通过 `gh api` 只读查询实际
GitHub 分支；认证或网络失败时返回“未验证”，不假称同步。`--require-synced` 仅检查 HEAD 是否相同，
不表示工作区所有研究证据已经发布。脚本只读取 Git 元数据和文件名，不打开媒体、gzip 正文、结果值或风险标签。

报告里的 `sealed_checkpoint_files` 列出已跟踪与未跟踪的 completion/final-freeze/source-freeze
标记，`status_not_inferred=true` 明确不推断 PASS。`absent_from_comparison_commit` 和
`staged_marker_paths` 提示哪些封存入口尚未进入比较提交。脚本不读取正文，明确不检查未暂存的正文变动。
标记已发布也不保证同一 family
的依赖、源码和全部证据齐全；复审时仍须核对注册清单及冻结哈希。默认只显示前 20 个路径，计数覆盖全部标记。

每次只选定完整、可解释的小批源码、文档和证据；检查冻结绑定、单文件大小、磁盘预留、链接和增量风格，
然后明确列出路径提交。不得自动 `git add -A`，不得把全部未跟踪文件或活跃配置一并提交。
上传到同一研究分支后，再次使用 `--remote` 核验 HEAD，记录 commit/PR/CI 链接，并在阶段汇报中交代
未发布的剩余内容。Git transport 认证失败时可以使用已验证的 GitHub Git Database API 路径；仍要核验真实 ref，
不能根据误导性的 “Everything up-to-date” 宣称上传成功。脚本不执行 add、commit、push、fetch、强推或合并。

盲测标签和封存结果不得用于回改已冻结的方法。自适应抽样目前是虚构有限总体上的点估计证明，
不证明真实 native whole-cost 上界、任意停止后的可靠区间、真实影片精度或 SOTA。发布和 CI 成功只说明
相应提交可复核及现有软件检查通过，不能替代新媒体目标、成本与独立评测证据。
