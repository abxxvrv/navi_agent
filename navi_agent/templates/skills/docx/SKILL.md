---
name: docx
description: "全面处理文档创建、编辑和分析，支持修订、批注、格式保留和文本提取。当需要处理专业 Word 文档（.docx 或 .doc 文件）时使用，包括：创建新文档、修改或编辑内容、处理修订、添加批注、提取文本内容或其他文档任务。也适用于中文学术报告、课程设计说明书等格式化文档的生成，以及将matplotlib图表插入到Word文档中。也适用于填写表单模板（中期验收表、申请书、评审表等），从多个参考文档（xlsx、docx、用户记忆）提取数据后填写到 .doc/.docx 表单中。"
---

# DOCX creation, editing, and analysis

## Overview

A user may ask you to create, edit, or analyze the contents of a .docx file. A .docx file is essentially a ZIP archive containing XML files and other resources that you can read or edit. You have different tools and workflows available for different tasks.

## Workflow Decision Tree

### Reading/Analyzing Content
Use "Text extraction" or "Raw XML access" sections below

### Creating New Document
Use "Creating a new Word document" workflow

### Editing Existing Document
- **Your own document + simple changes** → "Basic OOXML editing" workflow
- **Someone else's document** → "Redlining workflow" (recommended default)

### Filling Form Templates
Use "Form Filling Workflow" when populating .doc/.docx form templates with data from reference documents (申报书、汇总表、xlsx 等)

### Merging Documents
Use "Document Merging Workflow" when combining content from multiple documents

### Inserting Charts/Images
Use "Chart Insertion Workflow" when adding matplotlib charts or images to documents

### Data Analysis → Report Generation
Use "Data Analysis Report Workflow" when the task involves analyzing data with Python and producing a Word report with embedded charts

## Reading and analyzing content

### Text extraction (首选方案)

**当只需要读取文档文本内容时，Pandoc 是首选方案**——无需安装 Python 库，一条命令搞定。

#### .docx 文件
```bash
pandoc file.docx -t markdown --wrap=none
pandoc --track-changes=all file.docx -o output.md
```

#### .doc 文件（老版 Word 格式）
```bash
# Pandoc 不能处理 .doc 格式（只能处理 .docx）
# 必须先用 LibreOffice 转换为 .docx
soffice --headless --convert-to docx file.doc
# 然后用 Pandoc 读取转换后的 .docx
pandoc file.docx -t markdown --wrap=none
```

## Creating a new Word document

When creating a new Word document from scratch, use **docx-js**.

### Workflow
1. **MANDATORY - READ ENTIRE FILE**: Read [`docx-js.md`](docx-js.md) completely.
2. Create a JavaScript/TypeScript file using Document, Paragraph, TextRun components
3. Export as .docx using Packer.toBuffer()

### Running scripts with global npm packages

```bash
NODE_PATH=$(npm root -g) node generate_report.js
```

## Data Analysis Report Workflow

当任务涉及"用 Python 分析数据 → 生成 Word 报告（含图表）"时，按以下流程操作：

### Pipeline

```
Python 数据分析脚本 → 生成 PNG 图表 → docx-js 生成 Word 报告（嵌入图表）→ LibreOffice 转 PDF 验证
```

### Step 1: Python 数据分析 + 生成图表

```python
import matplotlib
matplotlib.use('Agg')  # ⚠️ 必须在 import pyplot 之前
import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei']
plt.rcParams['axes.unicode_minus'] = False

# 图表计数器模式（批量生成多张图时推荐）
plot_counter = 0

def save_plot(fig, name_hint):
    global plot_counter
    plot_counter += 1
    filename = f'plot_{plot_counter}_{name_hint}.png'
    fig.savefig(filename, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'>>> 图表已保存: {filename}')
    return filename
```

### Step 2: docx-js 嵌入图表

