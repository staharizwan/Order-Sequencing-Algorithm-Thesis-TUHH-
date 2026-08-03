import pandas as pd
import numpy as np
import random
import time
import os
from datetime import timedelta
from typing import Optional, Dict, Tuple, List
from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict
from dotenv import load_dotenv
import json
import copy
from scheduler_core import load_data, compute_asr

from Terminal_DB.sql_exporter import push_plans_excel_to_sql

load_dotenv()


def _evaluate_individual(args):
    (chrom, trailers_df, simulation_start, end_of_day_hour, eval_seed,
     eq_clock_north_init, eq_clock_south_init) = args
    return decode_sequence(
        chrom, trailers_df, simulation_start, end_of_day_hour, eval_seed,
        eq_clock_north_init, eq_clock_south_init
    )

from scheduler_core import (
    load_data,
    _handling_time_minutes,
    compute_lateness,
    calculate_train_service_time,
    export_plans_with_kpis,
    _map_destination,
)

class GAParams:
    def __init__(self,
                 population_size: int = 120,
                 generations: int = 120,
                 crossover_rate: float = 0.9,
                 mutation_rate: float = 0.08,
                 elitism_fraction: float = 0.06,
                 tournament_k: int = 4,
                 no_improve_patience: int = 30):
        self.population_size = population_size
        self.generations = generations
        self.crossover_rate = crossover_rate
        self.mutation_rate = mutation_rate
        self.elitism_fraction = elitism_fraction
        self.tournament_k = tournament_k
        self.no_improve_patience = no_improve_patience

GA_PARAMS = GAParams()

def seed_population(trailer_ids: List, pop_size: int, rng: random.Random) -> List[List]:
    population = []
    for _ in range(pop_size):
        p = list(trailer_ids)
        rng.shuffle(p)
        population.append(p)
    return population

def ox_crossover(p1: List, p2: List, rng: random.Random) -> Tuple[List, List]:

    n = len(p1)
    if n < 2:
        return p1[:], p2[:]
    a, b = sorted([rng.randrange(n), rng.randrange(n)])
    if a == b:
        b = (a + 1) % n

    def _child(pa, pb):
        child = [None] * n
        child[a:b+1] = pa[a:b+1]
        fill = [g for g in pb if g not in child]
        if not fill:  # identical parents
            return pa[:]
        idx = 0
        for i in range(n):
            if child[i] is None:
                child[i] = fill[idx % len(fill)]
                idx += 1
        return child

    return _child(p1, p2), _child(p2, p1)

def mutate_swap(chrom: List, rng: random.Random):
    i, j = rng.randrange(len(chrom)), rng.randrange(len(chrom))
    chrom[i], chrom[j] = chrom[j], chrom[i]

def mutate_shuffle_segment(chrom: List, rng: random.Random, max_seg: int = 6):
    n = len(chrom)
    a = rng.randrange(n)
    b = min(n - 1, a + rng.randrange(2, max_seg + 1))
    seg = chrom[a:b+1]
    rng.shuffle(seg)
    chrom[a:b+1] = seg

def tournament_select(pop: List[List], fit: List[float], k: int, rng: random.Random) -> List:
    idxs = [rng.randrange(len(pop)) for _ in range(k)]
    return pop[max(idxs, key=lambda i: fit[i])]

