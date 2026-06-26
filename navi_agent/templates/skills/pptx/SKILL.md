---
name: pptx
description: "演示文稿创建、编辑和分析。当用户需要处理 PowerPoint 演示文稿（.pptx 文件）时使用，包括创建新演示文稿、修改内容、处理布局、添加批注或演讲者备注等。也适用于将 guizang-ppt HTML 转换为 PPTX。也适用于基于 Word/文本文档生成学术汇报 PPT（docx→PPT），包含中文学术 PPT 配色方案、卡片布局、条形图、表格等常用模式。触发词：做PPT、做个PPT、演示文稿、slides、presentation、汇报PPT。"
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

## 7. 从文档生成 PPT（docx → PPT 工作流）

当用户要求"基于某个 Word 文档做一个 PPT"时，使用以下工作流：

### 7.1 提取文档文本

优先用 `markitdown`，不可用时用 `python-docx` 降级（python-docx 几乎总是已安装）：

```bash
# 方法1：markitdown（优先，需 pip install markitdown）
python -m markitdown 文档.docx

# 方法2：python-docx 降级（可靠，通常已安装）
python -c "
import docx
doc = docx.Document('文档.docx')
for p in doc.paragraphs:
    if p.text.strip():
        print(p.text)
"
```

> **注意**：`read_file` 读取 .docx 会得到乱码（二进制），必须用上述命令行方式提取文本。

### 7.2 结构化内容

从提取的文本中识别文档结构，规划幻灯片：

1. **标题页**：文档标题 + 作者 + 机构 + 日期
2. **目录页**：章节列表，每项带编号和简述
3. **内容页**：按章节拆分，每章 1-3 页幻灯片
4. **结尾页**：感谢 / Q&A

### 7.3 生成 PPT

用方式 B（pptxgenjs）编写 JS 脚本生成。参考下方学术中文 PPT 模式。

---

## 8. 学术中文 PPT 模式（pptxgenjs）

创建中文学术/课程报告 PPT 时的通用模式和样式参考。

### 8.1 配色方案

#### 浅色方案（默认）
```javascript
const C = {
  primary: '1B4F72',   // 深蓝 - 标题、强调
  accent:  '2E86C1',   // 亮蓝 - 装饰、链接
  light:   'EBF5FB',   // 浅蓝 - 背景高亮区
  dark:    '0B2F4A',   // 深蓝黑 - 正文
  white:   'FFFFFF',
  gray:    '7F8C8D',   // 次要文字
  red:     'E74C3C',   // 警告/重要
  green:   '27AE60',   // 成功/低风险
  orange:  'F39C12',   // 中等/中风险
  bg:      'F8FBFE',   // 幻灯片背景
};
```

#### 深色方案（高对比度商务风格）
黑底 + 暖米白文字 + 赤陶红强调色，视觉冲击力强，适合发布会/答辩/营销。
```javascript
const C = {
  bg:       '000000',   // 纯黑背景
  bgAlt:    '181818',   // 近黑辅底（卡片/区块）
  text:     'F8E8D8',   // 暖米白主文字
  accent:   'C05137',   // 赤陶红强调色
  gray:     '8A8A8A',   // 次要文字
  dimText:  'A0A0A0',   // 淡化文字
  accentLt: 'E07040',   // 浅赤陶（中等强调）
};
```

**从参考图片提取配色**：当用户提供参考图片要求匹配风格时，用 PIL 提取主色：
```python
from PIL import Image
import numpy as np
from collections import Counter

def extract_colors(path, n=5):
    img = Image.open(path).convert('RGB').resize((200, 200))
    pixels = (np.array(img).reshape(-1, 3) // 8) * 8  # 量化去噪
    for color, count in Counter(map(tuple, pixels.tolist())).most_common(n):
        print(f'  #{color[0]:02x}{color[1]:02x}{color[2]:02x}  ({count/len(pixels)*100:.1f}%)')

# 橙红系强调色单独提取（小面积但关键）
def find_accent(path):
    img = Image.open(path).convert('RGB')
    pixels = np.array(img).reshape(-1, 3)
    mask = (pixels[:,0]>150) & (pixels[:,1]<150) & (pixels[:,2]<120) & (pixels[:,0]>pixels[:,1])
    accent = pixels[mask]
    if len(accent) > 0:
        avg = accent.mean(axis=0).astype(int)
        print(f'  #{avg[0]:02x}{avg[1]:02x}{avg[2]:02x}')
```

提取后根据图片风格选择浅色或深色方案模板，将提取的 hex 值填入对应角色。

### 8.2 常用布局模式

#### 数据卡片（顶部统计数字）
```javascript
// 三个并排卡片，展示关键数据
const stats = [
  { val: '5.37亿', label: '全球患者', color: C.red },
  { val: '1.4亿',  label: '中国患者', color: C.orange },
  { val: '12.8%',  label: '患病率',   color: C.accent },
];
stats.forEach((st, i) => {
  const x = 5 + i * 31;
  s.addShape('roundRect', { x: `${x}%`, y: '18%', w: '27%', h: '14%',
    fill: { color: C.white }, shadow: { type: 'outer', blur: 6, offset: 2, color: 'D5D8DC' }, rectRadius: 0.08 });
  s.addText(st.val, { x: `${x}%`, y: '18%', w: '27%', h: '8%',
    fontSize: 24, fontFace: 'Arial', color: st.color, bold: true, align: 'center', valign: 'middle' });
  s.addText(st.label, { x: `${x}%`, y: '26%', w: '27%', h: '5%',
    fontSize: 11, fontFace: 'Arial', color: C.gray, align: 'center', valign: 'middle' });
});
```

