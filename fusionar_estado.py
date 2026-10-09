#!/usr/bin/env python3
"""
Resuelve conflictos de git en los ficheros de state/ fusionando las dos
versiones en vez de elegir una.

Hace falta porque el bot en la nube hace commit de su memoria cada pocos
minutos, asi que cualquier cambio local choca con el suyo tarde o temprano.

La memoria es acumulativa: un pid apuntado significa "ya avise de esto". Si en
un conflicto se elige un lado, los pids del otro se pierden y esos productos
vuelven a salir como nuevos, o sea avisos repetidos. La fusion correcta es la
union, quedandose con la marca de tiempo mas reciente de cada pid.

Uso, cuando un 'git pull --rebase' deje conflictos en state/:
    python fusionar_estado.py && git rebase --continue
"""

import json
import subprocess
import sys
from pathlib import Path


def version(etapa: int, ruta: str) -> dict:
    """Una de las dos versiones en conflicto: 2 = lo publicado, 3 = lo nuestro."""
    r = subprocess.run(["git", "show", f":{etapa}:{ruta}"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def conflictos() -> list[str]:
    r = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                       capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if l.startswith("state/") and l.endswith(".json")]


def main() -> int:
    pendientes = conflictos()
    if not pendientes:
        print("No hay conflictos en state/.")
        return 0

    for ruta in pendientes:
        publicado, nuestro = version(2, ruta), version(3, ruta)
        union = dict(publicado)
        for pid, visto in nuestro.items():
            # ante el mismo pid, la marca mas reciente: lo que importa es que
            # no se olvide, y la retencion de 30 dias cuenta desde la ultima
            if pid not in union or visto > union[pid]:
                union[pid] = visto
        Path(ruta).write_text(
            json.dumps(union, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        subprocess.run(["git", "add", ruta], check=True)
        print(f"{ruta}: publicado {len(publicado)} + nuestro {len(nuestro)} "
              f"-> union {len(union)} pids")

    print("\nFusionado. Ahora: git rebase --continue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