```javascript
const fs = require('fs');
const { Document, Packer, ImageRun, Paragraph, AlignmentType } = require('docx');

// 读取所有图表
const chartFiles = ['plot_1_boxplot.png', 'plot_2_heatmap.png'];
const charts = chartFiles.map(f => fs.readFileSync(f));

// 创建带图题的图片段落
function createChartParagraph(imageData, width, height, caption) {
    return [
        new Paragraph({
            alignment: AlignmentType.CENTER,
            spacing: { before: 240, after: 120 },
            children: [new ImageRun({
                type: "png",  // ⚠️ 必须指定
                data: imageData,
                transformation: { width, height },
                altText: { title: caption, description: caption, name: caption }
            })]
        }),
        new Paragraph({
            alignment: AlignmentType.CENTER,
            spacing: { before: 60, after: 240 },
            children: [new TextRun({ text: caption, bold: true, size: 21, font: "宋体" })]
        })
    ];
}

// 在文档中使用
sections: [{
    children: [
        // ...正文...
        ...createChartParagraph(charts[0], 500, 300, "图3-1  核心指标分布对比"),
        // ...更多内容...
    ]
}]
```

### Step 3: 验证

```bash
NODE_PATH=$(npm root -g) node generate_report.js
soffice --headless --convert-to pdf output.docx
```

### 常见陷阱

1. **plt.show() 阻塞**：CLI 环境下必须用 Agg 后端 + savefig（见 Common Issues #6）
2. **JS 中文引号转义**：含中文引号的字符串用单引号包裹（见 Common Issues #5）
3. **图片尺寸**：docx-js 的 `transformation` 单位是像素，热力图等大图建议 500-600px 宽
4. **Word 锁文件残留**：反复打开关闭 Word 后 ~$Normal.dotm 可能残留，导致保存报错（见 Common Issues #7）

## Form Filling Workflow

当需要填写表单模板（中期验收表、申请书、评审表、结题报告等）时，表单通常是 .doc 或 .docx 格式，需要从多个参考文档中提取数据后填入。

### 典型场景

- 大学生科技立项中期验收表 ← 申报书.docx + 汇总表.xlsx
- 课程设计报告封面 ← 学生信息 + 题目要求
- 政府/企业表格 ← 多份参考材料

### Workflow

#### 1. 读取所有参考数据源

```bash
# .docx 文件 → Pandoc 直接读
pandoc reference.docx -t markdown --wrap=none

# .doc 文件 → 先转 .docx
soffice --headless --convert-to docx file.doc
pandoc file.docx -t markdown --wrap=none

# xlsx 文件 → Python openpyxl
python -c "
import openpyxl
wb = openpyxl.load_workbook('data.xlsx')
for sheet_name in wb.sheetnames:
    ws = wb[sheet_name]
    for row in ws.iter_rows(values_only=False):
        vals = [f'{c.coordinate}={c.value}' for c in row if c.value is not None]
        if vals: print(' | '.join(vals))
"
```

> **Windows 注意**: `python3` 是 Windows Store 桩程序（exit code 49），必须用 `python`。

#### 2. 转换 .doc 模板为 .docx

```bash
soffice --headless --convert-to docx form_template.doc
```

#### 3. 解包 .docx 获取 XML

```bash
python ooxml/scripts/unpack.py form_template.docx unpacked_form
```

#### 4. 分析表结构

```python
from lxml import etree
ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
tree = etree.parse('unpacked_form/word/document.xml')
root = tree.getroot()

tbl = root.find(f'.//{{{ns}}}tbl')
rows = tbl.findall(f'{{{ns}}}tr')

for i, tr in enumerate(rows):
    cells = tr.findall(f'{{{ns}}}tc')
    cell_texts = []
    for j, tc in enumerate(cells):
        texts = [t.text for t in tc.iter(f'{{{ns}}}t') if t.text]
        cell_texts.append(f'C{j}=[{"".join(texts).strip()}]')
    print(f'R{i}: ' + ' | '.join(cell_texts))
```

