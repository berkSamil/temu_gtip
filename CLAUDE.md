# GTİP Müşaviri — CLAUDE.md

*Mevzuat tabanlı, kaynak-agnostik dijital gümrük müşaviri robotu.*

Bu dosya Cursor ve Claude Code için ortak bağlam kaynağıdır. Her session başında oku.

---

## PROJE DURUMU

**Aktif:** `scripts/gtip_matcher.py`, `scripts/build_db.py`, `scripts/eval_gtip.py`, `data/`
**Arşivlendi:** `scripts/temu_scraper.py` — scraping tamamen bırakıldı, dokunma.
**Hedef:** Türk gümrük mevzuatına (fasıl notları + izahname + yorum kuralları + içindekiler) hâkim, structured reasoning ile sınıflandırma yapan **dijital müşavir**. Herhangi bir ürün tanımı (TEMU manifest, broker XLSX, elle giriş, BSX elektronik, ileride PDF/görsel/dikte) → gerekçeli pozisyon/GTİP önerisi + alternatifler + varsayımlar + bilgi talepleri. TEMU sadece ilk gold set kaynağı; sistem her input kaynağına genelleşmeli.

**Sayısal durum (güncel):**
- DB: 15,718 GTİP, 3,986 pozisyon, 96 fasıl notu, 97 izahname, 6 yorum kuralı, 99 bölüm-fasıl kaydı
- Input: herhangi bir Excel (TEMU manifest, elle giriş, başka kaynak)
- Model: deepseek-v4-flash varsayılan (--provider deepseek), anthropic için claude-haiku-4-5

---

## KAPI GİBİ KURALLAR — PROJENİN YÖNÜ

**Bu sistem bir sınıflandırıcı değil, dijital gümrük müşaviri.**
GTİP gri-alan domain'i — aynı ürün koşula göre farklı pozisyona gider (3924/3925 montaj, balıkçılık misinası mamul/hammadde → 9507 vs 5404/5406, yapışkanlı klips form/fonksiyon). "Doğru cevap" tek değildir, "doğru muhakeme" vardır. Gold set bile koşullu — etiketler sabit değil, tartışmalı vakalar gold set'i güncelleyebilir.

**Müşavir tanımı:** uzman gibi muhakeme eden, mevzuatı (fasıl notu/izahname/yorum kuralları) ürünle karşılaştıran, **varsayımlarını açık eden**, eksik bilgi talep eden, alternatifleri yan yana koyup farkı söyleyen aktör. Bir TEMU sınıflandırıcı DEĞİL — TEMU tarihsel bir gold set kaynağı, sistem kaynak-agnostik genelleşmeli.

**Sistemin değeri:** Çıktının "ne dediği" değil **"niye dediği"**. Her adımın JSON şeması bir muhakeme artefaktı: varsayım, alternatif, karar noktası, eksik bilgi talebi.

### Evrim eksenleri (mimari yol haritası)

| Eksen | Şu an | Hedef |
|---|---|---|
| **Etkileşim** | Tek-turn + 1b Turn 2 (opsiyonel) | Multi-turn default; her adım proaktif soru sorabilir; kullanıcı dallar arasında dolanır |
| **Çıktı tipi** | Tek GTİP kodu | Koşullu çıktı: "X eğer monte; Y değilse" — branching reasoning |
| **Adımlar arası** | İzole (her adım kendi promptu) | Reasoning continuity: 0a varsayımı 0b'ye, 1a kararı 1b'ye taşınır, geri sorgulanır |
| **Modalite** | Metin | + Görsel (vision), uzun vadede tablo/PDF/dikte |
| **Gold set** | Sabit XLSX | Living dataset: koşullu etiketler, vaka-bazlı revizyon |
| **Domain** | TEMU manifest | Kaynak agnostik (BSX elektronik, broker, manuel) |
| **Değerlendirme** | Eval metric ortalaması | Reasoning trace inspection — metric alt veri |

### Kapı gibi 4 kural

