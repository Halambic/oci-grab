#!/home/Mahion/oci-grab/venv/bin/python3
"""Boucle sur LaunchInstance jusqu'à décrocher une instance ARM Always Free.

Usage :
    ./grab.py --check     # valide la config, liste AD/images/subnets, ne lance rien
    ./grab.py             # boucle jusqu'au succès
    ./grab.py --once      # une seule tentative (debug)
"""

import argparse
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import oci

# ─── Paramètres (surchargeables par variables d'environnement) ─────────────────
# Les deux shapes Always Free, essayés en alternance : on prend la première capacité
# qui se libère, quelle qu'elle soit. L'ARM est plus confortable, l'AMD moins convoitée.
SHAPES = [s.strip() for s in os.environ.get(
    "GRAB_SHAPES", "VM.Standard.A1.Flex,VM.Standard.E2.1.Micro").split(",") if s.strip()]
OCPUS = float(os.environ.get("GRAB_OCPUS", "2"))          # free tier réduit à 2 depuis juin 2026
MEMORY_GB = float(os.environ.get("GRAB_MEMORY_GB", "12"))  # idem : 12 Go max (shapes Flex only)
# 200 Go de stockage gratuit dans le tenancy. Le disque est le facteur limitant du
# projet (fenêtre glissante de VOD), donc on en réclame l'essentiel dès la création :
# 150 Go ≈ 55 h de programme à 6 Mbps, ou 95 h à 3,5 Mbps.
BOOT_DEFAUT = {"VM.Standard.A1.Flex": 150, "VM.Standard.E2.1.Micro": 150}
DISPLAY_NAME = os.environ.get("GRAB_NAME", "patreon-twitch-playout")
OS_NAME = os.environ.get("GRAB_OS", "Canonical Ubuntu")
OS_VERSION = os.environ.get("GRAB_OS_VERSION", "24.04")
SSH_PUBKEY = os.path.expanduser(os.environ.get("GRAB_SSH_PUBKEY", "~/.ssh/oracle_arm.pub"))
INTERVAL = int(os.environ.get("GRAB_INTERVAL", "90"))      # secondes entre deux tentatives
STATE_DIR = os.path.expanduser("~/oci-grab")

# Erreurs "normales" : la capacité manque, on réessaie
CAPACITY_MARKERS = ("out of host capacity", "out of capacity")
# Erreurs définitives : réessayer ne servira jamais à rien
FATAL_CODES = {
    "LimitExceeded",          # quota Always Free déjà consommé
    "NotAuthenticated",       # clé API / fingerprint faux
    "NotAuthorizedOrNotFound",
    "InvalidParameter",
    "CannotParseRequest",
    "QuotaExceeded",
}


def log(msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    with open(os.path.join(STATE_DIR, "grab.log"), "a") as fh:
        fh.write(line + "\n")


def notify(title, body):
    """Notification bureau + bip, pour être prévenu même sans regarder le terminal."""
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-u", "critical", "-a", "oci-grab", title, body],
            check=False,
        )
    print("\a", end="", flush=True)


def load_clients():
    cfg = oci.config.from_file()  # ~/.oci/config, profil DEFAULT
    placeholders = [k for k, v in cfg.items() if isinstance(v, str) and "REMPLACER" in v]
    if placeholders:
        sys.exit(
            "✗ ~/.oci/config contient encore des placeholders : "
            + ", ".join(placeholders)
            + "\n  Remplis-les avec les valeurs de la console Oracle, puis relance --check."
        )
    oci.config.validate_config(cfg)
    return (
        cfg,
        oci.identity.IdentityClient(cfg),
        oci.core.ComputeClient(cfg),
        oci.core.VirtualNetworkClient(cfg),
    )


