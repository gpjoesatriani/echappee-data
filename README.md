# Données cyclistes

Paquet de résultats, classements, coureurs et équipes, reconstruit chaque nuit à partir de [Wikidata](https://www.wikidata.org/) (CC0), via le miroir [QLever](https://qlever.dev/).

- `build_pack.py` : collecte et contrôle des données.
- `docs/update.json` : dernière version publiée, avec son empreinte SHA-256.
- `docs/data-N.json.gz` : le paquet de données.

Rien n'est publié si plus de 10 % des requêtes échouent.
