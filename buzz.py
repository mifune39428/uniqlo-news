"""ユニクロ・GUで「いま話題になっている商品」（BuZZ）を数字で選ぶ。

BuZZ の判断基準は2つの数字の合計:

  1. 投稿数 … 公式サイトのレビューが直近7日で何件増えたか（買った人の反応）
  2. 反応数 … 同じ7日間に、ニュース・ブログの見出しで何回取り上げられたか（世の中の反応）

  点数 = 直近7日のレビュー増加数 × 勢い ＋ 見出しに出た回数 × MENTION_WEIGHT
  勢い = 1 ＋ 2 × （直近7日のレビュー数 ÷ レビュー総数）

「勢い」を掛けるのは、感動パンツやエアリズムのような定番品が毎週たくさんレビューされるだけで
上位を埋めてしまうのを防ぐため（初回の試算で上位10件がほぼ定番品になった）。
レビューの大半がこの1週間に付いた商品（＝急に話題になった新作）は最大3倍になる。

X（旧Twitter）やInstagramの「いいね」数は、公式APIが有料・非公開で定期的に取れないので使わない。
公式レビューは実際に買った人しか書けないので、話題の強さを測る材料としてはむしろ確かである。

数え方:
- 商品一覧（公式サイトが画面の裏で使っている商品API）を性別ごとに全件読み、
  商品ごとのレビュー総数を日付つきで docs/buzz_history.json に控える。
- 7日前の控えとの差がそのまま「直近7日のレビュー数」になる。追加の問い合わせは要らない。
- 控えが7日分たまっていないあいだ（動かし始めの1週間）は、上位の商品だけ
  レビューの投稿日時を読みに行って直近7日の件数を数え、7日前の控えを後から作る。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import re
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(BASE_DIR, "docs", "buzz_history.json")
MENTIONS_PATH = os.path.join(BASE_DIR, "docs", "buzz_mentions.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "buzz.json")

# 商品APIは素のUAだと弾かれることがあるので、ブラウザと同じものを名乗る。
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
JST = dt.timezone(dt.timedelta(hours=9))

# 性別の区分。ベビーはキッズにまとめる（「女性用・男性用・子供用」の3つで見せる）。
STORES = [
    {
        "brand": "ユニクロ",
        "key": "UQ",
        "base": "https://www.uniqlo.com/jp",
        "genders": [(1071, "ウィメンズ"), (1072, "メンズ"), (1073, "キッズ"), (1074, "キッズ")],
    },
    {
        "brand": "GU",
        "key": "GU",
        "base": "https://www.gu-global.com/jp",
        "genders": [(2256, "ウィメンズ"), (2257, "メンズ"), (2258, "キッズ")],
    },
]
GENDERS = ["ウィメンズ", "メンズ", "キッズ"]

PAGE_SIZE = 100          # 商品一覧の1ページ（APIの上限が100）
REVIEW_PAGE = 20         # レビュー一覧の1ページ（APIの上限が20）
REVIEW_MAX_PAGES = 5     # 動かし始めの数え直しは100件まで。超えたら「100件以上」とみなす
# 数え直しをする商品数（ブランド×性別ごと）。レビュー総数の多い定番品と、
# 商品番号の新しい（＝最近出た）商品の両方から選ぶ。定番品だけにすると、
# 急に話題になった新作がいつまでも数えられず、BuZZが定番品で埋まった。
BOOTSTRAP_TOP_REVIEWED = 10
BOOTSTRAP_NEWEST = 20
HISTORY_KEEP_DAYS = 16   # 前の週との比較（急上昇の判定）に14日ぶん要る
MENTION_WEIGHT = 5       # 見出し1回 ＝ レビュー5件ぶんとして数える
# これ未満の商品はBuZZに載せない。キッズは大人物よりレビューが一桁少ないので低くする
# （同じ線を引くとキッズが数件しか残らなかった）。
MIN_SCORE = {"ウィメンズ": 20, "メンズ": 20, "キッズ": 6}
MIN_REVIEWS_TO_TRACK = 1  # レビュー0件の商品は控えに残さない（ファイルを小さくするため）
TOP_PER_GROUP = 12       # ブランド×性別ごとに載せる上限
MENTION_DAYS = 7

# 見出しの照合に使う商品名の「芯」。色・丈・年度などの枝葉を落とす。
NAME_NOISE_RE = re.compile(r"[\(（][^)）]*[\)）]|\s+(MN|WN|WM|KIDS|BABY)$", re.I)
NORMALIZE_RE = re.compile(r"[\s　・/／\-－―‐×xX＆&!！?？「」『』【】\[\]()（）]")
# Googleニュースに混ざる写真ページの見出し（「＜画像7 / 20＞…」）。古い記事なので数えない。
PHOTO_TITLE_RE = re.compile(r"^[＜<]?\s*画像\s*\d+\s*/\s*\d+\s*[＞>]")


def _get_json(url: str, timeout: int = 30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def today_jst() -> str:
    return dt.datetime.now(JST).strftime("%Y-%m-%d")


def days_ago(day: str, n: int) -> str:
    return (dt.date.fromisoformat(day) - dt.timedelta(days=n)).isoformat()


def normalize(text: str) -> str:
    return NORMALIZE_RE.sub("", (text or "").lower())


def core_name(name: str) -> str:
    """見出しと照らし合わせるための商品名。「mofusand UT/ショートスリーブ」なら「mofusandut」。"""
    name = NAME_NOISE_RE.sub("", name or "").strip()
    # UTは「作品名 UT/形」の形。記事の見出しには形まで書かれないので作品名＋UTで照らす。
    if "UT" in name and "/" in name:
        name = name.split("/")[0]
    return normalize(name)


# --------------------------------------------------------------------------
# 商品一覧
# --------------------------------------------------------------------------

def fetch_catalog() -> dict[str, dict]:
    """全商品を読み、「ブランド:商品ID」をキーにした辞書を返す。"""
    products: dict[str, dict] = {}
    for store in STORES:
        for gender_id, gender in store["genders"]:
            offset, total = 0, None
            while total is None or offset < total:
                url = (
                    f"{store['base']}/api/commerce/v5/ja/products"
                    f"?path={gender_id}&limit={PAGE_SIZE}&offset={offset}&httpFailure=true"
                )
                data = _get_json(url)["result"]
                total = data["pagination"]["total"]
                items = data.get("items") or []
                if not items:
                    break
                for item in items:
                    add_product(products, store, gender, item)
                offset += len(items)
                time.sleep(0.3)  # 公式サイトに負荷をかけない
            print(f"  ○ {store['brand']} {gender}（{gender_id}）: {total}件")
    return products


def add_product(products: dict, store: dict, gender: str, item: dict) -> None:
    pid = item.get("productId")
    if not pid:
        return
    key = f"{store['key']}:{pid}"
    if key in products:
        # 男女兼用の商品はウィメンズとメンズの両方に出てくる。
        if gender not in products[key]["genders"]:
            products[key]["genders"].append(gender)
        return

    rating = item.get("rating") or {}
    prices = item.get("prices") or {}
    base_price = ((prices.get("base") or {}).get("value"))
    promo_price = ((prices.get("promo") or {}) or {}).get("value") if prices.get("promo") else None
    color = item.get("representativeColorDisplayCode") or ""
    main_images = ((item.get("images") or {}).get("main") or {})
    image = ((main_images.get(color) or {}).get("image")
             or next((v.get("image") for v in main_images.values() if v.get("image")), ""))
    flags = ((item.get("representative") or {}).get("flags") or {})
    flag_codes = {f.get("code") for f in (flags.get("productFlags") or []) + (flags.get("priceFlags") or [])}
    price_group = item.get("priceGroup") or "00"
    url = f"{store['base']}/ja/products/{pid}/{price_group}"
    if color:
        url += f"?colorDisplayCode={color}"

    products[key] = {
        "key": key,
        "brand": store["brand"],
        "base": store["base"],
        "pid": pid,
        "name": item.get("name", ""),
        "genders": [gender],
        "price": base_price,
        "promo": promo_price,
        "image": image,
        "url": url,
        "rating": rating.get("average") or 0,
        "reviews": int(rating.get("count") or 0),
        "new": "salesStart" in flag_codes,
    }


# --------------------------------------------------------------------------
# レビュー数の控え
# --------------------------------------------------------------------------

def load_history() -> dict:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("first_day", "")
    data.setdefault("counts", {})
    return data


def save_history(history: dict) -> None:
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        # 行数が多いので字下げしない（差分は1行ずつにならなくても困らない）。
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def record_today(history: dict, products: dict[str, dict], day: str) -> None:
    """今日のレビュー総数を控える。同じ日に何度走っても最後の値で上書き。"""
    if not history["first_day"]:
        history["first_day"] = day
    counts = history["counts"]
    for key, product in products.items():
        if product["reviews"] < MIN_REVIEWS_TO_TRACK and key not in counts:
            continue
        counts.setdefault(key, {})[day] = product["reviews"]

    # 古い日付と、一覧から消えた商品を片付ける。
    oldest = days_ago(day, HISTORY_KEEP_DAYS)
    for key in list(counts):
        points = {d: c for d, c in counts[key].items() if d >= oldest}
        if key not in products and not any(d >= days_ago(day, 7) for d in points):
            points = {}
        if points:
            counts[key] = points
        else:
            del counts[key]


def count_at(points: dict[str, int], day: str) -> int | None:
    """day 以前でいちばん新しい控えの値。無ければ None。"""
    older = [d for d in points if d <= day]
    return points[max(older)] if older else None


def recent_review_count(store_base: str, pid: str) -> tuple[int, bool]:
    """レビューを新しい順に読み、直近7日に投稿された件数を数える。

    戻り値は（件数, 上限に達したか）。上限に達したら実際はもっと多い。
    """
    cutoff = time.time() - 7 * 86400
    found = 0
    for page in range(REVIEW_MAX_PAGES):
        url = (
            f"{store_base}/api/commerce/v5/ja/products/{urllib.parse.quote(pid)}/reviews"
            f"?limit={REVIEW_PAGE}&offset={page * REVIEW_PAGE}&sort=submission_time&httpFailure=true"
        )
        reviews = (_get_json(url, timeout=20).get("result") or {}).get("reviews") or []
        fresh = [r for r in reviews if (r.get("createDate") or 0) >= cutoff]
        found += len(fresh)
        if len(fresh) < len(reviews) or len(reviews) < REVIEW_PAGE:
            return found, False
        time.sleep(0.2)
    return found, True


def product_number(product: dict) -> int:
    """商品番号（E493455-000 の 493455）。発売が新しいほど大きい。"""
    digits = re.sub(r"\D", "", product["pid"].split("-")[0])
    return int(digits) if digits else 0


def bootstrap_baselines(history: dict, products: dict[str, dict], day: str) -> None:
    """控えが7日分たまるまでのつなぎ。上位の商品だけ7日前の件数を逆算して控えに入れる。

    7日前の控えがすでにある商品と、控えを取り始めてから新しく出た商品（7日前は0件とみなせる）は
    読みに行かない。だから動かし始めて1週間たてば、ここでの問い合わせは自然に0になる。
    """
    week_ago = days_ago(day, 7)
    counts = history["counts"]
    history_is_old_enough = history["first_day"] and history["first_day"] <= week_ago

    groups: dict[tuple[str, str], list[dict]] = {}
    for key, product in products.items():
        points = counts.get(key, {})
        if count_at(points, week_ago) is not None:
            continue
        if history_is_old_enough:
            continue  # 7日前の一覧に無かった＝新しく出た商品。基準は0でよい
        if product["reviews"] < 3:
            continue
        for gender in product["genders"]:
            groups.setdefault((product["brand"], gender), []).append(product)

    targets: dict[str, dict] = {}
    # 見出しに出てきた商品は、レビューの多少にかかわらず必ず数える（まさに話題の商品なので）。
    for product in products.values():
        if product.get("mentions7") and count_at(counts.get(product["key"], {}), week_ago) is None:
            targets[product["key"]] = product
    for members in groups.values():
        members.sort(key=lambda p: p["reviews"], reverse=True)
        for product in members[:BOOTSTRAP_TOP_REVIEWED]:
            targets[product["key"]] = product
        members.sort(key=product_number, reverse=True)
        for product in members[:BOOTSTRAP_NEWEST]:
            targets[product["key"]] = product
    if not targets:
        return

    print(f"  7日前のレビュー数を逆算（{len(targets)}商品。控えがたまれば不要になる）")

    def work(product: dict):
        try:
            return product, *recent_review_count(product["base"], product["pid"])
        except Exception:  # noqa: BLE001  1件取れなくても全体は続ける
            return product, None, False

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for product, recent, capped in pool.map(work, targets.values()):
            if recent is None:
                continue
            points = counts.setdefault(product["key"], {})
            points[week_ago] = max(0, product["reviews"] - recent)
            if capped:
                product["capped"] = True
            done += 1
    print(f"  逆算できたもの {done}件")


# --------------------------------------------------------------------------
# 見出しでの言及
# --------------------------------------------------------------------------

def update_mentions(fetched: list[dict]) -> list[dict]:
    """今回RSSで拾った見出しを7日ぶん控えて返す。

    ニュースとして載せなかった記事（「〇選」のようなおすすめ紹介も含む）も数える。
    記事として読む価値は薄くても、「世の中がその商品を取り上げている」という点では立派な反応なので。
    """
    try:
        with open(MENTIONS_PATH, encoding="utf-8") as f:
            stored = json.load(f).get("items", {})
    except (OSError, json.JSONDecodeError):
        stored = {}

    for item in fetched:
        title = item.get("title_original", "")
        if not title or PHOTO_TITLE_RE.search(title):
            continue
        stored.setdefault(item["id"], {
            "t": title,
            "s": item.get("source", ""),
            "u": item.get("url", ""),
            "p": item.get("published", ""),
        })

    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=MENTION_DAYS)).isoformat()
    stored = {k: v for k, v in stored.items() if v.get("p", "") >= cutoff}
    with open(MENTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump({"items": stored}, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    return list(stored.values())


GU_WORD_RE = re.compile(r"ジーユー|(?<![A-Za-z])GU(?![A-Za-z])")


def mentions_brand(brand: str, title: str) -> bool:
    if brand == "GU":
        return bool(GU_WORD_RE.search(title))
    return "ユニクロ" in title or "uniqlo" in title.lower()


def match_mentions(products: list[dict], mentions: list[dict]) -> None:
    """商品名の芯が見出しに入っている回数を数える。同じ話題の転載は見出しで1本に数える。"""
    titles = []
    seen = set()
    for mention in mentions:
        norm = normalize(mention["t"])
        if norm in seen:
            continue
        seen.add(norm)
        titles.append((norm, mention))

    for product in products:
        core = core_name(product["name"])
        # 「ソックス」「パンツ」のような短い一般名詞は、どの記事にも当たってしまうので数えない。
        if len(core) < 6:
            product["mentions7"], product["mention_titles"] = 0, []
            continue
        # 同じ名前の商品が他社にもある（「スウェットパーカ」など）ので、ブランド名も見出しに要る。
        hits = [
            m for norm, m in titles
            if core in norm and mentions_brand(product["brand"], m["t"])
        ]
        hits.sort(key=lambda m: m.get("p", ""), reverse=True)
        product["mentions7"] = len(hits)
        product["mention_titles"] = [{"t": m["t"], "s": m["s"], "u": m["u"]} for m in hits[:3]]


# --------------------------------------------------------------------------
# 点数と書き出し
# --------------------------------------------------------------------------

CRITERIA = {
    "title": "BuZZの判断基準",
    "formula": f"点数 ＝ 直近7日のレビュー増加数 × 勢い ＋ 見出しに出た回数 × {MENTION_WEIGHT}",
    "points": [
        "投稿数：ユニクロ・GU公式サイトの商品レビューが、直近7日で何件増えたか。実際に買った人しか書けないので、いちばん確かな「反応」として使っています。",
        f"反応数：同じ7日間に、ニュースやブログの見出しでその商品が何回取り上げられたか。1回をレビュー{MENTION_WEIGHT}件ぶんとして足します。",
        "勢い：レビュー総数のうち、この7日間に付いた割合が大きいほど点数を上げます（最大3倍）。定番品が毎週たくさんレビューされるだけで上位を占めないようにするためです。",
        f"ウィメンズ・メンズは{MIN_SCORE['ウィメンズ']}点以上、キッズ（ベビーを含む）はレビューが少ないので{MIN_SCORE['キッズ']}点以上の商品を載せています。",
        "🔥急上昇は、前の週よりレビューの増え方が2倍以上になった商品。NEWは公式サイトで「新作」の表示が付いている商品です。",
        "X（旧Twitter）やInstagramの「いいね」数は、公式に取得できる手段が無いため使っていません。",
    ],
}


def score_products(products: dict[str, dict], history: dict, day: str) -> list[dict]:
    week_ago, two_weeks_ago = days_ago(day, 7), days_ago(day, 14)
    counts = history["counts"]
    history_is_old_enough = history["first_day"] and history["first_day"] <= week_ago
    scored = []
    for key, product in products.items():
        points = counts.get(key, {})
        base = count_at(points, week_ago)
        if base is None:
            if not history_is_old_enough:
                continue  # まだ比べる材料が無い
            base = 0      # 7日前の一覧に無かった新しい商品
        reviews7 = max(0, product["reviews"] - base)
        prev_base = count_at(points, two_weeks_ago)
        prev7 = base - prev_base if prev_base is not None else None
        product["reviews7"] = reviews7
        product["prev7"] = prev7
        product["rising"] = bool(prev7 is not None and reviews7 >= 10 and reviews7 >= 2 * max(prev7, 1))
        scored.append(product)
    return scored


def build_buzz(fetched: list[dict]) -> dict | None:
    """BuZZ を計算して docs/buzz.json に書き出す。取れなければ None（前回の結果を残す）。"""
    day = today_jst()
    print("■ BuZZ（話題の商品）")
    try:
        products = fetch_catalog()
    except Exception as exc:  # noqa: BLE001  商品一覧が取れない回は前回のまま
        print(f"  × 商品一覧を取得できませんでした（前回のBuZZを残します）: {type(exc).__name__}: {exc}")
        return None
    if len(products) < 200:
        print(f"  × 商品が{len(products)}件しか取れませんでした（前回のBuZZを残します）")
        return None

    # 見出しの照合を先に済ませておく。見出しに出た商品は、下の逆算の対象に必ず入れる。
    mentions = update_mentions(fetched)
    match_mentions(list(products.values()), mentions)

    history = load_history()
    record_today(history, products, day)
    bootstrap_baselines(history, products, day)
    save_history(history)

    scored = score_products(products, history, day)
    for product in scored:
        share = product["reviews7"] / product["reviews"] if product["reviews"] else 0
        product["momentum"] = round(1 + 2 * min(share, 1), 2)
        product["score"] = round(
            product["reviews7"] * product["momentum"] + MENTION_WEIGHT * product["mentions7"]
        )

    ranked = [
        p for p in scored
        if any(p["score"] >= MIN_SCORE[g] for g in p["genders"])
    ]
    ranked.sort(key=lambda p: (p["score"], p["reviews7"]), reverse=True)

    # ブランド×性別ごとに上位だけ残す（男女兼用は両方に数える）。同じ名前の色違いは1つにまとめる。
    picked: dict[str, dict] = {}
    per_group: dict[tuple[str, str], int] = {}
    seen_names: set[tuple[str, str]] = set()
    for product in ranked:
        name_key = (product["brand"], normalize(product["name"]))
        if name_key in seen_names:
            continue
        room = [
            g for g in product["genders"]
            if product["score"] >= MIN_SCORE[g] and per_group.get((product["brand"], g), 0) < TOP_PER_GROUP
        ]
        if not room:
            continue
        seen_names.add(name_key)
        for g in room:
            per_group[(product["brand"], g)] = per_group.get((product["brand"], g), 0) + 1
        picked[product["key"]] = product

    items = []
    for rank, product in enumerate(sorted(picked.values(), key=lambda p: p["score"], reverse=True), start=1):
        items.append({
            "key": product["key"],
            "rank": rank,
            "brand": product["brand"],
            "name": product["name"],
            "genders": product["genders"],
            "price": product["price"],
            "promo": product["promo"],
            "image": product["image"],
            "url": product["url"],
            "rating": product["rating"],
            "reviews": product["reviews"],
            "reviews7": product["reviews7"],
            "capped": bool(product.get("capped")),
            "prev7": product["prev7"],
            "mentions7": product["mentions7"],
            "mention_titles": product["mention_titles"],
            "score": product["score"],
            "momentum": product["momentum"],
            "new": product["new"],
            "rising": product["rising"],
        })

    tracked_days = len({d for points in history["counts"].values() for d in points})
    payload = {
        "updated_at": dt.datetime.now(JST).isoformat(),
        "criteria": CRITERIA,
        "genders": GENDERS,
        "history_since": history["first_day"],
        "history_days": tracked_days,
        "catalog_size": len(products),
        "count": len(items),
        "items": items,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    by_gender = {g: sum(1 for i in items if g in i["genders"]) for g in GENDERS}
    print(f"  商品 {len(products)}件を確認 / BuZZ {len(items)}件 {by_gender}")
    return payload
