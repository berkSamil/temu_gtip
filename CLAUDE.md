# GTİP Sınıflandırma Sistemi — CLAUDE.md

Bu dosya Cursor ve Claude Code için ortak bağlam kaynağıdır. Her session başında oku.

---

## PROJE DURUMU

**Aktif:** `scripts/gtip_matcher.py`, `scripts/build_db.py`, `scripts/eval_gtip.py`, `data/`
**Arşivlendi:** `scripts/temu_scraper.py` — scraping tamamen bırakıldı, dokunma.
**Hedef:** Ürün adı + açıklama + material + kategori + görsel → 12 haneli GTİP önerisi.
  Şu an TEMU manifest'leri için çalışıyor, hedef: kaynak-agnostik genel GTİP robotu.

**Sayısal durum (güncel):**
- DB: 15,718 GTİP, 3,986 pozisyon, 96 fasıl notu, 97 izahname, 6 yorum kuralı, 99 bölüm-fasıl kaydı
- Input: herhangi bir Excel (TEMU manifest, elle giriş, başka kaynak)
- Model: claude-haiku-4-5 varsayılan, --refine ile sonnet ikinci geçiş

---

## MİMARİ

```
INPUT (herhangi bir Excel — Temu manifest, elle giriş, başka kaynak)
  ürün adı, açıklama, material, kategori, [görsel URL]
       ↓
  [ADIM 0a] BÖLÜM SEÇİMİ (21 bölüm → 5 aday)
      Model 21 bölüm listesine bakarak 5 aday bölüm seçer. (max_tokens=400)
      FONKSIYON MATERYALI EZER kuralı + tekstil form istisnası prompt'ta mevcut.
       ↓
  [ADIM 0b] FASIL SEÇİMİ (seçilen bölümlerin fasılları → 8 aday fasıl)
      Adım 0a başarısız olursa FTS fallback devreye girer. (max_tokens=400)
       ↓
  [ADIM 1] POZİSYON SEÇİMİ (4'lü pozisyon)
      Her aday fasıl için: fasıl notu + izahname özeti + tüm 4'lü pozisyonlar
      FTS ranked bloku da eklenir.
      Model fasıl + 4'lü pozisyon kodu seçer.
       ↓
  [ADIM 2] GTİP SEÇİMİ (12 haneli)
      Seçilen pozisyon altındaki TÜM 12'li GTİP'ler + fasıl notu + izahname
      Yorum kuralları system prompt'a gömülü.
      Model 12 haneli GTİP kodu seçer.
      → guven düşük/orta + --refine → sonnet ile 2. geçiş
       ↓
  DOĞRULAMA
      normalize_gtip_code() → format düzelt
      gtip_exists() → DB'de var mı? Yoksa alternatiflerden dene.
       ↓
  OUTPUT: _classified.xlsx + _classified.html
```

Fallback: Adım 1 pozisyon bulamazsa `_classify_flat()` eski tek-adımlı moda düşer.

---

## VERİ AKIŞI

Tüm kaynak veriler orijinal formatlarında data/ altında durur.
build_db.py hepsini parse edip tek SQLite DB'ye yazar.
Ara format (JSON, markdown) yok — SQLite tek kaynak.

```
  data/fasil_dosyalari/*.xls  ──┐  parse_fasil_xls()       → gtip, pozisyon, fasil_meta
  data/fasil_notlari/*.xls    ──┤  parse_fasil_notu()       → fasil_notlari
  data/izahname_notlari/*.doc ──┼► parse_izahname_doc()     → izahname_notlari  ✅
  data/yorum_kurallari/       ──┤  parse_yorum_kurallari()  → yorum_kurallari   ✅
  data/icindekiler/           ──┘  parse_icindekiler()      → bolum_fasil       ✅
                                           ↓
                                    gtip_2026.db (tek dosya)
```

Yorum Kuralları (6 kural) Adım 2 system prompt'una dinamik olarak eklenir (get_yorum_kurallari()).

build_db.py sağlık durumu (son kontrol):
  ✅ 15,718 GTİP, 0 boş tanım, 0 boş hiyerarşi
  ✅ 97 fasıl (77 reserved, beklenen eksik)
  ✅ 96 fasıl notu, bölüm/fasıl ayrımı doğru
  ✅ 3,986 pozisyon, hierarchy tracking doğru
  ✅ FTS5 indeksleri çalışıyor (gtip_fts, notlar_fts, izahname_fts)
  ✅ 97 izahname kaydı, 6 yorum kuralı, 99 bölüm-fasıl kaydı
  ✅ .doc → .docx dönüşümü soffice ile otomatik (build_db.py içinde)

---

## UZUN VADELİ EVRİM

```
  Faz 1 (ATLANDI): Claude API, izahname yok, flat sınıflandırma
  ► Faz 2 (BÜYÜK ÖLÇÜDE TAMAMLANDI):
       ✅ İzahname + yorum kuralları + içindekiler entegre
       ✅ Hiyerarşik daralma (0a → 0b → 1 → 2)
       ✅ Prompt caching
       ✅ eval_gtip.py
       ⏳ Prompt kalitesi iyileştirme (fasıl aday sayısı optimizasyonu devam ediyor)
       ⏳ Few-shot örnekler (classifications.db henüz yok)
       ⏳ Görsel entegrasyon (image_url → base64 → Claude, materyal tespiti için)
  Faz 3: Yeterli veri (1000+) → fine-tuned fasıl seçim modeli
         Claude sadece edge case fallback
  Faz 4: Tamamen local model, Claude API bağımlılığı sıfır
```

