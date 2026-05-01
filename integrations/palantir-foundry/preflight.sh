#!/usr/bin/env bash
# preflight.sh — Pre-deployment validation for Palantir Foundry → Veza OAA integration
#
# Usage:
#   ./preflight.sh --all          Run all checks non-interactively; exit 0=pass, 1=fail
#   ./preflight.sh                Show interactive menu
#   ./preflight.sh --env-file /path/to/.env   Override default .env path

set -o pipefail
# NOTE: set -e is intentionally NOT used — each check must run independently

# ── Script context ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SLUG="palantir-foundry"
MAIN_SCRIPT="${SCRIPT_DIR}/palantir_foundry.py"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python3"
LOGS_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="${SCRIPT_DIR}/preflight_${TIMESTAMP}.log"
ENV_FILE="${SCRIPT_DIR}/.env"
ALL_MODE=false

# ── Color helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
TESTS_PASSED=0; TESTS_FAILED=0; TESTS_WARNING=0

pass()  { echo -e "${GREEN}  ✓${NC} $*"; echo "[PASS] $*" >> "${LOG_FILE}"; ((TESTS_PASSED++)); }
fail()  { echo -e "${RED}  ✗${NC} $*"; echo "[FAIL] $*" >> "${LOG_FILE}"; ((TESTS_FAILED++)); }
warn()  { echo -e "${YELLOW}  ⚠${NC} $*"; echo "[WARN] $*" >> "${LOG_FILE}"; ((TESTS_WARNING++)); }
info()  { echo -e "${BLUE}  ℹ${NC} $*"; echo "[INFO] $*" >> "${LOG_FILE}"; }
header(){ echo ""; echo -e "${BLUE}══════════════════════════════════════════${NC}"; echo -e "${BLUE}  $*${NC}"; echo -e "${BLUE}══════════════════════════════════════════${NC}"; echo "--- $* ---" >> "${LOG_FILE}"; }

_mask() {
    local val="$1"
    local len=${#val}
    if [[ $len -le 8 ]]; then
        echo "***"
    else
        echo "${val:0:4}****${val: -4}"
    fi
}

# ── Argument parsing ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)          ALL_MODE=true ;;
        --env-file)     ENV_FILE="$2"; shift ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
    shift
done

# ── Log file init ──────────────────────────────────────────────────────────
mkdir -p "$(dirname "${LOG_FILE}")"
echo "Preflight run started at $(date -u +"%Y-%m-%dT%H:%M:%SZ")" > "${LOG_FILE}"
echo "Env file: ${ENV_FILE}" >> "${LOG_FILE}"

# ══════════════════════════════════════════════════════════════════════════════
# CHECK FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

check_system_requirements() {
    header "1 — System Requirements"

    # Python 3.9+
    if command -v python3 &>/dev/null; then
        PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        PY_MAJOR=$(echo "${PY_VER}" | cut -d. -f1)
        PY_MINOR=$(echo "${PY_VER}" | cut -d. -f2)
        if [[ "${PY_MAJOR}" -gt 3 ]] || { [[ "${PY_MAJOR}" -eq 3 ]] && [[ "${PY_MINOR}" -ge 9 ]]; }; then
            pass "Python ${PY_VER} (≥ 3.9 required)"
        else
            fail "Python ${PY_VER} — 3.9 or higher required"
        fi
    else
        fail "python3 not found in PATH"
    fi

    # pip3
    if command -v pip3 &>/dev/null || python3 -m pip --version &>/dev/null 2>&1; then
        pass "pip3 available"
    else
        fail "pip3 not found — install python3-pip"
    fi

    # curl
    if command -v curl &>/dev/null; then
        pass "curl $(curl --version 2>/dev/null | head -1 | awk '{print $2}')"
    else
        fail "curl not found — required for network checks"
    fi

    # jq (optional)
    if command -v jq &>/dev/null; then
        pass "jq $(jq --version 2>/dev/null)"
    else
        warn "jq not found — optional but useful for payload inspection"
    fi
}

