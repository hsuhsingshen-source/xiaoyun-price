#!/usr/bin/env python3
"""
小雲比價網站產生器

讀取 sources.json 裡的商店頁面,抓價格、換算成新台幣,
把結果寫成 docs/index.html(最低價黃色標示)、docs/prices.json,
並把每次結果累積到 docs/history.csv。

由 GitHub Actions 每小時執行一次,docs/ 資料夾以 GitHub Pages 發布。

本機測試:
    pip install -r requirements.txt
    python pricewatch.py
"""

import csv
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "sources.json"
OUT_DIR = BASE / "docs"
HTML_PATH = OUT_DIR / "index.html"
JSON_PATH = OUT_DIR / "prices.json"
CSV_PATH = OUT_DIR / "history.csv"

TZ = ZoneInfo("Asia/Taipei")
REQUEST_TIMEOUT = 20
HISTORY_POINTS = 72  # 圖表最多顯示最近幾次
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


# ----------------------------------------------------------------------
# 價格解析
# ----------------------------------------------------------------------
def to_number(text):
    """'HK$ 1,580' -> 1580.0;無法解析則回傳 None"""
    if text is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(text))
    if not m:
        return None
    try:
        value = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def price_from_json_ld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                offers = node.get("offers")
                if offers:
                    stack.append(offers)
                for key in ("price", "lowPrice"):
                    if key in node and to_number(node[key]):
                        return to_number(node[key])
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return None


def price_from_meta(soup):
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and to_number(tag.get("content")):
            return to_number(tag["content"])
    return None


PRICE_IN_TEXT = re.compile(r"(?:NT\$|NTD|\$|＄)\s*([\d,]{3,}(?:\.\d+)?)")


def extract_price(page_html, source):
    """
    依序收集候選價格:CSS selector -> regex -> JSON-LD -> meta -> (scan_text)網頁文字中的 $ 金額。
    若設定了 min_price / max_price,只接受落在範圍內的第一個價格,
    這樣不會把運費門檻、配件價格誤當成商品價格。
    """
    # 傳入 bytes,讓 BeautifulSoup 依網頁自己宣告的編碼解碼,避免亂碼
    soup = BeautifulSoup(page_html, "html.parser")
    lo = source.get("min_price")
    hi = source.get("max_price")

    def ok(v):
        return v is not None and (lo is None or v >= lo) and (hi is None or v <= hi)

    text = soup.get_text(" ", strip=True)
    candidates = []

    if source.get("selector"):
        for el in soup.select(source["selector"]):
            candidates.append(to_number(el.get("content") or el.get_text(" ", strip=True)))

    if source.get("regex"):
        for m in re.finditer(source["regex"], text):
            candidates.append(to_number(m.group(1)))

    candidates.append(price_from_json_ld(soup))
    candidates.append(price_from_meta(soup))

    if source.get("scan_text"):
        for m in PRICE_IN_TEXT.finditer(text):
            candidates.append(to_number(m.group(1)))

    for v in candidates:
        if ok(v):
            return v

    # 寬鬆模式(要在 sources.json 設 "loose": true 才會啟用):
    # 價格沒有 $ 符號、或藏在網頁原始碼裡時,改找所有「x,xxx」格式的數字,
    # 取落在範圍內的最小值(原價一定比特價高,所以最小值通常是現價)。
    # 一定要搭配 min_price / max_price 使用。
    if source.get("loose") and (lo is not None or hi is not None):
        raw = soup.decode()
        found = []
        for chunk in (text, raw):
            for m in LOOSE_NUMBER.finditer(chunk):
                v = to_number(m.group(1))
                if ok(v):
                    found.append(v)
            if found:
                return min(found)
    return None


LOOSE_NUMBER = re.compile(r"(?<![\d,.])(\d{1,2},\d{3})(?![\d,])")


def fetch_one(source, rates):
    result = {
        "name": source["name"],
        "url": source["url"],
        "currency": source["currency"],
        "price": None,
        "price_twd": None,
        "error": None,
    }
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        price = extract_price(resp.content, source)
        if price is None:
            result["error"] = (
                f"頁面讀到了({len(resp.content):,} 位元組)但找不到合理範圍內的價格,"
                "可能是價格由 JavaScript 載入"
            )
        else:
            rate = rates.get(source["currency"])
            if rate is None:
                result["error"] = f"缺少 {source['currency']} 匯率"
            else:
                result["price"] = price
                result["price_twd"] = round(price * rate)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        hint = "(網站擋掉了自動讀取)" if code in (403, 429, 503) else ""
        result["error"] = f"網站回應 HTTP {code}{hint}"
    except requests.RequestException as exc:
        result["error"] = f"連線失敗:{exc.__class__.__name__}"
    return result


BIGGO_HEADERS = {
    "User-Agent": "pricewatch-personal/1.0 (personal price tracker)",
    "Content-Type": "application/json",
}


