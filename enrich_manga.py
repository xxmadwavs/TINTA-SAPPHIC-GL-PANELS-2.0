#!/usr/bin/env python3
"""
enrich_manga.py
----------------
Rellena campos vacios de manga_data.json usando AniList y Jikan (MyAnimeList),
y genera un archivo aparte con sugerencias para campos que necesitan revision
manual (content_rating, subgeneros, advertencias, tags) porque usan un
vocabulario propio en espanol que no se puede mapear 1:1 de forma segura.

Uso:
    python3 enrich_manga.py manga_data.json

Genera:
    manga_data.actualizado.json   -> listo para "Importar JSON" en el admin
    sugerencias_revisar.json      -> cambios sugeridos para revisar a mano
    enrich_log.txt                -> log de que se encontro/no se encontro por titulo

No requiere librerias externas (solo stdlib: urllib, json, time, difflib).
"""

import json
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from difflib import SequenceMatcher

ANILIST_URL = "https://graphql.anilist.co"
JIKAN_BASE = "https://api.jikan.moe/v4"

# Cuanto esperar entre llamadas para no pasarnos de los limites publicos
ANILIST_DELAY = 0.7   # ~85 req/min
JIKAN_DELAY = 1.1      # Jikan pide <=3 req/s, sostenido ~60/min

PAIS_MAP = {
    "JP": "Japon",
    "KR": "Corea del Sur",
    "CN": "China",
    "TW": "Taiwan",
}

DEMOGRAFICO_MAP = {
    "shounen": "shounen",
    "shoujo": "shoujo",
    "seinen": "seinen",
    "josei": "josei",
}


def log(msg, logfile):
    print(msg)
    logfile.write(msg + "\n")


