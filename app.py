# streamlit run app.py
import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, time, timezone

# ====== ブランド / 文言（ここを変えるだけで表示が変わります） ======
FESTIVAL_NAME = "摩耶祭"
GROUP_NAME = "バドミントン部"
TAGLINE = "今年もクレープ続けました"
POST_ISSUE_MESSAGE = (
    "ご注文ありがとうございます。ご予約時間になりましたら**時間指定列**にお越しくださいませ。"
    "ご予約時間外にお越しいただいた場合、通常列へのご案内となります。予めご了承ください。"
    "※混雑状況により時間指定列へご案内致します。スタッフにお声掛けください。"
)

# ====== 基本設定 ======
st.set_page_config(page_title="Crepe Ticket", layout="centered")
JST = timezone(timedelta(hours=9))

SCOPE = ["https://www.googleapis.com/auth/spreadsheets",
         "https://www.googleapis.com/auth/drive"]
SLOTS_SHEET = "slots"
TICKETS_SHEET = "tickets"

ISSUE_START = time(11, 0)
ISSUE_END   = time(15, 30)
SLOT_MINUTES = 30
CAP_PER_SLOT = 20
EXPIRE_EXTRA_MIN = 30  # 枠終了+30分

# ====== Google Sheets 接続（キャッシュ） ======
@st.cache_resource(show_spinner=False)
def _client():
    info = dict(st.secrets["google_service_account"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPE)
    return gspread.authorize(creds)

@st.cache_resource(show_spinner=False)
def _sh():
    return _client().open_by_key(st.secrets["SHEET_ID"])

def ws(name: str):
    """ワークシート取得。無ければ作成して返す"""
    sh = _sh()
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=1000, cols=26)
    except gspread.exceptions.APIError:
        st.error("Google Sheets にアクセスできません。SHEET_ID と共有設定（client_emailを編集者で追加）を確認してください。")
        raise

# ====== ヘッダ / 当日枠生成 ======
def ensure_headers():
    w1 = ws(SLOTS_SHEET)
    h1 = ["date","slot_start","slot_end","cap","issued","code"]
    if w1.row_values(1) != h1:
        w1.clear(); w1.update("A1", [h1])

    w2 = ws(TICKETS_SHEET)
    h2 = ["ticket_id","issued_at","date","slot_start","slot_end","expires_at"]
    if w2.row_values(1) != h2:
        w2.clear(); w2.update("A1", [h2])

def today_str():
    return datetime.now(JST).date().isoformat()

@st.cache_data(show_spinner=False)
def slots_df(date_str: str) -> pd.DataFrame:
    df = pd.DataFrame(ws(SLOTS_SHEET).get_all_records())
    if df.empty:
        return pd.DataFrame(columns=["date","slot_start","slot_end","cap","issued","code"])
    return df[df["date"]==date_str].copy()

@st.cache_data(show_spinner=False)
def tickets_df(date_str: str) -> pd.DataFrame:
    df = pd.DataFrame(ws(TICKETS_SHEET).get_all_records())
    if df.empty:
        return pd.DataFrame(columns=["ticket_id","issued_at","date","slot_start","slot_end","expires_at"])
    return df[df["date"]==date_str].copy()

def ensure_today_slots(date_str: str):
    ensure_headers()
    slots_df.clear()
    df = slots_df(date_str)
    if not df.empty: return

    # 11:00〜15:30 / 30分刻み。A, B, C... を割当
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

# ====== 発券処理 ======
def _to_expiry(date_str: str, slot_end_hm: str) -> datetime:
    h, m = map(int, slot_end_hm.split(":"))
    d = datetime.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=JST) + timedelta(minutes=EXPIRE_EXTRA_MIN)

def issue_ticket(date_str: str, slot_start: str, slot_end: str):
    w = ws(SLOTS_SHEET)
    df_all = pd.DataFrame(w.get_all_records())
    recs = df_all[(df_all["date"]==date_str) & (df_all["slot_start"]==slot_start) & (df_all["slot_end"]==slot_end)]
    if recs.empty: raise RuntimeError("枠が見つかりません")
    r = recs.iloc[0]

    cap = int(r["cap"]); issued = int(r["issued"]); code = r["code"]
    if issued >= cap: raise RuntimeError("満枠です")

    ticket_no = f"{code}-{issued+1:03d}"
    now = datetime.now(JST)
    expires = _to_expiry(date_str, slot_end)

    # tickets へ記録
    ws(TICKETS_SHEET).append_row(
        [ticket_no, now.isoformat(), date_str, slot_start, slot_end, expires.isoformat()],
        value_input_option="USER_ENTERED"
    )

    # issued +1
    headers = w.row_values(1)
    vals = w.get_all_values()
    i_date, i_s, i_e, i_issued = headers.index("date"), headers.index("slot_start"), headers.index("slot_end"), headers.index("issued")
    for i in range(1, len(vals)):
        row = vals[i]
        if row[i_date]==date_str and row[i_s]==slot_start and row[i_e]==slot_end:
            w.update_cell(i+1, i_issued+1, issued+1)
            break

    slots_df.clear(); tickets_df.clear()
    return {"ticket_id": ticket_no, "slot": f"{slot_start}–{slot_end}", "expires_at": expires}

