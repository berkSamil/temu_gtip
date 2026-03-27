# GTİP Sınıflandırma Sistemi — CLAUDE.md

Bu dosya Cursor ve Claude Code için ortak bağlam kaynağıdır. Her session başında oku.

---

## PROJE DURUMU

**Aktif:** `scripts/gtip_matcher.py`, `scripts/build_db.py`, `data/`
**Arşivlendi:** `scripts/temu_scraper.py` — scraping tamamen bırakıldı, dokunma.
**Hedef:** Ürün adı + açıklama + material + kategori + görsel → 12 haneli GTİP önerisi.
  Şu an TEMU manifest'leri için çalışıyor, hedef: kaynak-agnostik genel GTİP robotu.

**Sayısal durum:**
- DB: 15,718 GTİP satırı, 96 fasıl notu, 98 fasıl dosyası
- Input: herhangi bir Excel (TEMU manifest, elle giriş, başka kaynak)
- Model: claude-haiku-4-5 varsayılan, --refine ile sonnet ikinci geçiş

---

## MİMARİ

```
INPUT (herhangi bir Excel — Temu manifest, elle giriş, başka kaynak)
  ürün adı, açıklama, material, kategori, [görsel URL]
       ↓
  [1] ADAY FASIL SEÇİMİ ⚠️ EN ZAYIF HALKA
      Şu an: material_hints (hardcoded) + FTS5 kelime araması
      FTS5 = SQLite full-text search. Ürün metnindeki kelimeleri
        tarife cetvelinde arar, eşleşen GTİP'lerin fasıllarını döner.

      SORUN: material→fasıl eşlemesi tarife mantığını yansıtmıyor.
        Hangi kriterin belirleyici olduğu (materyal, fonksiyon, kullanım
        amacı, işleme şekli) fasıldan fasıla, pozisyondan pozisyona
        değişir. Bu ayrımı sadece izahname notları yapıyor.

      HEDEF: material_hints kaldırılacak →
        izahname + genel kurallar + içindekiler context'e girer,
        embedding similarity + onaylı sınıflandırma geçmişi (few-shot)
       ↓
  [2] BAĞLAM OLUŞTURMA — her aday fasıl için kaynak seti:

      a) Fasıl notu          DB tablosu: fasil_notlari (mevcut ✅)
      b) GTİP satırları       DB tablosu: gtip (mevcut ✅)
                              Şu an: flat liste, --gtip-rows 120 ile kesilir.
                              97 fasılın 33'ünde 120+ GTİP var. Fasıl 29'da %93'ü görünmez.
                              HEDEF: 2 adımlı hiyerarşik daralma:
                                Adım A → tüm 4'lü pozisyonları göster (fasıl 84 = 84 poz, ~4.5K token)
                                Adım B → seçilen pozisyon altındaki TÜM 12'li GTİP'ler
                                         (en büyük: 29.33 = 211 GTİP, hâlâ yönetilebilir)
                              120 satır limiti kaldırılır, sınır doğal pozisyon boyutu olur.
                              2 API çağrısına çıkar ama accuracy karşılaştırılamaz.
      c) FTS ranked bloku     --retrieval, ürün metnine en yakın GTİP satırları (mevcut ✅)
      d) İzahname notları     DB tablosu: izahname_notlari (EKLENİYOR)
                              97 .doc → build_db.py parse → SQLite
      e) İçindekiler          DB tablosu: bolum_fasil_haritasi (EKLENİYOR)
                              21 bölüm → 97 fasıl lookup
      f) Genel Kurallar       System prompt'a statik metin olarak gömülü (EKLENİYOR)
                              6 kural, her sınıflandırma kararının yasal dayanağı
       ↓
  [3] CLAUDE API (claude-haiku-4-5 varsayılan)
      → JSON: {gtip_code, fasil, gerekce, guven, alternatifler}
      → guven düşük/orta + --refine → sonnet ile 2. geçiş
       ↓
  [4] DOĞRULAMA
      normalize_gtip_code() → format düzelt
      gtip_exists() → DB'de var mı?
      → yoksa alternatiflerden dene, o da yoksa "dogrulanamadi"
       ↓
  OUTPUT: _classified.xlsx + _classified.html
```

---

## VERİ AKIŞI

Tüm kaynak veriler orijinal formatlarında data/ altında durur.
build_db.py hepsini parse edip tek SQLite DB'ye yazar.
Ara format (JSON, markdown) yok — SQLite tek kaynak.

