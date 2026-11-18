"""Kod yapisini analiz et: modul bagimliliklari, dead code, karmaşıklık."""

import ast
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"C:\Users\fdemir\Documents\New project\junbo")
EXCLUDE = {"__pycache__", ".git", "venv", "backtest_archive"}

# tum modulleri tara
modules = {}
for p in ROOT.rglob("*.py"):
    if any(e in str(p) for e in EXCLUDE):
        continue
    rel = p.relative_to(ROOT)
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        imports = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imports.extend(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                imports.append(n.module)
        modules[str(rel)] = {"funcs": len(funcs), "classes": len(classes), "imports": imports}
    except SyntaxError as e:
        print(f"SYNTAX HATA: {rel}: {e}")

print(f"=== MODUL SAYISI: {len(modules)} ===")

# ic modul bagimlilik grafigi (en cok bagimli olanlar)
internal_deps = defaultdict(set)
for mod, info in modules.items():
    for imp in info["imports"]:
        for other in modules:
            base = other.replace("\\", ".").replace(".py", "")
            short = base.split(".")[-1]
            if imp == base or imp.endswith("." + short) or imp == short:
                if other != mod and "junbo" not in imp:
                    internal_deps[mod].add(other)

print()
print("=== EN COK BAGIMLI MODULLER (baskalari ona bagli - kritik) ===")
dep_count = defaultdict(int)
for mod, deps in internal_deps.items():
    for d in deps:
        dep_count[d] += 1
for mod, cnt in sorted(dep_count.items(), key=lambda kv: -kv[1])[:12]:
    print(f"  {cnt:3d} modul -> {mod}")

print()
print("=== EN COK BAGIMLIGI OLANLAR (spagetti riski) ===")
for mod, deps in sorted(internal_deps.items(), key=lambda kv: -len(kv[1]))[:10]:
    if deps:
        print(f"  {len(deps):2d} baglanti: {mod}")

# buyuk dosyalarin fonksiyon sayisi (god-object riski)
print()
print("=== GOD-OBJECT RISKI (fonksiyon sayisi > 25) ===")
for mod, info in sorted(modules.items(), key=lambda kv: -kv[1]["funcs"]):
    if info["funcs"] > 25:
        print(f"  {info['funcs']:3d} fonksiyon: {mod}")
