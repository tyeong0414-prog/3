# -*- coding: utf-8 -*-
"""머니 챙김장: 빌려준 돈·물건 기록 + 구독 D-day (Streamlit) - 실행: streamlit run app.py"""
import base64, calendar, hashlib, hmac, io, json, os, re, secrets, sqlite3, urllib.parse
from datetime import date, timedelta

import pandas as pd
import qrcode
import streamlit as st

APP_NAME = "머니 챙김장"
USERS_DB = "users.db"
TODAY = date.today()
st.set_page_config(page_title=APP_NAME, page_icon="💸", layout="centered")

CSS = """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
html, body, [class*="css"], .stApp { font-family: 'Pretendard', sans-serif; }
.stApp { background: linear-gradient(180deg,#f4f7ff 0%,#ffffff 320px); }
.block-container { padding-top: 1.2rem; max-width: 760px; }
.hero { background: linear-gradient(135deg,#4f46e5,#7c3aed 60%,#ec4899); color:#fff;
  padding: 22px 24px; border-radius: 22px; margin-bottom: 14px;
  box-shadow: 0 10px 28px rgba(79,70,229,.28); }
.hero h1 { margin:0; font-size:1.7rem; font-weight:800; color:#fff; }
.hero p { margin:4px 0 0; opacity:.9; font-size:.95rem; }
[data-testid="stMetric"] { background:#fff; border:1px solid #eceffd; border-radius:16px;
  padding:12px 14px; box-shadow:0 2px 10px rgba(30,41,99,.06); }
[data-testid="stMetricValue"] { font-weight:800; color:#312e81; }
.stTabs [data-baseweb="tab-list"] { gap:6px; overflow-x:auto; }
.stTabs [data-baseweb="tab"] { background:#eef0ff; border-radius:999px; padding:6px 14px; height:auto; }
.stTabs [aria-selected="true"] { background:#4f46e5; color:#fff !important; }
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display:none; }
div[data-testid="stExpander"] { background:#fff; border:1px solid #eceffd !important;
  border-radius:16px; box-shadow:0 2px 10px rgba(30,41,99,.05); margin-bottom:8px; }
.stButton>button, .stFormSubmitButton>button { border-radius:12px; font-weight:700; }
.stFormSubmitButton>button { background:#4f46e5; color:#fff; border:0; }
.scard { display:flex; align-items:center; gap:14px; background:#fff; border-radius:18px;
  padding:14px 16px; margin:8px 0 2px; border:1px solid #eceffd;
  box-shadow:0 3px 12px rgba(30,41,99,.07); }
.scard .badge { min-width:74px; text-align:center; font-weight:800; font-size:1.15rem;
  padding:10px 6px; border-radius:14px; color:#fff; }
.scard .nm { font-weight:700; font-size:1.02rem; color:#1e1b4b; }
.scard .sub { color:#6b7280; font-size:.85rem; }
.scard .amt { margin-left:auto; font-weight:800; color:#312e81; white-space:nowrap; }
.b-red{background:linear-gradient(135deg,#ef4444,#f97316)} .b-yel{background:linear-gradient(135deg,#f59e0b,#fbbf24)}
.b-blu{background:linear-gradient(135deg,#4f46e5,#6366f1)} .b-gry{background:#9ca3af}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# ---------------- DB ----------------
def db_path():
    u = st.session_state.get("user", "")
    return "loans_" + hashlib.sha256(u.encode()).hexdigest()[:16] + ".db"

def conn():
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS loans(
          id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT, person TEXT, kind TEXT,
          title TEXT, amount INTEGER DEFAULT 0, lent_date TEXT, due_date TEXT,
          done INTEGER DEFAULT 0, memo TEXT, photo TEXT);
        CREATE TABLE IF NOT EXISTS repayments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, loan_id INTEGER, pay_date TEXT, amount INTEGER, note TEXT);
        CREATE TABLE IF NOT EXISTS reminders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, loan_id INTEGER, sent_date TEXT, tone TEXT);
        CREATE TABLE IF NOT EXISTS notes(person TEXT PRIMARY KEY, memo TEXT);
        CREATE TABLE IF NOT EXISTS subs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, amount INTEGER, cycle TEXT,
          base_date TEXT, trial INTEGER DEFAULT 0, active INTEGER DEFAULT 1, memo TEXT);
        """)

def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]

def x(sql, args=()):
    with conn() as c:
        c.execute(sql, args)

