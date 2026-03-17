#include <SPI.h>
#include <MFRC522.h>
#include <Servo.h>

#define TRIG_PIN     9
#define ECHO_PIN     8
#define LED_LIBRE    6
#define LED_OCCUPE   7
#define RST_PIN      5
#define SS_PIN       10
#define SERVO_PIN    3

#define SEUIL_CM        10
#define INTERVALLE_MS   500
#define PORTE_OUVERTE   90
#define PORTE_FERMEE    0
#define DELAI_FERMETURE 5000

MFRC522 rfid(SS_PIN, RST_PIN);
Servo   servoPorte;

bool          placeOccupee   = false;
bool          porteOuverte   = false;
unsigned long derniereLecture = 0;
unsigned long tempsOuverture  = 0;

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

  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_LIBRE,  HIGH);
    digitalWrite(LED_OCCUPE, HIGH);
    delay(200);
    digitalWrite(LED_LIBRE,  LOW);
    digitalWrite(LED_OCCUPE, LOW);
    delay(200);
  }
  digitalWrite(LED_LIBRE, HIGH);

  Serial.println(F("SYSTEM:READY"));
}

void loop() {
  unsigned long now = millis();

  if (now - derniereLecture >= INTERVALLE_MS) {
    derniereLecture = now;

    float dist    = lireDistance();
    bool  nouveau = (dist > 0 && dist < SEUIL_CM);

    if (nouveau != placeOccupee) {
      placeOccupee = nouveau;
      if (!placeOccupee) fermerPorte();
      mettreAJourLEDs();
    }

    envoyerSensor(dist);
  }

  if (porteOuverte && (millis() - tempsOuverture >= DELAI_FERMETURE)) {
    fermerPorte();
  }

  if (!placeOccupee) {
    if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
      lireEtEnvoyerRFID();
      rfid.PICC_HaltA();
      rfid.PCD_StopCrypto1();
    }
  }

  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if      (cmd == "OPEN")  ouvrirPorte();
    else if (cmd == "CLOSE") fermerPorte();
  }
}

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

void lireEtEnvoyerRFID() {
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
    if (i < rfid.uid.size - 1) uid += ":";
  }
  uid.toUpperCase();

  MFRC522::PICC_Type type = rfid.PICC_GetType(rfid.uid.sak);
  String typeStr = rfid.PICC_GetTypeName(type);

  Serial.print(F("{\"type\":\"rfid\""));
  Serial.print(F(",\"uid\":\""));       Serial.print(uid);     Serial.print(F("\""));
  Serial.print(F(",\"card_type\":\"")); Serial.print(typeStr); Serial.print(F("\""));
  Serial.print(F(",\"place_libre\":"));
  Serial.print(placeOccupee ? F("false") : F("true"));
  Serial.println(F("}"));

  clignoteLED(LED_LIBRE, 1, 200);
}

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

void envoyerSensor(float distance) {
  Serial.print(F("{\"type\":\"sensor\""));
  Serial.print(F(",\"distance\":")); Serial.print(distance, 1);
  Serial.print(F(",\"occupe\":")); Serial.print(placeOccupee ? F("true") : F("false"));
  Serial.print(F(",\"porte_ouverte\":")); Serial.print(porteOuverte ? F("true") : F("false"));
  Serial.println(F("}"));
}

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