#### 5. 填写单元格

核心函数：

```python
def set_cell_text(cell, text, font_size='21'):
    """设置单元格文本，保留原有 run 格式"""
    paras = cell.findall(f'{{{ns}}}p')
    if not paras:
        p = etree.SubElement(cell, f'{{{ns}}}p')
        paras = [p]
    para = paras[0]

    # 复制已有格式
    existing_runs = para.findall(f'{{{ns}}}r')
    rpr_template = None
    if existing_runs:
        rpr = existing_runs[0].find(f'{{{ns}}}rPr')
        if rpr is not None:
            rpr_template = etree.fromstring(etree.tostring(rpr))

    for child in list(para):
        para.remove(child)

    r = etree.SubElement(para, f'{{{ns}}}r')
    if rpr_template is not None:
        r.append(rpr_template)
    else:
        rPr = etree.SubElement(r, f'{{{ns}}}rPr')
        rFonts = etree.SubElement(rPr, f'{{{ns}}}rFonts')
        rFonts.set(f'{{{ns}}}ascii', '宋体')
        rFonts.set(f'{{{ns}}}eastAsia', '宋体')
        rFonts.set(f'{{{ns}}}hAnsi', '宋体')
        sz = etree.SubElement(rPr, f'{{{ns}}}sz')
        sz.set(f'{{{ns}}}val', font_size)

    t = etree.SubElement(r, f'{{{ns}}}t')
    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    t.text = text

def add_paragraph_to_cell(cell, text, font_size='21'):
    """在单元格末尾追加段落（用于长文本内容区）"""
    p = etree.SubElement(cell, f'{{{ns}}}p')
    r = etree.SubElement(p, f'{{{ns}}}r')
    rPr = etree.SubElement(r, f'{{{ns}}}rPr')
    rFonts = etree.SubElement(rPr, f'{{{ns}}}rFonts')
    rFonts.set(f'{{{ns}}}ascii', '宋体')
    rFonts.set(f'{{{ns}}}eastAsia', '宋体')
    rFonts.set(f'{{{ns}}}hAnsi', '宋体')
    sz = etree.SubElement(rPr, f'{{{ns}}}sz')
    sz.set(f'{{{ns}}}val', font_size)

    t = etree.SubElement(r, f'{{{ns}}}t')
    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    t.text = text
```

填写模式：
- **短字段**（学院、姓名、类别）：直接 `set_cell_text(cell, value)`
- **长内容区**（研究进展、问题措施）：先保留标题段落，删除其余段落，再 `add_paragraph_to_cell(cell, content)`

```python
# 短字段
set_cell_text(rows[1][1], '数学与信息科学学院')  # R1C1 = 学院

# 长内容区：保留标题，删除其余，追加内容
content_cell = rows[8][0]  # R8C0 = 研究进展情况
paras = content_cell.findall(f'{{{ns}}}p')
for p in paras[1:]:  # 保留第一个段落（标题）
    content_cell.remove(p)
add_paragraph_to_cell(content_cell, long_text)
```

#### 6. 保存并打包

```python
tree.write('unpacked_form/word/document.xml',
           xml_declaration=True, encoding='UTF-8', standalone=True)
```

```bash
python ooxml/scripts/pack.py unpacked_form filled_form.docx --force
```

#### 7. 如需转回 .doc

```bash
soffice --headless --convert-to doc filled_form.docx
```

#### 8. 验证

```bash
soffice --headless --convert-to pdf filled_form.docx
pdftoppm -jpeg -r 150 filled_form.pdf page
```

## Document Merging Workflow

当需要将一个文档的内容合并到另一个文档时（例如：将生成的报告插入到模板文件中）。

### Key Points
- **只处理 body 的直接子元素**（段落、表格等），不要用 findall 递归查找
- 保留 `sectPr` 元素（页面设置）
- 从后往前删除避免索引变化
- 使用 `etree.fromstring(etree.tostring(para))` 深拷贝元素
- 打包时使用 `--force` 跳过验证（如果 XML 有问题）

