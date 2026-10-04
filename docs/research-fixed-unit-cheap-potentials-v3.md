# 固定单位廉价熵 potential：SOURCE v3 gate

目标是让 nested estimator 的 `a_i` 只在被抽中的 F 上计算。既有理论为
`K + Σ_U μ0_i + HT_F(a_i−μ0_i) + HT_M(Y_i−a_i)`，`M ⊆ F`。
同一个单位的 `a_i` 必须是抽样前定义好的固定 potential；它不能因同次选中的
其它单位、共享 lookahead/编码实例、执行顺序或缓存状态而改变。现有纯理论
family 保持不变；本实验不重跑其已有结果，不读取真实 a、medium Y、NORMAL
export 或 score，不宣称已有估计精度、速度或工业级收益。

固定 SOURCE 是 grain，384×216，24/1 fps，SAR 1:1，准备范围显示帧 0:576。
注册 descriptor 来自 corrected fast-proxy registration 的 grain SOURCE 字段，
其 compressed source SHA 为 d9f78cc9c2b1c41858368f4033879dfd5538735b1b01d1213dbe84abf676b529，
准备后 I420 SHA 为 887c5b5348abaa95f87e69d90fd0cdd1c1c0c644cf9bfd898896b9653c1ab792。
SOURCE-only analysis receipt 明确 analysis_only=1、fed576、produced/central/future
全部为0，TRACE SHA 为457b5ed4c2d8c7e407082a5b15dadca94653eb56d748c58f5023667b2b513f2b。
TRACE 中 IDR 显示帧为0、177、325、501，不能用 keyint250 代替真实切点。

四个固定单位是 [0,177)、[177,325)、[325,501)、[501,576)。每个单位使用固定
左右各48帧 SOURCE context，截到准备范围边缘。单位1固定输入 [129,373)。
每个被选单位有独立、新鲜的普通 FFmpeg/libx264 veryfast、CRF23、threads1
owner，完整读完自己的固定输入并 local EOF/flush。没有额外 preset override。
廉价 potential 是该独立输出里属于单位中央显示帧的实际 AVCC VCL 字节总和，
每个 VCL NAL 加4字节长度。它无需等于原连续 medium 的 Y；M 的 exact Y 仍必须
来自原连续目标，不能用本地 warm encode 代替。这一 gate 不实现 medium M。

ROOT review 后的一次 actual 验证预先规定 F contexts=(0,1) 和 (1,3)，两者各按
正、反顺序独立编码。总计8次廉价编码，共享单位1跨context/order重复，单位2
从不编码。缓存按 source/pixel/runtime/profile/unit/window raw SHA 完整 key
保存不可变 scalar+hash；同key值、逐帧整数VCL hash、全局VCL fingerprint、avcC、
非空完整MP4 byte/hash、完整local输出decoded I420 hash任一变化，都使整次draw
poison并弃权。缓存命中可复用同一不可变对象；此重复验证故意仍编码，费用照计。

Actual 只将 SOURCE compressed、所选固定window的raw union、一次unit window、
一次输出媒体保存在有明确cap且kernel sealed的RAM memfd。完整SOURCE只decode一次，
准备EOF、576帧、71663616 raw bytes和完整raw SHA都须核实。另有8次packet probe
和8次local输出pixel decode，费用全部计入。持久目录不存输出MP4或raw视频。
大输入不能用更短解析范围、silent truncation或半成品hash伪装完成。

SOURCE的纯协议用invented bytes/values验证：严格int/bool/extent/schema guards、
真实kernel seal/FD关闭、AVCC非空及截断、same-key immutable reuse及poison、
独立stdio、错误退出全wait4 fee、逃出进程组的orphan清理与wait4全费用。其Python
control子进程不是codec调用。SOURCE stage不打开media，不启动FFmpeg/ffprobe/compiler。
这些协议通过不意味着真实cheap编码固定性已通过。

External parent是唯一worker wait4 owner，worker是自身native子树的wait4 owner和
subreaper。所有子进程stdin=DEVNULL，stdout/stderr各自pipe、输出读取有上界；
PDEATHSIG=SIGKILL并检查父进程死亡race。失败、deadline、逃逸子进程和未完整输出
保留已付费用前缀；必须ECHILD且无owned descendants才能报完整child-tree CPU。
若无法收完子树，费用标UNKNOWN。worker的whole wait4包含startup/imports、SOURCE
读取、hash/closure、复制、所有codec/probe/pixel decode、receipt fsync、cleanup和exit。
内部细项不可再加到外部whole费用。parent startup/final fee write及native aggregate
RSS峰值未度量，明确UNKNOWN，不宣称免费或工业内存证据。

新family采用常规USTAR和普通gzip的 `closure.tar.gz`。archive完整包含Python
loaded-file、SOURCE static native executable/DSO hash manifests和SOURCE input hash
manifest；canonical源码/doc保留原位置，同时tar包含完整固定SOURCE快照；两份都计入family。
closure在注册前全部是已知SOURCE bytes，因此以完整固定gzip extent计预算，actual未知
费用和值receipt则仍按完整schema上界预留。它不裁tar
header、不用私有archive、不把普通receipt自限8KiB。native closure来自已存在的
SOURCE observed components并在新SOURCE stage静态核验每个文件，不启动工具。
未观测的动态plugin不在此gate支持范围。

