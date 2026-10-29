"""唯一程序入口：路径配置、命令参数与交互提示集中于此。"""
INPUT_DIR = ""
TEMPLATE = ""
SIGNATURE_IMAGE = ""  # 开票人签字图片路径；留空则不填签名。
STUDENT_ID = ""       # 学号，例如 "SA12345678"。
PHONE = ""            # 联系方式。
PASSENGER_NAME = ""   # 行程模板的第 1 位乘车人姓名；不同人员可用 --details 逐组填写。


import argparse
import json
import logging
from pathlib import Path
import sys

from imss.output.documents import create_template
from imss.recognition.extract import tesseract_path
from imss.core.models import Settings
from imss.runtime.office import run_process, soffice_path
from imss.workflow import build, scan, sync_pdf_report, recheck, update_review
from imss.runtime.paths import tessdata_path
from imss.output.workspace import default_output


def load_settings(path):
    return Settings.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig"))) if path else Settings()


def doctor(settings: Settings) -> bool:
    healthy = True
    try:
        path = tesseract_path(settings)
        args = [path, "--list-langs"]
        tessdata = tessdata_path(settings)
        if tessdata:
            args.extend(["--tessdata-dir", str(tessdata)])
        result = run_process(args, 20)
        available = set(result.stdout.decode("utf-8", errors="replace").splitlines()[1:])
        missing = set(settings.languages.split("+")) - available
        if result.returncode or missing:
            raise RuntimeError("缺少 OCR 语言模型: " + ", ".join(sorted(missing)))
        print(f"[OK] Tesseract: {path}；模型: {settings.languages}")
        if tessdata:
            print(f"     模型目录: {tessdata}")
    except Exception as exc:
        healthy = False
        print(f"[缺失] {exc}")
    try:
        path = soffice_path(settings)
        result = run_process([path, "--headless", "--version"], 20)
        if result.returncode:
            raise RuntimeError("LibreOffice 无法运行")
        print(f"[OK] LibreOffice: {path}")
    except Exception as exc:
        healthy = False
        print(f"[缺失] {exc}")
    print("处理过程完全离线；OCR 和 Office 仅使用项目 vendor 内组件。")
    return healthy


