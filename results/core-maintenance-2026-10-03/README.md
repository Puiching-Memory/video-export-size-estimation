# v2.1 身份与同步维护

本次修复三项实际可复现的正确性缺陷，没有改点估计公式。源码冻结 SHA-256 为 `b4440e98a293e38ffb1356a51068e1a75677ac0ba7e2e7417ec469d286f0ca3a`，逐文件摘要与代码快照见 [source-freeze.json](source-freeze.json) 和 [source-snapshot.zip](source-snapshot.zip)。先前短片、长片准确率仍对应冻结 v2 `d300d1f` / `f39`，不能将旧延时和预测误差当成本次修复的评测。

| 缺陷 | 实际证据 | 修复 |
| --- | --- | --- |
| 同 ABI 动态 x264 改版仍命中旧缓存 | 在 `/tmp` 复制库，仅改同长度 revision 字符串；FFmpeg 版本输出不变，实际 MP4 SEI/摘要变化；冻结 v2 仍返回旧缓存 | 从零帧 FFmpeg/FFprobe 实际进程映射获取加载文件，完整 SHA-256 绑定缓存和 family；同字节库迁移路径仍允许复用 |
| raw 输入 A→B→A 被错误绑定到 A | 真实 FFmpeg 编码 B，编码后恢复 A 和 mtime，冻结 v2 前后摘要相同却返回 exact；错误缓存还能被后续 prepared 请求复用 | Media 统一检查 device/inode/size/mtime/ctime，并保留最终完整摘要；读取、缓存和发布边界发现变化则拒绝 |
| Matroska 流元数据掩盖实际 A/V 延迟 | header 可识别的 bgr24 rawvideo，实际视频起点 5.125 秒、音频约 −0.021 秒，但两个 stream.start_pts 相同；旧 adapter 导出后将两轨归零 | 首个实际解码呈现帧校验对齐，正确处理 decoder 已应用的 AAC priming；缺少首帧 PTS 时立即拒绝；段及完整产物键更新 |

前两项冻结版实测保留于 [before/native-library.json](before/native-library.json)、[before/raw-identity.json](before/raw-identity.json)。同步缺陷的 [实际产物、完整解码和拒绝证据](../fps-seek-study-2026-10-03/maintenance/README.md) 单列保存。研究最初的两个 H264 延迟案例被旧像素格式或起点门禁拒绝，并不构成错误导出证据；正式回归采用真实可通过旧门禁的 rawvideo 案例。

新原生指纹改变缓存 namespace，也使旧校准 family 无法匹配。旧缓存中已经产生的错误身份绑定不会被新程序继续信任。检查消耗共享墙钟预算，可能减少短预算中的可完成探针数量；这项成本在独立 runtime 记录里测量。没有把本地库 SHA、输入 stat 或测试中的人工数据升级为业务可靠性证书。

最终 [144 项测试全部通过](unittest.log)，耗时 139.163 秒；Ruff check 和 format 全部通过。[runtime 验收](runtime/README.md) 的一次 CLI 发布为 24590 字节、24 秒、576 帧、6 段，容器恒等式 `734+14064+9792=24590`，完整解码和 SHA 校验通过。原生指纹实测 3 次为 0.521 / 0.406 / 0.433 秒，每次核验 213 个加载组件、228416872 字节；没有清 OS page cache，计时来自共享云环境。CLI 内部冷启动端到端为 3.467 秒，输入接收和发布成本单列。

首次全套检查在额外首帧时间戳边界修复前被主动中断，保留于 `provisional-unittest-interrupted.log`，不计为通过。下一次运行的 144 项中仅原预览超时测试失败：它以所有 rawvideo 进程为预览，误触发新增的零帧构建核验。测试现定位实际源预览进程，9 项内容回归通过；原失败保留于 `unittest-preview-selector-failure.log`，最终完整套件重新运行通过。

研究脚本的最后格式调整也单列： [research-formatting.json](research-formatting.json) 确认 Python AST 未变化，原字节保留于 [pre-format-research-runners.zip](pre-format-research-runners.zip)。原始研究结果中的脚本摘要和快照没有覆盖。

复现：

```sh
.venv/bin/python -m unittest discover -s tests -v
ruff check src tests benchmarks
ruff format --check src tests benchmarks
```

新编码器停止机制和 FPS seek 优化仍是独立研究代码，未并入本次维护版本，也未据已评分保留集调参。本次测试不构成 SOTA 或完整工业验收。
