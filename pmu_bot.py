#!/usr/bin/env python3
"""
Bot PMU - Detecte les chevaux caches : cote elevee mais bonnes capacites reelles.
"""

import os
import re
import requests
from datetime import datetime

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

API_BASE = "https://offline.turfinfo.api.pmu.fr/rest/client/61/programme"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

MIN_SCORE = 55.0
MIN_COTE = 8.0
MAX_RESULTS = 8


def get_programme(date_str):
    url = API_BASE + "/" + date_str
    resp = requests.get(url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    return resp.json()


def get_participants(date_str, reunion_num, course_num):
    url = API_BASE + "/" + date_str + "/R" + str(reunion_num) + "/C" + str(course_num) + "/participants"
    resp = requests.get(url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    return resp.json()


def parse_musique(musique):
    if not musique:
        return 0.0, 0.0, 0

    positions = re.findall(r"(\d{1,2}|[ADRT])[a-zA-Z]", musique.upper())
    if not positions:
        return 0.0, 0.0, 0

    positions = positions[:10]
    total_poids = 0.0
    total_points = 0.0
    top3_count = 0

    for i, pos in enumerate(positions):
        poids = 1.0 / (1 + i * 0.25)
        total_poids += poids

        if pos in ("A", "D", "R", "T", "0"):
            points = 0.0
        else:
            p = int(pos)
            if p == 1:
                points = 100.0
            elif p == 2:
                points = 85.0
            elif p == 3:
                points = 70.0
            elif p <= 5:
                points = 50.0
            elif p <= 8:
                points = 30.0
            else:
                points = 10.0
            if p <= 3:
                top3_count += 1

        total_points += points * poids

    score_forme = total_points / total_poids if total_poids > 0 else 0.0
    taux_top3 = (top3_count / len(positions)) * 100 if positions else 0.0
    return score_forme, taux_top3, len(positions)


def compute_jockey_stats(all_participants):
    jockey_scores = {}
    for p in all_participants:
        jockey = p.get("driver") or p.get("jockey") or ""
        if not jockey:
            continue
        score_forme, _, nb = parse_musique(p.get("musique", ""))
        if nb == 0:
            continue
        jockey_scores.setdefault(jockey, []).append(score_forme)
    return {j: sum(v) / len(v) for j, v in jockey_scores.items() if v}


def get_cote(participant):
    rapports = participant.get("dernierRapportDirect") or {}
    cote = rapports.get("rapport")
    if cote:
        return float(cote)
    rapports_ref = participant.get("dernierRapportReference") or {}
    cote = rapports_ref.get("rapport")
    if cote:
        return float(cote)
    return None


def score_cheval(p, jockey_stats):
    score_forme, taux_top3, nb_courses = parse_musique(p.get("musique", ""))
    if nb_courses == 0:
        return None

    jockey = p.get("driver") or p.get("jockey") or ""
    score_jockey = jockey_stats.get(jockey, 50.0)

    gains = p.get("gainsParticipant") or {}
    gains_carriere = (gains.get("gainsCarriere", 0) or 0) / 100.0
    age = p.get("age") or 5
    gains_par_an = gains_carriere / max(age - 1, 1)
    score_gains = min((gains_par_an / 20000.0) * 100, 100.0)

    score_contexte = 50.0
    deferre = p.get("deferre")
    if deferre and deferre != "SANS":
        score_contexte += 25.0
    if p.get("oeilleres") and p.get("oeilleres") != "SANS":
        score_contexte += 15.0
    score_contexte = min(score_contexte, 100.0)

    score_total = (score_forme * 0.40 + taux_top3 * 0.20 + score_jockey * 0.20 + score_gains * 0.10 + score_contexte * 0.10)

    return {
        "score": score_total,
        "forme": score_forme,
        "top3": taux_top3,
        "nb_courses": nb_courses,
    }


def analyser_journee(date_str):
    programme = get_programme(date_str)
    reunions = programme.get("programme", {}).get("reunions", [])

    detections = []
    courses_analysees = 0

    for reunion in reunions:
        r_num = reunion.get("numOfficiel")
        hippodrome = (reunion.get("hippodrome") or {}).get("libelleCourt", "?")

        for course in reunion.get("courses", []):
            c_num = course.get("numOrdre")
            if not r_num or not c_num:
                continue

            try:
                data = get_participants(date_str, r_num, c_num)
            except Exception:
                continue

            participants = data.get("participants", [])
            if not participants:
                continue

            courses_analysees += 1
            jockey_stats = compute_jockey_stats(participants)

            for p in participants:
                if p.get("statut") and p.get("statut") != "PARTANT":
                    continue

                cote = get_cote(p)
                if cote is None or cote < MIN_COTE:
                    continue

                res = score_cheval(p, jockey_stats)
                if res is None or res["score"] < MIN_SCORE:
                    continue

                valeur = res["score"] * (cote ** 0.5) / 10.0

                detections.append({
                    "cheval": p.get("nom", "?"),
                    "numero": p.get("numPmu", "?"),
                    "cote": cote,
                    "score": res["score"],
                    "forme": res["forme"],
                    "top3": res["top3"],
                    "nb_courses": res["nb_courses"],
                    "jockey": p.get("driver") or p.get("jockey") or "?",
                    "reunion": r_num,
                    "course": c_num,
                    "hippodrome": hippodrome,
                    "heure": course.get("heureDepart"),
                    "valeur": valeur,
                })

    detections.sort(key=lambda x: x["valeur"], reverse=True)
    return detections[:MAX_RESULTS], courses_analysees


def format_message(detections, courses_analysees, date_str):
    if not detections:
        return ("Chevaux caches PMU - " + date_str + "\n\nAucune detection sur " + str(courses_analysees) + " courses analysees.")

    message = "Chevaux caches PMU - " + date_str + "\n"
    message += str(courses_analysees) + " courses analysees\n\n"

    for d in detections:
        heure = d["heure"]
        if heure:
            try:
                heure = datetime.fromtimestamp(heure / 1000).strftime("%H:%M")
            except Exception:
                heure = "?"
        else:
            heure = "?"

        message += "R" + str(d["reunion"]) + "C" + str(d["course"]) + " " + d["hippodrome"] + " (" + heure + ")\n"
        message += "  N" + str(d["numero"]) + " " + d["cheval"] + "\n"
        message += "  Cote: " + str(round(d["cote"], 1)) + " | Score: " + str(round(d["score"])) + "/100\n"
        message += "  Forme: " + str(round(d["forme"])) + " | Top3: " + str(round(d["top3"])) + "% (" + str(d["nb_courses"]) + " courses)\n"
        message += "  Jockey: " + d["jockey"] + "\n\n"

    message += "Analyse statistique, pas une garantie de gain."
    return message


def send_telegram_message(text):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    resp = requests.post(url, data=payload, timeout=25)
    resp.raise_for_status()
    return resp.json()


def main():
    date_str = datetime.now().strftime("%d%m%Y")
    print("Analyse du programme PMU pour le " + date_str)

    try:
        detections, courses_analysees = analyser_journee(date_str)
        print("Courses analysees: " + str(courses_analysees))
        print("Detections: " + str(len(detections)))
    except Exception as e:
        print("Erreur lors de l'analyse:")
        print(str(e))
        send_telegram_message("Bot PMU: erreur lors de l'analyse du " + date_str + "\n" + str(e))
        return

    message = format_message(detections, courses_analysees, date_str)
    result = send_telegram_message(message)
    print("Message envoye:")
    print(result.get("ok"))


if __name__ == "__main__":
    main()
