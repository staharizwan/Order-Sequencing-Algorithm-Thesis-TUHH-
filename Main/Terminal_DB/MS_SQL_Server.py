import pandas as pd
import pyodbc
from pathlib import Path
import os
from dotenv import load_dotenv

#### Configuration loading
load_dotenv()

#### Test file, this file is only used if this python code is executed from main()
EXCEL_FILES = [
    "../MultipleScenarios_WSPT.xlsx"
]

#### The sheet "Plans" from the Excel file is pushed to the databases.
SHEET_NAME = "Plans"

#### SQL Server connection (SQL Authentication)
SQL_SERVER_HOST = os.getenv('SQL_SERVER_HOST')
SQL_SERVER_PORT = int(os.getenv("SQL_SERVER_PORT", 1433))
SQL_SERVER_DATABASE = os.getenv('SQL_SERVER_DATABASE') 
SQL_SERVER_USERNAME = os.getenv('SQL_SERVER_USERNAME')
SQL_SERVER_PASSWORD = os.getenv('SQL_SERVER_PASSWORD')

#### Plans table configuration
SCHEMA = "dbo"
TABLE_NAME = "SchedulePlans"
FULL_TABLE = f"[{SCHEMA}].[{TABLE_NAME}]"


#### Leftovers table configuration
LEFTOVERS_TABLE_NAME = "ScheduleLeftovers"
LEFTOVERS_FULL_TABLE = f"[{SCHEMA}].[{LEFTOVERS_TABLE_NAME}]"

EXPECTED_LEFTOVERS_COLUMNS = [
    "Scenario",
    "Region",
    "SemiTrailerID",
    "ParkingSlot",
    "DepartureDueDate",
]

#### Expected columns

EXPECTED_COLUMNS = [
    "Scenario",
    "Region",
    "Equipment",
    "SequencePos",
    "ParkingSlot",
    "SemiTrailerID",
    "PriorityCode",
    "DepartureDueDate",
    "DestSide",
    "ExpectedServiceMin",
    "StartTime",
    "FinishTime",
    "LatenessMin",
    "PriorityFactor",
    "WeightedLateness"
]

#### Truncating the existing orders table to overwrite stuff.
def truncate_table(conn: pyodbc.Connection) -> None:
    cur = conn.cursor()
    cur.execute(f"TRUNCATE TABLE {FULL_TABLE};")
    conn.commit()
######


#### Connecting to SQL Server
def get_conn() -> pyodbc.Connection:
    # SQL Server expects "host,port" (comma, not colon)
    server = f"{SQL_SERVER_HOST},{SQL_SERVER_PORT}"

    #### 
    conn_str = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={server};"
        f"DATABASE={SQL_SERVER_DATABASE};"
        f"UID={SQL_SERVER_USERNAME};"
        f"PWD={SQL_SERVER_PASSWORD};"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str)


#### The combination of SCENARIO and SEMITRAILERID is the unique key! The combo of these two has to be unique!!!
def create_table(conn: pyodbc.Connection) -> None:
    ddl = f"""
    IF OBJECT_ID(N'{FULL_TABLE}', N'U') IS NULL
    BEGIN
        CREATE TABLE {FULL_TABLE} (
            id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,

            Scenario NVARCHAR(100) NOT NULL,
            SemiTrailerID NVARCHAR(100) NOT NULL,

            Region NVARCHAR(100) NULL,
            Equipment NVARCHAR(100) NULL,
            SequencePos INT NULL,
            ParkingSlot NVARCHAR(100) NULL,

            PriorityCode INT NULL,
            DepartureDueDate DATETIME NULL,
            DestSide NVARCHAR(50) NULL,

            ExpectedServiceMin FLOAT NULL,
            StartTime DATETIME NULL,
            FinishTime DATETIME NULL,

            LatenessMin FLOAT NULL,
            PriorityFactor FLOAT NULL,
            WeightedLateness FLOAT NULL,

            CONSTRAINT UQ_SchedulePlans UNIQUE (Scenario, SemiTrailerID)
        );
    END
    """
    cur = conn.cursor()
    cur.execute(ddl)
    conn.commit()

