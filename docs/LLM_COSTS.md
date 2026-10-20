# Coût des LLM : le bon modèle pour chaque tâche

*Prix relevés le 20 octobre 2026 (USD par million de tokens). Ils bougent souvent : la page **Admin → Coût IA** montre ce que chaque tâche coûte réellement.*

## Ce que fait l'outil, et ce dont chaque tâche a besoin

| Tâche (`LLM_MODEL_…`) | Volume | Ce qui compte | Modèle conseillé |
|---|---|---|---|
| `CLASSIFY` : classer les articles en signaux | 500 à 900 appels par recherche | du jugement simple, en volume | **Claude Haiku 5.5** |
| `PREFILTER` : oui / non / peut-être avant de classer | idem, réponse de 30 tokens | coût minimal | **Claude Haiku 5.5** |
| `EXTRACT` : profil du CV, score de match des offres, choix des sources | quelques dizaines par jour | extraire proprement du JSON | **Claude Haiku 5.5** |
| `WRITE` : rapport de préparation, CV adapté et lettre | quelques appels par semaine | la qualité d'écriture, zéro invention | **Claude Sonnet 5.5** |
| `AGENT` : agents autonomes (désactivés par défaut) | rare | l'enchaînement d'outils | **Claude Sonnet 5.5** |

Le volume est entièrement dans la classification. La qualité qui se voit est dans l'écriture. On met donc le petit modèle là où il y a du volume, et le bon modèle là où il y a du texte qui part chez un recruteur.

## Ordres de grandeur

Base : 700 articles par recherche ; par article, environ 800 tokens d'article, 2 500 tokens de consignes (mises en cache) et 250 tokens de réponse.

| Modèle de classification | Prix entrée / sortie | Par recherche |
|---|---|---|
| Claude Sonnet 4.5 (ancien défaut du repo) | 3,00 / 15,00 | ≈ 4,80 $ |
| Claude Sonnet 5.5 | 2,00 / 10,00 | ≈ 3,00 $ |
| **Claude Haiku 5.5** | **0,10 / 0,50** | **≈ 0,16 $** |
| Gemini 2.5 Flash-Lite | 0,10 / 0,40 | ≈ 0,14 $ |
| Mistral Small 4 | 0,15 / 0,60 | ≈ 0,45 $ |

Autres postes :
- un CV adapté + lettre coûte environ 0,04 $ avec Sonnet 5.5, contre 0,06 $ avec Sonnet 4.5 ;
- le score de match d'une offre coûte environ 0,0004 $ avec Haiku 5.5, contre 0,012 $ avec Sonnet 4.5.

Avec une recherche par jour, la facture passe d'environ 100 à 150 $ par mois à **moins de 10 $**.

## Ce qui baisse encore la facture, sans IA

1. **Le filtre par mots-clés (gratuit).** Un article qui ne mentionne ni tes mots-clés, ni ton profil, ni les déclencheurs de `config/keywords.yaml` n'est jamais envoyé à l'IA. C'est `KEYWORD_PREFILTER_ENABLED=true` (activé par défaut dès que tu as des mots-clés). La part écartée s'affiche dans l'historique des recherches (« écartés sans IA »).
2. **Le cache des consignes.** Il est déjà activé pour Anthropic (`LLM_PROMPT_CACHE_ENABLED`) : les 2 500 tokens de consignes sont facturés 10 fois moins à partir du 2e article.
3. **Le profil compact du CV.** Il est calculé une fois à l'envoi du CV, puis réutilisé : environ 80 % de tokens en moins par préparation.
4. **Le préfiltre IA** (`LLM_PREFILTER_ENABLED=true`). Un appel très court avec Haiku élimine le bruit avant la classification complète. Il n'est rentable que si `CLASSIFY` reste sur un modèle plus cher que `PREFILTER`.

## Configuration recommandée (`.env`)

```bash
LLM_MODEL=anthropic/claude-sonnet-5-5          # écriture + agents
LLM_CHEAP_MODEL=anthropic/claude-haiku-5-5     # extraction + préfiltre
LLM_MODEL_CLASSIFY=anthropic/claude-haiku-5-5  # le gros volume
LLM_FALLBACK_MODEL=                            # optionnel, voir plus bas
KEYWORD_PREFILTER_ENABLED=true
LLM_PREFILTER_ENABLED=false                    # inutile quand CLASSIFY est déjà sur Haiku
```

Une seule clé API (Anthropic) suffit pour tout ça.

## Les autres fournisseurs (via LiteLLM, sans changer le code)

Il suffit de mettre le nom du modèle LiteLLM et la clé correspondante dans `.env` :

| Option | Exemple | À savoir |
|---|---|---|
| Google Gemini | `LLM_MODEL_CLASSIFY=gemini/gemini-2.5-flash-lite` + `GEMINI_API_KEY` | Le moins cher sur le papier. Sur l'offre gratuite, Google peut utiliser les données envoyées : à éviter pour tout ce qui contient ton CV (`EXTRACT`, `WRITE`). |
| Mistral | `LLM_FALLBACK_MODEL=mistral/mistral-small-latest` + `MISTRAL_API_KEY` | Hébergé dans l'UE, RGPD. Bon modèle de secours si Anthropic est indisponible. |
| DeepSeek | `deepseek/deepseek-chat` + clé DeepSeek | Très bon marché, mais données hébergées en Chine : pas pour le CV. |
| OpenAI | `openai/…` + `OPENAI_API_KEY` | Équivalent en prix sur les petits modèles. |

Vérifie le nom exact du modèle dans la documentation LiteLLM avant de l'utiliser : les fournisseurs renomment souvent leurs versions.

## Vérifier qu'on ne perd pas en qualité

1. Passe `LLM_MODEL_CLASSIFY` sur Haiku 5.5 et garde tout le reste tel quel.
2. Pendant une semaine, donne ton avis sur les signaux (Pertinent / Écarter).
3. Si la part de signaux « écartés » augmente nettement, ou si des entreprises de ta watchlist disparaissent, remets Sonnet 5.5 sur `CLASSIFY` seulement. Le gain reste important sur les autres tâches.
4. Comparer dans **Admin → Coût IA** (filtre 7 jours) avant et après.

## Piste suivante, non branchée

L'API Batch d'Anthropic (−50 %) conviendrait à la recherche quotidienne planifiée, qui n'a pas besoin d'une réponse immédiate. La recherche lancée depuis le dashboard, elle, doit rester synchrone.
