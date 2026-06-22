# html2pptx 实战工作流：网页展示 → PowerPoint 转换

## 适用场景

将已有的网页/交互式展示（如 guizang-ppt-skill 输出的横向翻页 HTML）转换为可编辑的 .pptx 文件。**这不是通用教程，而是实战经验积累。**

---

## 一、内容提取

假设源文件是单 HTML 文件，包含多个 `<section class="slide">`（或其他可识别容器）：

1. **找到各页容器**：源 HTML 中的 `<section>`/`<div>` 等，每个对应一页幻灯片
2. **逐页提取**：每页的标题、正文、引文、元数据等，保留原始层次结构
3. **记录配色**：提取每页的 `background-color`、文字颜色、强调色，形成色板

典型色板结构：
```css
/* 深色页 */
bg: #0a0a0b,  text: #f1efea,  accent: rgba(255,255,255,0.6)
/* 浅色页 */
bg: #f1efea,  text: #1a1a2e,  accent: rgba(26,26,46,0.6)
```

---

## 二、字体映射（重点）

网页常用 Google Fonts / 自定义字体，PowerPoint 只能用 web-safe 字体。**这是最常见的适配项，必须提前规划：**

| 网页字体 | Web-safe 替代 | 适用场景 |
|----------|---------------|----------|
| Playfair Display / Merriweather | **Georgia** | 标题/正文衬线 |
| Noto Serif SC / Source Han Serif | Georgia / Times New Roman | 中文标题衬线 |
| Inter / system-ui / -apple-system | **Arial** | 正文无衬线 |
| Noto Sans SC / Source Han Sans | Arial | 中文正文无衬线 |
| JetBrains Mono / SF Mono / Fira Code | **Courier New** | 等宽/元数据/代码 |
| Any display/script font | Georgia bold 或 Arial bold | 仅加粗处理，不保留装饰性字体 |

**策略**：全局替换，原 HTML 中使用 `font-family: 'Playfair Display', serif` → 新 HTML 中使用 `font-family: Georgia, serif`

---

## 三、创建幻灯片 HTML

### 3.1 尺寸与布局

```css
body {
  width: 720pt; height: 405pt;  /* 16:9，必须与 pptx.layout = 'LAYOUT_16x9' 一致 */
  margin: 0; padding: 0;
  display: flex;         /* 防止 margin collapse 破坏溢出验证 */
  box-sizing: border-box;
}
```

### 3.2 文本规则（最易出错）

- ✅ 所有文本必须在 `<p>`, `<h1>-<h6>`, `<ul>`, `<ol>` 中
- ❌ `<div>裸文本</div>` → 静默忽略，内容丢失
- ❌ `<span>文本</span>` → 静默忽略
- ❌ 手动子弹符 `•`、`-`、`*` → 使用 `<ul><li>` 替代

### 3.3 颜色与渐变

- CSS 渐变（`linear-gradient`、`radial-gradient`）**不支持**，必须用固态颜色替代
- 或预渲染为 PNG（用 Sharp 栅格化），使用 `<body style="background-image: url(...)">`
- 深色页背景用固态 `#0a0a0b` 即可，效果可接受

### 3.4 字号预估

根据内容量和 405pt 高度，**首轮给出保守字号**可减少迭代次数：

| 元素类型 | 保守字号 | 备注 |
|----------|---------|------|
| 主标题 h1 | 36-48pt | 如果页面还有大量其他内容，选下限 |
| 副标题 h2 | 18-22pt | |
| 正文 p | 10-13pt | |
| 引文/金句 | 14-18pt | 单独占一页时可放大 |
| 元数据/标签 | 8-9pt | Courier New 等宽小字 |
| 底部 footer | 8pt | |

**经验法则**：405pt ≈ 5.625 英寸。留出 padding 后有效空间约 330pt。每行文字按字号+行高估算，叠加所有内容行数+paddings+gap，确保总和 ≤ 350pt。

---

## 四、驱动脚本 convert.js

标准模板：

