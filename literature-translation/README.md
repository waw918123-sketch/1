# Evidence-Aligned Literature Translation (`evidence-aligned-translation`)

面向科研论文与出版商 XML/PDF 文献的高保真翻译与双语对齐 Skill，聚焦科研文献翻译中最核心的四大工程难点：**术语稳、段落全、关系准、可核验**。

## 核心设计

1. **双源解析与独立原生普查（`scripts/xml_pdf_extract.py`）**
   - **出版商 XML（JATS / PLOS / NLM）**：完整遍历 `article-title`、`abstract`、`body`、`back`、`fig`、`table-wrap`、`supplementary-material`、`app-group`、`fn` 等节点，生成精确 XPath 定位（如 `fig[1]/caption[1]/p[1]`）及句级编号（如 `P00001.S001`）；原样序列化并保护 `<sub>`、`<sup>`、`<italic>`、`<xref>`、`<tex-math>` 等内联标记并自动对 `<alternatives>` 公式去重；支持显式作用域过滤（如只译指定图 `g001`/`g002`），同时保留全文上下文供术语消歧。
   - **独立原生计数核对（Independent Census）**：单独统计符合范围的原生 XML/PDF 单元总数并与提取结果逐一比对，杜绝遗漏 `S1–S14 Fig`、`S1 Text`、`S1–S4 Table` 等补充材料却虚报 `100%` 覆盖率的问题。
   - **科研 PDF 降级支持**：内置 200-bin 递归 X-Y 切分与多栏行级同行重排、页眉页脚剥离、断词与中文换行修复、上标/下标 `^{}`/`_{}` 与 Symbol 字符恢复、`pdftotext` 兜底。

2. **术语抽取与强制锁定（`scripts/glossary_tools.py`）**
   - 自动从 `case.json`、`task.md`（含表格、列表与中英文自然语言指令）及 `.csv`/`.tsv`/`.json`/`.xlsx` 附件抽取给定术语表并标记为 `locked`；
   - 从全文正文提取高频候选术语与方法学辨析语句（如 `credible interval` ≠ `confidence interval`），避免仅翻译图注时脱离正文语境造成统计术语误译。

3. **按价值落盘与预算控制（`scripts/run_translate_prep.py`）**
   - `--init-result /app/output/result.json` 在分批前立即写入包含全部 `unit_id`/`locator`/`glossary`/`census` 的非空基线文件，避免超时交白卷；
   - 按 `--max-chars 9000`（单批至多 45 单元、单句不重复 `source_text`）切分轻量批次文件，`prep` 标准输出控制在 `<= 8,000` 字符，增量 `--merge` 控制在 `<= 1,500` 字符，`--strict` 终审截断上限为 `24,000` 字符。

4. **双语合并、否定作用范围与标记审计（`scripts/translate_audit.py`）**
   - `--merge` 逐批安全合并补丁，锁定原文章节编号与原文不被误改，并在全量译完后自动更新 `status` 与 `uncertainty`；
   - `--strict` 校验独立普查覆盖率、锁定术语执行率、`<sub>`/`<italic>`/`<tex-math>`/`^{}`/`_{}` 及半/全角面板标号 `(A2)`/`（A2）` 原样保留、数字一致性，以及双向（`EN<->ZH`）对比从句级**否定极性互换检测**（例如识别 `"does not increase A, but reduces B"` 误译为 `"增加 A，但不减少 B"`）。

## 目录结构

```text
submission/
├── SKILL.md
├── README.md
├── GITHUB_URL.txt
├── scripts/
│   ├── trans_common.py
│   ├── xml_pdf_extract.py
│   ├── glossary_tools.py
│   ├── run_translate_prep.py
│   ├── translate_audit.py
│   └── benchmark_suite.py
├── references/
│   ├── xml-and-pdf-locators.md
│   ├── glossary-and-fidelity.md
│   └── troubleshooting.md
└── examples/
    ├── minimal_case.json
    ├── minimal_task.md
    └── sample_result.json
```

## 依赖与快速验证

- 核心解析与审计支持纯 Python 3 标准库（`xml.etree.ElementTree`、`json`、`re`、`zipfile`、`csv`）即可处理出版商 XML；PDF 解析优先使用 `pymupdf`（`fitz`），缺失时自动降级 `pdftotext`。
- 运行合成对比基准（不包含任何正式赛题数据，仅用于自证可验证增益）：

```sh
python3 scripts/benchmark_suite.py
```

## 适用范围与已知限制

- **适用场景**：PLOS/JATS 等出版商 XML、指定图注/章节范围翻译、带术语表科研英中互译、含 `<sub>`/`<italic>`/化学式/面板号的双栏科研 PDF。
- **已知限制**：纯扫描图片型 PDF 在无可用 OCR 引擎时会保留原定位并写入 `human_review_items` 与 `uncertainty`，不会凭空补译。
