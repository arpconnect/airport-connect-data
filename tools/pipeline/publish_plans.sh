#!/usr/bin/env bash
# Publie le catalogue des plans dans le dépôt public airport-connect-data (ADR-8, spec R-185).
#
#   tools/pipeline/publish_plans.sh <dossier de sortie de fetch_plans.py>
#
# Le catalogue est commité dans published/plans/catalogue.json. En mode lien (distribution « link »,
# par défaut tant que D-9 n'est pas tranchée), l'application télécharge chaque plan à sa source.
# Le mode copie (« mirror ») n'est pas encore publié ici : il demandera de joindre les fichiers à une release.
set -euo pipefail

OUT="${1:?dossier de sortie de fetch_plans.py requis}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=git_publish.sh
. "$HERE/git_publish.sh"

CATALOGUE="$OUT/catalogue.json"
[ -f "$CATALOGUE" ] || { echo "Pas de catalogue.json : rien à publier." >&2; exit 2; }
DISTRIBUTION="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("distribution",""))' "$CATALOGUE")"
[ "$DISTRIBUTION" = "link" ] || { echo "Distribution « $DISTRIBUTION » non publiable ici (D-9) : arrêt." >&2; exit 2; }
VERSION="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$CATALOGUE")"
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "+ catalogue $VERSION vers published/plans/catalogue.json"
  exit 0
fi
mkdir -p published/plans
cp "$CATALOGUE" published/plans/catalogue.json
commit_and_push "Plans : publication du catalogue $VERSION" published/plans/catalogue.json