def decode_sequence(
    sequence: List,
    trailers_df: pd.DataFrame,
    simulation_start: pd.Timestamp,
    end_of_day_hour: int,
    eval_seed: Optional[int] = None,
    eq_clock_north_init: Optional[Dict[str, pd.Timestamp]] = None,
    eq_clock_south_init: Optional[Dict[str, pd.Timestamp]] = None,
) -> Tuple[pd.DataFrame, Dict[str, float], float, List[Dict[str, str]]]:
    """
    Decoding a chromosome into a schedule using ACTUAL equipment IDs with shared clocks.
    Returns: (schedule_df_with_kpi_cols, totals, fitness, leftovers_list_of_dicts)
    """
    rng = random.Random(eval_seed)

    # Local copies of equipment clocks (so each individual is independent)
    eq_clock_north = copy.deepcopy(eq_clock_north_init or {})
    eq_clock_south = copy.deepcopy(eq_clock_south_init or {})

    horizon_end = simulation_start.normalize() + pd.Timedelta(hours=end_of_day_hour)
    records: List[Dict] = []
    leftovers: List[Dict[str, str]] = []
    used_slots = set()

    for tid in sequence:
        row = trailers_df.loc[trailers_df["Trailer ID"] == tid].iloc[0]
        region_in = str(row.get("Region", "")).title()

        #### Robust DestSide design, handling incomplete datasets
        dest_side = row.get("DestSide")
        if pd.isna(dest_side) or dest_side is None:
            dest_val = row.get("Destination")
            if dest_val is not None:
                try:
                    mapped = _map_destination(pd.Series([dest_val])).iloc[0]
                    if pd.notna(mapped):
                        dest_side = str(mapped).title()
                except Exception:
                    dest_side = None

        #### Due date (using DepartureDueDate column name expected by KPI code)
        due = (
            pd.Timestamp(row["Departure Due Date"])
            if "Departure Due Date" in row
            else pd.Timestamp(row.get("DepartureDueDate"))
        )

        handle = _handling_time_minutes(row.get("Region"), dest_side, rng)
        prio_code = int(row.get("PriorityCode", 3))
        parking_slot = row.get("ParkingSlotIdentifier")
        

        if pd.notna(parking_slot) and parking_slot in used_slots:
            leftovers.append({"SemiTrailerID": tid, "Region": region_in, "ParkingSlot": parking_slot, "DepartureDueDate":due })
            continue
        
        #### Choosing equipment with the earliest availability
        chosen_eq = None
        start = None
        end = None
        next_region = None

        if region_in.startswith("North") and eq_clock_north:
            chosen_eq = min(eq_clock_north, key=eq_clock_north.get)
            start = eq_clock_north[chosen_eq]
            end = start + timedelta(minutes=handle)
            next_region = "North"

        elif region_in.startswith("South") and eq_clock_south:
            chosen_eq = min(eq_clock_south, key=eq_clock_south.get)
            start = eq_clock_south[chosen_eq]
            end = start + timedelta(minutes=handle)
            next_region = "South"
        
        else:
            best_n = min(eq_clock_north.values()) if eq_clock_north else None
            best_s = min(eq_clock_south.values()) if eq_clock_south else None

            def assign_north():
                nonlocal chosen_eq, start, end, next_region
                chosen_eq = min(eq_clock_north, key=eq_clock_north.get)
                start = eq_clock_north[chosen_eq]
                end = start + timedelta(minutes=handle)
                next_region = "East→North"

            def assign_south():
                nonlocal chosen_eq, start, end, next_region
                chosen_eq = min(eq_clock_south, key=eq_clock_south.get)
                start = eq_clock_south[chosen_eq]
                end = start + timedelta(minutes=handle)
                next_region = "East→South"

            dest_norm = (str(dest_side).strip().title() if dest_side is not None else None)

            if dest_norm == "North" and eq_clock_north:
                assign_north()
            elif dest_norm == "South" and eq_clock_south:
                assign_south()
            else:
                if best_n is not None and (best_s is None or best_n <= best_s):
                    assign_north()
                elif best_s is not None:
                    assign_south()
                else:
                    #print("EQUIP shortabge")
                    leftovers.append({"SemiTrailerID": tid, "Region": region_in, "ParkingSlot": parking_slot, "DepartureDueDate":due })
                    continue

        #### Horizon enforcement
        if end > horizon_end:
            leftovers.append({"SemiTrailerID": tid, "Region": next_region or region_in, "ParkingSlot": parking_slot, "DepartureDueDate":due })
            continue

        #### Committing the clock update
        if chosen_eq in eq_clock_north:
            eq_clock_north[chosen_eq] = end
        elif chosen_eq in eq_clock_south:
            eq_clock_south[chosen_eq] = end

        if pd.notna(parking_slot):
            used_slots.add(parking_slot)

        records.append({
            "Region": next_region,
            "Equipment": chosen_eq,                      
            "ParkingSlot": parking_slot,
            "SemiTrailerID": tid,
            "PriorityCode": prio_code,
            "DepartureDueDate": due,
            "DestSide": dest_side,
            "ExpectedServiceMin": round(float(handle), 2),
            "StartTime": start,
            "FinishTime": end,
        })

    schedule_df = pd.DataFrame(records)
    if not schedule_df.empty:
        schedule_df["SequencePos"] = schedule_df.groupby("Equipment").cumcount() + 1

    with_kpi, totals = compute_lateness(schedule_df)
    weighted_key = "TotalWeightedLateness" if "TotalWeightedLateness" in totals else "AverageWeightedLateness" 
    fitness = -totals.get("TotalLatenessMin", 0) - 0.7 * totals.get(weighted_key, 0)
    return with_kpi, totals, fitness, leftovers

