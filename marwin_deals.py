"""Бот скидок Marwin: раз в час ищет товары со скидкой больше 10% и постит новые в Telegram-канал.

Данные берутся из открытого API нового сайта new.marwin.kz (robots.txt: Allow /).
Запуск:  python marwin_deals.py run      — проверить и запостить новые скидки
         python marwin_deals.py dry      — только показать, что было бы отправлено
Настройки через переменные окружения (см. CONFIG ниже).
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://new.marwin.kz"
HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "seen.json"
LOG_PATH = HERE / "deals.log"

CONFIG = {
    "min_discount": int(os.environ.get("MIN_DISCOUNT", "10")),     # постим, если скидка > этого %
    "city_id": int(os.environ.get("CITY_ID", "2")),                # 2 = Алматы
    "in_stock_only": os.environ.get("IN_STOCK_ONLY", "1") == "1",
    "max_posts": int(os.environ.get("MAX_POSTS", "40")),           # за один запуск
    "first_run_posts": int(os.environ.get("FIRST_RUN_POSTS", "10")),  # при первом запуске: топ-N
    "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "channel": os.environ.get("TELEGRAM_CHANNEL", ""),             # @username канала или -100…
    "request_delay": 0.4,
}
UA = "marwin-deals-bot/1.0 (personal Telegram deals notifier)"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------- Marwin API ----------------
class Marwin:
    def __init__(self, delay=0.4):
        self.delay = delay
        self._last = 0.0

    def get(self, path, params=None, retries=3):
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(retries):
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                log(f"GET {path} ошибка ({attempt + 1}/{retries}): {e}")
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"Marwin API недоступен: {path}")

    def top_categories(self):
        return [c["id"] for c in self.get("/v1/catalog/categories", {"lang": "ru"})["items"]]

    def discounted(self, category_id, city_id, in_stock_only):
        page = 1
        while True:
            params = {"category_id": category_id, "city_id": city_id, "per_page": 100,
                      "lang": "ru", "page": page, "has_discount": 1, "sort": "discount"}
            if in_stock_only:
                params["availability"] = "in_stock"
            data = self.get("/v1/catalog/products", params)
            yield from data.get("items", [])
            if page >= (data.get("pagination") or {}).get("pages", 1):
                break
            page += 1

    def details(self, product_id, city_id):
        try:
            return self.get(f"/v1/catalog/products/{product_id}", {"lang": "ru", "city_id": city_id})
        except RuntimeError:
            return {}


def discount_of(item):
    p = item.get("price") or {}
    old, cur = p.get("old"), p.get("current")
    if old and cur and old > cur:
        return round((1 - cur / old) * 100)
    return int((item.get("badges") or {}).get("discount") or 0)


def collect(api, cfg):
    """Все товары со скидкой > min_discount по всему каталогу, без дублей."""
    found = {}
    for cat in api.top_categories():
        for it in api.discounted(cat, cfg["city_id"], cfg["in_stock_only"]):
            d = discount_of(it)
            if d > cfg["min_discount"] and (it.get("in_stock") or not cfg["in_stock_only"]):
                found[str(it["id"])] = dict(it, discount=d)
    return found


# ---------------- Telegram ----------------
def fmt_price(x):
    return f"{int(x):,}".replace(",", " ") + " ₸"


def short_description(text, limit=220):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:dot + 1] if dot > 80 else cut.rsplit(" ", 1)[0] + "…"


def caption(item, description):
    p = item["price"]
    url = f"{BASE}/product/{item['slug']}"
    lines = [f"🔥 <b>−{item['discount']}%</b>  {html.escape(item['name'])}",
             f"💰 <b>{fmt_price(p['current'])}</b>  <s>{fmt_price(p['old'])}</s>" if p.get("old") else
             f"💰 <b>{fmt_price(p['current'])}</b>"]
    desc = short_description(description)
    if desc:
        lines.append("")
        lines.append(html.escape(desc))
    lines.append("")
    lines.append(f'<a href="{url}">👉 Открыть в Marwin</a>')
    return "\n".join(lines)[:1024]


def tg(cfg, method, payload):
    if not cfg["bot_token"] or not cfg["channel"]:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL")
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/{method}"
    body = json.dumps(dict(payload, chat_id=cfg["channel"])).encode()
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            info = json.loads(e.read().decode() or "{}")
            retry = (info.get("parameters") or {}).get("retry_after")
            if e.code == 429 and retry:
                time.sleep(retry + 1)
                continue
            return info
        except urllib.error.URLError as e:
            log(f"Telegram ошибка сети: {e}")
            time.sleep(5)
    return {"ok": False}


def post(cfg, item, description):
    cap = caption(item, description)
    if item.get("image"):
        res = tg(cfg, "sendPhoto", {"photo": item["image"], "caption": cap, "parse_mode": "HTML"})
        if res.get("ok"):
            return True
        log(f"sendPhoto не прошёл ({res.get('description')}), шлю текстом")
    res = tg(cfg, "sendMessage", {"text": cap, "parse_mode": "HTML", "disable_web_page_preview": False})
    if not res.get("ok"):
        log(f"sendMessage ошибка: {res.get('description')}")
    return bool(res.get("ok"))


# ---------------- state / main ----------------
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return None


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")


def plan(found, seen):
    """Что постить: новые товары и те, у которых скидка выросла. Сортировка: сначала большие скидки."""
    todo = [it for k, it in found.items() if k not in seen or it["discount"] > seen[k]]
    return sorted(todo, key=lambda it: (-it["discount"], it["name"]))


def run(api, cfg, dry=False, sleep=time.sleep):
    found = collect(api, cfg)
    state = load_state()
    first_run = state is None
    seen = {} if first_run else state
    todo = plan(found, seen)
    log(f"Со скидкой >{cfg['min_discount']}%: {len(found)}; новых/подешевевших: {len(todo)}"
        + (" (первый запуск)" if first_run else ""))

    if first_run:
        limit = cfg["first_run_posts"]
        if not dry and todo:
            tg(cfg, "sendMessage", {"text": f"👋 Бот скидок Marwin запущен. Сейчас со скидкой больше "
                                             f"{cfg['min_discount']}%: {len(found)} товаров. "
                                             f"Вот топ-{min(limit, len(todo))}, дальше буду присылать только новые."})
    else:
        limit = cfg["max_posts"]

    posted = 0
    for it in todo[:limit]:
        desc = api.details(it["id"], cfg["city_id"]).get("description", "")
        if dry:
            print("-" * 40 + "\n" + caption(it, desc))
            ok = True
        else:
            ok = post(cfg, it, desc)
            sleep(3.2)  # лимит Telegram для каналов ~20 сообщений/мин
        if ok:
            seen[str(it["id"])] = it["discount"]
            posted += 1

    if first_run:  # остальное считаем уже известным, чтобы не завалить канал
        for k, it in found.items():
            seen.setdefault(k, it["discount"])
    # товары, у которых скидка закончилась, забываем: вернётся скидка — запостим снова
    seen = {k: v for k, v in seen.items() if k in found}
    if not dry:
        save_state(seen)
    log(f"Отправлено: {posted}; осталось в очереди: {max(0, len(todo) - posted) if not first_run else 0}")
    return posted


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    api = Marwin(CONFIG["request_delay"])
    if mode == "dry":
        run(api, CONFIG, dry=True)
    elif mode == "run":
        run(api, CONFIG)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
