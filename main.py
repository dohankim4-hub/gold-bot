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


def fetch_news():
    url = (f"https://news.google.com/rss/search?q={quote(NEWS_QUERY)}"
           "&hl=ko&gl=KR&ceid=KR:ko")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items, seen_titles = [], set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        source = (it.findtext("source") or "").strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            continue
        if pub < cutoff:
            continue
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        key = title[:25]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        items.append({"title": title, "link": link, "source": source, "pub": pub})
    items.sort(key=lambda x: x["pub"], reverse=True)
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
    lines = [f"📰 <b>금 시장 뉴스</b> | {kday(k)} {k:%H:%M}", ""]
    summary = ai_summary([i["title"] for i in new[:10]])
    if summary:
        lines += [f"💬 {html.escape(summary)}", ""]
    for i in pick:
        src = f" · {html.escape(i['source'])}" if i["source"] else ""
        lines.append(f'• <a href="{html.escape(i["link"])}">{html.escape(i["title"])}</a>{src}')
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


# ---------------- 내 포지션 기록 ----------------
HELP_TEXT = (
    "📘 <b>사용법</b> (이 대화창에 보내세요)\n\n"
    "<b>진입</b>\n"
    "매수 4012.5 0.1\n"
    "매도 4012.5 0.1 손절 4030 목표 3980 고점 저항\n"
    "  → 가격, 랏(생략 시 기본값), 손절·목표·근거는 선택\n\n"
    "<b>청산</b>\n"
    "청산 4030  → 가장 최근 포지션 청산\n"
    "청산 4030 3  → #3번 포지션 청산\n\n"
    "<b>기타</b>\n"
    "현황  → 보유 포지션·오늘 손익\n"
    "취소  → 마지막 입력 되돌리기 (채널 글도 삭제 시도)\n\n"
    "※ 15분 간격으로 처리돼서 채널 반영까지 최대 15분 정도 걸려요."
)


def usd(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def trade_day(dt):
    """한국시간 07시 기준 거래일"""
    return (dt.astimezone(KST) - timedelta(hours=DAY_START_HOUR)).date()


def pnl_of(p):
    diff = p["exit"] - p["entry"] if p["side"] == "BUY" else p["entry"] - p["exit"]
    return diff, diff * CONTRACT_SIZE * p["lots"]


def num(s):
    return float(s.replace(",", "").replace("$", ""))


def is_num(s):
    try:
        num(s)
        return True
    except ValueError:
        return False


def handle_open(state, side, args, when):
    if not args or not is_num(args[0]):
        return "⚠️ 가격을 같이 적어주세요. 예) 매수 4012.5 0.1"
    entry = num(args[0])
    rest = args[1:]
    lots = DEFAULT_LOT
    if rest and is_num(rest[0]):
        lots = num(rest[0])
        rest = rest[1:]
    sl = tp = None
    memo = []
    i = 0
    while i < len(rest):
        w = rest[i].lower()
        if w in ("손절", "sl") and i + 1 < len(rest) and is_num(rest[i + 1]):
            sl = num(rest[i + 1]); i += 2; continue
        if w in ("목표", "익절", "tp") and i + 1 < len(rest) and is_num(rest[i + 1]):
            tp = num(rest[i + 1]); i += 2; continue
        memo.append(rest[i]); i += 1

    pid = state.get("next_id", 1)
    state["next_id"] = pid + 1
    p = {"id": pid, "side": side, "entry": entry, "lots": lots, "sl": sl, "tp": tp,
         "memo": " ".join(memo), "open_time": when.isoformat(),
         "exit": None, "close_time": None}

    emoji = "🟢" if side == "BUY" else "🔴"
    lines = [f"{emoji} <b>{side} {SYMBOL_NAME}</b>  #{pid}",
             f"진입 {entry:,.2f} · {lots:g}랏"]
    if sl or tp:
        lines.append(f"손절 {sl:,.2f}" if sl else "손절 -")
        lines[-1] += f" · 목표 {tp:,.2f}" if tp else " · 목표 -"
    if p["memo"]:
        lines.append(f"근거: {html.escape(p['memo'])}")
    lines.append(f"🕒 {when.astimezone(KST):%m/%d %H:%M}")
    p["msg_open"] = send("\n".join(lines))

    state.setdefault("positions", []).append(p)
    state["last_action"] = {"type": "open", "id": pid}
    return f"✅ #{pid} {side} {entry:,.2f} · {lots:g}랏 등록했고 채널에 올렸어요."


def handle_close(state, args, when):
    if not args or not is_num(args[0]):
        return "⚠️ 청산 가격을 적어주세요. 예) 청산 4030"
    price = num(args[0])
    opens = [p for p in state.get("positions", []) if p["exit"] is None]
    if not opens:
        return "⚠️ 보유 중인 포지션이 없어요."
    if len(args) > 1:
        want = args[1].lstrip("#")
        target = next((p for p in opens if str(p["id"]) == want), None)
        if not target:
            return f"⚠️ #{want} 보유 포지션을 못 찾았어요. '현황'으로 번호를 확인하세요."
    else:
        target = opens[-1]

    target["exit"] = price
    target["close_time"] = when.isoformat()
    diff, money = pnl_of(target)
    emoji = "✅" if money > 0 else ("❌" if money < 0 else "➖")
    text = (f"{emoji} <b>청산 {target['side']} {SYMBOL_NAME}</b>  #{target['id']}\n"
            f"{target['entry']:,.2f} → {price:,.2f} ({diff:+,.2f})\n"
            f"손익 <b>{usd(money)}</b> · {target['lots']:g}랏\n"
            f"🕒 {when.astimezone(KST):%m/%d %H:%M}")
    target["msg_close"] = send(text)
    state["last_action"] = {"type": "close", "id": target["id"]}
    left = len(opens) - 1
    return f"✅ #{target['id']} 청산 기록 · 손익 {usd(money)}" + (f" (남은 보유 {left}개)" if left else "")


def handle_undo(state):
    la = state.get("last_action")
    pos = state.get("positions", [])
    if not la:
        return "⚠️ 되돌릴 입력이 없어요."
    p = next((x for x in pos if x["id"] == la["id"]), None)
    if not p:
        return "⚠️ 되돌릴 입력을 못 찾았어요."
    key = "msg_open" if la["type"] == "open" else "msg_close"
    deleted = False
    if p.get(key):
        try:
            tg("deleteMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"].strip(), message_id=p[key])
            deleted = True
        except Exception as e:
            print("[삭제 실패]", e)
    if la["type"] == "open":
        pos.remove(p)
        msg = f"↩️ #{p['id']} 진입 기록을 지웠어요."
    else:
        p["exit"] = p["close_time"] = p["msg_close"] = None
        msg = f"↩️ #{p['id']} 청산을 취소했어요 (다시 보유 중)."
    state["last_action"] = None
    return msg + ("" if deleted else "\n채널 글은 직접 지워주세요.")


