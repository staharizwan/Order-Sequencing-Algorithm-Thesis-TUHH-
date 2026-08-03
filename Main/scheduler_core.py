import pandas as pd
import numpy as np
import random
from datetime import timedelta
from typing import Optional, Dict, Tuple, List
from pathlib import Path
from dotenv import load_dotenv
import os, json
load_dotenv()


PLANNING_START_HOUR = int(os.getenv("PLANNING_START_HOUR"))
END_OF_DAY_HOUR = int(os.getenv("END_OF_DAY_HOUR"))

#### DESTINATION_MAP: Dict[str, str]
DESTINATION_MAP = json.loads(os.getenv("DESTINATION_MAP"))

#### HT_PARAMS: Dict[str, Dict[str, float]] 
HT_PARAMS = json.loads(os.getenv("HT_PARAMS"))

#### Stochastic Handling must stay True to use stochastic handling time model
STOCHASTIC_HANDLING = True
#### The very first seed selected 42, not relevant anymore.
RANDOM_SEED: Optional[int] = 42

#### ---------------- Utilities ----------------

def _normalize_region_text(s: pd.Series) -> pd.Series:
    #### Removing multiple white space characters and replacing them with a single white space using regex = \s+
    #### Capitalizing the first alphabet of each word using .title()
    #### Removal of any trailing and leading white spaces
    return (s.astype(str).str.strip().str.replace(r"\s+", " ", regex=True).str.title())

def _parse_export_flag(series: pd.Series) -> pd.Series:
    def to_flag(x) -> int:
        if pd.isna(x):
            return 0
        s = str(x).strip().lower()
        if s in {"1", "true", "yes", "y", "export", "exp"}:
            return 1
        if s in {"0", "false", "no", "n", "import", "imp"}:
            return 0
        try:
            v = int(float(s))
            return int(v != 0)
        except Exception:
            return 0
    return series.map(to_flag).astype(int)

def _map_destination(series: pd.Series) -> pd.Series:
    def to_side(x) -> Optional[str]:
        if pd.isna(x):
            return None
        key = str(x).strip().upper()
        return DESTINATION_MAP.get(key)
    return series.map(to_side)



#### Qunatile-based priority generation
def _priority_from_due(due: pd.Series) -> pd.Series:
    """
    Priority 1 (earliest third), 2 (middle third), 3 (latest third).
    Uses count-weighted quantiles of the *full* due-date distribution,
    with robust fallbacks when quantiles collapse.
    """
    dt = pd.to_datetime(due, errors="coerce")
    idx = dt.index

    #### If all missing or a single unique date → gives low priority (3)
    nunique = dt.nunique(dropna=True)
    if nunique <= 1:
        return pd.Series(3, index=idx, dtype=int)

    #### Computes count-weighted quantiles from actual trailer due dates
    valid = dt.dropna()
    q = valid.quantile([1/3, 2/3])
    q1, q2 = q.iloc[0], q.iloc[1]

    #### If quantiles collapse (e.g., massed on same timestamp), falls back to rank-based qcut
    if pd.isna(q1) or pd.isna(q2) or q1 >= q2:
        try:
            #### print("Fallback 2")
            #### Ranking the full vector (counts preserved) then qcut into 3 bins
            ranks = valid.rank(method="first")
            bins = pd.qcut(ranks, q=3, labels=[1, 2, 3])
            out = pd.Series(3, index=idx, dtype=int)
            out.loc[valid.index] = bins.astype(int)
            return out
        except Exception:
            #### Last-resort: equal-width cut on ranks
            bins = pd.cut(valid.rank(method="first"), bins=3, labels=[1, 2, 3], include_lowest=True)
            out = pd.Series(3, index=idx, dtype=int)
            out.loc[valid.index] = bins.astype(int)
            return out

    #### Normal path: bin by time thresholds (count-weighted)
    def assign(x):
        if pd.isna(x): 
            return 3
        if x <= q1:
            return 1
        elif x <= q2:
            return 2
        else:
            return 3

    return dt.map(assign).astype(int)


