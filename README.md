# Wimmich 0.3.14

Lokale Fotoverwaltung als Picasa-Ersatz. Etappe 1: Ordnerbaum, Rasteransicht,
Vollbild, Bewertungen, Suche über den Index, RAW+JPG als Stapel.
Seit 0.3.0 dazu: Großansicht mit Zoom, beschleunigte RAW-Anzeige,
Farbmarkierungen und Ablehnen. Seit 0.3.1: Immich-Abgleich mit Alben
und Personen. Seit 0.3.2: laufender Abgleich, eigenes Einstellungsfenster,
Ordner wieder entfernbar. Seit 0.3.3: einfache Retusche. Seit 0.3.4:
Zuschnitt und Grundeinstellungen. Seit 0.3.5 läuft alles in einem
Fenster, mit der Tastenbelegung von Cammello. Als Nächstes der lokale
Export.

**0.3.6:** Regler reagieren sofort, Vollbild zeigt nur das Bild, Tab
blendet die Leisten weg, Zuschnittformate zum Anklicken, größere
Farbanzeige.

**0.3.7:** drei Fehler behoben — Zuschnitt wirkte nicht, das Bild sprang
beim Reglerziehen in der Größe, der Weißabgleich maß an der falschen
Stelle.

**0.3.8:** Picasa-Ansicht (alle Ordner untereinander), Ordner
ausschließbar, Wärme in Kelvin, Regler für Klarheit und Details.

**0.3.9:** Build über GitHub Actions mit beigelegtem exiftool.

**0.3.10:** exiftool wird von SourceForge bezogen (exiftool.org verweist
selbst dorthin).

**0.3.11:** „Alle Fotos" scrollt endlos — wahlweise Ordner für Ordner
oder alle Bilder durchgehend nach Datum.

**0.3.12:** Drehen (fein und in 90-Grad-Schritten) und perspektivisches
Entzerren.

**0.3.13:** Drehung bis ±45°, Formatumschaltung wirkt auf den
bestehenden Zuschnittrahmen, Pipetten-Zeiger, Wimmich steht unter der
GPL v3.

**0.3.14:** Metadaten und Vorschauen deutlich schneller, Filterleiste
mit Sternen und Farben, Diagnosefenster.

## Lizenz

Wimmich steht unter der **GNU General Public License, Version 3 oder
später**. Der vollständige Text liegt in `LICENSE`.

Das ist keine willkürliche Wahl: Wimmich baut auf **PyQt6**, und PyQt ist
auf allen Plattformen nur unter der GPL v3 oder einer kommerziellen
Lizenz von Riverbank zu haben — anders als Qt selbst gibt es PyQt nicht
unter der LGPL. Sobald eine gebaute Fassung an Dritte geht, muss das
Ganze GPL-verträglich sein.

Was sonst noch mitläuft:

| Baustein | Lizenz |
|---|---|
| Wimmich | GPL v3 oder später |
| PyQt6 | GPL v3 oder kommerziell |
| Qt (in den PyQt-Paketen) | LGPL v3 |
| Pillow | HPND/MIT-CMU |
| numpy | BSD-3 |
| rawpy | MIT, bündelt LibRaw (LGPL-2.1 oder CDDL-1.0) |
| opencv-python (optional) | Apache 2.0 |
| exiftool (beigelegt) | Perl Artistic License oder GPL |

exiftool läuft als **eigener Prozess**, wird also nicht eingebunden. Wird
es dem Paket beigelegt, gehört sein Lizenztext dazu — der Build legt ihn
mit ab.

Kein fremder Quelltext: die aus Cammello übernommenen Teile stammen aus
demselben Haus und standen dort unter CC0.

## Grundsatz

Die Dateien auf der Platte sind die Wahrheit. Wimmich verschiebt, benennt und
löscht nichts. Der SQLite-Index ist reiner Cache und darf jederzeit gelöscht
werden — beim nächsten Einlesen ist er wieder da. Bewertungen werden zusätzlich
in die Datei geschrieben (XMP/EXIF), bei RAW in einen Sidecar `BILD.xmp` nach
Lightroom-Konvention. Die RAW-Datei selbst wird nie angefasst.

## Standalone-Fassung bauen

`.github/workflows/build.yml` baut über GitHub Actions eine
Windows-Fassung, die **exiftool mitbringt** — der Nutzer muss nichts
weiter installieren.

Auslöser: ein Versionstag (`git tag v0.3.9 && git push --tags`) oder von
Hand über Actions → „Wimmich bauen" → „Run workflow". Beim Lauf von Hand
lässt sich ankreuzen, ob OpenCV mitsoll.

### Warum exiftool eigens geholt wird

PyInstaller sammelt Python-Module ein. exiftool ist keines, sondern ein
eigenes Programm — es käme also nie von allein ins Paket, auch wenn es
auf dem Build-Rechner liegt. Der Workflow lädt es deshalb gezielt,
benennt `exiftool(-k).exe` in `exiftool.exe` um (das `-k` lässt das
Programm auf einen Tastendruck warten, was beim Aufruf aus Wimmich heraus
fatal wäre), legt den Ordner `exiftool_files` daneben und gibt beides per
`--add-data` mit.

### Bezugsquelle

**Nicht** github.com/exiftool/exiftool — dort liegt der Perl-Quelltext,
und das Repository hat (Stand August 2026) keine einzige
Veröffentlichung mit angehängten Dateien. Eine Windows-Programmdatei gibt
es dort also nicht.

SourceForge — exiftool.org verweist seine Download-Verknüpfungen selbst
dorthin, um den eigenen Server zu entlasten. Gebraucht wird:

```
https://sourceforge.net/projects/exiftool/files/exiftool-<version>_64.zip/download
```

Zwei Dinge, die man dabei falsch machen kann:

