"""Recompute rules with cached OCR and refresh PDF text; does not read labels."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from imss.recognition.extract import extract
from imss.storage import write_json
from imss.output.review import write_review
from imss.core.models import Material, Settings
from imss.recognition.rules import recognize
from imss.output.workspace import default_output, default_records, validate_layout
from imss.core.matching import group_materials
from imss.storage import validate_sources


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--rules-only', action='store_true', help='复用全部已提取文字，只重算规则；不读取 oracle')
    args = parser.parse_args()
    data = json.loads(args.manifest.read_text(encoding='utf-8'))
    settings = Settings.from_dict(data['settings'])
    root = Path(data['input'])
    output = default_output(root)
    processing = default_records(output)
    validate_layout(root.resolve(), output, processing)
    for directory in (output, processing):
        if directory.exists() and any(directory.iterdir()):
            raise ValueError('报销材料与核对清单目录应为空；本工具不覆盖已核对结果')
    records = []
    for index, saved in enumerate(data['materials']):
        material = Material(source=saved['source'], digest=saved['digest'], extension=saved['extension'],
                            duplicate_of=saved['duplicate_of'])
        if material.extension == '.pdf' and not args.rules_only:
            print('PDF', index + 1, material.source, flush=True)
            try:
                material.text, material.issues = extract(root / material.source, material.extension, settings)
            except Exception as exc:
                material.issues.append(f'提取失败: {type(exc).__name__}: {exc}')
        else:
            material.text = saved['text']
            material.issues = [issue for issue in saved['issues'] if 'OCR' in issue or '提取失败' in issue]
        recognize(material)
        records.append(material)
    validate_sources(root, records)
    group_materials([m for m in records if m.kind != '签领表'])
    data.pop('report', None)
    data.update(format=3, output=str(output), records_output=str(processing), materials=[m.to_dict() for m in records])
    output.mkdir(exist_ok=True)
    processing.mkdir(exist_ok=True)
    write_json(processing / '核对清单.json', data)
    write_review(processing / '核对清单.xlsx', records, root)