ROOT在创建family/注册/actual前已授权将此新family从256KiB扩为384KiB；
原96KiB纯理论family保持不变。聚合family预算对canonical文件及所有结果按filesystem block计数；先核算完整
worst-case allocation，再创建immutable SOURCE registration。预留32KiB final receipt，
每次持久写入前保留128MiB磁盘空间。Actual前必须ROOT exact reviewed gate绑定
registration、parent/source/broker bytes；parent先写exclusive attempt，任何失败都不
自动重试。SOURCE已有freeze不能重写。Final gate只可报告此开发fixture的机制证据；
下一步仍须验证 exact continuous M、成本预算可行性与随机设计下的真实accuracy。

首次未写盘检查已通过25项invented protocol，但发生在注册前，不能称registered ONE。
该次精确planning/kernel/Python control费用未保留，标UNKNOWN；其0媒体调用可核实。
SOURCE freeze只固化完整注册，随后仅一次registered metadata/schema fixture，核验
完整archive及身份和真实SOURCE descriptor；不重复已通过的相同协议函数。

此v2保留v1的四份canonical及a96b注册和全部fee，不运行已BLOCK的v1 actual。
独立fullread review SHA为7ac63e0597a89e8a4600a6b300d9b8a124d39567a7f9abbdc6fa41aeaf33dd42。
v1 worker错误继承native32MiB hard FSIZE，但所选raw union需要61,710,336 B；
v1 parent还把ProcessFailure后未返回的stdout空buffer误标complete空串。
v2仅更改resource角色、parent receipt语义及行政path/import绑定，potential、
recipe、math、profile和cache实现不变。Python worker128MiB FSIZE允许完成union，
每个native child仍显式32MiB FSIZE，不试图从hard32MiB子级自行提限。失败parent
complete stdout hash/byte字段为null，实际prefix count/hash保留在whole fee。

full receipt schema在freeze前核算。本v2的ONE registered SOURCE kernel metadata
fixture在128MiB worker中对invented memfd pwrite到61,710,336 B，明确是带holes的
单byte元数据范围检验，不是raw pixel/decoder/媒体证据；子级Python control以native
32MiB资源role尝试超限写入，核实内核EFBIG并产生非空stdout和stderr后失败退出。
fixture通过whole_receipt验证complete null、真实prefix以及全部wait4/ECHILD，保留
内部child费用和外部worker全费用而不重复加总。0媒体、0FFmpeg/codec实际。
v2新family独立384KiB含32KiB final，128MiB磁盘reserve；ROOT specific review gate
仍是actual必要入口。本fixture不重跑v1已通过25项协议或旧metadata fixture。

本v3保留v2唯一actual FAIL及全部已付费用：SOURCE raw identity/576 EOF与496frame
union成功，unit0 local encode/probe成功，但旧parse首schema guard失败。旧JSON
正文/keys未保存，所以其具体schema UNKNOWN；v3不把public SOURCE推论当旧实证。

v3从官方FFmpeg n7.1 `fftools/ffprobe.c`核证standard empty optional sections，完整
SOURCE单独作为常规tar member SOURCE/ffprobe-n7.1.c保存，SHA为
add82e4107e153f232362602e818e7804dd08abbb0e4a2db94cb43781b3beccc。
URL: https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1/fftools/ffprobe.c 。
同名stream sections包括program/group后代；match_section(4230:4247)遍历全部匹配，
check_section_show_entries(4618:4624)递归后代，SET_DO_SHOW(4666:4684)因此可开启
programs/groups；show_programs(3531:3548)/show_stream_groups(3780:3793)先写
array header然后遍历集合，JSON writer(1693:1752)令空集合输出[]。
只接受streams/packets必需字段和空list programs/stream_groups；未知key、非空
optional sections、多stream、空packets或错误type均继续拒绝，AVCC/geometry/PTS
等下游强约束保持原实现。

每次完整probe退出后、JSON/semantic解析和FD关闭前先留full byte count/SHA、
complete旗标与最大4096B实际raw prefix(base64)。随后记录实际topkeys、stream/
packet counts、section types、unit/local窗口及bounded stream contract/keys。
大型异常字段仅保留明确标记的bounded字段或完整field hash，不把prefix冒充
完整正文；JSON失败也保留原始prefix/full hash。诊断schema worst在freeze前
计算，并给全部8次probe持久receipt留足空间。

v3 SOURCE fixture只验证public标准schema与invented JSON诊断positive/negative，
不调用媒体工具、不重跑v2 kernel。v2已冻结worker128MiB/native32MiB资源与失败
prefix/wait4证明复用。Ruff只格式化新v3文件，AST确认encoder/raw/key/cache/math
/profile无变化。新family512KiB含32KiB final。所有启动显式LANG=C LC_ALL=C，
避开v2先前C.UTF-8闭包预flight失败。ROOT预授权新v3经过SOURCE审阅后唯一
grain mechanics实际attempt；任何真正actual FAIL均无自动重试。0mediumY/scoring。
