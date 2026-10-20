"""Prepare claves distintas y directorios privados sin iniciar servicios ni OAuth."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

from common.private import read_private_key
from scripts.authorize_gmail import _private_write


def private_directory(path):
    fresh = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("No se admiten enlaces en el directorio privado.")
    if os.name == "nt":
        setup = (
            "$p=$env:AGENT_PRIVATE_DIRECTORY; "
            "$u=[Security.Principal.WindowsIdentity]::GetCurrent().User; "
            "$a=[Security.AccessControl.DirectorySecurity]::new(); "
            "$a.SetOwner($u); $a.SetAccessRuleProtection($true,$false); "
            "$a.AddAccessRule([Security.AccessControl.FileSystemAccessRul"
            "e]::new($u,'FullControl','ContainerInherit,ObjectInherit','N"
            "one','Allow')); Set-Acl -LiteralPath $p -AclObject $a;"
        )
        check = (
            "$a=Get-Acl -LiteralPath $env:AGENT_PRIVATE_DIRECTORY; "
            "$u=[Security.Principal.WindowsIdentity]::GetCurrent().User.V"
            "alue; if(-not $a.AreAccessRulesProtected -or $a.GetOwner([Se"
            "curity.Principal.SecurityIdentifier]).Value -ne $u){exit 2};"
            " foreach($r in $a.Access){if($r.AccessControlType -eq 'Allow"
            "' -and $r.IdentityReference.Translate([Security.Principal.Se"
            "curityIdentifier]).Value -ne $u){exit 3}}"
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop';" + (setup if fresh else "") + check,
            ],
            env={
                **{
                    key: value for key, value in os.environ.items() if key.lower() != "psmodulepath"
                },
                "AGENT_PRIVATE_DIRECTORY": str(path),
            },
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise ValueError(
                "No se pudo verificar un directorio exclusivo; no se modificaron "
                "archivos existentes."
            )
    else:
        if fresh:
            path.chmod(0o700)
        if path.stat().st_mode & 0o077:
            raise ValueError("El directorio existente no tiene permisos privados.")


def prepare(work_root):
    root = Path(work_root).resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("El directorio privado debe estar fuera del proyecto.")
    for name in ("service-auth", "workflows", "calendar-oauth-clases"):
        private_directory(root / name)
    paths = [
        root / "service-auth" / f"{role}-api-key.txt" for role in ("tourism", "email", "agenda")
    ]
    paths.append(root / "calendar-oauth-clases" / "approval-key.txt")
    created = 0
    for path in paths:
        if path.exists():
            read_private_key(path)
        else:
            _private_write(path, secrets.token_urlsafe(48))
            read_private_key(path)
            created += 1
    return {
        "created": created,
        "preserved": len(paths) - created,
        "oauth_started": False,
        "services_started": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-root", required=True, help="Directorio privado existente fuera del proyecto"
    )
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.work_root)))
    except Exception:
        parser.exit(
            1, "No se completó la preparación privada. No se imprime ningún valor de clave.\n"
        )


if __name__ == "__main__":
    main()
