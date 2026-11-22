"""Mechanical Workshop Digital Twin - Streamlit dashboard (talks to FastAPI via REST only)."""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from plotly.subplots import make_subplots
from pyvis.network import Network

from api import API_BASE, STATUS_SIGNAL, ApiError, Client

st.set_page_config(page_title="Workshop Digital Twin", layout="wide")

GREEN, AMBER, RED, BLUE, GREY = "#2e7d5b", "#d98f00", "#c0392b", "#3d6fb6", "#8d99a6"
STATUS_COLORS = {"RUNNING": GREEN, "IDLE": AMBER, "DOWN": RED}
# can the machine be used right now? (computed by the backend from status + maintenance)
AVAIL_COLORS = {"Available": GREEN, "Occupied": BLUE, "Out of service": RED, "No data": GREY}
MACHINE_NAMES = {"machine1": "Lathe", "machine2": "Drill"}
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
.card{padding:14px 16px;border-radius:6px;border:1px solid var(--bd,transparent);border-left:6px solid var(--c);background:var(--bg,rgba(128,128,128,.10));margin-bottom:8px}
.card .k{font-size:.85rem;opacity:.75}.card .v{font-size:2rem;font-weight:700;line-height:1.25}
.card .s{font-size:.82rem;opacity:.7}
.badge{display:inline-block;padding:2px 12px;border-radius:999px;color:#fff;font-weight:600;font-size:1.1rem}
.chip{display:inline-block;margin:0 10px 4px 0;font-size:.85rem}
.chip i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
</style>""", unsafe_allow_html=True)

# ------------------------------------------------------------------ refresh
# Always live: the dashboard reads the FastAPI server at API_BASE (set in docker-compose).
# The replay sends one tick (one CSV row) every 10 s. Instead of redrawing on a blind timer,
# a tiny watcher asks the API once a second whether a new tick has arrived and redraws the
# page as soon as it has, so the dashboard changes right after each tick is sent.
TICK_SECONDS = 10
WATCH_SECONDS = 1
client = Client("live", API_BASE)


def tick_key(s):
    """What identifies the data currently shown: the latest message time and whether it is live."""
    return (s.get("timestamp"), bool(s.get("live"))) if s else None


@st.fragment(run_every=WATCH_SECONDS)
def watch_for_new_tick():
    try:
        latest = tick_key(client.state())
    except ApiError:
        latest = None
    if latest != st.session_state.get("shown"):
        st.rerun(scope="app")  # new tick (or live/idle change): redraw everything


def safe(fn, *args):
    try:
        return fn(*args)
    except ApiError as e:
        st.warning(f"Could not load data. {e}")
        return None


def tint(hex_color, alpha):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def card_style(color):
    # whole card gets a faint wash of its status color (green / amber / red)
    return f"--c:{color};--bg:{tint(color, .16)};--bd:{tint(color, .35)}"


def level_color(value, lim):
    if lim["limit"] is None:
        return GREEN
    return RED if value > lim["limit"] else AMBER if value >= lim["warn"] else GREEN


def card(label, value, sub="", color=GREEN):
    st.markdown(f'<div class="card" style="{card_style(color)}"><div class="k">{label}</div>'
                f'<div class="v">{value}</div><div class="s">{sub}</div></div>', unsafe_allow_html=True)


def badge(text):
    return f'<span class="badge" style="background:{STATUS_COLORS.get(text, "#888")}">{text}</span>'


def gauge(title, value):
    value = max(0.0, min(100.0, float(value)))  # keep the dial inside 0-100
    fig = go.Figure(go.Indicator(
        mode="gauge", value=round(value, 1), domain={"x": [0, 1], "y": [0, 1]},
        title={"text": title, "font": {"size": 16}},
        gauge={"axis": {"range": [0, 100], "tickvals": [0, 50, 100]}, "bar": {"color": "#33475b"},
               "steps": [{"range": [0, 50], "color": "#f0c4bf"}, {"range": [50, 80], "color": "#f5e2b3"},
                         {"range": [80, 100], "color": "#c5e3d4"}]}))
    # the number is a separate label centred under the arc, so it can never sit behind the dial
    fig.add_annotation(x=0.5, y=0.12, xref="paper", yref="paper", text=f"<b>{value:.1f}%</b>",
                       showarrow=False, font={"size": 34}, xanchor="center", yanchor="middle")
    fig.update_layout(height=230, margin=dict(l=30, r=30, t=70, b=15))
    return fig


LEVEL = {"DOWN": 0, "IDLE": 1, "RUNNING": 2}


def status_chart(machines):
    """One step line per machine: the line drops when it goes IDLE or DOWN. DOWN periods are shaded red."""
    fig = make_subplots(rows=len(machines), cols=1, shared_xaxes=True, vertical_spacing=0.2,
                        subplot_titles=[m["name"] for m in machines])
    drawn = False
    for i, m in enumerate(machines, start=1):
        data = safe(client.history, STATUS_SIGNAL[m["machineId"]], 60)
        if not data or len(data["points"]) < 2:
            continue
        df = pd.DataFrame(data["points"])
        df["t"] = pd.to_datetime(df["timestamp"], utc=True)
        df["value"] = df["value"].astype(str).str.upper()
        df["lvl"] = df["value"].map(LEVEL)
        df = df.dropna(subset=["lvl"])
        if len(df) < 2:
            continue
        drawn = True
        step = df["t"].iloc[-1] - df["t"].iloc[-2]
        fig.add_trace(go.Scatter(x=df["t"], y=df["lvl"], mode="lines", showlegend=False,
                                 line=dict(shape="hv", width=3, color="#33475b"), customdata=df["value"],
                                 hovertemplate="%{customdata}<br>%{x|%H:%M:%S}<extra></extra>"), row=i, col=1)
        down = df["value"].eq("DOWN")
        for _, part in df[down].groupby((down != down.shift()).cumsum()[down]):
            fig.add_vrect(x0=part["t"].iloc[0], x1=part["t"].iloc[-1] + step, fillcolor=RED,
                          opacity=0.15, line_width=0, row=i, col=1)
        last = df.iloc[-1]
        fig.add_trace(go.Scatter(x=[last["t"]], y=[last["lvl"]], mode="markers", showlegend=False,
                                 marker=dict(size=13, color=STATUS_COLORS[last["value"]]),
                                 hovertemplate=f"now: {last['value']}<extra></extra>"), row=i, col=1)
    if not drawn:
        return None
    fig.update_yaxes(tickvals=[0, 1, 2], ticktext=["DOWN", "IDLE", "RUNNING"], range=[-0.3, 2.3])
    fig.update_layout(height=170 * len(machines) + 40, margin=dict(l=10, r=10, t=30, b=10))
    return fig


def avail_badge(state):
    return f'<span class="badge" style="background:{AVAIL_COLORS.get(state, GREY)}">{state}</span>'


def machine_status(props):
    # live graph (ontology) uses operationalStatus; the mock data uses status
    return props.get("operationalStatus") or props.get("status")


def node_color(n):
    props = n.get("properties") or {}
    if n["label"] == "Machine" and machine_status(props) in STATUS_COLORS:
        return STATUS_COLORS[machine_status(props)]  # machines follow their live status
    if n.get("alertActive"):
        return RED
    return NODE_COLORS.get(n["label"], "#999")


def graph_html(g):
    net = Network(height="520px", width="100%", directed=True, cdn_resources="in_line",
                  bgcolor="#f7f7f5", font_color="#222")
    tiers = {}
    for n in g["nodes"]:
        tiers.setdefault(TIERS.get(n["label"], 3), []).append(n)
    for tier, items in tiers.items():
        for i, n in enumerate(items):
            props = n.get("properties") or {}
            hot = n.get("alertActive")
            status = machine_status(props) if n["label"] == "Machine" else None
            text = n["id"] + (f"\n{status}" if status else "")
            tip = n["label"] + "\n" + "\n".join(f"{k}: {v}" for k, v in props.items())
            net.add_node(n["id"], label=text, title=tip, shape="dot", size=22 if tier < 3 else 16,
                         color=node_color(n), x=(i - (len(items) - 1) / 2) * 130, y=tier * 150,
                         physics=False, borderWidth=4 if hot else 1)
    for e in g["edges"]:
        net.add_edge(e["source"], e["target"], label=e["type"], arrows="to", color="#9aa5ae")
    # fixed layout: no dragging, panning or zooming, so the graph stays put and just changes color
    net.set_options('{"edges":{"font":{"size":10,"align":"middle"},"smooth":false},'
                    '"interaction":{"hover":true,"dragNodes":false,"dragView":false,"zoomView":false},'
                    '"physics":{"enabled":false}}')
    html = net.generate_html()
    fit = 'network.once("afterDrawing", function(){ network.fit({animation:false}); });\n'
    assert "return network;" in html
    return html.replace("return network;", fit + "return network;", 1)


# ------------------------------------------------------------------ header
st.title("Mechanical Workshop Digital Twin")


try:
    state = client.state()
except ApiError:
    state = None  # backend has no telemetry yet (or is unreachable)
st.session_state["shown"] = tick_key(state)
watch_for_new_tick()
LIVE = bool(state and state.get("live"))
if not LIVE:
    # no live data: show zeros and no alerts until the replay script sends data again
    st.warning("Waiting for live data. Start the replay script and the dashboard will pick it up "
               "as soon as the first reading arrives.")
    state = {"zone": {"zoneId": "zone1", **{k: 0 for k in LIMITS}, "riskLevel": "-"}, "source": "-",
             "timestamp": None, "activeAlerts": 0,
             "machines": [{"machineId": mid, "name": name, "status": "-", "availabilityState": "No data",
                           "utilization": 0.0, "availability": 0.0} for mid, name in MACHINE_NAMES.items()]}

tab1, tab2, tab3, tab4 = st.tabs(["Overview", "Machine Analytics", "Knowledge Graph", "Alerts & History"])

# ------------------------------------------------------------------ overview
with tab1:
    z = state["zone"]
    if LIVE:
        ts = pd.to_datetime(state["timestamp"], utc=True)
        updated = f"{ts:%H:%M:%S} UTC ({state.get('lastMessageAgeSec') or 0:.0f} s ago)"
    else:
        updated = "no live data"
    st.markdown(f"**Zone:** {z['zoneId']} &nbsp; **Risk:** {z['riskLevel']} &nbsp; "
                f"**Data source:** {state['source']} &nbsp; **Last update:** {updated}")
    cols = st.columns(4)
    for col, (sig, lim) in zip(cols, LIMITS.items()):
        with col:
            v = z[sig]
            sub = f"alert above {lim['limit']} {lim['unit']}" if lim["limit"] else "no alert rule"
            card(lim["label"], f"{v:g} {lim['unit']}", sub, level_color(v, lim) if LIVE else GREY)
    cols = st.columns(3)
    for col, m in zip(cols, state["machines"]):
        with col:
            av = m["availabilityState"]
            st.markdown(f'<div class="card" style="{card_style(AVAIL_COLORS.get(av, GREY))}">'
                        f'<div class="k">{m["name"]}{" · " + m["status"].lower() if LIVE else ""}</div>'
                        f'<div class="v">{avail_badge(av)}</div>'
                        f'<div class="s">Utilization {m["utilization"]:.1f}% · Uptime {m["availability"]:.1f}%</div></div>',
                        unsafe_allow_html=True)
    with cols[2]:
        n = state["activeAlerts"]
        card("Active alerts", n, "all clear" if n == 0 else "see Alerts & History",
             RED if n else (GREEN if LIVE else GREY))
    st.caption("The dashboard shows the current state of the workshop. Green = normal, "
               "Amber = approaching the limit, red = alert rule triggered. "
               "Available = idle and free to use, Occupied = running, Out of service = down or in maintenance.")

# ------------------------------------------------------------------ machine analytics
with tab2:
    st.caption("Utilization = running time / scheduled time. Uptime (availability KPI) = time not DOWN / "
               "scheduled time. Both over the last 60 minutes of the current run.")
    for m in state["machines"]:
        av = m["availabilityState"]
        st.markdown(f"#### {m['name']} &nbsp; {avail_badge(av)}", unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        c1.plotly_chart(gauge("Utilization", m["utilization"]), key=f"u-{m['machineId']}")
        with c2:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            card("Availability now", av, f"machine is {m['status'].lower()}" if LIVE else "waiting for data",
                 AVAIL_COLORS.get(av, GREY))
        c3.plotly_chart(gauge("Uptime", m["availability"]), key=f"a-{m['machineId']}")
    st.markdown("#### Status over time (last 60 minutes)")
    fig = status_chart(state["machines"]) if LIVE else None
    if fig is not None:
        st.plotly_chart(fig, key="timeline")
        st.caption("The line steps down when a machine goes IDLE or DOWN. Red shading marks DOWN periods.")
    else:
        st.info("No status history for the current run yet.")

# ------------------------------------------------------------------ knowledge graph
with tab3:
    g = safe(client.graph)
    if g:
        chips = "".join(f'<span class="chip"><i style="background:{c}"></i>{n}</span>' for n, c in NODE_COLORS.items())
        st.markdown(chips + '<span class="chip"><i style="background:#fff;border:3px solid #c0392b"></i>'
                    'red node = active alert</span>', unsafe_allow_html=True)
        st.caption(f"{len(g['nodes'])} nodes · {len(g['edges'])} relationships. Machines take their status color "
                   "(green running, amber idle, red down) and alerts appear in red. Hover a node for its properties.")
        try:
            html = graph_html(g)
            if hasattr(st, "iframe"):  # newer Streamlit replaces components.html
                st.iframe(html, height=545)
            else:
                components.html(html, height=545)
        except Exception as e:  # keep the tab usable if pyvis fails
            st.error(f"Graph could not be drawn: {e}")
            st.dataframe(pd.DataFrame(g["edges"]), hide_index=True)

# ------------------------------------------------------------------ alerts & history
with tab4:
    minutes = st.select_slider("History window (minutes)", options=[10, 30, 60], value=30)
    cols = st.columns(2)
    for i, (sig, lim) in enumerate(LIMITS.items()):
        data = safe(client.history, sig, minutes) if LIVE else {"points": []}
        if not data:
            continue
        df = pd.DataFrame(data["points"], columns=["timestamp", "value"])
        df["time"] = pd.to_datetime(df["timestamp"], utc=True)
        fig = px.line(df, x="time", y="value", title=f"{lim['label']} ({lim['unit']})")
        fig.update_traces(line_color="#33475b")
        if lim["limit"]:
            fig.add_hline(y=lim["limit"], line_color=RED, line_dash="dash", annotation_text=f"limit {lim['limit']}")
        fig.update_layout(height=280, margin=dict(l=10, r=10, t=45, b=10), xaxis_title=None, yaxis_title=None)
        cols[i % 2].plotly_chart(fig, key=f"h-{sig}")
    st.markdown("#### Alert log")
    al = safe(client.alerts) if LIVE else []
    if al:
        df = pd.DataFrame(al).sort_values("createdAt", ascending=False)
        df["status"] = df["active"].map({True: "ACTIVE", False: "cleared"})
        st.dataframe(df[["createdAt", "severity", "description", "asset", "status"]], hide_index=True)
    elif al == []:
        st.success("No alerts recorded.")