check_python_dependencies() {
    header "2 — Python Dependencies"

    if [[ ! -f "${REQUIREMENTS}" ]]; then
        fail "requirements.txt not found at ${REQUIREMENTS}"
        return
    fi

    PYTHON_BIN="python3"
    if [[ -x "${VENV_PYTHON}" ]]; then
        PYTHON_BIN="${VENV_PYTHON}"
        info "Using venv Python: ${VENV_PYTHON}"
    else
        warn "venv not found at ${SCRIPT_DIR}/venv — using system python3"
    fi

    while IFS= read -r line || [[ -n "$line" ]]; do
        # Strip comments and version specifiers
        pkg=$(echo "${line}" | sed 's/#.*//' | tr -d ' ' | sed 's/[>=<!].*//')
        [[ -z "$pkg" ]] && continue

        # Python import name may differ from package name
        import_name="${pkg//-/_}"
        import_name="${import_name//python_dotenv/dotenv}"
        import_name="${import_name//oaaclient/oaaclient}"

        if "${PYTHON_BIN}" -c "import ${import_name}" 2>/dev/null; then
            ver=$("${PYTHON_BIN}" -c "import importlib.metadata; print(importlib.metadata.version('${pkg}'))" 2>/dev/null || echo "unknown")
            pass "${pkg} ${ver}"
        else
            fail "${pkg} — not importable (run: pip install -r requirements.txt)"
        fi
    done < "${REQUIREMENTS}"
}

_load_env() {
    if [[ -f "${ENV_FILE}" ]]; then
        # Export vars from .env, skipping comments and blanks
        while IFS= read -r line || [[ -n "$line" ]]; do
            line=$(echo "${line}" | sed 's/#.*//' | tr -d ' ')
            [[ -z "$line" ]] && continue
            export "${line?}" 2>/dev/null || true
        done < "${ENV_FILE}"
    fi
}

check_configuration() {
    header "3 — Configuration"

    if [[ -f "${ENV_FILE}" ]]; then
        pass ".env exists at ${ENV_FILE}"
        perms=$(stat -f "%OLp" "${ENV_FILE}" 2>/dev/null || stat -c "%a" "${ENV_FILE}" 2>/dev/null || echo "unknown")
        if [[ "${perms}" == "600" ]]; then
            pass ".env permissions: ${perms} (secure)"
        else
            warn ".env permissions: ${perms} — recommend chmod 600 ${ENV_FILE}"
        fi
    else
        fail ".env not found at ${ENV_FILE} — copy .env.example to .env and fill in values"
        return
    fi

    _load_env

    local all_ok=true
    for var in FOUNDRY_BASE_URL FOUNDRY_API_TOKEN VEZA_URL VEZA_API_KEY; do
        val="${!var:-}"
        if [[ -z "$val" ]]; then
            fail "${var} is not set"
            all_ok=false
        elif echo "${val}" | grep -qiE '^(your_|placeholder|change_me|example)'; then
            fail "${var} appears to be a placeholder value"
            all_ok=false
        else
            # Mask sensitive values
            if echo "${var}" | grep -qE 'PASSWORD|KEY|TOKEN|SECRET'; then
                info "${var} = $(_mask "${val}")"
            else
                info "${var} = ${val}"
            fi
            pass "${var} is set"
        fi
    done
}

