"""
GTİP Sınıflandırıcı
=====================
TEMU scraper çıktısındaki ürünleri GTİP veritabanı ve fasıl notları
kullanarak Claude API ile sınıflandırır.

Kullanım:
    # Scraper çıktısını sınıflandır
    python gtip_matcher.py output/test_scrape.xlsx

    # Farklı DB ve çıktı
    python gtip_matcher.py input.xlsx --db data/gtip_2026.db -o output/classified.xlsx

    # API key environment variable veya .env dosyasından okunur
    set ANTHROPIC_API_KEY=sk-ant-...
    python gtip_matcher.py input.xlsx

Gereksinimler:
    pip install anthropic openpyxl
"""

import sys
import os
import re
import json
import sqlite3
import argparse
import time

try:
    import openpyxl
except ImportError:
    print("openpyxl yüklü değil: pip install openpyxl")
    sys.exit(1)

try:
    import anthropic
except ImportError:
    print("anthropic yüklü değil: pip install anthropic")
    sys.exit(1)


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def get_candidate_fasils(keywords, description):
    """
    Ürün keywords ve açıklamasından olası fasıl numaralarını belirle.
    Basit keyword->fasıl mapping ile ilk eleme yapar.
    """
    text = (keywords + " " + description).lower()

    MATERIAL_FASIL_MAP = {
        'plastic': [39],
        'rubber': [40],
        'leather': [42],
        'wood': [44],
        'paper': [48],
        'textile': list(range(50, 64)),
        'fabric': list(range(50, 64)),
        'cotton': [52],
        'silk': [50],
        'wool': [51],
        'ceramic': [69],
        'glass': [70],
        'iron': [72, 73],
        'steel': [72, 73],
        'copper': [74],
        'aluminum': [76],
        'aluminium': [76],
        'metal': [73, 74, 76, 82, 83],
    }

    CATEGORY_FASIL_MAP = {
        'electrical': [85],
        'electronics': [85],
        'lighting': [85, 94],
        'lamp': [85, 94],
        'tools': [82],
        'toy': [95],
        'game': [95],
        'sport': [95],
        'furniture': [94],
        'clothing': [61, 62],
        'apparel': [61, 62],
        'footwear': [64],
        'shoe': [64],
        'bag': [42],
        'jewelry': [71],
        'watch': [91],
        'kitchen': [39, 73, 82],
        'home improvement': [39, 73, 83],
        'building supplies': [39, 73, 76],
        'bath': [39],
        'bathroom': [39],
        'home storage': [39, 73, 94],
        'automotive': [87],
        'vehicle': [87],
        'phone': [85],
        'computer': [84],
        'machine': [84],
        'cosmetic': [33],
        'beauty': [33],
        'food': list(range(1, 25)),
        'stationery': [48, 96],
    }

    fasils = set()

    for kw, chapters in MATERIAL_FASIL_MAP.items():
        if kw in text:
            fasils.update(chapters)

    for kw, chapters in CATEGORY_FASIL_MAP.items():
        if kw in text:
            fasils.update(chapters)

    if not fasils:
        fasils = {39, 73, 82, 83, 84, 85, 94, 96}

    return sorted(fasils)


def get_fasil_gtip_list(conn, fasil_no, limit=150):
    """Bir fasılın GTİP kodlarını ve tanımlarını getir."""
    c = conn.cursor()
    rows = c.execute("""
        SELECT gtip_code, tanim, tanim_hiyerarsi
        FROM gtip WHERE fasil = ?
        ORDER BY gtip_code
        LIMIT ?
    """, (fasil_no, limit)).fetchall()
    return rows


def get_fasil_notu(conn, fasil_no):
    """Fasıl notunu getir."""
    c = conn.cursor()
    row = c.execute(
        "SELECT fasil_notu FROM fasil_notlari WHERE fasil_no = ?",
        (fasil_no,)
    ).fetchone()
    return row[0] if row else ""


