#!/usr/bin/env python3

import serial
import json
import time
import sqlite3
import logging
import signal
import sys
import os
from datetime import datetime

import paho.mqtt.client as mqtt

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SERIAL_PORT  = "/dev/ttyUSB0"
SERIAL_BAUD  = 9600
MQTT_BROKER  = "localhost"
MQTT_PORT    = 1883
DB_PATH      = os.path.join(BASE_DIR, "data", "parking.db")
LOG_PATH     = os.path.join(BASE_DIR, "logs", "bridge.log")

os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

profil_cache = {}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sensor_data (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp     TEXT,
        heure         INTEGER,
        minute        INTEGER,
        jour_semaine  INTEGER,
        distance      REAL,
        occupe        INTEGER,
        porte_ouverte INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS rfid_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp     TEXT,
        uid           TEXT,
        card_type     TEXT,
        heure         INTEGER,
        jour_semaine  INTEGER,
        porte_ouverte INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS profils (
        uid               TEXT PRIMARY KEY,
        premiere_visite   TEXT,
        derniere_visite   TEXT,
        nb_visites        INTEGER DEFAULT 0,
        heures_frequentes TEXT,
        jours_frequents   TEXT,
        label             TEXT DEFAULT 'inconnu',
        card_type         TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS alertes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   TEXT,
        type_alerte TEXT,
        description TEXT,
        uid         TEXT
    )""")
    conn.commit()
    conn.close()
    log.info("Base de données initialisée.")


def profiler_badge(uid: str, card_type: str, now: datetime) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM profils WHERE uid = ?", (uid,)).fetchone()
    heure = now.hour
    jour  = now.weekday()

    if row is None:
        heures = json.dumps({str(heure): 1})
        jours  = json.dumps({str(jour): 1})
        conn.execute("""
            INSERT INTO profils
            (uid, premiere_visite, derniere_visite, nb_visites,
             heures_frequentes, jours_frequents, label, card_type)
            VALUES (?,?,?,1,?,?,'nouveau',?)
        """, (uid, now.isoformat(), now.isoformat(), heures, jours, card_type))
        profil = {
            "uid": uid, "nb_visites": 1, "label": "nouveau",
            "premiere_visite": now.isoformat(),
            "derniere_visite": now.isoformat(),
            "heures_frequentes": {str(heure): 1},
            "jours_frequents":   {str(jour): 1},
            "card_type": card_type
        }
        log.info("Nouveau badge enregistré : %s", uid)
    else:
        heures = json.loads(row["heures_frequentes"] or "{}")
        jours  = json.loads(row["jours_frequents"]   or "{}")
        heures[str(heure)] = heures.get(str(heure), 0) + 1
        jours[str(jour)]   = jours.get(str(jour), 0) + 1
        nb = row["nb_visites"] + 1
        if nb >= 10:
            label = "regulier"
        elif nb >= 3:
            label = "occasionnel"
        else:
            label = "nouveau"
        conn.execute("""
            UPDATE profils SET
                derniere_visite   = ?,
                nb_visites        = ?,
                heures_frequentes = ?,
                jours_frequents   = ?,
                label             = ?,
                card_type         = ?
            WHERE uid = ?
        """, (now.isoformat(), nb, json.dumps(heures), json.dumps(jours), label, card_type, uid))
        profil = {
            "uid":               uid,
            "nb_visites":        nb,
            "label":             label,
            "premiere_visite":   row["premiere_visite"],
            "derniere_visite":   now.isoformat(),
            "heures_frequentes": heures,
            "jours_frequents":   jours,
            "card_type":         card_type
        }
        log.info("Badge connu mis à jour : %s (label=%s, visites=%d)", uid, label, nb)

    conn.commit()
    conn.close()
    profil_cache[uid] = profil
    return profil


def sauvegarder_rfid_event(uid, card_type, heure, jour, porte_ouverte):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO rfid_events
        (timestamp, uid, card_type, heure, jour_semaine, porte_ouverte)
        VALUES (?,?,?,?,?,?)
    """, (datetime.now().isoformat(), uid, card_type, heure, jour,
          1 if porte_ouverte else 0))
    conn.commit()
    conn.close()


