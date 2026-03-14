#!/usr/bin/env python3

import json
import sqlite3
import threading
import logging
import os
from datetime import datetime

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(BASE_DIR, "data", "parking.db")
MQTT_BROKER = "localhost"
MQTT_PORT   = 1883

app      = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = "parking-iot-2025"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

etat = {
    "occupe":        False,
    "distance":      0,
    "porte_ouverte": False,
    "derniere_maj":  "",
    "mqtt_ok":       False,
    "arduino_ok":    False,
    "dernier_badge": None,
    "alertes":       [],
    "predictions":   [],
}


def on_mqtt_connect(client, userdata, flags, reason_code, properties):
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
            etat["dernier_badge"] = data
            socketio.emit("nouveau_badge", data)

        elif topic == "parking/profils_all":
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


mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_message = on_mqtt_message
try:
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    threading.Thread(target=mqtt_client.loop_forever, daemon=True).start()
except Exception as e:
    log.error("MQTT non disponible : %s", e)


@app.route("/")
def index():
    return render_template("dashboard.html")


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


if __name__ == "__main__":
    log.info("Dashboard sur http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)