check_network_connectivity() {
    header "4 — Network Connectivity"

    _load_env

    # Palantir Foundry connectivity
    FOUNDRY_HOST=$(echo "${FOUNDRY_BASE_URL:-}" | sed 's|https\?://||' | cut -d/ -f1)
    if [[ -n "${FOUNDRY_HOST}" ]]; then
        if curl -s --connect-timeout 10 -o /dev/null -w "%{http_code}" \
               "https://${FOUNDRY_HOST}/api/v2/admin/users?pageSize=1" \
               -H "Authorization: Bearer ${FOUNDRY_API_TOKEN:-}" \
               2>/dev/null | grep -qE '^[2345]'; then
            LATENCY=$(curl -s --connect-timeout 10 -o /dev/null \
                -w "%{time_connect}" \
                "https://${FOUNDRY_HOST}" 2>/dev/null || echo "?")
            pass "TCP reachable: ${FOUNDRY_HOST}:443 (connect latency: ${LATENCY}s)"
        else
            fail "Cannot reach ${FOUNDRY_HOST}:443 — check network/firewall"
        fi
    else
        fail "FOUNDRY_BASE_URL is not set — cannot test Foundry connectivity"
    fi

    # Veza connectivity
    VEZA_HOST=$(echo "${VEZA_URL:-}" | sed 's|https\?://||' | cut -d/ -f1)
    if [[ -n "${VEZA_HOST}" ]]; then
        if curl -s --connect-timeout 10 -o /dev/null -w "%{http_code}" \
               "https://${VEZA_HOST}" 2>/dev/null | grep -qE '^[2345]'; then
            LATENCY=$(curl -s --connect-timeout 10 -o /dev/null \
                -w "%{time_connect}" \
                "https://${VEZA_HOST}" 2>/dev/null || echo "?")
            pass "TCP reachable: ${VEZA_HOST}:443 (connect latency: ${LATENCY}s)"
        else
            fail "Cannot reach ${VEZA_HOST}:443 — check network/firewall"
        fi
    else
        fail "VEZA_URL is not set — cannot test Veza connectivity"
    fi
}

check_api_authentication() {
    header "5 — API Authentication"

    _load_env

    # Palantir Foundry auth test
    if [[ -z "${FOUNDRY_BASE_URL:-}" ]] || [[ -z "${FOUNDRY_API_TOKEN:-}" ]]; then
        fail "FOUNDRY_BASE_URL or FOUNDRY_API_TOKEN not set — skipping Foundry auth test"
    else
        RESP=$(curl -s -o /tmp/foundry_auth_resp.json -w "%{http_code}" \
            --connect-timeout 15 \
            "${FOUNDRY_BASE_URL}/api/v2/admin/users?pageSize=1" \
            -H "Authorization: Bearer ${FOUNDRY_API_TOKEN}" \
            -H "Accept: application/json" 2>/dev/null)
        if [[ "${RESP}" == "200" ]]; then
            pass "Palantir Foundry auth: HTTP 200 OK"
        else
            fail "Palantir Foundry auth: HTTP ${RESP}"
            info "Response preview: $(head -c 200 /tmp/foundry_auth_resp.json 2>/dev/null || echo '(empty)')"
        fi
        rm -f /tmp/foundry_auth_resp.json
    fi

    # Veza API key test
    if [[ -z "${VEZA_URL:-}" ]] || [[ -z "${VEZA_API_KEY:-}" ]]; then
        fail "VEZA_URL or VEZA_API_KEY not set — skipping Veza auth test"
    else
        RESP=$(curl -s -o /tmp/veza_auth_resp.json -w "%{http_code}" \
            --connect-timeout 15 \
            "${VEZA_URL}/api/v1/providers" \
            -H "Authorization: Bearer ${VEZA_API_KEY}" \
            -H "Accept: application/json" 2>/dev/null)
        if [[ "${RESP}" == "200" ]]; then
            pass "Veza API key: HTTP 200 OK"
        else
            fail "Veza API key: HTTP ${RESP}"
            info "Response preview: $(head -c 200 /tmp/veza_auth_resp.json 2>/dev/null || echo '(empty)')"
        fi
        rm -f /tmp/veza_auth_resp.json
    fi
}

check_veza_endpoint_access() {
    header "6 — Veza Endpoint Access"

    _load_env

    if [[ -z "${VEZA_URL:-}" ]] || [[ -z "${VEZA_API_KEY:-}" ]]; then
        fail "VEZA_URL or VEZA_API_KEY not set — skipping Veza endpoint test"
        return
    fi

    # Confirm the key has read permissions via GET /api/v1/providers
    RESP=$(curl -s -o /tmp/veza_ep_resp.json -w "%{http_code}" \
        --connect-timeout 15 \
        "${VEZA_URL}/api/v1/providers?page_size=1" \
        -H "Authorization: Bearer ${VEZA_API_KEY}" \
        -H "Accept: application/json" 2>/dev/null)

    if [[ "${RESP}" == "200" ]]; then
        pass "Veza read access confirmed (GET /api/v1/providers HTTP 200)"
    elif [[ "${RESP}" == "403" ]]; then
        fail "Veza API key lacks read permissions (HTTP 403) — verify key has OAA access"
    else
        fail "Veza endpoint check failed: HTTP ${RESP}"
        info "Response: $(head -c 200 /tmp/veza_ep_resp.json 2>/dev/null || echo '(empty)')"
    fi
    rm -f /tmp/veza_ep_resp.json
}