def create_leftovers_table(conn: pyodbc.Connection) -> None:
    ddl = f"""
    IF OBJECT_ID(N'{LEFTOVERS_FULL_TABLE}', N'U') IS NULL
    BEGIN
        CREATE TABLE {LEFTOVERS_FULL_TABLE} (
            id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,

            Scenario NVARCHAR(100) NOT NULL,
            SemiTrailerID NVARCHAR(100) NOT NULL,

            Region NVARCHAR(100) NULL,
            ParkingSlot NVARCHAR(100) NULL,
            DepartureDueDate DATETIME NULL,

            CONSTRAINT UQ_ScheduleLeftovers UNIQUE (Scenario, SemiTrailerID)
        );
    END
    """
    cur = conn.cursor()
    cur.execute(ddl)
    conn.commit()

#### Truncating the leftovers table to overwrite stuff
def truncate_leftovers_table(conn: pyodbc.Connection) -> None:
    cur = conn.cursor()
    cur.execute(f"TRUNCATE TABLE {LEFTOVERS_FULL_TABLE};")
    conn.commit()



#### Sanitizing the dataFrame for SQL Server
def remove_control_chars(s: str) -> str:
    """
    Removing the non-printable ASCII control chars (0–31) except tab/newline/CR.
    This removes '\x01' (U+0001) which may cause crashes.
    """
    if not isinstance(s, str):
        return s
    return "".join(ch for ch in s if (ord(ch) >= 32) or (ch in "\t\n\r"))


def clean_text_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Cleaning all object/string columns to avoid UnicodeEncodeError and SQL issues.
    """
    df = df.copy()
    obj_cols = df.select_dtypes(include=["object"]).columns
    for col in obj_cols:
        df[col] = df[col].apply(remove_control_chars)
    return df


def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()
    #df = clean_text_columns(df)

    # Parse datetime columns (pyodbc can send Python datetime objects)
    for col in ["DepartureDueDate", "StartTime", "FinishTime"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Numeric columns
    for col in ["ExpectedServiceMin", "LatenessMin", "PriorityFactor", "WeightedLateness"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Integer-like columns (nullable Int64)
    if "SequencePos" in df.columns:
        df["SequencePos"] = pd.to_numeric(df["SequencePos"], errors="coerce").astype("Int64")
    if "PriorityCode" in df.columns:
        df["PriorityCode"] = pd.to_numeric(df["PriorityCode"], errors="coerce").astype("Int64")

    # Replace NaN/NaT with None for pyodbc
    df = df.where(pd.notnull(df), None)
    return df


#### Upserting using MERGE (update + insert = upsert)
MERGE_SQL = """
MERGE {full_table} AS target
USING (SELECT
    ? AS Scenario,
    ? AS SemiTrailerID,
    ? AS Region,
    ? AS Equipment,
    ? AS SequencePos,
    ? AS ParkingSlot,
    ? AS PriorityCode,
    ? AS DepartureDueDate,
    ? AS DestSide,
    ? AS ExpectedServiceMin,
    ? AS StartTime,
    ? AS FinishTime,
    ? AS LatenessMin,
    ? AS PriorityFactor,
    ? AS WeightedLateness
) AS source
ON (target.Scenario = source.Scenario AND target.SemiTrailerID = source.SemiTrailerID)
WHEN MATCHED THEN
    UPDATE SET
        Region = source.Region,
        Equipment = source.Equipment,
        SequencePos = source.SequencePos,
        ParkingSlot = source.ParkingSlot,
        PriorityCode = source.PriorityCode,
        DepartureDueDate = source.DepartureDueDate,
        DestSide = source.DestSide,
        ExpectedServiceMin = source.ExpectedServiceMin,
        StartTime = source.StartTime,
        FinishTime = source.FinishTime,
        LatenessMin = source.LatenessMin,
        PriorityFactor = source.PriorityFactor,
        WeightedLateness = source.WeightedLateness
