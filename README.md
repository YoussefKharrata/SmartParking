# 🍓 Guide Installation — Raspberry Pi

## Structure des fichiers

```
/home/pi/parking/
├── mqtt_bridge.py     ← Pont Arduino → MQTT + SQLite
├── ml_module.py       ← Machine Learning
├── dashboard.py       ← Dashboard Flask (port 5000)
├── data/
│   └── parking.db     ← Base SQLite (auto-créée)
├── models/
│   ├── model_prediction.pkl  ← Modèle prédiction (auto-créé)
│   └── model_anomalie.pkl    ← Modèle anomalie (auto-créé)
└── logs/
    ├── bridge.log
    └── ml.log
```

---

## 1. Installation des dépendances

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip mosquitto mosquitto-clients

pip3 install pyserial paho-mqtt flask flask-socketio \
             scikit-learn pandas numpy joblib
```

## 2. Démarrer Mosquitto (broker MQTT)

```bash
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
# Vérifier :
mosquitto_sub -h localhost -t "parking/#" -v
```

## 3. Trouver le port série de l'Arduino

```bash
ls /dev/tty*
# Généralement : /dev/ttyUSB0 ou /dev/ttyACM0
# Si besoin, modifier SERIAL_PORT dans mqtt_bridge.py
sudo usermod -a -G dialout pi
```

## 4. Générer les données initiales + entraîner les modèles

```bash
cd /home/pi/parking

# Générer 30 jours de données simulées réalistes
python3 ml_module.py --generate-data --jours 30

# Entraîner les modèles
python3 ml_module.py --train-only
# → Affiche la précision (ex: "Précision: 87.3%")
```

**Pourquoi des données simulées ?**
Le ML a besoin de données pour apprendre. Au démarrage du projet,
on n'a pas encore de données réelles. Les données simulées reproduisent
un comportement réaliste (pics matin/midi/soir, moins le weekend).
Une fois le système en production, le ML se re-entraîne automatiquement
toutes les heures sur les vraies données collectées par l'Arduino.

## 5. Lancer les 3 services

### Terminal 1 — Pont MQTT
```bash
python3 /home/pi/parking/mqtt_bridge.py
```

### Terminal 2 — Module ML
```bash
python3 /home/pi/parking/ml_module.py
```

### Terminal 3 — Dashboard
```bash
python3 /home/pi/parking/dashboard.py
```

Accès dashboard : **http://<IP_RASPBERRY>:5000**

Pour trouver l'IP : `hostname -I`

---

## 6. Démarrage automatique (systemd)

Créer `/etc/systemd/system/parking-bridge.service` :
```ini
[Unit]
Description=Parking MQTT Bridge
After=network.target mosquitto.service

[Service]
ExecStart=/usr/bin/python3 /home/pi/parking/mqtt_bridge.py
WorkingDirectory=/home/pi/parking
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

Même chose pour `parking-ml.service` et `parking-dashboard.service`.

```bash
sudo systemctl enable parking-bridge parking-ml parking-dashboard
sudo systemctl start  parking-bridge parking-ml parking-dashboard
```

---

## 7. Architecture des flux de données

```
Arduino (USB série)
    │  JSON toutes les 500ms
    ▼
mqtt_bridge.py
    ├── Publie → parking/sensor   (temps réel)
    ├── Publie → parking/rfid     (événements badges)
    ├── Publie → parking/alerte   (anomalies)
    └── Sauvegarde → parking.db (SQLite)
                              │
                              ▼
                        ml_module.py
                    ├── Lit parking.db
                    ├── Entraîne RandomForest + IsolationForest
                    ├── Publie → parking/ml/result
                    └── Re-entraîne toutes les heures
                              │
                              ▼
                        dashboard.py (Flask)
                    ├── Souscrit à parking/#
                    ├── Affiche temps réel via SocketIO
                    └── API REST /api/stats, /api/rfid, etc.
```

---

## 8. Topics MQTT

| Topic                  | Direction       | Contenu                          |
|------------------------|-----------------|----------------------------------|
| `parking/sensor`       | Bridge → tous   | Distance, état, anomalie         |
| `parking/rfid`         | Bridge → tous   | UID, autorisé, horodatage        |
| `parking/porte`        | Bridge → tous   | Ouvert/Fermé                     |
| `parking/alerte`       | Bridge → tous   | Type alerte, description         |
| `parking/ml/result`    | ML → dashboard  | Prédictions 12h, anomalie ML     |
| `parking/commande`     | Dashboard → Bridge | OPEN / CLOSE porte            |
| `parking/status`       | Bridge → tous   | online / offline                 |

---

## 9. Test rapide sans Arduino

```bash
# Simuler des données Arduino depuis le terminal
mosquitto_pub -h localhost -t parking/sensor -m \
  '{"type":"sensor","distance":12.5,"occupe":true,"rfid_autorise":true,"porte_ouverte":false,"uid":"DE:AD:BE:EF","anomalie":false}'
```
