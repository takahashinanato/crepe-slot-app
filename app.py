# app.py
import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, time, timezone

# ===== 基本設定 =====
st.set_page_config(page_title="Crepe Slots", layout="centered")
JST = timezone(timedelta(hours=9))

SCOPE = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
SLOTS_SHEET = "slots"
TICKETS_SHEET = "tickets"

ISSUE_START = time(11, 0)   # 発券開始
ISSUE_END   = time(15, 30)  # 最終枠開始
SLOT_MINUTES = 30
CAP_PER_SLOT = 20

# ===== Google Sheets =====
@st.cache_resource(show_spinner=False)
def _client():
    creds = Credentials.from_service_account_info(
        st.secrets["google_service_account"], scopes=SCOPE
    )
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

def today_str():
    return datetime.now(JST).date().isoformat()

def ensure_headers():
    slots = ws(SLOTS_SHEET)
    if slots.row_values(1) != ["date","slot_start","slot_end","cap","issued","code"]:
        slots.clear()
        slots.update("A1",[["date","slot_start","slot_end","cap","issued","code"]])
    tickets = ws(TICKETS_SHEET)
    if tickets.row_values(1) != ["ticket_id","issued_at","date","slot_start","slot_end"]:
        tickets.clear()
        tickets.update("A1",[["ticket_id","issued_at","date","slot_start","slot_end"]])

@st.cache_data(show_spinner=False)
def slots_df(date_str: str):
    df = pd.DataFrame(ws(SLOTS_SHEET).get_all_records())
    if df.empty: 
        return pd.DataFrame(columns=["date","slot_start","slot_end","cap","issued","code"])
    return df[df["date"]==date_str].copy()

def ensure_today_slots(date_str: str):
    ensure_headers()
    slots_df.clear()
    df = slots_df(date_str)
    if not df.empty: return

    # 時間帯ごとにコード（A, B, C...）
    start_dt = datetime.combine(datetime.now(JST).date(), ISSUE_START, tzinfo=JST)
    end_last = datetime.combine(datetime.now(JST).date(), ISSUE_END, tzinfo=JST)
    rows, code = [], 65  # 65='A'

    cur = start_dt
    while cur <= end_last:
        s = cur.strftime("%H:%M")
        e = (cur + timedelta(minutes=SLOT_MINUTES)).strftime("%H:%M")
        rows.append([date_str, s, e, CAP_PER_SLOT, 0, chr(code)])
        code += 1
        cur += timedelta(minutes=SLOT_MINUTES)
    ws(SLOTS_SHEET).append_rows(rows, value_input_option="USER_ENTERED")
    slots_df.clear()

def issue_ticket(date_str: str, slot_start: str, slot_end: str):
    w = ws(SLOTS_SHEET)
    df_all = pd.DataFrame(w.get_all_records())
    recs = df_all[(df_all["date"]==date_str)&(df_all["slot_start"]==slot_start)]
    if recs.empty: raise RuntimeError("枠がありません")
    r = recs.iloc[0]

    cap = int(r["cap"]); issued = int(r["issued"]); code = r["code"]
    if issued >= cap: raise RuntimeError("満枠です")

    # 発券番号 = コード + 連番
    ticket_no = f"{code}-{issued+1:03d}"
    now = datetime.now(JST)

    # ticketsに記録
    ws(TICKETS_SHEET).append_row(
        [ticket_no, now.isoformat(), date_str, slot_start, slot_end],
        value_input_option="USER_ENTERED"
    )

    # issued+1更新
    headers = w.row_values(1)
    vals = w.get_all_values()
    i_date, i_s, i_e, i_issued = headers.index("date"), headers.index("slot_start"), headers.index("slot_end"), headers.index("issued")
    for i in range(1, len(vals)):
        row = vals[i]
        if row[i_date]==date_str and row[i_s]==slot_start and row[i_e]==slot_end:
            w.update_cell(i+1, i_issued+1, issued+1)
            break
    slots_df.clear()

    return ticket_no

# ===== UI =====
d = today_str()
ensure_today_slots(d)
st.title("発券ページ")

df = slots_df(d)
df["remain"] = df["cap"].astype(int) - df["issued"].astype(int)

st.caption("受付時間：11:00–15:30 / 各枠20名 / 発券後は番号をスクショしてください")

for _, r in df.iterrows():
    c1, c2, c3 = st.columns([2,1,2])
    with c1: st.write(f"**{r['slot_start']}–{r['slot_end']}**")
    with c2: st.write(f"残り: {r['remain']}/{int(r['cap'])}")
    with c3:
        if st.button("発券", key=f"issue-{r['slot_start']}"):
            try:
                no = issue_ticket(d, r["slot_start"], r["slot_end"])
                st.success("🎫 発券しました！")
                st.markdown(f"<h1 style='font-size:72px;text-align:center'>{no}</h1>", unsafe_allow_html=True)
                st.write("この番号をスクショしてレジで提示してください。")
                st.stop()
            except Exception as e:
                st.error(str(e))
