#!/usr/bin/env python3
"""
discover_gl_yuri.py
--------------------
Recorre AniList, MangaDex y Jikan (MyAnimeList) buscando TODO lo etiquetado
Girls' Love / Yuri (por genero/tag, no por titulo), fusiona los resultados
de las 3 fuentes en una sola ficha por obra, descarta automaticamente lo que
ya esta en tu manga_data.json (comparando titulo + titulos alternativos), y
deja el resto en un JSON de "candidatos nuevos" listo para revisar con
revisar_altas_nuevas.html.

No inventa nada ni decide por ti: content_rating, subgeneros, personajes y
advertencias quedan vacios (se rellenan en la pagina de revision, igual que
en sugerencias_revisar.json del script de enriquecimiento). Solo rellena
automaticamente los campos "seguros" (autor, anio, pais, seccion, estado,
portada, sinopsis en el idioma de origen, enlaces oficiales) y SIEMPRE los
deja marcados como candidatos, nunca como altas definitivas.

Uso:
    python3 discover_gl_yuri.py manga_data.json \
        [--max-pages-anilist 40] [--max-pages-mangadex 40] [--max-pages-jikan 40]

Genera:
    discoveries_gl_yuri.json  -> candidatos nuevos, para revisar_altas_nuevas.html
    discover_log.txt          -> resumen de lo recorrido y descartado

No requiere librerias externas (solo stdlib).
"""

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

ANILIST_URL = "https://graphql.anilist.co"
JIKAN_BASE = "https://api.jikan.moe/v4"
MANGADEX_BASE = "https://api.mangadex.org"

USER_AGENT = "gl-panels-discover-script/1.0"

ANILIST_DELAY = 0.9
JIKAN_DELAY = 1.3
MANGADEX_DELAY = 0.5

MAX_RETRIES_429 = 3
BACKOFF_SECONDS = [4, 10, 20]

DUP_THRESHOLD = 0.82   # umbral para decidir "ya lo tienes en catalogo"
CLUSTER_THRESHOLD = 0.82  # umbral para fusionar el mismo titulo entre fuentes

PAIS_MAP_ANILIST = {"JP": "Japon", "KR": "Corea del Sur", "CN": "China", "TW": "Taiwan"}
PAIS_MAP_MANGADEX = {"ja": "Japon", "ko": "Corea del Sur", "zh": "China", "zh-hk": "China"}
SECCION_POR_PAIS = {"Japon": "Manga", "Corea del Sur": "Manhwa", "China": "Manhua", "Taiwan": "Manhua"}

DEMOGRAFICO_MAP = {"shounen": "shounen", "shoujo": "shoujo", "seinen": "seinen", "josei": "josei"}

ESTADO_MAP_ANILIST = {
    "RELEASING": "Continúa", "FINISHED": "Finalizado",
    "CANCELLED": "Cancelado", "HIATUS": "Pausado", "NOT_YET_RELEASED": "Continúa",
}
ESTADO_MAP_MANGADEX = {
    "ongoing": "Continúa", "completed": "Finalizado",
    "cancelled": "Cancelado", "hiatus": "Pausado",
}
ESTADO_MAP_JIKAN = {
    "publishing": "Continúa", "finished": "Finalizado",
    "on hiatus": "Pausado", "discontinued": "Cancelado",
}

MANGADEX_LINK_LABELS = {
    "engtl": "Traduccion oficial (EN)", "raw": "Raw oficial",
    "bw": "BookWalker", "cdj": "CDJapan", "ebj": "eBookJapan",
}

STATS = {
    "anilist": {"vistos": 0, "errores": {}},
    "mangadex": {"vistos": 0, "errores": {}},
    "jikan": {"vistos": 0, "errores": {}},
}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def log(msg, logfile):
    print(msg)
    logfile.write(msg + "\n")