def pick_image(compute, compartment, shape):
    """Dernière image compatible avec le shape demandé (l'archi suit le shape)."""
    images = compute.list_images(
        compartment_id=compartment,
        operating_system=OS_NAME,
        operating_system_version=OS_VERSION,
        shape=shape,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        # Repli : n'importe quelle image compatible avec le shape
        images = compute.list_images(
            compartment_id=compartment, shape=shape,
            sort_by="TIMECREATED", sort_order="DESC",
        ).data
    if not images:
        sys.exit(f"✗ Aucune image compatible avec {shape} trouvée.")
    return images[0]


def pick_subnet(network, compartment):
    """Subnet public existant. Oracle en crée un avec l'assistant 'VCN with Internet Connectivity'."""
    subnets = [
        s for s in network.list_subnets(compartment_id=compartment).data
        if s.lifecycle_state == "AVAILABLE"
    ]
    if not subnets:
        sys.exit(
            "✗ Aucun subnet dans ce compartiment.\n"
            "  Console Oracle → Networking → Virtual Cloud Networks → Start VCN Wizard\n"
            "  → « VCN with Internet Connectivity » (tout par défaut), puis relance --check."
        )
    public = [s for s in subnets if not s.prohibit_public_ip_on_vnic]
    if not public:
        log("⚠ Aucun subnet public : l'instance n'aura pas d'IP publique (pas de RTMP sortant direct).")
    return (public or subnets)[0]


def targets(identity, tenancy, compartment):
    """Combinaisons (shape, AD, fault domain), essayées en rotation.

    L'ordre alterne les shapes plutôt que d'épuiser l'ARM avant d'essayer l'AMD :
    à 90 s par tentative, on veut goûter aux deux le plus tôt possible.
    """
    ads = identity.list_availability_domains(compartment_id=tenancy).data
    lieux = []
    for ad in ads:
        # list_fault_domains est exposé par le client Identity, pas Compute
        fds = identity.list_fault_domains(
            availability_domain=ad.name, compartment_id=compartment
        ).data
        if fds:
            lieux.extend((ad.name, fd.name) for fd in fds)
        else:
            lieux.append((ad.name, None))
    return [(shape, ad, fd) for ad, fd in lieux for shape in SHAPES]


def boot_gb(shape):
    forced = os.environ.get("GRAB_BOOT_GB")
    return int(forced) if forced else BOOT_DEFAUT.get(shape, 50)


def launch(compute, compartment, shape, ad, fd, image_id, subnet_id, ssh_key):
    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=compartment,
        availability_domain=ad,
        fault_domain=fd,
        shape=shape,
        display_name=DISPLAY_NAME,
        # shape_config n'existe que pour les shapes « Flex » ; l'envoyer sur un shape
        # fixe (E2.1.Micro) fait échouer la requête.
        shape_config=(
            oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=OCPUS, memory_in_gbs=MEMORY_GB
            )
            if shape.endswith(".Flex")
            else None
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id, boot_volume_size_in_gbs=boot_gb(shape)
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id, assign_public_ip=True
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )
    return compute.launch_instance(details).data