def load_df():
    loans = q("SELECT * FROM loans")
    if not loans:
        return pd.DataFrame()
    df = pd.DataFrame(loans)
    rep = pd.DataFrame(q("SELECT loan_id, SUM(amount) paid FROM repayments GROUP BY loan_id"))
    df = df.merge(rep, how="left", left_on="id", right_on="loan_id") if not rep.empty else df.assign(paid=0)
    df["paid"] = df["paid"].fillna(0).astype(int)
    df["remain"] = df.apply(lambda r: max(r["amount"] - r["paid"], 0) if r["kind"] == "돈" else 0, axis=1)
    def status(r):
        if r["done"]: return "반환완료"
        if r["due_date"] and r["due_date"] < TODAY.isoformat(): return "연체"
        if r["paid"] > 0: return "일부상환"
        return "대기"
    df["status"] = df.apply(status, axis=1)
    return df

won = lambda n: f"{int(n):,}원"

# ---------------- 구독 D-day ----------------
CYCLES = {"매주": None, "매월": 1, "3개월": 3, "매년": 12}

def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))

def next_charge(base, cycle):
    """결제 기준일부터 주기를 더해 오늘 이후 첫 결제일을 계산"""
    base = date.fromisoformat(base)
    k = 0
    while True:
        d = base + timedelta(days=7 * k) if cycle == "매주" else add_months(base, CYCLES[cycle] * k)
        if d >= TODAY:
            return d
        k += 1

def monthly_cost(amount, cycle):
    return {"매주": amount * 52 / 12, "매월": amount, "3개월": amount / 3, "매년": amount / 12}[cycle]

def dlabel(n):
    return "D-DAY 🔥" if n == 0 else f"D-{n}"

def load_subs():
    rows = q("SELECT * FROM subs")
    for r in rows:
        r["next"] = next_charge(r["base_date"], r["cycle"])
        r["dday"] = (r["next"] - TODAY).days
    return sorted(rows, key=lambda r: (not r["active"], r["dday"]))

def sub_alerts(subs):
    for r in subs:
        if not r["active"]: continue
        tag = "🆓 무료체험 종료 → 결제" if r["trial"] else "💳 결제"
        if r["dday"] == 0:
            st.error(f"🔥 오늘 {tag}: {r['name']} {won(r['amount'])}")
        elif r["dday"] <= 3:
            st.warning(f"⏰ {dlabel(r['dday'])} {tag}: {r['name']} {won(r['amount'])} ({r['next']})")

def subs_tab(subs):
    st.caption("정기결제·무료체험이 언제 얼마 나가는지 D-day로 확인하고, 안 쓰면 미리 해지하세요.")
    with st.form("addsub", clear_on_submit=True):
        name = st.text_input("서비스 이름*", placeholder="예: 넷플릭스, 쿠팡 와우, 헬스장")
        c1, c2 = st.columns(2)
        amount = c1.number_input("결제 금액(원)", min_value=0, step=1000, value=0)
        cycle = c2.selectbox("결제 주기", list(CYCLES), index=1)
        base = st.date_input("결제일 (다음 결제일 또는 지난 결제일 아무거나)", TODAY + timedelta(days=7))
        trial = st.checkbox("무료체험 중 (이 날짜에 첫 결제가 시작됨)")
        memo = st.text_input("메모 (해지 방법, 계정 등)")
        if st.form_submit_button("➕ 구독 추가", width="stretch"):
            if not name.strip():
                st.error("서비스 이름을 입력하세요.")
            else:
                x("INSERT INTO subs(name,amount,cycle,base_date,trial,memo) VALUES(?,?,?,?,?,?)",
                  (name.strip(), int(amount), cycle, base.isoformat(), int(trial), memo))
                st.rerun()
    act = [r for r in subs if r["active"]]
    if act:
        m = sum(monthly_cost(r["amount"], r["cycle"]) for r in act)
        within = sum(r["amount"] for r in act if r["dday"] <= 30)
        a, b, c = st.columns(3)
        a.metric("월 환산 고정지출", won(m))
        b.metric("연간 예상", won(m * 12))
        c.metric("30일 내 결제 예정", won(within))
    if not subs:
        st.info("등록된 구독이 없습니다."); return
    for r in subs:
        k = r["id"]
        if r["active"]:
            cls = "b-red" if r["dday"] <= 3 else "b-yel" if r["dday"] <= 7 else "b-blu"
            bd, tag = ("D-DAY" if r["dday"] == 0 else f"D-{r['dday']}"), (" · 🆓 무료체험" if r["trial"] else "")
        else:
            cls, bd, tag = "b-gry", "해지", ""
        st.markdown(f'<div class="scard"><div class="badge {cls}">{bd}</div><div><div class="nm">{r["name"]}</div>'
                    f'<div class="sub">{r["cycle"]} 결제{tag}</div></div><div class="amt">{won(r["amount"])}</div></div>',
                    unsafe_allow_html=True)
        with st.expander("자세히 · 관리"):
            if r["active"]:
                st.write(f"다음 결제일: **{r['next']}** ({dlabel(r['dday'])}) · 월 환산 {won(monthly_cost(r['amount'], r['cycle']))}")
                st.progress(max(0.0, min(1.0, 1 - r["dday"] / 30)), text="결제일까지 (30일 기준)")
            if r["memo"]: st.caption(r["memo"])
            c1, c2 = st.columns(2)
            if r["active"]:
                if c1.button("⏹️ 해지 완료 처리", key=f"sc{k}"):
                    x("UPDATE subs SET active=0 WHERE id=?", (k,)); st.rerun()
            elif c1.button("▶️ 다시 사용", key=f"sr{k}"):
                x("UPDATE subs SET active=1 WHERE id=?", (k,)); st.rerun()
            if c2.button("🗑️ 삭제", key=f"sd{k}"):
                x("DELETE FROM subs WHERE id=?", (k,)); st.rerun()

