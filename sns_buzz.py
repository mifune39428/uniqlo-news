"""SNS（X）で話題になっているユニクロ・GUの商品（🔥BuZZ）を数字で選ぶ。

X の公式APIは有料なので、X の投稿を検索できる Yahoo!リアルタイム検索を使う。
検索結果のページには、投稿ごとの本文・日時・いいね・リポスト・返信・引用の数が
（画面の裏のデータとして）入っているので、それを読む。APIキーは要らない。

見せ方は2つ:

  🚀 SNS急上昇 … ここ3日で投稿が急に増え、反応（いいね・リポスト）も大きい商品
  🔁 継続人気   … ここ2週間、ほぼ毎日投稿され続けている商品

数え方:
1. 見つける … 「ユニクロ」「GU 購入品」などの検索を「話題順」で読み、反応の大きい投稿を集める。
   投稿からLLMで商品名を抜き出す（「天才パンツ」のような呼び名もここで拾う）。
2. 数える   … 見つけた商品ごとに「ユニクロ 商品名」で新しい順に検索し、日ごとの投稿数と反応数を数える。
   一度見つけた商品は14日間追いかける（継続人気を判断するため）。
3. 点を付ける
   急上昇点 = （直近3日の投稿数 × 20 ＋ 直近3日の反応数）× 伸び率
   伸び率   = 直近3日の1日あたり投稿数 ÷ それより前（4〜14日前）の1日あたり投稿数（0.5〜5倍）
   継続点   = （1日あたり投稿数 × 20 ＋ 1日あたり反応数）× 14
   継続人気に入るのは、数えた期間（7日以上）の6割以上の日に投稿がある商品だけ。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.parse
import urllib.request

import llm_providers
import review_rank

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "buzz.json")
HISTORY_PATH = os.path.join(BASE_DIR, "docs", "buzz_history.json")

USER_AGENT = review_rank.USER_AGENT
JST = dt.timezone(dt.timedelta(hours=9))
SEARCH_URL = "https://search.yahoo.co.jp/realtime/search"
PAGINATION_URL = "https://search.yahoo.co.jp/realtime/api/v1/pagination"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# 話題の投稿を集める検索。「話題順」で上位40件ずつ読む。
# 性別の偏りを減らすため、メンズ・キッズの検索も入れる。
DISCOVERY_QUERIES = [
    "ユニクロ", "UNIQLO", "GU", "ジーユー",
    "ユニクロ 購入品", "GU 購入品", "ユニクロ 新作", "GU 新作",
    "ユニクロ メンズ", "GU メンズ", "ユニクロ キッズ", "GU キッズ",
]
DISCOVERY_DAYS = 7
MIN_DISCOVERY_REACTIONS = 20   # これより反応の少ない投稿は商品名を抜き出さない（LLMの枠の節約）
EXTRACT_BATCH = 25
MAX_EXTRACT_POSTS = 150

TRACK_DAYS = 14                # 一度見つけた商品を追いかける日数
MAX_TRACKED = 60               # 1回に数える商品数の上限（Yahoo!への問い合わせを抑える）
COUNT_MAX_PAGES = 4            # 1商品あたりの検索ページ数（最初が40件、以降10件ずつ）
REQUEST_GAP = 0.8              # 検索の間隔（秒）

RECENT_DAYS = 3
POST_WEIGHT = 20               # 投稿1件 ＝ 反応20件ぶん
GROWTH_MIN, GROWTH_MAX = 0.5, 5.0
RISING_MIN_POSTS = 3
RISING_MIN_GROWTH = 1.3
STEADY_MIN_DAYS = 7
STEADY_ACTIVE_RATIO = 0.6
TOP_PER_GENDER = 10
RISING_MIN_TOP_REACTIONS = 50  # 3日以内に、これだけ反応の付いた投稿が1つは要る
# 1回の集計にかける時間の上限。Yahoo!やLLMが重い日に、更新そのものを止めないための蓋。
# 手元でLLMの返事が遅い日に、全体で63分かかったことがある。
TOTAL_BUDGET = 20 * 60
COUNT_MIN_BUDGET = 3 * 60   # 数える時間は最低これだけ残す
GENDERS = review_rank.GENDERS

# 商品ではなく「種類」を指す言葉。これだけの名前は、検索しても他の商品の投稿まで数えてしまう
# （実際に「カーディガン」「タイツ」「ベルト」で数えたら、どれも1日13.8件で並んで意味が無かった）。
GENERIC_NAMES = {
    "tシャツ", "シャツ", "ロンt", "カットソー", "ニット", "セーター", "カーディガン", "パーカ",
    "パーカー", "スウェット", "トレーナー", "ブラウス", "ワンピース", "スカート", "パンツ",
    "ズボン", "ジーンズ", "デニム", "レギンス", "タイツ", "ソックス", "靴下", "インナー",
    "下着", "ブラトップ", "コート", "ジャケット", "ブルゾン", "ダウン", "アウター", "バッグ",
    "ベルト", "帽子", "キャップ", "マフラー", "手袋", "パジャマ", "ルームウェア", "ut",
    "エアリズム", "ヒートテック", "フリース", "ポロシャツ", "ベスト", "スーツ", "水着",
}


class YahooBlocked(Exception):
    pass


# --------------------------------------------------------------------------
# Yahoo!リアルタイム検索
# --------------------------------------------------------------------------

def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="ignore")


def search_page(query: str, popular: bool = False) -> dict:
    """検索結果の1ページ目（40件）。画面に埋め込まれたデータ（__NEXT_DATA__）を読む。"""
    params = {"p": query, "ei": "UTF-8"}
    if popular:
        params["md"] = "h"   # 話題順
    html = _fetch(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}")
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise YahooBlocked("検索結果のデータが見つかりません（画面の作りが変わったか、弾かれた）")
    return json.loads(match.group(1))["props"]["pageProps"]["pageData"]


def next_page(page_data: dict, oldest_id: str, page: int) -> list[dict]:
    """2ページ目以降（10件ずつ）。画面の「もっと見る」と同じ問い合わせ。"""
    params = dict(page_data["pagination"]["params"])
    params.update({"oldestTweetId": oldest_id, "b": page})
    body = _fetch(f"{PAGINATION_URL}?{urllib.parse.urlencode(params)}")
    try:
        return (json.loads(body).get("timeline") or {}).get("entry") or []
    except json.JSONDecodeError:
        return []


def post_text(entry: dict) -> str:
    text = (entry.get("displayText") or "").replace("\tSTART\t", "").replace("\tEND\t", "")
    return re.sub(r"https?://\S+", "", re.sub(r"\s+", " ", text)).strip()


def reactions(entry: dict) -> int:
    return sum(int(entry.get(k) or 0) for k in ("likesCount", "rtCount", "qtCount", "replyCount"))


# --------------------------------------------------------------------------
# 1. 見つける
# --------------------------------------------------------------------------

def discover_posts() -> list[dict]:
    """話題順の検索から、直近7日の反応の大きい投稿を集める。"""
    cutoff = time.time() - DISCOVERY_DAYS * 86400
    posts: dict[str, dict] = {}
    failures = 0
    for query in DISCOVERY_QUERIES:
        try:
            data = search_page(query, popular=True)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  × 「{query}」: {type(exc).__name__}: {exc}")
            continue
        for entry in (data.get("timeline") or {}).get("entry") or []:
            if (entry.get("createdAt") or 0) < cutoff or entry["id"] in posts:
                continue
            posts[entry["id"]] = {
                "id": entry["id"],
                "text": post_text(entry)[:220],
                "reactions": reactions(entry),
                "at": entry.get("createdAt") or 0,
                "query": query,
            }
        time.sleep(REQUEST_GAP)
    if failures == len(DISCOVERY_QUERIES):
        raise YahooBlocked("すべての検索に失敗しました")
    found = sorted(posts.values(), key=lambda p: p["reactions"], reverse=True)
    print(f"  話題の投稿 {len(found)}件（うち反応{MIN_DISCOVERY_REACTIONS}以上 "
          f"{sum(1 for p in found if p['reactions'] >= MIN_DISCOVERY_REACTIONS)}件）")
    return found


EXTRACT_PROMPT = """X（旧Twitter）の投稿から、ユニクロ・GU（ジーユー）の具体的な商品を抜き出してください。

