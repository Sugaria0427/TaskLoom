# OS算法验收脚本

脚本按“算法机制、实际功能、执行回执”组织。本文依次介绍分组、运行模式和结果，开发回归与性能实验继续使用各自入口。

## 1. 四个算法组

| 分组 | 算法机制 | 对应功能场景 |
|---|---|---|
| `process` | Fork/Join、依赖同步、异步等待、取消回收 | 模型派生子Agent并汇合；取消运行中的工具，观察排队任务接替资源。 |
| `resource` | 多资源原子准入、可行旁路、阶段银行家、大任务保护 | 资源竞争和真实阶段工具扩容，另保留保护开关的受控回调对照。 |
| `fair` | 成本DRR、期限老化、有界提升、优先级WRR、平台两级调度 | 优先级竞争和两个账号共用平台；持续成本份额由机制场景核对，平台定量公平由L4 benchmark评价。 |
| `io` | First Fit、SCAN、Single-flight、有序互斥 | 缓冲区分配及合并、扫描顺序、实际附件批读和重复计算共享。 |

每组先输出机制记录，再运行对应实际功能。独立课程模型与实际Agent回执分开记录。后端尚未公开的工具标记为 `NOT_READY`，不会将独立模型的通过当成生产接入通过。

## 2. 运行方式

在仓库根目录运行，Windows由项目入口转交WSL：

```powershell
python tools/project.py acceptance process
python tools/project.py acceptance resource
python tools/project.py acceptance fair
python tools/project.py acceptance io
```

无分组参数时显示菜单，`all`按上述顺序运行全部分组。原入口 `python tools/acceptance.py` 和旧单场景命令继续保留。

### 2.1 在线模式

默认连接已有服务，个人端地址为 `http://127.0.0.1:8795`，第二个人端为 `http://127.0.0.1:8798`。可通过 `--url` 与 `--second-url`修改。脚本只打印会话、监控和实验页面地址，不启动浏览器或弹出窗口。

在线模式沿用用户当前模型和资源配置，使用真实上游会产生API消费。工具竞争、优先级和取消场景需要在设置中开启工具耗时映射。双账号场景需要不同账号连接同一个平台；阶段扩容需要后端公开 `staged_top_words` 且总内存预算至少384 MiB。

`subagents`需要后端公开`spawn_agents`，`io_read`需要公开`read_documents`并返回First Fit/SCAN执行回执。脚本启动场景前检查工具清单，缺失时记录尚未就绪。

### 2.2 隔离模式与仅组件模式

```powershell
python tools/project.py acceptance all --isolated
python tools/project.py acceptance io --components-only
```

隔离模式启动临时个人端、平台与免费脚本模型，启用临时阶段工具和耗时映射，结束后关闭服务。它不读取真实上游私钥，也不修改已有服务配置。打印的临时地址只在脚本运行期间有效。

`--components-only`只运行所选组的机制示例，结果明确标注该范围。旧`acceptance verify`等价于全组隔离检查，并附带一日策略仿真。

## 3. 结果与判定

每次结果写入 `.runtime/acceptance/<时间与随机标识>/result.json`；分组、实际场景各自保存子报告，便于只查看对应算法。

| 状态 | 含义 | 退出码 |
|---|---|---:|
| `PASSED` | 本次所选范围的检查通过。 | 0 |
| `NOT_READY` / `INCOMPLETE` | 前置条件或后端接口尚未具备，整组存在未完成场景。 | 2 |
| `NOT_OBSERVED` | 任务完成，但目标竞争或共享现象未形成。 | 2 |
| `FAILED` / `INTERRUPTED` | 执行或断言失败；用户中断单独记录。 | 1 / 130 |

脚本只取消本次创建的任务。分组内各场景保留独立结果；目标竞争未形成时应调整配置后再展示，报告保留本次事实。

## 4. 其他脚本的位置

工程回归继续用 `project.py test <tests下相对路径>`；`project.py verify`用于完整工程交付，`verify-isolated`用于干净安装证明。

长期策略效果使用 `acceptance compare --days 7` 或页面 `/experiments`。WP6的`run/capture/report`负责实际任务与指标，`calibrate/simulate/plot`负责校准和FIFO容量模型；L4的`tests/l4/bench.py`负责平台专项基准。

`project.py resources-demo`保留为资源工作包的独立复现入口，`demo_multi_tool.py`保留为早期固定多工具链示例。现场展示统一从本算法分组入口进入，专项脚本用于展开说明。
