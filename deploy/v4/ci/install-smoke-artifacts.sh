# Shared artifact setup for install-smoke.sh. Kept separate so the 3-argument
# first-release fallback can be exercised without starting Docker containers.
prepare_install_smoke_artifacts() {
  local repo_root=$1 artifacts_dir=$2 parent
  shift 2

  [[ $# -eq 3 || $# -eq 6 ]] || return 2
  VERSION=$1
  TARBALL=$(realpath "$2") || return
  BUNDLE=$(realpath "$3") || return
  [[ $VERSION =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || return 2
  [[ -s $TARBALL && -s $BUNDLE ]] || {
    echo "candidate release assets are missing" >&2
    return 2
  }

  PREVIOUS_VERSION=""
  PREVIOUS_TARBALL=""
  PREVIOUS_BUNDLE=""
  BASELINE=0
  mkdir -p "$artifacts_dir"
  cp "$TARBALL" "$artifacts_dir/aiflow4-$VERSION.tar.gz"
  cp "$BUNDLE" "$artifacts_dir/aiflow4-$VERSION.tar.gz.sigstore.json"

  if [[ $# -eq 6 ]]; then
    PREVIOUS_VERSION=$4
    PREVIOUS_TARBALL=$(realpath "$5") || return
    PREVIOUS_BUNDLE=$(realpath "$6") || return
    [[ $PREVIOUS_VERSION =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || return 2
    [[ -s $PREVIOUS_TARBALL && -s $PREVIOUS_BUNDLE ]] || {
      echo "previous release assets are missing" >&2
      return 2
    }
    cp "$PREVIOUS_TARBALL" "$artifacts_dir/aiflow4-$PREVIOUS_VERSION.tar.gz"
    cp "$PREVIOUS_BUNDLE" "$artifacts_dir/aiflow4-$PREVIOUS_VERSION.tar.gz.sigstore.json"
  else
    # The first tagged release transitions from its parent's develop build into
    # the signed release; that commit hash is not a published release version.
    parent=$(git -C "$repo_root" rev-parse "${GITHUB_SHA:?GITHUB_SHA is required for a tagged install smoke test}^")
    PREVIOUS_VERSION=$parent
    PREVIOUS_TARBALL="$artifacts_dir/aiflow4-baseline-$parent.tar.gz"
    BASELINE=1
    git -C "$repo_root" archive --format=tar.gz --output="$PREVIOUS_TARBALL" "$parent" \
      aiflow pyproject.toml uv.lock prompts deploy
  fi
}
