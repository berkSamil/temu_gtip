"""
GTİP Eval Run Karşılaştırıcı
==============================
İki eval JSON'unu ve DB'yi okuyarak offline karşılaştırma raporu üretir.
API çağrısı yok.

Kullanım:
    python3 scripts/analyze_run.py experiments/run_XXXX.json \
        --baseline experiments/run_20260407_0052.json \
        --db data/gtip_2026.db

Çıktı:
    experiments/report_XXXX.md   — detaylı Markdown raporu
    terminal                     — özet tablo + düzelen/bozulan sayıları
"""

import sys
import os
import re
import json
import sqlite3
import argparse
import subprocess
from datetime import datetime


# ---------------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ---------------------------------------------------------------------------

def _clean(code):
    """GTİP kodundan nokta kaldır."""
    return re.sub(r'[^0-9]', '', code or '')


def _fasil(code):
    return _clean(code)[:2]


def _pozisyon(code):
    return _clean(code)[:4]


def _load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _auto_baseline(exp_dir, min_n=20):
    """
    experiments/ altındaki en yüksek exact accuracy'li JSON'u döner.
    min_n: bu sayının altındaki run'lar göz ardı edilir (küçük test run'ları).
    """
    best_path = None
    best_exact = -1.0
    for fname in os.listdir(exp_dir):
        if not (fname.startswith('run_') and fname.endswith('.json')):
            continue
        fpath = os.path.join(exp_dir, fname)
        try:
            d = _load_json(fpath)
            if d.get('n_total', 0) < min_n:
                continue
            exact = d.get('metrics', {}).get('fasil', {}).get('accuracy', -1)
            if exact > best_exact:
                best_exact = exact
                best_path = fpath
        except Exception:
            continue
    return best_path


def _get_pozisyon_tanim(conn, poz_code):
    """4'lü pozisyon kodunun tanımını DB'den çek."""
    if not poz_code:
        return ""
    clean = _clean(str(poz_code))[:4]
    row = conn.execute("""
        SELECT tanim FROM pozisyon
        WHERE substr(kod_clean, 1, 4) = ?
        ORDER BY seviye LIMIT 1
    """, (clean,)).fetchone()
    if row:
        return row[0] or ""
    # fallback: gtip tablosundan hiyerarşi
    row2 = conn.execute("""
        SELECT tanim_hiyerarsi FROM gtip
        WHERE substr(gtip_clean, 1, 4) = ?
        ORDER BY gtip_code LIMIT 1
    """, (clean,)).fetchone()
    return (row2[0] if row2 else "") or ""


