"""
골드 채널 자동 포스팅 봇
- 아침 브리핑 (화~토 08시)
- 미국 주요 지표 발표 30분 전 알림
- 금 1시간 ±1% 급변 / 100달러 단위 돌파·이탈 알림
- 금 뉴스 (매일 12시, 19시)
- 주간 리포트 (토요일 10시)
GitHub Actions가 15분마다 실행하고, 보낼 때가 된 것만 보냅니다.
"""
import os
import sys
import json
import math
import re
import html
import traceback
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
import yfinance as yf

# ================== 설정 (숫자만 바꿔서 쓰세요) ==================
MORNING_HOUR = 8          # 아침 브리핑 시각 (화~토)
NEWS_HOURS = [12, 19]     # 뉴스 올리는 시각
WEEKLY_HOUR = 10          # 주간 리포트 시각 (토요일)
MOVE_ALERT_PCT = 1.0      # 1시간 동안 이만큼(%) 움직이면 알림
ROUND_STEP = 100          # 이 단위(달러) 돌파/이탈 시 알림
EVENT_ALERT_MIN = 30      # 지표 발표 몇 분 전에 알릴지
NEWS_QUERY = "국제 금값"   # 뉴스 검색어
NEWS_COUNT = 3            # 뉴스 몇 개 올릴지
DAILY_REPORT_HOUR = 8     # 일일 매매 결산 시각 (화~토, 전 거래일 기준)
SYMBOL_NAME = "XAUUSD"    # 포지션 표시 이름
CONTRACT_SIZE = 100       # 1랏 = 100온스 (MT5 XAUUSD 일반 기준. 브로커마다 다르면 수정)
DEFAULT_LOT = 0.1         # 랏 수를 안 쓰면 이 값으로 기록
DAY_START_HOUR = 7        # 거래일 시작 시각 (한국시간 07시 = 금 시장 하루 시작)
# ===============================================================

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
STATE_FILE = "state.json"
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

GOLD = "GC=F"
SILVER = "SI=F"
DXY = "DX-Y.NYB"
US10Y = "^TNX"

# 경제지표 한글 이름 (앞에 있는 게 먼저 매칭됨)
EVENT_KO = [
    ("Core CPI", "근원 소비자물가(CPI)"),
    ("CPI", "소비자물가(CPI)"),
    ("Core PCE", "근원 PCE 물가"),
    ("PCE", "PCE 물가"),
    ("Core PPI", "근원 생산자물가(PPI)"),
    ("PPI", "생산자물가(PPI)"),
    ("Non-Farm Employment Change", "비농업 고용(NFP)"),
    ("Unemployment Rate", "실업률"),
    ("Unemployment Claims", "신규 실업수당 청구"),
    ("Average Hourly Earnings", "평균 시간당 임금"),
    ("Federal Funds Rate", "미국 기준금리 결정"),
    ("FOMC Statement", "FOMC 성명"),
    ("FOMC Press Conference", "FOMC 기자회견"),
    ("FOMC Meeting Minutes", "FOMC 의사록"),
    ("FOMC Economic Projections", "FOMC 경제전망"),
    ("Fed Chair", "연준 의장 발언"),
    ("GDP", "GDP 성장률"),
    ("Core Retail Sales", "근원 소매판매"),
    ("Retail Sales", "소매판매"),
    ("ISM Manufacturing PMI", "ISM 제조업 PMI"),
    ("ISM Services PMI", "ISM 서비스업 PMI"),
    ("JOLTS", "JOLTS 구인건수"),
    ("ADP", "ADP 민간고용"),
    ("Consumer Confidence", "소비자신뢰지수"),
    ("UoM", "미시간대 소비자심리"),
]
SUFFIX_KO = {" m/m": " (전월비)", " y/y": " (전년비)", " q/q": " (전분기비)"}

_events_cache = None


# ---------------- 공통 ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def tg(method, **data):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=30)
    if not r.ok:
        raise RuntimeError(f"텔레그램 {method} 실패: {r.status_code} {r.text}")
    return r.json().get("result")


def send(text, chat=None):
    """채널(기본) 또는 지정한 채팅으로 전송. 메시지 번호를 돌려줌"""
    chat = chat or os.environ["TELEGRAM_CHAT_ID"].strip()
    res = tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
             disable_web_page_preview="true")
    print("[전송]", text.splitlines()[0])
    return res.get("message_id") if isinstance(res, dict) else None