```
  data/fasil_dosyalari/*.xls  ──┐  parse_fasil_xls()       → gtip, pozisyon, fasil_meta
  data/fasil_notlari/*.xls    ──┤  parse_fasil_notu()       → fasil_notlari
  data/izahname_notlari/*.doc ──┼► parse_izahname_doc()     → izahname_notlari  (EKLENİYOR)
  data/yorum_kurallari/       ──┤  parse_yorum_kurallari()  → yorum_kurallari   (EKLENİYOR)
  data/icindekiler/           ──┘  parse_icindekiler()      → bolum_fasil       (EKLENİYOR)
                                           ↓
                                    gtip_2026.db (tek dosya)
```

İstisna: Genel Kurallar (6 kural) statik, system prompt'a gömülü.

build_db.py sağlık durumu (son kontrol):
  ✅ 15,718 GTİP, 0 boş tanım, 0 boş hiyerarşi
  ✅ 97 fasıl (77 reserved, beklenen eksik)
  ✅ 96 fasıl notu, bölüm/fasıl ayrımı doğru
  ✅ 3,986 pozisyon, hierarchy tracking doğru
  ✅ FTS5 indeksleri çalışıyor
  ⚠️ .doc dosyalar libreoffice --headless --convert-to docx ile
     dönüştürülmeli (build_db.py bunu otomatik yapacak)

---

## UZUN VADELİ EVRİM

```
  Faz 1 (ATLANDI): Claude API, izahname yok
  ► Faz 2 (ŞU AN): İzahname + genel kurallar + içindekiler entegre
         Onaylı sınıflandırmalar classifications.db'ye birikir
         Few-shot örnekler accuracy artırır
  Faz 3: Yeterli veri (1000+) → fine-tuned fasıl seçim modeli
         Claude sadece edge case fallback
  Faz 4: Tamamen local model, Claude API bağımlılığı sıfır
```

---

## DOSYA YAPISI

```
temu_gtip/
├── CLAUDE.md                  ← bu dosya
├── CLAUDE_PROJECT_HANDOFF.md  ← eski handoff, referans
├── requirements.txt
├── .env                       ← ANTHROPIC_API_KEY (gitignore'da)
├── scripts/
│   ├── gtip_matcher.py        ← ana program
│   ├── build_db.py            ← tüm veri kaynakları → gtip_2026.db
│   ├── eval_gtip.py           ← (YAZILACAK) gold set ile accuracy ölçümü
│   └── temu_scraper.py        ← ARŞİVLENDİ, dokunma
├── data/
│   ├── gtip_2026.db           ← SQLite (gitignore'da, build_db ile üretilir)
│   ├── fasil_dosyalari/       ← 98 fasıl XLS (TGTC 2026)
│   ├── fasil_notlari/         ← 96 fasıl notu XLS
│   ├── izahname_notlari/      ← (EKLENİYOR) 97 fasıl izahname .doc
│   ├── yorum_kurallari/       ← (EKLENİYOR) genel kurallar .xls + .doc
│   ├── icindekiler/           ← (EKLENİYOR) bölüm→fasıl haritası .xls + .doc
│   └── input.xlsx             ← örnek input
├── output/                    ← gitignore'da
├── experiments/               ← eval run JSON'ları
└── tests/                     ← pytest testleri
```

---

## VERİTABANI ŞEMASI

### Mevcut tablolar
```
  gtip             (id INTEGER PK, gtip_code TEXT UNIQUE, gtip_clean, fasil INT,
                    pozisyon, alt_pozisyon, tanim, tanim_hiyerarsi, olcu_birimi, seviye)
  pozisyon         (id INTEGER PK, kod TEXT UNIQUE, kod_clean, fasil INT, tanim, seviye)
  fasil_meta       (fasil_no INT PK, dosya_adi, satir_sayisi, gtip_sayisi, parse_tarihi)
  fasil_notlari    (fasil_no INT PK, bolum_notu, fasil_notu, tam_metin, kelime_sayisi, dosya_adi)
  gtip_fts         FTS5 (gtip_code, tanim, tanim_hiyerarsi) — content=gtip
  notlar_fts       FTS5 (fasil_no, tam_metin)
```

### Eklenecek tablolar
```
  izahname_notlari (fasil_no INT, pozisyon TEXT, metin TEXT, kelime_sayisi INT, dosya_adi TEXT)
  yorum_kurallari  (kural_no INT PK, baslik TEXT, metin TEXT, aciklama TEXT)
  bolum_fasil      (bolum_no INT, bolum_adi TEXT, fasil_no INT, fasil_adi TEXT)
  izahname_fts     FTS5 — izahname tam metin araması
```

### İstatistikler
```
  gtip: 15,718 satır (12 haneli), 97 fasıl (77 reserved/eksik)
  pozisyon: 3,986 satır
  fasil_notlari: 96 kayıt, en büyük fasıl 72 (21K char)
  En büyük fasıllar: 29 (1797 GTİP), 84 (1375), 72 (785), 85 (769)
```

