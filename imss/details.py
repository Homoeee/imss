"""审查目录中的分类人员配置，以及兼容的逐票补充信息。"""
from pathlib import Path
import json
import re

from imss.output.documents import validate_signature
from imss.storage import write_json

CATEGORIES = ('物品', '奖品', '工作餐', '行程', '快递')
DETAIL_NAME = 'detail.json'
PERSON_FIELDS = ('student_id', 'phone', 'signature_image')
PASSENGER_FIELDS = ('name', 'student_id', 'signature_image')
_DETAIL_STRINGS = re.compile(
    r'(?P<field>"(?:signature_image|template|meal_sheet)"\s*:\s*)?'
    r'(?P<value>"(?:[^"\\\x00-\x1f]|\\[^\r\n])*")'
)


def _escape_path_separators(match: re.Match) -> str:
    field, value = match.group('field', 'value')
    if not field or '\\' not in value:
        return match[0]
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        pass
    else:
        # JSON can silently turn a path such as D:\new into a string with a newline.
        if all(ord(char) >= 32 for char in decoded):
            return match[0]

    value = re.sub(r'\\+', lambda slashes: slashes[0] + '\\' * (len(slashes[0]) % 2), value)
    # A raw UNC path starts with two backslashes, which JSON represents with four.
    value = re.sub(r'^"\\\\(?!\\)', lambda prefix: prefix[0] + '\\\\', value)
    return field + value


def ensure_details(directory: Path):
    """只为缺失文件创建空白表，不覆盖已填写的信息。"""
    path = directory / DETAIL_NAME
    if not path.exists():
        values = {category: {**dict.fromkeys(PERSON_FIELDS, ''), 'is_prize': 0} for category in CATEGORIES}
        values['行程']['passengers'] = [{field: '' for field in PASSENGER_FIELDS}]
        values['行程']['purpose'] = ''
        values['工作餐']['meal_sheet'] = ''
        write_json(path, values)
    return path


def load_details(path: Path | None, defaults: dict):
    values = {}
    if path:
        content = path.read_text(encoding='utf-8-sig')
        values = json.loads(_DETAIL_STRINGS.sub(_escape_path_separators, content))
    if not isinstance(values, dict) or any(not isinstance(value, dict) for value in values.values()):
        raise ValueError('details 文件必须是发票类别、原文件名称或发票号到字段对象的映射')
    if set(defaults) - {*PERSON_FIELDS, 'passenger_name'}:
        raise ValueError('默认人员信息只支持 student_id、phone、signature_image、passenger_name')
    allowed = {*PERSON_FIELDS, 'category', 'purpose', 'invoice_number', 'is_prize', 'amount_equal',
               'actual_amount', 'template', 'meal_sheet', 'passenger_name', 'passenger_name_2', 'passenger_name_3', 'passengers'}

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
        if 'is_prize' in value:
            prize = value['is_prize']
            if type(prize) not in (int, bool) or prize not in (0, 1):
                raise ValueError(f'{key}.is_prize 必须是 0 或 1')
        if 'amount_equal' in value and type(value['amount_equal']) is not bool:
            raise ValueError(f'{key}.amount_equal 必须是 true 或 false')
        if 'meal_sheet' in value and not isinstance(value['meal_sheet'], str):
            raise ValueError(f'{key}.meal_sheet 必须是原文件名称字符串')
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
