# Scroll Speed

Régler la vitesse de la molette de la souris sous GNOME, le réglage qui manque
à côté de « Vitesse du pointeur ». Plus lent, plus rapide, une vitesse
horizontale séparée, une accélération optionnelle quand la molette tourne
vite. Fonctionne dans toutes les applications, sous Wayland comme sous X11.

[English version](README.md)

![Préférences](docs/preferences.png)

État : 0.1, construit et testé sur GNOME 51 / Ubuntu 26.10. Toute la chaîne
est couverte par des tests automatiques sur de vrais périphériques noyau, mais
elle n'a pas encore servi avec une souris physique (voir
[Ce qui est testé](#ce-qui-est-testé)).

## Installation

```bash
./pack.sh
gnome-extensions install --force scroll-speed@griffit.gmail.com.shell-extension.zip
```

Fermez la session puis rouvrez-la (Wayland ne peut pas charger une nouvelle
extension dans un Shell en cours d'exécution), activez *Scroll Speed*, ouvrez
ses préférences et :

1. cliquez une fois sur **Autoriser…** et saisissez votre mot de passe ;
2. déplacez le curseur **Vitesse de défilement**. Le changement est immédiat ;
   la zone *Essayer* en bas sert à le sentir.

Pour essayer sans fermer la session :

```bash
./try-now.sh 0.5
```

Le script demande la même autorisation si elle manque, puis divise la vitesse
de la molette par deux jusqu'à Ctrl+C.

## Fonctionnement

```
souris ──► evdev (noyau) ──► assistant (capture + mise à l'échelle) ──► clone uinput ──► libinput ──► GNOME ──► applications
```

`extension/helper/scroll_speed_helper.py` (Python, bibliothèque standard
uniquement) capture chaque souris à molette, crée un clone virtuel avec la
même identité et les mêmes boutons, et retransmet tous les événements tels
quels sauf la molette. Les déplacements de molette sont multipliés par le
facteur et émis au format haute résolution du noyau (120 unités par cran) :
×0,5 arrive donc aux applications comme un demi-cran, pas comme « un cran sur
deux ». Les applications qui ne comprennent que les crans entiers en reçoivent
un chaque fois qu'un cran complet s'est accumulé.

L'extension lance l'assistant, lui envoie les réglages au fil des changements
et l'arrête quand elle est désactivée. L'assistant s'arrête de lui-même si le
Shell disparaît, ce qui rend la souris. À la vitesse 1× sans accélération, les
souris ne sont pas touchées du tout.

### L'autorisation

L'assistant doit lire la souris et en créer une virtuelle. **Autoriser…**
installe `/etc/udev/rules.d/70-scroll-speed.rules` via polkit :

- accès à `/dev/uinput` et aux souris pour l'utilisateur connecté sur le poste
  (`uaccess`, le mécanisme qu'udev emploie pour les manettes et les cartes
  son) ;
- claviers et TrackPoint exclus : la règle n'expose jamais les frappes ;
- les clones retrouvent l'entrée de la base matérielle de la souris qu'ils
  reproduisent (DPI, marqueur trackball), donc le pointeur bouge exactement
  comme avant.

L'accès à `/dev/uinput` permet aux programmes de votre session de créer des
périphériques d'entrée ; les règles udev de Steam font de même. **Retirer…**
dans les préférences supprime la règle.

## Pourquoi pas une simple extension ?

C'est la première piste essayée, et elle est impossible sous GNOME 51 :

- Mutter remet l'événement de défilement au client Wayland *avant* que le code
  d'une extension ne s'exécute, et les événements livrés à une fenêtre
  n'atteignent jamais le JavaScript du Shell. Une extension ne peut ni les
  modifier ni même les voir.
- libinput sait mettre le défilement à l'échelle (profil d'accélération
  personnalisé, type « scroll »), mais Mutter ne transmet que la courbe de
  *déplacement du pointeur* de
  `org.gnome.desktop.peripherals.mouse custom-accel-config`.
- libinput 1.30 a des plugins Lua et Mutter 51 les charge depuis
  `~/.config/libinput/plugins`, ce qui ne demanderait aucun privilège, mais
  Ubuntu compile libinput sans Lua.

La molette est donc mise à l'échelle sous le compositeur. Si Mutter expose un
jour la courbe de défilement, ou si les distributions activent les plugins
Lua, l'assistant pourra être remplacé sans toucher aux préférences.

## Limites

- Molettes de souris uniquement. Le défilement du pavé tactile est calculé
  dans libinput et les TrackPoint défilent au bouton ; ni l'un ni l'autre
  n'est concerné.
- Un périphérique combiné clavier + souris qui apparaît comme un seul nœud est
  laissé tel quel (la règle ne donne aucun accès aux claviers).
- Une souris capturée par un autre programme (un remappeur) est ignorée, avec
  une nouvelle tentative toutes les 30 secondes.
- L'extension est désactivée sur l'écran de verrouillage, comme toute
  extension : la molette y reprend sa vitesse normale.

## Ce qui est testé

`tests/run.sh` lance tout :

- 24 tests unitaires de la mise à l'échelle (restes, changements de sens,
  accélération, souris basse résolution) ;
- 12 tests de bout en bout de l'assistant sur de vrais périphériques noyau :
  une fausse source créée avec uinput est capturée, clonée et mise à
  l'échelle, les réglages changent à chaud, la souris est rendue en réglage
  neutre (jamais au milieu d'un clic), à l'arrêt et sur SIGTERM, débranchement
  à chaud, un bouton maintenu retarde la capture, un second assistant refuse
  de démarrer ;
- tests unitaires de la logique des préférences, et de la règle udev exacte
  qu'écrit la commande d'installation (`udevadm verify`).

Vérifié à la main sur GNOME 51 :

- une souris virtuelle qui envoie 60/120 de cran fait défiler une fenêtre
  GTK 4 d'exactement 0,5 (12/120 donne 0,1 ; 240/120 donne 2) ;
- l'extension dans un Shell sans écran : assistant lancé, réglages transmis,
  arrêt à la désactivation, relance après un plantage ;
- la règle udev dans `udevadm test` : souris et uinput reçoivent l'accès, pas
  le clavier, un clone retrouve son `MOUSE_DPI`.

Pas encore testé : une souris physique de bout en bout, et la fenêtre polkit.

`tests/prefs_preview.js` ouvre la fenêtre de préférences hors du Shell et peut
l'enregistrer en PNG.

## Désinstallation

Préférences › **Retirer…**, puis `gnome-extensions uninstall scroll-speed@griffit.gmail.com`.

## Licence

GPL-2.0-or-later.
