"""
TEMU Urun Bilgisi Scraper
==========================
Excel'deki TEMU urun linklerinden urun bilgilerini ceker.
Chrome Debug modunda acilmis tarayiciya baglanir (CDP).

Kullanim:
  1) Masaustundeki 'Chrome Debug.bat' calistirin
  2) Temu hesabiniza giris yapin (ilk seferde)
  3) Scripti calistirin:
       python temu_scraper.py data/input.xlsx
       python temu_scraper.py data/input.xlsx -o output/result.xlsx
       python temu_scraper.py data/input.xlsx --images
"""

import sys
import os
import re
import json
import time
import argparse
import urllib.parse
import urllib.request

try:
    import openpyxl
except ImportError:
    print("openpyxl yuklu degil: pip install openpyxl")
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright yuklu degil: pip install playwright && playwright install chromium")
    sys.exit(1)

DETAIL_WAIT_MS = 15000
PAGE_TIMEOUT_MS = 30000
CAPTCHA_TIMEOUT = 120


def extract_goods_id(url):
    m = re.search(r'goods_id=(\d+)', url) or re.search(r'-g-(\d+)\.html', url)
    return m.group(1) if m else ''


def extract_gallery_url(url):
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return qs.get('top_gallery_url', [''])[0]
    except Exception:
        return ''


def wait_for_captcha(page, timeout=CAPTCHA_TIMEOUT):
    """Detect CAPTCHA and wait for user to solve it."""
    has_captcha = page.evaluate('''() => {
        return (document.body?.innerText || '').includes('Security Verification');
    }''')
    if not has_captcha:
        return False

    print("\n  *** CAPTCHA! Chrome penceresinde cozun... ***", end="", flush=True)
    for _ in range(timeout // 2):
        time.sleep(2)
        still = page.evaluate('''() => {
            return (document.body?.innerText || '').includes('Security Verification');
        }''')
        if not still:
            print(" OK", flush=True)
            time.sleep(2)
            return True
    print(" TIMEOUT", flush=True)
    return True


def wait_for_product_data(page, timeout_ms=DETAIL_WAIT_MS):
    try:
        page.wait_for_function(
            '() => { try { return window.rawData.store.goodsProperty.length > 0 } catch(e) { return false } }',
            timeout=timeout_ms
        )
        return True
    except Exception:
        return False


def extract_product_data(page):
    result = page.evaluate('''() => {
        const s = window.rawData?.store || {};
        const g = s.goods || {};

        const props = (s.goodsProperty || []).map(p => ({
            key: p.key || '',
            values: (p.values || []).join(', ')
        }));

        const gallery = (g.gallery || []).map(img =>
            typeof img === 'object' ? (img.url || '') : String(img)
        ).filter(Boolean);

        const banner = (g.bannerList || []).map(b =>
            typeof b === 'object' ? (b.url || '') : String(b)
        ).filter(Boolean);

        const fsd = s.formatSkuData || {};
        const variants = (fsd.skuTypeValues || []).map(v => ({
            key: v.type || '', values: (v.values || []).join(', ')
        }));

        return {
            goodsName: g.goodsName || null,
            status: g.status ?? null,
            goodsProperty: props,
            variants: variants,
            galleryUrls: gallery.slice(0, 5),
            bannerUrls: banner.slice(0, 3),
            hdThumbUrl: g.hdThumbUrl || '',
        };
    }''')

    meta = page.evaluate('''() => {
        const get = (sel) => {
            const el = document.querySelector(sel);
            return el ? el.getAttribute('content') || '' : '';
        };
        return {
            title: document.title || '',
            description: get('meta[name="description"]'),
            keywords: get('meta[name="keywords"]'),
        };
    }''')

    return result, meta


def safe_goto(page, url, retries=3):
    """Navigate with retry on context-destroyed errors (redirects)."""
    for attempt in range(retries):
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT_MS)
            time.sleep(2)
            wait_for_captcha(page)
            return True
        except Exception as e:
            if 'context was destroyed' in str(e) or 'navigation' in str(e).lower():
                time.sleep(3)
                try:
                    page.wait_for_load_state('domcontentloaded', timeout=15000)
                    time.sleep(2)
                    wait_for_captcha(page)
                    return True
                except Exception:
                    if attempt < retries - 1:
                        continue
            raise
    return False


def scrape_product(page, url, delay=2.0):
    goods_id = extract_goods_id(url)
    gallery_from_url = extract_gallery_url(url)

    row = {
        'url': url,
        'goods_id': goods_id,
        'title': '',
        'description': '',
        'keywords': '',
        'product_details': '',
        'image_url': gallery_from_url,
        'properties': '',
        'error': '',
    }

    try:
        safe_goto(page, url)

        loaded = wait_for_product_data(page)

        product, meta = extract_product_data(page)

        name = product.get('goodsName') or ''
        if not name or name == 'Unavailable for purchase':
            name = meta.get('title', '')
            name = re.sub(r'\s*[-\u2013]\s*Temu\b.*$', '', name).strip()
            if name.lower() in ('temu', ''):
                slug_m = re.search(r'temu\.com/(?:[a-z]{2}-[a-z]{2}/)?(.+?)(?:-g-\d+\.html|$)', url)
                if slug_m:
                    name = slug_m.group(1).replace('-', ' ').strip()
        row['title'] = name

        desc = meta.get('description', '')
        desc = re.sub(r'^(Shop |Check out this |Find |Free returns\.\s*)', '', desc).strip()
        row['description'] = desc
        row['keywords'] = meta.get('keywords', '').replace('&amp;', '&')

        gp = product.get('goodsProperty', [])
        if gp:
            details = [f"{p['key']}: {p['values']}" for p in gp if p.get('key') and p.get('values')]
            row['product_details'] = '; '.join(details)

        variants = product.get('variants', [])
        if variants:
            row['properties'] = '; '.join(f"{v['key']}: {v['values']}" for v in variants if v.get('key'))

        imgs = product.get('galleryUrls', [])
        if imgs:
            row['image_url'] = imgs[0]
        elif product.get('bannerUrls'):
            row['image_url'] = product['bannerUrls'][0]
        elif product.get('hdThumbUrl'):
            row['image_url'] = product['hdThumbUrl']

    except Exception as e:
        row['error'] = str(e)[:100]

    return row


