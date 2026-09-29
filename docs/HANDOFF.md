# Build-Bench 参赛 Handoff

更新：2026-09-29 · 仓库：`git@github.com:snozzz/build_bench.git`（`main`）· 最新提交 `d88e490`

## 0. 一句话现状

Agent 骨架、补丁打包机制、本地评测流水线都已打通，并在**官方验证器**上完成了第一个真实修复
（r-cran-digest，x86_64→aarch64，官方构建成功并产出 arm64 `.deb`）。确定性规则修复了 12 个 dev
用例（本地构建 12/12 通过）。LLM 修复循环已写好并用假模型测通，**还没接真实模型**，这是下一步提分的关键。

## 1. 比赛要点（我们的理解）

| 项 | 内容 |
|---|---|
| 任务 | 提交一个 **LLM Repair Agent**（不是补丁）。包在源架构能构建、在目标架构失败，Agent 修改源码树让目标架构构建成功 |
| 架构方向 | x86_64 ↔ aarch64 ↔ riscv64 双向；公开 dev 集只有 x86_64↔aarch64（各 100） |
| 提交物 | `agent-submission.zip`：`agent.yaml` + `src/` + `requirements.lock` + `README.md`，托管 Python 3.11 |
| 运行时 | 每个 Case 起一个实例：`/workspace/input` 只读（任务元数据+失败日志），`/workspace/work/repo` 可写，`/workspace/output` 写 `agent-result.json` |
| 本地限制 | starter kit 本地跑 Agent 用 `--network none`、1 CPU、1 GiB 内存、无 Docker socket；**协议 v0.1 没有构建反馈**（只能一次性诊断+修改） |
| 判分 | 平台对比 worktree 生成标准 `repair.diff` → 干净副本上 `git apply` → 官方目标架构构建 → 产物校验。主指标 Verified Build Success Rate；时间和 token 是次要指标 |
| 禁止 | 用例专属答案/预生成补丁；压制失败（`|| true`、整体跳测试）、删测试、假产物等"绕过构建"；代码里放密钥 |
| 时间线 | **10/9 报名截止**；11/13 选定最终版本；11/20 前公布结果 |
| 隐藏集 | 1000+ 用例，来自更广的生态，**会包含 riscv64 和可能的 RPM 包** |

仍未知（需要报名后用 Hosted Smoke Test 确认）：官方 LLM 调用方式/凭据机制、每用例超时与 token 预算、
隐藏集 Debian 用例 worktree 的确切目录结构、官方标准 diff 是否正确处理"新建文件"。

## 2. 核心技术思路

### 2.1 修复必须是"文本补丁"，这决定了整个打包策略

1. 官方标准 diff **拒绝二进制改动**（starter kit `generate_patch.py` 遇到 NUL/非 UTF-8 直接报错），
   所以 `.debian.tar.xz` 这类压缩包改不了。
2. 验证器（我们下载了 `buildbench-validator-context` 源码）只从 `.dsc` 构建（obs-build deb 后端），
   对 `input/**` 做 `git apply`。
3. 推论：隐藏集 Debian 用例给 Agent 的一定是**解包后的源码树**（与网站 Development Validation 的
   "unpacked tree" 上传格式一致），平台再用 `dpkg-source -b` 重新打成 `.dsc`。

`dpkg-source -b` 的行为我们逐一实测过（dpkg 1.22.6）：

| 做法 | 结果 |
|---|---|
| 直接改上游文件 | **失败**：`aborting due to unexpected upstream changes`（除非平台加 `--auto-commit`，未知） |
| 在 `debian/source/options` 加 `single-debian-patch` | 成功，但多数包没有这个文件 → 要新建文件 |
| 新建 quilt patch + 追加到 `series`，上游文件还原 | 成功（`dpkg-source -b` 会先应用未应用的 patch）；**但依赖官方 diff 支持新建文件** |
| **把 hunk 追加到 series 里最后一个已有 patch** | 成功；patch 已应用状态（`.pc/applied-patches`）保留上游改动，未应用状态还原上游改动，两种都通过 |

最终策略（`agent/src/finalize.py`）：
- 3.0 (quilt) 且已有 patch → **追加到最后一个 patch**（不新建任何文件，200 个里 131 个走这条）
- 3.0 (quilt) 但没有 patch → 新建 `debian/patches/archfix-<arch>-build.patch` + `series`（57 个）
- 3.0 (native) / 1.0 → 直接改文件（dpkg-source 自己记录）

