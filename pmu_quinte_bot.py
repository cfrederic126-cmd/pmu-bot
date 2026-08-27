"""
Bot d'analyse Quinté+ PMU
=========================

Récupère les partants d'une course PMU (Quinté+), calcule un score composite
par cheval en croisant forme, jockey, cotes (gagnant + placé + tendance),
et poste sur Telegram :
  - Le Top 5 des chevaux les "plus prêts à gagner"
  - Les "chevaux cachés" (score élevé mais cote élevée => valeur potentielle)

Prévu pour tourner via GitHub Actions (cron), sur le même principe que les
bots EuroDreams / EuroMillions déjà en place.

⚠️ Ceci s'appuie sur l'API non officielle du PMU (offline.turfinfo.api.pmu.fr).
Cette API n'est pas documentée publiquement : son format peut changer sans
préavis. Le script est écrit pour échouer proprement (et alerter) plutôt que
planter silencieusement si la structure change.

⚠️ Ceci est un outil d'aide à la décision statistique, pas une martingale.
Les paris hippiques comportent des risques. Aucune garantie de gain.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pmu_quinte_bot")

PMU_BASE_URL = "https://offline.turfinfo.api.pmu.fr/rest/client/7"
PMU_PERF_BASE_URL = "https://online.turfinfo.api.pmu.fr/rest/client/61"
REQUEST_TIMEOUT = 15

import re


def normalize_jockey_name(name: str) -> str:
    """
    Normalise un nom de jockey/driver pour comparaison entre les deux formats
    utilisés par l'API PMU : "S.RUIS" (endpoint participants, champ 'driver')
    vs "M. BOUCHEZ" (endpoint performances-detaillees, champ 'nomJockey').
    On ne garde que les lettres en majuscule.
    """
    if not name:
        return ""
    return re.sub(r"[^A-ZÀ-Ý]", "", name.upper())

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Pondération du score composite — ajustable facilement ici
# ---------------------------------------------------------------------------
WEIGHTS = {
    "forme": 0.30,       # musique / forme récente
    "jockey": 0.25,      # stats du jockey
    "aptitude": 0.20,    # distance / terrain
    "poids": 0.10,       # charge portée (poids relatif dans la course)
    "cote": 0.15,        # signal de marché (gagnant + placé + tendance)
}


# ---------------------------------------------------------------------------
# Modèle de données
# ---------------------------------------------------------------------------
@dataclass
class Cheval:
    numero: int
    nom: str
    jockey: str
    poids: Optional[float] = None
    musique: str = ""
    cote_gagnant: Optional[float] = None
    cote_placee: Optional[float] = None
    cote_gagnant_veille: Optional[float] = None  # pour calculer la tendance
    non_partant: bool = False

    score_forme: float = 0.0
    score_jockey: float = 0.0
    score_aptitude: float = 0.0
    score_poids: float = 0.0
    score_cote: float = 0.0
    score_total: float = 0.0

    tendance: str = "="  # "baisse", "hausse", "="
    ecart_gagnant_place: float = 0.0


# ---------------------------------------------------------------------------
# Récupération des données PMU
# ---------------------------------------------------------------------------
def _get_json(url: str) -> Optional[dict]:
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            "User-Agent": "Mozilla/5.0 (compatible; pmu-quinte-bot/1.0)"
        })
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error("Échec requête %s : %s", url, e)
        return None
    except json.JSONDecodeError as e:
        log.error("Réponse non-JSON depuis %s : %s", url, e)
        return None


def get_participants(date_str: str, reunion: str, course: str) -> Optional[dict]:
    """date_str format attendu : JJMMAAAA"""
    url = f"{PMU_BASE_URL}/programme/{date_str}/{reunion}/{course}/participants"
    return _get_json(url)


def get_combinaisons_rapports(date_str: str, reunion: str, course: str) -> Optional[dict]:
    """Cotes détaillées (gagnant/placé) — endpoint séparé selon les cas."""
    url = f"{PMU_BASE_URL}/programme/{date_str}/{reunion}/{course}/rapports-definitifs"
    return _get_json(url)


def get_performances_detaillees(date_str: str, reunion: str, course: str) -> Optional[dict]:
    """
    Historique des ~5 dernières courses de chaque partant, avec jockey/place
    à chaque course (y compris pour les autres chevaux du peloton de l'époque).
    Sert de base pour estimer la forme récente des jockeys, faute de flux
    dédié aux stats jockey dans l'API PMU.
    """
    url = f"{PMU_PERF_BASE_URL}/programme/{date_str}/{reunion}/{course}/performances-detaillees"
    return _get_json(url)


def build_jockey_stats(perf_data: Optional[dict]) -> dict[str, dict]:
    """
    Agrège, à partir des performances détaillées, un taux de victoire/place
    par jockey sur l'échantillon de courses passées référencées. Un même
    jockey peut apparaître plusieurs fois (sur des chevaux différents) :
    on cumule.
    """
    stats: dict[str, dict] = {}
    if not perf_data:
        return stats
    try:
        for participant in perf_data.get("participants", []):
            for course_passee in participant.get("coursesCourues", []):
                for p in course_passee.get("participants", []):
                    nom_jockey = p.get("nomJockey")
                    if not nom_jockey:
                        continue
                    key = normalize_jockey_name(nom_jockey)
                    if not key:
                        continue
                    entry = stats.setdefault(key, {"courses": 0, "victoires": 0, "places": 0})
                    entry["courses"] += 1
                    place = (p.get("place") or {}).get("place")
                    if place == 1:
                        entry["victoires"] += 1
                    if place is not None and place <= 3:
                        entry["places"] += 1
    except Exception as e:
        log.warning("Erreur agrégation stats jockey : %s", e)
    return stats


def get_distance_course(date_str: str, reunion: str, course: str) -> Optional[int]:
    """Récupère la distance (en mètres) de la course ciblée depuis le programme du jour."""
    url = f"{PMU_BASE_URL}/programme/{date_str}"
    programme = _get_json(url)
    if not programme:
        return None
    try:
        num_reunion = int(reunion.lstrip("R"))
        num_course = int(course.lstrip("C"))
        for r in programme.get("programme", {}).get("reunions", []):
            if r.get("numOfficiel") != num_reunion:
                continue
            for c in r.get("courses", []):
                if c.get("numOrdre") == num_course:
                    return c.get("distance")
    except (KeyError, TypeError, ValueError) as e:
        log.warning("Impossible de déterminer la distance de la course : %s", e)
    return None


def build_perf_by_numpmu(perf_data: Optional[dict]) -> dict[int, dict]:
    """Indexe les performances détaillées par numéro PMU du cheval pour lookup rapide."""
    result: dict[int, dict] = {}
    if not perf_data:
        return result
    for p in perf_data.get("participants", []):
        num = p.get("numPmu")
        if num is not None:
            result[num] = p
    return result


def find_quinte_du_jour(date_str: str) -> Optional[tuple[str, str]]:
    """
    Cherche automatiquement la course Quinté+ du jour dans le programme.
    Signal fiable (vérifié sur données réelles de l'API) : la course concernée
    liste un pari avec codePari == "QUINTE_PLUS" dans son tableau "paris".
    """
    url = f"{PMU_BASE_URL}/programme/{date_str}"
    programme = _get_json(url)
    if not programme:
        return None
    try:
        for reunion in programme.get("programme", {}).get("reunions", []):
            for course in reunion.get("courses", []):
                for pari in course.get("paris", []):
                    if pari.get("codePari") == "QUINTE_PLUS":
                        return f"R{reunion['numOfficiel']}", f"C{course['numOrdre']}"
    except (KeyError, TypeError) as e:
        log.error("Structure programme inattendue : %s", e)
    return None


# ---------------------------------------------------------------------------
# Parsing des participants + cotes
# ---------------------------------------------------------------------------
def parse_participants(data: dict) -> list[Cheval]:
    chevaux = []
    for p in data.get("participants", []):
        try:
            cheval = Cheval(
                numero=p.get("numPmu"),
                nom=p.get("nom", "?"),
                jockey=p.get("driver") or p.get("jockey", "?"),
                poids=p.get("poidsConditionMonte") or p.get("handicapPoids"),
                musique=p.get("musique", ""),
                non_partant=p.get("statut") == "NON_PARTANT",
            )
            # Cotes : le champ exact varie selon les réunions (galop/trot),
            # on tente plusieurs clés connues de l'API PMU
            dr = p.get("dernierRapportDirect", {}) or {}
            cheval.cote_gagnant = dr.get("rapport")
            dref = p.get("dernierRapportReference", {}) or {}
            cheval.cote_gagnant_veille = dref.get("rapport")

            chevaux.append(cheval)
        except Exception as e:
            log.warning("Erreur parsing participant %s : %s", p.get("numPmu"), e)
    return chevaux


def enrich_cotes_placees(chevaux: list[Cheval], rapports: Optional[dict]) -> None:
    """Ajoute la cote placé si l'endpoint rapports-definitifs est disponible."""
    if not rapports:
        return
    try:
        for r in rapports.get("rapports", []):
            if r.get("typePari") == "E_SIMPLE_PLACE":
                for combi in r.get("rapports", []):
                    num = combi.get("combinaison", [None])[0]
                    for c in chevaux:
                        if c.numero == num:
                            c.cote_placee = combi.get("dividendePourUnEuro")
    except Exception as e:
        log.warning("Impossible d'extraire les cotes placées : %s", e)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_forme(musique: str) -> float:
    """
    Score 0-100 basé sur la musique (ex: '1a2a3a4a0a').
    Les places récentes pèsent plus que les anciennes (pondération dégressive).
    """
    if not musique:
        return 0.0
    positions = []
    i = 0
    while i < len(musique) and len(positions) < 5:
        ch = musique[i]
        if ch.isdigit():
            positions.append(int(ch))
        i += 1

    if not positions:
        return 0.0

    total, poids_cumule = 0.0, 0.0
    for idx, place in enumerate(positions):
        poids = 1.0 / (idx + 1)  # la plus récente course pèse le plus
        # 0 = disqualifié/incident -> pénalité forte ; 1 = victoire -> max
        points = 100 if place == 1 else max(0, 100 - (place - 1) * 18) if place > 0 else 5
        total += points * poids
        poids_cumule += poids

    return round(total / poids_cumule, 1) if poids_cumule else 0.0


MIN_COURSES_JOCKEY_FIABLE = 5  # en dessous, échantillon jugé trop faible


def score_jockey(nom_jockey: str, jockey_stats: dict[str, dict]) -> float:
    """
    Score 0-100 basé sur le taux de victoire et de place du jockey, mesurés
    sur l'échantillon de courses passées agrégé via performances-detaillees.
    Retourne un score neutre (50) si le jockey est inconnu ou l'échantillon
    trop faible, pour ne pas fausser le classement sur une donnée peu fiable.
    """
    key = normalize_jockey_name(nom_jockey)
    entry = jockey_stats.get(key)
    if not entry or entry["courses"] < MIN_COURSES_JOCKEY_FIABLE:
        return 50.0

    taux_victoire = entry["victoires"] / entry["courses"]
    taux_place = entry["places"] / entry["courses"]

    # Victoire pondérée plus fort que la simple place ; plafonné à 100.
    score = taux_victoire * 130 + taux_place * 40
    return round(max(0.0, min(100.0, score)), 1)


DISTANCE_TOLERANCE_M = 400  # au-delà, la performance passée compte peu pour l'aptitude


def score_aptitude(perf_entry: Optional[dict], distance_cible: Optional[int]) -> float:
    """
    Score 0-100 basé sur les performances passées du cheval à des distances
    proches de celle de la course du jour. Pondère chaque course passée par
    la proximité de distance (plus l'écart est petit, plus ça compte) et par
    la place obtenue. Retourne un score neutre (50) si l'historique ou la
    distance cible sont indisponibles.
    """
    if not perf_entry or not distance_cible:
        return 50.0

    total, poids_cumule = 0.0, 0.0
    for course_passee in perf_entry.get("coursesCourues", []):
        distance_passee = course_passee.get("distance")
        if not distance_passee:
            continue
        ecart = abs(distance_passee - distance_cible)
        if ecart > DISTANCE_TOLERANCE_M:
            continue
        poids = 1.0 - (ecart / DISTANCE_TOLERANCE_M)  # 1.0 si distance identique, ->0 à la tolérance

        # Retrouver la performance du cheval lui-même dans cette course passée
        for p in course_passee.get("participants", []):
            if p.get("itsHim"):
                place = (p.get("place") or {}).get("place")
                statut = (p.get("place") or {}).get("statusArrivee")
                if place == 1:
                    points = 100
                elif place is not None and place <= 5:
                    points = max(0, 100 - (place - 1) * 18)
                elif statut == "DISQUALIFIE":
                    points = 10
                else:
                    points = 25  # non placé mais a terminé
                total += points * poids
                poids_cumule += poids
                break

    if poids_cumule == 0:
        return 50.0  # aucune course passée à une distance comparable
    return round(total / poids_cumule, 1)
    if poids is None or not poids_moyen_course:
        return 50.0
    ecart = poids_moyen_course - poids  # moins de poids porté = mieux
    return max(0.0, min(100.0, 50 + ecart * 5))


def score_cote(cheval: Cheval, cotes_gagnant_tries: list[float]) -> float:
    """
    Combine cote gagnant (rang dans la course), écart gagnant/placé,
    et tendance (mouvement de cote).
    """
    if cheval.cote_gagnant is None or not cotes_gagnant_tries:
        return 50.0

    rang = cotes_gagnant_tries.index(cheval.cote_gagnant) + 1
    score_rang = max(0.0, 100 - (rang - 1) * 8)

    # Tendance : si la cote a baissé depuis la veille -> signal positif
    bonus_tendance = 0.0
    if cheval.cote_gagnant_veille and cheval.cote_gagnant_veille > 0:
        variation = (cheval.cote_gagnant_veille - cheval.cote_gagnant) / cheval.cote_gagnant_veille
        if variation > 0.15:
            cheval.tendance = "baisse"
            bonus_tendance = 10
        elif variation < -0.15:
            cheval.tendance = "hausse"
            bonus_tendance = -5

    # Écart cote gagnant / placé : un écart faible = solidité (outsider fiable)
    bonus_ecart = 0.0
    if cheval.cote_placee and cheval.cote_gagnant:
        cheval.ecart_gagnant_place = cheval.cote_gagnant - cheval.cote_placee
        if cheval.cote_placee < cheval.cote_gagnant * 0.35:
            bonus_ecart = 8  # cote placé nettement plus basse -> régularité

    return max(0.0, min(100.0, score_rang + bonus_tendance + bonus_ecart))


def compute_scores(
    chevaux: list[Cheval],
    jockey_stats: Optional[dict] = None,
    perf_by_numpmu: Optional[dict] = None,
    distance_cible: Optional[int] = None,
) -> None:
    jockey_stats = jockey_stats or {}
    perf_by_numpmu = perf_by_numpmu or {}
    partants = [c for c in chevaux if not c.non_partant]
    cotes_triees = sorted([c.cote_gagnant for c in partants if c.cote_gagnant])
    poids_valides = [c.poids for c in partants if c.poids]
    poids_moyen = sum(poids_valides) / len(poids_valides) if poids_valides else None

    for c in partants:
        c.score_forme = score_forme(c.musique)
        c.score_jockey = score_jockey(c.jockey, jockey_stats)
        c.score_aptitude = score_aptitude(perf_by_numpmu.get(c.numero), distance_cible)
        c.score_poids = score_poids(c.poids, poids_moyen)
        c.score_cote = score_cote(c, cotes_triees)

        c.score_total = round(
            c.score_forme * WEIGHTS["forme"]
            + c.score_jockey * WEIGHTS["jockey"]
            + c.score_aptitude * WEIGHTS["aptitude"]
            + c.score_poids * WEIGHTS["poids"]
            + c.score_cote * WEIGHTS["cote"],
            1,
        )


# ---------------------------------------------------------------------------
# Sélection Top 5 + chevaux cachés
# ---------------------------------------------------------------------------
def top5(chevaux: list[Cheval]) -> list[Cheval]:
    partants = [c for c in chevaux if not c.non_partant]
    return sorted(partants, key=lambda c: c.score_total, reverse=True)[:5]


def chevaux_caches(chevaux: list[Cheval], top: list[Cheval]) -> list[Cheval]:
    """Score élevé (>60) mais cote gagnant élevée (>10) = valeur potentielle."""
    top_numeros = {c.numero for c in top}
    candidats = [
        c for c in chevaux
        if not c.non_partant
        and c.numero not in top_numeros
        and c.score_total >= 60
        and c.cote_gagnant and c.cote_gagnant >= 10
    ]
    return sorted(candidats, key=lambda c: c.score_total, reverse=True)[:3]


# ---------------------------------------------------------------------------
# Formatage + envoi Telegram
# ---------------------------------------------------------------------------
def format_message(date_str: str, reunion: str, course: str, top: list[Cheval], caches: list[Cheval]) -> str:
    lignes = [f"🐎 *Analyse Quinté+ — {date_str[:2]}/{date_str[2:4]}/{date_str[4:]} — {reunion}{course}*", ""]
    lignes.append("*Top 5 du modèle :*")
    for i, c in enumerate(top, 1):
        cote_txt = f"{c.cote_gagnant:.1f}" if c.cote_gagnant else "n/a"
        tendance_emoji = {"baisse": "📉", "hausse": "📈", "=": "➖"}[c.tendance]
        lignes.append(
            f"{i}. N°{c.numero} {c.nom} — score {c.score_total}/100 — "
            f"jockey {c.jockey} — cote {cote_txt} {tendance_emoji}"
        )

    if caches:
        lignes.append("")
        lignes.append("*Chevaux à cote intéressante (valeur potentielle) :*")
        for c in caches:
            lignes.append(f"• N°{c.numero} {c.nom} — score {c.score_total}/100 — cote {c.cote_gagnant:.1f}")

    lignes.append("")
    lignes.append("_Analyse statistique, ne constitue pas un conseil de pari. Jouer comporte des risques._")
    return "\n".join(lignes)


def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant dans l'environnement.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Échec envoi Telegram : %s", e)
        return False


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def main() -> int:
    today = dt.date.today()
    date_str = today.strftime("%d%m%Y")

    reunion_arg = os.environ.get("PMU_REUNION")
    course_arg = os.environ.get("PMU_COURSE")

    if reunion_arg and course_arg:
        reunion, course = reunion_arg, course_arg
    else:
        found = find_quinte_du_jour(date_str)
        if not found:
            log.error("Impossible de trouver le Quinté+ du jour automatiquement. "
                      "Définis PMU_REUNION / PMU_COURSE manuellement (ex: R1 / C4).")
            return 1
        reunion, course = found
        log.info("Quinté+ détecté automatiquement : %s%s", reunion, course)

    data = get_participants(date_str, reunion, course)
    if not data:
        log.error("Échec de récupération des participants.")
        return 1

    chevaux = parse_participants(data)
    if not chevaux:
        log.error("Aucun participant trouvé — vérifie la structure de l'API PMU (elle a peut-être changé).")
        return 1

    rapports = get_combinaisons_rapports(date_str, reunion, course)
    enrich_cotes_placees(chevaux, rapports)

    perf_data = get_performances_detaillees(date_str, reunion, course)
    jockey_stats = build_jockey_stats(perf_data)
    if not jockey_stats:
        log.warning("Stats jockey indisponibles ou vides — scores jockey neutres (50/100) pour cette course.")

    perf_by_numpmu = build_perf_by_numpmu(perf_data)
    distance_cible = get_distance_course(date_str, reunion, course)
    if not distance_cible:
        log.warning("Distance de la course indisponible — scores aptitude neutres (50/100).")

    compute_scores(chevaux, jockey_stats, perf_by_numpmu, distance_cible)
    top = top5(chevaux)
    caches = chevaux_caches(chevaux, top)

    message = format_message(date_str, reunion, course, top, caches)
    log.info("\n%s", message)

    if os.environ.get("DRY_RUN") == "1":
        log.info("DRY_RUN=1 — message non envoyé sur Telegram.")
        return 0

    ok = send_telegram(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