def run_ga(
    trailers_df: pd.DataFrame,
    simulation_start: pd.Timestamp,
    end_of_day_hour: int,
    params: GAParams,
    rng: random.Random,
    eq_clock_north_init: Dict[str, pd.Timestamp],
    eq_clock_south_init: Dict[str, pd.Timestamp],
):
    start_time = time.perf_counter()
    import copy
    trailer_ids = trailers_df["Trailer ID"].tolist()
    #### Preventing duplication in order to save computational resources
    dup_ids = (
    pd.Series(trailer_ids)
    .loc[pd.Series(trailer_ids).duplicated()]
    .unique()
    )
    #### Throwing an error upon finding any duplicate semi-trailer IDs
    if len(dup_ids) > 0:
        raise ValueError(
            f"Duplicate semi-trailer IDs detected in GA input: {list(dup_ids)}\n"
            "Each semi-trailer must appear exactly once in the exportable dataset."
        )
    
    #### Each chromosome is a unique permutation (no duplication)
    pop = [rng.sample(trailer_ids, len(trailer_ids)) for _ in range(params.population_size)]

    #### Ensure each chromosome is an independent copy to prevent shared references during mutation/crossover
    pop = [copy.deepcopy(c) for c in pop]

    #### Bulletproofing against duplication (though from the previous design): do not delete!
    for i, chrom in enumerate(pop):
        if len(chrom) != len(set(chrom)):
            print(f"Initial Chromosome {i} has duplicates!")
            
    print(len(pop))
    best = None
    best_fit = -float("inf")
    best_hist = []
    no_improve = 0

    for gen in range(params.generations):
        #### Parallel evaluation to minimize the runtime, fitness evaluation is computationally-expensive
        #### Each chromosome is decoded independently using a separate random seed, prevents bias
        seeds_for_eval = [rng.randrange(10**9) for _ in pop]
        max_workers = min(4, os.cpu_count() or 2)
        
        #### ProcessPoolExecutor is used to distribute evaluation across available CPU cores.
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(
                _evaluate_individual,
                [
                    (
                        chrom, trailers_df, simulation_start, end_of_day_hour, seed,
                        eq_clock_north_init, eq_clock_south_init
                    )
                    for chrom, seed in zip(pop, seeds_for_eval)
                ]
            ))

        fits = [fit for _, _, fit, _ in results]
        print(f"Generation {gen+1:03d} | Best: {max(fits):10.4f} | Mean: {np.mean(fits):10.4f} | Worst: {min(fits):10.4f}")
        gen_best_idx = int(np.argmax(fits))

        if fits[gen_best_idx] > best_fit:
            best_fit = fits[gen_best_idx]
            best = results[gen_best_idx]
            no_improve = 0
        else:
            no_improve += 1

        best_hist.append(best_fit)
        if no_improve >= params.no_improve_patience:
            break
        
        
        elite_count = max(1, int(params.elitism_fraction * len(pop)))
        elite_idxs = list(np.argsort(fits))[-elite_count:][::-1]

        #### Deep-copy of the elites to prevent any accidental shared references
        elites = [copy.deepcopy(pop[i]) for i in elite_idxs]

        new_pop = []
        while len(new_pop) < len(pop) - elite_count:
            p1 = tournament_select(pop, fits, params.tournament_k, rng)
            p2 = tournament_select(pop, fits, params.tournament_k, rng)

            if rng.random() < params.crossover_rate:
                c1, c2 = ox_crossover(p1, p2, rng)
            else:
                c1, c2 = p1[:], p2[:]

            if rng.random() < params.mutation_rate:
                mutate_swap(c1, rng)
            if rng.random() < params.mutation_rate:
                mutate_shuffle_segment(c1, rng)

            if rng.random() < params.mutation_rate:
                mutate_swap(c2, rng)
            if rng.random() < params.mutation_rate:
                mutate_shuffle_segment(c2, rng)

            #### Deep-copy of children before adding
            new_pop.append(copy.deepcopy(c1))
            if len(new_pop) < len(pop) - elite_count:
                new_pop.append(copy.deepcopy(c2))


        #### Combining the elites + new generation, using deep copy again to isolate references
        pop = copy.deepcopy(elites + new_pop)

        #### Another integrity check after a chromosome has been generated
        for i, chrom in enumerate(pop):
            if len(chrom) != len(set(chrom)):
                print(f" Generation {gen+1}, Chromosome {i} has duplicates!")

    
    print(f"GA stopped at generation {gen + 1} (Best fitness: {best_fit:.4f})")
    runtime_min = (time.perf_counter() - start_time) / 60.0
    assert best is not None
    best_kpis = best[1]
    best_kpis["RuntimeMinutes"] = round(runtime_min, 3)

    return best[0], best_kpis, best_hist, best[3]

