# streamlit run app.py
# 学祭クレープ：超シンプル版
# - 画面：?view=issue（発券） / ?view=lookup（発券番号で再表示）
# - 発券：枠・有効期限・発券番号を大きく表示（スクショ運用）
# - 運営の完了チェック等は不要（紙で番号メモ）
# - QRは「発券ページのURLに飛ばすための店頭ポスター」でのみ使用（アプリ内では非表示）
#
# Google Sheets:
#  - slots(date, slot_start, slot_end, cap, issued, open, note)
#  - tickets(ticket_id, issued_at, date, slot_start, slot_end, expires_at, method, status)  # statusは使わないが残す
#
# Secrets:
#  ADMIN_PIN は不要
#  SHEET_ID = "<spreadsheet-id>"
#  [google_service_account]  # サービスアカウントJSON（項目ごと）
#    type = "service_account"
#    project_id = "..."
#    private_key_id = "..."
#    private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
#    client_email = "xxx@xxx.iam.gserviceaccount.com"
#    client_id = "..."
#    token_uri = "https://oauth2.googleapis.com/token"

from datetime import datetime, timedelta, time, timezone

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

# ===== 基本設定 =====
st.set_page_config(page_title="Crepe Slots", layout="wide")
JST = timezone(timedelta(hours=9))

SCOPE = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
SLOTS_SHEET = "slots"
TICKETS_SHEET = "tickets"

# 枠設定（必要ならここを変える）
ISSUE_START = time(11, 0)   # 発券開始
ISSUE_END   = time(15, 30)  # 最終開始（例：15:30-16:00枠）
SLOT_MINUTES = 30
CAP_PER_SLOT = 20
EXPIRE_EXTRA_MIN = 30       # 枠終了 + 30分 が有効期限

# ===== Google Sheets =====
@st.cache_resource(show_spinner=False)
def _client():
    info = st.secrets["google_service_account"].to_dict()
    creds = Credentials.from_service_account_info(info, scopes=SCOPE)
    return gspread.authorize(creds)

@st.cache_resource(show_spinner=False)
def _sh():
    return _client().open_by_key(st.secrets["SHEET_ID"])

def ws(name: str):
    sh = _sh()
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=1000, cols=26)

def _ensure_headers():
    w1 = ws(SLOTS_SHEET);  h1 = ["date","slot_start","slot_end","cap","issued","open","note"]
    if w1.row_values(1) != h1: w1.clear(); w1.update("A1",[h1])
    w2 = ws(TICKETS_SHEET); h2 = ["ticket_id","issued_at","date","slot_start","slot_end","expires_at","method","status"]
    if w2.row_values(1) != h2: w2.clear(); w2.update("A1",[h2])

def today_str(): 
    return datetime.now(JST).date().isoformat()

@st.cache_data(show_spinner=False)
def slots_df(date_str: str) -> pd.DataFrame:
    df = pd.DataFrame(ws(SLOTS_SHEET).get_all_records())
    if df.empty:
        return pd.DataFrame(columns=["date","slot_start","slot_end","cap","issued","open","note"])
    return df[df["date"]==date_str].copy()

@st.cache_data(show_spinner=False)
def tickets_df(date_str: str) -> pd.DataFrame:
    df = pd.DataFrame(ws(TICKETS_SHEET).get_all_records())
    if df.empty:
        return pd.DataFrame(columns=["ticket_id","issued_at","date","slot_start","slot_end","expires_at","method","status"])
    return df[df["date"]==date_str].copy()

def ensure_today_slots(date_str: str):
    _ensure_headers()
    slots_df.clear()
    df = slots_df(date_str)
    if not df.empty:
        return
    # 当日分の枠を自動生成
    start_dt = datetime.combine(datetime.now(JST).date(), ISSUE_START, tzinfo=JST)
    end_last = datetime.combine(datetime.now(JST).date(), ISSUE_END, tzinfo=JST)
    rows = []
    cur = start_dt
    while cur <= end_last:
        s = cur.strftime("%H:%M")
        e = (cur + timedelta(minutes=SLOT_MINUTES)).strftime("%H:%M")
        rows.append([date_str, s, e, CAP_PER_SLOT, 0, True, ""])
        cur += timedelta(minutes=SLOT_MINUTES)
    ws(SLOTS_SHEET).append_rows(rows, value_input_option="USER_ENTERED")
    slots_df.clear()

def _to_dt(date_str: str, hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    d = datetime.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=JST)

def _new_ticket_id(now: datetime) -> str:
    # 短いけど十分ユニークなID（MMDD-xxxxx）
    return now.strftime("%m%d") + f"-{int(now.timestamp())%100000:05d}"