def _handling_time_minutes(storage_region: str, dest_side: Optional[str], rng: Optional[random.Random]) -> float:
    sr = (storage_region or "").lower()
    ds = (dest_side or "").lower()
    if sr == "east":
        key = "east"
    elif dest_side is not None and sr.startswith(ds[:1].lower()):
        key = "same"
    else:
        key = "cross"

    mean = HT_PARAMS[key]["mean"]
    std = HT_PARAMS[key]["std"]
    if STOCHASTIC_HANDLING:
        #### Use of Gaussian distribution to introduce randomness to the handling times.
        #### 
        r = rng or random.Random(RANDOM_SEED)
        #print(f"r :{r}")
        val = r.normalvariate(mean, std)
        return max(1.0, float(val))
    return float(mean)

#### Loading the data

def read_table(file_path, table_name):
    return pd.read_excel(file_path, sheet_name=table_name)

def load_data(file_path, parking_table, equipment_table):
    trailers = read_table(file_path, parking_table)
    equipment = read_table(file_path, equipment_table)
    
    required_eq = {"Equipment", "Region", "Status"}
    if not required_eq.issubset(set(equipment.columns)):
        raise ValueError("The equipment table must contain columns: Equipment, Region, Status")

    if "Region" in trailers.columns:
        trailers["Region"] = _normalize_region_text(trailers["Region"])
    if "Region" in equipment.columns:
        equipment["Region"] = _normalize_region_text(equipment["Region"])

    if "Export" in trailers.columns:
        trailers["ExportFlag"] = _parse_export_flag(trailers["Export"])
    else:
        trailers["ExportFlag"] = 0

    if "Destination" in trailers.columns:
        trailers["DestSide"] = _map_destination(trailers["Destination"]).str.title()
    else:
        trailers["DestSide"] = None

    #### Working like an internal alias, names that are verbose and space-seperated are prone to being 
    #### misreferenced
    trailers["DueDate"] = trailers["Departure Due Date"]
    ####trailers["PriorityCode"] = _priority_from_due(trailers["DueDate"])
    
    trailers["PriorityCode"] = (trailers.groupby("Region", group_keys=False)["DueDate"].apply(_priority_from_due))

    
    if "Occupancy" in trailers.columns:
        trailers["OccupiedFlag"] = trailers["Occupancy"].astype(int) == 1
    else:
        trailers["OccupiedFlag"] = True

    #### Dropping the rows without parking slot
    if "ParkingSlotIdentifier" in trailers.columns:
        trailers = trailers[trailers["ParkingSlotIdentifier"].notna()]

    active_equipment = equipment[equipment["Status"].astype(str).str.strip().str.title() == "Available"].copy()
    return trailers, active_equipment

