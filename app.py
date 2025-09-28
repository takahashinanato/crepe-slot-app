# streamlit run app.py
# 1ファイルで「発券 / 案内 / 管理」を実装
# 画面切替: ?view=issue / ?view=display / ?view=manage
# Google Sheets:
#  - slots(date, slot_start, slot_end, cap, issued, open, note)
#  - tickets(ticket_id, issued_at, date, slot_start, slot_end, expires_at, method, status)
#  - state(key, value)  # current_slot / banner / paused など
#
# Secrets (Streamlit Cloud の Secrets か .streamlit/secrets.toml)
# ADMIN_PIN = "1234"
# SHEET_ID = "<spreadsheet-id>"
# [google_service_account]
# ...サービスアカウントJSON...

import os
from datetime import datetime, timedelta, time, timezone
from urllib.parse import urlencode

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

# ===== 基本設定 =====
st.set_page_config(page_title="Crepe Slots", layout="wide")
JST = timezone(timedelta(hours=9))

SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SLOTS_SHEET = "slots"
TICKETS_SHEET = "tickets"
STATE_SHEET = "state"

OPEN_HOUR = 10
CLOSE_HOUR = 17
ISSUE_START = time(11, 0)
ISSUE_END = time(15, 30)      # 15:30-16:00 枠まで
SLOT_MINUTES = 30
CAP_PER_SLOT = 20
EXPIRE_EXTRA_MIN = 30

# ===== Google Sheets 接続 =====
@st.cache_resource(show_spinner=False)
def get_client():
    info = st.secrets["google_service_account"].to_dict()
    creds = Credentials.from_service_account_info(info, scopes=SCOPE)
    return gspread.authorize(creds)

@st.cache_resource(show_spinner=False)
def open_spreadsheet():
    gc = get_client()
    return gc.open_by_key(st.secrets["SHEET_ID"])

def get_ws(name: str):
    sh = open_spreadsheet()
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=1000, cols=26)

# ===== Utilities =====
def today_str():
    return datetime.now(JST).date().isoformat()

def ensure_headers(ws, headers):
    cur = ws.row_values(1)
    if cur == headers:
        return
    ws.clear()
    ws.update("A1", [headers])

@st.cache_data(show_spinner=False)
def list_slots_df(date_str: str) -> pd.DataFrame:
    ws = get_ws(SLOTS_SHEET)
    rows = ws.get_all_records()
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["date","slot_start","slot_end","cap","issued","open","note"])
    return df[df["date"] == date_str].copy()

@st.cache_data(show_spinner=False)
def list_tickets_df(date_str: str) -> pd.DataFrame:
    ws = get_ws(TICKETS_SHEET)
    rows = ws.get_all_records()
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["ticket_id","issued_at","date","slot_start","slot_end","expires_at","method","status"])
    return df[df["date"] == date_str].copy()

@st.cache_data(show_spinner=False)
def list_state_df() -> pd.DataFrame:
    ws = get_ws(STATE_SHEET)
    rows = ws.get_all_records()
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["key","value"])
    return df

def state_get(key: str, default: str = "") -> str:
    df = list_state_df()
    if df.empty:
        return default
    rec = df[df["key"] == key]
    if rec.empty:
        return default
    return str(rec.iloc[0]["value"])

def state_set(key: str, value: str):
    ws = get_ws(STATE_SHEET)
    ensure_headers(ws, ["key","value"])
    rows = ws.get_all_records()
    if not rows:
        ws.append_row([key, value], value_input_option="USER_ENTERED")
    else:
        keys = [r.get("key","") for r in rows]
        if key in keys:
            row_idx = keys.index(key) + 2
            ws.update_cell(row_idx, 2, str(value))
        else:
            ws.append_row([key, value], value_input_option="USER_ENTERED")
    list_state_df.clear()

def ensure_today_slots(date_str: str):
    # ヘッダ整備
    ws_slots = get_ws(SLOTS_SHEET)
    ensure_headers(ws_slots, ["date","slot_start","slot_end","cap","issued","open","note"])
    ws_t = get_ws(TICKETS_SHEET)
    ensure_headers(ws_t, ["ticket_id","issued_at","date","slot_start","slot_end","expires_at","method","status"])
    ws_s = get_ws(STATE_SHEET)
    ensure_headers(ws_s, ["key","value"])

    # 当日の行がなければ 11:00〜15:30 を生成
    list_slots_df.clear()
    df = list_slots_df(date_str)
    if not df.empty:
        return

    start_dt = datetime.combine(datetime.now(JST).date(), ISSUE_START, tzinfo=JST)
    end_last = datetime.combine(datetime.now(JST).date(), ISSUE_END, tzinfo=JST)
    rows = []
    cur = start_dt
    while cur <= end_last:
        slot_start = cur.strftime("%H:%M")
        slot_end = (cur + timedelta(minutes=SLOT_MINUTES)).strftime("%H:%M")
        rows.append([date_str, slot_start, slot_end, CAP_PER_SLOT, 0, True, ""])
        cur += timedelta(minutes=SLOT_MINUTES)

    ws_slots.append_rows(rows, value_input_option="USER_ENTERED")
    list_slots_df.clear()

