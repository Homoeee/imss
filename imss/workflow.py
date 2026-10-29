"""应用流程：扫描→核对→重新校验→生成；原件全程只读。"""
from datetime import datetime
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import shutil
import tempfile

from imss.output.documents import build_document, validate_signature
from imss.recognition.extract import IMAGE_EXTENSIONS, detect_extension, extract, image_pages
from imss.core.matching import group_materials
from imss.storage import digest_file, validate_sources, write_json
from imss.output.review import (apply_review, write_review, refresh_review, read_review, review_rows,
                                migrated_states)
from imss.core.rematching import group_verified_materials
from imss.core.candidates import pairing_candidates
from imss.core.models import KINDS, SPECIALS, STANDALONE_SHEETS, Bundle, Material, Settings, inside, safe_name
from imss.runtime.office import convert_office
from imss.recognition.rules import recognize
from imss.recognition.refine import refine_missing_amount
from imss.output.workspace import EVIDENCE_DIR, default_output, default_records, managed_path, validate_layout, reset_scan_directories
from imss.core.numbering import bundle_numbers

LOGGER = logging.getLogger(__name__)
STATE_NAME = '核对清单.json'


def sync_pdf_report(output: Path):
    """补导 PDF 后同步同一个状态文件，不另建报告文件。"""
    output = output.resolve()
    path = default_records(output) / STATE_NAME
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding='utf-8'))
    if Path(data['output']).resolve() != output:
        raise ValueError('核对清单不属于本次输出目录')
    report = data.get('report')
    if not report:
        return
    resolved = set()
    for entry in report['generated']:
        if entry.get('word'):
            pdf = output / Path(entry['word']).with_suffix('.pdf')
            if pdf.is_file() and pdf.read_bytes()[:5] == b'%PDF-':
                entry['pdf'] = pdf.name
                resolved.add(entry['group'])
    for entry in report['warnings']:
        if entry.get('group') in resolved:
            entry['issues'] = [issue for issue in entry['issues'] if '--skip-pdf' not in issue]
    report['warnings'] = [entry for entry in report['warnings'] if entry['issues']]
    report['pending'] = [entry for entry in report['pending'] if not (
        entry.get('group') in resolved and all('PDF 导出失败' in issue for issue in entry['issues']))]
    # 仅登记程序生成的文件，不能把用户另放的文件收编为下次可覆盖成果。
    managed = set(report.get('files', {}))
    managed.update(entry['pdf'] for entry in report['generated'] if entry.get('pdf'))
    managed_files = {name: managed_path(output, name) for name in managed}
    report['files'] = {name: digest_file(file_path) for name, file_path in managed_files.items()
                       if file_path.is_file()}
    write_json(path, data)


