#!/usr/bin/env bash
#
# fetch_reference.sh
#
# Reproducibly fetch the read-only AWS reference sample this demo builds on:
#   awslabs/amazon-bedrock-agentcore-samples
#     -> 01-features/06-observe-evaluate-optimize-your-agent
#        (03-optimize is the primary sample; 02-evaluate is used for evaluators)
#
# The sample is fetched with a sparse, blobless, shallow checkout so only the
# relevant subtree is downloaded. The result lands in:
#   reference/amazon-bedrock-agentcore-samples/
#
# This tree is a READ-ONLY starting point. Do not edit files in place; adapt
# them into src/agentcore_demo/ instead. Re-run this script to refresh the copy.
#
set -euo pipefail

REPO_URL="https://github.com/awslabs/amazon-bedrock-agentcore-samples.git"
SPARSE_PATH="01-features/06-observe-evaluate-optimize-your-agent"

# Resolve repo root as the parent of this script's directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST_DIR="${ROOT_DIR}/reference/amazon-bedrock-agentcore-samples"

echo "Fetching ${SPARSE_PATH}"
echo "  from ${REPO_URL}"
echo "  into ${DEST_DIR}"

# Start clean so re-runs are deterministic.
rm -rf "${DEST_DIR}"
mkdir -p "$(dirname "${DEST_DIR}")"

git clone --no-checkout --depth 1 --filter=blob:none "${REPO_URL}" "${DEST_DIR}"
git -C "${DEST_DIR}" sparse-checkout init --cone
git -C "${DEST_DIR}" sparse-checkout set "${SPARSE_PATH}"
git -C "${DEST_DIR}" checkout

# Drop the nested .git so this vendored tree is a plain read-only copy and does
# not become an embedded/nested git repo if the workspace is later initialized.
rm -rf "${DEST_DIR}/.git"

echo ""
echo "Done. Reference sample available at:"
echo "  ${DEST_DIR}/${SPARSE_PATH}"