---

## DOSYA YAPISI

```
temu_gtip/
├── CLAUDE.md                  ← bu dosya
├── README.md
├── requirements.txt
├── .env                       ← ANTHROPIC_API_KEY (gitignore'da)
├── scripts/
│   ├── gtip_matcher.py        ← ana program
│   ├── build_db.py            ← tüm veri kaynakları → gtip_2026.db
│   ├── eval_gtip.py           ← gold set ile accuracy ölçümü
│   ├── analyze_run.py         ← offline eval karşılaştırma raporu
│   ├── _test_live.py          ← canlı test
│   └── temu_scraper.py        ← ARŞİVLENDİ, dokunma
├── data/
│   ├── gtip_2026.db           ← SQLite (gitignore'da, build_db ile üretilir)
│   ├── fasil_dosyalari/       ← 98 fasıl XLS (TGTC 2026)
│   ├── fasil_notlari/         ← 96 fasıl notu XLS
│   ├── izahname_notlari/      ← 97 fasıl izahname .doc
│   ├── yorum_kurallari/       ← genel kurallar .xls + .doc
│   ├── icindekiler/           ← bölüm→fasıl haritası .xls + .doc
│   ├── gold_set_30.xlsx       ← eval gold set (30 ürün)
│   └── input.xlsx             ← örnek input
├── output/                    ← gitignore'da
└── experiments/               ← eval run JSON'ları + MD raporları (run_YYYYMMDD_HHMM)
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
  izahname_notlari (fasil_no INT, pozisyon TEXT, metin TEXT, kelime_sayisi INT, dosya_adi TEXT)
  yorum_kurallari  (kural_no INT PK, baslik TEXT, metin TEXT, aciklama TEXT)
  bolum_fasil      (bolum_no INT, bolum_adi TEXT, fasil_no INT, fasil_adi TEXT)
  gtip_fts         FTS5 (gtip_code, tanim, tanim_hiyerarsi) — content=gtip
  notlar_fts       FTS5 (fasil_no, tam_metin)
  izahname_fts     FTS5 — izahname tam metin araması
```

### İstatistikler
```
  gtip: 15,718 satır (12 haneli), 97 fasıl (77 reserved/eksik)
  pozisyon: 3,986 satır
  fasil_notlari: 96 kayıt
  izahname_notlari: 97 kayıt
  yorum_kurallari: 6 kayıt
  bolum_fasil: 99 kayıt (21 bölüm × n fasıl)
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

## CLI PARAMETRELERİ

```bash
python scripts/gtip_matcher.py input.xlsx \
  --db data/gtip_2026.db \              # DB yolu
  -o output/classified.xlsx \            # çıktı dosyası
  --model claude-haiku-4-5-20251001 \    # varsayılan
  --max-tokens 1200 \                    # Adım 2 max token
  --delay 0.5 \                          # API istekleri arası bekleme (saniye)
  --note-chars 0 \                       # fasıl notu max karakter (default: 0=kapalı)
  --izahname-chars 0 \                   # izahname max karakter (default: 0=kapalı)
  --gtip-rows 120 \                      # fallback modda fasıl başına GTİP satırı
  --retrieval 50 \                       # FTS ranked satır sayısı
  --refine \                             # düşük güvende sonnet ile 2. geçiş
  --refine-model claude-sonnet-4-20250514 \
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
  "alternatifler": ["3926.90.97.90.11"],
  "debug": {
    "candidate_bolumler": [7, 20],
    "bolum_raw_response": "...",
    "candidate_fasiller": [39, 83, 73],
    "fasil_raw_response": "...",
    "secilen_pozisyon": "3926",
    "secilen_fasil": 39,
    "pozisyon_raw_response": "...",
    "gtip_raw_response": "...",
    "token_usage": {"adim_0a": {...}, "adim_0b": {...}, "adim_1": {...}, "adim_2": {...}, "toplam": {...}},
    "token_breakdown": {...}
  }
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

# DB build (bir kez, veya --force ile rebuild)
python scripts/build_db.py data/fasil_dosyalari/ \
  --notlar data/fasil_notlari/ \
  --izahname data/izahname_notlari/ \
  --yorum data/yorum_kurallari/ \
  --icindekiler data/icindekiler/ \
  --db data/gtip_2026.db --force

# .env
ANTHROPIC_API_KEY=sk-ant-...
```

---

## ÇALIŞMA KURALLARI (Claude Code ve Cursor için)

1. Her eval run'ını kaydet: `experiments/run_YYYYMMDD_HHMM.json`
2. `defaults` listesine şu an dokunma — FTS fallback için gerekli
3. `temu_scraper.py`'a dokunma — arşivlenmiş
4. DB şemasını değiştirirsen `build_db.py --force` ile rebuild test et
5. Yeni veri kaynağı eklerken: orijinal dosya `data/` altına, parser `build_db.py`'a, çıktı SQLite'a

