# Sample Data — Palantir Foundry OAA Integration

This directory holds anonymised sample data used for local dry-run testing.

## Why samples?

The integration pulls live data from Palantir Foundry at runtime. For dry-run
testing without live credentials, place representative JSON fixtures here.
The dry-run tester reads from `--data-dir` (defaults to this directory).

## Files to place here

The integration script does **not** read flat files — it calls the Foundry REST
API directly. To run a payload-only dry-run without live API access, you must
mock the data at the Python level or run with valid credentials and
`--dry-run --save-json`.

For reference, below are the shapes of the objects the integration collects.

---

### users.json (optional fixture — list of user objects)
```json
[
  {
    "id": "usr-0001",
    "username": "jane.doe@example.com",
    "email": "jane.doe@example.com"
  },
  {
    "id": "usr-0002",
    "username": "john.smith@example.com",
    "email": "john.smith@example.com"
  }
]
```

### groups.json (optional fixture — list of group objects)
```json
[
  {
    "id": "grp-0001",
    "name": "data-engineers",
    "displayName": "Data Engineers"
  },
  {
    "id": "grp-0002",
    "name": "analysts",
    "displayName": "Analysts"
  }
]
```

### spaces.json (optional fixture — list of space/workspace objects)
```json
[
  {
    "rid": "ri.compass.main.folder.space-001",
    "displayName": "Engineering Space",
    "type": "COMPASS_FOLDER"
  }
]
```

### datasets.json (optional fixture — list of dataset objects)
```json
[
  {
    "rid": "ri.foundry.main.dataset.ds-001",
    "displayName": "Customer Orders",
    "type": "FOUNDRY_DATASET",
    "rowCount": 150000
  }
]
```

---

## Recommended workflow

1. Run with real credentials and `--dry-run --save-json` to capture a live payload:
   ```bash
   ./venv/bin/python3 palantir_foundry.py --dry-run --save-json --log-level DEBUG
   ```
2. The saved JSON (`palantir-foundry_oaa_payload.json`) can be inspected directly.
3. Anonymise and commit representative fixture files here for CI/testing purposes.
