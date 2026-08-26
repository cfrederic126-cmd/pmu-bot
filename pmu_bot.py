#!/usr/bin/env python3
"""
Bot PMU - Selectionne les meilleurs chevaux du jour en privilegiant ceux qui
sont des adeptes de la distance et du terrain de la course.
"""

import os
import re
import requests
from datetime import datetime

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

API_OFFLINE = "https://offline.turfinfo.api.pmu.fr/rest/client/61/programme"
API_ONLINE = "https://online.turfinfo.api.pmu.fr/rest/client/61/programme"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

TOP_PAR_COURSE = 3
MAX_SELECTIONS = 8
TOLERANCE_DISTANCE = 200


def get_json(url):
    resp = requests.get(url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    return resp.json()


def get_programme(date_str):
    return get_json(API_OFFLINE + "/" + date_str)


def get_participants(date_str, r, c):
    url = API_OFFLINE + "/" + date_str + "/R" + str(r) + "/C" + str(c) + "/participants"
    return get_json(url)


def get_performances(date_str, r, c):
    url = (API_ONLINE + "/" + date_str + "/R" + str(r) + "/C" + str(c)
           + "/performances-detaillees/pretty")
    return get_json(url)


def index_performances(perf_data):
    index = {}
    for p in perf_data.get("participants", []):
        num = p.get("numPmu")
        if num is None:
            continue
        index[num] = p.get("coursesCourues", []) or []
    return index


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


def extraire_place(course_passee):
    place = course_passee.get("place") or {}
    p = place.get("place")
    if p is None:
        p = course_passee.get("itemNumPlace")
    try:
        return int(p)
    except (TypeError, ValueError):
        return None


def score_affinites(courses_passees, distance_jour, hippodrome_jour):
    pts_dist = []
    pts_terrain = []

    for cp in courses_passees:
        place = extraire_place(cp)
        if place is None:
            continue

        if place == 1:
            pts = 100.0
        elif place == 2:
            pts = 85.0
        elif place == 3:
            pts = 70.0
        elif place <= 5:
            pts = 45.0
        else:
            pts = 15.0

        dist = cp.get("distance")
        if dist and distance_jour:
            try:
                if abs(int(dist) - int(distance_jour)) <= TOLERANCE_DISTANCE:
                    pts_dist.append(pts)
            except (TypeError, ValueError):
                pass

        hippo = cp.get("hippodrome") or {}
        nom_hippo = hippo.get("libelleCourt") or hippo.get("libelleLong") or ""
        if nom_hippo and hippodrome_jour:
            if nom_hippo.strip().upper() == hippodrome_jour.strip().upper():
                pts_terrain.append(pts)

    score_dist = sum(pts_dist) / len(pts_dist) if pts_dist else None
    score_terrain = sum(pts_terrain) / len(pts_terrain) if pts_terrain else None

    return score_dist, score_terrain, len(pts_dist), len(pts_terrain)


def compute_jockey_stats(participants):
    jockey_scores = {}
    for p in participants:
        jockey = p.get("driver") or p.get("jockey") or ""
        if not jockey:
            continue
        sf, _, nb = parse_musique(p.get("musique", ""))
        if nb == 0:
            continue
        jockey_scores.setdefault(jockey, []).append(sf)
    return {j: sum(v) / len(v) for j, v in jockey_scores.items() if v}


def get_cote(p):
    for key in ("dernierRapportDirect", "dernierRapportReference"):
        r = p.get(key) or {}
        c = r.get("rapport")
        if c:
            try:
                return float(c)
            except (TypeError, ValueError):
                pass
    return None


def score_cheval(p, jockey_stats, perfs, distance_jour, hippodrome_jour):
    score_forme, taux_top3, nb_courses = parse_musique(p.get("musique", ""))
    if nb_courses == 0:
        return None

    courses_passees = perfs.get(p.get("numPmu"), [])
    s_dist, s_terrain, nb_dist, nb_terrain = score_affinites(
        courses_passees, distance_jour, hippodrome_jour)

    score_distance = s_dist if s_dist is not None else score_forme * 0.7
    score_terrain = s_terrain if s_terrain is not None else score_forme * 0.7

    jockey = p.get("driver") or p.get("jockey") or ""
    score_jockey = jockey_stats.get(jockey, 50.0)

    score_contexte = 50.0
    if p.get("deferre") and p.get("deferre") != "SANS":
        score_contexte += 25.0
    if p.get("oeilleres") and p.get("oeilleres") != "SANS":
        score_contexte += 15.0
    score_contexte = min(score_contexte, 100.0)

    score_total = (
        score_distance * 0.25
        + score_terrain * 0.20
        + score_forme * 0.25
        + taux_top3 * 0.15
        + score_jockey * 0.10
        + score_contexte * 0.05
    )

    return {
        "score": score_total,
        "forme": score_forme,
        "top3": taux_top3,
        "dist": s_dist,
        "terrain": s_terrain,
        "nb_dist": nb_dist,
        "nb_terrain": nb_terrain,
        "nb_courses": nb_courses,
    }


def analyser_journee(date_str):
    programme = get_programme(date_str)
    reunions = programme.get("programme", {}).get("reunions", [])

    selections = []
    courses_analysees = 0

    for reunion in reunions:
        r_num = reunion.get("numOfficiel")
        hippodrome = (reunion.get("hippodrome") or {}).get("libelleCourt", "")

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

            try:
                perf_data = get_performances(date_str, r_num, c_num)
                perfs = index_performances(perf_data)
            except Exception:
                perfs = {}

            courses_analysees += 1
            distance = course.get("distance")
            jockey_stats = compute_jockey_stats(participants)

            classement = []
            for p in participants:
                if p.get("statut") and p.get("statut") != "PARTANT":
                    continue

                res = score_cheval(p, jockey_stats, perfs, distance, hippodrome)
                if res is None:
                    continue

                classement.append({
                    "cheval": p.get("nom", "?"),
                    "numero": p.get("numPmu", "?"),
                    "cote": get_cote(p),
                    "jockey": p.get("driver") or p.get("jockey") or "?",
                    "res": res,
                })

            if not classement:
                continue

            classement.sort(key=lambda x: x["res"]["score"], reverse=True)
            top = classement[:TOP_PAR_COURSE]

            selections.append({
                "reunion": r_num,
                "course": c_num,
                "hippodrome": hippodrome,
                "distance": distance,
                "heure": course.get("heureDepart"),
                "top": top,
                "score_max": top[0]["res"]["score"],
            })

    selections.sort(key=lambda x: x["score_max"], reverse=True)
    return selections[:MAX_SELECTIONS], courses_analysees


def format_message(selections, courses_analysees, date_str):
    if not selections:
        return "Selections PMU - " + date_str + "\n\nAucune course exploitable aujourd'hui."

    message = "Selections PMU - " + date_str + "\n"
    message += str(courses_analysees) + " courses analysees\n\n"

    for s in selections:
        heure = s["heure"]
        if heure:
            try:
                heure = datetime.fromtimestamp(heure / 1000).strftime("%H:%M")
            except Exception:
                heure = "?"
        else:
            heure = "?"

        message += "=== R" + str(s["reunion"]) + "C" + str(s["course"])
        message += " " + s["hippodrome"]
        if s["distance"]:
            message += " " + str(s["distance"]) + "m"
        message += " (" + heure + ") ===\n"

        for i, h in enumerate(s["top"], 1):
            r = h["res"]
            message += str(i) + ". N" + str(h["numero"]) + " " + h["cheval"]
            if h["cote"]:
                message += " (cote " + str(round(h["cote"], 1)) + ")"
            message += "\n"
            message += "   Score " + str(round(r["score"])) + "/100"
            message += " | Forme " + str(round(r["forme"])) + "\n"

            if r["dist"] is not None:
                message += "   Distance: " + str(round(r["dist"]))
                message += " sur " + str(r["nb_dist"]) + " courses\n"
            else:
                message += "   Distance: pas d'historique\n"

            if r["terrain"] is not None:
                message += "   Terrain: " + str(round(r["terrain"]))
                message += " sur " + str(r["nb_terrain"]) + " courses\n"
            else:
                message += "   Terrain: pas d'historique\n"

            message += "   Jockey: " + h["jockey"] + "\n"

        message += "\n"

    message += "Analyse statistique, pas une garantie de gain."
    return message


def send_telegram_message(text):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    last = None
    for chunk in chunks:
        payload = {"chat_id": CHAT_ID, "text": chunk}
        resp = requests.post(url, data=payload, timeout=25)
        resp.raise_for_status()
        last = resp.json()
    return last


def main():
    date_str = datetime.now().strftime("%d%m%Y")
    print("Analyse du programme PMU pour le " + date_str)

    try:
        selections, courses_analysees = analyser_journee(date_str)
        print("Courses analysees: " + str(courses_analysees))
        print("Selections: " + str(len(selections)))
    except Exception as e:
        print("Erreur lors de l'analyse:")
        print(str(e))
        send_telegram_message("Bot PMU: erreur lors de l'analyse du " + date_str + "\n" + str(e))
        return

    message = format_message(selections, courses_analysees, date_str)
    result = send_telegram_message(message)
    print("Message envoye:")
    print(result.get("ok") if result else "aucun")


if __name__ == "__main__":
    main()
