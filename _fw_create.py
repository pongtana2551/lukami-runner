# -*- coding: utf-8 -*-
# Lukami Fourthwall shop automation (drives the real admin UI via CDP on Edge 9222).
# The self-fulfilled product type is NOT creatable via Fourthwall's API, so we automate the UI.
# Functions: create_product, list_products, update_product, set_visibility, archive_product.
import os, re, json, hmac, hashlib, urllib.parse
from contextlib import contextmanager
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = "https://lukami-pay.akochanstar.workers.dev"
SHOP = "https://lukami-shop.fourthwall.com"
ADMIN_PRODUCTS = SHOP + "/admin/dashboard/products/all/"
THB_PER_USD = 34.5  # Fourthwall USD display price only; PromptPay charges the baht

LINK_SECRET = os.environ.get("LINK_SECRET") or open(os.path.join(HERE, "..", "lukami-worker", "_link_secret.txt"), encoding="utf-8").read().strip()

TRACK = """() => {
  const setReact=(el,v)=>{const p=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');const last=el.value;p.set.call(el,v);if(el._valueTracker)el._valueTracker.setValue(last);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
  const empt=[...document.querySelectorAll('input')].filter(x=>x.offsetParent!==null && (x.type==='text'||x.type===''||x.type==='number') && !x.value);
  const byY={};
  empt.forEach(x=>{const y=Math.round(x.getBoundingClientRect().top); (byY[y]=byY[y]||[]).push(x);});
  for(const y of Object.keys(byY)){ if(byY[y].length>=2){ setReact(byY[y][0],'3'); setReact(byY[y][1],'7'); return 'ok@'+y; } }
  return 'no-pair';
}"""

LIST_JS = """() => {
  const seen={}, out=[];
  for (const a of document.querySelectorAll('a[href*="/products/all/"]')) {
    const href=a.getAttribute('href')||'';
    const parts=href.split('/products/all/'); if(parts.length<2) continue;
    const id=parts[1].split('/')[0].split('?')[0];
    if(!id || id.length<20 || seen[id]) continue; seen[id]=1;
    let row=a.closest('tr')||a.closest('li')||a.parentElement;
    for(let k=0;k<4 && row && (row.innerText||'').length<3;k++) row=row.parentElement;
    const img=(row||a).querySelector('img');
    out.push({id, name:(a.innerText||'').trim(),
              rowtext:((row?row.innerText:'')||'').replace(/\\n+/g,' | '),
              img: img ? img.src : ''});
  }
  return out;
}"""

SAVE_VISIBLE_JS = "() => { const b=[...document.querySelectorAll('button')].filter(x=>x.offsetParent!==null && (x.innerText||'').trim()==='Save'); if(b.length){ b[b.length-1].click(); return true;} return false; }"

def _sign(baht, item):
    return hmac.new(LINK_SECRET.encode(), ("thb|%d|%s" % (baht, item)).encode(), hashlib.sha256).hexdigest()

def _button_html(name, baht):
    url = WORKER + "/pay?item=" + urllib.parse.quote(name) + "&amt=" + str(baht) + "&sig=" + _sign(baht, name)
    return ('<p><a href="%s" target="_blank" '
            'style="display:inline-block;background:#0a2a66;color:#ffffff;font-weight:bold;'
            'padding:14px 22px;border-radius:12px;text-decoration:none;font-size:16px;">'
            'Pay with PromptPay - scan QR to pay now</a></p>'
            '<p>Or use the Add to cart button below (card).</p>') % url

def parse_price(name):
    m = re.search(r'฿\s*([0-9,]+)', name or "")
    return int(m.group(1).replace(",", "")) if m else None

def base_name(name):
    return re.split(r'\s*·\s*฿', name or "")[0].strip()

def _load_cookies():
    cj = os.environ.get("FW_COOKIES_JSON")
    cf = os.environ.get("FW_COOKIES_FILE")
    if cj:
        return json.loads(cj)
    if cf and os.path.isfile(cf):
        return json.load(open(cf, encoding="utf-8"))
    return None

@contextmanager
def _session():
    with sync_playwright() as pw:
        cookies = _load_cookies()
        if cookies is not None:
            # CLOUD/headless mode: fresh Chromium + injected Fourthwall login cookies
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
            ctx.add_cookies(cookies)
            page = ctx.new_page()
            try:
                yield page
            finally:
                try: browser.close()
                except Exception: pass
        else:
            # LOCAL mode: attach to the logged-in Edge 9222 over CDP
            browser = None
            for attempt in range(3):  # busy Edge can make CDP target-enumeration slow; retry
                try:
                    browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=25000)
                    break
                except Exception:
                    if attempt == 2:
                        raise
            ctx = browser.contexts[0]
            cdp = ctx.new_cdp_session(ctx.pages[0])
            with ctx.expect_page(timeout=15000) as info:
                cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
            page = info.value
            try:
                yield page
            finally:
                try: page.close()
                except Exception: pass
                try:
                    s = browser.new_browser_cdp_session()
                    s.send("Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": False}); s.detach()
                except Exception: pass

def _fill_price(page, usd):
    try: page.get_by_label("Selling price").fill(str(usd))
    except Exception: page.locator('input[inputmode="decimal"], input[type="number"]').first.fill(str(usd))

def _set_description(page, html):
    page.get_by_role("button", name="Source code").click(timeout=6000); page.wait_for_timeout(1200)
    page.locator('.tox-dialog textarea').first.fill(html, timeout=6000)
    page.locator('.tox-dialog').get_by_role("button", name="Save").click(timeout=6000); page.wait_for_timeout(1000)

