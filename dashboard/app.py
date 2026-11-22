"""Mechanical Workshop Digital Twin - Streamlit dashboard (talks to FastAPI via REST only)."""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from pyvis.network import Network
from streamlit_autorefresh import st_autorefresh

from api import API_BASE, DEFAULT_MODE, STATUS_SIGNAL, ApiError, Client

st.set_page_config(page_title="Workshop Digital Twin", layout="wide")

GREEN, AMBER, RED = "#2e7d5b", "#d98f00", "#c0392b"
STATUS_COLORS = {"RUNNING": GREEN, "IDLE": AMBER, "DOWN": RED}
# warn = amber from here, limit = red above it (demo thresholds, not compliance limits).
# Humidity has no alert rule in the project, so it stays informational.
LIMITS = {
    "temperature_c": {"label": "Temperature", "unit": "°C", "warn": 27, "limit": 30},
    "humidity_pct": {"label": "Humidity", "unit": "%", "warn": None, "limit": None},
    "co2_ppm": {"label": "CO₂", "unit": "ppm", "warn": 1000, "limit": 1200},
    "noise_db": {"label": "Noise", "unit": "dB", "warn": 80, "limit": 85},
}
NODE_COLORS = {
    "Workshop": "#33475b", "Zone": "#2a9d8f", "Machine": "#3d6fb6", "Sensor": "#7b6fb0",
    "DataSource": "#8d99a6", "Reading": "#b7c4cf", "Alert": RED, "MaintenanceEvent": "#c58a2b", "KPI": "#5c9e6b",
}
TIERS = {"Workshop": 0, "Zone": 1, "Machine": 2, "Sensor": 2, "DataSource": 2,
         "Reading": 3, "Alert": 3, "MaintenanceEvent": 3, "KPI": 3}

st.markdown("""<style>
.card{padding:14px 16px;border-radius:6px;border-left:6px solid var(--c);background:rgba(128,128,128,.10);margin-bottom:8px}
.card .k{font-size:.85rem;opacity:.75}.card .v{font-size:2rem;font-weight:700;line-height:1.25}
.card .s{font-size:.82rem;opacity:.7}
.badge{display:inline-block;padding:2px 12px;border-radius:999px;color:#fff;font-weight:600;font-size:1.1rem}
.chip{display:inline-block;margin:0 10px 4px 0;font-size:.85rem}
.chip i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
</style>""", unsafe_allow_html=True)

# ------------------------------------------------------------------ sidebar
st.sidebar.header("Settings")
mode = st.sidebar.radio("Data source", ["mock", "live"], index=0 if DEFAULT_MODE == "mock" else 1,
                        help="Mock uses made-up data. Live calls the FastAPI server.")
base = API_BASE
scenario = "normal"
if mode == "live":
    base = st.sidebar.text_input("API address", API_BASE)
else:
    scenario = st.sidebar.radio("Mock scenario", ["normal", "alert", "lathe_down"],
                                format_func={"normal": "Normal", "alert": "Alerts firing", "lathe_down": "Lathe DOWN"}.get)
refresh = st.sidebar.slider("Refresh every (seconds)", 2, 10, 10)
st_autorefresh(interval=refresh * 1000, key="tick")
client = Client(mode, base, scenario)


def safe(fn, *args):
    try:
        return fn(*args)
    except ApiError as e:
        st.warning(f"Could not load data. {e}")
        return None


def level_color(value, lim):
    if lim["limit"] is None:
        return GREEN
    return RED if value > lim["limit"] else AMBER if value >= lim["warn"] else GREEN


def card(label, value, sub="", color=GREEN):
    st.markdown(f'<div class="card" style="--c:{color}"><div class="k">{label}</div>'
                f'<div class="v">{value}</div><div class="s">{sub}</div></div>', unsafe_allow_html=True)


def badge(text):
    return f'<span class="badge" style="background:{STATUS_COLORS.get(text, "#888")}">{text}</span>'


