# CLO Attainment Report Generator

A Flask app for uploading assessment reports, mapping questions to CLOs, calculating CLO attainment, and exporting formal CSV/PDF reports.

## Local Run

```powershell
pip install -r requirements.txt
python course_report.py
```

The local app starts at `http://127.0.0.1:8092`.

## Render

Use this start command:

```bash
gunicorn course_report:app
```
