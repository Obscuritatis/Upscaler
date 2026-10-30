# Video Upscaler 4K

Een eenvoudig Windows-programma dat oude video's (VHS, camcorder, DVD, oude digitale clips) en foto's naar 4K opschaalt met AI (Real-ESRGAN) op je NVIDIA-kaart, met zo min mogelijk vervorming.

## Installeren (eenmalig)

### Makkelijkste manier: Upscaler4K.exe

1. Werk je NVIDIA-stuurprogramma bij via de NVIDIA app of nvidia.com.
2. Download **Upscaler4K-windows.zip** van de downloadpagina en pak hem uit op een schijf met veel vrije ruimte, bijvoorbeeld `D:\Upscaler4K`.
3. Dubbelklik op **Upscaler4K.exe**. Python is niet nodig.
   - De eerste keer zegt Windows misschien "Windows heeft uw pc beschermd". Dat komt doordat het programma niet officieel ondertekend is. Klik op *Meer info* en dan *Toch uitvoeren*.
   - Bij de eerste keer opschalen downloadt het programma zelf ffmpeg en Real-ESRGAN naar de map `tools` naast de .exe.
   - Gaat er iets mis bij het opstarten, dan staat de foutmelding in `upscaler-log.txt` naast de .exe.

### Of: met Python (start.bat)

1. **Werk je NVIDIA-stuurprogramma bij** via de NVIDIA app of nvidia.com. Het programma gebruikt Vulkan, en dat zit in het stuurprogramma.
2. **Zet de map `video-upscaler`** ergens neer op een schijf met veel vrije ruimte, bijvoorbeeld `D:\video-upscaler`.
3. **Dubbelklik op `start.bat`.**
   - Is Python nog niet geïnstalleerd, dan probeert het venster dat automatisch te doen. Sluit daarna het venster en dubbelklik opnieuw op `start.bat`.
   - Lukt dat niet, installeer dan Python via https://www.python.org/downloads/ en vink tijdens de installatie **"Add python.exe to PATH"** aan.
4. Bij de **eerste keer opschalen** downloadt het programma zelf ffmpeg en Real-ESRGAN (ongeveer 200 MB) naar de submap `tools`.

## Gebruiken

1. Kies wat je wilt opschalen:
   - **Bestanden toevoegen…** voor losse video's en foto's.
   - **Hele map toevoegen…** voor bijvoorbeeld een heel seizoen. Submappen worden meegenomen.
2. Kies de map waarin het resultaat moet komen. Laat je dit leeg, dan komt elk resultaat naast het origineel.
3. Kies je instellingen (zie hieronder).
4. Selecteer een video in de lijst en klik op **Proefstuk (10 sec)**. Je krijgt dan `naam_4K_proef.mp4` met 10 seconden uit het midden van de video. Daarna opent vanzelf de **voor/na-vergelijking**.
5. Tevreden? Klik op **Start opschalen**. Alles in de lijst wordt na elkaar verwerkt. Het resultaat heet `naam_4K.mp4`.

**Goed om te weten over de wachtrij:**
- Gaat er bij één aflevering iets mis, dan gaat het programma gewoon door met de volgende. Aan het eind zie je hoeveel er gelukt, overgeslagen of mislukt zijn.
- Bestanden die al een `_4K`-versie hebben, worden overgeslagen. Je kunt een seizoen dus gerust opnieuw starten.
- Je kunt op elk moment op **Stop** drukken of de computer uitzetten. Start je later dezelfde video met **dezelfde instellingen**, dan gaat hij verder waar hij gebleven was.
- Zolang het programma werkt, gaat de pc niet in slaapstand.

## Voor/na vergelijken

Klik op **Voor/na bekijken** (na een proefstuk opent dit vanzelf). Je ziet links het origineel en rechts het resultaat.
- **Sleep** in het beeld om de scheidslijn te verschuiven.
- **Dubbelklik** op een plek, of klik op **Inzoomen (100%)**, om de echte pixels te zien. Dubbelklik nog een keer om terug te gaan.
- Met **Ander moment** kies je een ander beeld uit de video.

## Foto's

Foto's (JPG, PNG, BMP, TIFF, WebP) kun je samen met video's of los toevoegen. Ze worden vergroot tot ze precies in 4K passen: 3840×2160 voor liggende foto's en 2160×3840 voor staande. Het resultaat heet `naam_4K.jpg` of `naam_4K.png`.

- Foto's die al 4K of groter zijn (zoals de meeste telefoonfoto's) worden overgeslagen.
- Telefoonfoto's die gedraaid zijn opgeslagen, worden automatisch rechtop gezet.
- Doorzichtige delen in PNG-bestanden blijven doorzichtig.
- **AI-sterkte** en **Soort beeld** werken ook voor foto's. Gebruik *Tekenfilm / animatie* voor tekeningen en strips.

## Extra's

