"""Construit le paquet de données cyclistes publié dans docs/.

Sources : Wikidata (CC0), via le miroir QLever. Aucune donnée n'est publiée si la collecte échoue.
"""
import datetime as dt
import gzip
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import qlever

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
MANIFEST = DOCS / "update.json"
TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=420)).isoformat()
MAX_FAILURE_RATE = 0.10

TDF, GIRO, VUELTA = "Q33881", "Q33861", "Q33937"
MONUMENTS = ["Q33951", "Q33970", "Q33992", "Q34009", "Q34032"]
GRAND_TOURS = [TDF, GIRO, VUELTA]
WORLDS = "Q13603535"
SEIXAS = "Q125848633"
BIG_RACES = GRAND_TOURS + MONUMENTS + [WORLDS]
HISTORIC_ISO = {"Q29999": "NL", "Q172579": "IT", "Q174193": "GB", "Q15180": "RU"}

RACE_QUERIES = ["header", "race_editions", "race_stagewins", "race_gaps", "race_nostagewin", "race_longest_stages"]
EDITION_QUERIES = ["header", "edition_stages", "ranking", "jerseys", "edition_teams"]
RIDER_QUERIES = ["rider", "rider_teams", "rider_wins", "rider_rankings", "rider_starts"]
TEAM_QUERIES = ["header", "team_seasons", "team_names", "team_riders"]
BATCH_SIZE = {"rider_wins": 40, "rider_rankings": 60, "team_riders": 40, "header": 80}

results: dict[str, dict] = {}
calls = failures = 0


def key(name, id_=None, ids=None):
    return f"{name}|{id_ or ''}|{','.join(ids or [])}"


def is_q(x):
    return isinstance(x, str) and x.startswith("Q") and x[1:].isdigit()


def rows(name, id_=None, ids=None):
    r = results.get(key(name, id_, ids))
    return [dict(zip(r["v"], x)) for x in r["r"]] if r else []


def fetch(jobs, label):
    global calls, failures
    todo = [j for j in dict.fromkeys(jobs) if key(*j) not in results]
    tasks, grouped = [], {}
    for n, i, ids in todo:
        if n in qlever.BATCHABLE and i and not ids:
            grouped.setdefault(n, []).append(i)
        else:
            tasks.append(("one", n, i, ids))
    for n, lst in grouped.items():
        size = BATCH_SIZE.get(n, 100)
        tasks += [("batch", n, lst[k:k + size], None) for k in range(0, len(lst), size)]
    print(f"[{label}] {len(todo)} requêtes, {len(tasks)} appels", flush=True)

    def go(t):
        kind, n, i, ids = t
        return t, qlever.run_batch(n, i) if kind == "batch" else qlever.run(n, i, list(ids) if ids else None)

    with ThreadPoolExecutor(2) as pool:
        for fut in as_completed([pool.submit(go, t) for t in tasks]):
            calls += 1
            try:
                (kind, n, i, ids), res = fut.result()
            except Exception as e:
                failures += 1
                print("  échec", str(e)[:120], flush=True)
                continue
            if kind == "batch":
                for one, r in res.items():
                    results[key(n, one)] = r
            else:
                results[key(n, i, ids)] = res


def previous_pack() -> tuple[dict, int]:
    if not MANIFEST.exists():
        return {}, 1
    data = json.loads(MANIFEST.read_text(encoding="utf-8")).get("data")
    if not data:
        return {}, 1
    with gzip.open(DOCS / data["url"], "rt", encoding="utf-8") as f:
        return json.load(f), int(data["version"])


def keep_good_values(old: dict) -> None:
    """Une réponse vide ou amputée de moitié ressemble à une panne : on garde l'ancienne."""
    for k, was in old.items():
        now = results.get(k)
        if now is None or (was["r"] and len(now["r"]) < len(was["r"]) / 2):
            results[k] = was


def nationalities(people) -> dict:
    people = sorted(p for p in people if is_q(p))
    chunks = [tuple(people[i:i + 300]) for i in range(0, len(people), 300)]
    fetch([("nationality", None, c) for c in chunks], "nationalités")
    nat = {}
    for c in chunks:
        by = {}
        for x in rows("nationality", None, c):
            by.setdefault(x["p"], []).append(x)
        for p, rs in by.items():
            iso = next((x["sIso"] for x in rs if x.get("sIso")), None) \
                or next((x["cIso"] for x in rs if x.get("cIso")), None) \
                or next((HISTORIC_ISO[x["c"]] for x in rs if x.get("c") in HISTORIC_ISO), None)
            if iso:
                nat[p] = iso
        results.pop(key("nationality", None, c), None)
    return nat


def summary(old: dict) -> list[str]:
    if not old:
        return ["Résultats, classements et équipes de la saison actualisés"]
    lines, new_results = [], []
    for k, now in results.items():
        if not k.startswith("race_editions|"):
            continue
        was = {tuple(r) for r in old.get(k, {}).get("r", [])}
        for r in now["r"]:
            row = dict(zip(now["v"], r))
            if tuple(r) not in was and row.get("w") and row.get("edLabel"):
                new_results.append(row["edLabel"])
    new_results = list(dict.fromkeys(new_results))
    if new_results:
        extra = f" et {len(new_results) - 3} autres" if len(new_results) > 3 else ""
        lines.append("Résultats : " + ", ".join(new_results[:3]) + extra)
    moved = sum(1 for k, now in results.items()
                if k.startswith("rider_teams|") and k in old and old[k]["r"] != now["r"])
    if moved:
        lines.append(f"{moved} fiche{'s' if moved > 1 else ''} coureur avec un changement d'équipe")
    return lines or ["Classements et fiches actualisés"]