def fetch_biggo(source, rates):
    """
    透過 BigGo 的搜尋資料取得蝦皮、momo 等平台的價格(這些平台本身無法直接讀取)。
    使用的是 BigGo 官方開源 MCP 伺服器所呼叫的同一個搜尋端點,不需要金鑰。
    這不是有文件的公開 API,日後可能改版;改版時會顯示「取價失敗」。

    設定欄位(sources.json):
      type            "biggo"
      query           搜尋關鍵字,例如 "小雲15P"
      must_include    標題必須全部包含的字(不分大小寫),例如 ["15P"]
      must_exclude    標題只要含其中任何一個就排除,例如 ["10P", "遙控", "二手"]
      include_stores  只保留店名含這些字的結果,例如 ["蝦皮", "momo"];不填就全部保留
      exclude_stores  店名含這些字的結果直接排除,用來略過已有直接來源的商店,例如 ["PChome"]
      min_price / max_price   價格合理範圍,擋掉配件與不相干商品
      max_results     最多顯示幾筆(取最低價),預設 8
      region / site   預設 tw / biggo.com.tw
      api_base        預設 https://api.biggo.com
    """
    label = source.get("name", "BigGo")
    fail = {
        "name": label, "url": f"https://biggo.com.tw/s/{quote(source['query'])}/",
        "currency": "TWD", "price": None, "price_twd": None, "error": None,
    }
    api_base = source.get("api_base", "https://api.biggo.com").rstrip("/")
    url = f"{api_base}/api/v1/spa/search/{quote(source['query'], safe='')}/product"
    headers = dict(BIGGO_HEADERS)
    headers["site"] = source.get("site", "biggo.com.tw")
    headers["region"] = source.get("region", "tw")

    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        fail["error"] = f"BigGo 回應 HTTP {code}"
        return [fail]
    except (requests.RequestException, ValueError) as exc:
        fail["error"] = f"BigGo 連線或解析失敗:{exc.__class__.__name__}"
        return [fail]

    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        fail["error"] = "BigGo 回傳格式不符預期(可能已改版)"
        return [fail]

    must_in = [s.lower() for s in source.get("must_include", [])]
    must_out = [s.lower() for s in source.get("must_exclude", [])]
    stores_ok = [s.lower() for s in source.get("include_stores", [])]
    stores_no = [s.lower() for s in source.get("exclude_stores", [])]
    lo, hi = source.get("min_price"), source.get("max_price")
    domain = source.get("site", "biggo.com.tw")

    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "")
        t = title.lower()
        if any(k not in t for k in must_in) or any(k in t for k in must_out):
            continue
        price = to_number(it.get("price"))
        if price is None or (lo is not None and price < lo) or (hi is not None and price > hi):
            continue
        store = str((it.get("store") or {}).get("name") or "").strip()
        shop = str((it.get("shop") or {}).get("name") or "").strip()
        if stores_ok and not any(k in store.lower() for k in stores_ok):
            continue
        if any(k in store.lower() for k in stores_no):
            continue
        cur = str(it.get("currency") or "TWD").upper()
        rate = rates.get(cur, rates.get("TWD", 1.0) if cur in ("", "$", "NT$") else None)
        if rate is None:
            continue
        link = it.get("affurl") or it.get("url") or ""
        if isinstance(link, str) and link.startswith("/"):
            link = f"https://{domain}{link}"
        name = f"{label}:{store or '未知商店'}" + (f"({shop})" if shop else "")
        rows.append({
            "name": name, "url": link or fail["url"], "currency": cur if cur in rates else "TWD",
            "price": price, "price_twd": round(price * rate), "error": None,
        })

    rows.sort(key=lambda r: r["price_twd"])
    rows = rows[: source.get("max_results", 8)]
    if not rows:
        fail["error"] = "BigGo 搜尋有結果,但沒有符合條件的商品(關鍵字、商店或價格範圍都被濾掉了)"
        return [fail]
    return rows


def mark_lowest(results):
    valid = [r["price_twd"] for r in results if r["price_twd"] is not None]
    lowest = min(valid, default=None)
    for r in results:
        r["is_lowest"] = lowest is not None and r["price_twd"] == lowest
    return lowest


# ----------------------------------------------------------------------
# 歷史紀錄
# ----------------------------------------------------------------------
CSV_HEADER = ["時間", "商店", "幣別", "原價", "約合TWD", "是否最低", "錯誤"]


def append_history(results, stamp):
    OUT_DIR.mkdir(exist_ok=True)
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_HEADER)
        for r in results:
            w.writerow(
                [
                    stamp,
                    r["name"],
                    r["currency"],
                    r["price"] if r["price"] is not None else "",
                    r["price_twd"] if r["price_twd"] is not None else "",
                    "Y" if r["is_lowest"] else "",
                    r["error"] or "",
                ]
            )


def load_lowest_history():
    """回傳 [(時間, 當次最低TWD), ...],給趨勢圖用"""
    if not CSV_PATH.exists():
        return []
    per_run = {}
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            twd = to_number(row.get("約合TWD"))
            if twd is None:
                continue
            t = row["時間"]
            per_run[t] = min(per_run.get(t, twd), twd)
    return sorted(per_run.items())[-HISTORY_POINTS:]