### Workflow

```python
from lxml import etree

ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

def get_paragraph_text(para):
    text = ""
    for t in para.findall(f".//{{{ns}}}t"):
        if t.text:
            text += t.text
    return text

# 1. 获取body的直接子元素（关键！）
body_children = list(body)

# 2. 找到目标位置
target_index = None
for i, child in enumerate(body_children):
    if child.tag == f'{{{ns}}}p':
        text = get_paragraph_text(child)
        if target_text in text:
            target_index = i
            break

# 3. 从后往前删除（保留sectPr）
indices_to_remove = []
for i in range(target_index, len(body_children)):
    if body_children[i].tag == f'{{{ns}}}sectPr':
        continue
    indices_to_remove.append(i)

for i in reversed(indices_to_remove):
    body.remove(body_children[i])

# 4. 插入新内容（在sectPr之前）
sect_pr = body.find(f'{{{ns}}}sectPr')
for para in paras_to_insert:
    new_para = etree.fromstring(etree.tostring(para))
    if sect_pr is not None:
        body.insert(list(body).index(sect_pr), new_para)
```

### Verification
```bash
python ooxml/scripts/pack.py unpacked_dir output.docx --force
soffice --headless --convert-to pdf output.docx
pdftoppm -jpeg -r 150 output.pdf page
# 使用 vision_analyze 检查
```

## Chart Insertion Workflow

当需要将 matplotlib 生成的图表插入到 Word 文档中时。

### Workflow

1. **生成图表** (matplotlib) — 非交互式环境必须用 Agg 后端
```python
import matplotlib
matplotlib.use('Agg')  # ⚠️ 必须在 import pyplot 之前！CLI/脚本环境下 plt.show() 会阻塞
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei']
plt.rcParams['axes.unicode_minus'] = False

fig, ax = plt.subplots(figsize=(8, 5))
# ... 绘图代码 ...
plt.savefig('chart.png', dpi=200, bbox_inches='tight')
plt.close()  # ⚠️ 必须关闭释放内存，批量生成多张图时尤其重要
# ❌ 不要用 plt.show() — 在脚本/CLI 环境下会阻塞
```

2. **解包目标文档**
```bash
python ooxml/scripts/unpack.py document.docx unpacked_dir
```

3. **复制图片到 media 目录**
```python
import shutil
media_dir = os.path.join(unpacked_dir, "word", "media")
os.makedirs(media_dir, exist_ok=True)
shutil.copy2('chart.png', os.path.join(media_dir, 'chart.png'))
```

4. **添加图片引用**
```python
# word/_rels/document.xml.rels
# [Content_Types].xml
```

5. **使用 lxml 创建图片元素** (必须用这种方式，不要解析XML字符串)