def pct(a, b):
    return (a - b) / b * 100


def arrow(p):
    return "🔺" if p > 0 else ("🔻" if p < 0 else "➖")


def kday(d):
    return f"{d:%m/%d} ({WEEKDAYS[d.weekday()]})"


def daily_completed(symbol):
    """마감된 일봉만 (미국 날짜 기준 오늘까지)"""
    df = yf.Ticker(symbol).history(period="1mo", interval="1d")
    if df.empty:
        raise RuntimeError(f"{symbol} 가격 데이터 없음")
    today_ny = datetime.now(NY).date()
    df = df[[ts.date() <= today_ny for ts in df.index]]
    if len(df) < 2:
        raise RuntimeError(f"{symbol} 데이터 부족")
    return df


def quote_line(name, symbol, digits=2):
    df = daily_completed(symbol)
    last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
    p = pct(last, prev)
    return f"{name} {last:,.{digits}f} {arrow(p)} {p:+.2f}%"


def yield_line(name, symbol):
    df = daily_completed(symbol)
    last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
    return f"{name} {last:.2f}% ({last - prev:+.2f}%p)"


def safe(fn, *args):
    """보조 지표 하나 실패해도 메시지는 나가게"""
    try:
        return fn(*args)
    except Exception as e:
        print("[보조지표 실패]", e)
        return None


# ---------------- 경제 캘린더 ----------------
def event_ko(title):
    name = title
    for en, ko in EVENT_KO:
        if en.lower() in title.lower():
            name = ko
            break
    for suf, ko in SUFFIX_KO.items():
        if title.endswith(suf) and name != title:
            name += ko
    return name


def fetch_events():
    """이번 주 미국 '중요도 높음' 지표 (ForexFactory 무료 피드)"""
    global _events_cache
    if _events_cache is not None:
        return _events_cache
    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    out = []
    for e in r.json():
        if e.get("country") != "USD" or e.get("impact") != "High":
            continue
        try:
            t = datetime.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        out.append({
            "id": f'{e.get("title")}|{e["date"]}',
            "title": e.get("title", ""),
            "time": t,
            "forecast": e.get("forecast") or "-",
            "previous": e.get("previous") or "-",
        })
    out.sort(key=lambda x: x["time"])
    _events_cache = out
    return out


def event_alerts(state, now):
    alerted = state.setdefault("alerted_events", {})
    groups = {}
    for e in fetch_events():
        mins = (e["time"] - now).total_seconds() / 60
        if 0 < mins <= EVENT_ALERT_MIN + 5 and e["id"] not in alerted:
            groups.setdefault(e["time"], []).append(e)

    for t, evs in groups.items():
        mins = round((t - now).total_seconds() / 60)
        lines = [f"⏰ <b>약 {mins}분 후 미국 지표 발표</b> ({t.astimezone(KST):%H:%M})", ""]
        for e in evs:
            lines.append(f"• {html.escape(event_ko(e['title']))}")
            if e["forecast"] != "-" or e["previous"] != "-":
                lines.append(f"   예상 {html.escape(e['forecast'])} / 이전 {html.escape(e['previous'])}")
        lines += ["", "발표 전후 금 변동성 확대 가능 구간입니다."]
        send("\n".join(lines))
        for e in evs:
            alerted[e["id"]] = now.isoformat()

    cutoff = now - timedelta(days=3)
    for k, v in list(alerted.items()):
        if datetime.fromisoformat(v) < cutoff:
            del alerted[k]