def read_links(filepath):
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active
    links = []
    for row in ws.iter_rows(min_row=1, max_col=ws.max_column, values_only=False):
        for cell in row:
            val = str(cell.value or '').strip()
            if 'temu.com' in val and ('goods' in val or '-g-' in val):
                links.append(val)
    return links


def write_output(results, output_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "TEMU Products"

    headers = ['URL', 'Goods ID', 'Title', 'Description', 'Keywords',
               'Product Details', 'Image URL', 'SKU Variants', 'Error']
    ws.append(headers)

    for col in range(1, len(headers) + 1):
        ws.cell(1, col).font = openpyxl.styles.Font(bold=True)

    for r in results:
        ws.append([
            r['url'], r['goods_id'], r['title'], r['description'],
            r['keywords'], r['product_details'], r['image_url'],
            r['properties'], r['error']
        ])

    ws.column_dimensions['A'].width = 50
    ws.column_dimensions['C'].width = 50
    ws.column_dimensions['D'].width = 60
    ws.column_dimensions['E'].width = 30
    ws.column_dimensions['F'].width = 60

    wb.save(output_path)


def download_image(image_url, save_dir, goods_id):
    if not image_url:
        return ''
    try:
        ext = '.jpg'
        if '.png' in image_url:
            ext = '.png'
        elif '.webp' in image_url:
            ext = '.webp'
        fpath = os.path.join(save_dir, f"{goods_id}{ext}")
        req = urllib.request.Request(image_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        })
        resp = urllib.request.urlopen(req, timeout=15)
        with open(fpath, 'wb') as f:
            f.write(resp.read())
        return fpath
    except Exception:
        return ''


def main():
    parser = argparse.ArgumentParser(description='TEMU Product Scraper')
    parser.add_argument('input', help='Excel file with TEMU links')
    parser.add_argument('-o', '--output', help='Output Excel file')
    parser.add_argument('--images', action='store_true', help='Download product images')
    parser.add_argument('--delay', type=float, default=3.0, help='Delay between pages (seconds)')
    parser.add_argument('--port', type=int, default=9222, help='Chrome debugging port')
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Hata: {args.input} bulunamadi")
        sys.exit(1)

    output_path = args.output
    if not output_path:
        base = os.path.splitext(os.path.basename(args.input))[0]
        output_path = os.path.join('output', f'{base}_scraped.xlsx')
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    image_dir = None
    if args.images:
        image_dir = os.path.join(os.path.dirname(output_path), 'images')
        os.makedirs(image_dir, exist_ok=True)

    links = read_links(args.input)
    print(f"Toplam link: {len(links)}")
    if not links:
        print("Hata: Excel'de TEMU linki bulunamadi")
        sys.exit(1)

    print(f"Chrome'a baglaniliyor (port {args.port})...", flush=True)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f'http://127.0.0.1:{args.port}')
        except Exception:
            print(
                "\nChrome'a baglanilamadi!\n"
                "Masaustundeki 'Chrome Debug.bat' dosyasini cift tiklayin,\n"
                "Chrome acildiktan sonra scripti tekrar calistirin.\n"
            )
            sys.exit(1)

        ctx = browser.contexts[0]
        page = ctx.new_page()
        print("Baglandi.\n")

        results = []
        errors = 0

        for i, url in enumerate(links, 1):
            gid = extract_goods_id(url)
            print(f"  [{i}/{len(links)}] {gid or url[:50]}...", end=" ", flush=True)

            row = scrape_product(page, url, args.delay)

            if row['error']:
                print(f"HATA: {row['error']}")
                errors += 1
            else:
                dp = len(row['product_details'].split(';')) if row['product_details'] else 0
                tag = f"[{dp} details]" if dp > 0 else "[meta only]"
                print(f"{row['title'][:50]} {tag}")

            results.append(row)

            if i < len(links):
                time.sleep(args.delay)

        page.close()
        browser.close()

    if args.images and image_dir:
        print("\nResimler indiriliyor...")
        for r in results:
            if r['image_url'] and r['goods_id']:
                download_image(r['image_url'], image_dir, r['goods_id'])

    write_output(results, output_path)

    ok = len(results) - errors
    details = sum(1 for r in results if r['product_details'])
    imgs = sum(1 for r in results if r['image_url'])
    print(f"\nToplam         : {len(results)}")
    print(f"Basarili       : {ok}")
    print(f"Hata           : {errors}")
    print(f"Product Details: {details}/{ok}")
    print(f"Resim          : {imgs}/{ok}")
    print(f"Cikti          : {output_path}")


if __name__ == "__main__":
    main()
