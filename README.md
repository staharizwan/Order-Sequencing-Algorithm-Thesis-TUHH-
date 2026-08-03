# 🎓Master Thesis: **Development of Order Sequencing Algorithms for Semi-trailers in Inland Ports**


## Introduction 📑✒️

**This repository contains implementations of scheduling algorithms that operate on structured Excel input files to generate optimized assignment plans, key performance indicators (KPIs), and leftover job lists. The resulting outputs are persisted in the IGD_state database.**

This master's thesis was conducted with the department of Maritime Logistics at the Hamburg University of Technology (TUHH).

**Duration: 1st September 2025 - 2nd March 2026**

**The scheduling algorithms developed are as follows** :  
1. Greedy-Earliest Deadline First (**greedy_edf.py**)
2. Greedy-Weighted Shortest Processing Time (**greedy_wspt.py**)
3. Genetic Algorithm (**genetic_scheduler.py**)

**All the algorithms have been developed in Python 3.9.13**


## Startup Guide 🚀🚀
1. Clone the repository.
2. `cd root_folder` this is the root directory, where the .env file lives.
3. `venv\Scripts\activate` to activate the virtual environment.
4. `pip install -r requirements.txt` to install all the necessary packages.
5. Create the **.env** file based on **.env.example**.
6. The **.env** file needs to be filled out with all the input parameters and Excel file(s). 
7. `cd Main` to navigate to the folder where algorithms are.
8. `python selected-algorithm.py` to run the algorithm, replace with the name of the desired algorithm.


## Important Note ⚠️⚠️
1. The connection with the SQL database (IGD_state) is mandatory for the data to pushed into the database (discussed in the repository: **dbms-backend-main**).
2. The algorithms (the files mentioned in the previous section) **should** stay in **root/Main**.
3. The algorithms have been tested with 4 levels of port occupancies that are as follows:
4. The algorithms have been tested with four scenarios of port occupancy:
   - **VersionXL.xlsx** (Very high occupancy)
   - **VersionL.xlsx**  (High occupancy)
   - **VersionM.xlsx**  (Medium occupancy)
   - **VersionS.xlsx**  (Low occupancy)
5. The file **scheduler_core.py** only contains a few helper functions; besides that, the file was only used in the Monte Carlo variance analysis:
6. **scheduler_core.py** should not be used in the future for scheduling.
7. The 4 Excel files mentioned above have been used to test the algorithms; the parameters were maintained consistent across algorithms to ensure a fair experiment.
8. The **tabular structure**, **location** (inside Project/Main) and the **data format** as shown in the scenario files (input Excel files) is to be abided by in order for the algorithms to deliver effectively.
9.  The simulation start and end time **must** be in the 24-hour clock system.

## Guidelines for Filling the Input Excel File(s) and Reading the Output File✏️📝
1. Any row with an empty parking slot ID will be dropped.
2. For every semi-trailer that needs to be exported, must have 1 in the Export column and an "X" or "Y" in the destination column. The Destination mapping can be seen in the .env.example.
3. Any region besides North, South and East must not be used at all. Alberthafen has been divided into 3 regions within the framework of this thesis.
4. The due date format is to be abided by!
5. Priority column can be disregarded since it is being calculated based on the departure due dates.
6. All the semi-trailers with a corresponding value of 1 in the the Export column will be considered for scheduling; their corresponding destination columns must not be left empty for them.
7. The result-bearing Excel file(s) have 3 sheets, "Plans", "KPIs" and "Leftovers".
8. The column names in the result-bearing file(s) have been made self-explanatory; e.g. "SequencePos" is the sequence in which a particular semi-trailer will handled by the mentioned equipment.
9. The **Plans** sheet contains the all the generated plans (orders).
10. The **Leftovers** sheet contains the semi-trailers that were not scheduled inside the available scheduling window (the .env.example contains all the variables needed to run the algorithms).
11. Plans can be easily read by filtering the equipment(s) and scenario(s) of choice, **SequencePos** helps with the sequence determination. 


## Project Structure 📁
```
Project/
   ├── .env
   ├── .gitignore
   ├── README.md
   └── Main/
         ├── genetic_scheduler.py
         ├── greedy_edf.py
         ├── greedy_wspt.py
         ├── VersionXL.xlsx
         ├── VersionL.xlsx
         ├── VersionM.xlsx
         ├── VersionS.xlsx
         └── Terminal_DB/
                  ├── MS_SQL_Server.py
                  └── sql_exporter.py

```