# Literature Document Parser Skill (`literature-doc-parser`)

围绕科研 PDF 文献文档解析（Reading 01）设计的 Agent Skill 与轻量解析校验工具集，聚焦**提得全、排得对、拆得清、核得回**四大核心目标：

1. **提得全**：逐字提取正文、图表题注、脚注与参考文献，剥离跨页重复页眉页脚，对乱码/缺失映射块明确标注而不靠猜补齐。
2. **排得对**：通过基于跨栏界标带切分（Spanning Band Splitting）与列间隙投影（Column Gutter X-Y Cut）的版面重建算法，修正双栏/多栏论文的串栏、错行与跨栏拼接错误，并谨慎还原行尾断词连字符。
3. **拆得清**：基于字号分布、粗斜体与编号规则拆分多级章节层级；结合 `pdfplumber`（线条驱动 + 三线表文本对齐策略）与 `PyMuPDF` 提取复杂/无竖线学术表格并链接跨页续表；保留上下标（`^{...}` / `_{...}`）与希腊字母/数学符号。
4. **核得回**：为每个段落块分配唯一锚点 `p{page}_b{idx}`（含 1-based 页码、分栏号、角色与 PDF 坐标 `bbox`），并提供 `audit_result.py` 自动核验最终 `result.json` 的 `output_contract` 字段完整性及引用原文片段与页码的真实一致性。

---

## 目录结构

```text
.
├── SKILL.md                        # Agent 触发指令与 5 阶段标准工作流
├── README.md                       # 完整说明文档（依赖、调用、限制、异常与自查）
├── scripts/
│   ├── pdf_common.py               # 工具探测、字符规范化、编码缺陷检测与几何辅助
│   ├── pdf_probe.py                # 环境能力探测 + PDF 加密/扫描/乱码/分栏体检
│   ├── pdf_extract.py              # 多引擎版面顺序提取、章节/表格/公式/脚注/参考文献结构化
│   ├── pdf_render_ocr.py           # 高 DPI 页面/区域裁剪渲染 + 图像预处理 + 可选 OCR
│   └── audit_result.py             # output_contract 合规校验与原文引用核验器
├── references/
│   ├── output-contract.md          # Harbor 运行契约与 result.json 结构规范
│   ├── layout-and-tables.md        # 多栏阅读顺序、三线表与跨页续表处理规范
│   ├── formulas-and-ocr.md         # 公式上下标、乱码字体检测与扫描件 OCR 策略
│   └── troubleshooting.md          # 加密/扫描/乱码/缺失依赖/超时异常处理矩阵
└── examples/
    ├── minimal_case.json           # 最小合成输入示例（非正式测试案例）
    ├── minimal_task.md             # 最小合成任务说明示例
    └── sample_result.json          # 最小示意输出示例（仅作接口格式参考）
```

---

## 环境与依赖说明

- **必需运行时**：Python 3.9+（`scripts/` 仅使用标准库即可完成能力探测、契约审计与 `pdftotext`/`pdftoppm` 回退）。
- **推荐科学 PDF 工具链**（按可用性自动探测与降级，不硬编码单一库）：
  - Python 库：`pymupdf`（`fitz`，主推荐：span 级坐标/字体/上下标/矢量绘图/表提取与高 DPI 渲染）、`pdfplumber`（增强三线表与合并单元格提取）、`pypdf`（文本与加密检测回退）、`Pillow`（OCR 灰度与锐化预处理）、`rapidocr_onnxruntime`（可选离线 OCR）。
  - 系统 CLI：`pdftotext`、`pdftoppm`、`pdfinfo`、`tesseract`（含 `eng`/`chi_sim` 语言包时自动启用）、`soffice`（Office 渲染预览）。

---

## 完整调用流程

### 1. PDF 体检与能力探测
```bash
python3 scripts/pdf_probe.py /input/paper.pdf --out /app/work/parse/probe.json
```
输出 `probe.json`，列出可用工具链、加密状态、每页文本字符数、图片覆盖率、估算栏数以及是否需要 OCR 的页码列表。

### 2. 版面顺序与结构化提取
```bash
python3 scripts/pdf_extract.py /input/paper.pdf --out-dir /app/work/parse
```
生成：
- `/app/work/parse/document.json`：完整结构化解析树（含 `sections`, `tables`, `captions`, `footnotes`, `equations`, `references`, `headers_footers`, `unextracted_regions`, `blocks`）。
- `/app/work/parse/reading_order.md`：按真实阅读顺序重排的 Markdown，每段均附 `[p{page}_b{idx}|col{col}|{role}|bbox=[...]]` 溯源标记。
- `/app/work/parse/tables.json` 与 `/app/work/parse/tables.md`：表格结构化数组、Markdown 与 HTML（含 `colspan` 与跨页续表链接）。

### 3. 扫描页 / 乱码页 / 复杂图表的渲染与 OCR
```bash
# 对指定页进行 250 DPI 渲染并自动调用可用 OCR 引擎
python3 scripts/pdf_render_ocr.py /input/paper.pdf --out-dir /app/work/ocr --pages 1,4-5 --dpi 250 --ocr auto

# 对 document.json 中标记的异常区域按 bbox 局部高分辨率裁剪
python3 scripts/pdf_render_ocr.py /input/paper.pdf --out-dir /app/work/crops --crop "3:60,180,540,420" --dpi 300 --ocr auto
```

### 4. 输出审计与自查
```bash
python3 scripts/audit_result.py \
  --result /app/output/result.json \
  --case /input/case.json \
  --doc /app/work/parse/document.json \
  --strict
```
核验 `case_id`、`output_contract` 必需字段、引用页码范围、`block_id` 归属以及 `quote` 原文片段是否真实存在于对应页文本中。

---

## 适用范围与已知限制

- **适用场景**：单栏/双栏/多栏中英文科研论文 PDF、含三线表/有线表/跨页续表的实验文献、含上下标与希腊字母的方法学文本、扫描版或图文混合 PDF。
- **已知限制**：
  1. 若运行环境既无 `fitz` 也无 `pdftotext`（或 `pypdf`），仅能完成基础文件头/加密检查而无法重建几何版面。
  2. 极复杂手写体或重度公式嵌套图片在不具备 OCR/视觉模型复核时，脚本会标注 `unextracted_regions` 而不自动猜测公式语义。
  3. 跨页续表的自动合并基于相邻页表头/列数与题注关键字启发式匹配，最终结论仍应结合原文截图与题注复核。

---

## 最小示例说明

`examples/` 目录提供了一组**最小合成示例**（`minimal_case.json`、`minimal_task.md`、`sample_result.json`），仅用于演示输入输出契约与证据引用格式，不代表或冒充任何正式评测案例及答案。
