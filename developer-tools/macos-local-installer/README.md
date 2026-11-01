# Installation macOS locale — composant développeur 0.1.1

Compiler ses applications personnelles, contrôler leur provenance et les installer
avec sauvegarde. Le composant reste hors des `.app`, des ZIP publics et du cask.
Gatekeeper reste actif ; aucun abonnement Apple Developer n'est requis pour compiler.
Une signature ad hoc ne vaut ni signature Developer ID ni notarisation.

## Composant partagé et intégrations

Le moteur unique vit dans `ArN-LaB/homebrew-tap`. Chaque application contient
`install-local.command` et `.macos-local-install.json` : dépôt, build, bundle, identité,
version/SHA-256 du moteur et commits exacts des dépendances sources.
Le lanceur vérifie les octets avant exécution et garde son entrée Terminal pour les
confirmations. Il ne télécharge aucun code automatiquement.

Référence immuable publiée : **`local-installer-v0.1.1`**. Les intégrations clientes
contrôlent la version et le SHA-256 du moteur avant de l’exécuter.
Une évolution impose une nouvelle version et une mise à jour explicite des épingles.

Le moteur est cherché dans `../homebrew-tap/developer-tools/macos-local-installer`
puis `~/Documents/GitHub/homebrew-tap/developer-tools/macos-local-installer`.
`ARNLAB_INSTALLER_ROOT` choisit un autre emplacement, toujours contrôlé par SHA-256.
`ARNLAB_PYTHON` sélectionne Python 3.10+ avec les filtres tarfile sécurisés disponibles
(3.10.12+, 3.11.4+ ou 3.12+). Leur absence provoque un arrêt avant création de build.

## Commandes

Depuis le dépôt propre de l'application, après avoir commité ses changements :

```sh
./install-local.command --dry-run          # lecture seule, vérifie aussi les épingles
./install-local.command --build-only       # compile et crée un reçu, sans installer
./install-local.command --build --simulate # installation dans un nouveau dossier temporaire
./install-local.command                    # compilation et installation interactives
./install-local.command --list-backups     # inventaire, tailles et états, sans suppression
./install-local.command --app /chemin/App.app --receipt UUID
./install-local.command --rollback UUID_DE_TRANSACTION
```

L'installation exige `INSTALLER`, le retrait éventuel de quarantaine exige ensuite
`RETIRER`, la restauration exige `RESTAURER`. Les confirmations nécessitent un
Terminal interactif ; aucune option `--yes`. L'application cible doit être quittée
normalement avant installation/restauration. Le moteur vérifie son activité au début
et juste avant publication ; il ne ferme ni ne tue aucun processus.

## Zenith et Homebrew : deux installations distinctes

Le build personnel est **`/Applications/Zenith Local.app`**, identifiant
**`com.arn-lab.zenith.local`**. L'application du cask reste **`Zenith.app`**, identifiant
`com.arn-lab.zenith`. Le nouveau build local reçoit son identité dans son dossier de
compilation privé, avant scellement ad hoc. Les sources Swift et le build public
restent inchangés. Un bundle déjà scellé ou portant une autre signature est refusé
pour cette personnalisation, jamais re-signé silencieusement.

Le moteur lit les reçus des Caskrooms Homebrew standard et **refuse de remplacer ou
restaurer toute cible gérée par un cask**, même si elle est absente ou mal signée.
Il n'appelle pas `brew`, ne désinstalle rien et ne modifie aucune inscription Homebrew.
La cohabitation permet d'installer Zenith Local même si l'ancien Zenith du cask
échoue au contrôle de signature ; l'ancien bundle n'est ni déplacé ni réparé.
La publication/correction éventuelle de cette ancienne release est un sujet distinct.

Les préférences liées au bundle ont un domaine distinct. Les sources Zenith utilisent
encore un dossier de travail commun `~/Library/Application Support/VPN Tools/runtime` :
**ne pas lancer simultanément les deux éditions pour des tests VPN**. Le composant
ne migre ni paramètres, données ou secrets, et n'exécute aucun test VPN.

## Provenance et signatures

