"""
GTIP Siniflandirici
=====================
TEMU scraper ciktisi veya manuel doldurulmus Excel (product url, product title,
product details, ...) ile GTIP siniflandirma; Claude + SQLite cetvel.

Kullanim:
    python gtip_matcher.py output/input_scraped.xlsx
    python gtip_matcher.py output/input_scraped.xlsx --db data/gtip_2026.db -o output/classified.xlsx
    python gtip_matcher.py input.xlsx --refine --note-chars 6000
    python gtip_matcher.py input.xlsx --model claude-sonnet-4-20250514 --max-tokens 1600
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
    print("openpyxl yuklu degil: pip install openpyxl")
    sys.exit(1)

try:
    import anthropic
except ImportError:
    print("anthropic yuklu degil: pip install anthropic")
    sys.exit(1)


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

_TEMU_STOP = frozenset({
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'your', 'are', 'you', 'all', 'any',
    'can', 'has', 'have', 'pcs', 'pack', 'set', 'piece', 'pieces', 'item', 'items', 'sale',
    'shop', 'temu', 'free', 'new', 'hot', 'best', 'buy', 'get', 'one', 'two', 'off', 'out',
    'our', 'was', 'not', 'but', 'its', 'per', 'use', 'may', 'more', 'most', 'some', 'size',
})


def get_candidate_fasils(conn, product_details, keywords, description, title, max_fasils=8):
    """
    Aday fasillari belirle: urun metninden kelimeler -> FTS (gtip_fts + notlar_fts) ile
    eslesen satirlarin fasil numaralari. Material hints kaldirildi — tarife mantığına aykırı.
    """
    text = f"{title} {description} {keywords} {product_details}".lower()
    scores = {}

    words = sorted(
        set(re.findall(r'[a-zA-ZğüşıöçĞÜŞİÖÇ]{4,}', text)),
        key=len,
        reverse=True,
    )
    words = [w for w in words if w.lower() not in _TEMU_STOP][:14]

    for w in words:
        rows = search_gtip_fts(conn, w, limit=18)
        for r in rows:
            code = r[0]
            parts = str(code).split('.')
            if not parts or not parts[0].isdigit():
                continue
            fn = int(parts[0])
            scores[fn] = scores.get(fn, 0) + 1

    ordered = sorted(scores.keys(), key=lambda x: (-scores[x], x))
    out = []
    seen = set()
    for fn in ordered:
        if fn not in seen:
            seen.add(fn)
            out.append(fn)
        if len(out) >= max_fasils:
            return out

    defaults = [39, 73, 82, 83, 84, 85, 90, 94, 96, 61, 62, 33, 42, 95, 87, 71, 91, 48, 64]
    for d in defaults:
        if d not in seen:
            seen.add(d)
            out.append(d)
        if len(out) >= max_fasils:
            break
    return out


def get_fasil_gtip_list(conn, fasil_no, limit=200):
    c = conn.cursor()
    rows = c.execute("""
        SELECT gtip_code, tanim, tanim_hiyerarsi
        FROM gtip WHERE fasil = ?
        ORDER BY gtip_code
        LIMIT ?
    """, (fasil_no, limit)).fetchall()
    return rows


def get_fasil_notu(conn, fasil_no):
    c = conn.cursor()
    row = c.execute(
        "SELECT fasil_notu FROM fasil_notlari WHERE fasil_no = ?",
        (fasil_no,)
    ).fetchone()
    return row[0] if row else ""


def get_all_pozisyonlar(conn, fasil_no):
    """
    Fasılın tüm 4'lü pozisyonlarını döner.

    Tarife cetveli yarı-düzenlidir: bazı pozisyonlar cetvelde 4'lü değil
    doğrudan 6'lı seviyede başlar (örn. 9602.00, 9605.00). Bu durumda
    pozisyon tablosunda seviye=4 kaydı olmaz ama gtip tablosunda ilgili
    12'liler mevcuttur. Yalnızca pozisyon tablosuna bağlı kalmak bu
    pozisyonları Step 1'den gizler ve Step 2'ye geçilmesini engeller.

    Çözüm: gtip tablosundan substr(gtip_clean,1,4) ile sentetik 4'lü
    gruplar türet, açıklama için pozisyon tablosuna bak, yoksa gtip
    hiyerarşi metnini kullan.
    """
    c = conn.cursor()
    # 1) Fasıldaki tüm benzersiz 4-hane öneklerini GTİP tablosundan çıkar
    prefixes = [
        row[0]
        for row in c.execute("""
            SELECT DISTINCT substr(gtip_clean, 1, 4) AS p
            FROM gtip
            WHERE fasil = ?
            ORDER BY p
        """, (fasil_no,)).fetchall()
    ]
    result = []
    for pref in prefixes:
        # 2a) pozisyon tablosunda bu önekle başlayan en kısa kaydı ara
        row = c.execute("""
            SELECT tanim FROM pozisyon
            WHERE fasil = ? AND substr(kod_clean, 1, 4) = ?
            ORDER BY seviye
            LIMIT 1
        """, (fasil_no, pref)).fetchone()
        if row:
            result.append((pref, row[0]))
        else:
            # gtip tablosundan tanim_hiyerarsi ile temsil et
            g = c.execute("""
                SELECT tanim_hiyerarsi FROM gtip
                WHERE fasil = ? AND substr(gtip_clean, 1, 4) = ?
                ORDER BY gtip_code
                LIMIT 1
            """, (fasil_no, pref)).fetchone()
            if g:
                result.append((pref, g[0] or pref))
    return result


def get_gtips_by_pozisyon(conn, pozisyon_kod):
    """Seçilen 4'lü pozisyon altındaki tüm 12'li GTİP'leri döner."""
    clean = re.sub(r'[^0-9]', '', str(pozisyon_kod))[:4]
    c = conn.cursor()
    return c.execute("""
        SELECT gtip_code, tanim, tanim_hiyerarsi FROM gtip
        WHERE gtip_clean LIKE ?
        ORDER BY gtip_code
    """, (clean + '%',)).fetchall()


