import os, re, sys, json, time, random, threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import requests

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
DB = "items.json"  # dipakai hanya jika UPSTASH_URL tidak diisi
INTERVAL = 15 * 60

# Penyimpanan permanen (Render gratis menghapus file saat restart)
UP_URL = os.environ.get("UPSTASH_URL", "").rstrip("/")
UP_H = {"Authorization": "Bearer " + os.environ.get("UPSTASH_TOKEN", "")}

# Proxy opsional (pisahkan dengan koma). Hanya dipakai untuk membuka halaman produk.
PROXIES = [p.strip() for p in os.environ.get("PROXIES", "").split(",") if p.strip()]

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
UA_BOT = "TelegramBot (like TwitterBot)"
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "id-ID,id;q=0.9"})
TG = requests.Session()
lock = threading.Lock()
wlock = threading.Lock()
MODE = {}  # chat -> platform yang dipilih (sekali pakai)
LAST_OK = time.time()  # terakhir kali Telegram menjawab
_STATE = None  # data link di memori

# nama tampil, kata kunci domain yang diterima
PLAT = {
    "tokped": ("Tokopedia", ("tokopedia", "tiktok")),
    "tiktok": ("TikTok Shop", ("tiktok", "tokopedia")),
    "lazada": ("Lazada", ("lazada",)),
}


# ---------- penyimpanan ----------
def init_state():
    global _STATE
    if UP_URL:
        for _ in range(5):
            try:
                j = requests.post(UP_URL, headers=UP_H, json=["GET", "items"],
                                  timeout=(5, 10)).json()
                if "error" in j:
                    raise Exception(j["error"])
                res = j.get("result")
                _STATE = json.loads(res) if res else {}
                print("Data dimuat dari Upstash")
                return
            except Exception as e:
                print("Upstash gagal, coba lagi:", e)
                time.sleep(3)
        raise SystemExit("Tidak bisa membaca Upstash. Cek UPSTASH_URL dan UPSTASH_TOKEN.")
    try:
        _STATE = json.load(open(DB))
    except Exception:
        _STATE = {}


def load():
    return json.loads(json.dumps(_STATE))


def _push():
    with wlock:
        try:
            if UP_URL:
                j = requests.post(UP_URL, headers=UP_H,
                                  json=["SET", "items", json.dumps(_STATE)],
                                  timeout=(5, 15)).json()
                if "error" in j:
                    print("Upstash error:", j["error"])
            else:
                with open(DB + ".tmp", "w") as f:
                    json.dump(_STATE, f)
                os.replace(DB + ".tmp", DB)
        except Exception as e:
            print("gagal simpan:", e)


def save(d):
    global _STATE
    _STATE = d
    threading.Thread(target=_push, daemon=True).start()


def plat(it):
    p = it.get("platform")
    if p in PLAT:
        return p
    return "tiktok" if "tiktok" in it.get("url", "").lower() else "tokped"


# ---------- telegram ----------
def tg(method, _timeout=(10, 35), **p):
    global TG
    try:
        return TG.post(f"{API}/{method}", json=p, timeout=_timeout).json()
    except Exception:
        TG = requests.Session()  # buang sambungan yang macet, buat baru
        raise


def send(chat, text):
    for _ in range(3):
        try:
            tg("sendMessage", chat_id=chat, text=text,
               disable_web_page_preview=True, _timeout=(10, 20))
            return
        except Exception as e:
            print("gagal kirim, coba lagi:", e)
            time.sleep(1.5)


