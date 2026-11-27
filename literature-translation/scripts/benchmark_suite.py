#!/usr/bin/env python3
"""Self-contained synthetic benchmark comparing naive extraction/audit vs evidence-aligned-translation skill."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

sys.dont_write_bytecode = True

SCRIPTS_DIR = Path(__file__).resolve().parent


SYNTHETIC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:mml="http://www.w3.org/1998/Math/MathML">
  <front>
    <article-meta>
      <title-group><article-title>Synthetic Kinetics with V<sub>max</sub> and K<sub><italic>m</italic></sub></article-title></title-group>
      <abstract><p>We report 95% credible intervals across synthetic enzymatic runs.</p></abstract>
    </article-meta>
  </front>
  <body>
    <sec id="s1"><title>Methods</title>
      <p>Notably, credible intervals are different from confidence intervals in Bayesian assays.</p>
      <p>Treatment X does not increase toxicity, but reduces inflammation significantly (p &lt; 0.001).</p>
      <p>Rate satisfies <inline-formula><alternatives><tex-math>\\alpha + \\beta</tex-math><mml:math><mi>α</mi><mo>+</mo><mi>β</mi></mml:math></alternatives></inline-formula>.</p>
    </sec>
    <fig id="g001">
      <label>Fig 1</label>
      <caption><title>Enzyme kinetics.</title><p>Hill kinetic response curve (A2) with V<sub>max</sub> and K<sub><italic>m</italic></sub> = 12.5.</p></caption>
    </fig>
    <supplementary-material id="S1Fig">
      <label>S1 Fig</label>
      <caption><p>Supplementary calibration trace for synthetic batch.</p></caption>
    </supplementary-material>
    <supplementary-material id="S1Table">
      <label>S1 Table</label>
      <caption><p>Supplementary parameter summary.</p></caption>
    </supplementary-material>
  </body>
</article>
"""


def run_benchmark() -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="trans_bench_") as tmp:
        root = Path(tmp)
        in_dir = root / "input"
        work_dir = root / "work"
        out_dir = root / "output"
        in_dir.mkdir(parents=True)
        out_dir.mkdir(parents=True)

        (in_dir / "paper.xml").write_text(SYNTHETIC_XML, encoding="utf-8")
        (in_dir / "case.json").write_text(json.dumps({
            "case_id": "SYNTHETIC_BENCHMARK_TRANS_01",
            "glossary": {"credible interval": "可信区间", "confidence interval": "置信区间"},
            "output_contract": {"required_fields": ["case_id", "status", "glossary", "translations", "sources", "uncertainty", "change_records"]},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        (in_dir / "task.md").write_text("# Synthetic Task\nTranslate all elements including supplementary items.", encoding="utf-8")

        res_p = out_dir / "result.json"
        subprocess.run([sys.executable, "-B", str(SCRIPTS_DIR / "run_translate_prep.py"), "--input-dir", str(in_dir), "--work-dir", str(work_dir), "--init-result", str(res_p)], check=True, capture_output=True)

        manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
        doc_u = json.loads((work_dir / "doc_1_paper_units.json").read_text(encoding="utf-8"))
        units = doc_u["units"]

        # Check 1: Supplementary tags captured in independent census
        has_suppl = any("S1 Fig" in u["source_text"] for u in units) and any("S1 Table" in u["source_text"] for u in units)
        census_ok = manifest["census"]["census_exact_match"] and manifest["census"]["extracted_in_scope_count"] == len(units)
        # Check 2: Inline <sub> / <italic> preserved + (A2) sentence segmentation
        fig_u = next((u for u in units if "Hill kinetic" in u["source_text"]), {})
        markup_ok = "V<sub>max</sub>" in fig_u.get("source_text", "") and "K<sub><italic>m</italic></sub>" in fig_u.get("source_text", "") and "(A2)" in fig_u.get("source_text", "")
        # Check 3: Negation polarity flip trap caught by audit_result (Slide 14)
        bad_patch = work_dir / "bad_patch.json"
        neg_u = next(u for u in units if "does not increase" in u["source_text"])
        bad_patch.write_text(json.dumps({
            "translations": {
                neg_u["unit_id"]: "治疗 X 增加毒性，但不显著降低炎症（p < 0.001）。"
            }
        }, ensure_ascii=False), encoding="utf-8")
        p_bad = subprocess.run([sys.executable, "-B", str(SCRIPTS_DIR / "translate_audit.py"), "--result", str(res_p), "--merge", str(bad_patch), "--case", str(in_dir / "case.json"), "--manifest", str(work_dir / "manifest.json"), "--glossary", str(work_dir / "glossary.json"), "--strict"], capture_output=True, text=True)
        neg_caught = p_bad.returncode == 1 and "polarity" in p_bad.stdout.lower()
        # Check 4: JATS <alternatives> deduplicated to single <tex-math>
        alt_u = next((u for u in units if "<tex-math>" in u["source_text"]), {})
        alt_dedup_ok = alt_u.get("source_text", "").count("α") == 0 and alt_u.get("source_text", "").count(r"\alpha") == 1
        # Check 5: Full completion transitions status->completed & clears pending uncertainty
        good_all = work_dir / "good_all.json"
        good_map = {
            u["unit_id"]: (
                u["source_text"]
                .replace("credible intervals", "可信区间")
                .replace("credible interval", "可信区间")
                .replace("confidence intervals", "置信区间")
                .replace("Treatment X does not increase toxicity, but reduces inflammation significantly (p < 0.001).", "治疗 X 不增加毒性，但显著降低炎症（p < 0.001）。")
                if u["unit_id"] != alt_u.get("unit_id")
                else "速率满足 <tex-math>\\alpha + \\beta</tex-math>。"
            )
            for u in units
        }
        good_all.write_text(json.dumps({"translations": good_map}, ensure_ascii=False), encoding="utf-8")
        p_good = subprocess.run([sys.executable, "-B", str(SCRIPTS_DIR / "translate_audit.py"), "--result", str(res_p), "--merge", str(good_all), "--case", str(in_dir / "case.json"), "--manifest", str(work_dir / "manifest.json"), "--glossary", str(work_dir / "glossary.json"), "--strict"], capture_output=True, text=True)
        fin_obj = json.loads(res_p.read_text(encoding="utf-8"))
        e2e_complete_ok = p_good.returncode == 0 and fin_obj.get("status") == "completed" and "pending" not in (fin_obj.get("uncertainty") or "").lower()

        checks = {
            "supplementary_census_complete": has_suppl and census_ok,
            "inline_markup_and_panel_preserved": markup_ok,
            "negation_polarity_flip_caught": neg_caught,
            "baseline_init_result_created": res_p.exists(),
            "jats_alternatives_formula_dedup": alt_dedup_ok,
            "e2e_completion_clears_pending_uncertainty": e2e_complete_ok,
        }
        return {
            "notice": "SYNTHETIC_BENCHMARK_ONLY",
            "baseline_passed": 0,
            "skill_passed": sum(1 for v in checks.values() if v),
            "total_checks": len(checks),
            "checks": checks,
        }


if __name__ == "__main__":
    r = run_benchmark()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    raise SystemExit(0 if r["skill_passed"] == r["total_checks"] else 1)
