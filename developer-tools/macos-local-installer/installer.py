#!/usr/bin/env python3
"""Developer-only local installer. No downloaded code, privilege elevation or launch."""
import argparse
import ctypes
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

VERSION = "0.1.1"
STATE = Path.home() / "Library/Application Support/ArN-LaB Local Installer"
QUARANTINE = "com.apple.quarantine"
CASKROOMS = (Path("/opt/homebrew/Caskroom"), Path("/usr/local/Caskroom"))


class Refusal(RuntimeError):
    pass


def require_runtime():
    if sys.platform != "darwin" or sys.version_info < (3, 10):
        raise Refusal("macOS et Python 3.10+ avec filtres tarfile sécurisés sont nécessaires.")
    if not callable(getattr(tarfile, "data_filter", None)):
        raise Refusal("Python sans filtre tarfile sécurisé : utiliser Python 3.10.12+, 3.11.4+ ou 3.12+.")


def run(args, cwd=None, env=None, timeout=120):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout)
    if result.returncode:
        raise Refusal(f"Commande en échec ({result.returncode}) : {args[0]}\n{result.stderr[-1800:]}")
    return result.stdout.strip()


def git(root, *args):
    return run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", root, *args], timeout=45)


def repository(root, expected):
    root = Path(root).resolve(strict=True)
    if Path(git(root, "rev-parse", "--show-toplevel")) != root:
        raise Refusal("Le projet doit être à la racine de son dépôt Git.")
    origin = git(root, "remote", "get-url", "origin")
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", origin)
    if not match or match[1].lower() != expected.lower():
        raise Refusal(f"Origine Git inattendue ; attendu : {expected}")
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise Refusal(f"Modifications non commitées dans {root}. Les préserver puis créer un commit local.")
    if any(line.startswith("160000 ") for line in git(root, "ls-tree", "-r", "HEAD").splitlines()):
        raise Refusal("Sous-module Git non pris en charge : déclarer une dépendance source explicite.")
    return {"repository": expected, "commit": git(root, "rev-parse", "HEAD"),
            "tree": git(root, "rev-parse", "HEAD^{tree}"), "path": str(root)}


def manifest(root):
    value = json.loads((root / ".macos-local-install.json").read_text())
    if value["component_version"] != VERSION:
        raise Refusal("Version du composant incompatible.")
    if not re.fullmatch(r"[A-Za-z0-9 ._-]+\.app", value["bundle_name"]):
        raise Refusal("Nom de bundle invalide.")
    if not value.get("build") or not all(isinstance(x, str) for x in value["build"]):
        raise Refusal("Commande de compilation invalide.")
    output = Path(value["output"])
    if (output.is_absolute() or ".." in output.parts
            or not re.fullmatch(r"[A-Za-z0-9 ._-]+\.app", output.name)):
        raise Refusal("Sortie de compilation hors du dossier isolé.")
    for item in value.get("source_dependencies", []):
        if not re.fullmatch(r"[0-9a-f]{40}", item.get("commit", "")):
            raise Refusal("Chaque dépendance source exige un commit complet épinglé.")
    return value


def private_directory(path):
    """Never chmod/follow an existing foreign or permissive directory."""
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700, parents=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refusal(f"Dossier privé requis (propriétaire courant, mode 700) : {path}")
    if path.resolve() != path.absolute():
        raise Refusal(f"Lien symbolique interdit dans le chemin de stockage : {path}")


