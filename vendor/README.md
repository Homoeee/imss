# 项目内 OCR 与 Office 组件

当前工作副本已内置 Windows x64 组件：Tesseract 5.5.3.20260724、`tessdata_fast` 4.1.0 简体中文和英文模型、LibreOffice 26.2.6.3。Python 使用项目中已配置的解释器，不附带或切换 Python。

```text
vendor/
├── tesseract/
│   ├── tesseract.exe
│   ├── …依赖 DLL、工具、文档和许可证…
│   └── tessdata/
│       ├── chi_sim.traineddata
│       ├── eng.traineddata
│       └── LICENSE
├── libreoffice/
│   ├── program/
│   │   ├── soffice.com
│   │   ├── soffice.exe
│   │   └── …应用 DLL 与随包 VC++ 运行库…
│   └── …完整资源、字体和许可证…
└── SOURCES.json
```

组件按项目位置定位，与终端当前目录无关。程序不搜索系统 PATH 或系统常见安装位置，不使用 Microsoft Word。缺少组件或语言模型时明确报错，运行时不会联网下载。自定义 `tesseract`、`tessdata`、`soffice` 路径必须位于本项目 `vendor/` 内；相对路径以项目根目录为准。`office_backend` 默认 `libreoffice`，旧值 `auto` 也只使用项目内 LibreOffice。

## 组件来源与校验

[SOURCES.json](SOURCES.json) 记录版本、官方下载链接与 SHA-256 校验值。组件通过下载安装包后提取获得，无需修改系统 PATH 或安装应用到系统目录。

- Tesseract：来自 [Tesseract 官方 Release](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.3)，采用 [UB Mannheim Windows 构建](https://github.com/UB-Mannheim/tesseract/wiki)。保留完整依赖和 `doc/` 文档，主程序许可见 `tesseract/doc/LICENSE`。
- OCR 模型：来自 [tessdata_fast 4.1.0](https://github.com/tesseract-ocr/tessdata_fast/tree/4.1.0)，许可见 `tesseract/tessdata/LICENSE`。
- LibreOffice：来自 [Document Foundation 官方下载目录](https://download.documentfoundation.org/libreoffice/stable/26.2.6/win/x86_64/)。使用 MSI 的 `/a` 管理提取模式获得完整程序文件；将包内 `System64/` 的 VC++ DLL 复制到 `program/`，避免依赖系统安装的 VC++ 运行库。保留 `LICENSE.html`、`license.txt`、`NOTICE` 等随包文件，对应源码可从 [26.2.6 源码目录](https://download.documentfoundation.org/libreoffice/stable/26.2.6/src/) 获取。
- 解包使用 [7-Zip](https://www.7-zip.org/)，仅准备组件时需要，日常运行不需要。

IMSS 的 MIT 许可不替代第三方组件的许可。上述 Windows 文件不能作为 Linux/macOS 通用包。

## 检查和分发

使用项目指定的 Python 解释器执行：

```powershell
python main.py doctor
python -m unittest discover -s tests -v
```

移动或发布项目时，请完整携带 `tesseract/`、`libreoffice/` 及本目录中的说明、来源信息。Git 默认忽略二进制大文件，因此仅下载 Git 源码不包含这些组件；完整发布包应将它们另行打包，不能只复制 `.exe`。
