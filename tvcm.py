"""いま放映中のユニクロ・GUのテレビCMを集めて、docs/tvcm.json に書き出す。

どこから集めるか:
  1. ユニクロ公式YouTubeチャンネル … テレビCMは「2026 ヒートテック LifeとWear/〜 30秒」のような
     題名の短い動画で上がる。映像（サムネイル・再生）と、だいたいの放映開始日はここから取る。
  2. ユニクロ公式のプレスリリース … 「新TVCM〇月〇日より放映開始」と、出演者・曲が載る。
  3. このサイトが集めたニュース記事 … 「〇〇出演の新CMが放映」。GUのCMはここからしか拾えない
     （GUの公式チャンネルにはCMが上がっていない）。
  2と3は、CMに触れている文書だけをLLMに読ませて、CM名・出演者・放映開始日・終了日を抜き出す。
  一度読んだ文書は覚えておき、読み直さない。

放送期間の決め方（終了日はふつう発表されない）:
  - 発表された終了日があれば、その日まで。
  - 無ければ、CM動画の再生回数の伸びを見る。7日以上記録があって、1週間の伸びが止まっていれば
    「広告として流すのをやめた＝放送が終わった」とみなす。
  - 伸びの記録が足りないうちは、開始から8週間まで。伸び続けていても最長16週間まで。
  ここで決めた「いつまで」（until）は画面側でも見る人の日付と比べるので、
  自動更新が止まっても、期限を過ぎたCMは表示されない。

テレビCMとみなす条件:
  公式発表かニュースでテレビCMと確かめられたもの、または公式動画の再生回数が10万回以上のもの
  （大きなキャンペーンは動画広告としても流れるので、再生回数が桁違いに多い。
   ネット向けの紹介動画やインタビューは数千〜数万回にとどまる）。
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import time
import urllib.request
from difflib import SequenceMatcher

import llm_providers

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "tvcm.json")
HISTORY_PATH = os.path.join(BASE_DIR, "docs", "tvcm_history.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
JST = dt.timezone(dt.timedelta(hours=9))

CHANNELS = [{"brand": "ユニクロ", "id": "UCvm-1tfv4nk4dz_EHeRGbXw"}]
PRESS_BASE = "https://www.uniqlo.com"
PRESS_LIST = "https://www.uniqlo.com/jp/ja/contents/corp/press-release/{year}/"

MAX_SPOT_SECONDS = 125            # テレビCMは15・30・60・90・120秒
MIN_VIEWS = 100_000               # 公式の裏付けが無いときは、これだけ再生されていればCMとみなす
DEFAULT_DAYS = 56                 # 伸びの記録が足りないうちは、開始から8週間
MAX_DAYS = 112                    # 伸び続けていても最長16週間
STALL_WINDOW = 7                  # 伸びを比べる日数
STALL_GAIN = 20_000               # 1週間の伸びがこれ未満で、かつ…
STALL_RATIO = 0.01                # …1%未満なら「止まった」
KEEP_DAYS = 150                   # 記録を残す日数
MAX_WATCH_FETCH = 25              # 1回に開く動画ページの上限（新しい動画＋放映中の動画の再生回数）

# CMではない動画（インタビュー・メイキング・カタログなど）。長さが短くても外す。
NOT_SPOT_RE = re.compile(
    r"インタビュー|メイキング|magazine|カタログ|Concept Movie|コンセプトムービー|The Heart of LifeWear|"
    r"撮影|ライブ配信|LIVE|ダイジェスト|解説|ショート|#shorts", re.I)
YEAR_RE = re.compile(r"^(?:UNIQLO\s+)?(?:20\d\d|\d\d(?:FW|SS|AW))\s*", re.I)
LENGTH_RE = re.compile(r"\s*(?:\d+\s*(?:秒|s|sec))\s*$", re.I)
CM_WORD_RE = re.compile(r"TVCM|ＴＶＣＭ|テレビCM|新CM|新ＣＭ|CM放映|放映開始|オンエア|ＣＭ")


def _get(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def today_jst() -> dt.date:
    return dt.datetime.now(JST).date()


def normalize(text: str) -> str:
    return re.sub(r"[\s　・/／「」『』【】()（）\-－―‐、。！!？?〜~＆&]", "", (text or "").lower())


def is_http(url: str) -> bool:
    return isinstance(url, str) and url.startswith(("https://", "http://"))


# --------------------------------------------------------------------------
# 記録
# --------------------------------------------------------------------------

def load_history() -> dict:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("videos", {})   # 動画ID → 題名・公開日・長さ・日ごとの再生回数
    data.setdefault("docs", {})     # 読んだ文書のURL → 抜き出したCMの情報（無ければ空）
    return data


def save_history(history: dict) -> None:
    cutoff = (today_jst() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    history["videos"] = {k: v for k, v in history["videos"].items() if v.get("published", "") >= cutoff}
    history["docs"] = {k: v for k, v in history["docs"].items() if (v.get("published") or v.get("checked", "")) >= cutoff}
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


# --------------------------------------------------------------------------
# 1. 公式YouTubeチャンネル
# --------------------------------------------------------------------------

def channel_videos(channel_id: str) -> list[tuple[str, str]]:
    """チャンネルの動画一覧（新しい順に30本ほど）。（動画ID, 題名）の組を返す。"""
    page = _get(f"https://www.youtube.com/channel/{channel_id}/videos")
    match = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if not match:
        raise RuntimeError("動画一覧のデータが見つかりません（YouTubeの画面の作りが変わったかもしれません）")
    found: list[tuple[str, str]] = []

    def walk(node):
        if isinstance(node, dict):
            lockup = node.get("lockupViewModel")
            if isinstance(lockup, dict) and lockup.get("contentId"):
                meta = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
                found.append((lockup["contentId"], meta.get("title", {}).get("content", "")))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(match.group(1)))
    seen, unique = set(), []
    for vid, title in found:
        if vid not in seen:
            seen.add(vid)
            unique.append((vid, title))
    return unique


def watch_info(video_id: str) -> dict:
    """動画ページから、長さ・再生回数・公開日を読む。"""
    page = _get(f"https://www.youtube.com/watch?v={video_id}")
    def pick(key):
        found = re.search(rf'"{key}":"([^"]+)"', page)
        return found.group(1) if found else ""
    return {
        "length": int(pick("lengthSeconds") or 0),
        "views": int(pick("viewCount") or 0),
        "published": (pick("publishDate") or pick("uploadDate"))[:10],
    }


def spot_name(title: str) -> tuple[str, str]:
    """「2026 パフテック  LifeとWear/嘘だ！ 30秒」→（商品「パフテック」, CM名「LifeとWear/嘘だ！」）。"""
    body = LENGTH_RE.sub("", YEAR_RE.sub("", title.strip()))
    parts = [p for p in re.split(r"[ 　]+", body) if p]
    # 「UNIQLO KIDS スフレヤーンニット」のように、ブランド名から始まる題名は2語で商品名にする。
    if len(parts) >= 3 and parts[0].upper() in ("UNIQLO", "GU"):
        parts = [f"{parts[0]} {parts[1]}"] + parts[2:]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return body, body


def collect_videos(history: dict) -> int:
    """公式チャンネルの新しい動画を調べ、CMらしいものの再生回数を記録する。"""
    today = today_jst().isoformat()
    videos = history["videos"]
    fetched = 0
    for channel in CHANNELS:
        listing = channel_videos(channel["id"])
        for vid, title in listing:
            if vid in videos or NOT_SPOT_RE.search(title):
                continue
            if fetched >= MAX_WATCH_FETCH:
                break
            try:
                info = watch_info(vid)
            except Exception as exc:  # noqa: BLE001  1本読めなくても続ける
                print(f"  × 動画 {vid} を読めませんでした: {exc}")
                continue
            fetched += 1
            time.sleep(0.5)
            if not info["published"] or not 0 < info["length"] <= MAX_SPOT_SECONDS:
                # 長い動画はCMではない。次から読み直さないよう、印だけ残す。
                videos[vid] = {"skip": True, "title": title, "published": info["published"] or today}
                continue
            product, name = spot_name(title)
            videos[vid] = {
                "brand": channel["brand"], "title": title, "product": product, "name": name,
                "published": info["published"], "length": info["length"],
                "views": {today: info["views"]},
            }
            print(f"  ・CMらしい動画を見つけた: {title}（{info['length']}秒・{info['views']:,}回）")

    # 放映中かもしれない動画は、毎回再生回数を記録する（伸びが止まったかを見るため）。
    horizon = (today_jst() - dt.timedelta(days=MAX_DAYS)).isoformat()
    for vid, video in sorted(videos.items(), key=lambda kv: kv[1]["published"], reverse=True):
        if video.get("skip") or video["published"] < horizon or today in video["views"]:
            continue
        if fetched >= MAX_WATCH_FETCH:
            break
        try:
            video["views"][today] = watch_info(vid)["views"]
            fetched += 1
            time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            print(f"  × 動画 {vid} の再生回数を読めませんでした: {exc}")
    return fetched


# --------------------------------------------------------------------------
# 2・3. 公式リリースとニュースから「新CM」の発表を拾う
# --------------------------------------------------------------------------

def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", "\n", raw))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def press_documents(history: dict) -> list[dict]:
    """まだ読んでいない公式リリースのうち、CMに触れているもの。"""
    today = today_jst()
    years = [today.year] + ([today.year - 1] if today.month <= 4 else [])
    docs = []
    for year in years:
        try:
            listing = _get(PRESS_LIST.format(year=year))
        except Exception as exc:  # noqa: BLE001
            print(f"  × {year}年のプレスリリース一覧を読めませんでした: {exc}")
            continue
        oldest = (today - dt.timedelta(days=MAX_DAYS + 31)).strftime("%Y/%m")
        for path in sorted(set(re.findall(rf'href="(/jp/ja/contents/corp/press-release/{year}/\d\d/[^"#?]+\.html)"', listing))):
            url = PRESS_BASE + path
            # 放映中のCMに関係しうるのは最近のリリースだけ。古い月のものは読まない。
            if url in history["docs"] or path.split("press-release/")[1][:7] < oldest:
                continue
            try:
                raw = _get(url)
            except Exception as exc:  # noqa: BLE001
                print(f"  × リリースを読めませんでした: {url}（{exc}）")
                continue
            time.sleep(0.3)
            text = page_text(raw)
            date = re.search(r"(20\d\d)年(\d\d)月(\d\d)日", text)
            published = f"{date.group(1)}-{date.group(2)}-{date.group(3)}" if date else today.isoformat()
            if not CM_WORD_RE.search(text):
                history["docs"][url] = {"checked": today.isoformat(), "published": published, "cms": []}
                continue
            image = re.search(r'<img[^>]+src="(/jp/ja/contents/corp/press-release/[^"]+\.(?:png|jpe?g))"', raw)
            docs.append({
                "url": url, "kind": "公式発表", "published": published,
                "title": text.splitlines()[0][:120] if text else "",
                "text": text[:3500],
                "image": PRESS_BASE + image.group(1) if image else "",
            })
    return docs


def news_documents(items: list[dict], history: dict) -> list[dict]:
    """サイトに載せたニュースのうち、CMに触れていて、まだ読んでいないもの。"""
    docs = []
    horizon = (today_jst() - dt.timedelta(days=MAX_DAYS)).isoformat()
    for item in items:
        url = item.get("url", "")
        if url in history["docs"] or not is_http(url) or item.get("published", "")[:10] < horizon:
            continue
        hay = f"{item.get('title_ja', '')} {item.get('summary_ja', '')} {item.get('title_original', '')}"
        if not re.search(r"CM|ＣＭ|コマーシャル", hay):
            continue
        docs.append({
            "url": url, "kind": "ニュース", "published": item.get("published", "")[:10],
            "title": item.get("title_ja", ""),
            "text": f"見出し: {item.get('title_ja', '')}\n原題: {item.get('title_original', '')}\n要約: {item.get('summary_ja', '')}\n出典: {item.get('source', '')}",
            "image": item.get("image", ""),
        })
    return docs


EXTRACT_PROMPT = """次の文書が、ユニクロまたはGU（ジーユー）の新しいテレビCMの放映を伝えているか判断し、
伝えていればCMの情報を抜き出してください。今日は{today}です。