def similar(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def normalize(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def bucket_key(norm):
    if not norm:
        return ""
    return norm.split(" ", 1)[0][:4]


def gen_temp_id():
    import random
    import string
    base = format(int(time.time() * 1000), "x")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"cand_{base}{rand}"


def http_get_json(url, headers=None, source=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES_429:
                time.sleep(BACKOFF_SECONDS[attempt])
                attempt += 1
                continue
            reason = f"http_{e.code}"
            if source:
                STATS[source]["errores"][reason] = STATS[source]["errores"].get(reason, 0) + 1
            return None, reason
        except Exception as e:
            reason = f"conn_error:{type(e).__name__}"
            if source:
                STATS[source]["errores"][reason] = STATS[source]["errores"].get(reason, 0) + 1
            return None, reason


def http_post_json(url, payload, headers=None, source=None):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    h.update(headers or {})
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES_429:
                time.sleep(BACKOFF_SECONDS[attempt])
                attempt += 1
                continue
            reason = f"http_{e.code}"
            if source:
                STATS[source]["errores"][reason] = STATS[source]["errores"].get(reason, 0) + 1
            return None, reason
        except Exception as e:
            reason = f"conn_error:{type(e).__name__}"
            if source:
                STATS[source]["errores"][reason] = STATS[source]["errores"].get(reason, 0) + 1
            return None, reason


# ---------------------------------------------------------------------------
# Indice del catalogo existente (para descartar duplicados)
# ---------------------------------------------------------------------------

def build_catalog_index(catalog):
    """bucket -> lista de (titulo_normalizado, titulo_original, entry)"""
    index = {}
    for entry in catalog:
        titles = [entry.get("titulo")] + list(entry.get("titulos_alt") or [])
        for t in titles:
            if not t:
                continue
            norm = normalize(t)
            if not norm:
                continue
            index.setdefault(bucket_key(norm), []).append((norm, t, entry))
    return index


def is_in_catalog(candidate_titles, catalog_index):
    """Devuelve (True, titulo_catalogo, score) si el candidato ya esta en el catalogo."""
    best_title, best_score = None, 0
    for ct in candidate_titles:
        if not ct:
            continue
        norm_c = normalize(ct)
        if not norm_c:
            continue
        bucket = catalog_index.get(bucket_key(norm_c), [])
        for norm_e, orig_e, _entry in bucket:
            if norm_c == norm_e:
                return True, orig_e, 1.0
            score = similar(norm_c, norm_e)
            if score > best_score:
                best_score, best_title = score, orig_e
    if best_score >= DUP_THRESHOLD:
        return True, best_title, best_score
    return False, best_title, best_score


# ---------------------------------------------------------------------------
# Clustering entre fuentes (mismo titulo visto en AniList/MangaDex/Jikan)
# ---------------------------------------------------------------------------

class Cluster:
    __slots__ = ("titles_norm", "anilist", "mangadex", "jikan")

    def __init__(self):
        self.titles_norm = set()
        self.anilist = None
        self.mangadex = None
        self.jikan = None


def merge_into_clusters(clusters, cluster_index, source_name, raw, titles):
    """Busca un cluster existente por similitud de titulo; si no hay, crea uno."""
    norm_titles = [normalize(t) for t in titles if t]
    norm_titles = [t for t in norm_titles if t]
    if not norm_titles:
        return

    found = None
    for nt in norm_titles:
        bucket = cluster_index.get(bucket_key(nt), [])
        for cluster in bucket:
            if any(nt == existing or similar(nt, existing) >= CLUSTER_THRESHOLD for existing in cluster.titles_norm):
                found = cluster
                break
        if found:
            break

    if found is None:
        found = Cluster()
        clusters.append(found)

    found.titles_norm.update(norm_titles)
    setattr(found, source_name, raw)

    for nt in norm_titles:
        cluster_index.setdefault(bucket_key(nt), [])
        if found not in cluster_index[bucket_key(nt)]:
            cluster_index[bucket_key(nt)].append(found)


# ---------------------------------------------------------------------------
# AniList: pagina por genero "Girls Love"
# ---------------------------------------------------------------------------

ANILIST_DISCOVER_QUERY = """
query ($page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage total }
    media(genre: "Girls Love", type: MANGA, sort: POPULARITY_DESC) {
      id
      title { romaji english native }
      synonyms
      startDate { year }
      status
      countryOfOrigin
      format
      isAdult
      genres
      tags { name }
      description(asHtml: false)
      coverImage { large }
      siteUrl
      chapters
      staff(perPage: 5) { edges { role node { name { full } } } }
    }
  }
}
"""


def fetch_anilist_gl(max_pages, logfile):
    results = []
    page = 1
    while page <= max_pages:
        data, err = http_post_json(ANILIST_URL, {"query": ANILIST_DISCOVER_QUERY, "variables": {"page": page}}, source="anilist")
        time.sleep(ANILIST_DELAY)
        if err or not data or "data" not in data:
            log(f"[AniList] pagina {page}: error {err}", logfile)
            break
        media_list = data["data"]["Page"]["media"]
        STATS["anilist"]["vistos"] += len(media_list)
        for m in media_list:
            titles = [m["title"].get("romaji"), m["title"].get("english"), m["title"].get("native")] + (m.get("synonyms") or [])
            titles = [t for t in titles if t]
            if not titles:
                continue
            autores = [e["node"]["name"]["full"] for e in m.get("staff", {}).get("edges", [])
                       if e.get("role", "").lower() in ("story", "story & art", "author", "original creator")]
            if not autores:
                autores = [e["node"]["name"]["full"] for e in m.get("staff", {}).get("edges", [])[:2]]
            pais = PAIS_MAP_ANILIST.get(m.get("countryOfOrigin"))
            results.append({
                "titulo_principal": m["title"].get("romaji") or m["title"].get("english") or titles[0],
                "titulos": titles,
                "autor": autores,
                "anio_inicio": (m.get("startDate") or {}).get("year"),
                "estado": ESTADO_MAP_ANILIST.get(m.get("status")),
                "pais_cultura": pais,
                "seccion": SECCION_POR_PAIS.get(pais),
                "isAdult": m.get("isAdult"),
                "genres": m.get("genres") or [],
                "tags": [t["name"] for t in (m.get("tags") or [])][:15],
                "desc": m.get("description"),
                "imagen": (m.get("coverImage") or {}).get("large"),
                "capitulos_totales": m.get("chapters"),
                "url": m.get("siteUrl"),
                "id": m.get("id"),
            })
        has_next = data["data"]["Page"]["pageInfo"].get("hasNextPage")
        log(f"[AniList] pagina {page} ok ({len(media_list)} obras, total fuente: {data['data']['Page']['pageInfo'].get('total')})", logfile)
        if not has_next:
            break
        page += 1
    return results


# ---------------------------------------------------------------------------
# MangaDex: resuelve el tag "Girls Love" y pagina por el
# ---------------------------------------------------------------------------

def resolve_mangadex_tag_id(logfile):
    data, err = http_get_json(f"{MANGADEX_BASE}/manga/tag", source="mangadex")
    if err or not data:
        log(f"[MangaDex] no se pudo resolver el tag Girls Love: {err}", logfile)
        return None
    for t in data.get("data", []):
        attrs = t.get("attributes", {}) or {}
        name_en = (attrs.get("name") or {}).get("en", "")
        if name_en.strip().lower() in ("girls love", "yuri") and attrs.get("group") == "genre":
            return t["id"]
    return None


def fetch_mangadex_gl(max_pages, logfile):
    tag_id = resolve_mangadex_tag_id(logfile)
    if not tag_id:
        log("[MangaDex] tag Girls Love no encontrado, se omite esta fuente.", logfile)
        return []

    results = []
    limit = 100
    for page in range(max_pages):
        offset = page * limit
        q = urllib.parse.urlencode({
            "limit": limit, "offset": offset,
            "includedTags[]": tag_id,
        }, doseq=True)
        url = (f"{MANGADEX_BASE}/manga?{q}&includes[]=author&includes[]=artist&includes[]=cover_art"
               f"&order[followedCount]=desc")
        data, err = http_get_json(url, source="mangadex")
        time.sleep(MANGADEX_DELAY)
        if err or not data:
            log(f"[MangaDex] pagina offset={offset}: error {err}", logfile)
            break
        items = data.get("data", [])
        STATS["mangadex"]["vistos"] += len(items)
        for c in items:
            attrs = c.get("attributes", {}) or {}
            titles_dict = attrs.get("title", {}) or {}
            alt_list = attrs.get("altTitles", []) or []
            titles = [v for v in titles_dict.values() if v]
            for alt in alt_list:
                titles.extend([v for v in alt.values() if v])
            if not titles:
                continue

            autores, cover_file = [], None
            for rel in c.get("relationships", []):
                if rel.get("type") in ("author", "artist"):
                    name = (rel.get("attributes") or {}).get("name")
                    if name and name not in autores:
                        autores.append(name)
                if rel.get("type") == "cover_art":
                    cover_file = (rel.get("attributes") or {}).get("fileName")

            imagen = f"https://uploads.mangadex.org/covers/{c['id']}/{cover_file}.512.jpg" if cover_file else None
            lang = attrs.get("originalLanguage")
            pais = PAIS_MAP_MANGADEX.get(lang)
            demografico = DEMOGRAFICO_MAP.get((attrs.get("publicationDemographic") or "").lower())

            tags = attrs.get("tags", []) or []
            generos = [t["attributes"]["name"].get("en") for t in tags
                       if t.get("attributes", {}).get("group") in ("genre", "theme")
                       and t.get("attributes", {}).get("name", {}).get("en")]

            capitulos = None
            if attrs.get("status") == "completed":
                try:
                    capitulos = int(float(attrs.get("lastChapter")))
                except (TypeError, ValueError):
                    capitulos = None

            links = attrs.get("links", {}) or {}
            enlaces = []
            for key, label in MANGADEX_LINK_LABELS.items():
                val = links.get(key)
                if val and (val.startswith("http://") or val.startswith("https://")):
                    enlaces.append({"label": label, "url": val})
                elif val and key == "bw":
                    enlaces.append({"label": label, "url": f"https://bookwalker.jp/{val}"})

            desc_dict = attrs.get("description", {}) or {}
            desc = desc_dict.get("en") or next(iter(desc_dict.values()), None)

            results.append({
                "titulo_principal": titles[0],
                "titulos": titles,
                "autor": autores,
                "anio_inicio": attrs.get("year"),
                "estado": ESTADO_MAP_MANGADEX.get(attrs.get("status")),
                "pais_cultura": pais,
                "seccion": SECCION_POR_PAIS.get(pais),
                "content_rating_raw": attrs.get("contentRating"),
                "demografico": demografico,
                "genres": generos,
                "desc": desc,
                "imagen": imagen,
                "capitulos_totales": capitulos,
                "enlaces_lectura": enlaces,
                "url": f"https://mangadex.org/title/{c['id']}",
                "id": c.get("id"),
            })
        log(f"[MangaDex] offset {offset} ok ({len(items)} obras, total fuente: {data.get('total')})", logfile)
        if offset + limit >= (data.get("total") or 0) or not items:
            break
    return results


# ---------------------------------------------------------------------------
# Jikan: resuelve el genero "Girls Love" y pagina por el
# ---------------------------------------------------------------------------

def resolve_jikan_genre_id(logfile):
    data, err = http_get_json(f"{JIKAN_BASE}/genres/manga", source="jikan")
    time.sleep(JIKAN_DELAY)
    if err or not data:
        log(f"[Jikan] no se pudo resolver el genero Girls Love: {err}", logfile)
        return None
    for g in data.get("data", []):
        if g.get("name", "").strip().lower() in ("girls love", "shoujo ai"):
            return g.get("mal_id")
    return None


def fetch_jikan_gl(max_pages, logfile):
    genre_id = resolve_jikan_genre_id(logfile)
    if not genre_id:
        log("[Jikan] genero Girls Love no encontrado, se omite esta fuente.", logfile)
        return []

    results = []
    for page in range(1, max_pages + 1):
        url = f"{JIKAN_BASE}/manga?genres={genre_id}&order_by=start_date&sort=desc&page={page}&limit=25"
        data, err = http_get_json(url, source="jikan")
        time.sleep(JIKAN_DELAY)
        if err or not data:
            log(f"[Jikan] pagina {page}: error {err}", logfile)
            break
        items = data.get("data", [])
        STATS["jikan"]["vistos"] += len(items)
        for c in items:
            titles = [c.get("title"), c.get("title_english")] + [t.get("title") for t in c.get("titles", [])]
            titles = [t for t in titles if t]
            if not titles:
                continue
            autores = [a.get("name", "").replace(", ", " ").strip() for a in c.get("authors", [])]
            demografico = None
            if c.get("demographics"):
                demografico = DEMOGRAFICO_MAP.get(c["demographics"][0]["name"].lower())
            anio = None
            if (c.get("published") or {}).get("from"):
                anio = int(c["published"]["from"][:4])
            es_hentai = any(g["name"].lower() in ("hentai", "erotica") for g in c.get("genres", []))
            es_ecchi = any(g["name"].lower() == "ecchi" for g in c.get("genres", []))
            results.append({
                "titulo_principal": c.get("title") or titles[0],
                "titulos": titles,
                "autor": autores,
                "anio_inicio": anio,
                "estado": ESTADO_MAP_JIKAN.get((c.get("status") or "").lower()),
                "demografico": demografico,
                "genres": [g["name"] for g in c.get("genres", [])],
                "themes": [t["name"] for t in c.get("themes", [])],
                "desc": c.get("synopsis"),
                "imagen": ((c.get("images") or {}).get("jpg") or {}).get("large_image_url"),
                "capitulos_totales": c.get("chapters"),
                "es_hentai": es_hentai,
                "es_ecchi": es_ecchi,
                "tipo_fuente": c.get("type"),
                "url": c.get("url"),
                "id": c.get("mal_id"),
            })
        log(f"[Jikan] pagina {page} ok ({len(items)} obras)", logfile)
        pagination = data.get("pagination", {}) or {}
        if not pagination.get("has_next_page") or not items:
            break
    return results


# ---------------------------------------------------------------------------
# Fusion final por cluster -> ficha candidata
# ---------------------------------------------------------------------------

def first_truthy(*vals):
    for v in vals:
        if v:
            return v
    return None


def build_candidate(cluster):
    a, m, j = cluster.anilist, cluster.mangadex, cluster.jikan

    titulo = first_truthy(
        (m or {}).get("titulo_principal"),
        (j or {}).get("titulo_principal"),
        (a or {}).get("titulo_principal"),
    )
    titulos_todos = list(dict.fromkeys(
        ((a or {}).get("titulos") or []) + ((m or {}).get("titulos") or []) + ((j or {}).get("titulos") or [])
    ))
    titulos_alt = [t for t in titulos_todos if t and t != titulo][:8]

    autor = first_truthy((m or {}).get("autor"), (j or {}).get("autor"), (a or {}).get("autor")) or []
    anio = first_truthy((m or {}).get("anio_inicio"), (j or {}).get("anio_inicio"), (a or {}).get("anio_inicio"))
    estado = first_truthy((m or {}).get("estado"), (a or {}).get("estado"), (j or {}).get("estado"))
    pais = first_truthy((m or {}).get("pais_cultura"), (a or {}).get("pais_cultura"))
    seccion = first_truthy((m or {}).get("seccion"), (a or {}).get("seccion"))
    demografico = first_truthy((m or {}).get("demografico"), (j or {}).get("demografico"))
    imagen = first_truthy((m or {}).get("imagen"), (a or {}).get("imagen"), (j or {}).get("imagen"))
    desc = first_truthy((a or {}).get("desc"), (m or {}).get("desc"), (j or {}).get("desc"))
    capitulos = first_truthy((m or {}).get("capitulos_totales"), (j or {}).get("capitulos_totales"), (a or {}).get("capitulos_totales"))
    enlaces = (m or {}).get("enlaces_lectura") or []

    generos_fuente_en = list(dict.fromkeys(
        ((m or {}).get("genres") or []) + ((a or {}).get("genres") or []) +
        ((j or {}).get("genres") or []) + ((j or {}).get("themes") or []) + ((a or {}).get("tags") or [])
    ))

    rating_sugerido = None
    mdx_rating = (m or {}).get("content_rating_raw")
    es_adult = bool((a or {}).get("isAdult")) or bool((j or {}).get("es_hentai"))
    if mdx_rating in ("erotica", "pornographic") or es_adult:
        rating_sugerido = "erotica"
    elif mdx_rating == "suggestive" or (j or {}).get("es_ecchi"):
        rating_sugerido = "sugestiva"

    fuentes = {}
    if a:
        fuentes["anilist"] = {"id": a.get("id"), "url": a.get("url")}
    if m:
        fuentes["mangadex"] = {"id": m.get("id"), "url": m.get("url")}
    if j:
        fuentes["jikan"] = {"id": j.get("id"), "url": j.get("url")}

    return {
        "id": gen_temp_id(),
        "titulo": titulo,
        "titulos_alt": titulos_alt,
        "seccion": seccion or "Sin Clasificación",
        "estado": estado or "",
        "imagen": imagen or "",
        "desc": desc or "",
        "desc_idioma_fuente": "en (sin traducir, revisar antes de publicar)",
        "tags": [],
        "in_biblioteca": False,
        "in_files": False,
        "adult": bool(es_adult),
        "subido": "",
        "tropos": [],
        "personajes": [],
        "enlaces_lectura": enlaces,
        "autor": autor,
        "anio_inicio": anio,
        "content_rating": None,
        "demografico": demografico,
        "pais_cultura": [pais] if pais else [],
        "subgeneros": [],
        "advertencias": [],
        "capitulos_totales": capitulos,
        "generos_fuente_en": generos_fuente_en,
        "content_rating_sugerido": rating_sugerido,
        "fuentes": fuentes,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manga_data_path")
    parser.add_argument("--max-pages-anilist", type=int, default=40)
    parser.add_argument("--max-pages-mangadex", type=int, default=40)
    parser.add_argument("--max-pages-jikan", type=int, default=40)
    args = parser.parse_args()

    with open(args.manga_data_path, encoding="utf-8") as f:
        catalog = json.load(f)

    logfile = open("discover_log.txt", "w", encoding="utf-8")
    log(f"Catalogo actual: {len(catalog)} entradas.", logfile)
    catalog_index = build_catalog_index(catalog)

    log("\n=== Recorriendo AniList (genero: Girls Love) ===", logfile)
    anilist_items = fetch_anilist_gl(args.max_pages_anilist, logfile)

    log("\n=== Recorriendo MangaDex (tag: Girls Love) ===", logfile)
    mangadex_items = fetch_mangadex_gl(args.max_pages_mangadex, logfile)

    log("\n=== Recorriendo Jikan / MyAnimeList (genero: Girls Love) ===", logfile)
    jikan_items = fetch_jikan_gl(args.max_pages_jikan, logfile)

    log(f"\nTotal obras vistas -> AniList: {len(anilist_items)}, MangaDex: {len(mangadex_items)}, Jikan: {len(jikan_items)}", logfile)

    # --- Fusionar por titulo entre las 3 fuentes ---
    clusters = []
    cluster_index = {}
    for it in anilist_items:
        merge_into_clusters(clusters, cluster_index, "anilist", it, it["titulos"])
    for it in mangadex_items:
        merge_into_clusters(clusters, cluster_index, "mangadex", it, it["titulos"])
    for it in jikan_items:
        merge_into_clusters(clusters, cluster_index, "jikan", it, it["titulos"])

    log(f"\nObras unicas tras fusionar fuentes: {len(clusters)}", logfile)

    # --- Descartar lo que ya esta en el catalogo ---
    nuevos = []
    ya_en_catalogo = 0
    for cluster in clusters:
        candidate = build_candidate(cluster)
        titles_to_check = [candidate["titulo"]] + candidate["titulos_alt"]
        dup, matched_title, score = is_in_catalog(titles_to_check, catalog_index)
        if dup:
            ya_en_catalogo += 1
            continue
        nuevos.append(candidate)

    log(f"Ya estaban en tu catalogo (descartados): {ya_en_catalogo}", logfile)
    log(f"Candidatos NUEVOS para revisar: {len(nuevos)}", logfile)

    with open("discoveries_gl_yuri.json", "w", encoding="utf-8") as f:
        json.dump(nuevos, f, ensure_ascii=False, indent=2)

    resumen = ["", "===== RESUMEN POR FUENTE ====="]
    for src, s in STATS.items():
        resumen.append(f"{src}: {s['vistos']} obras vistas, errores={s['errores']}")
    resumen.append("===============================")
    for line in resumen:
        log(line, logfile)

    logfile.close()
    print(f"\nListo. {len(nuevos)} candidatos nuevos en discoveries_gl_yuri.json")
    print(f"({ya_en_catalogo} descartados por ya estar en tu catalogo)")
    print("Revisa discover_log.txt para ver el detalle por fuente.")


if __name__ == "__main__":
    main()
