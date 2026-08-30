#!/usr/bin/env bash
set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${CYAN}${BOLD}==> Installing Nightwatch (OpenAI Codex Unattended Supervisor)...${NC}"

# Check Python version
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}Error: python3 is required but not found in PATH.${NC}" >&2
    exit 1
fi

PYTHON_MAJOR=$(python3 -c "import sys; print(sys.version_info[0])")
PYTHON_MINOR=$(python3 -c "import sys; print(sys.version_info[1])")

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
    echo -e "${RED}Error: Python 3.11+ is required. Found Python $PYTHON_MAJOR.$PYTHON_MINOR.${NC}" >&2
    exit 1
fi

INSTALL_DIR="${HOME}/.local/share/codex-nightwatch"
REPO_URL="https://github.com/Igzela/codex-nightwatch-lab.git"

if [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "${CYAN}Updating existing installation at ${INSTALL_DIR}...${NC}"
    git -C "$INSTALL_DIR" fetch --depth=1 origin master
    git -C "$INSTALL_DIR" reset --hard origin/master
else
    echo -e "${CYAN}Cloning Nightwatch into ${INSTALL_DIR}...${NC}"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
fi

echo -e "${CYAN}Registering user-local CLI launcher...${NC}"
python3 "$INSTALL_DIR/nightwatch/bin/nightwatch" install

BIN_DIR="${HOME}/.local/bin"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "${RED}Warning: ${BIN_DIR} is not currently in your PATH.${NC}"
    echo -e "Add it to your shell configuration (e.g. ~/.bashrc or ~/.zshrc):"
    echo -e "    ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}\n"
fi

echo -e "${GREEN}${BOLD}✓ Nightwatch installed successfully!${NC}"
echo -e "\nRun ${BOLD}nightwatch doctor${NC} to verify your environment setup."
echo -e "Run ${BOLD}nightwatch --help${NC} for command usage.\n"
