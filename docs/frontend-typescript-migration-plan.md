# Codey 前端完整迁移到 TypeScript：修改方案与实施教程

日期：2026-10-14。适用仓库：`E:\codey`。

这是一份实施方案，不是迁移完成报告。编写时检查了当前源码、静态文件服务、前端测试和 CI；没有执行迁移，没有安装依赖，也没有运行整套测试。下文标为“新增”的文件、命令和环境变量目前都需要实现后才能使用。

目标是一次完整工程：在迁移分支完成全部前端 TS 化、模块化、构建、测试、打包和发布验证，再切换默认前端。实施有先后顺序，交付只有一套完整前端，不把长期 JS/TS 混用当作最终结果。

## 1. 先确定这次工程究竟改什么

采用以下技术组合：

| 层 | 最终选择 | 原因 |
|---|---|---|
| 产品前端语言 | TypeScript | 明确字段、参数、状态与模块接口 |
| 页面交互 | 原生 DOM | 现有界面和行为已有实现，直接保留 |
| 模块 | ESM `import/export` | 替代 `window.Codey*` 和脚本加载顺序 |
| 构建 | Vite | 处理 TS、CSS 与静态资源引用 |
| 单元测试 | Vitest；DOM 场景用 jsdom | 直接测试 TS 模块与状态规则 |
| 浏览器测试 | 保留 Python Playwright 测试体系 | 延续现有行为证据 |
| 运行服务 | 现有 Python HTTP 服务 | 保留授权、Origin、SSE 和安装方式 |
| 发布资源 | `codey/web` | 延续 Python 包的静态资源位置 |

第一轮保留现有布局、CSS、DOM ID、快捷键、文案和业务规则。语言迁移时发现的产品行为问题单独记录；除非它阻止迁移或验证，不把修复混入这次改写。

完成标准不是“所有文件后缀变成 `.ts`”，而是：业务源码和 HTML 中的业务脚本全部进入 TS；跨模块依赖有明确类型；关键网络输入经过验证；产物能由 Python 和安装包正确提供；旧版本保存的数据仍能读取；用户流程保持兼容。

