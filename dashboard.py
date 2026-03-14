#!/usr/bin/env python3
"""
============================================================
 Parking Intelligent — Dashboard Flask avec Profiling RFID
============================================================
"""

import json
import sqlite3
import threading
import logging
import os
from datetime import datetime

from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt

DB_PATH     = "/home/pi/parking/data/parking.db"
MQTT_BROKER = "localhost"
MQTT_PORT   = 1883

app      = Flask(__name__)
app.config["SECRET_KEY"] = "parking-iot-2025"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── État global ───────────────────────────────────────────
etat = {
    "occupe":        False,
    "distance":      0,
    "porte_ouverte": False,
    "derniere_maj":  "",
    "mqtt_ok":       False,
    "arduino_ok":    False,
    "dernier_badge": None,   # dernier profil scanné
    "alertes":       [],
    "predictions":   [],
}

# ── MQTT ─────────────────────────────────────────────────
def on_mqtt_connect(client, userdata, flags, rc):
    etat["mqtt_ok"] = True
    client.subscribe("parking/#")

def on_mqtt_message(client, userdata, msg):
    try:
        data  = json.loads(msg.payload.decode())
        topic = msg.topic

        if topic == "parking/sensor":
            etat.update({
                "occupe":        data.get("occupe", False),
                "distance":      data.get("distance", 0),
                "porte_ouverte": data.get("porte_ouverte", False),
                "derniere_maj":  data.get("timestamp", ""),
                "arduino_ok":    True,
            })
            socketio.emit("sensor_update", etat)

        elif topic == "parking/profil":
            # Nouveau badge scanné → envoyer au dashboard
            etat["dernier_badge"] = data
            socketio.emit("nouveau_badge", data)

        elif topic == "parking/profils_all":
            # Liste complète des profils
            socketio.emit("profils_update", data)

        elif topic == "parking/porte":
            etat["porte_ouverte"] = data.get("etat") == "ouverte"
            socketio.emit("porte_update", {"porte_ouverte": etat["porte_ouverte"]})

        elif topic == "parking/alerte":
            etat["alertes"].insert(0, data)
            etat["alertes"] = etat["alertes"][:50]
            socketio.emit("nouvelle_alerte", data)

        elif topic == "parking/ml/result":
            etat["predictions"] = data.get("predictions", [])
            socketio.emit("ml_update", data)

    except Exception as e:
        log.error("MQTT message error: %s", e)

mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_message = on_mqtt_message
try:
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    threading.Thread(target=mqtt_client.loop_forever, daemon=True).start()
except Exception as e:
    log.error("MQTT non disponible : %s", e)

# ── API REST ──────────────────────────────────────────────
@app.route("/api/etat")
def api_etat():
    return jsonify(etat)