def get_izahname(conn, fasil_no, max_chars=3000):
    """Fasıl izahname metnini döner (kırpılmış)."""
    c = conn.cursor()
    row = c.execute(
        "SELECT metin FROM izahname_notlari WHERE fasil_no = ?",
        (fasil_no,)
    ).fetchone()
    if not row or not row[0]:
        return ""
    return row[0][:max_chars]


def get_yorum_kurallari(conn):
    """Tüm yorum kurallarını tek metin olarak döner (özet)."""
    c = conn.cursor()
    rows = c.execute(
        "SELECT kural_no, metin FROM yorum_kurallari ORDER BY kural_no"
    ).fetchall()
    parts = []
    for kural_no, metin in rows:
        parts.append(f"KURAL {kural_no}:\n{(metin or '')[:600]}")
    return "\n\n".join(parts)


_TAXONOMY_CACHE = {}
_PROMPT_CACHE = {}

def get_fasil_taxonomy(conn, note_chars=300):
    """
    bolum_fasil tablosundan tüm bölüm→fasıl hiyerarşisini döner.
    Her fasıl için kısa fasıl notu özeti de eklenir. Sonuç cache'lenir.
    """
    cache_key = note_chars
    if cache_key in _TAXONOMY_CACHE:
        return _TAXONOMY_CACHE[cache_key]

    c = conn.cursor()
    rows = c.execute(
        "SELECT bolum_no, bolum_adi, fasil_no, fasil_adi FROM bolum_fasil ORDER BY bolum_no, fasil_no"
    ).fetchall()

    parts = []
    current_bolum = None
    for bolum_no, bolum_adi, fasil_no, fasil_adi in rows:
        if bolum_no != current_bolum:
            parts.append(f"\nBÖLÜM {bolum_no}: {bolum_adi}")
            current_bolum = bolum_no
        if note_chars > 0:
            note = get_fasil_notu(conn, fasil_no)
            note_excerpt = f" | {note[:note_chars]}" if note else ""
        else:
            note_excerpt = ""
        parts.append(f"  Fasıl {fasil_no:02d}: {fasil_adi}{note_excerpt}")

    result = "\n".join(parts)
    _TAXONOMY_CACHE[cache_key] = result
    return result


def build_fasil_prompt(conn):
    if 'fasil' in _PROMPT_CACHE:
        return _PROMPT_CACHE['fasil']
    kurallar = get_yorum_kurallari(conn)
    blok = f"TARIFEYE ILISKIN GENEL YORUMLAMA KURALLARI:\n{kurallar}" if kurallar else ""
    result = _FASIL_PROMPT_BASE.format(kurallar_blok=blok)
    _PROMPT_CACHE['fasil'] = result
    return result


def build_pozisyon_prompt(conn):
    if 'pozisyon' in _PROMPT_CACHE:
        return _PROMPT_CACHE['pozisyon']
    kurallar = get_yorum_kurallari(conn)
    blok = f"TARIFEYE ILISKIN GENEL YORUMLAMA KURALLARI:\n{kurallar}" if kurallar else ""
    result = _POZISYON_PROMPT_BASE.format(kurallar_blok=blok)
    _PROMPT_CACHE['pozisyon'] = result
    return result


def search_gtip_fts(conn, query, limit=20):
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


def _product_search_words(title, desc, keywords, product_details, max_words=20):
    text = f"{title} {desc} {keywords} {product_details}".lower()
    words = sorted(
        set(re.findall(r'[a-zA-ZğüşıöçĞÜŞİÖÇ]{4,}', text)),
        key=len,
        reverse=True,
    )
    return [w for w in words if w.lower() not in _TEMU_STOP][:max_words]


def retrieve_ranked_gtips(conn, title, desc, keywords, product_details, top_n=50, per_query=14):
    """
    Urun metninden kelimeler -> FTS; skorla birlestir. Cetvelde gercek satirlari getirir
    (sadece fasil basi sirali liste yerine ilgili 392x/732x vb. satirlari modele sunar).
    """
    words = _product_search_words(title, desc, keywords, product_details, max_words=22)
    scores = {}
    for w in words:
        rows = search_gtip_fts(conn, w, limit=per_query)
        for idx, r in enumerate(rows):
            code = r[0]
            bump = max(1, per_query - idx)
            scores[code] = scores.get(code, 0) + bump
    if not scores:
        return []
    ordered = sorted(scores.keys(), key=lambda c: (-scores[c], c))[:top_n]
    cur = conn.cursor()
    out = []
    for code in ordered:
        row = cur.execute(
            "SELECT gtip_code, tanim, tanim_hiyerarsi FROM gtip WHERE gtip_code = ?",
            (code,),
        ).fetchone()
        if row:
            out.append(row)
    return out


def _json_from_balanced_braces(s):
    if not s:
        return None
    start = s.find('{')
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    quote_ch = ''
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == quote_ch:
                in_str = False
            continue
        if c in '"\'':
            in_str = True
            quote_ch = c
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def extract_first_json_object(text):
    """
    Claude yaniti: ```json ... ``` bloklari veya metindeki ilk dengeli JSON nesnesi.
    """
    if not text:
        return None
    s = text.strip()
    for block in re.findall(r'```(?:json)?\s*(.*?)```', s, re.DOTALL | re.IGNORECASE):
        got = _json_from_balanced_braces(block.strip())
        if got is not None:
            return got
    return _json_from_balanced_braces(s)


_GTIP_RE = re.compile(r'^\d{4}\.\d{2}\.\d{2}\.\d{2}\.\d{2}$')


def normalize_gtip_code(code):
    """Turk cetveli formati: XX.XX.XX.XX.XX"""
    if not code:
        return None
    s = str(code).strip().replace(' ', '').replace(',', '.')
    if not _GTIP_RE.match(s):
        return None
    return s


def gtip_exists(conn, code):
    if not code:
        return False
    row = conn.execute('SELECT 1 FROM gtip WHERE gtip_code = ?', (code,)).fetchone()
    return row is not None