# ---------------- 가격 알림 ----------------
def price_check(state, now):
    df = yf.Ticker(GOLD).history(period="5d", interval="1h")
    if df.empty:
        raise RuntimeError("금 1시간봉 데이터 없음")
    done = df[df.index <= now - timedelta(hours=1)]  # 완성된 봉만
    if len(done) < 2:
        return
    bar_id = done.index[-1].isoformat()
    if state.get("last_price_bar") == bar_id:
        return  # 이미 확인한 봉
    state["last_price_bar"] = bar_id

    last, prev = float(done["Close"].iloc[-1]), float(done["Close"].iloc[-2])
    p = pct(last, prev)
    msgs = []
    if abs(p) >= MOVE_ALERT_PCT:
        word = "급등" if p > 0 else "급락"
        msgs.append(f"{arrow(p)} <b>금 1시간 {word}</b> {p:+.2f}%\n{prev:,.1f} → {last:,.1f}")

    lv_prev, lv_now = math.floor(prev / ROUND_STEP), math.floor(last / ROUND_STEP)
    if lv_now > lv_prev:
        msgs.append(f"🚀 <b>금 {lv_now * ROUND_STEP:,}달러 돌파</b>\n현재 {last:,.1f}")
    elif lv_now < lv_prev:
        msgs.append(f"⚠️ <b>금 {lv_prev * ROUND_STEP:,}달러 이탈</b>\n현재 {last:,.1f}")

    for m in msgs:
        send(m)


# ---------------- 아침 브리핑 ----------------
def morning_brief(now):
    k = now.astimezone(KST)
    g = daily_completed(GOLD)
    last, prev = g.iloc[-1], g.iloc[-2]
    p = pct(last["Close"], prev["Close"])

    lines = [
        f"☀️ <b>골드 모닝 브리핑</b> | {kday(k)}",
        "",
        f"🥇 <b>금 선물</b> ({g.index[-1]:%m/%d} 마감)",
        f"종가 {last['Close']:,.1f} {arrow(p)} {p:+.2f}%",
        f"시가 {last['Open']:,.1f} · 고가 {last['High']:,.1f} · 저가 {last['Low']:,.1f}",
        "",
        "📊 <b>함께 볼 지표</b>",
    ]
    for line in (safe(quote_line, "은", SILVER), safe(quote_line, "달러인덱스", DXY),
                 safe(yield_line, "미 10년물", US10Y)):
        if line:
            lines.append(line)

    try:
        evs = [e for e in fetch_events() if now <= e["time"] < now + timedelta(hours=24)]
        lines += ["", "🗓 <b>오늘 주요 일정</b> (미국·중요도 높음)"]
        if evs:
            for e in evs:
                t = e["time"].astimezone(KST)
                when = f"{t:%H:%M}" if t.date() == k.date() else f"내일 {t:%H:%M}"
                extra = ""
                if e["forecast"] != "-":
                    extra = f" — 예상 {html.escape(e['forecast'])} / 이전 {html.escape(e['previous'])}"
                lines.append(f"• {when} {html.escape(event_ko(e['title']))}{extra}")
        else:
            lines.append("• 큰 일정 없음")
    except Exception as e:
        print("[일정 실패]", e)

    send("\n".join(lines))


# ---------------- 뉴스 ----------------
def gold_now_line():
    """현재 금 시세 (전일 마감 대비)"""
    intraday = yf.Ticker(GOLD).history(period="2d", interval="15m")
    if intraday.empty:
        raise RuntimeError("금 실시간 데이터 없음")
    cur = float(intraday["Close"].iloc[-1])
    daily = daily_completed(GOLD)
    today_ny = datetime.now(NY).date()
    # 오늘 봉이 진행 중이면 그 전날 종가와 비교
    base_row = daily.iloc[-2] if daily.index[-1].date() == today_ny else daily.iloc[-1]
    p = pct(cur, float(base_row["Close"]))
    return f"💰 <b>금 현재가</b> {cur:,.1f} {arrow(p)} {p:+.2f}%"


def ai_summary(titles):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    prompt = ("다음은 최근 금 시장 관련 뉴스 제목들이다. 금 시장 흐름을 한국어 2~3문장으로 담백하게 요약해줘. "
              "과장, 전망 단정, 매매 권유는 하지 말고 요약 문장만 출력해.\n\n"
              + "\n".join(f"- {t}" for t in titles))
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-5-5", "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"]).strip()
    except Exception as e:
        print("[AI 요약 실패]", e)
        return None