def to_dt(date_str: str, hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    d = datetime.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=JST)

def issue_ticket(date_str: str, slot_start: str, slot_end: str, method: str = "mobile"):
    # 最新 slots を再読込
    ws_slots = get_ws(SLOTS_SHEET)
    df_all = pd.DataFrame(ws_slots.get_all_records())
    if df_all.empty:
        raise RuntimeError("枠が未生成です")

    recs = df_all[(df_all["date"]==date_str) & (df_all["slot_start"]==slot_start) & (df_all["slot_end"]==slot_end)]
    if recs.empty:
        raise RuntimeError("該当の枠が存在しません")
    r = recs.iloc[0]

    if str(r.get("open", True)).lower() not in ("true","1","yes"):
        raise RuntimeError("この枠は現在停止中です")

    cap = int(r.get("cap", CAP_PER_SLOT))
    issued = int(r.get("issued", 0))
    if issued >= cap:
        raise RuntimeError("満枠のため発券できません")

    # tickets へ追加
    ws_t = get_ws(TICKETS_SHEET)
    now = datetime.now(JST)
    expires = to_dt(date_str, slot_end) + timedelta(minutes=EXPIRE_EXTRA_MIN)
    ticket_id = now.strftime("%Y%m%d-") + f"{int(now.timestamp())%100000:05d}"
    ws_t.append_row([
        ticket_id,
        now.isoformat(),
        date_str,
        slot_start,
        slot_end,
        expires.isoformat(),
        method,
        "valid",
    ], value_input_option="USER_ENTERED")

    # slots.issued を +1
    headers = ws_slots.row_values(1)
    date_col = headers.index("date")+1
    s_col = headers.index("slot_start")+1
    e_col = headers.index("slot_end")+1
    issued_col = headers.index("issued")+1
    all_vals = ws_slots.get_all_values()

    target_row_idx = None
    for i in range(1, len(all_vals)):
        row = all_vals[i]
        if len(row) < max(date_col, s_col, e_col):
            continue
        if row[date_col-1]==date_str and row[s_col-1]==slot_start and row[e_col-1]==slot_end:
            target_row_idx = i+1
            break
    if target_row_idx is None:
        raise RuntimeError("更新対象行が見つかりません")

    ws_slots.update_cell(target_row_idx, issued_col, issued+1)

    # キャッシュ無効化
    list_slots_df.clear()
    list_tickets_df.clear()

    return {
        "ticket_id": ticket_id,
        "slot": f"{slot_start}–{slot_end}",
        "expires_at": expires,
    }

# ===== 画面：発券 =====
def page_issue():
    st.title("発券ページ")

    paused = state_get("paused", "false").lower() == "true"
    banner = state_get("banner", "")
    if banner:
        st.info(banner)
    if paused:
        st.error("現在、発券は一時停止中です。通常列をご利用ください。")
        return

    d = today_str()
    ensure_today_slots(d)

    list_slots_df.clear()
    df = list_slots_df(d)
    if df.empty:
        st.warning("本日の発券枠が設定されていません。")
        return

    def hm_to_time(hm: str) -> time:
        h,m = map(int, hm.split(":"))
        return time(h, m)

    df["remain"] = df.apply(lambda r: int(r["cap"]) - int(r["issued"]), axis=1)
    df["disabled"] = df.apply(
        lambda r: (hm_to_time(r["slot_start"]) < ISSUE_START)
                  or (hm_to_time(r["slot_start"]) > ISSUE_END)
                  or (str(r["open"]).lower() not in ("true","1","yes"))
                  or (int(r["issued"]) >= int(r["cap"])),
        axis=1
    )

    st.caption("受付 11:00–15:30（各30分枠20名） / 有効期限=枠終了+30分 / 期限切れは通常列へ")

    for _, row in df.iterrows():
        c1, c2, c3 = st.columns([2,1,2])
        with c1:
            st.write(f"**{row['slot_start']}–{row['slot_end']}**")
        with c2:
            st.write(f"残り: {row['remain']}/{int(row['cap'])}")
        with c3:
            if st.button("発券する", key=f"btn_{row['slot_start']}", disabled=bool(row["disabled"]) or paused):
                try:
                    res = issue_ticket(d, row["slot_start"], row["slot_end"], method="mobile")
                    st.success("発券しました！")
                    st.markdown(f"**あなたの枠:** {res['slot']}")
                    st.markdown(f"**有効期限:** {res['expires_at'].astimezone(JST).strftime('%H:%M')} まで")
                    st.link_button("案内ページを開く", f"?{urlencode({'view':'display'})}")
                    st.stop()
                except Exception as e:
                    st.error(str(e))