def scan(root: Path, output: Path | None, settings: Settings, template: Path | None = None,
         records_output: Path | None = None) -> Path:
    """校验输入后清空旧输出，重新扫描原件并生成核对清单；原件只读。"""
    root = root.resolve()
    output = (output or default_output(root)).resolve()
    records_output = (records_output or default_records(output)).resolve()
    if not root.is_dir():
        raise ValueError(f'输入目录不存在: {root}')
    validate_layout(root, output, records_output)
    if template and (not template.is_file() or detect_extension(template) != '.docx'):
        raise ValueError('模板必须是存在的 .docx 文件')
    if template and any(inside(template, directory) for directory in (output, records_output)):
        raise ValueError('模板不能放在即将清空的报销材料或核对清单目录内，请先移到输入目录或其他位置')
    paths = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or not inside(path, root):
            continue
        if template and path.resolve() == template.resolve():
            continue
        if path.name.startswith(("~$", ".")) or path.suffix.lower() in (".json", ".csv", ".xlsx", ".toml", ".py", ".md"):
            continue
        paths.append(path)
    if not paths:
        raise ValueError("输入目录内没有材料（模板、配置、脚本及已有输出会被排除）")
    # 错误路径、无效模板和空输入不能触发删除；清理前再次校验固定目录边界。
    LOGGER.info('清理并重建输出目录：%s；%s', output, records_output)
    reset_scan_directories(root, output, records_output)
    records, hashes = [], {}
    def scan_one(pair):
        index, path = pair
        LOGGER.info("识别 [%s/%s] %s", index + 1, len(paths), path.relative_to(root))
        material = Material(source=path.relative_to(root).as_posix(), digest=digest_file(path), extension="")
        try:
            material.extension = detect_extension(path)
            if not material.extension:
                material.issues.append("不支持或损坏的文件，原样归档待核对")
            else:
                result = extract(path, material.extension, settings)
                material.text, material.issues = result
                material.layout = getattr(result, 'layout', [])
                recognize(material)
                try:
                    refine_missing_amount(path, material, settings)
                except Exception as exc:
                    material.issues.append(f'局部数字OCR失败: {exc}')
        except Exception as exc:
            material.issues.append(f"提取失败: {type(exc).__name__}: {exc}")
        return material
    with ThreadPoolExecutor(max_workers=settings.workers) as executor:
        extracted = list(executor.map(scan_one, enumerate(paths)))
    for material in extracted:
        if material.digest in hashes:
            material.duplicate_of = hashes[material.digest]
            material.issues.append("内容与另一份材料完全相同，仅归档一次用于组装")
        else:
            hashes[material.digest] = material.source
        records.append(material)
    # 在写清单前匹配，否则缺件和疑似配对直到生成时才会暴露。
    group_materials([m for m in records if m.kind not in STANDALONE_SHEETS])
    manifest = records_output / STATE_NAME
    write_json(manifest, {"format": 3, "input": str(root), "output": str(output), "records_output": str(records_output),
                          "template": str(template.resolve()) if template else None,
                          "settings": settings.__dict__, "materials": [m.to_dict() for m in records]})
    write_review(records_output / "核对清单.xlsx", records, root)
    return manifest


def unique_path(directory: Path, stem: str, extension: str) -> Path:
    candidate = directory / (stem + extension)
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}__{index:02d}{extension}"
        index += 1
    return candidate


def recheck(manifest_path: Path, settings_override: Settings | None = None) -> dict:
    """升级已有扫描：重读未确认原件，保留确认结果、未确认编辑及既有生成报告。

    先验证所有原件摘要，再刷新 Excel；Excel 被占用时不提交 JSON。
    本操作不重新发布报销文件，核对完成后仍由 build 统一生成。
    """
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    if data.get('format') != 3:
        raise ValueError('仅支持新版核对清单，请先 scan')
    root = Path(data['input']).resolve()
    output = Path(data['output']).resolve()
    processing = manifest_path.resolve().parent
    validate_layout(root, output, processing)
    if str(processing) != data['records_output']:
        raise ValueError('核对清单必须位于原目录')
    records = [Material(**value) for value in data['materials']]
    validate_sources(root, records)
    review = processing / '核对清单.xlsx'
    previous = read_review(review)
    states = migrated_states(records, previous)
    applied = deepcopy(records)
    mode = apply_review(review, applied)
    if mode == 'legacy':
        for original, material in zip(records, applied):
            if material.confirmed:
                states[material.source].update(confirmed='已校验', **{
                    field: getattr(material, field) if getattr(material, field) != getattr(original, field)
                    else states[material.source][field] for field in ('kind', 'item', 'amount')})
    confirmed_sources = {m.source for m in applied if m.confirmed}
    settings = settings_override or Settings.from_dict(data['settings'])
    def reread(material):
        if material.source in confirmed_sources or not material.extension or material.duplicate_of:
            return material
        LOGGER.info('重新识别 %s', material.source)
        fresh = Material(material.source, material.digest, material.extension)
        try:
            result = extract(root / fresh.source, fresh.extension, settings)
            fresh.text, fresh.issues = result
            fresh.layout = getattr(result, 'layout', [])
            recognize(fresh)
            try:
                refine_missing_amount(root / fresh.source, fresh, settings)
            except Exception as exc:
                fresh.issues.append(f'局部数字OCR失败: {exc}')
        except Exception as exc:
            fresh.issues.append(f'提取失败: {type(exc).__name__}: {exc}')
        return fresh
    with ThreadPoolExecutor(max_workers=settings.workers) as executor:
        records = list(executor.map(reread, records))
    group_materials([m for m in records if m.kind not in STANDALONE_SHEETS])
    proposals = pairing_candidates(records)
    write_review(review, records, root, states=states)
    data.update(materials=[m.to_dict() for m in records], settings=settings.__dict__)
    write_json(manifest_path, data)
    return dict(materials=len(records), candidates=len(proposals),
                unique_candidates=sum(p['status'] == '双方唯一候选' for p in proposals),
                confirmed=len(confirmed_sources))