def handle_status(state, now):
    pos = state.get("positions", [])
    opens = [p for p in pos if p["exit"] is None]
    today = trade_day(now)
    today_closed = [p for p in pos if p["exit"] is not None
                    and trade_day(datetime.fromisoformat(p["close_time"])) == today]
    total = sum(pnl_of(p)[1] for p in pos if p["exit"] is not None)
    lines = ["📋 <b>현황</b>", ""]
    if opens:
        lines.append("보유 중")
        lines += [f"• #{p['id']} {p['side']} {p['entry']:,.2f} · {p['lots']:g}랏" for p in opens]
    else:
        lines.append("보유 중인 포지션 없음")
    lines += ["", f"오늘 청산 {len(today_closed)}건 · {usd(sum(pnl_of(p)[1] for p in today_closed))}",
              f"누적 손익 {usd(total)}"]
    return "\n".join(lines)


def process_commands(state, now):
    """봇 개인 대화창으로 온 명령 처리"""
    offset = state.get("tg_offset", 0)
    updates = tg("getUpdates", offset=offset, timeout=0,
                 allowed_updates='["message"]') or []
    owner = (os.environ.get("TELEGRAM_OWNER_ID") or "").strip() or state.get("owner_id")
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            continue
        uid = str(msg["from"]["id"])
        chat = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        when = datetime.fromtimestamp(msg["date"], timezone.utc)

        if not owner:  # 처음 말 건 사람을 주인으로 등록
            owner = uid
            state["owner_id"] = uid
            send("🔐 주인으로 등록됐어요. 이제 이 봇은 당신 명령만 받습니다.\n\n" + HELP_TEXT, chat)
            continue
        if uid != owner:
            continue

        parts = text.replace(",", "").split()
        if not parts:
            continue
        cmd, args = parts[0].lstrip("/").lower(), parts[1:]
        try:
            if cmd in ("매수", "buy", "롱", "long"):
                reply = handle_open(state, "BUY", args, when)
            elif cmd in ("매도", "sell", "숏", "short"):
                reply = handle_open(state, "SELL", args, when)
            elif cmd in ("청산", "close", "종료"):
                reply = handle_close(state, args, when)
            elif cmd in ("취소", "undo"):
                reply = handle_undo(state)
            elif cmd in ("현황", "status"):
                reply = handle_status(state, now)
            else:
                reply = HELP_TEXT
        except Exception as e:
            traceback.print_exc()
            reply = f"⚠️ 처리 중 오류: {html.escape(str(e))}"
        send(reply, chat)


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
        run("일일 결산", lambda: daily_pnl_report(state, now))
    elif mode == "weekly_pnl":
        run("주간 결산", lambda: weekly_pnl_report(state, now))
    elif mode == "monthly":
        run("월간 결산", lambda: monthly_pnl_report(state, now))
    else:  # auto: 15분마다 실행되며 때가 된 것만 보냄
        run("포지션 명령", lambda: process_commands(state, now))
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
