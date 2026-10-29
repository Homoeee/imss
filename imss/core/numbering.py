"""Assign consecutive output numbers independently of grouping labels."""

from imss.core.models import Bundle


def bundle_numbers(bundles: list[Bundle], start: int = 1) -> tuple[dict[str, str], int]:
    if not 1 <= start <= 9999:
        raise ValueError('起始序号应为 1 到 9999；00 专用于审查表和新闻稿')
    return ({bundle.key: f'{index:02d}' for index, bundle in enumerate(bundles, start)},
            start + len(bundles))
