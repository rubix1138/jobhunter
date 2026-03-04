# Threat Models

This directory contains threat modeling artifacts for this project.

## Files

| File | Purpose |
|------|---------|
| `<project>-description.md` | System description for `stride-gpt identify` (headless CLI) |
| `<project>.json` | OWASP Threat Dragon v2 Data Flow Diagram model |

---

## Tools

### stride-gpt-cli — Headless STRIDE threat generation

Generates an initial threat list from a system description file.

```bash
# Install (once)
~/scripts/install-threat-tools.sh

# Generate threats from description
stride-gpt identify threat-models/<project>-description.md

# Check API key options
stride-gpt --help
```

Requires `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in your environment.

### STRIDE-GPT Streamlit App — Interactive threat analysis

Provides the full interactive experience: attack trees, DREAD risk scoring,
mitigation suggestions, and test case generation.

```bash
# Launch (opens http://localhost:8501 in browser)
~/scripts/run-stride-gpt.sh
```

Paste your system description from `<project>-description.md` into the app.

### OWASP Threat Dragon — Data Flow Diagram editor

Visual DFD editor. Attach threats to components, create trust boundaries,
export reports.

```bash
# Open an existing model
# Launch Threat Dragon from the desktop/app menu, then:
# File → Open → select threat-models/<project>.json
```

Download: https://github.com/OWASP/threat-dragon/releases/tag/v2.5.0

---

## Workflow

1. **Initial threat generation** (start here for a new project):
   ```bash
   stride-gpt identify threat-models/<project>-description.md
   ```
   Review the output and note the threats that apply to your architecture.

2. **Interactive deep-dive** (DREAD scoring, attack trees, mitigations):
   ```bash
   ~/scripts/run-stride-gpt.sh
   # Paste system description → select Claude → explore threats
   ```

3. **DFD creation** (visual diagram with threats attached to components):
   - Open Threat Dragon desktop app
   - Open `threat-models/<project>.json`
   - Add process nodes, data stores, external entities, and data flows
   - Attach threats from your stride-gpt output to relevant components
   - Save — the JSON file is updated in place

4. **Validate artifacts** (run before pushing):
   ```bash
   ~/scripts/threat-model-check.sh
   ```

5. **CI validation** runs automatically on push/PR via
   `.github/workflows/threat-model-check.yml`. It checks that:
   - `threat-models/` directory exists
   - At least one `*.json` file is present and valid JSON

---

## STRIDE Categories

| Letter | Threat | Example |
|--------|--------|---------|
| **S** | Spoofing | Attacker impersonates a user or service |
| **T** | Tampering | Attacker modifies data in transit or at rest |
| **R** | Repudiation | User denies performing an action |
| **I** | Information Disclosure | Sensitive data exposed to unauthorized parties |
| **D** | Denial of Service | Service made unavailable |
| **E** | Elevation of Privilege | Attacker gains unauthorized capabilities |

---

## Note on placeholder models

The initial `<project>.json` is a minimal placeholder that satisfies the CI
JSON check. It has no DFD nodes or threats yet. Open it in Threat Dragon to
build the full model interactively — the GUI handles node positioning and
edge routing that cannot be done by hand-editing JSON.