1. **Eval metric düştü/çıktı diye AĞLAMA.** "Pozisyon %X düştü, regresyon" yazma. Run-to-run varyasyonu ~20pp (bkz `[[feedback_eval_yorumlama]]`). Karar reasoning trace okumakla verilir, metric tablosuyla değil.
2. **Bana SORMADAN kod yazma.** Hipotez teklif et → onay bekle → tek değişiklik. "Önce diff göster sonra uygula" değil — önce **sor**, sonra yaz. Bu CLAUDE.md edit'leri için bile geçerli; küçük yazım dahil.
3. **Reasoning trace birincil çıktı.** JSON'da `degerlendirme` (kapsam/haric/eslestirme/karar), `varsayimlar`, `karar_noktasi`, `alternatif_pozisyon`, `soru` — bu alanların varlığı + yapısı + içeriği sistemin gerçek değeri. Sınıflandırma kodu sadece bir özet.
4. **"Emin değilim, şu bilgi lazım" failure değil, feature.** Modelin epistemik alçakgönüllülüğü (uncertainty + bilgi talebi) sistemin olgunluğudur. Sistem ne kadar açıkça "şu varsayımı yaptım, şu durumda farklı olur" diyebiliyorsa o kadar iyi. Belirsizliği gizleyen yüksek-güven cevaplar daha kötüdür.

### Pratik sonuçlar

- Hipotez önerirken "%X'i %Y'ye çıkarır" yerine "reasoning şu yönde zenginleşir / şu varsayımı açık eder / şu yeni soru çıkar" diye çerçevele
- Eval sonucu özetlerken önce trace inceleme, sonra (varsa) metric — metric başlık değil
- Yeni feature önerirken birincil eksen: interaktivite, reasoning continuity, koşullu çıktı, multimodal
- Tek run "kötü" diye revert ETME; reasoning trace bozulduysa revert et

---

## MİMARİ

```
INPUT (ürün tanımı — kaynak agnostik: TEMU manifest, broker XLSX, elle giriş,
       BSX elektronik, ileride PDF/görsel/dikte)
  ürün adı, açıklama, material, kategori, [görsel URL]
       ↓
  [ADIM 0a] BÖLÜM SEÇİMİ (21 bölüm → 5 aday)
      Model 21 bölüm listesine bakarak 5 aday bölüm seçer. (max_tokens=400)
      FONKSIYON MATERYALI EZER kuralı + tekstil form istisnası prompt'ta mevcut.
       ↓
  [ADIM 0b] FASIL SEÇİMİ (seçilen bölümlerin fasılları → 8 aday fasıl)
      max_tokens=400. Adım 0a/0b parse fail olursa pipeline hata döner.
       ↓
  [ADIM 1a] POZİSYON SEÇİMİ (4'lü pozisyon)
      İlk 5 aday fasıl için: fasıl notu + izahname özeti + tüm 4'lü pozisyonlar
      Model fasıl + 4'lü pozisyon kodu seçer. (max_tokens=1500)
      Parse fail olursa pipeline hata döner ({"error": "adim_1_parse_fail"}).
       ↓
  [ADIM 1b] POZİSYON DOĞRULAMA (reasoning model)
      Aday pozisyonlar için izahname karşılaştırması.
      JSON: degerlendirme(kapsam/haric/eslestirme/karar) + varsayimlar
          + alternatif_pozisyon + karar_noktasi + soru
      DeepSeek: deepseek-v4-pro (reasoning), max_tokens=10000
      Anthropic: claude-sonnet-4-*, max_tokens=1200
       ↓
  [ADIM 1b — TURN 2] (sadece --interactive flag ile)
      Kullanıcıya 1b karar özeti gösterilir (pozisyon_kod, alternatif,
      karar_noktasi, varsayimlar, soru). Cevap girilirse 1b multi-turn
      olarak ikinci kez çağrılır, ek bilgiyle yeniden değerlendirir.
      Turn 1 JSON ayrı saklanır (adim1b_parsed); Turn 2 JSON ayrı
      (adim1b_turn2_parsed). Turn 2 başarısız olursa Turn 1 korunur.
       ↓
  ► ÇIKIŞ: 4 haneli pozisyon kodu (varsayılan)
      Pipeline burada durur. gtip_code = 4 haneli pozisyon.
       ↓  (sadece --adim2 flag ile)
  [ADIM 2] GTİP SEÇİMİ (12 haneli) — VARSAYILAN KAPALI
      Seçilen pozisyon altındaki TÜM 12'li GTİP'ler + fasıl notu + izahname
      Yorum kuralları system prompt'a gömülü. (max_tokens default 1200)
       ↓
  OUTPUT: _classified.xlsx + _classified.html
```

