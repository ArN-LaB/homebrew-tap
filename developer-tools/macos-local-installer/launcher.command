#!/bin/bash
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")" && pwd -P)"
PYTHON="${ARNLAB_PYTHON:-python3}"
exec "$PYTHON" -c '
import hashlib, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
config = json.loads((root / ".macos-local-install.json").read_text())
candidates = ([pathlib.Path(os.environ["ARNLAB_INSTALLER_ROOT"])]
              if "ARNLAB_INSTALLER_ROOT" in os.environ else [
                  root.parent / "homebrew-tap/developer-tools/macos-local-installer",
                  pathlib.Path.home() / "Documents/GitHub/homebrew-tap/developer-tools/macos-local-installer"])
for directory in candidates:
    path = directory / "installer.py"
    if path.is_file():
        code = path.read_bytes()
        if hashlib.sha256(code).hexdigest() != config["component_sha256"]:
            sys.exit("ARRÊT : empreinte du composant partagé différente. Vérifier la version ; aucun téléchargement automatique.")
        if (directory / "VERSION").read_text().strip() != config["component_version"]:
            sys.exit("ARRÊT : version du composant différente.")
        sys.argv = [str(path), "--project", str(root), *sys.argv[2:]]
        # Execute exactly the bytes verified above, not a second file read.
        exec(compile(code, str(path), "exec"), {"__name__": "__main__", "__file__": str(path)})
        break
else:
    sys.exit("Composant partagé absent. Voir docs/LOCAL_MACOS_INSTALL.md ; aucun accès réseau automatique.")
' "$PROJECT" "$@"