def run_scenario_ga(
    file_path: str,
    parking_table: str,
    equipment_table: str,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = 21,
    seed: Optional[int] = 50,
    params: GAParams = GA_PARAMS
) -> Tuple[pd.DataFrame, Dict[str, float], Dict[str, List]]:
    trailers_df, equipment_df = load_data(file_path, parking_table, equipment_table)

    #### Filtering to get export semi-trailers only
    trailers_df = trailers_df[
        (trailers_df["ExportFlag"] == 1)
        & (trailers_df["OccupiedFlag"])
        & (trailers_df["ParkingSlotIdentifier"].notna())
    ].copy()
    
    trailers_df["Region"] = (
        trailers_df["Region"]
        .astype(str)
        .str.strip()
        .replace(["nan", "NaN", "Nan", ""], pd.NA)
    )
    #### Checking for missing regions
    missing_region = trailers_df[trailers_df["Region"].isna()]
    if not missing_region.empty:
        raise ValueError(
            "Invalid input data: exportable semi-trailers with missing Region:\n"
            + missing_region[["Trailer ID", "ParkingSlotIdentifier"]].to_string(index=False)
        )
    
    #### Checking for invalid regions
    allowed_prefixes = ("North", "South", "East")
    bad_region_rows = trailers_df[~trailers_df["Region"].astype(str).str.startswith(allowed_prefixes, na=False)]

    if not bad_region_rows.empty:
        bad_vals = bad_region_rows["Region"].unique().tolist()
        raise ValueError(
            "Invalid Region labels detected (must start with North/South/East).\n"
            f"Found: {bad_vals}\n"
            + bad_region_rows[["Trailer ID", "Region", "ParkingSlotIdentifier"]].to_string(index=False)
        )

    ####
    
    #### Setting default planning start if needed
    if simulation_start is None:
        #### Automatically starting the day at 9am
        simulation_start = pd.Timestamp.now().normalize() + pd.Timedelta(hours = 9)

    #### Building clocks 
    eq_north = equipment_df[equipment_df["Region"].str.startswith("North")]
    eq_south = equipment_df[equipment_df["Region"].str.startswith("South")]
    eq_clock_north_init = {eq: simulation_start for eq in eq_north["Equipment"].tolist()}
    eq_clock_south_init = {eq: simulation_start for eq in eq_south["Equipment"].tolist()}

    rng = random.Random(seed)
    sched, kpis, hist, leftovers_list = run_ga(
        trailers_df, simulation_start, end_of_day_hour, params, rng,
        eq_clock_north_init, eq_clock_south_init
    )
    #### Computing the ASR for the GA

    #### Reloading the dataset
    trailers_full, _ = load_data(file_path, parking_table, equipment_table)

    #### Flattening GA leftovers (list of dicts) : list of semi-trailer IDs
    leftover_ids = [entry["SemiTrailerID"] for entry in leftovers_list]

    #### Computing ASR using SAME simulation_start as GA
    total_npd, npd_left, asr = compute_asr(
        trailers_full,
        leftover_ids,
        simulation_start
    )
    kpis["ASR_total_not_past_due"] = total_npd
    kpis["ASR_not_past_due_leftovers"] = npd_left
    kpis["ASR"] = asr
    ################
    #### Diagnostic check for lateness validity
    print("\nDiagnostic check for lateness validity:")
    print("Total semi-trailers in input:", trailers_df.shape[0])
    print("Serviced semi-trailers in schedule:", sched["SemiTrailerID"].nunique())
    print("Leftovers:", len(leftovers_list))
    
    leftovers = {"Leftovers": leftovers_list}  
    return sched, kpis, leftovers