def save_new(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_private(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refusal(f"Fichier privé non fiable : {path}")
        return json.load(stream)


def members(app):
    yield app
    for directory, directories, files in os.walk(app, followlinks=False):
        for name in sorted(directories + files):
            yield Path(directory) / name


def digest(app):
    """Content, permissions and internal links; quarantine is deliberately separate."""
    app = Path(app)
    if app.is_symlink() or not app.is_dir():
        raise Refusal("Bundle absent ou lien symbolique à sa racine.")
    base = app.resolve()
    result = hashlib.sha256()
    for path in sorted(members(app), key=lambda p: p.relative_to(app).as_posix()):
        info = path.lstat()
        relative = path.relative_to(app).as_posix()
        kind = stat.S_IFMT(info.st_mode)
        if kind not in (stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
            raise Refusal(f"Fichier spécial interdit : {relative}")
        if not path.resolve(strict=True).is_relative_to(base):
            raise Refusal(f"Lien sortant du bundle : {relative}")
        result.update(json.dumps([relative, kind, stat.S_IMODE(info.st_mode)]).encode())
        if path.is_symlink():
            result.update(os.readlink(path).encode())
        elif path.is_file():
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    result.update(block)
    return result.hexdigest()


def attribute_names(path):
    # Python's os.*xattr API is Linux-only. Use Darwin's native no-follow API.
    library = ctypes.CDLL(None, use_errno=True)
    function = library.listxattr
    function.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    function.restype = ctypes.c_ssize_t
    size = function(os.fsencode(path), None, 0, 1)  # XATTR_NOFOLLOW
    if size < 0:
        raise OSError(ctypes.get_errno(), "Impossible de lire les attributs", str(path))
    buffer = ctypes.create_string_buffer(size)
    count = function(os.fsencode(path), buffer, size, 1)
    if count < 0:
        raise OSError(ctypes.get_errno(), "Attributs modifiés pendant leur lecture", str(path))
    return buffer.raw[:count].split(b"\0")


def remove_quarantine(path):
    library = ctypes.CDLL(None, use_errno=True)
    function = library.removexattr
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    function.restype = ctypes.c_int
    if function(os.fsencode(path), QUARANTINE.encode(), 1):
        raise OSError(ctypes.get_errno(), "Retrait ciblé échoué", str(path))


def quarantine_paths(app):
    return [p for p in members(app) if QUARANTINE.encode() in attribute_names(p)]


def verify(app, config):
    app = Path(app)
    fingerprint = digest(app)
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if plist.get("CFBundleIdentifier") != config["bundle_id"] or plist.get("CFBundlePackageType") != "APPL":
        raise Refusal("Identifiant ou type de bundle inattendu.")
    executable = plist.get("CFBundleExecutable", "")
    if not executable or Path(executable).name != executable:
        raise Refusal("CFBundleExecutable invalide.")
    binary = app / "Contents/MacOS" / executable
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise Refusal("Exécutable du bundle absent ou non exécutable.")
    with binary.open("rb") as stream:
        if stream.read(4).hex() not in ("cffaedfe", "feedfacf", "cefaedfe", "feedface", "cafebabe", "bebafeca", "cafebabf", "bfbafeca"):
            raise Refusal("Exécutable non Mach-O.")
    display = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(app)],
                             capture_output=True, text=True, timeout=60)
    detail = display.stderr + display.stdout
    if display.returncode:
        if "not signed at all" not in detail or (app / "Contents/_CodeSignature").exists():
            raise Refusal("Signature illisible ou endommagée : " + detail[-800:])
        signature = "non signé (compilation locale uniquement)"
    else:
        run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app])
        signature = "ad hoc valide" if "Signature=adhoc" in detail else "signature existante valide, conservée"
    if digest(app) != fingerprint:
        raise Refusal("Bundle modifié pendant sa vérification.")
    return {"sha256": fingerprint, "bundle_id": config["bundle_id"], "signature": signature,
            "version": plist.get("CFBundleShortVersionString", "?"),
            "quarantine_count": len(quarantine_paths(app))}


def snapshot(root, commit, destination):
    """Export committed sources; no execution from ignored/untracked local files."""
    require_runtime()  # Fail before creating even the source archive on old Python.
    archive = destination.parent / (destination.name + ".tar")
    with archive.open("xb") as stream:
        subprocess.run(["git", "-C", str(root), "archive", "--format=tar", commit],
                       stdout=stream, check=True, timeout=120)
    destination.mkdir(mode=0o700)
    with tarfile.open(archive) as source:
        # Refuse links in source snapshots, including submodules (not archived by Git).
        for entry in source.getmembers():
            if not (entry.isfile() or entry.isdir()) or not (destination / entry.name).resolve().is_relative_to(destination):
                raise Refusal("Archive source contenant un lien ou un chemin interdit.")
        source.extractall(destination, filter="data")
    archive.unlink()


