# EasyBench — Project State Document

> 用于跨会话交接。把此文件提供给新的 AI agent，它能快速了解全部上下文。

---

## 1. 项目目标

构建一个**单细胞组学 benchmark 自动化流水线**：输入一个 benchmark 类型（如 integration），自动完成：

1. 搜索含公开单细胞数据集的论文
2. 下载数据集
3. 复现论文实验
4. 用 benchmark 指标评价结果
5. 输出排名报告

---

## 2. 技术栈

- **语言**：Python 3.9+
- **LLM**：DeepSeek V4（通过 OmicsClaw 的 `autoagent.llm_client`）
- **框架基座**：[OmicsClaw](https://github.com/TianGzlab/OmicsClaw)（Apache 2.0）
- **仓库**：`https://github.com/hyqhanson/EasyBench.git`
- **本地路径**：`c:\Users\Yiqi Huang\Desktop\codex\OmicsClaw`

---

## 3. 凭证信息

### DeepSeek API Key（LLM 调用用）

```
sk-your-deepseek-api-key
```

已永久保存至 Windows 用户环境变量 `DEEPSEEK_API_KEY`。
新终端打开后会自动生效，验证：`echo $env:DEEPSEEK_API_KEY`

### GitHub Token（推送用）

```
ghp_your-github-token
```

推送命令（需要 Token）：
```bash
git remote -v
git push easybench main
git remote set-url easybench https://github.com/hyqhanson/EasyBench.git  # 用完清除
```

### Semantic Scholar API Key

```
s2k-your-semantic-scholar-key
```

环境变量：`SEMANTIC_SCHOLAR_API_KEY`
已保存至 Windows 用户环境变量。

### Springer Nature OA API Key

```
your-springer-nature-key
```

环境变量：`SPRINGER_NATURE_OA_API_KEY`
仅 OA API 可用（Meta API 返回 401）。

### Unpaywall 邮箱

```
your-email@fudan.edu.cn
```

硬编码于 `search.py` 的 `_UNPAYWALL_EMAIL` 常量中，用于 Unpaywall API 查询。

---

## 4. 流水线架构（7 个 Stage + 6 个 Agent + Processor）

```
benchmark-type (e.g. "integration")
  │
  ├── Stage 0:   benchmark_dispatch         [benchmark_dispatch.py]
  │              搜索 PubMed/arXiv/GitHub/Zenodo/Scholar → 提取含数据的论文
  │              自动解压代码(.zip/.tar.gz) → benchmark_code/{type}/
  │              产出: paper_metadata.json + data/ + benchmark_code/
  │
  ├── Stage 1:   agent_preflight (AgentScanner) [skills/agents/agent_preflight/]
  │              LLM 配型: protocol + code + data → execution_plan.json
  │              产出: execution_plan.json → output_dir/01_preflight/{slug}/
  │
  ├── Stage 2:   agent_curator (AgentCurator) [skills/agents/agent_curator/]
  │              三层流水线:
  │                1. AgentCurator.curate()      — LLM 格式检测 → curation_plan.json
  │                2. CurationExecutor.run()     — 确定性转换 → curated.h5ad
  │                3. CuratorValidator.validate()— 反幻觉验证
  │              产出: curated.h5ad + output_dir/02_curator/{slug}/curation_plan.json
  │
  ├── Stage 3:   processor (AgentProcessor)  [skills/processor/processor.py]  ← 🆕
  │              轻量 scanpy 预处理: QC → Normalize → HVG → PCA
  │              无 OmicsClaw skill 依赖，跳过 preflight gating
  │              自动降采样(>100K cells)、跳过极小/artifact数据集
  │              产出: output_dir/03_process_data/{slug}/{dataset_id}.processed.h5ad
  │
  ├── Stage 4:   reproduce (AgentReproduce)  [skills/agents/agent_reproduce/]
  │              多入口脚本(Rmd/R/py) + Stitch模式 + Monitor+Fix
  │              产出: reproduce_result.json (含 LLM 评分)
  │
  ├── Stage 5:   reproducibility_evaluation  [reproducibility_evaluation.py]
  │              评估复现成功率, 建议缺失指标
  │
  └── Stage 6:   benchmark_evaluation        [benchmark_evaluation.py]
                 autoagent evaluator 计算生物学指标 → 排名 → 报告
```

### Agent/Processor 定义

| # | 名称 | 阶段 | 文件 | 类型 |
|---|------|------|------|------|
| 1 | AgentScanner | Stage 1 | `scanner.py`, `runner.py` | Agent (LLM) |
| 2 | AgentCurator | Stage 2 | `curator.py`, `curator_runner.py` | Agent (LLM) |
| 3 | CurationExecutor | Stage 2 | `executor.py` | 确定性执行 |
| 4 | CuratorValidator | Stage 2 | `validator.py` | 确定性验证 |
| 5 | AgentMonitor | Stage 4 | `monitor.py` | 错误签名检测 |
| 6 | AgentFix | Stage 4 | `fix.py` | LLM诊断+自动修复 |
| 7 | AgentEvaluator | Stage 4 | `agent_evaluator.py` | LLM评分 |
| 8 | **Processor** | **Stage 3** | **`processor.py`** | **纯 scanpy** |

---

## 5. 已完成的代码工作

### 核心文件清单

| 文件 | 说明 | 状态 |
|------|------|------|
| `skills/literature/core/downloader.py` | 下载器（GEO/SRA/Zenodo/cellxgene + 代理绕过 + BioProject解析） | ✅ 重写 |
| `skills/literature/core/llm_collector.py` | LLM 文献收集器（DeepSeek 驱动） | ✅ 稳定 |
| `skills/literature/core/search.py` | PubMed API 封装 + 全文抓取 | ✅ 稳定 |
| `skills/orchestrator/benchmark_dispatch/benchmark_dispatch.py` | Stage 0：数据收集调度 + 代码自动解压 | ✅ 增强 |
| `skills/orchestrator/benchmark_suite/benchmark_suite.py` | 7 阶段管道编排器 + checkpoint 恢复 | ✅ 重写 |
| `skills/orchestrator/reproducibility_evaluation/reproducibility_evaluation.py` | Stage 5：可复现性评价 | ✅ 稳定 |
| `skills/orchestrator/benchmark_evaluation/benchmark_evaluation.py` | Stage 6：benchmark 评价 | ⚠️ 骨架 |
| **`skills/agents/agent_preflight/scanner.py`** | **AgentScanner — LLM protocol-code-data 配型** | ✅ **新增** |
| **`skills/agents/agent_preflight/runner.py`** | **AgentScanner 批处理 runner** | ✅ **新增** |
| **`skills/agents/agent_curator/curator.py`** | **AgentCurator — LLM 格式检测 + curation_plan** | ✅ **新增** |
| **`skills/agents/agent_curator/curator_runner.py`** | **AgentCurator 批处理 runner** | ✅ **新增** |
| **`skills/agents/agent_curator/executor.py`** | **CurationExecutor — 确定性 h5ad 转换** | ✅ **新增** |
| **`skills/agents/agent_curator/validator.py`** | **CuratorValidator — 反幻觉验证** | ✅ **新增** |
| **`skills/agents/agent_reproduce/runner.py`** | **AgentReproduce — 多入口脚本 + Stitch + Fix** | ✅ **新增** |
| **`skills/agents/agent_reproduce/monitor.py`** | **AgentMonitor — 错误签名检测** | ✅ **新增** |
| **`skills/agents/agent_reproduce/fix.py`** | **AgentFix — LLM诊断+自动安装** | ✅ **新增** |
| **`skills/processor/processor.py`** | **Processor — 轻量 scanpy 预处理 (Stage 3)** | ✅ **新增** |
| `omicsclaw.py` | `oc run benchmark-suite` 入口 | ✅ 已集成 |
| `_run_full_preflight.py` | 独立运行 Stage 1 AgentScanner | ✅ 新增 |
| `_run_full_curator.py` | 独立运行 Stage 2 AgentCurator | ✅ 新增 |
| `ATTRIBUTION.md` | 项目归属声明 | ✅ 已创建 |
| `README.md` | 项目文档 | ✅ 已重写 |

### 2026-06-15 新增功能

#### downloader.py 重写
1. **代理绕过**: `_get_session()`/`_get()` — 所有 HTTP 请求使用 `trust_env=False`，解决 Clash/V2Ray 拦截
2. **BioProject 解析**: `_is_bioproject_id()` + `_resolve_bioproject_to_sra_ids()` — PRJNA/PRJEB/PRJDB → SRA UIDs
3. **Zenodo 下载**: `download_from_zenodo()` — DOI / record URL / 纯 ID 三种格式
4. **GEO suppl 下载器重写**: 数据文件优先（`.mtx` > `.csv`）、总大小预算 `max_total_gb`、去 `[:10]` 限制、正确识别 `.mtx.gz`

#### benchmark_dispatch.py 增强
1. **Zenodo 集成**: `save_accepted_papers()` 现在下载 zenodo_data + zenodo_code
2. **数据自发现**: `rediscover_paper_data_if_needed()` — 初始下载无 sc-data 时，扫描 GitHub zip 的 README/DATA.md 找实际数据链接

#### benchmark_suite.py Stage 1 重写
1. **正确数据源**: 从 `benchmark_data/{type}/` 而非旧的 `dispatch_dir/downloaded_data/`
2. **多格式发现**: `_find_sc_data_files()` — `.h5ad` → 10X 目录 → `.mtx.gz` → `.h5` → `.rds` → `.csv` → `.tar`
3. **按论文粒度**: `_process_one_paper()` — 每篇论文独立输出目录
4. **两阶段流水线**: `sc-standardize-input` → `sc-preprocessing`（benchmark analysis 在 Stage 4）
5. **模块名映射**: `_SKILL_MODULE_MAP` — 28 个 skill 的目录名→.py 文件名映射（修复 `sc_preprocess` ≠ `sc_preprocessing`）

### LLM Collector 功能



### 已有的修复（2026-06-13 前）

- PubMed XML 解析 bug（`re.findall` 返回元组不能用 `str.join`）
- 外部搜索超时问题（15s 单请求 + 全局 180s 预算）
- 3 个 LLM prompt 从"找 benchmark"改为"找数据"
- `_benchmark_context` → `_analysis_context`
- `benchmark_evaluation` → `reproducibility_evaluation`（重命名防混淆）
- Stage 1/2 顺序交换（process_data 在 reproduce_paper 之前）
- **PubMed 两阶段搜索**: fft[Filter] 失败后自动回退到宽搜索
- **Unpaywall OA 集成**: 新增函数，找非 PMC 的 OA 全文
- **arXiv PDF 重试**: 15s → 25s 逐步超时，减少 deadline 占用
- **InsecureRequestWarning 抑制**: `urllib3.disable_warnings()`

---

## 6. 当前状态

### ✅ 可以正常工作

```bash
# 全新运行（从 Stage 0 开始到 Stage 6）
python -m skills.orchestrator.benchmark_suite.benchmark_suite --benchmark-type integration --output ./e2e_test --use-llm

# 只跑 Stage 0（搜索+下载），跳过后续所有
python -m skills.orchestrator.benchmark_suite.benchmark_suite --benchmark-type integration --output ./e2e_test --use-llm --no-process --no-evaluate

# 用 --resume 跳过已完成的 Stage 0，只跑 Stage 1+2
python -m skills.orchestrator.benchmark_suite.benchmark_suite --benchmark-type integration --output ./e2e_test --resume --use-llm --no-process --no-evaluate

# 只想重新跑 Stage 2（删除 checkpoint_02 后 resume）
Remove-Item e2e_test/.checkpoint_02 -Force
python -m skills.orchestrator.benchmark_suite.benchmark_suite --benchmark-type integration --output ./e2e_test --resume --use-llm

# 只想跑 Stage 3（预处理）
Remove-Item e2e_test/.checkpoint_03 -Force
python -m skills.orchestrator.benchmark_suite.benchmark_suite --benchmark-type integration --output ./e2e_test --resume
```


### ⚠️ 已知限制

1. **目前大部分fully accepted文章出自springer nature** 
2. **arXiv FULL_TEXT 富化率不高** — PDF 下载仍有成功率问题
3. **论文质量参差不齐** — 部分 DATA_ONLY 论文来自 microarray/bulk 技术
4. **SRA/Figshare 匹配为 0** — 正则可能不够全面
5. **LLM 排序的"推测"问题** — 常推测"likely has data"但实际未找到
7. **bulk RNA-seq 兼容** — 当论文的数据为 `.csv`/`.txt.gz` 格式，`sc-standardize-input` 可能无法正确处理
8. **Zendo 数据不完整** — multiomics 论文的 zenodo_data 链接只含协议 PDF，真实数据通过自发现从 GitHub 找到



---

## 7. 后续目标（按优先级）

### P0：修复 CurationExecutor 剩余 bug

- [ ] 消除 `same file` 冲突（Pattern 2 跳过 `curated.h5ad` 后仍有遗留）
- [ ] `_run_full_curator.py` 运行后自动清理 `.tmp` 目录
- [ ] bulk CSV/TXT 转置检测精度提升（`curated_but_mismatched` 太多）

### P1：CEL/BGX/RDS 转换

- [ ] 使用已安装的 R 环境执行 LLM 生成的 R 代码（oligo/limma）
- [ ] 安装 `pyreadr` 处理 RDS 文件
- [ ] 对 `.gz` 文件先解压再传递给转换器

### P2：AgentScanner 增强

- [ ] 利用 execution_plan 中的 `best_path` 信息自动选择后续处理路径
- [ ] bulk RNA → `bulkrna-de`，scRNA → `sc-preprocessing`
- [ ] 对论文自有代码（`analysis_main.R`）自动生成适配数据路径

### P3：Stage 2 对接 Stage 1 输出

- [ ] 让 reproduce_paper 接收 `paper_metadata.json` + Stage 1 产出的 `processed.h5ad`
- [ ] 当前 `run_stage_reproduce()` 从 `literature_results` 取 text，而非从 `benchmark_data/` 取 metadata

### P4：LLM 诊断节点

- [ ] Stage 1 处理失败时，把目录结构 + 错误信息发给 LLM，让它诊断并建议修复
- [ ] 类似 `rediscover_paper_data_if_needed()` 的模式

### P5：Stage 4 benchmark evaluation

- [ ] 在 Stage 1 产出的 `processed.h5ad` 上运行 benchmark skill（如 `sc-batch-integration`）
- [ ] 用 `omicsclaw/autoagent/evaluator.py` 计算 iLISI/ASW 等指标

### P6：bulk RNA-seq 兼容

- [ ] 检测 bulk 数据 → 跳过 `sc-preprocessing`，直接走 `bulkrna-de` 或 `bulkrna-qc`
- [ ] `.txt.gz` 格式先解压再读取

---

## 8. 目录结构速查

```
D:\HYQ\EasyBench\                       # 项目根目录 (EasyBench fork)
    omicsclaw.py                        # CLI 入口
    pyproject.toml                      # 项目配置
    _run_full_preflight.py              # 独立运行 AgentScanner (Stage 0.5)
    _run_full_curator.py                # 独立运行 AgentCurator (Stage 1)
    README.md
    ATTRIBUTION.md

    skills/
      literature/core/
        downloader.py                   # 数据下载（GEO/SRA/Zenodo/cellxgene）
        llm_collector.py                # LLM 文献收集器
        search.py                       # PubMed API + 全文抓取
        steps.py                        # 论文步骤提取
        extractor.py                    # 元数据提取

      agents/                           # ← 🆕 Agent 代码
        agent_preflight/                # Stage 0.5: AgentScanner
          scanner.py                    #   LLM protocol-code-data 配型
          runner.py                     #   批处理 runner
        agent_curator/                  # Stage 1: AgentCurator
          curator.py                    #   LLM 格式检测 → curation_plan.json
          curator_runner.py             #   批处理 runner
          executor.py                   #   确定性 h5ad 转换
          validator.py                  #   反幻觉验证

      orchestrator/
        benchmark_dispatch/             # Stage 0
        reproduce_paper/                # Stage 2
        benchmark_suite/                # 管道编排器
        reproducibility_evaluation/     # Stage 3
        benchmark_evaluation/           # Stage 4

      bulkrna/                          # bulk RNA-seq 工具
        bulkrna-de/                     #   差异表达 (DESeq2/ttest)
        bulkrna-qc/                     #   QC

    benchmark_data/
      integration_e2e_test/            
        {paper_slug}/
          paper_metadata.json
          execution_plan.json           # Stage 0.5 产出
          curation_plan.json            # Stage 1 产出
          data/                         # 原始下载数据
          unpacked_data/                # 解压后的数据
          curated.h5ad                  # Stage 1 产出 (部分论文)

    benchmark_code/
      integration_e2e_test/
        {paper_slug}/                   # 克隆/下载的论文代码

    docs/
      EasyBench_PROJECT_STATE.md        # 本文件
      next_stage_design.md
      literature_collection_changes.md
```

---

## 9. 常用命令

```bash
# 设置 API key
$env:DEEPSEEK_API_KEY="sk-4fffcb..."

# Stage 0.5: AgentScanner — LLM protocol-code-data 配型
python _run_full_preflight.py

# Stage 1: AgentCurator — 格式检测 + h5ad 转换
python _run_full_curator.py

# 快速测试（mock 模式，完整的端到端流程）
python -m skills.orchestrator.benchmark_suite.benchmark_suite \
  --benchmark-type integration --output ./quick_test \
  --use-llm --no-download --no-process \
  --no-reproduce-clone --no-reproduce-install --no-reproduce-run --no-evaluate

# 恢复中断的运行
python -m skills.orchestrator.benchmark_suite.benchmark_suite \
  --benchmark-type integration --output ./quick_test --resume

# 查看预飞行摘要
python _show_preflight.py

# 查看策划状态
python _check_curated_results.py

# 处理文件解压（递归）
python _test_unpack.py
```

# 语法检查
python -m py_compile skills/orchestrator/benchmark_suite/benchmark_suite.py
python -m py_compile skills/literature/core/downloader.py
python -m py_compile skills/orchestrator/benchmark_dispatch/benchmark_dispatch.py

# 推送到 EasyBench（粘贴 Token）
git remote set-url easybench https://TOKEN@github.com/hyqhanson/EasyBench.git
git push easybench main
git remote set-url easybench https://github.com/hyqhanson/EasyBench.git

# --- 代理配置（复旦图书馆代理，用于访问 paywalled 论文） ---

# 步骤 1：持久化学号到环境变量（只需执行一次）
[Environment]::SetEnvironmentVariable('FUDAN_PROXY_USER', '24110720041', 'User')

# 步骤 2：保存密码到 Windows 凭据管理器（只需执行一次，替换 <你的密码>）
& "C:\ProgramData\anaconda3\envs\easybench\python.exe" -c "import keyring; keyring.set_password('OmicsClaw.FudanProxy', '24110720041', '<你的密码>')"

# 步骤 3：验证配置
$env:FUDAN_PROXY_USER = [Environment]::GetEnvironmentVariable('FUDAN_PROXY_USER', 'User')
& "C:\ProgramData\anaconda3\envs\easybench\python.exe" scripts/check_proxy.py

# 备用方案：直接用环境变量（不推荐，密码明文）
$env:FUDAN_PROXY_USER = "24110720041"
$env:FUDAN_PROXY_PASSWORD = "<你的密码>"

# 自定义代理地址（默认 libproxy.fudan.edu.cn:8080）
$env:FUDAN_PROXY_HOST = "your-proxy-host:port"
```

---

## 10. 远程仓库

- **origin**：`https://github.com/TianGzlab/OmicsClaw.git`（OmicsClaw 官方仓库，只读）
- **easybench**：`https://github.com/hyqhanson/EasyBench.git`（自有仓库，可推送）

---
## 创建网站
每周更新最新数据/方法

## agent需要做到反馈结果与自我迭代