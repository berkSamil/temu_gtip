# TEMU → GTİP Sınıflandırma Projesi — Teknik El Kitabı (Claude için)

Bu belge, repoya bakmadan başka bir asistanın projeyi anlaması, hata ayıklaması ve genişletmesi için yazılmıştır.

---

## 1. PROJECT OVERVIEW

### Bu program ne yapar? (tek paragraf)

Bu proje, **Temu** ürün sayfalarından (Playwright + kullanıcının açık Chrome oturumu) ürün metnini çeker, ardından **Türkiye Gümrük Tarife Cetveli** verisinin tutulduğu **SQLite** veritabanı ve **Anthropic Claude (Messages API)** ile her ürün için **12 haneli GTİP kodu**, güven düzeyi, Türkçe gerekçe ve alternatif kodlar üretir. Sonuçlar **Excel** ve **HTML** rapor olarak yazılır.

### Tam boru hattı (input → işlemler → output)

1. **Girdi:** `data/input.xlsx` (veya benzeri) içinde **Temu ürün URL’leri** (hücrelerde metin).
2. **Scraper (`scripts/temu_scraper.py`):** Chrome’u **CDP** ile `127.0.0.1:9222` üzerinden bağlar → her URL’de sayfa yükler → `window.rawData.store` ve `<meta>` alanlarından başlık, açıklama, anahtar kelime, özellik tablosu, görsel URL, varyant özeti çeker → `output/<input_adı>_scraped.xlsx` + eş adlı `.html`; isteğe bağlı `images/`.
3. **Veritabanı (`scripts/build_db.py`, bir kez / güncellemede):** Resmi fasıl **XLS** dosyaları ve isteğe bağlı fasıl notu dosyaları → `data/gtip_2026.db` (`gtip`, `fasil_notlari`, FTS5 `gtip_fts`, vb.).
4. **Matcher (`scripts/gtip_matcher.py`):** Scraper çıktısı Excel’i okur → ürün başına aday fasılları **malzeme ipucu + FTS** ile seçer → seçilen fasılların **not özeti + GTIP satırları + FTS snippet** ile Claude’a gider → JSON cevap → kodlar DB’de doğrulanır → `output/..._classified.xlsx` + `.html`.

### Çözdüğü problem

Temu listelerindeki ürünler için **elle veya kopyala-yapıştır** GTİP bulmak yerine: **standartlaştırılmış ürün metni + resmi cetvel metni + LLM muhakemesi** ile tekrarlanabilir sınıflandırma ve raporlama.

---

## 2. FILE-BY-FILE BREAKDOWN

**Not:** `data/chrome_debug/` altındaki Chrome profil/cache dosyaları proje kaynağı değil; `.gitignore` ile dışlanmış çalışma artığıdır.

### `requirements.txt`

- **Purpose:** Pip bağımlılıkları.
- **Key functions/classes:** Yok.
- **Tam içerik:**

```
xlrd>=2.0
openpyxl>=3.1
anthropic>=0.40
playwright>=1.40
```

- **Not:** `gtip_matcher.py` **xlrd kullanmaz**; `build_db.py` için.

### `.gitignore`

```
__pycache__/
*.pyc
*.pyo
.env
output/
mappings/
*.db-journal
data/chrome_debug/
data/browser_profile/
data/*.db
scripts/_test_live.py
```

### `.env` (repoda genelde yok)

- **Purpose:** `ANTHROPIC_API_KEY` saklamak.
- **Örnek:** `ANTHROPIC_API_KEY=sk-ant-api03-...`
- **Okuma:** `gtip_matcher.py` `main()` içinde önce `os.environ`, sonra repo kökünde `.env` dosyasında `ANTHROPIC_API_KEY=` satırı.

### `data/gtip_2026.db` (build ile üretilir; `.gitignore`: `data/*.db`)

- **Purpose:** GTİP satırları, fasıl notları, FTS indeksleri.
- **Şema (`build_db.py` → `create_db`):**

| Tablo | Açıklama |
|--------|-----------|
| `gtip` | `gtip_code`, `gtip_clean`, `fasil`, `pozisyon`, `alt_pozisyon`, `tanim`, `tanim_hiyerarsi`, `olcu_birimi`, `seviye` |
| `pozisyon` | Ara seviye kodlar (4/6/8/10 hane) |
| `fasil_meta` | Parse meta |
| `fasil_notlari` | `fasil_no`, `bolum_notu`, `fasil_notu`, `tam_metin`, … |
| `gtip_fts` | FTS5, `content=gtip` |
| `notlar_fts` | FTS5 fasıl notları |

Matcher kullanımı: `gtip`, `fasil_notlari`, `gtip_fts`.

### `data/input.xlsx`

- **Purpose:** Scraper’a verilecek Temu linkleri.
- **`read_links()`:** Aktif sheet’te tüm hücreler; değerde `temu.com` ve (`goods` veya `-g-`) geçenler link.

