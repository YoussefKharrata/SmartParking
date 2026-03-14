/*
 * ============================================================
 *  Système de Parking Intelligent — Arduino Uno
 *  Logique :
 *    1. Capteur ultrason → place libre ou occupée
 *    2. Si place LIBRE  → lire badge RFID et envoyer au RPi
 *    3. Si badge lu     → ouvrir la porte (servo)
 *    4. Le Raspberry Pi fait le profiling (pas l'Arduino)
 *
 *  Pas de liste blanche sur l'Arduino — toute décision
 *  d'autorisation est prise côté Raspberry Pi.
 *
 *  Câblage :
 *    HC-SR04  : TRIG→D9  ECHO→D8
 *    MFRC522  : SDA→D10  SCK→D13  MOSI→D11  MISO→D12  RST→D5
 *               VCC→3.3V (JAMAIS 5V !)
 *    Servo    : Signal→D3 (PWM)  VCC→5V  GND→GND
 *    LED verte: D6 + résistance 220Ω
 *    LED rouge: D7 + résistance 220Ω
 * ============================================================
 */

#include <SPI.h>
#include <MFRC522.h>
#include <Servo.h>

// ── Broches ──────────────────────────────────────────────
#define TRIG_PIN     9
#define ECHO_PIN     8
#define LED_LIBRE    6
#define LED_OCCUPE   7
#define RST_PIN      5
#define SS_PIN       10
#define SERVO_PIN    3

// ── Paramètres ───────────────────────────────────────────
#define SEUIL_CM        10    // < 10 cm = véhicule présent
#define INTERVALLE_MS   500   // Lecture ultrason toutes les 500ms
#define PORTE_OUVERTE   90    // Angle servo ouvert
#define PORTE_FERMEE    0     // Angle servo fermé
#define DELAI_FERMETURE 5000  // Fermeture auto après 5 secondes

// ── Objets ───────────────────────────────────────────────
MFRC522 rfid(SS_PIN, RST_PIN);
Servo   servoPorte;

// ── Variables d'état ─────────────────────────────────────
bool          placeOccupee   = false;
bool          porteOuverte   = false;
unsigned long derniereLecture = 0;
unsigned long tempsOuverture  = 0;

// ═══════════════════════════════════════════════════════════
void setup() {
  Serial.begin(9600);
  while (!Serial);

  pinMode(TRIG_PIN,   OUTPUT);
  pinMode(ECHO_PIN,   INPUT);
  pinMode(LED_LIBRE,  OUTPUT);
  pinMode(LED_OCCUPE, OUTPUT);

  servoPorte.attach(SERVO_PIN);
  servoPorte.write(PORTE_FERMEE);

  SPI.begin();
  rfid.PCD_Init();

  // Clignotement démarrage
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_LIBRE,  HIGH);
    digitalWrite(LED_OCCUPE, HIGH);
    delay(200);
    digitalWrite(LED_LIBRE,  LOW);
    digitalWrite(LED_OCCUPE, LOW);
    delay(200);
  }
  digitalWrite(LED_LIBRE, HIGH); // Place libre au démarrage

  Serial.println(F("SYSTEM:READY"));
}

// ═══════════════════════════════════════════════════════════
void loop() {
  unsigned long now = millis();

  // ── 1. Lecture ultrason ───────────────────────────────
  if (now - derniereLecture >= INTERVALLE_MS) {
    derniereLecture = now;

    float dist     = lireDistance();
    bool  nouveau  = (dist > 0 && dist < SEUIL_CM);

    if (nouveau != placeOccupee) {
      placeOccupee = nouveau;

      // Véhicule parti → fermer la porte
      if (!placeOccupee) fermerPorte();

      mettreAJourLEDs();
    }

    // Envoyer état courant au Raspberry Pi
    envoyerSensor(dist);
  }

  // ── 2. Fermeture automatique après délai ─────────────
  if (porteOuverte && (millis() - tempsOuverture >= DELAI_FERMETURE)) {
    fermerPorte();
  }

  // ── 3. Lecture RFID — seulement si place LIBRE ───────
  //    (un véhicule veut entrer → il scanne son badge)
  if (!placeOccupee) {
    if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
      lireEtEnvoyerRFID();
      rfid.PICC_HaltA();
      rfid.PCD_StopCrypto1();
    }
  }

  // ── 4. Commandes depuis Raspberry Pi ─────────────────
  //    OPEN  → ouvrir la porte (RPi a validé le badge)
  //    CLOSE → fermer la porte
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if      (cmd == "OPEN")  ouvrirPorte();
    else if (cmd == "CLOSE") fermerPorte();
  }
}

