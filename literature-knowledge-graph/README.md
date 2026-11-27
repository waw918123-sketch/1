# Literature Knowledge Graph (`literature-knowledge-graph`)

围绕 **阅读15｜文献知识图谱构建（`product-arena-10-760d1a0b`）** 开发的科研文献知识图谱构建、跨篇实体对齐与禁止合并守护、多格式语料身份解析及可核验图谱查询 Skill，直击文献图谱评测中的核心陷阱与 Harbor 低预算约束：

1. **抽得准（Verified Entities & Author vs. Cited Role）**：
   - 仅抽取论文中真实出现的科研对象（`researcher`、`material`、`method`、`task`、`metric`、`conclusion`、`dataset`、`condition`、`concept`），严格区分作者自述（`author_self_statement`）与他人转述（`cited_or_third_party`）。
2. **连得对（Controlled Relations, Conditions & Anti-Cooccurrence）**：
   - 使用受控关系词表（`references/kg-vocab.json`），强制绑定关系方向、适用条件（`conditions`）与统计值（`statistics`）；明令禁止把共现（`co_occurrence`）、转述或推测写成直接证据，禁止跨篇混合条件/样本/数值结论，禁止对观察性研究使用 `causes`。
3. **对得齐（Principled Alignment & Explicit Non-Merge Guard）**：
   - 支持别名/缩写归一与跨篇共享抽象概念对齐（`merged_candidates`），同时强制记录不得合并的对象（`non_merge_records`：不同物理样品、不同模型实例/数据集、不同实验条件、同名异义词、以及同篇多格式文件）。
   - **语料身份隔离与防泄漏**：自动识别 `corpus-manifest.json`、`material.json`、`primary-material.json`、`secondary-material.json` 与 `secondary-publisher.xml`，将同篇多格式变体归并到同一 `source_id`（绝不算作跨篇证据），且严格阻止 `case.json` 根级 DOI/title/authors 泄漏到第二篇论文。
4. **查得到、回得去（JATS `paragraph_id`, XML Math Cross-Check & Executed Queries）**：
   - 严格区分 PDF 页码（`has_pdf_pages: true`）与非 PDF 语料（`has_pdf_pages: false, page: null`），对非 PDF 语料强制绑定 `source_id` + 原生 JATS `paragraph_id` + 原文短引，绝不把自建页面 1 称作原 PDF 页。
   - 自动用 `secondary-publisher.xml` 核对线性化行内公式与上下标（`--formula-diffs`），对未提供图像像素、补充附件与底层实验/临床数据记录边界且绝不推算缺失数值。
   - 通过 `query_kg.py` 确定性执行 `>= 3` 条真实图谱查询，输出精确子图（`result_subgraph`）、原文证据（`supporting_evidence`）与无法确定关系（`indeterminate_relations`），并一键导出 `knowledge_graph.json`、`knowledge_graph.graphml`、`knowledge_graph.jsonld` 与 `kg_report.md`。

---

## 目录结构