厳守すること:
- 投稿に書かれている商品だけ。推測で足さない。
- name は公式の商品名に近い形で、色・サイズ・価格は入れない。
  例: 「フーデッドリブT」「ブラッシュドジャージーバレルレッグパンツ」「ヒートテックタイツ」「シアードットT」
  「天才パンツ」のような呼び名は、正式名が本文から分からなければそのまま使う。
- 「UT」はコラボ名を付ける（例: 「ちいかわ UT」）。
- **「カーディガン」「パンツ」「タイツ」のような種類だけの言葉は商品名にしない。**
  投稿から商品を特定できる語（素材・形・シリーズ名・コラボ名）が付いた名前だけを返す。
  例: 「カーディガン」→ ✕ / 「ソフトランベリーVネックカーディガン」→ ○
  特定できなければ、その投稿は空配列にする。
- brand は "ユニクロ" か "GU"。UNIQLO : C・ユニクロ ユー は "ユニクロ"。
- gender は "ウィメンズ" "メンズ" "キッズ" "不明" のどれか。キッズ・ベビー・子供服は "キッズ"。
- ブランド全体の話、店舗・セール全般の話、他社との比較だけ、コーデの総評だけで
  商品が特定できない投稿は空配列にする。
- 出力はJSON配列のみ。要素数は入力と同じ{count}件、iは入力の番号。前置き・コードフェンスを付けない。

