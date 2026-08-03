import pandas as pd
from datetime import timedelta
from typing import Optional, Dict, List, Tuple
import random
from dotenv import load_dotenv
import os, json
from Terminal_DB.sql_exporter import push_plans_excel_to_sql

load_dotenv()



#### Importing all the helper functions
from scheduler_core import (
    load_data,
    _handling_time_minutes,
    PLANNING_START_HOUR,
    END_OF_DAY_HOUR,
    export_plans_with_kpis
)

def region_plan_edf(
    region: str,
    region_trailers: pd.DataFrame,
    equipment_for_region: pd.DataFrame,
    service_time_override_min: Optional[int] = None,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed: Optional[int] = 50,
    eq_clock_shared: Optional[Dict[str, pd.Timestamp]] = None 
    ):
    rng = random.Random(seed)

    if region_trailers.empty or equipment_for_region.empty:
        return pd.DataFrame(), region_trailers.get("Trailer ID", pd.Series([], dtype=object)).tolist()

    #### Defining simulation start and horizon
    if simulation_start is None:
        today = pd.Timestamp.now().normalize()
        simulation_start = today + pd.Timedelta(hours=PLANNING_START_HOUR)
    horizon_end = simulation_start.normalize() + pd.Timedelta(hours = end_of_day_hour)

    ####  service time per semi-trailer
    def row_time(row) -> float:
        if service_time_override_min is not None:
            return float(service_time_override_min)
        return _handling_time_minutes(row.get("Region"), row.get("DestSide"), rng)

    region_trailers = region_trailers.copy()
    region_trailers["ExpectedServiceMin"] = region_trailers.apply(row_time, axis=1)

    #### EDF sorting: earliest due first; then by PriorityCode 
    region_trailers = region_trailers.sort_values(
        by=["DueDate", "PriorityCode", "ExpectedServiceMin"],
        ascending=[True, True, True],
        kind="mergesort"
    ).reset_index(drop=True)
    

    eq_list = equipment_for_region["Equipment"].tolist()

    #### Shared clock support (synchronizes East→North with North, etc.)
    if eq_clock_shared is not None:
        eq_clock = eq_clock_shared
    else:
        eq_clock = {eq: simulation_start for eq in eq_list}

    seq_counter = {eq: 1 for eq in eq_list}

    assignments = []
    leftovers = []

    #### Scheduling Loop
    for _, tr in region_trailers.iterrows():
        #### Choosing the earliest-available equipment
        chosen_eq = min(eq_list, key=lambda e: eq_clock[e])

        start_time = eq_clock[chosen_eq]
        minutes_needed = float(tr["ExpectedServiceMin"])
        finish_time = start_time + timedelta(minutes=minutes_needed)

        #### Checking if job fits within the horizon
        if finish_time <= horizon_end:
            assignments.append({
                "Region": region,
                "Equipment": chosen_eq,
                "SequencePos": seq_counter[chosen_eq],
                "ParkingSlot": tr.get("ParkingSlotIdentifier"),
                "SemiTrailerID": tr.get("Trailer ID"),
                "PriorityCode": int(tr.get("PriorityCode", 9999)),
                "DepartureDueDate": tr.get("Departure Due Date"),
                "DestSide": tr.get("DestSide"),
                "ExpectedServiceMin": round(minutes_needed, 2),
                "StartTime": start_time,
                "FinishTime": finish_time,
            })
            seq_counter[chosen_eq] += 1
            eq_clock[chosen_eq] = finish_time  #### updated shared clock
        else:
                leftovers.append({
                "SemiTrailerID": tr.get("Trailer ID"),
                "Region": region,
                "DepartureDueDate": tr.get("Departure Due Date"),
                "ParkingSlot": tr.get("ParkingSlotIdentifier")
               
            })

    #### Sorting final schedule per equipment timeline
    out = pd.DataFrame(assignments).sort_values(["Equipment", "SequencePos"]).reset_index(drop=True)
    return out, leftovers



