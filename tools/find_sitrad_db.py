import os
import glob

print("Buscando la base de datos de SITRAD...")
# SITRAD suele guardar sus datos en estas rutas
rutas_busqueda = [
    r"C:\ProgramData\Sitrad*",
    r"C:\ProgramData\Sitrad PRO*",
    r"C:\Program Files (x86)\Sitrad*",
    r"C:\Program Files\Sitrad*",
    r"C:\Sitrad*",
]

archivos_encontrados = []

for ruta in rutas_busqueda:
    for base in glob.glob(ruta):
        print(f"Explorando: {base}")
        for root, dirs, files in os.walk(base):
            for file in files:
                if file.endswith((".db", ".sqlite", ".sqlite3", ".json")):
                    if "config" in file.lower() or "data" in file.lower() or "server" in file.lower() or file.endswith(".db"):
                        archivos_encontrados.append(os.path.join(root, file))

if archivos_encontrados:
    print("\n=== POSIBLES BASES DE DATOS ENCONTRADAS ===")
    for arch in archivos_encontrados:
        print(f"- {arch} (Tamaño: {os.path.getsize(arch)/1024:.1f} KB)")
    print("\nSi encuentras un archivo .db o .sqlite, avísame y te paso un script para corregir el ID del convertidor directamente.")
else:
    print("No se encontraron bases de datos en las rutas comunes.")