# 智能材料整理系统(Intelligent Material Sorting System, IMSS)

> 当前项目面向中科大先研院研会活动报销材料整理，暂不支持其他文件整理。

>当前仍然是半自动工具，不可避免的只靠文字提取有些文件是识别不出用途的，所以需要手动确认

> 欢迎二次开发，让工具变得更加智能和强大

离线识别发票、订单截图、行程单及 Word 材料，按模板组合生成Word 和 PDF。

## 安装与检查

使用已有 Python 3.10+ 环境安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## 如何使用

**1. 扫描原始材料。**

```powershell
python main.py scan "..\本次材料"   # 将所有报销材料放入同一文件夹，传入文件夹绝对路径
```

直接运行 `python main.py` 后输入材料路径，或在 `main.py` 顶部填写 `INPUT_DIR`，也会先扫描，等待核对。

**2. 打开 `..\核对清单\核对清单.xlsx`，核对并保存。**

填写“类型、名称、金额”；不填使用 C 列展示的识别值，填写则采用用户值；确认后在 H 列下拉选择“已校验”。

**3. 在审查文件夹 `..\核对清单` 中填写 `detail.json`。**

按“物品、奖品、工作餐、行程、快递”这几种发票类型，填写承诺人的 `signature_image`（签名图片路径）、`student_id`（学号）、`phone`（联系方式）。
同类别发票共用这些信息，未填写的类别不填充。签名图片路径若填写相对路径，则以 `detail.json` 所在目录为准。

行程类另填 `purpose`（乘车事由），`passengers` 是乘车人列表，每人一个对象，填写 `name`、`student_id`、`signature_image`；内置行程表支持最多三人。
格式及示例见 [使用说明](docs/使用说明.md#人员用途和奖品状态)，空白示例见 [details.example.json](details.example.json)。

**4. 保存 JSON 和 Excel，关闭 Excel 后生成。**

```powershell
python main.py build "..\核对清单\核对清单.xlsx"
```

程序自动读取同目录的 `detail.json`，并将对应类别的信息填入报销材料。只处理 Excel 中“已校验”的材料，输出到“报销材料”文件夹。

## 固定输出结构

```text
├── 报销材料\                         # 与输入同级，固定名称
│   ├── 原始材料\                     # 重命名后的报销附件
│   │   ├── 01.打印纸-35.80元-电子发票.pdf
│   │   └── 01.打印纸-35.80元-购买记录.jpg
│   │   └── ...
│   ├── 00.某项目审查表.pdf
│   ├── 00.某活动新闻稿.pdf
│   ├── 01.打印纸_35.80.docx
│   ├── 01.打印纸_35.80.pdf
│   ├── 02.某活动签领表.pdf
│   └── 03.某活动签到表.pdf
│   └── ...
└── 核对清单\                         # 与输入同级，固定名称
    ├── 核对清单.xlsx                  # 用户修改；诊断在 build 后刷新
    ├── detail.json                    # 按类别填写签名、学号、联系方式及乘车人
    └── 核对清单.json                  # 扫描文字、原件摘要、设置及生成报告,系统维护，无需手动修改
```

## 常用选项

```powershell
# 明确配对的材料可一次扫描并生成；疑似配对仍等待确认
python main.py run "D:\报销\本次材料"

# 使用自己的模板、业务信息和统一承诺人信息
python main.py build "D:\报销\核对清单\核对清单.xlsx" --template "D:\模板.docx" --student-id "SA12345678" 
--phone "13800000000" --signature-image "D:\签字.png"

# 暂时只生成 Word；之后补导出 PDF
python main.py build "D:\报销\核对清单\核对清单.xlsx" --skip-pdf
python main.py export-pdf "D:\报销\报销材料"
```

统一签名、学号和联系方式也可在 `main.py` 顶部配置。按类别的人员信息、逐笔用途及奖品勾选见 [详细使用说明](docs/使用说明.md)。
未配置用途时“物品名称（用途）”使用清单物品名，真实用途可用 details 覆盖；其他未提供的人员及签字字段留空。

## 项目结构与二次开发

```text
main.py                        唯一入口：路径配置、参数、交互与环境检查
imss/
├── core/                      业务数据、配对和编号规则
│   ├── models.py              Material / Bundle / Settings 与金额校验
│   ├── matching.py            明确匹配 → 金额候选 → 完整性检查
│   ├── candidates.py          多证据候选排序与争用检查
│   └── numbering.py           连续编号
├── recognition/               文件提取与规则识别
│   ├── extract.py             文件头、PDF/Word/图片、OCR
│   ├── rules.py               类型判断、正文清理与识别编排
│   ├── fields.py              商品区域、实付金额与订单号提取
│   ├── layout.py              坐标视觉行、字段位置与证据
│   └── refine.py              支付/费用区域局部数字 OCR
├── output/                    Word 与 Excel 输出
│   ├── documents.py           模板填充、附件布局、签字图片
│   ├── review.py              彩色核对表、人工修改读取与诊断刷新
│   ├── candidate_review.py    候选工作表、批量确认与冲突检查
│   └── workspace.py           固定目录约定
├── runtime/                   本地组件定位与进程调用
│   ├── paths.py               vendor 定位和边界校验
│   └── office.py              LibreOffice 隔离转换
├── resources/templates/       五类单页模板与承诺图片
├── storage.py                 JSON、SHA-256 与原件校验
└── workflow.py                扫描与生成流程编排
tests/                         业务、核对闭环、输出与真实组件测试
tools/                         开发工具与离线评测，不参与用户入口
docs/                          使用、开发和测试说明
vendor/                        OCR、Office 及其许可证
```

类型判断在 `recognition/rules.py`；更改配对在 `core/matching.py`；更改清单在 `output/review.py`；更改模板布局在 `output/documents.py`。
更详细的调用关系和修改约定见 [开发说明](docs/开发说明.md)。

订单识别现在按正文结构提取：没有“商品名称”标签时检查商品标题附近的价格和数量，排除店铺、规格及推荐商品。金额优先读取实际支付字段，明确标注的优惠不计为实付；
多个实付值冲突时留空，只有应付款、行程页面费用或疑似缺小数点时保留核对提示。支付账单也支持顶部支出金额和换行的订单编号。

OCR 保留文字框、原始置信度和页码，按视觉行补全分散字段；首轮文字过少或置信度低时，增加一次稀疏版式识别并择优。金额漏读时按支付/费用标签定位区域做数字 OCR，
补出的金额必须人工核查。 已经漏读成空白或严重乱码的文字不能靠规则恢复；商品长标题与发票简称也可能需要在核对表中统一。

### 进行单元测试

```powershell
python -m unittest discover -s tests -v
```

测试材料与输出可能含个人信息，本地私有语料不进入公开仓库。

项目代码使用 MIT 协议，第三方组件保留各自许可。
