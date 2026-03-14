#!/usr/bin/env python3
"""
============================================================
 Parking Intelligent — mqtt_bridge.py
 
 Rôle :
   1. Lire les JSON de l'Arduino (série USB)
   2. Faire le PROFILING des badges RFID
   3. Décider d'ouvrir la porte → envoyer OPEN à l'Arduino
   4. Publier tout sur MQTT
   5. Sauvegarder dans SQLite (source pour le ML)

 Logique de profiling :
   - Chaque UID est enregistré avec horodatage, fréquence,
     heures habituelles, jours habituels
   - Un profil se construit automatiquement au fil du temps
   - Tout badge est accepté (parking ouvert), le profiling
     sert à la supervision et à la détection d'anomalies ML

 Installation :
   pip3 install pyserial paho-mqtt
   
 Démarrage :
   python3 mqtt_bridge.py
============================================================
"""

import serial
import json
import time
import sqlite3
import logging
import signal
import sys
import os
from datetime import datetime, timedelta
from collections import defaultdict

import paho.mqtt.client as mqtt

# ── Configuration ─────────────────────────────────────────
SERIAL_PORT  = "/dev/ttyUSB0"   # ou /dev/ttyACM0
SERIAL_BAUD  = 9600
MQTT_BROKER  = "localhost"
MQTT_PORT    = 1883
DB_PATH      = "/home/pi/parking/data/parking.db"
LOG_PATH     = "/home/pi/parking/logs/bridge.log"

os.makedirs("/home/pi/parking/data", exist_ok=True)
os.makedirs("/home/pi/parking/logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ── Cache profils en mémoire (évite trop de lectures DB) ──
profil_cache = {}  # uid → dict profil

# ═══════════════════════════════════════════════════════════
# BASE DE DONNÉES
# ═══════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    # Données capteurs (pour ML)
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

    # Événements RFID bruts
    c.execute("""CREATE TABLE IF NOT EXISTS rfid_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp     TEXT,
        uid           TEXT,
        card_type     TEXT,
        heure         INTEGER,
        jour_semaine  INTEGER,
        porte_ouverte INTEGER
    )""")

    # Profils utilisateurs (construit automatiquement)
    c.execute("""CREATE TABLE IF NOT EXISTS profils (
        uid              TEXT PRIMARY KEY,
        premiere_visite  TEXT,
        derniere_visite  TEXT,
        nb_visites       INTEGER DEFAULT 0,
        heures_frequentes TEXT,   -- JSON: {"8": 15, "17": 12, ...}
        jours_frequents   TEXT,   -- JSON: {"0": 10, "1": 8, ...}
        label             TEXT DEFAULT 'inconnu',  -- 'regulier','occasionnel','nouveau'
        card_type         TEXT
    )""")

    # Alertes
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


# ═══════════════════════════════════════════════════════════
# PROFILING RFID
# ═══════════════════════════════════════════════════════════
def profiler_badge(uid: str, card_type: str, now: datetime) -> dict:
    """
    Met à jour le profil d'un badge RFID.
    Retourne le profil mis à jour.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT * FROM profils WHERE uid = ?", (uid,)
    ).fetchone()

    heure = now.hour
    jour  = now.weekday()

    if row is None:
        # Nouveau badge → créer profil
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
        # Badge connu → mettre à jour
        heures = json.loads(row["heures_frequentes"] or "{}")
        jours  = json.loads(row["jours_frequents"]   or "{}")

        heures[str(heure)] = heures.get(str(heure), 0) + 1
        jours[str(jour)]   = jours.get(str(jour), 0) + 1

        nb = row["nb_visites"] + 1

        # Calculer le label selon la fréquence
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
        """, (
            now.isoformat(), nb,
            json.dumps(heures), json.dumps(jours),
            label, card_type, uid
        ))

        profil = {
            "uid":              uid,
            "nb_visites":       nb,
            "label":            label,
            "premiere_visite":  row["premiere_visite"],
            "derniere_visite":  now.isoformat(),
            "heures_frequentes": heures,
            "jours_frequents":   jours,
            "card_type":        card_type
        }
        log.info("Badge connu mis à jour : %s (label=%s, visites=%d)", uid, label, nb)

    conn.commit()
    conn.close()

    # Mettre en cache
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
    rows = conn.execute(
        "SELECT * FROM profils ORDER BY nb_visites DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════
# MQTT
# ═══════════════════════════════════════════════════════════
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("Connecté au broker MQTT")
        client.subscribe("parking/commande")
        client.publish("parking/status", json.dumps({
            "status": "online",
            "timestamp": datetime.now().isoformat()
        }), retain=True)
    else:
        log.error("Échec MQTT, code: %d", rc)


def on_message(client, userdata, msg):
    """Reçoit commandes dashboard → relaie à l'Arduino."""
    try:
        payload = json.loads(msg.payload.decode())
        cmd = payload.get("commande", "")
        if cmd in ["OPEN", "CLOSE"]:
            userdata["serial"].write((cmd + "\n").encode())
            log.info("Commande envoyée à l'Arduino : %s", cmd)
    except Exception as e:
        log.error("Erreur commande : %s", e)


# ═══════════════════════════════════════════════════════════
# TRAITEMENT DES MESSAGES ARDUINO
# ═══════════════════════════════════════════════════════════
def traiter_message(data: dict, client: mqtt.Client, ser: serial.Serial):
    now      = datetime.now()
    msg_type = data.get("type", "")
    data["timestamp"] = now.isoformat()

    # ── Données capteur ───────────────────────────────────
    if msg_type == "sensor":
        sauvegarder_sensor(data, now)
        client.publish("parking/sensor", json.dumps(data))

    # ── Badge RFID scanné ─────────────────────────────────
    elif msg_type == "rfid":
        uid       = data.get("uid", "")
        card_type = data.get("card_type", "")

        # 1. Profiler le badge
        profil = profiler_badge(uid, card_type, now)

        # 2. Sauvegarder l'événement
        sauvegarder_rfid_event(uid, card_type, now.hour, now.weekday(), True)

        # 3. Ouvrir la porte (tout badge → accès autorisé)
        ser.write(b"OPEN\n")
        log.info("Porte ouverte pour UID: %s (%s)", uid, profil["label"])

        # 4. Publier sur MQTT pour le dashboard
        payload = {
            "timestamp": now.isoformat(),
            "uid":       uid,
            "card_type": card_type,
            "profil":    profil,
        }
        client.publish("parking/rfid",   json.dumps(data))
        client.publish("parking/profil", json.dumps(payload))

        # 5. Publier la liste complète des profils
        tous = get_tous_profils()
        client.publish("parking/profils_all", json.dumps(tous))

    # ── État de la porte ──────────────────────────────────
    elif msg_type == "porte":
        client.publish("parking/porte", json.dumps(data))
        log.info("Porte : %s", data.get("etat"))


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
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

    client = mqtt.Client(userdata={"serial": ser})
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
