---
name: pptx
description: "演示文稿创建、编辑和分析。当用户需要处理 PowerPoint 演示文稿（.pptx 文件）时使用，包括创建新演示文稿、修改内容、处理布局、添加批注或演讲者备注等。也适用于将 guizang-ppt HTML 转换为 PPTX。"
metadata:
  short-description: "PowerPoint 演示文稿创建、编辑与分析"
---

# PPTX 演示文稿处理指南

## 概述

本技能提供创建、编辑和分析 .pptx 文件的能力。核心依赖 `$NAVI_HOME/skills/pptx/scripts/` 下的 Python 和 JavaScript 脚本。以下用 `%NAVI_HOME%` 表示路径。

## 脚本路径速查

| 脚本 | 路径 |
|------|------|
| html2pptx.js | `%NAVI_HOME%/skills/pptx/scripts/html2pptx.js` |
| thumbnail.py | `%NAVI_HOME%/skills/pptx/scripts/thumbnail.py` |
| inventory.py | `%NAVI_HOME%/skills/pptx/scripts/inventory.py` |
| replace.py | `%NAVI_HOME%/skills/pptx/scripts/replace.py` |
| rearrange.py | `%NAVI_HOME%/skills/pptx/scripts/rearrange.py` |
| unpack.py | `%NAVI_HOME%/skills/pptx/ooxml/scripts/unpack.py` |
| pack.py | `%NAVI_HOME%/skills/pptx/ooxml/scripts/pack.py` |
| validate.py | `%NAVI_HOME%/skills/pptx/ooxml/scripts/validate.py` |

参考文档：
- html2pptx.md: `%NAVI_HOME%/skills/pptx/html2pptx.md`
- ooxml.md: `%NAVI_HOME%/skills/pptx/ooxml.md`

执行脚本时使用 `run_command` 工具，例如：
```bash
python "%NAVI_HOME%/skills/pptx/scripts/thumbnail.py" presentation.pptx
```

---

## 1. 读取和分析内容

### 1.1 文本提取
```bash
python -m markitdown 文件路径.pptx
```

### 1.2 原始 XML 访问（评论、备注、动画等）
```bash
python "%NAVI_HOME%/skills/pptx/ooxml/scripts/unpack.py" <pptx文件> <输出目录>
```

关键 XML 文件：
- `ppt/presentation.xml` — 主元数据和幻灯片引用
- `ppt/slides/slide{N}.xml` — 各幻灯片内容
- `ppt/notesSlides/notesSlide{N}.xml` — 演讲者备注
- `ppt/comments/modernComment_*.xml` — 批注
- `ppt/theme/theme1.xml` — 主题配色和字体
- `ppt/media/` — 图片等媒体

### 1.3 字体和颜色提取
分析现有设计时：读取 `ppt/theme/theme1.xml` 中的 `<a:clrScheme>`（颜色方案）和 `<a:fontScheme>`（字体方案），以及 slides XML 中的实际使用情况。

---

## 2. 创建新演示文稿

### 方式 A：html2pptx 工作流（推荐，精确还原 HTML 布局）

**必须先完整阅读 `html2pptx.md`**，了解所有语法规则。

**如果是将已有的网页展示（如 guizang-ppt-skill 输出的 HTML）转换为 PowerPoint，建议同时阅读 `html2pptx-workflow.md`**——它包含了实战中的字体映射策略、溢出调试流程、Module Resolution 陷阱等经验。

#### 工作流
1. **阅读 `html2pptx.md`** — 使用 `read_file` 完整读取
2. **创建 HTML 幻灯片** — 尺寸 720pt × 405pt（16:9）
   - 所有文本必须在 `<p>`, `<h1>-<h6>`, `<ul>`, `<ol>` 中
   - 渐变和图标必须先用 Sharp 栅格化为 PNG
   - 占位图表区用 `class="placeholder"`
3. **运行转换脚本** — 创建 JS 文件调用 `html2pptx()` 函数
4. **缩略图验证** — 检查文字截断、重叠、定位问题
   ```bash
   python "%NAVI_HOME%/skills/pptx/scripts/thumbnail.py" output.pptx workspace/thumbnails --cols 4
   ```

#### 依赖
html2pptx.js 依赖 `playwright` 和 `sharp`。如果用户拒绝安装，使用方式 B。

### 方式 B：直接使用 pptxgenjs（简化流程）

当 html2pptx.js 的依赖（playwright、sharp）不可用时，直接用 pptxgenjs 手动创建。