def _add_image(page, image_path):
    page.locator('input[type="file"]').first.set_input_files(image_path, timeout=8000); page.wait_for_timeout(3500)
    page.get_by_role("button", name="Add photo", exact=False).first.click(timeout=6000); page.wait_for_timeout(6000)

def _publish(page, public):
    target = "Public" if public else "Hidden"
    for cur in ("Public", "Hidden", "Private"):
        try:
            el = page.get_by_text(cur, exact=True)
            if el.count() > 0 and el.first.is_visible():
                el.first.click(force=True, timeout=4000); break
        except Exception: continue
    page.wait_for_timeout(1300)
    page.get_by_text(target, exact=True).first.click(timeout=4000); page.wait_for_timeout(700)
    page.evaluate(SAVE_VISIBLE_JS); page.wait_for_timeout(4000)

# ---- public functions ----------------------------------------------------
def create_product(name, baht, image_path, publish=True, log=print):
    baht = int(baht); name = name.strip()
    if not os.path.isfile(image_path):
        return {"ok": False, "url": None, "error": "image not found"}
    name_full = "%s · ฿%d" % (name, baht)
    usd = round(baht / THB_PER_USD, 2)
    with _session() as page:
        try:
            page.goto(ADMIN_PRODUCTS, wait_until="domcontentloaded", timeout=45000); page.wait_for_timeout(4000)
            page.get_by_role("button", name="Create new product").first.click(timeout=10000); page.wait_for_timeout(2500)
            page.get_by_text("Sell something I have", exact=False).first.click(timeout=10000); page.wait_for_timeout(6000)
            log("draft")
            page.get_by_label("Product name").fill(name_full)
            _fill_price(page, usd)
            _set_description(page, _button_html(name, baht)); log("button")
            try: page.get_by_text("Product category").first.scroll_into_view_if_needed(timeout=4000)
            except Exception: pass
            page.wait_for_timeout(400)
            page.get_by_text("Product category").first.click(force=True, timeout=6000); page.wait_for_timeout(1300)
            page.get_by_text("Bundles", exact=True).first.click(timeout=4000); page.wait_for_timeout(700)
            page.evaluate(TRACK); page.wait_for_timeout(500)
            _add_image(page, image_path); log("image")
            page.get_by_role("button", name="Save", exact=True).first.click(timeout=6000); page.wait_for_timeout(5500)
            if (page.get_by_text("This field is required").count() or 0) > 0:
                return {"ok": False, "url": None, "error": "save blocked (required field)"}
            log("saved")
            if publish: _publish(page, True); log("published")
            pub = page.evaluate("() => { const a=[...document.querySelectorAll('a')].find(x=>x.href.indexOf('/products/')>0 && x.href.indexOf('/admin')<0); return a?a.href:''; }")
            return {"ok": True, "url": pub or "", "error": None}
        except Exception as e:
            return {"ok": False, "url": None, "error": str(e)[:200]}

def list_products(log=print):
    with _session() as page:
        try:
            page.goto(ADMIN_PRODUCTS, wait_until="domcontentloaded", timeout=45000); page.wait_for_timeout(5000)
            rows = page.evaluate(LIST_JS)
            for r in rows:
                r["price"] = parse_price(r["name"])
                r["base"] = base_name(r["name"])
                t = r.get("rowtext", "")
                r["status"] = "Public" if "Public" in t else ("Hidden" if "Hidden" in t else ("Private" if "Private" in t else "?"))
                r["admin_url"] = ADMIN_PRODUCTS + r["id"] + "/"
            return {"ok": True, "products": rows}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "products": []}

def update_product(product_id, name, baht, image_path=None, publish=None, log=print):
    baht = int(baht); name = name.strip()
    name_full = "%s · ฿%d" % (name, baht)
    usd = round(baht / THB_PER_USD, 2)
    with _session() as page:
        try:
            page.goto(ADMIN_PRODUCTS + product_id + "/", wait_until="domcontentloaded", timeout=45000); page.wait_for_timeout(5000)
            page.get_by_label("Product name").fill(name_full); log("name")
            _fill_price(page, usd); log("price")
            _set_description(page, _button_html(name, baht)); log("button")
            if image_path and os.path.isfile(image_path):
                _add_image(page, image_path); log("image")
            page.get_by_role("button", name="Save", exact=True).first.click(timeout=6000); page.wait_for_timeout(5000)
            if (page.get_by_text("This field is required").count() or 0) > 0:
                return {"ok": False, "error": "save blocked (required field)"}
            log("updated")
            if publish is not None:
                _publish(page, publish); log("visibility")
            return {"ok": True, "url": ADMIN_PRODUCTS + product_id + "/"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

def set_visibility(product_id, public, log=print):
    with _session() as page:
        try:
            page.goto(ADMIN_PRODUCTS + product_id + "/", wait_until="domcontentloaded", timeout=45000); page.wait_for_timeout(5000)
            _publish(page, public); log("visibility -> " + ("Public" if public else "Hidden"))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

def archive_product(product_id, log=print):
    with _session() as page:
        try:
            page.goto(ADMIN_PRODUCTS + product_id + "/", wait_until="domcontentloaded", timeout=45000); page.wait_for_timeout(5000)
            page.get_by_role("button", name="Options").first.click(force=True, timeout=6000); page.wait_for_timeout(1200)
            page.get_by_text("Archive", exact=True).first.click(timeout=5000); page.wait_for_timeout(1500)
            for nm in ("Archive", "Confirm", "Yes"):
                try:
                    b = page.get_by_role("button", name=nm, exact=False)
                    if b.count() > 0 and b.last.is_visible(): b.last.click(timeout=3000); break
                except Exception: continue
            page.wait_for_timeout(3000); log("archived")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
