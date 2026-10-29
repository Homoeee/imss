"""Render local PDF QA contact sheets without changing the PDFs."""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw
import pypdfium2 as pdfium


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    thumbnails = []
    for index, path in enumerate(sorted(args.source.glob('*.pdf')), 1):
        with pdfium.PdfDocument(path) as document:
            for page_number in range(len(document)):
                page = document[page_number]
                bitmap = page.render(scale=1.4)
                image = bitmap.to_pil().convert('RGB')
                bitmap.close()
                page.close()
                label = f'{index:02d}' + (f'-{page_number + 1}' if len(document) > 1 else '')
                image.save(args.destination / (label + '.png'))
                image.thumbnail((380, 540))
                thumbnails.append((label, image))
    for start in range(0, len(thumbnails), 9):
        sheet = Image.new('RGB', (1200, 1740), '#dddddd')
        draw = ImageDraw.Draw(sheet)
        for offset, (label, image) in enumerate(thumbnails[start:start + 9]):
            x, y = (offset % 3) * 400, (offset // 3) * 580
            draw.text((x + 10, y + 5), label, fill='black')
            sheet.paste(image, (x + 10, y + 30))
        sheet.save(args.destination / f'sheet-{start // 9 + 1}.png')
    print('Rendered pages:', len(thumbnails))