| Extra | Uitleg |
|---|---|
| **Minder flikkering** (staat standaard aan) | AI-opschaling werkt per beeld, waardoor fijne details zoals haar, gras of een bakstenen muur een beetje kunnen "dansen". Deze stap middelt alleen pixels die van beeld tot beeld bijna gelijk zijn. Stilstaande details worden daardoor rustiger, terwijl beweging scherp blijft. |
| **Vloeiender beeld (50/60 beelden per seconde)** | Bij geïnterlacede video (VHS, DV) wordt elk halfbeeld een eigen beeld. Dat is precies de beweging zoals hij opgenomen is, zonder te raden. Bij andere video berekent RIFE de tussenbeelden. Bij een scènewissel kan er dan één beeld half-doorzichtig zijn. Het opschalen duurt ongeveer twee keer zo lang. De eerste keer wordt RIFE gedownload (ca. 430 MB). |
| **Gezichten herstellen** | GFPGAN maakt wazige gezichten scherp, wat vooral bij familiefoto's en -video's veel verschil maakt. Let wel op: het kan een gezicht licht veranderen, en in video kan een gezicht soms een beetje flikkeren. Vergelijk het daarom eerst met een proefstuk. De eerste keer worden extra onderdelen geïnstalleerd (ca. 450 MB). |
| **Pc uitzetten als alles klaar is** | Handig voor een nacht vol afleveringen. Na afloop krijg je 2 minuten om het uitzetten te annuleren met de knop **Uitzetten annuleren**. |

## Instellingen

| Instelling | Uitleg |
|---|---|
| **Soort beeld** | *Echte opnames* gebruikt het model `realesrgan-x4plus`. *Tekenfilm / animatie* gebruikt `realesr-animevideov3`, dat veel sneller is maar alleen goed werkt bij getekend beeld. |
| **AI-sterkte** | Bij 100% zie je alleen het AI-beeld: heel scherp, maar soms "geschilderd" (wasachtige gezichten, verzonnen details). Lager mengt het AI-beeld met een gewone, nette vergroting. **75%** is een goed begin; kies 50 tot 60% als het te kunstmatig oogt. |
| **Strepen bij beweging (deinterlace)** | Oude video (VHS, DV, DVD) is vaak "geïnterlaced", wat kamstrepen geeft bij beweging. *Auto* herkent dat meestal. Zie je toch strepen, zet het op *aan*. |
| **Ruis/korrel eerst verminderen** | Alleen voor erg korrelige of ruisende opnames. Meestal niet nodig, omdat het AI-model zelf al ruis wegfiltert. |
| **Zwarte balken tot 3840×2160** | Oude 4:3-video wordt 2880×2160 met zwarte balken links en rechts, zodat het bestand precies 4K is. De beeldverhouding blijft altijd correct. |
| **Bestandsformaat** | *H.265* geeft kleinere bestanden en speelt af in VLC en op de meeste tv's. De Windows-app Media Player heeft daarvoor de "HEVC-video-extensie" nodig. *H.264* speelt overal af, maar de bestanden zijn groter. |

## Goed om te weten

- **Het duurt lang.** Elk beeld gaat los door het AI-model. Met *Echte opnames* op een RTX 3060 kun je rekenen op enkele beelden per seconde. Dat is een schatting, want ik heb het niet op jouw kaart kunnen meten. Een uur VHS heeft 90.000 beelden, dus reken op een nacht of langer. Het venster toont tijdens het werk de echte snelheid en de resterende tijd.
- **Schijfruimte.** Tijdens het werk staan er tijdelijke beelden in een verborgen map `.upscale_werk_…` naast het resultaat. Voor een uur SD-video is dat ongeveer 60 tot 80 GB. Die map wordt na afloop automatisch verwijderd.
- **Eerlijk over de kwaliteit.** AI-opschalen maakt het beeld veel scherper, maar het *verzint* fijne details. Echte details die nooit op de band stonden, komen niet terug. Fijne structuren zoals gras en haar kunnen soms licht "flikkeren". **Minder flikkering** en een lagere AI-sterkte verminderen dat.
- **Geluid en datum** van het origineel worden overgenomen.

## Problemen?

- **"Real-ESRGAN vindt geen geschikte grafische kaart"**: werk het NVIDIA-stuurprogramma bij en herstart de pc.
- **Het venster sluit meteen**: open een opdrachtprompt in deze map en typ `py upscaler.py` om de foutmelding te zien.
- Het logboek van een lopende opdracht staat in `.upscale_werk_…\log.txt`.

## Zelf de .exe bouwen

Normaal bouwt GitHub de .exe automatisch. Wil je hem zelf maken, dubbelklik dan op `bouw-exe.bat` (Python moet geïnstalleerd zijn). Het resultaat staat in `dist\Upscaler4K`.

## Voor gevorderden: zonder venster

```
py upscaler.py --cli "C:\Video's\vakantie.avi" "C:\Foto's\oma.jpg" --uit D:\4K --sterkte 70
```

Je kunt ook een map opgeven. Opties: `--animatie`, `--sterkte 0-100`, `--deinterlace auto|aan|uit`, `--ontruis`, `--geen-balken`, `--h264`, `--geen-antiflikker`, `--vloeiend`, `--gezichten`, `--proef`.