def plan_all_regions_edf(
    trailers: pd.DataFrame,
    equipment: pd.DataFrame,
    service_time_override_min: Optional[int] = None,
    simulation_start=None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed: Optional[int] = 50
):
    #### Candidate semi-trailers that can be scheduled
    cand = trailers[
        (trailers["ExportFlag"] == 1) &
        (trailers["OccupiedFlag"]) &
        (trailers["ParkingSlotIdentifier"].notna())
    ].copy()
    cand["Region"] = (
    cand["Region"]
    .astype(str)
    .str.strip()
    .replace(["nan", "NaN", "Nan", ""], pd.NA)
    )
    #### Checking for missing regions
    missing_region = cand[cand["Region"].isna()]
    if not missing_region.empty:
        raise ValueError(
            "Invalid input data: exportable semi-trailers with missing Region:\n"
            + missing_region[["Trailer ID", "ParkingSlotIdentifier"]].to_string(index=False)
        )
    
    #### Checking for invalid regions
    allowed_prefixes = ("North", "South", "East")
    bad_region_rows = cand[~cand["Region"].astype(str).str.startswith(allowed_prefixes, na=False)]

    if not bad_region_rows.empty:
        bad_vals = bad_region_rows["Region"].unique().tolist()
        raise ValueError(
            "Invalid Region labels detected (must start with North/South/East).\n"
            f"Found: {bad_vals}\n"
            + bad_region_rows[["Trailer ID", "Region", "ParkingSlotIdentifier"]].to_string(index=False)
        )
    ####
    
    
    
    plans: Dict[str, pd.DataFrame] = {}
    leftovers: Dict[str, List] = {}

    #### Splitting equipment by operational region
    eq_north = equipment[equipment["Region"].str.startswith("North")]
    eq_south = equipment[equipment["Region"].str.startswith("South")]

    #### Initializing shared availability clocks (prevent equipment usage conflict)
    eq_clock_north = {eq: simulation_start for eq in eq_north["Equipment"].tolist()}
    eq_clock_south = {eq: simulation_start for eq in eq_south["Equipment"].tolist()}

    #### Group semi-trailers by storage region
    for region, region_trailers in cand.groupby("Region"):

        #### EAST storage — splits by destination side
        if region.startswith("East"):
            east_north = region_trailers[region_trailers["DestSide"] == "North"]
            east_south = region_trailers[region_trailers["DestSide"] == "South"]

            #### East→North handled by the equipment in the North (shared clock)
            plan_north, lo_north = region_plan_edf(
                "East→North",
                east_north,
                eq_north,
                service_time_override_min=service_time_override_min,
                simulation_start=simulation_start,
                end_of_day_hour=end_of_day_hour,
                seed=seed,
                eq_clock_shared=eq_clock_north  
            )

            #### East→South handled by the equipment in the South (shared clock)
            plan_south, lo_south = region_plan_edf(
                "East→South",
                east_south,
                eq_south,
                service_time_override_min=service_time_override_min,
                simulation_start=simulation_start,
                end_of_day_hour=end_of_day_hour,
                seed=seed,
                eq_clock_shared=eq_clock_south  
            )

            #### Combining both sub-regions
            plans[region] = pd.concat([plan_north, plan_south], ignore_index=True)
            leftovers[region] = lo_north + lo_south

        #### NORTH region — handled by equipment in the North (shared clock)
        elif region.startswith("North"):
            plan, lo = region_plan_edf(
                region,
                region_trailers,
                eq_north,
                service_time_override_min=service_time_override_min,
                simulation_start=simulation_start,
                end_of_day_hour=end_of_day_hour,
                seed=seed,
                eq_clock_shared=eq_clock_north  
            )
            plans[region] = plan
            leftovers[region] = lo

        #### SOUTH region — equipment in the South (shared clock)
        elif region.startswith("South"):
            plan, lo = region_plan_edf(
                region,
                region_trailers,
                eq_south,
                service_time_override_min=service_time_override_min,
                simulation_start=simulation_start,
                end_of_day_hour=end_of_day_hour,
                seed=seed,
                eq_clock_shared=eq_clock_south  
            )
            plans[region] = plan
            leftovers[region] = lo

        #### Unknown regions safeguard
        else:
            plans[region] = pd.DataFrame()
            leftovers[region] = region_trailers.get("Trailer ID", pd.Series([], dtype=object)).tolist()

    return plans, leftovers


