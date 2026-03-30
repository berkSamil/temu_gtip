import sys, os, json, time
sys.stdout.reconfigure(encoding='utf-8')
from playwright.sync_api import sync_playwright

url = 'https://www.temu.com/no-en/2pcs-jaw-trainer-fitness-facial-muscle-exerciser-silicone-chewing-bite-exerciser-g-601104552083872.html?_oak_mp_inf=EKCDs%2Fq41ogBGiBhZTgyNzVmNTczMjc0ZDFmYjM4ZmMzNDQwY2U0Yjg3YiDDpcD%2B0TM%3D&top_gallery_url=https%3A%2F%2Fimg.kwcdn.com%2Fproduct%2Ffancy%2Fdb760d9f-a17b-4ed4-b6f1-c32adee27769.jpg&spec_gallery_id=25035358988&refer_page_sn=10005&freesia_scene=1&_oak_freesia_scene=1&_oak_rec_ext_1=MTAwMA&_oak_gallery_order=397043156%2C1403566890%2C488240312%2C1369122923%2C1748257053&refer_page_el_sn=200024&ab_scene=1&_x_sessn_id=shx2pj53t2&refer_page_name=home&refer_page_id=10005_1774355222770_enog7gqm88'


def wait_for_captcha(page, timeout=120):
    """If CAPTCHA is visible, wait for user to solve it."""
    for _ in range(timeout // 2):
        has_captcha = page.evaluate('''() => {
            const el = document.querySelector('[class*="captcha"], [class*="Captcha"], [class*="verify"]');
            const txt = document.body?.innerText || '';
            return !!(el || txt.includes('Security Verification'));
        }''')
        if not has_captcha:
            return True
        time.sleep(2)
    return False


with sync_playwright() as pw:
    try:
        browser = pw.chromium.connect_over_cdp('http://127.0.0.1:9222')
    except Exception as e:
        print(f"Chrome'a baglanilamadi: {e}")
        print("Chrome Debug.bat calistirin.")
        sys.exit(1)

    ctx = browser.contexts[0]
    page = ctx.new_page()

    print(f"Loading...")
    page.goto(url, wait_until='domcontentloaded', timeout=45000)
    time.sleep(3)

    has_captcha = page.evaluate('''() => {
        return (document.body?.innerText || '').includes('Security Verification');
    }''')

    if has_captcha:
        print("*** CAPTCHA goruldu! Chrome'da cozmeni bekliyorum... ***")
        wait_for_captcha(page)
        print("CAPTCHA cozuldu, devam ediliyor...")
        time.sleep(3)

    title = page.title()
    print(f"Title: {title}")

    for i in range(12):
        time.sleep(2)
        try:
            gp_len = page.evaluate(
                '(() => { try { return window.rawData.store.goodsProperty.length } catch(e) { return -1 } })()'
            )
            status = page.evaluate(
                '(() => { try { return window.rawData.store.goods.status } catch(e) { return "?" } })()'
            )
            print(f"  [{(i+1)*2}s] goodsProperty={gp_len} status={status}")
            if gp_len > 0:
                raw = page.evaluate('''(() => {
                    try { return JSON.stringify(window.rawData.store.goodsProperty, null, 2) }
                    catch(e) { return "err: " + e }
                })()''')
                print(f"\n=== RAW goodsProperty ===")
                print(raw)

                goods = page.evaluate('''(() => {
                    try {
                        let g = window.rawData.store.goods;
                        return {
                            name: g.goodsName,
                            status: g.status,
                            gallery_count: (g.gallery||[]).length,
                            first_gallery: (g.gallery||[])[0],
                            hdThumbUrl: g.hdThumbUrl || null,
                        }
                    } catch(e) { return {error: e.toString()} }
                })()''')
                print(f"\n=== Goods info ===")
                print(json.dumps(goods, indent=2))
                break
        except Exception as e:
            print(f"  [{(i+1)*2}s] {str(e)[:80]}")

    page.screenshot(path='output/live_test.png')
    print("\nScreenshot saved.")
    page.close()
    browser.close()
