# Personnalisation du profil et ingestion d'offres

Deux ajouts inspirés de projets externes (MIT) :
[kevinpz/claude-job-search](https://github.com/kevinpz/claude-job-search)
(profil scoré, dealbreakers, garde-fous CV) et
[agent-reach](https://github.com/EdisonChenAI/agent-reach) (lecteur de pages
Jina Reader, canal LinkedIn — voir `docs/LINKEDIN.md`).

## 1. `config/user_profile.yaml`

Nouveaux champs, tous optionnels :

| Champ | Effet |
|---|---|
| `core_skills` | +15 pts par compétence trouvée dans l'offre (préfiltre gratuit) |
| `bonus_skills` | +5 pts par compétence |
| `dealbreakers` | un seul terme trouvé ⇒ score 0, l'offre n'atteint **jamais** un appel LLM payant |
| `required_employers` | employeurs qui ne peuvent pas disparaître d'un CV adapté |
| `cv_headline` | headline figée sur tous les CV adaptés (`null` = suit l'offre) |

Le barème complet du préfiltre est dans `src/signal_tracker/jobs/relevance.py` :
rôle 30, compétence cœur 15, domaine 10, compétence bonus 5, plafonné à 100.
C'est ce score que `JOBS_AUTO_SCORE_MIN_HEURISTIC` filtre avant la notation IA :
les dealbreakers sont donc aussi un levier de coût, pas seulement de confort.

Les termes sont cherchés en mots entiers (`jobs/filters.keyword_in_text`), donc
`stage` ne matche pas `stagnation`, mais `stage` matche `Stage data engineer`.

## 2. Ajouter une offre depuis n'importe quelle URL

Pour tout ce que les backends ATS ne couvrent pas (LinkedIn, Welcome to the
Jungle, page carrières maison) :

```bash
python scripts/add_job.py "https://.../une-offre" --user-id 1
python scripts/add_job.py URL --title "Data Engineer" --company "Acme" --no-ai
```

La page est lue via **Jina Reader** (`https://r.jina.ai/<url>`) : pas de clé
API, pas de navigateur, pas de login. L'offre est enregistrée comme un
`JobOffer` normal avec `ats="manual"`, scorée par le préfiltre, puis notée par
l'IA si `--user-id` a un CV. Rejouer la même URL met à jour la ligne au lieu
de la dupliquer.

Limites : l'URL demandée transite chez un tiers (Jina), le service est
rate-limité sans clé, et une offre visible seulement après connexion revient
tronquée ou vide. Le titre et l'entreprise sont devinés depuis le markdown
(ligne `Title:` ou premier `#`, découpé sur `-` / `chez` / `at`) : passez
`--title` / `--company` dès que la devinette se trompe.

Pas encore d'écran dans le dashboard : c'est du CLI pour l'instant.