#### Planning
def region_plan(
    region: str,
    region_trailers: pd.DataFrame,
    equipment_for_region: pd.DataFrame,
    service_time_override_min: Optional[int] = None,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed: Optional[int] = None,
    eq_clock_shared: Optional[Dict[str, pd.Timestamp]] = None  
):
    rng = random.Random(seed)

    if region_trailers.empty or equipment_for_region.empty:
        return pd.DataFrame(), region_trailers.get("Trailer ID", pd.Series([], dtype=object)).tolist()

    if simulation_start is None:
        today = pd.Timestamp.now().normalize()
        simulation_start = today + pd.Timedelta(hours=PLANNING_START_HOUR)
    horizon_end = simulation_start.normalize() + pd.Timedelta(hours=end_of_day_hour)

    #### Handling time per semi-trailer
    def row_time(row) -> float:
        if service_time_override_min is not None:
            return float(service_time_override_min)
        return _handling_time_minutes(row.get("Region"), row.get("DestSide"), rng)

    region_trailers = region_trailers.copy()
    region_trailers["ExpectedServiceMin"] = region_trailers.apply(row_time, axis=1)

    #### Random order of semi-trailers
    region_trailers = region_trailers.sample(frac=1.0, random_state = rng.randint(0, 10**9)).reset_index(drop=True)

    #### Setting up the equipment
    eq_list = equipment_for_region["Equipment"].tolist()

    #### Shared clocks
    if eq_clock_shared is not None:
        eq_clock = eq_clock_shared
    else:
        eq_clock = {eq: simulation_start for eq in eq_list}

    seq_counter = {eq: 1 for eq in eq_list}

    assignments = []
    leftovers = []

    #### Random scheduling loop : NOT used anymore, was only executed as a Monte Carlo reference
    for _, tr in region_trailers.iterrows():
        chosen_eq = rng.choice(eq_list)  #### random equipment choice

        start_time = eq_clock[chosen_eq]
        minutes_needed = float(tr["ExpectedServiceMin"])
        finish_time = start_time + timedelta(minutes=minutes_needed)

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
            eq_clock[chosen_eq] = finish_time  #### updated clock
        else:
            leftovers.append({
            "SemiTrailerID": tr.get("Trailer ID"),
            "Region": region,
            "ParkingSlot": tr.get("ParkingSlotIdentifier"),
            "DepartureDueDate": tr.get("Departure Due Date")
            
        })


    out = pd.DataFrame(assignments).sort_values(["Equipment", "SequencePos"]).reset_index(drop=True)
    return out, leftovers

def plan_all_regions(
    trailers: pd.DataFrame,
    equipment: pd.DataFrame,
    service_time_override_min: Optional[int] = None,
    simulation_start = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed: Optional[int] = None
):
    cand = trailers[
        (trailers["ExportFlag"] == 1) &
        (trailers["OccupiedFlag"]) &
        (trailers["ParkingSlotIdentifier"].notna())
    ].copy()

    plans: Dict[str, pd.DataFrame] = {}
    leftovers: Dict[str, List] = {}

    eq_north = equipment[equipment["Region"].str.startswith("North")]
    eq_south = equipment[equipment["Region"].str.startswith("South")]

    #### Shared clocks 
    eq_clock_north = {eq: simulation_start for eq in eq_north["Equipment"].tolist()}
    eq_clock_south = {eq: simulation_start for eq in eq_south["Equipment"].tolist()}

    for region, region_trailers in cand.groupby("Region"):
        if region.startswith("East"):
            east_north = region_trailers[region_trailers["DestSide"] == "North"]
            east_south = region_trailers[region_trailers["DestSide"] == "South"]

            plan_north, lo_north = region_plan(
                "East→North", east_north, eq_north,
                service_time_override_min=service_time_override_min,
                simulation_start=simulation_start,
                end_of_day_hour=end_of_day_hour,
                seed=seed,
                eq_clock_shared=eq_clock_north
            )

            plan_south, lo_south = region_plan(
                "East→South", east_south, eq_south,
                service_time_override_min=service_time_override_min,
                simulation_start=simulation_start,
                end_of_day_hour=end_of_day_hour,
                seed=seed,
                eq_clock_shared=eq_clock_south
            )

            plans[region] = pd.concat([plan_north, plan_south], ignore_index=True)
            leftovers[region] = lo_north + lo_south

        elif region.startswith("North"):
            plan, lo = region_plan(
                "North", region_trailers, eq_north,
                service_time_override_min=service_time_override_min,
                simulation_start=simulation_start,
                end_of_day_hour=end_of_day_hour,
                seed=seed,
                eq_clock_shared=eq_clock_north
            )
            plans[region] = plan
            leftovers[region] = lo

        elif region.startswith("South"):
            plan, lo = region_plan(
                "South", region_trailers, eq_south,
                service_time_override_min=service_time_override_min,
                simulation_start=simulation_start,
                end_of_day_hour=end_of_day_hour,
                seed=seed,
                eq_clock_shared=eq_clock_south
            )
            plans[region] = plan
            leftovers[region] = lo

        else:
            plans[region] = pd.DataFrame()
            leftovers[region] = region_trailers.get("Trailer ID", pd.Series([], dtype=object)).tolist()

    return plans, leftovers