def sanitize_classification(conn, result):
    """
    Claude bazen HS kalibinda ama TR cetvelinde olmayan kodlar uydurur (or. 3926.90.99.00.00).
    Sadece SQLite gtip tablosunda gercekten var olan kodlari birakir.
    """
    if not isinstance(result, dict):
        return result
    out = dict(result)

    raw_main = out.get('gtip_code', '') or ''
    norm = normalize_gtip_code(raw_main)
    if norm and gtip_exists(conn, norm):
        out['gtip_code'] = norm
    else:
        if raw_main:
            warn = f" [Model onerdigi kod veritabaninda yok: {raw_main}]"
            out['gerekce'] = (out.get('gerekce', '') + warn)[:2500]
            out['guven'] = 'dusuk'
        out['gtip_code'] = ''

    alts = out.get('alternatifler', [])
    if isinstance(alts, list):
        valid = []
        seen = set()
        main = out.get('gtip_code') or ''
        for a in alts:
            n = normalize_gtip_code(a)
            if n and gtip_exists(conn, n) and n not in seen and n != main:
                seen.add(n)
                valid.append(n)
        out['alternatifler'] = valid
    return out


# ---------------------------------------------------------------------------
# Claude API classification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Sen deneyimli bir Turk Gumruk Tarife siniflandirma uzmanisin. Girdi: urun tanimi +
TARIFE CETVELI VERILERI (fasil notlari ve GTIP satirlari). Cikti: tek bir 12 haneli kod + gerekce.

GENEL MUHAKEME (urun adi ezberleme yok; her kalemi basligin yasal tanimina gore ele):

1) ASIL FONKSIYON: Urun ne yapar? (mekanik tutma, olcu, yazi, giyim, elektriksel iletkenlik/yalitim,
   gida ile temas, insaat/yapi, oyuncak, vb.) Süs/marka/SEO metni fonksiyonu degistirmez.

2) SPESIFIKLIK: Ayni fasil icinde veya komsu basliklarda daha ozel bir alt pozisyon var mi?
   Varsa onu tercih et; "Digerleri"yi son care olarak kullan.

3) ELEME (kisa mantik): Yanlis sinif secimlerini cetveldeki BASLIK TANIMINA gore ele:
   - Ayni fasilda yan yana duran alt pozisyonlarin yasal kapsamlari farkli olabilir; hangi kodun
     gectigini FASIL NOTU ve listedeki kod/hiyerarsi tanimlarindan cikar. Daha "dar" veya "ozel"
     gorunen bir kodu, metindeki tanim urunle uyusmuyorsa secme; bir alt basligi baska birinin
     yerine koyma.
   - Pazarlama, magaza kategorisi veya SEO etiketi gumruk basliginin yasal anlamini degistirmez.
   - Teknik fonksiyon gerektiren basliklar: urun o fonksiyonu gercekten saglamiyorsa o basligi ele.
   - Baska fasilda cetvel metnine gore daha uygun ozel baslik varsa genel "diger"den once onu dusun.

4) FASIL NOTLARI VE CETVEL METNI: Birincil kaynak bu mesajdaki fasil notlari ile GTIP satir
   tanimlaridir; acik dahil/haric ifadelerini aynen uygula.

5) GEREKCE (Turkce, 3-5 cumle, madde isareti kullanabilirsin):
   - Secilen pozisyonun urunun ana fonksiyonuyla uyumu.
   - En az bir mantikli alternatif baslik veya fasil (genelde komsu veya sik karisan) ve NIYE
     uygun olmadigi: baslik tanimindaki zorunlu ozellik (or. yalitim, gida temasi, insaat malzemesi)
     urunda yoksa veya dar kapsam disinda kaliyorsa soyle. Urun metninde gecmeyen kelime,
     ornek urun veya kategori UYDURMA (ornek: baska bir urun adi yazmak yasak).

6) KOD KAYNAGI: gtip_code ve alternatifler SADECE bu mesajdaki TARIFE CETVELI listesindeki
   satirlardan birebir kopya olmali. Listede yoksa uydurma; en yakin gercek satiri sec veya bos birak.

7) ONCELIKLI GTIP (varsa): "METNE GORE ONCELIKLI" bolumu yalnizca metin-kelime eslesmesiyle
   siralanmistir; tek basina yeterli degildir. Nihai kod mutlaka fasil notu ve satir tanimiyla
   uyumlu olmali; celisirse cetvel metnini esas al.

Yanitini SADECE su JSON formatinda ver:
{
  "gtip_code": "XXXX.XX.XX.XX.XX",
  "fasil": 39,
  "gerekce": "Turkce muhakeme metni",
  "guven": "yuksek|orta|dusuk",
  "alternatifler": ["YYYY.YY.YY.YY.YY"]
}"""

REFINE_SYSTEM_PROMPT = """Ayni gorev: Turk gumruk GTIP siniflandirmasi. Onceki JSON cevabi zayif veya eksik olabilir.
TARIFE metnini tekrar dikkatle uygula; gtip_code ve alternatifler SADECE mesajdaki listede var olan
12 haneli kodlardan secilsin. Yanit SADECE gecerli JSON (gtip_code, fasil, gerekce, guven, alternatifler)."""

_FASIL_PROMPT_BASE = """Sen deneyimli bir Turk Gumruk Tarife siniflandirma uzmanisin.
Gorev: Asagidaki tarife taksonomisini kullanarak urun icin 3-5 aday FASIL belirle.

{kurallar_blok}

Yanitini SADECE su JSON formatinda ver:
{{
  "aday_fasiller": [96, 33, 71],
  "gerekce": "Kisa Turkce gerekce (2-3 cumle)"
}}"""

_POZISYON_PROMPT_BASE = """Sen deneyimli bir Turk Gumruk Tarife siniflandirma uzmanisin.
Gorev: Urun icin dogru FASIL ve 4 haneli POZİSYONU belirle.

Verilen fasil notlari, izahname ve pozisyon listesine gore en uygun pozisyonu sec.

{kurallar_blok}