Vite 有原生 TypeScript 模板，不要求引入 React 或 Vue。参见 [Vite 官方入门](https://vite.dev/guide/)。

## 2. 当前仓库的真实情况

当前 `codey/web/assets` 有 20 个 JS 文件，合计约 5,873 行；`index.html` 共约 1,656 行，包含页面 DOM 和较长的内联业务脚本。这些数字只用于确认本次检查范围，不作为以后必须保持的门禁。

已确认的迁移约束：

1. 现有模块使用 IIFE 和 `window.Codey*`，部分依赖通过 `init(deps)` 注入。
2. `index.html` 除启动入口外，还承载事件派发、会话操作、审批和项目操作。
3. `ui_state.js` 使用多个已有 localStorage 键；会话的 `_eventKeys`、`_toolKeys`、`_terminalRunSet` 是不可枚举的运行时索引。
4. 供应商目录可动态加载，不能把供应商 ID 写死成六个字面量。
5. 草稿含文本、选择区间、选择方向和 revision。发送后只能清理仍属于本次发送的草稿。
6. 请求 attempt 包含运行 ID；迟到事件不能改变新 attempt，终态不能被迟到开始事件复活。
7. SSE 已有游标、去重、对账 Promise、事件缓冲、溢出后重新对账和 resync 处理。
8. 启动先授权，再采用供应商目录；恢复服务器 UI 状态完成后才开放输入并连接事件。
9. `http_plumbing.py` 当前只允许 `/assets` 中的 JS/CSS；图标由 `/icon.ico` 单独提供。
10. 静态文件缓存有 ETag 和缓存预算；HTML 会替换 `__APP_VERSION__`，版本包含资源内容摘要。
11. 架构测试明确要求传统同步脚本、IIFE、旧全局命名空间。这些断言需要有对应的新约束。
12. 部分浏览器测试通过路由拦截提供包内 HTML/资源；另有测试直接调用 `activeSession()` 等页面全局函数。
13. Python 安装包的数据规则目前只列出 HTML、图标和一层 JS/CSS。
14. Windows 和 Linux CI 已配置 Node 24。Windows 跑 Python 3.11/3.12/3.13，Linux 主要跑 3.12 和部分边界回归。
15. `tools/check_javascript.py` 同时检查网页 JS 和 `codey/providers` 的 JS/MJS。后者不是网页前端迁移范围。

这决定了本方案必须覆盖研究抽屉、研究图谱、本地上下文、模型管理和运行详情，不能只迁移 composer、requests、SSE。

## 3. 一次完成的工作方式

建立迁移分支，在分支内完成所有改动；默认发布入口最后统一切换。中间可以编译候选产物和运行测试，用来定位问题。

建议把工程拆成可审查提交，但不要把未完成迁移作为正式发布版本。完成前旧发布资源仍能承担日常使用。

```powershell
Set-Location E:\codey
git status --short
git rev-parse HEAD
git switch -c codex/frontend-typescript
```

如果分支已存在，使用 `git switch codex/frontend-typescript`。如果工作区有其他修改，先辨认归属；不要自动 stash、删除或把无关文件一起提交。

记录起点 SHA，后续基线、截图和迁移验证都关联该 SHA。迁移期间冻结不必要的 DOM/CSS 改动，否则难以判断差异来自哪里。

## 4. 建立基线：保存具体证据

新增文档 `docs/frontend-migration-baseline.md`，记录：提交 SHA、操作系统、Python/Node/npm/浏览器版本、每条检查命令、退出码、通过/失败/跳过数量、已知问题、截图目录。

先确认环境，不要假定本机与 CI 工具齐全：

```powershell
python --version
node --version
npm --version
python -m playwright --version
```

执行现有检查，逐条确认退出码：

```powershell
python -m ruff check .
python -m mypy codey
pyrefly check codey
python -m tools.source_size_inventory
python -m tools.machine_contract_gate
python -m tools.check_javascript
python -m pytest -q
```

完整浏览器 E2E 是现有 opt-in 测试，需要显式打开，并恢复原环境值：

```powershell
$taskOldE2E = $env:RUN_BROWSER_E2E
try {
    $env:RUN_BROWSER_E2E = '1'
    python -m pytest -q tests/test_ui_browser_e2e.py
    if ($LASTEXITCODE -ne 0) { throw 'Baseline browser E2E failed' }
} finally {
    if ($null -eq $taskOldE2E) {
        Remove-Item Env:RUN_BROWSER_E2E -ErrorAction SilentlyContinue
    } else {
        $env:RUN_BROWSER_E2E = $taskOldE2E
    }
}
```

另一次用于保存完整流程的产物：

```powershell
python -m tools.ui_e2e --artifacts .e2e-artifacts/frontend-before --json
```

不要宣称任意机器都能直接运行真实 Edge E2E；确认工具所需浏览器可用。缺浏览器属于未验证，不能以跳过作为通过。

保存至少这些界面：空会话、长回复、运行中、审批卡片、发送失败、研究结果、变更抽屉、本地上下文、模型设置、窄窗口。截图使用固定视口、数据和字体环境；动态时间等内容可以统一处理，但不能掩盖布局变化。

## 5. 先建立功能与测试对照表

新增 `docs/frontend-migration-acceptance.md`。每行包含功能、旧测试位置、新测试位置、真实 HTTP 验证、结果及证据路径。

| 功能 | 必须保持的行为 | 现有测试入口举例 |
|---|---|---|
| 授权 | 清除启动 fragment；授权先于目录、状态和 SSE | `test_operator_bootstrap_before_ui_requests.py` |
| 草稿 | 会话隔离、光标恢复、发送失败保留、发送期间编辑不丢失 | `test_ui_draft_persistence.py`、`test_ui_workflow.py` |
| 请求 | 重试原请求；旧 attempt 不影响新 attempt | `test_ui_manual_retry.py`、`test_ui_workflow.py` |
| 保存 | revision、409 冲突、队列保存、退出页面保存 | `test_ui_local_save_status.py`、`test_ui_state_store.py` |
| 会话 | 新建、导航、重命名、搜索、刷新恢复 | conversation/navigation、title/rename、recovery/search 测试 |
| 模型 | 动态供应商、连接、模型、effort、thinking、设置范围 | `test_ui_model_management.py`、settings 测试 |
| 阅读 | 选择、复制、引用、跟随、返回最新、原地更新 | selection、reading/follow、inplace/render 测试 |
| 变更 | 项目隔离、迟到响应、恢复失败、文件展开 | `test_ui_changes_refresh.py`、`test_ui_workflow.py` |
| 研究 | 历史、恢复、证据、笔记、图谱 | research 相关测试及完整 E2E |
| 本地上下文 | 当前范围、刷新、动作、导出 | local context 相关源码与测试搜索结果 |
| 审批 | 拒绝、通过、断线恢复、HTTP 对账、结果回填 | 完整 `tools.ui_e2e` 流程 |
| 布局 | 键盘、焦点返回、窄/短窗口、抽屉互斥 | desktop/interactions、settings/focus、workflow 测试 |

表中的部分名称是测试组描述，不是可直接运行的文件名。实施时先用下面的搜索补全精确节点：

```powershell
rg --files tests -g '*ui*.py' -g '*operator*.py' -g '*http*.py'
rg -n 'Codey|activeSession\(|page.evaluate|route\(|skip' tests -g 'test_ui*.py'
rg -n '/api/|new EventSource|localStorage' codey/web
```

## 6. 最终目录及资源所有权

```text
frontend/
  package.json
  package-lock.json
  .npmrc
  tsconfig.json
  tsconfig.tools.json
  tsconfig.tests.json
  vite.config.ts
  vitest.config.ts
  eslint.config.mjs
  index.html
  public/icon.ico
  src/
    main.ts
    bootstrap.ts
    app.ts
    api/
      transport.ts
      endpoints.ts
      dto.ts
      decode.ts
    domain/
      session.ts
      messages.ts
      requests.ts
      providers.ts
      research.ts
    state/
      store.ts
      ui-state.ts
      persistence.ts
      normalize.ts
      indexes.ts
    events/
      types.ts
      decode.ts
      sse.ts
      dispatch.ts
    ui/
      [对应现有 20 个模块的文件]
      conversation-actions.ts
      project-actions.ts
      approvals.ts
      menus.ts
      dom.ts
    styles/tokens.css
    styles/app.css
    testing/bridge.ts
  tests/
    [模块与启动测试]
    fixtures/contracts/
  dist/                     # 忽略；生产候选产物
  dist-test/                # 忽略；带测试桥的候选产物
  coverage/                 # 忽略

codey/web/                  # 提交；完整生产构建结果
  index.html
  icon.ico
  frontend-build.json       # 新增；发布校验元数据
  assets/*.js
  assets/*.css

tools/
  publish_frontend.py        # 新增
  export_frontend_contracts.py # 新增
  frontend_package_smoke.py # 新增
```

源码归 `frontend` 管理，发布目录归发布脚本管理。以后修功能修改 TS/CSS/源 HTML，不手改 `codey/web` 中的生成文件。

生产生成文件提交进 Git。这符合现有 Python 包安装体验：源码 checkout 和 wheel 都能直接使用已构建前端；用户运行 Codey 无需 npm。CI 负责证明生成文件与源码一致。

`frontend-build.json` 不需要通过 HTTP 提供，它供发布与打包校验使用。`.vite/manifest.json` 留在候选目录，不作为公开静态文件发布。

## 7. 旧文件到新文件：不要漏模块

| 当前文件 | 建议新位置 | 修改要点 |
|---|---|---|
| `operator_auth.js` | `api/operator-auth.ts` | 保留 fragment 清除和同源凭据交换 |
| `render.js` | `ui/render.ts` | 保留转义、Markdown、复制、更新与折叠逻辑 |
| `process.js` | `ui/process.ts` | 保留组内增量更新和展开状态 |
| `messages.js` | `ui/messages.ts` | 消息判别联合；注入动作回调 |
| `research_graph.js` | `ui/research-graph.ts` | 图谱响应、范围和迟到响应 |
| `research_drawer.js` | `ui/research-drawer.ts` | 笔记缓存、引用、证据、焦点 |
| `research_runs.js` | `state/research-runs.ts` | 历史、恢复资格、持久化 |
| `changes_drawer.js` | `ui/changes-drawer.ts` | 请求所属项目、缓存和恢复动作 |
| `local_context_drawer.js` | `ui/local-context-drawer.ts` | 当前范围、动作、导出、DOM 元数据 |
| `run_details.js` | `ui/run-details.ts` | 原地展示、不改变消息数据 |
| `models.js` | `state/models.ts` | 设置读取、允许模型规则 |
| `model_settings.js` | `ui/model-settings.ts` | revision、来源操作、错误和重试 |
| `settings.js` | `ui/settings.ts` | 设置范围、异步保存、关闭焦点 |
| `provider_ui.js` | `ui/provider-ui.ts` | 动态目录、实时默认值、模型选择 |
| `ui_state.js` | `state/ui-state.ts` 等 | 初次可整体迁移，随后按责任拆开 |
| `sse.js` | `events/sse.ts` | 保留游标、缓冲、对账和事件接收语义 |
| `requests.js` | `state/requests.ts` | request/attempt 身份与终态规则 |
| `composer.js` | `ui/composer.ts` | 发送锁、草稿 revision、重试和失败动作 |
| `conversation_nav.js` | `ui/conversation-nav.ts` | 导航、预览、快捷键 |
| `conversation_ui.js` | `ui/conversation-ui.ts` | 阅读、选择、侧栏尺寸、搜索和焦点 |
| HTML 内联脚本 | `app.ts`、`bootstrap.ts`、actions、dispatch | 函数逐项迁出，不只保留一个大 TS 文件 |
| `tokens.css`、`app.css` | `src/styles/` | 初次原样复制；保持导入顺序 |

每个迁移模块记录旧导出名、新导出名、依赖、内部状态、绑定监听、计时器、销毁方式和测试。文件拆分依据职责，不强迫每个文件低于任意行数。

## 8. 建立前端工具链

手动建立 `frontend`，不要用脚手架替换现有页面。

新增 `frontend/package.json`：

```json
{
  "name": "codey-frontend",
  "private": true,
  "type": "module",
  "engines": { "node": "24.x" },
  "scripts": {
    "typecheck": "tsc -p tsconfig.json && tsc -p tsconfig.tools.json && tsc -p tsconfig.tests.json",
    "lint": "eslint src tests",
    "test": "vitest run",
    "test:coverage": "vitest run --coverage",
    "build": "npm run typecheck && vite build",
    "build:test": "npm run typecheck && vite build --mode test",
    "build:watch": "vite build --watch",
    "check": "npm run typecheck && npm run lint && npm run test"
  }
}
```

这里的 `&&` 是 npm 脚本中的失败短路，不是为了分隔终端输出。`build:watch` 只负责构建，另一个终端持续运行类型检查；它不能代替发布门禁。

新增 `.npmrc`：

```ini
save-exact=true
engine-strict=true
```

安装兼容的稳定工具版本并锁定：

```powershell
Set-Location E:\codey\frontend
npm install --save-dev --save-exact typescript vite vitest jsdom @types/node @vitest/coverage-v8 eslint @eslint/js typescript-eslint
npm ls
```

安装成功不等于版本组合验证完成。确认 Node engine、peer dependencies、TS 与 typescript-eslint 的支持范围；Vitest 与 coverage provider 使用兼容版本。之后提交 `package.json` 和锁文件，以 `npm ci` 复现。不使用 `--force` 掩盖不兼容。

本文不指定一组未经本仓库测试的版本号。实施时记录实际选择及验证结果；跨平台产物可重复性要求 Node/npm 版本一致，必要时精确固定 CI 和本机小版本。

在根 `.gitignore` 新增：

```gitignore
/frontend/node_modules/
/frontend/dist/
/frontend/dist-test/
/frontend/coverage/
/frontend/*.tsbuildinfo
```

在根 `.gitattributes` 为构建输入和生成资源明确换行策略，避免 Windows checkout 的 CRLF 导致源码摘要与 Linux 不同：

```gitattributes
frontend/** text eol=lf
frontend/public/*.ico binary
codey/web/index.html text eol=lf
codey/web/assets/*.js text eol=lf
codey/web/assets/*.css text eol=lf
codey/web/frontend-build.json text eol=lf
```

检查现有规则的优先级。只对本次迁移文件规范化，不执行影响全仓库无关文件的批量 renormalize；未来新增图片、字体要追加 binary 规则。文件摘要基于约定的真实文件字节，不能用“比较时忽略所有空白”掩盖生成产物差异。

## 9. TypeScript：浏览器、工具、测试分开检查

新增 `frontend/tsconfig.json`：

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "types": ["vite/client"],
    "strict": true,
    "noEmit": true,
    "allowJs": false,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noFallthroughCasesInSwitch": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "isolatedModules": true,
    "verbatimModuleSyntax": true,
    "forceConsistentCasingInFileNames": true,
    "skipLibCheck": true
  },
  "include": ["src/**/*.ts"]
}
```

新增 `tsconfig.tools.json`：

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "lib": ["ES2022"],
    "types": ["node"],
    "strict": true,
    "noEmit": true,
    "verbatimModuleSyntax": true,
    "skipLibCheck": true
  },
  "include": ["vite.config.ts", "vitest.config.ts"]
}
```

