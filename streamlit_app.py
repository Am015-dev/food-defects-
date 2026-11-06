"""Παρατηρητήριο Τιμών -- Streamlit version.

Reads data/prices/*.csv.gz (one file per day, kept in the GitHub repo).
The "Update now" button fetches every shop with the user's own e-food
login header and saves today's file, locally and (if a token is set) to
GitHub.

Secrets (Streamlit Cloud: App settings -> Secrets; locally: env vars):
  EFOOD_AUTH_HEADER  one header from the logged-in browser, "Name: value"
  GITHUB_TOKEN       fine-grained token, Contents: read/write on this repo
  GITHUB_REPO        e.g. "Am015-dev/food-defects-"
"""

import os

import pandas as pd
import streamlit as st

from shops import SHOP_URLS
from snapshot_files import DATA_DIR, fetch_today, push_to_github, save_local, to_csv_gz

st.set_page_config(page_title="Παρατηρητήριο Τιμών", page_icon="🛒", layout="wide")


def secret(name):
    try:
        return st.secrets.get(name) or os.environ.get(name, "")
    except Exception:  # noqa: BLE001 -- no secrets.toml at all is normal locally
        return os.environ.get(name, "")


@st.cache_data
def load_all(_stamp):
    # ponytail: reads every daily file each load; fine for months of data,
    # switch to reading only the last N files if the app gets slow.
    files = sorted(DATA_DIR.glob("*.csv.gz"))
    if not files:
        return pd.DataFrame()
    return pd.concat((pd.read_csv(f) for f in files), ignore_index=True)


def data_stamp():
    return tuple((f.name, f.stat().st_mtime) for f in sorted(DATA_DIR.glob("*.csv.gz")))


# ---- sidebar: update -------------------------------------------------------
with st.sidebar:
    st.header("Ενημέρωση")
    auth = secret("EFOOD_AUTH_HEADER")
    if not auth:
        st.warning("Λείπει το EFOOD_AUTH_HEADER. Χωρίς αυτό, το e-food απαντά authentication_error.")
    if st.button("Update now", type="primary", use_container_width=True, disabled=not auth):
        bar = st.progress(0.0)
        day, rows, errors = fetch_today(auth, progress=lambda i, label: bar.progress(i / 13, label))
        bar.empty()
        for err in errors:
            st.error(err)
        if rows:
            blob = to_csv_gz(rows)
            save_local(day, blob)
            st.success(f"{len(rows):,} προϊόντα αποθηκεύτηκαν για {day}.")
            token, repo = secret("GITHUB_TOKEN"), secret("GITHUB_REPO")
            if token and repo:
                try:
                    push_to_github(day, blob, token, repo)
                    st.success("Αποθηκεύτηκε και στο GitHub.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"GitHub: {exc}")
            else:
                st.info("Χωρίς GITHUB_TOKEN/GITHUB_REPO: το αρχείο μένει μόνο τοπικά.")
            st.cache_data.clear()

df = load_all(data_stamp())

st.title("Παρατηρητήριο Τιμών")
if df.empty:
    st.info(
        "Δεν υπάρχουν ακόμα δεδομένα. Βάλε το EFOOD_AUTH_HEADER στα secrets και πάτα **Update now**."
    )
    st.stop()

latest_day = df["date"].max()
today = df[df["date"] == latest_day]
st.caption(
    f"Τελευταία ενημέρωση: {latest_day} · {today['shop'].nunique()} καταστήματα · {len(today):,} προϊόντα"
)

c1, c2, c3 = st.columns(3)
c1.metric("Τιμή €0,00", int(today["zero_price_bug"].sum()))
c2.metric("Ψεύτικη τιμή 30 ημερών", int(today["placeholder_bug"].sum()))
c3.metric("Επαληθευμένες προσφορές", int(today["deal_pct"].notna().sum()))

shop_filter = st.multiselect("Καταστήματα", sorted(today["shop"].unique()))
view = today[today["shop"].isin(shop_filter)] if shop_filter else today
view = view.assign(link=view["shop_id"].map(SHOP_URLS))

cols = ["shop", "name", "category", "price", "full_price", "l30d_price", "unit_price", "link"]
link_cfg = {"link": st.column_config.LinkColumn("e-food", display_text="άνοιγμα")}

bugs, deals, search, history = st.tabs(["Σφάλματα", "Προσφορές", "Αναζήτηση", "Ιστορικό"])

with bugs:
    st.subheader("Τιμή €0,00")
    st.dataframe(view[view["zero_price_bug"] == 1][cols], hide_index=True, column_config=link_cfg)
    st.subheader("Τιμή 30 ημερών ≤ €0,05")
    st.dataframe(view[view["placeholder_bug"] == 1][cols], hide_index=True, column_config=link_cfg)

with deals:
    d = view[view["deal_pct"].notna()].sort_values("deal_pct", ascending=False)
    st.dataframe(d[["deal_pct"] + cols], hide_index=True, column_config=link_cfg)

with search:
    q = st.text_input("Προϊόν")
    if q:
        hits = view[view["name"].str.contains(q, case=False, na=False, regex=False)]
        st.dataframe(hits.sort_values("unit_price")[cols], hide_index=True, column_config=link_cfg)

with history:
    name = st.selectbox("Προϊόν", sorted(df["name"].unique()), index=None, placeholder="Διάλεξε προϊόν")
    if name:
        h = df[df["name"] == name].pivot_table(index="date", columns="shop", values="price")
        st.line_chart(h)
        st.dataframe(h)
