"""Exécute les requêtes SPARQL de l'appli (assets/sparql) sur QLever, miroir rapide de Wikidata.

Les requêtes sont écrites pour le service officiel (query.wikidata.org). QLever ne connaît pas
le « service de libellés » de Wikidata ni les préfixes implicites : on les ajoute ici.
"""
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPARQL_DIR = ROOT / "sparql"
ENDPOINT = "https://qlever.dev/api/wikidata"
UA = "cycling-data-pack/1.0 (non-commercial)"
ENTITY = "http://www.wikidata.org/entity/"

PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX psn: <http://www.wikidata.org/prop/statement/value-normalized/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
PREFIX pqn: <http://www.wikidata.org/prop/qualifier/value-normalized/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX schema: <http://schema.org/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

_LABEL_SERVICE = re.compile(r"SERVICE\s+wikibase:label\s*\{[^{}]*\}", re.S)


def template(name: str) -> str:
    text = (SPARQL_DIR / f"{name}.rq").read_text(encoding="utf-8")
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def to_qlever(q: str) -> tuple[str, list[str], dict[str, str]]:
    """Retire le service de libellés de Wikidata.

    Renvoie la requête adaptée, la liste des variables d'origine (dans l'ordre) et,
    pour chaque variable « ?xLabel », la variable ?x dont il faut chercher le libellé.
    Les libellés sont ensuite récupérés en lot (voir labels()).
    """
    q = _LABEL_SERVICE.sub("", q)
    sel_start = q.index("SELECT")
    head_end = q.index("WHERE")
    head = q[sel_start:head_end]
    original = re.findall(r"\?(\w+)", head)
    label_of = {}
    for v in original:
        if v.endswith("Label"):
            base = v[: -len("Label")]
            # ?xLabel est fourni par le service seulement si ?x n'est pas lié autrement à un libellé
            if not re.search(rf"\?{v}\b", q[head_end:]):
                label_of[v] = base
    new_head = head
    for v in label_of:
        new_head = re.sub(rf"\s\?{v}\b", "", new_head)
    # La variable dont on veut le libellé doit être renvoyée (ex. ?birthPlace pour ?birthPlaceLabel)
    missing = [b for b in dict.fromkeys(label_of.values()) if b not in original]
    if missing:
        new_head = new_head.rstrip() + " " + " ".join("?" + b for b in missing) + " "
    q = q[:sel_start] + new_head + q[head_end:]
    return PREFIXES + q, original, label_of


def build(name: str, id_: str | None = None, ids: list[str] | None = None) -> str:
    q = template(name)
    if id_:
        q = q.replace("{{ID}}", id_)
    if ids:
        q = q.replace("{{IDS}}", " ".join(f"wd:{i}" for i in ids))
    return q


# Du français d'abord, puis les langues les plus riches en cyclisme
LABEL_LANGS = ["fr", "en", "mul", "it", "es", "nl", "de", "pt"]
_label_cache: dict[str, str | None] = {}