# ---------------- 잠금 ----------------
def get_pin():
    try:
        return str(st.secrets.get("PIN", ""))
    except Exception:
        return ""

# ---------------- 계정(로그인) ----------------
def users_db():
    c = sqlite3.connect(USERS_DB)
    c.execute("CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, salt TEXT, pw TEXT, created TEXT)")
    return c

def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()

def signup(name, pw, pw2):
    if not re.fullmatch(r"[A-Za-z0-9_가-힣]{3,20}", name): return "아이디는 3~20자의 한글·영문·숫자·_ 만 가능합니다."
    if len(pw) < 6: return "비밀번호는 6자 이상이어야 합니다."
    if pw != pw2: return "비밀번호 확인이 일치하지 않습니다."
    salt = secrets.token_hex(16)
    try:
        with users_db() as c:
            c.execute("INSERT INTO users VALUES(?,?,?,?)", (name.lower(), salt, hash_pw(pw, salt), date.today().isoformat()))
    except sqlite3.IntegrityError:
        return "이미 사용 중인 아이디입니다."
    return None

def check_login(name, pw):
    with users_db() as c:
        r = c.execute("SELECT salt, pw FROM users WHERE username=?", (name.lower(),)).fetchone()
    return bool(r) and hmac.compare_digest(r[1], hash_pw(pw, r[0]))

LOGIN_CSS = """
<style>
.login-hero { text-align:center; background: linear-gradient(135deg,#4f46e5,#7c3aed 60%,#ec4899); color:#fff;
  padding: 34px 20px 28px; border-radius: 26px; margin: 8vh 0 18px; box-shadow: 0 14px 34px rgba(79,70,229,.30); }
.login-hero .logo { font-size:3.2rem; line-height:1; }
.login-hero h1 { margin:8px 0 4px; font-size:1.9rem; font-weight:800; color:#fff; }
.login-hero p { margin:0; opacity:.92; }
.chips { display:flex; justify-content:center; gap:8px; margin-top:14px; flex-wrap:wrap; }
.chips span { background:rgba(255,255,255,.2); padding:4px 12px; border-radius:999px; font-size:.82rem; }
</style>
"""

