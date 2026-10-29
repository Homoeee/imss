"""Prepare private shuffled fixtures and compare predictions with a separate oracle.

The organizer never receives oracle.json. Reference filenames label expected groups,
not individual screenshot transaction amounts. No private fixture is Git-tracked.
"""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from imss.core.models import Material, Settings
from imss.core.matching import group_materials
from imss.recognition.rules import recognize
from imss.storage import write_json


def parse_filename(stem):
    match = re.match(r"^(\d+(?:\.\d+)?)[._](.*?)[_-](\d+(?:\.\d+)?)(?:元)?$", stem)
    if not match:
        raise ValueError(f"无法解析参考报销单命名: {stem}")
    return match.group(1), match.group(2), format(Decimal(match.group(3)), ".2f")


def reference_kind(name):
    for token in ("电子发票", "行程单", "运单明细", "购买清单", "签领表", "订单截图"):
        if token in name:
            return {"订单截图": "购买记录"}.get(token, token)
    if "审批表" in name or "审查表" in name:
        return "审查表"
    if "新闻稿" in name:
        return "新闻稿"
    return "未知"


def prepare(reference: Path, destination: Path, seed: int):
    from docx import Document
    if destination.exists():
        raise ValueError("测试集目录已存在；为保证可重复性，不覆盖")
    groups = {}
    for path in sorted(reference.glob("*.docx")):
        number, item, amount = parse_filename(path.stem)
        from lxml import etree
        import zipfile
        with zipfile.ZipFile(path) as archive:
            xml = etree.fromstring(archive.read("word/document.xml"))
            text = "".join(xml.xpath(".//w:t/text()", namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}))
        category = ("行程" if "打车发票" in text else "工作餐" if "工作餐" in path.stem else "快递" if "寄送" in path.stem else "奖品" if "☑是，签领单附后" in text else "物品")
        groups[number] = {"id": number, "item": item, "amount": amount, "category": category,
                          "is_prize": True if "☑是，签领单附后" in text else False,
                          "reference_word": path.name, "reference_pdf": path.with_suffix('.pdf').name,
                          "raw_sources": [], "signed_declaration_available_in_raw": False}
    sources = sorted((reference / "原始材料").iterdir())
    sources += sorted(path for path in reference.glob("*.pdf") if not path.with_suffix(".docx").exists())
    randomizer = random.Random(seed)
    randomizer.shuffle(sources)
    input_dir = destination / "input"
    input_dir.mkdir(parents=True)
    records = []
    for index, source in enumerate(sources):
        if not source.is_file():
            continue
        kind = reference_kind(source.name)
        expected_groups = []
        if source.parent.name == "原始材料":
            prefix = re.match(r"^([\d.+]+)[._]", source.name)
            if not prefix:
                raise ValueError(f"无法识别参考分组: {source.name}")
            code = prefix.group(1)
            if "+" in code and code.startswith("18+"):
                expected_groups = ["18", "19"]
            elif code in groups:
                expected_groups = [code]
            elif code.split(".")[0] in groups:
                expected_groups = [code.split(".")[0]]
            else:
                raise ValueError(f"找不到参考组: {source.name}: {code}")
        extension = source.suffix.lower()
        if index % 11 == 0 and extension in (".jpg", ".png"):
            extension = ".temp" if index % 2 else ".tmp"
        opaque = hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()[:12] + extension
        shutil.copy2(source, input_dir / opaque)
        record = {"file": opaque, "original": source.relative_to(reference).as_posix(),
                  "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "kind": kind, "groups": expected_groups,
                  "invoice_amount": groups[expected_groups[0]]["amount"] if kind == "电子发票" else None}
        for group in expected_groups:
            groups[group]["raw_sources"].append(opaque)
        records.append(record)
    oracle = {"format": 1, "seed": seed, "reference": str(reference.resolve()), "files": records,
              "groups": list(groups.values()), "limitations": [
                  "原始材料无独立承诺书、签名、用途、学号或奖品勾选，不能用原始材料恢复这些事实。",
                  "参考名称包含用途和人员等人工信息，严格同名不能等同于 OCR 正确率。",
                  "部分订单截图金额是分笔金额，不能用参考组总额作为每张截图金额真值。",
                  "没有情况说明或午餐样例；两份工作餐是晚餐，不能声称覆盖了午餐特有规则。"]}
    write_json(destination / "oracle.json", oracle)
    print(json.dumps({"files": len(records), "groups": len(groups), "types": dict(Counter(x['kind'] for x in records)),
                      "categories": dict(Counter(x['category'] for x in groups.values())), "input": str(input_dir.resolve())}, ensure_ascii=False, indent=2))