def finalize_local_linker_signature(app):
    """Only a NEW isolated build may receive its missing bundle resource seal."""
    display = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(app)],
                             capture_output=True, text=True, timeout=60)
    if display.returncode:
        return  # verify() distinguishes truly unsigned from damaged signatures.
    valid = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                           capture_output=True, text=True, timeout=120)
    if not valid.returncode:
        return  # Preserve ALL valid bundle signatures, including ad hoc / Developer ID.
    detail = display.stderr + display.stdout
    if ((app / "Contents/_CodeSignature").exists() or "Signature=adhoc" not in detail
            or "linker-signed" not in detail or "Sealed Resources=none" not in detail):
        raise Refusal("Signature existante invalide ; aucune réparation automatique.")
    # Verify the linker signature as standalone Mach-O: codesign interprets the
    # original binary inside Contents/MacOS as a bundle and requires a resource seal.
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = plist["CFBundleExecutable"]
    if Path(executable).name != executable:
        raise Refusal("Exécutable invalide avant scellement.")
    with tempfile.TemporaryDirectory(prefix="linker-check-", dir=app.parent) as directory:
        binary = Path(directory) / "executable"
        shutil.copy2(app / "Contents/MacOS" / executable, binary)
        run(["/usr/bin/codesign", "--verify", "--strict", binary])
    print("Nouveau build local : scellement ad hoc du bundle Swift (signature provisoire du linker uniquement).", flush=True)
    # --force is narrowly scoped to the linker-only main signature above. Never
    # --deep: nested signatures are preserved; the --app path never calls this.
    run(["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", app])


def personalize_build(app, config):
    """Distinct local identity, only inside a freshly exported build workspace."""
    source_id = config.get("build_bundle_id")
    if not source_id:
        return
    plist_path = app / "Contents/Info.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    if plist.get("CFBundleIdentifier") != source_id or source_id == config["bundle_id"]:
        raise Refusal("Identité du build source inattendue ; personnalisation refusée.")
    display = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(app)],
                             capture_output=True, text=True, timeout=60)
    detail = display.stdout + display.stderr
    if ((app / "Contents/_CodeSignature").exists() or display.returncode
            or "Signature=adhoc" not in detail or "linker-signed" not in detail
            or "Sealed Resources=none" not in detail):
        raise Refusal("Identité locale : seul un nouveau build avec signature provisoire du linker peut être personnalisé.")
    # Validate the untouched executable before writing the new bundle identity.
    executable = plist["CFBundleExecutable"]
    if Path(executable).name != executable:
        raise Refusal("CFBundleExecutable invalide avant personnalisation.")
    with tempfile.TemporaryDirectory(prefix="identity-check-", dir=app.parent) as directory:
        binary = Path(directory) / "executable"
        shutil.copy2(app / "Contents/MacOS" / executable, binary)
        run(["/usr/bin/codesign", "--verify", "--strict", binary])
    plist["CFBundleIdentifier"] = config["bundle_id"]
    plist["CFBundleName"] = plist["CFBundleDisplayName"] = config["bundle_name"][:-4]
    plist_path.write_bytes(plistlib.dumps(plist))


def dependencies_for(root_config):
    dependencies = []
    for item in root_config.get("source_dependencies", []):
        expected = item.get("commit", "")
        if not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise Refusal("Dépendance sans commit complet épinglé.")
        path = Path(os.environ.get(item["env"], os.path.expanduser(item["default"])))
        evidence = repository(path, item["repository"])
        if evidence["commit"] != expected:
            raise Refusal(f"Dépendance {item['repository']} : commit attendu {expected}, HEAD trouvé {evidence['commit']}. Le checkout existant est conservé.")
        dependencies.append((item, evidence))
    return dependencies


