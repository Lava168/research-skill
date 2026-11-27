# 配置规范

## 坐标与来源

使用 JSON。`schema_version` 为 1，页码从 1 开始。所有 `bbox` 均为 PDF 左上角坐标 `[x0,y0,x1,y1]`，单位 pt；图片 `crop_px` 为原始像素坐标。默认配置不含本次科研图的特定矩形或 xref。

读取 `inspect` 生成的 `inventory.json` 获取页面尺寸、源文件 SHA-256、图片 xref 和变换。xref 仅在当前 PDF 内有意义；重新导出 PDF 后可能变化。设置 `tiles_by_xref`、`clips_by_xref` 或特殊阴影时必须绑定 `source.sha256`，防止对另一文件误用坐标。

| 字段 | 作用 |
|---|---|
| `schema_version` | 当前为 1 |
| `source.sha256` | 可选；使用图片 xref 特定操作时必需 |
| `source.page_number` | 配置对应的页码 |
| `source.width_pt` / `height_pt` | 核对页面尺寸，不用于强行缩放输入 |
| `background` | 幻灯片背景，如 `#FFFFFF` |
| `panels` | 面板 ID → `{bbox, name}`；按对象中心归属 |
| `raster_regions` | 需局部栅格化的复杂区域列表 |
| `images` | 图片命名、切割、裁切和特殊阴影设置 |
| `text` | 字体映射、基线和局部位置修正 |
| `grouping.enabled` | 默认为 true；false 时保留平面对象 |
| `expected` | 已验证页面的对象数量，用于回归检查 |

## 面板和局部艺术图

```json
{
  "schema_version": 1,
  "background": "#FFFFFF",
  "panels": {
    "a": {"bbox": [30, 40, 280, 220], "name": "a｜研究流程"},
    "b": {"bbox": [300, 40, 560, 220], "name": "b｜统计结果"}
  },
  "raster_regions": [
    {"name": "complex-artwork", "bbox": [60, 90, 110, 130], "seqno": 35, "dpi": 432, "remove_text": true}
  ]
}
```

示例坐标仅用于展示字段，不应直接套用。`seqno` 是区域插入时的绘制顺序，应根据源对象清单确认。局部区域替换会省略该区域内的原生艺术图对象，不能选得过大或跨过其他面板。`remove_text:true` 保持文字独立；图像本身仍为位图。

## 图片

`images.names_by_xref` 为图片命名。`tiles_by_xref` 指定一张拼图的所有分块；分块必须完整覆盖原图且没有重复。`clips_by_xref` 将原 PDF 的页面裁切映射到图片像素 alpha。

```json
{
  "images": {
    "names_by_xref": {"42": "microscopy"},
    "tiles_by_xref": {
      "42": [
        {"name": "microscopy-left", "crop_px": [0, 0, 500, 400]},
        {"name": "microscopy-right", "crop_px": [500, 0, 1000, 400]}
      ]
    },
    "clips_by_xref": {
      "43": {"bbox": [300, 50, 500, 210], "flatten_background": "#FFFFFF"}
    }
  }
}
```

`flatten_background` 可省略以保留透明度。使用它时保留内部 RGBA 原件，最终输出固定背景色的 RGB 图片。先确认图片位于该颜色背景上，不会覆盖其他内容。

复杂灰度 Multiply 阴影另有 `images.shadows`，值为 `{form_xref, form_name, page_content_xref, graphics_state}`。该分支用于可由黑色 alpha 表示的灰度阴影，依赖实际 PDF Form 结构；不要推测资源名。参考经过绑定的 `assets/nature-figure1.json`，其他文件优先采用可检查的局部栅格区域。

## 字体和文字修正

```json
{
  "text": {
    "baseline_factor": 0.994,
    "font_map": [
      {"pattern": "Harding|Times", "family": "Times New Roman"},
      {"pattern": ".*", "family": "Arial"}
    ],
    "adjustments": [
      {"panel": "i", "regex": "^\\*+$", "dy_pt": -0.75}
    ]
  }
}
```

按顺序匹配 PDF 字体名，使用首个符合的 `font_map` 项。`adjustments` 按面板和文本正则匹配；负 `dy_pt` 向上，正值向下，也可设置 `dx_pt`。示例星号修正来自 Arial 与源字体的字形差异；新 PDF 应根据渲染决定是否使用。

需要精确字体文件时，设置 `text.font_files` 为路径列表或 `{path,family}` 列表。路径相对配置文件解析。仅使用有权使用的字体；字体文件不随本技能分发。中文页面应选择覆盖中文字符的已安装字体，并检查字符宽度。

## 回归配置

`assets/nature-figure1.json` 绑定本次原始 PDF，预期为 `text:417`、`vector:2121`、`image:23`、`groups:10`。代码通过外部配置读取这些值，主程序不固定 a–j、原图文件名或原工作目录。

默认配置不要求这些数量。若新文件包含只有位图的统计图，不应伪造相同数量的对象，也不要把自动检查通过当作已完成可编辑重建。
