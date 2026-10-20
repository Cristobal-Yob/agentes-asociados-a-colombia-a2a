"""Lectura de claves del operador sin revelar valores ni cargar archivos .env."""

import os
import subprocess
from pathlib import Path


def read_private_key(path):
    original = Path(path)
    target = original.resolve()
    if (
        original.is_symlink()
        or not target.is_file()
        or target.is_relative_to(Path(__file__).resolve().parents[1])
        or not 32 <= target.stat().st_size <= 4096
    ):
        raise ValueError("La clave debe ser un archivo privado regular fuera del proyecto.")
    if os.name == "nt":
        script = (
            "$ErrorActionPreference='Stop'; $a=Get-Acl -LiteralPath $env:AGENT_KEY_FILE; "
            "$u=[Security.Principal.WindowsIdentity]::GetCurrent().User.V"
            "alue; if(-not $a.AreAccessRulesProtected -or $a.GetOwner([Se"
            "curity.Principal.SecurityIdentifier]).Value -ne $u){exit 2};"
            " foreach($r in $a.Access){if($r.AccessControlType -eq 'Allow"
            "' -and $r.IdentityReference.Translate([Security.Principal.Se"
            "curityIdentifier]).Value -ne $u){exit 3}}"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env={
                **{
                    key: value for key, value in os.environ.items() if key.lower() != "psmodulepath"
                },
                "AGENT_KEY_FILE": str(target),
            },
            capture_output=True,
            timeout=8,
            check=False,
        )
        if result.returncode:
            raise ValueError("La clave no tiene permisos exclusivos del usuario actual.")
    elif target.stat().st_mode & 0o077:
        raise ValueError("La clave no tiene permisos privados.")
    key = target.read_text(encoding="utf-8").strip()
    if not 32 <= len(key) <= 4096 or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError("La clave privada no tiene un formato válido.")
    return key


def configured_key(name):
    value, path = os.getenv(name, ""), os.getenv(name + "_FILE", "")
    if value and path:
        raise ValueError("Configure la clave mediante un valor o un archivo, sin ambigüedad.")
    return read_private_key(path) if path else value
