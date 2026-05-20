# EasyBench

> **Automated benchmark pipeline for single-cell omics** — from literature discovery to multi-metric evaluation.

EasyBench builds on [OmicsClaw](https://github.com/TianGzlab/OmicsClaw)'s
multi-omics analysis skills and adds a fully automated benchmark pipeline
that takes a **benchmark type** as input and produces a **ranked evaluation
report** as output.

---

## Core Concept

```
benchmark-type (e.g. "integration")
  │
  ├── Stage 0:  Literature & Dataset Collection
  │             PubMed / arXiv / GitHub / Zenodo → papers with public data
  │
  ├── Stage 1:  Paper Reproduction
  │             Clone repo → build env → run paper-specific commands → verify outputs
  │
  ├── Stage 2:  Data Processing
  │             Downloaded .h5ad → sc-preprocessing → sc-analysis-skill
  │
  ├── Stage 3:  Reproducibility Evaluation
  │             Did the reproduction succeed?  Which phase failed?  Score.
  │
  └── Stage 4:  Benchmark Evaluation
                Apply benchmark-specific metrics → rank datasets / methods → report
```

**Key insight**: we search for **single-cell datasets** (not benchmark papers).
The benchmark is built *from* the data.

---

## 5-Stage Pipeline

| Stage | Name | What it does | Key output |
|-------|------|--------------|------------|
| 0 | `benchmark_dispatch` | LLM-powered search across PubMed, arXiv, GitHub, Zenodo, Scholar. Extracts GEO / SRA / cellxgene accessions. | `literature/` with metadata, `llm_audit.json` for review |
| 1 | `reproduce_paper` | Discovers code repos from paper text (LLM + regex + metadata). Clones, installs deps, runs paper-specific commands, verifies output files. | `reproducibility/plan.json`, `result.json`, `report.md` |
| 2 | `process_data` | Auto-processes downloaded `.h5ad` through OmicsClaw sc tools (sc-preprocessing, sc-batch-integration, etc.) | `processed.h5ad` per dataset |
| 3 | `reproducibility_evaluation` | Scores clone / install / run / verify phases, suggests missing metrics. | `reproducibility_metrics.json` |
| 4 | `benchmark_evaluation` | Uses autoagent evaluator to compute biological metrics (iLISI, silhouette, etc.), generates rankings and composite scores. | `benchmark_metrics.json`, `benchmark_report.md` |

---

## Quick Start

```bash
# Full pipeline (LLM-powered, requires DeepSeek API key)
export DEEPSEEK_API_KEY="sk-..."
python -m skills.orchestrator.benchmark_suite.benchmark_suite \
  --benchmark-type integration \
  --output ./my_benchmark \
  --use-llm

# Resume an interrupted run
python -m skills.orchestrator.benchmark_suite.benchmark_suite \
  --benchmark-type integration \
  --output ./my_benchmark \
  --resume

# Skip heavy stages for quick iteration
python -m skills.orchestrator.benchmark_suite.benchmark_suite \
  --benchmark-type integration \
  --output ./quick_test \
  --use-llm \
  --no-download --no-process --no-reproduce-clone --no-reproduce-install --no-reproduce-run --no-evaluate
```

---

## Output Structure

```
output_dir/
  00_benchmark_dispatch/         # Collected literature + datasets
    literature/
      llm_results.json           # Final search results
      llm_audit.json             # LLM prompts & decisions (debuggable)
      llm_{pmid}/                # Per-paper metadata
  01_reproduce/reproducibility/  # Reproduction artifacts
    plan.json / result.json / report.md / commands.sh
  02_process_data/               # Processed .h5ad files
  03_reproducibility_evaluation/ # Reproducibility scores
  04_benchmark_evaluation/       # Benchmark metrics & rankings
  .checkpoint_00 ~ .checkpoint_04
  benchmark_suite_summary.json
  benchmark_suite_report.md
```

---

## Supported Benchmark Types

| Type | Analysis Focus | Metrics |
|------|---------------|---------|
| `integration` | Multi-dataset harmonization | iLISI, cLISI, batch ASW, cell-type ASW |
| `clustering` | Unsupervised cell grouping | silhouette, Calinski-Harabasz, n_clusters |
| `annotation` | Cell-type classification | n_cell_types, mean_confidence |
| `spatial` | Spatial domain detection | Moran's I, domain separation |
| `trajectory` | Pseudotime inference | trajectory correlation, Kendall's Tau |
| `batch_correction` | Batch effect removal | kBET, ASW batch, batch entropy |
| ... | (extensible via `metrics_catalog.json`) | |

---

## LLM Integration

EasyBench uses DeepSeek (via OmicsClaw's `autoagent.llm_client`) for:

1. **Query generation** — LLM generates targeted PubMed / arXiv queries to find papers with public datasets
2. **Paper details extraction** — LLM reads paper abstracts and extracts GEO/SRA IDs, species, tissue, methods
3. **Article ranking** — LLM ranks candidates by likelihood of containing reusable data

All LLM interactions are recorded in `llm_audit.json` for review and debugging.

---

## Attribution

EasyBench is based on [OmicsClaw](https://github.com/TianGzlab/OmicsClaw)
(Apache 2.0).  See [ATTRIBUTION.md](ATTRIBUTION.md) for details.

---

## License

Apache 2.0.  See [LICENSE](LICENSE).
    <a href="README.md"><b>English</b></a> ·
    <a href="README_zh-CN.md"><b>简体中文</b></a> ·
    <a href="#-why-omicsclaw"><b>Why</b></a> ·
    <a href="#-quick-start"><b>Quick Start</b></a> ·
    <a href="#-capabilities"><b>Capabilities</b></a> ·
    <a href="#-domains"><b>Domains</b></a> ·
    <a href="https://TianGzlab.github.io/OmicsClaw/"><b>Docs Site</b></a>
  </p>
</div>

# OmicsClaw

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![CI](https://img.shields.io/badge/CI-passing-brightgreen.svg)](https://github.com/TianGzlab/OmicsClaw/actions)
[![Website](https://img.shields.io/badge/Website-Live-brightgreen.svg)](https://TianGzlab.github.io/OmicsClaw/)

> [!NOTE]
> 🚀 **v0.1.1** ships the unified `oc` CLI, graph memory, app backend, remote execution, bot frontends, and **89 generated skills**.

OmicsClaw turns local multi-omics tools into AI-callable skills. The LLM plans and operates; Python/R/CLI tools process data in your local or remote runtime.

## 🖥️ App Workspace

<p align="center">
  <img src="docs/images/omicsclaw-app-overview.png" alt="OmicsClaw App showing connected backend, AutoAgent, datasets, skills, memory, remote bridge, and multi-omics analysis cards" width="94%"/>
</p>

<p align="center">
  <b>One workspace for chat, datasets, skills, execution, memory, and analysis outputs.</b>
</p>

## 💡 Why OmicsClaw?

| Common pain | OmicsClaw answer |
|---|---|
| Analyses restart from zero | Persistent workspace, sessions, and graph memory |
| Python, R, and CLI tools are scattered | Unified skill runner plus natural-language routing |
| Large data lives on servers | Local UI with remote Linux execution over SSH |
| Reports, artifacts, and parameters drift | Standard skill output contracts and reproducible demos |

## ✨ Capabilities

| | | | |
|---|---|---|---|
| 🧠 **Memory**<br/>Sessions, preferences, lineage | 🔒 **Local-first**<br/>Raw data stays in your runtime | 🧰 **89 skills**<br/>Generated catalog + demos | 🧭 **Smart routing**<br/>Natural language to tools |
| 🖥️ **CLI / TUI**<br/>`oc interactive`, `oc tui` | 🌐 **App backend**<br/>FastAPI for desktop/web | 🔌 **MCP-ready**<br/>Attach external tools | 📡 **Remote mode**<br/>SSH tunnel to Linux servers |

## ⚡ Quick Start

```bash
git clone https://github.com/TianGzlab/OmicsClaw.git
cd OmicsClaw
bash 0_setup_env.sh
conda activate OmicsClaw
oc list
oc run spatial-preprocess --demo --output /tmp/omicsclaw_demo
```

Configure chat and runtime settings:

```bash
oc onboard
oc interactive
```

If `oc` is not on `PATH`, use `python omicsclaw.py <command>`.

<p align="center">
  <img src="docs/images/OmicsClaw_configure_fast.png" alt="OmicsClaw setup wizard" width="82%"/>
</p>

## 🧭 Interfaces

| Surface | Entry point | Use it for |
|---|---|---|
| 🧪 Skill runner | `oc run <skill> --demo` | Reproducible analysis |
| 💬 Interactive CLI | `oc interactive` | Natural-language workflows |
| 🖥️ Full-screen TUI | `oc tui` | Terminal workspace sessions |
| 🌐 App backend | `oc app-server` | Desktop/web frontends |
| 📡 Remote server | `oc app-server` over SSH | Server-side data and jobs |
| 🤖 Bots | `python -m bot.run --channels ...` | Telegram, Feishu, and more |
| 🔌 MCP | `oc mcp add ...` | External tool integration |

Remote mode uses `127.0.0.1`, SSH tunneling, and `OMICSCLAW_REMOTE_AUTH_TOKEN`. See [remote execution](docs/engineering/remote-execution.mdx) and the [legacy remote guide](docs/_legacy/remote-connection-guide.md).

## 📦 Installation

| Path | Best for | Command |
|---|---|---|
| 🥇 **Full conda** | Real analysis with Python + R + bioinformatics CLIs | `bash 0_setup_env.sh` |
| 🪶 **Lightweight venv** | Chat, routing, dev, Python-only skills | `pip install -e ".[interactive]"` |
| 🖥️ **Desktop/web backend** | OmicsClaw-App or browser frontends | `oc app-server --host 127.0.0.1 --port 8765` |
| 🧠 **Memory API** | Inspect graph memory over HTTP | `pip install -e ".[memory]"` then `oc memory-server` |

📖 Details: [installation guide](docs/_legacy/INSTALLATION.md), [quickstart](docs/introduction/quickstart.mdx).

**Dependency sources:** Python in [pyproject.toml](pyproject.toml), conda/R/CLIs in [environment.yml](environment.yml), GitHub-only R packages in [0_setup_env.sh](0_setup_env.sh). No root `requirements.txt` is used as the primary entrypoint.

## 🧬 Domains

`skills/catalog.json` is generated by `scripts/generate_catalog.py` and currently lists **89 skills**.

| Domain | Examples | Docs |
|---|---|---|
| 🧫 Spatial transcriptomics | QC, domains, annotation, deconvolution, CNV, trajectory | [spatial](docs/domains/spatial.mdx) |
| 🔬 Single-cell omics | QC, clustering, annotation, doublets, velocity, GRN | [singlecell](docs/domains/singlecell.mdx) |
| 🧬 Genomics | QC, alignment, variants, CNV, assembly, epigenomics | [genomics](docs/domains/genomics.mdx) |
| 🧪 Proteomics | DIA/DDA, PTM, networks, biomarkers | [proteomics](docs/domains/proteomics.mdx) |
| ⚗️ Metabolomics | Peaks, normalization, annotation, pathways | [metabolomics](docs/domains/metabolomics.mdx) |
| 📈 Bulk RNA-seq | DE, enrichment, co-expression, deconvolution, survival | [bulkrna](docs/domains/bulkrna.mdx) |
| 🧠 Orchestration | Routing, planning, literature support | [orchestrator](docs/domains/orchestrator.mdx) |

Run `oc list` for the current CLI catalog.

## ❓ FAQ

<details>
<summary><b>Does OmicsClaw upload my raw data?</b></summary>

No. Skills run in the configured local or remote runtime; LLM calls should receive context and tool results, not raw omics matrices.

</details>

<details>
<summary><b>Which installation path should I use?</b></summary>

Use `bash 0_setup_env.sh` for real analysis. Use the lightweight venv only for chat, routing, development, or Python-only skills.

</details>

<details>
<summary><b>Can the desktop App run jobs on a server?</b></summary>

Yes. Run `oc app-server` on the remote Linux host, keep it bound to `127.0.0.1`, and connect through the App's SSH tunnel runtime.

</details>

<details>
<summary><b>🛠️ Developer Notes</b></summary>

Before complex repository work, read [README.md](README.md), [AGENTS.md](AGENTS.md), [SPEC.md](SPEC.md), and the relevant code/docs.

```bash
python -m pytest -v
make test
python scripts/generate_catalog.py
```

Use a brief plan, targeted tests, and verification evidence for non-trivial repository changes. New skills should follow [CONTRIBUTING.md](CONTRIBUTING.md) and [templates/SKILL-TEMPLATE.md](templates/SKILL-TEMPLATE.md).

Desktop provider changes should preserve the OmicsClaw-App backend contract: `/providers` reports the active provider/model/endpoint, `/providers/test` performs a short live LLM connectivity probe, and `/chat/stream` must reinitialize the provider runtime when a request changes model even if the provider id is unchanged.

Interactive CLI provider changes should share the same runtime resolution path: `LLM_PROVIDER=custom` must honor `LLM_BASE_URL`, `OMICSCLAW_MODEL`, and `LLM_API_KEY`; explicit CLI `--provider` / `--model` overrides must win over environment defaults; malformed custom endpoints should return actionable diagnostics instead of `(no response)`.

TUI helpers under `omicsclaw/interactive/_tui_support.py` stay dependency-light so support tests can run without optional memory or Textual installs. When adding Textual containers, mount the parent widget into the live tree before mounting child widgets.

</details>

## ⚠️ Safety

| Rule | Meaning |
|---|---|
| 🔒 Local-first | Raw data processing happens in your local or remote runtime |
| 🧪 Research use only | Not a medical device; no clinical diagnosis |
| 👩‍🔬 Expert review | Validate scientific outputs before decisions |
| 🔐 Remote caution | Use localhost binding, SSH tunnels, and tokens |

See [data privacy](docs/safety/data-privacy.mdx) and [rules/disclaimer](docs/safety/rules-and-disclaimer.mdx).

## 👥 Community

Maintainers: Luyi Tian (Principal Investigator), Weige Zhou (Lead Developer), Liying Chen (Developer), and Pengfei Yin (Developer).

🐛 [Issues](https://github.com/TianGzlab/OmicsClaw/issues) · 💬 [Discussions](https://github.com/TianGzlab/OmicsClaw/discussions) · 📖 [Docs](https://TianGzlab.github.io/OmicsClaw/)

## 🙏 Acknowledgments

Inspired by [ClawBio](https://github.com/ClawBio/ClawBio) and [Nocturne Memory](https://github.com/Dataojitori/nocturne_memory).

## 📜 License

Apache-2.0. See [LICENSE](LICENSE).

## 📝 Citation

```bibtex
@software{omicsclaw2026,
  title = {OmicsClaw: A Memory-Enabled AI Agent for Multi-Omics Analysis},
  author = {Zhou, Weige and Chen, Liying and Yin, Pengfei and Tian, Luyi},
  year = {2026},
  url = {https://github.com/TianGzlab/OmicsClaw}
}
```

[⬆ Back to top](#top)