```python
from lxml import etree

NSMAP = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
}

def create_image_element(rid, width_emu, height_emu, image_filename):
    W, WP, A, PIC, R = NSMAP['w'], NSMAP['wp'], NSMAP['a'], NSMAP['pic'], NSMAP['r']
    
    p = etree.Element(f'{{{W}}}p')
    pPr = etree.SubElement(p, f'{{{W}}}pPr')
    jc = etree.SubElement(pPr, f'{{{W}}}jc')
    jc.set(f'{{{W}}}val', 'center')
    
    r = etree.SubElement(p, f'{{{W}}}r')
    drawing = etree.SubElement(r, f'{{{W}}}drawing')
    
    inline = etree.SubElement(drawing, f'{{{WP}}}inline')
    inline.set('distT', '0')
    inline.set('distB', '0')
    inline.set('distL', '0')
    inline.set('distR', '0')
    
    extent = etree.SubElement(inline, f'{{{WP}}}extent')
    extent.set('cx', str(width_emu))
    extent.set('cy', str(height_emu))
    
    docPr = etree.SubElement(inline, f'{{{WP}}}docPr')
    docPr.set('id', '1')
    docPr.set('name', image_filename)
    
    graphic = etree.SubElement(inline, f'{{{A}}}graphic')
    graphicData = etree.SubElement(graphic, f'{{{A}}}graphicData')
    graphicData.set('uri', 'http://schemas.openxmlformats.org/drawingml/2006/picture')
    
    pic = etree.SubElement(graphicData, f'{{{PIC}}}pic')
    nvPicPr = etree.SubElement(pic, f'{{{PIC}}}nvPicPr')
    cNvPr = etree.SubElement(nvPicPr, f'{{{PIC}}}cNvPr')
    cNvPr.set('id', '1')
    cNvPr.set('name', image_filename)
    etree.SubElement(nvPicPr, f'{{{PIC}}}cNvPicPr')
    
    blipFill = etree.SubElement(pic, f'{{{PIC}}}blipFill')
    blip = etree.SubElement(blipFill, f'{{{A}}}blip')
    blip.set(f'{{{R}}}embed', rid)
    etree.SubElement(blipFill, f'{{{A}}}stretch').append(etree.Element(f'{{{A}}}fillRect'))
    
    spPr = etree.SubElement(pic, f'{{{PIC}}}spPr')
    xfrm = etree.SubElement(spPr, f'{{{A}}}xfrm')
    ext = etree.SubElement(xfrm, f'{{{A}}}ext')
    ext.set('cx', str(width_emu))
    ext.set('cy', str(height_emu))
    prstGeom = etree.SubElement(spPr, f'{{{A}}}prstGeom')
    prstGeom.set('prst', 'rect')
    etree.SubElement(prstGeom, f'{{{A}}}avLst')
    
    return p

def create_caption_element(caption):
    W = NSMAP['w']
    
    p = etree.Element(f'{{{W}}}p')
    pPr = etree.SubElement(p, f'{{{W}}}pPr')
    jc = etree.SubElement(pPr, f'{{{W}}}jc')
    jc.set(f'{{{W}}}val', 'center')
    
    r = etree.SubElement(p, f'{{{W}}}r')
    rPr = etree.SubElement(r, f'{{{W}}}rPr')
    rFonts = etree.SubElement(rPr, f'{{{W}}}rFonts')
    rFonts.set(f'{{{W}}}ascii', '宋体')
    rFonts.set(f'{{{W}}}eastAsia', '宋体')
    rFonts.set(f'{{{W}}}hAnsi', '宋体')
    etree.SubElement(rPr, f'{{{W}}}b')
    sz = etree.SubElement(rPr, f'{{{W}}}sz')
    sz.set(f'{{{W}}}val', '21')
    
    t = etree.SubElement(r, f'{{{W}}}t')
    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    t.text = caption
    
    return p
```

6. **获取图片尺寸并插入**
```python
from PIL import Image

def get_image_dimensions(image_path, max_width_emu=5000000):
    with Image.open(image_path) as img:
        width_px, height_px = img.size
    
    width_emu = int(width_px * 914400 / 96)
    height_emu = int(height_px * 914400 / 96)
    
    if width_emu > max_width_emu:
        ratio = max_width_emu / width_emu
        width_emu = max_width_emu
        height_emu = int(height_emu * ratio)
    
    return width_emu, height_emu

# 插入图片和图题
img_para = create_image_element(rid, width_emu, height_emu, filename)
caption_para = create_caption_element('图1  图表标题')

body.insert(target_index + 1, img_para)
body.insert(target_index + 2, caption_para)
```

### Chart Naming Convention

- 图1, 图2, 图3... (中文报告)
- 图题使用宋体五号（21 half-points），居中，加粗

### Common Chart Positions

| 图表类型 | 建议位置 |
|---------|---------|
| 分布图（饼图/柱状图） | 描述性统计部分，对应表格之后 |
| 箱线图/散点图 | 分析结果部分 |
| 热力图 | 相关性分析部分 |
| 模型对比图 | 预测模型部分 |