#### ---------------- KPIs ----------------

def compute_lateness(assignments: pd.DataFrame):
    if assignments.empty:
        return assignments, {"TotalSemiTrailers": 0, "TotalLatenessMin": 0.0, "TotalWeightedLateness": 0.0,
                             "OnTimeCount": 0, "LateCount": 0}
    df = assignments.copy()

    def lat(row):
        dd = row.get("DepartureDueDate")
        if pd.isna(dd):
            return 0.0
        return max(0.0, (row["FinishTime"] - dd).total_seconds() / 60.0)

    df["LatenessMin"] = round(df.apply(lat, axis=1), 2)
    df["PriorityFactor"] = (df["PriorityCode"].max() + 1 - df["PriorityCode"]).clip(lower=1)
    df["WeightedLateness"] = round(df["LatenessMin"] * df["PriorityFactor"], 2)
    #### Priority factor is high (bigger number) for jobs that are urgent, this is an inversion technique employed
    #### to somehow make sure that any late semi-trailer order is penalised more.
    totals = {
        "TotalSemiTrailers": int(len(df)),
        "TotalLatenessMin": round(float(df["LatenessMin"].sum()), 2),
        "MeanLatenessMin" : round(float(df["LatenessMin"].mean()), 2),
        "AverageWeightedLateness": round(df["WeightedLateness"].sum()/(df["PriorityFactor"].sum()),2),
        "OnTimeCount": int((df["LatenessMin"] == 0).sum()),
        "LateCount": int((df["LatenessMin"] > 0).sum()),
        "OnTimeRatio": round(int((df["LatenessMin"] == 0).sum())/int(len(df)), 4),
    }

    #### Calculating the total service time
    totals.update(calculate_train_service_time(df))

    return df, totals

##### ASR
def compute_asr(trailers_df: pd.DataFrame, leftovers, simulation_start: pd.Timestamp):


    if trailers_df.empty:
        return 0, 0, None

    cand = trailers_df[
        (trailers_df["ExportFlag"] == 1) &
        (trailers_df["OccupiedFlag"]) &
        (trailers_df["ParkingSlotIdentifier"].notna())
    ].copy()

    if cand.empty:
        return 0, 0, None

    #### Finding the correct due-date column
    if "DepartureDueDate" in cand.columns:
        due_col = "DepartureDueDate"
    elif "Departure Due Date" in cand.columns:
        due_col = "Departure Due Date"
    elif "DueDate" in cand.columns:
        due_col = "DueDate"
    else:
        raise KeyError("No due date column found for ASR computation.")

    #### Denominator: not-past-due exportable trailers ---
    not_past_due_ids = cand[cand[due_col] >= simulation_start]["Trailer ID"].tolist()
    total_not_past_due = len(not_past_due_ids)


    flat_leftovers = []
    if isinstance(leftovers, dict):
        for region, entries in leftovers.items():
            for entry in entries:
                if isinstance(entry, dict):
                    flat_leftovers.append(entry.get("SemiTrailerID"))
                else:
                    flat_leftovers.append(entry)
    else:
        flat_leftovers = list(leftovers)

    #### Numerator: how many of the not-past-due candidates were sacrificed
    npd_leftovers = sum(tid in flat_leftovers for tid in not_past_due_ids)

    asr = npd_leftovers / total_not_past_due if total_not_past_due > 0 else None

    return total_not_past_due, npd_leftovers, asr



#### ------- Export helpers (with KPIs) ----------


