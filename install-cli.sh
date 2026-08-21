#!/usr/bin/env bash
# ==============================================================================
# Blackboard Scraper CLI Global Installer
# Installs 'bbscraper', 'blackboard', and 'bb' into ~/.local/bin/
# ==============================================================================

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"

echo "🎓 Installing Blackboard Scraper Global CLI..."
echo "   ↳ Project Directory: ${PROJECT_DIR}"
echo "   ↳ Target Bin Dir:    ${BIN_DIR}"

# 1. Verify virtual environment python exists
if [ ! -f "${VENV_PYTHON}" ]; then
    echo "❌ Error: Virtual environment not found at ${VENV_PYTHON}"
    echo "   Please create it first: python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
    exit 1
fi

# 2. Ensure target bin directory exists
mkdir -p "${BIN_DIR}"

# 3. Create the master wrapper script
WRAPPER_FILE="${BIN_DIR}/bbscraper"

cat << INNER_EOF > "${WRAPPER_FILE}"
#!/usr/bin/env bash
# Auto-generated Blackboard Scraper Global Launcher

PROJECT_DIR="${PROJECT_DIR}"
VENV_PYTHON="\${PROJECT_DIR}/.venv/bin/python"

if [ ! -f "\${VENV_PYTHON}" ]; then
    echo "❌ Error: Blackboard Scraper virtual environment missing at: \${VENV_PYTHON}" >&2
    exit 1
fi

exec "\${VENV_PYTHON}" "\${PROJECT_DIR}/main.py" "\$@"
INNER_EOF

chmod +x "${WRAPPER_FILE}"

# 4. Create symlinks for aliases
ln -sf "${WRAPPER_FILE}" "${BIN_DIR}/blackboard"
ln -sf "${WRAPPER_FILE}" "${BIN_DIR}/bb"

echo "✨ Successfully installed global commands:"
echo "   • bbscraper  -> ${WRAPPER_FILE}"
echo "   • blackboard -> ${BIN_DIR}/blackboard"
echo "   • bb         -> ${BIN_DIR}/bb"
echo ""
echo "🚀 You can now run 'bbscraper', 'blackboard', or 'bb' from any terminal directory!"