def gauge(title, value):
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=value, number={"suffix": " %"}, title={"text": title},
        gauge={"axis": {"range": [0, 100]}, "bar": {"color": "#33475b"},
               "steps": [{"range": [0, 50], "color": "#f0c4bf"}, {"range": [50, 80], "color": "#f5e2b3"},
                         {"range": [80, 100], "color": "#c5e3d4"}]}))
    fig.update_layout(height=210, margin=dict(l=20, r=20, t=50, b=10))
    return fig


def status_segments(machines):
    rows = []
    for m in machines:
        data = safe(client.history, STATUS_SIGNAL[m["machineId"]], 60)
        if not data or len(data["points"]) < 2:
            continue
        df = pd.DataFrame(data["points"])
        df["t"] = pd.to_datetime(df["timestamp"], utc=True)
        df["grp"] = (df["value"] != df["value"].shift()).cumsum()
        seg = df.groupby("grp").agg(Start=("t", "first"), Status=("value", "first"))
        step = df["t"].iloc[-1] - df["t"].iloc[-2]
        seg["End"] = seg["Start"].shift(-1).fillna(df["t"].iloc[-1] + step)
        seg["Machine"] = m["name"]
        rows.append(seg)
    return pd.concat(rows) if rows else None


def graph_html(g):
    net = Network(height="560px", width="100%", directed=True, cdn_resources="in_line",
                  bgcolor="#f7f7f5", font_color="#222")
    tiers = {}
    for n in g["nodes"]:
        tiers.setdefault(TIERS.get(n["label"], 3), []).append(n)
    for tier, items in tiers.items():
        for i, n in enumerate(items):
            hot = n.get("alertActive")
            tip = n["label"] + "\n" + "\n".join(f"{k}: {v}" for k, v in (n.get("properties") or {}).items())
            net.add_node(n["id"], label=n["id"], title=tip, shape="dot", size=22 if tier < 3 else 16,
                         color=RED if hot else NODE_COLORS.get(n["label"], "#999"),
                         x=(i - (len(items) - 1) / 2) * 130, y=tier * 150, physics=False,
                         borderWidth=4 if hot else 1)
    for e in g["edges"]:
        net.add_edge(e["source"], e["target"], label=e["type"], arrows="to", color="#9aa5ae")
    net.set_options('{"edges":{"font":{"size":10,"align":"middle"},"smooth":false},'
                    '"interaction":{"hover":true},"physics":{"enabled":false}}')
    return net.generate_html()


# ------------------------------------------------------------------ header
st.title("Mechanical Workshop Digital Twin")
if mode == "mock":
    st.info("Showing mock data. Switch Data source to live in the sidebar once the API is running.")

tab1, tab2, tab3, tab4 = st.tabs(["Overview", "Machine Analytics", "Knowledge Graph", "Alerts & History"])

# ------------------------------------------------------------------ overview
with tab1:
    state = safe(client.state)
    if state:
        z = state["zone"]
        ts = pd.to_datetime(state["timestamp"], utc=True)
        age = (pd.Timestamp.now(tz="UTC") - ts).total_seconds()
        st.markdown(f"**Zone:** {z['zoneId']} &nbsp; **Risk:** {z['riskLevel']} &nbsp; "
                    f"**Data source:** {state['source']} &nbsp; **Last update:** {ts:%H:%M:%S} UTC ({age:.0f} s ago)")
        cols = st.columns(4)
        for col, (sig, lim) in zip(cols, LIMITS.items()):
            with col:
                v = z[sig]
                sub = f"alert above {lim['limit']} {lim['unit']}" if lim["limit"] else "no alert rule"
                card(lim["label"], f"{v:g} {lim['unit']}", sub, level_color(v, lim))
        cols = st.columns(3)
        for col, m in zip(cols, state["machines"]):
            with col:
                st.markdown(f'<div class="card" style="--c:{STATUS_COLORS.get(m["status"], "#888")}">'
                            f'<div class="k">{m["name"]}</div><div class="v">{badge(m["status"])}</div>'
                            f'<div class="s">Utilization {m["utilization"]:.1f}% · Availability {m["availability"]:.1f}%</div></div>',
                            unsafe_allow_html=True)
        with cols[2]:
            n = state["activeAlerts"]
            card("Active alerts", n, "all clear" if n == 0 else "see Alerts & History", RED if n else GREEN)
        st.caption("Thresholds are demo values for this project, not regulatory limits. "
                   "Amber = approaching the limit, red = alert rule triggered.")

