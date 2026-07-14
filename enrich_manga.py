#!/usr/bin/env python3
"""
enrich_manga.py
----------------
Rellena campos vacios de manga_data.json usando MangaDex, AniList y Jikan
(MyAnimeList), y genera un archivo aparte con sugerencias para campos que
necesitan revision manual (content_rating, subgeneros, advertencias, tags)
porque usan un vocabulario propio en espanol que no se puede mapear 1:1 de
forma segura.

Uso:
    python3 enrich_manga.py manga_data.json

Genera:
    manga_data.actualizado.json   -> listo para "Importar JSON" en el admin
    sugerencias_revisar.json      -> cambios sugeridos para revisar a mano
    enrich_log.txt                -> log de que se encontro/no se encontro
                                      por titulo, INCLUYENDO el motivo del
                                      fallo (429, error de red, sin match...)

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
MANGADEX_BASE = "https://api.mangadex.org"

USER_AGENT = "gl-panels-enrich-script/1.1"

# Cuanto esperar entre llamadas para no pasarnos de los limites publicos.
# Jikan y AniList son estrictos con IPs compartidas (como las de GitHub
# Actions), asi que van mas despacio. MangaDex es mas permisivo.
ANILIST_DELAY = 0.9
JIKAN_DELAY = 1.3
MANGADEX_DELAY = 0.4

MAX_RETRIES_429 = 2
BACKOFF_SECONDS = [4, 10]  # espera creciente entre reintentos por 429

PAIS_MAP_ANILIST = {
    "JP": "Japon",
    "KR": "Corea del Sur",
    "CN": "China",
    "TW": "Taiwan",
}

PAIS_MAP_MANGADEX = {
    "ja": "Japon",
    "ko": "Corea del Sur",
    "zh": "China",
    "zh-hk": "China",
}

DEMOGRAFICO_MAP = {
    "shounen": "shounen",
    "shoujo": "shoujo",
    "seinen": "seinen",
    "josei": "josei",
}

# Tally global para el resumen final (diagnostico de por que fallo algo)
STATS = {
    "mangadex": {"ok": 0, "sin_match": 0, "error": {}},
    "jikan": {"ok": 0, "sin_match": 0, "error": {}},
    "anilist": {"ok": 0, "sin_match": 0, "error": {}},
}


def log(msg, logfile):
    print(msg)
    logfile.write(msg + "\n")


def similar(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _record_error(source, reason):
    STATS[source]["error"][reason] = STATS[source]["error"].get(reason, 0) + 1


def http_get_json(url, headers=None, source=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES_429:
                time.sleep(BACKOFF_SECONDS[attempt])
                attempt += 1
                continue
            reason = f"http_{e.code}"
            if source:
                _record_error(source, reason)
            return None, reason
        except Exception as e:
            reason = f"conn_error:{type(e).__name__}"
            if source:
                _record_error(source, reason)
            return None, reason


def http_post_json(url, payload, headers=None, source=None):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    h.update(headers or {})
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES_429:
                time.sleep(BACKOFF_SECONDS[attempt])
                attempt += 1
                continue
            reason = f"http_{e.code}"
            if source:
                _record_error(source, reason)
            return None, reason
        except Exception as e:
            reason = f"conn_error:{type(e).__name__}"
            if source:
                _record_error(source, reason)
            return None, reason


# ---------------------------------------------------------------------------
# MangaDex
# ---------------------------------------------------------------------------

def query_mangadex(title):
    q = urllib.parse.urlencode({"title": title, "limit": 5, "order[relevance]": "desc"})
    url = f"{MANGADEX_BASE}/manga?{q}&includes[]=author&includes[]=artist"
    data, err = http_get_json(url, source="mangadex")
    time.sleep(MANGADEX_DELAY)
    if err:
        return None, err
    if not data or not data.get("data"):
        STATS["mangadex"]["sin_match"] += 1
        return None, "sin_resultados"

    candidates = data["data"]
    best_entry, best_score = None, 0
    for c in candidates:
        attrs = c.get("attributes", {}) or {}
        titles_dict = attrs.get("title", {}) or {}
        alt_list = attrs.get("altTitles", []) or []
        titles = [v for v in titles_dict.values() if v]
        for alt in alt_list:
            titles.extend([v for v in alt.values() if v])
        score = max((similar(title, t) for t in titles), default=0)
        if score > best_score:
            best_score, best_entry = score, c

    if not best_entry or best_score < 0.45:
        STATS["mangadex"]["sin_match"] += 1
        return None, "sin_match_confiable"

    STATS["mangadex"]["ok"] += 1
    attrs = best_entry.get("attributes", {}) or {}
    titles_dict = attrs.get("title", {}) or {}
    alt_list = attrs.get("altTitles", []) or []

    autores = []
    for rel in best_entry.get("relationships", []):
        if rel.get("type") in ("author", "artist"):
            name = (rel.get("attributes") or {}).get("name")
            if name and name not in autores:
                autores.append(name)

    lang = attrs.get("originalLanguage")
    pais = PAIS_MAP_MANGADEX.get(lang)
    demografico = DEMOGRAFICO_MAP.get((attrs.get("publicationDemographic") or "").lower())

    tags = attrs.get("tags", []) or []
    generos = [
        t["attributes"]["name"].get("en")
        for t in tags
        if t.get("attributes", {}).get("group") in ("genre", "theme")
        and t.get("attributes", {}).get("name", {}).get("en")
    ]

    content_rating = attrs.get("contentRating")  # safe/suggestive/erotica/pornographic

    capitulos = None
    if attrs.get("status") == "completed":
        try:
            capitulos = int(float(attrs.get("lastChapter")))
        except (TypeError, ValueError):
            capitulos = None

    titulos_alt_all = [v for v in titles_dict.values() if v]
    for alt in alt_list:
        titulos_alt_all.extend([v for v in alt.values() if v])
    titulos_alt_all = list(dict.fromkeys([t for t in titulos_alt_all if similar(title, t) < 0.99]))

    return {
        "match_score": best_score,
        "autor": autores,
        "anio_inicio": attrs.get("year"),
        "pais_cultura": pais,
        "demografico": demografico,
        "genres": generos,
        "titulos_alt": titulos_alt_all[:6],
        "capitulos_totales": capitulos,
        "content_rating_raw": content_rating,
    }, None


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
    data, err = http_post_json(ANILIST_URL, payload, source="anilist")
    time.sleep(ANILIST_DELAY)
    if err:
        return None, err
    if not data or "data" not in data or not data["data"].get("Media"):
        STATS["anilist"]["sin_match"] += 1
        return None, "sin_resultados"

    m = data["data"]["Media"]
    titles = [t for t in [m["title"].get("romaji"), m["title"].get("english"), m["title"].get("native")] if t]
    best = max((similar(title, t) for t in titles), default=0)
    if best < 0.45:
        STATS["anilist"]["sin_match"] += 1
        return None, "sin_match_confiable"

    STATS["anilist"]["ok"] += 1
    autores = [
        e["node"]["name"]["full"]
        for e in m.get("staff", {}).get("edges", [])
        if e.get("role", "").lower() in ("story", "story & art", "author", "original creator")
    ]
    if not autores:
        autores = [e["node"]["name"]["full"] for e in m.get("staff", {}).get("edges", [])[:2]]
    return {
        "match_score": best,
        "autor": autores,
        "anio_inicio": m.get("startDate", {}).get("year"),
        "pais_cultura": PAIS_MAP_ANILIST.get(m.get("countryOfOrigin")),
        "isAdult": m.get("isAdult"),
        "genres": m.get("genres", []),
        "tags": [t["name"] for t in m.get("tags", [])][:15],
        "titulos_alt": list(dict.fromkeys([t for t in titles + m.get("synonyms", []) if t and similar(title, t) < 0.99])),
        "capitulos_totales": m.get("chapters"),
    }, None


# ---------------------------------------------------------------------------
# Jikan (MyAnimeList)
# ---------------------------------------------------------------------------

def query_jikan(title):
    q = urllib.parse.quote(title)
    url = f"{JIKAN_BASE}/manga?q={q}&limit=5"
    data, err = http_get_json(url, source="jikan")
    time.sleep(JIKAN_DELAY)
    if err:
        return None, err
    if not data or not data.get("data"):
        STATS["jikan"]["sin_match"] += 1
        return None, "sin_resultados"

    candidates = data["data"]
    best_entry, best_score = None, 0
    for c in candidates:
        titles = [c.get("title"), c.get("title_english")] + [t.get("title") for t in c.get("titles", [])]
        titles = [t for t in titles if t]
        score = max((similar(title, t) for t in titles), default=0)
        if score > best_score:
            best_score, best_entry = score, c
    if not best_entry or best_score < 0.45:
        STATS["jikan"]["sin_match"] += 1
        return None, "sin_match_confiable"

    STATS["jikan"]["ok"] += 1
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
    }, None


# ---------------------------------------------------------------------------
# Fusion de resultados y aplicacion a la entrada
# ---------------------------------------------------------------------------

def is_empty(v):
    return v is None or v == "" or v == [] or v is False


def first_truthy(*vals):
    for v in vals:
        if v:
            return v
    return None


def enrich_entry(entry, logfile):
    titulo = entry["titulo"]
    changes = {}       # se escriben directo en la entrada (campos vacios)
    suggestions = {}   # se guardan aparte para revisar

    mdx, mdx_err = query_mangadex(titulo)
    jik, jik_err = query_jikan(titulo)
    ani, ani_err = query_anilist(titulo)

    if not mdx and not jik and not ani:
        log(f"[SIN DATOS] {titulo}  (mangadex={mdx_err}, jikan={jik_err}, anilist={ani_err})", logfile)
        return changes, suggestions
    log(f"[OK] {titulo}  (mangadex={'si' if mdx else mdx_err}, jikan={'si' if jik else jik_err}, anilist={'si' if ani else ani_err})", logfile)

    # Orden de preferencia: mangadex y jikan primero porque su vocabulario
    # (demografico, content rating) encaja mejor con el tuyo; anilist de
    # respaldo (mejor cobertura de manhwa/manhua a veces).

    # --- autor ---
    if is_empty(entry.get("autor")):
        autor = first_truthy((mdx or {}).get("autor"), (jik or {}).get("autor"), (ani or {}).get("autor"))
        if autor:
            changes["autor"] = autor

    # --- anio_inicio ---
    if is_empty(entry.get("anio_inicio")):
        anio = first_truthy((mdx or {}).get("anio_inicio"), (jik or {}).get("anio_inicio"), (ani or {}).get("anio_inicio"))
        if anio:
            changes["anio_inicio"] = anio

    # --- titulos_alt ---
    if is_empty(entry.get("titulos_alt")):
        alt = list(dict.fromkeys(
            ((mdx or {}).get("titulos_alt") or [])
            + ((jik or {}).get("titulos_alt") or [])
            + ((ani or {}).get("titulos_alt") or [])
        ))
        alt = [a for a in alt if a][:6]
        if alt:
            changes["titulos_alt"] = alt

    # --- demografico ---
    if is_empty(entry.get("demografico")):
        demo = first_truthy((mdx or {}).get("demografico"), (jik or {}).get("demografico"))
        if demo:
            changes["demografico"] = demo

    # --- pais_cultura ---
    if is_empty(entry.get("pais_cultura")):
        pais = first_truthy((mdx or {}).get("pais_cultura"), (ani or {}).get("pais_cultura"))
        if pais:
            changes["pais_cultura"] = [pais]

    # --- capitulos_totales (solo si finalizado, para no marcar como total algo que sigue) ---
    if is_empty(entry.get("capitulos_totales")) and entry.get("estado") == "Finalizado":
        caps = first_truthy((mdx or {}).get("capitulos_totales"), (jik or {}).get("capitulos_totales"), (ani or {}).get("capitulos_totales"))
        if caps:
            changes["capitulos_totales"] = caps

    # --- SUGERENCIAS (nunca se escriben directo, siempre a revisar) ---
    generos_ingles = list(dict.fromkeys(
        ((mdx or {}).get("genres") or [])
        + ((ani or {}).get("genres") or [])
        + ((jik or {}).get("genres") or [])
        + ((jik or {}).get("themes") or [])
    ))
    if generos_ingles:
        suggestions["generos_fuente_en"] = generos_ingles

    rating_sugerido = None
    mdx_rating = (mdx or {}).get("content_rating_raw")
    if mdx_rating in ("erotica", "pornographic") or (ani or {}).get("isAdult") or (jik or {}).get("es_hentai"):
        rating_sugerido = "erotica"
    elif mdx_rating == "suggestive" or (jik or {}).get("es_ecchi"):
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

    resumen = ["", "===== RESUMEN POR FUENTE ====="]
    for src, s in STATS.items():
        resumen.append(f"{src}: {s['ok']} encontrados, {s['sin_match']} sin coincidencia, errores={s['error']}")
    resumen.append("===============================")
    for line in resumen:
        log(line, logfile)

    logfile.close()
    print(f"\nListo. {updated} entradas con campos vacios rellenados.")
    print(f"{len(all_suggestions)} entradas con sugerencias para revisar en sugerencias_revisar.json")
    print("Revisa el RESUMEN POR FUENTE al final de enrich_log.txt si algo sigue sin funcionar:")
    print("  - muchos 'http_429' = las APIs estan bloqueando al runner por rate limit")
    print("  - muchos 'conn_error' = problema de red/DNS en el runner")
    print("  - muchos 'sin_resultados'/'sin_match_confiable' = titulos que esas fuentes no tienen o no matchean bien")


if __name__ == "__main__":
    main()
