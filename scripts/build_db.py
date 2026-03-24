"""
GTİP Veritabanı Oluşturucu
===========================
Türk Gümrük Tarife Cetveli fasıl XLS dosyalarını ve fasıl notlarını
parse edip SQLite veritabanına yazar.

Kullanım:
    # Tek dosya
    python build_db.py 39_fasıl_2026.xls

    # Klasördeki tüm fasıllar
    python build_db.py data/fasil_dosyalari/

    # Fasıl notlarını da dahil et
    python build_db.py data/fasil_dosyalari/ --notlar data/fasil_notlari/

    # Çıktı DB ismi belirtme
    python build_db.py data/fasil_dosyalari/ --notlar data/fasil_notlari/ --db data/gtip_2026.db

    # Mevcut DB varsa üzerine yaz
    python build_db.py data/fasil_dosyalari/ --force
"""

import sys
import os
import re
import sqlite3
import glob
import argparse

try:
    import xlrd
except ImportError:
    print("xlrd yüklü değil: pip install xlrd")
    sys.exit(1)


# ---------------------------------------------------------------------------
# DB Schema
# ---------------------------------------------------------------------------

def create_db(db_path):
    """SQLite veritabanı şemasını oluştur (mevcut tabloları siler)."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("DROP TABLE IF EXISTS gtip_fts")
    c.execute("DROP TABLE IF EXISTS notlar_fts")
    c.execute("DROP TABLE IF EXISTS gtip")
    c.execute("DROP TABLE IF EXISTS pozisyon")
    c.execute("DROP TABLE IF EXISTS fasil_meta")
    c.execute("DROP TABLE IF EXISTS fasil_notlari")

    c.execute("""
        CREATE TABLE gtip (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gtip_code TEXT NOT NULL,      -- 12 haneli: 3926.90.97.90.29
            gtip_clean TEXT NOT NULL,     -- noktasız: 392690979029
            fasil INTEGER,               -- fasıl no: 39
            pozisyon TEXT,               -- 4 hane noktalı: 39.26
            alt_pozisyon TEXT,           -- 6 hane noktasız: 392690
            tanim TEXT NOT NULL,         -- tanım (çok satırlı birleştirilmiş)
            tanim_hiyerarsi TEXT,        -- üst pozisyonlardan tam hiyerarşi
            olcu_birimi TEXT,            -- ölçü birimi
            seviye INTEGER,              -- hiyerarşi seviyesi (tire sayısı)
            UNIQUE(gtip_code)
        )
    """)

    c.execute("""
        CREATE TABLE pozisyon (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kod TEXT NOT NULL,
            kod_clean TEXT NOT NULL,
            fasil INTEGER,
            tanim TEXT NOT NULL,
            seviye INTEGER,              -- 4=pozisyon, 6=altpoz, 8=altalt, 10=...
            UNIQUE(kod)
        )
    """)

    c.execute("""
        CREATE TABLE fasil_meta (
            fasil_no INTEGER PRIMARY KEY,
            dosya_adi TEXT,
            satir_sayisi INTEGER,
            gtip_sayisi INTEGER,
            parse_tarihi TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE fasil_notlari (
            fasil_no INTEGER PRIMARY KEY,
            bolum_notu TEXT,             -- bölüm (section) notu
            fasil_notu TEXT,             -- fasıl (chapter) notu
            tam_metin TEXT,              -- bolum + fasıl birleşik
            kelime_sayisi INTEGER,
            dosya_adi TEXT
        )
    """)

    c.execute("CREATE INDEX idx_gtip_clean ON gtip(gtip_clean)")
    c.execute("CREATE INDEX idx_gtip_fasil ON gtip(fasil)")
    c.execute("CREATE INDEX idx_gtip_pozisyon ON gtip(pozisyon)")
    c.execute("CREATE INDEX idx_gtip_alt_pozisyon ON gtip(alt_pozisyon)")
    c.execute("CREATE INDEX idx_pozisyon_kod ON pozisyon(kod_clean)")

    c.execute("""
        CREATE VIRTUAL TABLE gtip_fts
        USING fts5(gtip_code, tanim, tanim_hiyerarsi, content=gtip, content_rowid=id)
    """)

    c.execute("""
        CREATE VIRTUAL TABLE notlar_fts
        USING fts5(fasil_no, tam_metin)
    """)

    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Fasıl (tarife) parser
# ---------------------------------------------------------------------------

def parse_fasil_xls(filepath):
    """
    Tek bir fasıl XLS dosyasını parse et.
    Returns: (fasil_no, gtip_rows, pozisyon_rows)
    """
    wb = xlrd.open_workbook(filepath)
    sh = wb.sheet_by_index(0)

    entries = []
    for r in range(sh.nrows):
        pos = str(sh.cell_value(r, 0)).strip()
        desc = str(sh.cell_value(r, 1)).strip()
        olcu = str(sh.cell_value(r, 2)).strip() if sh.ncols > 2 else ""

        if olcu and olcu.replace('.', '').replace('0', '') == '':
            olcu = ""

        entries.append((pos, desc, olcu))

    merged = []
    for pos, desc, olcu in entries:
        if pos == "" and desc != "" and merged:
            prev = merged[-1]
            merged[-1] = (prev[0], prev[1] + " " + desc, prev[2] or olcu)
        elif pos != "" or desc != "":
            merged.append((pos, desc, olcu))

    fasil_no = None
    m = re.search(r'(\d+)', os.path.basename(filepath))
    if m:
        fasil_no = int(m.group(1))

    gtip_rows = []
    pozisyon_rows = []
    hierarchy = {}

    for pos, desc, olcu in merged:
        clean = pos.replace(".", "").replace(" ", "")

        if not clean.isdigit() or len(clean) < 4:
            continue

        dash_count = 0
        for ch in desc:
            if ch == '-':
                dash_count += 1
            elif ch != ' ':
                break

        clean_desc = re.sub(r'^[\s\-]+', '', desc).strip()
        if clean_desc.endswith(':'):
            clean_desc = clean_desc[:-1].strip()

        if len(clean) == 12:
            pozisyon_4 = clean[:4]
            alt_poz_6 = clean[:6]

            parts = []
            for lvl in sorted(hierarchy.keys()):
                if lvl < dash_count:
                    parts.append(hierarchy[lvl])
            parts.append(clean_desc)
            tanim_hiyerarsi = " > ".join(parts)

            gtip_rows.append({
                'gtip_code': pos,
                'gtip_clean': clean,
                'fasil': fasil_no,
                'pozisyon': pozisyon_4[:2] + "." + pozisyon_4[2:],
                'alt_pozisyon': alt_poz_6,
                'tanim': clean_desc,
                'tanim_hiyerarsi': tanim_hiyerarsi,
                'olcu_birimi': olcu if olcu != '-' else '',
                'seviye': dash_count
            })
        elif len(clean) in (4, 6, 8, 10):
            pozisyon_rows.append({
                'kod': pos,
                'kod_clean': clean,
                'fasil': fasil_no,
                'tanim': clean_desc,
                'seviye': len(clean)
            })

            hierarchy[dash_count] = clean_desc
            for k in list(hierarchy.keys()):
                if k > dash_count:
                    del hierarchy[k]

    return fasil_no, gtip_rows, pozisyon_rows


# ---------------------------------------------------------------------------
# Fasıl notları parser
# ---------------------------------------------------------------------------

def parse_fasil_notu(filepath):
    """
    Fasıl notu XLS dosyasını parse et.
    Bölüm notu (BÖLÜM header'ından FASIL header'ına kadar) ve
    fasıl notunu (FASIL header'ından sonrası) ayırır.
    Returns: (fasil_no, bolum_notu, fasil_notu, tam_metin)
    """
    wb = xlrd.open_workbook(filepath)
    sh = wb.sheet_by_index(0)

    lines = []
    for r in range(sh.nrows):
        row_parts = []
        for c_idx in range(sh.ncols):
            v = str(sh.cell_value(r, c_idx)).strip()
            if v:
                row_parts.append(v)
        if row_parts:
            lines.append(" ".join(row_parts))
        else:
            lines.append("")

    fasil_no = None
    m = re.search(r'(\d+)', os.path.basename(filepath))
    if m:
        fasil_no = int(m.group(1))

    # "FASIL XX" satırını bul — bölüm/fasıl ayırıcı
    fasil_line_idx = None
    for i, line in enumerate(lines):
        if re.match(r'^FAS[IİıiÝ]L\s+\S', line, re.IGNORECASE):
            fasil_line_idx = i
            break

    if fasil_line_idx is not None and fasil_line_idx > 0:
        bolum_text = "\n".join(lines[:fasil_line_idx]).strip()
        fasil_text = "\n".join(lines[fasil_line_idx:]).strip()
    else:
        bolum_text = ""
        fasil_text = "\n".join(lines).strip()

    tam_metin = (bolum_text + "\n" + fasil_text).strip() if bolum_text else fasil_text

    return fasil_no, bolum_text, fasil_text, tam_metin


# ---------------------------------------------------------------------------
# DB insert
# ---------------------------------------------------------------------------

def insert_tarife(conn, fasil_no, gtip_rows, pozisyon_rows, filename):
    """Tarife verilerini veritabanına ekle."""
    c = conn.cursor()

    c.execute("DELETE FROM gtip WHERE fasil = ?", (fasil_no,))
    c.execute("DELETE FROM pozisyon WHERE fasil = ?", (fasil_no,))
    c.execute("DELETE FROM fasil_meta WHERE fasil_no = ?", (fasil_no,))

    for row in gtip_rows:
        c.execute("""
            INSERT OR REPLACE INTO gtip
            (gtip_code, gtip_clean, fasil, pozisyon, alt_pozisyon,
             tanim, tanim_hiyerarsi, olcu_birimi, seviye)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row['gtip_code'], row['gtip_clean'], row['fasil'],
            row['pozisyon'], row['alt_pozisyon'], row['tanim'],
            row['tanim_hiyerarsi'], row['olcu_birimi'], row['seviye']
        ))

    for row in pozisyon_rows:
        c.execute("""
            INSERT OR REPLACE INTO pozisyon (kod, kod_clean, fasil, tanim, seviye)
            VALUES (?, ?, ?, ?, ?)
        """, (row['kod'], row['kod_clean'], row['fasil'], row['tanim'], row['seviye']))

    c.execute("""
        INSERT OR REPLACE INTO fasil_meta (fasil_no, dosya_adi, satir_sayisi, gtip_sayisi)
        VALUES (?, ?, ?, ?)
    """, (fasil_no, filename, len(gtip_rows) + len(pozisyon_rows), len(gtip_rows)))

    conn.commit()
    return len(gtip_rows), len(pozisyon_rows)