- Fasil notu ve izahname dahil/haric hukumlerini aynen uygula.
- En ozel pozisyonu sec; "Digerleri"ni son care olarak kullan.
- Listede olmayan pozisyon uydurma.

Yanitini SADECE su JSON formatinda ver:
{{
  "fasil": 84,
  "pozisyon_kod": "84.73",
  "gerekce": "Kisa Turkce gerekce (2-3 cumle)"
}}"""


def _needs_refine(cls):
    if cls.get('error') or cls.get('parse_hatasi'):
        return False
    if not cls.get('gtip_code'):
        return True
    g = (cls.get('guven') or '').lower()
    return g in ('dusuk', 'orta')


def build_tarife_context(
    conn,
    title,
    desc,
    keywords,
    product_details,
    note_max_chars,
    gtip_rows_per_fasil,
    retrieval_top_n,
):
    ranked = retrieve_ranked_gtips(
        conn, title, desc, keywords, product_details, top_n=retrieval_top_n
    )
    parts = []
    if ranked:
        rlines = "\n".join(
            f"  {g[0]}  {g[1]}" + (f"  [{g[2]}]" if g[2] else "")
            for g in ranked
        )
        parts.append(f"=== METNE GORE ONCELIKLI GTIP (FTS skor) ===\n{rlines}")

    candidate_fasils = get_candidate_fasils(conn, product_details, keywords, desc, title)
    for fno in candidate_fasils[:6]:
        gtips = get_fasil_gtip_list(conn, fno, limit=200)
        if not gtips:
            continue
        note = get_fasil_notu(conn, fno)
        excerpt = (note[:note_max_chars] if note else "(not yok)")

        gtip_lines = "\n".join(
            f"  {g[0]}  {g[1]}" + (f"  [{g[2]}]" if g[2] else "")
            for g in gtips[:gtip_rows_per_fasil]
        )
        parts.append(
            f"=== FASIL {fno} ===\n"
            f"FASIL NOTU:\n{excerpt}\n\n"
            f"GTIP KODLARI:\n{gtip_lines}"
        )

    return "\n\n".join(parts), candidate_fasils


def build_pozisyon_context(conn, candidate_fasils, title, desc, keywords,
                           product_details, note_max_chars, retrieval_top_n,
                           izahname_max_chars=1500):
    """Adım 1 context: fasıl notları + izahname özeti + tüm 4'lü pozisyonlar."""
    ranked = retrieve_ranked_gtips(
        conn, title, desc, keywords, product_details, top_n=min(retrieval_top_n, 20)
    )
    parts = []
    if ranked:
        rlines = "\n".join(f"  {g[0]}  {g[1]}" for g in ranked)
        parts.append(f"=== METNE GORE FTS REFERANS ===\n{rlines}")

    for fno in candidate_fasils[:5]:
        pozlar = get_all_pozisyonlar(conn, fno)
        if not pozlar:
            continue
        note = get_fasil_notu(conn, fno)
        excerpt = (note[:note_max_chars] if note else "(not yok)")
        izahname = get_izahname(conn, fno, izahname_max_chars)
        poz_lines = "\n".join(f"  {p[0]}  {p[1]}" for p in pozlar)
        block = (
            f"=== FASIL {fno} ===\n"
            f"FASIL NOTU:\n{excerpt}\n\n"
        )
        if izahname:
            block += f"IZAHNAME (ozet):\n{izahname}\n\n"
        block += f"4'LU POZISYONLAR:\n{poz_lines}"
        parts.append(block)

    return "\n\n".join(parts)


def _get_ara_pozisyonlar(conn, fasil_no, poz4_clean):
    """4'lü pozisyon altındaki ara seviye (6,8,10 hane) pozisyonları döner: {kod_clean: tanim}"""
    c = conn.cursor()
    rows = c.execute("""
        SELECT kod_clean, tanim FROM pozisyon
        WHERE fasil = ?
          AND length(kod_clean) > 4
          AND length(kod_clean) < 12
          AND substr(kod_clean, 1, 4) = ?
        ORDER BY length(kod_clean), kod_clean
    """, (fasil_no, poz4_clean)).fetchall()
    return {r[0]: r[1] for r in rows}


def _format_gtip_grouped(gtips, ara_pozlar):
    """12'li GTİP listesini ara pozisyon başlıklarıyla gruplu formatlar."""
    if not ara_pozlar:
        return "\n".join(f"  {g[0]}  {g[1]}" for g in gtips)

    def best_header(gtip_clean):
        best = None
        for ara_clean in ara_pozlar:
            if gtip_clean.startswith(ara_clean):
                if best is None or len(ara_clean) > len(best):
                    best = ara_clean
        return best

    lines = []
    last_header = None
    for g in gtips:
        gtip_clean = re.sub(r'[^0-9]', '', g[0])
        header = best_header(gtip_clean)
        if header and header != last_header:
            lines.append(f"\n  [{header}]  {ara_pozlar[header]}")
            last_header = header
        lines.append(f"    {g[0]}  {g[1]}")

    return "\n".join(lines).lstrip("\n")


def build_gtip_context(conn, fasil_no, pozisyon_kod, note_max_chars, izahname_max_chars):
    """Adım 2 context: fasıl notu + seçilen pozisyonun tüm 12'lileri (ara seviye başlıklı)."""
    poz4_clean = re.sub(r'[^0-9]', '', str(pozisyon_kod))[:4]
    gtips = get_gtips_by_pozisyon(conn, pozisyon_kod)
    note = get_fasil_notu(conn, fasil_no)
    izahname = get_izahname(conn, fasil_no, izahname_max_chars)

    parts = []
    if note:
        parts.append(f"=== FASIL {fasil_no} NOTU ===\n{note[:note_max_chars]}")
    if izahname:
        parts.append(f"=== FASIL {fasil_no} IZAHNAME (ozet) ===\n{izahname}")
    if gtips:
        ara_pozlar = _get_ara_pozisyonlar(conn, fasil_no, poz4_clean)
        gtip_block = _format_gtip_grouped(gtips, ara_pozlar)
        parts.append(f"=== POZISYON {pozisyon_kod} ALTINDAKI TUM GTIP'LER ===\n{gtip_block}")

    return "\n\n".join(parts), gtips