#### Monte Carlo functions commented as they won't be needed again
#### Monte Carlo simulation has already been conducted. 
'''
def monte_carlo_single_scenario_ga(file_path: str, parking_table: str, equipment_table: str, runs: int = 30, simulation_start: Optional[pd.Timestamp] = None, end_of_day_hour: int = 21, seed_base: int = 1000, params: GAParams = GA_PARAMS) -> pd.DataFrame:
    records = []
    for run_idx in range(runs):
        seed = seed_base + run_idx
        print(f"SEED: {seed}")
        sched, kpis, leftovers = run_scenario_ga(file_path, parking_table, equipment_table, simulation_start, end_of_day_hour, seed, params)
        kpis.update({"Run": run_idx, "Seed": seed})
        records.append(kpis)
    return pd.DataFrame(records)
'''
'''
from concurrent.futures import ProcessPoolExecutor, as_completed
import os
'''
'''
def _run_scenario_montecarlo(args):
    """Helper for parallel scenario-level execution."""
    scenario_name, file_path, parking_table, equipment_table, runs, simulation_start, end_of_day_hour, seed_base, params = args

    print(f"\n▶ Running Monte Carlo GA for {scenario_name} scenario...")
    df = monte_carlo_single_scenario_ga(
        file_path=file_path,
        parking_table=parking_table,
        equipment_table=equipment_table,
        runs=runs,
        simulation_start=simulation_start,
        end_of_day_hour=end_of_day_hour,
        seed_base=seed_base,
        params=params
    )
    df["Scenario"] = scenario_name
    print(f" Completed {scenario_name} scenario ({len(df)} runs)")
    return df

'''
'''
def monte_carlo_multi_scenario_ga(
    files: Dict[str, str],
    parking_table: str,
    equipment_table: str,
    runs: int = 2,
    simulation_start: Optional[pd.Timestamp] = None,
    end_of_day_hour: int = 23,
    seed_base: int = 1000,
    params: GAParams = GA_PARAMS
) -> pd.DataFrame:
    """Parallel Monte Carlo execution for multiple GA scenarios."""
    results = []
    
    #### Sequential Running
    print(f"\n Starting sequential GA Monte Carlo for {len(files)} scenarios...")
    for scenario_name, file_path in files.items():
        print(f"\n▶ Running Monte Carlo GA for {scenario_name} scenario...")
        start_time = time.perf_counter()

        # Run Monte Carlo for this scenario (multiple seeds)
        df = monte_carlo_single_scenario_ga(
            file_path=file_path,
            parking_table=parking_table,
            equipment_table=equipment_table,
            runs=runs,
            simulation_start=simulation_start,
            end_of_day_hour=end_of_day_hour,
            seed_base=seed_base,
            params=params
        )

        df["Scenario"] = scenario_name
        results.append(df)

        runtime_min = (time.perf_counter() - start_time) / 60.0
        print(f" Completed {scenario_name} scenario ({len(df)} runs) in {runtime_min:.2f} min")
    
    ####
    
    #### Parallel processing with ProcessPoolExecutor
    
    args_list = [
        (scenario_name, file_path, parking_table, equipment_table, runs,
         simulation_start, end_of_day_hour, seed_base, params)
        for scenario_name, file_path in files.items()1
    ]
    # Use 4 parallel workers (one per scenario, since you have 4)
    max_workers = min(len(args_list), max(1, os.cpu_count() // 3))

    print(f"\n Launching parallel GA Monte Carlo for {len(args_list)} scenarios "
          f"using {max_workers} workers...")

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_scenario_montecarlo, args): args[0] for args in args_list}
        for future in as_completed(futures):
            scenario = futures[future]
            try:
                df = future.result()
                results.append(df)
            except Exception as e:
                print(f" Scenario {scenario} failed: {e}")
    ####
    
    
    all_df = pd.concat(results, ignore_index=True)
    print("\n All Monte Carlo GA scenarios completed successfully!")
    return all_df
    '''