def run_scenario_edf(
    file_path: str,
    parking_table: str,
    equipment_table: str,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed: Optional[int] = 50
):
    trailers, equipment = load_data(file_path, parking_table, equipment_table)

    #### Improved duplicate semi-trailer handling
    exportable = trailers[
    (trailers["ExportFlag"] == 1) &
    (trailers["OccupiedFlag"]) &
    (trailers["ParkingSlotIdentifier"].notna())
    ].copy()
    exportable["Trailer ID"] = (
        exportable["Trailer ID"]
        .astype(str)
        .str.strip()
        .replace(["", "-", "nan", "NaN", "None"], pd.NA)
    )

    #### Checking for missing IDs in exportables only
    invalid_rows = exportable[exportable["Trailer ID"].isna()]
    if not invalid_rows.empty:
        raise ValueError(
            f"Invalid or missing Semi-trailer IDs among exportables in '{file_path}'.\n"
            f"Rows:\n{invalid_rows[['Trailer ID','Region','ParkingSlotIdentifier']]}"
        )

    #### Checking duplicates ONLY in exportables
    dup_mask = exportable["Trailer ID"].duplicated(keep=False)
    dup_rows = exportable[dup_mask].sort_values("Trailer ID")

    if not dup_rows.empty:
        dup_ids = dup_rows["Trailer ID"].unique().tolist()
        raise ValueError(
            f"\n*** DUPLICATE EXPORTABLE SEMI-TRAILER IDs FOUND in '{file_path}' ***\n"
            f"Duplicate IDs: {dup_ids}\n\n"
            f"Rows with duplicates:\n{dup_rows[['Trailer ID','Region','ParkingSlotIdentifier']]}\n"
            "Each exportable trailer must have a unique ID.\n"
        )
   
    if simulation_start is None:
        simulation_start = pd.Timestamp.now().normalize() + pd.Timedelta(hours = PLANNING_START_HOUR)

    plans, leftovers = plan_all_regions_edf(
        trailers, equipment,
        service_time_override_min=None,
        simulation_start=simulation_start,
        end_of_day_hour=end_of_day_hour,
        seed=seed,
    )

    all_plans = pd.concat(plans.values(), ignore_index=True) if plans else pd.DataFrame()
    return all_plans, leftovers


import pandas as pd
from typing import Dict, Optional, Tuple
from scheduler_core import compute_lateness, END_OF_DAY_HOUR

def run_and_kpi_multiple_scenarios_edf(
    files: Dict[str, str],
    parking_table: str,
    equipment_table: str,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed: Optional[int] = 50,
    output_file: str = "MultipleScenarios_EDF.xlsx",
    write_leftovers: bool = True,
) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame]]:

    kpi_rows = []
    plans_dict = {}
    leftovers_rows = []

    for scenario_name, file_path in files.items():
        schedule_df, leftovers = run_scenario_edf(
            file_path,
            parking_table,
            equipment_table,
            simulation_start=simulation_start,
            end_of_day_hour=end_of_day_hour,
            seed=seed,
        )

        #### Computing KPIs and totals
        with_kpi, totals = compute_lateness(schedule_df)
        
#### ---- ASR calculation ----
        from scheduler_core import load_data, compute_asr

        #### Reloading the  full dataset (because run_scenario_edf does NOT return it)
        trailers, _ = load_data(file_path, parking_table, equipment_table)
        total_npd, npd_left, asr = compute_asr(trailers, leftovers, simulation_start)

        totals["ASR_total_not_past_due"] = total_npd
        totals["ASR_not_past_due_leftovers"] = npd_left
        totals["ASR"] = asr
        # -------------------------- 
        
        totals["Scenario"] = scenario_name
        kpi_rows.append(totals)
        plans_dict[scenario_name] = with_kpi

        for region, los in leftovers.items():
            for entry in los:
                leftovers_rows.append({
                    "Scenario": scenario_name,
                    "Region": region,
                    **entry        
                })

    #### Combining KPIs
    kpi_df = pd.DataFrame(kpi_rows)

    #### Combining plans
    all_plans_combined = pd.concat(
        [df.assign(Scenario = name) for name, df in plans_dict.items()],
        ignore_index=True
    ) if plans_dict else pd.DataFrame()
    

    #### Exporting to Excel (KPIs, Plans and Leftovers; 3 Sheets)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        kpi_df.to_excel(writer, sheet_name="KPIs", index=False)
        all_plans_combined.to_excel(writer, sheet_name="Plans", index=False)
        if write_leftovers:
            pd.DataFrame(leftovers_rows).to_excel(writer, sheet_name="Leftovers", index=False)
            
    #### Pushing the plans to the dbo.SchedulePlans and dbo.ScheduleLeftovers databases
    #### Overwrite = True means that the tables in SQL will be emptied (truncated) before being upserted any plans. 
    #### Upsert = Update + Insert 
    push_plans_excel_to_sql(output_file, overwrite = True)
    ####
    
    print("####")
    print(schedule_df.columns)
    print(with_kpi.columns)
    print("####")
    
    print(f"Exported EDF multi-scenario results to '{output_file}'")
    return kpi_df, plans_dict