# ====== 共通ヘッダ / チケット表示 ======
def brand_header():
    st.markdown(
        f"""
        <div style="text-align:center; margin: 4px 0 12px;">
          <div style="font-size:22px; font-weight:700;">{FESTIVAL_NAME} / {GROUP_NAME}</div>
          <div style="font-size:14px; opacity:.9">{TAGLINE}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def render_ticket(t, title="あなたの発券情報"):
    st.subheader(title)
    st.markdown(
        f"<div style='font-size:76px;font-weight:800;text-align:center;margin:8px 0 4px'>{t['ticket_id']}</div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"- 枠時間：{t['slot']}")
    st.markdown(f"- 有効期限：**{t['expires_at'].astimezone(JST).strftime('%H:%M')}** まで")
    st.warning("※ 期限切れの場合は通常列をご利用ください")
    # 発券後の案内メッセージ
    st.success(POST_ISSUE_MESSAGE)
    st.info("この画面をスクショしてください")

# ====== ルーティング & UI ======
st.markdown(
    "<div style='display:flex;gap:8px;margin:8px 0'>"
    "<a href='?view=lookup'><button>🔎 発券番号で再表示</button></a>"
    "</div>", unsafe_allow_html=True
)

view = st.query_params.get("view", "issue")
view = view[0] if isinstance(view, list) else view

d = today_str()
ensure_today_slots(d)

if view == "issue":
    brand_header()
    st.title("発券")

    # 同一ブラウザは当日1回ロック
    if st.session_state.get("issued_date") == d:
        t = {
            "ticket_id": st.session_state["ticket_id"],
            "slot":      st.session_state["slot"],
            "expires_at":st.session_state["expires_at"],
        }
        render_ticket(t, "本日は既に発券済みです")
        st.stop()

    df = slots_df(d)
    if df.empty:
        st.warning("本日の枠がありません")
        st.stop()

    def hm_to_time(hm): h,m = map(int, hm.split(":")); return time(h,m)
    df["remain"] = df["cap"].astype(int) - df["issued"].astype(int)
    df["disabled"] = df.apply(lambda r:
        (hm_to_time(r["slot_start"])<ISSUE_START) or
        (hm_to_time(r["slot_start"])>ISSUE_END) or
        (int(r["issued"])>=int(r["cap"])), axis=1
    )

    st.caption("受付：11:00–15:30 / 各30分枠20名 / 期限切れは通常列へ")

    for _, r in df.iterrows():
        c1,c2,c3 = st.columns([2,1,2])
        with c1: st.write(f"**{r['slot_start']}–{r['slot_end']}**（コード: {r['code']}）")
        with c2: st.write(f"残り: {int(r['remain'])}/{int(r['cap'])}")
        with c3:
            if st.button("発券", key=f"issue-{r['slot_start']}", disabled=bool(r["disabled"])):
                try:
                    res = issue_ticket(d, r["slot_start"], r["slot_end"])
                    # 当日ロック用に保存
                    st.session_state["issued_date"]  = d
                    st.session_state["ticket_id"]    = res["ticket_id"]
                    st.session_state["slot"]         = res["slot"]
                    st.session_state["expires_at"]   = res["expires_at"]
                    render_ticket(res)
                    st.stop()
                except Exception as e:
                    st.error(str(e))

elif view == "lookup":
    brand_header()
    st.title("発券番号で再表示")
    tid = st.text_input("発券番号 半角で入力（例 A-001）").strip()
    if st.button("表示") and tid:
        df = tickets_df(d)
        hit = df[df["ticket_id"]==tid]
        if hit.empty:
            st.error("本日の発券情報が見つかりません")
        else:
            r = hit.iloc[0]
            t = {
                "ticket_id": r["ticket_id"],
                "slot": f"{r['slot_start']}–{r['slot_end']}",
                "expires_at": datetime.fromisoformat(r["expires_at"]),
            }
            render_ticket(t)
else:
    brand_header()
    st.write("上のボタンから画面を選んでください。")