新增 `tsconfig.tests.json`：

```json
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "types": ["vite/client", "node"]
  },
  "include": ["tests/**/*.ts", "src/**/*.ts"]
}
```

测试使用显式 `import { describe, it, expect } from 'vitest'`，无需给产品代码注入测试全局。

理解几个会产生大量报错的选项：

| 选项 | 实际修法 |
|---|---|
| `strict` | 检查 null，补函数参数和返回值类型 |
| `noUncheckedIndexedAccess` | 数组第一项可能不存在，先确认非空 |
| `exactOptionalPropertyTypes` | `field?: string` 不等于可以显式写 `field: undefined` |
| `verbatimModuleSyntax` | 类型使用 `import type`；运行时值正常 import |
| `allowJs: false` | 产品源码不能借 JS 输入绕过迁移 |
| `noEmit` | tsc 检查，Vite 输出；不会生成第二套 JS |

`skipLibCheck` 只跳过依赖声明文件内部检查，不允许跳过本项目类型问题。TS 配置行为参见 [官方 TSConfig 文档](https://www.typescriptlang.org/tsconfig/)。

## 10. 增加真正约束类型逃逸的 lint

新增 `frontend/eslint.config.mjs`：

```javascript
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));

export default tseslint.config(
  { ignores: ['dist/**', 'dist-test/**', 'coverage/**', 'node_modules/**'] },
  {
    files: ['src/**/*.ts', 'tests/**/*.ts'],
    extends: [js.configs.recommended, ...tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.json', './tsconfig.tests.json'],
        tsconfigRootDir: root
      }
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      '@typescript-eslint/ban-ts-comment': ['error', {
        'ts-ignore': true,
        'ts-nocheck': true,
        'ts-expect-error': 'allow-with-description',
        minimumDescriptionLength: 10
      }]
    }
  }
);
```

该配置是安装兼容版本后的起点，不是已在本仓库验证的成品。负面类型测试可以使用带原因的 `@ts-expect-error`；产品代码中需要逐项说明例外。

lint 不能证明所有 `as` 都有依据，断言仍需审查。尤其禁止把接口 JSON 直接断言成业务对象。带类型信息的 lint 配置说明见 [typescript-eslint 官方文档](https://typescript-eslint.io/getting-started/typed-linting/)。

事件回调中的异步操作显式捕获错误：

```typescript
sendButton.addEventListener('click', () => {
  void sendActiveDraft().catch(showSendFailure);
});
```

这里只用于已有失败处理之外的异常出口，不能把一个捕获函数加到所有地方后就宣称业务失败都处理好了。

## 11. Vite 配置：候选构建与测试构建分离

新增 `frontend/vite.config.ts`：

```typescript
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

export default defineConfig(({ mode }) => ({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: '/',
  publicDir: 'public',
  define: {
    __FRONTEND_TEST__: JSON.stringify(mode === 'test')
  },
  build: {
    outDir: mode === 'test' ? 'dist-test' : 'dist',
    emptyOutDir: true,
    target: 'es2022',
    assetsDir: 'assets',
    assetsInlineLimit: 0,
    manifest: true,
    sourcemap: process.env.FRONTEND_BUILD_SOURCEMAP === '1' ? 'hidden' : false
  }
}));
```

在 `src/env.d.ts` 声明：

```typescript
declare const __FRONTEND_TEST__: boolean;
```

选择生产 sourcemap 默认关闭，必要时在候选构建生成 hidden map 作为调试证据。发布脚本不复制 `.map`；当前 Python 静态服务也不必为调试扩大资源范围。

不在配置中绑定 Rollup 专属选项：当前 Vite 官方文档已有 Rolldown 构建说明，不应把旧示例的 bundler 内部配置当作永远通用。这里仅使用公开的基本构建项，实施时按锁定版本验证。[Vite 构建配置](https://vite.dev/config/build-options)

## 12. HTML 与 CSS 的实际迁移

复制 `codey/web/index.html` 到 `frontend/index.html`，随后：

1. 保留 DOM 结构、ID、类名、ARIA、按钮属性。
2. 删除旧 20 个业务脚本标签。
3. 把唯一内联业务脚本完整迁出；逐项核对函数和监听，不删除逻辑。
4. 删除旧 CSS 发布路径，改由 TS 入口导入源 CSS。
5. 把图标复制到 `frontend/public/icon.ico`，保留 HTML 中 `/icon.ico`。
6. 页面末尾只留下模块入口。

```html
<script type="module" src="/src/main.ts"></script>
```

`src/main.ts`：

```typescript
import './styles/tokens.css';
import './styles/app.css';
import { bootstrap } from './bootstrap';
import { showStartupFailure } from './ui/startup-status';

void bootstrap().catch(showStartupFailure);
```

模块脚本有延迟执行语义，顶层 import 会先执行所有模块顶层代码。因此不仅要迁移 `boot()`，还要处理旧模块 import 时立即绑定监听、请求网络或读取状态的副作用。

原则：import 定义函数与类型；应用组装后才绑定监听和发请求。纯常量和不会触发外部操作的辅助对象可以留在顶层。

CSS 保持 tokens 在 app 之前；构建后检查最终 CSS 顺序、字体、变量、选择区、inert 和响应式行为。不要因为 Vite 自动抽取 CSS 就假定视觉完全不变。

## 13. 整理真实协议：先检查生产者和消费者

协议来源包括：

- `codey/app/server.py`：实际路由、请求授权及 HTTP 状态。
- `codey/app/api.py`：UI 状态、提交、变更、审批等响应。
- `run_registry.py`：运行状态快照。
- `event_bus.py`、`event_payloads.py`：事件作用域、游标与消息形状。
- `codey/storage/ui_state_store.py`、`codey/app/model_settings.py` 等：保存数据允许字段及 revision。
- 当前前端消费代码与相关 Python 行为测试。

建立接口清单，每条包含 method、path、请求字段、成功响应、错误响应、可选/null 语义、状态码、幂等性、缓存要求、消费者。

已在当前源码中出现的路径至少覆盖：

| 组 | 路径 |
|---|---|
| 授权 | `/api/operator_session` |
| 状态与事件 | `/api/state`、`/api/events`、`/api/ui_state` |
| 请求与会话 | `/api/run`、`/api/stop`、`/api/new_chat`、`/api/pick_folder` |
| 模型与设置 | `/api/provider_catalog`、`/api/providers`、`/api/model_settings`、`/api/model_catalog`、`/api/api_models`、`/api/local_provider` |
| 项目变更 | `/api/changes`、`/api/changes/restore` |
| 审批与教学 | `/api/shell_approval`、`/api/teach/resume` |
| 运行详情 | `/api/run_details` |
| 研究 | `/api/research/restore`、`/api/research/graph`、`/api/research/notes` |
| 本地上下文 | `/api/ghost/summary`、`/api/ghost/action`、`/api/ghost/export` |

这是一份当前消费者清单，不声称包含所有后端路由。按完整搜索补全，不凭路径名猜请求 method。

供应商 ID 用动态字符串与目录验证：

```typescript
export type ProviderId = string;

export interface ProviderCatalogEntry {
  id: ProviderId;
  label: string;
}
```

这是内部最小目录模型，实际 DTO 字段单独对照后端。运行时检查 ID 是否在目录中，不能用 `type ProviderId = 'deepseek' | 'local' | ...` 锁死扩展能力。

effort 目前出现的集合可以定义为联合类型，但默认 provider/recommended provider 使用 getter，避免启动时解构出一个之后不再更新的默认值。

## 14. 区分网络 DTO、业务状态与保存格式

不要试图用一个 `Session` 接口同时描述服务器响应、本地缓存和运行时对象。

```text
未知 JSON
  → 验证/兼容归一化
  → DTO 或 StoredSession
  → 业务 Session
  → 渲染

业务 Session
  → 明确序列化
  → StoredSession
  → localStorage / Python UI state
```

`dto.ts` 保留服务器的 `run_id`、`session_id`、`active_id`、`base_revision`。内部模型继续使用已有 `runId`、`projectId`、`createdAt` 等命名，避免大面积无收益改名。

给所有 ID 建 branded type 不是本轮必要条件。先明确参数名和边界；若实际出现同为字符串的 ID 混用问题，再对关键 ID 引入品牌类型，不把复杂泛型当作迁移目标。

草稿定义可直接依据现有结构：

```typescript
export interface Draft {
  text: string;
  start: number;
  end: number;
  direction: 'none' | 'forward' | 'backward';
  revision: number;
}
```

请求 attempt 初次保持现有字段与可选语义：

```typescript
export type AttemptState = 'sending' | 'running' | 'failed' | 'done' | 'paused';

export interface RequestAttempt {
  runId: string;
  state: AttemptState;
  error?: string;
  warning?: string;
  sendFailure?: string;
  runOwner?: string;
}
```

不要立即把它改成“失败状态必须有 error”的严格判别联合，除非已经确认历史数据归一化和全部构造路径都满足该约束。类型描述现有事实后，才能合理加强。

消息更适合按 `type` 建判别联合。旧消息类型至少包括 user、asst、thinking、turn、tool_pending、tool、info、err、teach、shell_request、shell_result、run_state、request_status、limit、pause、review、research_done、done；以实际构造与后端保存规则补全。

## 15. 网络输入验证：写法、容错和错误分类

新增小工具，而不是从 `response.json()` 直接获得业务对象：

```typescript
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function requireString(value: unknown, field: string): string {
  if (typeof value !== 'string') throw new Error(`Invalid field: ${field}`);
  return value;
}
```

当前提交成功响应实际返回 `ok` 与 `run_id`。可形成：

```typescript
export interface AcceptedRunDto {
  ok: true;
  run_id: string;
}

export function decodeAcceptedRun(value: unknown): AcceptedRunDto {
  if (!isRecord(value) || value.ok !== true) {
    throw new Error('Invalid run acceptance response');
  }
  const runId = requireString(value.run_id, 'run_id');
  if (!runId) throw new Error('Missing accepted run ID');
  return { ok: true, run_id: runId };
}
```

这里比旧前端接受空运行 ID 更严格：实施时要用真实后端和已有兼容案例确认。如果存在合法旧响应，先设计兼容策略，不能悄悄制造新错误。是否必须与客户端提交的 run ID 相等，同样依据后端合同确认。

运行状态快照的已确认字段来自 `codey/app/run_registry.py` 中 `payload()` 的实际构造逻辑，包括 run_id、session_id、busy、run_status、project、task、provider、summary、stop_reason、provider_failure、last_terminal_event、last_shell_result、research_restore_runs 和 pending_event。最后几项不能省略：当前 `applyRunState()` 会依次重放 shell result、terminal event 和 pending event，再同步运行状态、研究恢复资格与 requests。

定义 DTO 时把这三个重放事件的可空性和各自事件类型写清楚；在 snapshot decoder 内调用相应事件 decoder。不要把 `/api/state` 简化成 `{ busy, run_id }`，否则断线恢复会丢失审批结果和最终回复。

分清四类输入：

| 输入 | 策略 |
|---|---|
| 成功 API 响应 | 检查关键字段；多余字段可接受 |
| 错误 API 响应 | 保留 HTTP 状态和 reason/error；非 JSON 错误有兜底 |
| 老版本缓存 | 宽容归一化、缺省补全；避免整段会话清空 |
| SSE | 已知关键事件检查；未知事件按明确规则跳过；无效关键事件触发受控对账 |

无效事件处理必须有节流和单次对账合并；不能遇到连续错误就每条事件请求 `/api/state`。日志不输出启动 token、cookie、模型密钥或整段敏感消息。

网络验证库可以选择手写小 decoder 或经验证的 schema 库。第一轮不强制新增库，但必须统一关键边界，不能每个模块自己猜字段。

## 16. 统一 HTTP 传输，但保留业务响应时序

新增 `api/transport.ts`：

```typescript
export async function request(
  path: string,
  init: RequestInit = {}
): Promise<Response> {
  return fetch(path, {
    ...init,
    credentials: 'same-origin'
  });
}

export async function readJson(response: Response): Promise<unknown> {
  const value: unknown = await response.json();
  return value;
}
```

这是刻意保持薄的传输层。缓存要求由端点明确设置，POST 与 GET 也由端点决定。API 错误的业务分类由端点或调用者处理。

特别保留提交时序：当前逻辑在确认 `response.ok` 后执行 `onSendStarted()` 清理符合条件的草稿，再读取接受响应并对账。把所有操作改成 `postJson<T>()`，可能不经意变成先等待 JSON 解码再清草稿，改变行为。

迁移时先保持该时序。若决定加强“确认 run_id 后再清理”，必须单独说明行为变化并增加异常响应、响应中断和用户编辑场景的测试。

不要在 transport 自动重试所有请求。提交任务、审批和恢复有外部效果，丢失响应不等于后端没执行。沿用运行 ID 和对账逻辑，避免重复提交。

`response.json()` 只能消费一次。接受响应与失败响应各自由明确函数消费，不让 transport 和业务层重复读取 body。

## 17. 模块迁移的标准操作：以渲染模块为例

迁移一个模块时按下面的操作顺序完成：

1. 把原 JS 复制到对应 TS 源文件。
2. 去掉 IIFE 外壳和 `window.CodeyX = { ... }`。
3. 用命名导出保留原来公开函数。
4. 给参数、返回值、依赖和内部状态补类型。
5. 把对其他 `window.Codey*` 的访问改成 import 或注入回调。
6. 把 import 时绑定事件、网络请求等副作用移入显式初始化。
7. 运行类型检查；修真正的不确定性，不用 `any` 填平。
8. 运行该模块行为测试，确认旧算法和输出保持。
9. 在迁移清单勾选旧公开函数都有新对应。

纯函数直接导出：

```typescript
export function escapeHtml(value: string): string {
  // 把现有实现原样迁入；不要借迁移重新发明转义逻辑。
  return value.replace(/[&<>"']/g, character => {
    const replacements: Record<string, string> = {
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    };
    return replacements[character] ?? character;
  });
}
```

这是类型示例，实际以当前 `render.js` 行为为准。渲染层仍须维持原有文本转义；TS 不防止把未经处理的内容塞进 `innerHTML`。

有内部可变状态的模块可用工厂实例；无需为了统一风格把每个纯函数也改成工厂。

## 18. 把 `init(deps)` 改成明确依赖接口

当前依赖注入思路可以继续使用。以 composer 为例，实际接口应从旧 `deps.*` 使用点提取：

```typescript
export interface ComposerDependencies {
  getActiveId(): string;
  getRunningSessionId(): string | null;
  findSession(id: string): Session | undefined;
  persistActiveNow(): void;
  acceptRunResponse(response: Response, sessionId: string): Promise<void>;
  // 实际实现还需加入旧 composer 消费的其他依赖。
}

export function createComposer(deps: ComposerDependencies) {
  let sendingSessionId = '';
  return {
    isSending: () => Boolean(sendingSessionId),
    // 迁入发送、草稿、重试及绑定逻辑。
  };
}
```

示例中的 `Session` 来自 domain 文件；这是接口片段，不能当作完整 composer 文件直接替换。

禁止“接口写成 `Record<string, Function>`”或把全部依赖重新装进一个巨大的 AppContext。按职责注入，避免依赖从代码中消失。

### 处理 composer 与 requests 的循环

当前 requests 需要 composer 的发送状态和重试动作，composer 又需要 requests 的操作。不要互相 import 工厂实例。

由 `app.ts` 组装回调：

```typescript
const requests = createRequests({
  ...requestDependencies,
  isSending: () => composer.isSending(),
  retryTask: (sessionId, requestId) => composer.retryTask(sessionId, requestId)
});
const composer = createComposer({ ...composerDependencies, requests });
```

这种闭包方式的条件是：`createRequests` 构造期间不能调用引用 composer 的回调。先创建全部实例，再 bind/restore/connect。若模块必须在构造时调用依赖，改成明确两阶段组装或共享独立状态，不用非空断言掩盖未初始化实例。

## 19. DOM 类型错误应该怎么修

常见错误不是 TS 刁难，而是旧代码暗中假定 DOM 总会存在且类型正确。

新增 `ui/dom.ts`：

```typescript
export function requireElement<T extends Element>(
  id: string,
  constructor: { new (...args: never[]): T }
): T {
  const element = document.getElementById(id);
  if (!(element instanceof constructor)) {
    throw new Error(`Missing or invalid UI element: ${id}`);
  }
  return element;
}

const task = requireElement('task', HTMLTextAreaElement);
const send = requireElement('send', HTMLButtonElement);
```

`const task/send` 是应用组装阶段的用法，不应放在会被无 DOM 测试 import 的模块顶层。必要 DOM 缺失时明确启动失败；可选 DOM 使用普通查询并判空。

事件目标需要实际缩窄：

```typescript
row.addEventListener('click', event => {
  const target = event.target;
  if (target instanceof Element && target.closest('button')) return;
  switchSession(sessionId);
});
```

不要写 `event.target as HTMLElement` 后直接 `.closest()`。类似地，`document.activeElement` 不保证有 `.focus()`，焦点恢复前检查 `instanceof HTMLElement`、`isConnected` 和当前焦点归属。

当前 DOM 上的 `_returnFocus`、`_diffContent` 等元数据可以暂时使用局部扩展接口，或改成 `WeakMap<Element, Metadata>`。不要给全局 HTMLElement 声明几十个可选业务字段。WeakMap 改法必须保持缓存比较和焦点语义，并验证元素更新后 metadata 是否仍属于正确实例。

## 20. 状态与持久化：最容易被类型迁移破坏的地方

把状态源集中到 `state/store.ts`，第一轮保持现有可变更新策略。不要同时换成全新 reducer 框架；这会扩大行为变化。

重点保留：

- `codey:sessions`、`codey:active`、`codey:projects`、`codey:ui-updated`、`codey:ui-revision`。
- 侧栏宽度等其他存储键与归一化逻辑。
- 本地 meaningful state、启动期间 dirty 状态与服务器恢复的优先级。
- 本地 revision 与服务器 base revision 的区别。
- 正在保存、等待下一次保存、409 冲突与手动重试。
- 400ms 合并保存的现有行为。
- pagehide 捕获草稿、缓存、sendBeacon 和兜底保存。
- terminalRuns/researchRuns 的现有长度限制与去重规则。

### 运行时索引不进入保存数据

可以先保留不可枚举属性，然后增加显式序列化函数，逐项输出允许保存字段：

```typescript
export interface SessionIndexes {
  eventKeys: Set<string>;
  toolKeys: Set<string>;
  terminalRunIds: Set<string>;
}
```

若索引改放到独立 Map/WeakMap，必须覆盖：恢复会话、删除消息、替换 pending tool、删除会话和重新加载。内存里的 Set 不能依赖 JSON round-trip 自动恢复。

保存前用 `serializeSession(session)` 输出 StoredSession；恢复时用 `normalizeStoredSession(raw)` 和 `hydrateIndexes(session)`。通过测试证明 `JSON.stringify` 不包含运行时索引。

### 旧消息不能因类型不完整被清空

已有缓存可能出现缺字段、未知消息类型和部分旧字段。先用归一化兼容，记录不可显示的未知消息策略；不要 decoder 抛错后把整个会话列表恢复成空。

网络状态中的审批等安全关键字段要求严格；历史展示字段的修复策略可以宽容。两类边界不要用同一套强制规则。

## 21. composer 与请求状态：逐项保留发送合同

不要只验证“点击发送后出现回复”。至少用以下场景驱动迁移：

1. 空输入不发送。
2. 双击、Enter 和点击竞争时只提交一次。
3. 一次提交在途时切换会话，失败仍属于原会话。
4. 发送失败不清草稿，不破坏选择区。
5. 发送成功前用户输入新内容，成功响应不能清除新内容。
6. 文本没变但 revision 改变，仍不能认为是原草稿。
7. 重试追加同一 request 的 attempt，不把当前新输入作为重试文本。
8. 重试只能用于符合旧逻辑条件的失败请求。
9. 请求与响应使用同一个客户端 run ID 合同。
10. POST 在途时，较早的空闲快照不能认定其失败。
11. 迟到 task_start 不能复活终态 attempt。
12. 旧 attempt 结束不能结束新 attempt。
13. 409 busy 后对账，显示正确运行所属会话。
14. worker busy、模型选择无效等错误保持当前恢复动作。
15. 响应丢失时先对账，不自动发送第二次。

当前 `clearDraftIfUnchanged()` 会先捕获最新草稿，比较文本与 revision，再决定清理。这些步骤和顺序保留，不把比较缩减为字符串相等。

如果将 sending lock 放进共享状态，保证修改和清理发生在原有 try/finally 对应时机。不要为了类型方便把锁变成只限制当前活动会话。

## 22. SSE：保留当前协议，补齐生命周期

保持当前 `EventSource` 自动重连机制。`onerror` 当前主要安排状态提示；不要叠加另一套创建新连接的重试定时器，导致两个连接并存。

实例内部至少显式管理：

```typescript
let source: EventSource | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconcilePromise: Promise<void> | null = null;
let lastKnownEventId = 0;
let disposed = false;
```

提供 `connect()`、`reconcileRunState()`、`acceptRunResponse()`、`eventKey()` 和 `dispose()`。dispose 不等于关掉 source 就结束：还需清计时器、阻止晚到 fetch 更新、移除监听并释放缓冲。

游标处理保持现有细节：

- 使用 `lastEventId`，缺失时才使用 payload event_id。
- 只有合法安全整数参与常规游标更新。
- 小于或等于已知游标的普通事件被去重。
- `resync_required.cursor` 可以为 0，且可要求游标回退；不能统一套用“只接受更大游标”。
- `hello` 引发对账和 provider status 刷新。
- 对账已有 Promise 时共享它，不创建第二个请求。
- 对账中缓冲事件；超过当前 100 条限制后标记 gap，再次对账。
- 旧 `/api/state` 返回不能覆盖已接受的更新终态。

### 事件类型设计

将已知事件做判别联合。未知事件用外层结果表示，不把 `{ type: string }` 塞进同一个联合破坏缩窄：

```typescript
export type DecodeEventResult =
  | { kind: 'known'; event: ServerEvent }
  | { kind: 'unknown'; type: string }
  | { kind: 'invalid'; reason: string };
```

已知类型来自生产者与消费者核对，至少包含 hello、resync_required、status、providers、turn、reasoning、tool_started、tool、info、ghost_post_turn_warning、teach_request、task_start、task_done、shell_request、shell_result、reply、review。

### 事件派发顺序不可随意改

当前 `handleServerEvent()` 的任务完成清理、requests.acceptsEvent、运行作用域过滤以及具体事件分支有明确先后。先迁入 `dispatch.ts` 保持顺序；后续抽函数时用测试证明等价。

审批请求和结果允许比运行存活更久；不能给所有带 run_id 的事件统一加“仅当前运行可接收”过滤。

SSE 测试使用可控 fake EventSource、fake timer、可控 Promise，分别制造断线、游标重置、重复、乱序、缓冲溢出和销毁后响应。不靠真实 sleep 猜竞态。

## 23. 页面启动：按照当前流程构造，不凭想象重排

当前流程的核心顺序：

```text
页面脚本准备状态和函数
  → authorize()
  → adoptCatalog()
  → 采用实时默认 provider
  → 注册持久化退出处理
  → 初始化各 UI / 状态 / SSE / requests / composer 模块
  → 渲染侧栏与聊天、同步控件
  → 刷新 provider 状态
  → await restoreUiStateFromServer()
  → 开放 task 输入、更新发送控件
  → connectEvents()
```

迁移前检查 boot 之前的顶层代码还做了哪些缓存读取、绑定、模型加载等操作。只复制 `boot()` 不能完整保留启动行为。

新增 `bootstrap.ts`，注入可测试的启动阶段接口；`app.ts` 创建实例并提供实现。避免 bootstrap 自己创建第二套状态。

授权函数先从 fragment 提取 token，再清除 fragment，然后交换 cookie；已有 cookie 则 probe。授权失败时后续目录、状态、任务、SSE 不得继续请求。

启动错误展示使用现有状态区域，保留不可操作状态，给出可理解的恢复指引。不要未经设计提供会重复绑定的“再初始化一次”按钮。

启动至少测试：授权拒绝、目录较慢、服务器恢复失败、旧缓存、本地编辑与服务器恢复竞争、重复调用、document.hidden、缺动画帧、模块错误。就绪条件使用真正状态/控件条件，不把 `requestAnimationFrame` 当作必要启动门槛。

## 24. 研究、变更、本地上下文与模型：不能把它们当作小配件

### 研究

保留 researchRuns 的归一化、来源/引用/证据字段、笔记缓存失效、图谱深度、当前 tab、note focus，以及服务端决定的恢复资格。历史记录中的 `restoreable` 不应仅凭缓存值恢复为 true。

研究恢复请求有外部效果，不自动重试。会话切换期间返回的研究结果要更新原归属，不能覆盖新会话抽屉。

### 项目变更

给请求保存明确的 project 身份或已有请求序列。A 项目查询未完成时切换到 B，A 的晚响应不能替换 B；抽屉关闭后晚响应不能把它重新打开。

AbortController 可节省网络，但不能替代身份检查：abort 之前已经完成的响应仍可能执行后续代码。保留当前判断，不借迁移移除。

### 本地上下文

summary/action/export 的输入和输出分别定义。保留 session/project 作用域，确认页面下载使用的文件名、编码、Blob MIME 和 URL.revokeObjectURL 行为。密钥和本地状态文件不进入构建资源。

### 模型与设置

modelSelection 中 connection_id、base_url、model、name、effort、thinking 和每模型 effort 映射分别处理。保留 null 与缺失的区别，不把 `thinking: false` 因为是假值而删除。

设置保存中的 base_revision/409 也需要独立测试；UI state revision 与模型设置 revision 不共用一个变量。

## 25. 前端单元测试配置与示例

新增 `frontend/vitest.config.ts`：

```typescript
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    globals: false,
    include: ['tests/**/*.test.ts'],
    restoreMocks: true,
    unstubGlobals: true,
    coverage: {
      provider: 'v8',
      include: ['src/**/*.ts'],
      exclude: ['src/**/*.d.ts', 'src/testing/**']
    }
  }
});
```

纯状态/协议测试默认 Node；DOM 测试在文件首行指定：

```typescript
// @vitest-environment jsdom
```

显式清理每项测试的 DOM、timer、实例和网络 fake。`restoreMocks` 不会自动销毁你的 EventSource，也不会清理模块闭包状态。[Vitest 配置说明](https://vitest.dev/config/)

草稿归一化测试示例，按现有算法验证有价值的边界：

```typescript
import { expect, it } from 'vitest';
import { normalizeDraft } from '../src/state/normalize';

it('clamps invalid draft selection and preserves text', () => {
  expect(normalizeDraft({
    text: 'hello', start: 99, end: -1, direction: 'invalid', revision: -2
  })).toEqual({
    text: 'hello', start: 5, end: 5, direction: 'none', revision: 0
  });
});
```

其他必要测试使用真实模块输入输出，不能通过直接修改测试实现里的状态来证明模块正确。

协议 fixture 新增导出工具：调用 Python 的真实响应构造函数/受控 HTTP fixture，生成提交接受、忙碌错误、运行状态、UI state、审批事件、研究与模型样本。固定测试时钟、ID、路径，移除敏感字段；不要把随机路径写入版本库。

`python -m tools.export_frontend_contracts --check` 是待实现命令，比较重新生成样本和提交样本；差异失败。TS decoder 测试读取相同样本，另外增加缺字段、错误类型、额外字段和旧版本数据。

样本证明具体场景，不能代替全部协议定义；后端改变时由导出校验和消费测试共同暴露。

## 26. 迁移现有 Python 测试：明确替代关系

把旧测试分为四组处理：

| 旧测试 | 新位置/新方式 |
|---|---|
| 真正用户操作与视觉行为 | 保留 Playwright；必要时改等待就绪条件 |
| 后端授权、HTTP、保存与审批合同 | 保留 Python 测试 |
| Node `vm` 加载 IIFE、调用旧命名空间 | 转 Vitest import 模块；记录一对一场景 |
| 固定旧文件名、传统脚本、源码字符串顺序 | 改为源码结构/产物结构/启动行为验证 |

例如 `test_operator_bootstrap_before_ui_requests.py` 中 Node VM 授权测试迁入 TS 单测；真实后端 token 过期与并发交换测试继续保留。HTML 文本搜索“authorize 在 adoptCatalog 前”改成可控 Promise 的启动测试和真实 HTTP 请求顺序证据。

旧 `test_ui_architecture.py` 改为检查：

- 源 HTML 无业务内联脚本且只有模块入口。
- 产品 TS 不引用旧 `window.Codey*`。
- 生产 HTML 引用的 JS/CSS 存在且路径合法。
- 生产 HTML 不引用 `/src/`、`.ts`、Vite dev client 或 localhost 开发端口。
- 所有生产 JS/CSS 来自同一份构建元数据。
- 无测试桥、无旧脚本标签、无测试源码。
- CSS 与 DOM 约束仍由对应行为测试覆盖。

搜索残留不仅查测试：`tools/ui_e2e.py` 也直接调用 `activeSession()`；所有 recorder/review/probe 工具都检查一次。

## 27. 测试全局函数的替代：优先用户操作，必要时测试桥

业务模块最终不挂载 `window.Codey*`。测试准备、检查复杂状态可用集中测试桥，避免为每条测试暴露几十个产品全局函数。

建议测试桥只有少量能力：等待 ready、读取不可变快照、通过正式 normalize/store 路径装载 fixture、受控注入事件。不要暴露真实 token 或设置无限制命令执行入口。

`bootstrap` 完成后在测试模式挂载：

```typescript
if (__FRONTEND_TEST__) {
  const { installTestBridge } = await import('./testing/bridge');
  installTestBridge(app);
}
```

生产构建中的编译常量为 false，工具应消除分支。必须检查实际 bundle/module graph，并在真实浏览器确认 `window.__codeyTest` 不存在。仅扫描某个字符串还不足以证明没有测试模块，需要结合构建 manifest、源码导入规则和浏览器检查。

测试桥只存在于 `dist-test`，发布脚本拒绝其目录。内部状态测试可使用测试构建；授权、资源加载、核心发送/审批/恢复流程必须有生产构建测试。不要让全部浏览器证据都依赖另一套测试产物。

桥返回 snapshot 时拷贝或序列化，避免测试偷偷修改真实状态。迁移旧 `page.evaluate('activeSession()...')` 时区分“读取”和“执行动作”，后者尽量改成点击、输入和键盘操作。

## 28. Python 候选资源接入：具体改哪些地方

第一轮仍通过 Python 同源提供候选资源，使用 watch 构建后刷新。不依赖 Vite 开发服务器作为产品验证入口。[Vite 后端集成文档](https://vite.dev/guide/backend-integration)

新增待实现环境变量 `FRONTEND_ROOT`。需要同步修改：

1. `codey/env_names.py`：按项目现有约定登记名称。
2. `codey/app/http_plumbing.py`：资源目录的选择与验证。
3. `codey/app/server.py`：图标和所有引用资源根目录的位置。
4. 测试 fixture：如何为一次服务实例选择候选构建。

当前 `server.py` 直接 import 了 `WEB_DIR`，不能只在 `http_plumbing` 后续改一个全局值；已经 import 的别名不会自动更新。

推荐新增不可变 `FrontendPaths` 配置，在服务初始化时选择 root，并让 send_index、resolve_web_asset、icon 路径都使用同一个配置。如果为了最小修改保留模块级配置，则明确进程启动时冻结，并让所有调用者通过同一函数获取，测试不能并行切换全局 root。

选择规则：未设置则使用包内 `codey/web`；设置则解析绝对路径，确认 root/index.html/assets 存在且匹配完整构建。无效时明确失败，不静默退回旧页面。

保留资源扩展名允许名单和路径越界检查。第一版只发布一层 JS/CSS 和独立图标；以后需要字体/图片时再同时扩展 MIME、路径规则和包数据。

同一个候选 root 可用于迁移测试，完成后也可保留为本地开发选项；它不能允许来自 HTTP 参数的任意路径选择。

实现后开发命令为：

```powershell
# 终端 A
Set-Location E:\codey\frontend
npm run build:watch

# 终端 B：先等待首次构建成功
Set-Location E:\codey
$env:FRONTEND_ROOT = (Resolve-Path frontend/dist).Path
python -m codey --help
# 按 help 和当前 README 中的 ui 启动方式运行，打开服务生成的授权链接。
```

不要用 `vite preview` 判断授权、Origin 或 SSE 是否正常。它适合静态资源预览，不能代替 Python 服务。

## 29. 缓存与 ESM：具体兼容策略

第一版采用保守策略：保留现有 HTML 的 no-cache、ETag、内容摘要版本逻辑；Vite JS/CSS 使用哈希文件名，未带 `v` 参数时沿用现有资源 no-cache。这样先保证正确性，不必同时重写缓存体系。

图标仍通过 `/icon.ico` 提供，保持 revalidation。不要单凭“URL 名字看起来有哈希”就给任意请求加一年 immutable。

如后续优化 hash 文件 immutable，使用发布构建元数据中的资源名单校验；只对已知内容寻址产物开放。保留旧 `?v=` 策略时也要检查其合同，不扩大到任意查询参数。

`send_index()` 目前使用 assets 的一层 glob；第一版限制产物为该结构。若出现嵌套 chunk、字体或图片，要改成受限递归遍历并测试，不让资源摘要漏掉新文件。

检查 ESM MIME：JS 继续提供 `application/javascript`，CSS 提供 `text/css`。模块脚本遇到 404 或错误 MIME 会直接加载失败，浏览器测试记录 network failure 和 pageerror。

缓存测试至少覆盖：首次 200、If-None-Match 后 304、HTML 或资源变化后新 ETag、同包版本但不同资源的 HTML 更新、源路径越界、未知扩展名、错误 MIME、资源来自同一 root。

当前 `_static_etag()` 是包含 mtime/size 等签名的弱 ETag，并不是文件 SHA-256。方案中发布元数据使用内容摘要，但不应因此宣称现有 HTTP ETag 已是内容哈希。若测试要求同大小、同 mtime 的资源替换也被识别，需要同时调整静态缓存失效策略；只更改 ETag 字符串而保留旧缓存签名仍可能读到旧内容。

## 30. 发布脚本：具体输入、检查和写入算法

新增 `tools/publish_frontend.py`，设计三个模式：

```powershell
python -m tools.publish_frontend --validate
python -m tools.publish_frontend --check
python -m tools.publish_frontend
```

`--validate` 检查候选是否可发布；`--check` 比较候选与当前生产发布文件，退出码非零表示不同或无效；默认模式发布。所有模式默认只接受 `frontend/dist`，显式拒绝 dist-test。

发布检查按下面的算法实现：

1. 从 repo root 推导固定 dist 和 web 路径，resolve 后验证它们在预期 workspace 内，不从任意外部参数推导删除路径。
2. 读取 `dist/index.html`、图标和 Vite manifest；使用 HTMLParser 读取 src/href，不靠一个正则覆盖全部 HTML。
3. HTML 除 `/icon.ico` 外的本地 JS/CSS 引用必须属于 `/assets/`，存在并在 dist 内；拒绝 `..`、开发 URL、源 TS 路径。
4. 遍历 manifest 的 file/css/assets/imports/dynamicImports 引用关系，检查所有 chunk 和静态依赖存在；不仅检查 HTML 直接引用。
5. 第一版仅接受发布规则允许的 JS/CSS 与图标；出现新类型先更新服务与包数据，不悄悄漏文件。
6. 核对实际 assets 目录与 manifest 依赖，拒绝异常产物、测试 chunk、调试 map 和旧业务脚本残留。
7. 计算每个发布文件的 SHA-256，使用相对 POSIX 路径排序。
8. 计算源码摘要：frontend 的源 HTML、src、public、package/lock、tsconfig、Vite 配置等真正构建输入。测试配置与 CI 可另记，避免无关测试改动强迫重新发布资源。
9. 生成 `frontend-build.json`，包含 schema 版本、源摘要和文件摘要；不放绝对路径、当前时间、机器名，避免跨平台不可重复。
10. `--check` 比较文件集合和每个字节，包括元数据；只检查，不写文件。
11. 发布模式先在同一文件系统的临时 sibling 目录完整写入并重新验证，然后切换到 web。
12. 切换前检查生产目录是否含未管理的文件，存在则拒绝并指出文件。旧源码删除是首次迁移的明确步骤，不把不认识的文件当垃圾删。
13. 切换失败恢复旧目录，清理自己创建且已验证路径的临时目录。

首次完整迁移时，旧 `codey/web/assets/*.js`、CSS 和 HTML 的所有权从手写源码变成生成产物；在切换提交中明确替换。图标源进入 `frontend/public`，由构建复制。

### 不承诺热发布时目录替换完全原子

Windows 上替换非空目录可能受文件句柄影响；跨平台的两步重命名也可能存在短暂空档。第一轮发布命令要求本地开发服务停止后执行，再重启验证。发布 wheel/安装包时，生成完整包后正常发布即可。

如果以后需要运行中的热切换，应做版本目录和进程级资源 root 切换，并考虑已打开页面的旧 chunk。不能先删除所有旧哈希资源再让旧页面继续发动态 import。

### 发布脚本自身的必要测试

缺 JS、缺 CSS、缺动态 chunk、越界 manifest、测试目录、额外产物、摘要不同、未管理文件、check 不写文件、复制失败恢复、重复发布无变化。它会替换发布目录，值得用临时目录测试失败路径。

## 31. 改 JavaScript 语法检查与源码统计

当前 `tools/check_javascript.py` 对网页 JS 使用 `node --check path.js`。ESM bundle 可能带 import/export，而 `codey/web` 不一定处在 `type: module` 包范围里，Node 可能按 CommonJS 解释。

修改为：网页构建 JS 通过 stdin 按 module 解析；provider JS/MJS 保留各自原有规则。

Python 示例：

```python
subprocess.run(
    ['node', '--input-type=module', '--check'],
    input=path.read_text(encoding='utf-8'),
    text=True,
    capture_output=True,
    check=True,
)
```

错误报告补充对应 path，避免 stdin 输出缺失文件定位。不要在整个 Python 仓库根新增 `type: module` 改变 provider 工具行为。

`source_size_inventory.py` 当前 SOURCE_ROOTS 不包含 frontend，suffix 不包含 `.ts`。新增 frontend 和 `.ts`；生产 minified 产物应标为 generated 或排除人工源码统计，不能拿生成文件行数评价维护性。

## 32. 真实 HTTP 与生产浏览器验证

保留可控路由 fixture 做界面场景，但新增/扩展真实服务 fixture：

- 创建临时 AppContext 和状态目录。
- 创建 `CodeyHTTPServer(('127.0.0.1', 0), Handler)`。
- 使用 `launch_url()` 得到带启动凭据的链接。
- 浏览器真正请求 Python 返回的 HTML/JS/CSS。
- 不拦截 `/` 和 `/assets`；必要时仅替换模型提供方，避免依赖公网模型。
- 收集 pageerror、资源失败、HTTP 状态及已分类的 console error。
- 完成后关闭浏览器、HTTP server、线程和 AppContext。

核心场景必须使用生产 dist 或最终 `codey/web`：启动授权、资源加载、发送、停止、审批、刷新恢复、SSE 对账。测试期间的模型必须是受控 scripted provider；若某项必须访问外部模型，明确单独列为 live evidence。

不要要求预期错误场景的 console 永远为空；例如模拟拒绝/断网可能产生预期日志。检查意外错误，同时显式列出预期错误，不能用通配过滤屏蔽全部 console error。

检查 SSE 的并发活动连接数量，自动重连导致历史请求总数增加不等于重复连接。用服务计数或 EventSource 生命周期观测当前连接，不只数所有网络请求。

## 33. 安装包：wheel 与 sdist 分别证明

修改 `pyproject.toml` 包数据，保留现有规则并增加：

```toml
"web/frontend-build.json"
```

如果最终允许嵌套 assets，补相应包数据规则并实际检查 archive；不能假定 `*.js` 会包含任意深度目录。

sdist 策略确定为：包含已构建 `codey/web`，安装时无需 Node；同时包含完整 frontend 源码和锁文件以支持审查与开发重建。新增/调整 `MANIFEST.in`，精确列入源码与配置，排除 node_modules、候选 dist、coverage 和测试产物。

```text
include frontend/package.json frontend/package-lock.json frontend/.npmrc
include frontend/tsconfig.json frontend/tsconfig.tools.json frontend/tsconfig.tests.json
include frontend/vite.config.ts frontend/vitest.config.ts frontend/eslint.config.mjs
include frontend/index.html
recursive-include frontend/src *.ts *.css
recursive-include frontend/public *.ico
recursive-include frontend/tests *.ts *.json
```

若有重建必需的其他文件或发布工具，要同时加入；不能假定 setuptools 会因 Git 跟踪自动包含所有内容。

构建前先完成发布一致性检查：

```powershell
Set-Location E:\codey\frontend
npm ci
npm run check
npm run build
Set-Location E:\codey
python -m tools.publish_frontend --check
python -m build
```

`build` 依赖需要纳入开发/CI 环境，以上新增 publish 命令要先实现。

新增 `frontend_package_smoke.py`，具体执行：

1. 检查 wheel zip 中 HTML、图标、metadata 和 metadata 引用资源全部存在。
2. 在 repo 外创建虚拟环境与临时工作目录。
3. 安装 wheel 及所需运行依赖，清除对子进程有影响的 PYTHONPATH。
4. 确认 `codey.__file__` 位于该环境 site-packages，不在 E:\codey。
5. 子进程从临时目录启动真实服务，使用临时状态 home。
6. 输出通过受控通道取得的 launch URL，不把凭据写入公开日志。
7. 真实浏览器访问，确认授权、资源、输入就绪和关键流程。
8. 关闭服务、线程和浏览器，超时明确失败。
9. 解压 sdist，在隔离目录构建 wheel 并重复资源检查；确认无需 npm 也能安装已有产物。
10. 另在开发环境从 sdist 中 frontend 源码执行 npm ci/build，检查它确实可重建。

不要通过在父进程里 import codey 再切 cwd 来证明安装包；模块缓存仍可能来自原仓库。

## 34. CI：把新门禁放到现有门禁之前

修改 `.github/workflows/ci.yml`，保留 Windows Python matrix 和 Linux 当前任务。Node setup 后添加 npm 缓存：

```yaml
- name: Set up Node.js
  uses: actions/setup-node@v4
  with:
    node-version: "24"
    cache: npm
    cache-dependency-path: frontend/package-lock.json

- name: Install frontend dependencies
  working-directory: frontend
  run: npm ci

- name: Frontend checks
  working-directory: frontend
  run: npm run check

- name: Frontend production build
  working-directory: frontend
  run: npm run build

- name: Verify frontend contracts
  run: python -m tools.export_frontend_contracts --check

- name: Verify published frontend
  run: python -m tools.publish_frontend --check
```

需要 Python 的检查放在 Python dependencies 安装之后。修改原 Node step，避免重复 setup。

主 Windows 任务运行 coverage；测试桥构建只在需要的浏览器 job 生成。其他 Python matrix 可省重复 coverage，但保留类型、构建和产物检查。Linux 也做安装、类型、模块测试和构建，以发现大小写、路径和换行差异。

完整 E2E 单独一个 Windows job 或主任务显式设置：

```yaml
- name: Required browser E2E
  env:
    RUN_BROWSER_E2E: "1"
  run: python -m pytest -q tests/test_ui_browser_e2e.py
```

确认所需真实浏览器可用，并禁止该必需节点 skip。现有 machine_contract_gate 对必需检查已有 no-skip 机制，可按其约定扩展；不要把全项目所有合理 skip 都变成失败。

新增安装包验证任务：构建 wheel/sdist、archive 检查、隔离安装、生产浏览器 smoke。保存前端 coverage、浏览器截图/trace、基线和安装包检查结果。

保持 Python coverage 门禁；增加 TS coverage 并优先约束 requests、persistence、SSE、decode 的关键分支。初次依据实际结果定阈值，不靠排除复杂文件凑百分比。

## 35. 实施顺序：每一步有明确退出条件

| 步骤 | 做什么 | 离开该步骤前必须确认 |
|---|---|---|
| 1 | 建立分支、基线、功能对照 | 起点可运行，失败/skip 已解释 |
| 2 | 工具链与源目录 | npm ci、最小 tsc/Vite 可运行 |
| 3 | DTO、domain、decoder、协议样本 | 类型与真实响应对齐 |
| 4 | render/process 等基础模块 | 原渲染输出、复制、展开保持 |
| 5 | 状态、归一化、索引、持久化 | 旧缓存、409、pagehide 保持 |
| 6 | 模型、会话、研究、抽屉 | 作用域、迟到响应、焦点保持 |
| 7 | requests/composer | 草稿与 attempt 全部关键场景通过 |
| 8 | SSE/dispatch | 游标、去重、对账、终态和审批通过 |
| 9 | app/bootstrap 与全部内联迁出 | 授权顺序、恢复、就绪通过 |
| 10 | Python 候选入口与测试迁移 | ESM 资源真正由 Python 提供 |
| 11 | 发布脚本、缓存、语法工具 | 完整产物一致，失败路径可恢复 |
| 12 | CI、wheel、sdist | 跨平台与隔离安装验证通过 |
| 13 | 切换默认、移除旧源码与兼容桥 | 只有完整 TS 源码和生产发布产物 |
| 14 | 最终验收与回退演练 | 功能矩阵有完整证据 |

步骤内允许回到前一步修依赖，但不降低 strict、不删除失败场景、不以测试构建替代生产验证。

## 36. 如何定位典型迁移错误

| 现象 | 优先检查 | 正确处理方向 |
|---|---|---|
| 页面空白 | 模块路径、MIME、pageerror、顶层异常 | 修资源和初始化，不加旧脚本兜底 |
| 类型报 hundreds errors | domain、依赖接口、DOM 空值 | 先补共享模型，再修调用者 |
| provider 默认值错误 | 解构快照、目录时序 | 使用实时 getter，保留 adoptCatalog 顺序 |
| 刷新后历史消失 | serializer/normalizer/后端允许字段 | 恢复 round-trip，不清缓存掩盖 |
| 发送后新输入消失 | draft revision、清理时机 | 保留文本与 revision 双检查 |
| 旧运行复活 | attempt identity、dispatch 顺序 | 恢复终态过滤和对账规则 |
| SSE 重复消息 | 游标、session 索引、两条连接 | 查去重与实例生命周期 |
| 页面关闭后仍保存 | dispose、晚响应、pagehide | 区分退出保存与重复绑定 |
| npm build 过了但 tsc 失败 | 构建脚本没运行 typecheck | 把 tsc 放进正式 build 门禁 |
| Node syntax 对 bundle 报错 | CommonJS 解释 ESM | 网页产物按 module 解析 |
| 源码启动正常、wheel 失败 | 包数据、动态 chunk、源码 import 污染 | 隔离安装检查真实请求 |
| Linux 独有失败 | import 大小写、路径、换行 | 统一文件名与相对路径 |
| 产物 --check 总不同 | 时间戳、绝对路径、版本、环境输入 | 去机器特有字段，锁定构建输入 |

不要用删除 localStorage、关闭授权、全局 `as any` 或更新截图基线来处理尚未理解的回归。

## 37. 最终命令与验收证据

以下是新增脚本全部实现后的最终检查顺序，不是当前可直接完整执行的现有命令集：

```powershell
Set-Location E:\codey\frontend
npm ci
npm run typecheck
npm run lint
npm run test:coverage
npm run build

Set-Location E:\codey
python -m tools.export_frontend_contracts --check
python -m tools.publish_frontend --check
python -m tools.check_javascript
python -m ruff check .
python -m mypy codey
pyrefly check codey
python -m tools.machine_contract_gate
python -m pytest -q
python -m tools.ui_e2e --artifacts .e2e-artifacts/frontend-after --json
python -m build
# 运行新增 package smoke 工具，参数按最终实现文档。
```

使用显式 RUN_BROWSER_E2E 执行必需 pytest E2E 节点，或将同等完整流程作为 gate；记录所选方式与结果，不能因为已调用工具就把 skipped pytest 写成 passed。

最终报告至少包含：

- 迁移前/后 SHA、实际工具版本、20 个旧模块及内联函数的对应清单。
- TS 产品源码无 JS 输入、无业务 HTML 内联脚本、无旧全局依赖。
- 类型、lint、模块、协议、Python、生产浏览器、完整 E2E 的结果。
- 功能矩阵每项证据，尤其请求/草稿/SSE/审批/恢复。
- Windows/Linux 构建与发布目录字节一致性。
- wheel、sdist、隔离安装与真实资源加载结果。
- 生产测试桥不存在、无开发服务器依赖。
- 视觉对照和所有有意差异的说明。
- 回退验证及已知未验证项；有必需项未验证就不能写“全部完成”。

## 38. 回退与日后开发

这次迁移不改变服务器协议和保存数据格式，尽量让回退只涉及代码与发布资源。若新增数据字段或版本，必须验证旧前端读取新保存数据，否则仅 git revert 不足以恢复使用。

发布前用独立临时 checkout/旧 wheel 验证旧版本仍能启动和读取迁移产生的兼容状态，不在有未提交修改的主工作区强行 reset。

发布后通过完整迁移提交的 revert 或旧版本包回退，包含源码、工具链、锁文件、Python 静态接入、测试、CI 和生成资源。不要只恢复 HTML。

日常开发流程最终变成：修改 TS/CSS/源 HTML → 类型与相关测试 → 构建 → 发布脚本 → 生产浏览器验证 → 提交源码、锁文件变化和生成资源。

一次完整迁移值得交付的结果，是“以后能安全修改并正常发布”的整套前端工程。验收应围绕这些具体能力，不围绕后缀数量、目录数量或覆盖率数字。