# ===== 画面：案内 =====
def page_display():
    st.title("案内ページ（現在案内中の枠）")
    st.caption(f"営業 {OPEN_HOUR}:00–{CLOSE_HOUR}:00 / 発券 11:00–15:30")

    banner = state_get("banner", "")
    if banner:
        st.info(banner)

    current = state_get("current_slot", "")
    if not current:
        st.warning("現在案内中の枠は未設定です。運営にお問い合わせください。")
        return

    st.markdown(
        "<div style='font-size:120px;font-weight:800;text-align:center;margin:24px 0;'>"
        f"ただいま<br>{current} 枠"
        "</div>",
        unsafe_allow_html=True,
    )
    st.caption("※ 表示を更新すると最新の案内枠が反映されます（自動更新にしたい場合は後で追加可）。")

# ===== 画面：管理 =====
def page_manage():
    st.title("運営管理ページ")

    if "auth_ok" not in st.session_state:
        st.session_state.auth_ok = False

    if not st.session_state.auth_ok:
        pin = st.text_input("運営PIN", type="password")
        if st.button("ログイン"):
            if pin == st.secrets["ADMIN_PIN"]:
                st.session_state.auth_ok = True
            else:
                st.error("PINが違います")
        st.stop()

    d = today_str()
    ensure_today_slots(d)

    list_slots_df.clear()
    df = list_slots_df(d)
    if df.empty:
        st.error("本日の枠がありません")
        return

    with st.expander("本日の枠（残数 / 発券停止）", expanded=True):
        dv = df.copy()
        dv["remain"] = dv["cap"].astype(int) - dv["issued"].astype(int)
        dv = dv[["slot_start","slot_end","cap","issued","remain","open","note"]]
        st.dataframe(dv, use_container_width=True)

    st.subheader("現在案内中の枠")
    options = [f"{r['slot_start']}–{r['slot_end']}" for _, r in df.iterrows()]
    current = state_get("current_slot", options[0] if options else "")
    idx = options.index(current) if current in options else 0
    sel = st.selectbox("切替", options, index=idx if options else None)
    if st.button("案内枠を更新"):
        state_set("current_slot", sel)
        st.success(f"案内枠を {sel} に更新しました")

    st.subheader("枠の開閉（発券の一時停止/再開）")
    target = st.selectbox("対象枠", options, key="openclose")
    c1, c2 = st.columns(2)
    if c1.button("この枠を停止(発券不可)"):
        ws = get_ws(SLOTS_SHEET)
        headers = ws.row_values(1)
        all_vals = ws.get_all_values()
        for i in range(1, len(all_vals)):
            row = all_vals[i]
            if row[headers.index("date")] == d and (row[headers.index("slot_start")] + "–" + row[headers.index("slot_end")] == target):
                ws.update_cell(i+1, headers.index("open")+1, False)
                list_slots_df.clear()
                st.success(f"{target} を停止しました")
                break
    if c2.button("この枠を再開"):
        ws = get_ws(SLOTS_SHEET)
        headers = ws.row_values(1)
        all_vals = ws.get_all_values()
        for i in range(1, len(all_vals)):
            row = all_vals[i]
            if row[headers.index("date")] == d and (row[headers.index("slot_start")] + "–" + row[headers.index("slot_end")] == target):
                ws.update_cell(i+1, headers.index("open")+1, True)
                list_slots_df.clear()
                st.success(f"{target} を再開しました")
                break

    st.subheader("全体一時停止・お知らせ")
    paused = state_get("paused", "false").lower() == "true"
    cc1, cc2 = st.columns([1,3])
    with cc1:
        if st.button("発券を全体停止" if not paused else "発券停止を解除"):
            state_set("paused", "false" if paused else "true")
            st.success("状態を切り替えました")
    with cc2:
        banner = state_get("banner", "")
        txt = st.text_input("お知らせバナー（空で非表示）", value=banner)
        if st.button("バナー更新"):
            state_set("banner", txt)
            st.success("バナーを更新しました")

    st.caption("※ 通常列は紙の会計証で運用。")

# ===== ルーティング =====
params = st.query_params  # 新API
raw_view = params.get("view", "issue")
view = raw_view[0] if isinstance(raw_view, list) else raw_view

try:
    if view == "issue":
        page_issue()
    elif view == "display":
        page_display()
    elif view == "manage":
        page_manage()
    else:
        st.write("ページを選んでください：")
        c1, c2, c3 = st.columns(3)
        if c1.button("発券 (Issue)"):
            st.query_params.update({"view": "issue"})
            st.rerun()
        if c2.button("案内 (Display)"):
            st.query_params.update({"view": "display"})
            st.rerun()
        if c3.button("管理 (Manage)"):
            st.query_params.update({"view": "manage"})
            st.rerun()
except Exception as e:
    st.error(f"エラー: {e}")