LLM 和规则引擎因此可以**直接改上游源码**，由 finalize 统一转成 quilt 形式。

### 2.2 踩过的坑（都已修复，队友改代码时要注意）

- **starter kit 的 diff 生成器对新建文件不写 `new file mode`**，`git apply` 会报
  `dev/null: No such file or directory`。这是我们尽量不新建文件的原因。
- **换行**：`Path.read_text()` 会把 CRLF 变成 LF，导致补丁上下文不匹配；所有读写都用字节解码。
  `str.splitlines()` 还会在 `\f`、`\v`、单独 `\r`、U+2028 处断行，官方生成器用的就是它，所以
  `ChangeSet` 拒绝修改含这些字符的文件。
- **日志巨大**：有个 dev 日志 2.4 GB，而 Agent 只有 1 GiB 内存 → 永远只读日志尾部（8 MB）。
- **ssh 里用 `pkill -f <pattern>` 会杀掉自己的会话**（pattern 出现在远端命令行里）→ 用 `pkill -f "[p]attern"`。

### 2.3 Agent 流水线

```
/workspace/input ──> context.py   任务/架构/包布局识别（deb 解包树 / .dsc / RPM spec）
                   buildlog.py  读日志尾部，切出最终错误级联、之前的首批真实错误、出错源码位置
                        │
                        v
                   fixers/      确定性规则（目前：dpkg-gensymbols 符号文件修复）
                        │
                        v
                   llm/         OpenAI 兼容客户端 + 11 个工具的有界循环（未配置时跳过）
                        │
                        v
                   finalize.py  上游改动 → quilt hunk（见 2.1）
                        │
                        v
             /workspace/output/agent-result.json + archfix-report.json (+ transcript)
```

- 所有写操作都走 `ChangeSet`（`agent/src/changes.py`）：限制在包目录内、拒绝 `.pc/`/符号链接/二进制/
  非 UTF-8/特殊换行文件、保留 CRLF、出现内部异常时全部回滚，保证不会留下半个补丁。
- 任何情况都写 `agent-result.json` 并以 0 退出（非 0 退出算 agent_error）。
- 纯标准库，`requirements.lock` 为空。

### 2.4 确定性规则：符号文件修复（`agent/src/fixers/symbols.py`）

dpkg-gensymbols 失败时日志里有精确的 diff，丢失的符号行是 `+#MISSING: <ver># <原行>`。
规则：在日志指明的 symbols 文件（找不到就搜所有 `debian/*.symbols*`）里找到原行，加上
`(optional)` 标签（保留已有标签，如 `(c++|optional)`）。只有检查等级 4 把"新增符号"当错误时，
才以 `(arch=<目标>)` 加入新符号。这是 Debian 维护者处理架构相关符号的标准做法，不属于绕过。

### 2.5 LLM 修复循环（`agent/src/llm/`，已完成、未接模型）

- **配置全走环境变量**，包里不含任何密钥：`ARCHFIX_LLM_BASE_URL` / `ARCHFIX_LLM_MODEL` /
  `ARCHFIX_LLM_API_KEY`（兼容 `BB_LLM_*`、`OPENAI_*`），预算 `ARCHFIX_MAX_STEPS`（默认 40）、
  `ARCHFIX_MAX_SECONDS`（1200）、`ARCHFIX_MAX_TOKENS`（1.5M）。
- **首条消息是预组装的"病历"**（约 14k 字符）：包信息、迁移方向、失败命令、日志摘录、
  `debian/control`、`debian/rules`、`series`、错误行附近的源码片段。目的是在没有构建反馈、
  token 计入次要指标的前提下，减少模型盲目翻文件。
- **工具**：`list_dir`、`read_file`、`search`、`find_files`、`search_log`、`read_log`、
  `replace_in_file`（精确唯一替换）、`write_file`、`show_changes`、`revert_file`、`finish`。
- **系统提示词**写了硬约束（不能构建、不许绕过、不改版本/changelog、rules 用 TAB）和跨架构知识：
  x86 专属头文件/内建函数/编译选项、arm64 上 `char` 默认无符号、FMA 收缩导致浮点差异、
  long double 差异、riscv64 需 `-latomic`、symbols 文件架构标签、multiarch 路径等。
