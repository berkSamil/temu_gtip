# temu_gtip

Temu ürün metni + Türkiye gümrük tarife cetveli (SQLite) ile **12 haneli GTİP** önerisi üreten araçlar. İsteğe bağlı Playwright scraper; matcher **scraper Excel** veya **manuel doldurulmuş Excel** ile çalışır.

## Gereksinimler

- Python 3.10+
- `pip install -r requirements.txt`
- Scraper için: `playwright install chromium` ve Chrome’u remote debugging ile açma (bkz. `scripts/temu_scraper.py` başlığı)
- Matcher için: [Anthropic API](https://www.anthropic.com/) anahtarı — ortam değişkeni `ANTHROPIC_API_KEY` veya repo kökünde `.env` (`ANTHROPIC_API_KEY=...`). **`.env` asla commitlenmez.**

## Veritabanı (`data/gtip_2026.db`)

`.gitignore` nedeniyle **SQLite dosyası repoda yok**; yerelde üretin:

```bash
python scripts/build_db.py data/fasil_dosyalari/ --notlar data/fasil_notlari/ --db data/gtip_2026.db --force
```

Repoda `data/fasil_dosyalari/` ve `data/fasil_notlari/` XLS kaynakları vardır.

## GTİP matcher (Ana akış)

Manuel veya scraper çıktısı Excel:

```bash
python scripts/gtip_matcher.py path/to/products.xlsx --db data/gtip_2026.db -o output/classified.xlsx
```

İsteğe bağlı: `--refine`, `--model`, `--note-chars`, `--retrieval` (bkz. `python scripts/gtip_matcher.py -h`).

**Manuel Excel sütunları** (örnek): `product url`, `product title`, `category path`, thumbnail, `product details` — `normalize_product_row` bunları iç forma çevirir.

## Temu scraper (isteğe bağlı)

```bash
python scripts/temu_scraper.py data/input.xlsx --delay 12 --jitter 8 --warmup-every 12
```

Çıktı `output/` altında (`.gitignore`).

## Dokümantasyon

- `CLAUDE_PROJECT_HANDOFF.md` — proje teknik özeti (başka asistan / el değişimi için)

## Lisans / uyarı

Tarife verileri resmi kaynaklardan türetilmelidir; GTİP önerileri **nihai gümrük kararı değildir**. API anahtarlarını ve kişisel link listelerini herkese açık repoda paylaşmayın.