```text
.
├── SKILL.md                                  # Agent 触发说明与 4 阶段低预算工作流
├── README.md                                 # 中文项目说明、架构、字段字典、运行指南与已知限制
├── GITHUB_URL.txt                            # 本作品 GitHub 公开仓库地址
├── scripts/
│   ├── doc_common.py                         # 能力探测、DOI/术语归一化、编码缺陷检测、词表与契约解析
│   ├── pdf_layout.py                         # 多栏 PDF 版面重建、三线表/续表、上下标与元数据抽取
│   ├── pdf_ocr.py                            # 扫描页与损毁符号区域定向渲染与 OCR
│   ├── corpus_ingest.py                      # 多格式语料身份解析、同篇多格式归并、XML 公式核对与线索提取
│   ├── prep_kg.py                            # Stage 1 入口：初始化语料/基线图谱并锁定 result.json
│   ├── query_kg.py                           # 双模检索：语料块/线索/公式差异检索 + 确定性图谱子图查询
│   ├── kg_export.py                          # GraphML / JSON-LD / KG JSON / Markdown 导出与 XML 校验
│   ├── kg_audit.py                           # 增量合并（保 ID 稳定性）、6 维严格审计与自动导出
│   └── benchmark_suite.py                    # 全场景自包含合成对照基准（涵盖双篇隔离/XML核对/增量更新/PDF表单元格）
├── references/
│   ├── kg-vocab.json                         # 受控实体与关系类型词表（含因果禁忌与禁用关系）
│   ├── kg-schema.md                          # result.json、nodes/edges/alignment/queries/incremental 字段规范
│   ├── alignment-and-evidence.md             # 实体对齐与禁止合并准则、自述vs转述、JATS 定位规范
│   └── troubleshooting.md                    # 异常、非PDF定位、线性化公式、缺失材料与零命中处置矩阵
└── examples/
    ├── minimal_case.json                     # 最小合成输入示例（非正式测试案例）
    ├── minimal_task.md                       # 最小合成任务说明示例
    ├── sample_kg_corpus/                     # 自包含合成双篇语料（manifest + primary/secondary JSON + XML）
    └── sample_result.json                    # 对应合成示例的输出示意（非正式测试答案）
```

---

## 环境与依赖

- **必需运行时**：Python 3.9+（仅用标准库即可完整运行 JSON/JATS XML/Markdown/TXT/Office 摄取、XML 公式差异核对、图谱查询、6 维严格审计、增量更新与 GraphML/JSON-LD/Markdown 导出）。
- **可选增强链**（处理真实复杂 PDF 时自动探测并启用）：
  - Python：`pymupdf`（`fitz`）、`pdfplumber`、`pypdf`、`pypdfium2`、`Pillow`、`rapidocr_onnxruntime`
  - 系统 CLI：`pdftotext`、`pdftoppm`、`tesseract`

---

## 快速使用

```bash
# 1. 初始化语料、识别同篇多格式变体并锁定基线 result.json
python3 scripts/prep_kg.py --input-dir /input --work-dir /app/work --init-result /app/output/result.json

# 2. 按章节块、KG 线索、表格行列与 XML 线性化公式差异检索
python3 scripts/query_kg.py --work-dir /app/work --source-id primary --chunk chunk_001
python3 scripts/query_kg.py --work-dir /app/work --cues --formula-diffs
python3 scripts/query_kg.py --work-dir /app/work --table tbl_1 --row "Total"

# 3. 合并 patch、执行查询、6 维严格审计并导出 GraphML / JSON-LD / Markdown
python3 scripts/kg_audit.py --result /app/output/result.json --merge /app/work/patch.json --case /input/case.json --work-dir /app/work --strict

# 4. 交互式图谱查询验证
python3 scripts/query_kg.py --graph /app/output/knowledge_graph.json --kg-query-compare "DFT"
python3 scripts/query_kg.py --graph /app/output/knowledge_graph.json --kg-query-conflicts

# 5. 运行自包含合成对照基准测试
python3 scripts/benchmark_suite.py
```

---

## 适用范围与已知限制

- **适用范围**：单篇或多篇中英文科研文献（`corpus-manifest.json`、`material.json`、结构化论文 JSON、JATS/PLOS XML、PDF、Markdown/TXT、DOCX/XLSX/PPTX），支持跨篇实体对齐与不合并隔离、线性化公式与 XML 核对、既有图谱增量更新以及结构化子图查询。
- **已知限制**：
  1. 若宿主环境缺少 `pymupdf`、`pdftotext` 与 `pypdf`，对 PDF 文件仅能完成文件头/加密校验，无法解析文本版面；非 PDF 结构化语料则无需上述库即可全功能运行。
  2. 图像像素、未附带补充附件与底层临床/实验原始数据不在可推断范围内，Skill 会明确记录 `unavailable_materials` 与 `uncertainty`，绝不猜测缺失数值。
  3. 仅依赖正则的预抽取线索（`kg_cues.json`）仅作为低预算导航辅助，最终实体/关系语义归并、对齐与禁止合并判断以原文片段及 `kg_audit.py --strict` 核验为准。
