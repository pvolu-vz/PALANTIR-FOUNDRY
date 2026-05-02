#!/usr/bin/env bash
# install_palantir_foundry.sh — One-command installer for Palantir Foundry–Veza OAA integration
set -uo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
REPO_URL="${REPO_URL:-https://github.com/pvolu-vz/PALANTIR-FOUNDRY.git}"
BRANCH="${BRANCH:-main}"
INTEGRATION_SUBDIR="integrations/palantir-foundry"
INSTALL_DIR="${INSTALL_DIR:-/opt/VEZA/palantir-foundry-veza}"
SCRIPTS_DIR="${INSTALL_DIR}/scripts"
LOGS_DIR="${INSTALL_DIR}/logs"
NON_INTERACTIVE=false
OVERWRITE_ENV=false

# ---------------------------------------------------------------------------
# Colors / logging helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive)  NON_INTERACTIVE=true ;;
        --overwrite-env)    OVERWRITE_ENV=true ;;
        --install-dir)      INSTALL_DIR="$2"; SCRIPTS_DIR="${INSTALL_DIR}/scripts"; LOGS_DIR="${INSTALL_DIR}/logs"; shift ;;
        --repo-url)         REPO_URL="$2"; shift ;;
        --branch)           BRANCH="$2"; shift ;;
        *) die "Unknown flag: $1" ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Detect OS / package manager
# ---------------------------------------------------------------------------
OS_ID=""
PKG_MGR=""

if [[ -f /etc/os-release ]]; then
    OS_ID=$(grep '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"')
fi

if command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
elif command -v apt-get &>/dev/null; then
    PKG_MGR="apt-get"
else
    warn "No supported package manager found (dnf/yum/apt-get). Assuming dependencies are pre-installed."
fi

_install_pkg() {
    local pkg="$1"
    if [[ -z "${PKG_MGR}" ]]; then
        warn "Cannot install ${pkg} — no package manager detected."
        return
    fi
    info "Installing ${pkg}..."
    case "${PKG_MGR}" in
        dnf|yum) "${PKG_MGR}" install -y "${pkg}" >/dev/null ;;
        apt-get) apt-get install -y "${pkg}" >/dev/null ;;
    esac
}

# ---------------------------------------------------------------------------
# System dependency checks
# ---------------------------------------------------------------------------
info "Checking system dependencies..."

command -v git &>/dev/null || _install_pkg git

if ! command -v curl &>/dev/null; then
    if [[ "${OS_ID}" == "amzn" ]]; then
        warn "Skipping curl install on Amazon Linux (curl-minimal conflict)"
    else
        _install_pkg curl
    fi
fi

command -v python3 &>/dev/null || _install_pkg python3
python3 -m pip --version &>/dev/null || _install_pkg python3-pip

# venv check — python3-venv is not a separate package on Amazon Linux 2023 / RHEL 9+
if ! python3 -m venv --help &>/dev/null 2>&1; then
    case "${PKG_MGR}" in
        dnf|yum) _install_pkg python3-virtualenv ;;
        apt-get) _install_pkg python3-venv ;;
    esac
fi

# ---------------------------------------------------------------------------
# Python version check (>= 3.8)
# ---------------------------------------------------------------------------
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "${PYTHON_VERSION}" | cut -d. -f1)
PYTHON_MINOR=$(echo "${PYTHON_VERSION}" | cut -d. -f2)

if [[ "${PYTHON_MAJOR}" -lt 3 ]] || { [[ "${PYTHON_MAJOR}" -eq 3 ]] && [[ "${PYTHON_MINOR}" -lt 8 ]]; }; then
    die "Python >= 3.8 is required (found ${PYTHON_VERSION})"
fi
ok "Python ${PYTHON_VERSION} detected"

# ---------------------------------------------------------------------------
# Create directory layout
# ---------------------------------------------------------------------------
info "Creating installation directories..."
mkdir -p "${SCRIPTS_DIR}" "${LOGS_DIR}"
ok "Directories created at ${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Clone repo and copy integration files
# ---------------------------------------------------------------------------
info "Cloning integration from ${REPO_URL} (branch: ${BRANCH})..."
tmp_dir=$(mktemp -d)
trap 'rm -rf "${tmp_dir}"' EXIT

