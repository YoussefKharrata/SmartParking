#!/usr/bin/env python3

import json
import time
import random
import sys
from datetime import datetime
import paho.mqtt.client as mqtt
from config import NB_PLACES, MQTT_BROKER, MQTT_PORT

places_etat = {i: False for i in range(1, NB_PLACES + 1)}


def on_commande(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        cmd  = data.get("commande", "")
        if cmd == "OPEN":
            send_porte("ouverte")
        elif cmd == "CLOSE":
            send_porte("fermee")
    except Exception:
        pass


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_message = on_commande
client.connect(MQTT_BROKER, MQTT_PORT)
client.subscribe("parking/commande")
client.loop_start()

RFID_CARDS = [
    ("4A:3F:1C:88", "MIFARE 1KB"),
    ("B2:9E:47:D1", "MIFARE 1KB"),
    ("CC:11:5A:2F", "MIFARE Ultralight"),
    ("99:FF:AA:00", "MIFARE 1KB"),
    ("DE:AD:BE:EF", "MIFARE 1KB"),
]


def pub(topic, payload):
    client.publish(topic, json.dumps(payload))
    print(f"  >> {topic} : {json.dumps(payload)}")


def send_sensor(place_id=None, occupe=None, distance=None):
    global places_etat
    if place_id is None:
        place_id = random.randint(1, NB_PLACES)
    if occupe is None:
        occupe = places_etat[place_id]
    if distance is None:
        distance = round(random.uniform(3, 12), 1) if occupe else round(random.uniform(30, 100), 1)
    places_etat[place_id] = occupe
    pub("parking/sensor", {
        "type":          "sensor",
        "place_id":      place_id,
        "distance":      distance,
        "occupe":        occupe,
        "porte_ouverte": False,
        "timestamp":     datetime.now().isoformat(),
    })


def send_all_sensors():
    for pid in range(1, NB_PLACES + 1):
        send_sensor(place_id=pid)
        time.sleep(0.05)


RFID_VISIT_COUNTS = {}


def send_rfid(uid=None, card_type=None):
    global RFID_VISIT_COUNTS
    if uid is None:
        uid, card_type = random.choice(RFID_CARDS)
    now = datetime.now()

    RFID_VISIT_COUNTS[uid] = RFID_VISIT_COUNTS.get(uid, 0) + 1
    nb = RFID_VISIT_COUNTS[uid]
    label = "regulier" if nb >= 10 else "occasionnel" if nb >= 3 else "nouveau"

    place_libre = any(not v for v in places_etat.values())

    profil = {
        "uid":               uid,
        "nb_visites":        nb,
        "label":             label,
        "premiere_visite":   now.isoformat(),
        "derniere_visite":   now.isoformat(),
        "heures_frequentes": {str(now.hour): nb},
        "jours_frequents":   {str(now.weekday()): nb},
        "card_type":         card_type,
    }

    pub("parking/rfid", {
        "type":        "rfid",
        "uid":         uid,
        "card_type":   card_type,
        "place_libre": place_libre,
        "timestamp":   now.isoformat(),
    })
    pub("parking/profil", {
        "timestamp": now.isoformat(),
        "uid":        uid,
        "card_type":  card_type,
        "profil":     profil,
    })
    pub("parking/profils_all", list(
        {"uid": u, "nb_visites": v,
         "label": "regulier" if v >= 10 else "occasionnel" if v >= 3 else "nouveau",
         "card_type": "MIFARE 1KB", "premiere_visite": now.isoformat(),
         "derniere_visite": now.isoformat(),
         "heures_frequentes": json.dumps({str(now.hour): v}),
         "jours_frequents": json.dumps({str(now.weekday()): v})}
        for u, v in RFID_VISIT_COUNTS.items()
    ))

    if not place_libre:
        pub("parking/alerte", {
            "type":      "warning",
            "message":   f"Badge {uid} refusé — parking complet",
            "timestamp": now.isoformat(),
        })
        print(f"  !! Parking complet — refus pour {uid}")
    else:
        time.sleep(0.3)
        send_porte("ouverte")
        time.sleep(2)
        send_porte("fermee")


def send_porte(etat):
    pub("parking/porte", {
        "type": "porte",
        "etat": etat,
        "timestamp": datetime.now().isoformat(),
    })


def send_alerte(msg):
    pub("parking/alerte", {
        "type":      "danger",
        "message":   msg,
        "timestamp": datetime.now().isoformat(),
    })


def auto_loop():
    print("Mode automatique — Ctrl+C pour arrêter\n")
    while True:
        send_all_sensors()

        if random.random() < 0.15:
            send_rfid()

        if random.random() < 0.05:
            send_alerte("Occupation sans RFID détectée")

        time.sleep(2)


def manual_loop():
    print(f"Mode manuel — {NB_PLACES} places configurées")
    print("Commandes disponibles :")
    print("  s              tous les sensors (état actuel)")
    print("  s<N>           sensor place N (ex: s1)")
    print("  s<N>0          place N libre  (ex: s10)")
    print("  s<N>1          place N occupée (ex: s11)")
    print("  r              badge RFID aléatoire")
    print("  r <uid>        badge RFID avec UID précis")
    print("  o              porte ouverte")
    print("  f              porte fermée")
    print("  a <msg>        alerte manuelle")
    print("  etat           afficher état toutes les places")
    print("  q              quitter\n")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        parts = line.split(None, 1)
        cmd   = parts[0].lower()
        arg   = parts[1] if len(parts) > 1 else None

        if cmd == "q":
            break
        elif cmd == "etat":
            for pid, occ in places_etat.items():
                print(f"  Place {pid}: {'OCCUPÉE' if occ else 'LIBRE'}")
        elif cmd == "s":
            send_all_sensors()
        elif cmd.startswith("s") and len(cmd) >= 2:
            try:
                if cmd[-1] in ("0", "1"):
                    pid   = int(cmd[1:-1])
                    occup = cmd[-1] == "1"
                    if 1 <= pid <= NB_PLACES:
                        send_sensor(place_id=pid, occupe=occup)
                    else:
                        print(f"  Place invalide (1-{NB_PLACES})")
                else:
                    pid = int(cmd[1:])
                    if 1 <= pid <= NB_PLACES:
                        send_sensor(place_id=pid)
                    else:
                        print(f"  Place invalide (1-{NB_PLACES})")
            except ValueError:
                print("  commande inconnue")
        elif cmd == "r":
            if arg:
                send_rfid(uid=arg.upper(), card_type="MIFARE 1KB")
            else:
                send_rfid()
        elif cmd == "o":
            send_porte("ouverte")
        elif cmd == "f":
            send_porte("fermee")
        elif cmd == "a":
            send_alerte(arg or "Alerte test")
        else:
            print("  commande inconnue")


def main():
    mode = "manual"
    if len(sys.argv) > 1 and sys.argv[1] == "--auto":
        mode = "auto"

    print(f"Simulation Arduino — {NB_PLACES} places — mode {'automatique' if mode == 'auto' else 'manuel'}")
    print(f"Broker : {MQTT_BROKER}:{MQTT_PORT}\n")

    send_all_sensors()
    time.sleep(0.5)

    try:
        if mode == "auto":
            auto_loop()
        else:
            manual_loop()
    except KeyboardInterrupt:
        pass

    client.loop_stop()
    client.disconnect()
    print("Déconnecté.")


if __name__ == "__main__":
    main()