- **Nicht** das Quellarchiv `Image-ExifTool-<version>.tar.gz` nehmen. Das
  ist die reine Perl-Fassung und läuft auf einem Windows-Rechner ohne
  Perl-Installation nicht. Nur `exiftool-<version>_64.zip` bringt eine
  Perl-Umgebung mit.
- **Nicht** die Adresse eintragen, die der Browser beim Herunterladen
  anzeigt. Die zeigt auf einen Spiegel und trägt ein Ablaufdatum
  (`e=...`) samt Sitzungsschlüssel — nach ein paar Stunden ist sie tot.
  Die `/download`-Adresse oben ist beständig und leitet selbst weiter.

Der Workflow lädt mit `curl.exe -L --fail`, prüft danach, ob die Datei
wirklich mit `PK` beginnt (SourceForge liefert bei Problemen gern eine
HTML-Seite mit Statuscode 200), und bricht ab, wenn `exiftool_files`
fehlt — das wäre das sichere Zeichen für das falsche Archiv.

`config.find_exiftool()` sucht dann in dieser Reihenfolge: die
Einstellung, das beigelegte exiftool, der PATH des Systems. Die
beigelegte Fassung hat Vorrang vor dem PATH, damit die Standalone-Fassung
sich überall gleich verhält.

Zwei Schritte im Workflow prüfen das Ergebnis, statt darauf zu hoffen:
`exiftool -ver` muss nach dem Herunterladen antworten, und nach dem Bauen
wird im fertigen Paket nachgesehen, ob `exiftool.exe` wirklich drin ist
und läuft. Beides bricht den Build ab, wenn es fehlschlägt.

### Warum --onedir und nicht --onefile

`exiftool_files` besteht aus tausenden kleiner Perl-Dateien. Bei
`--onefile` würden die bei **jedem** Start in ein Temporärverzeichnis
entpackt — das kostet Sekunden. Das Ergebnis ist deshalb ein Ordner, der
als ZIP verteilt wird.

### Größe

Hier gemessen (Linux, dieselbe Zusammenstellung): 218 MB entpackt,
124 MB als ZIP. Den Löwenanteil hat Qt mit rund 102 MB, dann numpy mit
42 MB. **Mit OpenCV kommen 195 MB obendrauf** — deshalb ist es
standardmäßig draußen.

### Obergrenzen in requirements.txt

Alle Abhängigkeiten haben eine Obergrenze. Das ist die Lehre aus einem
Cammello-Build: dort hat eine 18 Stunden zuvor veröffentlichte Fassung
einer Abhängigkeit den Windows-Build zerlegt, während Mac und Linux
durchliefen. Ohne Deckel kann jede beliebige Veröffentlichung einen
Build über Nacht kippen, der gestern noch lief.

## Installation

```
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python main.py
```

Zusätzlich muss **exiftool** im PATH liegen (`exiftool -ver` muss antworten).
Ohne exiftool startet die App, zeigt aber keine Metadaten und schreibt keine
Bewertungen in die Dateien — die Statuszeile weist darauf hin.

## RAW und JPEG als Stapel

Dateien mit gleichem Namensstamm im gleichen Ordner gelten als eine Aufnahme:
`DSC_0001.NEF` und `DSC_0001.JPG` ergeben eine Kachel mit dem Abzeichen
„RAW+JPG". `DSC_0001-2.JPG` ist ein eigener Stamm und bleibt getrennt.

Drei Folgen davon:

- Eine Bewertung gilt für **alle** Dateien des Stapels. Sonst hätte das NEF
  vier Sterne und das JPEG keine.
- Die Vorschau wird aus dem JPEG gezogen, nicht aus dem NEF. Das spart bei
  jeder Kachel das Entwickeln der RAW-Datei.
- Welche Datei den Stapel vertritt, steuert `prefer_raw` in der
  Konfigurationsdatei (Vorgabe: RAW).

Der Schalter „RAW+JPG stapeln" in der Suchleiste schaltet das ab; die
Einstellung wird gemerkt.

## Bewerten, ablehnen, markieren

Konventionen von Lightroom und Bridge, wie in Cammello:

- **Sterne** 0 bis 5, in `xmp:Rating`.
- **Ablehnen** ist Bewertung −1, kein eigenes Feld. Abgelehnte Kacheln
  werden gedämpft gezeichnet. Sobald ein Sternfilter aktiv ist, fallen
  sie immer heraus — eine Ablehnung hat keine sinnvolle Sternzahl.
- **Farbmarkierung** steht als TEXT in `xmp:Label`. Der Text hängt an der
  Sprache des Lightroom-Farbsatzes: derselbe rote Punkt heißt einmal
  „Rot" und einmal „Red". Wimmich liest gegen alle bekannten Sätze und
  schreibt im eingestellten (`label_set`, Vorgabe `de`). Ein unbekannter
  Text wird nicht angetastet.

## Großansicht

Zwei Ebenen: beim Blättern kommt die auf 2560 px verkleinerte Fassung,
erst beim Zoomen das unverkleinerte Bild. Nachbarbilder werden im Voraus
geladen.

**RAW ist deshalb schnell:** für die Anzeige wird die eingebettete
JPEG-Vorschau benutzt, nicht `rawpy.postprocess()`. Entwickelt wird nur,
wenn keine eingebettete Vorschau existiert.

## Filtern nach Sternen und Farben

In der Leiste über dem Raster: fünf Sterne, fünf Farbfelder, „ohne", ein
✕ für Abgelehnte. Die Regel ist die aus Cammello:

- **Sterne wirken als UND** — ein Klick auf den dritten Stern zeigt alles
  ab drei Sternen. Nochmal auf denselben Stern hebt den Filter auf.
- **Farben wirken als ODER** — mehrere Felder gleichzeitig sind möglich.
  „ohne" fängt auch Bilder mit einem unbekannten Markierungstext ein, wie
  er entsteht, wenn in Lightroom ein eigener Farbsatz benutzt wird.
- **Beide Gruppen zusammen wieder als UND**: drei Sterne UND (Rot ODER
  Grün).

