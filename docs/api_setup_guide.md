# EasyBench API 配置指南

> 本文档帮助新用户配置运行 EasyBench 所需的所有 API 密钥和外部服务。
> 最后更新：2026-06-17

---

## 目录

1. [DeepSeek API（LLM 核心）](#1-deepseek-apillm-核心)
2. [GitHub Token（代码推送）](#2-github-token代码推送)
3. [Semantic Scholar API Key（文献搜索）](#3-semantic-scholar-api-key文献搜索)
4. [Springer Nature API Key（Nature 期刊搜索）](#4-springer-nature-api-keynature-期刊搜索)
5. [Unpaywall 邮箱（OA 全文获取）](#5-unpaywall-邮箱oa-全文获取)
6. [复旦图书馆代理（付费论文下载可选）](#6-复旦图书馆代理付费论文下载可选)
7. [快速验证](#7-快速验证)
8. [常见问题](#8-常见问题)

---

## 1. DeepSeek API（LLM 核心）

**用途**：驱动 LLM 文献收集器（`llm_collector.py`），包括生成搜索查询、提取论文元数据（GSE/SRA/Zenodo ID）、排序候选论文等。

**获取方式**：

1. 访问 [DeepSeek 开放平台](https://platform.deepseek.com/)
2. 注册账号 → 进入控制台 → API Keys
3. 创建一个新 API Key，复制保存（离开页面后不可再次查看）

**设置方法**：

```powershell
# 方法 A：临时设置（推荐，不影响系统）
$env:DEEPSEEK_API_KEY = "sk-你的key"

# 方法 B：持久化到 Windows 用户环境变量（永久生效）
[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', 'sk-你的key', 'User')
```

设置后验证：

```powershell
echo $env:DEEPSEEK_API_KEY
# 应输出: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> ⚠️ 每次打开新终端后，方法 A 需要重新设置；方法 B 需执行以下命令加载到当前会话：
> ```powershell
> $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
> ```

---

## 2. GitHub Token（代码推送）

**用途**：将本地修改推送到 `https://github.com/hyqhanson/EasyBench.git` 远程仓库。

**获取方式**：

1. 登录 [GitHub](https://github.com) → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. 点击 **Generate new token (classic)**
3. 设置有效期（建议 30-90 天，或选择 `No expiration`）
4. 勾选权限 `repo`（完整仓库控制）
5. 生成后复制保存

**设置方法**：

```powershell
# 配置远程仓库 URL（替换 YOUR_TOKEN）
git remote set-url easybench https://YOUR_TOKEN@github.com/hyqhanson/EasyBench.git

# 推送到远程
git push easybench main

# 推送完成后，建议清除 Token（避免 Token 泄露）
git remote set-url easybench https://github.com/hyqhanson/EasyBench.git
```

---

## 3. Semantic Scholar API Key（文献搜索）

**用途**：搜索 Semantic Scholar 文献数据库，获取论文元数据。相比 PubMed，Semantic Scholar 能提供更丰富的引用信息和论文分类。

**获取方式**：

1. 访问 [Semantic Scholar API 申请页](https://www.semanticscholar.org/product/api#api-key-form)
2. 填写邮箱和用途描述
3. 提交后 API Key 将发送到你的邮箱

**设置方法**：

```powershell
# 临时设置
$env:SEMANTIC_SCHOLAR_API_KEY = "s2k-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# 或持久化
[Environment]::SetEnvironmentVariable('SEMANTIC_SCHOLAR_API_KEY', 's2k-xxxxxxxx...', 'User')
```

> 💡 Semantic Scholar 有免费 API 额度（无 API Key 时约 1 req/s，有 Key 时约 10 req/s）。

---

## 4. Springer Nature API Key（Nature 期刊搜索）

**用途**：搜索 Nature、Nature Methods、Nature Communications、Scientific Reports 等 Springer Nature 旗下期刊的论文。

**获取方式**：

1. 访问 [Springer Nature API 控制台（dev.springernature.com）](https://dev.springernature.com/)
2. 注册账号（支持 GitHub 登录）
3. 在控制台申请两个 Key：
   - **Meta API Key**：搜索元数据（标题、摘要等）
   - **Open Access API Key**：获取开放获取全文

**设置方法**：

```powershell
# 两个 Key 都需要设置
[Environment]::SetEnvironmentVariable('SPRINGER_NATURE_API_KEY', '你的MetaAPIKey', 'User')
[Environment]::SetEnvironmentVariable('SPRINGER_NATURE_OA_API_KEY', '你的OAAPIKey', 'User')
```

> ⚠️ 注意事项：
> - **Meta API** 对新注册用户可能返回 HTTP 401（未授权），这是 Springer 的账户审核机制。如果遇到 401，可等待几天后重试，或只使用 OA API。
> - **OA API** 通常注册后即可使用，但只能搜索开放获取论文。
> - 代码中实现了自动回退：Meta API 失败时自动切换到 OA API。

---

## 5. Unpaywall 邮箱（OA 全文获取）

**用途**：通过 Unpaywall API 查找开放获取（OA）论文的全文 PDF 链接。用于获取非 PubMed Central 的 OA 论文全文。

**获取方式**：

无需注册或申请 Key，只需一个有效的邮箱地址（用于 API 调用时的身份标识和限流）。

**设置方法**：

硬编码在 `skills/literature/core/search.py` 的 `_UNPAYWALL_EMAIL` 常量中：

```python
_UNPAYWALL_EMAIL = 'your.email@example.com'
```

也可在调用时通过环境变量覆盖：

```python
import os
os.environ['UNPAYWALL_EMAIL'] = 'your.email@example.com'
```

> 💡 Unpaywall API 免费使用，无需 Key，但需要提供邮箱作为调用者的身份标识（仅用于限流）。

---

## 6. 复旦图书馆代理（付费论文下载 — 可选）

**用途**：通过复旦大学图书馆代理下载 Nature 等出版社的付费论文 PDF。**仅限复旦大学师生或有类似教育机构代理的用户使用。**

**设置方式（二选一）：**

### 方式 A：Credential Manager（推荐，密码加密存储）

```powershell
# 步骤 1：持久化学号到环境变量
[Environment]::SetEnvironmentVariable('FUDAN_PROXY_USER', '你的学号', 'User')

# 步骤 2：保存密码到 Windows 凭据管理器（只能手动输入）
# 打开 Windows 凭据管理器 → Windows 凭据 → 添加普通凭据
# 网络地址: OmicsClaw.FudanProxy
# 用户名: 你的学号
# 密码: 你的统一认证密码

# 或通过命令行设置（需手动输入密码）：
& "C:\ProgramData\anaconda3\envs\easybench\python.exe" -c "import keyring; keyring.set_password('OmicsClaw.FudanProxy', '你的学号', '你的密码')"

# 步骤 3：验证配置
$env:FUDAN_PROXY_USER = [Environment]::GetEnvironmentVariable('FUDAN_PROXY_USER', 'User')
python scripts/check_proxy.py
```

### 方式 B：环境变量（快速但不安全，密码明文）

```powershell
$env:FUDAN_PROXY_USER = "你的学号"
$env:FUDAN_PROXY_PASSWORD = "你的统一认证密码"
```

### 自定义代理地址

如果你的机构使用不同的代理服务器，可通过环境变量指定：

```powershell
$env:FUDAN_PROXY_HOST = "your-proxy-host:port"
```

> ⚠️ 代理用于解决出版社付费墙（paywall）问题。没有代理不影响 Stage 0 的文献搜索和数据下载，只会影响付费论文 PDF 的全文获取。

---

## 7. 快速验证

将所有 API Key 设置好后，执行以下命令验证：

```powershell
# 切换到 EasyBench 目录
cd D:\HYQ\EasyBench

# 设置 LLM API Key（如果还没设置）
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')

# 验证关键环境变量
echo "DEEPSEEK_API_KEY=$env:DEEPSEEK_API_KEY"
echo "SEMANTIC_SCHOLAR_API_KEY=$env:SEMANTIC_SCHOLAR_API_KEY"
echo "SPRINGER_NATURE_OA_API_KEY=$env:SPRINGER_NATURE_OA_API_KEY"
echo "SPRINGER_NATURE_API_KEY=$env:SPRINGER_NATURE_API_KEY"
echo "FUDAN_PROXY_USER=$env:FUDAN_PROXY_USER"

# 快速运行流水线（mock 模式，测试配置是否正常工作）
python -m skills.orchestrator.benchmark_suite.benchmark_suite `
  --benchmark-type integration --output ./quick_test `
  --no-download --no-process --no-reproduce-clone --no-reproduce-install --no-reproduce-run --no-evaluate
```

如果配置正确，你会看到 LLM 开始搜索并输出论文摘要。

---

## 8. 常见问题

### Q1：运行时提示 `xxx_API_KEY not set`

检查对应环境变量是否设置：

```powershell
echo $env:DEEPSEEK_API_KEY           # 是否为空？
echo $env:SEMANTIC_SCHOLAR_API_KEY   # 是否为空？
```

如果为空，使用前面章节的方法设置。

### Q2：Springer Nature Meta API 返回 401

这是 Springer 的账户审核机制。解决方案：

1. 只使用 OA API（设置 `SPRINGER_NATURE_OA_API_KEY`）
2. OA API 的关键字搜索可能有限，代码已实现自动回退到 OA API

### Q3：下载 GEO/Zenodo 数据失败

可能是网络问题导致的 SSL/代理错误。代码已内置代理绕过（`trust_env=False`），但如果你的网络环境特殊，可以尝试：

```powershell
# 检查能否访问 GEO
curl -I https://ftp.ncbi.nlm.nih.gov/geo/series/

# 检查能否访问 Zenodo
curl -I https://zenodo.org/api/records/
```

### Q4：运行流水线时 DeepSeek API 调用超时

DeepSeek API 有时响应较慢。代码已设置全局超时，如果频繁超时可尝试：

- 减少同时搜索的来源数量
- 检查网络连接
- 在 `search.py` 中适当增加 `_SEARCH_TIMEOUT` 值（默认 15 秒）

### Q5：是否需要所有 API Key 才能运行？

| API Key | 必要性 | 不设置的影响 |
|---------|--------|------------|
| **DeepSeek API** | ✅ 必须 | 流水线无法启用 LLM 驱动的文献收集 |
| **Semantic Scholar** | ⚠️ 推荐 | 跳过 Semantic Scholar 来源的搜索 |
| **Springer Nature** | ⚠️ 推荐 | 跳过 Nature 期刊论文搜索 |
| **GitHub Token** | ❌ 可选 | 无法推送代码，不影响流水线运行 |
| **Unpaywall** | ❌ 可选 | 无法获取非 PMC 的 OA 全文 |
| **复旦代理** | ❌ 可选 | 无法下载付费 PDF，不影响数据下载 |

---

## 附录：一键设置脚本

以下是完整的设置脚本（Windows PowerShell），将**DeepSeek API Key + Semantic Scholar + Springer Nature** 一次性持久化：

```powershell
# ====================================
# EasyBench API 一键设置
# ====================================

Write-Host "=== EasyBench API 一键设置 ===" -ForegroundColor Cyan

# 1. DeepSeek API Key
$dsKey = Read-Host "请输入 DeepSeek API Key (留空跳过)"
if ($dsKey) {
    [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', $dsKey, 'User')
    Write-Host "  ✅ DEEPSEEK_API_KEY 已设置" -ForegroundColor Green
}

# 2. Semantic Scholar API Key
$ssKey = Read-Host "请输入 Semantic Scholar API Key (留空跳过)"
if ($ssKey) {
    [Environment]::SetEnvironmentVariable('SEMANTIC_SCHOLAR_API_KEY', $ssKey, 'User')
    Write-Host "  ✅ SEMANTIC_SCHOLAR_API_KEY 已设置" -ForegroundColor Green
}

# 3. Springer Nature OA API Key
$snKey = Read-Host "请输入 Springer Nature OA API Key (留空跳过)"
if ($snKey) {
    [Environment]::SetEnvironmentVariable('SPRINGER_NATURE_OA_API_KEY', $snKey, 'User')
    Write-Host "  ✅ SPRINGER_NATURE_OA_API_KEY 已设置" -ForegroundColor Green
}

# 4. Springer Nature Meta API Key
$snMetaKey = Read-Host "请输入 Springer Nature Meta API Key (留空跳过)"
if ($snMetaKey) {
    [Environment]::SetEnvironmentVariable('SPRINGER_NATURE_API_KEY', $snMetaKey, 'User')
    Write-Host "  ✅ SPRINGER_NATURE_API_KEY 已设置" -ForegroundColor Green
}

Write-Host "`n=== 设置完成！===" -ForegroundColor Cyan
Write-Host "新 PowerShell 窗口需要运行以下命令加载：" -ForegroundColor Yellow
Write-Host '  $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")' -ForegroundColor Gray
```