def similar(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "manga-enrich-script/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # backoff simple y reintento una vez
            time.sleep(3)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception:
                return None
        return None
    except Exception:
        return None


def http_post_json(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(3)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception:
                return None
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# AniList
# ---------------------------------------------------------------------------

ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: MANGA) {
    title { romaji english native }
    startDate { year }
    countryOfOrigin
    isAdult
    genres
    tags { name }
    description(asHtml: false)
    staff(perPage: 5) {
      edges { role node { name { full } } }
    }
    synonyms
    chapters
  }
}
"""


def query_anilist(title):
    payload = {"query": ANILIST_QUERY, "variables": {"search": title}}
    data = http_post_json(ANILIST_URL, payload)
    time.sleep(ANILIST_DELAY)
    if not data or "data" not in data or not data["data"].get("Media"):
        return None
    m = data["data"]["Media"]
    titles = [t for t in [m["title"].get("romaji"), m["title"].get("english"), m["title"].get("native")] if t]
    best = max((similar(title, t) for t in titles), default=0)
    if best < 0.45:
        return None  # probablemente no es el mismo titulo
    autores = [e["node"]["name"]["full"] for e in m.get("staff", {}).get("edges", []) if e.get("role", "").lower() in ("story", "story & art", "author", "original creator")]
    if not autores:
        autores = [e["node"]["name"]["full"] for e in m.get("staff", {}).get("edges", [])[:2]]
    return {
        "match_score": best,
        "autor": autores,
        "anio_inicio": m.get("startDate", {}).get("year"),
        "pais_cultura": PAIS_MAP.get(m.get("countryOfOrigin")),
        "isAdult": m.get("isAdult"),
        "genres": m.get("genres", []),
        "tags": [t["name"] for t in m.get("tags", [])][:15],
        "titulos_alt": list(dict.fromkeys([t for t in titles + m.get("synonyms", []) if t and similar(title, t) < 0.99])),
        "capitulos_totales": m.get("chapters"),
    }


# ---------------------------------------------------------------------------
# Jikan (MyAnimeList)
# ---------------------------------------------------------------------------

def query_jikan(title):
    q = urllib.parse.quote(title)
    url = f"{JIKAN_BASE}/manga?q={q}&limit=5"
    data = http_get_json(url)
    time.sleep(JIKAN_DELAY)
    if not data or not data.get("data"):
        return None
    candidates = data["data"]
    best_entry, best_score = None, 0
    for c in candidates:
        titles = [c.get("title"), c.get("title_english")] + [t.get("title") for t in c.get("titles", [])]
        titles = [t for t in titles if t]
        score = max((similar(title, t) for t in titles), default=0)
        if score > best_score:
            best_score, best_entry = score, c
    if not best_entry or best_score < 0.45:
        return None
    c = best_entry
    autores = [a.get("name", "").replace(", ", " ").strip() for a in c.get("authors", [])]
    demografico = None
    if c.get("demographics"):
        demografico = DEMOGRAFICO_MAP.get(c["demographics"][0]["name"].lower())
    anio = None
    if c.get("published", {}).get("from"):
        anio = int(c["published"]["from"][:4])
    return {
        "match_score": best_score,
        "autor": autores,
        "anio_inicio": anio,
        "demografico": demografico,
        "genres": [g["name"] for g in c.get("genres", [])],
        "themes": [t["name"] for t in c.get("themes", [])],
        "capitulos_totales": c.get("chapters"),
        "titulos_alt": [t.get("title") for t in c.get("titles", []) if t.get("title") and similar(title, t.get("title")) < 0.99],
        "es_hentai": any(g["name"].lower() == "hentai" for g in c.get("genres", [])) or any(g["name"].lower() == "erotica" for g in c.get("genres", [])),
        "es_ecchi": any(g["name"].lower() == "ecchi" for g in c.get("genres", [])),
    }


# ---------------------------------------------------------------------------
# Fusion de resultados y aplicacion a la entrada
# ---------------------------------------------------------------------------

def is_empty(v):
    return v is None or v == "" or v == [] or v is False


def enrich_entry(entry, logfile):
    titulo = entry["titulo"]
    changes = {}       # se escriben directo en la entrada (campos vacios)
    suggestions = {}   # se guardan aparte para revisar

    ani = query_anilist(titulo)
    jik = query_jikan(titulo)

    if not ani and not jik:
        log(f"[SIN DATOS] {titulo}", logfile)
        return changes, suggestions
    log(f"[OK] {titulo}  (anilist={'si' if ani else 'no'}, jikan={'si' if jik else 'no'})", logfile)

    # --- autor ---
    if is_empty(entry.get("autor")):
        autor = (jik or {}).get("autor") or (ani or {}).get("autor")
        if autor:
            changes["autor"] = autor

    # --- anio_inicio ---
    if is_empty(entry.get("anio_inicio")):
        anio = (jik or {}).get("anio_inicio") or (ani or {}).get("anio_inicio")
        if anio:
            changes["anio_inicio"] = anio

    # --- titulos_alt ---
    if is_empty(entry.get("titulos_alt")):
        alt = list(dict.fromkeys(((ani or {}).get("titulos_alt") or []) + ((jik or {}).get("titulos_alt") or [])))
        alt = [a for a in alt if a][:6]
        if alt:
            changes["titulos_alt"] = alt

    # --- demografico (solo tiene sentido si seccion == Manga, pero lo dejamos igual, se filtra fuera) ---
    if is_empty(entry.get("demografico")):
        demo = (jik or {}).get("demografico")
        if demo:
            changes["demografico"] = demo

    # --- pais_cultura ---
    if is_empty(entry.get("pais_cultura")):
        pais = (ani or {}).get("pais_cultura")
        if pais:
            changes["pais_cultura"] = [pais]

    # --- capitulos_totales (solo si finalizado, para no marcar como total algo que sigue) ---
    if is_empty(entry.get("capitulos_totales")) and entry.get("estado") == "Finalizado":
        caps = (jik or {}).get("capitulos_totales") or (ani or {}).get("capitulos_totales")
        if caps:
            changes["capitulos_totales"] = caps

    # --- SUGERENCIAS (nunca se escriben directo, siempre a revisar) ---
    generos_ingles = list(dict.fromkeys(((ani or {}).get("genres") or []) + ((jik or {}).get("genres") or []) + ((jik or {}).get("themes") or [])))
    if generos_ingles:
        suggestions["generos_fuente_en"] = generos_ingles

    rating_sugerido = None
    if (ani or {}).get("isAdult") or (jik or {}).get("es_hentai"):
        rating_sugerido = "erotica"
    elif (jik or {}).get("es_ecchi"):
        rating_sugerido = "sugestiva"
    if rating_sugerido:
        suggestions["content_rating_sugerido"] = rating_sugerido

    if suggestions:
        suggestions["titulo"] = titulo
        suggestions["id"] = entry["id"]

    return changes, suggestions


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 enrich_manga.py manga_data.json")
        sys.exit(1)

    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    logfile = open("enrich_log.txt", "w", encoding="utf-8")
    all_suggestions = []
    updated = 0

    for i, entry in enumerate(data):
        changes, suggestions = enrich_entry(entry, logfile)
        if changes:
            entry.update(changes)
            updated += 1
        if suggestions:
            all_suggestions.append(suggestions)
        if (i + 1) % 20 == 0:
            print(f"... {i + 1}/{len(data)} procesados")

    with open("manga_data.actualizado.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    with open("sugerencias_revisar.json", "w", encoding="utf-8") as f:
        json.dump(all_suggestions, f, ensure_ascii=False, indent=2)

    logfile.close()
    print(f"\nListo. {updated} entradas con campos vacios rellenados.")
    print(f"{len(all_suggestions)} entradas con sugerencias para revisar en sugerencias_revisar.json")


if __name__ == "__main__":
    main()