# ------------------------------------------------------------------ machine analytics
with tab2:
    kp = safe(client.kpis)
    state = safe(client.state)
    if kp and state:
        st.caption(f"KPIs over the last {kp.get('windowMinutes', '?')} minutes")
        status = {m["machineId"]: m["status"] for m in state["machines"]}
        for m in kp["machines"]:
            st.markdown(f"#### {m['name']} &nbsp; {badge(status.get(m['machineId'], 'IDLE'))}", unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            c1.plotly_chart(gauge("Utilization", m["utilization"]), key=f"u-{m['machineId']}")
            c2.plotly_chart(gauge("Availability", m["availability"]), key=f"a-{m['machineId']}")
        seg = status_segments(state["machines"])
        st.markdown("#### Status timeline (last 60 minutes)")
        if seg is not None:
            fig = px.timeline(seg, x_start="Start", x_end="End", y="Machine", color="Status",
                              color_discrete_map=STATUS_COLORS)
            fig.update_yaxes(autorange="reversed", title=None)
            fig.update_layout(height=230, margin=dict(l=10, r=10, t=10, b=10), xaxis_title=None)
            st.plotly_chart(fig, key="timeline")
        else:
            st.info("Status history is not available from the API yet.")
        pred = client.predict()
        st.markdown("#### Prediction")
        if pred:
            col = RED if pred["predictedStatus"] != "OK" else GREEN
            card("Lathe DOWN risk", f"{pred['downRiskPct']}%", f"Predicted status: {pred['predictedStatus']}", col)
        else:
            st.caption("No prediction available (the /api/predict endpoint is optional).")

# ------------------------------------------------------------------ knowledge graph
with tab3:
    g = safe(client.graph)
    if g:
        chips = "".join(f'<span class="chip"><i style="background:{c}"></i>{n}</span>' for n, c in NODE_COLORS.items())
        st.markdown(chips + '<span class="chip"><i style="background:#fff;border:3px solid #c0392b"></i>'
                    'red node = active alert</span>', unsafe_allow_html=True)
        st.caption(f"{len(g['nodes'])} nodes · {len(g['edges'])} relationships. Hover a node to see its properties.")
        try:
            html = graph_html(g)
            if hasattr(st, "iframe"):  # newer Streamlit replaces components.html
                st.iframe(html, height=580)
            else:
                components.html(html, height=580)
        except Exception as e:  # keep the tab usable if pyvis fails
            st.error(f"Graph could not be drawn: {e}")
            st.dataframe(pd.DataFrame(g["edges"]), hide_index=True)

# ------------------------------------------------------------------ alerts & history
with tab4:
    minutes = st.select_slider("History window (minutes)", options=[10, 30, 60], value=30)
    cols = st.columns(2)
    for i, (sig, lim) in enumerate(LIMITS.items()):
        data = safe(client.history, sig, minutes)
        if not data:
            continue
        df = pd.DataFrame(data["points"])
        df["time"] = pd.to_datetime(df["timestamp"], utc=True)
        fig = px.line(df, x="time", y="value", title=f"{lim['label']} ({lim['unit']})")
        fig.update_traces(line_color="#33475b")
        if lim["limit"]:
            fig.add_hline(y=lim["limit"], line_color=RED, line_dash="dash", annotation_text=f"limit {lim['limit']}")
        fig.update_layout(height=280, margin=dict(l=10, r=10, t=45, b=10), xaxis_title=None, yaxis_title=None)
        cols[i % 2].plotly_chart(fig, key=f"h-{sig}")
    st.markdown("#### Alert log")
    al = safe(client.alerts)
    if al:
        df = pd.DataFrame(al).sort_values("createdAt", ascending=False)
        df["status"] = df["active"].map({True: "ACTIVE", False: "cleared"})
        st.dataframe(df[["createdAt", "severity", "description", "asset", "status"]], hide_index=True)
    elif al == []:
        st.success("No alerts recorded.")