def export_plans_with_kpis(all_plans_df: pd.DataFrame,
                           leftovers: Dict[str, List],
                           output_file: str = "DailyPlans.xlsx") -> str:
    """Writes per-Region sheets, Leftovers, and KPIs (overall + per-region)."""
    with_kpi, totals = compute_lateness(all_plans_df)
    
    #### Ensuring seamless output for the GA---------
    all_plans_df = all_plans_df.copy()
    if "Equipment" not in all_plans_df.columns and "Tug" in all_plans_df.columns:
        all_plans_df["Equipment"] = all_plans_df["Tug"]

    if "SequencePos" not in all_plans_df.columns:
        #### Assigning sequential numbers per Equipment for sorting consistency
        all_plans_df["SequencePos"] = (
            all_plans_df.groupby("Equipment").cumcount() + 1
        )
    ####--------------------------------------------

    per_region_rows = []
    if not all_plans_df.empty and "Region" in all_plans_df.columns:
        for region, df_r in all_plans_df.groupby("Region"):
            _, t = compute_lateness(df_r)
            t["Region"] = region
            per_region_rows.append(t)
    cols_to_drop = ["NorthTrainFurnishingTime", "SouthTrainFurnishingTime"]
    per_region_df = pd.DataFrame(per_region_rows) if per_region_rows else pd.DataFrame()
    per_region_df = per_region_df.drop(columns = [c for c in cols_to_drop if c in per_region_df.columns])
        
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        if not all_plans_df.empty and "Region" in all_plans_df.columns:
            for region, plan in all_plans_df.groupby("Region"):
                plan = plan.sort_values(["Equipment", "SequencePos"], kind="mergesort")
                plan.to_excel(writer, sheet_name=str(region)[:31], index=False)

        leftovers_rows = [
            {"Region": reg, "SemiTrailerID": tid}
            for reg, ids in (leftovers or {}).items()
            for tid in ids
        ]
        pd.DataFrame(leftovers_rows).to_excel(writer, sheet_name="Leftovers", index=False)

        kpi_overall = pd.DataFrame([totals])
        kpi_overall.to_excel(writer, sheet_name="KPIs", index = False, startrow = 0)
        if not per_region_df.empty:
            pd.DataFrame([{"PerRegion": ""}]).to_excel(writer, sheet_name="KPIs", index=False, startrow = len(kpi_overall) + 2)
            #print(str(len(kpi_overall)) + " = Overall KPI Length")
            per_region_df.to_excel(writer, sheet_name = "KPIs", index = False, startrow = len(kpi_overall) + 4)

    print(f"\n Plans + KPIs exported to {output_file}")
    return output_file



def run_scenario(file_path: str,
                 parking_table: str,
                 equipment_table: str,
                 simulation_start: Optional[pd.Timestamp] = None,
                 end_of_day_hour: int = END_OF_DAY_HOUR,
                 seed: Optional[int] = 42):
    trailers, equipment = load_data(file_path, parking_table, equipment_table)

    if simulation_start is None:
        simulation_start = pd.Timestamp.now().normalize() + pd.Timedelta(hours = PLANNING_START_HOUR)

    plans, leftovers = plan_all_regions(
        trailers, equipment,
        service_time_override_min = None,
        simulation_start = simulation_start,
        end_of_day_hour = end_of_day_hour,
        seed = seed,
    )

    all_plans = pd.concat(plans.values(), ignore_index=True) if plans else pd.DataFrame()
    return all_plans, leftovers


def run_multiple_scenarios(files: Dict[str, str],
                           parking_table: str,
                           equipment_table: str,
                           simulation_start: Optional[pd.Timestamp] = None,
                           end_of_day_hour: int = END_OF_DAY_HOUR,
                           seed: Optional[int] = 42) -> Dict[str, pd.DataFrame]:
    outputs = {}
    for scenario_name, path in files.items():
        df, _ = run_scenario(path, parking_table, equipment_table, simulation_start, end_of_day_hour, seed)
        outputs[scenario_name] = df
    return outputs


