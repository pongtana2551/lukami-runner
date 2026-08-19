# -*- coding: utf-8 -*-
# One-shot cloud runner (GitHub Actions). Drains pending product jobs from the worker,
# executes each via HEADLESS Fourthwall automation (login cookies from FW_COOKIES_JSON env),
# then syncs the product list back. The workflow re-runs this every few minutes.
import os, base64, tempfile, json, urllib.request, urllib.parse
import _fw_create as fw

BASE = "https://lukami-pay.akochanstar.workers.dev"
ADMIN = os.environ["ADMIN_KEY"]

def api(path, method="GET", body=None):
    url = BASE + "/api" + path + ("&" if "?" in path else "?") + "key=" + urllib.parse.quote(ADMIN)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": "Mozilla/5.0 (LukamiCloudRunner)", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.load(r)

def sync_products():
    try:
        r = fw.list_products()
        if r.get("ok"):
            prods = [{"id": p["id"], "base": p["base"], "name": p["name"], "price": p["price"],
                      "status": p["status"], "img": p["img"]} for p in r["products"]]
            api("/sync", "POST", {"products": prods})
            print("synced", len(prods), "products")
        else:
            print("list failed:", r.get("error"))
    except Exception as e:
        print("sync error:", str(e)[:150])

def run_job(job, image_b64):
    t = job.get("type"); img_path = None
    if image_b64:
        raw = image_b64.split(",", 1)[-1]
        img_path = os.path.join(tempfile.gettempdir(), "lukami_job_" + job["id"] + ".png")
        open(img_path, "wb").write(base64.b64decode(raw))
    if t == "add":    return fw.create_product(job["name"], job["baht"], img_path, publish=job["publish"])
    if t == "edit":   return fw.update_product(job["product_id"], job["name"], job["baht"], image_path=img_path, publish=job["publish"])
    if t == "hide":   return fw.set_visibility(job["product_id"], False)
    if t == "show":   return fw.set_visibility(job["product_id"], True)
    if t == "delete": return fw.archive_product(job["product_id"])
    return {"ok": False, "error": "unknown job type: %s" % t}

def main():
    print("cloud runner: draining pending jobs...")
    n = 0
    while n < 12:
        try:
            r = api("/job/next")
        except Exception as e:
            print("poll error:", str(e)[:150]); break
        job = r.get("job")
        if not job:
            break
        n += 1
        print("JOB:", job.get("type"), "|", job.get("name") or job.get("product_id"))
        try:
            res = run_job(job, r.get("image_b64"))
        except Exception as e:
            res = {"ok": False, "error": str(e)[:180]}
        api("/job/result", "POST", {"id": job["id"], "ok": bool(res.get("ok")), "error": str(res.get("error") or "")})
        print("  ->", "OK" if res.get("ok") else ("FAIL: " + str(res.get("error"))))
    sync_products()
    print("done. processed", n, "job(s)")

if __name__ == "__main__":
    main()
