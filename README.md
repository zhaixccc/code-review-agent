# code-review-agent

基于 **LangGraph + DeepSeek API** 的 GitHub 自动代码审查 Agent：有新的 push（或 Pull Request）时，自动审查改动并把结果评论回 GitHub。

## 工作流程

```
GitHub webhook ──► 验证签名 / 过滤事件 ──► LangGraph
                                            │
 fetch_changes ─► triage ─► recall_memory ─┬─► review_file（每个文件并行，DeepSeek）─┐
                                           └──────────（无可审查文件）──────────────┤
                                                                                    ▼
                                                      synthesize ─► publish ─► learn
```

| 节点 | 作用 |
|---|---|
| `fetch_changes` | 通过 GitHub API 读取 commit / PR 的变更文件（分页取全，最多 `MAX_COMMIT_PAGES` 页）与 diff |
| `triage` | 跳过删除文件、锁文件、依赖/产物目录、二进制；**按风险排序**（认证、权限、SQL、支付、迁移等路径优先）并在总字符预算内挑选；敏感文件（`.env`、私钥等）不送模型，直接给出确定性告警 |
| `recall_memory` | （可选）从 Hindsight 召回该仓库的约定与历史反馈，作为不可信背景附加到提示词 |
| `review_file` | 用 LangGraph `Send` 对每个文件并行调用 DeepSeek；大 diff 分块；送模型前对疑似密钥打码，并对新增行做确定性密钥扫描；输出结构化 findings |
| `synthesize` | 合并、去重、排序；**结论由代码根据严重度确定**（不由模型决定）；再让模型写简短总结 |
| `publish` | push → 提交评论（commit comment，同一提交重复审查时更新而非新增）；PR → 行内评论按已有评论去重 + 一条汇总评论（更新而非新增） |
| `learn` | （可选）把本次审查沉淀到 Hindsight；`--post` 之外的试跑不写记忆 |

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

## 长期记忆（Hindsight，可选）

接入 [Hindsight](https://github.com/vectorize-io/hindsight) 后，Agent 对每个仓库（独立 memory bank：`cra-<owner>--<repo>`）记住并召回：

- 仓库约定：从**默认分支**读取 `MEMORY_CONVENTION_FILES`（如 `AGENTS.md`、`CONTRIBUTING.md`）。
- 维护者反馈：审查评论上的 +1 / -1 反应，只采信仓库所有者、token 所有者和 `TRUSTED_FEEDBACK_USERS` 的反应。

用法：

```powershell
# 1. 启动 Hindsight（需要它自己的 LLM 配置；DeepSeek 等 OpenAI 兼容端点是否可用请先自行验证）
docker run -p 8888:8888 -p 9999:9999 -e HINDSIGHT_API_LLM_API_KEY=<key> ghcr.io/vectorize-io/hindsight:latest

# 2. 安装客户端并开启
.\.venv\Scripts\python.exe -m pip install -e ".[memory]"
# .env 中设置 HINDSIGHT_URL=http://localhost:8888

# 3. 预热记忆，并查看某次审查会拿到什么背景
.\.venv\Scripts\python.exe -m code_review_agent learn --repo owner/name
.\.venv\Scripts\python.exe -m code_review_agent recall --repo owner/name --path src/auth.py
```

安全策略与限制：

- 记忆内容一律作为**不可信背景**放入提示词，不能改变输出格式或审查规则；召回结果先打码再使用。
- 只记忆可信来源（默认分支上的约定文件、可信用户的反馈），**不记忆**未合并 PR 的内容、diff 原文、第三方评论。
- 未设置 `HINDSIGHT_URL`、未安装客户端或服务不可用时自动降级为无记忆审查，不影响主流程。
- 反馈基于 reaction，对“一条评论包含多个问题”的提交报告粒度较粗；目前只使用 recall，未使用 reflect。

## 配置项

见 [.env.example](.env.example)。常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_MODEL` | `deepseek-chat` | 也可换成其他 DeepSeek 对话模型 |
| `REVIEW_EVENTS` | `push` | `push`、`pull_request` |
| `MAX_COMMITS_PER_PUSH` | `5` | 一次 push 只审查最近 N 个提交 |
| `MAX_FILES_PER_REVIEW` | `30` | 每次审查的文件上限 |
| `LLM_CONCURRENCY` | `4` | 并行调用模型的文件数 |
| `MAX_TOTAL_PATCH_CHARS` | `160000` | 单次审查送模型的 diff 总字符预算（按风险优先） |
| `MAX_COMMIT_PAGES` | `5` | 读取提交文件列表的最大页数（每页 100 个） |
| `HINDSIGHT_URL` | 空 | 为空则关闭长期记忆 |
| `STATE_DIR` | 空 | 去重与记忆记账文件位置（跨重启持久化） |
| `REVIEW_LANGUAGE` | `Simplified Chinese` | 评论语言 |

## 隐私与限制

- 被审查的 diff 会发送到 DeepSeek API，请确认私有代码可以交给该服务处理。
- 审查是辅助手段：模型可能漏报或误报，不能替代人工评审和测试。
- 评论内容不会包含 API 密钥或 GitHub token；日志里只记录错误类型，不记录代码或模型响应。
- 重复投递去重持久化在 `STATE_DIR`（单机文件）；任务队列在进程内。单进程部署足够，多实例部署需要换成共享队列/存储。
- 被判定为敏感的文件（`.env`、私钥、凭据文件）及 diff 中疑似密钥的内容不会发送给模型，也不会写入评论；扫描是启发式的，不能保证覆盖所有格式。
- 超出文件数/字符预算的低风险文件会被跳过，并在报告中列出，不会假装已审查。
