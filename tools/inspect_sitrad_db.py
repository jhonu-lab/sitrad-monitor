import sqlite3

DB_PATH = r"C:\Sitrad_Investigacion\SitradService.db"

def main():
    print(f"Conectando a {DB_PATH}...")
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        cursor = conn.cursor()

        # 1. Obtener todas las tablas
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        print(f"\nTablas encontradas: {tables}\n")

        # 2. Buscar tablas relacionadas con convertidores e instrumentos
        for table in tables:
            if any(keyword in table.lower() for keyword in ['convert', 'instrument', 'server', 'device']):
                print(f"=== Contenido de la tabla: {table} ===")
                try:
                    cursor.execute(f"SELECT * FROM {table} LIMIT 10;")
                    columns = [description[0] for description in cursor.description]
                    rows = cursor.fetchall()
                    
                    print(" | ".join(columns))
                    print("-" * 50)
                    for row in rows:
                        print(" | ".join(str(val) for val in row))
                    print("\n")
                except Exception as e:
                    print(f"Error leyendo {table}: {e}")

        conn.close()
    except Exception as e:
        print(f"Error al abrir la base de datos: {e}")
        print("Asegúrate de que SITRAD no tenga el archivo bloqueado.")

if __name__ == "__main__":
    main()