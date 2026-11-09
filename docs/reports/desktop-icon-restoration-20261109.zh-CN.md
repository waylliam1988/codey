# 桌面图标恢复工作记录（2026-11-09）

## 补充排查：Explorer 仍显示 Python 图标

用户确认上一轮修复后，标题栏已正确，任务栏分组仍显示 Python。
上一轮 `WM_GETICON` 验证只覆盖窗口大、小图标，不能证明 Explorer 的应用分组图标；
此前将窗口大图标称为任务栏图标，结论范围过大，以下历史验证需按此限制理解。

- 直接读取真实 Explorer 任务栏 UI Automation：分组名为 `Python 3.12 (64-bit)`，
  `AutomationId` 为 `Appid: {6D809377-6AF0-444B-8957-A3773F02200E}\\Python312\\python.exe`。
- 当前桌面入口没有显式 Windows AppUserModelID，窗口被归入 Python 可执行文件的应用身份。
- 已在创建窗口、导入 WebView 前调用 Windows `SetCurrentProcessExplicitAppUserModelID("Codey.Desktop")`；非 Windows 不加载 Windows 库。
- 使用 `ctypes.OleDLL` 保留 HRESULT 的错误传播，不添加静默回退、额外依赖或打包配置。
- 已增加身份值与参数类型、非 Windows 隔离、调用失败传播的回归测试，并加强现有启动测试，确认身份设置先于窗口创建。
- 改前 5 个补充用例失败，原因是缺少身份设置入口；改后全部通过。

补充验证结果：

- `python -m pytest tests/test_desktop_taskbar.py tests/test_desktop_startup.py tests/test_desktop_http_listener_lifecycle.py tests/test_server.py::UiLaunchTests::test_window_attaches_to_shared_service -q`：27 通过。
- 改动 Python 文件 Ruff 通过；生产入口 Mypy、Pyrefly、ty 均通过，无新增显式 `Any`。
- 真实 WinForms/WebView2 隔离窗口：读取进程显式应用身份得到 `Codey.Desktop`；Explorer UI Automation 同时保留用户旧 Python 分组，并为隔离窗口创建 `AutomationId: Appid: Codey.Desktop` 的新分组。
- 已截取并人工检查实际 Explorer 任务栏按钮，显示 Codey 图标；这次验证覆盖任务栏本身，不仅检查 `WM_GETICON`。
- 证据目录为 `.e2e-artifacts/desktop-taskbar-native-20261109/`，包含 `result.json`、`explorer-groups.json`、`explorer-taskbar-icon.png` 与可复现的 `probe.py`。
- 验证只创建隔离窗口，不启动共享服务或模型，不修改用户旧窗口与任务；检查完自动关闭隔离窗口。
- 未运行完整 pytest、覆盖率或安装包测试。本次补充修复随提交交付，用户需要重新打开桌面窗口以应用新的进程身份。

## 目标与根因

- 恢复 Codey 原生窗口标题栏及任务栏图标，沿用现有 `codey/web/icon.ico`。
- `bf665b78`（2026-10-21）将窗口启动从 `server.py` 迁到 `desktop.py` 时漏掉 `webview.start(icon=...)`。
- 当前 Windows pywebview 在未指定图标时从 `sys.executable` 提取图标，因此显示 Python 图标。
- 图标资源仍存在，且与前端源文件一致；本次不更换图标、不修改打包配置。

## 进度

- 已核对旧启动代码、新入口和已安装 pywebview Windows 后端，确认遗漏参数是根因。
- 已补启动入口回归测试：在项目外的工作目录启动，要求传入已存在的包内图标，同时保持原启动时序与 WebView 存储配置。
- 改前回归测试准确失败：`webview.start` 缺少 `icon` 参数；已在生产入口直接传入包内图标的绝对路径。
- 生产修复仅恢复图标参数，无资源存在性回退或其他防御分支。
- 定向测试、原生图标检查及静态检查均通过，文档已更新，随本次提交交付。
- 工作区已有 runtime、聊天搜索实验和其他测试改动，不纳入本次提交。

## 验证范围

- 桌面启动相关 pytest，检查传入图标路径、与当前工作目录/状态目录独立，以及现有启动时序。
- 使用隔离状态目录打开原生窗口，检查 Windows 原生窗口图标；不接入用户任务或模型。
- 改动 Python 文件 Ruff、生产入口类型检查及显式 `Any` 检查。
- 不运行完整 pytest、覆盖率、安装包构建或发布测试。

## 验证结果

- 改前定向回归测试：1 失败，原因与本次生产缺陷一致。
- `python -m pytest tests/test_desktop_startup.py tests/test_desktop_http_listener_lifecycle.py -q`：22 通过，涵盖启动时序、失败恢复、窗口关闭、服务监听与图标路径。
- `python -m pytest tests/test_server.py::UiLaunchTests::test_window_attaches_to_shared_service -q`：1 通过；仅运行旧服务测试文件中的桌面入口用例，没有扩大到整个服务测试文件。
- `python -m ruff check codey/app/desktop.py tests/test_desktop_startup.py`：通过。
- `python -m mypy codey/app/desktop.py --python-version 3.11 --follow-imports=silent`：通过。
- `pyrefly check codey/app/desktop.py --python-version 3.11`：0 errors。
- `ty check codey/app/desktop.py --python-version 3.11`：通过。
- 改动生产文件及新测试签名未引入显式 `Any`；不涉及类型豁免或关闭检查规则。
- 未运行完整 pytest、覆盖率、安装包构建或发布测试；没有发布版本或修改打包配置。

## Windows 原生验证

通过生产 `open_desktop` 创建真实 WinForms/WebView2 窗口，状态存储使用
`.e2e-artifacts/desktop-icon-native-20261109/isolated-state/`。
只替换服务启动回调为图标检查，不启动共享服务、不接入用户任务、不调用模型。
检查后自动关闭该窗口。

- 在窗口与启动文档就绪后，用 `WM_GETICON` 取得窗口小图标与大图标，均为有效句柄；此检查不覆盖 Explorer 应用分组图标。
- 使用 `LoadImageW` 从包内 `icon.ico` 加载参考图标，以 `DrawIconEx` 将三个句柄绘制到相同 16×16 位图；两个窗口图标均与参考图标逐像素一致。
- 原生窗口 `ShowIcon` 与 `ShowInTaskbar` 均为 `True`。
- 结果保存于 `.e2e-artifacts/desktop-icon-native-20261109/result.json`，复现脚本为同目录 `probe.py`。

第一次验证脚本调用 .NET `Icon.ToBitmap()` 时失败。检查确认 ICO 内的 PNG 尺寸与 CRC 正确，
图标资源未损坏；失败来自该 .NET 转换路径对 PNG 编码 ICO 的支持限制。
验证脚本改为读取 Windows 实际图标句柄并用 GDI 渲染后通过，不让生产代码或图标资源迁就验证工具。