def update_review(manifest_path: Path):
    """只升级核对表的列、状态和原件链接；不重跑 OCR 或发布报销材料。"""
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    root, output, processing = Path(data['input']).resolve(), Path(data['output']).resolve(), manifest_path.resolve().parent
    validate_layout(root, output, processing)
    if str(processing) != data['records_output']:
        raise ValueError('核对清单必须位于原目录')
    records = [Material(**value) for value in data['materials']]
    validate_sources(root, records)
    path = processing / '核对清单.xlsx'
    if path.is_file():
        rows = read_review(path)
        states = migrated_states(records, rows)
        applied = deepcopy(records)
        mode = apply_review(path, applied)
        if mode == 'legacy':
            for original, material in zip(records, applied):
                if material.confirmed:
                    states[material.source].update(confirmed='已校验', **{
                        field: getattr(material, field) if getattr(material, field) != getattr(original, field)
                        else states[material.source][field] for field in ('kind', 'item', 'amount')})
    else:
        states = None
    write_review(path, records, root, states=states)
    return path


def archive_name(material: Material) -> str:
    if material.kind in SPECIALS:
        return safe_name(material.title or material.item or Path(material.source).stem)
    if material.item and material.amount and material.kind != "未知":
        return safe_name(material.item) + "-" + material.amount + "元-" + material.kind
    if material.item and material.kind in ("签领表", "购买清单", "承诺书"):
        return safe_name(material.item) + "-" + material.kind
    return "待核对-" + safe_name(Path(material.source).stem) + "-" + material.kind


def archive_materials(records: list[Material], bundles: list[Bundle], numbering: dict[str, str],
                      root: Path, output: Path, records_output: Path):
    """复制已有报销编号的附件到“原始材料”，输入原件保持不变。"""
    numbers = {m.source: numbering[b.key] for b in bundles if b.key in numbering for m in b.materials}
    # 附件集中放在唯一子目录；未匹配原件留在输入目录，由清单链接访问。
    archive = output / EVIDENCE_DIR
    for material in records:
        material.archived = ''
        if material.kind in (*SPECIALS, *STANDALONE_SHEETS):
            continue
        number = numbers.get(material.source) or numbers.get(material.duplicate_of)
        if not number:
            continue
        archive.mkdir(exist_ok=True)
        directory = archive
        extension = material.extension or Path(material.source).suffix.lower()
        if not extension or len(extension) > 12:
            extension = '.bin'
        prefix = number + '.' if number else '待核对-'
        destination = unique_path(directory, prefix + archive_name(material), extension)
        shutil.copy2(root / material.source, destination)
        material.archived = str(destination)


def export_special(material: Material, destination: Path, settings: Settings, source: Path | None = None):
    source = source or Path(material.archived)
    if material.extension == ".pdf":
        shutil.copy2(source, destination)
    elif material.extension in IMAGE_EXTENSIONS:
        images = list(image_pages(source, settings))
        try:
            if not images:
                raise ValueError("图片没有可导出页面")
            # Explicit dpi produces a readable physical page size without resampling the source.
            images[0].save(destination, format="PDF", save_all=True, append_images=images[1:], resolution=settings.dpi)
        finally:
            for image in images:
                image.close()
    elif material.extension in (".doc", ".docx"):
        convert_office(source, destination, settings)
    else:
        raise ValueError("特殊材料格式不支持 PDF 转换")