// ═══════════════════════════════════════════════════════════
// Mesure de distance
// ═══════════════════════════════════════════════════════════
float lireDistance() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duree = pulseIn(ECHO_PIN, HIGH, 30000);
  if (duree == 0) return 0;

  float d = duree * 0.034 / 2.0;
  return (d < 2 || d > 400) ? 0 : d;
}

// ═══════════════════════════════════════════════════════════
// Lire badge RFID et envoyer brut au Raspberry Pi
// L'Arduino ne décide RIEN — il transmet juste l'UID
// ═══════════════════════════════════════════════════════════
void lireEtEnvoyerRFID() {
  // Construire UID lisible
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
    if (i < rfid.uid.size - 1) uid += ":";
  }
  uid.toUpperCase();

  // Type de carte
  MFRC522::PICC_Type type = rfid.PICC_GetType(rfid.uid.sak);
  String typeStr = rfid.PICC_GetTypeName(type);

  // Envoyer au Raspberry Pi → il traitera le profiling
  Serial.print(F("{\"type\":\"rfid\""));
  Serial.print(F(",\"uid\":\""));   Serial.print(uid);   Serial.print(F("\""));
  Serial.print(F(",\"card_type\":\"")); Serial.print(typeStr); Serial.print(F("\""));
  Serial.print(F(",\"place_libre\":true"));
  Serial.println(F("}"));

  // Clignoter pendant que le RPi décide
  clignoteLED(LED_LIBRE, 1, 200);
}

// ═══════════════════════════════════════════════════════════
// Porte
// ═══════════════════════════════════════════════════════════
void ouvrirPorte() {
  if (porteOuverte) return;
  servoPorte.write(PORTE_OUVERTE);
  porteOuverte   = true;
  tempsOuverture = millis();

  clignoteLED(LED_LIBRE, 2, 150);
  Serial.println(F("{\"type\":\"porte\",\"etat\":\"ouverte\"}"));
}

void fermerPorte() {
  if (!porteOuverte) return;
  servoPorte.write(PORTE_FERMEE);
  porteOuverte = false;
  Serial.println(F("{\"type\":\"porte\",\"etat\":\"fermee\"}"));
}

// ═══════════════════════════════════════════════════════════
// Envoi état capteur
// ═══════════════════════════════════════════════════════════
void envoyerSensor(float distance) {
  Serial.print(F("{\"type\":\"sensor\""));
  Serial.print(F(",\"distance\":")); Serial.print(distance, 1);
  Serial.print(F(",\"occupe\":")); Serial.print(placeOccupee ? F("true") : F("false"));
  Serial.print(F(",\"porte_ouverte\":")); Serial.print(porteOuverte ? F("true") : F("false"));
  Serial.println(F("}"));
}

// ═══════════════════════════════════════════════════════════
void mettreAJourLEDs() {
  digitalWrite(LED_LIBRE,  placeOccupee ? LOW  : HIGH);
  digitalWrite(LED_OCCUPE, placeOccupee ? HIGH : LOW);
}

void clignoteLED(int pin, int fois, int delaiMs) {
  bool etat = digitalRead(pin);
  for (int i = 0; i < fois; i++) {
    digitalWrite(pin, HIGH); delay(delaiMs);
    digitalWrite(pin, LOW);  delay(delaiMs);
  }
  digitalWrite(pin, etat ? HIGH : LOW);
}
