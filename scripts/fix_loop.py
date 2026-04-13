#!/usr/bin/env python3
"""
fix_loop.py — Kronik GTİP hatalarını izole edip hızlı deney→değerlendirme döngüsü.

Subcommands:
  show-chronic   --baseline <run.json>
      Baseline'da exact=False olan ürünleri ve gold set indekslerini göster.
      revert_log.json geçmişini de listeler.

  quick-test     --baseline <run.json>
      Kronik ürünleri --items ile eval et, baseline'a kıyasla karşılaştır.

  full-eval      --baseline <run.json>
      30 ürün full eval + analyze_run.py rapor + regresyon kontrolü.

  revert         --reason "..."
      git restore scripts/gtip_matcher.py + revert_log.json'a kaydet.

  test-cycle     --baseline <run.json>
      quick-test → iyiyse full-eval+analyze → regresyon varsa auto-revert.
"""

import sys
import os
import json
import subprocess
import argparse
from datetime import datetime

# analyze_run modülünden gerekli fonksiyonları import et
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_run import _match_results, _transition, _level_correct, _load_json

# ---------------------------------------------------------------------------
# Sabitler (proje köküne göre)
# ---------------------------------------------------------------------------
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
REVERT_LOG   = os.path.join(_ROOT, 'experiments', 'revert_log.json')
GOLD_SET     = os.path.join(_ROOT, 'data', 'gold_set_30.xlsx')
DB           = os.path.join(_ROOT, 'data', 'gtip_2026.db')
EVAL_SCRIPT  = os.path.join(_ROOT, 'scripts', 'eval_gtip.py')
ANALYZE_SCRIPT = os.path.join(_ROOT, 'scripts', 'analyze_run.py')
MATCHER_FILE = os.path.join(_ROOT, 'scripts', 'gtip_matcher.py')
EXPERIMENTS_DIR = os.path.join(_ROOT, 'experiments')
OUTPUT_DIR   = os.path.join(_ROOT, 'output')


# ---------------------------------------------------------------------------
# revert_log yardımcıları
# ---------------------------------------------------------------------------

def _load_revert_log():
    if not os.path.exists(REVERT_LOG):
        return []
    with open(REVERT_LOG, encoding='utf-8') as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get('entries', [])