# 뉴스 출처 (이름, 주소, 한국어 여부). 구글이 막혀도 나머지에서 가져옴
NEWS_FEEDS = [
    ("구글뉴스", f"https://news.google.com/rss/search?q={quote(NEWS_QUERY)}&hl=ko&gl=KR&ceid=KR:ko", True),
    ("연합뉴스", "https://www.yna.co.kr/rss/market.xml", True),
    ("연합뉴스", "https://www.yna.co.kr/rss/international.xml", True),
    ("연합뉴스", "https://www.yna.co.kr/rss/economy.xml", True),
    ("뉴시스", "https://www.newsis.com/RSS/economy.xml", True),
    ("한국경제", "https://www.hankyung.com/feed/finance", True),
    ("한국경제", "https://www.hankyung.com/feed/international", True),
    ("Investing.com", "https://www.investing.com/rss/news_11.rss", False),
]
KO_GOLD = re.compile(r"금값|금\s?가격|금\s?시세|국제\s?금|금\s?선물|금\s?현물|금\s?투자|금\s?ETF|골드바|금괴|금\s?매입|금\s?보유")
EN_GOLD = re.compile(r"\bgold\b", re.I)


def parse_pub(it):
    for tag in ("pubDate", "{http://purl.org/dc/elements/1.1/}date"):
        raw = (it.findtext(tag) or "").strip()
        if not raw:
            continue
        try:
            return parsedate_to_datetime(raw)
        except Exception:
            pass
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def fetch_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    items, seen = [], set()
    for name, url, korean in NEWS_FEEDS:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"[뉴스 출처 실패] {name}: {e}")
            continue
        for it in root.iter("item"):
            title = html.unescape((it.findtext("title") or "").strip())
            link = (it.findtext("link") or "").strip()
            source = (it.findtext("source") or "").strip() or name
            if source and title.endswith(" - " + source):
                title = title[: -len(" - " + source)]
            if not (KO_GOLD if korean else EN_GOLD).search(title):
                continue
            pub = parse_pub(it)
            if pub is None or pub < cutoff:
                continue
            key = re.sub(r"\W", "", title)[:20]
            if key in seen or not link:
                continue
            seen.add(key)
            items.append({"title": title, "link": link, "source": source,
                          "pub": pub, "korean": korean})
    # 한국어 기사 먼저, 그다음 최신순
    items.sort(key=lambda x: (not x["korean"], -x["pub"].timestamp()))
    return items


def post_news(state, now, force=False):
    items = fetch_news()
    posted = state.get("posted_news", [])
    new = [i for i in items if i["link"] not in posted]
    if not new:
        if not force:
            print("[뉴스] 새 기사 없음")
            return
        new = items
    pick = new[:NEWS_COUNT]
    if not pick:
        return

    k = now.astimezone(KST)
    lines = [f"📰 <b>금 시장 뉴스</b>", f"{kday(k)} {k:%H:%M}", ""]
    price = safe(gold_now_line)
    if price:
        lines += [price, ""]
    summary = ai_summary([i["title"] for i in new[:10]])
    if summary:
        lines += [f"💬 <b>요약</b>", html.escape(summary), ""]
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
    lines.append("━━━━━━━━━━━━")
    for n, i in enumerate(pick):
        t = i["pub"].astimezone(KST)
        when = f"{t:%H:%M}" if t.date() == k.date() else f"{t:%m/%d %H:%M}"
        lines += [
            "",
            f'{nums[n]} <b><a href="{html.escape(i["link"])}">{html.escape(i["title"])}</a></b>',
            f"      <i>{html.escape(i['source'])} · {when}</i>",
        ]
    lines += ["", "━━━━━━━━━━━━", "👆 제목을 누르면 기사로 이동합니다"]
    send("\n".join(lines))

    state["posted_news"] = (posted + [i["link"] for i in pick])[-100:]


# ---------------- 주간 리포트 ----------------
def week_stats(symbol):
    df = daily_completed(symbol)
    today_ny = datetime.now(NY).date()
    monday = today_ny - timedelta(days=today_ny.weekday())
    wk = df[[ts.date() >= monday for ts in df.index]]
    before = df[[ts.date() < monday for ts in df.index]]
    if wk.empty or before.empty:
        raise RuntimeError(f"{symbol} 주간 데이터 부족")
    return {
        "close": float(wk["Close"].iloc[-1]),
        "prev": float(before["Close"].iloc[-1]),
        "high": float(wk["High"].max()),
        "low": float(wk["Low"].min()),
        "start": wk.index[0], "end": wk.index[-1],
    }