Ein abgelehntes Bild kommt durch keinen aktiven Sternfilter — eine
Ablehnung hat keine sinnvolle Sternzahl.

## Warum es jetzt schneller ist

Drei Engpässe, alle gemessen:

**Metadaten liefen komplett über exiftool.** Das kostet 2,1 ms je Datei
im günstigsten Fall und 18 ms bei kleineren Stapeln, weil der
Prozessstart dann nicht mehr aufgeht. Pillow liest dieselben Angaben aus
JPEG und TIFF in **0,4 ms** — Aufnahmedatum, Kamera, Objektiv, Maße,
Bewertung und Farbmarkierung. exiftool wird nur noch für RAW-Dateien,
XMP-Sidecars und alles gebraucht, was Pillow nicht öffnen kann.

**Metadaten wurden erst nach dem gesamten Durchlauf gelesen.** Bei einer
großen Bibliothek standen die Bilder deshalb lange unter „ohne
Aufnahmedatum". Jetzt läuft das Lesen Ordner für Ordner mit.

**RAW-Vorschauen ohne rawpy starteten exiftool je Datei.** Ein einzelner
Start kostet 85 ms unter Linux und ein Vielfaches unter Windows mit der
mitgelieferten Perl-Umgebung — bei 300 RAW-Dateien sind das Minuten.
Wimmich holt die eingebettete Vorschau jetzt selbst aus der Datei: RAW
tragen ihre Vorschauen als vollständige JPEG-Blöcke in sich, erkennbar an
den Markierungen FF D8 und FF D9. Gelesen wird stufenweise (4, dann 24,
dann 64 MB), weil die große Vorschau meist vorn liegt. Ergebnis: **15 ms**
statt 85 und mehr.

Dazu kommt `draft()` beim JPEG-Dekodieren — der Dekoder arbeitet gleich
verkleinert, statt erst das ganze Bild aufzubauen. Bei einer 45-MP-Datei
sinkt die Kachelerzeugung von **1049 ms auf 326 ms**.

## Diagnose

Der Knopf „Diagnose" in der Werkzeugleiste sagt, woran es hängt: ob
rawpy und exiftool vorhanden sind, wie viele Bilder noch ohne Metadaten
sind, und wie lange das Lesen und Dekodieren der ersten Datei der
aktuellen Ansicht tatsächlich dauert. Das unterscheidet in Sekunden
zwischen „rawpy fehlt", „exiftool fehlt" und „der Durchlauf ist noch
nicht fertig".

**Wenn RAW-Vorschauen zäh sind, ist fast immer rawpy nicht installiert.**
Dann greift der Notweg oben, der zwar schnell, aber gröber ist.

## Alle Fotos — endlos scrollen

Ganz oben im Baum steht **Alle Fotos**. Die Sortierauswahl in der Leiste
bestimmt, wie durchlaufen wird:

- **Ordner** — Ordner für Ordner, jeder mit einer Kopfzeile aus Name und
  Pfad. Die Picasa-Ansicht.