GIT_TERMINAL_PROMPT=0 git clone --branch "${BRANCH}" --depth 1 --single-branch \
    "${REPO_URL}" "${tmp_dir}" 2>/dev/null || die "git clone failed — check REPO_URL and BRANCH"

if [[ ! -d "${tmp_dir}/${INTEGRATION_SUBDIR}" ]]; then
    die "Integration directory '${INTEGRATION_SUBDIR}' not found in repo"
fi

cp -f "${tmp_dir}/${INTEGRATION_SUBDIR}/palantir_foundry.py" "${SCRIPTS_DIR}/"
cp -f "${tmp_dir}/${INTEGRATION_SUBDIR}/requirements.txt"   "${SCRIPTS_DIR}/"
ok "Integration files copied to ${SCRIPTS_DIR}"

# ---------------------------------------------------------------------------
# Python virtual environment
# ---------------------------------------------------------------------------
info "Creating Python virtual environment..."
python3 -m venv "${SCRIPTS_DIR}/venv"

info "Installing Python dependencies..."
"${SCRIPTS_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${SCRIPTS_DIR}/venv/bin/pip" install --quiet -r "${SCRIPTS_DIR}/requirements.txt"
ok "Dependencies installed"

# ---------------------------------------------------------------------------
# .env generation
# ---------------------------------------------------------------------------
ENV_FILE="${SCRIPTS_DIR}/.env"

if [[ -f "${ENV_FILE}" ]] && [[ "${OVERWRITE_ENV}" == "false" ]]; then
    warn ".env already exists at ${ENV_FILE} — skipping (use --overwrite-env to replace)"
else
    if [[ "${NON_INTERACTIVE}" == "true" ]]; then
        # CI / non-interactive mode: read from environment variables
        : "${VEZA_URL:?VEZA_URL is required in non-interactive mode}"
        : "${VEZA_API_KEY:?VEZA_API_KEY is required in non-interactive mode}"
        : "${FOUNDRY_BASE_URL:?FOUNDRY_BASE_URL is required in non-interactive mode}"
        : "${FOUNDRY_API_TOKEN:?FOUNDRY_API_TOKEN is required in non-interactive mode}"
        _veza_url="${VEZA_URL}"
        _veza_api_key="${VEZA_API_KEY}"
        _foundry_base_url="${FOUNDRY_BASE_URL}"
        _foundry_api_token="${FOUNDRY_API_TOKEN}"
    else
        echo ""
        info "Configure credentials (values will be written to ${ENV_FILE}):"

        IFS= read -r -p "  Veza tenant URL (e.g. https://your-company.veza.com): " _veza_url </dev/tty
        IFS= read -r -s -p "  Veza API key: " _veza_api_key </dev/tty; echo >/dev/tty

        IFS= read -r -p "  Palantir Foundry base URL (e.g. https://your-company.palantirfoundry.com): " _foundry_base_url </dev/tty
        IFS= read -r -s -p "  Palantir Foundry API token: " _foundry_api_token </dev/tty; echo >/dev/tty
    fi

    cat > "${ENV_FILE}" <<EOF
# Palantir Foundry – Veza OAA Integration
# Generated by install_palantir_foundry.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Permissions are set to 600 — do not commit this file to version control.

# Palantir Foundry Configuration
FOUNDRY_BASE_URL=${_foundry_base_url}
FOUNDRY_API_TOKEN=${_foundry_api_token}

# Veza Configuration
VEZA_URL=${_veza_url}
VEZA_API_KEY=${_veza_api_key}
EOF

    chmod 600 "${ENV_FILE}"
    ok ".env written and secured (chmod 600)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================================"
ok "Palantir Foundry–Veza OAA integration installed!"
echo "========================================================"
echo ""
echo "  Install path : ${INSTALL_DIR}"
echo "  Script       : ${SCRIPTS_DIR}/palantir_foundry.py"
echo "  Config       : ${ENV_FILE}"
echo "  Logs         : ${LOGS_DIR}"
echo ""
echo "  Next steps:"
echo "    1. Review / update credentials in ${ENV_FILE}"
echo "    2. Run a dry-run to validate:"
echo ""
echo "       cd ${SCRIPTS_DIR}"
echo "       ./venv/bin/python3 palantir_foundry.py --dry-run --save-json --log-level DEBUG"
echo ""
echo "    3. When ready, push to Veza:"
echo ""
echo "       ./venv/bin/python3 palantir_foundry.py --log-level INFO"
echo ""