### `scripts/gtip_matcher.py`

- **Purpose:** Scraper Excel’ini okuyup Claude ile GTİP üretmek; Excel+HTML yazmak.

#### Global sabitler

| Ad | Açıklama |
|----|-----------|
| `_TEMU_STOP` | FTS kelime seçiminde gürültü azaltma (İngilizce stopword seti) |
| `SYSTEM_PROMPT` | Claude sistem talimatı (Türkçe, JSON şeması) |
| `_GTIP_RE` | `^\d{4}\.\d{2}\.\d{2}\.\d{2}\.\d{2}$` |

**`_TEMU_STOP` (tam):**

```python
_TEMU_STOP = frozenset({
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'your', 'are', 'you', 'all', 'any',
    'can', 'has', 'have', 'pcs', 'pack', 'set', 'piece', 'pieces', 'item', 'items', 'sale',
    'shop', 'temu', 'free', 'new', 'hot', 'best', 'buy', 'get', 'one', 'two', 'off', 'out',
    'our', 'was', 'not', 'but', 'its', 'per', 'use', 'may', 'more', 'most', 'some', 'size',
})
```

#### `get_candidate_fasils(conn, product_details, keywords, description, title, max_fasils=8)`

- **Returns:** `list[int]` fasıl numaraları.
- **Adımlar:** Metni birleştir → `material_hints` ile fasıl skoru (+2) → kelime regex ile uzun kelimeler, stopword filtre, max 14 kelime → her kelime için `search_gtip_fts(..., limit=18)` ile eşleşen kodların fasılına +1 → skora göre sırala → `max_fasils` dolunca dön → yetmezse `defaults` listesi ile doldur.

**`material_hints` (tam tuple listesi):**

```python
(
    ('plastic', 39), ('rubber', 40), ('leather', 42), ('wood', 44), ('paper', 48),
    ('cotton', 52), ('polyester', 54), ('wool', 51), ('nylon', 55), ('silk', 50),
    ('linen', 53), ('ceramic', 69), ('porcelain', 69), ('glass', 70),
    ('steel', 73), ('stainless', 73), ('iron', 73), ('copper', 74),
    ('aluminum', 76), ('aluminium', 76), ('brass', 74), ('zinc', 79), ('metal', 73),
    ('silicone', 39), ('pvc', 39), ('abs', 39), ('eva', 39), ('bamboo', 44),
)
```

**`defaults`:** `[39, 73, 82, 83, 84, 85, 90, 94, 96, 61, 62, 33, 42, 95, 87, 71, 91, 48, 64]`

#### `get_fasil_gtip_list(conn, fasil_no, limit=200)`

- SQL: `SELECT gtip_code, tanim, tanim_hiyerarsi FROM gtip WHERE fasil = ? ORDER BY gtip_code LIMIT ?`

#### `get_fasil_notu(conn, fasil_no)`

- `fasil_notlari.fasil_notu` döner.

#### `search_gtip_fts(conn, query, limit=20)`

- `gtip_fts MATCH ?`; exception → `[]`.

#### `normalize_gtip_code(code)` / `gtip_exists(conn, code)`

- TR format doğrulama ve varlık kontrolü.

#### `sanitize_classification(conn, result)`

- Ana kod DB’de yoksa `gtip_code` boş, uyarı `gerekce`, `guven` düşük.
- `alternatifler` filtrelenir.

#### `classify_product(client, product_info, conn)`

- Aday 6 fasıl → not `[:2500]` + GTIP ilk 120 satır → FTS blok → `claude-haiku-4-5-20251001`, `max_tokens=900`.
- JSON: `re.search(r'\{[^{}]*\}', text, re.DOTALL)` — **iç içe `{` ile kırılgan**.
- Rate limit: 30s, 60s retry.

#### `read_scraped_excel` / `write_classified_excel` / `write_classified_html` / `main()`

- CLI: `input`, `-o`, `--db` (default `data/gtip_2026.db`), `--delay` (default 0.5).

### `scripts/temu_scraper.py`

- **Purpose:** CDP ile Chrome’a bağlanıp Temu scrape.

#### Sabitler

- `DETAIL_WAIT_MS = 18000`, `PAGE_TIMEOUT_MS = 45000`, `CAPTCHA_TIMEOUT = 120`

#### Önemli fonksiyonlar

- `detect_captcha`, `wait_for_captcha`, `wait_for_product_data`, `extract_product_data`, `_raw_signal`, `human_activity`, `safe_goto`, `scrape_product`, `read_links`, `write_output`, `write_html`, `download_image`, `main()`

- **`scrape_product(page, url, delay=2.0)`:** `delay` parametresi kullanılmıyor; gecikme `main` döngüsünde.

#### CDP

- `pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')`, default port **9222**.

### `scripts/build_db.py`

