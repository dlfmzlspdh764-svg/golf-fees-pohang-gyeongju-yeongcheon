#!/usr/bin/env python3
"""
포항·경주·영천 골프장 요금 데이터 갱신 스크립트.

하는 일
  1) 골프존 티스캐너 공개 티타임 목록(로그인 불필요, robots.txt 허용)에서
     골프장별·날짜별(오늘부터 N일) 최저 그린피와 잔여 티타임 수를 수집
  2) 공식 홈페이지 요금표 중 자동 파싱이 가능한 곳(보문GC, 힐스카이CC)은
     오늘 날짜에 해당하는 기간 요금표를 다시 읽어 갱신
  3) 그 외 공식 요금 페이지는 변경 감지(해시)만 하여, 바뀌면 '재확인 필요' 표시
  4) data.json 과 데이터가 내장된 golf-fees.html / index.html 을 다시 생성

사용법
  python3 refresh.py            # 기본 7일
  python3 refresh.py --days 10
  python3 refresh.py --no-live  # 티스캐너 수집 생략
주기 실행: GitHub Actions(.github/workflows/refresh.yml)가 매일 06:00 KST 에 실행
"""
import argparse, datetime as dt, hashlib, html, json, os, re, sys, time
from zoneinfo import ZoneInfo

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # 공식 페이지 파싱/감시에만 필요
    BeautifulSoup = None

KST = ZoneInfo("Asia/Seoul")
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "courses_base.json")
STATE = os.path.join(HERE, "monitor_state.json")
OUT_JSON = os.path.join(HERE, "data.json")
TEMPLATE = os.path.join(HERE, "app_template.html")
OUT_STANDALONE = os.path.join(HERE, "golf-fees.html")
OUT_INDEX = os.path.join(HERE, "index.html")
OUT_TT = os.path.join(HERE, "teetimes.json")

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36 golf-fee-app/1.0")
TS_API = "https://foapi.teescanner.com/v1"
TS_HEADERS = {"User-Agent": UA, "Referer": "https://m.teescanner.com/",
              "Accept": "application/json, text/plain, */*"}
DELAY = 0.4  # 요청 간 간격(초) — 서버 부담 최소화
WD = "월화수목금토일"

S = requests.Session()


def now_kst():
    return dt.datetime.now(KST)


def log(*a):
    print(now_kst().strftime("%Y-%m-%d %H:%M:%S KST"), *a, flush=True)


# ---------------------------------------------------------------- 티스캐너
def ts_holidays():
    try:
        r = S.get(f"{TS_API}/common/calendar/getHoliDayList", headers=TS_HEADERS, timeout=20)
        return set(r.json()["data"]["holi"])
    except Exception as e:
        log("공휴일 목록 실패:", e)
        return set()


def part_of(hhmm):
    h = int(hhmm[:2])
    return "1부" if h < 10 else ("2부" if h < 15 else "3부")


def holes_of(t):
    """18 / 9 / 99(9홀x2=18홀) / 0(표기 불일치: 코스명은 9홀인데 9홀 상품 표시가 없음)"""
    if t.get("is_9h") == "Y":
        return 9
    if t.get("is_9h2") == "Y":
        return 99
    cn = html.unescape(t.get("course_name") or "")
    if re.search(r"9\s*홀", cn) and not re.search(r"18\s*홀|[xX×]\s*2", cn):
        return 0
    return 18


def ts_day(seq, day):
    url = f"{TS_API}/booking/getTeeTimeListbyGolfclub"
    r = S.get(url, params={"golfclub_seq": seq, "roundDay": day, "orderType": ""},
              headers=TS_HEADERS, timeout=25)
    r.raise_for_status()
    j = r.json()
    lst = (j.get("data") or {}).get("teeTimeList") or []
    res = {"count": len(lst), "min18": None, "min18_time": None, "min9": None,
           "parts": {}, "caddie": sorted({t.get("caddie_name", "") for t in lst if t.get("caddie_name")})}
    for t in lst:
        cost = t.get("min_cost") or 0
        if not cost:
            continue
        tm = t.get("teetime_time", "")
        if holes_of(t) == 0:  # 18홀/9홀 판단 불가 -> 최저가 계산에서 제외
            continue
        if t.get("is_9h") == "Y":
            res["min9"] = cost if res["min9"] is None else min(res["min9"], cost)
            continue
        if res["min18"] is None or cost < res["min18"]:
            res["min18"], res["min18_time"] = cost, tm
        p = part_of(tm) if tm else "기타"
        res["parts"][p] = cost if p not in res["parts"] else min(res["parts"][p], cost)
    res["count18"] = sum(1 for t in lst if t.get("is_9h") != "Y")
    res["_rows"] = lst
    return res


