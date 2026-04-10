# Web Service Spec (参考 spring_research 风格)

本文档描述 `wechat-article-to-markdown` 项目的 Web 服务层全部需求，参考 `spring_research` 项目的 Flask 应用架构。

---

## 1. 整体架构

| 模块 | 文件 | 职责 |
|------|------|------|
| 配置 | `config.yml` / `config_loader.py` | YAML 配置加载，提供 server/settings 属性 |
| 任务队列 | `task_queue.py` | 持久化任务队列，基于 `meta.json`，后台 worker 线程 |
| Web 服务 | `app.py` | Flask HTTP API + 静态文件服务 |
| MCP 服务 | `mcp_server.py` | MCP stdio server，供外部 AI agent 调用 |
| 前端 | `templates/index.html` | 单页 Web UI（添加/队列/已下载三个 Tab） |
| 核心逻辑 | `wechat_article_to_markdown.py` | 抓取、解析、图片下载、Markdown 转换 |

启动入口为 `app.py`，启动时依次：
1. 加载 `config.yml`
2. 初始化 `TaskQueue` 并启动后台 worker 线程
3. 调用 `mcp_server.init()` 共享 config 和 task_queue 实例
4. 调用 `mcp_server.start_in_thread()` 在 daemon 线程中运行 MCP stdio server
5. 启动 Flask HTTP 服务

---

## 2. 配置 (`config.yml`)

```yaml
server:
  host: "0.0.0.0"
  port: 5001
  debug: false

settings:
  passwd: "wiz"           # 访问密码，默认 "wiz"
  output_dir: "output"    # 文章输出目录
  max_concurrent: 1       # 最大并发下载数（当前固定为 1）
  auto_refresh_interval: 5  # 前端队列自动刷新间隔（秒）
  max_queue_size: 100     # 最大等待队列长度
```

- `config.yml` 加入 `.gitignore`，防止密码泄漏
- 提供 `config.sample.yml` 作为模板（含注释）
- `Config` 类通过 `@property` 暴露所有配置项，支持 `reload()`

---

## 3. 文章存储结构

以 URL 中的唯一 ID 作为目录名（从 `/s/<ID>` 路径提取；query 风格 URL 用 sha256 短哈希兜底）：

```
output/
  <article_id>/
    meta.json       # 任务状态 + 文章元数据（持久化，重启不丢失）
    index.html      # 精简后的 HTML（图片引用本地路径，移除 script/style/base64）
    index.md        # 转换后的 Markdown
    images/         # 下载的图片文件
      img_001.png
      img_002.jpg
      ...
```

### 3.1 `meta.json` 结构

```json
{
  "id": "MS-1iU5YMS43Pt9LKl2pTQ",
  "url": "https://mp.weixin.qq.com/s/MS-1iU5YMS43Pt9LKl2pTQ",
  "status": "success",
  "title": "文章标题",
  "author": "公众号名称",
  "publish_time": "2024-01-15 10:30:00",
  "error": "",
  "dt_task": "2024-01-15 12:00:00",
  "dt_start": "2024-01-15 12:00:01",
  "dt_done": "2024-01-15 12:00:30"
}
```

### 3.2 `index.html` 精简规则

`index.html` 在 `index.md` **之后**生成，经过以下处理：
- 所有 `<img>` 的 `src` / `data-src` 如果匹配已下载图片，则替换为本地 `images/img_xxx.ext` 路径
- 未下载的远程 URL 和 base64 data URI 的 `src` 清空（避免文件膨胀）
- 移除 `data-src` 属性
- 移除 `<script>`、`<noscript>`、`<style>` 标签
- `index.html` 与 `index.md` 共用 `images/` 目录下的外部图片资源

---

## 4. 任务队列 (`task_queue.py`)

### 4.1 任务状态

| 状态 | 说明 |
|------|------|
| `pending` | 等待处理 |
| `running` | 正在下载 |
| `success` | 下载完成 |
| `failed` | 下载失败 |
| `timeout` | 超时（running 超过 30 分钟自动检测） |

### 4.2 去重逻辑（`add_task`）

