# 运行环境

## 依赖

本技能使用 Python 提取 PDF 与处理 PPTX 包，使用 JavaScript 的 Artifact Tool 创建原生幻灯片对象。运行时软件不随技能分发。

| 组件 | 用途 | 本次验证版本 |
|---|---|---|
| Python | 管线、图像和 OOXML | 3.12 |
| PyMuPDF | 文字、路径、图片、页面渲染 | 1.26.6 |
| Pillow | 原始图片、裁切与 alpha | 13.0.0 |
| NumPy | 蒙版与像素覆盖核对 | 2.3.5 |
| lxml | OOXML 分组与结构检查 | 6.1.1 |
| Node.js | Artifact Tool 构建 | 24.22.3 |
| @oai/artifact-tool | 原生可编辑形状与文本 | 2.8.194 |
| @napi-rs/canvas | 文字宽度测量 | 0.1.100 |
| Presentations 技能校验工具 | 格式、布局与导入检查 | 运行环境提供 |
| LibreOffice | 可选的实际渲染 | 运行环境提供 |

`requirements.txt` 声明 Python 依赖范围。已有受管理运行环境优先使用其依赖，不重新安装或覆盖。普通独立环境可在虚拟环境中运行 `python -m pip install -r requirements.txt`；这不提供 Artifact Tool 或 Presentations 校验工具。不要假设这两项可从公共 npm 安装，也不要自动寻找非官方替代包。

## 路径发现

优先读取以下变量：

- `CODEX_PRIMARY_RUNTIME_PYTHON`：运行时 Python。
- `CODEX_PRIMARY_RUNTIME_NODE`：运行时 Node。
- `CODEX_PRIMARY_RUNTIME_NODE_MODULES`：运行时 Node 包目录。
- `CODEX_PRIMARY_RUNTIME_ROOT`：运行时根目录。
- `PDF_PPT_PRESENTATIONS_SKILL_ROOT`：可选，显式指定 Presentations 技能目录；其中应含 `container_tools/artifact_tool_utils.mjs` 及两个校验脚本。

没有显式 Presentations 路径时，默认在 `${CODEX_HOME:-$HOME/.codex}/skills/builtins/presentations` 发现它。脚本不会写入这些运行时目录。字体通过系统字体发现或构建器配置注册；实际文本使用的字体名会写入对象索引。

## 调用

从任意工作目录执行技能脚本即可，无须在技能目录生成输出。例如：

```bash
SKILL_DIR="/path/to/reconstruct-pdf-figures"
python3 "$SKILL_DIR/scripts/convert.py" doctor
python3 "$SKILL_DIR/scripts/convert.py" inspect --input "paper-figure.pdf" --page 1 --work-dir "inspection"
python3 "$SKILL_DIR/scripts/convert.py" build --input "paper-figure.pdf" --page 1 --work-dir "build-v1" --output "deliverables/figure.pptx" --render
```

`doctor` 仅检查依赖，不联网、不安装软件。`build` 缺少必需依赖时在提取前终止。`--render` 需要 `soffice` 或 `libreoffice` 在 PATH 中。将 `build-v1` 和 `deliverables` 设为同级目录，避免私有校验记录进入交付目录；不符合时脚本会在提取前报错。

## 已知边界

- 每次提取一个页面，生成一页 PPT；不将尺寸不同的 PDF 页面强行塞入同一个固定比例的演示文稿。
- 当前入口拒绝带页面旋转元数据的 PDF，避免提取坐标与可视尺寸不一致。先生成可视方向一致的规范化副本，再检查。
- 密码 PDF 需要使用有权访问的解密副本。
- 图形很多的页面生成的 PPT 对象也很多。按模块分组方便编辑，但不能保证所有演示软件的交互性能一致。
- 字体不可用时进行明确记录的替代，并在最终渲染中检查；中文页面应选择覆盖中文字形的字体映射。

## 小型端到端检查

在具备上述依赖的环境中执行：

```bash
python3 -m unittest discover -s "$SKILL_DIR/tests" -v
```

测试使用临时生成的 PDF，不需要用户的论文文件。它验证输入与输出路径独立、源尺寸、文字与矢量保留、页码选择和覆盖保护。完整科研图的像素与排版还需要按 `SKILL.md` 做视觉检查。