- Origine Git attendue, arbre propre, HEAD/arbre enregistrés et recontrôlés après build.
  Compilation d'exports Git isolés ; les anciens builds et fichiers ignorés du checkout
  ne servent pas de sources. Toute dépendance source exige un SHA complet, contrôlé
  avant création du workspace, puis exporté à ce SHA. Aucun checkout existant n'est modifié.
- Bundle APPL, ID attendu, exécutable Mach-O, modes, liens internes et empreinte complète.
  Fichiers spéciaux et liens sortants refusés. `codesign --verify --deep --strict` pour
  les signatures présentes ; toute signature valide de bundle reste intacte.
- Exception limitée au nouveau build isolé : une signature ad hoc provisoire
  `linker-signed`, sans sceau de ressources, est contrôlée comme Mach-O autonome,
  puis le bundle reçoit son sceau ad hoc. Aucun `--deep` pour signer ; aucune signature
  imbriquée, signature Developer ID ou notarisation n'est remplacée.
- Reçus privés sous `~/Library/Application Support/ArN-LaB Local Installer` (dossier 700,
  fichiers 600). Ils enregistrent sources, dépendances et empreinte du résultat. Un reçu
  téléchargé ou le SHA livré avec un ZIP GitHub ne remplace pas un reçu local :
  sans correspondance, reconstruire depuis les sources. `--app` ne signe rien.
- Seul `com.apple.quarantine` peut être retiré, sur la copie préparée seulement, après
  provenance vérifiée et approbation distincte. Le bundle source garde ses attributs.
  Aucun retrait global, certificat, accès au trousseau ou réglage Gatekeeper.

## Installation, sauvegardes et concurrence

Copie `ditto`, verrou coopératif, contrôles répétés, journal et déplacement atomique
exclusif (`renamex_np`, `RENAME_EXCL`). L'ancienne version reste dans
`/Applications/.arnlab-local-installer/UUID/previous.app`. Une restauration contrôle
identités disque et empreintes ; les copies externes ou modifiées sont préservées.
Une ancienne application dont la signature est invalide reste refusée comme cible de
remplacement et comme restauration ; la cohabitation Zenith n'affaiblit pas ce contrôle.

`--list-backups` indique l'UUID, le chemin, la taille, la présence d'une ancienne version
et l'état de la transaction, y compris les préparations interrompues sans journal.
**Pas de purge automatique ni de suppression par le composant dans cette version** :
les sauvegardes peuvent occuper de l'espace ; les examiner avant tout nettoyage manuel.
La liste ne supprime ni ne crée de transaction et n'accepte pas de chemin de purge.

Les éditeurs/installeurs et lanceurs externes qui ignorent le verrou ne sont pas exclus
absolument. Les changements détectés conservent les copies ; une interruption laisse
journaux et sauvegardes pour récupération. Ne pas exécuter des mises à jour concurrentes.
Pas de `sudo`, élévation ou lancement automatique. Un `/Applications` non accessible
entraîne un arrêt. Les reçus n'isolent pas du code malveillant lancé sous le même compte.

Les systèmes de build peuvent télécharger leurs dépendances habituelles. Le moteur
ne contacte aucun service réseau. Vérifier les sources reste nécessaire : une origine
Git attendue n'est pas une attestation cryptographique d'auteur. `codesign` et Gatekeeper
ont des rôles distincts ; aucune garantie universelle de lancement sur un Mac administré.
Voir [Apple : signature](https://developer.apple.com/library/archive/documentation/Security/Conceptual/CodeSigningGuide/Procedures/Procedures.html)
et [notarisation](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution).

## Validation locale

```sh
python3 -B -m unittest discover -s developer-tools/macos-local-installer/tests -v
```

Tests hors réseau sur bundles Mach-O synthétiques et destinations temporaires :
installation, mise à jour, restauration, cohabitation avec un ancien bundle sans sceau,
Homebrew, processus actifs, commits de dépendances, filtres Python, quarantaine,
collisions, sauvegardes et pseudo-terminal. Aucune modification de `/Applications`.