def build(root, config, state):
    require_runtime()
    source = repository(root, config["repository"])
    dependencies = dependencies_for(config)
    private_directory(state)
    build_id = str(uuid.uuid4())
    workspace = state / ("build-" + build_id)
    workspace.mkdir(mode=0o700)
    exported = workspace / "source"
    snapshot(root, source["commit"], exported)
    environment = os.environ.copy()
    for index, (item, evidence) in enumerate(dependencies):
        target = workspace / f"dependency-{index}"
        snapshot(Path(evidence["path"]), evidence["commit"], target)
        environment[item["env"]] = str(target)
    command = [x.replace("{python}", sys.executable) for x in config["build"]]
    print("Compilation isolée :", workspace, flush=True)
    with (workspace / "build.log").open("x") as stream:
        result = subprocess.run(command, cwd=exported, env=environment,
                                stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
    if result.returncode:
        raise Refusal(f"Compilation échouée ({result.returncode}). Journal : {workspace / 'build.log'}")
    if repository(root, config["repository"]) != source:
        raise Refusal("Sources modifiées pendant la compilation.")
    for item, evidence in dependencies:
        if repository(Path(evidence["path"]), item["repository"]) != evidence:
            raise Refusal("Dépendance source modifiée pendant la compilation.")
    app = exported / config["output"]
    if not app.resolve(strict=True).is_relative_to(exported):
        raise Refusal("Le bundle produit sort du dossier de compilation isolé.")
    digest(app)  # Validate structure and links before signing any new build.
    personalize_build(app, config)
    finalize_local_linker_signature(app)
    result = verify(app, config)
    receipt = {"schema": 1, "id": build_id, "component_version": VERSION,
               "component_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "source": source, "dependencies": [v for _, v in dependencies],
               "app": str(app), "result": result, "created": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    save_new(state / (build_id + ".json"), receipt)
    print(f"Compilation vérifiée : {app}\nReçu local : {build_id}\n{json.dumps(result, ensure_ascii=False)}")
    return app, build_id


def identifier(value):
    if str(uuid.UUID(value)) != value:
        raise Refusal("Identifiant de transaction invalide.")
    return value


def trusted(app, receipt_id, config, state):
    private_directory(state)
    receipt = read_private(state / (identifier(receipt_id) + ".json"))
    result = verify(app, config)
    if (receipt.get("schema") != 1 or receipt.get("id") != receipt_id
            or receipt["source"]["repository"] != config["repository"]
            or receipt["result"]["bundle_id"] != config["bundle_id"]
            or receipt["result"]["sha256"] != result["sha256"]):
        raise Refusal("Le bundle ne correspond pas au reçu de compilation local.")
    expected_dependencies = {(item["repository"], item["commit"])
                             for item in config.get("source_dependencies", [])}
    recorded_dependencies = {(item.get("repository"), item.get("commit"))
                             for item in receipt.get("dependencies", [])}
    if expected_dependencies != recorded_dependencies:
        raise Refusal("Le reçu ne correspond pas aux dépendances épinglées actuelles ; recompiler.")
    return result


def identity(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino, digest(path)]


def move_exclusive(source, destination):
    # Darwin renamex_np(RENAME_EXCL): never replace a competing destination.
    library = ctypes.CDLL(None, use_errno=True)
    function = library.renamex_np
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(os.fsencode(source), os.fsencode(destination), 4):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def approve(message, exact):
    if not sys.stdin.isatty():
        raise Refusal("Approbation interactive nécessaire ; relancer dans Terminal.")
    return input(f"{message}\nSaisir {exact} pour confirmer : ").strip() == exact


def lock(store):
    private_directory(store)
    fd = os.open(store / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise Refusal("Verrou non fiable.")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return os.fdopen(fd, "r+")


def homebrew_owners(target):
    """Read installed cask receipts, never invoke brew or alter its registration."""
    owners = []
    for caskroom in CASKROOMS:
        if not caskroom.is_dir():
            continue
        for receipt_path in caskroom.glob("*/.metadata/INSTALL_RECEIPT.json"):
            metadata = receipt_path.parent
            config = json.loads((metadata / "config.json").read_text())
            settings = {}
            for layer in ("default", "env", "explicit"):
                settings.update(config.get(layer, {}))
            appdir = Path(settings.get("appdir", "/Applications")).expanduser()
            receipt = json.loads(receipt_path.read_text())
            for artifact in receipt.get("uninstall_artifacts", []):
                app = artifact.get("app", [])
                if not app:
                    continue
                if not isinstance(app, list) or not isinstance(app[0], str):
                    raise Refusal(f"Reçu Homebrew non reconnu : {receipt_path}")
                options = next((item for item in app if isinstance(item, dict)), {})
                name = options.get("target", Path(app[0]).name)
                # Conservatively protect case variants too (usual APFS is insensitive).
                if str((appdir / name).absolute()).casefold() == str(Path(target).absolute()).casefold():
                    owners.append(receipt_path.parent.parent.name)
    return owners


def running_applications(bundle_id):
    # NSWorkspace is read-only here: no quit event, Apple Events or TCC request.
    script = '''ObjC.import('AppKit');
    function run(argv) {
      const running = $.NSWorkspace.sharedWorkspace.runningApplications, found = [];
      for (let i = 0; i < running.count; i++) {
        const app = running.objectAtIndex(i);
        if (ObjC.unwrap(app.bundleIdentifier) === argv[0])
          found.push({pid:Number(app.processIdentifier),path:ObjC.unwrap(app.bundleURL.path)});
      }
      return JSON.stringify(found);
    }'''
    return json.loads(run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script, bundle_id], timeout=30))


def protect_destination(target, config):
    owners = homebrew_owners(target)
    if owners:
        raise Refusal(f"Destination gérée par Homebrew ({', '.join(owners)}) : remplacement et restauration refusés. Utiliser une édition locale avec nom et identifiant distincts.")
    target = Path(target).absolute()
    active = [a for a in running_applications(config["bundle_id"])
              if str(Path(a["path"]).absolute()).casefold() == str(target).casefold()]
    # Also cover a Mach-O launched directly, before NSWorkspace registration.
    processes = run(["/bin/ps", "-axo", "pid=,comm="], timeout=30)
    prefix = str(target / "Contents/MacOS") + "/"
    if active or any(line.strip().split(None, 1)[-1].startswith(prefix)
                     for line in processes.splitlines() if line.strip()):
        raise Refusal(f"Application en cours : {target.name}. Quittez-la normalement puis relancez ; aucun processus n'est arrêté automatiquement.")


def list_backups(root, config):
    """Read-only inventory. No automatic deletion of backups or foreign files."""
    root = Path(root)
    if root.is_symlink() or root.resolve() != root.absolute():
        raise Refusal("Destination de sauvegardes redirigée par un lien.")
    store = root / ".arnlab-local-installer"
    if not store.exists():
        return []
    private_directory(store)
    result = []
    for transaction in sorted(store.iterdir()):
        if transaction.name == "lock":
            continue
        try:
            identifier(transaction.name)
            private_directory(transaction)
            record_file = next((transaction / name for name in ("installed.json", "plan.json")
                                if (transaction / name).exists()), None)
            record = read_private(record_file) if record_file else {}
            if record_file and (record.get("schema") != 1 or record.get("id") != transaction.name):
                raise Refusal("Journal de transaction non reconnu.")
            if record_file and (record.get("bundle_id") != config["bundle_id"] or record.get("bundle_name") != config["bundle_name"]):
                continue
            size = sum(p.lstat().st_size for p in members(transaction) if stat.S_ISREG(p.lstat().st_mode))
            result.append({"id": transaction.name, "bytes": size,
                           "state": "restaurée" if (transaction / "restored.json").exists()
                           else "installée" if (transaction / "installed.json").exists()
                           else "préparée/à examiner" if record_file else "interrompue, sans journal",
                           "previous": (transaction / "previous.app").is_dir(), "path": str(transaction)})
        except (OSError, ValueError, Refusal) as error:
            raise Refusal(f"Sauvegarde non fiable, aucun changement : {transaction.name}: {error}") from error
    return result


def install(app, receipt_id, config, state, destination_root, consent=approve):
    app = Path(app).absolute()
    result = trusted(app, receipt_id, config, state)
    root = Path(destination_root)
    if root.is_symlink() or root.resolve() != root.absolute() or not root.is_dir():
        raise Refusal("Destination absente ou redirigée par un lien.")
    target = root / config["bundle_name"]
    protect_destination(target, config)
    if not consent(f"Installer {config['bundle_name']} {result['version']} dans {root} ?", "INSTALLER"):
        raise Refusal("Installation annulée ; aucun bundle modifié.")
    with lock(root / ".arnlab-local-installer"):
        transaction_id = str(uuid.uuid4())
        transaction = root / ".arnlab-local-installer" / transaction_id
        transaction.mkdir(mode=0o700)
        staged = transaction / "prepared.app"
        backup = transaction / "previous.app"
        old = None
        if target.exists() or target.is_symlink():
            verify(target, config)
            old = identity(target)
        run(["/usr/bin/ditto", app, staged], timeout=300)
        if verify(staged, config)["sha256"] != result["sha256"]:
            raise Refusal("Copie préparée différente du build vérifié.")
        quarantined = quarantine_paths(staged)
        if quarantined:
            if not consent(f"Retirer UNIQUEMENT {QUARANTINE} sur la copie préparée de {config['bundle_name']} ({len(quarantined)} éléments) ? Provenance : reçu local {receipt_id}.", "RETIRER"):
                raise Refusal(f"Quarantaine conservée. Installation annulée ; copie : {staged}")
            for path in quarantined:
                remove_quarantine(path)
        if quarantine_paths(staged) or verify(staged, config)["sha256"] != result["sha256"]:
            raise Refusal("Vérification finale de la copie préparée échouée.")
        if trusted(app, receipt_id, config, state)["sha256"] != result["sha256"]:
            raise Refusal("Build source modifié avant publication.")
        record = {"schema": 1, "id": transaction_id, "bundle_name": config["bundle_name"],
                  "bundle_id": config["bundle_id"], "receipt": receipt_id, "previous": old,
                  "prepared": identity(staged)}
        save_new(transaction / "plan.json", record)
        protect_destination(target, config)
        try:
            if old:
                if identity(target) != old:
                    raise Refusal("Modification concurrente détectée avant sauvegarde.")
                move_exclusive(target, backup)
                if identity(backup) != old:
                    raise Refusal("Modification concurrente conservée dans previous.app.")
            move_exclusive(staged, target)
            if identity(target) != record["prepared"]:
                raise Refusal("Installation remplacée ou modifiée par un autre processus.")
            verify(target, config)
            save_new(transaction / "installed.json", record)
        except Exception:
            # Never remove or overwrite an external process's bundle. Preserve every copy.
            if target.exists() and identity(target) == record["prepared"]:
                move_exclusive(target, transaction / "rejected.app")
            if backup.exists() and not (target.exists() or target.is_symlink()):
                move_exclusive(backup, target)
            raise
    print(f"Installation vérifiée : {target}\nTransaction : {transaction_id}\nSauvegardes : {transaction}")
    return transaction_id


def rollback(transaction_id, config, root, consent=approve):
    transaction_id = identifier(transaction_id)
    root = Path(root)
    if root.is_symlink() or root.resolve() != root.absolute() or not root.is_dir():
        raise Refusal("Destination redirigée ou absente.")
    with lock(root / ".arnlab-local-installer"):
        transaction = root / ".arnlab-local-installer" / transaction_id
        if not transaction.is_dir():
            raise Refusal("Transaction de restauration absente.")
        private_directory(transaction)
        record = read_private(transaction / "installed.json")
        if record["bundle_name"] != config["bundle_name"] or record["bundle_id"] != config["bundle_id"]:
            raise Refusal("Transaction d'une autre application.")
        current, previous = root / config["bundle_name"], transaction / "previous.app"
        protect_destination(current, config)
        if not record["previous"] or identity(current) != record["prepared"] or identity(previous) != record["previous"]:
            raise Refusal("Retour arrière refusé : installation/sauvegarde modifiée, ou aucune version précédente.")
        verify(previous, config)
        if not consent("Restaurer la version précédente ? La version actuelle sera conservée.", "RESTAURER"):
            raise Refusal("Restauration annulée.")
        if identity(current) != record["prepared"] or identity(previous) != record["previous"]:
            raise Refusal("Modification concurrente avant restauration.")
        protect_destination(current, config)
        saved = transaction / "replaced.app"
        move_exclusive(current, saved)
        try:
            if identity(saved) != record["prepared"]:
                raise Refusal("Modification concurrente conservée dans replaced.app.")
            move_exclusive(previous, current)
            if identity(current) != record["previous"]:
                raise Refusal("Restauration modifiée par un autre processus.")
            verify(current, config)
            save_new(transaction / "restored.json", record)
        except Exception:
            if not (current.exists() or current.is_symlink()):
                move_exclusive(saved, current)
            raise
    print(f"Version précédente restaurée et vérifiée : {current}")


def main():
    parser = argparse.ArgumentParser(description=f"ArN-LaB installation locale macOS {VERSION}")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="lecture seule, sans compilation ni installation")
    parser.add_argument("--build", action="store_true", help="compiler des sources commitées dans un dossier isolé")
    parser.add_argument("--build-only", action="store_true", help="compiler et créer le reçu, sans installation")
    parser.add_argument("--app", type=Path, help="bundle à contrôler ; installer exige un reçu local")
    parser.add_argument("--receipt", help="UUID du reçu créé sur ce Mac")
    parser.add_argument("--rollback", help="UUID de la transaction à restaurer")
    parser.add_argument("--simulate", action="store_true", help="installation dans un nouveau dossier temporaire, jamais /Applications")
    parser.add_argument("--list-backups", action="store_true", help="inventorier les sauvegardes et leur taille, sans suppression")
    args = parser.parse_args()
    if args.app and (args.build or args.build_only):
        raise Refusal("Choisir un bundle existant OU une nouvelle compilation.")
    if args.receipt and not args.app:
        raise Refusal("--receipt nécessite --app.")
    require_runtime()
    root = args.project.resolve(strict=True)
    config = manifest(root)
    if args.list_backups:
        if args.build or args.build_only or args.app or args.rollback or args.receipt or args.simulate:
            raise Refusal("--list-backups est une commande de lecture seule indépendante.")
        print(json.dumps(list_backups(Path("/Applications"), config), indent=2, ensure_ascii=False))
        return
    if args.dry_run:
        print("SIMULATION EN LECTURE SEULE : aucun fichier, attribut ou réglage modifié.")
        print(json.dumps(repository(root, config["repository"]), indent=2, ensure_ascii=False))
        print(f"Composant {VERSION} ; bundle {config['bundle_name']} ; ID {config['bundle_id']}")
        print("Compilation isolée :", " ".join(config["build"]))
        for _, evidence in dependencies_for(config):
            print("Dépendance épinglée :", evidence["repository"], evidence["commit"])
        print("Destination :", "dossier temporaire" if args.simulate else "/Applications")
        if args.app:
            print(json.dumps(verify(args.app, config), indent=2, ensure_ascii=False))
            print("Installer ce fichier exige un reçu de compilation local ; une URL GitHub ou un SHA téléchargé ne suffit pas.")
        return
    if args.rollback:
        if args.build or args.build_only or args.app or args.simulate:
            raise Refusal("--rollback doit être utilisé seul.")
        rollback(args.rollback, config, Path("/Applications"))
        return
    if not (args.build or args.build_only or args.app):
        if not approve("Compiler depuis les sources locales commitées puis proposer l'installation ?", "COMPILER"):
            raise Refusal("Annulé.")
        args.build = True
    if args.build or args.build_only:
        args.app, args.receipt = build(root, config, STATE)
    if args.build_only:
        return
    if not args.receipt:
        raise Refusal("Artefact sans reçu de compilation local : installation refusée. Utiliser --build.")
    # A downloaded receipt cannot be supplied by path: only a private, locally issued UUID.
    trusted(args.app, args.receipt, config, STATE)
    destination = Path(tempfile.mkdtemp(prefix="arnlab-install-simulation-")).resolve() if args.simulate else Path("/Applications")
    if args.simulate:
        print("SIMULATION :", destination)
    install(args.app, args.receipt, config, STATE, destination)


if __name__ == "__main__":
    try:
        main()
    except (Refusal, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print("ARRÊT : " + str(error), file=sys.stderr)
        sys.exit(1)