def send_long(chat, text):
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 3800:
            send(chat, buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        send(chat, buf)


# ---------- format ----------
def rp(x):
    return "-" if x is None else "Rp" + f"{int(x):,}".replace(",", ".")


def hargastr(c):
    h = rp(c["price"])
    pm = c.get("price_max")
    if pm and pm != c["price"]:
        v = c.get("variants") or 0
        h += " – " + rp(pm) + (f" ({v} variasi)" if v > 1 else " (ada variasi)")
    return h


def stokstr(c):
    s = "-" if c["stock"] is None else str(c["stock"])
    a = {True: "Tersedia", False: "Habis", None: "?"}[c["available"]]
    return f"{s} ({a})"


def fmt(c):
    return f"{c['name']}\nHarga: {hargastr(c)}\nStok: {stokstr(c)}"


def entry(i, it, err=None):
    c = it.get("last")
    nama = PLAT[plat(it)][0]
    if not c:
        lines = [f"{i}. [{nama}] (belum ada data)"]
    else:
        lines = [f"{i}. [{nama}] {c['name'][:60]}",
                 f"   Harga: {hargastr(c)}",
                 f"   Stok: {stokstr(c)}"]
    t = it.get("target")
    if t:
        hit = c and c["price"] is not None and c["price"] <= t
        lines.append(f"   Target: {rp(t)}" + (" 🎯 tercapai" if hit else ""))
    if err:
        lines.append(f"   ⚠️ Gagal update: {err[:160]}")
    lines.append(f"   {it['url']}")
    return "\n".join(lines)


def show_list(chat, prefix=""):
    with lock:
        items = load().get(chat, [])
    if not items:
        send(chat, prefix + "Belum ada produk.")
    else:
        send_long(chat, prefix + "\n\n".join(entry(i, it) for i, it in enumerate(items, 1)))


# ---------- parser ----------
def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def num(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def price_of(x):
    """Angka harga dari str/angka/dict. None kalau tersamar (*, ?) atau tidak jelas."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, dict):
        for k in ("real_price", "sale_price", "min_price", "price_val", "price", "price_str"):
            if k in x:
                p = price_of(x[k])
                if p is not None:
                    return p
        return None
    if isinstance(x, (int, float)):
        return float(x) if x >= 100 else None
    if isinstance(x, str):
        s = re.sub(r"(?i)rp\.?", "", x).strip()
        if not s or "*" in s or "?" in s:
            return None
        if re.fullmatch(r"\d+\.\d{1,2}", s):
            return float(s)
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", s):
            return float(re.sub(r"[.,]", "", s))
        if re.fullmatch(r"\d+", s):
            return float(s)
    return None


def from_jsonld(text):
    for m in re.finditer(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', text, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for o in walk(data):
            t = o.get("@type")
            if t != "Product" and not (isinstance(t, list) and "Product" in t):
                continue
            offs = o.get("offers") or []
            if isinstance(offs, dict):
                offs = [offs]
            flat = []
            for off in offs:
                if isinstance(off, dict):
                    flat.append(off)
                    sub = off.get("offers")
                    if isinstance(sub, list):
                        flat.extend(x for x in sub if isinstance(x, dict))
            lows, highs, avs, maxcount = [], [], [], 1
            for off in flat:
                lo = num(off.get("lowPrice") or off.get("price"))
                hi = num(off.get("highPrice") or off.get("price"))
                if lo is not None:
                    lows.append(lo)
                if hi is not None:
                    highs.append(hi)
                av = str(off.get("availability", ""))
                if av:
                    avs.append(av)
                try:
                    maxcount = max(maxcount, int(off.get("offerCount") or 1))
                except Exception:
                    pass
            if not lows:
                continue
            leaf = [x for x in flat if not isinstance(x.get("offers"), list)]
            price = min(lows)
            pmax = max(highs) if highs else price
            return {
                "name": o.get("name", "Produk"),
                "price": price,
                "price_max": pmax if pmax != price else None,
                "variants": max(len(leaf), maxcount),
                "stock": None,
                "available": any("InStock" in a for a in avs) if avs else None,
            }
    return None


def embedded_json(text):
    blobs = []
    for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', text, re.S):
        try:
            blobs.append(json.loads(m.group(1)))
        except Exception:
            pass
    for m in re.finditer(r'window\.[A-Za-z_]+\s*=\s*(\{.*?\})\s*;?\s*</script>', text, re.S):
        try:
            blobs.append(json.loads(m.group(1)))
        except Exception:
            pass
    return blobs


def from_payload(text):
    """Data produk TikTok Shop (product_base + skus). Return (data, ada_payload, tersamar)."""
    seen = masked = False
    for blob in embedded_json(text):
        for o in walk(blob):
            pb = o.get("product_base")
            if not isinstance(pb, dict):
                continue
            seen = True
            skus = o.get("skus") or pb.get("skus") or []
            prices, stocks = [], []
            for s in skus if isinstance(skus, list) else []:
                if not isinstance(s, dict):
                    continue
                p = price_of(s.get("price"))
                if p is not None:
                    prices.append(p)
                for k in ("stock", "sku_quantity", "stock_num", "quantity"):
                    v = s.get(k)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        stocks.append(int(v))
                        break
            if not prices:
                p = price_of(pb.get("price"))
                if p is not None:
                    prices.append(p)
            if prices:
                total = sum(stocks) if stocks else None
                lo, hi = min(prices), max(prices)
                return {
                    "name": pb.get("title") or pb.get("name") or "Produk TikTok Shop",
                    "price": lo,
                    "price_max": hi if hi != lo else None,
                    "variants": len(prices),
                    "stock": total,
                    "available": (total > 0) if total is not None else None,
                }, True, False
            masked = True
    return None, seen, masked


def parse_page(text):
    info = {"payload": False, "masked": False, "captcha": False}
    c = from_jsonld(text)
    if c:
        return c, info
    c, seen, masked = from_payload(text)
    info["payload"], info["masked"] = seen, masked
    if c:
        return c, info
    t = re.search(r'property="og:title"[^>]*content="([^"]+)"', text)
    p = re.search(r'(?:product|og):price:amount"[^>]*content="([\d.,]+)"', text)
    if t and p and price_of(p.group(1)) is not None:
        return {"name": t.group(1), "price": price_of(p.group(1)), "price_max": None,
                "variants": 1, "stock": None, "available": None}, info
    info["captcha"] = bool(re.search(r"captcha|verify-?center|slide to verify",
                                     text[:30000], re.I))
    return None, info


def diag(nama, platform, r, info):
    u = urlparse(r.url)
    if info["masked"]:
        why = "harga disamarkan oleh TikTok (*)"
    elif info["captcha"]:
        why = "halaman verifikasi/captcha"
    elif info["payload"]:
        why = "data produk ada, harga tidak terbaca"
    else:
        why = "data produk tidak ditemukan"
    msg = (f"{nama}: {why} [HTTP {r.status_code} | {(u.netloc + u.path)[:55]} | "
           f"{len(r.text)//1024}KB]")
    if platform in ("tokped", "tiktok"):
        msg += ("\nSaran: salin link dari WEBSITE Tokopedia di browser "
                "(bukan tombol Bagikan di aplikasi), lalu kirim dengan /tokped.")
    return msg


def proxy_kw():
    """Pilih proxy acak tiap percobaan (kalau PROXIES diisi)."""
    if not PROXIES:
        return {}
    p = random.choice(PROXIES)
    return {"proxies": {"http": p, "https": p}}


def fetch(url, platform):
    nama, ok = PLAT[platform]
    uas = [UA, UA_DESKTOP] if platform == "lazada" else [UA, UA_DESKTOP, UA_BOT]
    last = None
    err = None
    for ua in uas:
        try:
            r = S.get(url, headers={"User-Agent": ua}, allow_redirects=True,
                      timeout=(8, 15), **proxy_kw())
        except requests.RequestException as e:
            err = e  # proxy mati / jaringan putus: coba lagi dengan proxy lain
            continue
        host = urlparse(r.url).netloc.lower()
        if not any(k in host for k in ok):
            raise Exception(f"Halaman bukan {nama} (dialihkan ke {host})")
        if platform == "lazada" and ("punish" in r.url or "x5sec" in r.url):
            raise Exception("Lazada menampilkan verifikasi anti-bot")
        c, info = parse_page(r.text)
        if c:
            return c
        last = (r, info)
    if last is None:
        raise Exception(f"{nama}: koneksi gagal ({type(err).__name__})")
    raise Exception(diag(nama, platform, *last))


def sig(c):
    return (c.get("price"), c.get("price_max"), c.get("stock"), c.get("available"))


# ---------- pengecekan ----------
def _one(t):
    c, url, p = t
    try:
        return (c, url), fetch(url, p)
    except Exception as e:
        print("gagal:", url, e)
        return (c, url), e


def refresh(chat_filter=None, notify=True):
    """Ambil data terbaru (paralel). Jaringan dijalankan di luar lock."""
    with lock:
        d = load()
        todo = [(c, it["url"], plat(it)) for c, its in d.items()
                if chat_filter in (None, c) for it in its]
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = dict(ex.map(_one, todo))
    alerts = []
    with lock:
        d = load()
        for c, its in d.items():
            for it in its:
                cur = results.get((c, it["url"]))
                if cur is None or isinstance(cur, Exception):
                    continue
                old = it.get("last")
                if notify and old and sig(old) != sig(cur):
                    alerts.append((c, f"🔔 Ada perubahan!\n\nSebelum:\n{fmt(old)}\n\n"
                                      f"Sekarang:\n{fmt(cur)}\n\n{it['url']}"))
                tgt = it.get("target")
                if tgt and cur["price"] is not None:
                    if cur["price"] <= tgt:
                        if not it.get("hit") and notify:
                            alerts.append((c, f"🎯 Harga mencapai target {rp(tgt)}!\n\n"
                                              f"{fmt(cur)}\n\n{it['url']}"))
                        it["hit"] = True
                    else:
                        it["hit"] = False
                it["last"] = cur
        save(d)
    for c, t in alerts:
        send(c, t)
    return results


def loop():
    while True:
        time.sleep(INTERVAL)
        try:
            refresh(None, notify=True)
        except Exception as e:
            print("error loop:", e)


def restart():
    time.sleep(1)
    with wlock:  # tunggu penyimpanan terakhir selesai
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


def watchdog():
    """Kalau Telegram tidak menjawab > 3 menit, restart otomatis."""
    while True:
        time.sleep(60)
        if time.time() - LAST_OK > 180:
            print("Koneksi macet, restart otomatis...")
            restart()


# ---------- server kesehatan + self-ping (agar Render tidak tidur) ----------
class Health(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if body:
            self.wfile.write(b"ok")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(False)

    def log_message(self, *a):
        pass


def serve():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


def keepalive():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    while True:
        time.sleep(600)
        if url:
            try:
                requests.get(url, timeout=15)
            except Exception:
                pass


# ---------- perintah ----------
HELP = (
    "Cara pakai:\n"
    "1. Pilih jenis link: /tokped, /tiktok, atau /lazada\n"
    "2. Kirim link produknya\n\n"
    "/list - daftar link (data terakhir)\n"
    "/cek - cek ulang semua link sekarang\n"
    "/edit <no> <harga> - alarm jika harga <= target\n"
    "   contoh: /edit 2 500000   (hapus: /edit 2 off)\n"
    "/hapus <no> - hapus link\n"
    "/restart - restart bot (data aman)\n\n"
    "Produk bervariasi: bot memantau harga TERENDAH dan menampilkan rentang harga."
)


def add_link(chat, url):
    p = MODE.get(chat)
    if not p:
        send(chat, "Pilih dulu: /tokped, /tiktok, atau /lazada, lalu kirim linknya.")
        return
    send(chat, "⏳ Mengambil data produk...")
    try:
        cur = fetch(url, p)
    except Exception as e:
        send(chat, f"❌ Gagal: {e}")
        return
    dup = False
    n = 0
    with lock:
        d = load()
        items = d.setdefault(chat, [])
        if any(x["url"] == url for x in items):
            dup = True
        else:
            items.append({"url": url, "platform": p, "last": cur})
            save(d)
            n = len(items)
    if dup:
        send(chat, "Link ini sudah dipantau.")
        return
    MODE.pop(chat, None)
    send(chat, f"✅ Dipantau tiap 15 menit ({PLAT[p][0]}), nomor {n}:\n\n{fmt(cur)}\n\n"
               f"Set target harga: /edit {n} <harga>")


def handle(u):
    msg = u.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = str(msg.get("chat", {}).get("id", ""))
    if not text or not chat:
        return
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    m = re.search(r"https?://\S+", text)
    url = m.group(0) if m else None

    if cmd in ("/start", "/help"):
        send(chat, HELP)

    elif cmd == "/restart":
        send(chat, "♻️ Restart... data link tetap tersimpan.")
        try:
            tg("getUpdates", offset=u["update_id"] + 1, timeout=0)
        except Exception:
            pass
        restart()

    elif cmd == "/list":
        show_list(chat)

    elif cmd == "/cek":
        with lock:
            has = bool(load().get(chat))
        if not has:
            send(chat, "Belum ada produk.")
            return
        send(chat, "⏳ Mengecek semua link, mohon tunggu...")
        res = refresh(chat, notify=False)
        with lock:
            items = load().get(chat, [])
        blocks = []
        for i, it in enumerate(items, 1):
            r = res.get((chat, it["url"]))
            blocks.append(entry(i, it, err=str(r) if isinstance(r, Exception) else None))
        send_long(chat, "\n\n".join(blocks))

    elif cmd == "/hapus":
        try:
            n = int(text.split()[1]) - 1
            if n < 0:
                raise ValueError
            with lock:
                d = load()
                d[chat].pop(n)
                save(d)
        except Exception:
            send(chat, "Format: /hapus 1 (lihat nomor di /list)")
            return
        show_list(chat, prefix="✅ Dihapus.\n\n📋 Sisa daftar:\n\n")

    elif cmd == "/edit":
        parts = text.split()
        usage = ("Format: /edit <no> <harga>\nContoh: /edit 2 500000\n"
                 "Hapus target: /edit 2 off\nNomor lihat di /list")
        if len(parts) < 3:
            send(chat, usage)
            return
        try:
            n = int(parts[1]) - 1
        except ValueError:
            send(chat, usage)
            return
        v = parts[2].lower()
        reply = None
        with lock:
            d = load()
            items = d.get(chat, [])
            if not (0 <= n < len(items)):
                reply = "Nomor tidak ada. Lihat /list"
            else:
                it = items[n]
                if v in ("off", "hapus", "0"):
                    it.pop("target", None)
                    it.pop("hit", None)
                    reply = f"Target nomor {n+1} dihapus."
                else:
                    mm = re.fullmatch(r"(?:rp)?([\d.,]+)(rb|k)?", v)
                    if not mm:
                        reply = usage
                    else:
                        price = int(re.sub(r"[.,]", "", mm.group(1)))
                        if mm.group(2):
                            price *= 1000
                        it["target"] = price
                        c = it.get("last")
                        now = ""
                        if c and c["price"] is not None:
                            now = f"\nHarga sekarang: {rp(c['price'])}"
                            it["hit"] = c["price"] <= price
                            if it["hit"]:
                                now += " (sudah di bawah target)"
                        reply = (f"🎯 Target nomor {n+1}: {rp(price)}. "
                                 f"Kamu dikabari saat harga <= target.{now}")
                save(d)
        send(chat, reply)

    elif cmd in ("/tokped", "/tiktok", "/lazada"):
        MODE[chat] = cmd[1:]
        if url:
            add_link(chat, url)
        else:
            send(chat, f"Oke, kirim link {PLAT[cmd[1:]][0]} sekarang.")

    elif url:
        add_link(chat, url)


def safe_handle(u):
    try:
        handle(u)
    except Exception as e:
        print("error handle:", e)


def main():
    global LAST_OK
    init_state()
    threading.Thread(target=serve, daemon=True).start()
    threading.Thread(target=keepalive, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        tg("setMyCommands", commands=[
            {"command": "tokped", "description": "Tambah link Tokopedia"},
            {"command": "tiktok", "description": "Tambah link TikTok Shop"},
            {"command": "lazada", "description": "Tambah link Lazada"},
            {"command": "cek", "description": "Cek ulang semua link"},
            {"command": "list", "description": "Daftar link tersimpan"},
            {"command": "edit", "description": "Set target harga"},
            {"command": "hapus", "description": "Hapus link"},
            {"command": "restart", "description": "Restart bot"},
        ])
    except Exception:
        pass
    offset = None
    print("Bot jalan (Render)...", "proxy:", len(PROXIES), "| Upstash:", bool(UP_URL))
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=25)
            if not r.get("ok"):
                print("Telegram menolak:", r.get("description"))
                time.sleep(3)
                continue
            LAST_OK = time.time()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                threading.Thread(target=safe_handle, args=(u,), daemon=True).start()
        except Exception as e:
            print("error poll:", e)
            time.sleep(2)


if __name__ == "__main__":
    main()