def _call_classify(client, model, max_tokens, system_prompt, user_msg):
    return client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )


def _api_call_with_retry(client, model, max_tokens, system_prompt, user_msg):
    """Rate limit retry mantığıyla API çağrısı yapar."""
    try:
        return _call_classify(client, model, max_tokens, system_prompt, user_msg)
    except anthropic.RateLimitError:
        for wait in [30, 60]:
            print(f"\n    Rate limit, {wait}s bekleniyor...", end="", flush=True)
            time.sleep(wait)
            try:
                return _call_classify(client, model, max_tokens, system_prompt, user_msg)
            except anthropic.RateLimitError:
                continue
        raise


def classify_product(client, product_info, conn, opts=None):
    """
    2-adımlı hiyerarşik sınıflandırma:
      Adım 1: Aday fasıllar → 4'lü pozisyon seçimi
      Adım 2: Seçilen pozisyon altındaki tüm 12'liler → GTİP seçimi

    opts: model, max_tokens, note_max_chars, izahname_max_chars,
          gtip_rows_per_fasil (fallback için), retrieval_top_n,
          refine, refine_model, refine_max_tokens
    """
    opts = opts or {}
    model               = opts.get('model', 'claude-haiku-4-5-20251001')
    max_tokens          = int(opts.get('max_tokens', 1200))
    note_max_chars      = int(opts.get('note_max_chars', 8000))
    izahname_max_chars  = int(opts.get('izahname_max_chars', 0))
    gtip_rows_per_fasil = int(opts.get('gtip_rows_per_fasil', 120))
    retrieval_top_n     = int(opts.get('retrieval_top_n', 50))
    do_refine           = bool(opts.get('refine'))
    refine_model        = opts.get('refine_model', 'claude-sonnet-4-20250514')
    refine_max_tokens   = int(opts.get('refine_max_tokens', 1200))

    title           = product_info.get('title', '')
    desc            = product_info.get('description', '')
    keywords        = product_info.get('keywords', '')
    product_details = product_info.get('product_details', '')
    sku_variants    = product_info.get('sku_variants', '')

    product_text = (
        f"Baslik: {title}\n"
        f"Aciklama: {desc}\n"
        f"Urun Detaylari: {product_details or '(belirtilmemis)'}\n"
        f"Varyantlar: {sku_variants or '(yok)'}"
    )

    # ------------------------------------------------------------------
    # ADIM 0 — Fasıl seçimi (taxonomy-driven, FTS yok)
    # ------------------------------------------------------------------
    taxonomy = get_fasil_taxonomy(conn, note_chars=0)
    fasil_user_msg = (
        f"Asagidaki urun icin dogru fasillari sec.\n\n"
        f"URUN BILGILERI:\n{product_text}\n\n"
        f"TURK GUMRUK TARIFE TAKSONOMISI:\n{taxonomy}\n\n"
        f"Yanitini SADECE JSON olarak ver."
    )
    fasil_system_prompt   = build_fasil_prompt(conn)
    pozisyon_system_prompt = build_pozisyon_prompt(conn)

    candidate_fasils = []
    try:
        fasil_resp = _api_call_with_retry(client, model, 400, fasil_system_prompt, fasil_user_msg)
        fasil_parsed = extract_first_json_object(fasil_resp.content[0].text)
        if fasil_parsed and fasil_parsed.get('aday_fasiller'):
            raw = fasil_parsed['aday_fasiller']
            candidate_fasils = [int(x) for x in raw if str(x).isdigit()][:5]
    except Exception:
        pass

    # Adım 0 başarısız olursa FTS fallback
    if not candidate_fasils:
        candidate_fasils = get_candidate_fasils(conn, product_details, keywords, desc, title)

    # ------------------------------------------------------------------
    # ADIM 1 — Pozisyon seçimi
    # ------------------------------------------------------------------
    poz_context = build_pozisyon_context(
        conn, candidate_fasils, title, desc, keywords,
        product_details, note_max_chars, retrieval_top_n,
        izahname_max_chars=izahname_max_chars
    )
    poz_user_msg = (
        f"Asagidaki urun icin dogru FASIL ve 4 haneli POZISYONU sec.\n\n"
        f"URUN BILGILERI:\n{product_text}\n\n"
        f"TARIFE CETVELI:\n{poz_context}\n\n"
        f"Yanitini SADECE JSON olarak ver."
    )

    poz_result = None
    try:
        poz_resp = _api_call_with_retry(client, model, 600, pozisyon_system_prompt, poz_user_msg)
        poz_parsed = extract_first_json_object(poz_resp.content[0].text)
        if poz_parsed and poz_parsed.get('pozisyon_kod'):
            poz_result = poz_parsed
    except Exception:
        pass

    # Pozisyon seçimi başarısızsa eski flat yönteme düş
    if not poz_result:
        return _classify_flat(client, product_info, conn, opts,
                              candidate_fasils, note_max_chars,
                              gtip_rows_per_fasil, retrieval_top_n)

    fasil_no     = poz_result.get('fasil')
    pozisyon_kod = str(poz_result.get('pozisyon_kod', '')).strip()

    gtips_check = get_gtips_by_pozisyon(conn, pozisyon_kod)
    if not gtips_check:
        return _classify_flat(client, product_info, conn, opts,
                              candidate_fasils, note_max_chars,
                              gtip_rows_per_fasil, retrieval_top_n)

    # ------------------------------------------------------------------
    # ADIM 2 — GTİP seçimi
    # ------------------------------------------------------------------
    gtip_context, _ = build_gtip_context(
        conn, fasil_no, pozisyon_kod, note_max_chars, izahname_max_chars
    )

    # Yorum kurallarını system prompt'a ekle
    kurallar = get_yorum_kurallari(conn)
    system_step2 = (
        f"TARIFEYE ILISKIN GENEL YORUMLAMA KURALLARI:\n{kurallar}\n\n---\n\n{SYSTEM_PROMPT}"
        if kurallar else SYSTEM_PROMPT
    )

    gtip_user_msg = (
        f"Asagidaki urun icin dogru 12 haneli GTIP kodunu belirle.\n\n"
        f"URUN BILGILERI:\n{product_text}\n"
        f"Secilen pozisyon: {pozisyon_kod} (Fasil {fasil_no})\n\n"
        f"Urun bilgisini ve asagidaki tarife metnini kullan.\n\n"
        f"TARIFE CETVELI VERILERI:\n{gtip_context}\n\n"
        f"Yanitini SADECE JSON olarak ver."
    )

    def run_step2(sys_p, mdl, mtok):
        resp = _api_call_with_retry(client, mdl, mtok, sys_p, gtip_user_msg)
        text = resp.content[0].text.strip()
        parsed = extract_first_json_object(text)
        if parsed is None:
            return {"gtip_code": "", "gerekce": text[:300], "guven": "dusuk",
                    "error": "JSON parse edilemedi", "parse_hatasi": True}
        return sanitize_classification(conn, parsed)

    try:
        out = run_step2(system_step2, model, max_tokens)
        out.pop('parse_hatasi', None)
        if do_refine and _needs_refine(out):
            refined = run_step2(REFINE_SYSTEM_PROMPT, refine_model, refine_max_tokens)
            refined.pop('parse_hatasi', None)
            if (not refined.get('error') and refined.get('gtip_code')
                    and gtip_exists(conn, refined['gtip_code'])):
                refined['gerekce'] = ('[Ikinci gecis] ' + str(refined.get('gerekce', '')))[:2500]
                return refined
        return out
    except Exception as e:
        return {"gtip_code": "", "gerekce": "", "guven": "", "error": str(e)[:100]}