```javascript
const pptxgen = require('pptxgenjs');
const pptx = new pptxgen();
pptx.layout = 'LAYOUT_16x9';

const slide = pptx.addSlide();
slide.background = { color: '#fafaf8' };

// 添加文本
slide.addText('标题', {
  x: '10%', y: '20%', w: '80%', h: '15%',
  fontSize: 36, fontFace: 'Arial', color: '#0a0a0a',
  fontWeight: '200', align: 'center'
});

// 添加形状
slide.addShape('rect', {
  x: '10%', y: '40%', w: '30%', h: '20%',
  fill: { color: '#f0f0ee' }
});

await pptx.writeFile({ fileName: 'output.pptx' });
```

**⚠️ pptxgenjs 关键注意事项**：
- **颜色格式**：只支持 6 位 hex RGB（如 `#002FA7`）或 `pptxgen.SchemeColor`。**不支持** `rgba()`、`rgb()`、带透明度的颜色格式。使用 rgba 会被静默替换为 `#000000`。
- **形状 API**：使用字符串 `'rect'`、`'roundRect'`、`'ellipse'` 等，**不要**用 `pptxgen.shapes.RECTANGLE`（会报 undefined 错误）。
- **字体**：只支持 web-safe 字体。中文用 Arial 即可，不要用 `'Noto Sans SC'` 等自定义字体。
- **位置单位**：支持百分比（`'10%'`）和英寸数值。

---

## 3. 编辑现有演示文稿

使用 OOXML 编辑工作流。**必须先完整阅读 `ooxml.md`**。

### 3.1 工作流
1. **阅读 `ooxml.md`** — 使用 `read_file` 完整读取
2. **解包**：`python "%NAVI_HOME%/skills/pptx/ooxml/scripts/unpack.py" <文件> <输出目录>`
3. **编辑 XML**：修改 `ppt/slides/slide{N}.xml` 等
4. **验证**：`python "%NAVI_HOME%/skills/pptx/ooxml/scripts/validate.py" <目录> --original <文件>`
5. **打包**：`python "%NAVI_HOME%/skills/pptx/ooxml/scripts/pack.py" <输入目录> <输出文件>`

---

## 4. 使用模板创建演示文稿

### 4.1 工作流
1. **提取模板信息**：
   ```bash
   python -m markitdown template.pptx > template-content.md
   python "%NAVI_HOME%/skills/pptx/scripts/thumbnail.py" template.pptx
   ```
   阅读模板内容 md 和缩略图，分析布局。

2. **创建模板清单**：记录每张幻灯片的用途和布局类型

3. **重排幻灯片**：
   ```bash
   python "%NAVI_HOME%/skills/pptx/scripts/rearrange.py" template.pptx working.pptx 0,1,2,3
   ```
   （索引从 0 开始，可重复以复制幻灯片）

4. **提取文本结构**：
   ```bash
   python "%NAVI_HOME%/skills/pptx/scripts/inventory.py" working.pptx text-inventory.json
   ```
   阅读 text-inventory.json，了解所有 shape 及其属性。

5. **生成替换文本**：基于 inventory 创建 replacement-text.json，包含段落属性和格式化信息

6. **应用替换**：
   ```bash
   python "%NAVI_HOME%/skills/pptx/scripts/replace.py" working.pptx replacement-text.json output.pptx
   ```

---

## 5. 生成缩略图

```bash
python "%NAVI_HOME%/skills/pptx/scripts/thumbnail.py" presentation.pptx [输出前缀]
# 默认 5 列，最多 30 张/图
# 可用 --cols 3-6 调整列数
```

---

## 6. 依赖安装

如果需要安装依赖：

```bash
pip install "markitdown[pptx]" defusedxml
npm install -g pptxgenjs playwright sharp
```

**本机已安装 LibreOffice**（版本 26.2.3.2），路径 `C:\Program Files\LibreOffice\program\soffice.exe`，`soffice` 已在 PATH 中。

PPTX → PDF 转换命令：
```bash
soffice --headless --convert-to pdf --outdir ./output_dir input.pptx
```

PDF → PPTX 转换命令：
```bash
soffice --headless --convert-to pptx --outdir ./output_dir input.pdf
```

检测 LibreOffice 安装路径：
```bash
where soffice
```

---

## 7. 代码风格

- 代码简洁，避免冗余变量和不必要的 print
- 生成 PPTX 相关的 JS/Python 代码时，优先使用 skill 目录下的脚本

---

> **注意**：`html2pptx.md` 和 `ooxml.md` 是此技能的关键参考文档。在处理创建或编辑任务时，务必先完整阅读对应文档。