厳守すること:
- 文書に書いてあることだけ。推測で足さない。分からない項目は null（出演者は空配列）。
- テレビCMの放映（放送）を伝えていなければ "cms" は空配列。
  CMに出ている人の話題だけ、ネット動画だけ、CMの撮影の裏話だけ、他社のCM、は空配列。
- brand は "ユニクロ" か "GU"。
- product はCMで扱う商品・シリーズ（例: ヒートテック、パフテック、フリース）。分からなければ null。
- name は「」や『』で示されたCM名（例: LifeとWear/ヒートテックと生きていく）。分からなければ null。
- start / end は "YYYY-MM-DD"。年が書いていなければ今日に近い年で補う。終了日が書いていなければ end は null。
- 出力はJSONオブジェクトのみ。前置き・コードフェンスを付けない。

出力形式:
{{"cms": [{{"brand": "ユニクロ", "product": "...", "name": "...", "talents": ["..."], "song": null, "start": "2026-10-05", "end": null}}]}}

文書（{kind}・{published}）:
{text}
"""


def parse_extract(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise llm_providers.ResponseInvalid("JSONが見つかりません")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise llm_providers.ResponseInvalid(f"JSONとして読めません: {exc}") from exc
    if not isinstance(data.get("cms"), list):
        raise llm_providers.ResponseInvalid("cms がありません")
    return data


def valid_date(value) -> str:
    try:
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return ""


def read_documents(docs: list[dict], history: dict) -> None:
    """文書をLLMに読ませ、CMの情報を記録に入れる。読めなかった文書は次回に回す。"""
    today = today_jst().isoformat()
    for doc in docs:
        prompt = EXTRACT_PROMPT.format(today=today, kind=doc["kind"], published=doc["published"], text=doc["text"])
        try:
            data = parse_extract(llm_providers.generate_text(prompt, validate=parse_extract))
        except llm_providers.LLMError as exc:
            print(f"  × CM情報の読み取りに失敗（次回に回します）: {doc['title'][:40]} / {exc}")
            continue
        cms = []
        for cm in data["cms"]:
            if not isinstance(cm, dict):
                continue
            brand = "GU" if str(cm.get("brand", "")).upper() in ("GU", "ジーユー") else "ユニクロ"
            cms.append({
                "brand": brand,
                "product": str(cm.get("product") or "").strip(),
                "name": str(cm.get("name") or "").strip(),
                "talents": [str(t).strip() for t in (cm.get("talents") or []) if str(t).strip()][:6],
                "song": str(cm.get("song") or "").strip(),
                "start": valid_date(cm.get("start")) or doc["published"],
                "end": valid_date(cm.get("end")),
            })
        history["docs"][doc["url"]] = {
            "checked": today, "kind": doc["kind"], "title": doc["title"],
            "published": doc["published"], "image": doc["image"], "cms": cms,
        }
        if cms:
            print(f"  ・新CMの発表: {doc['title'][:50]}（{len(cms)}本）")


# --------------------------------------------------------------------------
# まとめる
# --------------------------------------------------------------------------

# CM名の頭に付くシリーズ名。ここが共通なだけで「同じCM」としてしまわないよう、比べる前に外す
# （「LifeとWear/往復書簡」と「LifeとWear/嘘だ！」が同じCMと判定され、開始日がずれた）。
SERIES_RE = re.compile(r"lifeとwear|lifewear|ライフウェア|uniqlo|ユニクロ|篇|編")


def same_cm(a_product: str, a_name: str, b_product: str, b_name: str) -> bool:
    """同じCMかどうか。商品名が両方わかっていて食い違えば別のCM。CM名はシリーズ名を除いて比べる。"""
    pa, pb = normalize(a_product), normalize(b_product)
    na, nb = SERIES_RE.sub("", normalize(a_name)), SERIES_RE.sub("", normalize(b_name))
    if pa and pb and not (pa in pb or pb in pa):
        return False
    if na and nb:
        return na in nb or nb in na or SequenceMatcher(None, na, nb).ratio() >= 0.7
    # どちらかのCM名が分からないときは、商品名が一致していれば同じとみなす。
    return bool(pa and pb)


def views_until(views: dict[str, int], start: str) -> tuple[str, str]:
    """再生回数の伸びから「いつまで放映とみなすか」と、その理由を返す。"""
    begin = dt.date.fromisoformat(start)
    cap = begin + dt.timedelta(days=MAX_DAYS)
    default = begin + dt.timedelta(days=DEFAULT_DAYS)
    days = sorted(views)
    if len(days) >= 2:
        last = dt.date.fromisoformat(days[-1])
        base_day = next((d for d in reversed(days) if dt.date.fromisoformat(d) <= last - dt.timedelta(days=STALL_WINDOW)), None)
        if base_day:
            gain = views[days[-1]] - views[base_day]
            if gain < STALL_GAIN and gain < views[base_day] * STALL_RATIO:
                return base_day, "再生の伸びが止まった"          # この日以降は流れていないとみなす
            # まだ伸びている。8週を過ぎても、伸びている間は（最長16週まで）出し続ける。
            return min(cap, max(default, last + dt.timedelta(days=STALL_WINDOW))).isoformat(), "推定"
    return default.isoformat(), "推定"


def build_records(history: dict) -> list[dict]:
    today = today_jst()
    announcements = [
        {**cm, "doc_url": url, "doc_kind": doc.get("kind", ""), "doc_title": doc.get("title", ""), "doc_image": doc.get("image", "")}
        for url, doc in history["docs"].items() for cm in doc.get("cms", [])
    ]

    # 公式動画を、15秒・30秒などの違いをまとめて1本のCMにする。
    groups: list[dict] = []
    for vid, video in history["videos"].items():
        if video.get("skip"):
            continue
        group = next((g for g in groups if g["brand"] == video["brand"]
                      and normalize(g["name"]) == normalize(video["name"])
                      and normalize(g["product"]) == normalize(video["product"])), None)
        if group is None:
            group = {"brand": video["brand"], "product": video["product"], "name": video["name"],
                     "videos": [], "start": video["published"]}
            groups.append(group)
        group["videos"].append((vid, video))
        group["start"] = min(group["start"], video["published"])

    records = []
    for group in groups:
        # いちばん再生されている動画を代表にする（ふつうは30秒版）。
        vid, top = max(group["videos"], key=lambda v: max(v[1]["views"].values() or [0]))
        max_views = max(top["views"].values() or [0])
        matched = [a for a in announcements if a["brand"] == group["brand"]
                   and same_cm(a["product"], a["name"], group["product"], group["name"])]
        tv = bool(matched)
        if not tv and max_views < MIN_VIEWS:
            continue   # 公式の裏付けも無く、再生も少ない＝ネット向けの動画
        start = min([a["start"] for a in matched if a["start"]] or [group["start"]])
        ends = [a["end"] for a in matched if a["end"]]
        if ends:
            until, reason = max(ends), "発表"
        else:
            until, reason = views_until(top["views"], start)
        talents = []
        for a in matched:
            talents += [t for t in a["talents"] if t not in talents]
        records.append({
            "key": f"{group['brand']}:{normalize(group['product'])}:{normalize(group['name'])}",
            "brand": group["brand"], "product": group["product"], "name": group["name"],
            "talents": talents, "song": next((a["song"] for a in matched if a["song"]), ""),
            "start": start, "until": until, "until_reason": reason, "tv_confirmed": tv,
            "video_id": vid, "views": max_views,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "image": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "sources": [{"t": a["doc_title"], "u": a["doc_url"], "k": a["doc_kind"]} for a in matched][:3],
        })

    # 公式動画が無いCM（GUなど）は、発表そのものを載せる。
    for a in announcements:
        if any(r["brand"] == a["brand"] and same_cm(a["product"], a["name"], r["product"], r["name"]) for r in records):
            continue
        if not is_http(a["doc_url"]):
            continue
        key = f"{a['brand']}:{normalize(a['product'])}:{normalize(a['name'])}"
        existing = next((r for r in records if r["key"] == key), None)
        if existing:
            existing["talents"] += [t for t in a["talents"] if t not in existing["talents"]]
            existing["sources"].append({"t": a["doc_title"], "u": a["doc_url"], "k": a["doc_kind"]})
            continue
        until, reason = (a["end"], "発表") if a["end"] else (
            (dt.date.fromisoformat(a["start"]) + dt.timedelta(days=DEFAULT_DAYS)).isoformat(), "推定")
        records.append({
            "key": key, "brand": a["brand"], "product": a["product"], "name": a["name"] or a["product"],
            "talents": a["talents"], "song": a["song"],
            "start": a["start"], "until": until, "until_reason": reason, "tv_confirmed": True,
            "video_id": "", "views": 0, "url": a["doc_url"],
            "image": a["doc_image"] if is_http(a["doc_image"]) else "",
            "sources": [{"t": a["doc_title"], "u": a["doc_url"], "k": a["doc_kind"]}],
        })

    # 終わったものは載せない（画面でも日付を見るので、ここでは数日分の余裕を持たせて残しておく）。
    soon = (today - dt.timedelta(days=1)).isoformat()
    records = [r for r in records if r["until"] >= soon and r["start"] <= (today + dt.timedelta(days=30)).isoformat()]
    records.sort(key=lambda r: (r["start"], r["views"]), reverse=True)
    return records


CRITERIA = [
    "ユニクロ公式YouTubeのCM動画と、公式のプレスリリース・ニュースの「新CM放映」の発表から集めています。",
    f"公式発表やニュースでテレビCMと確かめられたもの、または公式のCM動画が{MIN_VIEWS // 10000}万回以上再生されているものを載せています。",
    "放送の終了日は、発表されていればその日まで。ふつうは発表されないので、CM動画の再生回数の伸びが1週間止まったら放送が終わったとみなして外します。"
    f"伸びの記録が足りないうちは開始から{DEFAULT_DAYS // 7}週間、伸び続けていても最長{MAX_DAYS // 7}週間で外します。",
]


def build_tvcm(items: list[dict]) -> dict | None:
    """放映中のCMを調べて docs/tvcm.json に書き出す。何も調べられなかった回は None（前回のものを残す）。"""
    print("■ 📺 放映中のテレビCM")
    history = load_history()
    worked = False
    try:
        fetched = collect_videos(history)
        print(f"  公式YouTube: 動画ページを{fetched}本確認")
        worked = True
    except Exception as exc:  # noqa: BLE001  YouTubeが読めなくても発表からは作れる
        print(f"  × 公式YouTubeを読めませんでした: {type(exc).__name__}: {exc}")
    try:
        docs = press_documents(history) + news_documents(items, history)
        if docs:
            print(f"  CMに触れている文書 {len(docs)}件を読む")
        read_documents(docs, history)
        worked = True
    except Exception as exc:  # noqa: BLE001
        print(f"  × 発表を読めませんでした: {type(exc).__name__}: {exc}")
    save_history(history)
    if not worked:
        return None

    records = build_records(history)
    payload = {
        "updated_at": dt.datetime.now(JST).isoformat(),
        "criteria": CRITERIA,
        "items": records,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    for r in records:
        print(f"  ○ {r['brand']} {r['product']}「{r['name']}」 {r['start']}〜{r['until']}（{r['until_reason']}）"
              f"{' TV確認' if r['tv_confirmed'] else ''} {r['views']:,}回")
    return payload
