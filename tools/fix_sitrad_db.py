import sqlite3
import os
import shutil

# Ruta que encontró tu script anterior
DB_PATH = r"C:\Sitrad_Investigacion\SitradService.db"

def fix_db():
    if not os.path.exists(DB_PATH):
        print(f"❌ No encuentro la base de datos en {DB_PATH}")
        return

    # 1. Hacer copia de seguridad por si acaso
    backup_path = DB_PATH + ".bak"
    shutil.copy2(DB_PATH, backup_path)
    print(f"✅ Copia de seguridad creada en {backup_path}")

    # 2. Conectar
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
    except Exception as e:
        print(f"❌ Error al abrir la BD. ¿Seguro que detuviste el servicio en services.msc? Error: {e}")
        return

    # 3. Buscar tablas
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"Tablas encontradas: {tables}")

    # 4. Buscar la tabla de convertidores
    conv_table = next((t for t in tables if 'conv' in t.lower() or 'device' in t.lower()), None)
    
    if conv_table:
        print(f"\nRevisando tabla: {conv_table}")
        cursor.execute(f"SELECT * FROM {conv_table};")
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        
        print(" | ".join(columns))
        for row in rows:
            print(" | ".join(str(val) for val in row))
            
        # 5. Borrar el ID 2 (el fantasma)
        id_col = 'id' if 'id' in columns else columns[0]
        print(f"\nBorrando el convertidor fantasma (ID = 2) de la tabla {conv_table}...")
        cursor.execute(f"DELETE FROM {conv_table} WHERE {id_col} = 2;")
        conn.commit()
        print("✅ Convertidor 2 borrado.")
    else:
        print("❌ No encontré la tabla de convertidores automáticamente. Pásame la lista de tablas de arriba.")

    # 6. Buscar la tabla de instrumentos y asegurarnos de que apunte al convertidor correcto (3)
    inst_table = next((t for t in tables if 'instr' in t.lower()), None)
    if inst_table:
        print(f"\nRevisando tabla de instrumentos: {inst_table}")
        cursor.execute(f"SELECT * FROM {inst_table};")
        inst_columns = [desc[0] for desc in cursor.description]
        print(" | ".join(inst_columns))
        for row in cursor.fetchall():
            print(" | ".join(str(val) for val in row))
            
        # Si hay una columna converterId, la forzamos al 3
        if 'converterId' in inst_columns or 'converter_id' in inst_columns:
            col = 'converterId' if 'converterId' in inst_columns else 'converter_id'
            print(f"\nForzando a que el Instrumento 1 apunte al Convertidor 3...")
            cursor.execute(f"UPDATE {inst_table} SET {col} = 3 WHERE id = 1;")
            conn.commit()
            print("✅ Instrumento actualizado al convertidor 3.")

    conn.close()

if __name__ == "__main__":
    fix_db()