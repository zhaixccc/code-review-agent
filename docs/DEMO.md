# 本地修改、推送与触发 code-review-agent 演示

> 本指南区分两种方式：**CLI 手动 dry-run**（不需要公网 webhook）和 **GitHub Webhook 自动触发**（需要 GitHub 能访问 webhook URL）。不要把 `.env`、token 或 webhook secret 加入 Git。

## 1. 先选演示方式

| 方式 | 用途 | 是否需要公网 URL | 是否写评论 |
|---|---|---:|---:|
| CLI `review` | 最快验证模型、影响面分析与报告 | 否 | 默认否；加 `--post` 才发布 |
| Webhook + Pull Request | 展示 push/PR 自动触发、服务日志、GitHub 行内与汇总评论 | 是（隧道或部署） | 是，服务处理后自动发布 |

现场演示建议先跑 CLI dry-run 确认配置和效果，再展示 webhook 自动流程。

## 2. 做一个可审查的 GitHub 改动

推荐使用**功能分支 + Pull Request**，不要为了演示把故意缺陷推到 `main`。

在 PowerShell：

```powershell
Set-Location D:\AI\智能体测评\code-review-agent

git switch main
git pull --ff-only origin main
git switch -c demo/review-agent
```

然后在 VS Code 中修改一个真实的源码文件，最好是小而可解释的改动，并同步更新/运行对应测试。不要只改 Markdown：审查模型的主要输入是源码 diff，文档改动通常难展示代码审查能力。

验证和上传：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
git status --short
git diff -- src tests

git add src\某个源码文件.py tests\对应测试.py
git commit -m "demo: describe the behavior change"
git push -u origin demo/review-agent
```

把 `某个源码文件.py`、`对应测试.py` 换成你实际编辑的路径；只 `git add` 这次的文件，**不要使用 `git add -A`**，避免把 `.env`、`.state`、简历或其他本地资料带上。

Push 后到 GitHub 创建 PR。PR 是较适合展示的方式：审查结果会显示在 PR 的行内评论与汇总评论中。

## 3. 方式 A：CLI 本地 dry-run（推荐先做）

取刚 push 的 commit SHA（GitHub commit 页面或 `git rev-parse HEAD`），运行：

```powershell
.\.venv\Scripts\python.exe -m code_review_agent review `
  --repo zhaixccc/code-review-agent `
  --sha <刚推送的完整 commit SHA> `
  --explain
```

- **不加 `--post` 默认 dry-run**：调用真实 GitHub/DeepSeek，终端打印报告，但不写 GitHub 评论。
- 如果审查 PR，可加 `--pr <PR编号>`；`--sha` 要使用该 PR 当前 head commit 的 SHA。
- 确认报告后才用 `--post` 发布。`--post` 会实际写 GitHub 评论，需要有目标仓库评论权限的 `GITHUB_TOKEN`。
- CLI 方式不要求 Webhook、Cloudflare Tunnel 或公开服务端口。

例如，对 PR 进行 dry-run：

```powershell
.\.venv\Scripts\python.exe -m code_review_agent review `
  --repo zhaixccc/code-review-agent `
  --sha <PR当前head commit SHA> `
  --pr <PR编号> `
  --explain
```

## 4. 方式 B：GitHub Webhook 自动触发 PR 审查

### 4.1 本地服务配置

`code-review-agent` 从项目根目录 `.env` 读取配置。至少需要已配置：

- `DEEPSEEK_API_KEY`
- `GITHUB_TOKEN`（读取仓库/PR，并发布 review 评论所需权限）
- `GITHUB_WEBHOOK_SECRET`（随机、至少 16 个字符；只放本机 `.env` 和 GitHub Webhook 的 Secret 字段）
- `HOST=127.0.0.1`
- `PORT=18765`（本例服务端口；若选其他端口，下面的命令和隧道 URL 要一致）
- `ALLOWED_REPOS=zhaixccc/code-review-agent`（推荐，限制服务只审指定仓库）
- `REVIEW_EVENTS=pull_request`（本例只用 PR，避免 Push 与 PR 对同一改动重复审查）

不要把 `.env` 内容贴到聊天、截图、Issue 或提交里。Webhook Secret 与 GitHub Token 不要复用。

启动服务：

```powershell
Set-Location D:\AI\智能体测评\code-review-agent
.\.venv\Scripts\python.exe -m code_review_agent serve
```

在另一个 PowerShell 窗口验证本地服务：

