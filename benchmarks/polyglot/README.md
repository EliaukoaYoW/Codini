# Codini Polyglot Benchmark

该目录用于把 Aider Polyglot 题库适配为 Codini 的外部端到端编码评测。

## 固定基线

- 数据集：`Aider-AI/polyglot-benchmark` 的 Python 题库
- 语言：Python
- 当前范围：自动发现 `dataset/python/` 下全部 140 道题
- 首要指标：`pass@1`

一次评测期间题库必须保持不变。入口会在启动时计算每道题的目录摘要，Verifier 运行前再次校验，发现变化即判定无效。

## 本地题目

Python 原始题目保存在 `dataset/python/`。适配器直接发现包含 `.meta/config.json` 的练习目录，并根据官方元数据读取 solution、test 和 example 文件；官方 example、`.approaches` 与 `.articles` 不会复制进 Agent 工作区。

## 运行

项目只提供一个评测入口：

```console
python benchmarks/run_polyglot.py
```

入口会依次运行全部 Python case：创建临时隔离目录、在 Docker Agent 容器中调用 Codini、在独立的 Docker verifier 容器中执行测试，并在结束或异常时删除临时工作区。

每次运行的正式结果保存在 `benchmarks/results/<时间戳>--codini-python/`：

- `summary.json`：`pass_rate_1`、成功数、超时、错误、每题平均耗时、模型、Git commit、镜像和题库摘要，以及 Codini 的 token、模型调用、工具调用和停止原因聚合。
- `cases.jsonl`：每道题的 Agent 指标、Verifier 结果、退出码、耗时、solution 摘要和测试日志。

每完成一道题就会追加明细并更新汇总，因此中途停止时，已完成结果仍可用于分析。

评测为 Codini 设置 12 个初始工具步骤；只要最近操作仍产生进展，现有动态预算最多可扩展到 36 步。连续 5 次无进展或单题运行超过 600 秒仍会停止，避免循环任务无限消耗资源。该设置只作用于 Polyglot benchmark，不改变 Codini CLI 的日常默认值。

## 单题隔离与验证

`isolation.py` 为每个 case 在系统临时目录中创建唯一工作区。Agent 容器只挂载该工作区，不能访问宿主机仓库、可信题库源或 Docker socket；不同 case 不共享 Session、Working Memory、Episodic Memory 或 Durable Memory。

Agent 容器允许访问模型 API，使用只读根文件系统并仅对当前题目工作区开放写入。`verifier.py` 不会直接在 Agent 工作区中运行测试：它先拒绝测试、配置、文档篡改和未声明文件，再把允许的 solution 文件复制到全新的可信题目副本。Verifier 容器断网、只读挂载题目、移除全部 Linux capability，并启用 `no-new-privileges`。

## 基础镜像

构建镜像：

```console
docker build --pull -f benchmarks/polyglot/Dockerfile -t codini-polyglot-pyjs:0.1 .
```

验证工具链：

```console
docker run --rm codini-polyglot-pyjs:0.1
```

镜像包含当前仓库的 Codini，以及 Python、pytest、Node.js、npm 和 JavaScript 测试依赖；不包含 Java、C++、Go、Rust、Aider 或模型凭据。入口只在启动 Agent 容器时传入所选 Provider 的三项环境变量，Verifier 容器不会获得这些变量。

## 结果边界

入口得到的结果应标记为 `Codini on Aider Polyglot — Python subset`，不能直接声明为覆盖六种语言的 Aider 官方完整排行榜成绩。