check_deployment_structure() {
    header "7 — Deployment Structure"

    info "Running as: $(whoami)"

    # Main script
    if [[ -r "${MAIN_SCRIPT}" ]]; then
        pass "Main script readable: ${MAIN_SCRIPT}"
    else
        fail "Main script not found or not readable: ${MAIN_SCRIPT}"
    fi

    # requirements.txt
    if [[ -r "${REQUIREMENTS}" ]]; then
        pass "requirements.txt readable: ${REQUIREMENTS}"
    else
        fail "requirements.txt not found: ${REQUIREMENTS}"
    fi

    # venv
    if [[ -x "${VENV_PYTHON}" ]]; then
        pass "venv Python found: ${VENV_PYTHON}"
    else
        warn "venv not found at ${SCRIPT_DIR}/venv — create with: python3 -m venv ${SCRIPT_DIR}/venv"
    fi

    # logs directory writability
    mkdir -p "${LOGS_DIR}" 2>/dev/null
    if [[ -d "${LOGS_DIR}" ]] && [[ -w "${LOGS_DIR}" ]]; then
        pass "logs/ directory writable: ${LOGS_DIR}"
    else
        fail "logs/ directory not writable: ${LOGS_DIR}"
    fi

    # samples directory
    if [[ -d "${SCRIPT_DIR}/samples" ]]; then
        SAMPLE_COUNT=$(find "${SCRIPT_DIR}/samples" -maxdepth 1 -type f | wc -l | tr -d ' ')
        info "samples/ directory present (${SAMPLE_COUNT} file(s))"
    else
        warn "samples/ directory not found — create it for dry-run testing"
    fi

    # .env.example
    if [[ -f "${SCRIPT_DIR}/.env.example" ]]; then
        pass ".env.example present"
    else
        warn ".env.example missing — should be committed as a credential template"
    fi
}

# ══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS (interactive menu only)
# ══════════════════════════════════════════════════════════════════════════════

show_config() {
    header "Current Configuration"
    _load_env
    for var in FOUNDRY_BASE_URL FOUNDRY_API_TOKEN VEZA_URL VEZA_API_KEY; do
        val="${!var:-<not set>}"
        if echo "${var}" | grep -qE 'PASSWORD|KEY|TOKEN|SECRET'; then
            [[ "${val}" == "<not set>" ]] && echo "  ${var} = <not set>" || echo "  ${var} = $(_mask "${val}")"
        else
            echo "  ${var} = ${val}"
        fi
    done
}

generate_env_template() {
    TARGET="${SCRIPT_DIR}/.env.example"
    if [[ -f "${TARGET}" ]]; then
        info ".env.example already exists at ${TARGET}"
    else
        cat > "${TARGET}" <<'ENVEOF'
# Palantir Foundry – Veza OAA Integration
# Copy this file to .env and fill in your real values.
# chmod 600 .env

FOUNDRY_BASE_URL=https://your-company.palantirfoundry.com
FOUNDRY_API_TOKEN=your_foundry_api_token_here

VEZA_URL=https://your-instance.veza.com
VEZA_API_KEY=your_veza_api_key_here
ENVEOF
        info ".env.example created at ${TARGET}"
    fi
}

install_dependencies() {
    header "Installing Python Dependencies"
    if [[ ! -d "${SCRIPT_DIR}/venv" ]]; then
        info "Creating venv..."
        python3 -m venv "${SCRIPT_DIR}/venv" && pass "venv created" || fail "venv creation failed"
    fi
    "${SCRIPT_DIR}/venv/bin/pip" install --quiet --upgrade pip
    "${SCRIPT_DIR}/venv/bin/pip" install --quiet -r "${REQUIREMENTS}" \
        && pass "Dependencies installed" \
        || fail "pip install failed"
}

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

