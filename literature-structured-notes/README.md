# Literature Summary & Structured Notes (`literature-structured-notes`)

围绕 **阅读10｜文献总结与结构化笔记** 设计的科研文献结构化阅读与笔记生成 Skill，重点解决基础 Agent 在真实论文总结中的四大痛点：

1. **对得上（章节对齐与论证承接）**：自动提取论文原生多级章节骨架（`section_id`、`order_index`、`parent_id`、`prev/next_section_id`），按章节原本顺序分块阅读并记录 `transition_from_prev` 承接关系，避免套用脱离原文结构的通用摘要模板。
2. **留得住（前提条件与局限保留）**：通过确定性正则预抽取缩写定义、前提/边界条件线索（温度、压力、样本量、假设、适用范围）和局限性线索，强制在论文卡片与结论知识点中保留 `preconditions` 与 `limitations`（未提及则显式标注 `unreported`）。
3. **找得回（细粒度可核验回链）**：每条章节笔记、论文卡片字段与知识点必须挂载 `(doc_id, page, printed_page, section, block_id / table_id+row_label+col_header, quote)` 锚点，并通过 `notes_audit.py --strict` 对引用页码、单元格数值及原文逐字片段做自动化核验。
4. **用得上（可检索、可比较、可复用）**：统一 `paper_cards` 多篇对比矩阵、`knowledge_points` 规范术语归并（`canonical_term` + `aliases` + 去重/冲突检测）、人机复核边界（`verification_mode`: `deterministic` vs `human_review_required`），并可一键导出 Obsidian/Zettelkasten 友好的结构化 Markdown 笔记（`--export-md`）。

---

## 目录结构

```text
.
├── SKILL.md                        # Agent 触发规范与 4 阶段标准执行流程
├── README.md                       # 使用说明、字段词典、依赖降级链与基准测试说明
├── GITHUB_URL.txt                  # 本作品 GitHub 公开仓库地址
├── scripts/
│   ├── doc_common.py               # 能力探测、符号/连字规范化、术语键规范化、契约字段收集
│   ├── pdf_layout.py               # PDF 体检与版面重建（递归 X-Y 切分、三线表/续表、公式编号、页码映射）
│   ├── pdf_ocr.py                  # 扫描页/局部区域高 DPI 渲染与 Tesseract/RapidOCR 置信度报告
│   ├── doc_ingest.py               # 统一多格式摄取（PDF/JATS XML/MD/TXT/DOCX/XLSX/PPTX）、章节分块与线索预提取
│   ├── prep_notes.py               # 一键入口：发现输入、摄取文档、定向 OCR、生成 notes_brief.json 与基线 result.json
│   ├── query_notes.py              # 低 Token 检索器：按章节/分块/表格/条件线索/局限线索/术语返回带锚点片段
│   ├── notes_audit.py              # 增量合并、术语去重与冲突检测、4 维严格审计与 Markdown 笔记导出
│   └── benchmark_suite.py          # 自包含合成基准测试（多栏 PDF + JATS XML 多篇对比 + 负例陷阱 + 损坏文件阻断）
├── references/
│   ├── note-schemas.md             # 完整字段字典（paper_cards / section_notes / knowledge_points / evidence_anchors）
│   ├── section-and-evidence.md     # 章节对齐、前提保留、锚点粒度与跨篇对比规范
│   └── troubleshooting.md          # 扫描件/加密文档/双栏错序/超长文/多语言/缺依赖处理矩阵
└── examples/
    ├── minimal_case.json           # 最小合成输入示例（非正式测试案例）
    ├── minimal_task.md             # 最小合成任务说明示例
    ├── sample_result.json          # 最小示意结构化输出示例
    └── sample_notes.md             # 由 notes_audit.py 导出的示意 Markdown 笔记
```

---

## 环境与依赖

- **必需运行时**：Python 3.9+（仅用标准库即可处理 JATS XML、Markdown/TXT、DOCX/XLSX/PPTX、契约审计与 `pdftotext`/`pypdf` 回退）。
- **推荐 PDF/OCR 链**（按可用性自动探测与降级）：
  - Python：`pymupdf`（`fitz`）、`pdfplumber`、`pypdf`、`pypdfium2`、`Pillow`、`rapidocr_onnxruntime`
  - 系统 CLI：`pdftotext`、`pdftoppm`、`tesseract`

---

## 快速使用

### 1. 一键准备与基线初始化
```bash
python3 scripts/prep_notes.py --input-dir /input --work-dir /app/work --init-result /app/output/result.json
```

### 2. 按章节分块与线索检索
```bash
python3 scripts/query_notes.py --work-dir /app/work --chunk chunk_001
python3 scripts/query_notes.py --work-dir /app/work --conditions --limitations --terms
```

### 3. 合并笔记、严格审计与导出 Markdown
```bash
python3 scripts/notes_audit.py \
  --result /app/output/result.json \
  --merge /app/work/patch.json \
  --case /input/case.json \
  --work-dir /app/work \
  --export-md /app/output/notes.md \
  --strict
```

### 4. 运行自包含合成基准自测
```bash
python3 scripts/benchmark_suite.py
```

---

## 适用范围与已知限制

- **适用范围**：单篇或多篇中英文科研论文（PDF、JATS/PLOS XML、Markdown/TXT、DOCX/XLSX/PPTX），支持双栏/多栏版式、三线表/跨页续表、长文章节分块及结构化问答/笔记生成。
- **已知限制**：
  1. 若运行环境缺少 `pymupdf`、`pdftotext` 与 `pypdf`，仅能完成 PDF 文件头校验而无法抽取版面文本。
  2. 纯扫描版 PDF 且无 `tesseract`/`rapidocr` 时，脚本会诚实标注 `human_review_required`，不猜测图像文字。
  3. 正则预抽取的缩写与条件/局限线索用于辅助防漏，最终语义归并与跨篇对比仍须以原文锚点复核为准。
