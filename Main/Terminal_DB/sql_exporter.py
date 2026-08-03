import pandas as pd
import pyodbc

#### Importing all the existing SQL utilities
from .MS_SQL_Server import (
    get_conn,
    create_table,
    truncate_table,
    sanitize_dataframe,
    upsert_plans,
    EXPECTED_COLUMNS,
    create_leftovers_table,
    truncate_leftovers_table,
    upsert_leftovers,
    EXPECTED_LEFTOVERS_COLUMNS,
)

def push_plans_excel_to_sql(excel_file: str, overwrite: bool = True) -> int:
    """
    Reading Plans sheet from an Excel file and pushing it into SQL Server.
    """
    print("\n================ SQL EXPORT =================")
    print(f"Excel file: {excel_file}")
    print(f"Overwrite mode: {'ON (TRUNCATE)' if overwrite else 'OFF (UPSERT only)'}")

    conn = get_conn()
    print("SQL connection established")

    try:
        create_table(conn)

        if overwrite:
            truncate_table(conn)

        df = pd.read_excel(excel_file, sheet_name="Plans")
        print(f"Rows read from Excel: {len(df)}")

        print("Dropping empty rows...")
        df = df.dropna(how="all")

        df = sanitize_dataframe(df)

        print(f"Inserting / updating {len(df)} rows into SQL Server...")
        upsert_plans(conn, df)

        print("SQL export completed successfully")

        #upsert_plans(conn, df)


#### Leftovers export (optional); since not all plans may produce leftovers
        try:
            df_left = pd.read_excel(excel_file, sheet_name="Leftovers")
            df_left = df_left.dropna(how="all")
            df_left = sanitize_dataframe(df_left)

            missing = set(EXPECTED_LEFTOVERS_COLUMNS) - set(df_left.columns)
            if missing:
                raise ValueError(f"Missing columns in Leftovers sheet: {sorted(missing)}")

            create_leftovers_table(conn)
            if overwrite:
                truncate_leftovers_table(conn)

            print(f"Inserting / updating {len(df_left)} leftovers into SQL Server...")
            upsert_leftovers(conn, df_left)

        except ValueError as e:
            #### pandas to raise a ValueError if sheet doesn't exist
            if "Worksheet named" in str(e) and "Leftovers" in str(e):
                print("No 'Leftovers' sheet found — skipping leftovers export.")
            else:
                raise

        print("SQL export completed successfully")
        return len(df)


    except Exception as e:
        print("ERROR during SQL export")
        print(f"{type(e).__name__}: {e}")
        raise

    finally:
        conn.close()
        print("SQL connection closed\n")

