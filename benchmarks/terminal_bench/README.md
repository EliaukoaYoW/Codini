# Codini × Terminal-Bench 2.1

这里保存 Codini 的 Harbor Installed Agent 适配层。Codini 会被安装并运行在每道题自己的任务容器内，题目文件、工具执行和 verifier 都留在 Harbor 的隔离边界中。

这里的 `--sandbox none` 只关闭 Codini 的内层 Bubblewrap；真正的隔离边界是
Harbor 为每道题创建的 Docker 容器，不会在宿主机工作区直接执行题目。

## 当前接入边界

- 已具备：headless 单次运行、容器内安装、任务指令传递、自动审批、运行日志、token/step 指标回收和 ATIF v1.7 轨迹导出。
- 超时控制：单次模型请求最长 120 秒，Agent 总执行最长 875 秒；总超时会终止 Codini 的整个进程组，并在当前题库最短的 900 秒 Harbor 边界前完成清理。
- 完成门禁：任务提示要求修改后实际验证；正常完成且剩余时间充足时，还会续跑一次最多 6 步、180 秒的独立验收，避免只给计划或未经验证就宣称成功。
- 当前模型后端：`openai/<model>`、`siliconflow/<model>`。
- 当前安装源：由本地 Codini wheel 生成的离线运行包；任务容器不会联网下载 Codini 依赖。
- 容器要求：Linux，且镜像能够提供或安装 Python 3.10+。
- 尚未宣称：五次全量正式榜单成绩、Harbor Hub 上传。

## 运行

首次使用只需安装一次 Harbor：

```powershell
uv tool install harbor==0.20.0
```

之后在仓库根目录直接运行：

```powershell
python benchmarks/run_terminal_bench.py
```

入口默认扫描并运行 `benchmarks/terminal_bench/dataset/` 中的全部本地任务。
它会自动读取 `.env`、检查 Docker/Harbor、复用已存在的任务镜像、拉取缺失镜像，
并复用或构建当前 Codini wheel 与离线运行包，
并将完整过程和结果写入
`benchmarks/results/<时间戳>--terminal-bench/`。

只验证其中一道题：

```powershell
python benchmarks/run_terminal_bench.py --task fix-git
```

需要调整尝试次数、并发数或 Codini 步数时：

```powershell
python benchmarks/run_terminal_bench.py --attempts 1 --concurrency 1 --max-steps 20
```

每次运行固定保留：

- `run.log`：完整终端过程；
- `summary.json`：本次入口参数、耗时、状态和结果路径；
- `jobs/`：Harbor 原始 job、trial、Agent 轨迹和 verifier 结果。

运行入口只放宽基础设施阶段：Agent setup、environment build 和 verifier
使用 `2.0` 倍超时，并对相应的超时异常重试一次。Agent 由容器内监督器管理，
最多运行 875 秒，确保能够在 Harbor 终止 trial 前清理整个进程组；每次主运行和
完成验收的退出码、耗时及超时状态会写入 trial 的 `agent/supervisor.json`，并汇总到
`cases.jsonl`。具体策略与镜像准备记录会写入 `summary.json`，基础设施失败不会被
混入 Codini 的诊断通过率。

正式提交 Terminal-Bench 2.1 榜单要求每题至少运行 5 次，并公开上传 Harbor Hub；本目录只完成本地接入准备，不自动上传任何结果。