def trend_svg(points):
    if len(points) < 2:
        return "<p class='note'>累積兩次以上的紀錄後,這裡會出現最低價趨勢圖。</p>"
    w, h, pad = 640, 160, 28
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    coords = []
    for i, (_, v) in enumerate(points):
        x = pad + i * (w - 2 * pad) / (len(points) - 1)
        y = h - pad - (v - lo) * (h - 2 * pad) / span
        coords.append((x, y))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    last_x, last_y = coords[-1]
    return (
        f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='最低價趨勢圖' "
        f"style='width:100%;max-width:{w}px'>"
        f"<polyline points='{poly}' fill='none' stroke='#555' stroke-width='2'/>"
        f"<circle cx='{last_x:.1f}' cy='{last_y:.1f}' r='5' fill='#ffeb3b' stroke='#555' stroke-width='2'/>"
        f"<text x='{pad}' y='14' font-size='12' fill='#666'>最高 NT$ {hi:,.0f}</text>"
        f"<text x='{pad}' y='{h - 8}' font-size='12' fill='#666'>最低 NT$ {lo:,.0f}</text>"
        f"</svg>"
    )


# ----------------------------------------------------------------------
# 輸出網頁
# ----------------------------------------------------------------------
def write_site(results, stamp, config, lowest):
    OUT_DIR.mkdir(exist_ok=True)
    title = config.get("title", "小雲 15P 比價")

    ordered = sorted(results, key=lambda x: (x["price_twd"] is None, x["price_twd"] or 0))
    rows = []
    for r in ordered:
        cls = " class='lowest'" if r["is_lowest"] else ""
        name_link = (
            f"<a href='{html.escape(r['url'])}' target='_blank' rel='noopener'>"
            f"{html.escape(r['name'])}</a>"
        )
        if r["price_twd"] is None:
            body = f"<td colspan='3' class='err'>取價失敗:{html.escape(r['error'] or '')}</td>"
        else:
            body = (
                f"<td>{html.escape(r['currency'])} {r['price']:,.0f}</td>"
                f"<td>NT$ {r['price_twd']:,}</td>"
                f"<td>{'最低價' if r['is_lowest'] else ''}</td>"
            )
        rows.append(f"<tr{cls}><td>{name_link}</td>{body}</tr>")

    headline = (
        f"目前最低價:<strong>NT$ {lowest:,}</strong>" if lowest is not None else "目前沒有取得任何價格"
    )

    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="1800">
<title>{html.escape(title)}</title>
<style>
 body{{font-family:system-ui,"Noto Sans TC","PingFang TC",sans-serif;margin:0;color:#222;background:#fafafa}}
 main{{max-width:760px;margin:0 auto;padding:24px 16px}}
 h1{{font-size:22px;margin:0 0 4px}}
 .sub{{color:#666;font-size:14px;margin:0 0 20px}}
 table{{border-collapse:collapse;width:100%;background:#fff}}
 th,td{{border:1px solid #ddd;padding:10px 12px;text-align:left;font-size:15px}}
 th{{background:#f1f1f1}}
 tr.lowest{{background:#ffeb3b;font-weight:600}}
 .err{{color:#b00020}}
 .note{{color:#666;font-size:13px;margin-top:14px}}
 h2{{font-size:16px;margin:28px 0 8px}}
 a{{color:#0b57d0}}
 @media(max-width:520px){{th,td{{padding:8px;font-size:14px}}}}
</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="sub">{headline} · 最後更新 {stamp}(台灣時間)</p>
<table>
<tr><th>商店</th><th>原價</th><th>約合新台幣</th><th></th></tr>
{''.join(rows)}
</table>
<h2>最低價趨勢</h2>
{trend_svg(load_lowest_history())}
<p class="note">黃色列為目前最低價。非新台幣價格依固定匯率換算,僅供參考,實際以商店結帳頁為準。
資料每小時自動更新一次,<a href="history.csv">下載歷史紀錄</a>。</p>
</main></body></html>"""
    HTML_PATH.write_text(page, encoding="utf-8")

    JSON_PATH.write_text(
        json.dumps(
            {"updated": stamp, "lowest_twd": lowest, "results": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    sources = config.get("sources", [])
    rates = config.get("rates_to_twd", {"TWD": 1.0})
    if not sources:
        sys.exit("sources.json 的 sources 是空的,請先加入商店。")

    stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    results = []
    for s in sources:
        if s.get("type") == "biggo":
            results.extend(fetch_biggo(s, rates))
        else:
            results.append(fetch_one(s, rates))
    lowest = mark_lowest(results)

    append_history(results, stamp)
    write_site(results, stamp, config, lowest)

    for r in results:
        if r["price_twd"] is None:
            print(f"[失敗] {r['name']}: {r['error']}")
        else:
            flag = "  <-- 最低價" if r["is_lowest"] else ""
            print(f"[成功] {r['name']}: {r['currency']} {r['price']:,.0f} ≈ NT$ {r['price_twd']:,}{flag}")


if __name__ == "__main__":
    main()
