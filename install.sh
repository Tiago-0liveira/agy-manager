#!/usr/bin/env bash
# agym Linux / macOS One-Line Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/Tiago-0liveira/agy-manager/main/install.sh | bash

set -e

REPO="Tiago-0liveira/agy-manager"
INSTALL_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/agym"
BIN_DIR="${HOME}/.local/bin"
EXE_PATH="${BIN_DIR}/agym"

echo "================================================="
echo "   Installing agym (Antigravity Profile Manager) "
echo "================================================="

mkdir -p "${BIN_DIR}"

INSTALLED=0

OS="$(uname -s)"
ARCH="$(uname -m)"

TARGET_ASSET=""
case "${OS}" in
  Linux)
    case "${ARCH}" in
      x86_64) TARGET_ASSET="agym-linux-amd64" ;;
      *) echo "Notice: Unrecognized Linux arch: ${ARCH}" ;;
    esac
    ;;
  Darwin)
    case "${ARCH}" in
      arm64) TARGET_ASSET="agym-darwin-arm64" ;;
      x86_64) TARGET_ASSET="agym-darwin-amd64" ;;
      *) echo "Notice: Unrecognized macOS arch: ${ARCH}" ;;
    esac
    ;;
esac

# 1. Attempt Standalone Binary Download from Latest GitHub Release
if [ -n "${TARGET_ASSET}" ]; then
  echo "Checking latest release on GitHub for standalone binary (${TARGET_ASSET})..."
  API_URL="https://api.github.com/repos/${REPO}/releases/latest"
  DOWNLOAD_URL=$(curl -sSL -H "User-Agent: agym-installer-sh" "${API_URL}" 2>/dev/null | \
    grep "browser_download_url.*${TARGET_ASSET}" | cut -d '"' -f 4 | head -n 1 || true)

  if [ -n "${DOWNLOAD_URL}" ]; then
    echo "Found standalone binary. Downloading..."
    TEMP_FILE="${BIN_DIR}/agym.tmp"
    if curl -fsSL -o "${TEMP_FILE}" "${DOWNLOAD_URL}"; then
      chmod +x "${TEMP_FILE}"
      mv -f "${TEMP_FILE}" "${EXE_PATH}"
      INSTALLED=1
      echo "Standalone binary installed successfully."
    fi
  fi
fi

# 2. Fallback to Python Virtual Environment
if [ "${INSTALLED}" -eq 0 ]; then
  echo "Falling back to Python virtual environment installation..."

  PYTHON_BIN=""
  for candidate in python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      PY_VER="$("${candidate}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
      if [ -n "${PY_VER}" ]; then
        MAJOR=$(echo "${PY_VER}" | cut -d. -f1)
        MINOR=$(echo "${PY_VER}" | cut -d. -f2)
        if [ "${MAJOR}" -ge 3 ] && [ "${MINOR}" -ge 10 ]; then
          PYTHON_BIN="${candidate}"
          break
        fi
      fi
    fi
  done

  if [ -z "${PYTHON_BIN}" ]; then
    echo "Error: Python 3.10+ is required when standalone binaries are unavailable."
    echo "Please install Python 3.10 or higher."
    exit 1
  fi

  echo "Using Python: ${PYTHON_BIN}"
  VENV_DIR="${INSTALL_BASE}/venv"
  if [ ! -d "${VENV_DIR}" ]; then
    echo "Creating isolated virtual environment in ${VENV_DIR}..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  fi

  VENV_PIP="${VENV_DIR}/bin/pip"
  VENV_AGYM="${VENV_DIR}/bin/agym"

  echo "Installing agym from GitHub..."
  "${VENV_PIP}" install --upgrade "git+https://github.com/${REPO}.git"

  ln -sf "${VENV_AGYM}" "${EXE_PATH}"
  chmod +x "${EXE_PATH}"
  INSTALLED=1
  echo "Python environment setup complete."
fi

# 3. PATH Configuration
PATH_FOUND=0
case ":$PATH:" in
  *":${BIN_DIR}:"*) PATH_FOUND=1 ;;
esac

if [ "${PATH_FOUND}" -eq 0 ]; then
  echo "Adding ${BIN_DIR} to PATH..."
  EXPORT_CMD="export PATH=\"\$HOME/.local/bin:\$PATH\""
  
  SHELL_NAME="$(basename "${SHELL:-bash}")"
  case "${SHELL_NAME}" in
    zsh)
      PROFILE_FILE="$HOME/.zshrc"
      ;;
    *)
      PROFILE_FILE="$HOME/.bashrc"
      ;;
  esac

  if [ -f "${PROFILE_FILE}" ]; then
    if ! grep -q ".local/bin" "${PROFILE_FILE}"; then
      echo "" >> "${PROFILE_FILE}"
      echo "# Added by agym installer" >> "${PROFILE_FILE}"
      echo "${EXPORT_CMD}" >> "${PROFILE_FILE}"
      echo "Updated ${PROFILE_FILE}."
    fi
  fi
fi

echo ""
echo "Installation Verified!"
"${EXE_PATH}" --help | head -n 3

echo ""
echo "================================================="
echo "   agym has been successfully installed!         "
echo "================================================="
echo "Location:  ${BIN_DIR}/agym"
echo ""
echo "Quickstart:"
echo "  agym setup personal      # Set up an isolated profile"
echo "  agym personal            # Launch Antigravity under profile"
echo "  agym usage               # Check live quota health"
echo "  agym update              # Update agym to latest release"
echo ""
if [ "${PATH_FOUND}" -eq 0 ]; then
  echo "Notice: Please restart your terminal session or run:"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
