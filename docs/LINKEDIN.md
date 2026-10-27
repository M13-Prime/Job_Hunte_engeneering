# Canal LinkedIn (optionnel) — agent-reach / mcporter

Désactivé par défaut. Ajoute à l'agent de préparation des outils pour trouver
les bons interlocuteurs sur LinkedIn et les inclure (nom, rôle, URL) dans
`contacts` du rapport.

## Risques

Il utilise une session LinkedIn authentifiée via `mcporter` (cf.
[agent-reach](https://github.com/EdisonChenAI/agent-reach)). C'est contraire
aux CGU de LinkedIn : le compte peut être restreint. Préférez un compte
secondaire. Le client espace les appels, plafonne par jour et s'arrête au
premier blocage/login.

## Activer

1. Construire l'image avec `INSTALL_LINKEDIN=1` (installe Node + `mcporter`) :
   `INSTALL_LINKEDIN=1 docker compose -f docker-compose.lite.yml up -d --build`.
   Puis configurer le serveur `linkedin` (cookies) dans le volume
   `mcporter-config` (monté sur `/home/app/.mcporter`) — voir le README
   d'agent-reach. Non testé de bout en bout : vérifier avec `scripts/doctor.py`.
2. Dans `.env` : `LINKEDIN_ENABLED=true` (+ `LINKEDIN_DAILY_LIMIT`,
   `LINKEDIN_MIN_INTERVAL_SECONDS`).
3. `python scripts/doctor.py` doit afficher `LinkedIn (mcporter)` en OK.

## Outils exposés à l'agent de préparation

`linkedin_search_people`, `linkedin_get_profile`, `keep_contact`. Les contacts
gardés sont ajoutés au rapport (3 max) avec `linkedin_url`.
