#!/usr/bin/env bash
# Fonctions communes de publication dans le dépôt public airport-connect-data (ADR-8).
# Sourcé par publish_timetable.sh et publish_plans.sh ; s'exécute dans GitHub Actions.

# commit_and_push <message> <chemin>… : commite les chemins donnés et pousse sur la branche courante,
# en rejouant sur la dernière version distante si un autre traitement a poussé entre-temps.
commit_and_push() {
  local message="$1"; shift
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -- "$@"
  if git diff --cached --quiet; then
    echo "Rien de nouveau à commiter."
    return 0
  fi
  git commit --quiet -m "$message"
  local branch attempt
  branch="$(git rev-parse --abbrev-ref HEAD)"
  for attempt in 1 2 3 4 5; do
    if git push --quiet origin "HEAD:$branch"; then
      echo "Poussé sur $branch : $message"
      return 0
    fi
    echo "Poussée refusée (tentative $attempt) : reprise sur la version distante." >&2
    sleep $((attempt * 5))
    git pull --quiet --rebase origin "$branch"
  done
  echo "Impossible de pousser après 5 tentatives." >&2
  return 1
}
