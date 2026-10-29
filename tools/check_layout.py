"""Controlled layout test using oracle groups, separate from recognition scores."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from imss.output.documents import build_document
from imss.recognition.extract import detect_extension
from imss.core.models import Bundle, Material, Settings, safe_name


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    oracle = json.loads((args.corpus / 'oracle.json').read_text(encoding='utf-8'))
    by_file = {value['file']: value for value in oracle['files']}
    args.destination.mkdir(parents=True, exist_ok=True)
    for group in oracle['groups']:
        materials = []
        for name in group['raw_sources']:
            path = (args.corpus / 'input' / name).resolve()
            label = by_file[name]
            materials.append(Material(source=name, digest=label['sha256'], extension=detect_extension(path),
                                      kind=label['kind'], item=group['item'], amount=group['amount'],
                                      category=group['category'], archived=str(path)))
        bundle = Bundle(group['id'], group['item'], group['amount'], materials)
        destination = args.destination / (group['id'] + '.' + safe_name(group['item']) + '_' + group['amount'] + '.docx')
        if destination.exists():
            continue
        build_document(bundle, group['id'], None, destination, Settings(),
                       details={'category': group['category'], 'is_prize': group['is_prize'], 'amount_equal': True})
        print(destination.name)
    (args.destination / 'TEST_SCOPE.txt').write_text(
        'Controlled layout test ONLY. Grouping, item names, amounts and prize flags were supplied by the reference oracle. '
        'This is NOT an automatic recognition result. Purposes, IDs, contact details and signatures are left blank. '
        'Never use these drafts for submission without review and actual signatures.', encoding='utf-8')