def collect_live(courses, days):
    hol = ts_holidays()
    today = now_kst().date()
    dates = [today + dt.timedelta(days=i) for i in range(days)]
    date_meta = []
    for d in dates:
        ds = d.isoformat()
        date_meta.append({"date": ds, "label": f"{d.month}/{d.day}({WD[d.weekday()]})",
                          "weekend": d.weekday() >= 5 or ds in hol, "holiday": ds in hol})
    live = {}
    tt = TTBuilder()
    for c in courses:
        seq = c.get("teescanner_seq")
        if not seq:
            continue
        per = {}
        for dm in date_meta:
            try:
                v = ts_day(seq, dm["date"])
                rows = v.pop("_rows", [])
                if rows:
                    tt.add(c["id"], dm["date"], rows)
                per[dm["date"]] = v
            except Exception as e:
                per[dm["date"]] = {"error": str(e)[:120]}
            time.sleep(DELAY)
        live[c["id"]] = {"seq": seq, "url": f"https://www.teescanner.com/booking/detail?tab=teetime&golfclub_seq={seq}",
                         "days": per}
        mins = [v["min18"] for v in per.values() if v.get("min18")]
        log(f"티스캐너 {c['name']}: {len(mins)}/{len(per)}일 가격 확인, 최저 {min(mins) if mins else '-'}")
    fetched = now_kst().isoformat(timespec="seconds")
    return ({"source": "골프존 티스캐너 공개 티타임 목록", "fetched_at": fetched,
             "dates": date_meta, "courses": live}, tt.out(fetched, date_meta))


class TTBuilder:
    """티타임 개별 목록을 문자열 사전 + 배열로 압축 (teetimes.json)
    행 = [시각, 1인 그린피, 할인 전 금액(같으면 0), 홀(18 / 9 / 99=9홀x2 18홀 / 0=표기 불일치), 코스명#, 캐디#, 안내문#, 상품태그#, 4인 필수(1/0)]
    #은 dict 의 인덱스, -1 = 없음. 값은 티스캐너 응답 그대로이며 추정치를 넣지 않음."""
    KEYS = ("course", "caddie", "note", "tag")

    def __init__(self):
        self.d = {k: [] for k in self.KEYS}
        self.ix = {k: {} for k in self.KEYS}
        self.courses = {}

    def i(self, k, v):
        v = html.unescape((v or "").strip())
        if not v:
            return -1
        if v not in self.ix[k]:
            self.ix[k][v] = len(self.d[k])
            self.d[k].append(v)
        return self.ix[k][v]

    def add(self, cid, date, lst):
        rows = []
        for t in lst:
            cost = t.get("min_cost") or 0
            tm = (t.get("teetime_time") or "")[:5]
            if not cost or not tm:
                continue
            org = t.get("min_orgin_cost") or 0
            holes = holes_of(t)
            rows.append([tm, cost, org if org and org != cost else 0, holes,
                         self.i("course", t.get("course_name")), self.i("caddie", t.get("caddie_name")),
                         self.i("note", t.get("benefit_comment")), self.i("tag", t.get("product_tag_nm")),
                         1 if t.get("is_4p") == "Y" else 0])
        rows.sort(key=lambda r: (r[0], r[1]))
        if rows:
            self.courses.setdefault(cid, {})[date] = rows

    def out(self, fetched, dates):
        return {"source": "골프존 티스캐너 공개 티타임 목록", "fetched_at": fetched,
                "dates": [d["date"] for d in dates],
                "fields": ["time", "price", "orig_price", "holes", "course", "caddie", "note", "tag", "four_required"],
                "dict": self.d, "courses": self.courses}


# ---------------------------------------------------------------- 공식 페이지
def fetch_html(url):
    r = S.get(url, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    b = r.content
    for enc in ("utf-8", "cp949"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "ignore")


def grid(table):
    g = {}
    for r, tr in enumerate(table.find_all("tr")):
        c = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in g:
                c += 1
            rs, cs = int(cell.get("rowspan") or 1), int(cell.get("colspan") or 1)
            txt = cell.get_text(" ", strip=True)
            for i in range(rs):
                for j in range(cs):
                    g[(r + i, c + j)] = txt
            c += cs
    if not g:
        return []
    R = max(k[0] for k in g) + 1
    C = max(k[1] for k in g) + 1
    return [[g.get((r, c), "") for c in range(C)] for r in range(R)]


def num(s):
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d{4,})", s or "")
    return int(m.group(1).replace(",", "")) if m else None