print_summary() {
    echo ""
    echo -e "${BLUE}══════════════════════════════════════════${NC}"
    echo -e "  Preflight Summary"
    echo -e "${BLUE}══════════════════════════════════════════${NC}"
    echo -e "  ${GREEN}Passed:${NC}   ${TESTS_PASSED}"
    echo -e "  ${RED}Failed:${NC}   ${TESTS_FAILED}"
    echo -e "  ${YELLOW}Warnings:${NC} ${TESTS_WARNING}"
    echo -e "  Log:      ${LOG_FILE}"
    echo -e "${BLUE}══════════════════════════════════════════${NC}"
    echo ""
    {
        echo "--- Summary ---"
        echo "Passed: ${TESTS_PASSED}  Failed: ${TESTS_FAILED}  Warnings: ${TESTS_WARNING}"
        echo "Completed at $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    } >> "${LOG_FILE}"
}

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if [[ "${ALL_MODE}" == "true" ]]; then
    echo ""
    echo "  Palantir Foundry → Veza OAA — Preflight Check (--all)"
    echo "  Log: ${LOG_FILE}"
    echo ""
    check_system_requirements
    check_python_dependencies
    check_configuration
    check_network_connectivity
    check_api_authentication
    check_veza_endpoint_access
    check_deployment_structure
    print_summary

    if [[ "${TESTS_FAILED}" -gt 0 ]]; then
        exit 1
    fi
    exit 0
fi

# Interactive menu
while true; do
    echo ""
    echo -e "${BLUE}  Palantir Foundry → Veza OAA — Preflight Menu${NC}"
    echo "  ─────────────────────────────────────────────"
    echo "  1) System requirements"
    echo "  2) Python dependencies"
    echo "  3) Configuration (.env)"
    echo "  4) Network connectivity"
    echo "  5) API authentication"
    echo "  6) Veza endpoint access"
    echo "  7) Deployment structure"
    echo "  ─────────────────────────────────────────────"
    echo "  8) Run all checks"
    echo "  ─────────────────────────────────────────────"
    echo "  9) Show current config"
    echo " 10) Generate .env.example template"
    echo " 11) Install Python dependencies (venv)"
    echo "  ─────────────────────────────────────────────"
    echo "  0) Exit"
    echo ""
    IFS= read -r -p "  Select [0-11]: " choice </dev/tty

    case "${choice}" in
        1) TESTS_PASSED=0; TESTS_FAILED=0; TESTS_WARNING=0; check_system_requirements; print_summary ;;
        2) TESTS_PASSED=0; TESTS_FAILED=0; TESTS_WARNING=0; check_python_dependencies; print_summary ;;
        3) TESTS_PASSED=0; TESTS_FAILED=0; TESTS_WARNING=0; check_configuration; print_summary ;;
        4) TESTS_PASSED=0; TESTS_FAILED=0; TESTS_WARNING=0; check_network_connectivity; print_summary ;;
        5) TESTS_PASSED=0; TESTS_FAILED=0; TESTS_WARNING=0; check_api_authentication; print_summary ;;
        6) TESTS_PASSED=0; TESTS_FAILED=0; TESTS_WARNING=0; check_veza_endpoint_access; print_summary ;;
        7) TESTS_PASSED=0; TESTS_FAILED=0; TESTS_WARNING=0; check_deployment_structure; print_summary ;;
        8)
            TESTS_PASSED=0; TESTS_FAILED=0; TESTS_WARNING=0
            check_system_requirements
            check_python_dependencies
            check_configuration
            check_network_connectivity
            check_api_authentication
            check_veza_endpoint_access
            check_deployment_structure
            print_summary
            ;;
        9)  show_config ;;
        10) generate_env_template ;;
        11) install_dependencies ;;
        0)  echo "Exiting."; exit 0 ;;
        *)  echo "Invalid choice. Enter 0-11." ;;
    esac
done