---

## GTİP KODU FORMAT

Türk tarife yapısı:
```
  XX            Fasıl (97 fasıl, bölümlere ayrılmış)
  XXXX          Pozisyon (4 hane)
  XXXXXX        Alt pozisyon (6 hane, uluslararası HS seviyesi)
  XXXXXXXX      Türkiye istatistik pozisyonu (8 hane)
  XXXXXXXXXXXX  12 haneli tam GTİP
```

Noktalı format: `XXXX.XX.XX.XX.XX` (5 grup)
Regex: `^\d{4}\.\d{2}\.\d{2}\.\d{2}\.\d{2}$`
DB'de olmayan kod geçersizdir — `sanitize_classification()` filtreler.

---

## TEMEL FONKSİYONLAR (gtip_matcher.py)

### Aday seçimi & arama
| Fonksiyon | Ne yapar |
|-----------|----------|
| `get_candidate_fasils()` | material_hints + FTS → aday fasıl listesi (max 8) |
| `search_gtip_fts()` | FTS5 sorgusu, limit ile |
| `_product_search_words()` | Ürün metninden FTS arama kelimeleri çıkarır (stop words filtresi) |
| `retrieve_ranked_gtips()` | FTS per-word, skor sıralı GTİP satırları (hardcoded değil, metin-driven) |

### Bağlam & sınıflandırma
| Fonksiyon | Ne yapar |
|-----------|----------|
| `build_tarife_context()` | Fasıl notu + GTİP satırları + FTS bloku birleştirir → Claude'a giden context |
| `classify_product()` | Ana orchestrator: context oluştur → API çağır → refine kararı → sonuç |
| `_call_classify()` | Claude API wrapper (tek çağrı) |
| `_needs_refine()` | guven == "dusuk" veya "orta" → True |

### Doğrulama & parse
| Fonksiyon | Ne yapar |
|-----------|----------|
| `extract_first_json_object()` | Brace-balanced JSON parse (eski kırılgan regex yerine) |
| `_json_from_balanced_braces()` | Nested {} dengeleyerek ilk geçerli JSON bloğunu bulur |
| `normalize_gtip_code()` | Çeşitli formatları XXXX.XX.XX.XX.XX'e normalize eder |
| `gtip_exists()` | DB'de bu GTİP var mı? |
| `sanitize_classification()` | Tam doğrulama: normalize → DB check → alternatif dene → fallback |

### Input/Output
| Fonksiyon | Ne yapar |
|-----------|----------|
| `normalize_product_row()` | Farklı kolon adlarını standartlaştırır (title/product_title, vb.) |
| `read_scraped_excel()` | Input Excel oku → product listesi |
| `write_classified_excel()` | Sonuç Excel yaz |
| `write_classified_html()` | Sonuç HTML rapor yaz |

### SYSTEM_PROMPT
  Ana prompt: "Sen deneyimli bir Türk Gümrük Tarife sınıflandırma uzmanısın..."
  Claude'a tarife cetveli context'i + ürün bilgisi gider,
  JSON formatında {gtip_code, fasil, gerekce, guven, alternatifler} döner.
  REFINE_SYSTEM_PROMPT: Aynı görev, önceki cevap zayıfsa ikinci geçiş.
  ⚠️ Genel Kurallar henüz system prompt'ta değil — Faz 2'de eklenecek.

---

## BİLİNEN SORUNLAR (öncelik sırasıyla)

1. **İzahname, genel kurallar, içindekiler entegre değil**
   En kritik eksik. build_db.py'a parser eklenecek, DB'ye yazılacak.
   System prompt'a genel kurallar gömülecek. Faz 2 kapsamında.

2. **120 satır limiti**
   97 fasılın 33'ünde 120+ GTİP var. Fasıl 29'da %93'ü görünmez.
   Hiyerarşik daralma ile çözülecek (4'lü pozisyon → 12'li GTİP).

3. **material_hints hardcoded**
   ('plastic', 39), ('rubber', 40)... tuple listesi.
   Tarife mantığına aykırı — fonksiyon/kullanım/işleme şekli
   materyalden belirleyici olabilir. İzahname entegrasyonu +
   embedding similarity ile replace edilecek.

4. **defaults fallback listesi**
   [39, 73, 82, 83, 84, 85, 90, 94, 96...] — veri-driven değil.
   İçindekiler + izahname entegrasyonu sonrası kaldırılacak.

5. **Görsel desteği yok**
   Claude görsel alabilir ama pipeline'a entegre değil.
   Özellikle materyal tespiti için kritik.

6. **Experiment tracking yok**
   Hangi parametreyle hangi accuracy çıktı, kayıt altında değil.
   experiments/ klasörüne JSON olarak kaydedilecek.

