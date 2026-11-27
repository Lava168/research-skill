---
name: reconstruct-pdf-figures
description: Use when a user asks to deconstruct a PDF scientific figure into an editable PowerPoint at the original page size, including portrait pages, native text, heatmaps, statistical plots, microscopy panels, and independently editable components. Also use for 原尺寸PDF转PPT、科研图解构、竖版可编辑PPT. A bitmap-only PDF requires separate manual reconstruction.
---

# PDF 科研图原尺寸转可编辑 PPT

将一个 PDF 页面重建为一页同尺寸 PPT。保留文字为文本框、原生路径为矢量形状、照片为独立图片，并按配置分组。以原始页面物理尺寸为准：**1 PDF pt = 12,700 EMU = 4/3 CSS px**。纵向页面自然得到竖版 PPT。

## 运行前

1. 读取当前环境适用的 PDF 与 Presentations 技能要求。将本技能所在目录记作 `SKILL_DIR`，不要假定安装目录名。
2. 运行依赖检查。使用运行环境提供的 Python、Node、Artifact Tool 和 Presentations 校验工具；缺少依赖时按 [运行环境](references/runtime.md) 处理。

```bash
python3 "$SKILL_DIR/scripts/convert.py" doctor
```

3. 将输入 PDF、构建工作目录和交付目录分开，例如 `build-v1/` 与 `deliverables/` 为同级目录。私有检查记录不能放进交付目录或其子目录。使用新的工作目录与输出文件名；脚本拒绝覆盖已有输出。输入 PDF 保持不变。

## 执行流程

### 1. 查看原页和对象清单

```bash
python3 "$SKILL_DIR/scripts/convert.py" inspect \
  --input "input.pdf" --page 1 --work-dir "inspection"
```

查看 `inspection/source.png` 与 `inventory.json`，核对页面尺寸、字体、图片变换、路径和裁切。页码从 1 开始，每次只处理一页。扫描图只能保留图片，不能据此声称已得到可编辑文字或统计图。

### 2. 准备配置

普通页面可使用 `assets/default.json`。需要面板分组、图片拼图拆分、复杂阴影、局部栅格化或字体微调时，复制默认配置并按 [配置规范](references/configuration.md) 填写。

配置原则：

- 所有矩形均使用 PDF 页面左上角为原点的 pt 坐标。
- 直接从 PDF 提取照片，保留原始像素；按整数像素切割拼图，检查覆盖完整、无重复。
- 同时考虑图像软蒙版和页面裁切。透明边缘出现灰框时，先核对 alpha；只有背景明确固定且无叠加风险时才按配置合成到背景色。
- 用局部 `raster_regions` 保存难以准确重建的渐变/混合艺术图，保留其文字为独立对象。不要为了绕过难点把整页变成截图。
- 通过文本规则处理字体替换引起的基线与显著性星号间距；保留上下标和旋转标签。
- 查看 `extraction_report.json` 中的不支持项。对复杂填充规则、遮罩、混合模式和变换进行局部处理与目视复核。

### 3. 构建并渲染

```bash
python3 "$SKILL_DIR/scripts/convert.py" build \
  --input "input.pdf" --page 1 \
  --config "figure-config.json" \
  --work-dir "build-v1" \
  --output "deliverables/figure-editable.pptx" --render
```

无特殊配置时省略 `--config`。`--render` 使用 LibreOffice 输出预览；不能运行 LibreOffice 时，使用其他可用的实际 PPT 渲染器完成目视检查。

内部阶段按顺序运行：`extract_pdf.py` → `build_pptx.mjs` → `group_objects.py` → `validate_pptx.py` → `finalize_pptx.mjs`。详见 [重建与检查](references/workflow.md)。

### 4. 检查后交付

检查源图与 `build-v1/preview.png` 的整页和关键局部：文字完整性、上下标、热图、点位、比例尺、图片裁切、星号与括号、各面板层级。结构校验通过不等于视觉一致。

核对 `verification.json`、`grouping_report.json`、`finalization.json` 与 `delivery.json`。确认源文件未改动，最终文件与已验证候选文件的 SHA-256 相同。需要修订时换工作目录和输出文件名，重新渲染受影响局部。

交付 PPT 并说明编辑范围：文字/路径可编辑；显微照片和高密度点云仍为图片组件；矢量统计图不包含原始数值表。不要声称在 Microsoft PowerPoint 中验证过，除非确实执行过。

## 已验证的科研图配置

`assets/nature-figure1.json` 保存了本流程的实用示例：a–j 分组、六张与七张拼图拆分、复杂阴影、b 区裁切及白底合成、i 区星号上移。它绑定来源 PDF 的 SHA-256，不能套用到其他 PDF。

```bash
python3 "$SKILL_DIR/scripts/convert.py" build \
  --input "your-original-figure.pdf" --page 1 \
  --config "$SKILL_DIR/assets/nature-figure1.json" \
  --work-dir "figure1-rebuild" \
  --output "deliverables/figure1-editable.pptx" --render
```

包中提供代码和配置，不包含用户原始 PDF、显微图片或运行时依赖。

## 资源

- [配置规范](references/configuration.md)：字段、坐标、图片切割、字体和原图绑定。
- [运行环境](references/runtime.md)：依赖、路径发现、运行方法与已验证版本。
- [重建与检查](references/workflow.md)：对象提取、层级、裁切、分组与校验边界。
- `tests/`：不依赖用户原始 PDF 的小型端到端检查。