def _classify_flat(client, product_info, conn, opts, candidate_fasils,
                   note_max_chars, gtip_rows_per_fasil, retrieval_top_n):
    """Eski tek-adımlı flat sınıflandırma (fallback)."""
    model             = opts.get('model', 'claude-haiku-4-5-20251001')
    max_tokens        = int(opts.get('max_tokens', 1200))
    do_refine         = bool(opts.get('refine'))
    refine_model      = opts.get('refine_model', 'claude-sonnet-4-20250514')
    refine_max_tokens = int(opts.get('refine_max_tokens', 1200))

    title           = product_info.get('title', '')
    desc            = product_info.get('description', '')
    keywords        = product_info.get('keywords', '')
    product_details = product_info.get('product_details', '')
    sku_variants    = product_info.get('sku_variants', '')

    tarife_context, _ = build_tarife_context(
        conn, title, desc, keywords, product_details,
        note_max_chars, gtip_rows_per_fasil, retrieval_top_n
    )
    user_msg = (
        f"Asagidaki urun icin dogru 12 haneli GTIP kodunu belirle.\n\n"
        f"URUN BILGILERI:\nBaslik: {title}\nAciklama: {desc}\n"
        f"Urun Detaylari: {product_details or '(belirtilmemis)'}\n"
        f"Varyantlar: {sku_variants or '(yok)'}\n\n"
        f"TARIFE CETVELI VERILERI:\n{tarife_context}\n\n"
        f"Yanitini SADECE JSON olarak ver."
    )

    def run_once(sys_p, mdl, mtok):
        resp = _api_call_with_retry(client, mdl, mtok, sys_p, user_msg)
        text = resp.content[0].text.strip()
        parsed = extract_first_json_object(text)
        if parsed is None:
            return {"gtip_code": "", "gerekce": text[:300], "guven": "dusuk",
                    "error": "JSON parse edilemedi", "parse_hatasi": True}
        return sanitize_classification(conn, parsed)

    try:
        out = run_once(SYSTEM_PROMPT, model, max_tokens)
        out.pop('parse_hatasi', None)
        if do_refine and _needs_refine(out):
            refined = run_once(REFINE_SYSTEM_PROMPT, refine_model, refine_max_tokens)
            refined.pop('parse_hatasi', None)
            if (not refined.get('error') and refined.get('gtip_code')
                    and gtip_exists(conn, refined['gtip_code'])):
                refined['gerekce'] = ('[Ikinci gecis] ' + str(refined.get('gerekce', '')))[:2500]
                return refined
        return out
    except Exception as e:
        return {"gtip_code": "", "gerekce": "", "guven": "", "error": str(e)[:100]}


# ---------------------------------------------------------------------------
# Excel I/O
# ---------------------------------------------------------------------------

def _slug_header(name):
    """Excel baslik hucrelerini karsilastirma icin normalize et."""
    if name is None:
        return ''
    s = str(name).lower()
    # Turkish character transliteration
    tr_map = str.maketrans('üöşıığçÜÖŞIİĞÇ', 'uosiigcuosiigc')
    s = s.translate(tr_map)
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')


def normalize_product_row(row):
    """
    Scraper satiri veya manuel import (or. product_url, product_title, category_path,
    thumbnail, product_details) -> classify_product / HTML icin tek sema.
    """
    by_slug = {}
    for k, v in row.items():
        if k is None:
            continue
        sk = _slug_header(k if isinstance(k, str) else str(k))
        by_slug[sk] = v if v is not None else ''

    def pick(*slugs):
        for s in slugs:
            if s in by_slug and str(by_slug[s]).strip():
                return str(by_slug[s]).strip()
        return ''

    url = pick('url', 'product_url', 'product_link', 'link')
    title = pick('title', 'product_title', 'urun_basligi', 'urun_adi',
                 'turkce_ticari_tanim', 'ticari_tanim', 'turkce_ticar_itanim', 'mal_tanimi')
    description = pick('description', 'aciklama', 'desc', 'urun_aciklamasi')
    keywords = pick('keywords', 'keyword', 'category_path', 'category', 'kategori')
    product_details = pick('product_details', 'urun_detaylari')
    if not product_details:
        for sk, val in by_slug.items():
            if 'product' in sk and 'detail' in sk and str(val).strip():
                product_details = str(val).strip()
                break

    image_url = pick('image_url', 'thumbnail_url', 'img_url', 'kucuk_resim_url', 'resim_url')
    if not image_url:
        for sk, val in by_slug.items():
            if 'thumbnail' in sk and str(val).strip():
                image_url = str(val).strip()
                break
            if 'image' in sk and 'url' in sk and str(val).strip():
                image_url = str(val).strip()
                break

    sku_variants = pick('sku_variants', 'properties', 'variants', 'varyantlar')
    goods_id = pick('goods_id', 'goodsid', 'item_id')
    if not goods_id and url:
        m = re.search(r'goods_id=(\d+)', url) or re.search(r'-g-(\d+)\.html', url)
        if m:
            goods_id = m.group(1)

    err = row.get('error', '')
    if err is None:
        err = ''

    return {
        'url': url,
        'goods_id': goods_id,
        'title': title,
        'description': description,
        'keywords': keywords,
        'product_details': product_details,
        'image_url': image_url,
        'sku_variants': sku_variants,
        'error': str(err),
    }