if __name__ == "__main__":
    from datetime import datetime 
    #FILES = {"Very High" : "VersionXL.xlsx"} #### A single scenario file can also be run like this.
    FILES = json.loads(os.getenv("files"))
    PARK_TBL = os.getenv('PARK_SHEET')
    EQUIP_TBL = os.getenv("EQUIP_SHEET")
    START = pd.Timestamp(os.getenv("START"))
    END = int(os.getenv('END_OF_DAY_HOUR'))
    
    #### Multiple scenario run (runs single scenario as well)
    all_results = []
    for scenario_name, file_path in FILES.items():
        x1 = datetime.now()
        print(f"Start: {x1}")
        print(f"\n Running GA for {scenario_name} scenario...")
        #### The end of day has been set to 21:00 by default (24 h clock system to be followed)- Should be an integer!
        sched, kpis, leftovers = run_scenario_ga(file_path, PARK_TBL, EQUIP_TBL, simulation_start=START, end_of_day_hour= END, seed=50)

        #### Adding the Scenario to schedule
        sched["Scenario"] = scenario_name

        #### Leftovers list-of-dicts : A dataFrame with Scenario and Region
        leftover_rows = []
        for entry in leftovers.get("Leftovers", []):
            leftover_rows.append({
                "Scenario": scenario_name,
                "Region": entry.get("Region"),
                "SemiTrailerID": entry.get("SemiTrailerID"),
                "ParkingSlot": entry.get("ParkingSlot"),
                "DepartureDueDate":entry.get("DepartureDueDate")
            })
        leftovers_df = pd.DataFrame(leftover_rows)

        #### Storing for combined export
        all_results.append({
            "Scenario": scenario_name,
            "KPIs": kpis,
            "Plans": sched,
            "Leftovers": leftovers_df
        })

        #### Individual export
        leftovers_map = defaultdict(list)

        for entry in leftovers.get("Leftovers", []):
            leftovers_map[entry.get("Region") or "Unknown"].append(entry.get("SemiTrailerID"))
        
        #### The following function call (an obsolete design choice) has been commented since all the plans are combined later (outside the loop)
        ''' 
        export_plans_with_kpis(
            all_plans_df=sched,
            leftovers=leftovers_map,
            output_file=f"GA_Plans_With_KPIs_{scenario_name}.xlsx")
        '''
        #### A time counter to keep track of the run duration
        x2 = datetime.now()
        print(f"Time taken: {x2 - x1}")
        

    #### Combining all results into one Excel file with 3 clean sheets
    #### Combining KPI summaries
    kpi_df = pd.DataFrame([r["KPIs"] | {"Scenario": r["Scenario"]} for r in all_results])

    #### Combining Plans and Leftovers across scenarios
    plans_combined = pd.concat([r["Plans"] for r in all_results], ignore_index=True)
    leftovers_combined = pd.concat([r["Leftovers"] for r in all_results], ignore_index=True)

    #### Enforcing the desired logical scenario order across all sheets
    #### Scenario ordering is only relevant when multiple scenarios are present.
    scenario_order = ["Very High", "High", "Medium", "Low"]

    if not plans_combined.empty:
        plans_combined = plans_combined.sort_values(
            ["Scenario", "Region", "Equipment", "SequencePos"],
            key=lambda col: pd.Categorical(col, categories=scenario_order, ordered=True),
            kind="mergesort"
        )
        preferred_plan_order = [
        "Region", "Equipment", "SequencePos",
        "ParkingSlot", "SemiTrailerID", "PriorityCode", "DepartureDueDate",
        "DestSide", "ExpectedServiceMin", "StartTime", "FinishTime",
        "LatenessMin", "PriorityFactor", "WeightedLateness", "Scenario"
        ]
        plans_combined = plans_combined[[c for c in preferred_plan_order if c in plans_combined.columns]]

    if not kpi_df.empty:
        #### Reordering KPI columns for clarity in order to match the results from other algorithms
        preferred_order = [
            "TotalSemiTrailers", "TotalLatenessMin", "MeanLatenessMin", "AverageWeightedLateness",
            "OnTimeCount", "LateCount", "OnTimeRatio",
            "NorthTrainFurnishingTime", "SouthTrainFurnishingTime", "ASR_total_not_past_due", "ASR_not_past_due_leftovers", "ASR", "RuntimeMinutes","Scenario"
        ]
        kpi_df = kpi_df[[c for c in preferred_order if c in kpi_df.columns]]
        kpi_df = kpi_df.sort_values(
            "Scenario",
            key=lambda col: pd.Categorical(col, categories=scenario_order, ordered=True)
        )

    if not leftovers_combined.empty:
        leftovers_combined = leftovers_combined.sort_values(
            "Scenario",
            key=lambda col: pd.Categorical(col, categories=scenario_order, ordered=True)
        )
        expected_cols = ["Scenario", "Region", "SemiTrailerID", "ParkingSlot", "DepartureDueDate"]
        for col in expected_cols:
                if col not in leftovers_combined.columns:
                    leftovers_combined[col] = np.nan
        leftovers_combined = leftovers_combined[expected_cols]

    SQL_INPUT = "MultipleScenarios_GA.xlsx"
    with pd.ExcelWriter(SQL_INPUT, engine="openpyxl") as writer:
        kpi_df.to_excel(writer, sheet_name="KPIs", index=False)
        if not plans_combined.empty:
            plans_combined.to_excel(writer, sheet_name="Plans", index=False)
        if not leftovers_combined.empty:
            leftovers_combined.to_excel(writer, sheet_name="Leftovers", index=False)
    
    #### Pushing the plans to the dbo.SchedulePlans and dbo.ScheduleLeftovers databases
    #### Overwrite = True means that the tables in SQL will be emptied (truncated) before being upserted any plans. 
    #### Upsert = Update + Insert 
    push_plans_excel_to_sql(SQL_INPUT, overwrite=True)
    
    print(f"Exported GA results (KPIs + Plans + Leftovers) to {SQL_INPUT}")
    