def weekly_report(now):
    g = week_stats(GOLD)
    p = pct(g["close"], g["prev"])
    lines = [
        f"📅 <b>주간 골드 리포트</b> | {g['start']:%m/%d} ~ {g['end']:%m/%d}",
        "",
        "🥇 <b>금 선물</b>",
        f"주간 종가 {g['close']:,.1f} {arrow(p)} {p:+.2f}%",
        f"주간 고가 {g['high']:,.1f} · 저가 {g['low']:,.1f}",
        "",
        "📊 <b>함께 본 지표 (주간)</b>",
    ]
    for name, sym, digits in (("은", SILVER, 2), ("달러인덱스", DXY, 2)):
        s = safe(week_stats, sym)
        if s:
            sp = pct(s["close"], s["prev"])
            lines.append(f"{name} {s['close']:,.{digits}f} {arrow(sp)} {sp:+.2f}%")
    t = safe(week_stats, US10Y)
    if t:
        lines.append(f"미 10년물 {t['close']:.2f}% ({t['close'] - t['prev']:+.2f}%p)")

    try:
        past = [e for e in fetch_events() if e["time"] <= now]
        if past:
            names = []
            for e in past:
                n = event_ko(e["title"])
                if n not in names:
                    names.append(n)
            lines += ["", "🗓 <b>이번 주 주요 지표</b>"]
            lines += [f"• {html.escape(n)}" for n in names[:8]]
    except Exception as e:
        print("[일정 실패]", e)

    send("\n".join(lines))


# ---------------- 내 포지션 기록 (채널 글 자동 인식) ----------------
# 채널에 직접 쓴 글이 아래 단어로 시작하면 기록합니다.
#   매수 3875 1랏 손절 3860 목표 3900   (매수/매도/롱/숏/buy/sell)
#   청산 3890        → 가장 최근 보유 포지션 청산
#   #2 청산 3890     → 2번 포지션 청산
# 글을 '수정'하면 기록도 같이 고쳐집니다.
REACT_ON_RECORD = True    # 기록되면 그 글에 ✍ 반응 달기 (확인용)

ENTRY_WORDS = {"매수": "BUY", "buy": "BUY", "롱": "BUY", "long": "BUY",
               "매도": "SELL", "sell": "SELL", "숏": "SELL", "short": "SELL"}
CLOSE_WORDS = ("청산", "close", "종료")
SKIP_WORDS = ("xauusd", "gold", "골드", "금")
NUM = r"\d[\d,]*(?:\.\d+)?"


