#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${CYAN}${BOLD}==> Installing Nightwatch (OpenAI Codex Unattended Supervisor)...${NC}"

if ! command -v node >/dev/null 2>&1; then
    echo -e "${RED}Error: Node.js 22+ is required but not found in PATH.${NC}" >&2
    exit 1
fi

NODE_MAJOR=$(node -p "process.versions.node.split('.')[0]")
if [ "$NODE_MAJOR" -lt 22 ]; then
    echo -e "${RED}Error: Node.js 22+ is required. Found Node $(node -v).${NC}" >&2
    exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
    echo -e "${RED}Error: npm is required but not found in PATH.${NC}" >&2
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

echo -e "${CYAN}Building the TypeScript control plane...${NC}"
(
  cd "$INSTALL_DIR"
  npm install
  npm run build
  npm prune --omit=dev
)

BIN_DIR="${HOME}/.local/bin"
mkdir -p "$BIN_DIR"
ln -sfn "$INSTALL_DIR/dist/cli.js" "$BIN_DIR/nightwatch"
chmod +x "$INSTALL_DIR/dist/cli.js"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo -e "${RED}Warning: ${BIN_DIR} is not currently in your PATH.${NC}"
    echo -e "Add it to your shell configuration (e.g. ~/.bashrc or ~/.zshrc):"
    echo -e "    ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}\n"
fi

echo -e "${GREEN}${BOLD}✓ Nightwatch installed successfully!${NC}"
echo -e "\nRun ${BOLD}nightwatch doctor${NC} to verify your environment setup."
echo -e "Run ${BOLD}nightwatch${NC} (or ${BOLD}nightwatch web${NC}) to open the browser control plane.\n"