def gate():
    if st.session_state.get("user"):
        return
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)
    st.markdown(f'<div class="login-hero"><div class="logo">💸</div><h1>{APP_NAME}</h1>'
                '<p>빌려준 건 받아내고, 새는 구독료는 D-day로 막는다</p>'
                '<div class="chips"><span>🤝 빌려준 기록</span><span>🔔 구독 D-day</span><span>📱 QR 접속</span></div></div>',
                unsafe_allow_html=True)
    tl, ts = st.tabs(["🔑 로그인", "✨ 회원가입"])
    with tl:
        with st.form("login"):
            n = st.text_input("아이디")
            p = st.text_input("비밀번호", type="password")
            if st.form_submit_button("로그인", width="stretch"):
                fails = st.session_state.get("fails", 0)
                if fails >= 5:
                    st.error("로그인 시도가 너무 많습니다. 페이지를 새로고침한 뒤 다시 시도하세요.")
                elif check_login(n.strip(), p):
                    st.session_state.user = n.strip().lower(); st.session_state.fails = 0; st.rerun()
                else:
                    st.session_state.fails = fails + 1
                    st.error("아이디 또는 비밀번호가 올바르지 않습니다.")
    with ts:
        with st.form("signup"):
            n = st.text_input("사용할 아이디", help="3~20자, 한글·영문·숫자·_")
            p = st.text_input("비밀번호 (6자 이상)", type="password")
            p2 = st.text_input("비밀번호 확인", type="password")
            if st.form_submit_button("가입하고 시작하기", width="stretch"):
                err = signup(n.strip(), p, p2)
                if err: st.error(err)
                else:
                    st.session_state.user = n.strip().lower(); st.rerun()
    st.caption("계정마다 데이터가 분리되어 저장됩니다. 비밀번호는 암호화(해시)되어 저장되며 복구할 수 없습니다.")
    st.stop()

# ---------------- 메시지 ----------------
TONES = ["친구(말랑)", "직장 동료(정중)", "가족(편하게)", "단호하게"]
def make_msg(r, tone):
    who, what = r["person"], r["title"]
    amt = f"{won(r['remain'])}" if r["kind"] == "돈" else f"'{what}'"
    due = f" ({r['due_date']}까지)" if r["due_date"] else ""
    T = {
     "친구(말랑)": f"{who}야~ 지난번에 빌려준 {amt} 기억나? 슬슬 챙겨야 할 것 같아서ㅎㅎ 편할 때 알려줘!{due}",
     "직장 동료(정중)": f"{who}님, 안녕하세요. 이전에 빌려드린 {amt} 건으로 연락드립니다{due}. 편하신 시간에 말씀 부탁드립니다.",
     "가족(편하게)": f"{who}, 빌려간 {amt} 언제쯤 돌려줄 수 있어? 날짜 정해서 알려줘{due}.",
     "단호하게": f"{who}님, 약속하신 {amt} 반환 기한{due}이 지났습니다. 이번 주 내로 일정을 확정해 연락 주세요.",
    }
    return T[tone]

# ---------------- 화면 조각 ----------------
def alerts(df):
    if df.empty: return
    act = df[(df.done == 0)]
    over = act[act.status == "연체"]
    soon = act[act.due_date.fillna("").between(TODAY.isoformat(), (TODAY + timedelta(days=1)).isoformat())]
    if len(over): st.error(f"⚠️ 연체 {len(over)}건: " + ", ".join(over.person + "·" + over.title))
    if len(soon): st.warning(f"⏰ 오늘·내일 기한 {len(soon)}건: " + ", ".join(soon.person + "·" + soon.title))
    cnt = act[act.direction == "빌려줌"].groupby("person").size()
    for p, n in cnt.items():
        if n >= 3: st.info(f"✋ {p}님에게 미회수 {n}건 - 추가로 빌려주기 전에 확인하세요.")

def add_form():
    with st.form("add", clear_on_submit=True):
        direction = st.radio("구분", ["빌려줌", "빌림"], horizontal=True)
        person = st.text_input("상대방 이름*")
        kind = st.radio("종류", ["돈", "물건"], horizontal=True)
        title = st.text_input("물건명 / 용도*", placeholder="예: 전동드릴, 점심값")
        amount = st.number_input("금액(원) - 돈일 때", min_value=0, step=1000)
        c1, c2 = st.columns(2)
        d = c1.date_input("빌려준 날", TODAY)
        has_due = c2.checkbox("기한 있음", True)
        due = c2.date_input("돌려받을 기한", TODAY + timedelta(days=14))
        memo = st.text_area("메모")
        pic = st.file_uploader("사진/송금내역 (선택)", type=["png", "jpg", "jpeg"])
        if st.form_submit_button("➕ 저장", width="stretch"):
            if not person.strip() or not title.strip():
                st.error("이름과 물건명은 필수입니다.")
            elif kind == "돈" and amount <= 0:
                st.error("금액을 입력하세요.")
            else:
                ph = base64.b64encode(pic.getvalue()).decode() if pic else None
                x("INSERT INTO loans(direction,person,kind,title,amount,lent_date,due_date,memo,photo) VALUES(?,?,?,?,?,?,?,?,?)",
                  (direction, person.strip(), kind, title.strip(), int(amount) if kind == "돈" else 0,
                   d.isoformat(), due.isoformat() if has_due else None, memo, ph))
                st.success("저장했습니다!")