def report_success(cfg, compute, network, instance, shape):
    log(f"✔ INSTANCE OBTENUE ({shape}) : {instance.id}")
    log("  Attente du passage en RUNNING…")
    instance = oci.wait_until(
        compute, compute.get_instance(instance.id), "lifecycle_state", "RUNNING",
        max_wait_seconds=900,
    ).data
    ip = None
    for att in compute.list_vnic_attachments(
        compartment_id=instance.compartment_id, instance_id=instance.id
    ).data:
        vnic = network.get_vnic(att.vnic_id).data
        ip = vnic.public_ip or vnic.private_ip
        if vnic.public_ip:
            break
    info = {
        "obtenu_le": datetime.now(timezone.utc).isoformat(),
        "instance_id": instance.id,
        "shape": shape,
        "region": cfg["region"], "ip": ip,
        "ssh": f"ssh -i ~/.ssh/oracle_arm ubuntu@{ip}" if ip else None,
    }
    if shape.endswith(".Flex"):
        info.update(ocpus=OCPUS, memoire_gb=MEMORY_GB)
    path = os.path.join(STATE_DIR, "instance.json")
    with open(path, "w") as fh:
        json.dump(info, fh, indent=2)
    log(f"  IP : {ip}")
    log(f"  Connexion : ssh -i ~/.ssh/oracle_arm ubuntu@{ip}")
    log(f"  Détails écrits dans {path}")
    notify(f"Instance Oracle obtenue 🎉 ({shape})",
           f"{ip} — ssh -i ~/.ssh/oracle_arm ubuntu@{ip}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="valide la config sans rien lancer")
    ap.add_argument("--once", action="store_true", help="une seule tentative")
    ap.add_argument("--sweep", action="store_true",
                    help="une passe sur toutes les cibles puis sortie (code 2 si rien) — pour un cron")
    args = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(SSH_PUBKEY):
        sys.exit(f"✗ Clé publique SSH absente : {SSH_PUBKEY}")
    ssh_key = open(SSH_PUBKEY).read().strip()

    cfg, identity, compute, network = load_clients()
    tenancy = cfg["tenancy"]
    compartment = os.environ.get("GRAB_COMPARTMENT", tenancy)  # racine par défaut

    # Garde-fou : ne jamais créer une 2ᵉ instance (le quota d'essai le permettrait,
    # mais elle serait hors Always Free donc facturable à la fin des 30 jours).
    already = os.path.exists(os.path.join(STATE_DIR, "instance.json"))
    live = [
        i for i in compute.list_instances(compartment_id=compartment).data
        if i.display_name == DISPLAY_NAME and i.lifecycle_state != "TERMINATED"
    ]
    if (already or live) and not args.check:
        for i in live:
            log(f"Instance déjà existante : {i.id} ({i.lifecycle_state})")
        if already and not live:
            log("instance.json existe déjà — supprime-le si tu veux vraiment relancer.")
        log("Rien à faire, arrêt.")
        return 0

    images = {shape: pick_image(compute, compartment, shape) for shape in SHAPES}
    subnet = pick_subnet(network, compartment)
    combos = targets(identity, tenancy, compartment)

    log(f"Région     : {cfg['region']}")
    for shape in SHAPES:
        taille = f"{OCPUS} OCPU / {MEMORY_GB} Go" if shape.endswith(".Flex") else "taille fixe"
        log(f"Shape      : {shape}  {taille} / boot {boot_gb(shape)} Go"
            f"  [{images[shape].display_name}]")
    log(f"Subnet     : {subnet.display_name} ({'public' if not subnet.prohibit_public_ip_on_vnic else 'privé'})")
    log(f"Cibles     : {len(combos)} combinaisons (shape × AD × fault domain)")

    if args.check:
        try:  # info bonus : quota Always Free restant
            avail = oci.limits.LimitsClient(cfg).get_resource_availability(
                service_name="compute", limit_name="standard-a1-core-count",
                compartment_id=tenancy, availability_domain=combos[0][1],
            ).data
            log(f"Quota A1   : {avail.used or 0} cœurs utilisés, {avail.available} disponibles")
        except Exception as exc:
            log(f"Quota A1   : non lisible ({type(exc).__name__})")
        log("✔ Config valide. Lance ./grab.py (sans --check) pour démarrer la boucle.")
        return

    def attempt(shape, ad, fd, n):
        """0 = instance obtenue, 1 = erreur définitive, None = réessayable."""
        court = shape.split(".")[-1]  # « Flex » ou « Micro »
        try:
            inst = launch(compute, compartment, shape, ad, fd,
                          images[shape].id, subnet.id, ssh_key)
        except oci.exceptions.ServiceError as exc:
            msg = (exc.message or "").lower()
            short_ad = ad.split(":")[-1]
            if any(m in msg for m in CAPACITY_MARKERS):
                log(f"#{n} {court} {short_ad}/{fd or '-'} : pas de capacité")
            elif exc.status == 429 or exc.code == "TooManyRequests":
                if args.sweep:  # en CI, dormir 5 min ne sert qu'à brûler des minutes
                    log(f"#{n} throttling OCI — passe interrompue, retour au prochain cron")
                    return 2
                log(f"#{n} throttling OCI — pause 5 min")
                time.sleep(300)
            elif exc.code in FATAL_CODES:
                log(f"✗ Erreur définitive {exc.status} {exc.code} : {exc.message}")
                notify("oci-grab arrêté", f"{exc.code} : {exc.message}")
                return 1
            else:
                log(f"#{n} erreur {exc.status} {exc.code} : {exc.message}")
            return None
        except Exception as exc:  # réseau coupé, DNS, etc.
            log(f"#{n} erreur locale {type(exc).__name__} : {exc}")
            return None
        report_success(cfg, compute, network, inst, shape)
        return 0

    if args.sweep:  # mode CI : une passe sur toutes les cibles, puis on rend la main
        for n, (shape, ad, fd) in enumerate(combos, 1):
            if n > 1:
                time.sleep(INTERVAL)  # 20 s suffisait à se faire throttler
            res = attempt(shape, ad, fd, n)
            if res is not None:
                return res
        log("Aucune capacité sur les cibles — nouvelle passe au prochain déclenchement.")
        return 2

    log(f"Boucle démarrée — une tentative toutes les ~{INTERVAL}s. Ctrl-C pour arrêter.")
    for n, (shape, ad, fd) in enumerate(itertools.cycle(combos), 1):
        res = attempt(shape, ad, fd, n)
        if res is not None:
            return res
        if args.once:
            log("--once : arrêt après une tentative.")
            return 2
        time.sleep(INTERVAL + random.uniform(0, INTERVAL * 0.3))


if __name__ == "__main__":
    sys.exit(main() or 0)