def build(manifest_path: Path, review: Path | None = None, template: Path | None = None,
          skip_pdf: bool = False, start: int = 1, settings_override: Settings | None = None,
          details_path: Path | None = None, person_defaults: dict | None = None) -> dict:
    """以用户保存的 Excel 为核对输入，兼容旧 JSON 入口，在暂存目录生成后发布。

    可重复运行：只替换上次生成且摘要未变的文件，保留人工新增文件，
    如用户修改过成果则停止，避免重生成覆盖手工修改。
    """
    if start < 1 or start > 9999:
        raise ValueError('起始序号应为 1 到 9999；00 专用于审查表和新闻稿')
    if manifest_path.suffix.lower() == '.xlsx':
        if review and review.resolve() != manifest_path.resolve():
            raise ValueError('指定的 Excel 与 --review 不一致，请只指定一份核对清单')
        review = manifest_path
        manifest_path = review.parent / STATE_NAME
        if not review.is_file():
            raise ValueError(f'核对清单不存在: {review}')
        if not manifest_path.is_file():
            raise ValueError(f'缺少程序保存的扫描记录: {manifest_path}；请恢复扫描时生成的记录并与 Excel 放在同一目录，无需手工编辑 JSON')
    elif manifest_path.suffix.lower() != '.json':
        raise ValueError('build 请传入核对清单.xlsx；兼容旧版核对清单.json 入口')
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    if data.get('format') != 3:
        raise ValueError('旧版扫描结果请重新扫描；新版状态文件为核对清单/核对清单.json')
    root = Path(data['input']).resolve()
    output = Path(data['output']).resolve()
    processing = manifest_path.resolve().parent
    validate_layout(root, output, processing)
    if str(processing) != data['records_output']:
        raise ValueError('核对清单必须位于原目录；移动材料后请重新扫描')
    if not output.is_dir():
        raise ValueError('报销材料目录不存在，请恢复原目录')
    previous = data.get('report', {}).get('files', {})
    for name, digest in previous.items():
        path = managed_path(output, name)
        if path.exists() and (not path.is_file() or digest_file(path) != digest):
            raise ValueError(f'生成文件已被人工修改，请先移到输出目录外再重新生成: {name}')
    review = review or processing / '核对清单.xlsx'
    if not review.is_file():
        raise ValueError(f'核对清单不存在: {review}')
    # 先读取表格，防止 Excel 占用或格式错误导致生成一半才发现问题。
    records = [Material(**value) for value in data['materials']]
    validate_sources(root, records)
    recognized = deepcopy(records)
    review_mode = apply_review(review, records)
    with tempfile.TemporaryDirectory(prefix='imss-build-') as directory:
        staging = Path(directory)
        report = _build_into(data, records, root, staging, processing, review, template,
                             skip_pdf, start, settings_override, details_path, person_defaults,
                             verified_only=review_mode == 'simple')
        staged_files = {p.relative_to(staging).as_posix(): p for p in staging.rglob('*') if p.is_file()}
        names = set(staged_files)
        destinations = {name: managed_path(output, name) for name in names}
        for destination in destinations.values():
            if destination.parent.exists() and not destination.parent.is_dir():
                raise ValueError('输出子目录位置已有同名文件，请先移到输出目录外: ' + str(destination.parent))
        conflicts = [name for name, path in destinations.items() if path.exists() and name not in previous]
        if conflicts:
            raise ValueError('拒绝覆盖非本次生成的文件: ' + '、'.join(sorted(conflicts)))
        refresh_review(review, records, root, report, recognized=recognized)
        # 状态只在生成完成后更新；重跑以同一份扫描数据和最新人工修改为准。
        for name, path in staged_files.items():
            destination = destinations[name]
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(path, destination)
        for name in set(previous) - names:
            managed_path(output, name).unlink(missing_ok=True)
        report['output'] = str(output)
        for m in report['materials']:
            if m['archived']:
                relative = Path(m['archived']).relative_to(staging).as_posix()
                m['archived'] = str(managed_path(output, relative))
        report['files'] = {name: digest_file(destinations[name]) for name in names}
        data['report'] = report
        write_json(manifest_path, data)
    # Excel 的输入列保持原样，诊断列已按最新校验结果刷新。
    return report