| 已有状态 | 行为 | 返回 action |
|----------|------|-------------|
| `success` | 不重复下载 | `exists` |
| `failed` / `timeout` | 重置为 pending，重新入队 | `retry` |
| `pending` / `running` | 直接返回 | `already_queued` |
| 不存在 | 创建新任务 | `created` |

### 4.3 队列列表（`get_tasks`）

- 默认**不返回** `success` 状态的任务（已完成任务从队列移除，只在"已下载"视图展示）
- 提供 `include_success=True` 参数可包含已完成任务
- 按 `dt_task` 降序排列，限制返回条数（默认 50）

### 4.4 超时检测

- `running` 状态的任务，如果 `dt_task` 距当前时间超过 30 分钟，自动标记为 `timeout`
- 超时检测在读取时动态判断，不修改 `meta.json`

### 4.5 一键重试（`retry_all`）

- 将所有 `failed` 和 `timeout` 状态的任务重置为 `pending`
- 返回重置的任务数量

### 4.6 单任务重试（`retry_task`）

- 输入：`article_id`
- 仅当任务状态为 `failed` 或 `timeout` 时可重试，否则抛出 `ValueError`
- 重置为 `pending`，清空 error，更新 `dt_task`
- 不存在时返回 `None`

### 4.7 URL 查询（`lookup_by_url`）

- 输入：微信文章 URL
- 提取 article_id，查找对应 `meta.json`
- 如果 status 为 `success`，额外返回：
  - `markdown`：完整 Markdown 文件内容
  - `browse_url`：可浏览的 .md 文件路径（如 `/articles/<id>/index.md`）
  - `browse_html_url`：可浏览的 .html 文件路径
- 如果未提交过，返回 `None`

### 4.8 后台 Worker

- 单独的 daemon 线程，轮询 pending 任务（按 `dt_task` 升序取最早的）
- 调用 `fetch_article()` 执行下载
- 成功时更新 meta.json 为 success + 写入 title/author/publish_time
- 失败时更新 meta.json 为 failed + 写入 error 信息
- 使用 `sys.exit(1)` 替换为 `raise RuntimeError`，避免杀死 web worker 进程

---

## 5. HTTP API (`app.py`)

所有 API 返回 JSON，包含 `status` 字段（`"success"` 或 `"error"`）。

### 5.1 页面

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 渲染 Web UI (`templates/index.html`) |
| GET | `/articles/<path>` | 静态文件服务（Markdown / HTML / 图片） |

### 5.2 任务 API

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/api/tasks` | 无 | 列出队列中的任务（不含 success） |
| GET | `/api/tasks/<task_id>` | 无 | 查询单个任务状态 |
| POST | `/api/tasks` | passwd | 添加下载任务 |
| POST | `/api/tasks/<task_id>/retry` | passwd | 重试单个失败/超时任务 |
| POST | `/api/tasks/retry_all` | passwd | 一键重试所有失败/超时任务 |
| POST | `/api/lookup` | 无 | 根据 URL 查询下载状态及内容 |

#### POST `/api/tasks` 请求体

```json
{ "url": "https://mp.weixin.qq.com/s/...", "passwd": "wiz" }
```

返回中包含 `action` 字段：`created` / `exists` / `retry` / `already_queued`，以及对应的中文 `message`。

#### POST `/api/lookup` 请求体

```json
{ "url": "https://mp.weixin.qq.com/s/..." }
```

返回：
- `found: false`：URL 未提交过
- `found: true`：返回完整 meta 信息；若 `status=success`，额外返回 `markdown`（完整内容）、`browse_url`（完整可访问 URL）、`browse_html_url`

### 5.3 文章列表 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/articles` | 列出所有已下载文章（status=success），含标题、作者、时间、大小、图片数 |

---

## 6. MCP Server (`mcp_server.py`)

MCP stdio 协议服务，与 Flask 共享 config 和 task_queue 实例。

### 6.1 工具列表

| 工具名 | 参数 | 说明 |
|--------|------|------|
| `add_download_task` | `url`, `passwd` | 添加下载任务 |
| `list_tasks` | 无 | 列出队列中的任务 |
| `get_task` | `task_id` | 查询单个任务 |
| `list_articles` | 无 | 列出已下载文章 |
| `read_article` | `article_id` | 读取文章 Markdown 内容 |
| `retry_task` | `task_id`, `passwd` | 重试单个失败/超时任务 |
| `retry_all` | `passwd` | 一键重试所有失败/超时任务 |
| `lookup_url` | `url` | 根据 URL 查询下载状态，成功则返回完整 Markdown + 可浏览 URL |