- 用 `tools/fake_llm.py`（按脚本回复的假 OpenAI 端点）测通：搜索→读文件→搜日志→修改→
  错误路径被正确拒绝→看 diff→finish，产出的补丁能打包。

## 3. 验证体系（由快到准）

| 层级 | 工具 | 覆盖 | 现状 |
|---|---|---|---|
| 1. 补丁往返自测 | `tools/selftest_finalize.py` | 全部 200 个 dev 用例：做改动→finalize→标准 diff→干净树 `git apply`→`dpkg-source -b`→再解包确认改动还在 | **200/200 通过** |
| 2. 批量跑 Agent | `tools/harness.py batch` | 按官方目录准备工作区、跑 Agent、用 starter kit 生成器出 diff、回放+打包 | 200 个全跑，无崩溃；12 个产生修复 |
| 3. 本地近似构建 | `tools/localbuild.py` / `harness.py build` | `ubuntu:<series>` 容器按目标架构构建（amd64 原生，arm64 走 QEMU），仿 Launchpad：非 root、amd64 用 `-b`、其他用 `-B` | 符号类 **12/12**：原版失败（均为 dpkg-gensymbols 错误），修复版构建成功 |
| 4. 官方本地验证器 | `harness.py validate` + 官方 rcran 包 | 仅 r-cran-digest 一个用例有完整冻结环境 | 原版复现官方失败（`xmmintrin.h`），修复版 **官方构建成功**（约 35 分钟，QEMU） |
| 5. 网站 Development Validation | `tools/make_bundle.py` 打包 | 官方冻结环境，全部 dev 用例 | **需先报名** |
| 6. Hosted Smoke Test | 上传 `bb package` 的 zip | 确认官方目录结构、LLM 网络策略、新建文件支持 | **需先报名** |

注意：第 3 层用的是**现在**的 Ubuntu 仓库，依赖版本和官方冻结环境不同，只作参考。

## 4. Dev 数据集画像

- 200 个用例：100 x86_64→aarch64，100 aarch64→x86_64；12 个 Ubuntu series（resolute 42、noble 35、plucky 30…）。
- 源码格式：3.0 (quilt) 189、3.0 (native) 8、1.0 3。全部在 build 阶段失败。
- 主要失败类型（`tools/analyze_cases.py`，粗分类）：
  - 符号文件不匹配：12（已由规则解决）
  - 测试失败：约 40 个（Go 测试超时、ctest/meson/pytest 个别用例、浮点精度等）
  - x86 专属代码：`xmmintrin.h`、`sys/io.h`、`-msse`、`avx2`、`__float128`、x86 汇编等（适合 LLM）
  - 链接错误、C/C++ 编译错误（`-Werror` 新告警、类型问题）
  - 聚类：7 个 kinetic 的 Haskell 包在 amd64 上 `ld.gold: requires dynamic R_X86_64_PC32 reloc`，
    是同一个 GHC 9.0.2 + gold 工具链问题，有希望做成一条规则（还没验证）
  - 难啃的：gcc-*-cross、mingw 工具链、ocaml、rocm-llvm、grub2-signed 等
- 参考：论文里 GPT-5 单次修复约 6%，带 3 轮构建反馈可到 63%。v0.1 没有构建反馈，所以诊断质量最关键。

## 5. 环境与复现

**分工原则**：Mac 只放干净代码；所有构建/测试在 WSL 构建机（Ubuntu 24.04，20 核，7.6 GB 内存，
约 1 TB 磁盘；SSH 访问找 snoz）。每完成一个模块就 push 一次，方便回滚。

构建机目录 `~/bb/`：

```text
build_bench/                    本仓库（由 scripts/sync-to-wsl.sh 同步，rsync --delete）
BuildBench-Agent-Baseline/      官方 starter kit（bb doctor/demo/check/test/package）
data/dev/                       200 个 dev 用例（解压后 2.7 GB）
data/local/                     官方 rcran 本地验证包
ref/validator/                  官方验证器源码（v0.1.0-rc.3）
runs/                           批量运行结果、本地构建日志
```