def _build_into(data, records, root, output, records_output, review, template,
                skip_pdf, start, settings_override, details_path, person_defaults, verified_only=False):
    """在独立暂存目录组装附件及文档；输出路径由上层统一发布。"""
    all_records = records
    waiting = [m for m in records if not m.confirmed] if verified_only else []
    if verified_only:
        records = [m for m in records if m.confirmed]
    settings = settings_override or Settings.from_dict(data["settings"])
    template = template or (Path(data["template"]) if data.get("template") else None)
    if template and (not template.is_file() or detect_extension(template) != ".docx"):
        raise ValueError("找不到有效的 .docx 模板")
    if any(m.kind not in KINDS for m in records):
        raise ValueError("扫描结果含未知类型，请使用核对清单编辑")
    details = json.loads(details_path.read_text(encoding='utf-8-sig')) if details_path else {}
    if not isinstance(details, dict) or any(not isinstance(value, dict) for value in details.values()):
        raise ValueError("details 文件必须是分组代号或发票号到字段对象的映射")
    person_defaults = dict(person_defaults or {})
    if set(person_defaults) - {'student_id', 'phone', 'signature_image', 'passenger_name'}:
        raise ValueError("默认人员信息只支持 student_id、phone、signature_image、passenger_name")
    allowed = {'category', 'purpose', 'student_id', 'phone', 'invoice_number', 'is_prize', 'amount_equal', 'actual_amount', 'template', 'signature_image',
               'passenger_name', 'passenger_name_2', 'passenger_name_3'}
    for value in [person_defaults, *details.values()]:
        for field in ('student_id', 'phone', 'signature_image', 'passenger_name', 'passenger_name_2', 'passenger_name_3'):
            if field in value and not isinstance(value[field], str):
                raise ValueError(f"{field} 必须是字符串")
        if value.get('signature_image'):
            path = Path(value['signature_image']).expanduser()
            if not path.is_absolute():
                path = (details_path.parent if value is not person_defaults and details_path else Path.cwd()) / path
            validate_signature(path)
            value['signature_image'] = str(path.resolve())
    for key, value in details.items():
        if set(value) - allowed:
            raise ValueError(f"details 中含未知字段: {key}")
        for flag in ('is_prize', 'amount_equal'):
            if flag in value and type(value[flag]) is not bool:
                raise ValueError(f"{key}.{flag} 必须是 true 或 false")
        if value.get('category', '物品') not in ('物品', '奖品', '工作餐', '行程', '快递'):
            raise ValueError(f"报销类别无效: {key}")
    group = group_verified_materials if verified_only else group_materials
    bundles, pending = group([m for m in records if m.kind not in STANDALONE_SHEETS])
    numbering, next_number = bundle_numbers([b for b in bundles if not b.issues], start=start)
    archive_materials(records, bundles, numbering, root, output, records_output)
    report = {"format": 1, "created": datetime.now().astimezone().isoformat(),
              "input": str(root), "output": str(output),
              "records_output": str(records_output),
              "review_file": str(review.resolve()) if review else None,
              "details_file": str(details_path.resolve()) if details_path else None,
              "person_defaults": person_defaults,
              "archive_policy": "编号附件放入原始材料子目录；报销单、审查表、新闻稿、签领表、签到表及情况说明放在报销材料根目录；未配对原件保留在输入目录，通过核对清单访问",
              "generated": [], "pending": [], "warnings": [], "materials": []}
    report['pending'].extend({'source': m.source, 'issues': ['待校验，未参与本次配对和生成']} for m in waiting)
    for material in records:
        if material.issues:
            report["warnings"].append({"source": material.source, "issues": material.issues})
        if material.kind == "未知" and not material.duplicate_of:
            report["pending"].append({"source": material.source, "issues": material.issues or ["未知材料类型"]})
    for material in pending:
        report["pending"].append({"source": material.source, "issues": material.issues})
    sequence = next_number
    specials = [m for m in records if m.kind in SPECIALS and not m.duplicate_of]
    front = sorted((m for m in specials if m.kind != "情况说明"), key=lambda m: (0 if m.kind == "审查表" else 1, m.source))
    back = sorted((m for m in specials if m.kind == "情况说明"), key=lambda m: m.source)
    receipt_sheets = sorted((m for m in records if m.kind in STANDALONE_SHEETS and not m.duplicate_of),
                            key=lambda m: (m.group, m.source))

    def add_special(material, fixed_number=None):
        nonlocal sequence
        title = material.title or material.item or Path(material.source).stem
        suffix = material.kind
        if material.kind in STANDALONE_SHEETS:
            from imss.recognition.tables import sheet_heading
            if sheet_heading(title)[0]:
                suffix = ''  # “某活动签收表”保留原表名，不再拼成“签收表签领表”。
        elif title.endswith(material.kind):
            title = title[:-len(material.kind)]
        number = fixed_number if fixed_number is not None else f'{sequence:02d}'
        if fixed_number is None:
            sequence += 1
        directory = output
        destination = directory / f"{number}.{safe_name(title, 56)}{suffix}.pdf"
        # 人员签名难以 OCR 不妨碍保留原表；标题已识别，低置信度仍留在核对清单提示。
        blocking = [issue for issue in material.issues if not (
            material.kind in STANDALONE_SHEETS and 'OCR 平均置信度偏低' in issue)]
        if blocking:
            report["pending"].append({"source": material.source, "issues": blocking})
            return
        try:
            if destination.exists():
                raise FileExistsError(f"拒绝覆盖: {destination}")
            directory.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="imss-special-") as directory:
                temporary = Path(directory) / destination.name
                export_special(material, temporary, settings, source=root / material.source)
                temporary.replace(destination)
            report["generated"].append({"sequence": number, "kind": material.kind,
                                        "sources": [material.source], "pdf": destination.relative_to(output).as_posix()})
        except Exception as exc:
            report["pending"].append({"source": material.source, "issues": [f"PDF 导出失败: {exc}"]})

    for material in front:
        add_special(material, fixed_number='00')
    for bundle in bundles:
        if bundle.issues:
            report["pending"].append({"group": bundle.key, "sources": [m.source for m in bundle.materials], "issues": bundle.issues})
            continue
        number = numbering[bundle.key]
        stem = number + "." + safe_name(bundle.item) + "_" + bundle.amount
        destination = output / (stem + ".docx")
        LOGGER.info("生成 %s", destination.name)
        try:
            invoice = next(m for m in bundle.materials if m.kind == '电子发票')
            fields = {**person_defaults, **details.get(invoice.source, {}),
                      **details.get(bundle.key.removeprefix('manual:'), {})}
            # 自动组在 Excel 中显示短代号；允许直接复制 G 列作为 details 的键。
            # 同时保留上面的旧版 invoice:<摘要> 键；手工分组不再套用自动组配置。
            if bundle.key.startswith('invoice:') or invoice.group.startswith('reviewed-'):
                fields.update(details.get('auto-' + invoice.digest[:16], {}))
            for invoice_id in invoice.invoice_ids:
                if invoice_id in details:
                    fields.update(details[invoice_id])
                    break
            item_template = template
            if fields.get('template'):
                item_template = Path(fields['template'])
                if not item_template.is_absolute():
                    item_template = details_path.parent / item_template
                if not item_template.is_file() or detect_extension(item_template) != '.docx':
                    raise ValueError("该分组的 template 不是有效 DOCX")
            build_document(bundle, number, item_template, destination, settings, details=fields)
            generated = {"sequence": number, "kind": "报销材料", "item": bundle.item,
                         "group": bundle.key, "sources": [m.source for m in bundle.materials],
                         "amount": bundle.amount, "word": destination.name, "pdf": None,
                         "requires_signature_review": True}
            report["generated"].append(generated)
            report["warnings"].append({"group": bundle.key, "issues": ["请核对用途、奖品状态、报销金额及人员信息；承诺栏采用模板原图，开票人签字按用户配置填入，未提供时留空"]})
            if skip_pdf:
                report["warnings"].append({"group": bundle.key, "issues": ["根据 --skip-pdf 跳过 Word 的 PDF 导出"]})
            else:
                try:
                    pdf = convert_office(destination, output / (stem + ".pdf"), settings)
                    generated["pdf"] = pdf.name
                except Exception as exc:
                    report["pending"].append({"group": bundle.key, "issues": [f"Word 已保存，PDF 导出失败: {exc}"]})
        except Exception as exc:
            report["pending"].append({"group": bundle.key, "issues": [f"Word 生成失败: {exc}"]})
    for material in receipt_sheets:
        add_special(material)
    for material in back:
        add_special(material)
    report["materials"] = [m.to_dict() for m in all_records]
    return report
