from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from skills.agents.agent_preflight.scanner import AgentScanner, _build_file_tree


class AgentScannerCodeSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paper_dir = self.root / "benchmark_data" / "run" / "paper"
        self.code_dir = self.root / "benchmark_code" / "run" / "paper"
        self.repo = self.code_dir / "paper-repository"
        self.paper_dir.mkdir(parents=True)
        self.repo.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_script(self, relative_path: str, content: str = "print('ok')\n") -> None:
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _make_multimodal_repo(self) -> None:
        for index in range(45):
            self._write_script(
                f"NanoDam_DamID_CHIPseq_RNAseq/NanoDam/script_{index:02d}.R"
            )
        self._write_script("ShinyApp/app.R", "shiny::runApp()\n")
        self._write_script("ShinyApp/filtering_steps.R")
        self._write_script(
            "scRNAseq/analyses/2_integrate_replicates/integrate_replicates.Rmd"
        )
        self._write_script(
            "scRNAseq/analyses/10_MELD/MELD_preprocessing.Rmd"
        )
        self._write_script(
            "scRNAseq/analyses/10_MELD/parameter_estimation_meld_tx22.py"
        )

    def test_large_module_cannot_hide_other_script_directories(self) -> None:
        self._make_multimodal_repo()
        scanner = AgentScanner(self.paper_dir, self.code_dir)

        summary = scanner._scan_code()
        repo = summary["repos"][0]
        prompt = scanner._build_prompt({}, {}, summary)

        selected_modules = {Path(path).parts[0] for path in repo["script_list"]}
        self.assertEqual(
            selected_modules,
            {"NanoDam_DamID_CHIPseq_RNAseq", "ShinyApp", "scRNAseq"},
        )
        self.assertIn(
            "scRNAseq/analyses/2_integrate_replicates/integrate_replicates.Rmd",
            prompt,
        )
        self.assertIn("scRNAseq/analyses/10_MELD/MELD_preprocessing.Rmd", prompt)
        self.assertIn("NanoDam_DamID_CHIPseq_RNAseq", prompt)
        self.assertIn("ShinyApp/app.R", prompt)
        self.assertNotIn("## Benchmark Objective", prompt)
        self.assertNotIn("For integration", prompt)
        self.assertNotIn("Treat Shiny apps", prompt)

    def test_sampling_is_structural_not_keyword_ranked(self) -> None:
        for index in range(80):
            self._write_script(f"aaa_dense/same_directory/job_{index:02d}.py")
        self._write_script("zzz_rare/arbitrary_name/only_script.R")
        self._write_script("middle/another_branch/task.sh")

        repo = AgentScanner(self.paper_dir, self.code_dir)._scan_code()["repos"][0]
        selected = repo["script_list"]
        directory_paths = {
            item["path"] for item in repo["script_directories"]
        }

        self.assertIn("zzz_rare/arbitrary_name/only_script.R", selected)
        self.assertIn("middle/another_branch/task.sh", selected)
        self.assertIn("aaa_dense/same_directory", directory_paths)
        self.assertIn("zzz_rare/arbitrary_name", directory_paths)
        self.assertIn("middle/another_branch", directory_paths)
        self.assertLessEqual(len(selected), 60)

    def test_broken_symlink_is_a_skipped_leaf(self) -> None:
        cache_link = self.repo / "snakemake" / ".snakemake"
        cache_link.parent.mkdir(parents=True)
        try:
            cache_link.symlink_to(
                self.root / "author-machine" / ".snakemake",
                target_is_directory=True,
            )
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        tree = _build_file_tree(self.repo)
        snakemake = next(
            child for child in tree["children"] if child["name"] == "snakemake"
        )
        link = next(
            child for child in snakemake["children"] if child["name"] == ".snakemake"
        )

        self.assertEqual(link["type"], "symlink")
        self.assertEqual(link["error"], "broken_symlink")
        self.assertEqual(link["children"], [])
        self.assertEqual(
            AgentScanner(self.paper_dir, self.code_dir)._scan_code()["repo_count"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