def usd(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def trade_day(dt):
    """한국시간 07시 기준 거래일"""
    return (dt.astimezone(KST) - timedelta(hours=DAY_START_HOUR)).date()


def pnl_of(p):
    diff = p["exit"] - p["entry"] if p["side"] == "BUY" else p["entry"] - p["exit"]
    return diff, diff * CONTRACT_SIZE * p["lots"]


def fnum(s):
    return float(s.replace(",", ""))


def parse_trade(text):
    """채널 글 → {'type': 'open'|'close', ...} 또는 None"""
    if not text:
        return None
    lines = text.strip().splitlines()
    first = lines[0]
    m_id = re.search(r"#\s*(\d+)", first)
    pid = int(m_id.group(1)) if m_id else None
    body = re.sub(r"#\s*\d+", " ", first)
    words = re.sub(r"[^\w\s.,]", " ", body).split()
    while words and words[0].lower() in SKIP_WORDS:
        words = words[1:]
    if not words:
        return None
    w0 = words[0].lower()
    rest = " ".join(words[1:])

    if any(w0.startswith(k) for k in CLOSE_WORDS):
        nums = re.findall(NUM, rest)
        if not nums:
            return None
        price = fnum(nums[0])
        if not 100 <= price <= 100000:
            return None
        return {"type": "close", "price": price, "id": pid}

    side = next((v for k, v in ENTRY_WORDS.items() if w0.startswith(k)), None)
    if not side:
        return None
    full = " ".join(lines)
    sl = re.search(r"(?:손절|sl|stop)\s*[:：]?\s*(" + NUM + ")", full, re.I)
    tp = re.search(r"(?:목표|익절|tp|target)\s*[:：]?\s*(" + NUM + ")", full, re.I)
    clean = re.sub(r"(?:손절|sl|stop|목표|익절|tp|target)\s*[:：]?\s*" + NUM, " ", rest, flags=re.I)
    lot_after = r"(?:랏|lots?)\s*[:：]?\s*(" + NUM + ")"   # 'lot 0.3'
    lot_before = r"(" + NUM + r")\s*(?:랏|lots?)"           # '0.3랏'
    lot = re.search(lot_after, clean, re.I)
    if lot:
        clean = re.sub(lot_after, " ", clean, flags=re.I)
    else:
        lot = re.search(lot_before, clean, re.I)
        clean = re.sub(lot_before, " ", clean, flags=re.I)
    nums = re.findall(NUM, clean)
    if not nums:
        return None
    price = fnum(nums[0])
    if not 100 <= price <= 100000:
        return None
    if lot:
        lots = fnum(lot.group(1))
    elif len(nums) > 1 and fnum(nums[1]) < 100:
        lots = fnum(nums[1])
    else:
        lots = DEFAULT_LOT
    return {"type": "open", "side": side, "entry": price, "lots": lots,
            "sl": fnum(sl.group(1)) if sl else None,
            "tp": fnum(tp.group(1)) if tp else None}


def react(chat, message_id):
    if not REACT_ON_RECORD:
        return
    try:
        tg("setMessageReaction", chat_id=chat, message_id=message_id,
           reaction=json.dumps([{"type": "emoji", "emoji": "✍"}]))
    except Exception as e:
        print("[반응 실패]", e)


def is_my_channel(chat):
    target = os.environ["TELEGRAM_CHAT_ID"].strip().lower()
    return (str(chat.get("id")) == target
            or ("@" + (chat.get("username") or "")).lower() == target)


def record_post(state, post, edited=False):
    text = post.get("text") or post.get("caption") or ""
    mid = post["message_id"]
    chat = post["chat"]["id"]
    when = datetime.fromtimestamp(post["date"], timezone.utc)
    pos = state.setdefault("positions", [])
    t = parse_trade(text)

    by_open = next((p for p in pos if p.get("src_open") == mid), None)
    by_close = next((p for p in pos if p.get("src_close") == mid), None)

    if edited:  # 수정된 글이면 기존 기록 고치기
        if by_open and t and t["type"] == "open":
            by_open.update(side=t["side"], entry=t["entry"], lots=t["lots"], sl=t["sl"], tp=t["tp"])
            print(f"[수정] #{by_open['id']} 진입 정보 수정")
        elif by_close and t and t["type"] == "close":
            by_close["exit"] = t["price"]
            print(f"[수정] #{by_close['id']} 청산가 수정")
        elif by_open or by_close:
            print("[수정] 거래 형식이 아니게 바뀐 글 - 기존 기록 유지")
        elif t:
            edited = False  # 처음 인식되는 글이면 새로 기록
        if edited:
            return

    if not t or by_open or by_close:
        return

    if t["type"] == "open":
        pid = state.get("next_id", 1)
        state["next_id"] = pid + 1
        pos.append({"id": pid, "side": t["side"], "entry": t["entry"], "lots": t["lots"],
                    "sl": t["sl"], "tp": t["tp"], "open_time": when.isoformat(),
                    "exit": None, "close_time": None, "src_open": mid})
        print(f"[기록] #{pid} {t['side']} {t['entry']} {t['lots']}랏")
        react(chat, mid)
    else:
        opens = [p for p in pos if p["exit"] is None]
        target = (next((p for p in opens if p["id"] == t["id"]), None) if t["id"]
                  else (opens[-1] if opens else None))
        if not target:
            print("[청산] 맞는 보유 포지션 없음:", text[:30])
            return
        target.update(exit=t["price"], close_time=when.isoformat(), src_close=mid)
        print(f"[기록] #{target['id']} 청산 {t['price']} → {usd(pnl_of(target)[1])}")
        react(chat, mid)


def process_channel(state, now):
    """채널에 올라온 내 글 읽어서 포지션 기록"""
    offset = state.get("tg_offset", 0)
    updates = tg("getUpdates", offset=offset, timeout=0,
                 allowed_updates='["channel_post","edited_channel_post"]') or []
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        post = u.get("channel_post") or u.get("edited_channel_post")
        if not post or not is_my_channel(post.get("chat", {})):
            continue
        record_post(state, post, edited="edited_channel_post" in u)


def closed_between(state, start, end):
    """거래일 start ~ end (포함) 에 청산된 거래"""
    out = []
    for p in state.get("positions", []):
        if p["exit"] is None:
            continue
        d = trade_day(datetime.fromisoformat(p["close_time"]))
        if start <= d <= end:
            out.append(p)
    return sorted(out, key=lambda p: p["close_time"])


def summary_lines(trades):
    if not trades:
        return ["청산된 거래가 없습니다."]
    pnls = [pnl_of(p)[1] for p in trades]
    wins = sum(1 for v in pnls if v > 0)
    losses = sum(1 for v in pnls if v < 0)
    decided = wins + losses
    rate = f"{wins / decided * 100:.0f}%" if decided else "-"
    return [f"거래 {len(trades)}건 · 승 {wins} / 패 {losses} · 승률 {rate}",
            f"손익 합계 <b>{usd(sum(pnls))}</b>"]


def trade_line(p):
    return (f"• #{p['id']} {p['side']} {p['entry']:,.2f} → {p['exit']:,.2f} · "
            f"{p['lots']:g}랏 · {usd(pnl_of(p)[1])}")


def footer_lines(state):
    pos = state.get("positions", [])
    opens = [p for p in pos if p["exit"] is None]
    total = sum(pnl_of(p)[1] for p in pos if p["exit"] is not None)
    lines = [""]
    if opens:
        lines.append("📌 보유 중: " + ", ".join(
            f"#{p['id']} {p['side']} {p['entry']:,.2f}" for p in opens))
    lines.append(f"📈 누적 손익 {usd(total)}")
    return lines


def daily_pnl_report(state, now):
    day = trade_day(now) - timedelta(days=1)
    trades = closed_between(state, day, day)
    lines = [f"📒 <b>일일 매매 결산</b> | {kday(day)}", ""] + summary_lines(trades)
    if trades:
        lines += [""] + [trade_line(p) for p in trades]
    send("\n".join(lines + footer_lines(state)))


def weekly_pnl_report(state, now):
    end = trade_day(now) - timedelta(days=1)
    start = end - timedelta(days=end.weekday())  # 그 주 월요일
    trades = closed_between(state, start, end)
    lines = [f"🗂 <b>주간 매매 결산</b> | {start:%m/%d} ~ {end:%m/%d}", ""] + summary_lines(trades)
    if trades:
        lines += ["", "요일별"]
        d = start
        while d <= end:
            dt = [p for p in trades if trade_day(datetime.fromisoformat(p["close_time"])) == d]
            if dt:
                lines.append(f"• {kday(d)} {usd(sum(pnl_of(p)[1] for p in dt))} ({len(dt)}건)")
            d += timedelta(days=1)
        best = max(trades, key=lambda p: pnl_of(p)[1])
        worst = min(trades, key=lambda p: pnl_of(p)[1])
        lines += ["", f"최대 수익 #{best['id']} {usd(pnl_of(best)[1])}",
                  f"최대 손실 #{worst['id']} {usd(pnl_of(worst)[1])}"]
    send("\n".join(lines + footer_lines(state)))


def monthly_pnl_report(state, now):
    last_day = trade_day(now).replace(day=1) - timedelta(days=1)  # 지난달 말일
    start = last_day.replace(day=1)
    trades = closed_between(state, start, last_day)
    lines = [f"🏁 <b>{start.month}월 매매 결산</b>", ""] + summary_lines(trades)
    if trades:
        lines += ["", "주차별"]
        weeks = {}
        for p in trades:
            d = trade_day(datetime.fromisoformat(p["close_time"]))
            wk_start = d - timedelta(days=d.weekday())
            weeks.setdefault(wk_start, []).append(p)
        for i, (ws, ts) in enumerate(sorted(weeks.items()), 1):
            lines.append(f"• {ws:%m/%d}~ 주 {usd(sum(pnl_of(p)[1] for p in ts))} ({len(ts)}건)")
        best = max(trades, key=lambda p: pnl_of(p)[1])
        worst = min(trades, key=lambda p: pnl_of(p)[1])
        lines += ["", f"최대 수익 #{best['id']} {usd(pnl_of(best)[1])}",
                  f"최대 손실 #{worst['id']} {usd(pnl_of(worst)[1])}"]
    send("\n".join(lines + footer_lines(state)))


# ---------------- 실행 ----------------
def main():
    mode = (os.environ.get("MODE") or "auto").strip().lower()
    now = datetime.now(timezone.utc)
    k = now.astimezone(KST)
    today = k.date().isoformat()
    state = load_state()
    errors = []

    def run(name, fn):
        try:
            fn()
        except Exception:
            errors.append(name)
            print(f"[오류] {name}")
            traceback.print_exc()

    if mode == "test":
        send("✅ 봇 연결 테스트 성공!")
    elif mode == "morning":
        run("아침 브리핑", lambda: morning_brief(now))
    elif mode == "news":
        run("뉴스", lambda: post_news(state, now, force=True))
    elif mode == "weekly":
        run("주간 리포트", lambda: weekly_report(now))
    elif mode == "price":
        state.pop("last_price_bar", None)
        run("가격 알림", lambda: price_check(state, now))
    elif mode == "daily":
        run("포지션 기록", lambda: process_channel(state, now))
        run("일일 결산", lambda: daily_pnl_report(state, now))
    elif mode == "weekly_pnl":
        run("포지션 기록", lambda: process_channel(state, now))
        run("주간 결산", lambda: weekly_pnl_report(state, now))
    elif mode == "reset":
        for key in ("positions", "next_id", "last_action"):
            state.pop(key, None)
        print("포지션 기록을 초기화했습니다.")
    elif mode == "monthly":
        run("포지션 기록", lambda: process_channel(state, now))
        run("월간 결산", lambda: monthly_pnl_report(state, now))
    else:  # auto: 15분마다 실행되며 때가 된 것만 보냄
        run("포지션 기록", lambda: process_channel(state, now))
        run("지표 알림", lambda: event_alerts(state, now))
        run("가격 알림", lambda: price_check(state, now))

        # 아침 브리핑: 화~토, 설정 시각부터 4시간 안에 한 번
        if (k.weekday() in (1, 2, 3, 4, 5) and MORNING_HOUR <= k.hour < MORNING_HOUR + 4
                and state.get("last_morning") != today):
            def _m():
                morning_brief(now)
                state["last_morning"] = today
            run("아침 브리핑", _m)

        # 뉴스: 각 시각부터 3시간 안에 한 번
        done_slots = state.setdefault("news_slots", [])
        for h in NEWS_HOURS:
            slot = f"{today}-{h}"
            if h <= k.hour < h + 3 and slot not in done_slots:
                def _n(slot=slot):
                    post_news(state, now)
                    done_slots.append(slot)
                run("뉴스", _n)
        state["news_slots"] = done_slots[-20:]

        # 주간 리포트: 토요일, 설정 시각부터 4시간 안에 한 번
        if (k.weekday() == 5 and WEEKLY_HOUR <= k.hour < WEEKLY_HOUR + 4
                and state.get("last_weekly") != today):
            def _w():
                weekly_report(now)
                state["last_weekly"] = today
            run("주간 리포트", _w)

        # 일일 매매 결산: 화~토 (전 거래일)
        if (k.weekday() in (1, 2, 3, 4, 5) and DAILY_REPORT_HOUR <= k.hour < DAILY_REPORT_HOUR + 4
                and state.get("last_daily_pnl") != today):
            def _d():
                daily_pnl_report(state, now)
                state["last_daily_pnl"] = today
            run("일일 결산", _d)

        # 주간 매매 결산: 토요일
        if (k.weekday() == 5 and WEEKLY_HOUR <= k.hour < WEEKLY_HOUR + 4
                and state.get("last_weekly_pnl") != today):
            def _wp():
                weekly_pnl_report(state, now)
                state["last_weekly_pnl"] = today
            run("주간 결산", _wp)

        # 월간 매매 결산: 매월 1일
        if (k.day == 1 and WEEKLY_HOUR <= k.hour < WEEKLY_HOUR + 4
                and state.get("last_monthly_pnl") != today):
            def _mp():
                monthly_pnl_report(state, now)
                state["last_monthly_pnl"] = today
            run("월간 결산", _mp)

    save_state(state)
    if errors:
        print("실패한 작업:", ", ".join(errors))
    sys.exit(0)


if __name__ == "__main__":
    main()