### 6.2 运行方式

- **集成模式**：由 `app.py` 调用 `init(config, task_queue)` + `start_in_thread()`，在 daemon 线程中运行
- **独立模式**：直接 `python mcp_server.py`，自行加载 config 和创建 task_queue

---

## 7. Web 前端 (`templates/index.html`)

单页应用，三个 Tab：

### 7.1 添加 Tab
- 密码输入框（自动保存到 cookie，30 天有效）
- URL 输入框（textarea，支持粘贴）
- 提交按钮，显示操作结果提示（成功/已存在/失败）

### 7.2 队列 Tab
- 任务列表：显示 ID、URL、状态 badge、标题、错误信息、时间
- 已完成任务**不在队列中显示**
- 状态 badge：等待中(黄) / 下载中(蓝) / 失败(红) / 超时(深红)
- 每个失败/超时任务显示独立的"重试"按钮（调用 `POST /api/tasks/<id>/retry`）
- 顶部"重试所有失败/超时任务"批量按钮（仅在有可重试任务时显示）
- 密码读取优先级：输入框 > cookie 缓存（无需切换到添加 Tab 输入密码）
- 自动刷新（可配置间隔，仅在队列 Tab 激活时刷新）

### 7.3 已下载 Tab
- 文章列表：标题、作者、时间、大小、图片数
- 点击打开全屏 Modal 查看 Markdown 渲染内容
- Markdown 中的相对图片路径自动补全为 `/articles/<id>/` 前缀
- ESC 关闭 Modal

### 7.4 UI 特性
- 响应式设计，移动端友好
- 暗黑模式支持（`prefers-color-scheme: dark`）
- iOS safe area 适配
- 减少动画支持（`prefers-reduced-motion`）

---

## 8. 核心抓取逻辑 (`wechat_article_to_markdown.py`)

### 8.1 URL 处理
- `normalize_wechat_url()`：清理转义字符、HTML 实体、补全 https、去除引号/尖括号
- `extract_article_id()`：从 `/s/<ID>` 提取唯一 ID；query 风格 URL 用 sha256[:16] 兜底

### 8.2 抓取流程 (`fetch_article`)

1. 启动 Camoufox 反检测浏览器
2. `page.goto()` 加载页面（60s 超时，最多 3 次重试，重试间隔 3s）
3. `wait_for_selector("#js_content")` 等待正文加载（15s 超时）
4. 额外等待 2s 确保 JS 执行完毕
5. 获取完整 HTML（`page.content()`）
6. BeautifulSoup 解析 HTML
7. 提取元数据（标题、作者、发布时间）
8. 处理正文 DOM：修复 `data-src` → `src`、提取代码块、移除噪声元素、收集图片 URL
9. 转换为 Markdown（markdownify）
10. 并发下载图片（5 并发）到 `images/` 目录
11. 替换 Markdown 中的远程图片链接为本地路径
12. 写入 `index.md`
13. **最后**生成精简 `index.html`（替换图片为本地路径、移除 base64/script/style）

### 8.3 错误处理
- 标题提取失败 → `raise RuntimeError("未能提取到文章标题，可能触发了验证码")`
- 正文提取失败 → `raise RuntimeError("未能提取到正文内容")`
- 使用 `raise RuntimeError` 而非 `sys.exit(1)`，避免杀死 web worker 进程

---

## 9. 依赖

```
flask
flask-cors
pyyaml
mcp
camoufox[geoip]
markdownify
beautifulsoup4
httpx
```

Python 版本要求：`>= 3.10`（mcp 依赖要求）

---

## 10. 安全

- `config.yml` 在 `.gitignore` 中，防止密码泄漏
- 所有写操作（添加任务、重试）需要密码认证
- 查询接口（`/api/lookup`、`/api/tasks` GET、`/api/articles`）无需密码
- CORS 已开启（`flask-cors`）
