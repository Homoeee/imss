"""审查目录中的分类人员配置，以及兼容的逐票补充信息。"""
from pathlib import Path
import json

from imss.output.documents import validate_signature
from imss.storage import write_json

CATEGORIES = ('物品', '奖品', '工作餐', '行程', '快递')
DETAIL_NAME = 'detail.json'
PERSON_FIELDS = ('student_id', 'phone', 'signature_image')
PASSENGER_FIELDS = ('name', 'student_id', 'signature_image')


def ensure_details(directory: Path):
    """只为缺失文件创建空白表，不覆盖已填写的信息。"""
    path = directory / DETAIL_NAME
    if not path.exists():
        values = {category: {field: '' for field in PERSON_FIELDS} for category in CATEGORIES}
        values['行程']['passengers'] = [{field: '' for field in PASSENGER_FIELDS}]
        values['行程']['purpose'] = ''
        write_json(path, values)
    return path


def load_details(path: Path | None, defaults: dict):
    values = json.loads(path.read_text(encoding='utf-8-sig')) if path else {}
    if not isinstance(values, dict) or any(not isinstance(value, dict) for value in values.values()):
        raise ValueError('details 文件必须是发票类别、原文件名称或发票号到字段对象的映射')
    if set(defaults) - {*PERSON_FIELDS, 'passenger_name'}:
        raise ValueError('默认人员信息只支持 student_id、phone、signature_image、passenger_name')
    allowed = {*PERSON_FIELDS, 'category', 'purpose', 'invoice_number', 'is_prize', 'amount_equal',
               'actual_amount', 'template', 'passenger_name', 'passenger_name_2', 'passenger_name_3', 'passengers'}

    def validate_person(value, base, fields):
        for field in fields:
            if field in value and not isinstance(value[field], str):
                raise ValueError(f'{field} 必须是字符串')
        if value.get('signature_image'):
            image = Path(value['signature_image']).expanduser()
            if not image.is_absolute():
                image = base / image
            validate_signature(image)
            value['signature_image'] = str(image.resolve())

    validate_person(defaults, Path.cwd(), (*PERSON_FIELDS, 'passenger_name'))
    for key, value in values.items():
        if set(value) - allowed:
            raise ValueError(f'details 中含未知字段: {key}')
        validate_person(value, path.parent, (*PERSON_FIELDS, 'passenger_name', 'passenger_name_2', 'passenger_name_3', 'purpose'))
        for flag in ('is_prize', 'amount_equal'):
            if flag in value and type(value[flag]) is not bool:
                raise ValueError(f'{key}.{flag} 必须是 true 或 false')
        if value.get('category', '物品') not in CATEGORIES:
            raise ValueError(f'报销类别无效: {key}')
        if 'passengers' in value:
            passengers = value['passengers']
            if not isinstance(passengers, list) or len(passengers) > 3:
                raise ValueError(f'{key}.passengers 必须是列表，内置行程表最多支持 3 位乘车人')
            for person in passengers:
                if not isinstance(person, dict) or set(person) - set(PASSENGER_FIELDS):
                    raise ValueError(f'{key}.passengers 每项只支持 name、student_id、signature_image')
                validate_person(person, path.parent, PASSENGER_FIELDS)
        # 分类模板里的空字符串只是待填写栏，不应清除显式提供的统一默认值。
        if key in CATEGORIES:
            values[key] = {field: content for field, content in value.items()
                           if (content != '' or field == 'purpose') and (field != 'passengers' or any(any(p.values()) for p in content))}
    return values


def expand_passengers(fields):
    """列表中每位乘车人独立对应模板的姓名、学号、签名栏。"""
    fields = dict(fields)
    if 'passengers' in fields:
        for index in range(3):
            person = fields['passengers'][index] if index < len(fields['passengers']) else {}
            suffix = '' if index == 0 else f'_{index + 1}'
            fields['passenger_name' + suffix] = person.get('name', '')
            fields['student_id' + suffix] = person.get('student_id') or (fields.get('student_id', '') if index == 0 else '')
            fields['signature_image' + suffix] = person.get('signature_image') or (fields.get('signature_image', '') if index == 0 else '')
    return fields