def _save_revert_log(entries):
    os.makedirs(EXPERIMENTS_DIR, exist_ok=True)
    with open(REVERT_LOG, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _already_tried(diff):
    """
    Mevcut diff daha önce denenerek revert edilmiş mi?
    Boş diff veya eşleşme yoksa None döner.
    """
    if not diff or not diff.strip():
        return None
    for entry in _load_revert_log():
        if entry.get('reverted') and entry.get('git_diff', '').strip() == diff.strip():
            return entry
    return None


# ---------------------------------------------------------------------------
# Git yardımcıları
# ---------------------------------------------------------------------------

def _get_git_diff():
    """scripts/gtip_matcher.py için HEAD'e göre mevcut diff (staged+unstaged)."""
    try:
        result = subprocess.run(
            ['git', 'diff', 'HEAD', '--', 'scripts/gtip_matcher.py'],
            capture_output=True, text=True, timeout=10, cwd=_ROOT,
        )
        return result.stdout
    except Exception:
        return ''


def _do_git_restore():
    """git restore scripts/gtip_matcher.py — True döner başarıysa."""
    result = subprocess.run(
        ['git', 'restore', 'scripts/gtip_matcher.py'],
        capture_output=True, text=True, cwd=_ROOT,
    )
    return result.returncode == 0, result.stderr.strip()


# ---------------------------------------------------------------------------
# Kronik ürün tespiti
# ---------------------------------------------------------------------------

def _get_chronic_items(baseline_data):
    """
    Baseline run'ında pozisyon=False olan tüm ürünleri döner.
    (fasıl yanlışsa pozisyon da zaten False olur.)
    Returns: list of (1_based_gold_index, result_dict)
    Full run (n_total==len(results)) varsayımı: results[i] → gold index i+1.
    """
    results = baseline_data.get('results', [])
    chronic = []
    for i, r in enumerate(results, 1):
        if _level_correct(r, 'pozisyon') is False:
            chronic.append((i, r))
    return chronic


# ---------------------------------------------------------------------------
# Eval çalıştırma
# ---------------------------------------------------------------------------

def _run_eval(items_str=None):
    """
    eval_gtip.py çalıştır (--delay 0, note/izahname 0).
    Subprocess output doğrudan terminale akar.
    Returns: yeni oluşturulan run JSON yolu veya None.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(EXPERIMENTS_DIR, exist_ok=True)

    # Başlamadan önce mevcut run JSON'larını kaydet
    before = set(
        f for f in os.listdir(EXPERIMENTS_DIR)
        if f.startswith('run_') and f.endswith('.json')
    )

    cmd = [
        sys.executable, EVAL_SCRIPT,
        GOLD_SET,
        '--db', DB,
        '--delay', '0',
        '--note-chars', '0',
        '--izahname-chars', '0',
    ]
    if items_str:
        cmd += ['--items', items_str]

    print(f"  $ {' '.join(os.path.basename(c) if c == EVAL_SCRIPT else c for c in cmd)}")
    print()

    ret = subprocess.run(cmd, cwd=_ROOT)

    if ret.returncode != 0:
        print(f"\n  [HATA] eval_gtip.py başarısız (return code {ret.returncode})")
        return None

    # Yeni oluşturulan run JSON'unu bul
    after = set(
        f for f in os.listdir(EXPERIMENTS_DIR)
        if f.startswith('run_') and f.endswith('.json')
    )
    new_files = sorted(after - before)
    if not new_files:
        # Fallback: en son değiştirilen
        all_runs = sorted(
            [f for f in os.listdir(EXPERIMENTS_DIR) if f.startswith('run_') and f.endswith('.json')],
            key=lambda f: os.path.getmtime(os.path.join(EXPERIMENTS_DIR, f)),
            reverse=True,
        )
        if all_runs:
            return os.path.join(EXPERIMENTS_DIR, all_runs[0])
        return None

    return os.path.join(EXPERIMENTS_DIR, new_files[-1])


def _run_analyze(run_path, baseline_path):
    """
    analyze_run.py çalıştır. Subprocess output terminale akar.
    Returns: rapor .md yolu veya None.
    """
    cmd = [
        sys.executable, ANALYZE_SCRIPT,
        run_path,
        '--baseline', baseline_path,
        '--db', DB,
    ]
    print(f"  $ {' '.join(os.path.basename(c) if c in (ANALYZE_SCRIPT,) else c for c in cmd)}")
    print()

    ret = subprocess.run(cmd, cwd=_ROOT)

    if ret.returncode != 0:
        print(f"\n  [HATA] analyze_run.py başarısız (return code {ret.returncode})")
        return None

    run_ts = os.path.basename(run_path).replace('run_', '').replace('.json', '')
    report_path = os.path.join(EXPERIMENTS_DIR, f'report_{run_ts}.md')
    return report_path if os.path.exists(report_path) else None


# ---------------------------------------------------------------------------
# Metrik karşılaştırma
# ---------------------------------------------------------------------------

def _print_comparison(baseline_data, run_data):
    base_m = baseline_data.get('metrics', {})
    run_m  = run_data.get('metrics', {})
    print(f"\n  {'Metrik':<20} {'Baseline':>10} {'Bu Run':>10} {'Fark':>8}")
    print(f"  {'-'*52}")
    for level, label in [('fasil', 'Fasıl (2h)'), ('pozisyon', 'Pozisyon (4h)'),
                          ('alt_poz', 'Alt poz (6h)'), ('exact', 'Exact (12h)')]:
        ba = base_m.get(level, {}).get('accuracy', 0)
        ra = run_m.get(level, {}).get('accuracy', 0)
        diff = ra - ba
        arrow = '⬆' if diff > 0.5 else ('⬇' if diff < -0.5 else '—')
        print(f"  {label:<20} {ba:>9.1f}% {ra:>9.1f}% {arrow} {diff:>+.1f}pp")
    print()


def _check_regression(baseline_data, run_data):
    """
    Regresyon var mı? Pozisyon seviyesinde ölçülür.
    - Baseline'da pozisyon doğru olan herhangi bir ürün bozulduysa → regresyon
    - Pozisyon accuracy 3pp'den fazla düştüyse → regresyon
    Returns: (is_regression: bool, reason: str)
    """
    pairs = _match_results(
        baseline_data.get('results', []),
        run_data.get('results', []),
    )
    broken = [(b, r) for b, r in pairs if _transition(b, r, 'pozisyon') == 'broken']

    base_poz = baseline_data.get('metrics', {}).get('pozisyon', {}).get('accuracy', 0)
    run_poz  = run_data.get('metrics', {}).get('pozisyon', {}).get('accuracy', 0)
    drop     = base_poz - run_poz

    if broken:
        titles = [b.get('title', '')[:45] for b, _ in broken[:3]]
        suffix = '...' if len(broken) > 3 else ''
        return True, f"{len(broken)} pozisyon bozuldu: {titles}{suffix}"

    if drop > 3.0:
        return True, f"Pozisyon accuracy {drop:.1f}pp düştü ({base_poz:.1f}% → {run_poz:.1f}%)"

    return False, ""


# ---------------------------------------------------------------------------
# show-chronic
# ---------------------------------------------------------------------------

def cmd_show_chronic(args):
    baseline_data = _load_json(args.baseline)
    chronic = _get_chronic_items(baseline_data)

    m = baseline_data.get('metrics', {})
    print(f"\nBaseline : {args.baseline}")
    print(f"Tarih    : {baseline_data.get('timestamp','?')[:16]}")
    print(f"Fasıl    : {m.get('fasil',{}).get('accuracy',0):.1f}%  "
          f"Pozisyon: {m.get('pozisyon',{}).get('accuracy',0):.1f}%  "
          f"Exact: {m.get('exact',{}).get('accuracy',0):.1f}%")
    print()

    if not chronic:
        print("Kronik ürün yok — baseline'da tüm ürünler exact doğru.")
    else:
        indices = [str(idx) for idx, _ in chronic]
        print(f"Kronik yanlış ürünler ({len(chronic)} adet, exact=False):\n")
        for idx, r in chronic:
            title     = (r.get('title') or '')[:65]
            correct   = r.get('correct_gtip', '')
            predicted = r.get('predicted_gtip', '') or '(boş)'
            guven     = r.get('guven', '?')
            m_r       = r.get('metrics', {})
            fasil_sym = '✓' if m_r.get('fasil')    else '✗'
            poz_sym   = '✓' if m_r.get('pozisyon') else '✗'
            print(f"  [{idx:2d}] {title}")
            print(f"       Doğru: {correct}  |  Tahmin: {predicted}  "
                  f"[fasıl:{fasil_sym} poz:{poz_sym}] [{guven}]")
        print(f"\n  --items: {','.join(indices)}")

    # Mevcut diff daha önce denendi mi?
    diff = _get_git_diff()
    prev = _already_tried(diff)
    if prev:
        print(f"\n⚠️  UYARI: Mevcut diff daha önce denendi ve revert edildi!")
        print(f"   Tarih   : {prev.get('timestamp','?')[:16]}")
        print(f"   Gerekçe : {prev.get('reason','?')}")

    # revert_log geçmişi
    log = _load_revert_log()
    print(f"\n{'='*55}")
    if not log:
        print("revert_log.json: henüz giriş yok.")
    else:
        print(f"Geçmiş denemeler ({len(log)} giriş):\n")
        for entry in log:
            ts     = entry.get('timestamp', '?')[:16]
            reason = entry.get('reason', '?')
            run_m  = entry.get('run_metrics', {})
            exact  = run_m.get('exact', {}).get('accuracy', '?')
            report = entry.get('report_path', '') or ''
            print(f"  [{ts}] {reason}")
            print(f"    Exact: {exact}%  |  Rapor: {os.path.basename(report) if report else '—'}")
            if entry.get('reverted'):
                print(f"    → REVERT EDİLDİ")
            print()


# ---------------------------------------------------------------------------
# quick-test
# ---------------------------------------------------------------------------

def _do_quick_test(baseline_data):
    """
    Kronik ürünleri eval et.
    Returns: (run_path, run_data, fixed_count, total_chronic)
    """
    chronic = _get_chronic_items(baseline_data)
    if not chronic:
        print("Kronik ürün yok — quick-test atlanıyor.")
        return None, None, 0, 0

    indices = [str(idx) for idx, _ in chronic]
    items_str = ','.join(indices)
    total = len(chronic)

    print(f"  Kronik ürünler ({total} adet) — indeksler: {items_str}")
    run_path = _run_eval(items_str=items_str)
    if not run_path:
        return None, None, 0, total

    run_data    = _load_json(run_path)
    run_results = run_data.get('results', [])
    fixed       = sum(1 for r in run_results if _level_correct(r, 'pozisyon') is True)
    fasil_ok    = sum(1 for r in run_results if _level_correct(r, 'fasil') is True)
    exact_ok    = sum(1 for r in run_results if _level_correct(r, 'exact') is True)

    n = len(run_results)
    print(f"\n  Quick-test sonuçları ({n} ürün, baseline'da tümü pozisyon yanlış):")
    print(f"    Fasıl    : {fasil_ok}/{n}  ({fasil_ok/n*100:.1f}%)")
    print(f"    Pozisyon : {fixed}/{n}  ({fixed/n*100:.1f}%)  ← baseline: 0%")
    print(f"    Exact    : {exact_ok}/{n}  ({exact_ok/n*100:.1f}%)")

    if fixed:
        print(f"\n  ✅ {fixed} kronik ürün pozisyon düzeldi:")
        for r in run_results:
            if _level_correct(r, 'pozisyon') is True:
                ex_sym = '✓' if _level_correct(r, 'exact') else '~'
                print(f"     {ex_sym} {r.get('title','')[:60]}")
                print(f"       doğru: {r.get('correct_gtip','')}  tahmin: {r.get('predicted_gtip','')}")

    still_wrong = [r for r in run_results if _level_correct(r, 'pozisyon') is not True]
    if still_wrong:
        print(f"\n  Hâlâ pozisyon yanlış ({len(still_wrong)}):")
        for r in still_wrong:
            m = r.get('metrics', {})
            fasil_sym = '✓' if m.get('fasil') else '✗'
            print(f"     ✗ {(r.get('title') or '')[:55]}  →  "
                  f"{r.get('predicted_gtip','') or '(boş)'}  [fasıl:{fasil_sym}]")

    print(f"\n  Run JSON: {run_path}")
    return run_path, run_data, fixed, total


def cmd_quick_test(args):
    baseline_data = _load_json(args.baseline)
    print(f"\nBaseline: {args.baseline}")

    diff = _get_git_diff()
    prev = _already_tried(diff)
    if prev:
        print(f"\n⚠️  Bu diff daha önce denendi ve revert edildi ({prev.get('timestamp','?')[:16]}). "
              f"Devam etmek için manuel onay gerekiyor.")
        print(f"   Gerekçe: {prev.get('reason','?')}")
        print()

    print()
    _do_quick_test(baseline_data)


# ---------------------------------------------------------------------------
# full-eval
# ---------------------------------------------------------------------------

def _do_full_eval(baseline_data, baseline_path):
    """
    30 ürün full eval → analyze → regresyon kontrolü.
    Returns: (run_path, report_path, is_regression, reason)
    """
    print("  Full eval çalıştırılıyor (30 ürün)...")
    run_path = _run_eval()
    if not run_path:
        return None, None, False, "eval başarısız"

    run_data = _load_json(run_path)

    print(f"\n  Analiz çalıştırılıyor...")
    report_path = _run_analyze(run_path, baseline_path)

    _print_comparison(baseline_data, run_data)

    is_regression, reason = _check_regression(baseline_data, run_data)
    return run_path, report_path, is_regression, reason


def cmd_full_eval(args):
    baseline_data = _load_json(args.baseline)
    print(f"\nBaseline: {args.baseline}")

    diff = _get_git_diff()
    prev = _already_tried(diff)
    if prev:
        print(f"\n⚠️  Bu diff daha önce denendi ve revert edildi ({prev.get('timestamp','?')[:16]}). "
              f"Devam etmek için manuel onay gerekiyor.")
        print(f"   Gerekçe: {prev.get('reason','?')}")
        print()

    print()
    run_path, report_path, is_regression, reason = _do_full_eval(baseline_data, args.baseline)

    if is_regression:
        print(f"❌ REGRESYON: {reason}")
    else:
        print(f"✅ Regresyon yok.")

    if report_path:
        print(f"Rapor: {report_path}")
    if run_path:
        print(f"Run  : {run_path}")


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------

def _do_revert(reason, run_path=None, report_path=None,
               run_data=None, baseline_data=None):
    """
    git restore + revert_log'a kaydet.
    Returns: True başarıysa.
    """
    diff = _get_git_diff()
    log  = _load_revert_log()

    entry = {
        'timestamp':  datetime.now().isoformat(),
        'reason':     reason,
        'git_diff':   diff,
        'run_path':   run_path or '',
        'report_path': report_path or '',
        'reverted':   True,
    }
    if run_data:
        entry['run_metrics'] = run_data.get('metrics', {})
    if baseline_data:
        entry['baseline_metrics'] = baseline_data.get('metrics', {})

    log.append(entry)
    _save_revert_log(log)

    ok, err = _do_git_restore()
    if ok:
        print(f"  ✅ git restore scripts/gtip_matcher.py — başarılı")
    else:
        print(f"  ❌ git restore başarısız: {err}")
        return False

    print(f"  revert_log.json güncellendi ({len(log)} giriş toplam)")
    if report_path:
        print(f"  Rapor: {report_path}")
    return True


def cmd_revert(args):
    print(f"\nRevert gerekçesi: {args.reason}")
    _do_revert(args.reason)


# ---------------------------------------------------------------------------
# test-cycle
# ---------------------------------------------------------------------------

def cmd_test_cycle(args):
    print(f"\n{'='*60}")
    print(f"TEST CYCLE")
    print(f"Baseline: {args.baseline}")
    print(f"{'='*60}\n")

    baseline_data = _load_json(args.baseline)

    # Daha önce denenen diff kontrolü
    diff = _get_git_diff()
    prev = _already_tried(diff)
    if prev:
        print(f"⚠️  Bu diff daha önce denendi ve revert edildi ({prev.get('timestamp','?')[:16]}).")
        print(f"   Gerekçe: {prev.get('reason','?')}")
        print(f"   Aynı değişikliği tekrar denemek için revert_log.json'dan ilgili girişi kaldırın.")
        return

    if not diff or not diff.strip():
        print("⚠️  scripts/gtip_matcher.py'da HEAD'e göre değişiklik yok. Test cycle anlamlı değil.")
        return

    # ------------------------------------------------------------------ [1/3]
    print(f"[1/3] Quick-test (kronik ürünler)...")
    print()
    run_path, _, fixed, total_chronic = _do_quick_test(baseline_data)

    if run_path is None and total_chronic == 0:
        print("Kronik ürün yok — test cycle sonlandı.")
        return

    if run_path is None:
        print("Quick-test başarısız — durduruluyor.")
        return

    if fixed == 0:
        print(f"\n✗ Hiç iyileşme yok ({total_chronic} kronik üründe pozisyon=0). Full eval atlanıyor.")
        return

    print(f"\n✓ {fixed}/{total_chronic} kronik ürün düzeldi — full eval başlatılıyor...")

    # ------------------------------------------------------------------ [2/3]
    print(f"\n[2/3] Full eval (30 ürün)...")
    print()
    full_run_path, report_path, is_regression, reason = _do_full_eval(
        baseline_data, args.baseline
    )

    if full_run_path is None:
        print("Full eval başarısız — durduruluyor.")
        return

    full_run_data = _load_json(full_run_path)

    # ------------------------------------------------------------------ [3/3]
    print(f"[3/3] Regresyon kontrolü...")
    if is_regression:
        print(f"\n❌ REGRESYON: {reason}")
        print(f"   Auto-revert yapılıyor...")
        _do_revert(
            reason=f"auto-revert: {reason}",
            run_path=full_run_path,
            report_path=report_path,
            run_data=full_run_data,
            baseline_data=baseline_data,
        )
    else:
        base_poz = baseline_data.get('metrics', {}).get('pozisyon', {}).get('accuracy', 0)
        run_poz  = full_run_data.get('metrics', {}).get('pozisyon', {}).get('accuracy', 0)
        diff_pp  = run_poz - base_poz

        if diff_pp > 0:
            print(f"\n✅ İyileşme: pozisyon {diff_pp:+.1f}pp  ({base_poz:.1f}% → {run_poz:.1f}%)")
        else:
            print(f"\n— Regresyon yok, kronik ürünlerde iyileşme ama genel pozisyon değişmedi "
                  f"({diff_pp:+.1f}pp).")

        if report_path:
            print(f"   Rapor  : {report_path}")
        if full_run_path:
            print(f"   Run    : {full_run_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='GTİP fix_loop — kronik hata izolasyon ve test döngüsü',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest='command', metavar='<subcommand>')

    p_show = sub.add_parser('show-chronic', help='Baseline kronik ürünleri listele')
    p_show.add_argument('--baseline', required=True, help='Baseline run JSON yolu')

    p_quick = sub.add_parser('quick-test', help='Kronik ürünleri hızlı test et')
    p_quick.add_argument('--baseline', required=True)

    p_full = sub.add_parser('full-eval', help='30 ürün full eval + analiz')
    p_full.add_argument('--baseline', required=True)

    p_revert = sub.add_parser('revert', help='gtip_matcher.py revert + log')
    p_revert.add_argument('--reason', required=True, help='Revert gerekçesi')

    p_cycle = sub.add_parser('test-cycle',
                              help='quick-test → full-eval → regresyon varsa auto-revert')
    p_cycle.add_argument('--baseline', required=True)

    args = parser.parse_args()

    if args.command == 'show-chronic':
        cmd_show_chronic(args)
    elif args.command == 'quick-test':
        cmd_quick_test(args)
    elif args.command == 'full-eval':
        cmd_full_eval(args)
    elif args.command == 'revert':
        cmd_revert(args)
    elif args.command == 'test-cycle':
        cmd_test_cycle(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
