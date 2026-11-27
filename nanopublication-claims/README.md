# Nanopublication Atomic Claims & Provenance (`nanopublication-claims`)

围绕 **阅读14｜Nanopublication（`product-arena-09-0e8befd4`）** 开发的科研论文原子断言拆解、最小证据绑定、PubInfo 溯源与机器可读导出 Skill，重点攻克基础 Agent 在 Nanopublication 提取中的四大核心痛点与 Harbor 弱模型预算陷阱：

1. **拆得细（Atomic Assertions & Controlled Predicates）**：将复合长句与多指标结果拆分为独立可判别真假的 `Subject — Predicate — Object` 原子断言，保留 `conditions`、`statistics`、`polarity`、`study_design`，并将关系映射到 `references/predicate-vocab.json` 受控词表（规避 `relates_to` 等模糊谓词）。
2. **对得上（Minimal Sufficient Evidence & Exact Cell Binding）**：每条断言仅绑定最短充分连续原文片段（`8–400` 字符）及 `(doc_id, page, printed_page, block_id, bbox, quote_sha256)`；引用表格数值时强制绑定 `(table_id, row_label, col_header, value)` 精确行列交叉单元格，彻底防止同表邻行/邻列串值（如把 Ocean 数值错挂给 Earth）。
3. **追得回（Locked PubInfo, Causal Guard & Confidence Calibration）**：
   - `prep_nanopub.py` 从 PDF/JATS XML 确定性抽取并锁定 `pubinfo`（`title`、`authors` 严格过滤编辑/审稿人、`year`、`journal`、`doi`、`pmid`、`license`、`file_sha256`），防止弱模型后续覆写出错。
   - 内置统计与因果越界拦截（禁止将 `p = 0.33` / 无显著差异写成「相等」，禁止将观察性研究标为 `causes`）及置信度与方法/OCR 质量强校准（禁止对 `extrapolation` / `indirect_inference` 无依据标 `high`）。
   - 自动检测 PDF 文本层统计符号损坏（如 `p ,0.03`）并触发局部高 DPI 渲染与 OCR/人工复核标记。
4. **读得懂 & 省预算（Compact Index + Validated TriG / JSON-LD Export）**：
   - 严格控制工具返回（`prep_nanopub.py` / `query_nanopub.py` 输出 `<= 16–18 KB`，绝不默认展开全表单元格），并在第一轮即写入锁定的基线 `result.json`。
   - 一键导出经语法校验的 W3C RDF **TriG**（`nanopublications.trig`，含 `Head` / `assertion` / `provenance` / `pubinfo` 四图）、**JSON-LD**（`nanopublications.jsonld`）与核验报告（`nanopubs_report.md`）。

---

## 目录结构

```text
.
├── SKILL.md                              # Agent 触发规范与 3 阶段低预算工作流
├── README.md                             # 项目说明、字段词典、运行方式与已知限制
├── GITHUB_URL.txt                        # 本作品 GitHub 公开仓库地址
├── scripts/
│   ├── doc_common.py                     # 能力探测、Symbol/连字修复、统计符号异常检测、受控词表加载
│   ├── pdf_layout.py                     # PDF 体检与版面重建（递归 X-Y 切分、三线表/续表、PubInfo 抽取）
│   ├── pdf_ocr.py                        # 扫描页/统计符号损坏区域高 DPI 渲染与 Tesseract/RapidOCR
│   ├── doc_ingest.py                     # 统一多格式摄取（PDF/JATS XML/MD/TXT/DOCX/XLSX/PPTX）与线索预提取
│   ├── prep_nanopub.py                   # 一键入口：摄取、定向 OCR、紧凑 nanopub_brief.json 与锁定基线 result.json
│   ├── query_nanopub.py                  # 低 Token 检索器：按 chunk/断言线索/统计量/表格行(--row)检索
│   ├── nanopub_export.py                 # W3C TriG / JSON-LD / Markdown 导出与 TriG 语法校验器
│   ├── nanopub_audit.py                  # 增量合并、URI/PubInfo 自动注入、5 维严格审计与自动导出
│   └── benchmark_suite.py                # 自包含合成对照基准（对比朴素基线 vs本 Skill 全套陷阱拦截）
├── references/
│   ├── predicate-vocab.json              # 受控关系词表（含因果禁忌研究设计与同义词映射）
│   ├── nanopub-schema.md                 # result.json、Head/Assertion/Provenance/PubInfo 字段字典
│   ├── decomposition-and-evidence.md     # 原子拆分、表格单元格绑定、因果边界与置信度校准规范
│   └── troubleshooting.md                # 加密/损坏 PDF、扫描件、p值字符损毁、超大表与冲突处置矩阵
└── examples/
    ├── minimal_case.json                 # 最小合成输入示例（非正式测试案例）
    ├── minimal_task.md                   # 最小合成任务说明示例
    ├── sample_nanopubs_20.json           # 20 条可复用合成 Nanopublication 标准样例集（含示意与正反边界）
    ├── sample_nanopubs.trig              # 对应 20 条样例导出并经校验的 W3C TriG 文件
    └── sample_benchmark_cases.json       # 可复用的多场景合成题集与审计陷阱测试用例
```

---

## 环境与依赖

- **必需运行时**：Python 3.9+（仅用标准库即可处理 JATS XML、Markdown/TXT、DOCX/XLSX/PPTX、受控词表审计、`pdftotext`/`pypdf` 回退与 TriG/JSON-LD 导出）。
- **推荐 PDF/OCR/RDF 链**（自动探测与降级）：
  - Python：`pymupdf`（`fitz`）、`pdfplumber`、`pypdf`、`pypdfium2`、`Pillow`、`rapidocr_onnxruntime`、`rdflib`
  - 系统 CLI：`pdftotext`、`pdftoppm`、`tesseract`

---

## 快速使用

```bash
# 1. 准备输入、锁定 PubInfo 并生成基线 result.json
python3 scripts/prep_nanopub.py --input-dir /input --work-dir /app/work --init-result /app/output/result.json

# 2. 按章节分块、统计/设计线索与指定表格行检索
python3 scripts/query_nanopub.py --work-dir /app/work --chunk chunk_001
python3 scripts/query_nanopub.py --work-dir /app/work --claim-cues --stats --methods-design
python3 scripts/query_nanopub.py --work-dir /app/work --table tbl_1 --row "Total"

# 3. 合并 patch、执行 5 维严格审计并自动导出 TriG / JSON-LD / Markdown
python3 scripts/nanopub_audit.py --result /app/output/result.json --merge /app/work/patch.json --case /input/case.json --work-dir /app/work --strict

# 4. 运行自包含对照基准测试
python3 scripts/benchmark_suite.py
```

---

## 适用范围与已知限制

- **适用范围**：单篇或多篇中英文科研论文（PDF、JATS/PLOS XML、Markdown/TXT、DOCX/XLSX/PPTX），支持双栏/多栏版式、三线表/跨页续表、统计结果与跨章节/跨论文断言去重与冲突检测。
- **已知限制**：
  1. 若宿主环境缺少 `pymupdf`、`pdftotext` 与 `pypdf`，仅能完成 PDF 文件头校验而无法抽取文本版面。
  2. 纯扫描版或 `p ,0.03` 字符损毁页面在缺少 OCR 引擎（`tesseract`/`rapidocr`）时，脚本会生成截图并诚实标注 `human_review_required`，不猜测符号方向。
  3. 正则预抽取的断言/统计线索仅作低预算导航辅助，最终主体-关系-客体语义归并仍以原文锚点与 `nanopub_audit.py --strict` 核验为准。