def card(r):
    icon = {"대기": "🟦", "일부상환": "🟨", "연체": "🟥", "반환완료": "✅"}[r["status"]]
    amt = f" · 남은 {won(r['remain'])}/{won(r['amount'])}" if r["kind"] == "돈" else ""
    due = f" · 기한 {r['due_date']}" if r["due_date"] else ""
    with st.expander(f"{icon} {r['person']} - {r['title']}{amt}{due}"):
        st.caption(f"{r['direction']} · {r['lent_date']} · 상태: {r['status']}")
        if r["memo"]: st.write(r["memo"])
        if r["photo"]: st.image(base64.b64decode(r["photo"]), width=240)
        reps = q("SELECT * FROM repayments WHERE loan_id=? ORDER BY pay_date", (r["id"],))
        if reps:
            st.dataframe(pd.DataFrame(reps)[["pay_date", "amount", "note"]].rename(
                columns={"pay_date": "날짜", "amount": "금액", "note": "메모"}), hide_index=True)
        k = r["id"]
        if not r["done"]:
            if r["kind"] == "돈":
                c1, c2 = st.columns([2, 1])
                pay = c1.number_input("상환 금액", 0, int(r["remain"]), 0, 1000, key=f"p{k}")
                if c2.button("상환 기록", key=f"pb{k}") and pay > 0:
                    x("INSERT INTO repayments(loan_id,pay_date,amount,note) VALUES(?,?,?,?)",
                      (k, TODAY.isoformat(), int(pay), ""))
                    if pay >= r["remain"]: x("UPDATE loans SET done=1 WHERE id=?", (k,))
                    st.rerun()
            if st.button("✅ 반환 완료", key=f"d{k}"):
                x("UPDATE loans SET done=1 WHERE id=?", (k,)); st.rerun()
            if r["direction"] == "빌려줌":
                tone = st.selectbox("말투", TONES, key=f"t{k}")
                msg = make_msg(r, tone)
                st.text_area("추천 문구 (복사해서 사용)", msg, key=f"m{k}")
                enc = urllib.parse.quote(msg)
                st.markdown(f"[📱 문자로 보내기](sms:?&body={enc})")
                if st.button("📨 독촉 기록 남기기", key=f"r{k}"):
                    x("INSERT INTO reminders(loan_id,sent_date,tone) VALUES(?,?,?)", (k, TODAY.isoformat(), tone))
                    st.rerun()
        else:
            if st.button("↩️ 완료 취소", key=f"u{k}"):
                x("UPDATE loans SET done=0 WHERE id=?", (k,)); st.rerun()
        rem = q("SELECT sent_date, tone FROM reminders WHERE loan_id=?", (k,))
        if rem: st.caption("독촉 이력: " + ", ".join(f"{a['sent_date']}({a['tone']})" for a in rem))
        if st.button("🗑️ 삭제", key=f"x{k}"):
            for t, col in (("repayments", "loan_id"), ("reminders", "loan_id")):
                x(f"DELETE FROM {t} WHERE {col}=?", (k,))
            x("DELETE FROM loans WHERE id=?", (k,)); st.rerun()

def list_tab(df, direction):
    if df.empty:
        st.info("아직 기록이 없습니다. '➕ 등록' 탭에서 추가하세요."); return
    d = df[df.direction == direction]
    c1, c2, c3 = st.columns([2, 2, 2])
    kw = c1.text_input("검색(이름/물건)", key=f"kw{direction}")
    stt = c2.multiselect("상태", ["대기", "일부상환", "연체", "반환완료"], ["대기", "일부상환", "연체"], key=f"st{direction}")
    sort = c3.selectbox("정렬", ["기한 임박순", "최근 등록순", "사람별"], key=f"so{direction}")
    d = d[d.status.isin(stt)]
    if kw: d = d[d.person.str.contains(kw) | d.title.str.contains(kw)]
    d = {"기한 임박순": d.sort_values("due_date", na_position="last"),
         "최근 등록순": d.sort_values("id", ascending=False),
         "사람별": d.sort_values("person")}[sort]
    if d.empty: st.caption("조건에 맞는 항목이 없습니다."); return
    for _, r in d.iterrows(): card(r)