FTS fallback yok — Adım 0 veya 1 fail olursa pipeline hata döner. `_classify_flat()` ve FTS-tabanlı candidate fonksiyonları kaldırıldı (2026-05-12).

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
  ✅ FTS5 indeksleri DB'de var (gtip_fts, notlar_fts, izahname_fts) — runtime sınıflandırmada kullanılmıyor
  ✅ 97 izahname kaydı, 6 yorum kuralı, 99 bölüm-fasıl kaydı
  ✅ .doc → .docx dönüşümü soffice ile otomatik (build_db.py içinde)

---

## UZUN VADELİ EVRİM

Aktif hipotezler ve yol haritası → memory'de.

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
│   ├── eval_gtip.py           ← gold set üzerinde reasoning trace + sinyal toplama (metric alt veri)
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
  --provider deepseek \                  # varsayılan: deepseek | anthropic
  --model deepseek-v4-flash \            # varsayılan: provider'a göre otomatik
  --adim1b-model deepseek-v4-pro \       # 1b reasoning model
  --max-tokens 1200 \                    # Adım 2 için max token (default 1200)
  --delay 0 \                            # API istekleri arası bekleme (saniye)
  --note-chars 0 \                       # fasıl notu max karakter (default: 0=kapalı)
  --izahname-chars 0 \                   # izahname max karakter (default: 0=kapalı)
  --no-adim1b \                          # 1b izahname doğrulamasını atla
  --adim2                                # 12 haneli GTİP seçimini etkinleştir (varsayılan: kapalı)
```

**eval_gtip.py ek parametreler:**
```bash
python scripts/eval_gtip.py data/gold_set_30.xlsx \
  --workers 50 \                         # paralel iş parçacığı (default 50; DeepSeek yüklendiğinde düşür)
  --interactive \                        # 1b karar özetini göster, Turn 2 müdahalesine izin ver
  --limit 5 \                            # ilk N ürünü çalıştır (smoke test için)
  --items 6,21,25 \                      # 1-tabanlı belirli indeksler
  --log-prompts                          # tüm promptları JSON'a yaz (dosya büyür)
```

---

## INPUT/OUTPUT FORMAT

*Input formatı kaynak-agnostik; aşağıdaki kolonlar TEMU'dan miras ama herhangi bir kaynaktan map edilebilir (`normalize_product_row()` varyantları handle eder).*

**Input Excel kolonları:**
```
  title / product_title
  description / aciklama
  keywords / category_path / category
  product_details
  image_url / thumbnail_url
  url / product_url
