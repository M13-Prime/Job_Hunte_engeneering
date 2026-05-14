# Signal Tracker

Détecte des **signaux faibles** indiquant qu'une entreprise va probablement
recruter dans les domaines **Sustainability / AI / Data / ESG / CSR**, avant
que les offres ne soient publiées. Envoie chaque matin un digest classé par
priorité, avec entreprise, signal, personne à contacter et angle d'attaque.

> Statut : **Phase 1**. Pipeline RSS -> dedup -> classifier LiteLLM ->
> stockage opérationnel. Le digest email arrive en Phase 3.

## Stack

- Python 3.11+, gestion de deps via [`uv`](https://docs.astral.sh/uv/)
- `feedparser`, `httpx`, `pydantic` v2, `pydantic-settings`, `sqlalchemy` 2.x
- **`litellm`** comme couche LLM unifiée (provider configurable via `.env`)
- `tenacity`, `apscheduler`, `jinja2`, `rich`
- Lint : `ruff`. Type-check : `mypy --strict`. Tests : `pytest`.

## Install

```bash
uv sync --extra dev          # installe le projet + deps dev dans .venv
cp .env.example .env         # renseigne au moins LLM_MODEL et la clé du provider
```

## Premier run (Phase 0)

```bash
make demo
```

Le squelette doit :

1. charger `.env` (via `pydantic-settings`) et `config/user_profile.yaml` ;
1. créer `data/signals.db` (SQLite) et toutes les tables ;
1. insérer un `raw_item` de démo et l'afficher dans un tableau `rich`.

Tu peux aussi lancer les tests :

```bash
make test
make lint
make typecheck
```

## Pipeline (Phase 1)

```bash
make collect            # lit les flux RSS configurés -> data/signals.db
make classify           # classe le backlog via LLM_MODEL
make pipeline           # = collect + classify
make classify -- --limit 5   # ne classer que 5 items (debug / cost cap)
```

- `make collect` fetch chaque flux RSS de `config/sources.yaml` (cache local 6h),
  dédoublonne par SHA256(source+url) et insère dans `raw_items`.
- `make classify` itère sur les `raw_items` non classés, appelle
  `litellm.acompletion` via le wrapper unique `classify()`, valide la sortie
  avec Pydantic et insère un `Signal` quand `is_relevant=True`. Dédup
  hebdomadaire par `(company_normalized, signal_type, ISO week)`.

## Configuration

|Fichier                     |Rôle                                                                 |
|----------------------------|---------------------------------------------------------------------|
|`.env`                      |Secrets et runtime (LLM model, clés API, SMTP, scheduling)           |
|`config/user_profile.yaml`  |Mon profil (domaines, postes ciblés, géo) — utilisé pour le scoring  |
|`config/keywords.yaml`      |Mots-clés par type de signal                                         |
|`config/sources.yaml`       |Sources activées (RSS, GDELT, NewsAPI, Pappers, France Travail, ...) |

## Changer de LLM

Le wrapper passe **exclusivement** par `litellm.acompletion`. Pour switcher
de provider, modifie `.env` — pas de code à toucher :

```bash
# Anthropic (défaut)
LLM_MODEL=anthropic/claude-sonnet-4-5
ANTHROPIC_API_KEY=sk-ant-...

# OpenAI
LLM_MODEL=openai/gpt-4o-mini
OPENAI_API_KEY=sk-...

# Gemini
LLM_MODEL=gemini/gemini-2.0-flash
GEMINI_API_KEY=...

# Local (Ollama)
LLM_MODEL=ollama/llama3.1
OLLAMA_BASE_URL=http://localhost:11434
```

Tu peux aussi définir `LLM_FALLBACK_MODEL` (utilisé par LiteLLM en cas
d'échec du modèle principal).

## Arborescence

```
.
├── src/signal_tracker/
│   ├── config.py              # Settings (.env) + loaders YAML
│   ├── main.py                # Entrée CLI (phase 0 demo)
│   ├── collectors/            # Phase 1+ : RSS, GDELT, NewsAPI, Pappers, ...
│   ├── classifier/            # Phase 1  : wrapper LiteLLM + prompts + schémas
│   ├── storage/               # SQLAlchemy : models.py, db.py
│   ├── notifier/              # Phase 3  : digest Jinja2 + SMTP/Resend
│   └── utils/                 # dedup, normalize, ...
├── config/                    # user_profile.yaml, keywords.yaml, sources.yaml
├── data/                      # SQLite DB
├── scripts/                   # collect / classify / digest / daily
└── tests/
```

## Roadmap

- **Phase 0** — squelette, modèles SQLAlchemy, config, demo. ✅
- **Phase 1** *(en cours)* — collecteur RSS + classifier LiteLLM (pipeline E2E). ✅
- **Phase 2** — Pappers, GDELT, NewsAPI, France Travail (pics de recrutement).
- **Phase 3** — digest email quotidien + APScheduler.
- **Phase 4** — boucle de feedback (relevant / not_relevant / contacted).
- **Phase 5** *(optionnel)* — mini dashboard FastAPI.

## Garde-fous

- **Pas de scraping LinkedIn** ni de sites aux CGU strictes.
- `robots.txt` respecté ; rate limiting (1 req/s par défaut).
- Cache HTTP local de 6h sur les RSS.
- Tous les secrets dans `.env` — jamais en dur.
- Usage personnel, non commercial.
