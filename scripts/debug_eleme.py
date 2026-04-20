"""
YOK vakası debug: Adım 1'de degerlendirme dict'ine girmeyen pozisyonları görünür yap.
Prompt'a "elenenleri de açıkla" satırı eklenir, 5 YOK vakası için API çağrısı yapılır.

Kullanım:
    python scripts/debug_eleme.py
"""

import sys, os, json, sqlite3
sys.path.insert(0, os.path.dirname(__file__))

import anthropic
from gtip_matcher import (
    build_pozisyon_context,
    build_pozisyon_prompt,
    _api_call_ctx_with_retry,
    extract_first_json_object,
)

DB_PATH  = "data/gtip_2026.db"
RUN_FILE = "experiments/run_20260416_1608.json"

# ── YOK vakalarını run JSON'dan çek ─────────────────────────────────────────
with open(RUN_FILE) as f:
    data = json.load(f)

yok_vakalar = []
for r in data["results"]:
    correct = r.get("debug", {}).get("correct_poz", "")
    pred    = r.get("debug", {}).get("pred_poz", "")
    raw     = r.get("debug", {}).get("pozisyon_raw_response", "")
    if pred and correct and pred != correct and correct not in raw:
        yok_vakalar.append({
            "title":            r.get("title", ""),
            "correct_poz":      correct,
            "pred_poz":         pred,
            "candidate_fasils": r["debug"].get("candidate_fasiller", []),
        })

print(f"{len(yok_vakalar)} YOK vakası bulundu.\n")

# ── Prompt: elenenler için de açıklama iste ──────────────────────────────────
conn = sqlite3.connect(DB_PATH)

# API key .env'den yükle
api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not api_key:
    env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_file):
        for line in open(env_file):
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                api_key = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                break

client = anthropic.Anthropic(api_key=api_key)
model  = "claude-haiku-4-5-20251001"

base_system = build_pozisyon_prompt(conn)
system_min7 = base_system.replace(
    "- Listede olmayan pozisyon uydurma.",
    "- Listede olmayan pozisyon uydurma.\n"
    "- Degerlendirme dict'ine EN AZ 7 pozisyon yaz (uyar veya uymaz).",
)

import re

for vaka in yok_vakalar:
    title       = vaka["title"]
    correct_poz = vaka["correct_poz"]
    pred_poz    = vaka["pred_poz"]
    cand_fasils = vaka["candidate_fasils"]

    product_text = f"Baslik: {title}"
    poz_context = build_pozisyon_context(
        conn, cand_fasils, title, "", "", "",
        note_max_chars=0, retrieval_top_n=20, izahname_max_chars=0,
    )
    poz_context_block = f"TARIFE CETVELI:\n{poz_context}"
    poz_query = (
        f"Asagidaki urun icin dogru FASIL ve 4 haneli POZISYONU sec.\n\n"
        f"URUN BILGILERI:\n{product_text}\n\n"
        f"Yukaridaki tarife cetvelini kullan.\n\n"
        f"Yanitini SADECE JSON olarak ver."
    )

    print(f"{'='*70}")
    print(f"{title}")
    print(f"correct={correct_poz}  pred={pred_poz}")
    print()

    try:
        resp = _api_call_ctx_with_retry(
            client, model, 1200, system_min7, poz_context_block, poz_query
        )
        raw = resp.content[0].text
        print(raw)
    except Exception as e:
        print(f"HATA: {e}")
    print()

conn.close()