def fingerprint(pack: dict) -> str:
    """Le serveur peut renvoyer les lignes dans un autre ordre : ce n'est pas une nouveauté."""
    entries = {k: [v["v"], sorted(json.dumps(r, ensure_ascii=False) for r in v["r"])]
               for k, v in pack.get("entries", {}).items()}
    canon = json.dumps([entries, pack.get("index"), pack.get("nat")], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def main() -> int:
    old_pack, version = previous_pack()
    old = old_pack.get("entries", {})
    cal = json.loads((ROOT / "races.json").read_text(encoding="utf-8"))
    races = list(dict.fromkeys([r["qid"] for r in cal["races"] if r.get("qid")] + [h["qid"] for h in cal.get("history", [])]))

    fetch([(n, r, None) for r in races for n in RACE_QUERIES] + [
        ("pantheon_gt", None, None), ("pantheon_monuments", None, None), ("pantheon_gt_stages", None, None),
        ("pantheon_worlds", None, None), ("winners_multi", None, tuple(BIG_RACES)),
        ("speeds_multi", None, tuple(GRAND_TOURS + MONUMENTS)),
    ], "courses")

    editions = {}
    for r in races:
        for e in rows("race_editions", r):
            d = (e.get("date") or "")[:10]
            if is_q(e.get("ed")) and d and RECENT <= d <= TODAY.isoformat():
                editions[e["ed"]] = e.get("edLabel")
    fetch([(n, e, None) for e in editions for n in EDITION_QUERIES], "éditions récentes")

    riders = {SEIXAS}
    for e in editions:
        riders |= {x.get("w") for x in rows("edition_stages", e)}
        riders |= {x.get("w") for x in rows("jerseys", e)}
        for x in rows("ranking", e):
            try:
                if float(x.get("rank") or 99) <= 10:
                    riders.add(x.get("r"))
            except ValueError:
                pass
    riders = {r for r in riders if is_q(r)}
    fetch([(n, r, None) for r in sorted(riders) for n in RIDER_QUERIES], "coureurs")

    teams = {x.get("team") for r in riders for x in rows("rider_teams", r)
             if not x.get("end") or x["end"][:10] >= RECENT}
    for e in editions:
        teams |= {x.get("team") for x in rows("edition_teams", e)}
    teams = {t for t in teams if is_q(t)}
    fetch([(n, t, None) for t in sorted(teams) for n in TEAM_QUERIES], "équipes")

    nat = nationalities(riders)
    print(f"{calls} appels, {failures} échecs", flush=True)
    if not calls or failures / calls > MAX_FAILURE_RATE:
        print("Trop d'échecs : rien n'est publié.", flush=True)
        return 1

    # Les libellés manquants en français viennent des autres langues
    for prefix, ids in (("rider", riders), ("header", set(races) | set(editions) | teams)):
        missing = [i for i in ids if (r := results.get(key(prefix, i))) and r["r"] and r["r"][0][r["v"].index("label")] is None]
        names = qlever.labels(missing)
        for i in missing:
            r = results[key(prefix, i)]
            r["r"][0][r["v"].index("label")] = names.get(i)

    index = {}
    for r in races:
        h = (rows("header", r) or [{}])[0]
        if h.get("label"):
            index[r] = [r, h["label"], "race", h.get("desc"), h.get("logo") or h.get("img")]
    for e, label in editions.items():
        if label and is_q(e):
            index[e] = [e, label, "edition", None, None]
    for r in riders:
        h = (rows("rider", r) or [{}])[0]
        if h.get("label"):
            index[r] = [r, h["label"], "rider", h.get("desc"), h.get("img")]
    for t in teams:
        h = (rows("header", t) or [{}])[0]
        if h.get("label"):
            index[t] = [t, h["label"], "team", h.get("desc"), h.get("logo") or h.get("img")]

    keep_good_values(old)
    # Une réponse vide ne doit jamais masquer les données déjà présentes dans l'appli
    for k in [k for k, v in results.items() if not v["r"]]:
        del results[k]
    body ={"entries": results, "index": sorted(index.values(), key=lambda x: x[0]), "nat": dict(sorted(nat.items()))}
    if old_pack and fingerprint(old_pack) == fingerprint(body):
        print("Rien de nouveau.", flush=True)
        return 0

    version += 1
    pack = {"version": version, "date": TODAY.isoformat(), **body}
    DOCS.mkdir(exist_ok=True)
    name = f"data-{version}.json.gz"
    raw = gzip.compress(json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9, mtime=0)
    (DOCS / name).write_bytes(raw)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    manifest["data"] = {"version": version, "date": TODAY.isoformat(), "url": name,
                        "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "summary": summary(old)}
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for f in DOCS.glob("data-*.json.gz"):
        if f.name != name:
            f.unlink()
    print(f"Paquet {version} : {len(results)} réponses, {len(raw) / 1e6:.2f} Mo", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
