#!/usr/bin/env bash
# Publie une base d'horaires validée dans le dépôt public airport-connect-data (ADR-8, spec R-172).
#
#   tools/pipeline/publish_timetable.sh <dossier de sortie du générateur>
#
# Environnement (fourni par GitHub Actions) : GH_TOKEN, GITHUB_REPOSITORY. DRY_RUN=1 n'envoie rien.
#
# Ordre imposé (R-172) :
#   1. la base, en pièce jointe d'une release « timetable-<version> » jamais réécrite ;
#   2. vérification de l'adresse publique : téléchargement et empreinte SHA-256 ;
#   3. le manifeste, commité dans published/timetable/manifest.json.
# Un client ne voit donc jamais un manifeste qui pointe vers un fichier absent ou différent.
# Les 8 releases les plus récentes sont gardées, pour les appareils qui terminent un téléchargement.
set -euo pipefail

OUT="${1:?dossier de sortie du générateur requis}"
KEEP_RELEASES=8
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=git_publish.sh
. "$HERE/git_publish.sh"

MANIFEST="$OUT/manifest.json"
[ -f "$MANIFEST" ] || { echo "Pas de manifest.json : la construction a échoué, rien à publier." >&2; exit 2; }
field() { python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): d=d[k]
print(d)' "$MANIFEST" "$1"; }
VERSION="$(field version)"
FILE_NAME="$(field file.name)"
FILE_URL="$(field file.url)"
EXPECTED_SHA="$(field file.sha256_gzip)"
TAG="timetable-$VERSION"

[ "$(sha256sum "$OUT/$FILE_NAME" | cut -d' ' -f1)" = "$EXPECTED_SHA" ] || { echo "Empreinte incohérente pour $FILE_NAME" >&2; exit 2; }
case "$FILE_URL" in
  "https://github.com/${GITHUB_REPOSITORY:?}/releases/download/$TAG/$FILE_NAME") ;;
  *) echo "Adresse inattendue dans le manifeste : $FILE_URL" >&2; exit 2 ;;
esac
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "+ release $TAG avec $FILE_NAME ; manifeste vers published/timetable/manifest.json"
  exit 0
fi

# 1. Base en pièce jointe d'une release immuable (une relance réutilise la release existante).
if gh release view "$TAG" >/dev/null 2>&1; then
  echo "Release $TAG déjà présente : vérification de la pièce jointe."
else
  gh release create "$TAG" "$OUT/$FILE_NAME" --title "Horaires $VERSION" --latest=false \
    --notes "Base d'horaires Airport Connect $VERSION, construite depuis le GTFS d'Île-de-France Mobilités (Licence Mobilités). Le manifeste publié est published/timetable/manifest.json."
fi

# 2. L'adresse publique doit servir exactement le fichier attendu (sans décompression à la volée).
tmp="$(mktemp -d)"
curl --fail --silent --show-error --location --retry 5 --retry-delay 10 -o "$tmp/$FILE_NAME" "$FILE_URL"
[ "$(sha256sum "$tmp/$FILE_NAME" | cut -d' ' -f1)" = "$EXPECTED_SHA" ] || { echo "Le fichier publié ne correspond pas au manifeste" >&2; exit 2; }
rm -rf "$tmp"

# 3. Manifeste versionné dans le dépôt : la bascule est atomique (un commit).
mkdir -p published/timetable
cp "$MANIFEST" published/timetable/manifest.json
commit_and_push "Horaires : publication $VERSION" published/timetable/manifest.json

# 4. Ménage : seules les releases d'horaires les plus récentes sont gardées.
gh release list --limit 200 --json tagName,createdAt \
  --jq '[.[] | select(.tagName | startswith("timetable-"))] | sort_by(.createdAt) | reverse | .['"$KEEP_RELEASES"':] | .[].tagName' |
while read -r old; do
  if [ -n "$old" ] && [ "$old" != "$TAG" ]; then
    gh release delete "$old" --cleanup-tag --yes
    echo "Release supprimée : $old"
  fi
done
echo "Publié : $TAG"