def period_dates(text, year_hint):
    """'2026. 9. 14.(월) ~ 9. 30.(수)' / '2026년 10월 1일(목) ~ 10월 31일(토)' 형태 파싱"""
    nums = re.findall(r"(?:(\d{4})\s*[.년]\s*)?(\d{1,2})\s*[.월]\s*(\d{1,2})\s*[.일]?", text)
    ds = []
    y = year_hint
    for yy, mm, dd in nums:
        if yy:
            y = int(yy)
        try:
            ds.append(dt.date(y, int(mm), int(dd)))
        except ValueError:
            pass
    if len(ds) >= 2:
        a, b = ds[0], ds[1]
        if b < a:
            b = b.replace(year=b.year + 1)
        return a, b
    return None


def summarize(rows, cols):
    """rows: [[부, 시간, v...]], cols: 요일 헤더 -> weekday/weekend min/max"""
    wk, we = [], []
    for r in rows:
        for h, v in zip(cols, r[2:]):
            n = num(v)
            if not n:
                continue
            (we if re.search(r"토|일|공휴", h) else wk).append(n)
    if not wk or not we:
        return None
    return {"weekday": {"min": min(wk), "max": max(wk)}, "weekend": {"min": min(we), "max": max(we)}}


def parse_bomun(h, today):
    s = BeautifulSoup(h, "lxml")
    heads = [x for x in s.find_all(string=re.compile(r"시행일자"))]
    tables = [t for t in s.find_all("table") if not t.find("table")]
    out = []
    for hd in heads:
        per = period_dates(str(hd), today.year)
        if not per:
            continue
        # 시행일자 뒤 첫 헤더표 + 데이터표
        nxt = hd.find_next("table")
        hdr = grid(nxt)
        data_t = nxt.find_next("table")
        rows = grid(data_t)
        if not hdr or not rows:
            continue
        cols = hdr[0][2:]
        out.append((per, cols, rows))
    return out


