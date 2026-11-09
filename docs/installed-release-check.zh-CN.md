# 发布安装检查（仅显式手动执行）

`tests/test_installed_runtime.py` 已迁出默认 pytest。冷启动、重连、升级、再次冷启动，以及源码包自包含检查保留在 `tools/release_package_check.py` 和 `tools/installed_runtime_probe.py`，不会被日常 pytest、全量 pytest 或普通 CI 测试步骤自动执行。

## 构建与安装分开

```text
指定 Git 提交
  → 检查 Git 树文件数/字节预算
  → Git archive 导出到新的仓库外目录
  → 在副本中构建一次 sdist
  → 从这个 sdist 构建一次 wheel

已有 sdist + wheel
  → 检查源码包包含构建后端等必要材料
  → 在另一个新目录创建虚拟环境并安装 wheel
  → 用安装环境 Python -I 验证服务冷启动、重连、升级和再次冷启动
```

构建沿用 `tools.build_runtime` / setuptools，不另建打包实现。导出依据是指定提交的 Git 树，不通过 `copytree`、工作区递归搜索或测试专属白名单复制源码；未提交修改、未跟踪实验结果、被忽略的 `node_modules` 和旧构建产物不参与该次提交的导出。记录解析后的实际提交号，不能把旧提交的结果当成当前未提交工作区的验证。

源码包清单同时包含新 CLI 和 probe，使源码包内的轻量边界测试能导入对应工具；它们不加入安装后的生产模块清单。

## 发布时的显式命令

下面只是手动发布操作说明，日常任务不要运行。先提交需要发布的修改，选择健康且有足够空间的普通磁盘；优先使用工作盘以外的物理 SSD。不同盘符不一定是不同物理盘，脚本不自动判断存储介质。

```powershell
python -m tools.release_package_check build --commit <明确提交号> --work-dir E:\codey-release\candidate-build

$releasePackage = Get-Content E:\codey-release\candidate-build\build-report.json -Raw | ConvertFrom-Json
python -m tools.release_package_check check --wheel $releasePackage.wheel --sdist $releasePackage.sdist --work-dir E:\codey-release\candidate-check
```

`--commit`、`--work-dir` 和检查输入包均没有隐式默认值。两个工作目录必须尚不存在，并且位于本工作仓库外；已有目录拒绝覆盖，不自动删除。安装检查不执行构建，重复安装验证也不需要重新生成包。

## I/O 边界与日志

- 导出前读取 `git ls-tree` 元数据，文件数超过10,000或总内容超过128 MiB即停止，不开始导出；符号链接、子模块等非普通文件明确拒绝，不隐式访问外部目录。预算是输入规模限制，不是磁盘带宽限速。
- 工作副本、构建中间文件、pip 临时目录、虚拟环境、服务状态均位于显式工作目录内。子进程的 `TEMP` / `TMP` / `TMPDIR` 重设到其 `tmp`，不继承日常 pytest 的 R 盘临时目录；不改本机 `.pytest-local.toml`。
- 从副本生成 sdist，wheel 再从 sdist 构建，不再直接 `pip wheel D:\codey` 或在日常仓库根目录调用 `build_sdist`。现有 `MANIFEST.in` 的递归规则作用于导出的有限副本，不接触工作区里的未跟踪结果目录。
- 构建、安装按阶段串行进行；复用现有 deadline scope / 进程树所有权，构建总期限600秒，安装检查总期限360秒，各子阶段也有独立上限。停止或失败后不继续下一阶段。这是协作式取消与子进程超时，不是磁盘驱动级的 I/O 超时保证。
- 各阶段实时写入工作目录中的 `*.log`，在启动前打印阶段名/日志路径。失败目录保留供检查；成功后分别写 `build-report.json` 或 `check-report.json`。中断/失败没有通过回执，不自动重跑。
- 安装环境使用 `--system-site-packages` 借用本机依赖；wheel 用 `--ignore-installed --no-deps --no-index` 安装到新环境。它验证已安装的项目可脱离源码运行，不代表验证了完全离线的全部依赖分发。正常构建需要本机已有 setuptools/wheel 等依赖，不自动联网安装。
- 以上改造降低工作目录膨胀、重复构建和源盘中间写入造成的额外 I/O，不承诺消除所有磁盘负载，也不据此断言已找到整机冻结的唯一原因。

## 日常验证

只验证本次隔离机制时，可运行：

```powershell
python -m pytest -q tests/test_release_package_check.py
```

这些测试使用微型归档和替身命令执行器，检查目录隔离、提交绑定、输入预算、单次构建顺序、已有包消费、失败停止、日志和进程树收尾，不调用 Git 导出、pip、真实构建器或服务。真实安装 probe 不作为测试导入，也没有新增自动 CI 发布步骤。

本次改造没有执行真实发布构建/安装检查，不能宣称新发布链路已实机通过。
