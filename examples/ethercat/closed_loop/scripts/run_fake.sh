#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT/build}"
FAKE_HOME="${FAKE_EC_HOMEDIR:-/tmp/atlas-fake-ethercat}"
SHIM_DIR="$BUILD_DIR/fake-lib-shim"

: "${FAKE_EC_SO:?Set FAKE_EC_SO to the full path of libfakeethercat.so.1}"

CONTROLLER="$BUILD_DIR/atlas_ethercat_controller"
PLANT="$BUILD_DIR/atlas_ethercat_plant"

if [[ ! -x "$CONTROLLER" || ! -x "$PLANT" ]]; then
    echo "Build the project first: cmake -S . -B build && cmake --build build -j"
    exit 2
fi

mkdir -p "$SHIM_DIR"
ln -sfn "$(realpath "$FAKE_EC_SO")" "$SHIM_DIR/libethercat.so.1"

export LD_LIBRARY_PATH="$SHIM_DIR:${LD_LIBRARY_PATH:-}"
export FAKE_EC_HOMEDIR="$FAKE_HOME"
export FAKE_EC_PREFIX="/atlas"
rm -rf "$FAKE_EC_HOMEDIR"
mkdir -p "$FAKE_EC_HOMEDIR"

warm_pid=""
plant_pid=""

cleanup() {
    [[ -n "$warm_pid" ]] && kill "$warm_pid" 2>/dev/null || true
    [[ -n "$plant_pid" ]] && kill "$plant_pid" 2>/dev/null || true
}
trap cleanup EXIT

echo "[1/3] Start a first controller instance so its output PDOs exist."
FAKE_EC_NAME=atlas-controller-bootstrap "$CONTROLLER" 30000 &
warm_pid=$!
sleep 1

echo "[2/3] Start the plant with swapped PDO directions."
FAKE_EC_NAME=atlas-plant "$PLANT" 8000 &
plant_pid=$!
sleep 1

echo "[3/3] Restart controller so it discovers the plant input variables."
kill "$warm_pid" 2>/dev/null || true
wait "$warm_pid" 2>/dev/null || true
warm_pid=""

FAKE_EC_NAME=atlas-controller "$CONTROLLER" 5000

wait "$plant_pid" || true
plant_pid=""
echo "Fake EtherCAT closed loop finished."