#### Monte Carlo simulation; only used for variance calculation in the beginning, not relevant anymore unless,
#### a Monte Carlo variance analysis has to be conduced across all the algorithms.


'''
def monte_carlo_single_scenario_edf(
    file_path: str,
    parking_table: str,
    equipment_table: str,
    runs: int = 30,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed_base: int = 1000,
    scenario_name: Optional[str] = None,
    export_xlsx: Optional[str] = None,
) -> pd.DataFrame:
    """
    Monte Carlo for one scenario file using the GREEDY (EDF) scheduler.
    Returns a DataFrame with one row per run (KPIs).
    """
    from pathlib import Path
    label = scenario_name or Path(file_path).stem


    rows = []
    for run_idx in range(runs):
        seed = seed_base + run_idx

        schedule_df, _ = run_scenario_edf(
            file_path,
            parking_table,
            equipment_table,
            simulation_start=simulation_start,
            end_of_day_hour=end_of_day_hour,
            seed=seed,
        )
        with_kpi, totals = compute_lateness(schedule_df)
        rows.append({
            "Scenario": label,
            "Run": run_idx,
            "Seed": seed,
            **totals
        })

    df = pd.DataFrame(rows)

    if export_xlsx:
        df.to_excel(export_xlsx, index=False)
        print(f"Monte Carlo (single) exported to: {export_xlsx}")

    return df


#### Monte Carlo Simulation over Multiple Scenarios
def monte_carlo_scenarios_edf(
    files: Dict[str, str],                   # {"Low": "VersionS.xlsx", "Medium": "VersionM.xlsx", ...}
    parking_table: str,
    equipment_table: str,
    runs: int = 30,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed_base: int = 1000,
    export_xlsx: Optional[str] = None,
) -> pd.DataFrame:
    """
    Monte Carlo for multiple scenario files using GREEDY.
    Returns a DataFrame with one row per (scenario, run).
    """
    results = []

    for scenario_name, file_path in files.items():
        for run_idx in range(runs):
            seed = seed_base + run_idx
            print(f"SEED: {seed}")
            schedule_df, _ = run_scenario_edf(
                file_path,
                parking_table,
                equipment_table,
                simulation_start=simulation_start,
                end_of_day_hour=end_of_day_hour,
                seed=seed,
            )
            with_kpi, totals = compute_lateness(schedule_df)

            results.append({
                "Scenario": scenario_name,
                "Run": run_idx,
                "Seed": seed,
                **totals
            })

    df = pd.DataFrame(results)

    if export_xlsx:
        df.to_excel(export_xlsx, index=False)
        print(f"Monte Carlo (multi) exported to: {export_xlsx}")

    return df
'''

if __name__ == "__main__":
    #files = {"Very High" : "VersionXL.xlsx"} #### A single scenario file can also be run like this.
    PARK_SHEET = os.getenv('PARK_SHEET')
    EQUIP_SHEET = os.getenv("EQUIP_SHEET")
    START = pd.Timestamp(os.getenv("START"))
    files = json.loads(os.getenv("files"))
    

    
    #### Main starting point, the function is fully capable to run any number of scenarios, given they are mentioned in the "files" (in the .env) dict with their respective file names.
    kpi_df, plans = run_and_kpi_multiple_scenarios_edf(
        files, PARK_SHEET, EQUIP_SHEET, simulation_start = START,
        seed = 50,
        output_file = "MultipleScenarios_EDF.xlsx", 
        write_leftovers = True
    )