def insert_notlar(conn, fasil_no, bolum_notu, fasil_notu, tam_metin, filename):
    """Fasıl notlarını veritabanına ekle."""
    c = conn.cursor()
    c.execute("DELETE FROM fasil_notlari WHERE fasil_no = ?", (fasil_no,))
    kelime = len(tam_metin.split())
    c.execute("""
        INSERT INTO fasil_notlari (fasil_no, bolum_notu, fasil_notu, tam_metin, kelime_sayisi, dosya_adi)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (fasil_no, bolum_notu, fasil_notu, tam_metin, kelime, filename))
    conn.commit()
    return kelime


def rebuild_fts(conn):
    """FTS indekslerini yeniden oluştur."""
    c = conn.cursor()
    c.execute("INSERT INTO gtip_fts(gtip_fts) VALUES('rebuild')")
    c.execute("DELETE FROM notlar_fts")
    rows = c.execute("SELECT fasil_no, tam_metin FROM fasil_notlari").fetchall()
    for fno, metin in rows:
        c.execute("INSERT INTO notlar_fts (fasil_no, tam_metin) VALUES (?, ?)", (str(fno), metin))
    conn.commit()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(conn):
    """Veritabanı istatistiklerini yazdır."""
    c = conn.cursor()

    total_gtip = c.execute("SELECT COUNT(*) FROM gtip").fetchone()[0]
    total_poz = c.execute("SELECT COUNT(*) FROM pozisyon").fetchone()[0]
    total_fasil = c.execute("SELECT COUNT(*) FROM fasil_meta").fetchone()[0]
    total_notlar = c.execute("SELECT COUNT(*) FROM fasil_notlari").fetchone()[0]
    total_kelime = c.execute("SELECT COALESCE(SUM(kelime_sayisi), 0) FROM fasil_notlari").fetchone()[0]

    print(f"\n{'='*50}")
    print(f"VERİTABANI ÖZETİ")
    print(f"{'='*50}")
    print(f"Toplam fasıl       : {total_fasil}")
    print(f"Toplam GTİP        : {total_gtip}")
    print(f"Toplam pozisyon    : {total_poz}")
    print(f"Fasıl notları      : {total_notlar} ({total_kelime:,} kelime)")
    print(f"{'='*50}")

    print(f"\nFasıl detayları:")
    rows = c.execute("""
        SELECT fasil_no, dosya_adi, gtip_sayisi
        FROM fasil_meta ORDER BY fasil_no
    """).fetchall()
    for fasil_no, dosya, gtip_sayi in rows:
        print(f"  Fasıl {fasil_no:2d}: {gtip_sayi:5d} GTİP  ({dosya})")

    print(f"\nÖrnek arama: 'plastik':")
    rows = c.execute("""
        SELECT gtip_code, tanim FROM gtip_fts
        WHERE gtip_fts MATCH 'plastik' LIMIT 5
    """).fetchall()
    for code, tanim in rows:
        print(f"  {code}: {tanim[:70]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='GTİP Veritabanı Oluşturucu',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Örnekler:
  python build_db.py data/fasil_dosyalari/
  python build_db.py data/fasil_dosyalari/ --notlar data/fasil_notlari/
  python build_db.py data/fasil_dosyalari/ --notlar data/fasil_notlari/ --db data/gtip_2026.db
        """
    )
    parser.add_argument('input', help='Fasıl XLS dosyası veya klasör yolu')
    parser.add_argument('--notlar', help='Fasıl notları klasör yolu')
    parser.add_argument('--db', default='data/gtip_2026.db', help='SQLite DB çıktı yolu (varsayılan: data/gtip_2026.db)')
    parser.add_argument('--force', action='store_true', help='Mevcut DB varsa üzerine yaz')
    args = parser.parse_args()

    db_path = args.db

    if os.path.exists(db_path) and not args.force:
        print(f"Hata: {db_path} zaten mevcut. Üzerine yazmak için --force kullanın.")
        sys.exit(1)

    # Tarife dosyaları
    if os.path.isdir(args.input):
        files = sorted(
            glob.glob(os.path.join(args.input, '*.xls')) +
            glob.glob(os.path.join(args.input, '*.xlsx'))
        )
    elif os.path.isfile(args.input):
        files = [args.input]
    else:
        print(f"Hata: {args.input} bulunamadı")
        sys.exit(1)

    if not files:
        print(f"Hata: {args.input} içinde XLS dosyası bulunamadı")
        sys.exit(1)

    # Not dosyaları
    note_files = []
    if args.notlar:
        if os.path.isdir(args.notlar):
            note_files = sorted(
                glob.glob(os.path.join(args.notlar, '*.xls')) +
                glob.glob(os.path.join(args.notlar, '*.xlsx'))
            )
        else:
            print(f"Uyarı: {args.notlar} klasörü bulunamadı, notlar atlanıyor.")

    print(f"Veritabanı   : {db_path}")
    print(f"Fasıl dosyası: {len(files)}")
    print(f"Not dosyası  : {len(note_files)}")
    print()

    conn = create_db(db_path)

    # --- Tarife parse ---
    total_gtip = 0
    total_poz = 0
    errors = []

    for filepath in files:
        filename = os.path.basename(filepath)
        try:
            fasil_no, gtip_rows, pozisyon_rows = parse_fasil_xls(filepath)
            n_gtip, n_poz = insert_tarife(conn, fasil_no, gtip_rows, pozisyon_rows, filename)
            total_gtip += n_gtip
            total_poz += n_poz
            print(f"  + Fasıl {fasil_no:2d}: {n_gtip:5d} GTİP, {n_poz:3d} pozisyon  ({filename})")
        except Exception as e:
            errors.append((filename, str(e)))
            print(f"  ! HATA: {filename} -> {e}")

    # --- Notlar parse ---
    total_kelime = 0
    for filepath in note_files:
        filename = os.path.basename(filepath)
        try:
            fasil_no, bolum, fasil, tam = parse_fasil_notu(filepath)
            kelime = insert_notlar(conn, fasil_no, bolum, fasil, tam, filename)
            total_kelime += kelime
            print(f"  + Not {fasil_no:2d}: {kelime:5d} kelime  ({filename})")
        except Exception as e:
            errors.append((filename, str(e)))
            print(f"  ! HATA: {filename} -> {e}")

    # --- FTS rebuild ---
    rebuild_fts(conn)

    # --- Sonuç ---
    print_stats(conn)

    if errors:
        print(f"\n! {len(errors)} dosyada hata oluştu:")
        for fname, err in errors:
            print(f"  {fname}: {err}")

    conn.close()
    print(f"\nVeritabanı kaydedildi: {db_path}")


if __name__ == "__main__":
    main()