出力形式:
[{{"i":1,"products":[{{"brand":"ユニクロ","name":"フーデッドリブT","gender":"ウィメンズ"}}]}}]

投稿:
{posts}
"""


def parse_extract(text: str, expected: int) -> list[dict]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        raise llm_providers.ResponseInvalid("JSON配列が見つかりません")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise llm_providers.ResponseInvalid(f"JSONとして読めません: {exc}") from exc
    if not isinstance(data, list) or len(data) != expected:
        raise llm_providers.ResponseInvalid(f"{expected}件のはずが{len(data) if isinstance(data, list) else '?'}件です")
    return data


def extract_products(posts: list[dict], deadline: float) -> list[dict]:
    """投稿から商品名を抜き出し、投稿ごとの反応数を商品に積み上げる。"""
    targets = [p for p in posts if p["reactions"] >= MIN_DISCOVERY_REACTIONS][:MAX_EXTRACT_POSTS]
    mentions: list[dict] = []
    for offset in range(0, len(targets), EXTRACT_BATCH):
        # 数える時間が残らなくなるくらい遅い日は、抜き出しを途中で切り上げる。
        if time.time() > deadline - COUNT_MIN_BUDGET:
            print(f"  ・時間切れのため{offset}件までで抜き出しを打ち切り")
            break
        batch = targets[offset : offset + EXTRACT_BATCH]
        prompt = EXTRACT_PROMPT.format(
            count=len(batch),
            posts="\n".join(f"[{n}] {p['text']}" for n, p in enumerate(batch, start=1)),
        )
        try:
            raw = llm_providers.generate_text(prompt, validate=lambda t, n=len(batch): parse_extract(t, n))
            rows = parse_extract(raw, len(batch))
        except llm_providers.LLMError as exc:
            print(f"  × 商品名の抜き出しに失敗（この{len(batch)}件は飛ばします）: {exc}")
            continue
        for n, row in enumerate(rows, start=1):
            try:
                post = batch[int(row.get("i", n)) - 1]
            except (TypeError, ValueError, IndexError):
                post = batch[n - 1]
            for product in row.get("products") or []:
                name = str(product.get("name", "")).strip()
                brand = "GU" if str(product.get("brand", "")).strip().upper() in ("GU", "ジーユー") else "ユニクロ"
                gender = str(product.get("gender", "")).strip()
                norm = review_rank.normalize(name)
                # 「種類」だけの言葉は、検索しても他の商品まで数えてしまうので捨てる。
                if len(norm) < 4 or norm in GENERIC_NAMES:
                    continue
                mentions.append({
                    "brand": brand,
                    "name": name,
                    "gender": gender if gender in GENDERS else "",
                    "reactions": post["reactions"],
                })
    print(f"  商品名を抜き出した投稿 {len(targets)}件 → 商品の言及 {len(mentions)}件")
    return mentions


# --------------------------------------------------------------------------
# 商品の同定（呼び名をまとめ、公式の商品一覧と結び付ける）
# --------------------------------------------------------------------------

def product_key(brand: str, name: str) -> str:
    return f"{brand}:{review_rank.normalize(name)}"


def merge_names(mentions: list[dict], history: dict) -> list[dict]:
    """表記ゆれをまとめる。「バレルレッグパンツ」と「ブラッシュドジャージーバレルレッグパンツ」は同じ商品。

    短いほうの名前が長いほうに含まれていれば同じとみなし、短いほう（検索に強い）を代表にする。
    すでに追いかけている商品の名前があれば、そちらに寄せる（日ごとの記録を途切れさせない）。
    """
    groups: dict[str, dict] = {}
    known = {key: item for key, item in history.get("products", {}).items()}

    def find_group(brand: str, norm: str) -> str | None:
        for key, group in list(groups.items()) + [(k, v) for k, v in known.items() if k not in groups]:
            if group["brand"] != brand:
                continue
            other = review_rank.normalize(group["name"])
            if norm == other or (min(len(norm), len(other)) >= 5 and (norm in other or other in norm)):
                return key
        return None

    for mention in sorted(mentions, key=lambda m: len(m["name"])):
        norm = review_rank.normalize(mention["name"])
        key = find_group(mention["brand"], norm)
        if key is None:
            key = product_key(mention["brand"], mention["name"])
        group = groups.setdefault(key, {
            "key": key,
            "brand": mention["brand"],
            "name": known.get(key, {}).get("name", mention["name"]),
            "genders": set(known.get(key, {}).get("genders", [])),
            "discovery": 0,
        })
        if mention["gender"]:
            group["genders"].add(mention["gender"])
        group["discovery"] += mention["reactions"]
    return list(groups.values())


def match_catalog(group: dict, catalog: dict[str, dict] | None) -> dict | None:
    """公式の商品一覧から同じ商品を探す（画像・価格・リンクに使う）。レビューの多いものを優先。"""
    if not catalog:
        return None
    norm = review_rank.normalize(group["name"])
    if len(norm) < 4:
        return None
    best = None
    for product in catalog.values():
        if product["brand"] != group["brand"]:
            continue
        core = review_rank.core_name(product["name"])
        full = review_rank.normalize(product["name"])
        if norm == core or norm == full or (len(norm) >= 5 and norm in full) or (len(core) >= 5 and core in norm):
            if best is None or product["reviews"] > best["reviews"]:
                best = product
    return best


# --------------------------------------------------------------------------
# 2. 数える
# --------------------------------------------------------------------------

def search_query(group: dict) -> str:
    brand = "GU" if group["brand"] == "GU" else "ユニクロ"
    return f"{brand} {group['name']}"


def count_posts(group: dict) -> dict:
    """「ユニクロ 商品名」を新しい順に読み、日ごとの投稿数と反応数を数える。"""
    cutoff = time.time() - TRACK_DAYS * 86400
    data = search_page(search_query(group))
    entries = list((data.get("timeline") or {}).get("entry") or [])
    oldest_id = (data.get("timelineSettings") or {}).get("oldestTweetId") or (entries[-1]["id"] if entries else "")
    total = ((data.get("timeline") or {}).get("head") or {}).get("totalResultsAvailable") or len(entries)
    page = 1
    # 14日前まで届いていなければ、もう少し読む（ページの上限まで）。
    while entries and entries[-1].get("createdAt", 0) >= cutoff and page < COUNT_MAX_PAGES and len(entries) < total:
        time.sleep(REQUEST_GAP)
        more = next_page(data, oldest_id, page)
        if not more:
            break
        entries.extend(more)
        oldest_id = more[-1]["id"]
        page += 1

    seen, days_posts, days_reactions = set(), {}, {}
    top_reactions = 0
    for entry in entries:
        at = entry.get("createdAt") or 0
        if at < cutoff or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        day = dt.datetime.fromtimestamp(at, JST).strftime("%Y-%m-%d")
        days_posts[day] = days_posts.get(day, 0) + 1
        days_reactions[day] = days_reactions.get(day, 0) + reactions(entry)
        top_reactions = max(top_reactions, reactions(entry))
    oldest_at = min((e.get("createdAt") or 0) for e in entries) if entries else time.time()
    # 数え切れた範囲（ページの上限で14日前まで届かなかったときは、届いた日まで）。
    # 全件読めた（それより古い投稿は無い）か、14日前まで届いたなら14日ぶん数えたことになる。
    complete = len(entries) >= total or oldest_at < cutoff
    covered_from = cutoff if complete else oldest_at
    return {
        "posts": days_posts,
        "reactions": days_reactions,
        "covered_from": dt.datetime.fromtimestamp(covered_from, JST).strftime("%Y-%m-%d"),
        "top": top_reactions,
        # ページの上限で読み切れなかった＝実際の投稿数はこれより多い。
        "capped": not complete,
    }


def load_history() -> dict:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("products", {})
    return data


def save_history(history: dict) -> None:
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


# --------------------------------------------------------------------------
# 3. 点を付ける
# --------------------------------------------------------------------------

CRITERIA = {
    "title": "BuZZの判断基準",
    "points": [
        "X（旧Twitter）の投稿を、Yahoo!リアルタイム検索で数えています。数えるのは投稿数と、いいね・リポスト・引用・返信を足した「反応数」です。",
        "まず「ユニクロ」「GU 購入品」などの話題の投稿から、話題になっている商品を見つけます。次に商品ごとに検索して、日ごとの投稿数と反応数を数えます。",
        f"🚀 SNS急上昇：直近{RECENT_DAYS}日の投稿が、それまでより増えている商品。点数 ＝（直近{RECENT_DAYS}日の投稿数×{POST_WEIGHT} ＋ 反応数）× 伸び率。伸び率は、直近{RECENT_DAYS}日とそれより前の、1日あたり投稿数の比です（{GROWTH_MIN}〜{GROWTH_MAX:g}倍）。反応が{RISING_MIN_TOP_REACTIONS}以上付いた投稿が1つも無い商品は入れません。",
        f"🔁 継続人気：ここ{TRACK_DAYS}日のうち6割以上の日に投稿がある商品。点数 ＝（1日あたりの投稿数×{POST_WEIGHT} ＋ 1日あたりの反応数）×{TRACK_DAYS}。",
        "Instagram・TikTokは、外から数を取得する手段が無いため入れていません。Yahoo!リアルタイム検索はXの投稿をすべて集めているわけではないので、数は実際の一部です（商品どうしの比較に使っています）。",
        "「カーディガン」「パンツ」のような種類だけの言葉は、検索しても他の商品の投稿まで数えてしまうので、商品として扱いません。",
        "「≧」が付いた数は、読み取れるページの上限に達したという意味で、実際の投稿はそれより多いことを示します。",
        "買った人のレビューの伸びは「⭐ レビュー」で見られます。",
    ],
}


def daily_rate(values: dict[str, int], start: str, end: str) -> tuple[float, int]:
    """start〜end（両端含む）の1日あたりの値と、その日数。"""
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    days = (last - first).days + 1
    if days <= 0:
        return 0.0, 0
    total = sum(v for d, v in values.items() if start <= d <= end)
    return total / days, days


def score(record: dict, today: str) -> dict:
    posts, reacts = record["posts"], record["reactions"]
    recent_start = review_rank.days_ago(today, RECENT_DAYS - 1)
    before_end = review_rank.days_ago(today, RECENT_DAYS)
    covered_from = max(record["covered_from"], review_rank.days_ago(today, TRACK_DAYS - 1))

    recent_rate, _ = daily_rate(posts, recent_start, today)
    recent_posts = sum(v for d, v in posts.items() if d >= recent_start)
    recent_reacts = sum(v for d, v in reacts.items() if d >= recent_start)
    if covered_from <= before_end:
        before_rate, _ = daily_rate(posts, covered_from, before_end)
        growth = recent_rate / max(before_rate, 0.2)
    else:
        growth = None   # 直近3日より前が数えられていない（投稿が多すぎてページが足りない）
    covered_days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(covered_from)).days + 1
    active_days = sum(1 for d, v in posts.items() if d >= covered_from and v)
    all_posts = sum(v for d, v in posts.items() if d >= covered_from)
    all_reacts = sum(v for d, v in reacts.items() if d >= covered_from)
    per_day_posts = all_posts / max(covered_days, 1)
    per_day_reacts = all_reacts / max(covered_days, 1)

    capped = min(max(growth, GROWTH_MIN), GROWTH_MAX) if growth is not None else 1.0
    return {
        "recent_posts": recent_posts,
        "recent_reactions": recent_reacts,
        "growth": round(growth, 2) if growth is not None else None,
        "rising_score": round((recent_posts * POST_WEIGHT + recent_reacts) * capped),
        "is_rising": growth is not None and recent_posts >= RISING_MIN_POSTS and growth >= RISING_MIN_GROWTH,
        "covered_days": covered_days,
        "active_days": active_days,
        "posts_per_day": round(per_day_posts, 1),
        "reactions_per_day": round(per_day_reacts),
        "steady_score": round((per_day_posts * POST_WEIGHT + per_day_reacts) * TRACK_DAYS),
        # ページの上限で14日前まで届かないほど投稿が多い商品は、毎日投稿があるとみなしてよい。
        "is_steady": (covered_days >= STEADY_MIN_DAYS and active_days / covered_days >= STEADY_ACTIVE_RATIO)
                     or (covered_days < STEADY_MIN_DAYS and per_day_posts >= 5),
    }


def daily_series(posts: dict[str, int], today: str, days: int = TRACK_DAYS) -> list[int]:
    return [posts.get(review_rank.days_ago(today, n), 0) for n in range(days - 1, -1, -1)]


# --------------------------------------------------------------------------
# まとめ
# --------------------------------------------------------------------------

def build_sns_buzz(catalog: dict[str, dict] | None) -> dict | None:
    """BuZZ を計算して docs/buzz.json に書き出す。Yahoo!に弾かれた回は None（前回の結果を残す）。"""
    print("■ 🔥BuZZ（SNSの急上昇・継続人気）")
    deadline = time.time() + TOTAL_BUDGET
    today = dt.datetime.now(JST).strftime("%Y-%m-%d")
    history = load_history()
    try:
        posts = discover_posts()
    except YahooBlocked as exc:
        print(f"  × Yahoo!リアルタイム検索を読めませんでした（前回のBuZZを残します）: {exc}")
        return None

    groups = merge_names(extract_products(posts, deadline), history)

    # 今回見つけたものと、この14日で見つけて追いかけているものを合わせて数える。
    tracked = history["products"]
    cutoff = review_rank.days_ago(today, TRACK_DAYS)
    for key in [k for k, v in tracked.items() if v.get("last_found", "") < cutoff]:
        del tracked[key]
    for group in groups:
        entry = tracked.setdefault(group["key"], {"brand": group["brand"], "name": group["name"], "genders": []})
        entry["genders"] = sorted(set(entry["genders"]) | group["genders"])
        entry["last_found"] = today
        entry["discovery"] = group["discovery"]
    order = sorted(tracked.items(), key=lambda kv: (kv[1].get("last_found") == today, kv[1].get("discovery", 0)),
                   reverse=True)[:MAX_TRACKED]

    print(f"  商品ごとに投稿を数える（{len(order)}商品）")
    results = []
    blocked = 0
    for key, entry in order:
        if time.time() > deadline:
            print(f"  ・時間切れのため{len(results)}商品で打ち切り（残りは次回）")
            break
        group = {"key": key, **entry}
        try:
            record = count_posts(group)
        except Exception as exc:  # noqa: BLE001  1商品数えられなくても続ける
            blocked += 1
            if blocked >= 5 and not results:
                print(f"  × 検索が続けて失敗しました（前回のBuZZを残します）: {exc}")
                return None
            continue
        time.sleep(REQUEST_GAP)
        # 日ごとの数は前回までの記録と合わせる（新しい順に読むので、前回のほうが古い日を知っていることがある）。
        for field in ("posts", "reactions"):
            merged = dict(entry.get(field, {}))
            for day, value in record[field].items():
                merged[day] = max(merged.get(day, 0), value)
            entry[field] = {d: v for d, v in merged.items() if d > cutoff}
            record[field] = entry[field]
        entry["covered_from"] = min(entry.get("covered_from", record["covered_from"]), record["covered_from"])
        record["covered_from"] = entry["covered_from"]
        entry["last_counted"] = today

        product = match_catalog(group, catalog)
        numbers = score(record, today)
        genders = list(entry["genders"]) or (product["genders"] if product else [])
        if product:
            genders = sorted(set(genders) | set(product["genders"]), key=GENDERS.index)
        results.append({
            "key": key,
            "brand": entry["brand"],
            "name": entry["name"],
            "official_name": product["name"] if product else "",
            "genders": [g for g in GENDERS if g in genders],
            "image": product["image"] if product else "",
            "url": product["url"] if product else "",
            "price": product["price"] if product else None,
            "promo": product["promo"] if product else None,
            "search_url": f"{SEARCH_URL}?{urllib.parse.urlencode({'p': search_query(group), 'ei': 'UTF-8', 'md': 'h'})}",
            "daily": daily_series(record["posts"], today),
            "top_reactions": record["top"],
            "capped": record["capped"],
            **numbers,
        })
    save_history(history)

    def pick(flag: str, key: str) -> list[dict]:
        chosen, per_gender = [], {}
        for item in sorted((r for r in results if r[flag]), key=lambda r: r[key], reverse=True):
            genders = item["genders"] or ["不明"]
            if all(per_gender.get(g, 0) >= TOP_PER_GENDER for g in genders):
                continue
            for g in genders:
                per_gender[g] = per_gender.get(g, 0) + 1
            chosen.append(item)
        return chosen

    # 反応の大きい投稿が1つも無いものは「急上昇」と呼ばない（ただの言及の増加なので）。
    for item in results:
        item["is_rising"] = item["is_rising"] and item["top_reactions"] >= RISING_MIN_TOP_REACTIONS
    rising = pick("is_rising", "rising_score")
    steady = pick("is_steady", "steady_score")
    payload = {
        "updated_at": dt.datetime.now(JST).isoformat(),
        "criteria": CRITERIA,
        "genders": GENDERS,
        "source": "Yahoo!リアルタイム検索（X）",
        "tracked": len(results),
        "rising": rising,
        "steady": steady,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"  🚀急上昇 {len(rising)}件 / 🔁継続人気 {len(steady)}件（{len(results)}商品を数えた）")
    return payload
