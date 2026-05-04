# GTİP Classifier — LLM-based Turkish Customs Tariff Classification

**TR:** Ürün metni → 12 haneli Türk gümrük tarife kodu (GTİP). Hiyerarşik LLM pipeline.

**EN:** Free-text product description → 12-digit Turkish customs tariff code (GTİP / HS code). Hierarchical multi-step LLM pipeline backed by a structured legal knowledge base.

---

## Background / Arka Plan

**EN:** Every product crossing a Turkish customs border must carry a GTİP code — Turkey's implementation of the international Harmonized System (HS). The code determines duty rates, VAT, and import restrictions. It is also a legal document: misclassification carries penalties.

The Turkish tariff schedule has 97 chapters, 3,986 four-digit positions, and 15,718 twelve-digit leaf codes — each with its own legal definition. Classification is not a semantic matching problem. The correct code is determined by **explanatory notes (izahname)**: dense legal texts that define what each position includes, what it excludes, and where excluded products are redirected.

There is no universal rule. For some product types material is decisive, for others physical form, for others primary function. Each candidate position must be evaluated against its own izahname. Synthetic fishing line goes to chapter 54 (synthetic monofilaments), not chapter 95 (fishing equipment) — because the izahname for position 9507 explicitly excludes monofilaments, regardless of end-use context. The pipeline is designed around this: it pools candidate positions, then reads and reasons over each position's legal text before committing.

**TR:** Her üründe GTİP kodu zorunlu; yanlış kod cezai yaptırım demek. 15.718 yaprak kod, hiyerarşik yapı. Doğru kodu semantik benzerlik değil, izahname belirler — kapsam, hariç tutma, yönlendirme kuralları. Evrensel kural yok; her aday pozisyon için izahname ayrı okunmalı.

---

## Knowledge Base / Bilgi Tabanı

**EN:** All legal knowledge is stored in a single SQLite database built from official 2026 Turkish tariff sources:

- **15,718 leaf codes** with full hierarchy paths and definitions
- **3,986 four-digit positions** with definitions
- **96 chapter notes** — legal texts defining inclusions and exclusions per chapter
- **97 explanatory note documents (izahname)** — one per chapter, full legal commentary
- **6 general HS interpretation rules** — applied universally across all classifications

There is no vector database or embedding layer. The model sees the relevant legal texts directly in its prompt. At query time the pipeline selects which chapters, positions, and izahname excerpts are relevant, assembles them into structured context blocks, and injects them into the appropriate step.

**TR:** Tüm hukuki bilgi tek SQLite dosyasında. Embedding yok — model hukuki metni doğrudan prompt'ta görür. Sorgu anında ilgili fasıl notları ve izahname seçilip context'e ekleniyor.

---

## Pipeline

**EN:** Five-stage hierarchical narrowing. Each stage constrains the input space of the next. A wrong chapter selection in step 0a makes the correct code unreachable downstream.

---

### Step 0a — Section selection
**Input:** Product title, description, material, category + all 21 tariff sections with brief descriptions.  
**Output:** ~5 candidate sections.  
**Model:** claude-haiku

---

### Step 0b — Chapter selection
**Input:** Product + chapters (fasıl) belonging to the 5 candidate sections.  
**Output:** ~5 candidate chapters.  
**Model:** claude-haiku

---

### Step 1a — Position evaluation
**Input:** Product + for each candidate chapter: all 4-digit positions with their definitions + chapter legal notes.  
**Output:** A structured evaluation dict — each candidate position rated `Uyar` (matches) or `Uzmaz` (doesn't match) with one-line reasoning.

```json
{
  "degerlendirme": {
    "3926": "Uyar: plastikten diğer eşya, şekil kısıtı yok",
    "3919": "Uzmaz: sadece yassı şekiller (film, bant, folyo) — 3B klips bu değil"
  },
  "fasil": 39,
  "pozisyon_kod": "3926"
}
```

**Model:** claude-haiku

---

### Step 1b — Izahname verification
**Input:** Product + for each position that passed 1a: the position's legal definition and its izahname excerpt.  
**Output:** Structured per-position evaluation:

```json
{
  "degerlendirme": {
    "3926": {
      "kapsam": "plastiklerden yapılmış, başka yerde belirtilmemiş diğer eşya",
      "haric": "kendinden yapışkan yassı şekiller → 3919",
      "eslestirme": "ürün 3B klips, yassı şekil değil — 3919 haric kapsamına girmiyor",
      "karar": "Uyar"
    },
    "3919": {
      "kapsam": "kendinden yapışkan plastik levha, plaka, film, bant — yassı şekiller",
      "haric": "3B şekilli ürünler bu pozisyona girmez",
      "eslestirme": "ürün 3B; 'yassı şekil' kısıtını karşılamıyor",
      "karar": "Uzmaz"
    }
  },
  "fasil": 39,
  "pozisyon_kod": "3926",
  "soru": ""
}
```

**Model:** claude-sonnet

---

### Step 2 — 12-digit code selection
**Input:** Product + all leaf codes under the selected 4-digit position + chapter notes + izahname + 6 general HS interpretation rules.  
**Output:** Final code with confidence and Turkish reasoning:

```json
{
  "gtip_code": "3926.90.97.90.29",
  "fasil": 39,
  "guven": "yuksek",
  "gerekce": "Ürün plastikten mamul kablo klipsi. Kendinden yapışkan ancak 3B şekilli — 3919 (yassı şekiller) değil 3926 (diğer plastik eşya). Alt pozisyon ağacında 3926.90.97.90 'plastikten diğer eşya' genel dalına, .29 alt koduna düşüyor.",
  "alternatifler": ["3926.90.97.90.11"]
}
```

**Model:** claude-haiku

---

**TR:** 5 aşamalı hiyerarşik daralma. Her adım sonrakinin giriş uzayını kısıtlar. 1b adımı en kritik: her aday pozisyon için izahname metni okunuyor, kapsam + hariç + eşleştirme alanları doldurularak karar veriliyor.

---

## Setup

```bash
pip install -r requirements.txt   # anthropic, xlrd, openpyxl
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# Build DB from source files (one-time, requires LibreOffice for .doc parsing)
python scripts/build_db.py data/fasil_dosyalari/ \
  --notlar data/fasil_notlari/ --izahname data/izahname_notlari/ \
  --yorum data/yorum_kurallari/ --icindekiler data/icindekiler/ \
  --db data/gtip_2026.db --force

# Classify
python scripts/gtip_matcher.py input.xlsx --db data/gtip_2026.db -o output/result.xlsx

# Eval against gold set
python scripts/eval_gtip.py --gold data/gold_set_pozisyon_33.xlsx \
  --db data/gtip_2026.db --out experiments/run_$(date +%Y%m%d_%H%M).json

# Compare two runs
python scripts/analyze_run.py experiments/run_a.json experiments/run_b.json \
  --db data/gtip_2026.db > experiments/report.md
```

Input Excel expects columns like `title`, `description`, `category_path`, `product_details` — variant names are normalized automatically.

---

*Tariff data from official Turkish customs sources (Türkiye Gümrük Tarife Cetveli 2026). GTİP suggestions are not legal customs decisions.*
