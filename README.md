# oci-grab

Obtenir une instance Oracle Cloud **Always Free** dans une région où la capacité est
chroniquement épuisée, sans laisser un PC allumé pour ça.

Le script boucle sur `LaunchInstance` en alternant les deux shapes gratuits et toutes
les combinaisons de domaines de disponibilité et de fault domains. Il encaisse
l'erreur « Out of host capacity » comme un événement normal, temporise sur le
throttling d'OCI, et s'arrête net sur une erreur définitive (quota dépassé,
authentification cassée) plutôt que de boucler des jours pour rien.

## Utilisation

```bash
./grab.py --check    # valide la config, liste shapes/images/subnet, ne lance rien
./grab.py            # boucle jusqu'au succès
./grab.py --sweep    # une passe sur toutes les cibles puis sortie — pour un cron
```

Configuration par variables d'environnement : `GRAB_SHAPES`, `GRAB_OCPUS`,
`GRAB_MEMORY_GB`, `GRAB_BOOT_GB`, `GRAB_INTERVAL`, `GRAB_NAME`, `GRAB_COMPARTMENT`.

Au succès : `instance.json` (IP, commande SSH), notification bureau, et arrêt.
Un garde-fou vérifie qu'aucune instance du même nom n'existe déjà avant de lancer —
une deuxième instance sortirait de l'Always Free et deviendrait facturable.

## En continu, sans PC allumé

- `oci-grab.service` : unit systemd utilisateur, pour la machine locale.
- `.github/workflows/grab.yml` : une passe toutes les 15 minutes chez GitHub. Le run
  reste vert quand il n'y a pas de capacité, échoue seulement sur erreur réelle, et
  ouvre un ticket quand l'instance est enfin obtenue.

Secrets attendus par le workflow : `OCI_USER`, `OCI_TENANCY`, `OCI_FINGERPRINT`,
`OCI_REGION`, `OCI_KEY` (clé privée PEM), `SSH_PUBKEY`.

## Prérequis

Un `~/.oci/config` valide et le SDK Python : `pip install oci`.