- `create_db`, `parse_fasil_xls`, `parse_fasil_notu`, `insert_tarife`, `insert_notlar`, `rebuild_fts`, `print_stats`, `main()`
- `--force` olmadan mevcut DB’ye yazmaz.

### `scripts/__pycache__/`

- Bytecode; kaynak değil.

### `output/*`

- Scraper/matcher çıktıları; `.gitignore` ile repoda olmayabilir.

---

## 3. DATA STRUCTURES

### Scraper satırı (`scrape_product`)

```json
{
  "url": "https://www.temu.com/...-g-601100183417510.html",
  "goods_id": "601100183417510",
  "title": "…",
  "description": "…",
  "keywords": "…",
  "product_details": "Key: Value; Key2: Value2",
  "image_url": "https://…",
  "properties": "SKU varyant özeti",
  "error": ""
}
```

Excel başlıkları: `SKU Variants` → `read_scraped_excel` ile `sku_variants`.

### Matcher çıktısı

```json
{
  "gtip_code": "3926.90.97.90.29",
  "fasil": 39,
  "gerekce": "…",
  "guven": "yuksek",
  "alternatifler": ["…"]
}
```

Hata: `"error": "…"` anahtarı.

---

## 4. MATCHING ALGORITHM

- **Strateji:** Malzeme + **FTS5** ile aday fasıl → **Claude** ile nihai kod (prompt: sadece listedeki kodlar).
- **Referans:** `build_db.py` ile yüklenen resmi tarife XLS + fasıl notları.
- **Klasik tek başına fuzzy/ML yok.**

**Pseudocode:**

```
for product in excel:
  fasils = get_candidate_fasils(...)
  context = birleştir(fasil_notu[:2500], gtip_satirlari[:120] x6, fts_sonuclari)
  text = Claude(system, user=context + ürün_alanları)
  obj = json_extract(text)  # kırılgan regex
  result = sanitize_classification(db, obj)
```

**Edge:** Geçersiz kod → boş + düşük güven; rate limit retry; FTS hata → `[]`.

---

## 5. EXTERNAL DEPENDENCIES & APIS

| Paket | Kullanım |
|--------|----------|
| xlrd | build_db `.xls` |
| openpyxl | Excel |
| anthropic | Claude API |
| playwright | CDP |

**API:** `anthropic` SDK, model `claude-haiku-4-5-20251001`, `max_tokens=900`.

**Harici süreç:** Chrome `--remote-debugging-port` (dokümantasyonda `Chrome Debug.bat`).

---

## 6. INPUT FORMAT

**Scraper:** Excel, hücrelerde `temu.com` + `goods` veya `-g-` içeren URL.

**Matcher:** Scraper çıktı kolonları: `URL`, `Goods ID`, `Title`, `Description`, `Keywords`, `Product Details`, `Image URL`, `SKU Variants`, `Error`.

---

## 7. OUTPUT FORMAT

**Matcher Excel kolonları:** `URL`, `Goods ID`, `Title`, `Product Details`, `GTIP Kodu`, `Fasil`, `Gerekce`, `Guven`, `Alternatifler`, `Hata`

**HTML:** `output_path` ile aynı kök ad + `.html`.

---

## 8. CONFIGURATION

| Öğe | Değer |
|-----|--------|
| Varsayılan DB | `data/gtip_2026.db` |
| Model | `claude-haiku-4-5-20251001` |
| max_tokens | 900 |
| Matcher `--delay` | 0.5 |
| Scraper `--delay` / `--jitter` | 8.0 / 5.0 |
| CDP port | 9222 |
| Fasıl notu kesiti | 2500 karakter |
| GTIP modele | ilk 120 satır (200 çekilir) |
| FTS ürün token | 12 terim |
| Aday fasıl | 8 (6 context) |

---

## 9. KNOWN ISSUES & LIMITATIONS

1. JSON çıkarma regex’i iç içe `{}` ile kırılır.
2. Fasıl notu 2500 karakterle kesiliyor.
3. GTIP listesi `ORDER BY gtip_code` + 120 satır — geç kodlar context’e girmeyebilir.
4. `scrape_product` `delay` kullanılmıyor.
5. Temu CAPTCHA / throttle riski.
6. `material_hints` / `defaults` sabit listeler.

---

## 10. HOW TO RUN

```bash
pip install -r requirements.txt
playwright install chromium
```

```bash
python scripts/build_db.py data/fasil_dosyalari/ --notlar data/fasil_notlari/ --db data/gtip_2026.db --force
```

```bash
python scripts/temu_scraper.py data/input.xlsx -o output/run_scraped.xlsx
```

```bash
set ANTHROPIC_API_KEY=sk-ant-...
python scripts/gtip_matcher.py output/run_scraped.xlsx --db data/gtip_2026.db -o output/my_gtip.xlsx
```

---

*Dosya yolu: `CLAUDE_PROJECT_HANDOFF.md` (repo kökü).*