def sauvegarder_sensor(data, now):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO sensor_data
        (timestamp, heure, minute, jour_semaine, distance, occupe, porte_ouverte)
        VALUES (?,?,?,?,?,?,?)
    """, (
        now.isoformat(), now.hour, now.minute, now.weekday(),
        data.get("distance", 0),
        1 if data.get("occupe") else 0,
        1 if data.get("porte_ouverte") else 0,
    ))
    conn.commit()
    conn.close()


def get_tous_profils() -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM profils ORDER BY nb_visites DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        log.info("Connecté au broker MQTT")
        client.subscribe("parking/commande")
        client.publish("parking/status", json.dumps({
            "status": "online",
            "timestamp": datetime.now().isoformat()
        }), retain=True)
    else:
        log.error("Échec MQTT, code: %s", reason_code)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        cmd = payload.get("commande", "")
        if cmd in ["OPEN", "CLOSE"]:
            userdata["serial"].write((cmd + "\n").encode())
            log.info("Commande envoyée à l'Arduino : %s", cmd)
    except Exception as e:
        log.error("Erreur commande : %s", e)


def traiter_message(data: dict, client: mqtt.Client, ser: serial.Serial):
    now      = datetime.now()
    msg_type = data.get("type", "")
    data["timestamp"] = now.isoformat()

    if msg_type == "sensor":
        sauvegarder_sensor(data, now)
        client.publish("parking/sensor", json.dumps(data))

    elif msg_type == "rfid":
        uid       = data.get("uid", "")
        card_type = data.get("card_type", "")
        place_libre = data.get("place_libre", True)

        profil = profiler_badge(uid, card_type, now)

        if not place_libre:
            log.info("Place occupée — accès refusé pour UID: %s", uid)
            ser.write(b"CLOSE\n")
            sauvegarder_rfid_event(uid, card_type, now.hour, now.weekday(), False)
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                INSERT INTO alertes (timestamp, type_alerte, description, uid)
                VALUES (?,?,?,?)
            """, (now.isoformat(), "refus_place_pleine",
                  f"Badge {uid} refusé — place occupée", uid))
            conn.commit()
            conn.close()
            client.publish("parking/alerte", json.dumps({
                "type": "warning",
                "message": f"Badge {uid} refusé — place occupée",
                "timestamp": now.isoformat(),
            }))
        else:
            ser.write(b"OPEN\n")
            log.info("Porte ouverte pour UID: %s (%s)", uid, profil["label"])
            sauvegarder_rfid_event(uid, card_type, now.hour, now.weekday(), True)

        payload = {
            "timestamp":  now.isoformat(),
            "uid":        uid,
            "card_type":  card_type,
            "profil":     profil,
            "place_libre": place_libre,
        }
        client.publish("parking/rfid",   json.dumps(data))
        client.publish("parking/profil", json.dumps(payload))
        client.publish("parking/profils_all", json.dumps(get_tous_profils()))

    elif msg_type == "porte":
        client.publish("parking/porte", json.dumps(data))
        log.info("Porte : %s", data.get("etat"))


def main():
    log.info("=== Démarrage Parking Bridge (mode profiling) ===")
    init_db()

    try:
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=2)
        time.sleep(2)
        log.info("Port série ouvert : %s", SERIAL_PORT)
    except serial.SerialException as e:
        log.error("Port série introuvable : %s", e)
        log.error("Essayez : ls /dev/tty*")
        sys.exit(1)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata={"serial": ser})
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    except Exception as e:
        log.error("Connexion MQTT échouée : %s", e)
        sys.exit(1)

    client.loop_start()

    def signal_handler(sig, frame):
        log.info("Arrêt propre...")
        client.publish("parking/status",
                       json.dumps({"status": "offline"}), retain=True)
        client.loop_stop()
        ser.close()
        sys.exit(0)

    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log.info("En attente des données Arduino...")

    while True:
        try:
            if ser.in_waiting > 0:
                ligne = ser.readline().decode("utf-8", errors="ignore").strip()
                if not ligne:
                    continue
                if ligne == "SYSTEM:READY":
                    log.info("Arduino prêt !")
                    client.publish("parking/status", json.dumps({
                        "status": "arduino_ready",
                        "timestamp": datetime.now().isoformat()
                    }))
                    continue
                try:
                    data = json.loads(ligne)
                    traiter_message(data, client, ser)
                except json.JSONDecodeError:
                    log.debug("Ligne non-JSON : %s", ligne)
        except serial.SerialException as e:
            log.error("Erreur série : %s — Reconnexion dans 5s...", e)
            time.sleep(5)
        except Exception as e:
            log.error("Erreur : %s", e)
            time.sleep(1)


if __name__ == "__main__":
    main()