#### The default seed used in the function is 50, it should stay as is since it doesn't have any profound impact
#### on the results.
def run_and_kpi_multiple_scenarios(
    files: Dict[str, str],
    parking_table: str,
    equipment_table: str,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed: Optional[int] = 50,
    output_file: str = "MultipleScenariosWithKPIs.xlsx"
) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame], pd.DataFrame]:
    """
    This function can multiple (single file works too) scenario files once each and return combined KPIs.
    #### Each scenario was a workload condition.
    Also writes:
      - KPIs to sheet 'KPIs'
      - All plans to sheet 'Plans'
      - Leftovers to sheet 'Leftovers'

    Returns:
        - KPI dataframe
        - Dict of scenario_name -> plans dataframe
        - Leftovers dataframe
    """
    kpi_rows = []
    plans_dict = {}
    leftovers_rows = []

    for scenario_name, file_path in files.items():
        schedule_df, leftovers = run_scenario(
            file_path,
            parking_table,
            equipment_table,
            simulation_start=simulation_start,
            end_of_day_hour=end_of_day_hour,
            seed=seed,
        )
        #### Runs for every scenario
        with_kpi, totals = compute_lateness(schedule_df)
        totals["Scenario"] = scenario_name
        
        trailers, _ = load_data(file_path, parking_table, equipment_table)
        total_npd, npd_left, asr = compute_asr(trailers, leftovers, simulation_start)

        totals["ASR_total_not_past_due"] = total_npd
        totals["ASR_not_past_due_leftovers"] = npd_left
        totals["ASR"] = asr
        
        kpi_rows.append(totals)
        plans_dict[scenario_name] = with_kpi

        #### New export logic WITH parking spots
        for region, entries in leftovers.items():
            for entry in entries:
                leftovers_rows.append({
                    "Scenario": scenario_name,
                    "Region": entry.get("Region", region),
                    "SemiTrailerID": entry.get("SemiTrailerID"),
                    "ParkingSlot": entry.get("ParkingSlot"),
                    "DepartureDueDate" : entry.get("DepartureDueDate")
                })
                
    #### End of loop
    kpi_df = pd.DataFrame(kpi_rows)
    all_plans_combined = pd.concat(
        [df.assign(Scenario = name) for name, df in plans_dict.items()],
        ignore_index=True
    )
    leftovers_df = pd.DataFrame(leftovers_rows)

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        kpi_df.to_excel(writer, sheet_name="KPIs", index=False)
        all_plans_combined.to_excel(writer, sheet_name="Plans", index=False)
        if not leftovers_df.empty:
            leftovers_df.to_excel(writer, sheet_name="Leftovers", index=False)

    print(f"\n Exported KPIs, plans, and leftovers to '{output_file}'")
    
    print("####")
    print(schedule_df.columns)
    print(with_kpi.columns)
    print("####")
    return kpi_df, plans_dict, leftovers_df