#### 横向条形图（特征重要性等）
```javascript
const features = [
  { name: 'HbA1c',       val: 0.2173, color: 'E74C3C' },
  { name: '空腹血糖',     val: 0.1487, color: 'F39C12' },
  { name: '多尿',        val: 0.0824, color: '2E86C1' },
];
const maxVal = 0.25;
features.forEach((f, i) => {
  const y = 18 + i * 7.5;
  const barW = (f.val / maxVal) * 45;  // 最大占 45% 宽度
  s.addText(f.name, { x: '3%', y: `${y}%`, w: '30%', h: '5%',
    fontSize: 10, align: 'right', valign: 'middle' });
  s.addShape('roundRect', { x: '35%', y: `${y+0.5}%`, w: '48%', h: '4%',
    fill: { color: 'EBF5FB' }, rectRadius: 0.03 });
  s.addShape('roundRect', { x: '35%', y: `${y+0.5}%`, w: `${barW}%`, h: '4%',
    fill: { color: f.color }, rectRadius: 0.03 });
  s.addText(f.val.toFixed(4), { x: `${35+barW}%`, y: `${y}%`, w: '10%', h: '5%',
    fontSize: 10, bold: true, align: 'left', valign: 'middle' });
});
```

#### 分层风险卡片（三列带边框颜色）
```javascript
const risks = [
  { level: '低风险', pct: '60.5%', actual: '6.16%',  color: '27AE60' },
  { level: '中风险', pct: '1.8%',  actual: '60.00%', color: 'F39C12' },
  { level: '高风险', pct: '37.7%', actual: '93.43%', color: 'E74C3C' },
];
risks.forEach((r, i) => {
  const x = 5 + i * 31;
  s.addShape('roundRect', { x: `${x}%`, y: '18%', w: '27%', h: '38%',
    fill: { color: C.white }, line: { color: r.color, width: 2.5 }, rectRadius: 0.1 });
  // ... 标题、占比、实际患病率依次排列
});
```

#### 页脚模式
```javascript
function addFooter(slide, pageNum) {
  slide.addText('机构名称', { x: 0, y: '95%', w: '100%', h: '5%',
    fontSize: 8, color: C.gray, align: 'left', margin: [0,0,0,40] });
  slide.addText(`${pageNum}`, { x: 0, y: '95%', w: '100%', h: '5%',
    fontSize: 8, color: C.gray, align: 'right', margin: [0,40,0,0] });
}
```

### 8.3 深浅交替主题

当用户要求深浅搭配（避免全部黑底或全部白底单调）时，定义两套色板并交替使用：

```javascript
const D = { bg: '000000', bgAlt: '181818', text: 'F8E8D8', dim: 'A0A0A0', accent: 'C05137', accentLt: 'E07040', gray: '8A8A8A', line: '2A2A2A' };
const L = { bg: 'F0E0D0', bgAlt: 'F8E8D8', text: '1A1A1A', dim: '5A5A5A', accent: 'C05137', accentLt: 'E07040', gray: '8A8A8A', line: 'D8C8B8' };

// 推荐交替模式（内容页浅色更易读）：
// 1-封面: DARK  2-目录: LIGHT  3-背景: DARK  4-数据: LIGHT
// 5-分析: DARK  6-模型: LIGHT  7-特征: DARK  8-分层: LIGHT
// 9-结论: DARK  10-致谢: DARK

function footer(slide, n, theme) {
  const c = theme === 'dark' ? D : L;
  // footer 颜色跟随主题
}
```

两套色板共用同一 accent 色保持统一感。`bgAlt` 用于卡片/区块背景，`line` 用于分割线和边框。

### 8.4 注意事项

- 中文 PPT 字体统一用 `'Arial'`（pptxgenjs 不支持自定义中文字体名）
- 颜色只用 6 位 hex，不支持 rgba
- 表格用 `s.addTable()` 实现，表头行用 fill 背景色，行交替用 `bg`/`bgAlt` 增加层次
- 信息提示框用 `roundRect` + 浅色填充 + 左侧彩色竖条
- **不要使用 emoji**：PPT 中的编号、标记、分隔符全部用纯文字/色块/数字，不用 emoji 字符。用 `1.` `2.` `3.` 代替 `1️⃣`，用色块标签代替图标，用 `***` 代替 `★`。pptxgenjs 的 emoji 渲染在不同系统上不一致。
- **用户偏好**：用户不喜欢 emoji，要求用纯文字/色块标识。深色页面放"震撼数据+结论"，浅色页面放"详细内容+表格"，节奏感更好。

---

## 9. 代码风格

- 代码简洁，避免冗余变量和不必要的 print
- 生成 PPTX 相关的 JS/Python 代码时，优先使用 skill 目录下的脚本

---

> **注意**：`html2pptx.md` 和 `ooxml.md` 是此技能的关键参考文档。在处理创建或编辑任务时，务必先完整阅读对应文档。