```javascript
const pptxgen = require('pptxgenjs');
const html2pptx = require('./html2pptx');  // 拷贝到项目目录！
const path = require('path');

async function createPresentation() {
    const pptx = new pptxgen();
    pptx.layout = 'LAYOUT_16x9';
    pptx.author = 'Navi';
    pptx.title = '演示文稿标题';

    const slideFiles = [
        'slide1.html', 'slide2.html', /* ... 所有 slide */
    ];

    for (const file of slideFiles) {
        console.log(`Converting ${file}...`);
        const { slide } = await html2pptx(file, pptx);
        console.log(`  ✓ ${file}`);
    }

    await pptx.writeFile({ fileName: 'output.pptx' });
    console.log('✅ Presentation saved!');
}

createPresentation().catch(err => {
    console.error('Failed:', err);
    process.exit(1);
});
```

### ⚠️ Module Resolution 陷阱

`html2pptx.js` 在技能目录，内部 `require('playwright')`。如果 convert.js 在项目目录运行，**Node 找不到 playwright**（它在技能目录的 node_modules 里，不在项目目录）。

**修复方法**：将 `html2pptx.js` 从 `%NAVI_HOME%/skills/pptx/scripts/html2pptx.js` 复制到项目目录，然后 `require('./html2pptx')`。

或者在项目目录安装 playwright：`npm install playwright`（无需全局安装）。

---

## 五、溢出错误调试流程（最耗时环节）

html2pptx 内置验证器会检查：内容是否超出 body、底部余量是否 ≥ 0.5 英寸。**错误信息精确到点数**，这是调试的核心依据。

### 5.1 错误解读

```
Content exceeds body: 83.3pt overflow    ← 严重超出，需大幅缩小
Content exceeds body: 9.8pt overflow     ← 微调即可通过
Content too close to bottom edge (0.39" < 0.50")  ← 底部余量不足
```

### 5.2 调整手法速查表

| 溢出量 | 调整策略 |
|--------|---------|
| 80+ pt | 大幅缩减：字号 ×0.5~0.6，padding ×0.5 |
| 30-80 pt | 中度缩减：字号 ×0.7，padding ×0.75 |
| 10-30 pt | 局部调整：减小 gap/间距 4-8pt，或 padding 减少 25% |
| < 10 pt | 微调：padding 减 2-4pt，或 margin-bottom 减几 pt |
| 底部余量不足 | 减少 body padding-bottom 或最后一个元素的 margin-bottom |

### 5.3 典型迭代次数

- **第 1 次**：根据内容估算字号，运行 → 大概率溢出 60-120pt
- **第 2 次**：大幅缩小，运行 → 溢出减少到 10-30pt
- **第 3 次**：微调 padding/gap，运行 → 通过 ✅

### 5.4 具体下手点（从高杠杆到低杠杆）

1. **h1 字号**（影响最大）：48pt → 36pt → 28pt，每减少 4pt 通常释放 8-12pt 空间
2. **body padding**：30pt → 24pt → 18pt
3. **flex gap**：18pt → 12pt → 8pt
4. **段落 margin**：`p { margin: 0 0 8pt 0 }` → `margin: 0`
5. **如果有多栏布局**：减少列数或让文字更精简
6. **底部元素**：foot 的 padding 单独减少

---

## 六、缩略图验证

转换完成后，生成缩略图检查视觉问题：

```bash
python "%NAVI_HOME%/skills/pptx/scripts/thumbnail.py" output.pptx
```

重点检查：
- 文字是否被截断（比溢出检查更直观）
- 元素是否重叠
- 颜色是否丢失（渐变变空白是最常见的）
- 对齐是否合理

---

## 七、常见问题速查

| 问题 | 原因 | 修复 |
|------|------|------|
| 文字在 PPT 中消失 | 文本在 `<div>`/`<span>` 裸标签中 | 包在 `<p>` 或 `<h1>-<h6>` 中 |
| 字体变了 | 使用了非 web-safe 字体 | 替换为 Georgia/Arial/Courier New |
| 渐变背景变纯色 | CSS 渐变不支持 | 用 Sharp 预渲染 PNG 或改用固态颜色 |
| 底部被切掉 | 底部余量 < 0.5" | 减小 padding-bottom 或整体缩小 |
| 运行报错找不到模块 | html2pptx.js 在另一目录 require 失败 | 复制到项目目录后 `require('./html2pptx')` |
| 内容超出很多 | 首轮字号估算过大 | 参考字号表保守起调 |