def read_scraped_excel(filepath):
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active
    headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    products = []
    for r in range(2, ws.max_row + 1):
        row = {}
        for i, h in enumerate(headers):
            if h:
                row[h.lower().replace(' ', '_')] = ws.cell(r, i + 1).value or ''
        products.append(normalize_product_row(row))
    return products


def write_classified_excel(products, classifications, output_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "GTIP Siniflandirma"

    out_headers = ['URL', 'Goods ID', 'Title', 'Product Details',
                   'GTIP Kodu', 'Fasil', 'Gerekce', 'Guven', 'Alternatifler', 'Hata']
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
            prod.get('product_details', ''),
            cls.get('gtip_code', ''),
            cls.get('fasil', ''),
            cls.get('gerekce', ''),
            cls.get('guven', ''),
            alt_str,
            cls.get('error', ''),
        ])

    ws.column_dimensions['A'].width = 50
    ws.column_dimensions['C'].width = 50
    ws.column_dimensions['D'].width = 60
    ws.column_dimensions['E'].width = 20
    ws.column_dimensions['G'].width = 60
    wb.save(output_path)


def _esc(text):
    return (str(text) or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def write_classified_html(products, classifications, output_path):
    html_path = os.path.splitext(output_path)[0] + '.html'

    cards = []
    for i, (prod, cls) in enumerate(zip(products, classifications), 1):
        gtip = cls.get('gtip_code', '')
        guven = cls.get('guven', '')
        gerekce = cls.get('gerekce', '')
        alts = cls.get('alternatifler', [])
        error = cls.get('error', '')
        fasil = cls.get('fasil', '')

        guven_class = {'yuksek': 'high', 'orta': 'mid', 'dusuk': 'low'}.get(guven, 'low')

        details_html = ''
        pd = prod.get('product_details', '')
        if pd:
            rows = ''
            for item in str(pd).split('; '):
                parts = item.split(': ', 1)
                if len(parts) == 2:
                    rows += f'<tr><td class="pk">{_esc(parts[0])}</td><td>{_esc(parts[1])}</td></tr>'
            details_html = f'<table class="props">{rows}</table>'

        alt_html = ''
        if alts and isinstance(alts, list) and alts[0]:
            alt_html = '<div class="alts">Alt: ' + ', '.join(f'<code>{_esc(a)}</code>' for a in alts) + '</div>'

        img_url = prod.get('image_url', '')
        img_html = f'<img src="{_esc(img_url)}" loading="lazy">' if img_url else '<div class="no-img">No image</div>'

        error_html = f'<div class="error">{_esc(error)}</div>' if error else ''

        cards.append(f'''
    <div class="card">
      <div class="card-img">{img_html}</div>
      <div class="card-body">
        <div class="card-num">#{i}</div>
        <h2><a href="{_esc(prod.get('url', ''))}" target="_blank">{_esc(prod.get('title', '')) or 'Untitled'}</a></h2>
        <div class="gtip-box {guven_class}">
          <span class="gtip-code">{_esc(gtip) or '?'}</span>
          <span class="gtip-badge">{_esc(guven)}</span>
          <span class="gtip-fasil">Fasil {_esc(str(fasil))}</span>
        </div>
        <p class="gerekce">{_esc(gerekce)}</p>
        {alt_html}
        {details_html}
        {error_html}
      </div>
    </div>''')

    ok = sum(1 for c in classifications if not c.get('error'))
    high = sum(1 for c in classifications if c.get('guven') == 'yuksek')

    html = f'''<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GTIP Siniflandirma ({len(products)} urun)</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f0f2f5; color: #1a1a1a; padding: 20px; }}
  .header {{ max-width: 1100px; margin: 0 auto 24px; }}
  .header h1 {{ font-size: 24px; font-weight: 700; }}
  .header .stats {{ color: #666; margin-top: 4px; font-size: 14px; }}
  .stats span {{ margin-right: 16px; }}
  .card {{ max-width: 1100px; margin: 0 auto 16px; background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08); display: flex; overflow: hidden; }}
  .card-img {{ width: 200px; min-height: 200px; flex-shrink: 0; background: #f7f7f7; display: flex; align-items: center; justify-content: center; }}
  .card-img img {{ width: 100%; height: 100%; object-fit: cover; }}
  .no-img {{ color: #ccc; font-size: 13px; }}
  .card-body {{ padding: 16px 20px; flex: 1; min-width: 0; }}
  .card-num {{ font-size: 12px; color: #999; margin-bottom: 4px; }}
  h2 {{ font-size: 15px; font-weight: 600; margin-bottom: 10px; line-height: 1.3; }}
  h2 a {{ color: #1a1a1a; text-decoration: none; }}
  h2 a:hover {{ color: #e67e00; }}
  .gtip-box {{ display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 6px; margin-bottom: 10px; }}
  .gtip-box.high {{ background: #e8f5e9; border-left: 4px solid #2e7d32; }}
  .gtip-box.mid {{ background: #fff8e1; border-left: 4px solid #f9a825; }}
  .gtip-box.low {{ background: #fce4ec; border-left: 4px solid #c62828; }}
  .gtip-code {{ font-family: 'Consolas', monospace; font-size: 16px; font-weight: 700; }}
  .gtip-badge {{ font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; text-transform: uppercase; }}
  .high .gtip-badge {{ background: #2e7d32; color: #fff; }}
  .mid .gtip-badge {{ background: #f9a825; color: #fff; }}
  .low .gtip-badge {{ background: #c62828; color: #fff; }}
  .gtip-fasil {{ font-size: 12px; color: #666; }}
  .gerekce {{ font-size: 13px; color: #444; margin-bottom: 8px; line-height: 1.4; }}
  .alts {{ font-size: 12px; color: #666; margin-bottom: 8px; }}
  .alts code {{ background: #f0f0f0; padding: 1px 5px; border-radius: 3px; }}
  .props {{ font-size: 12px; border-collapse: collapse; margin-bottom: 8px; }}
  .props tr {{ border-bottom: 1px solid #f0f0f0; }}
  .props td {{ padding: 2px 10px 2px 0; }}
  .pk {{ font-weight: 600; color: #333; white-space: nowrap; }}
  .error {{ padding: 6px 10px; background: #fff0f0; color: #c00; border-radius: 4px; font-size: 13px; }}
  @media (max-width: 700px) {{
    .card {{ flex-direction: column; }}
    .card-img {{ width: 100%; height: 180px; }}
  }}
</style>
</head>
<body>
<div class="header">
  <h1>GTIP Siniflandirma Sonuclari</h1>
  <div class="stats">
    <span>{len(products)} urun</span>
    <span>{ok} siniflandirildi</span>
    <span>{high} yuksek guven</span>
  </div>
</div>
{"".join(cards)}
</body>
</html>'''

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return html_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='GTIP Siniflandirici (Claude API)')
    parser.add_argument('input', help='Scraper ciktisi Excel dosyasi')
    parser.add_argument('-o', '--output', help='Cikti dosyasi')
    parser.add_argument('--db', default='data/gtip_2026.db', help='GTIP veritabani yolu')
    parser.add_argument('--delay', type=float, default=0.5, help='API istekleri arasi bekleme (saniye)')
    parser.add_argument(
        '--model',
        default='claude-haiku-4-5-20251001',
        help='Ilk gecis Claude model id',
    )
    parser.add_argument('--max-tokens', type=int, default=1200, help='Ilk gecis max_tokens')
    parser.add_argument(
        '--note-chars',
        type=int,
        default=8000,
        metavar='N',
        help='Fasil notundan modele giden max karakter (once 2500; artirdi)',
    )
    parser.add_argument(
        '--gtip-rows',
        type=int,
        default=120,
        metavar='N',
        help='Fallback modda her aday fasil icin GTIP satir sayisi',
    )
    parser.add_argument(
        '--izahname-chars',
        type=int,
        default=3000,
        metavar='N',
        help='Izahname metninden modele giden max karakter (varsayilan: 3000)',
    )
    parser.add_argument(
        '--retrieval',
        type=int,
        default=50,
        metavar='N',
        help='Urun metnine gore FTS ile getirilecek oncelikli GTIP satiri',
    )
    parser.add_argument(
        '--refine',
        action='store_true',
        help='guven dusuk/orta veya kod yoksa ikinci gecis (daha guclu model)',
    )
    parser.add_argument(
        '--refine-model',
        default='claude-sonnet-4-20250514',
        help='Ikinci gecis model id',
    )
    parser.add_argument('--refine-max-tokens', type=int, default=1200)
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
        print("Hata: ANTHROPIC_API_KEY bulunamadi.")
        print("  .env dosyasina ANTHROPIC_API_KEY=sk-ant-... yazin")
        sys.exit(1)

    if not os.path.isfile(args.input):
        print(f"Hata: {args.input} bulunamadi")
        sys.exit(1)

    if not os.path.isfile(args.db):
        print(f"Hata: {args.db} bulunamadi. Once build_db.py calistirin.")
        sys.exit(1)

    output_path = args.output
    if not output_path:
        base = os.path.splitext(os.path.basename(args.input))[0]
        output_path = os.path.join('output', f'{base}_classified.xlsx')
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    conn = sqlite3.connect(args.db)
    client = anthropic.Anthropic(api_key=api_key)

    classify_opts = {
        'model': args.model,
        'max_tokens': args.max_tokens,
        'note_max_chars': args.note_chars,
        'izahname_max_chars': args.izahname_chars,
        'gtip_rows_per_fasil': args.gtip_rows,
        'retrieval_top_n': args.retrieval,
        'refine': args.refine,
        'refine_model': args.refine_model,
        'refine_max_tokens': args.refine_max_tokens,
    }

    products = read_scraped_excel(args.input)
    print(f"Toplam urun: {len(products)}")

    if not products:
        print("Hata: Excel'de urun bulunamadi")
        sys.exit(1)

    classifications = []
    errors = 0

    for i, prod in enumerate(products, 1):
        title = prod.get('title', '')[:50]
        print(f"  [{i}/{len(products)}] {title}...", end=" ", flush=True)

        cls = classify_product(client, prod, conn, classify_opts)

        if cls.get('error'):
            print(f"HATA: {cls['error'][:60]}")
            errors += 1
        else:
            code = cls.get('gtip_code', '?')
            guven = cls.get('guven', '?')
            print(f"-> {code} ({guven})")

        classifications.append(cls)
        if i < len(products):
            time.sleep(args.delay)

    write_classified_excel(products, classifications, output_path)
    html_path = write_classified_html(products, classifications, output_path)
    conn.close()

    high = sum(1 for c in classifications if c.get('guven') == 'yuksek')
    print(f"\nToplam         : {len(products)}")
    print(f"Siniflandirildi: {len(products) - errors}")
    print(f"Yuksek guven   : {high}")
    print(f"Hata           : {errors}")
    print(f"Excel          : {output_path}")
    print(f"HTML           : {html_path}")


if __name__ == "__main__":
    main()
