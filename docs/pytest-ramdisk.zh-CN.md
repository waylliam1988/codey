# 本机 pytest RAM 盘

正常执行 `pytest` 或 `python -m pytest` 即可。只有项目根目录存在
`.pytest-local.toml` 时，测试才会启用指定的临时根目录，例如：

```toml
[ramdisk]
root = 'R:\codey-pytest'
```

该文件已被 Git 忽略，不应提交。路径必须是绝对路径；工具不会通过盘符猜测
是否为 RAM 盘。GitHub Actions 会忽略这份配置；无配置的其他机器也保持原行为。
无需修改 Windows 的用户或系统 TEMP 环境变量。

每次独立启动测试都会创建一个 `run-*` 目录。Python `tempfile`、pytest 的默认
`tmp_path` 和隔离 home，以及继承环境的子进程，均使用该运行目录。
pytest 启动信息和结束摘要会显示实际路径。显式指定的 `--basetemp`、
`PYTEST_DEBUG_TEMPROOT` 和 `PYTEST_STATE_HOME` 优先。

重复加载 conftest 或启动嵌套 pytest 会复用同一运行目录，只有创建它的进程
负责最终清理。成功运行的目录在进程退出时删除；失败运行保留目录和隔离 home，
便于检查。强制终止的进程可能留下目录，不会在下一次测试时自动删除。
确认对应测试及其子进程均已退出后，可以手动删除残留的 `run-*` 目录。

目标盘不存在、不可写或初始化时剩余空间不足 16 MiB，会提示并保持默认临时目录。
该检查不是测试容量预测；运行中空间不足仍会导致测试报错。

配置在 conftest 导入时生效，作用于测试进程及继承环境的子进程。它不会重定向
启动阶段已产生的写入、`__pycache__`、coverage、项目内日志或显式指定的输出路径。
在同一 Python 进程中调用 `pytest.main()` 时，环境变更会持续到进程退出。

删除或改名 `.pytest-local.toml` 后，在新进程中启动测试即可停用。
