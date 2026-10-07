#!/usr/bin/env bash
# Clone the pinned Apache Fineract release into the local working tree.
#
# This tree is the ONLY place Java is edited. It is gitignored: the committed
# artifact is the generated patch (see export-fineract-patch.sh), not the source.
#
# We clone directly to $DEST rather than cloning elsewhere and copying, because
# `cp -r src/* dest/` skips dotfiles and would drop .git -- leaving a tree that
# `git diff` cannot produce a patch from.
set -euo pipefail

TAG="${FINERACT_TAG:-1.15.0}"
SHA="${FINERACT_SHA:-9e76f088db71a4458b68f7855b03b21d23b86f1c}"
REPO="https://github.com/apache/fineract.git"

cd "$(dirname "$0")/.."
DEST="${FINERACT_SRC:-fineract-customization/upstream}"

if [ -d "$DEST/.git" ]; then
  echo "==> $DEST already cloned; verifying pin"
else
  echo "==> cloning Fineract $TAG into $DEST"
  # core.autocrlf=input at clone time: Fineract's spotless config enforces UNIX
  # line endings globally, so a default Windows clone fails spotlessCheck on
  # files we never touched.
  git clone --depth 1 --branch "$TAG" --config core.autocrlf=input "$REPO" "$DEST"
fi

ACTUAL="$(git -C "$DEST" rev-parse HEAD)"
if [ "$ACTUAL" != "$SHA" ]; then
  echo "FATAL: expected $TAG to be $SHA but found $ACTUAL" >&2
  echo "The upstream tag moved. Re-pin deliberately; do not silently build something else." >&2
  exit 1
fi

echo "==> OK: Fineract $TAG @ $ACTUAL"