def evaluate(oracle_path: Path, manifest: Path, output: Path, rerun_rules: bool = False):
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    data = json.loads(manifest.read_text(encoding="utf-8"))
    materials = [Material(**value) for value in data["materials"]]
    if rerun_rules:
        for material in materials:
            # Retain extractor errors and confidence flags; discard old rule diagnostics.
            material.issues = [issue for issue in material.issues if "OCR" in issue or "提取失败" in issue or "Word 内嵌图片" in issue]
            material.item = material.amount = material.group = ""
            material.order_ids = []
            recognize(material)
    predictions = {m.source: m for m in materials}
    labels = {x["file"]: x for x in oracle["files"]}
    rows, type_correct, amount_correct, invoice_count = [], 0, 0, 0
    for source, label in labels.items():
        material = predictions.get(source)
        correct = material is not None and material.kind == label["kind"]
        type_correct += correct
        amount_ok = None
        if label["invoice_amount"]:
            invoice_count += 1
            amount_ok = material is not None and material.amount == label["invoice_amount"]
            amount_correct += amount_ok
        rows.append({"file": source, "expected_kind": label["kind"], "actual_kind": material.kind if material else None,
                     "kind_correct": correct, "expected_groups": label["groups"], "predicted_item": material.item if material else None,
                     "invoice_amount_correct": amount_ok, "predicted_amount": material.amount if material else None,
                     "issues": material.issues if material else ["没有预测结果"]})
    bundles, pending = group_materials(materials)
    expected_sets = {frozenset(g["raw_sources"]): g["id"] for g in oracle["groups"]}
    complete, ready, wrong = [], [], []
    for bundle in bundles:
        files = frozenset(m.source for m in bundle.materials)
        actual_groups = {g for file in files for g in labels[file]["groups"]}
        if len(actual_groups) > 1:
            wrong.append({"bundle": bundle.key, "groups": sorted(actual_groups)})
        if files in expected_sets:
            complete.append(expected_sets[files])
            if not bundle.issues:
                ready.append(expected_sets[files])
    summary = {"material_count": len(labels), "type_correct": type_correct, "type_accuracy": type_correct / len(labels),
               "invoice_count": invoice_count, "invoice_amount_correct": amount_correct,
               "invoice_amount_accuracy": amount_correct / invoice_count if invoice_count else None,
               "expected_groups": len(oracle["groups"]), "proposed_groups": len(bundles),
               "exact_attachment_groups": len(complete), "ready_exact_groups": len(ready),
               "wrong_cross_group_merges": len(wrong), "unpaired_materials": len(pending),
               "signed_declarations_missing": len(oracle["groups"]), "per_type": {}}
    for kind in sorted({x["kind"] for x in labels.values()}):
        kind_rows = [row for row in rows if row["expected_kind"] == kind]
        summary["per_type"][kind] = {"correct": sum(row["kind_correct"] for row in kind_rows), "total": len(kind_rows)}
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "comparison.json", {"summary": summary, "files": rows, "cross_group_merges": wrong,
                                           "complete_groups": complete, "ready_groups": ready, "limitations": oracle["limitations"]})
    write_json(output / "predictions.json", {**data, "materials": [m.to_dict() for m in materials]})
    lines = ["# 乱序原始材料测试结果", "", "该结果使用真实材料的副本；参考分组与文件名仅在评分程序读取，不提供给识别器。", "",
             "| 指标 | 结果 |", "| --- | --- |",
             f"| 材料分类正确 | {type_correct}/{len(labels)} |", f"| 发票总额正确 | {amount_correct}/{invoice_count} |",
             f"| 附件集合与参考完全一致 | {len(complete)}/{len(oracle['groups'])} |",
             f"| 无警告且附件完整 | {len(ready)}/{len(oracle['groups'])} |",
             f"| 错误跨组合并 | {len(wrong)} |", f"| 原始输入缺少已签字承诺区域 | {len(oracle['groups'])}/{len(oracle['groups'])} |", "",
             "## 分类明细", "", "| 类型 | 正确 / 总数 |", "| --- | --- |"]
    lines += [f"| {kind} | {value['correct']}/{value['total']} |" for kind, value in summary['per_type'].items()]
    lines += ["", "## 解释", "", "完整分组要求附件集合与参考完全一致，包括行程单、补差价、签领表和共享附件。分类与金额正确不代表分组成功。签名缺失与无法推断的人工用途单列，不作为 OCR 错误。", ""]
    lines += [f"- {limitation}" for limitation in oracle["limitations"]]
    (output / "结果.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("reference", type=Path)
    prep.add_argument("destination", type=Path)
    prep.add_argument("--seed", type=int, default=20261022)
    score = sub.add_parser("evaluate")
    score.add_argument("oracle", type=Path)
    score.add_argument("manifest", type=Path)
    score.add_argument("output", type=Path)
    score.add_argument("--rerun-rules", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.reference, args.destination, args.seed)
    else:
        evaluate(args.oracle, args.manifest, args.output, args.rerun_rules)