def _get_fasil_tanim(conn, fasil_no):
    """Fasıl tanımını bolum_fasil tablosundan çek."""
    if not fasil_no:
        return ""
    try:
        row = conn.execute("""
            SELECT fasil_adi FROM bolum_fasil WHERE fasil_no = ?
        """, (int(fasil_no),)).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def _get_bolum_tanim(conn, bolum_no):
    """Bölüm tanımını bolum_fasil tablosundan çek."""
    if not bolum_no:
        return ""
    try:
        row = conn.execute("""
            SELECT DISTINCT bolum_adi FROM bolum_fasil WHERE bolum_no = ?
        """, (int(bolum_no),)).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def _parse_gerekce(raw_response):
    """
    Raw JSON response'tan 'gerekce' field'ını tam olarak parse et.
    Brace-balanced parse kullanır — iç içe JSON'larda da çalışır.
    Başarısızsa None döner.
    """
    if not raw_response:
        return None
    try:
        start = raw_response.index('{')
        depth = 0
        for i, ch in enumerate(raw_response[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    obj = json.loads(raw_response[start:i + 1])
                    gerekce = obj.get('gerekce') or obj.get('reasoning') or ''
                    return gerekce.replace('\n', ' ').strip() if gerekce else None
    except Exception:
        pass
    return None


def _raw_summary(raw_response, fallback_chars=150):
    """
    Raw JSON response'tan gerekce alanını döner.
    - JSON parse başarılıysa: tam gerekce metni (kısaltmadan).
    - Parse başarısızsa: ilk fallback_chars karakter, son boşluktan kesilmiş + '...'
    """
    if not raw_response:
        return "(raw response yok)"
    gerekce = _parse_gerekce(raw_response)
    if gerekce:
        return gerekce
    # Fallback: smart word-boundary truncation
    text = raw_response.replace('\n', ' ').strip()
    if len(text) <= fallback_chars:
        return text
    cut = text[:fallback_chars].rfind(' ')
    if cut > fallback_chars // 2:
        return text[:cut] + '...'
    return text[:fallback_chars] + '...'


def _derive_config(run_data):
    """JSON'dan config parametrelerini çıkar."""
    cfg = {
        'model':          run_data.get('model', '?'),
        'note_chars':     run_data.get('params', {}).get('note_chars', '?'),
        'izahname_chars': run_data.get('params', {}).get('izahname_chars', '?'),
        'max_tokens':     run_data.get('params', {}).get('max_tokens', '?'),
        'gtip_rows':      run_data.get('params', {}).get('gtip_rows', '?'),
        'retrieval':      run_data.get('params', {}).get('retrieval', '?'),
        'refine':         run_data.get('refine', False),
        'timestamp':      run_data.get('timestamp', '?'),
    }
    # bolum/fasil aday sayılarını results'tan çıkar
    bolum_lens, fasil_lens = [], []
    for r in run_data.get('results', []):
        dbg = r.get('debug') or {}
        bl = dbg.get('candidate_bolumler') or []
        fl = dbg.get('candidate_fasiller') or []
        if bl: bolum_lens.append(len(bl))
        if fl: fasil_lens.append(len(fl))
    cfg['bolum_aday'] = max(bolum_lens) if bolum_lens else '?'
    cfg['fasil_aday'] = max(fasil_lens) if fasil_lens else '?'
    return cfg


def _match_results(base_results, run_results):
    """
    İki run'ın sonuçlarını title+correct_gtip ile eşleştir.
    Returns: list of (base_item, run_item) — her ikisi de None olabilir.
    """
    def key(r):
        return (r.get('title', '').strip().lower()[:60],
                _clean(r.get('correct_gtip', '')))

    run_map = {key(r): r for r in run_results}
    pairs = []
    for b in base_results:
        k = key(b)
        pairs.append((b, run_map.get(k)))
    return pairs


# ---------------------------------------------------------------------------
# Geçiş matrisi
# ---------------------------------------------------------------------------

def _level_correct(item, level):
    """item'ın belirtilen seviyede doğru olup olmadığını döner (True/False/None)."""
    return (item.get('metrics') or {}).get(level)


def _transition(base_item, run_item, level):
    """
    'fixed'   : yanlış→doğru
    'broken'  : doğru→yanlış
    'chronic' : yanlış→yanlış
    'stable'  : doğru→doğru
    'missing' : run_item yok
    """
    if run_item is None:
        return 'missing'
    b = _level_correct(base_item, level)
    r = _level_correct(run_item, level)
    if b is True  and r is True:  return 'stable'
    if b is False and r is True:  return 'fixed'
    if b is True  and r is False: return 'broken'
    if b is False and r is False: return 'chronic'
    return 'unknown'


# ---------------------------------------------------------------------------
# Hata analizi (tek ürün)
# ---------------------------------------------------------------------------

def _analyze_error(conn, base_item, run_item, run_name):
    """
    Tek bir hatalı ürün için detaylı analiz dict'i döner.
    run_item None ise sadece base verileriyle çalışır.
    """
    correct     = base_item.get('correct_gtip', '')
    title       = base_item.get('title', '')
    base_pred   = base_item.get('predicted_gtip', '')
    run_pred    = run_item.get('predicted_gtip', '') if run_item else ''

    correct_fasil  = _fasil(correct)
    correct_poz    = _pozisyon(correct)

    base_dbg = base_item.get('debug') or {}
    run_dbg  = run_item.get('debug') or {} if run_item else {}

    base_bolumler = base_dbg.get('candidate_bolumler') or []
    base_fasiller = base_dbg.get('candidate_fasiller') or []
    run_bolumler  = run_dbg.get('candidate_bolumler') or []
    run_fasiller  = run_dbg.get('candidate_fasiller') or []

    # Kırılma noktası (run veya base'e göre, run tercihli)
    dbg_main = run_dbg if run_item else base_dbg
    fasiller_main = run_fasiller or base_fasiller
    bolumler_main = run_bolumler or base_bolumler
    pred_main = run_pred or base_pred

    # Doğru bölümü DB'den bul
    correct_fasil_int = int(correct_fasil) if correct_fasil.isdigit() else None
    correct_bolum = None
    if correct_fasil_int:
        row = conn.execute("""
            SELECT bolum_no FROM bolum_fasil WHERE fasil_no = ?
        """, (correct_fasil_int,)).fetchone()
        correct_bolum = row[0] if row else None

    # Kırılma noktası tespiti
    breakpoint = "GTİP"
    if correct_fasil not in [str(f)[:2].zfill(2) if str(f).isdigit()
                              else str(f).zfill(2) for f in fasiller_main]:
        # Doğru fasıl candidate'a girememiş
        if correct_bolum and correct_bolum not in bolumler_main:
            breakpoint = "BÖLÜM (doğru bölüm candidate'a girmemiş)"
        else:
            breakpoint = "FASİL (doğru fasıl candidate'a girmemiş)"
    elif _pozisyon(pred_main) != correct_poz:
        breakpoint = "POZİSYON (doğru fasıl seçildi ama yanlış pozisyon)"
    else:
        breakpoint = "GTİP (pozisyon doğru ama 12'li yanlış)"

    # DB'den tanımlar
    pred_poz    = _pozisyon(pred_main) if pred_main else ''
    correct_poz_tanim   = _get_pozisyon_tanim(conn, correct_poz)
    pred_poz_tanim      = _get_pozisyon_tanim(conn, pred_poz)
    correct_fasil_tanim = _get_fasil_tanim(conn, correct_fasil_int)
    pred_fasil_no       = int(_fasil(pred_main)) if _fasil(pred_main).isdigit() else None
    pred_fasil_tanim    = _get_fasil_tanim(conn, pred_fasil_no)

    # Candidate diff
    base_fasil_set = set(str(x) for x in base_fasiller)
    run_fasil_set  = set(str(x) for x in run_fasiller)
    new_in_run  = sorted(run_fasil_set - base_fasil_set, key=lambda x: int(x) if x.isdigit() else 99)
    gone_in_run = sorted(base_fasil_set - run_fasil_set, key=lambda x: int(x) if x.isdigit() else 99)

    # Raw gerekçe — tam metin (kısaltmasız)
    bolum_summary    = _raw_summary(dbg_main.get('bolum_raw_response'))
    fasil_summary    = _raw_summary(dbg_main.get('fasil_raw_response'))
    pozisyon_summary = _raw_summary(dbg_main.get('adim1a_parsed') or dbg_main.get('adim1a_raw_response') or dbg_main.get('pozisyon_raw_response'))
    gtip_summary     = _raw_summary(dbg_main.get('gtip_raw_response'))

    return {
        'title':               title,
        'correct':             correct,
        'correct_fasil':       correct_fasil,
        'correct_poz':         correct_poz,
        'correct_fasil_tanim': correct_fasil_tanim,
        'correct_poz_tanim':   correct_poz_tanim,
        'correct_bolum':       correct_bolum,
        'base_pred':           base_pred,
        'run_pred':            run_pred,
        'pred_poz':            pred_poz,
        'pred_poz_tanim':      pred_poz_tanim,
        'pred_fasil_tanim':    pred_fasil_tanim,
        'breakpoint':          breakpoint,
        'base_bolumler':       base_bolumler,
        'run_bolumler':        run_bolumler,
        'base_fasiller':       base_fasiller,
        'run_fasiller':        run_fasiller,
        'new_in_run':          new_in_run,
        'gone_in_run':         gone_in_run,
        'bolum_summary':       bolum_summary,
        'fasil_summary':       fasil_summary,
        'pozisyon_summary':    pozisyon_summary,
        'gtip_summary':        gtip_summary,
    }


# ---------------------------------------------------------------------------
# Token karşılaştırması
# ---------------------------------------------------------------------------

def _avg_tokens(results, step):
    vals = []
    for r in results:
        dbg = r.get('debug') or {}
        tok = (dbg.get('token_usage') or {}).get(step)
        if tok:
            vals.append(tok.get('in', 0) + tok.get('out', 0))
    return round(sum(vals) / len(vals)) if vals else 0


# ---------------------------------------------------------------------------
# Git diff yardımcıları
# ---------------------------------------------------------------------------

def _run_git_diff(n_commits=1, files=None):
    """git diff HEAD~N -- <files> çalıştır, çıktıyı döner. Hata varsa boş string."""
    files = files or ['scripts/gtip_matcher.py', 'CLAUDE.md']
    cmd = ['git', 'diff', f'HEAD~{n_commits}', '--'] + files
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout
    except Exception:
        return ''


def _parse_prompt_diff(diff_text):
    """
    git diff çıktısından prompt değişkenlerini ve config satırlarını çıkar.
    Returns: list of dict {type, variable, removed, added}
    """
    if not diff_text:
        return []

    changes = []
    current_var = None
    current_added = []
    current_removed = []

    # Prompt değişken isimleri
    prompt_vars = {
        '_BOLUM_PROMPT_BASE', '_FASIL_PROMPT_BASE', '_POZISYON_PROMPT_BASE',
        'SYSTEM_PROMPT', 'REFINE_SYSTEM_PROMPT',
    }
    # Config ile ilgili anahtar kelimeler
    config_keys = {'bolum_aday', 'fasil_aday', 'max_tokens', 'note_chars',
                   'izahname_chars', 'candidate_bolumler', 'candidate_fasils',
                   '[:5]', '[:8]', '[:3]', 'max_fasils'}

    for line in diff_text.splitlines():
        # Değişken başlangıcı tespiti
        for var in prompt_vars:
            if var in line and ('=' in line or line.startswith('+') or line.startswith('-')):
                if current_var and (current_added or current_removed):
                    changes.append({
                        'type': 'prompt',
                        'variable': current_var,
                        'removed': current_removed[:5],
                        'added': current_added[:5],
                    })
                current_var = var
                current_added = []
                current_removed = []
                break

        if line.startswith('+') and not line.startswith('+++'):
            content = line[1:].strip()
            if current_var:
                current_added.append(content[:120])
            # Config değişikliği mi?
            for ck in config_keys:
                if ck in content:
                    changes.append({
                        'type': 'config',
                        'variable': ck,
                        'removed': [],
                        'added': [content[:120]],
                    })
                    break
        elif line.startswith('-') and not line.startswith('---'):
            content = line[1:].strip()
            if current_var:
                current_removed.append(content[:120])

    # Son değişken
    if current_var and (current_added or current_removed):
        changes.append({
            'type': 'prompt',
            'variable': current_var,
            'removed': current_removed[:5],
            'added': current_added[:5],
        })

    # Deduplicate config
    seen = set()
    deduped = []
    for c in changes:
        key = (c['type'], c['variable'], tuple(c['added']))
        if key not in seen:
            seen.add(key)
            deduped.append(c)
    return deduped


def _broken_noise_analysis(conn, base_item, run_item, base_cfg, run_cfg):
    """
    BOZULAN ürün için gürültü analizi:
    - Yeni giren fasılın seçilen yanlış tahminle örtüşüyor mu?
    - Evetse → gürültü, hayırsa → nondeterminizm
    Returns: dict
    """
    if run_item is None:
        return {}

    base_dbg = base_item.get('debug') or {}
    run_dbg  = run_item.get('debug') or {}

    base_fasiller = [str(x).zfill(2) for x in (base_dbg.get('candidate_fasiller') or [])]
    run_fasiller  = [str(x).zfill(2) for x in (run_dbg.get('candidate_fasiller')  or [])]
    base_bolumler = base_dbg.get('candidate_bolumler') or []
    run_bolumler  = run_dbg.get('candidate_bolumler')  or []

    run_pred      = run_item.get('predicted_gtip', '') or ''
    pred_fasil    = _fasil(run_pred).zfill(2)

    new_fasils = sorted(set(run_fasiller) - set(base_fasiller))
    new_bolums = sorted(set(str(x) for x in run_bolumler) -
                        set(str(x) for x in base_bolumler))

    # Seçilen yanlış fasıl yeni girenden mi geliyor?
    triggered_by = [f for f in new_fasils if f == pred_fasil]

    bolum_delta = f"bölüm aday: {base_cfg.get('bolum_aday','?')} → {run_cfg.get('bolum_aday','?')}"
    fasil_delta = f"fasıl aday: {base_cfg.get('fasil_aday','?')} → {run_cfg.get('fasil_aday','?')}"

    if triggered_by:
        verdict = (f"🔊 GÜRÜLTÜ: yeni giren fasıl {triggered_by[0]} "
                   f"({_get_fasil_tanim_str(triggered_by[0])}) tetikledi "
                   f"({fasil_delta})")
    elif new_fasils and pred_fasil not in base_fasiller:
        verdict = (f"🔊 GÜRÜLTÜ (dolaylı): pred fasıl {pred_fasil} baseline'da yoktu, "
                   f"yeni fasıllar: {new_fasils} ({fasil_delta})")
    else:
        verdict = f"🎲 PROMPT/MODEL NONDETERMİNİZMİ (candidate listesi aynı, {fasil_delta})"

    return {
        'verdict':      verdict,
        'new_fasils':   new_fasils,
        'new_bolums':   new_bolums,
        'bolum_delta':  bolum_delta,
        'fasil_delta':  fasil_delta,
        'base_fasiller': base_fasiller,
        'run_fasiller':  run_fasiller,
        'base_bolumler': base_bolumler,
        'run_bolumler':  run_bolumler,
    }


# Fasıl tanımını int veya str'den çek (conn bağımlı değil, cache üzerinden)
_FASIL_TANIM_CACHE = {}

def _get_fasil_tanim_str(fasil_str):
    """String fasıl no'dan tanım (cache'siz, boş döner eğer conn yok)."""
    return _FASIL_TANIM_CACHE.get(str(fasil_str).zfill(2), '')


def _warm_fasil_cache(conn):
    """bolum_fasil tablosundan tüm fasıl tanımlarını önbelleğe al."""
    rows = conn.execute("SELECT fasil_no, fasil_adi FROM bolum_fasil").fetchall()
    for no, adi in rows:
        _FASIL_TANIM_CACHE[str(no).zfill(2)] = adi or ''


# ---------------------------------------------------------------------------
# Rapor oluşturma
# ---------------------------------------------------------------------------

def build_report(base_data, run_data, base_path, run_path, conn):
    lines = []
    a = lines.append

    _warm_fasil_cache(conn)

    base_cfg = _derive_config(base_data)
    run_cfg  = _derive_config(run_data)
    base_name = os.path.basename(base_path)
    run_name  = os.path.basename(run_path)

    a(f"# GTİP Eval Karşılaştırma Raporu")
    a(f"")
    a(f"- **Bu run:** `{run_name}` ({run_data.get('timestamp','?')[:16]})")
    a(f"- **Baseline:** `{base_name}` ({base_data.get('timestamp','?')[:16]})")
    a(f"- **Rapor tarihi:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    a(f"")

    # -----------------------------------------------------------------------
    # 1. Pipeline Karşılaştırması
    # -----------------------------------------------------------------------
    a(f"## 1. Pipeline Karşılaştırması")
    a(f"")
    a(f"| Parametre | Baseline | Bu Run |")
    a(f"|-----------|----------|--------|")
    params = [
        ('Model',           'model'),
        ('Bölüm aday sayısı','bolum_aday'),
        ('Fasıl aday sayısı','fasil_aday'),
        ('note_chars',      'note_chars'),
        ('izahname_chars',  'izahname_chars'),
        ('max_tokens',      'max_tokens'),
        ('gtip_rows',       'gtip_rows'),
        ('retrieval',       'retrieval'),
        ('refine',          'refine'),
    ]
    for label, key in params:
        bv = base_cfg.get(key, '?')
        rv = run_cfg.get(key, '?')
        marker = ' ← **değişti**' if str(bv) != str(rv) else ''
        a(f"| {label} | {bv} | {rv}{marker} |")
    a(f"")

    # -----------------------------------------------------------------------
    # 2. Özet Tablo
    # -----------------------------------------------------------------------
    a(f"## 2. Özet Tablo")
    a(f"")
    a(f"| Metrik | Baseline | Bu Run | Fark |")
    a(f"|--------|----------|--------|------|")
    base_m = base_data.get('metrics', {})
    run_m  = run_data.get('metrics', {})
    for level, label in [('fasil','Fasıl (2 hane)'), ('pozisyon','Pozisyon (4 hane)'),
                          ('alt_poz','Alt poz (6 hane)'), ('exact','Exact (12 hane)')]:
        ba = base_m.get(level, {}).get('accuracy', 0)
        ra = run_m.get(level, {}).get('accuracy', 0)
        diff = ra - ba
        arrow = '⬆' if diff > 0.5 else ('⬇' if diff < -0.5 else '—')
        a(f"| {label} | {ba:.1f}% | {ra:.1f}% | {arrow} {diff:+.1f}pp |")
    a(f"")
    bnh = base_data.get('n_total', 0)
    rnh = run_data.get('n_total', 0)
    a(f"Ürün sayısı: baseline={bnh}, bu run={rnh}")
    a(f"")

    # -----------------------------------------------------------------------
    # 3. Geçiş Matrisi
    # -----------------------------------------------------------------------
    a(f"## 3. Geçiş Matrisi")
    a(f"")

    pairs = _match_results(base_data.get('results', []), run_data.get('results', []))

    for level, label in [('fasil', 'Fasıl'), ('pozisyon', 'Pozisyon'), ('exact', 'Exact')]:
        fixed, broken, chronic, stable = [], [], [], []
        for base_item, run_item in pairs:
            t = _transition(base_item, run_item, level)
            entry = {
                'title':   base_item.get('title', '')[:55],
                'correct': base_item.get('correct_gtip', ''),
                'base_pred': base_item.get('predicted_gtip', ''),
                'run_pred':  run_item.get('predicted_gtip', '') if run_item else '(yok)',
            }
            if t == 'fixed':   fixed.append(entry)
            elif t == 'broken': broken.append(entry)
            elif t == 'chronic': chronic.append(entry)
            elif t == 'stable':  stable.append(entry)

        a(f"### {label} Geçiş Matrisi")
        a(f"")
        a(f"- ✅ Düzelen: {len(fixed)} | ❌ Bozulan: {len(broken)} | "
          f"🔴 Kronik: {len(chronic)} | ⚪ Sabit doğru: {len(stable)}")
        a(f"")

        if fixed:
            a(f"**✅ DÜZELEN ({len(fixed)} ürün):**")
            a(f"")
            for e in fixed:
                a(f"- `{e['title']}`")
                a(f"  - Doğru: `{e['correct']}` | Baseline: `{e['base_pred']}` → **Bu run: `{e['run_pred']}`**")
            a(f"")

        if broken:
            a(f"**❌ BOZULAN ({len(broken)} ürün):**")
            a(f"")
            for e in broken:
                a(f"- `{e['title']}`")
                a(f"  - Doğru: `{e['correct']}` | Baseline: `{e['base_pred']}` → **Bu run: `{e['run_pred']}`**")
            a(f"")

        if chronic:
            a(f"**🔴 KRONİK ({len(chronic)} ürün):**")
            a(f"")
            for e in chronic:
                a(f"- `{e['title']}`")
                a(f"  - Doğru: `{e['correct']}` | Baseline: `{e['base_pred']}` → Bu run: `{e['run_pred']}`")
            a(f"")

    # -----------------------------------------------------------------------
    # 4. Hata Analizi
    # -----------------------------------------------------------------------
    a(f"## 4. Hata Analizi")
    a(f"")
    a(f"> Her yanlış tahmin için kırılma noktası, candidate kontrol, DB tanımları, gerekçe özeti.")
    a(f"")

    # Sadece run'da yanlış olan ürünleri analiz et (exact False)
    wrong_pairs = [
        (b, r) for b, r in pairs
        if r is not None and _level_correct(r, 'exact') is False
    ]
    # Yoksa base'de yanlış olanları da ekle
    if not wrong_pairs:
        wrong_pairs = [
            (b, r) for b, r in pairs
            if _level_correct(b, 'exact') is False
        ]

    for i, (base_item, run_item) in enumerate(wrong_pairs, 1):
        ana = _analyze_error(conn, base_item, run_item, run_name)

        # Transition durumu
        trans_exact = _transition(base_item, run_item, 'exact') if run_item else 'unknown'
        trans_poz   = _transition(base_item, run_item, 'pozisyon') if run_item else 'unknown'
        trans_label = {
            'fixed':   '✅ Düzelen',
            'broken':  '❌ Bozulan',
            'chronic': '🔴 Kronik',
            'stable':  '⚪ Sabit',
            'unknown': '?',
        }

        a(f"### 4.{i} {ana['title'][:60]}")
        a(f"")
        a(f"| Alan | Değer |")
        a(f"|------|-------|")
        a(f"| Doğru GTİP | `{ana['correct']}` (Fasıl {ana['correct_fasil']}: {ana['correct_fasil_tanim']}) |")
        a(f"| Baseline tahmin | `{ana['base_pred']}` |")
        a(f"| Bu run tahmin | `{ana['run_pred']}` |")
        a(f"| Durum (exact) | {trans_label.get(trans_exact,'?')} |")
        a(f"| Durum (poz) | {trans_label.get(trans_poz,'?')} |")
        a(f"")

        # a) Kırılma noktası
        a(f"**a) Kırılma Noktası:** {ana['breakpoint']}")
        a(f"")

        # b) Candidate kontrolü
        a(f"**b) Candidate Kontrolü:**")
        a(f"")
        correct_fasil_str = str(ana['correct_fasil']).lstrip('0') or '0'
        run_fasil_strs = [str(x) for x in ana['run_fasiller']]
        # normalize for comparison: both as zero-padded 2 digit
        cf2 = ana['correct_fasil'].zfill(2) if ana['correct_fasil'] else ''
        run_fasil_padded = [str(x).zfill(2) for x in ana['run_fasiller']]
        base_fasil_padded = [str(x).zfill(2) for x in ana['base_fasiller']]

        if ana['run_fasiller']:
            in_run = cf2 in run_fasil_padded
            in_base = cf2 in base_fasil_padded
            bolum_rank = (ana['run_bolumler'].index(ana['correct_bolum']) + 1
                          if ana['correct_bolum'] in ana['run_bolumler'] else None)
            fasil_rank = (run_fasil_padded.index(cf2) + 1 if in_run else None)
            bolum_rank_str = (f"sıra {bolum_rank}/{len(ana['run_bolumler'])}"
                              if bolum_rank else "❌ yok")
            fasil_rank_str = (f"sıra {fasil_rank}/{len(run_fasil_padded)}"
                              if fasil_rank else "❌ yok")
            a(f"- Doğru bölüm: {ana['correct_bolum']} | "
              f"Run bölüm adayları: {ana['run_bolumler']} | "
              f"{'✅' if bolum_rank else '❌'} {bolum_rank_str}")
            a(f"- Doğru fasıl: {ana['correct_fasil']} | "
              f"Run fasıl adayları: {ana['run_fasiller']} | "
              f"{'✅' if fasil_rank else '❌'} {fasil_rank_str}")
            if in_run and not in_base:
                a(f"- ⚠️ Baseline'da yoktu, bu run'da var — ama yine de seçilmedi")
            elif in_run:
                a(f"- ⚠️ Fasıl candidate'da var ama seçilmedi → Adım 1/2 sorunu")
        else:
            in_base = cf2 in base_fasil_padded
            fasil_rank = (base_fasil_padded.index(cf2) + 1 if in_base else None)
            fasil_rank_str = (f"sıra {fasil_rank}/{len(base_fasil_padded)}"
                              if fasil_rank else "❌ yok")
            a(f"- Doğru fasıl: {ana['correct_fasil']} | "
              f"Base fasıl adayları: {ana['base_fasiller']} | "
              f"{'✅' if fasil_rank else '❌'} {fasil_rank_str}")
        a(f"")

        # c) DB tanımları
        a(f"**c) Pozisyon Tanımları (DB):**")
        a(f"")
        a(f"| | Kod | Tanım |")
        a(f"|-|-----|-------|")
        a(f"| Doğru | `{ana['correct_poz']}` | {ana['correct_poz_tanim'] or '(bulunamadı)'} |")
        a(f"| Tahmin | `{ana['pred_poz']}` | {ana['pred_poz_tanim'] or '(bulunamadı)'} |")
        a(f"")

        # d) Candidate diff (run vs baseline)
        if run_item and (ana['new_in_run'] or ana['gone_in_run']):
            a(f"**d) Fasıl Candidate Diff (baseline → bu run):**")
            a(f"")
            if ana['new_in_run']:
                new_with_names = [
                    f"{f} ({_get_fasil_tanim(conn, int(f)) if f.isdigit() else '?'})"
                    for f in ana['new_in_run']
                ]
                a(f"- ➕ Yeni giren fasıllar: {', '.join(new_with_names)}")
            if ana['gone_in_run']:
                gone_with_names = [
                    f"{f} ({_get_fasil_tanim(conn, int(f)) if f.isdigit() else '?'})"
                    for f in ana['gone_in_run']
                ]
                a(f"- ➖ Çıkan fasıllar: {', '.join(gone_with_names)}")
            a(f"")
        elif run_item:
            a(f"**d) Fasıl Candidate Diff:** Aynı (değişmedi)")
            a(f"")

        # e) Model gerekçesi — detay seviyesi transition'a göre
        if trans_exact == 'stable':
            pass  # sabit doğru: gerekçe yok
        elif trans_exact == 'fixed':
            # Düzelen: en alakalı 1 adım (pozisyon veya fasıl)
            a(f"**e) Model Gerekçesi:**")
            a(f"")
            best = ana['pozisyon_summary'] or ana['fasil_summary'] or ana['bolum_summary']
            a(f"> {best}")
            a(f"")
        else:
            # Bozulan / Kronik: tüm adımlar tam gerekçeyle
            a(f"**e) Model Gerekçesi (tüm adımlar):**")
            a(f"")
            if ana['bolum_summary'] and ana['bolum_summary'] != '(raw response yok)':
                a(f"**Adım 0a — Bölüm:**")
                a(f"> {ana['bolum_summary']}")
                a(f"")
            if ana['fasil_summary'] and ana['fasil_summary'] != '(raw response yok)':
                a(f"**Adım 0b — Fasıl:**")
                a(f"> {ana['fasil_summary']}")
                a(f"")
            if ana['pozisyon_summary'] and ana['pozisyon_summary'] != '(raw response yok)':
                a(f"**Adım 1 — Pozisyon:**")
                a(f"")
                a(f"Doğru pozisyon: {ana['correct_poz']}  \"{ana['correct_poz_tanim'] or '(bulunamadı)'}\"")
                a(f"")
                a(f"Model seçti: {ana['pred_poz']}  \"{ana['pred_poz_tanim'] or '(boş)'}\"")
                a(f"")
                a(f"> {ana['pozisyon_summary']}")
                a(f"")
            if ana['gtip_summary'] and ana['gtip_summary'] != '(raw response yok)':
                a(f"**Adım 2 — GTİP:**")
                a(f"> {ana['gtip_summary']}")
                a(f"")

        # f) Bozulma analizi — sadece BOZULAN ürünler için
        if trans_exact == 'broken' and run_item:
            noise = _broken_noise_analysis(conn, base_item, run_item, base_cfg, run_cfg)
            a(f"**f) Bozulma Analizi:**")
            a(f"")
            a(f"- Config farkı: {noise.get('bolum_delta','')} | {noise.get('fasil_delta','')}")
            a(f"- Baseline fasıl adayları: `{noise.get('base_fasiller', [])}`")
            a(f"- Bu run fasıl adayları:   `{noise.get('run_fasiller', [])}`")
            if noise.get('new_fasils'):
                new_with_names = [
                    f"{f} ({_get_fasil_tanim_str(f)})" for f in noise['new_fasils']
                ]
                a(f"- Yeni giren fasıllar: {', '.join(new_with_names)}")
            if noise.get('new_bolums'):
                a(f"- Yeni giren bölümler: {noise['new_bolums']}")
            a(f"- **Sonuç: {noise.get('verdict', '?')}**")
            a(f"")

        a(f"---")
        a(f"")

    # -----------------------------------------------------------------------
    # 5. Kronik Sorun Grupları
    # -----------------------------------------------------------------------
    a(f"## 5. Kronik Sorun Grupları")
    a(f"")

    chronic_pairs = [
        (b, r) for b, r in pairs
        if _transition(b, r, 'exact') == 'chronic'
    ]

    if not chronic_pairs:
        a(f"Kronik yanlış yok (veya run eşleştirilemedi).")
        a(f"")
    else:
        # Gruplama
        no_candidate  = []  # doğru fasıl candidate'a hiç girmemiş
        has_candidate = []  # candidate'da var ama seçilmemiş
        wrong_gtip    = []  # pozisyon doğru ama GTİP yanlış

        for base_item, run_item in chronic_pairs:
            ana = _analyze_error(conn, base_item, run_item, run_name)
            dbg_check = run_item.get('debug') or {} if run_item else base_item.get('debug') or {}
            fasiller_check = dbg_check.get('candidate_fasiller') or []
            cf2 = ana['correct_fasil'].zfill(2)
            fasil_padded = [str(x).zfill(2) for x in fasiller_check]

            if cf2 not in fasil_padded:
                no_candidate.append(ana)
            elif ana['breakpoint'].startswith('GTİP'):
                wrong_gtip.append(ana)
            else:
                has_candidate.append(ana)

        if no_candidate:
            a(f"### 5.1 Candidate'a Giremiyor ({len(no_candidate)} ürün)")
            a(f"Doğru fasıl/bölüm hiç seçilmemiş — Adım 0a/0b sorunu.")
            a(f"")
            for ana in no_candidate:
                a(f"- **{ana['title'][:55]}**")
                a(f"  - Doğru: Fasıl {ana['correct_fasil']} ({ana['correct_fasil_tanim']}), "
                  f"Bölüm {ana['correct_bolum']}")
                a(f"  - Candidate fasıllar: {ana['run_fasiller'] or ana['base_fasiller']}")
                a(f"  - Model bölüm gerekçesi: *{ana['bolum_summary']}*")
            a(f"")

        if has_candidate:
            a(f"### 5.2 Candidate'da Var Ama Seçilemiyor ({len(has_candidate)} ürün)")
            a(f"Gürültü sorunu — doğru fasıl listede ama model başka birine kayıyor.")
            a(f"")
            for ana in has_candidate:
                a(f"- **{ana['title'][:55]}**")
                a(f"  - Doğru fasıl {ana['correct_fasil']} listede, ama seçilen: "
                  f"`{ana['run_pred']}` (fasıl {_fasil(ana['run_pred'])})")
                a(f"  - Fasıl gerekçesi: *{ana['fasil_summary']}*")
            a(f"")

        if wrong_gtip:
            a(f"### 5.3 Pozisyon Doğru, GTİP Yanlış ({len(wrong_gtip)} ürün)")
            a(f"Adım 2 sorunu — 12'li seçimde hata.")
            a(f"")
            for ana in wrong_gtip:
                a(f"- **{ana['title'][:55]}**")
                a(f"  - Doğru: `{ana['correct']}`, Tahmin: `{ana['run_pred']}`")
            a(f"")

        # Kelime tetikleme analizi
        a(f"### 5.4 Anahtar Kelime / Pattern Analizi")
        a(f"")
        # Basit: başlıktaki kelimeleri yanlış fasıl ile ilişkilendir
        triggers = {}
        for ana in no_candidate + has_candidate:
            wrong_fasil = _fasil(ana['run_pred'])
            title_words = re.findall(r'[a-zA-ZğüşıöçĞÜŞİÖÇ]{4,}',
                                     ana['title'].lower())
            for w in title_words[:6]:
                key = f"{w} → fasıl {wrong_fasil}"
                triggers[key] = triggers.get(key, 0) + 1
        # En sık pattern
        sorted_triggers = sorted(triggers.items(), key=lambda x: -x[1])[:10]
        if sorted_triggers:
            a(f"En sık tetikleme patternleri (başlık kelimesi → yanlış fasıl):")
            a(f"")
            for pattern, cnt in sorted_triggers:
                a(f"- `{pattern}` ({cnt}x)")
            a(f"")

    # -----------------------------------------------------------------------
    # 6. Token Karşılaştırması
    # -----------------------------------------------------------------------
    a(f"## 6. Token Karşılaştırması")
    a(f"")
    a(f"| Adım | Baseline ort. | Bu run ort. | Fark |")
    a(f"|------|---------------|-------------|------|")
    base_results = base_data.get('results', [])
    run_results  = run_data.get('results', [])
    for step, label in [('adim_0a','Adım 0a (bölüm)'), ('adim_0b','Adım 0b (fasıl)'),
                        ('adim_1','Adım 1 (pozisyon)'), ('adim_2','Adım 2 (GTİP)'),
                        ('toplam','Toplam')]:
        bt = _avg_tokens(base_results, step)
        rt = _avg_tokens(run_results, step)
        diff = rt - bt
        a(f"| {label} | {bt:,} | {rt:,} | {diff:+,} |")
    a(f"")

    # -----------------------------------------------------------------------
    # 7. Kod/Prompt Değişiklikleri
    # -----------------------------------------------------------------------
    a(f"## 7. Kod/Prompt Değişiklikleri")
    a(f"")

    diff_text = _run_git_diff(n_commits=1, files=['scripts/gtip_matcher.py', 'CLAUDE.md'])
    if not diff_text:
        a(f"*git diff çalıştırılamadı veya değişiklik yok.*")
        a(f"")
    else:
        changes = _parse_prompt_diff(diff_text)

        # Değişen prompt değişkenleri
        prompt_changes = [c for c in changes if c['type'] == 'prompt']
        config_changes = [c for c in changes if c['type'] == 'config']

        if prompt_changes:
            a(f"### 7.1 Prompt Değişiklikleri")
            a(f"")
            for ch in prompt_changes:
                a(f"**`{ch['variable']}`**")
                if ch['removed']:
                    for line in ch['removed']:
                        a(f"  - ~~`{line}`~~")
                if ch['added']:
                    for line in ch['added']:
                        a(f"  + `{line}`")
                a(f"")
        else:
            a(f"### 7.1 Prompt Değişiklikleri")
            a(f"")
            a(f"*Prompt değişkeni değişikliği tespit edilmedi.*")
            a(f"")

        if config_changes:
            a(f"### 7.2 Config / Parametre Değişiklikleri")
            a(f"")
            for ch in config_changes:
                for line in ch['added']:
                    a(f"- `{ch['variable']}`: `{line}`")
            a(f"")

        # Bozulan ürünlerle eşleştirme — spesifik neden
        broken_items = [
            (b, r) for b, r in pairs
            if _transition(b, r, 'exact') == 'broken' and r is not None
        ]
        if broken_items:
            a(f"### 7.3 Değişiklik → Bozulma Eşleştirmesi")
            a(f"")
            a(f"| Bozulan Ürün | Spesifik Neden |")
            a(f"|--------------|----------------|")
            for b_item, r_item in broken_items:
                title_short = b_item.get('title', '')[:45]
                noise = _broken_noise_analysis(conn, b_item, r_item, base_cfg, run_cfg)
                run_pred = r_item.get('predicted_gtip', '') or ''
                correct  = b_item.get('correct_gtip', '') or ''

                # B2 revert: 3925 → 3919 geçişi (yapışkanlı fitil sorunu)
                if (_pozisyon(correct)[:4] == '3925' and
                        _pozisyon(run_pred)[:4] in ('3919', '3920')):
                    reason = 'B2 revert: 3925 koruması kaldırıldı → 3919\'a kaydı'

                # Gürültü: yeni giren fasıl seçilen yanlış tahminin fasılı
                elif noise.get('new_fasils'):
                    pred_fasil = _fasil(run_pred).zfill(2)
                    triggered = [f for f in noise['new_fasils'] if f == pred_fasil]
                    base_count = run_cfg.get('fasil_aday', '?') != base_cfg.get('fasil_aday', '?')
                    old_n = base_cfg.get('fasil_aday', '?')
                    new_n = run_cfg.get('fasil_aday', '?')
                    if triggered:
                        tanim = _get_fasil_tanim_str(triggered[0])[:40]
                        reason = (f'gürültü: fasıl aday artışı ({old_n}→{new_n}) '
                                  f'fasıl {triggered[0]} ({tanim}) soktu → model oraya kaçtı')
                    else:
                        # Yeni fasıl girdi ama tahmin başka — dolaylı gürültü
                        new_str = ', '.join(
                            f"{f}({_get_fasil_tanim_str(f)[:20]})"
                            for f in noise['new_fasils'][:3]
                        )
                        reason = (f'dolaylı gürültü: yeni giren fasıllar [{new_str}] '
                                  f'listeyi bozdu — pred fasıl {pred_fasil} ({old_n}→{new_n})')
                else:
                    # Candidate aynı — nondeterminizm veya prompt değişikliği
                    changed_prompts = [c['variable'] for c in prompt_changes]
                    if changed_prompts:
                        reason = f"prompt değişikliği ({', '.join(changed_prompts[:2])}) / nondeterminizm"
                    else:
                        reason = 'nondeterminizm (candidate aynı, prompt değişmedi)'

                a(f"| {title_short} | {reason} |")
            a(f"")

        # Raw diff özeti (ilk 60 satır, prompt bloklarından)
        a(f"### 7.4 Raw Diff (gtip_matcher.py — ilk 60 satır)")
        a(f"")
        a(f"```diff")
        diff_lines = [l for l in diff_text.splitlines()
                      if not l.startswith('diff ') and not l.startswith('index ')]
        a('\n'.join(diff_lines[:60]))
        a(f"```")
        a(f"")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Terminal özeti
# ---------------------------------------------------------------------------

def print_summary(base_data, run_data, base_path, run_path, pairs):
    base_name = os.path.basename(base_path)
    run_name  = os.path.basename(run_path)

    base_m = base_data.get('metrics', {})
    run_m  = run_data.get('metrics', {})

    print(f"\n{'='*60}")
    print(f"KARŞILAŞTIRMA: {run_name}")
    print(f"BASELINE:      {base_name}")
    print(f"{'='*60}")
    print(f"{'Metrik':<20} {'Baseline':>10} {'Bu run':>10} {'Fark':>8}")
    print(f"{'-'*50}")
    for level, label in [('fasil','Fasıl (2h)'), ('pozisyon','Pozisyon (4h)'),
                          ('alt_poz','Alt poz (6h)'), ('exact','Exact (12h)')]:
        ba = base_m.get(level, {}).get('accuracy', 0)
        ra = run_m.get(level, {}).get('accuracy', 0)
        diff = ra - ba
        arrow = '⬆' if diff > 0.5 else ('⬇' if diff < -0.5 else '—')
        print(f"  {label:<18} {ba:>9.1f}% {ra:>9.1f}% {arrow} {diff:>+.1f}pp")
    print(f"{'='*60}")

    # Geçiş sayıları (exact)
    fixed = sum(1 for b, r in pairs if _transition(b, r, 'exact') == 'fixed')
    broken = sum(1 for b, r in pairs if _transition(b, r, 'exact') == 'broken')
    chronic = sum(1 for b, r in pairs if _transition(b, r, 'exact') == 'chronic')
    stable = sum(1 for b, r in pairs if _transition(b, r, 'exact') == 'stable')

    print(f"\nExact geçiş: ✅ Düzelen={fixed}  ❌ Bozulan={broken}  "
          f"🔴 Kronik={chronic}  ⚪ Sabit doğru={stable}")
    if broken > 0:
        print(f"\n  ❌ Bozulan ürünler:")
        for b, r in pairs:
            if _transition(b, r, 'exact') == 'broken':
                print(f"     {b.get('title','')[:50]}")
                print(f"       {b.get('correct_gtip','')}  |  baseline: {b.get('predicted_gtip','')}  →  bu run: {r.get('predicted_gtip','')}")
    if fixed > 0:
        print(f"\n  ✅ Düzelen ürünler:")
        for b, r in pairs:
            if _transition(b, r, 'exact') == 'fixed':
                print(f"     {b.get('title','')[:50]}")
                print(f"       {b.get('correct_gtip','')}  |  baseline: {b.get('predicted_gtip','')}  →  bu run: {r.get('predicted_gtip','')}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='GTİP eval run karşılaştırıcı (offline)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('run',          help='Karşılaştırılacak run JSON yolu')
    parser.add_argument('--baseline',   default=None,
                        help='Baseline JSON (belirtilmezse experiments/ altında en iyi exact\'i seçer)')
    parser.add_argument('--db',         default='data/gtip_2026.db')
    parser.add_argument('--out',        default=None,
                        help='Rapor çıktı yolu (varsayılan: experiments/report_<run_ts>.md)')
    args = parser.parse_args()

    if not os.path.exists(args.run):
        print(f"Hata: run dosyası bulunamadı: {args.run}")
        sys.exit(1)

    if not os.path.exists(args.db):
        print(f"Hata: DB bulunamadı: {args.db}")
        sys.exit(1)

    # Baseline seç
    if args.baseline:
        baseline_path = args.baseline
    else:
        exp_dir = os.path.dirname(args.run) or 'experiments'
        baseline_path = _auto_baseline(exp_dir)
        if not baseline_path:
            print("Hata: baseline bulunamadı. --baseline ile belirtin.")
            sys.exit(1)
        print(f"Auto-baseline: {baseline_path}")

    if not os.path.exists(baseline_path):
        print(f"Hata: baseline dosyası bulunamadı: {baseline_path}")
        sys.exit(1)

    # Aynı dosyaysa uyar
    if os.path.abspath(args.run) == os.path.abspath(baseline_path):
        print("Uyarı: run ve baseline aynı dosya. Self-comparison yapılıyor.")

    print(f"Yükleniyor: {args.run}")
    run_data  = _load_json(args.run)
    print(f"Yükleniyor: {baseline_path}")
    base_data = _load_json(baseline_path)

    conn = sqlite3.connect(args.db)

    # Eşleştir
    pairs = _match_results(base_data.get('results', []), run_data.get('results', []))
    matched = sum(1 for _, r in pairs if r is not None)
    print(f"Eşleştirilen ürün: {matched}/{len(pairs)}")

    # Terminal özet
    print_summary(base_data, run_data, baseline_path, args.run, pairs)

    # Rapor oluştur
    print("Rapor oluşturuluyor...")
    report = build_report(base_data, run_data, baseline_path, args.run, conn)

    # Çıktı yolu
    if args.out:
        out_path = args.out
    else:
        run_ts = os.path.basename(args.run).replace('run_', '').replace('.json', '')
        exp_dir = os.path.dirname(args.run) or 'experiments'
        out_path = os.path.join(exp_dir, f'report_{run_ts}.md')

    os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"Rapor kaydedildi: {out_path}")
    conn.close()


if __name__ == '__main__':
    main()