```

**Output JSON yapısı (--adim2 kapalı, varsayılan):**
```json
{
  "gtip_code": "3926",                    // 4 haneli pozisyon (--adim2 ile 12 hane)
  "fasil": 39,
  "gerekce": "[3926] Uyar: ... | [3924] Uymaz: ...",
  "guven": "yuksek|orta|dusuk",           // 1b Uyar sayısı 1→yuksek, >1→orta, 0→dusuk
  "alternatifler": [],
  "soru": "",                             // 1b'nin sorduğu netleştirme sorusu (varsa)
  "error": "",                            // adim_1_parse_fail, adim_1_pozisyon_db_yok, vs
  "debug": {
    "candidate_bolumler": [7, 20],
    "candidate_fasiller": [39, 83, 73],
    "bolum_raw_response": "...",
    "fasil_raw_response": "...",
    "adim1a_parsed":      { ... },         // 1a JSON (degerlendirme + pozisyon_kod)
    "adim1b_parsed":      { ... },         // 1b Turn 1 JSON (snapshot, Turn 2 override etse bile kalır)
    "adim1b_turn2_parsed": { ... },        // 1b Turn 2 JSON (--interactive ile)
    "adim1b_turn2_raw":   "...",
    "reasoning_0a/0b/1a/1b/1b_turn2": "...",  // DeepSeek thinking blokları
    "secilen_pozisyon":   "3926",
    "secilen_fasil":      39,
    "token_usage": {"adim_0a": {...}, "adim_0b": {...}, "adim_1": {...},
                    "adim_1b": {...}, "adim_1b_turn2": {...}, "adim_2": {...}, "toplam": {...}},
    "token_breakdown":    { ... }
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
ANTHROPIC_API_KEY=sk-ant-...   # anthropic provider için
DEEPSEEK_API_KEY=sk-...        # deepseek provider için (varsayılan)
```

---

## ÇALIŞMA KURALLARI (Claude Code ve Cursor için)

1. Her eval run'ını kaydet: `experiments/run_YYYYMMDD_HHMM.json`
2. FTS fallback yok — yeni recovery yolu önerme (silindi, kalıcı karar)
3. `temu_scraper.py`'a dokunma — arşivlenmiş
4. DB şemasını değiştirirsen `build_db.py --force` ile rebuild test et
5. Yeni veri kaynağı eklerken: orijinal dosya `data/` altına, parser `build_db.py`'a, çıktı SQLite'a

---

## DEĞİŞİKLİK CYCLE'I (prompt / pipeline değişiklikleri için)

1. **Hipotez öner — reasoning ekseni üzerinden çerçevele.** "Bu değişiklik şu reasoning yapısını şu yönde zenginleştirir / şu varsayımı açık eder / şu yeni soru çıkar." Sadece "metric'i artırır" gerekçesi YETERSİZ. Kullanıcı onayını **mutlaka** bekle.
2. **Tek değişiklik yap** — iki değişikliği asla birleştirme; sebep-sonuç izlenemez olur.
3. **Eval çalıştır** (sinyal toplamak için, hüküm vermek için değil):
   ```bash
   python3 scripts/eval_gtip.py data/gold_set_pozisyon_33.xlsx --workers 50
   ```
4. **Reasoning trace üzerinden yorumla** (`[[feedback_eval_yorumlama]]` + KAPI GİBİ KURALLAR):
   - **Önce trace oku**: `adim1a_parsed.degerlendirme`, `adim1b_parsed` (kapsam/haric/eslestirme/karar), `varsayimlar`, `karar_noktasi`, `alternatif_pozisyon`, `soru` — yapı zenginleşti mi? Model varsayımlarını açık ediyor mu? Doğru soruları soruyor mu? Alternatifleri karşılaştırıyor mu?
   - **Sonra (opsiyonel) metric**: pozisyon_secim vs sadece destek sinyali. Tek run baseline değil — varyasyon ~20pp. "%X düştü" başlık DEĞİL.
   - **Vaka-vaka bak**: hipotezin niyetlediği yere etki etti mi? Yan etki var mı?
5. **Karar: reasoning kalitesi ekseninde.**
   - Trace zenginleşti → commit
   - Trace değişmedi veya bozuldu → revert (metric ne derse desin)
   - Belirsiz → 2-3 run daha; hâlâ belirsizse trace inspeksiyonu derinleştir

**Sabit parametreler:** `--note-chars 0 --izahname-chars 0` (varsayılan).
**fix_loop** pause'da — `_check_regression` mantığı tek-run baseline kabul ediyordu, GTİP gri-alan domainine uygun değil.
**FTS deneyleri yok** — FTS fallback kalıcı kapalı ([[feedback_fts_kapali]]).

