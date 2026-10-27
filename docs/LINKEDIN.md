# Canal LinkedIn (optionnel) — agent-reach / mcporter

Désactivé par défaut. Donne à Signal Tracker un accès LinkedIn à trois choses :

- **Actualités** : publications récentes des pages entreprise surveillées,
  collectées comme n'importe quelle source et classées par le LLM
  (`config/sources.yaml` → section `linkedin`).
- **Offres** : recherche d'offres LinkedIn et lecture d'une offre (équipe,
  département, recruteur si affiché) — outils de l'agent de préparation.
- **Personnes** : recherche de décideurs + lecture d'un profil ; les contacts
  vérifiés (3 max, avec leur département) sont ajoutés au rapport avec
  `linkedin_url`.

## Risques

Session LinkedIn authentifiée, contraire aux CGU de LinkedIn : le compte peut
être restreint. Utilisez un compte secondaire. Le client espace les appels,
plafonne par jour et s'arrête au premier blocage / écran de login.

## Installation

Le backend est [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server)
(publié sous `mcp-server-linkedin`, ex-`linkedin-scraper-mcp`), exposé via
`mcporter`. Il pilote un Chromium (Patchright) et garde un profil de
navigateur persistant dans `~/.linkedin-mcp/profile/` — plus de `session.json`
ni de variable `LINKEDIN_COOKIE` depuis février 2026.

```bash
npm install -g mcporter
uvx patchright install chromium
uvx mcp-server-linkedin --login     # fenêtre de navigateur, ~5 min pour 2FA/captcha
uvx mcp-server-linkedin --transport streamable-http --port 8099
mcporter config add linkedin http://127.0.0.1:8099/mcp
```

`--login` a besoin d'un écran : faites-le sur votre poste, pas en Docker ni en
SSH sans affichage, puis réutilisez `~/.linkedin-mcp`.

Image Docker : construire avec `INSTALL_LINKEDIN=1` installe Node + `mcporter`
dans l'image ; le profil et la config se montent via les volumes
`mcporter-config` (`/home/app/.mcporter`) et `linkedin-profile`
(`/home/app/.linkedin-mcp`). Le serveur LinkedIn lui-même (Chromium) reste à
faire tourner à côté : sur une e2-micro (1 Go), lancez-le ponctuellement
plutôt qu'en permanence.

## Activer

1. `.env` : `LINKEDIN_ENABLED=true` (+ `LINKEDIN_DAILY_LIMIT`,
   `LINKEDIN_MIN_INTERVAL_SECONDS`, `LINKEDIN_MCPORTER_BIN`).
2. Actualités : dans `config/sources.yaml`, `linkedin.enabled: true` et
   quelques `companies` (`name` + `slug` de `linkedin.com/company/<slug>`).
3. `python scripts/doctor.py` → ligne `LinkedIn (mcporter)` en OK.

## Vérifier les noms d'arguments

Les noms d'outils suivent ceux du serveur ci-dessus (`get_person_profile`,
`get_company_profile`, `get_company_posts`, `search_jobs`, `get_job_details`),
mais **les noms d'arguments n'ont pas été vérifiés** contre une installation
réelle. Avant la première utilisation :

```bash
python scripts/linkedin_probe.py
```

Il affiche les schémas réels ; ajustez `src/signal_tracker/linkedin/client.py`
si un argument diffère (p. ex. `linkedin_url` vs `company_url`).

## Outils exposés à l'agent de préparation

`linkedin_search_people`, `linkedin_get_profile`, `linkedin_company_profile`,
`linkedin_company_posts`, `linkedin_search_jobs`, `linkedin_job_details`,
`keep_contact`. L'agent est limité à ~5 appels LinkedIn par préparation.

Les offres LinkedIn ne remontent pas (encore) dans la page Offres : elles sont
lues à la demande par l'agent. Les offres de la page Offres viennent des ATS
et, en option, de JobSpy (`JOBSPY_ENABLED`).