#### Monte Carlo section to stay commented, this experiment was run in the past!
    '''
    # Run Monte Carlo for all scenarios (e.g., 50 runs each)
    print("\n Running Monte Carlo GA across all scenarios...\n")
    X4  = datetime.now()
    print(f"START TIME MC:{X4}")
    all_kpi_df = monte_carlo_multi_scenario_ga(
        FILES,
        PARK_TBL,
        EQUIP_TBL,
        runs = 50,
        simulation_start=START,
        end_of_day_hour=21
    )

    # Desired EDD-style column order
    col_order = [
        "Scenario", "Run", "Seed",
        "TotalTrailers", "TotalLatenessMin", "MeanLatenessMin",
        "AverageWeightedLateness", "OnTimeTrailers", "LateTrailers",
        "OnTimeRatio", "NorthTrainFurnishingTime", "SouthTrainFurnishingTime"
    ]

    # Ensure all expected columns exist (fill missing with NaN)
    for c in col_order:
        if c not in all_kpi_df.columns:
            all_kpi_df[c] = np.nan

    # Reorder columns and sort by scenario, run
    scenario_order = ["Very High", "High", "Medium", "Low"]
    all_kpi_df = all_kpi_df[col_order].sort_values(
        ["Scenario", "Run"],
        key=lambda col: pd.Categorical(col, categories=scenario_order, ordered=True)
    )

    # Export to Excel (single sheet, EDD-style)
    output_file = "GA_MonteCarlo_AllScenarios_M_23Nov.xlsx"
    all_kpi_df.to_excel(output_file, index=False, sheet_name="GA_MonteCarlo")
    print(f"END TIME MC:{datetime.now()}")
    print(f"TOTAL DURATION : {datetime.now() - X4}")
    print(f"\n Exported GA Monte Carlo results to '{output_file}'.")
    '''
