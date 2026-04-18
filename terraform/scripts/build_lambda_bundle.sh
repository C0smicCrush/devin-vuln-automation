#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <repo-root> <zip-path>" >&2
  exit 1
fi

ROOT_DIR="$1"
ZIP_PATH="$2"
BUILD_DIR="${ROOT_DIR}/build/lambda"

mkdir -p "$(dirname "${ZIP_PATH}")"
rm -rf "${BUILD_DIR}" "${ZIP_PATH}"
mkdir -p "${BUILD_DIR}"

python3 -m pip install --quiet -r "${ROOT_DIR}/requirements.txt" -t "${BUILD_DIR}"
cp -R "${ROOT_DIR}/config" "${BUILD_DIR}/config"
cp -R "${ROOT_DIR}/scripts" "${BUILD_DIR}/scripts"
cp \
  "${ROOT_DIR}/common.py" \
  "${ROOT_DIR}/aws_runtime.py" \
  "${ROOT_DIR}/lambda_intake.py" \
  "${ROOT_DIR}/lambda_worker.py" \
  "${ROOT_DIR}/lambda_poller.py" \
  "${ROOT_DIR}/lambda_discovery.py" \
  "${BUILD_DIR}/"

(
  cd "${BUILD_DIR}"
  zip -qr "${ZIP_PATH}" .
)