7. **requirements.txt'te playwright var**
   Scraping bırakıldı, playwright satırı silinmeli.

---

## CLI PARAMETRELERİ

```bash
python scripts/gtip_matcher.py input.xlsx \
  --db data/gtip_2026.db \           # DB yolu
  -o output/classified.xlsx \         # çıktı dosyası
  --model claude-haiku-4-5-20251001 \ # varsayılan, ucuz
  --max-tokens 1200 \                 # ilk geçiş max token
  --delay 0.5 \                       # API istekleri arası bekleme (saniye)
  --note-chars 8000 \                 # fasıl notu uzunluğu (10000'e çıkarılmalı)
  --gtip-rows 120 \                   # fasıl başına GTİP satırı (kaldırılacak, hiyerarşik daralma)
  --retrieval 50 \                    # FTS ranked satır sayısı
  --refine \                          # düşük güvende sonnet ile 2. geçiş
  --refine-model claude-sonnet-4-20250514
  --refine-max-tokens 1200
```

---

## INPUT/OUTPUT FORMAT

**Input Excel kolonları** (normalize_product_row() tüm varyantları handle eder):
```
  title / product_title
  description / aciklama
  keywords / category_path / category
  product_details
  image_url / thumbnail_url
  url / product_url
```

**Output JSON yapısı:**
```json
{
  "gtip_code": "3926.90.97.90.29",
  "fasil": 39,
  "gerekce": "Türkçe muhakeme...",
  "guven": "yuksek|orta|dusuk",
  "alternatifler": ["3926.90.97.90.11"]
}
```

**Output dosyaları:**
  `_classified.xlsx` — GTİP kodu, güven, gerekçe kolonları eklenmiş input
  `_classified.html` — görsel rapor

---

## ORTAM

```bash
pip install -r requirements.txt
# requirements.txt: xlrd>=2.0, openpyxl>=3.1, anthropic>=0.40
# ⚠️ playwright satırı silinmeli

# DB build (bir kez, veya --force ile rebuild)
python scripts/build_db.py data/fasil_dosyalari/ \
  --notlar data/fasil_notlari/ \
  --db data/gtip_2026.db --force

# .env
ANTHROPIC_API_KEY=sk-ant-...

# İzahname parse için (build_db.py otomatik kullanacak)
# libreoffice kurulu olmalı (.doc → .docx dönüşümü)
# pip install python-docx
```

---

## MÜHENDİSLİK HEDEFLERİ (Faz 2 — şu an)

### Adım 1 — İzahname + genel kurallar + içindekiler entegrasyonu
- build_db.py'a parse_izahname_doc() ekle (libreoffice + python-docx)
- build_db.py'a parse_yorum_kurallari() ve parse_icindekiler() ekle
- DB'ye izahname_notlari, yorum_kurallari, bolum_fasil tabloları
- 97 izahname .doc'u data/izahname_notlari/ altına koy
- --force ile rebuild, doğrula

### Adım 2 — Hiyerarşik daralma
- 120 satır limiti kaldır
- 2 adımlı sınıflandırma: pozisyon seçimi → GTİP seçimi
- Genel kuralları system prompt'a göm

### Adım 3 — Baseline ölç
- scripts/eval_gtip.py yaz: gold Excel → matcher → accuracy by fasıl
- experiments/ klasörüne ilk run JSON'ı kaydet
- Faz 2 öncesi/sonrası karşılaştırma

### Adım 4 — Test suite
- pytest tests/ — DB geçerli GTİP dönüyor mu, JSON parse crash etmiyor mu
- Bilinen 10+ ürün → beklenen fasıl/pozisyon geliyor mu

### Adım 5 — Embedding katmanı
- Onaylı sınıflandırmalar classifications.db'ye kayıt
- Yeni ürün → nearest neighbor → few-shot örnek
- material_hints'ı replace eder

### Adım 6 — Görsel entegrasyon
- Input Excel'de image_url varsa Claude'a base64 olarak gönder

---

## ÇALIŞMA KURALLARI (Claude Code ve Cursor için)

1. Her değişiklik öncesi testleri çalıştır: `pytest tests/ -v`
2. Her eval run'ını kaydet: `experiments/run_YYYYMMDD_HH.json`
3. `material_hints` ve `defaults` listelerine şu an dokunma — izahname
   entegrasyonu + embedding sonrası kaldırılacak
4. `temu_scraper.py`'a dokunma — arşivlenmiş
5. DB şemasını değiştirirsen `build_db.py --force` ile rebuild test et
6. Yeni veri kaynağı eklerken: orijinal dosya `data/` altına,
   parser `build_db.py`'a, çıktı SQLite'a