网络（构建机在国内，这些是踩出来的）：
- GitHub / PyPI 直连可用；`ghcr.io` 不稳；Docker Hub 直连和代理都不通。
- Docker Hub 用 `docker.m.daocloud.io`、`docker.1ms.run` 镜像（已写进 `/etc/docker/daemon.json`）。
- GHCR 用 `ghcr.nju.edu.cn`；**跑 starter kit 前先 `source ~/.bb_env`**（覆盖 `BB_VALIDATOR_IMAGE` 等，digest 与官方一致）。
- Ubuntu 仓库：TUNA（focal/jammy/noble/plucky/questing/resolute），停止支持的 series 用 USTC old-releases。
- Windows 上的 Clash 通过一条反向 SSH 隧道暴露在构建机 `127.0.0.1:7897`（`bb-winproxy` 服务），
  需要代理时 `source ~/.bb_proxy_env`。
- 一次性主机配置脚本：`infra/wsl/setup-host.sh`（Docker、binfmt QEMU、代理服务）。

常用命令：

```bash
./scripts/sync-to-wsl.sh                                   # Mac → 构建机
cd ~/bb && python3 build_bench/tools/harness.py batch data/dev/buildbench-development-cases-v0.1 runs/batch --jobs 6
python3 build_bench/tools/harness.py build runs/batch --which both --parallel 2    # 本地构建（原版+修复版）
python3 build_bench/tools/selftest_finalize.py data/dev/buildbench-development-cases-v0.1 runs/selftest --jobs 8
python3 build_bench/tools/make_bundle.py runs/batch dev-validation.zip --only-changed # 网站开发验证上传包
source ~/.bb_env && cd BuildBench-Agent-Baseline/starter-kit && ./bb check --agent ~/bb/build_bench/agent
# 带模型跑（配置好后）：
ARCHFIX_LLM_BASE_URL=... ARCHFIX_LLM_MODEL=... ARCHFIX_LLM_API_KEY=... python3 build_bench/tools/harness.py case <case-dir> runs/x
```

## 6. 待决策与风险

1. **LLM 选型**（最高优先级）：给出 base_url / key / 模型名即可开跑。官方最终用什么方式提供模型还未知，
   所以客户端按最通用的 OpenAI 兼容协议写。
2. **测试失败类的处理尺度**：规则禁止"压制失败"。建议只允许在目标架构上**精确跳过单个**确属架构相关的测试
   并写注释，禁止整体 `|| true` / `nocheck`。需要团队统一意见。
3. **报名（10/9 截止）**：报名后才能用网站开发验证（官方冻结环境）和 Hosted Smoke Test。
4. **新建文件是否被官方 diff 支持**：影响 57 个无 patch 的包。可以故意上传一个新建文件的版本跑冒烟测试来确认。
5. 隐藏集会有 riscv64（本地可用 QEMU，但很慢）和可能的 RPM 包（目前只做了 spec 识别，没做 RPM 修复流程）。

## 7. 下一步（建议分工）

- **A. 接模型 + 跑 dev 基线**：配好 API → 全量跑 → 用本地构建给修复打分 → 迭代提示词和病历内容。
- **B. 扩展确定性规则**：Haskell/ld.gold 聚类（先在本地复现）、x86 编译选项/头文件的常见模式、
  dh_install multiarch 路径等；每条规则都要有本地构建前后对比。
- **C. 评测基础设施**：报名后批量提交网站 Development Validation；给本地构建建一张"哪些用例能在本地复现"
  的表（先跑 100 个 amd64 目标用例的原版构建）。
- **D. 覆盖面**：RPM spec 修复流程（上游改动 → PatchN + `%patch`）、riscv64 本地构建镜像。
- **E. 提交流程**：`bb check` 已通过；报名后上传做 Hosted Smoke Test，确认目录结构和网络/凭据策略。

## 8. 提交历史

| 提交 | 内容 |
|---|---|
| `53167ef` | 构建机配置脚本、同步脚本、数据集分析工具 |
| `6dd416d` | Agent 核心（上下文/日志分析/ChangeSet）、符号文件规则、本地 Case harness |
| `13c7d68` | 不新建文件的 quilt 记录策略、换行加固、200 用例往返自测、harness 官方验证器接入 |
| `d88e490` | LLM 修复循环、本地目标架构构建器、开发验证打包、假 LLM 端点 |
