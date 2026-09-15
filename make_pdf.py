import asyncio, sys
from pathlib import Path
from playwright.async_api import async_playwright
from pypdf import PdfReader, PdfWriter

SRC = (Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else
       Path("/home/user/workspace/repo/index-offline.html")).as_uri()
OUT = sys.argv[1]
if Path(OUT).exists():
    raise SystemExit(f"Refusing to overwrite an existing snapshot: {OUT}")

PRINT_CSS = """
@page { size: Letter landscape; margin: 12mm 10mm; }
* { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
.nav { display: none !important; }
.site-header { position: static !important; }
.section, .panel, .kpi, .chart-wrap, .cost-table, .callout, .meth-item, .sources {
  break-inside: avoid !important; page-break-inside: avoid !important;
}
.section { page-break-before: auto; scroll-margin-top: 0 !important; }
h2, h3, .subcap { break-after: avoid !important; page-break-after: avoid !important; }
body { background: #fff !important; }
"""

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1280, "height": 1000})
        requests, errors = [], []
        async def block_external(route):
            requests.append(route.request.url)
            await route.abort()
        await pg.route("http://**", block_external)
        await pg.route("https://**", block_external)
        pg.on("pageerror", lambda error: errors.append(str(error)))
        await pg.goto(SRC, wait_until="load")
        await pg.wait_for_function("""typeof Chart !== 'undefined' &&
            [...document.querySelectorAll('canvas')].every(c => Chart.getChart(c))""")
        await pg.evaluate("""Object.values(Chart.instances).forEach(c => {
            c.stop(); c.options.animation = false; c.update('none');
        })""")
        data_as_of = await pg.locator(".updated-badge").inner_text()
        n = await pg.evaluate("document.querySelectorAll('canvas').length")
        drawn = await pg.evaluate("""
            Array.from(document.querySelectorAll('canvas'))
                 .filter(c => Chart.getChart(c) && c.width > 0 && c.height > 0 &&
                     c.getContext('2d').getImageData(0, 0, c.width, c.height)
                     .data.some((v, i) => i % 4 === 3 && v > 0)).length
        """)
        print(f"canvases: {drawn}/{n} rendered; external requests: {len(requests)}; errors: {errors}")
        if drawn != n or requests or errors:
            raise RuntimeError("Offline snapshot validation failed")
        await pg.add_style_tag(content=PRINT_CSS)
        await pg.emulate_media(media="print")
        await pg.wait_for_timeout(1500)
        await pg.pdf(path=OUT, format="Letter", landscape=True,
                     print_background=True,
                     margin={"top": "12mm", "bottom": "12mm",
                             "left": "10mm", "right": "10mm"})
        await b.close()
    writer = PdfWriter()
    reader = PdfReader(OUT)
    # Browser print pagination can spill only a card border/shadow onto a page.
    # Preserve chart-only pages, which contain an image even with no PDF text.
    pages = [i for i, page in enumerate(reader.pages)
             if page.extract_text().strip() or len(page.images) or page.get("/Annots")]
    writer.append(reader, pages=pages)
    print(f"PDF pages: {len(pages)}; removed {len(reader.pages) - len(pages)} pagination-only pages")
    writer.add_metadata({
        "/Title": "Housing Dashboard — Snapshot",
        "/Author": "Perplexity Computer",
        "/Subject": "Howard County Housing Affordability Dashboard; " + data_as_of,
    })
    temporary = Path(OUT).with_suffix(".metadata.pdf")
    with temporary.open("wb") as stream:
        writer.write(stream)
    temporary.replace(OUT)
    print("wrote", OUT)

asyncio.run(main())
