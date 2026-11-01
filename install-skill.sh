#!/usr/bin/env bash
# ============================================================================
# Instalador de la skill «teltonika-commands» (teltonika-commands) — Español
# ============================================================================
# Este script instala la skill del repositorio en tu agente de código usando
# el Skills CLI estándar (npx skills, de skills.sh / vercel-labs).
#
# USO:
#   ./install-skill.sh                          # Instalación INTERACTIVA: elige
#                                               #   agente y ámbito (proyecto/global)
#   ./install-skill.sh --list                   # Solo lista las skills del repo
#   ./install-skill.sh --global                 # Ámbito GLOBAL (~/<agente>/skills)
#   ./install-skill.sh --global -y              # Global sin preguntas (CI/CD)
#   ./install-skill.sh --agent claude-code      # Instalar solo para Claude Code
#   ./install-skill.sh --copy                   # Copiar archivos (sin symlinks)
#
# OPCIONES DE ÁMBITO (lo que recuerdas de la instalación de skills):
#   (por defecto)  → PROYECTO: se instala en ./<agente>/skills/ del proyecto
#                    en el que estés; se comparte con tu equipo vía git.
#   --global (-g)  → GLOBAL: se instala en ~/<agente>/skills/ y queda
#                    disponible en TODOS tus proyectos de ese equipo.
#
# Requisitos: Node.js + npx (npx descarga el CLI sin instalar nada globalmente).
# Requiere el origen git correcto en REPO (déjalo apuntando al repo compartido).
# ============================================================================
set -euo pipefail

REPO="${SKILL_REPO:-CUBO-IA/Claude_workshop_skill}"
SKILL_NAME="teltonika-commands"

uso() {
  sed -n '1,40p' "$(realpath "$0")"; exit 0
}

FLAGS=(add "$REPO" --skill "$SKILL_NAME" -y)
MODO="proyecto"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) uso ;;
    --list|-l) FLAGS=(add "$REPO" --skill "$SKILL_NAME" --list); MODO="solo listado"; shift ;;
    -g|--global) FLAGS+=("-g"); MODO="global" ;;
    --copy) FLAGS+=("--copy") ;;
    --agent) shift; FLAGS+=("-a" "$1"); shift ;;
    *) echo "Opción no reconocida: $1 (mira --help)"; exit 2 ;;
  esac
done

if ! command -v npx >/dev/null 2>&1; then
  echo "Node.js (npx) no está instalado. Instala Node 18+ y vuelve a probar:" >&2
  echo "  https://nodejs.org  |  Ubuntu: sudo apt install nodejs npm" >&2
  exit 1
fi

echo "Repo:    $REPO"
echo "Skill:   $SKILL_NAME"
echo "Scope:   $MODO"
echo "Running: npx skills ${FLAGS[*]}"
echo
exec npx -y skills "${FLAGS[@]}"