def _post(query: str, tries: int = 6) -> dict:
    data = urllib.parse.urlencode({"query": query}).encode()
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(ENDPOINT, data=data, headers={
                "User-Agent": UA, "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except Exception as e:  # réseau ou surcharge (429) : on patiente de plus en plus
            last = e
            time.sleep(5 * 2 ** attempt)
    raise RuntimeError(str(last))


def labels(ids) -> dict[str, str | None]:
    """Libellés français (à défaut anglais) d'entités, avec cache."""
    todo = [i for i in dict.fromkeys(ids) if i and re.fullmatch(r"Q\d+", i) and i not in _label_cache]
    for k in range(0, len(todo), 400):
        chunk = todo[k:k + 400]
        q = PREFIXES + "SELECT ?x ?l (LANG(?l) AS ?lang) WHERE { VALUES ?x { " + " ".join("wd:" + i for i in chunk) +             " } ?x rdfs:label ?l FILTER(LANG(?l) IN (" + ",".join(f'"{x}"' for x in LABEL_LANGS) + ")) }"
        res = _post(q)
        found: dict[str, dict[str, str]] = {}
        for b in res["results"]["bindings"]:
            found.setdefault(b["x"]["value"][len(ENTITY):], {})[b["lang"]["value"]] = b["l"]["value"]
        for i in chunk:
            f = found.get(i, {})
            _label_cache[i] = next((f[x] for x in LABEL_LANGS if f.get(x)), None)
    return {i: _label_cache.get(i) for i in ids if i}


def run(name: str, id_: str | None = None, ids: list[str] | None = None, tries: int = 6) -> dict:
    """Renvoie {"v": [variables], "r": [[valeurs]]} (format compact de l'instantané)."""
    query, vars_, label_of = to_qlever(build(name, id_, ids))
    try:
        res = _post(query, tries)
    except RuntimeError as e:
        raise RuntimeError(f"{name} {id_}: {e}") from None
    raw = []
    for b in res["results"]["bindings"]:
        row = {}
        for v, x in b.items():
            val = x["value"]
            if val.startswith(ENTITY):
                val = val[len(ENTITY):]
            if x.get("datatype", "").endswith("#boolean"):
                val = "true" if val in ("true", "1") else "false"
            row[v] = val
        raw.append(row)
    if label_of:
        names = labels([r.get(base) for r in raw for base in label_of.values()])
        for r in raw:
            for lv, base in label_of.items():
                if r.get(base):
                    # comme le service Wikidata : l'identifiant brut s'il n'y a aucun libellé
                    r[lv] = names.get(r[base]) or r[base]
    return {"v": vars_, "r": [[r.get(v) for v in vars_] for r in raw]}


# ---------- Requêtes groupées : une requête pour 100 entités au lieu de 100 requêtes ----------

BATCHABLE = {
    "header", "edition_stages", "ranking", "jerseys", "edition_teams",
    "rider", "rider_teams", "rider_wins", "rider_rankings", "rider_starts",
    "team_seasons", "team_names", "team_riders",
}


def _batch_query(name: str, ids: list[str]) -> str:
    q = template(name).replace("wd:{{ID}}", "?_id")
    # plus de LIMIT global (header/rider : on garde la première ligne de chaque entité)
    q = re.sub(r"\}\s*LIMIT\s+\d+\s*$", "}", q.strip())
    sel = q.index("SELECT")
    # sous-requêtes : elles doivent aussi renvoyer et grouper par ?_id
    head, body = q[: sel + 6], q[sel + 6:]
    body = body.replace("SELECT ", "SELECT ?_id ").replace("GROUP BY ", "GROUP BY ?_id ")
    q = head + " ?_id" + body
    where = q.index("{", q.index("WHERE"))
    values = " VALUES ?_id { " + " ".join("wd:" + i for i in ids) + " } "
    return q[: where + 1] + values + q[where + 1:]


def run_batch(name: str, ids: list[str]) -> dict[str, dict]:
    """Exécute une requête pour plusieurs entités ; renvoie {id: {"v":…, "r":…}} (vide si rien)."""
    query, vars_, label_of = to_qlever(_batch_query(name, ids))
    vars_ = [v for v in vars_ if v != "_id"]
    res = _post(query)
    raw = []
    for b in res["results"]["bindings"]:
        row = {}
        for v, x in b.items():
            val = x["value"]
            if val.startswith(ENTITY):
                val = val[len(ENTITY):]
            if x.get("datatype", "").endswith("#boolean"):
                val = "true" if val in ("true", "1") else "false"
            row[v] = val
        raw.append(row)
    if label_of:
        names = labels([r.get(base) for r in raw for base in label_of.values()])
        for r in raw:
            for lv, base in label_of.items():
                if r.get(base):
                    r[lv] = names.get(r[base]) or r[base]
    out = {i: {"v": vars_, "r": []} for i in ids}
    for r in raw:
        i = r.get("_id")
        if i in out:
            row = [r.get(v) for v in vars_]
            if name in ("header", "rider"):  # LIMIT 1 d'origine : toujours la même ligne, d'un jour à l'autre
                if not out[i]["r"] or json.dumps(row) < json.dumps(out[i]["r"][0]):
                    out[i]["r"] = [row]
                continue
            out[i]["r"].append(row)
    return out
