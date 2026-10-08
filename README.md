<!--
"""
TEACHERS APP project 
Timetable allocation tool done with flask and AI powered

mrbacco04@gmail.com
Feb 23, 2026 

"""
-->
# TIMETABLE_APP

Version 1 (V1) of a Flask-based timetable allocation tool.

## What V1 includes
- Teacher and skill CRUD
- Weekly timetable UI (Monday-Friday tabs)
- Period grid (8:00-15:00, lunch 12:00-13:00)
- Grade/Class columns (Grade 1 to Grade 5)
- Teacher allocation with conflict prevention
- CSV bulk import for skills and teachers

## Run
1. `python -m venv .venv`
2. `.\\.venv\\Scripts\\activate`
3. `pip install -r requirements.txt`
4. `python app.py`
5. Open `http://127.0.0.1:8000`

## Tests
`python -m unittest discover -s tests`