def people_tab(df):
    if df.empty: st.info("데이터가 없습니다."); return
    m = df[(df.kind == "돈") & (df.done == 0)]
    if not m.empty:
        g = m.pivot_table(index="person", columns="direction", values="remain", aggfunc="sum", fill_value=0)
        for c in ("빌려줌", "빌림"):
            if c not in g: g[c] = 0
        g["순잔액(받을 돈)"] = g["빌려줌"] - g["빌림"]
        st.subheader("사람별 잔액"); st.dataframe(g, width="stretch")
    st.subheader("사람별 메모")
    p = st.selectbox("사람", sorted(df.person.unique()))
    cur = q("SELECT memo FROM notes WHERE person=?", (p,))
    memo = st.text_area("개인 메모(예: 잘 갚는 편)", cur[0]["memo"] if cur else "")
    if st.button("메모 저장"):
        x("INSERT OR REPLACE INTO notes VALUES(?,?)", (p, memo)); st.success("저장됨")

def summary_tab(df):
    if df.empty: st.info("데이터가 없습니다."); return
    ym = TODAY.strftime("%Y-%m")
    mm = df[(df.kind == "돈") & (df.direction == "빌려줌")]
    this = mm[mm.lent_date.str.startswith(ym)]
    a, b, c = st.columns(3)
    a.metric("이번 달 빌려준 돈", won(this.amount.sum()))
    b.metric("누적 회수액", won(mm.paid.sum()))
    c.metric("미회수 잔액", won(mm[mm.done == 0].remain.sum()))
    st.metric("빌린 돈 잔액(갚을 돈)", won(df[(df.kind == "돈") & (df.direction == "빌림") & (df.done == 0)].remain.sum()))
    st.bar_chart(df.groupby("status").size())

def backup_tab():
    data = {"loans": q("SELECT * FROM loans"), "repayments": q("SELECT * FROM repayments"),
            "reminders": q("SELECT * FROM reminders"), "notes": q("SELECT * FROM notes"), "subs": q("SELECT * FROM subs")}
    st.warning("무료 클라우드 서버는 재시작 시 데이터가 초기화될 수 있습니다. 주기적으로 백업하세요.")
    st.download_button("⬇️ 백업 내려받기(JSON)", json.dumps(data, ensure_ascii=False), f"loans_{TODAY}.json", "application/json")
    up = st.file_uploader("백업 복원(JSON)", type="json")
    if up and st.button("복원 실행(현재 데이터 덮어씀)"):
        d = json.load(up)
        with conn() as c:
            for t in ("loans", "repayments", "reminders", "notes", "subs"):
                c.execute(f"DELETE FROM {t}")
                for row in d.get(t, []):
                    cols = ",".join(row); ph = ",".join("?" * len(row))
                    c.execute(f"INSERT INTO {t}({cols}) VALUES({ph})", list(row.values()))
        st.success("복원 완료"); st.rerun()
    st.divider()
    st.subheader("📱 QR로 접속")
    url = st.text_input("배포된 앱 주소", st.session_state.get("url", ""), placeholder="https://내앱.streamlit.app")
    if url:
        st.session_state.url = url
        img = qrcode.make(url); buf = io.BytesIO(); img.save(buf, "PNG")
        st.image(buf.getvalue(), width=220, caption="휴대폰 카메라로 스캔하세요")
        st.download_button("QR 이미지 저장", buf.getvalue(), "app_qr.png", "image/png")

# ---------------- main ----------------
gate()
init_db()
st.markdown(f'<div class="hero"><h1>💸 {APP_NAME}</h1><p>{st.session_state.user}님, 오늘도 새는 돈 없이 챙겨요 👋</p></div>', unsafe_allow_html=True)
if st.button("🚪 로그아웃"):
    st.session_state.clear(); st.rerun()
df = load_df()
subs = load_subs()
sub_alerts(subs)
alerts(df)
t = st.tabs(["🔔 구독 D-day", "➕ 등록", "📤 빌려준 것", "📥 빌린 것", "👥 사람별", "📊 요약", "⚙️ 백업·QR"])
with t[0]: subs_tab(subs)
with t[1]: add_form()
with t[2]: list_tab(df, "빌려줌")
with t[3]: list_tab(df, "빌림")
with t[4]: people_tab(df)
with t[5]: summary_tab(df)
with t[6]: backup_tab()
