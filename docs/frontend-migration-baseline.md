# 前端 TypeScript 迁移基线

起点分支：`codex/frontend-typescript`

迁移第一阶段建立了 `frontend/` 的 Node/Vite 工具链，并生成候选生产构建。当前默认发布目录仍保留原版前端，候选构建通过 `FRONTEND_ROOT` 选择后由 Python 服务提供。

已完成的基线检查：

- `npm ci`：通过（Node 依赖已写入 `frontend/package-lock.json`）。
- `npm run typecheck`：通过。
- `npm test`：通过，2 个协议解码测试。
- `python -m tools.check_javascript`：通过，21 个现有 JavaScript 文件。
- `python -m pytest -q tests/test_env_names.py tests/test_http_plumbing.py tests/test_ui_architecture.py`：通过，19 passed，14 subtests。
- `python -m mypy codey/app/http_plumbing.py codey/env_names.py`：通过。
- `python -m ruff check codey/app/http_plumbing.py codey/env_names.py tools/publish_frontend.py tools/check_javascript.py tools/source_size_inventory.py`：通过。

候选构建验证：

```powershell
Set-Location E:\codey\frontend
npm ci
npm run build

Set-Location E:\codey
$env:FRONTEND_ROOT = (Resolve-Path frontend\dist).Path
python -c "from codey.app.http_plumbing import validate_web_root; print(validate_web_root())"
Remove-Item Env:FRONTEND_ROOT
```

当前限制：完整迁移、生产目录切换、完整 Python 回归、浏览器发布验收尚未完成；不能把本文件中的阶段结果当作最终迁移报告。