def main(argv=None, person_defaults=None):
    """唯一入口：scan 识别核对，build 读取修改，run 供明确匹配批量处理。"""
    configured = {key: value for key, value in {'student_id': STUDENT_ID, 'phone': PHONE,
                                               'passenger_name': PASSENGER_NAME}.items() if value}
    if SIGNATURE_IMAGE:
        signature = Path(SIGNATURE_IMAGE).expanduser()
        if not signature.is_absolute():
            signature = Path(__file__).resolve().parent / signature
        configured['signature_image'] = str(signature.resolve())
    person_defaults = {**configured, **(person_defaults or {})}
    parser = argparse.ArgumentParser(description="离线报销材料整理，不调用任何大模型或云端 API")
    sub = parser.add_subparsers(dest="command")
    for name in ("scan", "run"):
        command = sub.add_parser(name, help="清空旧输出并重新扫描、导出核对表" if name == "scan" else "清空旧输出、扫描并生成 Word/PDF")
        command.add_argument("input", type=Path)
        command.add_argument("--template", type=Path)
        command.add_argument("--config", type=Path)
        command.add_argument("--details", type=Path, help="按发票号或发票原文件名称提供用途、乘车人等信息（仅 run 使用）")
        if name == "run":
            command.add_argument("--review", type=Path, help="应用已确认的核对清单（xlsx，兼容 CSV）")
            command.add_argument("--skip-pdf", action="store_true")
            command.add_argument("--start", type=int, default=1, help="报销单连续整数编号起点，默认 01；审查表和新闻稿均为 00")
            command.add_argument("--signature-image", type=Path, help="开票人签字图片；按比例插入模板签名栏")
            command.add_argument("--student-id", help="默认学号；逐组 details 可覆盖")
            command.add_argument("--phone", help="默认联系方式；逐组 details 可覆盖")
            command.add_argument("--passenger-name", help="默认第 1 位乘车人姓名；逐组 details 可覆盖")
    command = sub.add_parser("build", help="读取修改并保存的核对清单.xlsx，重新校验后生成材料")
    command.add_argument("manifest", type=Path, metavar="核对清单.xlsx",
                         help="修改并保存的 Excel 核对清单；兼容旧 JSON 入口，扫描记录由程序自动读取")
    command.add_argument("--review", type=Path, help="旧 JSON 入口可指定核对表；直接传入 Excel 时无需此参数")
    command.add_argument("--template", type=Path)
    command.add_argument("--config", type=Path, help="覆盖扫描时的运行配置")
    command.add_argument("--details", type=Path, help="逐笔信息 JSON，键为发票号或 A 列发票原文件名称；参考 details.example.json")
    command.add_argument("--skip-pdf", action="store_true")
    command.add_argument("--start", type=int, default=1, help="报销单连续整数编号起点，默认 01")
    command.add_argument("--signature-image", type=Path)
    command.add_argument("--student-id")
    command.add_argument("--phone")
    command.add_argument("--passenger-name", help="默认第 1 位乘车人姓名；逐组 details 可覆盖")
    command = sub.add_parser("recheck", help="重新识别已有批次，保留核对修改并刷新多证据候选")
    command.add_argument("manifest", type=Path)
    command.add_argument("--config", type=Path)
    command = sub.add_parser('review', help='按已有识别结果升级八列核对表并修复原件链接，不重跑OCR')
    command.add_argument('manifest', type=Path)
    command = sub.add_parser("doctor", help="检查本地 OCR、中文模型和 LibreOffice")
    command.add_argument("--config", type=Path)
    command = sub.add_parser("init-template", help="生成可编辑的示例 .docx 模板")
    command.add_argument("path", type=Path)
    command = sub.add_parser("export-pdf", help="补导出已有 Word，保留同名 PDF")
    command.add_argument("directory", type=Path)
    command.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if not args.command:
        entered = input("请输入材料文件夹路径: ").strip().strip('"')
        if not entered:
            parser.print_help()
            return 1
        arguments = ["scan", entered]
        if TEMPLATE:
            arguments.extend(["--template", TEMPLATE])
        return main(arguments, person_defaults=person_defaults)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        settings = load_settings(getattr(args, "config", None))
        if args.command == 'review':
            path = update_review(args.manifest)
            print(f'核对清单已更新：{path}；类型、名称、金额留空沿用识别值；仅已校验材料参与build。')
            return 0
        if args.command == 'recheck':
            result = recheck(args.manifest, settings if args.config else None)
            print(f"核对清单已更新，共 {result['materials']} 份材料；保留 {result['confirmed']} 份已校验材料及手工填写。")
            print(f"请核查 {args.manifest.parent / '核对清单.xlsx'}，修改类型、名称、金额并选择已校验后执行 build。")
            return 0
        if args.command == "doctor":
            return 0 if doctor(settings) else 2
        if args.command == "init-template":
            create_template(args.path)
            print(f"模板已生成: {args.path.resolve()}")
            return 0
        if args.command == "export-pdf":
            from imss.runtime.office import convert_office
            if not args.directory.is_dir():
                raise ValueError("目录不存在")
            paths = sorted(args.directory.glob("*.docx"))
            if not paths:
                raise ValueError("目录没有 .docx 文件")
            failed = 0
            for path in paths:
                if path.with_suffix(".pdf").exists():
                    with path.with_suffix('.pdf').open('rb') as stream:
                        valid = stream.read(5) == b'%PDF-'
                    if valid:
                        print(f"保留已有 PDF: {path.with_suffix('.pdf').name}")
                    else:
                        failed += 1
                        print(f"[失败] 已有同名文件不是有效 PDF，请移走后重试: {path.with_suffix('.pdf').name}", file=sys.stderr)
                    continue
                try:
                    convert_office(path, path.with_suffix(".pdf"), settings)
                    print(f"已导出: {path.with_suffix('.pdf').name}")
                except Exception as exc:
                    failed += 1
                    print(f"[失败] {path.name}: {exc}", file=sys.stderr)
            sync_pdf_report(args.directory)
            return 2 if failed else 0
        if args.command in ("scan", "run"):
            manifest = scan(args.input, default_output(args.input), settings, args.template)
            print(f"核对清单: {manifest.parent / '核对清单.xlsx'}")
            print(f'修改并保存、关闭 Excel 后执行：python main.py build "{manifest.parent / "核对清单.xlsx"}"')
            if args.command == "scan":
                return 0
        else:
            manifest = args.manifest
        defaults = dict(person_defaults or {})
        for name in ('signature_image', 'student_id', 'phone', 'passenger_name'):
            value = getattr(args, name, None)
            if value is not None:
                defaults[name] = str(value.resolve()) if isinstance(value, Path) else value
        report = build(manifest, review=getattr(args, "review", None), template=args.template,
                       skip_pdf=args.skip_pdf, start=args.start,
                       settings_override=settings if args.command == "build" and args.config else None,
                       details_path=args.details, person_defaults=defaults)
        words = sum(bool(value.get('word')) for value in report['generated'])
        pdfs = sum(bool(value.get('pdf')) for value in report['generated'])
        print(f"完成: Word {words} 份，PDF {pdfs} 份；{len(report['pending'])} 条待处理记录；输出: {report['output']}")
        print(f"处理记录: {manifest.parent}；原输入文件保留。")
        for entry in report['pending']:
            print('[待处理] ' + str(entry.get('source') or entry.get('group')) + '：' + '；'.join(entry['issues']))
        if report['pending']:
            review_file = Path(report['review_file'])
            state_path = manifest.parent / '核对清单.json' if manifest.suffix.lower() == '.xlsx' else manifest
            excel_entry = (review_file.suffix.lower() == '.xlsx'
                           and state_path.resolve() == (review_file.parent / '核对清单.json').resolve())
            command = (f'python main.py build "{review_file}"' if excel_entry
                       else f'python main.py build "{manifest}" --review "{review_file}"')
            print(f'修改核对清单后可重复执行：{command}')
        return 2 if report["pending"] else 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1



if __name__ == "__main__":
    if len(sys.argv) == 1 and INPUT_DIR:
        args = ["scan", INPUT_DIR]
        if TEMPLATE:
            args.extend(["--template", TEMPLATE])
        raise SystemExit(main(args))
    raise SystemExit(main())