- **Aufnahmedatum** — alle Bilder durchgehend chronologisch, mit einer
  Kopfzeile je Monat („Februar 2026"). Ordnergrenzen spielen keine Rolle
  mehr. Undatierte Aufnahmen stehen am Ende.

Kopfzeilen nehmen eine eigene Reihe ein und lassen sich nicht auswählen;
beim Blättern werden sie übersprungen. Sobald etwas im Suchfeld steht,
entfällt die Gruppierung — Treffer aus vielen Ordnern sollen zusammen
stehen.

### Wirklich endlos

Es wird nur geholt, was die Ansicht braucht: 300 Zeilen je Nachschub,
über Qts `canFetchMore`/`fetchMore`. Bei einem Bestand von 50.000
Aufnahmen (66.667 Dateien) steht das erste Stück nach **75 ms** in der
Ordneransicht und nach **148 ms** nach Datum — vorher hätte das
vollständige Laden gut zweieinhalb Sekunden gedauert.

Zwei Dinge waren dafür nötig, beide gemessen:

- **Keine Fensterfunktionen für die Stapelbildung.** `ROW_NUMBER() OVER
  (PARTITION BY …)` zwingt SQLite, erst das ganze Ergebnis aufzubauen:
  750 ms bis zur ersten Zeile. Dieselbe Auswahl über eine Unterabfrage
  (`p.id = (SELECT … LIMIT 1)`) plus Index auf `(stack_key, is_raw,
  filename)` kann dem Index folgen: **11 ms**.
- **Kein `taken_at IS NULL` in der Sortierung.** Der Ausdruck hebelt den
  Index aus (124 ms). `ASC NULLS LAST` liefert dieselbe Reihenfolge —
  undatierte hinten — und benutzt den Index: **2 ms**. Für SQLite älter
  als 3.30 gibt es einen Rückfall auf die langsame Schreibweise.

Ende-Taste und Strg+A holen den Rest nach; bei 50.000 Aufnahmen dauert
das rund 1,4 Sekunden. Die Sortierungen „Dateiname" und „Zuletzt
geändert" haben keinen passenden Index und brauchen rund 300 ms bis zur
ersten Zeile — für die Ordneransicht belanglos, in „Alle Fotos"
spürbar.

## Ordner ausschließen

Rechtsklick auf einen Ordner im Baum → **Ordner ausschließen**. Der
Ordner samt Unterordnern wird übergangen: die Einträge fliegen aus dem
Index, der Scanner läuft beim nächsten Mal gar nicht erst hinein, und
der Ordnerwächter überwacht ihn nicht mehr. **Die Dateien bleiben
unangetastet.** Ausgeschlossene Ordner stehen weiter im Baum, grau und
mit dem Zusatz „(ausgeschlossen)"; über dasselbe Menü kommen sie wieder
zurück.

## Ein Fenster, zwei Ansichten

Es gibt keine eigenen Fenster mehr für Ansicht und Bearbeitung. Rechts
neben dem Baum liegt entweder das **Raster** oder die **Lupe**; in der
Lupe steht die Bearbeitungsleiste daneben. **E** öffnet die Lupe, **G**
wechselt hin und her, **Esc** führt zurück.

## Tastenbelegung — wie in Cammello

Absichtlich Zeichen für Zeichen dieselbe, damit die Handgriffe von dort
hier sitzen. **F1** zeigt sie im Programm.

| Taste | Wirkung |
|---|---|
| ← → | blättern |
| ↑ ↓ | eine Rasterzeile (in der Lupe ein Bild) |
| Pos1 / Ende | erstes / letztes Bild |
| 0–5 | Sterne |
| 6 7 8 9 | Rot, Gelb, Grün, Blau |
| Strg+0 | Farbmarkierung entfernen |
| X | ablehnen |
| **M** | Ziffern umschalten: Sterne ⇄ Farben |
| E | Lupe · G Raster ein/aus |
| Z | Zoom umschalten · Strg +/− stufenweise |
| **+ / −** | Belichtung (ohne Strg) |
| **W** | Pipette für den Weißabgleich |
| **C** | Zuschnitt an/aus |
| darin 1–6 | frei, 3:2, 4:3, 1:1, 16:9, 5:4 — gleiche Ziffer nochmal kippt hoch ⇄ quer |
| darin Enter / Esc | übernehmen / abbrechen · Shift+C hebt auf |
| B | Vorher / Nachher |
| F | Vollbild — nur das Bild, keine Leisten |
| Tab | Leisten, linke Spalte (mit Suchfeld) und Panel ein/aus |
| I | Bildangaben |
| Strg+A / Strg+D | alles wählen / Auswahl aufheben |
| Strg+Z | Bearbeitungsschritt zurück |
| Strg+F | in die Suchleiste; Enter führt zurück |
| F5 / F6 | neu einlesen / Immich abgleichen · Strg+, Einstellungen |

Zwei Eigenheiten, die aus Cammello stammen und leicht zu übersehen sind:

- **M** schaltet um, was die Ziffern 0–5 tun. Im Sternmodus setzen sie
  Bewertungen, im Farbmodus Markierungen. Was gerade gilt, steht in der
  Einblendung unten rechts.
- **+ und −** ändern die Belichtung, **nicht** den Zoom. Der Zoom liegt
  auf Strg + und Strg −.

Alles davon geht auch per Rechtsklick auf eine Kachel.

## Bearbeiten

In der Lupe. Werkzeuge über die Tasten, Regler in der Leiste rechts:
Belichtung, Kontrast, Tiefen, Lichter, Sättigung, Wärme, Tint — dazu
„Farben auffrischen" für ausgeblichene Abzüge und Dias.

Die **Pipette** (W) macht den angeklickten Punkt neutral. Der Mauszeiger
wird dabei zur Pipette, deren Spitze genau auf dem Punkt sitzt, der
gemessen wird — Qt bringt keinen solchen Zeiger mit, er ist gezeichnet. Sie rechnet
die nötigen Regler direkt aus, statt zu probieren: Rot und Blau werden
in `apply_tone` um 0,30 gegeneinander verschoben, Grün um 0,18 — daraus
lässt sich auflösen. Ein Blaustich von R/G/B 0,410/0,500/0,575 wird zu
0,479/0,479/0,479, also exakt neutral.

Retusche-Werkzeuge (Fleck, Riss, Rote Augen) liegen auf der Leiste und
werden mit dem Pinselregler bedient.

### Drehen und Entzerren

In der Bearbeitungsleiste:

- **↺ 90° / ↻ 90°** oder Taste **R** (Umschalt+R andersherum)
- **Drehung** — feiner Regler, ±45° in Hundertstelschritten. Für schiefe
  Horizonte reichen wenige Grad; die großen Winkel sind für Aufnahmen
  gedacht, die schräg gehalten wurden. Bei 45° muss das Bild auf das
  2,5-Fache vergrößert werden, damit keine leeren Ecken bleiben — was
  außerhalb liegt, ist weg.
- **Perspektive ↕** — stürzende Linien. Positiv zieht die Oberkante
  auseinander, begradigt also eine von unten fotografierte Fassade.
- **Perspektive ↔** — dasselbe für seitliche Verzerrung.

**Es entstehen keine leeren Ecken.** Das Bild wird so weit vergrößert,
dass der Rahmen gefüllt bleibt. Der nötige Faktor wird nicht über eine
Formel bestimmt, sondern durch Ausprobieren: eine Formel müsste für
Drehung *und* Entzerrung gleichzeitig stimmen, die Prüfung kostet
dagegen nur vier Matrixmultiplikationen. Gemessen über Winkel von 0,5°
bis 45° und Entzerrung bis ±0,4: schwarze Fläche jeweils 0,00 %. Die
nötige Vergrößerung wächst mit dem Winkel: 1,4× bei 15°, 1,9× bei 30°,
2,5× bei 45° (bei einem quadratischen Bild 2,0×).

Eine reine Vierteldrehung ohne alles andere läuft über `numpy.rot90` —
exakt und ohne Neuberechnung der Bildpunkte.

### Warum Klicks trotzdem sitzen

Drehung, 90-Grad-Schritte, Entzerrung und die Vergrößerung stecken in
**einer** Abbildung, die von der *Ausgabe* zur *Eingabe* zeigt. Pillow
braucht für eine perspektivische Umformung genau diese Richtung — und
das Hauptfenster braucht sie, um einen Mausklick in der gedrehten
Ansicht auf die ursprüngliche Stelle zurückzurechnen. Eine Abbildung,
zwei Verwendungen, keine Invertierung.

Geprüft mit einem Bild, dessen vier Ecken verschieden gefärbt sind: nach
einer 90-Grad-Drehung landet ein Klick oben links exakt auf der Farbe,
die vorher unten links lag — und ebenso für die drei übrigen Ecken.

Die Reihenfolge dahinter: Retuschen liegen im **ungedrehten** Bild, dann
kommt die Geometrie, zuletzt der Zuschnitt. So wandert ein Fleck nicht,
wenn später gedreht wird.

### Zuschnitt

**C** schaltet den Modus an, **Enter** übernimmt. Ein Format aus der
Leiste oder über die Zifferntasten wirkt **auch auf einen bereits
gezogenen Rahmen**: der wird auf das neue Verhältnis gebracht, wobei
Mittelpunkt und Fläche so weit wie möglich erhalten bleiben und der
Rahmen im Bild bleibt. Solange der Modus läuft,
bleibt das ganze Bild sichtbar und der Rahmen liegt darüber; sobald er
aus ist, zeigt die Lupe den Zuschnitt. Darüber erscheint eine Leiste mit den
Formaten zum Anklicken — frei, 3:2, 4:3, 1:1, 16:9, 5:4 — plus einem
Knopf für hoch/quer. Die Zifferntasten 1–6 tun dasselbe, und dieselbe
Ziffer nochmal kippt zwischen hoch und quer. Beim Aufziehen bleibt das **ganze** Bild
sichtbar, der Bereich außerhalb wird nur abgedunkelt (mit Drittel-Linien
zum Ausrichten). Das ist kein Schönheitsfehler: Retuschen sind relativ
zum ganzen Bild gespeichert, und wenn die Ansicht mitschneiden würde,
säße ein danach gesetzter Fleck an der falschen Stelle. Der Zuschnitt
greift beim Ausgeben.

**Nichts davon verändert die Originaldatei.** Eine Bearbeitung ist eine
Liste von Arbeitsschritten, die in der Datenbank liegt und beim Anzeigen
frisch gerechnet wird. Bearbeitete Bilder tragen ein ✎ auf der Kachel.
Für ein fertiges Bild gibt es „Als Datei speichern" — dann wird in
voller Auflösung neu gerechnet, nicht die Vorschau hochskaliert.

Koordinaten und Pinselgrößen sind als Bruchteil des **ganzen** Bildes
gespeichert — auch dann, wenn die Ansicht gerade zugeschnitten ist.
Deshalb sitzt ein Fleck, der auf der verkleinerten oder zugeschnittenen
Ansicht gesetzt wurde, beim Ausgeben in voller Auflösung an derselben
Stelle, und er wandert nicht, wenn der Zuschnitt später geändert wird.

### Wie die Werkzeuge arbeiten

- **Fleck** sucht in der Nachbarschaft die Stelle, deren *Rand* dem Rand
  der Fehlstelle am ähnlichsten ist, kopiert sie herüber, gleicht ihre
  Farbe an und blendet sie weich ein. Der Farbabgleich ist der Punkt:
  ohne ihn entsteht ein Flicken anderer Farbe statt einer unauffälligen
  Stelle.
- **Riss** ist derselbe Reparaturpinsel entlang der gezogenen Linie.
- **Rote Augen** dämpft nur Bildpunkte, in denen Rot deutlich über Grün
  und Blau liegt, und zieht den Rotkanal auf das Niveau der anderen
  beiden. Die Pupille wird nicht schwarz gemalt, das Glanzlicht im Auge
  bleibt erhalten.
- **Auffrischen** zieht jeden Farbkanal einzeln auf den vollen Bereich —
  das holt Kontrast zurück *und* nimmt den Farbstich, weil verblasste
  Kanäle unterschiedlich weit geschrumpft sind. Dazu auf Wunsch eine
  Grauwelt-Korrektur und eine Spur Sättigung. „Stärke vorschlagen"
  schätzt aus dem genutzten Tonwertumfang.

### Bekannte Grenze

Die Kacheln im Raster zeigen das **unbearbeitete** Bild; nur das ✎ weist
auf gespeicherte Schritte hin. Die Vorschauen im Cache stammen aus der
Originaldatei und werden nicht neu gerechnet.

### Warum die Regler sofort reagieren

Ein Reglerschritt auf einem 2560 px breiten Feld kostete rund 700 ms —
das Ziehen fühlte sich zäh an. Jetzt wird beim Ziehen auf einer auf
800 px verkleinerten Fassung gerechnet (etwa 6 ms), und nach 220 ms
Ruhe zieht Wimmich scharf nach. Die scharfe Fassung wird außerdem nur
so groß gerechnet, wie die Anzeigefläche wirklich ist: 10 ms statt
700 ms im eingepassten Zustand. Erst beim Zoomen auf 100 % wird das
ganze Feld gebraucht — dann dauert das Nachziehen wieder rund eine
halbe Sekunde, aber nur einmal nach dem Loslassen.

Auch das Schreiben in die Datenbank wird gesammelt (600 ms), statt bei
jedem Reglerschritt zu speichern.

### Feste Reihenfolge

Gerechnet wird immer in dieser Folge, egal wie gesetzt wurde:

1. Farben auffrischen — sonst passte ein Fleck zum verblassten Bild
2. Grundeinstellungen
3. Flecken, Risse, rote Augen — im ungedrehten Bild
4. Drehen und Entzerren
5. Zuschnitt — zuletzt, im gedrehten Rahmen

Zuschnitt, Geometrie, Grundeinstellungen und Auffrischen gibt es je Bild
nur einmal;
ein neuer Wert ersetzt den alten. Stehen alle Regler auf null, wird gar
kein Schritt gespeichert.

### OpenCV ist optional

Ist `opencv-python` installiert, füllt Wimmich Risse mit dem
Telea-Verfahren; das schließt dünne Strukturen sauberer. Ohne OpenCV
läuft alles weiter, nur mit einem einfacheren Ausbreiten der
Nachbarwerte.

**Bei einem RAW+JPG-Stapel wird das JPEG bearbeitet.** Eine RAW-Datei
lässt sich nicht sinnvoll pixelweise verändern.

## Personen

Unter **Erkunden → Personen** stehen die benannten Personen, jede mit
ihrem Gesichtsbildchen vom Server (der Cache liegt neben den
Vorschauen und darf jederzeit gelöscht werden). Rechtsklick:

- **Umbenennen …**
- **Andere Person hier aufgehen lassen …** — die angeklickte Person
  bleibt, die gewählte geht in ihr auf. Das geschieht auch auf dem
  Server und lässt sich nicht zurücknehmen.

Erkannte Gesichter ohne Namen stehen NICHT einzeln im Baum, sondern
hinter dem Eintrag **Ohne Namen (n)** — ein Klick zeigt sie als
Kacheln im Raster, wo man sie an ihrem Gesicht erkennt. Dort per
Rechtsklick oder Doppelklick benennen, oder in eine schon benannte
Person schieben. Ohne Verbindung zu Immich sind alle diese Einträge
ausgegraut: geändert wird immer zuerst auf dem Server, danach zieht
Wimmich nach.

## Einstellungen

Strg+, oder „Einstellungen" in der Werkzeugleiste. Drei Reiter:

- **Bibliothek** — Ordner hinzufügen und entfernen. Beim Entfernen
  verschwinden nur die Einträge aus dem Index; die Bilder auf der Platte
  bleiben unangetastet. Einen Ordner aufnehmen geht seit 0.3.43 auch
  direkt über Datei → „Ordner hinzufügen …".
- **Ansicht** — Kachelgröße, Vorschaugröße, wer den Stapel vertritt,
  Sprache der Farbmarkierungen, ob XMP geschrieben wird.
- **Immich** — Server, Schlüssel, Hochladen, Personen, laufender
  Abgleich, Ordnerüberwachung, Verbindungstest.

## Bedienung

| Aktion | Weg |
|---|---|
| Bibliotheksordner aufnehmen | Einstellungen → Bibliothek |
| Neu einlesen | F5 |
| Ordner wählen | Baum links |
| Unterordner einbeziehen | Schalter „Unterordner" |
| Suchen | Suchfeld oben, Enter |
| Bearbeiten | E |
| Einstellungen | Strg+, |
| Immich abgleichen | F6 |
| Stapel an/aus | Schalter „RAW+JPG stapeln" |
| Bewerten | Tasten 0–5 im Raster oder in der Großansicht |
| Ablehnen | X |
| Farbe setzen | 6 Rot, 7 Gelb, 8 Grün, 9 Blau · Strg+0 entfernt |
| Alles davon per Maus | Rechtsklick auf die Kachel |
| Großansicht | Doppelklick auf die Kachel |
| Zoom | Klick ins Bild (100 % an der Klickstelle), Z, Mausrad, Strg +/− |
| Verschieben | Ziehen, solange gezoomt |
| Vollbild | F oder Doppelklick |
| Bildangaben | I |
| Blättern | Pfeiltasten, Leertaste |
| Zurück | Esc — erst Vollbild, dann Zoom, dann schließen |

## Dateien

| Datei | Aufgabe |
|---|---|
| `config.py` | Pfade, unterstützte Endungen, Einstellungen |
| `db.py` | SQLite-Index, Volltextsuche, Bewertungen |
| `exif.py` | exiftool im `-stay_open`-Modus, Stapel-Lesen, XMP-Schreiben |
| `thumbs.py` | Vorschau-Cache, RAW über eingebettete Vorschau bzw. rawpy |
| `scanner.py` | Hintergrunddurchlauf über die Ordner |
| `models.py` | Qt-Modell und Zeichnen der Kacheln |
| `viewer.py` | Vollbild |
| `mainwindow.py` | Fenster, Suche, Filter, Steuerung |
| `theme.py` | Farben und Stylesheet |
| `previews.py` | Ebenen-Cache, RAW-Beschleunigung, Vorausladen |
| `imageview.py` | Zoom, Verschieben, Overlays (aus Cammello portiert) |
| `marks.py` | Farbmarkierungen und Ablehnen |
| `immich.py` | Immich-Client nach offizieller Spezifikation |
| `sync.py` | Abgleich im Hintergrund |
| `settings.py` | Einstellungsfenster (Bibliothek, Ansicht, Immich) |
| `retouch.py` | Rechenkerne für Retusche, Regler, Kelvin (numpy) |
| `edits.py` | Schrittfolgen, Speichern, Anwenden |
| `canvas.py` | Bildfläche mit Zuschnitt, Pinsel, Pipette |
| `edit_panel.py` | Bearbeitungsleiste rechts |

Ablageorte unter Windows:
`%LOCALAPPDATA%\Wimmich\config\` (Einstellungen und Index),
`%LOCALAPPDATA%\Wimmich\cache\thumbs\` (Vorschaubilder).

## Geprüft

Getestet mit echten Dateien und exiftool 12.76:

- Stapel-Lesen der Metadaten, Bewertung schreiben und zurücklesen (JPEG und Sidecar)
- Bewertungen überleben ein vollständiges Löschen und Neuaufbauen des Index
- Volltextsuche inklusive Präfixsuche, Sonderzeichen brechen sie nicht
- Rekursive Ordnerabfrage trifft nicht versehentlich Nachbarordner
  (`/Fotos/Cann` findet nichts aus `/Fotos/Cannes`)
- Vorschau-Cache greift beim zweiten Aufruf
- Stapelbildung: Paare werden zusammengefasst, `-2`-Varianten nicht;
  eine Bewertung landet in beiden Dateien des Stapels und überlebt den
  Neuaufbau; Vorschau kommt nachweislich aus dem JPEG
- Bestehende Datenbanken aus 0.1.0 und 0.2.0 werden beim Start ergänzt,
  nicht verworfen (gegen echte alte Datenbanken beider Stände geprüft)
- Ablehnung und Farbe gehen durch XMP und kommen zurück; ein englisches
  „Green" aus Lightroom wird als Grün erkannt
- Sternfilter blendet Abgelehnte aus, Farbfilter greift über Sprachgrenzen
- Weiterblättern auf ein vorbereitetes Bild: rund 15 ms
- Immich gegen einen nachgebauten Server geprüft (`tests/fake_immich.py`),
  der die Spezifikation abbildet — in moderner UND alter Fassung:
  Endpunkterkennung, Rückfall bei den Gerätefeldern, Prüfsummenabgleich,
  Upload mit Sidecar, Dublettenerkennung, Alben, Personen, falscher
  Schlüssel, toter Server. Zweiter Abgleichlauf lädt nichts erneut hoch.
- Laufender Abgleich: Zeitgeber startet und stoppt nach Einstellung,
  Ordnerwächter schlägt bei einer neuen Datei an, liest neu ein und
  gleicht ab; ein toter Server im stillen Modus öffnet kein Fenster
- Ordner entfernen: Einträge verschwinden aus Index und Volltextsuche,
  die Dateien bleiben auf der Platte, der Wächter wird nachgezogen
- Retusche mit Messwerten statt Augenmaß, gegen künstlich beschädigte
  Bilder: Fleck 0,199 → 0,023 Abweichung vom Original; Riss 0,326 →
  0,010 (Telea) bzw. 0,011 (ohne OpenCV); rote Augen Rotüberschuss
  0,700 → −0,009 bei erhaltenem Glanzlicht; verblasstes Bild
  Tonwertumfang 0,167 → 0,992 und Farbstich 0,083 → 0,002
- Grundeinstellungen gemessen: +1 EV hebt die Helligkeit 0,500 → 0,850;
  Kontrast ±0,5 ergibt 0,065 bzw. 0,195 statt 0,130; Sättigung −1 macht
  0,000; Wärme ±0,5 verschiebt R/B von 1,005 auf 1,359 bzw. 0,743;
  Tiefen +0,8 hebt dunkle Stellen 0,168 → 0,367 und lässt helle fast in
  Ruhe (0,364 → 0,426)
- Zuschnitt: Ausgabe hat genau die eingestellten Anteile, greift auch in
  voller Auflösung, ein danach gesetzter Fleck behält seine Koordinaten
  im ganzen Bild
- Tastenbelegung Stück für Stück durchgespielt: Blättern, Pos1/Ende,
  Zeilensprung, Sterne, M-Umschaltung, Farben, Ablehnen, Strg+A/D,
  Lupe, Zoom, Belichtung über +/−, Strg+ als Zoom statt Belichtung,
  Pipette, Zuschnitt mit Seitenverhältnis und Hoch/Quer-Kippen, Enter,
  Shift+C, Vorher/Nachher, Bildangaben, Strg+Z, G und Esc
- Tempo gemessen: Reglerschritt 708 ms → 6 ms beim Ziehen; scharfes
  Nachziehen 10 ms im eingepassten Zustand (598 ms bei 100 % Zoom)
- Vollbild und Tab blenden Werkzeugleiste, Filterleiste, die ganze linke
  Spalte samt Suchfeld (seit 0.3.45), Panel und
  Statuszeile aus und wieder ein
- Zuschnittleiste: Knöpfe setzen das Format, Zifferntasten ebenso,
  hoch/quer kippt, Enter blendet die Leiste wieder aus
- Picasa-Ansicht: Kopfzeilen sitzen an den Ordnerwechseln, nehmen eine
  eigene Reihe ein und sind nicht auswählbar; 11 Bilder ergeben 15 Zeilen
- Ordner ausschließen: Einträge verschwinden, ein erneutes Einlesen holt
  sie nicht zurück, die Dateien bleiben liegen, Unterordner sind
  mitgemeint, „Wieder aufnehmen" stellt alles her
- Klarheit/Details an künstlichen Strukturen und an einer Kante gemessen
  (Zahlen oben); Klarheit 20 ms, Details 10 ms auf der Vorschaugröße
- Kelvin-Anzeige gegen mehrere Aufnahmetemperaturen geprüft, Hin- und
  Rückrechnung stimmt auf vier Stellen
- Drehung und Entzerrung: 90-Grad-Schritte tauschen die Kanten richtig
  (an vier verschieden gefärbten Ecken nachgewiesen), feine Drehung und
  Entzerrung hinterlassen 0,00 % leere Fläche, ein Klick landet nach
  einer 90-Grad-Drehung an allen vier Ecken exakt richtig, und ein Fleck
  behält bei 90° + 2° + Zuschnitt seine Koordinaten im Originalbild
- Formatumschaltung auf einen bestehenden Rahmen: 3:2, 4:3, 1:1, 16:9
  und 5:4 ergeben Verhältnisse von 1,500 / 1,333 / 1,000 / 1,778 / 1,250,
  das Kippen auf hoch ergibt 0,750, und der Rahmen bleibt im Bild
- Pipette rechnerisch geprüft: Blaustich 0,410/0,500/0,575 wird zu
  0,479/0,479/0,479 (Abweichung max−min: 0,0000)
- Die drei Fehler aus 0.3.7 gegen die Behebung nachgemessen:
  Zuschnitt 0,5 × 0,4 ergibt genau 0,5 × 0,4 der Anzeige und ein Klick
  auf die Mitte landet bei 0,50/0,40 im ganzen Bild; die dargestellte
  Breite bleibt über vier Reglerschritte und das scharfe Nachziehen auf
  809 px konstant (Abweichung 0,0 px), im Zoom ebenso; ein Pipettenklick
  auf eine blaustichige Fläche bringt sie von 0,413/0,500/0,568 auf
  0,476/0,476/0,476 (Abweichung 0,1544 → 0,0002)
- Retusche greift außerhalb des Pinsels nachweislich nicht ins Bild ein,
  verändert die Eingabe nicht, überlebt die Runde durch JSON und die
  Datenbank, und trifft bei 200 px und 800 px dieselbe relative Fläche

Nicht getestet, weil in dieser Umgebung kein echtes Material vorlag:
**RAW-Anzeige mit echten NEF/CR2-Dateien.** Der Weg ist gebaut (rawpy
`extract_thumb`, sonst `postprocess`), muss aber an deinem Material laufen.

## Herkunft

`imageview.py` und `previews.py` sind Portierungen aus
[Cammello](https://github.com/krichel89/Cammello) (`culling_view.py`,
`previews.py`), PyQt5 → PyQt6. Die Bedienung ist absichtlich identisch.

## Immich

Einrichten über „Immich einrichten": Serveradresse und API-Schlüssel
(Immich → Kontoeinstellungen → API-Schlüssel). Der Knopf „Verbindung
prüfen" sagt sofort, ob es klappt. Abgleich mit F6.

**Nötige Rechte am Schlüssel (16).** Sie stehen seit 0.3.41 auch im
Einstellungsfenster, in Immichs eigener Gruppierung und zum Abhaken —
Immich bietet dort nur Kreuzchen, ein Kopierblock nützt also nichts:

| Gruppe | Rechte |
| --- | --- |
| `asset` | read, view, download, upload, delete |
| `album` | read, create, update, delete |
| `albumAsset` | create, delete |
| `person` | read, update, merge |
| `user` | read |
| `server` | about |

`person.update` und `person.merge` kamen mit 0.3.44 dazu (Umbenennen
und Zusammenführen im Baum unter Erkunden → Personen); ohne sie
antwortet der Server mit 403, alles andere läuft weiter.

`asset.view` ist von `asset.read` und `asset.download` getrennt und wird
leicht übersehen: ohne dieses Recht antwortet der Server bei jeder
Vorschau mit 403 (das war die Ursache der leeren Serverkacheln bis
0.3.29). Die Kreuzchen in Wimmich sind eine reine Merkliste — Wimmich
kann in Immich keine Rechte setzen.

**Ablauf:** Für lokale Bilder ohne Serverkennung wird die SHA-1-Prüfsumme
gebildet und stapelweise angefragt, was schon auf dem Server liegt
(`/assets/bulk-upload-check`). Nur die tatsächlich fehlenden Dateien
gehen hoch. Bei RAW wird der XMP-Sidecar mitgeschickt, damit Bewertung
und Farbmarkierung gleich mitkommen. Danach werden Alben und Personen
geholt und lokal gespiegelt.

**Was NICHT passiert:** Wimmich lädt nichts vom Server herunter. Bilder,
die nur in Immich liegen, tauchen in den Alben nicht auf. Die Ordner auf
der Platte bleiben führend.

### Serverunabhängigkeit

Gebaut gegen die offizielle OpenAPI-Spezifikation, ohne Annahmen über
eine bestimmte Immich-Version:

- Authentifizierung über `x-api-key` — seit jeher unverändert.
- Die Endpunktnamen wurden irgendwann von Einzahl auf Mehrzahl
  umgestellt (`/asset/upload` → `/assets`, `/album` → `/albums`). Welche
  Fassung ein Server spricht, wird beim Verbinden einmal ausprobiert und
  gemerkt — nicht an einer Versionsnummer festgemacht, denn die Zuordnung
  Version→Pfad ist nirgends verlässlich dokumentiert.
- `deviceAssetId`/`deviceId` verlangen ältere Server, neuere lehnen sie
  ab. Wimmich schickt sie mit und lässt sie beim ersten Ablehnen weg.
  Entschieden wird an der ANTWORT des Servers, nicht an einem gemerkten
  Schalter — sonst verlieren gleichzeitige Uploads ein Rennen (seit
  0.3.42; gemessen: vorher scheiterten 3 von 8 Uploads bei vier
  Strängen).
- Umleitungen auf einen anderen Rechner werden ABGELEHNT. `urllib`
  würde den `x-api-key` mitschicken; ein untergeschobenes 302 reichte
  sonst, um den Schlüssel abzugreifen. Umleitungen innerhalb desselben
  Servers (gleiches Verfahren, gleicher Wirt) gehen durch. Als
  Serveradresse sind nur `http://` und `https://` zugelassen.

### Laufender Abgleich

Zwei Auslöser, beide im Einstellungsfenster abschaltbar:

- **Zeitgeber** — alle N Minuten (Vorgabe 15), solange Adresse und
  Schlüssel gesetzt sind.
- **Ordnerwächter** — reagiert auf Änderungen in den Bibliotheksordnern.
  Weil beim Kopieren einer Speicherkarte Meldung auf Meldung folgt,
  wartet Wimmich 8 Sekunden Ruhe ab, liest dann neu ein und gleicht
  danach ab.

Der laufende Abgleich ist **still**: er meldet sich nur in der
Statuszeile. Ein kurz nicht erreichbarer Server öffnet kein Fenster,
sondern wird beim nächsten Durchlauf erneut versucht. Nur der Abgleich
von Hand (F6) zeigt Fehler als Fenster.

Qt kann nur begrenzt viele Ordner gleichzeitig überwachen — auf Windows
ist die Grenze eng. Wimmich deckelt deshalb bei 400 Ordnern. Bei einer
sehr tief verschachtelten Bibliothek greift dann nur noch der Zeitgeber.

### Alben und Personen

Stehen im Seitenbaum unter den Ordnern. Beides ist ein Spiegel des
Servers: Personen kommen aus Immichs Gesichtserkennung, namenlose werden
ausgeblendet. Die Verbindung zu den lokalen Dateien läuft über die
Asset-Kennung — es erscheint also nur, was schon abgeglichen ist.

### Der Schlüssel

liegt im Klartext in der `config.json` im Benutzerprofil. Die Datei ist
nur für den angemeldeten Benutzer lesbar, aber das ist keine
Schlüsselverwaltung. Wer das ungern hat, legt in Immich einen eigenen
Schlüssel nur für Wimmich an, mit genau den Rechten oben.