def search_gtip_fts(conn, query, limit=20):
    """FTS ile GTİP arama."""
    c = conn.cursor()
    try:
        rows = c.execute("""
            SELECT gtip_code, tanim, tanim_hiyerarsi
            FROM gtip_fts WHERE gtip_fts MATCH ?
            LIMIT ?
        """, (query, limit)).fetchall()
        return rows
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Claude API classification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Sen bir Türk Gümrük Tarife sınıflandırma uzmanısın. Görevin, verilen ürün
bilgilerine ve tarife cetveli verilerine dayanarak doğru 12 haneli GTİP kodunu belirlemek.

Kurallar:
- Her zaman 12 haneli (XX.XX.XX.XX.XX formatında) bir GTİP kodu öner.
- Sınıflandırmada ürünün ASLİ FONKSİYONUNU esas al, yüzeysel özelliklerini değil.
- Fasıl notlarını dikkatlice uygula — hangi ürünlerin dahil/hariç olduğunu kontrol et.
- "wall mount" veya "self-adhesive" gibi montaj yöntemleri tek başına inşaat malzemesi yapmaz.
- Emin olamadığın durumlarda gerekçeni açıkla ve güven seviyeni belirt.

Yanıtını şu JSON formatında ver:
{
  "gtip_code": "XXXX.XX.XX.XX.XX",
  "fasil": 39,
  "gerekce": "Kısa gerekçe",
  "guven": "yüksek|orta|düşük",
  "alternatifler": ["YYYY.YY.YY.YY.YY"]
}"""


def classify_product(client, product_info, conn):
    """
    Tek bir ürünü Claude API ile sınıflandır.
    Returns: dict with gtip_code, fasil, gerekce, guven, alternatifler
    """
    title = product_info.get('title', '')
    desc = product_info.get('description', '')
    keywords = product_info.get('keywords', '')
    material = product_info.get('material', '')
    properties = product_info.get('properties', '')

    candidate_fasils = get_candidate_fasils(keywords, desc + " " + title)

    context_parts = []
    for fno in candidate_fasils[:5]:
        gtips = get_fasil_gtip_list(conn, fno, limit=100)
        if not gtips:
            continue
        note = get_fasil_notu(conn, fno)
        note_excerpt = note[:1500] if note else "(not yok)"

        gtip_lines = "\n".join(f"  {g[0]}  {g[1]}" for g in gtips[:80])
        context_parts.append(
            f"--- Fasıl {fno} ---\n"
            f"Not (özet):\n{note_excerpt}\n\n"
            f"GTİP kodları:\n{gtip_lines}"
        )

    fts_terms = re.sub(r'[^\w\s]', ' ', title).split()[:5]
    fts_results = []
    for term in fts_terms:
        if len(term) > 3:
            fts_results.extend(search_gtip_fts(conn, term, limit=5))
    if fts_results:
        fts_lines = "\n".join(f"  {r[0]}  {r[1]}" for r in fts_results[:15])
        context_parts.append(f"--- FTS arama sonuçları ---\n{fts_lines}")

    tarife_context = "\n\n".join(context_parts)

    user_msg = f"""Aşağıdaki TEMU ürünü için doğru 12 haneli GTİP kodunu belirle.

ÜRÜN BİLGİLERİ:
Başlık: {title}
Açıklama: {desc}
Kategori: {keywords}
Malzeme: {material or '(belirtilmemiş, açıklamadan çıkar)'}
Özellikler: {properties or '(yok)'}

TARİFE CETVELİ VERİLERİ:
{tarife_context}

Yanıtını SADECE JSON olarak ver."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20250414",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )

        text = response.content[0].text.strip()
        json_m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if json_m:
            result = json.loads(json_m.group())
            return result
        else:
            return {"gtip_code": "", "gerekce": text[:200], "guven": "düşük", "error": "JSON parse edilemedi"}

    except Exception as e:
        return {"gtip_code": "", "gerekce": "", "guven": "", "error": str(e)}


# ---------------------------------------------------------------------------
# Excel I/O
# ---------------------------------------------------------------------------

def read_scraped_excel(filepath):
    """Scraper çıktısı Excel'i oku."""
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active

    headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    products = []
    for r in range(2, ws.max_row + 1):
        row = {}
        for i, h in enumerate(headers):
            if h:
                row[h.lower().replace(' ', '_')] = ws.cell(r, i + 1).value or ''
        products.append(row)
    return products