def parse_hillsky(h, today):
    s = BeautifulSoup(h, "lxml")
    out = []
    for t in s.find_all("table"):
        if t.find("table"):
            continue
        g = grid(t)
        if not g or "티오프" not in " ".join(g[0]):
            continue
        prev = t.find_previous(string=re.compile(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일"))
        per = period_dates(str(prev), today.year) if prev else None
        if not per:
            continue
        cols = g[0][2:6]
        rows = [r[:6] for r in g[1:]]
        out.append((per, cols, rows))
    return out


PARSERS = {"bomun": parse_bomun, "hillsky": parse_hillsky}


def apply_parser(c, h, today):
    periods = PARSERS[c["monitor"]["parser"]](h, today)
    cur = [p for p in periods if p[0][0] <= today <= p[0][1]]
    if not cur:
        return False
    (a, b), cols, rows = cur[0]
    sm = summarize(rows, cols)
    if not sm:
        return False
    o = c["official"]
    o.update(sm)
    o["period"] = f"{a:%Y.%m.%d} ~ {b:%m.%d}"
    o["valid_from"], o["valid_to"] = a.isoformat(), b.isoformat()
    o["status"] = "verified"
    lines = []
    for ci, col in enumerate(cols):
        vals = [num(r[2 + ci]) for r in rows if len(r) > 2 + ci and num(r[2 + ci])]
        if vals:
            lines.append(f"{col}: {min(vals):,} ~ {max(vals):,}")
    nxt = [p for p in periods if p[0][0] > today]
    for (na, nb), ncols, nrows in nxt[:1]:
        ns = summarize(nrows, ncols)
        if ns:
            lines.append(f"다음 기간({na:%m.%d}~{nb:%m.%d}): 평일 {ns['weekday']['min']:,}~{ns['weekday']['max']:,}"
                         f" / 주말 {ns['weekend']['min']:,}~{ns['weekend']['max']:,}")
    o["rows"] = lines
    o["auto_parsed_at"] = now_kst().isoformat(timespec="seconds")
    return True


def section_hash(h, marker):
    s = BeautifulSoup(h, "lxml")
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    txt = re.sub(r"\s+", " ", s.get_text(" "))
    i = txt.find(marker)
    seg = txt[i:i + 4000] if i >= 0 else txt
    seg = re.sub(r"\b20\d\d[.\-/]\d{2}[.\-/]\d{2}\b", "", seg)  # 페이지 상단 '오늘 날짜' 등 제거
    imgs = re.findall(r"/uploads/[^\"' ]+|/static/NOTICE/[^\"' ]+", h)
    return hashlib.sha1((seg + "|".join(imgs)).encode()).hexdigest()


def check_official(courses):
    if BeautifulSoup is None:
        log("bs4 미설치: 공식 페이지 감시 생략 (pip install beautifulsoup4 lxml)")
        return
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    today = now_kst().date()
    for c in courses:
        m = c.get("monitor")
        o = c["official"]
        # 기간이 지난 요금표 표시
        if o.get("valid_to") and dt.date.fromisoformat(o["valid_to"]) < today and o.get("status") == "verified":
            o["status"] = "stale"
            o["note"] = (o.get("note", "") + " 게시 기간이 지났습니다. 최신 요금표를 확인하세요.").strip()
        if not m:
            continue
        try:
            h = fetch_html(m["url"])
        except Exception as e:
            o["check_error"] = f"공식 페이지 접속 실패: {str(e)[:80]}"
            log(c["name"], "공식 페이지 실패", e)
            continue
        o["checked_at"] = now_kst().isoformat(timespec="seconds")
        if m.get("parser"):
            try:
                ok = apply_parser(c, h, today)
                log(c["name"], "자동 파싱", "성공" if ok else "해당 기간 표 없음")
                if ok:
                    continue
            except Exception as e:
                log(c["name"], "자동 파싱 오류", e)
        hv = section_hash(h, m.get("marker", ""))
        prev = state.get(c["id"])
        if prev is None:
            state[c["id"]] = {"hash": hv, "baseline_at": now_kst().isoformat(timespec="seconds")}
        elif prev["hash"] != hv:
            o["changed"] = True
            o["changed_note"] = "공식 요금 페이지 내용이 검증 시점 이후 바뀌었습니다. 새 요금을 확인하세요."
            log(c["name"], "공식 페이지 변경 감지")
        time.sleep(DELAY)
    json.dump(state, open(STATE, "w"), ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 출력
def build_html(data, tt=None):
    tpl = open(TEMPLATE, encoding="utf-8").read()
    js = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    payload = js(data)
    page = tpl.replace("/*__EMBEDDED_DATA__*/null", payload)
    # golf-fees.html: 파일 하나로 동작하도록 티타임 상세까지 내장
    open(OUT_STANDALONE, "w", encoding="utf-8").write(
        page.replace("/*__EMBEDDED_TEETIMES__*/null", js(tt) if tt else "null"))
    # index.html: data.json / teetimes.json 을 먼저 읽고(티타임은 '시간대별' 탭에서 지연 로드),
    # 실패하면(파일로 열 때 등) 내장 요약 데이터 사용
    open(OUT_INDEX, "w", encoding="utf-8").write(
        page.replace("const PREFER_JSON = false", "const PREFER_JSON = true"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--no-official", action="store_true")
    a = ap.parse_args()

    base = json.load(open(BASE, encoding="utf-8"))
    courses = base["courses"]
    prev = json.load(open(OUT_JSON, encoding="utf-8")) if os.path.exists(OUT_JSON) else {}

    if not a.no_official:
        check_official(courses)
    live = prev.get("live")
    tt = json.load(open(OUT_TT, encoding="utf-8")) if os.path.exists(OUT_TT) else None
    if not a.no_live:
        try:
            new_live, new_tt = collect_live(courses, a.days)
            ok_days = sum(1 for cv in new_live["courses"].values()
                          for v in cv["days"].values() if v.get("min18") or v.get("count"))
            if ok_days == 0:
                log("실시간 수집 결과가 비어 있음(접속 차단 등), 이전 데이터 유지")
            else:
                live, tt = new_live, new_tt
        except Exception as e:
            log("실시간 수집 실패, 이전 데이터 유지:", e)
    data = {"meta": {**base["meta"], "generated_at": now_kst().isoformat(timespec="seconds")},
            "courses": courses, "live": live}
    json.dump(data, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if tt:
        json.dump(tt, open(OUT_TT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    if os.path.exists(TEMPLATE):
        build_html(data, tt)
    st = {}
    for c in courses:
        st[c["official"]["status"]] = st.get(c["official"]["status"], 0) + 1
    log("완료:", len(courses), "개 골프장", st, "->", OUT_JSON)


if __name__ == "__main__":
    main()