### docx-js 插入图片（推荐方案）

当用 docx-js 生成新文档时，直接在 JS 中嵌入图片：

```javascript
const fs = require('fs');
const { ImageRun } = require('docx');

// 读取图片
const chartData = fs.readFileSync('chart.png');

// 在段落中插入
new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [new ImageRun({
        type: "png",  // ⚠️ 必须指定 type 参数
        data: chartData,
        transformation: { width: 500, height: 300 },  // 单位：像素
        altText: { title: "图表标题", description: "图表描述", name: "chart1" }
    })]
})
```

## Chinese Document Formatting

### Font Size Conversion (字号 to half-points)

| 字号 | pt | half-points (size) |
|------|-----|-------------------|
| 小二 | 18pt | 36 |
| 小三 | 15pt | 30 |
| 四号 | 14pt | 28 |
| 小四 | 12pt | 24 |
| 五号 | 10.5pt | 21 |
| 小五 | 9pt | 18 |

### Page Margins (DXA: 1cm ≈ 567)

| Preset | Top/Bottom | Left | Right |
|--------|------------|------|-------|
| Academic | 2.5cm=1418 | 3cm=1701 | 2cm=1134 |
| Standard | 2.54cm=1440 | 3.18cm=1800 | 3.18cm=1800 |

### Font Settings for Mixed Text

```javascript
new TextRun({
  text: "中文 English",
  font: { eastAsia: '宋体', ascii: 'Times New Roman', hAnsi: 'Times New Roman' },
  size: 24,
  color: '000000'
})
```

## Document Quality Verification

### Visual Inspection Workflow

```bash
soffice --headless --convert-to pdf document.docx
pdftoppm -jpeg -r 150 document.pdf page
# 使用 vision_analyze 检查每页
```

### Common Check Points

- 封面：标题居中、个人信息对齐
- 目录：层级清晰、页码右对齐
- 正文：首行缩进两字符、行间距适当
- 表格：表头居中加粗、数据对齐
- 图表：居中显示、图题格式统一
- 页眉页脚：位置正确、格式统一

## Common Issues

### Issue 1: NODE_PATH Not Found
```bash
NODE_PATH=$(npm root -g) node script.js
```

### Issue 2: Pack Validation Failed
```bash
python ooxml/scripts/pack.py unpacked_dir output.docx --force
```

### Issue 3: XML Namespace Error When Inserting Images
使用 lxml 创建元素，不要解析 XML 字符串。

### Issue 4: Chinese Font Not Displaying
设置 `eastAsia` 字体属性。

### Issue 5: JavaScript String Syntax Error with Chinese Quotes
中文文本中的引号（`""` 或 `""`）会与 JavaScript 字符串定界符冲突，导致 `SyntaxError: Unexpected identifier`。

```javascript
// ❌ 错误：中文引号 "" 被当作字符串结束符
new TextRun({ text: "实现从"被动治疗"向"主动健康管理"的转变。", size: 24 })

// ✅ 方案1：外层用单引号（最简洁）
new TextRun({ text: '实现从"被动治疗"向"主动健康管理"的转变。', size: 24 })

// ✅ 方案2：转义内部引号
new TextRun({ text: "实现从\"被动治疗\"向\"主动健康管理\"的转变。", size: 24 })

// ✅ 方案3：使用模板字面量（反引号）
new TextRun({ text: `实现从"被动治疗"向"主动健康管理"的转变。`, size: 24 })
```

**调试技巧**：当报错指向中文文本的某个位置时，检查该位置附近是否有引号、括号等特殊字符与 JS 语法冲突。**方案1（单引号）最不容易出错。**

### Issue 6: matplotlib plt.show() Blocks in CLI/Script Environment
在 CLI 或脚本环境中调用 `plt.show()` 会阻塞执行，因为默认后端尝试打开 GUI 窗口。