WHEN NOT MATCHED THEN
    INSERT (
        Scenario, SemiTrailerID, Region, Equipment, SequencePos, ParkingSlot,
        PriorityCode, DepartureDueDate, DestSide,
        ExpectedServiceMin, StartTime, FinishTime,
        LatenessMin, PriorityFactor, WeightedLateness
    )
    VALUES (
        source.Scenario, source.SemiTrailerID, source.Region, source.Equipment, source.SequencePos, source.ParkingSlot,
        source.PriorityCode, source.DepartureDueDate, source.DestSide,
        source.ExpectedServiceMin, source.StartTime, source.FinishTime,
        source.LatenessMin, source.PriorityFactor, source.WeightedLateness
    );
"""

def upsert_plans(conn: pyodbc.Connection, df: pd.DataFrame) -> None:
    sql = MERGE_SQL.format(full_table=FULL_TABLE)
    cur = conn.cursor()
    cur.fast_executemany = True  

    records = df[
        [
            "Scenario", "SemiTrailerID", "Region", "Equipment", "SequencePos", "ParkingSlot",
            "PriorityCode", "DepartureDueDate", "DestSide",
            "ExpectedServiceMin", "StartTime", "FinishTime",
            "LatenessMin", "PriorityFactor", "WeightedLateness"
        ]
    ].values.tolist()

    cur.executemany(sql, records)
    conn.commit()


MERGE_LEFTOVERS_SQL = """
MERGE {full_table} AS target
USING (SELECT
    ? AS Scenario,
    ? AS SemiTrailerID,
    ? AS Region,
    ? AS ParkingSlot,
    ? AS DepartureDueDate
) AS source
ON (target.Scenario = source.Scenario AND target.SemiTrailerID = source.SemiTrailerID)
WHEN MATCHED THEN
    UPDATE SET
        Region = source.Region,
        ParkingSlot = source.ParkingSlot,
        DepartureDueDate = source.DepartureDueDate
WHEN NOT MATCHED THEN
    INSERT (Scenario, SemiTrailerID, Region, ParkingSlot, DepartureDueDate)
    VALUES (source.Scenario, source.SemiTrailerID, source.Region, source.ParkingSlot, source.DepartureDueDate);
"""


def upsert_leftovers(conn: pyodbc.Connection, df: pd.DataFrame) -> None:
    sql = MERGE_LEFTOVERS_SQL.format(full_table=LEFTOVERS_FULL_TABLE)
    cur = conn.cursor()
    cur.fast_executemany = True

    df = df.copy()
    if "DepartureDueDate" in df.columns:
        df["DepartureDueDate"] = pd.to_datetime(df["DepartureDueDate"], errors="coerce")

    df = df.where(pd.notnull(df), None)

    records = df[
        ["Scenario", "SemiTrailerID", "Region", "ParkingSlot", "DepartureDueDate"]
    ].values.tolist()

    cur.executemany(sql, records)
    conn.commit()


#### Main pipeline
def main():
    conn = get_conn()
    create_table(conn)
    truncate_table(conn)
    
    for excel_file in EXCEL_FILES:
        path = Path(excel_file)
        print(f"\n Processing file: {path.resolve()}")

        #### Reading ONLY the "Plans" sheet
        try:
            df = pd.read_excel(path, sheet_name=SHEET_NAME)
        except ValueError:
            raise ValueError(f"Sheet '{SHEET_NAME}' not found in {path.name}")

        #### Validating the columns
        missing = set(EXPECTED_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"Missing columns in {path.name}: {sorted(missing)}")

        #### Removing the empty rows
        df = df.dropna(how="all")

        #### Sanitizing for the SQL Server
        df = sanitize_dataframe(df)

        #### Upsertion
        upsert_plans(conn, df)

        print(f"Upserted {len(df)} rows into {SQL_SERVER_DATABASE}.{SCHEMA}.{TABLE_NAME}")

    conn.close()
    print("\nAll data successfully stored in SQL Server.")

if __name__ == "__main__":
    main()