def write_classified_excel(products, classifications, output_path):
    """Sınıflandırma sonuçlarını Excel'e yaz."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "GTİP Sınıflandırma"

    out_headers = ['URL', 'Goods ID', 'Title', 'Keywords', 'Material',
                   'GTİP Kodu', 'Fasıl', 'Gerekçe', 'Güven', 'Alternatifler', 'Hata']
    ws.append(out_headers)

    bold = openpyxl.styles.Font(bold=True)
    for c in range(1, len(out_headers) + 1):
        ws.cell(1, c).font = bold

    for prod, cls in zip(products, classifications):
        alts = cls.get('alternatifler', [])
        alt_str = ', '.join(alts) if isinstance(alts, list) else str(alts)

        ws.append([
            prod.get('url', ''),
            prod.get('goods_id', ''),
            prod.get('title', ''),
            prod.get('keywords', ''),
            prod.get('material', ''),
            cls.get('gtip_code', ''),
            cls.get('fasil', ''),
            cls.get('gerekce', ''),
            cls.get('guven', ''),
            alt_str,
            cls.get('error', ''),
        ])

    ws.column_dimensions['A'].width = 50
    ws.column_dimensions['C'].width = 50
    ws.column_dimensions['F'].width = 20
    ws.column_dimensions['H'].width = 60

    wb.save(output_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='GTİP Sınıflandırıcı (Claude API)')
    parser.add_argument('input', help='Scraper çıktısı Excel dosyası')
    parser.add_argument('-o', '--output', help='Çıktı Excel dosyası')
    parser.add_argument('--db', default='data/gtip_2026.db', help='GTİP veritabanı yolu')
    parser.add_argument('--delay', type=float, default=0.5, help='API istekleri arası bekleme (saniye)')
    args = parser.parse_args()

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')

    if not api_key:
        env_file = os.path.join(os.path.dirname(__file__), '..', '.env')
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.strip().startswith('ANTHROPIC_API_KEY='):
                        api_key = line.strip().split('=', 1)[1].strip().strip('"').strip("'")

    if not api_key:
        print("Hata: ANTHROPIC_API_KEY bulunamadı.")
        print("  set ANTHROPIC_API_KEY=sk-ant-...")
        print("  veya .env dosyasına ANTHROPIC_API_KEY=sk-ant-... yazın")
        sys.exit(1)

    if not os.path.isfile(args.input):
        print(f"Hata: {args.input} bulunamadı")
        sys.exit(1)

    if not os.path.isfile(args.db):
        print(f"Hata: {args.db} bulunamadı. Önce build_db.py çalıştırın.")
        sys.exit(1)

    output_path = args.output
    if not output_path:
        base = os.path.splitext(os.path.basename(args.input))[0]
        output_path = os.path.join('output', f'{base}_classified.xlsx')

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    conn = sqlite3.connect(args.db)
    client = anthropic.Anthropic(api_key=api_key)

    products = read_scraped_excel(args.input)
    print(f"Toplam ürün: {len(products)}")

    if not products:
        print("Hata: Excel'de ürün bulunamadı")
        sys.exit(1)

    classifications = []
    errors = 0

    for i, prod in enumerate(products, 1):
        title = prod.get('title', '')[:50]
        print(f"  [{i}/{len(products)}] {title}...", end=" ", flush=True)

        cls = classify_product(client, prod, conn)

        if cls.get('error'):
            print(f"HATA: {cls['error'][:60]}")
            errors += 1
        else:
            code = cls.get('gtip_code', '?')
            guven = cls.get('guven', '?')
            print(f"→ {code} ({guven})")

        classifications.append(cls)

        if i < len(products):
            time.sleep(args.delay)

    write_classified_excel(products, classifications, output_path)

    conn.close()

    print(f"\nToplam       : {len(products)}")
    print(f"Sınıflandırma: {len(products) - errors}")
    print(f"Hata         : {errors}")
    print(f"Çıktı        : {output_path}")


if __name__ == "__main__":
    main()