```python
# ❌ 错误：脚本环境下 plt.show() 阻塞
import matplotlib.pyplot as plt
plt.plot([1,2,3])
plt.show()  # 阻塞！

# ✅ 正确：使用 Agg 后端 + savefig
import matplotlib
matplotlib.use('Agg')  # 必须在 import pyplot 之前
import matplotlib.pyplot as plt

plt.plot([1,2,3])
plt.savefig('output.png', dpi=150, bbox_inches='tight')
plt.close()  # 释放内存
```

**检测方法**：如果 Python 脚本执行超时或无输出，检查是否使用了 `plt.show()`。

### Issue 7: Word "内存或磁盘空间不足" Save Error
Word 报告"内存或磁盘空间不足，保存失败"通常**不是**磁盘空间问题（可用空间充足时仍会出现）。

**诊断清单**（按概率排序）：

1. **~$ 锁文件残留**（最常见）：Word 的临时锁定文件（`~$Normal.dotm`、`~$ilding Blocks.dotx` 等）未被正常清理，导致 Word 误判资源被占用。
   ```bash
   # 检查是否存在残留锁文件
   ls -la "$APPDATA/Microsoft/Templates/~\$*"
   ls -la "$APPDATA/Microsoft/Document Building Blocks/"*/~\$*
   
   # 清理（关闭 Word 后执行）
   rm -f "$APPDATA/Microsoft/Templates/~\$Normal.dotm"
   rm -f "$APPDATA/Microsoft/Document Building Blocks/"*/~\$*
   ```

2. **孤立加载项 .wll 残留**（高概率）：已卸载软件（如 MathType、Zotero、Grammarly）的 Word 加载项文件残留在 STARTUP 目录，Word 每次启动尝试加载时出错，导致内存泄漏或资源冲突。
   ```bash
   # 检查 STARTUP 目录中的加载项
   ls -la "$APPDATA/Microsoft/Word/STARTUP/"
   
   # 如果发现 .wll 文件但对应软件已卸载，直接删除
   rm -f "$APPDATA/Microsoft/Word/STARTUP/MathPage.wll"
   ```
   
   **典型症状**：Word 进程内存异常高（>400MB），保存时偶发"内存不足"错误。
   
   **确认方法**：检查 `Program Files` 中对应软件是否存在：
   ```bash
   ls -la "/c/Program Files/MathType" 2>/dev/null || echo "MathType未安装"
   ```
   如果软件不存在但 .wll 文件存在，就是孤立加载项。

3. **Word 进程内存过高**：文档包含大量高分辨率嵌入图片，或多个 Word 实例同时运行。
   ```bash
   # 检查 Word 内存占用和实例数量
   tasklist | grep WINWORD
   ```

4. **系统内存不足**：其他程序（Chrome、VS Code 等）占用大量内存。

**完整清理脚本**（关闭 Word 后执行）：
```bash
# 清理锁文件
rm -f "$APPDATA/Microsoft/Templates/~\$Normal.dotm"
rm -f "$APPDATA/Microsoft/Document Building Blocks/2052/16/~\$ilding Blocks.dotx"
rm -f "$APPDATA/Microsoft/Document Building Blocks/2052/16/~\$ilt-In Building Blocks.dotx"

# 清理孤立加载项（根据实际情况调整文件名）
rm -f "$APPDATA/Microsoft/Word/STARTUP/MathPage.wll"

# 清理自动恢复残留
rm -f "$APPDATA/Microsoft/Word/~"*.wbk
```

**预防**：卸载 MathType 等软件时，手动检查并清理 `STARTUP` 目录中的 .wll 文件。

## Dependencies

- **pandoc**: Text extraction
- **docx**: `npm install -g docx` (creating new documents)
- **LibreOffice**: PDF/doc↔docx conversion
- **Poppler**: PDF to images
- **lxml**: XML manipulation
- **Pillow**: Image dimension calculation
- **openpyxl**: xlsx reading (for form filling data sources)
