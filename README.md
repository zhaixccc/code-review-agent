# code-review-agent

基于 **LangGraph + DeepSeek API** 的 GitHub 自动代码审查 Agent：有新的 push（或 Pull Request）时，自动审查改动并把结果评论回 GitHub。

## 工作流程

```
GitHub webhook ──► 验证签名 / 过滤事件 ──► LangGraph
                                            │
   fetch_changes ─► triage ─┬─► review_file（每个文件并行，DeepSeek）─┐
                            └──────────────（无可审查文件）──────────┤
                                                                     ▼
                                                  synthesize ─► publish
```

| 节点 | 作用 |
|---|---|
| `fetch_changes` | 通过 GitHub API 读取 commit / PR 的变更文件与 diff |
| `triage` | 跳过删除文件、锁文件、构建产物、二进制、超大 diff；限制单次审查文件数 |
| `review_file` | 用 LangGraph `Send` 对每个文件并行调用 DeepSeek；大 diff 分块；输出结构化 findings |
| `synthesize` | 合并、去重、排序；**结论由代码根据严重度确定**（不由模型决定）；再让模型写简短总结 |
| `publish` | push → 提交评论（commit comment）；PR → Review，问题行内评论，被拒绝时降级为完整正文 |

设计要点：

- **diff 带新文件行号**：模型只能把问题定位到真实存在的新增行；不存在的行号会被清除，不会发布错位评论。
- **提示注入防护**：commit message 和 diff 都视为不可信数据放入标签内；输出做净化（中和 `@提及`、远程图片、危险 HTML）。
- **失败不误导**：所有文件的模型调用都失败时不会发布“没有问题”的评论；部分失败会在评论里注明结果可能不完整。
- **成本控制**：文件数、单文件字符数、分块大小、每次 push 最多审查的提交数、并发数均可配置。
- **安全边界**：Webhook 强制 HMAC 签名校验；可用 `ALLOWED_REPOS` 白名单限制仓库；忽略机器人触发的事件、草稿 PR、合并提交、已删除分支；对重复投递去重。

## 快速开始

```powershell
cd code-review-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env      # 然后编辑 .env，填入密钥
.\.venv\Scripts\python.exe -m pytest      # 单元测试，无需密钥/网络
```

`.env` 已被 `.gitignore` 忽略；不要把任何密钥提交到仓库。

### 1. 本地试跑（默认只输出，不发布）

```powershell
.\.venv\Scripts\python.exe -m code_review_agent review --repo owner/name --sha <提交 SHA>
# 审查 PR：再加 --pr 12（--sha 为 PR 的 head 提交）
# 确认效果后才真正发布评论：加 --post
```

### 2. 接入 GitHub 自动审查

1. 在 `.env` 配置 `DEEPSEEK_API_KEY`、`GITHUB_TOKEN`、`GITHUB_WEBHOOK_SECRET`（建议同时设置 `ALLOWED_REPOS`）。
2. `GITHUB_TOKEN` 建议使用仅授权目标仓库的 fine-grained token：`Contents: Read and write`（提交评论）、`Pull requests: Read and write`（PR 审查）、`Metadata: Read`。
3. 启动服务：`.\.venv\Scripts\python.exe -m code_review_agent serve`（默认 `127.0.0.1:8080`）。
4. 让 GitHub 能访问该地址（本地开发可用 `ngrok`/`cloudflared` 等隧道；生产环境放在 HTTPS 反向代理之后）。
5. 在仓库 **Settings → Webhooks → Add webhook**：
   - Payload URL：`https://<你的域名>/webhook/github`
   - Content type：`application/json`
   - Secret：与 `GITHUB_WEBHOOK_SECRET` 相同
   - 事件：与 `REVIEW_EVENTS` 一致（默认 **Push**；若改为 `pull_request` 选 **Pull requests**）。同时开启两种会对同一改动重复审查。

## 配置项

见 [.env.example](.env.example)。常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_MODEL` | `deepseek-chat` | 也可换成其他 DeepSeek 对话模型 |
| `REVIEW_EVENTS` | `push` | `push`、`pull_request` |
| `MAX_COMMITS_PER_PUSH` | `5` | 一次 push 只审查最近 N 个提交 |
| `MAX_FILES_PER_REVIEW` | `30` | 每次审查的文件上限 |
| `LLM_CONCURRENCY` | `4` | 并行调用模型的文件数 |
| `REVIEW_LANGUAGE` | `Simplified Chinese` | 评论语言 |

## 隐私与限制

- 被审查的 diff 会发送到 DeepSeek API，请确认私有代码可以交给该服务处理。
- 审查是辅助手段：模型可能漏报或误报，不能替代人工评审和测试。
- 评论内容不会包含 API 密钥或 GitHub token；日志里只记录错误类型，不记录代码或模型响应。
- 当前去重缓存和任务队列在进程内；单进程部署足够，多实例部署需要换成共享队列/存储。