@app.route("/api/profils")
def api_profils():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM profils ORDER BY nb_visites DESC"
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/profil/<uid>")
def api_profil_detail(uid):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        profil = conn.execute(
            "SELECT * FROM profils WHERE uid = ?", (uid,)
        ).fetchone()

        historique = conn.execute("""
            SELECT timestamp, heure, jour_semaine
            FROM rfid_events WHERE uid = ?
            ORDER BY timestamp DESC LIMIT 50
        """, (uid,)).fetchall()

        conn.close()
        if not profil:
            return jsonify({"error": "UID non trouvé"}), 404

        return jsonify({
            "profil":     dict(profil),
            "historique": [dict(r) for r in historique]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/rfid")
def api_rfid():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT e.timestamp, e.uid, e.card_type, e.heure,
                   e.jour_semaine, p.label, p.nb_visites
            FROM rfid_events e
            LEFT JOIN profils p ON e.uid = p.uid
            ORDER BY e.timestamp DESC LIMIT 50
        """).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats")
def api_stats():
    try:
        conn = sqlite3.connect(DB_PATH)

        stats = {
            "total_mesures":   conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0],
            "taux_occupation": conn.execute("SELECT ROUND(AVG(occupe)*100,1) FROM sensor_data").fetchone()[0] or 0,
            "total_badges":    conn.execute("SELECT COUNT(*) FROM profils").fetchone()[0],
            "badges_reguliers":conn.execute("SELECT COUNT(*) FROM profils WHERE label='regulier'").fetchone()[0],
            "badges_nouveaux": conn.execute("SELECT COUNT(*) FROM profils WHERE label='nouveau'").fetchone()[0],
            "total_passages":  conn.execute("SELECT COUNT(*) FROM rfid_events").fetchone()[0],
            "occupation_par_heure": [
                {"heure": r[0], "taux": round(r[1]*100, 1)}
                for r in conn.execute(
                    "SELECT heure, AVG(occupe) FROM sensor_data GROUP BY heure ORDER BY heure"
                ).fetchall()
            ],
            "passages_par_heure": [
                {"heure": r[0], "nb": r[1]}
                for r in conn.execute(
                    "SELECT heure, COUNT(*) FROM rfid_events GROUP BY heure ORDER BY heure"
                ).fetchall()
            ],
        }
        conn.close()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/commande", methods=["POST"])
def api_commande():
    data = request.get_json()
    cmd  = data.get("commande", "")
    if cmd in ["OPEN", "CLOSE"]:
        mqtt_client.publish("parking/commande", json.dumps({"commande": cmd}))
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400

# ═══════════════════════════════════════════════════════════
# DASHBOARD HTML
# ═══════════════════════════════════════════════════════════
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Parking Intelligent</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@300;500;700;800&display=swap');

:root {
  --bg:     #080c14;
  --card:   #0d1520;
  --border: #162236;
  --green:  #00e676;
  --red:    #ff1744;
  --blue:   #00b0ff;
  --gold:   #ffd740;
  --purple: #e040fb;
  --orange: #ff6d00;
  --text:   #b0bec5;
  --dim:    #37474f;
  --mono:   'Share Tech Mono', monospace;
  --body:   'Exo 2', sans-serif;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--body);
  padding: 18px;
  min-height: 100vh;
}

/* Scanlines */
body::after {
  content: "";
  position: fixed; inset: 0; pointer-events: none; z-index: 9999;
  background: repeating-linear-gradient(
    0deg, transparent, transparent 3px,
    rgba(0,176,255,.018) 3px, rgba(0,176,255,.018) 4px
  );
}

/* ─── Header ────────────────────────────────────────── */
header {
  display: flex; justify-content: space-between; align-items: center;
  padding-bottom: 14px; margin-bottom: 20px;
  border-bottom: 1px solid var(--border);
}

.logo {
  font-family: var(--mono); font-size: 1.3rem;
  color: var(--blue); letter-spacing: 3px;
  text-shadow: 0 0 14px var(--blue);
}

.topbar { display: flex; gap: 20px; align-items: center; }

.pill {
  display: flex; align-items: center; gap: 6px;
  font-size: .72rem; letter-spacing: 1.5px;
  color: var(--dim); text-transform: uppercase;
}

.dot {
  width: 7px; height: 7px; border-radius: 50%;
}
.dot.on  { background: var(--green); box-shadow: 0 0 6px var(--green);
            animation: blink 2s infinite; }
.dot.off { background: var(--red); }

@keyframes blink { 50% { opacity: .3; } }

#clock { font-family: var(--mono); font-size: .85rem; color: var(--blue); }

/* ─── Tabs ──────────────────────────────────────────── */
.tabs {
  display: flex; gap: 4px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0;
}

.tab {
  padding: 8px 20px; font-size: .82rem;
  letter-spacing: 2px; text-transform: uppercase;
  background: none; border: none; cursor: pointer;
  color: var(--dim); font-family: var(--body);
  border-bottom: 2px solid transparent;
  transition: all .2s;
}
.tab:hover   { color: var(--text); }
.tab.active  { color: var(--blue); border-bottom-color: var(--blue); }

.tab-content { display: none; }
.tab-content.active { display: block; }

/* ─── Grid layouts ──────────────────────────────────── */
.grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.span2 { grid-column: span 2; }
.span3 { grid-column: span 3; }
.mt    { margin-top: 14px; }

/* ─── Card ──────────────────────────────────────────── */
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 18px;
  position: relative;
  overflow: hidden;
}

.card-accent {
  position: absolute; top: 0; left: 0;
  width: 3px; height: 100%;
  background: var(--blue);
}
.card-accent.green  { background: var(--green); }
.card-accent.red    { background: var(--red); }
.card-accent.gold   { background: var(--gold); }
.card-accent.purple { background: var(--purple); }

.card-title {
  font-size: .67rem; letter-spacing: 3px;
  color: var(--dim); text-transform: uppercase;
  margin-bottom: 14px;
}

/* ─── Place status ──────────────────────────────────── */
.place-big {
  text-align: center; padding: 14px 0;
}
.place-icon { font-size: 3.5rem; display: block; margin-bottom: 8px; }
.place-name {
  font-size: 1.5rem; font-weight: 800; letter-spacing: 4px;
  text-transform: uppercase;
}
.libre  { color: var(--green); text-shadow: 0 0 16px var(--green); }
.occupe { color: var(--red);   text-shadow: 0 0 16px var(--red); }

/* ─── Metric rows ───────────────────────────────────── */
.mrow {
  display: flex; justify-content: space-between; align-items: center;
  padding: 7px 0; border-bottom: 1px solid rgba(22,34,54,.8);
  font-size: .84rem;
}
.mrow:last-child { border: none; }
.mlabel { color: var(--dim); }
.mval { font-family: var(--mono); color: var(--blue); }
.mval.g { color: var(--green); }
.mval.r { color: var(--red); }
.mval.o { color: var(--gold); }

/* ─── Buttons ───────────────────────────────────────── */
.btn {
  width: 100%; padding: 10px; margin-top: 8px;
  border-radius: 5px; border: 1px solid;
  background: none; cursor: pointer;
  font-family: var(--body); font-weight: 700;
  font-size: .82rem; letter-spacing: 2px;
  transition: background .2s;
}
.btn-g { color: var(--green); border-color: var(--green); }
.btn-g:hover { background: rgba(0,230,118,.08); }
.btn-r { color: var(--red);   border-color: var(--red); }
.btn-r:hover { background: rgba(255,23,68,.08); }

/* ─── Badge alert (dernier scan) ────────────────────── */
.badge-alert {
  padding: 14px; border-radius: 6px;
  background: rgba(0,176,255,.06);
  border: 1px solid rgba(0,176,255,.25);
  margin-bottom: 0;
}

.badge-uid {
  font-family: var(--mono); font-size: 1.1rem;
  color: var(--blue); letter-spacing: 3px;
  word-break: break-all;
}

.label-pill {
  display: inline-block;
  padding: 2px 12px; border-radius: 20px;
  font-size: .72rem; font-weight: 700; letter-spacing: 2px;
  text-transform: uppercase; margin-top: 6px;
}
.label-regulier    { background: rgba(0,230,118,.18); color: var(--green); }
.label-occasionnel { background: rgba(255,215,64,.18); color: var(--gold); }
.label-nouveau     { background: rgba(0,176,255,.18);  color: var(--blue); }
.label-inconnu     { background: rgba(55,71,79,.3);    color: var(--dim); }

/* ─── Profils table ─────────────────────────────────── */
.ptable { width: 100%; border-collapse: collapse; font-size: .82rem; }
.ptable th {
  text-align: left; padding: 8px 10px;
  color: var(--dim); font-size: .68rem; letter-spacing: 2px;
  border-bottom: 1px solid var(--border); font-weight: 400;
}
.ptable td { padding: 9px 10px; border-bottom: 1px solid rgba(22,34,54,.6); }
.ptable tr:hover td { background: rgba(0,176,255,.04); cursor: pointer; }
.ptable .uid-cell { font-family: var(--mono); font-size: .78rem; color: var(--blue); }

/* ─── Mini heatmap des heures ───────────────────────── */
.heatmap { display: flex; gap: 3px; flex-wrap: wrap; margin-top: 10px; }
.hm-cell {
  width: 28px; height: 28px; border-radius: 4px;
  display: flex; align-items: center; justify-content: center;
  font-size: .6rem; color: var(--dim);
  background: rgba(22,34,54,.8);
  transition: background .3s;
}

/* ─── Alerte items ──────────────────────────────────── */
.alerte-item {
  padding: 9px 12px; margin-bottom: 7px;
  border-radius: 5px; font-size: .81rem;
  background: rgba(255,23,68,.07);
  border-left: 3px solid var(--red);
  display: flex; gap: 12px;
}
.alerte-ts { font-family: var(--mono); font-size: .7rem; color: var(--dim); white-space: nowrap; }

/* ─── Prédictions bar ───────────────────────────────── */
.pred-row {
  display: flex; gap: 5px; align-items: flex-end;
  height: 70px; margin-top: 10px;
}
.pred-col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 3px; }
.pred-bar {
  width: 100%; border-radius: 3px 3px 0 0; min-height: 3px;
  transition: height .6s ease;
}
.pred-bar.hi { background: var(--red);    box-shadow: 0 0 5px var(--red); }
.pred-bar.md { background: var(--orange); }
.pred-bar.lo { background: var(--green); }
.pred-h { font-size: .6rem; color: var(--dim); }

/* ─── Modale profil ─────────────────────────────────── */
.modal-overlay {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(8,12,20,.85);
  display: none; align-items: center; justify-content: center;
}
.modal-overlay.open { display: flex; }
.modal {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; width: 560px; max-width: 95vw;
  max-height: 80vh; overflow-y: auto; padding: 24px;
}
.modal-close {
  float: right; background: none; border: none;
  color: var(--dim); font-size: 1.2rem; cursor: pointer;
}
.histo-row {
  padding: 5px 0; font-size: .78rem; font-family: var(--mono);
  border-bottom: 1px solid rgba(22,34,54,.5);
  display: flex; gap: 16px;
}

/* ─── Chart ─────────────────────────────────────────── */
.chart-wrap { height: 180px; }

@media (max-width: 900px) {
  .grid3 { grid-template-columns: 1fr 1fr; }
  .span3  { grid-column: span 2; }
}
@media (max-width: 600px) {
  .grid3, .grid2 { grid-template-columns: 1fr; }
  .span2, .span3 { grid-column: span 1; }
}
</style>
</head>
<body>

<!-- ── Header ────────────────────────────────────────── -->
<header>
  <div class="logo">⬡ SMART PARKING</div>
  <div class="topbar">
    <div class="pill"><span class="dot on"  id="dot-mqtt"></span>MQTT</div>
    <div class="pill"><span class="dot on"  id="dot-arduino"></span>ARDUINO</div>
    <div id="clock">--:--:--</div>
  </div>
</header>

<!-- ── Tabs ──────────────────────────────────────────── -->
<div class="tabs">
  <button class="tab active" onclick="showTab('vue')">Vue d'ensemble</button>
  <button class="tab"        onclick="showTab('profils')">Profils RFID</button>
  <button class="tab"        onclick="showTab('stats')">Statistiques</button>
</div>

<!-- ═══════════════════════════════════════════════════ -->
<!-- TAB 1 : Vue d'ensemble                             -->
<!-- ═══════════════════════════════════════════════════ -->
<div id="tab-vue" class="tab-content active">
  <div class="grid3">

    <!-- Statut place -->
    <div class="card" id="card-place">
      <div class="card-accent green" id="accent-place"></div>
      <div class="card-title">État de la place</div>
      <div class="place-big">
        <span class="place-icon" id="place-icon">🟢</span>
        <div class="place-name libre" id="place-label">LIBRE</div>
      </div>
      <div class="mrow">
        <span class="mlabel">Distance</span>
        <span class="mval" id="m-dist">— cm</span>
      </div>
      <div class="mrow">
        <span class="mlabel">Dernière MAJ</span>
        <span class="mval" id="m-maj" style="font-size:.72rem">—</span>
      </div>
    </div>

    <!-- Porte -->
    <div class="card">
      <div class="card-accent"></div>
      <div class="card-title">Porte garage</div>
      <div class="place-big">
        <span class="place-icon" id="porte-icon">🚪</span>
        <div class="place-name" id="porte-label"
             style="color:var(--text);font-size:1.1rem">FERMÉE</div>
      </div>
      <button class="btn btn-g" onclick="cmd('OPEN')">▲ OUVRIR</button>
      <button class="btn btn-r" onclick="cmd('CLOSE')">▼ FERMER</button>
    </div>

    <!-- Dernier badge scanné -->
    <div class="card">
      <div class="card-accent purple"></div>
      <div class="card-title">Dernier badge scanné</div>
      <div id="badge-panel">
        <div style="color:var(--dim);font-size:.82rem;text-align:center;padding:20px 0">
          En attente d'un badge...
        </div>
      </div>
    </div>

    <!-- Prédictions ML -->
    <div class="card span2">
      <div class="card-accent gold"></div>
      <div class="card-title">Prédictions occupation — 12h</div>
      <div class="pred-row" id="pred-row">
        <div style="color:var(--dim);font-size:.78rem">En attente du module ML...</div>
      </div>
    </div>

    <!-- Alertes -->
    <div class="card">
      <div class="card-accent red"></div>
      <div class="card-title">⚠ Alertes récentes</div>
      <div id="alerte-list" style="max-height:160px;overflow-y:auto">
        <div style="color:var(--dim);font-size:.8rem">Aucune alerte</div>
      </div>
    </div>

  </div>
</div>

<!-- ═══════════════════════════════════════════════════ -->
<!-- TAB 2 : Profils RFID                               -->
<!-- ═══════════════════════════════════════════════════ -->
<div id="tab-profils" class="tab-content">
  <div class="grid3">

    <!-- Compteurs labels -->
    <div class="card">
      <div class="card-accent green"></div>
      <div class="card-title">Résumé profils</div>
      <div class="mrow"><span class="mlabel">Total badges</span>
        <span class="mval" id="p-total">—</span></div>
      <div class="mrow"><span class="mlabel">Réguliers (≥10 visites)</span>
        <span class="mval g" id="p-reguliers">—</span></div>
      <div class="mrow"><span class="mlabel">Occasionnels (3–9)</span>
        <span class="mval o" id="p-occasionnels">—</span></div>
      <div class="mrow"><span class="mlabel">Nouveaux (1–2)</span>
        <span class="mval" id="p-nouveaux">—</span></div>
      <div class="mrow"><span class="mlabel">Total passages</span>
        <span class="mval" id="p-passages">—</span></div>
    </div>

    <!-- Heatmap heures -->
    <div class="card span2">
      <div class="card-accent"></div>
      <div class="card-title">Heures de passage (tous badges)</div>
      <div class="heatmap" id="heatmap-heures">
        <!-- généré par JS -->
      </div>
    </div>

    <!-- Tableau des profils -->
    <div class="card span3">
      <div class="card-accent purple"></div>
      <div class="card-title">Tous les badges — cliquer pour le détail</div>
      <div style="overflow-x:auto">
        <table class="ptable" id="profils-table">
          <thead>
            <tr>
              <th>UID</th>
              <th>Type carte</th>
              <th>Label</th>
              <th>Visites</th>
              <th>1ère visite</th>
              <th>Dernière visite</th>
              <th>Heure fréquente</th>
              <th>Jour fréquent</th>
            </tr>
          </thead>
          <tbody id="profils-tbody"></tbody>
        </table>
      </div>
    </div>

    <!-- Historique passages RFID -->
    <div class="card span3">
      <div class="card-accent"></div>
      <div class="card-title">Historique des passages</div>
      <div style="overflow-x:auto">
        <table class="ptable" id="rfid-table">
          <thead>
            <tr>
              <th>Horodatage</th><th>UID</th><th>Type</th>
              <th>Heure</th><th>Jour</th><th>Label</th><th>Visites</th>
            </tr>
          </thead>
          <tbody id="rfid-tbody"></tbody>
        </table>
      </div>
    </div>

  </div>
</div>

<!-- ═══════════════════════════════════════════════════ -->
<!-- TAB 3 : Statistiques                               -->
<!-- ═══════════════════════════════════════════════════ -->
<div id="tab-stats" class="tab-content">
  <div class="grid2">

    <div class="card">
      <div class="card-accent gold"></div>
      <div class="card-title">Vue globale</div>
      <div class="mrow"><span class="mlabel">Total mesures</span>
        <span class="mval" id="s-mesures">—</span></div>
      <div class="mrow"><span class="mlabel">Taux d'occupation moyen</span>
        <span class="mval" id="s-taux">—%</span></div>
      <div class="mrow"><span class="mlabel">Badges enregistrés</span>
        <span class="mval" id="s-badges">—</span></div>
      <div class="mrow"><span class="mlabel">Total passages</span>
        <span class="mval" id="s-passages">—</span></div>
    </div>

    <div class="card">
      <div class="card-accent green"></div>
      <div class="card-title">Passages par heure</div>
      <div class="chart-wrap"><canvas id="chart-passages"></canvas></div>
    </div>

    <div class="card span2">
      <div class="card-accent"></div>
      <div class="card-title">Taux d'occupation par heure (%)</div>
      <div class="chart-wrap"><canvas id="chart-occ"></canvas></div>
    </div>

  </div>
</div>

<!-- ── Modale détail profil ─────────────────────────── -->
<div class="modal-overlay" id="modal-overlay" onclick="fermerModal(event)">
  <div class="modal" id="modal-body">
    <button class="modal-close" onclick="fermerModal()">✕</button>
    <div id="modal-content"></div>
  </div>
</div>

<script>
const socket = io();
let chartOcc = null, chartPass = null;

const JOURS = ['Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi','Dimanche'];

// ── Horloge ───────────────────────────────────────────
setInterval(() => {
  document.getElementById('clock').textContent =
    new Date().toLocaleTimeString('fr-FR');
}, 1000);

// ── Tabs ──────────────────────────────────────────────
function showTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'profils') { chargerProfils(); chargerRFID(); }
  if (name === 'stats')   { chargerStats(); }
}

// ── MQTT : état capteur ───────────────────────────────
socket.on('sensor_update', d => {
  const libre = !d.occupe;
  document.getElementById('place-icon').textContent  = libre ? '🟢' : '🔴';
  document.getElementById('place-label').textContent = libre ? 'LIBRE' : 'OCCUPÉE';
  document.getElementById('place-label').className   = 'place-name ' + (libre ? 'libre' : 'occupe');
  document.getElementById('accent-place').className  = 'card-accent ' + (libre ? 'green' : 'red');
  document.getElementById('m-dist').textContent      = d.distance + ' cm';
  document.getElementById('m-maj').textContent       =
    d.derniere_maj ? new Date(d.derniere_maj).toLocaleTimeString('fr-FR') : '—';
});

// ── MQTT : porte ──────────────────────────────────────
socket.on('porte_update', d => {
  const o = d.porte_ouverte;
  document.getElementById('porte-icon').textContent  = o ? '🔓' : '🚪';
  document.getElementById('porte-label').textContent = o ? 'OUVERTE' : 'FERMÉE';
  document.getElementById('porte-label').style.color = o ? 'var(--green)' : 'var(--text)';
});

// ── MQTT : nouveau badge scanné ───────────────────────
socket.on('nouveau_badge', d => {
  const p = d.profil || {};
  afficherDernierBadge(d.uid, p);
  // Rafraîchir le tableau profils si l'onglet est actif
  if (document.getElementById('tab-profils').classList.contains('active')) {
    chargerProfils();
    chargerRFID();
  }
});

// ── MQTT : liste profils mise à jour ──────────────────
socket.on('profils_update', profils => {
  if (document.getElementById('tab-profils').classList.contains('active')) {
    rendreTableauProfils(profils);
  }
});

// ── MQTT : prédictions ML ─────────────────────────────
socket.on('ml_update', d => {
  const preds = d.predictions || [];
  const row   = document.getElementById('pred-row');
  if (!preds.length) return;
  row.innerHTML = '';
  preds.forEach(p => {
    const pct = Math.round(p.prob_occupe * 100);
    const cls = pct > 70 ? 'hi' : pct > 40 ? 'md' : 'lo';
    row.innerHTML += `
      <div class="pred-col">
        <div class="pred-bar ${cls}" style="height:${pct}%"></div>
        <div class="pred-h">${p.heure}h</div>
      </div>`;
  });
});

// ── MQTT : alertes ────────────────────────────────────
socket.on('nouvelle_alerte', a => {
  const list = document.getElementById('alerte-list');
  const ts   = new Date(a.timestamp).toLocaleTimeString('fr-FR');
  list.insertAdjacentHTML('afterbegin', `
    <div class="alerte-item">
      <span class="alerte-ts">${ts}</span>
      <span>${a.message || a.description || ''}</span>
    </div>`);
});

// ── Afficher dernier badge ────────────────────────────
function afficherDernierBadge(uid, profil) {
  const label = profil.label || 'inconnu';
  const nb    = profil.nb_visites || 1;
  document.getElementById('badge-panel').innerHTML = `
    <div class="badge-alert">
      <div class="badge-uid">${uid}</div>
      <div style="font-size:.75rem;color:var(--dim);margin-top:4px">
        ${profil.card_type || ''}
      </div>
      <div>
        <span class="label-pill label-${label}">${label.toUpperCase()}</span>
      </div>
      <div style="margin-top:10px">
        <div class="mrow" style="padding:4px 0">
          <span class="mlabel" style="font-size:.78rem">Visites</span>
          <span class="mval" style="font-size:.85rem">${nb}</span>
        </div>
        <div class="mrow" style="padding:4px 0">
          <span class="mlabel" style="font-size:.78rem">1ère visite</span>
          <span class="mval" style="font-size:.72rem">
            ${profil.premiere_visite ? new Date(profil.premiere_visite).toLocaleDateString('fr-FR') : '—'}
          </span>
        </div>
        <div class="mrow" style="padding:4px 0;border:none">
          <span class="mlabel" style="font-size:.78rem">Heure habituelle</span>
          <span class="mval" style="font-size:.85rem">${heureFrequente(profil.heures_frequentes)}</span>
        </div>
      </div>
    </div>`;
}

// ── Helpers ───────────────────────────────────────────
function heureFrequente(raw) {
  try {
    const h = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if (!h) return '—';
    const best = Object.entries(h).sort((a,b) => b[1]-a[1])[0];
    return best ? best[0] + 'h' : '—';
  } catch { return '—'; }
}

function jourFrequent(raw) {
  try {
    const j = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if (!j) return '—';
    const best = Object.entries(j).sort((a,b) => b[1]-a[1])[0];
    return best ? JOURS[parseInt(best[0])] : '—';
  } catch { return '—'; }
}

function labelClass(label) {
  return 'label-' + (label || 'inconnu');
}

// ── Tableau profils ───────────────────────────────────
async function chargerProfils() {
  const r = await fetch('/api/profils');
  const profils = await r.json();
  rendreTableauProfils(profils);

  // Compteurs
  const total = profils.length;
  const reg   = profils.filter(p => p.label === 'regulier').length;
  const occ   = profils.filter(p => p.label === 'occasionnel').length;
  const nouv  = profils.filter(p => p.label === 'nouveau').length;
  const pass  = profils.reduce((s, p) => s + (p.nb_visites||0), 0);

  document.getElementById('p-total').textContent       = total;
  document.getElementById('p-reguliers').textContent   = reg;
  document.getElementById('p-occasionnels').textContent= occ;
  document.getElementById('p-nouveaux').textContent    = nouv;
  document.getElementById('p-passages').textContent    = pass;

  // Heatmap heures
  const stats = await (await fetch('/api/stats')).json();
  rendreHeatmap(stats.passages_par_heure || []);
}

function rendreTableauProfils(profils) {
  document.getElementById('profils-tbody').innerHTML = profils.map(p => `
    <tr onclick="ouvrirModal('${p.uid}')">
      <td class="uid-cell">${p.uid}</td>
      <td>${p.card_type || '—'}</td>
      <td><span class="label-pill ${labelClass(p.label)}">${(p.label||'inconnu').toUpperCase()}</span></td>
      <td style="font-family:var(--mono)">${p.nb_visites}</td>
      <td>${p.premiere_visite ? new Date(p.premiere_visite).toLocaleDateString('fr-FR') : '—'}</td>
      <td>${p.derniere_visite ? new Date(p.derniere_visite).toLocaleString('fr-FR') : '—'}</td>
      <td style="font-family:var(--mono)">${heureFrequente(p.heures_frequentes)}</td>
      <td>${jourFrequent(p.jours_frequents)}</td>
    </tr>`).join('');
}

// ── Historique passages ───────────────────────────────
async function chargerRFID() {
  const r = await fetch('/api/rfid');
  const rows = await r.json();
  document.getElementById('rfid-tbody').innerHTML = rows.map(e => `
    <tr>
      <td style="font-size:.75rem">${new Date(e.timestamp).toLocaleString('fr-FR')}</td>
      <td class="uid-cell">${e.uid}</td>
      <td>${e.card_type || '—'}</td>
      <td style="font-family:var(--mono)">${e.heure}h</td>
      <td>${JOURS[e.jour_semaine] || e.jour_semaine}</td>
      <td><span class="label-pill ${labelClass(e.label)}">${(e.label||'—').toUpperCase()}</span></td>
      <td style="font-family:var(--mono)">${e.nb_visites || '—'}</td>
    </tr>`).join('');
}

// ── Heatmap ───────────────────────────────────────────
function rendreHeatmap(data) {
  const maxNb = Math.max(...data.map(d => d.nb), 1);
  const map   = {};
  data.forEach(d => { map[d.heure] = d.nb; });

  let html = '';
  for (let h = 0; h < 24; h++) {
    const nb    = map[h] || 0;
    const alpha = nb / maxNb;
    const bg    = `rgba(0,176,255,${0.08 + alpha * 0.75})`;
    html += `<div class="hm-cell" style="background:${bg}" title="${h}h: ${nb} passages">
               ${h}h</div>`;
  }
  document.getElementById('heatmap-heures').innerHTML = html;
}

// ── Modale ────────────────────────────────────────────
async function ouvrirModal(uid) {
  const r = await fetch('/api/profil/' + uid);
  const d = await r.json();
  const p = d.profil;

  document.getElementById('modal-content').innerHTML = `
    <h3 style="font-family:var(--mono);color:var(--blue);margin-bottom:16px">${uid}</h3>
    <span class="label-pill ${labelClass(p.label)}">${(p.label||'inconnu').toUpperCase()}</span>

    <div style="margin-top:16px">
      <div class="mrow"><span class="mlabel">Type carte</span><span class="mval">${p.card_type||'—'}</span></div>
      <div class="mrow"><span class="mlabel">Nombre de visites</span><span class="mval">${p.nb_visites}</span></div>
      <div class="mrow"><span class="mlabel">Première visite</span>
        <span class="mval">${new Date(p.premiere_visite).toLocaleString('fr-FR')}</span></div>
      <div class="mrow"><span class="mlabel">Dernière visite</span>
        <span class="mval">${new Date(p.derniere_visite).toLocaleString('fr-FR')}</span></div>
      <div class="mrow"><span class="mlabel">Heure habituelle</span>
        <span class="mval">${heureFrequente(p.heures_frequentes)}</span></div>
      <div class="mrow"><span class="mlabel">Jour habituel</span>
        <span class="mval">${jourFrequent(p.jours_frequents)}</span></div>
    </div>

    <div style="margin-top:16px;font-size:.68rem;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin-bottom:8px">
      50 derniers passages
    </div>
    <div style="max-height:220px;overflow-y:auto">
      ${(d.historique||[]).map(h => `
        <div class="histo-row">
          <span style="color:var(--dim)">${new Date(h.timestamp).toLocaleString('fr-FR')}</span>
          <span>${h.heure}h</span>
          <span style="color:var(--dim)">${JOURS[h.jour_semaine]||''}</span>
        </div>`).join('')}
    </div>`;

  document.getElementById('modal-overlay').classList.add('open');
}

function fermerModal(e) {
  if (!e || e.target === document.getElementById('modal-overlay') || !e.target) {
    document.getElementById('modal-overlay').classList.remove('open');
  }
}

// ── Statistiques ──────────────────────────────────────
async function chargerStats() {
  const r = await fetch('/api/stats');
  const s = await r.json();

  document.getElementById('s-mesures').textContent  = s.total_mesures || 0;
  document.getElementById('s-taux').textContent     = (s.taux_occupation || 0) + '%';
  document.getElementById('s-badges').textContent   = s.total_badges || 0;
  document.getElementById('s-passages').textContent = s.total_passages || 0;

  const labels = Array.from({length:24}, (_, i) => i + 'h');

  // Chart occupation
  const occData = new Array(24).fill(0);
  (s.occupation_par_heure || []).forEach(d => { occData[d.heure] = d.taux; });
  if (chartOcc) chartOcc.destroy();
  chartOcc = new Chart(document.getElementById('chart-occ').getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data: occData,
        backgroundColor: occData.map(v =>
          v > 70 ? 'rgba(255,23,68,.65)' :
          v > 40 ? 'rgba(255,109,0,.65)' :
                   'rgba(0,230,118,.45)'),
        borderRadius: 4,
      }]
    },
    options: chartOptions('% occupation', 100)
  });

  // Chart passages
  const passData = new Array(24).fill(0);
  (s.passages_par_heure || []).forEach(d => { passData[d.heure] = d.nb; });
  if (chartPass) chartPass.destroy();
  chartPass = new Chart(document.getElementById('chart-passages').getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{ data: passData, backgroundColor: 'rgba(0,176,255,.55)', borderRadius: 4 }]
    },
    options: chartOptions('Passages')
  });
}

function chartOptions(label, max) {
  return {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: '#37474f', font: { size: 10 } }, grid: { color: 'rgba(22,34,54,.5)' } },
      y: { ticks: { color: '#37474f' }, grid: { color: 'rgba(22,34,54,.5)' }, ...(max ? { max } : {}) }
    }
  };
}

// ── Commande porte ────────────────────────────────────
function cmd(c) {
  fetch('/api/commande', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ commande: c })
  });
}

// ── Init ──────────────────────────────────────────────
chargerStats();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

if __name__ == "__main__":
    log.info("Dashboard sur http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