def issue_ticket(date_str: str, slot_start: str, slot_end: str):
    # 最新枠を取得し上限チェック
    w = ws(SLOTS_SHEET)
    df_all = pd.DataFrame(w.get_all_records())
    recs = df_all[(df_all["date"]==date_str)&(df_all["slot_start"]==slot_start)&(df_all["slot_end"]==slot_end)]
    if recs.empty:
        raise RuntimeError("枠が見つかりません")
    r = recs.iloc[0]
    if str(r.get("open", True)).lower() not in ("true","1","yes"):
        raise RuntimeError("この枠は停止中です")
    cap = int(r.get("cap", CAP_PER_SLOT))
    issued = int(r.get("issued", 0))
    if issued >= cap:
        raise RuntimeError("満枠です")

    # チケット作成
    now = datetime.now(JST)
    expires = _to_dt(date_str, slot_end) + timedelta(minutes=EXPIRE_EXTRA_MIN)
    ticket_id = _new_ticket_id(now)

    ws(TICKETS_SHEET).append_row([
        ticket_id, now.isoformat(), date_str, slot_start, slot_end, expires.isoformat(), "mobile", "valid"
    ], value_input_option="USER_ENTERED")

    # issued +1
    headers = w.row_values(1)
    vals = w.get_all_values()
    i_date, i_s, i_e, i_issued = headers.index("date"), headers.index("slot_start"), headers.index("slot_end"), headers.index("issued")
    target = None
    for i in range(1, len(vals)):
        row = vals[i]
        if row[i_date]==date_str and row[i_s]==slot_start and row[i_e]==slot_end:
            target = i+1; break
    if target: w.update_cell(target, i_issued+1, issued+1)

    slots_df.clear(); tickets_df.clear()

    return {
        "ticket_id": ticket_id,
        "slot": f"{slot_start}–{slot_end}",
        "issued_at": now,
        "expires_at": expires,
    }

def render_ticket_card(ticket: dict, title="あなたの発券情報"):
    st.subheader(title)
    st.markdown(f"### 枠：**{ticket['slot']}**")
    st.markdown(f"- 発券時刻：**{ticket['issued_at'].astimezone(JST).strftime('%H:%M:%S')}**")
    st.markdown(f"- 有効期限：**{ticket['expires_at'].astimezone(JST).strftime('%H:%M')}** まで")
    st.markdown(f"- 発券番号：**{ticket['ticket_id']}**（この画面を**スクショ**してください）")

# ===== 画面共通の簡易ナビ =====
st.markdown(
    "<div style='display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 16px'>"
    "<a href='?view=issue'><button>🎫 発券</button></a>"
    "<a href='?view=lookup'><button>🔎 発券番号で再表示</button></a>"
    "</div>",
    unsafe_allow_html=True,
)

# ===== ルーティング =====
view = st.query_params.get("view", "issue")
view = view[0] if isinstance(view, list) else view
d = today_str()
ensure_today_slots(d)

# ===== 発券 =====
if view == "issue":
    st.title("発券")
    # 同一ブラウザ：本日1枚ロック（必要に応じて外してもOK）
    if st.session_state.get("issued_date")==d:
        ticket = {
            "ticket_id": st.session_state.get("ticket_id"),
            "slot": st.session_state.get("slot"),
            "issued_at": st.session_state.get("issued_at"),
            "expires_at": st.session_state.get("expires_at"),
        }
        render_ticket_card(ticket, "本日は既に発券済みです")
        st.success("上の発券情報を提示してください（スクショ可）")
        st.stop()

    df = slots_df(d)
    if df.empty:
        st.warning("本日の枠がありません")
        st.stop()

    def hm_to_time(hm): 
        h,m = map(int, hm.split(":")); return time(h,m)

    df["remain"] = df.apply(lambda r: int(r["cap"]) - int(r["issued"]), axis=1)
    df["disabled"] = df.apply(lambda r: (hm_to_time(r["slot_start"])<ISSUE_START)
                                        or (hm_to_time(r["slot_start"])>ISSUE_END)
                                        or (str(r["open"]).lower() not in ("true","1","yes"))
                                        or (int(r["issued"])>=int(r["cap"])), axis=1)
    st.caption("受付：11:00–15:30（各30分枠20名） / 有効期限=枠終了+30分 / 期限切れは通常列へ")

    for _, r in df.iterrows():
        c1,c2,c3 = st.columns([2,1,2])
        with c1: st.write(f"**{r['slot_start']}–{r['slot_end']}**")
        with c2: st.write(f"残り: {r['remain']}/{int(r['cap'])}")
        with c3:
            if st.button("発券する", key=f"issue-{r['slot_start']}", disabled=bool(r["disabled"])):
                try:
                    res = issue_ticket(d, r["slot_start"], r["slot_end"])
                    # セッション保存（本日ロック & 再表示用）
                    st.session_state["issued_date"] = d
                    st.session_state["ticket_id"] = res["ticket_id"]
                    st.session_state["slot"] = res["slot"]
                    st.session_state["issued_at"] = res["issued_at"]
                    st.session_state["expires_at"] = res["expires_at"]
                    render_ticket_card(res)
                    st.success("※ この画面をスクショしてレジで提示してください。")
                    st.stop()
                except Exception as e:
                    st.error(str(e))

# ===== 発券番号で再表示 =====
elif view == "lookup":
    st.title("発券番号で再表示")
    st.caption("スクショを失くした場合は、発券番号を入力してください。")
    tid = st.text_input("発券番号", placeholder="例 0928-12345").strip()
    if st.button("表示する") and tid:
        df = tickets_df(d)
        hit = df[df["ticket_id"]==tid]
        if hit.empty:
            st.error("本日の発券情報が見つかりません")
        else:
            r = hit.iloc[0]
            ticket = {
                "ticket_id": r["ticket_id"],
                "slot": f"{r['slot_start']}–{r['slot_end']}",
                "issued_at": datetime.fromisoformat(r["issued_at"]),
                "expires_at": datetime.fromisoformat(r["expires_at"]),
            }
            render_ticket_card(ticket)
else:
    st.write("上のボタンから画面を選んでください。")
