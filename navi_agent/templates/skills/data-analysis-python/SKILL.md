---
name: data-analysis-python
description: "使用 Python 进行数据分析的完整工作流：数据加载、探索性分析、统计检验、机器学习建模、特征工程、超参数调优、可视化。当用户要求分析数据集（CSV/Excel）、做统计分析、建预测模型、画图表时使用。也适用于审查/修复用户提供的数据分析脚本、将现有脚本适配到非交互式环境运行。触发词：数据分析、统计分析、预测模型、机器学习、pandas、sklearn、相关性分析、卡方检验、方差分析、数据可视化、EDA。"
---

# Python 数据分析工作流

## 用户偏好

- **先建 venv 再装包**：用户明确要求先创建虚拟环境，再在其中安装依赖。不要直接 `pip install` 到系统环境。用户偏好 `python -m venv venv`。
- **质疑模型准确率是合理的**：当交叉验证准确率低于 75% 时，主动分析原因并提出改进方案。
- **先调查数据完整性再动手**：拿到数据集后，先确认数据量是否完整，再开始分析。

## 标准 Pipeline

```
1. 环境准备 → 2. 数据加载 → 3. EDA → 4. 统计检验 → 5. 特征工程 → 6. 建模 → 7. 调优 → 8. 可视化 → 9. 报告
```

### 1. 环境准备

```bash
python -m venv venv
source venv/Scripts/activate  # Windows Git Bash
pip install pandas numpy scikit-learn matplotlib seaborn scipy
# 可选：pip install xgboost lightgbm
```

### 2. 数据加载与初步探索

```python
import pandas as pd
df = pd.read_csv('data.csv')
print(f'形状: {df.shape}')
print(f'列名: {list(df.columns)}')
print(f'缺失值:\n{df.isnull().sum()}')
print(df.describe())
```

### 3. 探索性数据分析 (EDA)

- 分类变量：`value_counts()` + 卡方检验
- 数值变量：`describe()` + 分组箱线图
- 类别分布不均衡检查：目标变量各类别比例

### 4. 统计检验

| 场景 | 方法 | 代码 |
|------|------|------|
| 分类 vs 分类 | 卡方检验 | `scipy.stats.chi2_contingency(ct)` |
| 数值 vs 分类(2组) | t检验 | `scipy.stats.ttest_ind(g1, g2)` |
| 数值 vs 分类(3+组) | ANOVA | `scipy.stats.f_oneway(*groups)` |
| 数值 vs 数值 | Pearson/Spearman | `df.corr()` |

### 5. 特征工程

```python
# 交互特征、分箱、多项式、对数变换、领域知识特征
df['A_x_B'] = df['A'] * df['B']
df['age_group'] = pd.cut(df['age'], bins=[0,40,60,100], labels=[0,1,2]).astype(int)
df['age_log'] = np.log1p(df['age'])
```

### 6. 建模

```python
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, stratify=y, random_state=42)
model = RandomForestClassifier(class_weight='balanced')
skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
cv_scores = cross_val_score(model, X_train, y_train, cv=skf, scoring='accuracy')
```

### 7. 准确率不够时的改进顺序

1. 特征工程 → 2. 类别平衡 → 3. 更强模型(XGBoost) → 4. 超参调优 → 5. 简化任务

## 审查/修复用户提供的脚本

当用户提供现成的数据分析脚本要求运行时，按以下清单逐项检查：

### 快速检查清单

1. **数据文件路径**：脚本中的路径是否指向实际存在的文件？用 `ls` 或 `head` 验证。
2. **缺失依赖**：先 `python -c "import xxx"` 检查，缺什么装什么。
3. **matplotlib 后端**（关键）：CLI 环境下必须切换为非交互后端，否则 `plt.show()` 会阻塞。
4. **代码逻辑缺陷**：缩进错误、变量未定义、重复代码块、被截断的代码。

### matplotlib 非交互式环境适配

在 CLI/脚本模式下运行含 `plt.show()` 的代码，必须做以下修改：

```python
# 1. 文件顶部、import matplotlib.pyplot 之后立即设置后端
import matplotlib
matplotlib.use('Agg')  # 必须在 plt 导入之后、任何 plt 调用之前

# 2. 将所有 plt.show() 替换为 plt.savefig() + plt.close()
# 带计数器的模式：
plot_counter = 0
# ...绘图代码...
plot_counter += 1
plt.savefig(f'plot_{plot_counter}.png', dpi=150, bbox_inches='tight')
plt.close()
print(f'>>> 图表已保存: plot_{plot_counter}.png')
```

**常见错误模式**：用户脚本直接用 `plt.show()`，在非交互环境中会无限等待。替换为 `savefig` + `close` 即可。

### 常见陷阱

1. **类别编码顺序不一致**：LabelEncoder 映射顺序可能不符合业务逻辑，检查 `le.classes_`
2. **数据泄露**：StandardScaler 要在 train_test_split 之后 fit，只 transform test
3. **样本量太小**：<500 样本时复杂模型容易过拟合，优先用简单模型
4. **忘记 stratify**：分类任务的 train_test_split 必须加 `stratify=y`
5. **索引断裂**：异常值清洗后忘记 `reset_index(drop=True)`，导致后续 train_test_split 报错