```powershell
Invoke-RestMethod http://127.0.0.1:18765/healthz
```

返回 `status: ok` 表示进程已就绪。服务需保持运行，终止该窗口/进程就会停止。

### 4.2 让 GitHub 访问本地服务

`127.0.0.1` 只能本机访问，GitHub.com 无法直接连接。演示时可启动一个临时 HTTPS 隧道，例如：

```powershell
cloudflared tunnel --url http://127.0.0.1:18765
```

保留隧道窗口，复制 Cloudflare 输出的临时 `https://...` 地址。隧道停止或重启后 URL 可能变化，需要同步更新 Webhook 的 Payload URL。也可改用 ngrok 或部署到有 HTTPS 的服务器。

> 临时隧道会把公网流量转发至本机服务。Webhook 仍会校验 HMAC，但演示结束后建议关闭隧道；不要把 URL/Secret 公开发布。

### 4.3 配置 GitHub Webhook

目标仓库 **Settings → Webhooks → Add webhook**：

- **Payload URL**：`https://<当前隧道域名>/webhook/github`
- **Content type**：`application/json`
- **Secret**：填写与本机 `GITHUB_WEBHOOK_SECRET` 完全一致的值
- **事件**：选择 **Pull requests**（PR opened、synchronize、reopened、ready_for_review 会处理）
- 保存后可以在 **Recent Deliveries** 查看投递状态

确保 GitHub Token 对这个仓库可读、可发布 PR review/comment；使用 fine-grained token 时按 README 权限表授予最小必要权限。GitHub Secret 与服务器 `.env` 不一致时通常会返回 401。

### 4.4 触发并观察

1. 使用第 2 节的分支推送源代码改动并创建 PR。
2. GitHub 发出 `pull_request` webhook；服务通过 HMAC、事件、仓库白名单与去重检查后返回 **202**，响应只表示任务已排队。
3. 在运行服务的终端看后台日志：目标仓库/commit、最终 verdict、是否发布、失败数；不要记录或分享 token。
4. 等待模型调用完成后，在 PR 的 **Conversation** 查看汇总评论，在 **Files changed** 查看行内评论。
5. 如果是草稿 PR，服务会跳过；先标记 Ready for review。重复投递同一个事件会去重。

## 5. 只用 Push 事件时的差别

项目默认 `REVIEW_EVENTS=push`。Push webhook 会审查 push 中符合条件的 commit，并把结果写成 **commit comment**；它不会生成 PR 行内评论。若要演示 PR 行内评论，推荐采用上面 `REVIEW_EVENTS=pull_request` 的方式。

如果同时设置 `REVIEW_EVENTS=push,pull_request` 并在 GitHub 同时勾选 Push 与 Pull requests，同一变更可能被两个不同事件审查；除非你明确需要，否则演示时不要同时开启。

## 6. 故障排查

| 现象 | 检查 |
|---|---|
| Webhook 返回 401 | GitHub Secret 与 `.env` 中 `GITHUB_WEBHOOK_SECRET` 是否完全一致；服务是否重启加载了新配置 |
| Webhook 返回 202，但显示 ignored | `REVIEW_EVENTS`、`ALLOWED_REPOS`、事件类型、PR 是否 draft、是否是重复投递 |
| Webhook Delivery 失败 / timeout | 服务是否运行；隧道是否在线；Payload URL 是否是当前临时域名 |
| 已排队但没有评论 | 看服务器日志；确认 DeepSeek 可用、GitHub Token 有评论权限；全文件模型调用都失败时系统会**不发布误导性的“无问题”评论** |
| 本机 8080 / 8000 不能监听 | 检查 Windows 端口保留/占用；设置一个可用 `PORT`，启动命令与隧道目标端口保持一致 |
| 评论指向旧行或重复 | 看行号校验与评论指纹去重日志；同一提交重复审查会 upsert，不会一直刷汇总评论 |

## 7. 演示前安全检查

- [ ] 当前分支是 demo 分支，不是 `main`
- [ ] 只提交本次源码和测试；`git status` 不含 `.env`、`.state`、个人文件
- [ ] 已运行 `pytest` 与 `git diff --check`
- [ ] 默认先 dry-run 检查报告；仅在确认后启用自动发布
- [ ] `ALLOWED_REPOS` 限定到演示仓库，GitHub Token 按最小权限设置
- [ ] 演示结束关闭临时隧道与本地服务