'''
# ---------------- Monte Carlo-Single Scenario ------------------
def monte_carlo_single_scenario(
    file_path: str,
    parking_table: str,
    equipment_table: str,
    runs: int = 45,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed_base: int = 1000,
    scenario_name: Optional[str] = None
) -> pd.DataFrame:
    """
    Monte Carlo simulation for a single scenario file.

    Args:
        file_path: Path to Excel file.
        parking_table: Sheet name for parking data.
        equipment_table: Sheet name for equipment.
        runs: Number of simulations to run.
        simulation_start: Planning start time (optional).
        seed_base: Base seed for RNG.
        scenario_name: Optional label (e.g., "Low")

    Returns:
        DataFrame of KPIs per run.
    """
    scenario_label = scenario_name or Path(file_path).stem
    results = []

    for run_idx in range(runs):
        seed = seed_base + run_idx
        print(f"Seed base: {seed_base}, seed: {seed}")
        schedule_df, _ = run_scenario(
            file_path,
            parking_table,
            equipment_table,
            simulation_start=simulation_start,
            end_of_day_hour = end_of_day_hour,
            seed = seed,
        )

        with_kpi, totals = compute_lateness(schedule_df)

        results.append({
            "Scenario": scenario_label,
            "Run": run_idx,
            "Seed": seed,
            **totals
        })

    return pd.DataFrame(results)

# ---------------- MonteCarlo-Multiple Scenarios ----------------

def monte_carlo_scenarios(
    files: Dict[str, str],
    parking_table: str,
    equipment_table: str,
    runs: int = 30,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = END_OF_DAY_HOUR,
    seed_base: int = 1000,
) -> pd.DataFrame:
    """
    Run Monte Carlo simulations for multiple scenarios.

    Args:
        files: Dict of scenario name -> Excel path.
        parking_table: Sheet name for parking spots.
        equipment_table: Sheet name for equipment.
        runs: How many times to simulate each scenario.
        simulation_start: Start datetime for planning (same for all).
        seed_base: Base seed to offset randomness.

    Returns:
        DataFrame of KPIs per run, per scenario.
    """
    results = []

    for scenario_name, file_path in files.items():
        for run_idx in range(runs):
            seed = seed_base + run_idx
            print(f"SEED: {seed}")
            schedule_df, _ = run_scenario(
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
    return df
'''
#### Calculating the train service time
def calculate_train_service_time(assignments: pd.DataFrame) -> Dict[str, Optional[pd.Timestamp]]:

    if assignments.empty:
        return {
            "NorthTrainFinishTime": None,
            "SouthTrainFinishTime": None,
        }

    north_finish = assignments.loc[
        assignments["DestSide"] == "North", "FinishTime"
    ].max()
    
    north_start = assignments.loc[assignments["DestSide"] == "North", "StartTime"
    ].min()

    south_finish = assignments.loc[
        assignments["DestSide"] == "South", "FinishTime"
    ].max()
    
    south_start = assignments.loc[
        assignments["DestSide"] == "South", "StartTime"
    ].min()
    

    return {
        "NorthTrainFurnishingTime": (north_finish - north_start).total_seconds() / 60 if pd.notnull(north_finish) and pd.notnull(north_start) else None,
        "SouthTrainFurnishingTime": (south_finish - south_start).total_seconds() / 60 if pd.notnull(south_finish) and pd.notnull(south_start) else None,
    }

#### Computing the service times for multiple files
def compute_train_furnishing_times(assignments: pd.DataFrame) -> Dict[str, float]:
    
    results = {}

    if not assignments.empty and "DestSide" in assignments.columns:
        for side in ["North", "South"]:
            side_df = assignments[assignments["DestSide"] == side]
            if not side_df.empty:
                start = side_df["StartTime"].min()
                finish = side_df["FinishTime"].max()
                if pd.notnull(start) and pd.notnull(finish):
                    duration_min = (finish - start).total_seconds() / 60.0
                    results[f"{side}TrainFurnishingTime"] = duration_min
                else:
                    results[f"{side}TrainFurnishingTime"] = None
            else:
                results[f"{side}TrainFurnishingTime"] = None
    else:
        results = {
            "NorthTrainFurnishingTime": None,
            "SouthTrainFurnishingTime": None,
        }

    return results


#### ---------------- Main ----------------
if __name__ == "__main__":
    FILE = "./VersionL.xlsx"
    PARK_SHEET = os.getenv('PARK_SHEET')
    EQUIP_SHEET = os.getenv("EQUIP_SHEET")
    START = pd.Timestamp(os.getenv("START"))
    files = json.loads(os.getenv("files"))
    
    

    #### For Multiple Scenarios
    run_and_kpi_multiple_scenarios(
        files, PARK_SHEET, EQUIP_SHEET, simulation_start = START)
    
    
    #### The code below belongs to an earlier Monte Carlo trial,
    #### this has no influence over scheduling and must not be uncommented.
    
    '''
    monte_df = monte_carlo_scenarios(
        files = files,
        parking_table = PARK_SHEET,
        equipment_table = EQUIP_SHEET,
        runs = 50,
        simulation_start = START
    )

    # Save to Excel for analysis
    monte_df.to_excel("MonteCarloResults.xlsx", index = False)
    
    '''